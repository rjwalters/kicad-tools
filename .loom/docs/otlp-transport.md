# OTLP transport and artifact verification

Loom's published daemon artifacts include the `otlp` feature. Export remains
**disabled by default**; a local `cargo build` still omits this optional dependency.
For source installs use `cargo build -p loom-daemon --release --features otlp`.
After installing or updating, run `loom-daemon telemetry-capabilities --require-otlp`.
An enabled OTLP configuration on a feature-off binary reports `misconfigured`.

The daemon sends OTLP/HTTP JSON to the configured base URL's `/v1/logs` and
`/v1/metrics` paths. Lifecycle and role-tick records become logs; host health and
token-pool observations become gauge data points. Unknown observations remain
absent. The existing key-file credential is sent as a Bearer header. Configure
backend-specific authentication on the neutral Collector's outgoing exporters;
provider keys such as `ZAI_API_KEY` are unrelated to telemetry ingestion.

## Delivery and counters

OTLP responses follow the [OTLP specification](https://opentelemetry.io/docs/specs/otlp/).
Loom requires HTTP 200 and a valid JSON response, bounded to 4096 bytes. Receiver
error text is never retained because it can echo secrets or private payloads.

- Complete success acknowledges the request. A warning with zero rejected items
  increments `warnings`, without inventing rejected records.
- Partial success acknowledges the whole request and records accepted/rejected
  item counts. Rejected items are **never retried**.
- HTTP 429/502/503/504 and transport/timeouts leave items queued for the existing
  jittered retry loop. Other HTTP statuses permanently drop that request.
- Malformed/oversized HTTP 200 responses are permanent drops with acceptance
  explicitly unknown. This prevents one bad response from starving later data.
  No quarantine payload is retained, so quarantine storage is bounded at zero.

`observability_export.signal_counts` in daemon JSON status is keyed by
`log_records`, `metric_data_points`, and, when emitted, `spans`. Each entry has
`accepted`, `rejected`, `dropped`, `retry_scheduled`, and `warnings` counters.
Counters are process-lifetime totals. `retry_scheduled` counts attempted items
left queued after a transient failure, not unique records. Queue-capacity drops
remain the durable queue's separate envelope counter. The older
`records_exported` field counts **fully accepted envelopes**; a partially rejected
group contributes zero because the receiver does not identify the rejected
individual envelopes. Use signal counters for actual accepted item counts.

Contiguous same-signal groups are sent in queue order. A later signal failure
preserves the acknowledged prefix when the sender updates the durable queue;
retries only send the remaining suffix. A crash before that durable update or a
lost response can still replay data: delivery remains at least once. For dedupe,
use the exact stored envelope identity `(host_id, emitted_at, record)` (the replay
does not regenerate timestamps); metrics retain resource, name, attributes and
nanosecond timestamp. HTTP acknowledgment establishes receiver acceptance, not
backend query visibility. Verify the latter separately.

## Explicit artifact canary

The Rust one-shot driver sends only the supplied JSONL fixture and never reads a
provider credential or silently enables daemon export:

```console
loom-daemon telemetry-export --input envelopes.jsonl --endpoint http://127.0.0.1:4318 --key-file /private/path/collector-ingress.key
```

It bounds input to 1 MiB/1000 envelopes, rejects placeholder endpoints before
reading the key, prints acknowledgment/counter JSON, and exits unsuccessfully
for partial rejection, drops or retries. It performs no automatic retries.
Use synthetic fixtures; it assumes its input is already privacy-filtered, just
like the normal daemon collector's output. It is not a redaction tool.

The pinned Collector fixture lives under
`loom-daemon/tests/fixtures/otlp_transport/`. The Rust integration test starts only
its named container on a random loopback port, invokes the built daemon, and
inspects the Collector's independently decoded file output for lifecycle and
role-tick logs, host/token metrics, timestamps, values and absent measurements.

```console
cargo test -p loom-daemon --features otlp --test otlp_collector -- --ignored --nocapture
```

Docker is required and absence is a failure when explicitly invoked. The regular
suite marks this test ignored; the OTLP CI job explicitly runs it. To validate a
release or installed artifact instead of Cargo's debug binary, set
`LOOM_OTLP_TEST_BINARY` to its absolute path when invoking the same test. The
release workflow runs this Collector canary against the Linux x64 release binary
and the capability command on each native target (the Linux arm64 cross-build
cannot execute on its x86 runner).

For a contended development host, the same release workflow has a hosted-only
full gate mode. Supply `gate_sha` (the complete immutable candidate commit) and
`gate_issue` through `gh workflow run release.yml --ref <workflow-ref> -f
gate_sha=<40-character-sha> -f gate_issue=<issue>`. This mode skips release and
image jobs and runs the configured `bash .loom/scripts/build-gate.sh` in an
exact-candidate managed worktree with a fresh target directory and nextest.
It retains the actual command output plus candidate/base/path evidence as an
Actions artifact. No cached or equivalent-suite verdict is substituted.
The orchestrator still evaluates the separate real-change predicate; any
explicitly authorized scope exception must be documented independently.
