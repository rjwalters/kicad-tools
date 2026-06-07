"""Regression test pinning the chorus-test-revA reach floor (Issue #3237).

Issue #3237 documents a major regression between the May 10 snapshot
(30/48 fully-connected = 62%) and the post-PR-#3232 snapshot
(7-10/48 = 15-21%) on the same chorus-test-revA_v19_stripped.kicad_pcb
fixture.  Wave 1 of follow-ups (PR #3242 zone partition fix, PR #3243
clearance-safe grid auto-selector, PR #3244 monotonic-regression
early-exit for layer escalation) merged on 2026-06-06 23:34-23:51 UTC.

This module captures the **re-measurement** the Builder did against
HEAD a224b1e3 (post-Wave-1, the first commit where all three Wave 1
fixes are present) on 2026-06-06 evening:

    Backend  | Attempt 1 | Attempt 2 | Attempt 3 | Best | Auto-fix
    ---------+-----------+-----------+-----------+------+---------
    Python   | 6/48 (12%)| 6/48 (12%)| 5/48 (10%)| 6/48 | SKIPPED
    C++      | 5/48 (10%)| 3/48 (6%) | 4/48 (8%) | 5/48 | SKIPPED

Both backends still produce dramatically less reach than May 10's 62%.
The mechanism is unchanged from the issue body: each attempt detail-routes
27-30 nets but only 5-7 of those tally as "fully connected" in the final
result.  The 20-25 "partial-route" nets per attempt never get cleaned
because auto-fix is silently skipped when `--timeout` is exhausted by
the negotiated stage (issue #2802 deadline guard).

PR #3244 (#3241 early-exit) was expected to terminate the ladder when
reach monotonically degrades, freeing budget for auto-fix.  However, the
chorus pattern's per-attempt drops are 1-2 nets — below the
``REGRESSION_TOLERANCE=2`` threshold and ``HARD_DROP_NETS=5`` cutoff —
so the early-exit never fires on this dataset.  All three attempts still
run to completion, exhausting the 1500s budget.

The load-bearing follow-up is **issue #3238 (auto-fix budget
reservation)**, currently being built in parallel.  Once #3238 lands,
auto-fix will run with a guaranteed slice of the wall-clock budget and
should convert the 27-30 detail-routed nets back to fully-connected,
restoring reach to ~60% parity with May 10.

This module's tests serve two purposes:

1. **Floor assertion** (``test_chorus_reach_post_wave1_floor``): an
   opt-in slow integration test that runs the chorus recipe end-to-end
   and asserts the post-Wave-1 floor of ``nets_fully_routed >= 5``.
   Marked ``@pytest.mark.slow`` (25+ min wall-clock) so it gates manual
   verification and nightly runs only, not per-commit CI.

2. **Re-measurement record** (``test_post_wave1_measurement_documented``):
   a fast unit test that codifies the empirical numbers above so they
   show up in the test suite and future Builders can grep for the
   baseline when chorus reach changes again.  Failures here mean the
   re-measurement record drifted from the constants — bump the constants
   AND update the project memory file.

After #3238 lands and the regression is fully resolved, the floor in
``CHORUS_POST_WAVE1_FLOOR`` should be bumped to match the new reach.
Issue body's AC #1 sets the target at ≥ 30/48 (May 10 parity).
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

# --------------------------------------------------------------------------
# Post-Wave-1 baseline constants (Issue #3237 re-measurement).
# --------------------------------------------------------------------------

# Total multi-pad signal nets on chorus-test-revA_v19_stripped.kicad_pcb,
# after the five power/ground nets are excluded by the case config (see
# tests/baselines/chorus_test_revA.json).
CHORUS_NETS_TOTAL = 48  # v19 stripped fixture; +2 vs v18 used in the
# 2611 baseline because v19 adds the U7
# 74LVC1G17 connections.

# Post-Wave-1 floor: the smallest "Best result" observed across the two
# backends on 2026-06-06 evening.  CI must assert reach >= this so that
# future PRs cannot silently degrade further while #3238 is in flight.
# (Python = 6/48, C++ = 5/48 -> floor = 5.)
CHORUS_POST_WAVE1_FLOOR = 5

# Backend-specific best-result floors.  Useful when the integration
# test is parameterised by backend to confirm parity between Python and
# C++ stays within the issue body's AC #2 tolerance (C++ >= 80% of Python).
CHORUS_POST_WAVE1_FLOOR_PYTHON = 6
CHORUS_POST_WAVE1_FLOOR_CPP = 5

# May-10 reach target (issue body AC #1).  Once #3238 lands and reach
# recovers, the integration test below should be flipped from "assert
# >= floor" to "assert >= target" and the floor constant retired.
CHORUS_MAY10_TARGET = 30  # 30/48 = 62.5%

# The external chorus repo path, where the v19_stripped fixture lives.
# When the fixture is missing the slow integration test skips rather than
# fails, matching the existing pattern in ``test_benchmark_chorus.py``
# (see ``test_missing_pcb_skips_gracefully``).
CHORUS_FIXTURE_PATH = Path(
    "/Users/rwalters/GitHub/chorus/hardware/chorus-test-revA/kicad/"
    "chorus-test-revA_v19_stripped.kicad_pcb"
)


# --------------------------------------------------------------------------
# Fast unit test: re-measurement documented in constants.
# --------------------------------------------------------------------------


def test_post_wave1_measurement_documented() -> None:
    """The Builder's re-measurement constants are present and self-consistent.

    Failure of this test means either:

    1. The constants above were edited without bumping the comment block
       (re-measurement record drifted) -- update the docstring too.
    2. A future PR landed that improved reach -- bump
       ``CHORUS_POST_WAVE1_FLOOR`` to the new floor and update the
       per-backend constants.  Reference the PR in the docstring.
    """
    # Floor is at least 1 (the router must produce *something*) and
    # strictly below the May 10 target (otherwise this issue is closed).
    assert 1 <= CHORUS_POST_WAVE1_FLOOR < CHORUS_MAY10_TARGET, (
        "Post-Wave-1 floor must sit between 1 and the May 10 target "
        f"({CHORUS_MAY10_TARGET}); if it has reached the target, this "
        "test should be migrated to assert May 10 parity instead."
    )

    # Per-backend floors should bracket the global floor.
    assert CHORUS_POST_WAVE1_FLOOR_CPP <= CHORUS_POST_WAVE1_FLOOR_PYTHON, (
        "Empirically the Python backend reached >= C++ on chorus today "
        "(6 vs 5).  If this inverts, the issue body's 'this is not a "
        "C++-specific regression' assertion needs to be revisited."
    )
    assert (
        min(CHORUS_POST_WAVE1_FLOOR_PYTHON, CHORUS_POST_WAVE1_FLOOR_CPP)
        == CHORUS_POST_WAVE1_FLOOR
    ), (
        "Global floor must equal the smaller of the per-backend floors; "
        "drift here means the constants were updated inconsistently."
    )


def test_post_wave1_floor_matches_nets_total() -> None:
    """CHORUS_NETS_TOTAL is consistent with the v19 stripped fixture.

    The v19 fixture is named in the issue body's reproduction recipe and
    in the project memory file ``project_chorus_test_routing_2026_05_10b``.
    If the fixture is regenerated or the case config changes the skip-set,
    bump ``CHORUS_NETS_TOTAL`` to match the new multi-pad signal-net count.
    """
    # 48 multi-pad signal nets is the v19 number reported in the issue
    # body (cf. the v18 baseline JSON which has 46 because v19 adds U7
    # 74LVC1G17 connections).
    assert CHORUS_NETS_TOTAL == 48
    assert CHORUS_POST_WAVE1_FLOOR < CHORUS_NETS_TOTAL


# --------------------------------------------------------------------------
# Slow integration test: end-to-end reach floor.
# --------------------------------------------------------------------------


def _chorus_fixture_present() -> bool:
    """The chorus PCB lives outside the repo (boards/external/ vendor).

    See ``test_benchmark_chorus.py::test_missing_pcb_skips_gracefully``
    for the analogous skip pattern used by the existing benchmark.
    """
    return CHORUS_FIXTURE_PATH.exists()


@pytest.mark.slow
@pytest.mark.skipif(
    not _chorus_fixture_present(),
    reason=(
        "chorus-test-revA_v19_stripped.kicad_pcb fixture not present at "
        f"{CHORUS_FIXTURE_PATH}.  See docs/benchmark.md for fetch "
        "instructions; this test is intentionally a no-op on machines "
        "without the external chorus repo checked out."
    ),
)
@pytest.mark.skipif(
    os.environ.get("KCT_RUN_CHORUS_REACH_FLOOR") != "1",
    reason=(
        "Set KCT_RUN_CHORUS_REACH_FLOOR=1 to opt in to the 25+ minute "
        "chorus reach-floor integration test.  Default-off so the slow "
        "marker alone (e.g., `pytest -m slow`) does not block routine "
        "nightly runs that batch many slow tests."
    ),
)
def test_chorus_reach_post_wave1_floor(tmp_path: Path) -> None:
    """Run the chorus recipe and assert ``nets_fully_routed >= 5``.

    This is the load-bearing acceptance criterion derived from Issue #3237's
    re-measurement.  It uses the same recipe documented in the issue body's
    "How to reproduce" section, pinned to the Python backend (which scored
    6/48 on 2026-06-06, the higher of the two backends).  Wall-clock is
    25-30 minutes; the test is double-guarded by ``@pytest.mark.slow`` AND
    the ``KCT_RUN_CHORUS_REACH_FLOOR`` env var so it never runs by accident.

    The assertion intentionally uses the FLOOR (5) rather than the May 10
    target (30): until #3238 lands and unblocks auto-fix, reach is expected
    to stay at the floor.  Once #3238 merges and reach jumps to 27-30/48,
    update ``CHORUS_POST_WAVE1_FLOOR`` to the new measurement and (eventually)
    flip this test's assertion to the May 10 target.

    Note: this test does NOT validate the issue body's AC #3 ("auto-fix
    successfully runs"), because that's the explicit territory of #3238's
    sibling regression test.  We only validate that no further reach loss
    happens while #3238 is in flight.
    """
    output_pcb = tmp_path / "chorus_routed.kicad_pcb"

    # Mirror the issue body's "How to reproduce" recipe exactly, except
    # we pin to ``--backend python`` (the better-reaching backend in the
    # re-measurement).  Determinism comes from ``--seed 42`` + the
    # PYTHONHASHSEED env var (issue #3146 / PR #3193 finding).
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(CHORUS_FIXTURE_PATH),
        "--output",
        str(output_pcb),
        "--manufacturer",
        "jlcpcb-tier1",
        "--backend",
        "python",
        "--placement-feedback",
        "--placement-feedback-budget",
        "5",
        "--iterations",
        "50",
        "--auto-fix",
        "--auto-fix-passes",
        "2",
        "--auto-layers",
        "--timeout",
        "1500",
        "--seed",
        "42",
    ]
    env = {**os.environ, "PYTHONHASHSEED": "0"}

    # 30-minute timeout for the subprocess: the route itself is bounded
    # by ``--timeout 1500``s but there's startup + checkpoint overhead.
    result = subprocess.run(
        cmd,
        env=env,
        capture_output=True,
        text=True,
        timeout=1800,
    )

    # Parse the "Nets routed: N/48" line from the tail of stdout.
    last_routed = None
    for line in result.stdout.splitlines():
        stripped = line.strip()
        if stripped.startswith("Nets routed:"):
            # Form: "Nets routed:     N/48"
            rhs = stripped.split(":", 1)[1].strip()
            count_str = rhs.split("/", 1)[0].strip()
            try:
                last_routed = int(count_str)
            except ValueError:
                continue

    assert last_routed is not None, (
        "Could not parse 'Nets routed:' from chorus route stdout. "
        "Last 500 chars:\n" + result.stdout[-500:]
    )
    assert last_routed >= CHORUS_POST_WAVE1_FLOOR, (
        f"chorus reach regressed: routed {last_routed}/{CHORUS_NETS_TOTAL} "
        f"nets, below post-Wave-1 floor of {CHORUS_POST_WAVE1_FLOOR}.  "
        "See Issue #3237 for context; if this is intentional (e.g. a "
        "router refactor with known short-term cost), bump the floor "
        "constant in this module with justification.  Stdout tail:\n"
        + result.stdout[-1000:]
    )
