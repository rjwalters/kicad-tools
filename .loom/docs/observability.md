# Fleet Observability: end-to-end reference

> Epic [#4702](https://github.com/rjwalters/loom/issues/4702), Phase 4
> (#4860). This is the single entry point tying the whole pipeline together —
> **daemon config → wire schema → exporter → Cloudflare backend → dashboard
> views** — matching the map-plus-links pattern `daemon-reference.md` and
> `token-pool.md` already use. It is an operating summary, not a duplicate:
> every claim below has a canonical detail doc linked next to it, and this
> page should stay a map even as those detail docs grow.

## The pipeline, in one picture

```
loom-daemon (per host)
  observability.* config block (opt-in, off by default)
        │  collector: EventBus subscriber -> TelemetryEnvelope
        ▼
  durable disk-backed queue (survives sink outage / sleep)
        │  drains via a jittered-retry loop
        ▼
  exporter: HttpsExporter (default) or OtlpExporter (opt-in, #4858)
        │
        ▼
Cloudflare Worker backend (deploy-your-own, or an operator-run reference instance)
  D1 (durable history) + Durable Object (live "what's running now")
        │
        ├── /api/*     authenticated, full detail   (Cloudflare Access)
        └── /public/*  unauthenticated, redacted     (always reachable)
        │
        ▼
Dashboard UI (served by the same Worker) — authenticated + public views
```

Nothing here is mandatory: with no `observability` block (or `enabled:
false`), the daemon does none of the above — no subscription, no queue file,
no HTTP client, zero extra syscalls. Loom never phones home; every hop in
this pipeline is infrastructure **you** deploy and point your own daemons at.

## 1. Enable telemetry on a daemon

Add the `observability` block to that host's `.loom/config.json` — **except**
`ingestKeyFile`, see the callout below:

```json
{
  "observability": {
    "enabled": true,
    "endpoint": "https://<your-worker>.workers.dev/ingest",
    "batchSize": 50,
    "flushIntervalSecs": 30,
    "queueCapacity": 2000
  }
}
```

Precedence is **env > config > default**, the same rule every other
`autonomous.*`-style daemon subsystem follows
(`loom-daemon/src/config_resolver.rs`). Every key has a
`LOOM_OBSERVABILITY_*` env override:

| Config key | Env override | Default |
|---|---|---|
| `enabled` | `LOOM_OBSERVABILITY_ENABLED` | `false` |
| `endpoint` | `LOOM_OBSERVABILITY_ENDPOINT` | unset (disables export) |
| `ingestKeyFile` | `LOOM_OBSERVABILITY_INGEST_KEY_FILE` | `$HOME/.loom/observability/ingest.key` |
| `batchSize` | `LOOM_OBSERVABILITY_BATCH_SIZE` | 50 |
| `flushIntervalSecs` | `LOOM_OBSERVABILITY_FLUSH_INTERVAL_SECS` | 30 |
| `queueCapacity` | `LOOM_OBSERVABILITY_QUEUE_CAPACITY` | 2000 |
| `exporter` | `LOOM_OBSERVABILITY_EXPORTER` | `"https"` (or `"otlp"`, §3) |

The ingest key is **never inline in config** — `ingestKeyFile` is a path the
daemon reads once at startup and holds only in memory, sent solely as an
`Authorization: Bearer` header. A misconfigured block (missing endpoint or
unreadable key file) degrades to off; it does not crash the daemon. Source of
truth: `loom-daemon/src/observability/mod.rs`'s module doc (config
resolution, FLAGS-OFF posture, read-only invariant) and its `collector.rs` /
`queue.rs` / `exporter.rs` / `sender.rs` siblings (collector, durable queue,
exporter trait + HTTPS implementation, retry-drain loop).

**`ingestKeyFile` must never be committed to the shared `.loom/config.json`**
— unlike every other `observability.*` key above, it is host-specific by
definition (every host's key lives at a different, unshareable path). It
defaults to `$HOME/.loom/observability/ingest.key`, so the common case needs
no config value at all: install each host's key at that conventional path
(`dashboard/docs/deploy-runbook.md` step 9a) and leave `ingestKeyFile` unset
everywhere. A host that genuinely needs a non-default path (e.g. a system
path for a service account) sets it in the gitignored, per-host
`.loom-local/local.json` override tier (`config_resolver.rs`, highest
precedence) or via `$LOOM_OBSERVABILITY_INGEST_KEY_FILE` — never in the
committed file. Issue #5336 is exactly the failure mode this avoids: a
macOS `ingestKeyFile` value was committed to this repo's own shared
`.loom/config.json` and every other host that `git pull`ed `main` inherited
a path to a key file that did not exist on it, with telemetry silently off
for a day before anyone noticed.
`./defaults/scripts/check-ingest-key-file.sh` validates the resolved path on
any host — readable, and not a path copied from a different host's
`$HOME` — usable both right after provisioning a host and as a periodic
fleet-wide regression check.

## 2. What gets sent: the wire schema

Every push is a batch of versioned `TelemetryEnvelope`s
(`schema_version`, `emitted_at`, `host_id`, `record`). Record kinds:
`sweep.started`, `sweep.phase`, `sweep.completed`, `sweep.outcome`
(repo-scoped, each carrying a `visibility: public|private` tag derived from
the forge, private-by-default and private-safe-by-construction),
`tokens.snapshot`, `host.health` (host-level, no repo/visibility). Full
field-by-field reference, the `visibility` anti-leak contract, and the local
`sweep-outcome-telemetry.jsonl` journal (kept **regardless of whether any
exporter is configured**):
[`.loom/docs/telemetry-schema.md`](telemetry-schema.md).

## 3. Exporters: HTTPS (default) or OTLP (opt-in)

The default exporter is `HttpsExporter` — JSON-over-HTTPS `POST /ingest`,
batched (`batchSize`), retried with jitter, backed by the durable disk queue
so a sink outage or a sleeping host never silently drops data up to
`queueCapacity`. The `Exporter` trait (`exporter.rs`) and the drain loop
(`sender.rs`) are both deliberately generic, so a second sink is a drop-in
addition rather than a rewrite: `OtlpExporter` (epic Phase 4, issue
[#4858](https://github.com/rjwalters/loom/issues/4858)) translates the same
`TelemetryEnvelope` batches into OTLP logs (`/v1/logs`) and metrics
(`/v1/metrics`) requests for operators with an existing OpenTelemetry stack
(a self-hosted collector, Grafana, Honeycomb, …), reusing `sender.rs`'s
drain/retry loop unchanged.

Select it with `observability.exporter = "otlp"`
(`LOOM_OBSERVABILITY_EXPORTER` env override; **env > config > default**,
default `"https"`). It is opt-in twice over: off unless explicitly selected,
*and* gated behind the `otlp` Cargo feature — a default `loom-daemon` build
never compiles in the `opentelemetry-proto` dependency, so choosing
`HttpsExporter` costs nothing extra. The field-by-field
`TelemetryEnvelope` → OTLP mapping (which record kinds become logs vs.
metrics; how `host_id` / `emitted_at` / the repo-visibility tag map onto OTLP
resource/record attributes) is documented in
`loom-daemon/src/observability/otlp/mod.rs`'s module doc comment, verified by
`loom-daemon/src/observability/otlp/mapping.rs`'s unit tests.

**The HTTPS exporter verifies its own identity** (issue #4830). Each `/ingest`
success response echoes the `host_id` the presented key is bound to; the
exporter compares that against the identity this daemon resolved for itself
(`$LOOM_HOST_ID` > `$HOSTNAME` > `hostname`). On a disagreement — the wrong
host's key file installed on a machine, which silently mislabeled a whole
night of telemetry on 2026-07-31 — it logs a WARN **once per daemon lifetime**
and `loom-daemon health` reports an `observability DEGRADED` section (exit
`1`). Nothing else changes: the batch is still acked, and the key's binding
stays authoritative on the backend. Fix by installing the right key or setting
`$LOOM_HOST_ID` to match, then restarting the daemon.

This check is specific to the native ingest protocol, which is what defines
the echo. OTLP/HTTP has no equivalent — a success response carries only
`partial_success`, and a generic OTLP sink has no notion of a per-host key
binding to disagree with — so under `exporter = "otlp"` no mismatch is ever
published and the `observability` health section stays silent. Choosing OTLP
therefore trades this particular misconfiguration guardrail away; keep the
default `"https"` sink if you want it.

## 3b. Confirming telemetry is actually flowing

`loom-daemon health`'s `observability` section is **anomaly-only** by design
(issue #4830): it renders when something is wrong and stays silent otherwise.
That is the right call for a surface whose value is that every printed line is
worth reading — but on its own it left three very different hosts looking
identical (no section at all): one exporting perfectly, one with observability
disabled, and one that was configured, running, and had **never successfully
exported anything**. A `~/.loom/logs/observability-queue.jsonl` of 0 bytes is
equally consistent with "drained cleanly" and "nothing was ever enqueued", so
confirming the healthy case meant grepping `daemon.log` for the *absence* of a
warning.

`loom-daemon status` now states the answer positively (issue #5083):

```
Observability: OK — last export 12s ago, 3481 record(s) as host_id=studio-host → https://…/ingest
```

The same facts are machine-readable under `observability_export` in
`loom-daemon status --json`, so a watch loop can assert health instead of
inferring it from silence:

```bash
loom-daemon status --json | jq -e '.observability_export.state == "healthy"'
```

| `state` | Meaning | Rendered as |
|---|---|---|
| `disabled` | Exporter deliberately not running: `enabled=false`, or the block is absent. **Never** reported for `enabled: true` — see `misconfigured` below (#5337) | `Observability: disabled …` |
| `misconfigured` | `enabled: true`, but a required piece of config could not be resolved (no endpoint, no `ingestKeyFile`, or that file is missing/unreadable/empty, or `otlp` without the Cargo feature) — a config error to fix, not a benign off-by-choice state (#5337) | `Observability: MISCONFIGURED …` |
| `starting` | Running, nothing acked yet, still inside the grace window (3 × `flushIntervalSecs`, floored at 10 min) — a just-rolled daemon, not a fault | `Observability: starting …` |
| `never_exported` | Running well past the grace window and **no batch has ever been acked** — the silent failure mode | `Observability: NEVER EXPORTED …` |
| `healthy` | Batches are being acked and the ids agree | `Observability: OK …` |
| `host_id_mismatch` | Batches are being acked, but under a different `host_id` than this daemon reports for itself (the #4830 condition, §3 above) | `Observability: HOST-ID MISMATCH …` |
| `failing` | The most recent flush attempt errored; the queue is retrying with backoff | `Observability: FAILING …` |

`observability_export` also carries `host_id`, `endpoint`, `exporter`,
`started_at`, `last_success_at`, `last_failure_at`, `last_failure_detail`,
`records_exported`, `consecutive_failures`, and `flush_interval_secs`. A `null`
`observability_export` means the daemon binary predates #5083 — "cannot tell",
never "disabled"; restart the daemon onto a current binary. Under
`misconfigured`, `endpoint` reflects whatever piece of config *did* resolve
(`null` only when the endpoint itself is what's missing) and
`last_failure_detail` names the offending path plus the underlying error (e.g.
an `ingestKeyFile` `io::Error`'s `Display`, which includes the OS errno) — the
same "never the key itself" discipline every other error surface in this
module uses.

The health section keeps its anomaly-only contract. It now recognizes three
additional *non-green* conditions — `misconfigured`, `never_exported`, and
`failing` — which are anomalies by the same rule that already admitted
`host_id_mismatch`; `healthy`, `starting`, and `disabled` still render nothing
at all. When a section does render, its `detail` payload carries the full
`observability_export` record, so a machine consumer of `loom-daemon health
--json` gets the positive facts too.

**Note on scope**: this is a *transport-level* signal — it answers "are batches
being acked", not "is every record kind being enqueued". A host can report
`healthy` while a specific record kind is silently never queued (e.g. issue
#5084); the two checks are complementary.

## 4. The backend: deploy your own Cloudflare Worker

The Phase-2 backend is a Cloudflare Worker (D1 for durable history, a
Durable Object for live "what's running now" state, an hourly retention
cron) that also serves the dashboard UI as static assets. Full deploy
runbook — Wrangler setup, D1 migrations, admin token, per-host ingest key
provisioning, verifying telemetry lands — is
[`dashboard/docs/deploy-runbook.md`](https://github.com/rjwalters/loom/blob/main/dashboard/docs/deploy-runbook.md).
This is **your own infrastructure**; nothing in Loom points at a shared
backend by default.

## 5. Authenticated vs. public: two views, one redaction policy

Every query route exists twice — `/api/*` (authenticated, full detail) and
`/public/*` (unauthenticated, always reachable, redacted per record kind) —
enforced both at the edge (a Cloudflare Access policy in front of `/api/*`
only) and in the Worker itself (a per-kind field allowlist, defense in
depth). The dashboard root `/` is a single URL for both audiences: it
verifies the visitor's Access session in-Worker and falls back to the
redacted public variant on any failure (missing/expired/wrong-audience
token, even a JWKS fetch failure) — fail-closed by construction, never a
dead-end login wall for an anonymous visitor.

- Gating setup (custom domain requirement, route map, Access application
  config, the single-URL fallback mechanics):
  [`dashboard/docs/cloudflare-access.md`](https://github.com/rjwalters/loom/blob/main/dashboard/docs/cloudflare-access.md)
- Query API + live event tail, request/response shapes, pagination:
  [`dashboard/docs/query-api.md`](https://github.com/rjwalters/loom/blob/main/dashboard/docs/query-api.md)
- Token/cost analytics (burn curves, forecasting, per-repo attribution, and
  why that surface is authenticated-only): `dashboard/docs/token-analytics.md`

## 5b. Doc-maintenance throughput (Guide, local-only, issue #6136)

Everything in sections 1-5 above is the `sweep.*`/`tokens.snapshot` pipeline,
and it only ever covers **Builder sweeps** — the daemon's `SweepRegistry`
tracks a sweep's checkpoint file and phase transitions, which is what
`sweep.phase`/`sweep.completed`/`sweep.outcome` are sampled from
(`.loom/docs/telemetry-schema.md`). Support-role crons — Judge, Champion,
Curator, and Guide — run as role **prompts**
(`defaults/.claude/commands/loom/<role>.md`), not as tracked sweeps, so none
of them ever emit `sweep.*` records; their token spend falls into
`dashboard/docs/token-analytics.md`'s "unattributed" bucket, reported as a
single undifferentiated total with no per-role breakdown.

Guide's Document Maintenance phase (the WORK_LOG.md/WORK_PLAN.md/README.md
docs PRs) closes a **narrow slice** of that gap with its own small, decoupled
local telemetry surface — deliberately **not** wired into the
`loom-daemon`/Cloudflare pipeline above, since attaching a role prompt to the
`SweepRegistry` machinery would be a much larger change than this issue's
visibility-only scope:

- **Emission**: `create_docs_pr()` (Step 5) calls
  `./.loom/scripts/guide-docs-telemetry.sh record --pr <N> --duration-sec <N>
  --files <csv>` right before releasing the docs-guide lock, appending one
  JSON line — `{schema_version, emitted_at, emitted_at_epoch, host_id,
  record: {kind: "guide.docs_maintenance", repo, pr_number, duration_sec,
  files_changed}}` — to `.loom/logs/guide-docs-telemetry.jsonl` (gitignored,
  host-local, same directory `sweep-outcome-telemetry.jsonl` already lives
  in). `duration_sec` is the phase's elapsed lock-hold time
  (`docs-guide-lock.sh age`, read before release) — a proxy for agent/token
  spend, not a real token count (no token-usage API is available to a role
  prompt's shell environment).
- **Query**: `./.loom/scripts/guide-docs-telemetry.sh report --since 7d`
  (accepts `7d`/`24h`/`30m`/`90s`/a bare integer of seconds; `--json` for a
  machine-readable summary) prints doc-maintenance PR count and total/average
  phase time over the window, from one command — a zero-activity window
  renders "No doc-maintenance PRs in this window." rather than erroring.
- **What this does NOT do**: it does not add a `guide.*` kind to the wire
  schema in `.loom/docs/telemetry-schema.md`, does not export anywhere, and
  does not appear in the Cloudflare-backed dashboard — it is a purely local,
  single-host-at-a-time journal an operator queries directly on whichever
  host is running Guide. A fleet-wide, dashboard-integrated version of this
  (real per-account token attribution, multi-host aggregation) is a natural
  follow-up, not required by #6136's acceptance criteria.

## 5c. Merge-admission-recheck outcomes (merge-pr.sh, local-only, issue #6978)

`merge-pr.sh`'s synchronous-merge path calls `_recheck_mergeable_before_refusal()`
(#6104/#6118) whenever the forge's cached `.mergeable` reads `false` — it
re-queries (uncached) after a short backoff and, if still unresolved,
corroborates with a local `git merge-tree` check before deciding whether to
proceed with the merge anyway, refuse a confirmed conflict, or refuse an
unresolved/stale state. Like Guide's Document Maintenance phase (§5b above),
`merge-pr.sh` is a bash script invoked directly (by Champion's `--auto`, and
interactively) — never a tracked `loom-daemon` `SweepRegistry` sweep — so it
has no attachment point to the `sweep.*`/`tokens.snapshot` pipeline above
without a much larger change. #6156's efficacy review of that recheck found
this was the one actionable gap: the decision was only ever `echo`ed to
stdout/stderr, with nothing durable anywhere to answer (in retrospect) how
often the forge's cache lied, how often local corroboration confirmed a real
conflict vs. came back unresolved, or what the backoff cost in aggregate.

This closes that gap with the same small, decoupled local-telemetry pattern
as §5b, mirroring `guide-docs-telemetry.sh`:

- **Emission**: the mergeability gate calls
  `./.loom/scripts/merge-admission-telemetry.sh record --repo <owner/repo>
  --pr <N> --action <merge|refuse-conflict|refuse-stale> --reason <text>
  --retries-used <N> --backoff-delay-sec <N>` right after
  `_recheck_mergeable_before_refusal()` returns its decision and BEFORE
  branching on it with `info`/`error` (the `error` branch exits the script,
  so telemetry must be emitted first or it would never fire on a refusal).
  The call is wrapped so a failure here (unwritable log dir, missing `jq`,
  etc.) can never abort the merge path — this is observability-only, and the
  recheck's actual decision logic is unchanged. One JSON line —
  `{schema_version, emitted_at, emitted_at_epoch, host_id, record: {kind:
  "merge.admission_recheck", repo, pr_number, action, reason, retries_used,
  backoff_delay_sec}}` — is appended to
  `.loom/logs/merge-admission-telemetry.jsonl` (gitignored, host-local, same
  directory `sweep-outcome-telemetry.jsonl` and `guide-docs-telemetry.jsonl`
  already live in). `retries_used`/`backoff_delay_sec` are `null` when not
  supplied or non-numeric, never coerced to 0.
- **Query**: `./.loom/scripts/merge-admission-telemetry.sh report --since 7d`
  (accepts `7d`/`24h`/`30m`/`90s`/a bare integer of seconds; `--json` for a
  machine-readable summary) prints the invocation count broken down by
  `merge`/`refuse-conflict`/`refuse-stale` over the window, from one command
  — a zero-activity window renders "No merge-admission-recheck invocations in
  this window." rather than erroring.
- **What this does NOT do**: it does not add a `merge.*` kind to the wire
  schema in `.loom/docs/telemetry-schema.md`, does not export anywhere, and
  does not appear in the Cloudflare-backed dashboard — it is a purely local,
  single-host-at-a-time journal an operator queries directly on whichever
  host ran the merge. A fleet-wide, dashboard-integrated version of this
  (multi-host aggregation) is a natural follow-up, not required by #6978's
  observability-only scope. It also does not change
  `_recheck_mergeable_before_refusal()`'s actual decision logic in any way
  (#6156 recommended keeping that behavior as-is).

## 6. The operator reference instance

`dashboard.example.com` is a live, operator-owned deployment of this same
backend (not a shared Loom service — every fleet deploys its own). Its
specific account/database IDs, Access application layout, credential file
locations, and cutover history now live in that operator's own
infrastructure repo (example-org/fleet-repo#305), not in this repo — this repo's
[`dashboard/docs/reference-deployment.md`](https://github.com/rjwalters/loom/blob/main/dashboard/docs/reference-deployment.md)
only records the *shape* such a document should take (which values to
capture, and why) so you can produce the equivalent for your own instance.

## Map of every detail doc

| Doc | Covers |
|---|---|
| [`.loom/docs/telemetry-schema.md`](telemetry-schema.md) | Wire envelope, record kinds, visibility contract, local journal |
| `dashboard/docs/deploy-runbook.md` | Deploy your own Cloudflare backend end to end |
| `dashboard/docs/cloudflare-access.md` | Gating the authenticated view behind SSO; single-URL fallback |
| `dashboard/docs/query-api.md` | `/api/*` vs `/public/*` routes, redaction policy, live tail |
| `dashboard/docs/token-analytics.md` | Burn curves, forecasting, per-repo attribution |
| `defaults/scripts/guide-docs-telemetry.sh` | Local doc-maintenance throughput telemetry (§5b) — record + report, no daemon/Cloudflare involvement |
| `defaults/scripts/merge-admission-telemetry.sh` | Local merge-admission-recheck outcome telemetry (§5c) — record + report, no daemon/Cloudflare involvement |
| `dashboard/docs/reference-deployment.md` | Generic guidance/template for recording your own instance's deployment identity in your own infrastructure repo — carries no operator identity here |
| `loom-daemon/src/observability/mod.rs` | Config resolution, collector/queue/exporter/sender source of truth |
