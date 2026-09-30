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

Three runs are kept. The first is a debug build on a saturated host; the
second and third are both release builds on the same fleet host, before and
after the two durability-neutral fsync reductions from #8643 (`Journal::lock`
skips the parent-directory `fsync` once the journal already holds a record,
and `Journal::drain` commits the cursor once per delivered record instead of
once per journal entry). **The B/C pair is the point** — same host, same
shape, same command; the only thing that changed between them is the code.

| Field | A — debug, saturated | B — release, post-#8642 | C — release, post-#8643 |
| --- | --- | --- | --- |
| Measured | 2026-09-22, from the working tree that introduced this command | 2026-09-22, re-measured post-#8642 (drain-to-completion `run_once`), same session as the ladder below | 2026-09-22, from the working tree that closed #8643 |
| Build | `debug` profile, `--features otlp`, macOS aarch64 | `release` profile, `--features otlp`, rustc 1.96.0, Linux x86_64 | identical |
| Host | (not recorded beyond load) | 8-vCPU Xeon 8488C, ext4 (the harness's temp workspaces are on the same filesystem as the repo, so its fsyncs are real disk fsyncs) | identical |
| Host state | 1-minute load average ≈ 37 (a busy multi-sweep host, not an idle one) | 1-minute load average ≈6.6, i.e. ≈82% of 8 cores. A working fleet host with concurrent agent processes — **not idle**, and not claimed to be | 1-minute load average ≈ 11.1 before and after, i.e. ≈140% of 8 cores — busier than B, not idle |
| Shape | 5 phases (repair waterfall), 4 tool spans per attempt, 41 spans, 5 repetitions | identical | identical |
| `added_median_ns` | 5,572,089,583 (≈5.57 s per representative run) | 977,210,046 (≈0.98 s per representative run) | 648,161,508 (≈0.65 s per representative run) |
| `added_ns_per_span` | 135,904,623 (≈136 ms per span boundary pair) | 23,834,391 (≈23.8 ms per span boundary pair) | 15,808,817 (≈15.8 ms per span boundary pair) |
| `journal_bytes` | 44,751 | 45,565 | 45,647 |
| `bounded_record_bytes` | 18,661 (`bytes_per_span` 455) | 18,907 (`bytes_per_span` 461) | identical to B |
| `bounds` | max attribute value 14 B, 7 attributes, 0 events, 0 links per span | identical | identical |
| `reference` | 712 observed sweeps, p50 109 s, p90 2,846 s | 410 observed sweeps, p50 103 s, p90 3,279 s | 412 observed sweeps, p50 105 s, p90 3,279 s |
| `overhead_fraction_of_p50` | 0.0511 | 0.0095 | 0.0062 |

B's prior figures (measured at `5ee4fd120`, #8614) were 1,187,124,055 ns
added / 28,954,245 ns per span. The row above is a deliberate re-measurement
for #8642, not a drift: `run_once` now drains its journal to completion
instead of once (see ["The harness no longer caps at ~256
spans"](#the-harness-no-longer-caps-at-256-spans) below), which adds one extra
no-op `backfill` call to every instrumented run. That call is a single
`read_dir` against an already-retired (and thus empty or absent)
trace-context directory — no journal entries to drain, no fsyncs — so it
cannot explain the drop; the difference here is host-load noise between
sessions, consistent with the ±12% run-to-run spread this same host shows
within one session (see the ladder below). B is reproducible verbatim against
its own stated conditions; the reference denominator is this host's own
recorded history, so its `reference` row will differ elsewhere.

C isolates a *different*, independent change (#8643) against its own
pre-change baseline, measured on the same host in the same session as C
itself: the pre-#8643 baseline read 1,187,124,055 ns added / 28,954,245 ns per
span (the same `5ee4fd120` figures B superseded above, since C's branch point
predates #8642), falling to 648,161,508 ns / 15,808,817 ns per span after —
a ≈45% drop in per-span cost attributable to #8643 alone. Because B and C's
baselines were captured in different sessions under different host load, B and
C are each independently reproducible verbatim against their own stated
conditions, but the two are not a clean pair against *each other* — the
`reference` row differs between them for that reason.

```console
cargo build --release -p loom-daemon --features otlp
./target/release/loom-daemon telemetry-capabilities --require-otlp   # {"otlp":true,…}
./target/release/loom-daemon telemetry-overhead \
    --repetitions 5 --reference-workspace /path/to/loom
```

Read all three with their conditions attached. A is a **debug build on a
saturated host measuring the longest ordinary lifecycle** — an upper bound,
not a fleet figure. B is what this fleet host pays after #8642's
drain-to-completion fix: <1% of its p50 observed sweep, and ≈0.03% against p90
(3,279 s). C, measured separately against #8643's own pre-change baseline on
the same host, shows a further ≈45% per-span reduction attributable to that
specific fsync-skipping change. The p50 is small (103–105 s) in both B and C
because short-lived and failed dispatches are sweeps too, so the p50 fraction
is the conservative reading of the two, not the representative one.

B is ≈5.7× cheaper than A, but **that ratio cannot be attributed to the build
profile alone**: the two rows differ in profile, in host load, *and* in
machine/OS/architecture. Nothing here isolates those three, and no attempt is
made to. What the pair does establish is that the 136 ms/span figure was not
the fleet's, and ≈24-34 ms/span is — on this host, under this load, and
rising with span count (see the next section). C's own before/after pair, by
contrast, *does* isolate one thing (the #8643 code change alone), because both
of its measurements ran in the same session on the same host.

The `bounds` row is the acceptance-relevant half, and is identical across all
three: every attribute key emitted survived the allowlist, and the realised
maxima sit far under the declared caps (256 B per value, 32 events, 16 links).

## How overhead scales with span count

41 spans is one shape. Because `Journal::start`/`finish` each re-read and
re-parse the whole journal under the file lock, per-execution cost has an O(n²)
term in span count, which 41 spans would not reveal. `--tools-per-attempt`
varies the span count without changing anything else, so the curve is directly
measurable (release build, same host, same session, 3 repetitions per point
except the first). This table is post-#8642 (the fix that lifted the ~256-span
measurement cap) but **pre-#8643** — it measures the same code as column B
above, not column C:

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

### Isolating the #8643 fsync reduction

The ladder above measures the O(n²) re-parse term after #8642 lifted the
256-span cap, but before #8643's fsync reduction landed. A separate
same-session before/after ladder, measured on #8643's own branch before #8642
had landed there (so still capped at ~256 spans / `--tools-per-attempt 40`),
isolates #8643's effect on the flat part of the per-span cost instead:

| `--tools-per-attempt` | Spans | `added_median_ns` (post-#8643) | Per span | Journal |
| --- | --- | --- | --- | --- |
| 4 (default) | 41 | 0.65 s | 15.8 ms | 44.6 KiB |
| 12 | 81 | 1.35 s | 16.7 ms | 89.0 KiB |
| 20 | 121 | 2.09 s | 17.3 ms | 133.5 KiB |
| 28 | 161 | 3.05 s | 18.9 ms | 178.0 KiB |
| 40 | 221 | 4.37 s | 19.8 ms | 244.7 KiB |

(Pre-#8643, the same shape read 29.0 / 26.8 / 23.1 / 23.8 / 26.6 ms — noisier
and roughly 1.4–1.8× higher, consistent with the fsync-count reduction below.
`journal_bytes` is unchanged at every point, as expected: neither change alters
what the journal itself stores.)

Over this 5.4× increase in span count, per-span cost does not rise — it varies
between 15.8 and 19.8 ms with no monotone trend beyond noise, and a straight
line (`added_ms ≈ 19.7 × spans`) fits every point to within ±13%, comparable to
the spread *within* a single point on this host (host load during this session
ranged from ≈7 to ≈11 on 8 cores). This ladder is too short to resolve the
O(n²) re-parse term the extended table above finds past 221 spans; it isolates
only the flat, fsync-dominated part of the cost.

What dominates that constant is measured, not assumed. `strace` on one 41-span
run under #8643 counts **250 durability barriers**, down from 417 before
(measured on the pre-#8643 baseline above) — 82 `fdatasync` on the journal
(two per span: `Started`, `Completed`, unchanged — this is the per-boundary
write the doc's next section explains keeping), 44 `fsync` on the
trace-context directory (down from 170 — `Journal::lock` now only fsyncs it
while the journal is still empty), 83 `fsync` on cursor/context temp files
(down from 124 — `Journal::drain` now commits the cursor once per *delivered*
record instead of once per journal entry), and 41 `fsync` on the durable queue
(unchanged — `DurableQueue::push_durable` is outside this issue's scope). That
is ≈6.1 barriers per span, down from ≈10. A measured `fdatasync` on this
host's ext4/NVMe costs ≈2.7 ms (median over 200, p90 2.95 ms, measured
pre-#8643 and not re-measured here since the disk did not change), so 6.1
barriers/span predicts ≈16.5 ms/span against 15.8–19.8 ms measured. The
overhead is still, to a first approximation, fsync count × fsync latency; the
re-parse work found in the extended ladder above is small beside it at these
sizes, though this shorter ladder alone is too short to see it.

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

- The cost is now ≈0.6% of this host's p50 observed sweep and ≈0.02% of its
  p90 (column C above). Even the debug/saturated upper bound (column A,
  pre-#8643) was ≈5% of p50.
- The per-boundary journal append itself (`Journal::append`'s `sync_data`, two
  `fdatasync` per span, unchanged by #8643) is now a **larger share** of what
  remains — 82 of the 250 barriers on a 41-span run, ≈33%, up from the same 2
  of ~10 (≈20%) pre-#8643 — because the two savings below removed cost sitting
  *around* it (the lock path's directory `fsync`, the drain path's per-entry
  cursor commit) rather than touching it. Batching the append itself would
  trade away the property the design exists for — a trace that survives a
  crash mid-sweep (#8579), which is exactly the sweep whose trace is worth
  most — so it stays as-is regardless of its now larger share.

Two **durability-neutral** savings were identified above the append and have
both been implemented (#8643), verified against the code paths they touch
rather than assumed safe:

1. **`Journal::lock` no longer `fsync`s the parent directory on every call.**
   A directory `fsync` is needed only to durably link a *newly created*
   journal file — once the journal holds a record, that link is already
   stable, so `lock()` now fsyncs the parent only while the journal file is
   still empty (`loom-daemon/src/telemetry/trace/journal.rs`). This is
   durability-neutral because creation and the first `append` both happen
   while holding the same file lock: whichever call finds the journal empty is
   necessarily the one durably linking it before any record exists in it, so
   no record is ever made reachable ahead of its directory entry. A journal
   deleted by `retire_if_drained` and recreated later is empty again and gets
   a fresh directory `fsync`, verified by a dedicated test
   (`parent_directory_is_synced_to_link_a_new_journal_not_on_every_lock`).
2. **`Journal::drain` now commits the cursor once per *delivered* record, not
   once per journal entry.** Entries that deliver nothing (`Started`, `Owner`,
   `Supervisor`) are re-read and re-skipped on replay after a crash, so
   deferring their cursor commit into the next delivered record's commit (or a
   single commit for the batch's undelivered tail) costs nothing on replay —
   verified by `drain_commits_the_cursor_per_delivered_record_not_per_entry`
   and `a_failed_delivery_replays_only_the_record_it_failed_on`. This is a
   **narrower** batching than "once per whole batch": the backend's
   `TelemetryEnvelope` sink only deduplicates `sweep.completed`/
   `sweep.outcome` by `(kind, sweep_id)`; `trace.span` has no such index, so
   committing the cursor across multiple *delivered* records would let a crash
   between them re-offer — and duplicate — up to a whole batch of spans. That
   half of the idea (batching across deliveries) is **declined**: the queue's
   at-least-once contract does not extend to `trace.span`, and duplicating
   span rows is a correctness regression, not a durability-neutral one.

Together the two land the fsync count at ≈6.1 barriers per span (82
`fdatasync` + 44 directory `fsync` + 83 temp-file `fsync` + 41 queue `fsync`,
over 41 spans), down from ≈10, and `added_ns_per_span` at ≈15.8 ms on this
host, down from ≈29 ms (see the recorded measurements and ladder above). What
remains is out of this issue's scope: the once-per-span `DurableQueue`
directory `fsync` (`push_durable`) and the trace-context store's own atomic
replaces were not part of #8643's two proposed savings and were left
unchanged.

## What this cannot establish

This is an offline measurement of Loom's own instrumentation. It does not, and
must not be read to, establish backend ingestion behaviour, export latency
against a real endpoint, resolved provider/model identity for a live run, or
that backend log links resolve to the relevant spans. Those need an authorized
live run against a configured backend and are tracked as separate evidence.
