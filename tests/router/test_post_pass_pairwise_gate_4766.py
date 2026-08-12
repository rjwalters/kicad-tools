"""HV pairwise (creepage) gating of the copper-moving post-passes (issue #4766).

PR #4756 (issue #4699) widened the post-route pairwise audit to fresh +
preserved copper, and deliberately left one search-side gap open: the
``--lattice-optimize`` post-passes could still *move* copper without ever
consulting the pairwise predicate.  Two of the three geometric post-passes
could therefore close an HV creepage gap the search had opened, and only the
board-level audit would notice:

* ``TraceOptimizer`` (via ``optimize_routes_grid_synced``) -- its only hazard
  gate is ``CollisionChecker.path_is_clear``, and both checker implementations
  resolve clearance as the scalar ``grid.rules.trace_clearance``.
* ``drc_verify_and_nudge`` -- translates segments by up to 0.2 mm with the same
  scalar model.

The third, ``consolidate_routes_grid_synced``, provably cannot: its merged
segment is the exact union of the segments it replaces, so no gap to foreign
copper can shrink.  It is deliberately NOT gated (see the ``consolidate``
module docstring) and this file pins that too.

Every test here is dormant-by-construction without a ``--voltage-map`` table:
the "no table" variants double as the proof that the fixtures would fail on
pre-#4766 ``main``.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.drc_nudge import drc_verify_and_nudge
from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import (
    OptimizationConfig,
    TraceOptimizer,
    consolidate_routes_grid_synced,
    optimize_routes_grid_synced,
    pairwise_route_gate,
)
from kicad_tools.router.pairwise_clearance import (
    AttachZone,
    PairwiseClearanceTable,
    PairwisePathChecker,
    find_pairwise_violations,
    violation_pair_keys,
)
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules


@dataclass
class _StubRouter:
    """Minimal Autorouter stand-in -- the shape the nudge unit tests drive."""

    routes: list[Route] = field(default_factory=list)
    existing_routes: list[Route] = field(default_factory=list)
    rules: DesignRules = field(default_factory=DesignRules)
    pads: dict = field(default_factory=dict)
    nets: dict = field(default_factory=dict)
    net_names: dict = field(default_factory=dict)
    net_class_map: dict | None = None


ID_TO_NAME = {1: "HV", 2: "LV", 3: "OBST"}

# One HV pair: HV vs LV must hold 1.5 mm, everything else only the 0.2 mm DRU
# floor.  Shaped exactly like what ``--voltage-map`` installs.
TABLE = PairwiseClearanceTable(
    dru=0.2,
    net_voltages={"HV": 230.0, "LV": 0.0, "OBST": 0.0},
    required_by_pair={("HV", "LV"): 1.5},
)


def _route(net: int, name: str, points: list[tuple[float, float]], width: float = 0.25) -> Route:
    segments = [
        Segment(
            x1=a[0],
            y1=a[1],
            x2=b[0],
            y2=b[1],
            width=width,
            layer=Layer.F_CU,
            net=net,
            net_name=name,
        )
        for a, b in zip(points, points[1:], strict=False)
    ]
    return Route(net=net, net_name=name, segments=segments)


def _commit(router: Autorouter, route: Route) -> None:
    router.grid.mark_route(route)
    router.routes.append(route)


def _staircase(start_y: float, end_y: float, steps: int = 5) -> list[tuple[float, float]]:
    """Descending staircase from ``(5, start_y)`` -- what compress_staircase eats."""
    drop = (start_y - end_y) / steps
    points = [(5.0, start_y)]
    x, y = 5.0, start_y
    for _ in range(steps):
        x += 3.0
        points.append((x, y))
        y -= drop
        points.append((x, y))
    return points


def _optimize_fixture(
    *, hv_y: float = 10.0, lv_end_y: float = 11.5, hv_x2: float = 15.0
) -> tuple[Autorouter, Route]:
    """HV rail below a staircase whose compression swings LV down over it.

    ``compress_staircase`` replaces the staircase with a diagonal + horizontal
    pair, i.e. it drops the whole run to ``lv_end_y`` immediately -- copper
    lands directly above the HV rail at a gap the scalar checker is perfectly
    happy with (1.25 mm >> 0.2 mm DRU) and the pairwise requirement is not.
    """
    router = Autorouter(width=40.0, height=40.0)
    router.net_names = dict(ID_TO_NAME)
    _commit(router, _route(1, "HV", [(5.0, hv_y), (hv_x2, hv_y)]))
    lv = _route(2, "LV", _staircase(14.0, lv_end_y))
    _commit(router, lv)
    return router, lv


def _nudge_fixture(lv_y: float) -> tuple[Autorouter, Route]:
    """HV rail below an LV trace that OBST crowds from above.

    The nudge's only escape from the OBST clearance violation is downward, by
    ~0.12 mm -- inside its 0.2 mm budget, and straight into the HV creepage
    requirement it cannot see.
    """
    router = Autorouter(width=40.0, height=40.0)
    router.net_names = dict(ID_TO_NAME)
    _commit(router, _route(1, "HV", [(5.0, 10.0), (25.0, 10.0)]))
    lv = _route(2, "LV", [(8.0, lv_y), (22.0, lv_y)])
    _commit(router, lv)
    _commit(router, _route(3, "OBST", [(8.0, lv_y + 0.35), (22.0, lv_y + 0.35)]))
    return router, lv


def _pairs(router: Autorouter) -> set[tuple[str, str]]:
    return violation_pair_keys(
        find_pairwise_violations(router.routes, TABLE, id_to_name=ID_TO_NAME, dru=TABLE.dru)
    )


def _geometry(route: Route) -> list[tuple[float, float, float, float]]:
    return [(s.x1, s.y1, s.x2, s.y2) for s in route.segments]


def _optimizer() -> TraceOptimizer:
    # No collision checker: the scalar gate is not what is under test here, and
    # leaving it out makes the pairwise gate the only thing that can veto.
    return TraceOptimizer(config=OptimizationConfig(minimize_vias=False))


def _arm(router: Autorouter, *, zones: tuple[AttachZone, ...] = ()) -> None:
    router.rules.pairwise_clearance = TABLE
    router._pairwise_attach_zones_cache = zones


class TestPairwisePathCheckerFromRouter:
    """The ONE router-derived context resolver every pairwise consumer shares.

    Both post-passes here and #4507's path-level predicate resolve their table,
    id->name map and #4506 zones through ``PairwisePathChecker.from_router``.
    A second resolver would be free to drift into a different verdict on the
    same router, so this class pins the resolver's contract rather than a
    per-pass copy of it.
    """

    def test_dormant_without_a_voltage_map_table(self):
        router, _ = _optimize_fixture()
        assert PairwisePathChecker.from_router(router) is None
        assert pairwise_route_gate(router) is None

    def test_dormant_when_the_table_needs_no_widening(self):
        router, _ = _optimize_fixture()
        router.rules.pairwise_clearance = PairwiseClearanceTable(
            dru=0.2, net_voltages={"HV": 3.3}, required_by_pair={}
        )
        assert PairwisePathChecker.from_router(router) is None
        assert pairwise_route_gate(router) is None

    def test_context_carries_table_zones_and_id_map(self):
        router, _ = _optimize_fixture()
        zone = AttachZone(0.0, 0.0, 1.0, 1.0, frozenset({"HV", "LV"}))
        _arm(router, zones=(zone,))
        checker = PairwisePathChecker.from_router(router)
        assert checker is not None
        assert checker.table is TABLE
        assert checker.attach_zones == (zone,)
        assert checker.id_to_name is not None
        assert checker.id_to_name[1] == "HV"
        assert checker.id_to_name[2] == "LV"

    def test_unset_zone_cache_degrades_to_no_exemptions(self):
        """Never an unsafe accept: no cache means nothing is waived."""
        router, _ = _optimize_fixture()
        router.rules.pairwise_clearance = TABLE
        checker = PairwisePathChecker.from_router(router)
        assert checker is not None
        assert checker.attach_zones == ()

    def test_id_collision_resolves_deterministically(self):
        """Two names on one id must not depend on the mapping's iteration order."""
        rules = DesignRules(trace_clearance=0.2)
        rules.pairwise_clearance = TABLE
        forward = _StubRouter(rules=rules)
        forward._net_name_to_id = lambda: {"HV": 1, "AAA": 1}  # type: ignore[attr-defined]
        reverse = _StubRouter(rules=rules)
        reverse._net_name_to_id = lambda: {"AAA": 1, "HV": 1}  # type: ignore[attr-defined]

        a = PairwisePathChecker.from_router(forward)
        b = PairwisePathChecker.from_router(reverse)
        assert a is not None and b is not None
        assert a.id_to_name == b.id_to_name == {1: "AAA"}

    def test_a_router_without_a_grid_still_arms_on_its_own_routes(self):
        """The nudge's stub-router shape: routes but no grid (see AC6)."""
        rules = DesignRules(trace_clearance=0.2)
        rules.pairwise_clearance = TABLE
        route = _route(1, "HV", [(5.0, 10.0), (25.0, 10.0)])
        stub = _StubRouter(routes=[route], rules=rules, net_names=dict(ID_TO_NAME))

        checker = PairwisePathChecker.from_router(stub)
        assert checker is not None
        assert list(checker.foreign_routes()) == [route]


class TestOptimizePairwiseGate:
    """AC1 / AC4 / AC8 -- the trace-optimize pass."""

    def test_ungated_optimize_introduces_a_violation(self):
        """The pre-#4766 behaviour, and the reason this file exists."""
        router, lv = _optimize_fixture()
        assert _pairs(router) == set()

        optimize_routes_grid_synced(router, _optimizer())

        assert _geometry(lv) != _geometry(router.routes[1])
        assert _pairs(router) == {("HV", "LV")}

    def test_gated_optimize_keeps_the_pre_optimization_route(self):
        router, lv = _optimize_fixture()
        _arm(router)
        before = _geometry(lv)

        optimize_routes_grid_synced(router, _optimizer())

        assert router.routes[1] is lv, "vetoed route must keep its identity"
        assert _geometry(lv) == before
        assert _pairs(router) == set()

    def test_vetoed_route_never_enters_the_grid_transaction(self):
        router, lv = _optimize_fixture()
        _arm(router)

        optimize_routes_grid_synced(router, _optimizer())

        assert lv in router.grid.routes
        assert _geometry(router.grid.routes[-1]) == _geometry(lv)

    def test_optimization_that_stays_clear_is_accepted(self):
        """Same staircase, HV moved out of range -- the gate must not fire."""
        router, lv = _optimize_fixture(hv_y=4.0)
        _arm(router)
        before = _geometry(lv)

        optimize_routes_grid_synced(router, _optimizer())

        assert _geometry(router.routes[1]) != before
        assert router._pairwise_optimize_vetoes == 0

    def test_inherited_shortfall_does_not_freeze_the_route(self):
        """A pair that already violates before the pass is not this pass's doing."""
        router, lv = _optimize_fixture(lv_end_y=11.0, hv_x2=25.0)
        _arm(router)
        assert _pairs(router) == {("HV", "LV")}, "fixture must start dirty"
        before = _geometry(lv)

        optimize_routes_grid_synced(router, _optimizer())

        assert _geometry(router.routes[1]) != before
        assert router._pairwise_optimize_vetoes == 0
        # ... and the audit still reports it.
        assert _pairs(router) == {("HV", "LV")}

    def test_veto_count_is_recorded_and_surfaced(self, capsys):
        """AC8: the coarse veto's cost must be measurable, not silent."""
        router, _ = _optimize_fixture()
        _arm(router)

        optimize_routes_grid_synced(router, _optimizer())

        assert router._pairwise_optimize_vetoes == 1
        err = capsys.readouterr().err
        assert "HV pairwise gate: 1 route(s) kept pre-optimization geometry" in err
        assert "LV vs HV" in err

    def test_nothing_is_reported_when_nothing_is_vetoed(self, capsys):
        router, _ = _optimize_fixture(hv_y=4.0)
        _arm(router)

        optimize_routes_grid_synced(router, _optimizer())

        assert capsys.readouterr().err == ""


class TestAttachZoneLayerScoping:
    """AC1 -- the #4506/#4699 exemption must reach the gate, layer-scoped."""

    def _zone(self, layers: frozenset[str]) -> AttachZone:
        # Wide enough to contain the closest-gap midpoint the violation reports.
        return AttachZone(
            5.0,
            9.0,
            16.0,
            13.0,
            frozenset({"HV", "LV"}),
            frozenset({("HV", layers), ("LV", layers)}),
        )

    def test_zone_on_the_conflict_layer_waives_the_veto(self):
        router, lv = _optimize_fixture()
        _arm(router, zones=(self._zone(frozenset({"F.Cu"})),))
        before = _geometry(lv)

        optimize_routes_grid_synced(router, _optimizer())

        assert _geometry(router.routes[1]) != before
        assert router._pairwise_optimize_vetoes == 0

    def test_zone_on_another_layer_does_not_waive_the_veto(self):
        router, lv = _optimize_fixture()
        _arm(router, zones=(self._zone(frozenset({"In1.Cu"})),))
        before = _geometry(lv)

        optimize_routes_grid_synced(router, _optimizer())

        assert _geometry(router.routes[1]) == before
        assert router._pairwise_optimize_vetoes == 1


class TestNudgePairwiseRevert:
    """AC2 / AC8 -- the DRC nudge pass."""

    def test_ungated_nudge_introduces_a_violation(self):
        router, lv = _nudge_fixture(11.78)
        assert _pairs(router) == set()

        result = drc_verify_and_nudge(router)

        assert result.segments_nudged == 1
        assert result.pairwise_reverts == 0
        assert _pairs(router) == {("HV", "LV")}

    def test_gated_nudge_reverts_to_the_entry_snapshot(self):
        router, lv = _nudge_fixture(11.78)
        _arm(router)
        before = _geometry(lv)

        result = drc_verify_and_nudge(router)

        assert result.pairwise_reverts == 1
        assert _geometry(lv) == before
        assert _pairs(router) == set()
        # The reverted route re-opens the scalar violation the nudge fixed --
        # reported honestly rather than as a pre-revert success.
        assert result.remaining_violations == 1
        assert "HV pairwise gate: 1 route(s) reverted to pre-nudge geometry" in result.summary()

    def test_inherited_violation_is_not_reverted(self):
        """A pair already dirty at entry stays the audit's problem, not the gate's."""
        router, lv = _nudge_fixture(11.0)
        _arm(router)
        assert _pairs(router) == {("HV", "LV")}
        before = _geometry(lv)

        result = drc_verify_and_nudge(router)

        assert result.pairwise_reverts == 0
        assert _geometry(lv) != before
        assert _pairs(router) == {("HV", "LV")}

    def test_clear_nudge_is_left_alone(self):
        router, lv = _nudge_fixture(20.0)
        _arm(router)
        before = _geometry(lv)

        result = drc_verify_and_nudge(router)

        assert result.pairwise_reverts == 0
        assert _geometry(lv) != before


class TestPostPassesTogether:
    """AC4 headline -- optimize + nudge introduce nothing the audit did not see."""

    def test_full_post_pass_chain_introduces_no_new_pairs(self):
        router, _ = _optimize_fixture()
        _arm(router)
        before = _pairs(router)

        optimize_routes_grid_synced(router, _optimizer())
        drc_verify_and_nudge(router)
        consolidate_routes_grid_synced(router)

        assert _pairs(router) - before == set()

    def test_full_post_pass_chain_is_dirty_without_the_table(self):
        """The same chain on the pre-#4766 (dormant) path -- fails-on-main proof."""
        router, _ = _optimize_fixture()
        before = _pairs(router)

        optimize_routes_grid_synced(router, _optimizer())
        drc_verify_and_nudge(router)
        consolidate_routes_grid_synced(router)

        assert _pairs(router) - before == {("HV", "LV")}


class TestConsolidationIsNotGated:
    """AC3 -- copper-preserving by construction, so no gate is added."""

    def test_consolidation_runs_identically_with_the_table_armed(self):
        armed_geometry = None
        for arm in (False, True):
            router = Autorouter(width=40.0, height=40.0)
            router.net_names = dict(ID_TO_NAME)
            _commit(router, _route(1, "HV", [(5.0, 10.0), (25.0, 10.0)]))
            # Collinear run one merge away from a single segment, held at the
            # HV requirement boundary so a route-level veto would be visible.
            _commit(
                router,
                _route(2, "LV", [(8.0, 11.7), (12.0, 11.7), (16.0, 11.7), (20.0, 11.7)]),
            )
            if arm:
                _arm(router)

            stats = consolidate_routes_grid_synced(router)

            assert stats.runs_merged == 1
            geometry = _geometry(router.routes[1])
            assert len(geometry) == 1
            if armed_geometry is None:
                armed_geometry = geometry
            else:
                assert geometry == armed_geometry


class TestDormancy:
    """AC6 -- no ``--voltage-map`` means the predicate is never even consulted."""

    def test_no_pairwise_scan_happens_without_a_table(self, monkeypatch):
        def explode(*_args, **_kwargs):  # pragma: no cover - must never run
            raise AssertionError("dormant run consulted the pairwise predicate")

        monkeypatch.setattr(
            "kicad_tools.router.pairwise_clearance.find_pairwise_violations", explode
        )
        monkeypatch.setattr(
            "kicad_tools.router.pairwise_clearance.route_pairwise_violation", explode
        )
        monkeypatch.setattr("kicad_tools.router.optimizer.trace.route_pairwise_violation", explode)

        router, _ = _optimize_fixture()
        optimize_routes_grid_synced(router, _optimizer())
        drc_verify_and_nudge(router)
        consolidate_routes_grid_synced(router)

    def test_dormant_optimize_output_matches_the_ungated_transform(self):
        """Geometry from the gated code path with no table == ungated geometry."""
        expected = [(5.0, 14.0, 7.5, 11.5), (7.5, 11.5, 20.0, 11.5)]
        router, _ = _optimize_fixture()

        optimize_routes_grid_synced(router, _optimizer())

        assert _geometry(router.routes[1]) == expected
        assert not hasattr(router, "_pairwise_optimize_vetoes")

    def test_dormant_nudge_reports_zero_reverts(self):
        router, _ = _nudge_fixture(11.78)
        result = drc_verify_and_nudge(router)
        assert result.pairwise_reverts == 0
        assert "HV pairwise gate" not in result.summary()


class TestEdgeCases:
    def test_empty_route_list(self):
        router = Autorouter(width=40.0, height=40.0)
        router.net_names = dict(ID_TO_NAME)
        _arm(router)

        optimize_routes_grid_synced(router, _optimizer())
        result = drc_verify_and_nudge(router)

        assert router.routes == []
        assert result.pairwise_reverts == 0

    def test_nudge_on_a_router_without_a_grid(self):
        """The stub-router shape the nudge unit tests drive (no grid, no resync)."""
        rules = DesignRules(trace_clearance=0.2)
        rules.pairwise_clearance = TABLE
        lv = _route(2, "LV", [(8.0, 11.78), (22.0, 11.78)])
        stub = _StubRouter(
            routes=[
                _route(1, "HV", [(5.0, 10.0), (25.0, 10.0)]),
                lv,
                _route(3, "OBST", [(8.0, 12.13), (22.0, 12.13)]),
            ],
            rules=rules,
            net_names=dict(ID_TO_NAME),
        )
        before = _geometry(lv)

        result = drc_verify_and_nudge(stub)  # type: ignore[arg-type]

        # The gate is armed and reverts even without a grid to resync.
        assert result.pairwise_reverts == 1
        assert _geometry(lv) == before

    def test_zone_resolving_to_a_single_net_cannot_waive(self):
        router, lv = _optimize_fixture()
        _arm(router, zones=(AttachZone(5.0, 9.0, 16.0, 13.0, frozenset({"HV"})),))
        before = _geometry(lv)

        optimize_routes_grid_synced(router, _optimizer())

        assert _geometry(router.routes[1]) == before
        assert router._pairwise_optimize_vetoes == 1


@pytest.mark.parametrize("armed", [False, True])
def test_skip_nets_still_bypasses_the_pass(armed):
    """#3508 protection is upstream of the gate and stays intact."""
    router, lv = _optimize_fixture()
    if armed:
        _arm(router)
    before = _geometry(lv)

    optimize_routes_grid_synced(router, _optimizer(), skip_nets={2})

    assert router.routes[1] is lv
    assert _geometry(lv) == before
