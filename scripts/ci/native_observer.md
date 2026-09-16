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
