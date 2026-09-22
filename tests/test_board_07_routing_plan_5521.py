"""Board-07 recall measurement for the routing plan (Issue #5521).

Epic #5510 Phase 1c's honesty test.  Board 07's regression fixture is the
fleet's one board with *seed-invariant* opens — ``{DQ3, DQ4, TMDS_D0_N,
TMDS_D1_N}`` (``tests/test_board_07_matchgroup_test.py``) — so it is the
only place where "does the plan's predicted congestion have anything to do
with the nets that actually failed?" can be asked against a stable answer.

Two measurements, both **recorded rather than thresholded** (the issue asks
for the numbers; Phase 1 sets no bar on them):

1. **The DDR byte in isolation is feasible.**  Alone on an empty 4-layer
   board the 11-net byte routes 11/11 (#4089), and the plan agrees — it
   reports no overflow.  This is the control: it says the plan does not
   simply call every DDR bundle congested.
2. **The assembled fixture's recall.**  How many of the four real opens
   cross an overflowed corridor, using the same
   ``RoutingPlan.crossings_by_net_name()`` predicate ``net-status --why``
   and the fleet table use.

Both build the plan **in process** (``Autorouter.plan_routing()``, a coarse
global pass costing well under a second) rather than routing, so neither
touches ``regression-fixture/matchgroup_test_routed.kicad_pcb`` and neither
needs the recipe's 2400 s budget.  ``real_design/`` is never read.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARD_DIR = REPO_ROOT / "boards" / "07-matchgroup-test"
FIXTURE_DIR = BOARD_DIR / "regression-fixture"
FIXTURE_PCB = FIXTURE_DIR / "matchgroup_test.kicad_pcb"
NET_CLASS_MAP = FIXTURE_DIR / "net_class_map.json"

#: The recipe's pour nets (``generate_design.py`` ``POUR_NETS``): excluded
#: from the trace router, so excluded from the plan.
POUR_NETS = ["GND", "+1V2", "+1V8"]

#: The four seed-invariant opens on the committed routed fixture
#: (``tests/test_board_07_matchgroup_test.py::EXPECTED_OPEN_NETS``).  The
#: recall denominator is 4, not the 5 quoted in older issue prose — the
#: 2026-09-09 MIPI repair closed ``MIPI_DAT0_N``.
EXPECTED_OPEN_NETS = ("DQ3", "DQ4", "TMDS_D0_N", "TMDS_D1_N")

#: MEASURED on this fixture, 2026-09-21.  The Phase-1 capacity model reports
#: the assembled fixture FEASIBLE: the pitch-heuristic tile capacity is not
#: exceeded anywhere, so no corridor is named and recall is 0/4.  This is a
#: real negative result, not a skipped assertion — board 07's opens come from
#: bundle reversal (#3438/#5511), which a scalar per-edge capacity count
#: cannot see.  A later phase that teaches the plan about crossing order
#: should flip this number; when it does, update it here deliberately.
MEASURED_OVERFLOWED_EDGES = 0

#: MEASURED, same run: which of :data:`EXPECTED_OPEN_NETS` cross an
#: overflowed corridor.  Empty, necessarily, while the count above is 0 --
#: recorded as its own constant so the two can move independently when a
#: later phase makes the plan see this board's congestion.
MEASURED_CROSSING_OPENS: tuple[str, ...] = ()


def _plan_from_fixture():
    from kicad_tools.router.io import load_pcb_for_routing
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.rules import net_class_map_from_path

    router, _ = load_pcb_for_routing(
        str(FIXTURE_PCB),
        skip_nets=list(POUR_NETS),
        layer_stack=LayerStack.four_layer_sig_gnd_pwr_sig(),
        validate_drc=False,
    )
    router.net_class_map = net_class_map_from_path(NET_CLASS_MAP, pcb_path=FIXTURE_PCB)
    router.emit_routing_plan = True
    plan = router.plan_routing()
    assert plan is not None, "plan_routing() returned nothing on the board-07 fixture"
    return plan


@pytest.fixture(scope="module")
def fixture_plan():
    if not FIXTURE_PCB.is_file():
        pytest.skip(f"missing regression fixture: {FIXTURE_PCB}")
    return _plan_from_fixture()


@pytest.mark.slow
class TestDdrBundleIsolation:
    """The control: the byte alone on an empty board is not congested."""

    def test_plan_reports_the_isolated_byte_feasible(self):
        repro = BOARD_DIR / "ddr_bundle_isolation_repro.py"
        if not repro.is_file():  # pragma: no cover - fixture guard
            pytest.skip(f"missing isolation repro: {repro}")
        sys.path.insert(0, str(BOARD_DIR))
        try:
            from ddr_bundle_isolation_repro import build_isolated_router
        finally:
            sys.path.remove(str(BOARD_DIR))

        router, _nets = build_isolated_router(enable_certificate=False)
        router.emit_routing_plan = True
        plan = router.plan_routing()

        assert plan is not None
        assert plan.overflow_report is not None
        assert plan.overflow_report.feasible is True, (
            "the 11-net DDR byte alone on an empty 4-layer board routes 11/11 "
            "(#4089); a plan that calls it infeasible is over-reporting.\n"
            + plan.format_overflow_report()
        )
        assert plan.overflow_report.total_overflow == 0


@pytest.mark.slow
class TestAssembledFixtureRecall:
    """Recall of the four seed-invariant opens on the assembled fixture."""

    def test_overflowed_edge_count_is_recorded(self, fixture_plan):
        report = fixture_plan.overflow_report
        assert report is not None
        assert report.overflowed_edges == MEASURED_OVERFLOWED_EDGES, (
            "board 07's overflowed-edge count moved.  Phase 1 sets no "
            "threshold on it, so this is a RECORD, not a gate: confirm the "
            "change is intended and update MEASURED_OVERFLOWED_EDGES.\n"
            + fixture_plan.format_overflow_report()
        )

    def test_recall_of_the_seed_invariant_opens_is_recorded(self, fixture_plan):
        crossings = fixture_plan.crossings_by_net_name()
        crossing = sorted(net for net in EXPECTED_OPEN_NETS if crossings.get(net))
        assert crossing == list(MEASURED_CROSSING_OPENS), (
            f"board 07's recall moved: {len(crossing)}/{len(EXPECTED_OPEN_NETS)} "
            f"seed-invariant opens now cross an overflowed corridor "
            f"({crossing}), where {list(MEASURED_CROSSING_OPENS)} was recorded.  "
            "Phase 1 sets no threshold on this -- it is a RECORD.  If the "
            "capacity model genuinely improved, re-measure and update "
            "MEASURED_CROSSING_OPENS and MEASURED_OVERFLOWED_EDGES together.\n"
            + fixture_plan.format_overflow_report()
        )

    def test_the_four_opens_were_all_planned(self, fixture_plan):
        """Recall is only meaningful if the plan actually planned these nets.

        A net the global pass never saw (a pour net, a single-pad net)
        cannot be scored -- the fleet table excludes those from its
        denominator for the same reason.  All four of board 07's opens are
        ordinary signal nets, so all four must appear.
        """
        planned = {entry.name: entry.status for entry in fixture_plan.nets.values()}
        missing = [net for net in EXPECTED_OPEN_NETS if net not in planned]
        assert not missing, f"the plan never planned {missing}; recall would be vacuous"
        assert all(planned[net] != "pour_skipped" for net in EXPECTED_OPEN_NETS)

    def test_net_class_map_sidecar_is_real(self):
        """The fixture's net-class map must actually declare the DDR byte.

        Without it the plan would weigh every net at pitch 1.0 and the
        measurement above would be of a different board than the recipe
        routes.
        """
        data = json.loads(NET_CLASS_MAP.read_text())
        assert data, "empty net_class_map.json makes the measurement meaningless"
