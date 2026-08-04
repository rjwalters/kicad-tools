"""Lattice honors preserved (non-listed) copper as a hard obstacle (#4355).

Mode B of issue #4355: the lattice negotiation used to seed its clearance
model from PADS ONLY -- it never ingested ``router.existing_routes``, so a
newly-routed net legally crossed the preserved copper of a non-listed net and
shipped a ``clearance_segment_segment`` SHORT.

``LatticePathfinder.route_netset`` now accepts ``fixed_copper`` (the preserved
``Route``s), pre-seeds each per-pass ``CommittedCopper`` with it as an immovable
hard block, and therefore either routes AROUND the fixed copper or declines the
connection as unroutable -- it never emits copper overlapping the fixed net.

Issue #4597 extends that seed contract with the preserved net's CLASS
clearance.  ``route_netset(..., fixed_clearance={net: clr})`` seeds each
preserved segment/via at its own ``--net-class-map`` clearance instead of the
board-global DRU floor, so a multi-step ``--preserve-existing`` composition
(the documented HV-outer recipe) spaces cross-step pairs at the mapped gap --
the same result the map produces when both nets are routed in one pass.

These are fast unit-level checks (a handful of pads, no full board or
``kicad-cli``): they pin the seed contract that the CLI ``--nets`` /
``--preserve-existing`` paths depend on.
"""

from __future__ import annotations

from kicad_tools.router.lattice.geometry import seg_seg_dist
from kicad_tools.router.lattice.obstacles import CommittedCopper
from kicad_tools.router.lattice.pathfinder import LatticePathfinder
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.primitives import Pad, Route, Segment, Via
from kicad_tools.router.rules import DesignRules, NetClassRouting

_OUTLINE = [(0.0, 0.0), (20.0, 0.0), (20.0, 20.0), (0.0, 20.0)]


def _pad(x: float, y: float, net: int, *, ref: str) -> Pad:
    return Pad(
        x=x,
        y=y,
        width=1.0,
        height=1.0,
        net=net,
        net_name=f"N{net}",
        layer=Layer.F_CU,
        ref=ref,
        pin="1",
    )


def _wall_segment(layer: Layer, *, y1: float, y2: float, width: float = 0.5) -> Segment:
    """A vertical foreign-net (net 2) copper wall at x=10 on ``layer``."""
    return Segment(
        x1=10.0,
        y1=y1,
        x2=10.0,
        y2=y2,
        width=width,
        layer=layer,
        net=2,
        net_name="N2",
    )


def _shorts_against(
    routes: dict[object, Route],
    wall: Segment,
    *,
    trace_half: float,
    clearance: float,
) -> bool:
    """True if any routed segment overlaps ``wall`` below the required gap.

    Mirrors ``clearance_segment_segment``: a same-layer, cross-net segment whose
    centreline distance to the wall is under ``trace_half + wall_half +
    clearance`` is an emitted short.
    """
    wall_half = wall.width / 2.0
    gap = trace_half + wall_half + clearance
    wa = (wall.x1, wall.y1)
    wb = (wall.x2, wall.y2)
    for route in routes.values():
        for seg in route.segments:
            if seg.layer != wall.layer or seg.net == wall.net:
                continue
            d = seg_seg_dist((seg.x1, seg.y1), (seg.x2, seg.y2), wa, wb)
            if d < gap - 1e-6:
                return True
    return False


def _fixture():
    rules = DesignRules()
    stack = LayerStack.two_layer()
    # Net 1: two pads straddling the wall at x=10; the shortest route is a
    # straight F_CU trace at y=10 that crosses the wall dead-centre.
    pads = [_pad(2.0, 10.0, 1, ref="A"), _pad(18.0, 10.0, 1, ref="B")]
    conns = [((1, 0), pads[0], pads[1], None)]
    return rules, stack, pads, conns


def test_baseline_without_fixed_copper_shorts_through_wall() -> None:
    """Without ``fixed_copper`` the straight route crosses the wall (the bug)."""
    rules, stack, pads, conns = _fixture()
    wall = _wall_segment(Layer.F_CU, y1=2.0, y2=18.0)

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes, stats = pf.route_netset(conns, max_iterations=6)

    # The net routes (straight, shortest) and -- because the lattice never saw
    # the wall's copper -- it crosses it: a segment-segment short.
    assert stats.routed == 1
    assert _shorts_against(
        routes, wall, trace_half=rules.trace_width / 2.0, clearance=rules.trace_clearance
    )


def test_fixed_copper_prevents_short_through_preserved_net() -> None:
    """Seeding the wall as ``fixed_copper`` -> the route detours; never a short."""
    rules, stack, pads, conns = _fixture()
    wall = _wall_segment(Layer.F_CU, y1=2.0, y2=18.0)

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes, _stats = pf.route_netset(
        conns, fixed_copper=[Route(net=2, net_name="N2", segments=[wall])], max_iterations=6
    )

    # Whether the net detours (via to B_CU, across, back) or is declined, it is
    # NEVER emitted overlapping the F_CU wall.
    assert not _shorts_against(
        routes, wall, trace_half=rules.trace_width / 2.0, clearance=rules.trace_clearance
    )


def test_fixed_copper_full_partition_declines_not_shorts() -> None:
    """A wall on EVERY layer (no via detour) -> honest decline, not a short."""
    rules, stack, pads, conns = _fixture()
    # Full-height walls (beyond the board on both ends) on both routing layers:
    # no same-layer detour and no via crossing is possible.
    walls = [
        _wall_segment(Layer.F_CU, y1=-2.0, y2=22.0),
        _wall_segment(Layer.B_CU, y1=-2.0, y2=22.0),
    ]
    fixed = [Route(net=2, net_name="N2", segments=walls)]

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes, stats = pf.route_netset(conns, fixed_copper=fixed, max_iterations=6)

    # Reported unroutable rather than shorted.
    assert stats.routed == 0
    for wall in walls:
        assert not _shorts_against(
            routes, wall, trace_half=rules.trace_width / 2.0, clearance=rules.trace_clearance
        )


def test_fixed_copper_empty_is_byte_identical_noop() -> None:
    """``fixed_copper=None`` / ``[]`` leaves negotiation exactly as before."""
    rules, stack, pads, conns = _fixture()

    pf_none = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes_none, stats_none = pf_none.route_netset(conns, max_iterations=6)

    pf_empty = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes_empty, stats_empty = pf_empty.route_netset(conns, fixed_copper=[], max_iterations=6)

    assert stats_none.routed == stats_empty.routed == 1
    # Same key set and same emitted geometry (the seed set is empty either way).
    assert routes_none.keys() == routes_empty.keys()
    for key in routes_none:
        segs_a = [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in routes_none[key].segments]
        segs_b = [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in routes_empty[key].segments]
        assert segs_a == segs_b


# ---------------------------------------------------------------------------
# Issue #4597: preserved copper keeps its NET-CLASS clearance across the pass
# boundary (``--preserve-existing`` multi-step composition).
# ---------------------------------------------------------------------------

_HV_CLEARANCE = 2.0  # what a per-step --net-class-map puts on the HV net


def _short_walls() -> list[Segment]:
    """A stub wall on BOTH routing layers, short enough to route around.

    The full-height wall of the #4355 fixtures forces a decline once the gap
    grows to HV size; this one leaves a legal detour at either end on both
    layers, so the test measures the SPACING the map produced rather than
    merely "declined".
    """
    return [
        _wall_segment(Layer.F_CU, y1=8.0, y2=12.0),
        _wall_segment(Layer.B_CU, y1=8.0, y2=12.0),
    ]


def _min_gap_to_walls(routes: dict[object, Route], walls: list[Segment]) -> float:
    """Smallest EDGE-to-EDGE copper gap between routed copper and ``walls``.

    Centreline distance minus both half-widths, so the value is directly
    comparable with a required clearance (what a DRC report measures).
    """
    best = float("inf")
    for route in routes.values():
        for seg in route.segments:
            for wall in walls:
                if seg.layer != wall.layer or seg.net == wall.net:
                    continue
                d = seg_seg_dist(
                    (seg.x1, seg.y1),
                    (seg.x2, seg.y2),
                    (wall.x1, wall.y1),
                    (wall.x2, wall.y2),
                )
                best = min(best, d - seg.width / 2.0 - wall.width / 2.0)
    return best


def _route_against_walls(
    *,
    fixed_clearance: dict[int, float] | None,
    net_class: object | None = None,
) -> tuple[dict[object, Route], object, list[Segment]]:
    rules, stack, pads, _conns = _fixture()
    walls = _short_walls()
    conns = [((1, 0), pads[0], pads[1], net_class)]
    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes, stats = pf.route_netset(
        conns,
        fixed_copper=[Route(net=2, net_name="N2", segments=walls)],
        fixed_clearance=fixed_clearance,
        max_iterations=6,
    )
    return routes, stats, walls


def test_preserved_net_class_clearance_spaces_cross_step_pair() -> None:
    """Cross-step spacing: a preserved net mapped at 2.0mm gets 2.0mm (#4597).

    This is the reported defect: step 1 routes the HV net, step 2 routes an LV
    net with ``--preserve-existing`` and a map putting 2.0mm on the HV net.
    Pre-#4597 the preserved copper seeded at the board-global DRU floor, so the
    LV copper landed at ~0.2mm from it.
    """
    baseline, base_stats, walls = _route_against_walls(fixed_clearance=None)
    mapped, mapped_stats, _walls = _route_against_walls(fixed_clearance={2: _HV_CLEARANCE})

    # Both actually route (the wall is short enough to detour around on either
    # layer) -- the mapped run is spaced, not declined.
    assert base_stats.routed == 1
    assert mapped_stats.routed == 1

    base_gap = _min_gap_to_walls(baseline, walls)
    mapped_gap = _min_gap_to_walls(mapped, walls)

    # Pre-fix behavior, pinned: without the map the gap collapses to the DRU
    # floor and is nowhere near the 2.0mm the map asked for.
    assert base_gap < _HV_CLEARANCE
    # Post-fix: the preserved net's class clearance governs, exactly as it
    # would if both nets had been routed in one pass.
    assert mapped_gap >= _HV_CLEARANCE - 1e-6, f"mapped gap {mapped_gap:.4f} < {_HV_CLEARANCE}"


def test_new_net_class_clearance_still_governs_reverse_pairing() -> None:
    """Symmetry: preserved LV at the floor + new HV net mapped at 2.0mm (#4597).

    ``max(own_clr, stored_clr)`` already gave this direction; the test pins
    that the #4597 seed change does not regress it.
    """
    hv = NetClassRouting(name="HV", trace_width=0.2, clearance=_HV_CLEARANCE)
    routes, stats, walls = _route_against_walls(fixed_clearance=None, net_class=hv)

    assert stats.routed == 1
    gap = _min_gap_to_walls(routes, walls)
    assert gap >= _HV_CLEARANCE - 1e-6, f"reverse gap {gap:.4f} < {_HV_CLEARANCE}"


def _geometry(routes: dict[object, Route]) -> list[tuple]:
    return sorted(
        (key[0], key[1], s.x1, s.y1, s.x2, s.y2, s.layer)
        for key, route in routes.items()
        for s in route.segments
        if isinstance(key, tuple)
    )


def test_uniform_map_and_unmapped_nets_are_byte_identical_noops() -> None:
    """A map that adds nothing above the DRU floor changes nothing (#4597).

    Three no-op forms, all byte-identical to ``fixed_clearance=None``:
    a uniform map at ``rules.trace_clearance``, a map naming only nets that are
    not in the fixed set, and a map whose clearance is BELOW the floor (clamped
    up by ``max``, never shrinking the gap).
    """
    rules, _stack, _pads, _conns = _fixture()
    reference, _stats, _walls = _route_against_walls(fixed_clearance=None)

    for label, clearances in (
        ("uniform", {2: rules.trace_clearance}),
        ("unmapped", {99: 3.0}),
        ("below-floor", {2: 0.05}),
        ("empty", {}),
    ):
        routes, stats, _w = _route_against_walls(fixed_clearance=clearances)
        assert stats.routed == 1, label
        assert _geometry(routes) == _geometry(reference), label


def test_seed_tuples_carry_the_resolved_clearance() -> None:
    """``_set_fixed_copper`` stamps the resolved clearance onto every seed."""
    rules, stack, pads, _conns = _fixture()
    walls = _short_walls()
    via = Via(x=10.0, y=14.0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=2)
    fixed = [Route(net=2, net_name="N2", segments=walls, vias=[via])]

    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    pf._set_fixed_copper(fixed, {2: _HV_CLEARANCE})
    assert [r[-1] for r in pf._fixed_runs] == [_HV_CLEARANCE, _HV_CLEARANCE]
    assert pf._fixed_vias == [((10.0, 14.0), 2, _HV_CLEARANCE)]

    # Unmapped / below-floor / absent all resolve to the DRU floor.
    for clearances in (None, {}, {2: 0.05}, {7: 3.0}):
        pf._set_fixed_copper(fixed, clearances)
        assert [r[-1] for r in pf._fixed_runs] == [rules.trace_clearance] * 2
        assert pf._fixed_vias == [((10.0, 14.0), 2, rules.trace_clearance)]

    # ``None`` routes still reset the seed set (no stale fixed copper).
    pf._set_fixed_copper(None, {2: _HV_CLEARANCE})
    assert pf._fixed_runs == [] and pf._fixed_vias == []


def test_preserved_copper_off_the_routing_stack_is_still_skipped() -> None:
    """Copper on a non-routing layer is dropped, mapped clearance or not."""
    rules, stack, pads, _conns = _fixture()
    off_stack = Segment(
        x1=10.0, y1=8.0, x2=10.0, y2=12.0, width=0.5, layer=Layer.IN1_CU, net=2, net_name="N2"
    )
    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)  # two-layer stack
    pf._set_fixed_copper([Route(net=2, net_name="N2", segments=[off_stack])], {2: _HV_CLEARANCE})
    assert pf._fixed_runs == []


def test_same_net_preserved_copper_never_blocks_its_own_net() -> None:
    """A listed net's own stale copper in the fixed set cannot block it (#4355).

    The same-net exemption must survive the #4597 clearance seeding -- a large
    mapped clearance on the net's own copper must not make it unroutable.
    """
    rules, stack, pads, conns = _fixture()
    # Net 1's own stale copper, straight through the corridor it needs.
    stale = Segment(
        x1=10.0, y1=8.0, x2=10.0, y2=12.0, width=0.5, layer=Layer.F_CU, net=1, net_name="N1"
    )
    pf = LatticePathfinder(_OUTLINE, pads, rules, layer_stack=stack)
    routes, stats = pf.route_netset(
        conns,
        fixed_copper=[Route(net=1, net_name="N1", segments=[stale])],
        fixed_clearance={1: 3.0},
        max_iterations=6,
    )
    assert stats.routed == 1
    assert routes


def _committed(rules: DesignRules) -> CommittedCopper:
    return CommittedCopper(
        2,
        trace_half=rules.trace_width / 2.0,
        clearance=rules.trace_clearance,
        via_radius=rules.via_diameter / 2.0,
        via_via_gap=rules.via_diameter + rules.trace_clearance,
        same_net_via_gap=rules.via_drill + rules.min_hole_to_hole,
    )


def test_committed_via_carries_its_class_clearance() -> None:
    """A preserved HV via blocks a new LV trace at the HV class gap (#4597).

    ``CommittedCopper.add_via`` used to take no clearance at all, so the via
    term in ``seg_clear`` / ``node_clear`` was ``via_radius + own_clr +
    own_half`` -- the global via gap, whatever the map said.
    """
    rules = DesignRules()
    own_half = rules.trace_width / 2.0
    via_r = rules.via_diameter / 2.0
    # A probe trace just outside the GLOBAL via gap but well inside the 2.0mm
    # HV gap.
    probe_y = via_r + rules.trace_clearance + own_half + 0.05

    floor = _committed(rules)
    floor.add_via((0.0, 0.0), 2)  # pre-#4597 form: no clearance argument
    assert floor.seg_clear((-1.0, probe_y), (1.0, probe_y), 0, net=1)
    assert floor.node_clear((0.0, probe_y), 0, net=1)

    hv = _committed(rules)
    hv.add_via((0.0, 0.0), 2, _HV_CLEARANCE)
    assert not hv.seg_clear((-1.0, probe_y), (1.0, probe_y), 0, net=1)
    assert not hv.node_clear((0.0, probe_y), 0, net=1)

    # Same net is still exempt, and copper beyond the HV gap is still legal.
    assert hv.seg_clear((-1.0, probe_y), (1.0, probe_y), 0, net=2)
    clear_y = via_r + _HV_CLEARANCE + own_half + 1e-6
    assert hv.seg_clear((-1.0, clear_y), (1.0, clear_y), 0, net=1)


def test_committed_via_to_via_gap_grows_to_the_stored_class_clearance() -> None:
    """Cross-net via-to-via also honors the STORED via's clearance (#4597).

    One-sided by design: ``via_clear`` takes no querying-net clearance, so a
    NEWLY routed HV via still does not apply its own class clearance to other
    vias (pre-existing #4271 residual, deliberately not widened here).
    """
    rules = DesignRules()
    via_r = rules.via_diameter / 2.0
    probe = rules.via_diameter + rules.trace_clearance + 0.05  # just past the global gap

    floor = _committed(rules)
    floor.add_via((0.0, 0.0), 2)
    assert floor.via_clear((probe, 0.0), net=1)

    hv = _committed(rules)
    hv.add_via((0.0, 0.0), 2, _HV_CLEARANCE)
    assert not hv.via_clear((probe, 0.0), net=1)
    assert hv.via_clear((2.0 * via_r + _HV_CLEARANCE + 1e-6, 0.0), net=1)
