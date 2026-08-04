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
Cloudflare Worker backend (deploy-your-own, or the 2AM reference instance)
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

Add the `observability` block to that host's `.loom/config.json`:

```json
{
  "observability": {
    "enabled": true,
    "endpoint": "https://<your-worker>.workers.dev/ingest",
    "ingestKeyFile": "/etc/loom/observability-ingest.key",
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
| `ingestKeyFile` | `LOOM_OBSERVABILITY_INGEST_KEY_FILE` | unset (disables export) |
| `batchSize` | `LOOM_OBSERVABILITY_BATCH_SIZE` | 50 |
| `flushIntervalSecs` | `LOOM_OBSERVABILITY_FLUSH_INTERVAL_SECS` | 30 |
| `queueCapacity` | `LOOM_OBSERVABILITY_QUEUE_CAPACITY` | 2000 |
| `exporter` | `LOOM_OBSERVABILITY_EXPORTER` | `"https"` (or `"otlp"`, §3) |

The ingest key is **never inline in config** — `ingestKeyFile` is a path the
daemon reads once at startup and holds only in memory, sent solely as an
`Authorization: Bearer` header. A misconfigured block (missing endpoint or
key file) degrades to off; it does not crash the daemon. Source of truth:
`loom-daemon/src/observability/mod.rs`'s module doc (config resolution,
FLAGS-OFF posture, read-only invariant) and its `collector.rs` / `queue.rs` /
`exporter.rs` / `sender.rs` siblings (collector, durable queue, exporter
trait + HTTPS implementation, retry-drain loop).

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
Observability: OK — last export 12s ago, 3481 record(s) as host_id=robb-studio → https://…/ingest
```

The same facts are machine-readable under `observability_export` in
`loom-daemon status --json`, so a watch loop can assert health instead of
inferring it from silence:

```bash
loom-daemon status --json | jq -e '.observability_export.state == "healthy"'
```

| `state` | Meaning | Rendered as |
|---|---|---|
| `disabled` | Exporter not running: `enabled=false`, or enabled but under-configured (no endpoint / no readable ingest key / `otlp` without the Cargo feature) | `Observability: disabled …` |
| `starting` | Running, nothing acked yet, still inside the grace window (3 × `flushIntervalSecs`, floored at 10 min) — a just-rolled daemon, not a fault | `Observability: starting …` |
| `never_exported` | Running well past the grace window and **no batch has ever been acked** — the silent failure mode | `Observability: NEVER EXPORTED …` |
| `healthy` | Batches are being acked and the ids agree | `Observability: OK …` |
| `host_id_mismatch` | Batches are being acked, but under a different `host_id` than this daemon reports for itself (the #4830 condition, §3 above) | `Observability: HOST-ID MISMATCH …` |
| `failing` | The most recent flush attempt errored; the queue is retrying with backoff | `Observability: FAILING …` |

`observability_export` also carries `host_id`, `endpoint`, `exporter`,
`started_at`, `last_success_at`, `last_failure_at`, `last_failure_detail`,
`records_exported`, `consecutive_failures`, and `flush_interval_secs`. A `null`
`observability_export` means the daemon binary predates #5083 — "cannot tell",
never "disabled"; restart the daemon onto a current binary.

The health section keeps its anomaly-only contract. It now recognizes two
additional *non-green* conditions — `never_exported` and `failing` — which are
anomalies by the same rule that already admitted `host_id_mismatch`; `healthy`,
`starting`, and `disabled` still render nothing at all. When a section does
render, its `detail` payload carries the full `observability_export` record, so
a machine consumer of `loom-daemon health --json` gets the positive facts too.

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

## 6. The 2AM reference instance

`dashboard.2amlogic.com` is a live, operator-owned deployment of this same
backend (not a shared Loom service — every fleet deploys its own). Its
specific account/database IDs, Access application layout, credential file
locations, and cutover history (the hostname-wide Access app was retired in
favor of the single-URL `/login`-scoped layout on 2026-07-31) are recorded
in [`dashboard/docs/reference-deployment.md`](https://github.com/rjwalters/loom/blob/main/dashboard/docs/reference-deployment.md)
— useful as a concrete filled-in example of every value the deploy runbook
asks you to supply, not as a second copy of the how-to.

## Map of every detail doc

| Doc | Covers |
|---|---|
| [`.loom/docs/telemetry-schema.md`](telemetry-schema.md) | Wire envelope, record kinds, visibility contract, local journal |
| `dashboard/docs/deploy-runbook.md` | Deploy your own Cloudflare backend end to end |
| `dashboard/docs/cloudflare-access.md` | Gating the authenticated view behind SSO; single-URL fallback |
| `dashboard/docs/query-api.md` | `/api/*` vs `/public/*` routes, redaction policy, live tail |
| `dashboard/docs/token-analytics.md` | Burn curves, forecasting, per-repo attribution |
| `dashboard/docs/reference-deployment.md` | The 2AM instance specifically — concrete IDs, current state |
| `loom-daemon/src/observability/mod.rs` | Config resolution, collector/queue/exporter/sender source of truth |
