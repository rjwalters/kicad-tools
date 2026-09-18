# Native workload diagnostic observer

This is an **opt-in diagnostic**, not a scheduling fix for #5501. No workflow
loads it by default. It does not limit concurrency, alter test selection,
wrap `Popen`, change subprocess arguments, or set/extend timeouts.

On Linux, from the exact source checkout to investigate:

```sh
python scripts/ci/native_observer.py --output /tmp/native-observation-new -- \
  python -m pytest -n auto -o addopts= --benchmark-disable --timeout=60 \
  -m 'not slow' [the existing workflow selection arguments]
```

Preserve `PYTEST_XDIST_AUTO_NUM_WORKERS=4`, the 12 GiB container and existing
selection arguments and timeout markers. Retain source SHA, image digest,
container identity and original workload command separately using the CI run
receipt. The observer deliberately does not persist raw argv or environment:
commands have allowlisted categories, and parametrized test IDs are replaced
by stable digests to avoid copying secret parameter values. Diagnostic files
must still be treated as internal because test source paths/names remain.

The output directory must be new. `observer.jsonl` retains sampling interval,
cgroup v2 memory current/peak/max and OOM counters, plus the workload's exit
code. The wrapper returns the same ordinary exit code (or shell-conventional
128+signal). `processes.jsonl` records PID plus kernel start tick, PPID,
inherited pytest test/phase and worker, sampled RSS and sampled peak RSS.
Each pytest process writes a separate `pytest-PID.jsonl`: phase starts,
reports, and direct subprocess launch attempts. Launch events happen before
exec and therefore do **not** assert a child actually ran or exited cleanly.
The separate sampler survives an individual worker's death; killing the
entire container can still kill the sampler, so records are flushed as they
are written and CI must upload artifacts on failure.

No inference of ownership is made from a reused parent PID. A sampled native
child retains its inherited test/phase even when its worker has advanced or
died. This covers direct native, nested Python/CLI, and KiCad Python refill
processes that preserve the inherited observer token. Command categories are
hints, not a claim that generic Python consumes no native memory.

Limits are explicit:

- Sampling can miss short-lived descendants. Direct pytest launch attempts
  are retained even if polling misses the child; nested short-lived launches
  may be absent entirely. This is not an exhaustive exec tracing facility.
- Replacing/clearing the child environment can remove tracking. Direct launch
  records identify missing token inheritance; no environment is injected
  into those explicit replacement dictionaries.
- RSS peaks are sampled lower bounds. Do not add them and call the sum exact
  cgroup consumption; cache, shared pages and other processes differ.
- `memory.peak` may predate the observer. It is never reset. `memory.events`
  deltas separate new OOM events; missing/v1 counters are `null`, not zero.
- Process disappearance means unobserved, not an inferred exit code or kill.
  Only the direct workload's wait status is authoritative.
- The observation ends when the workload exits and lists still-observed
  processes; it does not wait indefinitely for or kill orphan descendants.

Before CI activation, independently review the observer and run its Linux
integration control. Passing fake-/proc tests on macOS is not proof of Linux
runtime behavior or of a memory fix. Full #5501 acceptance additionally needs
attributed actual workload evidence and an independently reviewed scheduling
policy, with every original test and assertion preserved.

After the workload has launched, a sampling/read/write failure disables further
sampling and emits a best-effort fixed-text warning. The observer continues
waiting for the original workload and returns its authoritative exit status;
it does not cancel the workload or add a deadline. It attempts a terminal
record with `diagnostics_degraded: true`. A terminal-record failure likewise
cannot replace the child status, and a closed/full stderr cannot cause a
second failure. In either case artifacts may be incomplete and must not be
read as proof of zero OOM events. Startup failures may abort before launch.

## Opt-in Test job wiring

The Test job enables observation only for a pull request whose body contains
`<!-- kct:native-diagnostics -->`. The body is evaluated as a boolean GitHub
expression, never interpolated into shell or written to diagnostics. Do not
add the marker or activate a run until the wiring has independent review.
Without it (including push events), `run_observed.py` directly execs the
original command; no diagnostics directory or plugin environment is added.

The five existing acceptance/bulk commands retain their argument arrays,
pipelines, selections and timeout flags. Each observed invocation uses a new
`board05`, `mask-copper`, `stitch`, `zones`, or `bulk` directory. The final
upload uses `always()` when opted in, preserving observations after a failed
step as well as successful measurements; runner/container loss can still
prevent artifact upload.

Each group has an identity receipt containing the checkout SHA, PR head SHA,
workflow file hash, run/attempt, configured image, job container ID and SHA256
of the exact JSON argv array. Raw command arguments/environment are not
retained. Recover the command from the frozen workflow and resolve its temp
paths before comparing the digest. The configured image tag is **not** an
immutable image identity: the receipt deliberately records `image_digest:
null`. Bind the authoritative Initialize-container pull-log digest to the
same run/container receipt after execution, without exposing a Docker socket
or granting host access. The PR marker selects diagnostics, not a resource,
concurrency or test-acceptance policy.

## Native concurrency gate (Issue #5501)

The observations above answered *who* owns the native workload: in run
`35099894489` the bulk pool launched **857** native processes, every one of
them attributed to an owning test and xdist worker, spread across 50+ modules.
Sampled peak RSS was **2.31 GiB median / 2.66 GiB max**, the pool reached
**4-way** native overlap, and `memory.current`/`memory.peak` reached the full
**12 GiB** with **209** `memory.events.max` events (no OOM that run). **15%**
of the launches came from a descendant process rather than from the worker
itself.

Because the workload is spread that thinly, moving modules between pools cannot
fix it. `src/kicad_tools/native_concurrency.py` instead bounds *how many native
processes may exist at once*, via a cross-process semaphore of `flock`-ed slot
files. It is inert unless `KCT_NATIVE_MAX_CONCURRENCY` names a positive
integer, so local runs, releases and every other CI job are unaffected.

| Variable | Meaning |
|---|---|
| `KCT_NATIVE_MAX_CONCURRENCY` | Positive integer bound. Unset/0/garbage ⇒ the gate never installs. |
| `KCT_NATIVE_SLOT_DIR` | Slot-file directory (default `<tempdir>/kct-native-slots`). |
| `KCT_NATIVE_SLOT_HELD` | Set in a permitted child; its own native descendants reuse the subtree's permit. |
| `KCT_NATIVE_SLOT_WAIT_SECONDS` | Fail-open ceiling on one wait (default 120 s). |
| `KCT_NATIVE_SLOT_LOG` | Permit-record directory; defaults to `KCT_NATIVE_OBSERVER_OUTPUT`. |

`flock` is released by the kernel when the holding fd closes *or* the holding
process dies, so a killed worker cannot strand a permit. Waiting is ordinary
wall-clock time inside the calling test: no pytest-timeout marker, `--timeout`
value or `subprocess` timeout is changed, extended or re-armed. A wait past the
ceiling launches unbounded and records the event rather than hanging; the audit
step treats any such fallback as a failure.

### Auditing and choosing the bound

`scripts/ci/analyze_native_observations.py` summarises the retained
observations and, with `--max-concurrency N`, **fails** on a cgroup
`oom`/`oom_kill` event, on a gate fallback to an unbounded launch, or on any
group whose observed native concurrency exceeded `N`. Run against the retained
pre-fix artifact it fails as expected:

```
$ analyze_native_observations.py <pre-fix artifact> --max-concurrency 1
FAIL bulk: observed native concurrency 4 exceeds the configured bound 1
```

`--simulate-bound N` replays that same run's launches through an N-slot queue,
keeping each worker's measured native durations and its measured think time
between them, and reports the wall time and per-test wait bound `N` would have
cost. That is how the shipped bound of 1 was selected rather than guessed:

| Bound | Added bulk span | Longest single wait | Worst single test |
|---|---|---|---|
| 1 | +257 s | 4.8 s | +23.1 s |
| 2 | +12 s | 1.4 s | +2.0 s |
| 4 (observed) | 0 s | 0 s | 0 s |

1 is chosen over 2 because the `CONSTRAINT_MEMCG` kill recorded for PR #5499
had **three** concurrent natives (2.36 + 2.36 + 1.66 = 6.38 GiB): a bound of 2
still permits 5.32 GiB, roughly 1 GiB under the configuration that actually
died, whereas a bound of 1 caps native anonymous memory at 2.66 GiB. Every test
whose modelled duration then exceeds 30 s already carries an explicit
300–900 s `pytest.mark.timeout` override, and the four serial acceptance groups
already observed a maximum native concurrency of 1, so the bound is measured to
cost them nothing.

The simulation is a **schedule** model only — it predicts wait and wall time,
never memory. Sampled RSS remains a lower bound and process-RSS sums are never
presented as cgroup usage; the cgroup counters are reported separately and
never derived from RSS.
