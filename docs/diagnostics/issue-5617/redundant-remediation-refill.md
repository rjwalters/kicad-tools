# Issue #5617 — the zone-refill remediation pass's redundant first `kicad-cli` launch

A retained, reproducible measurement of one further reducible cost inside the
five "zone fill" phases (`9b`, `10c`x2, `12b`, `13b`) that PR #5669's own
follow-up analysis flagged as "the second-largest cost now that phase 10 is
1.4%" (98.6s / 16.2% of the 609.8s step, per CI job `106665540456` on
`37a30b8c`). This is a further increment of Issue #5617 (`Part of #5240`),
building on the finer-grained profile PR #5637 added and the phase-4 fallback
profile `phase4-python-fallback-profile.md` (PR for issue #5617, merged as
#5669) added.

## Provenance

| | |
|---|---|
| Host | macOS 27.0 (Apple Silicon), 18 logical CPUs |
| Host load | Shared with several concurrent Loom sweeps throughout (`uptime` load average 20-37 against 18 cores) — see "Why this document does not claim a wall-clock percentage" below |
| Python | CPython 3.12.11 (`uv` project venv) |
| `kicad-cli` | 10.0.1 (`/Applications/KiCad/KiCad.app/Contents/MacOS/kicad-cli`) |
| Native extension | `router_cpp.cpython-312-darwin.so` present (`kct build-native --check`: router 1.0.0 / placement 2.1.0 / DRC 1.0.0) — not exercised by this change, checked per `CLAUDE.md` |
| Board fixture | `boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb` (the same routed artifact the CI job's phases 9b-13b operate on) |
| Source revision | First trace session: `729d44fe6`. Second (independent re-verification) session: `f20c8218e`. Third (pre-PR confirmation) session: `04acf85d9`, load average 78-83. |

## What `run_fill_zones()` actually launches

Every one of the five "zone fill" phases in
`boards/06-diffpair-test/generate_design.py` (`9b`, `10c` x2 for the pour-repair
rounds, `12b`, `13b`) is one call to
`kicad_tools.cli.runner.run_fill_zones()`. Tracing every `subprocess.run` call
it makes, on a clean copy of the routed fixture, on `main` (`729d44fe6`,
pre-this-change):

```
[  3.05s] kicad-cli pcb fill-zones --help              <- _kicad_cli_has_fill_zones probe (lru_cache'd across the 5 calls)
[  3.21s] kicad-cli pcb drc --help                      <- _kicad_drc_supports_refill probe (lru_cache'd across the 5 calls)
[ 13.78s] kicad-cli pcb drc --refill-zones --save-board <pcb>   <- the fill itself (_run_fill_zones_via_drc)
[ 22.34s] kicad-cli pcb drc --refill-zones --save-board <pcb>   <- _remediate_starved_thermal pass 1's OWN refill
[ 23.76s] kicad-cli pcb drc <pcb>                        <- _remediate_starved_thermal pass 1's read-only check
```

**Every kicad-cli release since KiCad 8 (`_kicad_cli_has_fill_zones` is
unconditionally `False` -- see its docstring) takes the DRC-fallback fill
path** (`_run_fill_zones_via_drc`), which already runs
`kicad-cli pcb drc --refill-zones --save-board` to fill the zones. Nothing
mutates the board between that call and `_remediate_starved_thermal`'s first
pass. That first pass's own step (1) -- `kicad-cli pcb drc --refill-zones
--save-board`, again -- is therefore **the same command run twice in a row
against unchanged input**: a fully redundant `kicad-cli` launch, one of the
three real (non-probe) `kicad-cli` round trips a clean first pass makes.

`tests/test_pour_fill_determinism_5578.py`'s own investigation already
established the load-bearing fact this optimisation depends on: *"The fill
engine is deterministic given identical input; it was not being given
identical input"* -- i.e. re-running the identical `kicad-cli pcb drc
--refill-zones --save-board <pcb>` command against an unchanged board is not
a source of nondeterminism; it reproduces the same output.

## The fix

`run_fill_zones()` now passes `skip_first_refill=True` into
`_remediate_starved_thermal()` exactly when it took the DRC-fallback fill path
(`_kicad_cli_has_fill_zones(kicad_cli)` is `False` -- true for every kicad-cli
release that exists today) **and** that fallback call used
`--refill-zones --save-board` (`_kicad_drc_supports_refill(kicad_cli)` is
`True`). Both conditions hold on the CI toolchain (kicad-cli 10.0.5) and this
dev host's (10.0.1). `_remediate_starved_thermal`'s pass-0 loop iteration then
skips straight to the `settle()` callback + the read-only DRC check, instead of
re-running the refill it already has. Every later pass (after
`force_solid_on_pads_by_uuid` / `save_pcb` actually mutate the board) still
refills unconditionally, since a real refill is required there. The native
`fill-zones` branch (currently unreachable, kept for a hypothetical future
kicad-cli) and any kicad-cli whose DRC lacks `--refill-zones` are untouched --
`skip_first_refill` defaults `False` and neither condition is asserted for
them.

Traced again, patched, same fixture:

```
[  2.92s] kicad-cli pcb fill-zones --help
[  2.90s] kicad-cli pcb drc --help
[ 14.41s] kicad-cli pcb drc --refill-zones --save-board <pcb>   <- the fill
[ 13.53s] kicad-cli pcb drc <pcb>                                <- remediation's read-only check
```

One fewer real `kicad-cli` launch per `run_fill_zones()` call: 3 -> 2 (the two
`--help` probes are `lru_cache`'d per process regardless, so they cost this
once across all five CI phases, not per phase).

## Independent re-verification (`f20c8218e`, one process, three fills)

The trace pair above was taken across two separate processes (pre-change via
`worktree.sh stash-push`, post-change after restoring). To remove "different
process / different `lru_cache` state / different code revision" as a variable,
the whole comparison was re-run **inside one process on the final patched
tree** (`f20c8218e` + this change), with pre-change behaviour reproduced by
monkeypatching `_remediate_starved_thermal` so the caller's
`skip_first_refill` argument is forced to `False` — i.e. exactly the code path
`main` takes today. Each of the three fills starts from a fresh `shutil.copy2`
of the same routed fixture. Script: "Reproducing this" below.

| run | behaviour | real `kicad-cli` launches | `--help` probes | kicad-cli wall | total wall | SHA-256 of the filled board |
|---|---|---:|---:|---:|---:|---|
| A | pre-change (`skip_first_refill=False`) | **3** | 2 (first call in the process) | 32.8 s | 48.1 s | `54af132f…6065` |
| B | post-change | **2** | 0 (`lru_cache` hit) | 24.6 s | 33.9 s | `54af132f…6065` |
| C | post-change, independent repeat | **2** | 0 (`lru_cache` hit) | 18.8 s | 26.3 s | `54af132f…6065` |

The launch the change removes is directly visible in run A's trace and absent
from B/C:

```
A: drc --refill-zones --save-board   9.61s   <- the fill (_run_fill_zones_via_drc)
A: drc --refill-zones --save-board  11.79s   <- remediation pass 0's OWN refill  <<< removed
A: drc                              11.44s   <- remediation pass 0's read-only check
B: drc --refill-zones --save-board  14.67s
B: drc                               9.91s
```

All three digests match each other **and** the four-run digest recorded in the
next section from the earlier session — `54af132f…6065` is stable across code
revision, process, and run order.

Because A paid the two one-time `--help` probes (4.7 s, `lru_cache`'d for the
rest of the process — in CI they are paid once across all five zone-fill
phases, not per phase), the honest per-phase delta is the removed launch
itself: **11.8 s out of A's 32.8 s of kicad-cli time**, i.e. one of three
round trips, consistent with the structural call-count argument below rather
than with the noisier total-wall column.

## Routing-output equivalence and determinism

Thirteen independent `run_fill_zones()` invocations across the three sessions,
each against a byte-identical fresh copy of the same routed fixture, SHA-256'd:

| session | run | code | real launches | SHA-256 |
|---|---|---|---:|---|
| first | A | pre-change (`729d44fe6`, via `worktree.sh stash-push`) | 3 | `54af132f...` |
| first | B | post-change | 2 | `54af132f...` |
| first | C | post-change (independent second run) | 2 | `54af132f...` |
| first | D | post-change (independent third run) | 2 | `54af132f...` |
| re-verification | A | pre-change behaviour forced on the patched tree (`f20c8218e`) | 3 | `54af132f...6065` |
| re-verification | B | post-change | 2 | `54af132f...6065` |
| re-verification | C | post-change (independent second run) | 2 | `54af132f...6065` |
| pre-PR #1 | A | pre-change behaviour forced on the patched tree (`04acf85d9`) | 3 | `54af132f...6065` |
| pre-PR #1 | B | post-change | 2 | `54af132f...6065` |
| pre-PR #1 | C | post-change (independent second run) | 2 | `54af132f...6065` |
| pre-PR #2 | A | pre-change behaviour forced on the patched tree (`04acf85d9`) | 3 | `54af132f...6065` |
| pre-PR #2 | B | post-change | 2 | `54af132f...6065` |
| pre-PR #2 | C | post-change (independent second run) | 2 | `54af132f...6065` |

All thirteen are **byte-identical** -- `shasum -a 256` on every output produces
the same digest, across three sessions, three source revisions and both code
paths. This directly confirms Acceptance item 2 (the change preserves the
exact routing/fill output) and that determinism holds run-to-run with the
change applied (issue #3144 / `PYTHONHASHSEED=42` / `--seed 42` semantics are
unaffected -- this change touches only which `kicad-cli` commands get
launched, never any RNG-consuming code).

The two **pre-PR** sessions were taken on `04acf85d9` (this branch's merge
base) under a load average of 78-83 on 18 cores, roughly triple the contention
of the earlier sessions, using the same one-process script as the
re-verification session. Their wall-clock numbers are correspondingly useless
(`A` 156.5 s / 161.1 s versus `B` 76.2 s / 83.1 s, `A` additionally paying the
two ~8 s `--help` probes as the first call in each process). But the two
load-independent facts this document rests on both reproduced exactly: the
**3 -> 2 real launch count**, and the **single stable digest** -- now
unchanged across every run ever taken of this comparison.

### Why the outputs are identical, not merely observed to be

Two mechanisms could have made the skipped launch load-bearing; neither is:

* **The zone geometry.** The skipped command is byte-for-byte the same
  `kicad-cli pcb drc --refill-zones --save-board <pcb>` the fill immediately
  above already ran, against a file nothing has touched in between
  (`_remediate_starved_thermal` is called directly on `result.output_path`).
  `tests/test_pour_fill_determinism_5578.py`'s investigation established the
  fill engine is deterministic given identical input, so the second run can
  only reproduce the first's geometry.
* **The net table.** `_run_drc()` snapshots and restores net declarations
  around every launch, because `--save-board` can strip them on
  kicad-tools-serialized boards. Skipping the launch does not skip a needed
  restore: `_run_fill_zones_via_drc()` performs exactly the same
  `_restore_net_declarations()` on `target_pcb` before returning (runner.py,
  in its `drc_report.exists()` success branch), so the board handed to
  remediation already has its nets restored.

One ordering difference does remain and is deliberate: pre-change, a
kicad-cli that produced no report on pass 0's refill returned *before* the
`settle()` carve ran; post-change that failure is instead detected by the
read-only DRC immediately after the carve, so the carve is on disk when
remediation gives up. That path is only reachable if kicad-cli succeeds for
the fill and then fails milliseconds later, and the carve it leaves behind is
the same clearance-correct geometry the success path ships (Issue #3711) --
the "applied == 0" early return below it already exits with exactly that
state.

## Why this document does not claim a wall-clock percentage

This measurement ran on a dev host shared with several other concurrent Loom
sweeps (`uptime` load average 24-37 against 18 logical CPUs throughout this
session) -- the same caveat `phase4-python-fallback-profile.md` documents for
its own host. Absolute per-call `kicad-cli` timings above swing by nearly 2x
between traces of the *same* command on this host (13.78s vs 14.41s for the
initial fill is fine; 22.34s vs 23.76s for two calls of near-identical shape
shows the noise floor). What is stable across every trace, clean or noisy, is
the **call count**: 3 real `kicad-cli` launches per `run_fill_zones()` call
before this change, 2 after -- a structural fact independent of host load,
and the basis for the projection below.

**Projection to CI** (not a substitute for Acceptance item 3's cohort
remeasurement): CI job `106665540456` (`37a30b8c`, cited in
`phase4-python-fallback-profile.md`) reports the five zone-fill phases
(`9b` + `10c`x2 + `12b` + `13b`) at 19.5s + 23.4s + 20.6s + 21.5s + 20.4s =
**105.4s (16.2% of the 609.8s step)**. If each of those five calls converges
in `_remediate_starved_thermal`'s first pass (true for this board-06 fixture,
both pre- and post-change, per the traces above -- neither run needed a
second pass to reach a clean audit), removing one of three real `kicad-cli`
launches per call is a reduction of roughly **1/3 of that 105.4s**, i.e.
**~35s, ~5.7% of the step**. This is a projection from a structural
call-count reduction, not a timed CI measurement; Acceptance item 3 (a
post-change hosted/self-hosted cohort median) is the number that confirms or
revises it.

## An unrelated flake encountered during verification

`tests/test_board07_native_refill_stability.py::test_repair_reconnects_the_native_refill_split`
and `::test_stability_pass_repairs_the_split_and_verifies_clean` timed out
(pytest-timeout's 180s `pytestmark`) twice under the same host contention
noted above. Both failures are pre-existing host-load flakiness, not a
regression from this change:

* The first test's timeout fires inside a pure Python list comprehension
  (`tests/test_board07_native_refill_stability.py:149`) -- it never calls
  `run_fill_zones` or any code this change touches at all.
* The second test's timeout (on a second, independent run) fires inside
  `shapely`'s C-level `intersection()` call
  (`src/kicad_tools/zones/fill_clearance.py:1068`, reached from the `settle`
  callback this change does *not* skip) -- CPU-bound geometry work unrelated
  to `kicad-cli` subprocess counts.

The remaining 5 of 7 tests in that file passed on both runs, including tests
that exercise `run_fill_zones` (and therefore the new `skip_first_refill`
path) directly.

## Reproducing this

```bash
export PATH="/Applications/KiCad/KiCad.app/Contents/MacOS:$PATH"   # or wherever kicad-cli lives
uv run kct build-native --check                                     # not required for this change,
                                                                      # but routine per CLAUDE.md

python3 - <<'EOF'
import subprocess as sp, time
from pathlib import Path

orig = sp.run
def traced(cmd, *a, **k):
    t0 = time.time()
    r = orig(cmd, *a, **k)
    print(f"[{time.time()-t0:6.2f}s] {' '.join(map(str, cmd))}")
    return r
sp.run = traced

from kicad_tools.cli.runner import run_fill_zones, find_kicad_cli
run_fill_zones(Path("boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb"),
                kicad_cli=find_kicad_cli())
EOF
```

### Reproducing the paired re-verification (three fills, one process)

```bash
export PATH="/Applications/KiCad/KiCad.app/Contents/MacOS:$PATH"

uv run python - <<'EOF'
import hashlib, shutil, subprocess, time
from pathlib import Path
from kicad_tools.cli import runner

fixture = Path("boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb")
work = Path("/tmp/issue5617-equiv"); work.mkdir(exist_ok=True)

_orig, calls = subprocess.run, []
def traced(cmd, *a, **k):
    t0 = time.time(); r = _orig(cmd, *a, **k)
    calls.append((time.time() - t0, " ".join(map(str, cmd)))); return r
subprocess.run = runner.subprocess.run = traced

cli = runner.find_kicad_cli()
real_remediate = runner._remediate_starved_thermal
def force_no_skip(pcb, c, *a, **k):          # reproduce pre-change behaviour
    k["skip_first_refill"] = False
    return real_remediate(pcb, c, *a, **k)

for label, pre in (("A(pre)", True), ("B(post)", False), ("C(post)", False)):
    target = work / f"board_{label}.kicad_pcb"
    shutil.copy2(fixture, target)
    runner._remediate_starved_thermal = force_no_skip if pre else real_remediate
    calls.clear(); t0 = time.time()
    runner.run_fill_zones(target, kicad_cli=cli)
    real = [c for c in calls if "--help" not in c[1]]
    print(f"{label:9s} launches={len(real)} wall={time.time()-t0:5.1f}s "
          f"sha256={hashlib.sha256(target.read_bytes()).hexdigest()}")
    for dt, text in calls:
        print(f"    [{dt:6.2f}s] {text}")
EOF
```
