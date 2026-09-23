# Instrumentation overhead on a representative run

`loom-daemon telemetry-overhead` answers one narrow, checkable question: when
Loom traces a sweep, how much wall time and how many bytes does the
instrumentation itself add, and do the emitted attributes/events stay inside
their declared caps?

It is **not** the synthetic fixture generator
([`telemetry-fixtures.md`](telemetry-fixtures.md)). That generator hand-builds
span records and therefore could not measure instrumentation cost at all. This
harness drives the real path instead: the same `observability::lifecycle` entry
points a dispatched sweep uses, the same journal fsync per boundary, the same
backfill drain onto a durable queue, and the same `SpanRecord::bounded`
emission policy.

```console
loom-daemon telemetry-overhead --repetitions 5 --output ./overhead.json
```

Both measured arms run inside throwaway workspaces. No provider, forge, or
network endpoint is contacted. The only path read outside those workspaces is
the reference workspace's own sweep-outcome journal, read-only.

| Flag | Meaning |
| --- | --- |
| `--repetitions N` | Runs per arm; every reported time is a median over `N`. Default 5. |
| `--tools-per-attempt N` | Owned `loom.tool` spans per role attempt. A **parameter of the shape being measured**, not a measured fleet average — Loom owns a tool span only at its own native-tool bridge, so the true count depends on the runtime. Default 4. Also the knob that varies span count, for ["How overhead scales with span count"](#how-overhead-scales-with-span-count). |
| `--reference-workspace DIR` | Workspace whose recorded sweep outcomes supply the reference denominator. Read-only; defaults to the current directory. |
| `--output PATH` | Also write the report here. It is always printed to stdout. |

The command requires an OTLP-enabled binary. Without the `otlp` feature nothing
is instrumented, so it refuses rather than reporting a zero that would read as
"free". Check with `loom-daemon telemetry-capabilities --require-otlp`.

## Measure a release build, and say which build you measured

A debug build's absolute nanosecond figures are not the fleet's. Measure the
same profile the fleet runs, and record the profile alongside the numbers —
an overhead figure without its build profile, host and commit is not evidence.

```console
cargo build --release -p loom-daemon --features otlp
/path/to/target/release/loom-daemon telemetry-overhead --output ./overhead.json
```

## The representative run

The default shape is the repair waterfall this instrumentation exists to make
legible: a Builder success, a **rejected** Judge, a Doctor recovery, a second
**accepted** Judge, then merge. That is deliberately the longest ordinary
lifecycle, so the reported overhead is an upper bound among normal outcomes
rather than a best case. With the default four tool spans per attempt it
persists 41 spans — one sweep root plus phase/attempt/preflight/run/tools for
each of the five phases.

The same execution is both the measurement and the shape fixture: the unit
tests assert the waterfall nests correctly and that both Judge attempts survive
as distinct spans with distinct attempt numbers, using the very run whose cost
is reported. A passing overhead number therefore cannot describe a span graph
nobody checked.

## Reading the report

`added_median_ns` is the instrumentation cost of one representative run:
median instrumented wall time minus the median of the identical call sequence
with tracing disabled. With tracing off every lifecycle entry point
short-circuits, so the baseline is the same program minus the instrumentation,
not a different one. `measure` asserts up front that one arm is tracing and the
other is not — an ambient `LOOM_OBSERVABILITY_*` override that silently
equalised the arms would otherwise produce a confidently wrong number.

`bytes` reports what the run persisted, because an exporter that is fast but
writes megabytes is not cheap: `journal_bytes` on disk before the durable
drain, and `bounded_record_bytes` for the span records handed to the exporter
after bounding. The fixed per-batch OTLP resource/scope envelope is excluded
because it does not scale with span count.

`bounds` is realised, not declared — the largest attribute value, attribute
count, event count and link count actually observed after `bounded()`, plus the
sorted list of attribute keys that survived the allowlist. The list is reported
so a reviewer can check it instead of trusting a boolean.

`reference` is the denominator, and it is **observed, not invented**: p50/p90 of
this host's own recorded sweep durations, with the sample size named.
Zero-length records are excluded as unmeasured runs rather than counted as
zero-second sweeps, which would deflate the denominator. Percentiles are
nearest-rank over the sorted sample, never interpolated, so each one is a
duration the host actually recorded — over ten records p90 is the ninth value,
not the maximum. A host with no recorded history reports `reference` and
`overhead_fraction_of_p50` as **absent** — an unknown denominator is never
rendered as zero or guessed.

`excludes` names what the number is not, so no reader mistakes it for
end-to-end cost: network export latency to a real backend, backend
ingestion/indexing, and any provider-side cost. A run with no model work has no
model cost to attribute, and this harness never calls a provider.

## Recorded measurements

Point-in-time records, **not a budget and not a threshold** — nothing gates on
these numbers, and they are kept only so a later measurement has something to be
compared against. Re-run the command rather than citing this table as current.

Two runs are kept, not one. The first is a debug build on a saturated host; the
second is a release build on a working-but-not-saturated host. **The pair is the
point** — a single row cannot tell a reader how much of a figure was the build
profile and how much was the machine it ran on.

| Field | A — debug, saturated | B — release, non-saturated |
| --- | --- | --- |
| Measured | 2026-09-22, from the working tree that introduced this command | 2026-09-22, re-measured post-#8642 (drain-to-completion `run_once`), same session as the ladder below |
| Build | `debug` profile, `--features otlp`, macOS aarch64 | `release` profile, `--features otlp`, rustc 1.96.0, Linux x86_64 |
| Host | (not recorded beyond load) | 8-vCPU Xeon 8488C, ext4 (the harness's temp workspaces are on the same filesystem as the repo, so its fsyncs are real disk fsyncs) |
| Host state | 1-minute load average ≈ 37 (a busy multi-sweep host, not an idle one) | 1-minute load average ≈6.6, i.e. ≈82% of 8 cores. A working fleet host with concurrent agent processes — **not idle**, and not claimed to be |
| Shape | 5 phases (repair waterfall), 4 tool spans per attempt, 41 spans, 5 repetitions | identical |
| `added_median_ns` | 5,572,089,583 (≈5.57 s per representative run) | 977,210,046 (≈0.98 s per representative run) |
| `added_ns_per_span` | 135,904,623 (≈136 ms per span boundary pair) | 23,834,391 (≈23.8 ms per span boundary pair) |
| `journal_bytes` | 44,751 | 45,565 |
| `bounded_record_bytes` | 18,661 (`bytes_per_span` 455) | 18,907 (`bytes_per_span` 461) |
| `bounds` | max attribute value 14 B, 7 attributes, 0 events, 0 links per span | identical |
| `reference` | 712 observed sweeps, p50 109 s, p90 2,846 s | 410 observed sweeps, p50 103 s, p90 3,279 s |
| `overhead_fraction_of_p50` | 0.0511 | 0.0095 |

B's prior figures (measured at `5ee4fd120`, #8614) were 1,187,124,055 ns
added / 28,954,245 ns per span. The row above is a deliberate re-measurement,
not a drift: `run_once` now drains its journal to completion instead of once
(#8642, see ["The harness no longer caps at ~256
spans"](#the-harness-no-longer-caps-at-256-spans) below), which adds one extra
no-op `backfill` call to every instrumented run. That call is a single
`read_dir` against an already-retired (and thus empty or absent)
trace-context directory — no journal entries to drain, no fsyncs — so it
cannot explain the drop; the difference here is host-load noise between
sessions, consistent with the ±12% run-to-run spread this same host shows
within one session (see the ladder below). B is reproducible verbatim; the
reference denominator is this host's own recorded history, so its `reference`
row will differ elsewhere:

```console
cargo build --release -p loom-daemon --features otlp
./target/release/loom-daemon telemetry-capabilities --require-otlp   # {"otlp":true,…}
./target/release/loom-daemon telemetry-overhead \
    --repetitions 5 --reference-workspace /path/to/loom
```

Read both with their conditions attached. A is a **debug build on a saturated
host measuring the longest ordinary lifecycle** — an upper bound, not a fleet
figure. B is what this fleet host actually pays: <1% of its p50 observed
sweep, and ≈0.03% against p90 (3,279 s). The p50 is small (103 s) because
short-lived and failed dispatches are sweeps too, so the p50 fraction is the
conservative reading of the two, not the representative one.

B is ≈5.7× cheaper than A, but **that ratio cannot be attributed to the build
profile alone**: the two rows differ in profile, in host load, *and* in
machine/OS/architecture. Nothing here isolates those three, and no attempt is
made to. What the pair does establish is that the 136 ms/span figure was not the
fleet's, and ≈24-34 ms/span is — on this host, under this load, and rising
with span count (see the next section).

The `bounds` row is the acceptance-relevant half, and is identical in both:
every attribute key emitted survived the allowlist, and the realised maxima sit
far under the declared caps (256 B per value, 32 events, 16 links).

## How overhead scales with span count

41 spans is one shape. Because `Journal::start`/`finish` each re-read and
re-parse the whole journal under the file lock, per-execution cost has an O(n²)
term in span count, which 41 spans would not reveal. `--tools-per-attempt`
varies the span count without changing anything else, so the curve is directly
measurable (release build, same host, same session, 3 repetitions per point
except the first):

| `--tools-per-attempt` | Spans | `added_median_ns` | Per span | Journal |
| --- | --- | --- | --- | --- |
| 4 (default) | 41 | 0.98 s | 23.8 ms | 44.5 KiB |
| 12 | 81 | 2.05 s | 25.3 ms | 88.9 KiB |
| 20 | 121 | 3.10 s | 25.6 ms | 133.3 KiB |
| 28 | 161 | 4.35 s | 27.0 ms | 177.7 KiB |
| 40 | 221 | 5.98 s | 27.1 ms | 244.3 KiB |
| 60 | 321 | 9.35 s | 29.1 ms | 355.2 KiB |
| 80 | 421 | 13.23 s | 31.4 ms | 466.2 KiB |
| 124 | 641 | 21.71 s | 33.9 ms | 710.3 KiB |

**Past 221 spans, per-span cost does rise, and now measurably.** Over the
41→641 span range (15.6×) `added_ns_per_span` climbs from 23.8 ms to 33.9 ms,
a 42% increase, and it climbs monotonically once the shape passes ~161 spans —
unlike the flat, no-trend 41→221 span segment, where the same 23-27 ms values
are within this host's own ±12% run-to-run spread (at 221 spans three
repetitions ranged 5.90–6.22 s) and could not be told apart from noise. A
line fit to the two endpoints (`per_span_ms ≈ 23.9 + 0.0156 × spans`) predicts
every interior point to within ~3%, consistent with the re-parse term now
being large enough to show up as a genuine trend rather than session-to-session
jitter. The 41-span row here is the same shape as row B above, re-measured in
this session for a like-for-like comparison across the ladder.

What dominates the *flat* part of that constant is measured, not assumed.
`strace` on one 41-span run counts **417 durability barriers** — 82
`fdatasync` on the journal (two per span: `Started`, `Completed`), 170 `fsync`
on the trace-context directory, 124 `fsync` on cursor/context temp files, 41
`fsync` on the durable queue — about 10 per span. A measured `fdatasync` on
this host's ext4 costs ≈2.7 ms (median over 200, p90 2.95 ms), so 10
barriers/span predicts ≈27 ms/span, matching the 41-221 span segment. It does
not explain the rise from 221 to 641 spans, where the per-boundary barrier
count per span is unchanged (still ~10) but the whole-journal re-parse in
`Journal::start`/`finish` grows with journal length — the O(n²) term the flat
segment could not resolve.

### The harness no longer caps at ~256 spans

Before #8642, above roughly 256 spans the command failed with `instrumented
run persisted 255 spans, expected N`. The cause was not a telemetry limit:
`Journal::drain` processes at most 512 journal entries per call, each span
writes two, and `measure()`'s `run_once` called `backfill` exactly once before
asserting that every declared span had arrived — so any shape needing more
than one drain pass under-counted. **Production was never affected** — the
drain cursor is persisted, the next backfill pass resumes from it, and
`retire_if_drained` refuses to retire a journal whose cursor is short of EOF,
so nothing was dropped. It was the *measurement* that was single-pass.

`run_once` now calls `backfill` in a loop until a pass drains zero spans, so
the measured window covers a run's full export regardless of span count. The
641-span row above (`--tools-per-attempt 124`) is the shape that used to fail
outright; it now completes and reports like any other.

## Is the per-boundary durable write acceptable?

**Yes — keep it. Do not batch the journal write.** Stated explicitly so the
trade-off is not left to inference:

- The cost is <1% of this host's p50 observed sweep and ≈0.03% of its p90.
  Even the debug/saturated upper bound is ≈5% of p50.
- The per-boundary journal append is only about **40%** of the measured cost:
  4 of the ~10 barriers per span (2 `fdatasync` on the journal, 2 directory
  `fsync` from the lock path). Batching it could not remove more than that, and
  would trade away the property the design exists for — a trace that survives a
  crash mid-sweep (#8579), which is exactly the sweep whose trace is worth most.
- The remaining **~60%** (≈6 barriers per span) is drain and queue bookkeeping:
  the cursor is persisted per drained *entry* (a temp-file `fsync`, a rename,
  and a directory `fsync` each time), plus one queue `fsync` per span and the
  trace-context store's own atomic replaces. In a live daemon that
  drain runs on the collector's periodic `spawn_blocking` pass, **not** on the
  sweep's critical path — so the sweep-visible cost is lower than the recorded
  figure, which folds the drain into the measured window.

If this ever does need to get cheaper, two **durability-neutral** savings come
first, before anything that weakens the journal (both tracked as #8643):

1. `Journal::lock` `fsync`s the parent directory on *every* call. That is needed
   only to durably link a newly created journal file; on every subsequent
   boundary the directory entry already exists. ≈2 barriers per span.
2. `Journal::drain` advances the cursor once per entry rather than once per
   batch. ≈4 barriers per span, in the export path rather than the trace path.

Neither changes what survives a crash of the traced process. Batching the
per-boundary write does, and is therefore the last option, not the first.

## What this cannot establish

This is an offline measurement of Loom's own instrumentation. It does not, and
must not be read to, establish backend ingestion behaviour, export latency
against a real endpoint, resolved provider/model identity for a live run, or
that backend log links resolve to the relevant spans. Those need an authorized
live run against a configured backend and are tracked as separate evidence.
