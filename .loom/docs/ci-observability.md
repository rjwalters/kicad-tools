# CI Observability: every build and CI run is captured in SigNoz

> Standing policy, issue [#8827](https://github.com/rjwalters/loom/issues/8827)
> (phase 4 of the build/CI-in-SigNoz set under epic
> [#8522](https://github.com/rjwalters/loom/issues/8522)). This page is the
> policy and an operating map — it states what must be captured and links the
> detail docs that say how. It is not a duplicate of them, and it should stay
> a map as they grow.

## The policy

**Every GitHub Actions run of every `2amlogic` repository — its runs, jobs,
durations, outcomes, and completed-job logs — is captured in SigNoz** by the
`loom-daemon ci-telemetry` poller. Capture is **org-scoped**: a new repo or a
new workflow is in scope on the day it appears, found by auto-discovery, with
no per-repo enrollment step to forget. **Run/job duration and outcome metrics
are never excludable** — not per repo, not per workflow, not temporarily. **Log
capture may be excluded for one repo only with a stated reason recorded in
that config entry itself**, reviewed like any other config change; an
exclusion without a reason is a policy violation, and there are no silent or
undocumented exclusions.

Why this is a standing rule rather than a one-off investigation: build and CI
time regressions are felt long before they are seen. The #7779 cancellation
storm (of the last 30 `main` runs, 22 cancelled) got a fix, but it was
weeks-invisible because nothing recorded CI time or outcome over time. This is the
observability face of the same family as
[`ci-principles.md`](ci-principles.md) — a check that cannot run must not look
like a check that passed, and a CI system nobody measures cannot be trusted to
be getting better.

## The pipeline, in one picture

```
GitHub Actions (every 2amlogic repo, auto-discovered)
        │  REST: org repos (ETag-cached) → runs (created_after watermark) → jobs
        │        → completed-job log zip (phase 2)
        ▼
loom-daemon ci-telemetry poller (per host; one poller is the normal case)
  durable dedup ledger  .loom/state/ci-telemetry/seen.jsonl  (exactly once per job)
  local journal         .loom/logs/ci-telemetry.jsonl        (written with no exporter)
        │  records: ci.run, ci.job, ci.job.log
        │  metrics: loom.ci.run.duration_ms, loom.ci.job.duration_ms
        │  traces:  one trace per run, one span per job
        ▼
observability OTLP exporter (the existing durable queue + drain loop)
        │
        ▼
neutral OTLP gateway — the REDACTION BOUNDARY
  ci.job.log scrub stage → shared transform/privacy allowlist (keep_keys)
        │
        ▼
SigNoz (trial deployment; managed cloud optional) — the named retro surface
```

Nothing new is invented for transport: the poller is a new **source** on the
pipeline [`observability.md`](observability.md) already documents. Where each
hop is specified:

| Hop | Detail doc |
|---|---|
| Record kinds, envelope, local-journal conventions | [`telemetry-schema.md`](telemetry-schema.md) |
| OTLP exporter, response classification, retry/drop policy | [`otlp-transport.md`](otlp-transport.md) |
| Exporter config, fan-out, "is it actually flowing" | [`observability.md`](observability.md) §1, §3, §3b |
| Gateway: allowlist, privacy stance, contract tests | [`defaults/observability/collector/README.md`](https://github.com/rjwalters/loom/blob/main/defaults/observability/collector/README.md) |
| SigNoz trial: deploy, saved views, retention mechanics | [`defaults/observability/signoz/README.md`](https://github.com/rjwalters/loom/blob/main/defaults/observability/signoz/README.md) |
| Why CI is measured at all | [`ci-principles.md`](ci-principles.md) |

## Phases

| Phase | Delivers | Issue | Status |
|---|---|---|---|
| 1 | `loom-daemon ci-telemetry` poller: `ci.run`/`ci.job` records, duration histograms, run→job traces, dedup ledger, local journal, `status` | [#8824](https://github.com/rjwalters/loom/issues/8824) | Open |
| 2 | Full completed-job logs as chunked `ci.job.log` records, with secret redaction enforced at the gateway | [#8825](https://github.com/rjwalters/loom/issues/8825) (blocked by #8824) | Open |
| 3 | SigNoz retro surfaces (`ci-queries.sql`, six saved views) and the metrics ≥30d retention split | [#8826](https://github.com/rjwalters/loom/issues/8826) (blocked by #8824/#8825) | Open |
| 4 | This policy doc and its wiring | [#8827](https://github.com/rjwalters/loom/issues/8827) | This doc |

Each phase adds its own reference section (config keys, dedup contract,
journal schema, chunking contract, standing queries) to this page when it
lands. Until phase 1 merges, the policy is stated but **not yet enforced by
any running code** — no host captures CI telemetry today.

## Capture scope & exclusions

- **Scope is the org, not a repo list.** The `org` key of the
  `autonomous.ciTelemetry` config block (planned default `2amlogic`, defined by #8824) names the captured org; repo
  discovery walks it on every poll. There is no allowlist of repos to keep in
  sync.
- **Metrics and run/job records are unconditional.** Durations, outcomes
  (success, failure, **cancelled** — a first-class outcome, never noise), and
  the `ci.run`/`ci.job` records are emitted for every repo in the org. A
  config surface that would suppress them for a repo does not satisfy this
  policy; phase 1's repo-exclusion key must be limited accordingly (or carry
  the same reason requirement and be treated as a policy exception reviewed
  here).
- **Log capture is the only excludable signal**, per repo, and each exclusion
  is a config entry **with a `reason` field** stating why (e.g. a repo whose
  logs cannot be adequately scrubbed yet). The exact key shape is defined by
  #8824 (`logCaptureEnabled` gate) and #8825 (log download); whatever shape
  lands, an entry without a reason is rejected in review.
- **Exclusions are reviewed like any other config change** — they live in the
  committed `.loom/config.json`, never in a host-local override tier, so they
  are visible in `git log` and to every host.

## Retention

| Signal | Retention | Why |
|---|---|---|
| Metrics (`loom.ci.*.duration_ms`, outcome counts) | **≥ 30 days** | Trends are the retro asset — "is CI getting slower" needs weeks of history, and metrics are cheap relative to logs |
| Logs (`ci.job.log`) and traces | **7 days** | Raw detail is for recent investigation; a regression is found by trend, then read in a recent run |

Trends outlive raw data by design. Implementation — the SigNoz retention
settings plus `retention.sql` for tables the pinned API misses, verified
against effective ClickHouse DDL rather than the API setting — is owned by
[#8826](https://github.com/rjwalters/loom/issues/8826); the signoz README's
"Retention and operation" section documents the current (pre-#8826) seven-day
trial setting.

## Redaction policy

**The gateway is the redaction boundary** (operator decision). The daemon
sends what GitHub sent, chunked and size-capped, with no pre-filtering; the
gateway's `ci.job.log`-specific transform stage scrubs secrets before the
shared `transform/privacy` allowlist, and both sinks receive only the scrubbed
output. The scrubber list lives in the repo, and a new secret family is added
to the list **and** the contract test in the same PR.

Scrub classes (each replaced with `[REDACTED:<class>]`), from #8825's design:

| Class | Matches |
|---|---|
| GitHub tokens | `ghp_`, `gho_`, `ghu_`, `ghs_`, `ghr_`, `github_pat_` prefixes |
| Anthropic keys | `sk-ant-` prefix |
| Other `sk-` bearer shapes | only when preceded by a key-ish context word (to avoid scrubbing prose) |
| AWS | `AKIA[0-9A-Z]{16}` access key IDs; `aws_secret_access_key` assignments |
| Auth headers | `Bearer <token>` and `Authorization:` header lines |
| Credential assignments | `password=…`, `secret=…`, `token=…` values |

`ci.job.log` is the **first and only** record kind whose body survives
`transform/privacy`; the exception is scoped to that kind and named
explicitly in the gateway's allowlist contract test. No other kind's handling
changes.

What this store is and is not: SigNoz here is a **trusted private operational
store**, not the public dashboard projection — the same framing as the
collector README's "Privacy and remote deployment" section. Nothing captured
under this policy flows to `/public/*` or any unauthenticated view.

## Rollout

Enabling capture is a per-host config operation, performed once #8824 (and,
for logs, #8825) has merged and the SigNoz trial is confirmed receiving:

1. The host already exports OTLP to the gateway — an `observability.exporters`
   entry `{ "kind": "otlp", "endpoint": "<gateway>" }` per
   [`observability.md`](observability.md) §3, confirmed healthy with
   `loom-daemon status --json | jq -e '.observability_exports.otlp.state == "healthy"'`.
2. Enable the poller with `autonomous.ciTelemetry.enabled = true` (FLAGS-OFF
   by default; `LOOM_CI_TELEMETRY_*` env overrides, **env > config >
   default**). Log capture is a separate gate (`logCaptureEnabled`, phase 2).
3. Confirm with `loom-daemon ci-telemetry status` — it distinguishes
   never-polled, last-ok + age, and failing + last error, so silence never
   reads as healthy.

**One poller is the normal case.** Records carry stable `run_id`/`job_id`
identities so a second poller on another host is deduplicable downstream, but
there is no per-repo lease protocol and none should be invented (one mechanism
per behaviour). Pick one fleet host to run capture.

**Destination.** The self-hosted SigNoz trial is the destination. SigNoz Cloud
remains optional — swapping only the gateway's exporter endpoint, per the
signoz README — and is not a prerequisite for this policy.
