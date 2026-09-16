"""Pad access sets: a committed route must not strand an unrouted pad.

Epic #5508 / Phase 1a (issue #5516).  Exercises
:mod:`kicad_tools.router.pad_access` on the 4-pad Kelvin cluster from
``tests/router/test_lattice_kelvin_physical_5444.py::_route_fixture``, extended
with a competing net (``COMP``) through the R10 -> U3 corridor and a third
component (``U9``) flanking U3.  Nothing is routed here: the access set is a
pure-geometry read of the copper committed so far.
"""

import pytest

from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.pad_access import (
    DIRECTIONS,
    AccessSet,
    DefaultAccessLegality,
    affected_pads,
    compute_access_set,
    direction_name,
    route_envelope,
)
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

# The Kelvin cluster's own geometry, verbatim from test_lattice_kelvin_physical_5444.
KELVIN_POSITIONS = {"R10": (5, 3), "Q1": (7, 11), "U2": (9, 14), "U3": (5, 15)}

# U9 flanks U3 west / east / south with a 0.4 mm edge gap -- narrower than the
# 0.6 mm (trace_width + 2 * trace_clearance) a stub needs, so those exits and
# the southern diagonals are closed by pad copper alone.
U9_PADS = {"1": (3.6, 15.0), "2": (6.4, 15.0), "3": (5.0, 16.4)}

# COMP's centreline sits 0.3 mm above U3's top edge (y = 14.5): a legal 0.2 mm
# copper gap for COMP itself, but any north-going 0.2 mm stub overlaps it.
COMP_Y = 14.2

NORTH = (0, -1)
EAST = (1, 0)


def _rules(**overrides) -> DesignRules:
    params = {"grid_resolution": 0.1, "trace_width": 0.2, "trace_clearance": 0.2}
    params.update(overrides)
    return DesignRules(**params)


def _router(**rule_overrides) -> Autorouter:
    return Autorouter(
        20,
        20,
        rules=_rules(**rule_overrides),
        force_python=True,
        physics_enabled=False,
    )


def _add_pad(router, ref, pin, x, y, net, net_name, **extra):
    router.add_component(
        ref,
        [
            {
                "number": pin,
                "x": x,
                "y": y,
                "width": extra.pop("width", 1),
                "height": extra.pop("height", 1),
                "net": net,
                "net_name": net_name,
                "layer": Layer.F_CU,
                **extra,
            }
        ],
    )


def _kelvin_cluster() -> Autorouter:
    """The 4-pad Kelvin cluster plus the U9 flankers.  Nothing is routed."""
    router = _router()
    for ref in ("R10", "U3", "Q1", "U2"):
        x, y = KELVIN_POSITIONS[ref]
        _add_pad(router, ref, "1", x, y, 1, "ISENSE_A+")
    for pin, (x, y) in U9_PADS.items():
        _add_pad(router, "U9", pin, x, y, 3, "FOREIGN")
    return router


def _comp_route() -> Route:
    """The competing net's committed copper (net 2, refs R20/R21)."""
    return Route(
        net=2,
        net_name="COMP",
        segments=[
            Segment(
                x1=1.0,
                y1=COMP_Y,
                x2=9.0,
                y2=COMP_Y,
                width=0.2,
                layer=Layer.F_CU,
                net=2,
                net_name="COMP",
            )
        ],
    )


def _u3_access(router) -> AccessSet:
    return compute_access_set(router.pads[("U3", "1")], router.grid, router.rules)


def _reject_reason(router, pad, direction) -> str:
    """Human-readable rejection for one direction, for assertion messages."""
    import math

    from kicad_tools.router import pad_access

    dx, dy = direction
    norm = math.hypot(dx, dy)
    ux, uy = dx / norm, dy / norm
    start = pad_access._ray_exit_extent(pad, ux, uy)
    length = pad_access._round_up_to_cells(
        router.rules.trace_width + 2 * router.rules.trace_clearance, router.grid.resolution
    )
    seg = Segment(
        x1=round(pad.x + start * ux, 6),
        y1=round(pad.y + start * uy, 6),
        x2=round(pad.x + (start + length) * ux, 6),
        y2=round(pad.y + (start + length) * uy, 6),
        width=router.rules.trace_width,
        layer=pad.layer,
        net=pad.net,
    )
    legal, loc = DefaultAccessLegality(router.grid, router.rules).stub_legal(seg, pad.net)
    _ok, actual, _loc = router.grid.validate_segment_clearance(
        seg, exclude_net=pad.net, min_clearance=router.rules.trace_clearance
    )
    return (
        f"{direction_name(direction)} "
        f"({seg.x1},{seg.y1})->({seg.x2},{seg.y2}) legal={legal} "
        f"clearance={actual:.4f} required={router.rules.trace_clearance} at={loc}"
    )


# ---------------------------------------------------------------------------
# The headline assertion: a committed competing route strands U3.
# ---------------------------------------------------------------------------


def test_u3_has_a_north_exit_before_the_competing_route_is_committed():
    router = _kelvin_cluster()
    access = _u3_access(router)
    pad = router.pads[("U3", "1")]

    assert not access.is_empty()
    # Not merely "non-empty": the surviving exit is the NORTH corridor COMP
    # will later occupy.  Naming it makes a coordinate slip fail loudly.
    north = access.stub_for(NORTH)
    assert north is not None, [_reject_reason(router, pad, d) for d in DIRECTIONS]
    assert (north.x0, north.y0) == (5.0, 14.5)
    assert (north.x1, north.y1) == (5.0, 13.9)
    assert north.width == router.rules.trace_width
    assert access.closing_copper == ()
    assert access.origin == (5, 15)
    assert access.origin_layer is Layer.F_CU
    assert access.origin_is_escape_terminal is False
    assert access.pad_key == ("U3", "1")


def test_competing_route_closes_every_exit_direction_individually():
    router = _kelvin_cluster()
    pad = router.pads[("U3", "1")]

    before = {
        direction: _u3_access(router).stub_for(direction) is not None for direction in DIRECTIONS
    }
    assert before[NORTH] is True
    # Every non-north direction is already closed by U9's pad copper.
    for direction in DIRECTIONS:
        if direction == NORTH:
            continue
        assert before[direction] is False, _reject_reason(router, pad, direction)

    router._mark_route(_comp_route())

    after = _u3_access(router)
    for direction in DIRECTIONS:
        assert after.stub_for(direction) is None, _reject_reason(router, pad, direction)


def test_competing_route_leaves_u3_with_an_empty_access_set():
    router = _kelvin_cluster()
    router._mark_route(_comp_route())
    access = _u3_access(router)

    assert access.is_empty()
    assert access.stubs == ()
    # The default stack is TWO layers, so via sites are genuinely enumerated --
    # they come out empty because a via site is only reachable in-pad (which
    # needs a via-in-pad fab tier, not configured here) or at a LEGAL stub end.
    assert router.grid.num_layers == 2
    assert access.via_sites == ()


def test_closing_copper_names_the_foreign_pads_and_the_competing_track():
    router = _kelvin_cluster()
    router._mark_route(_comp_route())
    access = _u3_access(router)

    assert access.is_empty()
    named = {(item.kind, item.ref, item.pin) for item in access.closing_copper}
    assert named == {
        ("foreign_pad", "U9", "1"),
        ("foreign_pad", "U9", "2"),
        ("foreign_pad", "U9", "3"),
        ("route_segment", "COMP", ""),
    }
    assert {item.net for item in access.closing_copper} == {2, 3}
    # None of it is shareable: negotiation cannot price its way through pad
    # copper or a hard-marked foreign route.  That is the epic's whole point.
    assert all(not item.shareable for item in access.closing_copper)
    assert all(item.marking for item in access.closing_copper)


def test_usage_marking_alone_does_not_close_access():
    """``mark_route_usage`` is invisible to the geometric predicates (#5516).

    The negotiated loop hard-marks its commits through ``_mark_route`` /
    ``mark_route`` (which append to ``grid.routes``); the initial pass ALSO
    calls ``mark_route_usage``, which only bumps ``usage_count``.  A fixture
    that used usage marking alone would silently assert nothing.
    """
    router = _kelvin_cluster()
    router.grid.mark_route_usage(_comp_route())

    access = _u3_access(router)
    assert not access.is_empty()
    assert access.stub_for(NORTH) is not None


# ---------------------------------------------------------------------------
# Via sites: fab tier, hole-to-hole, via-in-pad
# ---------------------------------------------------------------------------


def test_via_sites_are_enumerated_at_legal_stub_ends_on_a_two_layer_stack():
    router = _router(manufacturer="jlcpcb")
    _add_pad(router, "U1", "1", 5, 5, 1, "SIG")
    access = compute_access_set(router.pads[("U1", "1")], router.grid, router.rules)

    assert len(access.stubs) == len(DIRECTIONS)
    assert len(access.via_sites) == len(DIRECTIONS)
    for site in access.via_sites:
        # jlcpcb floors: drill >= 0.3, annular >= 0.15 => diameter >= 0.6.
        assert site.drill >= 0.3
        assert site.diameter >= 0.6
        assert site.layers == (Layer.F_CU, Layer.B_CU)
        assert site.in_pad is False
        assert site.from_stub is not None
        stub = access.stubs[site.from_stub]
        assert (site.x, site.y) == (stub.x1, stub.y1)


def test_hole_to_hole_floor_removes_a_via_site_without_closing_its_stub():
    """A PTH neighbour 0.95 mm past the east stub end clears copper but not drill.

    Copper: 0.95 - 0.325 (pad half) - 0.35 (via radius) = 0.275 >= via_clearance.
    Drill:  0.95 - 0.175 (via drill radius) - 0.3 (pad drill radius) = 0.475
            < min_hole_to_hole (0.5) -> the site is rejected, the stub is not.
    """
    router = _router(manufacturer="jlcpcb")
    _add_pad(router, "U1", "1", 5, 5, 1, "SIG")
    _add_pad(
        router,
        "J1",
        "1",
        7.05,
        5,
        3,
        "PTH",
        width=0.65,
        height=0.65,
        through_hole=True,
        drill=0.6,
    )
    access = compute_access_set(router.pads[("U1", "1")], router.grid, router.rules)

    assert router.rules.min_hole_to_hole == 0.5
    east = access.stub_for(EAST)
    assert east is not None and (east.x1, east.y1) == (6.1, 5.0)
    assert (6.1, 5.0) not in {(site.x, site.y) for site in access.via_sites}
    assert len(access.via_sites) == len(DIRECTIONS) - 1


def test_the_same_neighbour_without_a_drill_leaves_the_via_site_legal():
    """Isolates the hole-to-hole floor as the cause in the test above."""
    router = _router(manufacturer="jlcpcb")
    _add_pad(router, "U1", "1", 5, 5, 1, "SIG")
    _add_pad(router, "J1", "1", 7.05, 5, 3, "SMD", width=0.65, height=0.65)
    access = compute_access_set(router.pads[("U1", "1")], router.grid, router.rules)

    assert (6.1, 5.0) in {(site.x, site.y) for site in access.via_sites}
    assert len(access.via_sites) == len(DIRECTIONS)


@pytest.mark.parametrize(
    ("manufacturer", "expect_in_pad"),
    [("jlcpcb", False), ("jlcpcb-tier1", True)],
)
def test_via_in_pad_sites_appear_only_on_a_tier_that_supports_them(manufacturer, expect_in_pad):
    router = _router(manufacturer=manufacturer)
    _add_pad(router, "U1", "1", 5, 5, 1, "SIG")
    access = compute_access_set(router.pads[("U1", "1")], router.grid, router.rules)

    in_pad = [site for site in access.via_sites if site.in_pad]
    assert bool(in_pad) is expect_in_pad
    if expect_in_pad:
        assert (in_pad[0].x, in_pad[0].y) == (5, 5)
        assert in_pad[0].from_stub is None


# ---------------------------------------------------------------------------
# Negative control: the raster halo rejects, the geometry accepts (#5410).
# ---------------------------------------------------------------------------


def test_coarse_raster_halo_rejects_a_candidate_the_geometry_accepts():
    """Mirrors the #5410 ``DQS_N`` precedent that motivates the epic.

    ``_mark_via`` blocks a SQUARE of
    ``int((0.35 + 0.2 + 0.1) / 0.1) + 1 = 7`` cells (0.7 mm) around a 0.7 mm /
    0.35 mm via, so the east stub's end cell reads as blocked.  The true copper
    gap there is 0.213 mm against a 0.200 mm rule, so the stub is LEGAL and the
    access set must stay non-empty -- the raster is a labelling input only.
    """
    router = _router()
    _add_pad(router, "U1", "1", 5, 5, 5, "DQ3")
    foreign_via = Via(
        x=6.763,
        y=5.0,
        drill=0.35,
        diameter=0.7,
        layers=(Layer.F_CU, Layer.B_CU),
        net=4,
        net_name="DQS_N",
    )
    router._mark_route(Route(net=4, net_name="DQS_N", segments=[], vias=[foreign_via]))

    stub_end = router.grid.world_to_grid(6.1, 5.0)
    assert router.grid.is_blocked(stub_end[0], stub_end[1], Layer.F_CU, 5) is True

    access = compute_access_set(router.pads[("U1", "1")], router.grid, router.rules)
    east = access.stub_for(EAST)
    assert east is not None
    assert (east.x1, east.y1) == (6.1, 5.0)
    assert not access.is_empty()

    # And the gap really is above the rule, not merely "close".
    copper_gap = 6.763 - east.x1 - foreign_via.diameter / 2 - east.width / 2
    assert copper_gap == pytest.approx(0.213, abs=1e-6)
    assert copper_gap > router.rules.trace_clearance


# ---------------------------------------------------------------------------
# Same-net Kelvin copper: invisible to the predicates unless the caller says so.
# ---------------------------------------------------------------------------


def test_hard_same_net_copper_closes_access_that_the_predicates_would_allow():
    """``isolate_kelvin_branch``'s sibling branch is same-net, so every
    geometric predicate skips it (``route.net == exclude_net``).  Phase 1b/1c
    know the topology and pass those branches through ``hard_same_net``.
    """
    router = _kelvin_cluster()
    sibling = Route(
        net=1,
        net_name="ISENSE_A+",
        segments=[
            Segment(
                x1=1.0,
                y1=COMP_Y,
                x2=9.0,
                y2=COMP_Y,
                width=0.2,
                layer=Layer.F_CU,
                net=1,
                net_name="ISENSE_A+",
            )
        ],
    )
    router._mark_route(sibling)
    pad = router.pads[("U3", "1")]

    # Same-net: the north exit survives, because merging is electrically fine.
    permissive = compute_access_set(pad, router.grid, router.rules)
    assert permissive.stub_for(NORTH) is not None

    # Declared a hard obstacle for this edge search: the exit closes.
    isolated = compute_access_set(pad, router.grid, router.rules, hard_same_net=[sibling])
    assert isolated.is_empty()
    assert {item.kind for item in isolated.closing_copper} == {
        "foreign_pad",
        "kelvin_isolated",
    }


# ---------------------------------------------------------------------------
# Incremental invalidation contract for Phase 1b.
# ---------------------------------------------------------------------------


def test_route_envelope_matches_the_marked_envelope():
    rules = _rules()
    route = _comp_route()
    envelope = route_envelope(route, rules)

    dilation = max(
        rules.trace_clearance + rules.trace_width / 2,
        rules.via_clearance + rules.via_diameter / 2,
    )
    assert envelope == (
        pytest.approx(1.0 - 0.1 - dilation),
        pytest.approx(COMP_Y - 0.1 - dilation),
        pytest.approx(9.0 + 0.1 + dilation),
        pytest.approx(COMP_Y + 0.1 + dilation),
    )


def test_route_envelope_of_an_empty_route_is_degenerate():
    assert route_envelope(Route(net=7, net_name="NC"), _rules()) == (0.0, 0.0, 0.0, 0.0)


def test_affected_pads_returns_only_bbox_intersecting_terminals():
    router = _kelvin_cluster()
    sets = {
        key: compute_access_set(router.pads[key], router.grid, router.rules)
        for key in (("U3", "1"), ("U2", "1"), ("R10", "1"))
    }
    envelope = route_envelope(_comp_route(), router.rules)

    hits = affected_pads(sets, envelope)
    # COMP runs at y = 14.2 across the board: U3 (y=15) and U2 (y=14) are inside
    # the dilated envelope; R10 (y=3) is nowhere near it.
    assert hits == [("U2", "1"), ("U3", "1")]
    assert ("R10", "1") not in hits


def test_affected_pads_is_sorted_and_stable():
    router = _kelvin_cluster()
    sets = {
        key: compute_access_set(router.pads[key], router.grid, router.rules)
        for key in (("U3", "1"), ("U2", "1"), ("Q1", "1"), ("R10", "1"))
    }
    # A huge envelope hits everything; the order must be deterministic.
    assert affected_pads(sets, (-100.0, -100.0, 100.0, 100.0)) == sorted(sets)


def test_fingerprint_tracks_the_rules_a_journal_was_computed_under():
    router = _kelvin_cluster()
    access = _u3_access(router)
    assert access.fingerprint == (
        router.rules.trace_width,
        router.rules.trace_clearance,
        router.rules.via_clearance,
        router.rules.min_hole_to_hole,
        router.rules.manufacturer,
        router.grid.num_layers,
    )

    tiered = _router(manufacturer="jlcpcb-tier1")
    _add_pad(tiered, "U3", "1", 5, 15, 1, "ISENSE_A+")
    other = compute_access_set(tiered.pads[("U3", "1")], tiered.grid, tiered.rules)
    assert other.fingerprint != access.fingerprint


# ---------------------------------------------------------------------------
# Read-only contract.
# ---------------------------------------------------------------------------


def test_compute_access_set_does_not_mutate_the_grid():
    router = _kelvin_cluster()
    router._mark_route(_comp_route())
    grid = router.grid

    before = (
        grid._blocked.copy(),
        grid._net.copy(),
        grid._usage_count.copy(),
        len(grid.routes),
        len(grid._pads),
    )
    for key in router.pads:
        compute_access_set(router.pads[key], grid, router.rules)

    assert (grid._blocked == before[0]).all()
    assert (grid._net == before[1]).all()
    assert (grid._usage_count == before[2]).all()
    assert len(grid.routes) == before[3]
    assert len(grid._pads) == before[4]
