# Live agent output: the `session.output` record kind (#9764)

`session.output` is Loom's **live** agent-activity feed: readable, redacted,
issue-scoped log records published *while* a sweep or role run is still in
flight. It exists so an issue detail view can answer "what is this agent doing
right now" without waiting for a run to finish.

It is **off by default** and must be enabled deliberately. See
[Configuration](#configuration).

## What this is not

| Not this | Why |
|---|---|
| `session.summary` / `session.analysis` | Post-hoc aggregates on a 900 s default cadence ([`activity/transcript_ingest.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/activity/transcript_ingest.rs)). Useful, but neither readable nor incremental. |
| A transcript archive | `session.output` publishes a bounded, redacted, **selected** subset. It never claims to be a complete transcript, and it signals its own gaps (see [Gaps](#gaps-truncation-and-loss)). |
| A second log service | It reuses the existing observability exporter queues, the existing OTLP collector, and the existing identity resolution (#9445 / #9472). No new endpoint, no new sink. |
| A way to export raw session JSONL | Structurally impossible — see [The content boundary](#the-content-boundary). |

## The content boundary

Three independent properties bound what can reach the wire, in this order.
**None of them is a regex over the whole transcript**, and no configuration
relaxes any of them.

1. **The producer selects.** Only assistant-authored text and tool
   start/finish *metadata* ever become a record. User prompts, `thinking`
   blocks, `tool_use.input` (tool arguments), `tool_result.content` (raw tool
   results) and every internal bookkeeping record type are dropped **at parse
   time** — no record is constructed, so there is no ordering in which they
   could leak. Enforced in
   [`observability/session_output/claude.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/observability/session_output/claude.rs).
2. **The producer redacts.** `SessionOutputRecord::new_output` is the only
   constructor that accepts free text, and it always runs
   [`redact::scrub`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/telemetry/kinds/session_output/redact.rs)
   first. The applied policy name travels on the record as
   `loom.session.output.redaction` (currently `producer/v1`).
   Scrub-then-clip, never clip-then-scrub: clipping first can cut a secret in
   half and export the surviving prefix verbatim.
3. **OTLP only.** The kind is declared `native: false` in the registry
   ([`telemetry/kinds.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/telemetry/kinds.rs)), so the
   native HTTPS `/ingest` backend — the managed-cloud sink — **never** receives
   it, on any configuration. Enabling live output cannot start shipping session
   text to a managed destination, and cannot add or alter an exporter: the sink
   is built only from OTLP queues that were *already* resolved. No OTLP
   exporter configured means nothing is published at all.

The collector then re-applies the same secret classes at the gateway
(`transform/session_output_redaction`, before `transform/privacy`). That is
**defence in depth, not the boundary** — it exists so that neither layer is
ever the only one, and so a body from an older producer build is still
scrubbed.

Tests pinning each property: `the_kind_is_otlp_only_so_the_managed_https_sink_can_never_receive_it`,
`enabling_live_output_does_not_add_or_change_an_exporter`,
`only_assistant_text_and_tool_metadata_ever_become_records`,
`a_secret_straddling_the_bound_is_scrubbed_before_it_is_clipped`,
`the_gateway_re_scrubs_session_output_bodies_before_transform_privacy`.

## Schema

One OTLP **log record** per source event.

- **Body** — the already-redacted readable text for a content record; the
  category name for a status record. Read the body directly; it is not JSON.
- **`time_unix_nano`** — the **source** event time (the transcript record's own
  timestamp). A late or replayed record therefore sorts where it *happened*.
- **`observed_time_unix_nano`** — when the producer **read** it. Deliberately
  never merged with the source time; the delta is producer lag, and collapsing
  them would make a quiet run indistinguishable from a stalled export.
- **Severity** — `Info` normally, `Warn` for a coverage record that is not
  `live`, `Error` for a gap. An alert can key on severity alone.

### Attributes

Identity keys are reused, not redefined:

| Attribute | Notes |
|---|---|
| `loom.repo` | Canonical forge `owner/repo` from the workspace's `origin` remote. **Never** a directory basename — that was the #9445 defect. Omitted when genuinely unknown. |
| `loom.repo.visibility` | Always the fail-closed `private` — this producer makes no forge call to ask. Emitted only alongside a `loom.repo`. Lets a public view exclude readable session text without inspecting bodies. |
| `loom.issue` | Forge issue number. Omitted for an unattributed interactive session; never guessed from output text. |
| `loom.session_kind` | `sweep` · `role` · `interactive` — **why** `loom.issue` is absent when it is. `interactive` means deliberately unscoped; a `sweep`/`role` row with no issue means attribution was *lost*. A null `loom.issue` alone cannot distinguish the two. |
| `loom.sweep_id`, `loom.session_id`, `loom.attempt`, `loom.runtime`, `loom.role` | Run identity. `attempt` is what separates a retry from the original. |

Identity is resolved by **#9445's own resolver**
([`activity::session_context::SessionContext::resolve`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/activity/session_context.rs)),
once per stream on first sight — not re-derived here. `repo` comes from the
workspace's `origin` remote, so this feed and `session.summary` cannot disagree
about whose session a row belongs to, and no `gh` subprocess runs on the tick
path.

Kind-specific keys (all declared in `SESSION_OUTPUT_LOG_ATTRIBUTE_KEYS` and
contract-tested against the collector allowlist by
`collector_keeps_every_session_output_attribute`):

| Attribute | Meaning |
|---|---|
| `loom.session.output.schema` | Payload contract version a consumer pins (currently `1`). |
| `loom.session.output.category` | `output` · `tool_start` · `tool_finish` · `heartbeat` · `gap` · `coverage` |
| `loom.session.output.stream` | `assistant` · `tool` · `status` |
| `loom.session.output.stream_id` | The ordered stream this record belongs to — a session key, never a path. |
| `loom.session.output.sequence` | 0-based **line index** in the source transcript. Monotonic within a stream. |
| `loom.session.output.event_id` | `{stream_id}#{sequence}` — a pure function of the source event. |
| `loom.session.output.tool` / `.tool_ok` | Tool **name**, and success for a finish. Never arguments, never results. |
| `loom.session.output.coverage` | `live` · `unsupported` · `degraded` · `ended` |
| `loom.session.output.state` | `running` · `idle` · `ended` |
| `loom.session.output.truncated_bytes` | Bytes clipped from *this* record. Omitted when 0. |
| `loom.session.output.dropped_events` | Source events the run could not deliver, cumulative. Omitted when 0. |
| `loom.session.output.gap_reason` | Required on a `gap`. See [Gaps](#gaps-truncation-and-loss). |
| `loom.session.output.redaction` | Producer redaction policy that produced the body. |
| `loom.session.output.producer_lag_ms` | `observed_at - source_at` for this one record. |
| `loom.session.output.lag_p50_ms` / `.lag_p95_ms` / `.lag_max_ms` / `.lag_samples` / `.lag_historical_excluded` | Run-level producer-lag distribution. **Status records only.** See [Latency](#latency). |

### Ordering and de-duplication

Key on `stream_id` + `sequence`; `event_id` is the pure function of the two.
Because `sequence` is the source file's **line index** rather than a counter of
when this process happened to read it, a re-read after a producer restart
reproduces byte-identical `event_id`s — which is what makes consumer-side
de-duplication possible. Two records sharing one `source_at` (common: a
transcript writes several records inside one millisecond) are still
distinguishable by `sequence`.

Status records (`heartbeat`, `gap`, `coverage`) are sequenced on their **own**
stream, separate from any transcript's line numbering, so they can never
collide with a content record's id.

### Gaps, truncation and loss

Where delivery cannot be complete, that is **stated**, never implied. A
consumer that renders these records as a transcript without reading the gap
signals is choosing to imply a completeness the producer never claimed.

| `gap_reason` | Cause |
|---|---|
| `backlog_skipped` | Attached to a transcript that already had history; only the last 20 content events are retained. |
| `source_truncated` | The transcript shrank (rotated, or rewritten by a resumed session). |
| `backlog_too_large` | First attach to a file over 32 MiB — the producer jumps to the end rather than indexing it. |
| `export_queue_overflow` | The bounded OTLP queue discarded envelopes. Attributed to the runs the producer was publishing for. |

Per-record clipping is separate: a body over 2 000 characters is clipped,
reports `truncated_bytes`, **and** ends with an explicit
`…[+N chars truncated]` suffix so a reader of the body alone still knows it is
partial.

## Latency

The #9764 target is **p95 source-event-to-visible ≤ 10 s**. The budget:

| Stage | Default | For a live deployment |
|---|---|---|
| Producer read interval | 2 000 ms | 2 000 ms |
| Export flush (`observability.flushIntervalSecs`) | 30 s | **set to ≤ 5 s** |
| Gateway + backend ingest + consumer poll/cache | — | remaining ~3 s |

The **default 30 s flush interval does not meet the target.** A deployment that
wants the 10 s p95 must lower `observability.flushIntervalSecs`; nothing lowers
it implicitly.

Latency has two independently observable halves, and the producer refuses to
collapse them:

- **Producer lag** = `observed_time_unix_nano - time_unix_nano`. Measured
  directly, and summarized per run as p50/p95/max on every status record.
- **Pipeline lag** = `backend_ingest_time - observed_time_unix_nano`. Computed
  by the consumer from the two timestamps every record already carries.

### Historical timestamps are not latency

`observed_at - source_at` is a **latency** only when the producer was already
watching when the source event happened. A transcript that existed before the
producer attached is replayed from its retained tail, and those events'
timestamps can be hours old — subtracting them yields the file's **age**, and a
single such sample would dominate p95 and report a stall that never occurred.

[`latency::LagWindow::observe`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/observability/session_output/latency.rs)
therefore admits a sample only when `source_at >= watch_since`, and counts every
refusal in `lag_historical_excluded` so the exclusion is visible on the wire
rather than looking like missing data. A run that has measured nothing omits the
lag attributes entirely — a zero p95 would assert a latency nothing observed.

The window is a bounded 256-sample **sliding** window, so a resolved stall ages
out instead of keeping a healthy run's p95 red forever.

### Querying end-to-end latency

In SigNoz (ClickHouse `signoz_logs`), use `created_at` — the backend's **own
insert clock** — as the observation time. Three clocks are in play and only two
of them are the producer's:

| Column | Clock | Meaning |
|---|---|---|
| `timestamp` | source | when the agent emitted it |
| `observed_timestamp` | producer | when the producer read it |
| `created_at` (or `inserted_at`) | backend | when it became queryable |

> **Do not use `now64()` as the observation time.** `now64() - timestamp`
> measures how *old the row is at query time*, not how long it took to arrive —
> re-running the same query an hour later "reports" an hour of latency on
> identical rows. This is the query-side version of the same mistake the
> producer guards against on its own samples.

```sql
-- Source-to-queryable latency, end to end AND pipeline-only, over the last hour.
-- `created_at` is the backend's insert clock, so the result does not drift with
-- when the query is run.
--
-- Both time filters are load-bearing. The `timestamp` bound keeps a replayed
-- historical event (whose source time may be hours old) from entering the
-- distribution as if it were a stall; the `observed_timestamp` bound keeps a
-- row the producer read long ago out of it too.
SELECT
  count()                            AS samples,
  round(quantile(0.50)(e2e_ms))       AS e2e_p50_ms,
  round(quantile(0.95)(e2e_ms))       AS e2e_p95_ms,
  round(max(e2e_ms))                  AS e2e_max_ms,
  round(quantile(0.95)(pipeline_ms))  AS pipeline_p95_ms
FROM (
  SELECT
    toUnixTimestamp64Milli(created_at) - intDiv(timestamp, 1000000)          AS e2e_ms,
    toUnixTimestamp64Milli(created_at) - intDiv(observed_timestamp, 1000000) AS pipeline_ms
  FROM signoz_logs.distributed_logs_v2
  WHERE attributes_string['loom.session.output.event_id'] != ''
    AND attributes_string['loom.session.output.category'] IN ('output','tool_start','tool_finish')
    AND timestamp          >= toUnixTimestamp64Nano(now64() - INTERVAL 1 HOUR)
    AND observed_timestamp >= toUnixTimestamp64Nano(now64() - INTERVAL 1 HOUR)
);
```

Measured on the reference stack (producer → OTLP/HTTP → gateway collector →
SigNoz), exporting directly rather than through the daemon's batching queue:
**p50 211 ms, p95 239 ms, max 239 ms** over 9 content records. That is the
floor; a real deployment adds `observability.flushIntervalSecs` on top, which is
why the budget table above calls for ≤ 5 s rather than the 30 s default.

The producer half is read straight off the status records, no aggregation
needed:

```sql
SELECT
  attributes_string['loom.repo']  AS repo,
  attributes_number['loom.issue'] AS issue,
  attributes_number['loom.attempt'] AS attempt,
  max(attributes_number['loom.session.output.lag_p95_ms']) AS producer_p95_ms,
  max(attributes_number['loom.session.output.lag_max_ms']) AS producer_max_ms,
  max(attributes_number['loom.session.output.lag_samples']) AS samples,
  max(attributes_number['loom.session.output.lag_historical_excluded']) AS excluded
FROM signoz_logs.distributed_logs_v2
-- Status categories ONLY. A content row has no lag_* attribute, and a
-- ClickHouse Map lookup for a missing key returns 0 rather than NULL, so
-- including content rows silently averages in zeros that never existed.
WHERE attributes_string['loom.session.output.category'] IN ('heartbeat','coverage','gap')
  AND timestamp >= toUnixTimestamp64Nano(now64() - INTERVAL 1 HOUR)
GROUP BY repo, issue, attempt
ORDER BY producer_p95_ms DESC;
```

> **A missing attribute is not zero.** `attributes_number[...]` yields `0` for a
> key that is absent, so "this run measured nothing" (`lag_samples` absent) and
> "this run measured 0 ms" are indistinguishable by value alone. Always gate on
> `loom.session.output.category`, and treat `lag_samples = 0` as *unmeasured*
> rather than as an instantaneous pipeline.

## Consumer query example

The live feed for one attempt of one issue, in source order:

```sql
SELECT
  timestamp,            -- source event time
  observed_timestamp,   -- when the producer read it
  attributes_string['loom.session.output.category'] AS category,
  attributes_string['loom.session.output.tool']     AS tool,
  attributes_number['loom.session.output.sequence'] AS seq,
  attributes_string['loom.session.output.event_id'] AS event_id,
  body
FROM signoz_logs.distributed_logs_v2
WHERE attributes_string['loom.repo']  = 'rjwalters/loom'
  AND attributes_number['loom.issue'] = 9764
  AND attributes_number['loom.attempt'] = 1
  AND timestamp >= toUnixTimestamp64Nano(now64() - INTERVAL 30 MINUTE)
ORDER BY attributes_string['loom.session.output.stream_id'], seq
LIMIT 500;
```

Two concurrent issues separate on `loom.issue`; two attempts of the **same**
issue separate on `loom.attempt` (and on `loom.sweep_id`). Dropping the
`loom.attempt` predicate interleaves both attempts — intentionally, for a
"everything that happened on this issue" view.

**De-duplicate on `event_id`**, not on `(timestamp, body)`: a producer restart
legitimately republishes the retained tail with identical ids, and two distinct
records can share a timestamp.

**Always surface non-`live` coverage.** A feed that shows content rows but hides
a `coverage = unsupported` or `degraded` row is reporting an unsupported
runtime as live, which this contract exists to prevent:

```sql
SELECT
  attributes_string['loom.runtime']  AS runtime,
  attributes_string['loom.session.output.coverage'] AS coverage,
  attributes_string['loom.session.output.gap_reason'] AS gap_reason,
  count() AS records
FROM signoz_logs.distributed_logs_v2
WHERE attributes_string['loom.session.output.category'] IN ('coverage','gap')
  AND timestamp >= toUnixTimestamp64Nano(now64() - INTERVAL 1 HOUR)
GROUP BY runtime, coverage, gap_reason;
```

## Configuration

Live output is opt-in at **two** independent levels: an OTLP exporter must
already be configured, **and** `observability.liveOutput.enabled` must be true.

```json
{
  "observability": {
    "enabled": true,
    "flushIntervalSecs": 5,
    "otlp": [
      { "name": "signoz", "endpoint": "http://127.0.0.1:14318" }
    ],
    "liveOutput": {
      "enabled": true,
      "intervalMs": 2000,
      "heartbeatSecs": 30,
      "maxRuns": 64
    }
  }
}
```

| Key | Default | Meaning |
|---|---|---|
| `liveOutput.enabled` | `false` | The switch. Env override: `LOOM_OBSERVABILITY_LIVE_OUTPUT=1`. |
| `liveOutput.intervalMs` | `2000` | How often each open run's source is read. Also the producer half of the latency budget. |
| `liveOutput.heartbeatSecs` | `30` | Silence after which a `heartbeat` is emitted, so a quiet run is distinguishable from a stalled export. |
| `liveOutput.maxRuns` | `64` | Runs tracked at once. At the ceiling the producer stops *adding* runs (and logs once) rather than growing unbounded. |

Precedence is the house rule, **env > config > default**. Only the on/off switch
has an env override: three more env names would be three more ways for a host to
silently disagree with its committed config.

A nonsense value (`0`, a string, a negative) falls back to the default rather
than producing a zero interval — pinned by
`nonsense_knob_values_fall_back_rather_than_producing_a_zero_interval`.

### Collector

The gateway config ships the `transform/session_output_redaction` stage and the
`loom.session.output.*` allowlist entries already
([`defaults/observability/collector/config.yaml`](https://github.com/rjwalters/loom/blob/main/defaults/observability/collector/config.yaml)).
An **existing** collector deployment must be restarted with the updated config,
or every `loom.session.output.*` attribute is stripped by `transform/privacy`
and the bodies arrive un-rescrubbed by the gateway (the producer still scrubs
them).

## Supported runtimes

| Runtime | Coverage | Notes |
|---|---|---|
| **Claude** | `live` | Full adapter: assistant text, tool start/finish, subagent transcripts. |
| Codex | `unsupported` | No adapter. Emits one explicit `coverage = unsupported` record per run, severity `Warn`, and is never read. |
| Pi | `unsupported` | As above. |
| OpenCode | `unsupported` | As above. |

`SUPPORTED_RUNTIMES` in
[`observability/session_output.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/observability/session_output.rs)
is the single source of truth, and the unsupported path is tested
(`an_unsupported_runtime_is_flagged_and_never_read`). An unsupported runtime is
**never silently reported as live** — that is the whole purpose of the
`coverage` category. A missing/unknown runtime also resolves to `unsupported`,
not to a guess.

Codex, Pi and OpenCode adapters are tracked separately; they need per-runtime
source-format work equivalent to the Claude adapter and are deliberately not
stubbed here.

### `degraded` vs `unsupported`

- `unsupported` — no adapter exists. No `output` record will **ever** arrive.
  Terminal.
- `degraded` — a supported runtime whose source could not be located or read
  yet. Recoverable; the producer keeps looking and flips to `live` on success.
  A heartbeat stays `degraded` until a stream is found, so "we cannot find this
  run's output" never reads as "this run is quiet".

## Rollout

- **Required version**: the first release containing the #9764 merge. `main`
  bumps `VERSION` on nearly every merge and releases are cut at fleet-rollable
  boundaries ([`release-cadence.md`](release-cadence.md)), so rather than pin a
  number that would already be wrong by the time it shipped, confirm the
  capability on the host you are rolling out to. With `liveOutput.enabled` set,
  a daemon that carries this kind logs exactly one line at startup:

  ```text
  session.output: live output enabled (interval=2000ms, heartbeat=30s, max_runs=64, supported runtimes: claude)
  ```

  A daemon too old to have the kind ignores the `liveOutput` block silently and
  logs nothing — the absence of that line *is* the negative result. (A build
  that has the kind but no OTLP exporter configured says so distinctly: `live
  output is enabled but no OTLP exporter is configured`.)
- **Wire gate**: the kind pins schema gate `13`, separately from
  `NEW_KIND_SCHEMA_VERSION`, for the same reason `ci.job.log` pinned `9` — a
  backend must be able to refuse free-text session content at the version level
  without also refusing every other post-#8921 kind.
- **Order of operations**: update the collector config and restart the gateway
  **first**, then enable `liveOutput` on the daemons. The reverse order works
  but drops every kind-specific attribute at `transform/privacy` until the
  gateway catches up.
- **Rollback**: set `liveOutput.enabled` to `false` (or unset
  `LOOM_OBSERVABILITY_LIVE_OUTPUT`) and restart the daemon. The producer makes
  no subscription and no syscalls when disabled; nothing else changes
  behaviour, and no other telemetry kind depends on it.

## Verification

See
[`.loom/docs/verification-recipes.md`](verification-recipes.md) for the general
shape. Specific to this feature:

```bash
# 1. The producer, adapter, redaction, latency and collector contracts (offline).
cargo test -p loom-daemon session_output

# 2. The live path: real adapter → real records → real OTLP/HTTP export →
#    real collector stages → backend. Needs a reachable collector and the
#    `otlp` feature; the tests are #[ignore]d so CI never needs either.
LOOM_E2E_OTLP_ENDPOINT=http://127.0.0.1:14319 \
  cargo test -p loom-daemon --features otlp session_output::e2e \
  -- --ignored --nocapture
```

The e2e tests print every `event_id` they publish; those are the join key for
the [consumer query](#consumer-query-example) that confirms the rows landed.

### Recorded results

Verified on the reference stack (gateway collector 0.161.0 carrying this
repository's config → SigNoz):

| Acceptance | Observed |
|---|---|
| Readable, correlated records before the run ends | 3 content rows for one in-flight run (2 × `output`, 1 × `tool_start`), published across successive passes while the transcript was still being appended |
| Two concurrent issues separable | `loom.issue` 9764 / 9765, in two checkouts whose directory names resemble neither the repo nor each other; both rows carry `loom.repo = rjwalters/loom` |
| Visibility + attribution reason on the wire | every row carried `loom.repo.visibility = private` and `loom.session_kind = sweep` through the gateway into SigNoz |
| Two attempts of one issue separable | issue 9766 `loom.attempt` 1 and 2 — distinct `sweep_id`, `stream_id` and `event_id` |
| Worktree name unrelated to forge repo | `loom.repo` is the forge slug on every row; no `stream_id` contains a path fragment |
| Runtime coverage recorded | `claude → live` (2 rows); `codex`, `pi`, `opencode` → `unsupported` (1 row each, severity `Warn`) |
| Source-to-queryable latency, both clocks | e2e p50 **211 ms** / p95 **239 ms** / max **239 ms** over 9 content rows, using `created_at` as the observation clock |
| Historical timestamp not a latency | A 6 h-old source event was published (and sorts at its source time, `producer_lag_ms = 21600032`) while the run's heartbeat reported `lag_p95_ms = 1`, `lag_samples = 1`, `lag_historical_excluded = 1` — the outlier excluded from the distribution, and the exclusion visible |
| Credentials cannot pass | 4 live credential shapes plus a prompt canary in the source; the published body contained only `[REDACTED:*]` markers, the prompt line produced no record at all, and a `tool_use.input` holding a token produced a `tool_start` carrying only the tool name |
| Cannot enable managed-cloud export | `native: false` is asserted in two separate tests; `resolve_exporters` is byte-identical with live output on and off |

## Source map

| File | Covers |
|---|---|
| [`telemetry/kinds/session_output.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/telemetry/kinds/session_output.rs) | The record, the closed vocabularies, `LagStats`, stable `event_id` |
| [`telemetry/kinds/session_output/redact.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/telemetry/kinds/session_output/redact.rs) | Producer-edge redaction (`producer/v1`), scrub-then-clip |
| [`observability/session_output.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/observability/session_output.rs) | Config/gate, sink over existing OTLP queues, run tracking, the tick loop |
| [`observability/session_output/claude.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/observability/session_output/claude.rs) | The Claude adapter: incremental tail, content boundary, gap detection |
| [`observability/session_output/latency.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/observability/session_output/latency.rs) | Producer-lag window, percentiles, historical-sample exclusion |
| [`observability/otlp/mapping/session_output.rs`](https://github.com/rjwalters/loom/blob/main/loom-daemon/src/observability/otlp/mapping/session_output.rs) | OTLP log mapping, dual timestamps, severity |
| [`defaults/observability/collector/config.yaml`](https://github.com/rjwalters/loom/blob/main/defaults/observability/collector/config.yaml) | Gateway re-scrub stage + attribute allowlist |
