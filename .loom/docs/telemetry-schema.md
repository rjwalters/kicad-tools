# Fleet Telemetry Schema (wire format)

> Epic #4702, Phase 1 — the versioned telemetry record schema the fleet
> observability pipeline is built on. Defined in Rust in
> `loom-daemon/src/telemetry/` (`mod.rs` — record kinds + envelope;
> `visibility.rs` — the repo-visibility derivation helper). This document is the
> **format-independent reference** so the Phase-2 Workers/TypeScript backend can
> parse the wire format without the Rust types.

This document defines the schema + serialization only. **#4704 (below) is the
first consumer that actually persists records** — a durable, append-only local
journal of `sweep.outcome` records, with a `loom-daemon` CLI read surface, no
exporter or cloud backend required. #4705 (still schema-only as of this
writing) will additionally push these records to a cloud backend.

## Envelope

Every record is transmitted inside a versioned envelope:

```json
{
  "schema_version": 1,
  "emitted_at": "2026-07-30T12:00:00Z",
  "host_id": "fleet-host-abc",
  "record": {
    "kind": "sweep.outcome",
    "...": "record fields, flattened alongside `kind`"
  }
}
```

| Field            | Type              | Notes |
|------------------|-------------------|-------|
| `schema_version` | integer (`u32`)   | Current value: **1** (`CURRENT_SCHEMA_VERSION`). |
| `emitted_at`     | RFC 3339 datetime | When the daemon produced the envelope. |
| `host_id`        | string            | Stable identifier for the emitting host. Opaque to the schema. |
| `record`         | object            | The record payload, internally tagged on `kind` (see below). |

### `schema_version` semantics

`schema_version` is a **plain integer**, not a semver string, deliberately: a
backend ingesting a mixed-version fleet (some hosts on an older daemon mid
rolling-upgrade) gates on a numeric compare, with no semver parsing. It is bumped
only on a **breaking** wire change to the record shapes below. A backend should:

- **accept** records at any `schema_version` it recognizes;
- treat an **unknown (higher)** `schema_version` as forward-compatible where it
  can (unknown fields are additive) or route it to a dead-letter path otherwise;
- **never** silently coerce a missing `schema_version` to `0` — a record with no
  `schema_version` is malformed.

## `/ingest` response (the bound-`host_id` echo)

A push is a bare JSON array of envelopes; the backend answers a **2xx with a
JSON object**:

```json
{ "accepted": 50, "host_id": "fleet-host-abc" }
```

| Field      | Type    | Notes |
|------------|---------|-------|
| `accepted` | integer | How many envelopes from this batch were persisted. Whole-batch semantics: a batch is either fully accepted or rejected with a non-2xx. |
| `host_id`  | string  | **The host id the authenticated ingest key is bound to** — i.e. the identity the batch's rows were actually filed under. Added by issue #4830. |

`host_id` here is *not* echoed from the request. Every record is persisted
under the identity bound to the presented key, never the envelope's own
(client-supplied, opaque) `host_id` field — so this echo is what a host was
actually recorded as, which is exactly the value that differs when the wrong
host's key file has been installed on a machine.

**How the exporter uses it.** `loom-daemon`'s exporter compares this value
against the identity the daemon resolved for itself (`$LOOM_HOST_ID`, else
`$HOSTNAME`, else `hostname`) and on a disagreement logs a WARN **once per
daemon lifetime** and reports an `observability DEGRADED` section in
`loom-daemon health`. Nothing about the export changes: the batch stays acked
and the backend keeps filing under the key's binding, which remains
authoritative. See `dashboard/docs/deploy-runbook.md` §8.

**Compatibility.** The field is purely additive — no `schema_version` rev is
involved (that integer versions the *record* envelope, not this response).
Both directions are safe:

- an exporter that ignores the response body behaves exactly as before;
- a backend that does not send `host_id` (anything predating #4830) is treated
  by the exporter as "no identity to verify" and is **silently** skipped — it
  never produces a recurring "cannot verify" warning.

## `RepoVisibility` contract — private by default

Every record that references a repository carries a `visibility` tag, either
`"public"` or `"private"`. The Phase-2 public view exposes full detail for
`public` work and only redacted/summarized aggregates for `private` work, so this
tag is the schema-level anti-leak control.

**The decode is private-safe by construction.** The Rust deserializer maps *only*
the exact (case-insensitive) string `"public"` to `Public`; **everything else**
maps to `Private`:

- a missing `visibility` field ⇒ `Private`;
- an unknown label (e.g. `"internal"`) ⇒ `Private`;
- a `null`, a wrong-typed scalar (bool/number), or a nested array/object ⇒
  `Private`.

A partial or older-schema record can therefore **never accidentally decode to
`public`** and leak into the public view. The Phase-2 backend MUST implement the
same rule: default anything that is not exactly `"public"` to private. Redaction
keys off this tag, never off client-side filtering.

Visibility is derived at emit time from the forge (`gh api repos/{owner}/{repo}
--jq .private`) and cached per `owner/repo` with a TTL, so it costs no per-record
API call. A probe failure resolves to `private` — the same fail-safe default.

## Record kinds

The `record` object is internally tagged on `kind`. The tag values reuse the
frozen SSE `sweep.*` topic vocabulary where they overlap, plus the epic's added
kinds. Records that reference a repository carry `repo` + `visibility`; host-level
records (`tokens.snapshot`, `host.health`) do not.

### `sweep.started`

A sweep began work on an issue.

```json
{
  "kind": "sweep.started",
  "repo": "rjwalters/loom",
  "visibility": "public",
  "issue": 4703,
  "sweep_id": "sweep-issue-4703-0",
  "started_at": "2026-07-30T12:00:00Z",
  "model": "opus",
  "effort": "high"
}
```

`model` and `effort` are omitted when unset (empty-means-unset, mirroring
`SweepInfo`).

### `sweep.phase`

A sweep advanced to a new lifecycle phase (mirrors `sweep.issue.{N}.phase`).

```json
{
  "kind": "sweep.phase",
  "repo": "rjwalters/loom",
  "visibility": "public",
  "issue": 4703,
  "sweep_id": "sweep-issue-4703-0",
  "phase": "builder",
  "entered_at": "2026-07-30T12:03:20Z"
}
```

`phase` is a lifecycle name: `curator`, `builder`, `judge`, `doctor`, `merge`.

**Source and cadence (#4863)**: `Event::SweepPhase` — the record's only upstream —
is published by `SweepRegistry::sample_phase_transition`, which the reaper calls
once per live sweep per `reap_once` tick (the 30s reaper timer, plus every
read-path reap). It reads `.loom/sweep-checkpoint/issue-<N>.json` and publishes
**only when the phase changed since the previous tick**, so a sweep sitting in a
phase emits nothing further. The checkpoint's raw marker (`curator-done`,
`judge-rejected`) is normalized to the lifecycle vocabulary above before it is
published; an unrecognized marker passes through verbatim rather than being
guessed at, so `phase` should be read as "one of the five names, or an unknown
raw marker" — which is exactly how the dashboard types it
(`SweepPhaseName | string`).

Because the phase is polled rather than pushed, `entered_at` is the daemon's
*observation* instant, an honest upper bound on the real transition time.

### `sweep.completed`

A sweep reached a terminal state (the summary moment; richer detail is in the
paired `sweep.outcome`).

```json
{
  "kind": "sweep.completed",
  "repo": "rjwalters/loom",
  "visibility": "public",
  "issue": 4703,
  "sweep_id": "sweep-issue-4703-0",
  "completed_at": "2026-07-30T12:08:32Z",
  "result": "success"
}
```

`result` is one of `success`, `failure`, `cancelled`, `blocked`.

### `sweep.outcome`

The full post-hoc outcome: model/config/effort, per-phase durations, terminal
result, PR number, and (Issue #5357) the sweep's work-output — tokens
processed and lines changed. (A distinct type from the daemon's internal
`sweep_outcomes::OutcomeRecord`, which #4704 maps this into for its journal.)

```json
{
  "kind": "sweep.outcome",
  "repo": "rjwalters/loom",
  "visibility": "public",
  "issue": 4703,
  "sweep_id": "sweep-issue-4703-0",
  "model": "opus",
  "effort": "high",
  "config": { "runtime": "claude" },
  "phase_durations": [
    { "phase": "curator", "duration_sec": 12 },
    { "phase": "builder", "duration_sec": 340 }
  ],
  "total_duration_sec": 512,
  "result": "success",
  "pr_number": 4710,
  "tokens_in": 48213,
  "tokens_out": 6120,
  "lines_added": 214,
  "lines_deleted": 37
}
```

`config` (free-form string map), `phase_durations`, `model`, `effort`,
`pr_number`, `tokens_in`, `tokens_out`, `lines_added`, and `lines_deleted` are
omitted when empty/unset. `config` is a map — not fixed fields — so
operator-tunable knobs can be captured without a schema bump.

#### Work-output fields (Issue #5357)

Four **independently optional** fields, each omitted (never coerced to `0`)
when unavailable — a sweep with no PR carries no LOC pair; a sweep whose
Claude Code transcripts were pruned/rotated before capture carries no token
pair. The two pairs are unrelated to each other, so a record can carry
either, both, or neither.

| Field | Source | Notes |
|---|---|---|
| `tokens_in` | Sum of `input_tokens` + `cache_read_input_tokens` + `cache_creation_input_tokens` across the sweep's own Claude Code transcripts (parent session + every subagent). | **Raw**, not cost-weighted — this record already carries `model`, so a consumer applies whatever per-model pricing table it wants without a backfill when that table changes. |
| `tokens_out` | Sum of `output_tokens` across the same transcripts. | Kept separate from `tokens_in` — input and output tokens price very differently per model, so a cost-weighted total needs both counts plus `model`, not one pre-mixed number. |
| `lines_added` | `git diff --numstat` between the worktree's `HEAD` and its mainline merge base, summed. | **Local only — never a forge API call.** Sampled opportunistically while the worktree is still live (so a `--merge`-mode sweep's own synchronous post-merge worktree cleanup, which can complete before the daemon ever observes the sweep's process exit, does not erase it), with a live-probe fallback at outcome-write time for a sweep that died before any sampling tick. |
| `lines_deleted` | Same `git diff --numstat`, deletions side. | Kept as a separate field from `lines_added` (not a net) — a large refactor that adds and deletes a similar line count is not "no work done". |

Neither pair is added to the public (unauthenticated, private-repo) redaction
allowlist — like `pr_number`, they are workload detail about a private repo
and stay behind the same authenticated-only boundary (see
`dashboard/src/redaction.ts`).

### `tokens.snapshot`

A point-in-time view of the multi-account token pool (host-level — no `repo` /
`visibility`). Matches what `loom-daemon tokens check --ranking` knows.

```json
{
  "kind": "tokens.snapshot",
  "captured_at": "2026-07-30T12:00:00Z",
  "accounts": [
    {
      "account": "agent-1",
      "rank": 0,
      "usage_fraction": 0.42,
      "limit_window_reset_at": "2026-07-30T18:00:00Z",
      "exhausted": false
    },
    { "account": "agent-2", "exhausted": true }
  ]
}
```

Per account, `rank` / `usage_fraction` / `limit_window_reset_at` are omitted when
unknown; `exhausted` is always present.

Every field is read out of the pool's `.ranking` file, so each maps to one of its
pipe-delimited columns (`name|status|5h_util|limit_reset` — see
[`token-pool.md`](token-pool.md)): `rank` is the row's position, `usage_fraction`
is `5h_util`, `exhausted` is derived from `status`, and `limit_window_reset_at`
is `limit_reset`.

`limit_window_reset_at` is the instant the window **currently gating that
account** rolls over — the 7-day window for an `exhausted` account (when it
regains capacity), the 5-hour window otherwise (the rollover `usage_fraction` is
racing). The daemon resolves which one before writing, so a consumer never has to
know: it is always "when this account's constraint lifts". It is also the only
per-account field here that survives public redaction, aggregated across the pool
into `next_limit_window_reset_at` (the earliest reset, naming no account). A row
whose reset is absent or unparseable reports no reset at all rather than a
fabricated instant, so consumers must treat `null`/absent as *unknown* — never as
"resets now".

### `host.health`

Host CPU/disk headroom, the emitting binary's identity, and uptime (host-level —
no `repo` / `visibility`). Every measured field is optional so an unmeasurable
probe stays absent rather than being coerced to a fake zero (the daemon's
"unknown != zero" contract; see `cpu_headroom.rs` / `disk_headroom.rs`).

```json
{
  "kind": "host.health",
  "captured_at": "2026-07-30T12:00:00Z",
  "daemon_version": "0.16.0",
  "build_commit": "8c16fb5b",
  "built_at": "2026-07-30T03:09:51Z",
  "uptime_sec": 86400,
  "logical_cpus": 28,
  "cpu_idle_fraction": 0.83,
  "load_per_core": 0.51,
  "worktree_root_free_gb": 200,
  "worktree_root_total_gb": 1000
}
```

`cpu_idle_fraction`, `load_per_core`, `worktree_root_free_gb`, and
`worktree_root_total_gb` are omitted when unmeasurable. A consumer MUST treat an
absent measurement as "unknown", never as zero/full.

**`worktree_root_total_gb` (Issue #5356).** Total capacity (GB) of the
worktree-root scratch volume — the denominator `worktree_root_free_gb` needs to
become a percentage, which is comparable across a heterogeneous fleet in a way an
absolute free-GB figure is not (40 GB free means something very different on a
128 GB worker than on a 2 TB studio box). Sourced from the same `df -Pk` sample as
`worktree_root_free_gb` (`disk_headroom.rs`'s `worktree_root_disk_gb`, one probe
for both columns rather than two separate subprocess spawns).

Follows the **exact same "unknown != zero" contract** `worktree_root_free_gb`
already established: omitted, never a fabricated `0`, when the probe cannot
measure it. This can happen independently of the free-space reading, so a record
may legitimately carry `worktree_root_free_gb` with no `worktree_root_total_gb` —
a consumer that sees this shape MUST render the free reading in GB only and MUST
NOT compute a percentage against a fabricated denominator. This is also exactly
the shape a record from a pre-#5356 daemon has (the field did not exist yet), so
the same rendering rule keeps old and new "no total" records indistinguishable —
neither is an error.

This field is purely additive — a `schema_version` bump is unnecessary (see
"`schema_version` semantics" above: only a **breaking** wire change requires one),
and passes through public redaction unchanged for the same reason
`worktree_root_free_gb` does (`dashboard/src/redaction.ts`): total disk capacity
describes the machine, not any repo, issue, branch, or operator. It is a mild
fingerprinting signal for a named host — reviewed and deliberately allowed
through, since free-GB is already public and this is only the denominator that
turns it into a percentage.

**Binary identity (`build_commit` / `built_at`, #4956).** `daemon_version` is
`CARGO_PKG_VERSION`, so it only moves once per release: every build between two
releases reports the same string, and a day-stale daemon is indistinguishable
from current `main`. `build_commit` (the short git SHA the running binary was
compiled from) and `built_at` (when it was compiled) are the precise identity —
both come from the very same compile-time stamps `loom-daemon --version` prints
(`LOOM_DAEMON_GIT_COMMIT` / `LOOM_DAEMON_BUILD_TIME`, baked in by `build.rs`), so
the telemetry and the CLI can never disagree.

- `build_commit` is always sent. `"unknown"` is a *meaningful* value, not a
  missing measurement: it means the build host had no git (e.g. a
  release-tarball build). A record from a pre-#4956 daemon has no field at all,
  which decodes as an empty string.
- `built_at` is **omitted** when the build-time stamp was unavailable — an
  unknown build time is absent, never a fabricated instant, exactly like the
  measured fields above.

Both fields are additive and pass through public redaction unchanged (they
describe the released binary, not any repo or operator — see
`dashboard/src/redaction.ts`), so an older consumer that ignores unknown keys is
unaffected.

**Watchdog/crash-protection state (`protection`, #5352).** An optional object
carrying this host's watchdog/crash-protection classification — the same
verdict `loom-daemon status`'s own `Protection:` line and `--json`'s
`protection` object already compute
(`daemon_install_state::probe_protection`), reused rather than re-derived so
the two surfaces can never disagree:

```json
{ "state": "watchdog-not-provisioned", "watchdog_provisioned": false }
```

- `state` — one of `"protected"`, `"no-marker"` (crash protection disarmed —
  no autonomy-desired marker), `"watchdog-not-provisioned"` (marker present,
  but nothing is scheduled to detect a future daemon death), or `"unknown"`
  (the probe ran but could not answer the provisioning check itself — no
  `launchctl`/`systemctl`, or an unreachable `systemctl --user` bus).
- `watchdog_provisioned` — whether the watchdog job/timer was found
  provisioned, omitted when `state` is `"unknown"`.

The whole `protection` object is **omitted** on a record from a pre-#5352
daemon, or when the host-local probe could not construct a report at all — a
consumer MUST treat that absence as "not reported", never as "unprotected":
synthesizing a false negative from a missing field would be worse than no
signal at all.

## Persistence & read surface (`sweep.outcome`, Issue #4704)

The daemon durably records one `sweep.outcome` [`TelemetryEnvelope`] per
completed sweep — success, failure, or cancellation — to a local, append-only
JSONL journal: `<workspace_root>/.loom/logs/sweep-outcome-telemetry.jsonl`
(override via `LOOM_SWEEP_OUTCOME_TELEMETRY_JOURNAL_PATH`, or per-registry via
`SweepRegistryConfig::outcome_telemetry_path`). This happens **regardless of
whether any exporter is configured** — local durability is the point: history
survives a daemon restart and outlives any cloud backend (#4705).

Written by `loom-daemon/src/sweep_registry.rs`'s
`append_outcome_telemetry_journal` at the same three terminal-transition call
sites as the older, narrower `#4644` `OutcomeRecord` journal
(`sweep-outcomes.jsonl` — see `loom-daemon/src/sweep_outcomes.rs`'s module
doc for why the two files are kept separate): the reaper's crashed/exited
handling in `reap_once`, and the operator/watchdog-initiated `finish_cancel`.
Best-effort like its sibling — a write failure is logged and never blocks
reaping. Same bounded-retention policy: rotates to a single `.1` backup once
the file exceeds 5 MiB or its oldest line is more than 30 days old.

**`phase_durations` is sampled**: the registry samples each live sweep's
checkpoint (`.loom/sweep-checkpoint/issue-<N>.json`) once per reaper tick
(≤30s, finer in practice) and records each transition, because the checkpoint
is overwritten at every phase boundary and deleted by the sweep skill on
success — nothing on disk holds a history. Durations are therefore accurate to
within one sampling interval; the trailing in-flight segment (last observed
phase completion → terminal transition) is not attributed to any phase, so the
entries sum to at most `total_duration_sec`. A daemon restart mid-sweep loses
the earlier observations: such a record falls back to a single best-effort
entry (last known phase, whole duration) or an empty list, never a fabricated
phase name. Phase names are the checkpoint markers normalized to lifecycle
names (`curator-done` → `curator`, `judge-rejected` → `judge`), and a phase
that runs twice (the Judge↔Doctor cycle) yields two entries in lifecycle order.

**`pr_number` costs no forge call**: it is captured from the same checkpoint
read (the sweep skill records `pr_number` from `builder-done` onward), so it
names the PR *this sweep produced* and survives the checkpoint's deletion on
success. A terminal transition therefore adds no GraphQL round trip to the
reaper's hot path; the only forge lookup in the write path is the cached
`owner/repo` + visibility resolution.

**How `result` is decided** (all from state the daemon already holds — no extra
forge call):

| Terminal transition | `result` |
|---|---|
| Merge phase observed to complete (`merge-done` sampled) | `success` |
| Operator/watchdog cancel (`finish_cancel`) | `cancelled` |
| Clean exit (code `0`) with no `merge-done`, not the #4366 no-progress shape | `success` |
| Everything else — non-zero exit, unobservable exit status, the #4366 clean-exit-with-zero-progress shape, or a death that left a checkpoint behind | `failure` |

An *unobservable* exit status (a reconstructed entry reaped via `kill(pid, 0)`,
which yields no code) is deliberately a `failure`, not a `success`: absence of
evidence is not evidence of a merge. The schema's fourth variant, `blocked`, is
reserved for a human-decision blocker; the daemon does not yet emit it, because
the blocker signal (`sweep.issue.{N}.blocker`) and the post-Builder build gate
are both child-side and are not routed into the registry.

### Local inspection: `loom-daemon sweep-outcomes`

```bash
# Success rate and median duration, grouped by model (the #4137 AC4 query):
loom-daemon sweep-outcomes

# Individual records, newest first:
loom-daemon sweep-outcomes --records --limit 20

# Filter by model and/or result (success | failure | cancelled | blocked):
loom-daemon sweep-outcomes --model opus --result failure --records

# Machine-readable:
loom-daemon sweep-outcomes --json
```

Purely file-based (like `loom-daemon calibrate`) — no running daemon required.
`--workspace PATH` selects a different repo root (default `.`).

[`TelemetryEnvelope`]: #envelope
