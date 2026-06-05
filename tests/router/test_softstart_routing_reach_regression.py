"""Softstart routing reach regression guard (Issue #3199).

This test re-routes the softstart board with the per-pad channel
budget DISABLED (the "unaided" baseline) and asserts the resulting
fully-connected signal-net count is >= 6/10.  This is the load-
bearing AC for #3199.

History:
- PR #3142 baseline: softstart reached **6/10 signal nets** unaided
  (post-#3138 combined intervention).
- PR #3192 (Issue #3144) introduced a FIFO-on-seq A* tie-break that
  inadvertently regressed softstart's unaided reach to **5/10**,
  identified by bisect in #3199.
- This issue's fix (#3199): the A* tie-break now uses
  ``(f_score asc, -g_score asc, seq asc)`` -- a standard
  "greedy on ties" tertiary key restores the 6/10 baseline AND
  preserves the run-to-run determinism property #3144 required.

The test is gated behind ``KICAD_RUN_SLOW_SOFTSTART_REACH=1`` because
a fresh route of softstart takes ~75 seconds (faster than board 06,
but still meaningful at scale).  CI invokes this test in the dedicated
slow-board job; ``pnpm check:ci`` does NOT run it.

To run locally::

    KICAD_RUN_SLOW_SOFTSTART_REACH=1 uv run pytest \\
      tests/router/test_softstart_routing_reach_regression.py -v --no-cov

The fast equivalent for the bisect-resistant property (A* tie-break
key shape) lives in ``test_astar_tiebreak_determinism.py`` and runs
unconditionally.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "external" / "softstart"

# Floor for the unaided routing reach (Issue #3199 acceptance criterion).
# This is the *post-#3142, pre-#3192* baseline that #3192 inadvertently
# regressed.  Tighten this floor in a follow-up PR once routing reach
# improves beyond 6/10 with the budget active (see #3201).
SOFTSTART_UNAIDED_REACH_FLOOR = 6


def _slow_tests_enabled() -> bool:
    return os.environ.get("KICAD_RUN_SLOW_SOFTSTART_REACH") == "1"


pytestmark = pytest.mark.skipif(
    not _slow_tests_enabled(),
    reason=(
        "Slow softstart routing-reach test (~75s).  Set KICAD_RUN_SLOW_SOFTSTART_REACH=1 to enable."
    ),
)


def _route_softstart_unaided() -> int:
    """Route softstart with the per-pad channel budget disabled.

    Reuses the routing recipe from boards/external/softstart/
    generate_design.py (so the test stays in sync with the
    production board's design rules and skip-net list) but
    monkey-patches ``Autorouter._build_pad_channel_budgets`` to
    return ``[]`` so the per-pad budget cost-shaping term is a
    no-op.  This is the "unaided" baseline referenced by #3199.

    Returns the number of fully-connected signal nets.
    """
    os.environ.setdefault("PYTHONHASHSEED", "0")

    # Patch BEFORE importing the board recipe so the recipe's
    # ``router.route_with_escape()`` call sees the no-op budget.
    from kicad_tools.router.core import Autorouter

    Autorouter._build_pad_channel_budgets = lambda self, *a, **kw: []  # type: ignore[assignment]

    sys.path.insert(0, str(BOARD_DIR))
    try:
        import generate_design  # type: ignore[import-not-found]
    finally:
        sys.path.pop(0)

    output_dir = BOARD_DIR / "output"
    output_dir.mkdir(parents=True, exist_ok=True)
    generate_design.create_project(output_dir, "softstart")
    generate_design.create_softstart_schematic(output_dir)
    pcb_path = generate_design.create_softstart_pcb(output_dir)

    from kicad_tools.router import DesignRules, load_pcb_for_routing

    rules = DesignRules(
        grid_resolution=0.075,
        trace_width=0.3,
        trace_clearance=0.15,
        via_drill=0.3,
        via_diameter=0.6,
    )
    skip_nets = [
        "AC_LINE",
        "AC_NEUTRAL",
        "FUSED_LINE",
        "GND",
        "+3.3V",
        "VRECT",
        "SCAP_POS+",
        "SCAP_POS_GND",
        "SCAP_NEG+",
        "SCAP_NEG_GND",
        "ISENSE_POS",
    ]
    router, _net_map = load_pcb_for_routing(
        str(pcb_path),
        skip_nets=skip_nets,
        rules=rules,
    )
    router.route_with_escape(
        use_negotiated=True,
        per_net_timeout=45.0,
        timeout=420.0,
    )

    stats = router.get_statistics()
    return int(stats["nets_routed"])


class TestSoftstartUnaidedReach:
    """Softstart unaided routing-reach floor (Issue #3199).

    The "unaided" baseline = router with the per-pad channel budget
    (Issue #3143 infrastructure, introduced in PR #3198) explicitly
    disabled.  Tracking this number separately from the
    budget-active reach lets us catch regressions in the A* main
    routing pipeline that would otherwise be masked by the budget's
    cost-shaping term.
    """

    def test_unaided_reach_meets_floor(self) -> None:
        """``softstart`` must route >= 6/10 signal nets unaided.

        Acceptance criterion for Issue #3199.  Regressions below
        this floor indicate the A* main routing pipeline has lost
        the ability to find routes the post-#3142 baseline could.
        The fast-equivalent bisect anchor is the A* tie-break key
        shape asserted in ``test_astar_tiebreak_determinism.py``,
        but routing reach is the only end-to-end signal that
        catches algorithmic regressions in the search itself (vs
        the comparator).
        """
        reach = _route_softstart_unaided()
        print(f"  softstart unaided routing reach: {reach}/10")
        assert reach >= SOFTSTART_UNAIDED_REACH_FLOOR, (
            f"softstart unaided routing reach regressed to {reach}/10 "
            f"(floor is {SOFTSTART_UNAIDED_REACH_FLOOR}/10 per Issue #3199).  "
            "The most likely cause is a change to the A* search or "
            "comparator (src/kicad_tools/router/pathfinder.py "
            "``AStarNode``, src/kicad_tools/router/cpp/include/types.hpp "
            "``AStarNode::operator>``, or the escape pre-pass in "
            "src/kicad_tools/router/escape.py).  See #3199 for the "
            "bisect protocol."
        )
