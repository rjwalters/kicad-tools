# Forge event plane: operator reference

> [ADR-0021](https://github.com/rjwalters/loom/blob/main/docs/adr/0021-forge-event-plane.md), epic
> [#8764](https://github.com/rjwalters/loom/issues/8764). This is the
> operating reference for the **daemon half** — the `forgeEvents` config
> block, what the poll loop does, what it writes, and how to read its status.
> The Worker half is operator infrastructure and lives outside this
> repository; see §7.

**Default state: off.** A daemon with no `forgeEvents` block does not poll,
does not create a directory, does not read a key, and reports
`forge_events.state == "disabled"` on `loom-daemon status --json`. Its
external behaviour is identical to a pre-ADR-0021 daemon.

## 1. The pipeline, in one picture

```
GitHub App webhooks (operator's fleet-dispatch App)
        │  HMAC-verified, repo-allowlist classified
        ▼
operator's Webhook Worker + Durable Object  (NOT in this repo)
        │  GET /v1/hosts/{host}/events?after={cursor}&limit={page}
        │  -> { host_id, cursor, clamped, events[], has_more }
        ▼
loom-daemon forge_events (per host, opt-in)
        │  poll -> journal page -> persist cursor -> publish
        ▼
in-process EventBus topic `forge.event`   (summary payload only)
        │
        └── Phase 2 consumers (#8766) — none yet in Phase 1
```

**The feed never replaces polling.** It is additive prompt pressure: every
existing poll cadence runs unchanged, and a permanently dead feed costs
latency only. That is invariant 2 of ADR-0021, and it is why this can be
rolled out one host at a time.

## 2. Configuration

Add a `forgeEvents` block to `.loom/config.json` (or, for host-specific
values, `.loom-local/local.json`). Precedence is **env > config > default**,
the same as every other daemon subsystem.

| Key | Env override | Default | Meaning |
|---|---|---|---|
| `enabled` | `LOOM_FORGE_EVENTS_ENABLED` | `false` | Opt in. |
| `endpoint` | `LOOM_FORGE_EVENTS_ENDPOINT` | *(none)* | Feed base URL. The daemon appends `/v1/hosts/{hostId}/events`. |
| `hostId` | `LOOM_FORGE_EVENTS_HOST_ID` | *(none)* | The id the operator minted this host's key against. |
| `eventKeyFile` | `LOOM_FORGE_EVENTS_EVENT_KEY_FILE` | `$HOME/.loom/forge-events/key` | File holding the per-host bearer key. |
| `pollIntervalSecs` | `LOOM_FORGE_EVENTS_POLL_INTERVAL_SECS` | `10` | Cadence when healthy. |
| `pageSize` | `LOOM_FORGE_EVENTS_PAGE_SIZE` | `100` | Events requested per poll. |

```json
{
  "forgeEvents": {
    "enabled": true,
    "endpoint": "https://events.your-operator-domain.tld",
    "hostId": "mac-studio",
    "pollIntervalSecs": 10,
    "pageSize": 100
  }
}
```

**The block is deliberately not committed to this repo's own
`.loom/config.json`.** A committed placeholder endpoint is the #7815 exposure
— and the daemon refuses reserved placeholder domains (`example.com` and the
other RFC 2606/6761 names) outright, *before* it opens the key file, so a
copy-pasted sample can never send a real key to a documentation domain.

`hostId` has **no default**. It is not `$LOOM_HOST_ID` and not the hostname:
it is whatever identity the operator minted this host's key against, and
guessing it would turn a provisioning mistake into a permanent, quiet
`host_mismatch` instead of a named `misconfigured`.

### Provisioning a host

1. Ask the operator to mint a key for this host's id (the Worker stores only
   `SHA256(key) → host_id`; adding a host is a key mint, not a Worker change).
2. Place it at `$HOME/.loom/forge-events/key`, owner-read-only. **Never** put
   the key itself in any config file, and never in a repository — see the
   [credential policy](credential-storage.md).
3. Set `enabled`, `endpoint` and `hostId`.
4. Restart the daemon and check `loom-daemon status`.

**Rotation does not need a restart.** The key file is re-read on *every*
poll, so a rotation is a file swap.

## 3. What the daemon writes

Everything lives under `$HOME/.loom/forge-events/`:

| File | Purpose |
|---|---|
| `key` | The per-host bearer key (you provision it; the daemon only reads it). |
| `state.json` | The durable cursor, written atomically (temp file + rename). |
| `journal.jsonl` | One JSON line per non-empty page, capped at 10 MiB. |
| `journal.1` | The single rotation target: the most recent *full* journal. |

Both the journal and the cursor are **diagnostic**: a write failure is logged
and the page still advances. A restart then re-renders from the last durable
cursor, which is still inside the feed's retention window — and a re-rendered
page is a duplicated *prompt*, which is harmless because the forge is
authoritative.

A `state.json` that is absent, torn, or belongs to a different `host_id` reads
as cursor `0` — the start of the retention window — rather than as an error.

### Journal line shape

```json
{"at":"2026-09-23T12:00:00+00:00","host_id":"mac-studio","after":41,
 "cursor":43,"count":2,"clamped":false,"has_more":false,"events":[ … ]}
```

The `events` array is the feed's payload verbatim, for debugging. It is local
diagnostics only — it is **not** what goes on the bus (§4).

## 4. The `forge.event` bus topic

One `Event::Generic { topic: "forge.event", payload }` per **non-empty** page.
An empty page is a successful poll and publishes nothing.

```json
{"source":"forge-event-feed","host_id":"mac-studio","count":2,
 "first_seq":7,"last_seq":8,"types":["issues","pull_request"]}
```

Routing hints only: no repo, no issue or PR number, no title, no actor. A
subscriber that wants forge state has to go ask the forge — which is ADR-0014
invariant 1 made structural rather than merely intended.

**In Phase 1 there is no subscriber.** The topic is published and nothing
consumes it, so a host with the feed on behaves identically to one with it off
apart from the journal it writes and the status it reports. The consumers are
Phase 2 (#8766).

## 5. Reading the status

`loom-daemon status` always prints a `Forge events:` line, and
`loom-daemon status --json` always carries a `forge_events` object for a
daemon of this vintage (`null` there means a pre-ADR-0021 binary — never
"nothing happened"):

```bash
loom-daemon status --json | jq -e '.forge_events.state == "healthy"'
loom-daemon status --json | jq '.forge_events | {state, cursor, last_error}'
```

| `state` | Meaning | What to do |
|---|---|---|
| `disabled` | No block, or `enabled: false`. | Nothing. This is the default. |
| `misconfigured` | Opted in, but something did not resolve. `last_error_detail` names it. | Fix the named piece; the loop never started. |
| `connecting` | Running, first poll not finished. | Wait one cadence. |
| `failing` | Transport error, or a non-2xx that is not 401/403/404, or an over-cap / unparseable page, or a backwards cursor. | Check reachability and `last_error_detail`. |
| `auth_failed` | The feed answered 401/403, **or** the key file became unreadable/empty. | Re-provision or fix the key file. |
| `host_mismatch` | 404 for our host, or a 200 echoing a different `host_id`. Cursors from such a response are never applied. | Reconcile `hostId` with the id the key was minted against. |
| `backoff` | Any of the three above, sustained for 3 consecutive polls; the cadence has stretched to 5 minutes. `last_error` still names the class. | Same as the underlying class. One success restores both state and cadence. |
| `healthy` | The last poll succeeded — including a legitimately empty page. | Nothing. A quiet feed is the steady state. |

None of these are correctness faults. The polling floor keeps running under
every one of them; a red feed costs latency, not consistency.

## 6. Security properties

- **The key is sent only as `Authorization: Bearer`,** on requests to the
  configured endpoint. It appears in no log line, no error string, and on no
  status surface — only the key *file path* is ever named.
- **Redirects are disabled.** A redirect is an instruction from the network to
  re-send that header somewhere unconfigured; a 3xx is reported as a protocol
  failure instead of followed.
- **Reads are bounded at 256 KiB and over-limit is refused, never truncated.**
  A truncated page is a silently incomplete page, and applying its cursor
  would skip whatever did not fit. The cursor is left untouched so the next
  poll re-requests the same range.
- **The endpoint may not carry credentials, a query, or a fragment,** and may
  not be a reserved placeholder domain.
- **`hostId` must be URL-safe** (letters, digits, `-`, `_`, `.`). Anything
  else is refused rather than percent-encoded.
- **Nothing here writes to the forge.** No labels, no claims, no comments, no
  merges — a hostile or broken feed can at worst make this daemon re-query
  GitHub more often than necessary.

## 7. The Worker half (operator-side)

Not in this repository, by design — the same posture as the telemetry backend
(§4 of [`observability.md`](observability.md)): infrastructure *you* deploy
and point your own daemons at. It is a different GitHub App, HMAC secret and
key table than the telemetry Worker. Nothing in Loom carries an operator URL,
App id, or key.

What it must do to satisfy this consumer:

- verify the GitHub webhook HMAC and classify deliveries against a repo
  allowlist (`org/repo` membership only — never issue or PR numbers);
- serve `GET /v1/hosts/{host}/events?after={cursor}&limit={page}` behind a
  per-host bearer key, answering `{ host_id, cursor, clamped, events[],
  has_more }`;
- **echo `host_id`** on every 200 — the daemon refuses any page that does not
  match, including one with no `host_id` at all;
- return a **monotonically non-decreasing** `cursor`, and set `clamped` when
  the requested `after` fell outside the retention window;
- answer 401/403 for a bad key and 404 for an unknown host.

## See also

- [ADR-0021](https://github.com/rjwalters/loom/blob/main/docs/adr/0021-forge-event-plane.md) — the decision and its
  alternatives · [ADR-0014](https://github.com/rjwalters/loom/blob/main/docs/adr/0014-forge-coordination-decoupling.md) — the invariants
- [`observability.md`](observability.md) — the sibling operator-deployed
  backend, same shape
- [`credential-storage.md`](credential-storage.md) — where keys may live
- Source: `loom-daemon/src/forge_events.rs`
