# Forge state mirror: operator reference

> **Removed 2026-10-19 (#12809).** The per-host dashboard event feed client, its early-tick consumers
> (`forgeEvents.events.*`), feed-based poll gating (`forgeEvents.pollGating`)
> and the `forge_events.*` status block no longer exist. Every loop polls on its
> own timer. A config that still carries `forgeEvents`, or an environment that
> still sets `LOOM_FORGE_EVENTS_*`, is ignored; the daemon logs one startup
> warning naming them. History: ADR-0021 (superseded for the feed by this
> ruling).
>
> What remains is the **state mirror** below: discovery reads and the
> change-stream pull with its cursors.

## The state mirror (`forge.mirror`): discovery reads and the change stream

A **state mirror** is a second piece of operator infrastructure, separate
from the removed per-host feed. It keeps, per repo, the issues and PRs the
fleet asks about (state, labels, author, body, sub-issue counts, head and
base) current from GitHub App webhooks, corrects what webhooks miss with a
slow reconcile, and stamps every answer with the repo's **freshness
watermark**: `last_event_at`, `last_reconciled_at` and `served_at`. It also
serves a per-repo, sequence-numbered **change stream**. The daemon uses it
for two things, both off unless configured. Neither wakes a loop or gates a
poll.

```json
"forge": { "mirror": {
  "origin": "https://events.example.com",
  "keyRef": "ssm:///events/keys/host-a",
  "region": "example-region-1",
  "maxStalenessSecs": 600,
  "changePollSecs": 30
} }
```

- **`origin`**: `https://<host>` or a bare host, never a GitHub host.
- **`keyRef`** (`key_ref` is accepted too): `ssm://<parameter>` (read with
  the AWS CLI and an explicit region: `region`, else `AWS_REGION`, else
  `AWS_DEFAULT_REGION`) or `file:<path>`, as for `forge.gateway`
  ([`github-authentication.md`](github-authentication.md)). The key is held
  in memory and sent only as `Authorization: Bearer`.
- **`awsProfile`** (optional): as for `forge.gateway`. Unset, an `ssm://`
  read runs as the host's instance role; set, it names a profile in the
  default `~/.aws` files; invalid, `ssm://` reads are refused.
- **`maxStalenessSecs`** (default 600, clamped to 30-900): the discovery
  bound. 900 is this ADR's hard staleness cap.
- **`changePollSecs`** (default 30, clamped to 10-300): the change-stream
  pull cadence.

Put a fleet-wide value in the fleet tier and a canary in one host's tier.
The daemon re-reads the block every minute and re-reads the key every five
minutes, or at once after a `401`.

### 1. Discovery reads

Discovery reads the mirror once per step tick (§1.1). The per-listing mirror
leg that used to precede each ETag-cached REST listing (`GET /v1/mirror/items`
per label, per repo, per caller, with a two-minute stale hold and a 60-second
error back-off) was removed in #12307: a configuration that selected it
(`forge.mirror.discovery = "per_call"`, formerly the default) is accepted and
now lists per repo exactly as `"direct"` does.

The tick's one snapshot also feeds the other loops that used to list the forge
on their own, so on `"worklist"` they make no listing of their own: the
blocked-queue gauge (`loom:blocked` rows), quarantine reconciliation's blocked
set and claim reconciliation's open-PR listing. A repo the snapshot has not
read is a failed listing for them, never an empty one, and a stale repo is
served as it is: what those loops write stays gated on live reads of their own
(the per-issue comment and timeline scans, a PR's mergeability and state).
Discovery forge reads per tick are 0 in this mode (`loom.discovery.forge_reads`
on the tick span).

An idle repo whose last event and reconcile are both older than
`maxStalenessSecs` is served anyway and counted in `loom.discovery.stale_repos`.

**Decision reads never come from the mirror.** Claim, label-transition and
merge reads stay on their current path (ADR-0021's invariant 3: reads
that gate a decision are never held). A source test pins the modules that name
the mirror or the snapshot.

### 1.1 The worklist and the discovery chain (`forge.mirror.discovery`, #12311)

`"worklist"` is **one read per step tick**: `GET /v1/mirror/worklist`, which returns, per repo, a `status`
(`ok` | `unknown` | `backfilling` | `error`), the watermark, and every open
issue and PR with the fields discovery uses. It is ETagged, so an unchanged
fleet costs a `304`; the daemon keeps one in-memory `(etag, body)`, replaced
every tick, dropped on restart, never persisted and never merged with the
change stream.

The daemon walks a chain from the first tier and records the tier that
answered (`loom.discovery.tier`):

| Tier | Used when | How |
|---|---|---|
| `mirror` | first | the worklist, for every repo whose `status` is `ok` |
| `gateway` | the request failed, or a repo's `status` is not `ok` | the direct per-repo listing through the read gateway; only the repos the mirror could not answer for |
| `gh` | standalone always; on the fleet when the gateway also fails | the same listing with the installation's credential |
| `none` | every configured tier failed | no draw from that repo this tick; running sessions are unaffected |

No `forge.mirror` block means the chain is `gh` alone; `"direct"` skips the
mirror (break-glass). On the fleet the fallback tiers are outage modes and
are capped: a repo is listed at most once per work-finder interval and drawn
from its last listing in between; a gateway reserve refusal ends the tick's
discovery without tripping the rate-limit breaker; and there is no writer
tier. Claim, label-transition and merge reads never read the snapshot.

#### Standalone Loom (#12312)

One daemon outside any fleet, on GitHub or Gitea, is a first-class mode and
the default. With no `forge.mirror` block the chain is `[gh]`: the **direct
backend** lists each served repo once per tick with the installation's own
credential. There is no mirror request, no gateway request, no fleet config
and no second App. The tick span says `loom.discovery.tier = gh`, or `none`
when no repo could be read, and a failed listing serves the last-good copy
for `LAST_GOOD_TTL`.

The backend is forge-neutral at the interface and forge-specific underneath.
GitHub is one `/issues` walk (it includes PRs) plus `/pulls`. Gitea is
`/issues?type=issues` and `/issues?type=pulls` plus `/pulls?state=open`,
paged by `limit=50`. The draw needs number, kind, state, labels and a PR's
head SHA; a field a forge cannot give is `None` (`labels_at`, sub-issue and
blocked-by counts, `author_association`, the check summary) and the
classifier tolerates it. The landed pre-filter's GraphQL stays GitHub-only.
A response without an `ETag` is never cached, so it is never a stale hit.

On the fleet the same backend is the `gateway` and `gh` tiers. The fleet's
`gh` tier runs in an isolated `gh` environment (its own `GH_CONFIG_DIR`,
`LOOM_DISCOVERY_GH_CONFIG_DIR`, default `~/.loom/discovery-gh`, with every
token variable removed), so it never borrows an App profile and fails closed
on a host that has not logged in there.

### 2. Change stream

While a mirror is live, the daemon pulls `GET /v1/changes?repos=…&after=…`
for the repos it serves (its workspace and every provisioned root) every
`changePollSecs`. It keeps its own cursor per repo in
`$HOME/.loom/forge-mirror/cursors.json`, for one origin, and records the
change-to-pull latency.

- A repo with no cursor is baselined at its latest change, so a restart never
  replays history.
- A cursor the stream truncated, or one past the repo's latest change (the
  mirror's log was reset), re-baselines.

The pull publishes no bus event, ticks no loop early and holds back no poll:
polling runs at its configured cadence whatever the stream does, so a dead
stream costs telemetry only. The cursors and the pull are kept for the fleet
captain's future use (#12810).

### 3. Measuring it

- `loom.forge.mirror.reads{caller, repo, outcome}`: `outcome` is `hit`,
  `stale`, `unknown`, `error` or `skipped`. Every non-`hit` fell back to the
  forge. `caller = wake` counts change-stream requests (`repo = -`).
- `loom.forge.mirror.changes{repo, kind}` and
  `loom.forge.mirror.wake_latency{repo}` (summed seconds from a change at the
  mirror to the pull that saw it; divide by `changes` for the mean).

**Stale draws report back (#12318).** In worklist mode, a claim refused because
the live read disagreed with the drawn snapshot (`claimed`, or a
`label_flip_failed` whose item moved; never `claim_lost` or a host-side
refusal) makes the step driver `POST /v1/mirror/recheck {repo, number}`, so
the mirror re-reads that one item. The daemon is a polite client: the call is
fire-and-forget off the tick, deduplicated per `(repo, number)` per tick,
never retried, and capped by a local token bucket at the mirror's per-caller
default (60 an hour), so an excess is dropped on the host. It carries no state
and nothing depends on it having happened; the refusal cooldown still applies.
`loom.discovery.recheck{reason, outcome}` counts them (`outcome` = `sent`,
`error` or `skipped`); the refused-for-staleness count per host is the
daemon-side health measure for fleet discovery.

That discovery no longer reaches GitHub shows as `loom.forge.calls` for
`caller` `work_finder` falling while `reads{outcome=hit}`
rises.

**Break-glass.** Unset `forge.mirror`. Within a minute no listing asks the
mirror and no change stream is pulled; every read is exactly what it was
before. The fleet store fetch never depends on the mirror.

## See also

- [ADR-0021](https://github.com/rjwalters/loom/blob/main/docs/adr/0021-forge-event-plane.md) — the decision and its
  alternatives (the feed half is superseded) · [ADR-0014](https://github.com/rjwalters/loom/blob/main/docs/adr/0014-forge-coordination-decoupling.md) — the invariants
- [`observability.md`](observability.md) — the sibling operator-deployed
  backend, same shape
- [`credential-storage.md`](credential-storage.md) — where keys may live
- [`network-inputs.md`](network-inputs.md) — where the mirror sits among every network input to the daemon (row 7a)
- Source: `loom-daemon/src/forge_mirror.rs` and `forge_mirror/`
