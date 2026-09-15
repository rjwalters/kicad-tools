"""Live end-to-end control for issue #5333's corridor-starvation fix.

``test_pair_construction.py`` encodes the ``b4c764ef`` fix
(``_lattice_deadline`` reserving a fair share of the per-pair construction
window for the corridor-guided stage) against SYNTHETIC numbers lifted from
one captured production log -- it proves the arithmetic is correct, not that
the fix actually engages the corridor stage when driven by a real, live
``kct route --differential-pairs`` invocation against real board geometry.
That gap was explicitly named as outstanding in the #5333 progress comment
that shipped ``b4c764ef``: "confirm this fix actually resolves TMDS_D1
end-to-end in a live coupled search rather than just satisfying the
unit-level replay."

This module closes that gap with a real (not mocked, not replayed)
``kct route`` subprocess invocation against the committed Board07 regression
fixture (``boards/07-matchgroup-test/regression-fixture/matchgroup_test.kicad_pcb``),
using the SAME seed/timeout/search-timeout/net-class-map/length-match-groups
parameters the board's own recipe (``generate_design.py``) uses for its
negotiated route step, plus ``--differential-pairs`` (the one flag the issue
adds on top of the recipe per its curator boundaries).  The 17 non-diff-pair
singles (DQ0-7, DM0, A0-7) and the 3 pour nets are passed via ``--skip-nets``
purely to keep the invocation fast (diff pairs are routed FIRST by
``route_all_with_diffpairs``, before any single-ended net, so this changes
nothing about how the seven pairs are searched) -- this is a measurement
convenience, not a weakened check: every diff-pair net remains present, live,
and subject to the full authored net-class/coupling/skew gates.

Measured wall-clock on the reference host: ~75s (dominated by TMDS_D1's and
TMDS_D2's joint-A* fallback search after their corridor-guided construction
attempt is rejected).  Marked ``slow`` accordingly -- excluded from the
default ``-m "not slow"`` CI run, picked up by the nightly slow-tests
workflow (1200s per-test budget).

Per-pair outcome counts (bodies/iterations) vary a few percent run-to-run
because ``_lattice_deadline``'s reserve is wall-clock-based (machine-speed
sensitive); this module intentionally asserts only the STABLE, semantic
facts a regression should not be allowed to silently change:

- which pairs classify ``coupled-ok`` vs. remain open (never asserting exact
  iteration/body counts, which are not reproducible across hosts)
- that TMDS_D1's corridor-guided construction stage is actually INVOKED
  (``corridor_attempts`` > 0) -- the literal, live confirmation of the
  ``b4c764ef`` fix; a regression back to the pre-fix starvation bug would
  make this fail with ``corridor_attempts=0``
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.router.cpp_backend import is_cpp_available

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        not is_cpp_available(),
        reason="Live Board07 coupled routing requires the router_cpp backend (kct build-native)",
    ),
]

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURE_DIR = REPO_ROOT / "boards" / "07-matchgroup-test" / "regression-fixture"
FIXTURE_PCB = FIXTURE_DIR / "matchgroup_test.kicad_pcb"
FIXTURE_NET_CLASS_MAP = FIXTURE_DIR / "net_class_map.json"

# Issue #5333: the recipe's 26 non-diff-pair nets (9 DDR singles, 8 ADDR bus
# singles, 3 pour nets) -- skipped so the invocation only pays for the
# diff-pair pre-pass (which runs first regardless) plus a near-empty
# negotiated remainder, not a full 31-net Board07 route.
_NON_DIFFPAIR_NETS = [
    "GND",
    "+1V2",
    "+1V8",
    "DQ0",
    "DQ1",
    "DQ2",
    "DQ3",
    "DQ4",
    "DQ5",
    "DQ6",
    "DQ7",
    "DM0",
    "A0",
    "A1",
    "A2",
    "A3",
    "A4",
    "A5",
    "A6",
    "A7",
]

# The pairs the pre-b4c764ef #5333 investigation and the ninth-commit
# progress comment established as qualified (coupled-ok) independent of the
# corridor-starvation fix -- MIPI_CLK/MIPI_DAT0/TMDS_D0 resolve through the
# geometric lattice or an already-working corridor path, and DQS resolves
# through native partial-recovery.  A regression in any of these is a
# DIFFERENT defect than the one this module targets.
_EXPECTED_QUALIFIED_PAIRS = ("DQS", "MIPI_CLK", "MIPI_DAT0", "MIPI_DAT1", "TMDS_D0")

# TMDS_D1/TMDS_D2 are the two pairs #5333 has not yet resolved
# MIPI_DAT1 now qualifies through exact departure geometry after a native
# conservative-halo rejection. See ``_corridor_guided_departures`` for
# the current, still-accurate diagnosis of each).  This module does not
# require them to stay unresolved forever -- a future fix legitimately
# qualifying one is a welcome change to this list -- it only pins that they
# are not SILENTLY reported qualified by a quality-gate bypass.
_EXPECTED_STILL_OPEN_PAIRS = ("TMDS_D1", "TMDS_D2")


@pytest.fixture(scope="module")
def board07_live_diffpair_log(tmp_path_factory: pytest.TempPathFactory) -> str:
    """Run a real, live ``kct route --differential-pairs`` against Board07.

    One subprocess invocation, shared read-only by every test in this
    module (module-scoped) -- each test asserts a different fact about the
    same captured stdout rather than re-paying the ~75s routing cost.
    """
    scratch = tmp_path_factory.mktemp("board07_live_diffpair")
    input_pcb = scratch / "matchgroup_test.kicad_pcb"
    input_pcb.write_bytes(FIXTURE_PCB.read_bytes())
    net_class_map = scratch / "net_class_map.json"
    net_class_map.write_bytes(FIXTURE_NET_CLASS_MAP.read_bytes())
    output_pcb = scratch / "routed.kicad_pcb"

    env = os.environ.copy()
    env["PYTHONHASHSEED"] = "42"
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(input_pcb),
            "--output",
            str(output_pcb),
            "--manufacturer",
            "jlcpcb",
            "--strategy",
            "negotiated",
            "--no-auto-layers",
            "--layers",
            "4",
            "--seed",
            "42",
            # Issue #5333 repro parameters: mirror generate_design.py's
            # ROUTE_SEARCH_TIMEOUT_S / --deterministic-budget exactly (see
            # boards/07-matchgroup-test/generate_design.py); the derived
            # HARD TOTAL --timeout there also folds in placement-delta
            # probes this invocation does not run, so 600s (the same value
            # as --search-timeout) is sufficient here.
            "--timeout",
            "600",
            "--search-timeout",
            "600",
            "--deterministic-budget",
            "--skip-nets",
            ",".join(_NON_DIFFPAIR_NETS),
            "--net-class-map",
            str(net_class_map),
            "--length-match-groups",
            "--differential-pairs",
        ],
        capture_output=True,
        text=True,
        cwd=str(REPO_ROOT),
        env=env,
        timeout=600,
    )
    log = result.stdout + result.stderr
    # Partial (exit 3) is EXPECTED: three of the seven pairs remain
    # unresolved by design (see _EXPECTED_STILL_OPEN_PAIRS) and fall back to
    # the negotiated main strategy. A crash (anything other than 0 or the
    # documented partial-routing exit) is still a hard failure.
    assert result.returncode in (0, 3), (
        f"unexpected kct route exit code {result.returncode}; full output:\n{log}"
    )
    return log


def _pair_report_line(log: str, pair: str) -> str:
    match = re.search(rf"\[coupled-pair-report\] pair={re.escape(pair)} .*", log)
    assert match is not None, f"no [coupled-pair-report] line for pair={pair} in:\n{log}"
    return match.group(0)


def _construction_line_before(log: str, report_line: str) -> str | None:
    """The ``[coupled-construction]`` diagnostic immediately preceding a pair's report line."""
    prefix = log[: log.index(report_line)]
    # Construction logs are emitted before their own pair report. Stop at
    # the prior report so a missing target diagnostic cannot borrow its work.
    prefix = prefix.rsplit("[coupled-pair-report]", 1)[-1]
    matches = re.findall(r"\[coupled-construction\] .*", prefix)
    return matches[-1] if matches else None


@pytest.mark.parametrize("pair", _EXPECTED_QUALIFIED_PAIRS)
def test_previously_qualified_pairs_remain_coupled_ok(
    board07_live_diffpair_log: str, pair: str
) -> None:
    """DQS/MIPI_CLK/MIPI_DAT0/TMDS_D0 stay qualified (#5333 item 3: re-verify unaffected)."""
    line = _pair_report_line(board07_live_diffpair_log, pair)
    assert f"pair={pair} class=coupled-ok" in line, line
    assert "coupled=True" in line, line


@pytest.mark.parametrize("pair", _EXPECTED_STILL_OPEN_PAIRS)
def test_still_open_pairs_are_not_silently_promoted(
    board07_live_diffpair_log: str, pair: str
) -> None:
    """MIPI_DAT1/TMDS_D1/TMDS_D2 must never report qualified via a quality-gate bypass.

    This intentionally does NOT pin the exact classification string (a
    legitimate future fix changing ``landing-stall`` to ``joint-A*-plateau``,
    for instance, is not a regression) -- only that ``coupled=True`` never
    appears for these three without every other assertion in this module
    (particularly the corridor-engagement one below) also being re-examined.
    """
    line = _pair_report_line(board07_live_diffpair_log, pair)
    assert "coupled=False" in line, (
        f"pair={pair} unexpectedly reports coupled=True -- if this is a genuine fix, "
        f"update _EXPECTED_QUALIFIED_PAIRS / _EXPECTED_STILL_OPEN_PAIRS accordingly: {line}"
    )


def test_tmds_d1_corridor_stage_is_invoked_in_a_live_search(board07_live_diffpair_log: str) -> None:
    """Live confirmation of the b4c764ef corridor-starvation fix (#5333).

    Before b4c764ef, TMDS_D1's ``[coupled-construction]`` diagnostic reported
    ``corridor_attempts=0`` in production (the lattice stage burned the
    entire per-pair wall-clock window on tail widening before the
    corridor-guided stage ever ran) -- see pair_construction.py's
    ``CORRIDOR_WALL_RESERVE_FRACTION`` docstring for the full measurement.
    This asserts the corridor stage now actually runs at least once in a
    real, live search (not a replay), which is the literal, durable version
    of the "confirm end-to-end" verification named in the #5333 progress
    comment that shipped that fix.
    """
    report_line = _pair_report_line(board07_live_diffpair_log, "TMDS_D1")
    construction_line = _construction_line_before(board07_live_diffpair_log, report_line)
    assert construction_line is not None, (
        "no [coupled-construction] diagnostic found for TMDS_D1 -- the "
        "construction stage (added by #5333) did not run at all"
    )
    match = re.search(r"corridor_attempts=(\d+)", construction_line)
    assert match is not None, construction_line
    corridor_attempts = int(match.group(1))
    assert corridor_attempts > 0, (
        "TMDS_D1's corridor-guided construction stage was never invoked "
        f"(corridor_attempts=0) -- this is the exact pre-b4c764ef starvation "
        f"regression: {construction_line}"
    )
    iters_match = re.search(r"corridor_iters=(\d+)", construction_line)
    assert iters_match is not None, construction_line
    assert int(iters_match.group(1)) > 0, construction_line
