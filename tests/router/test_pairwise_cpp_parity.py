"""C++ ``validate_route`` pairwise (HV-isolation) clearance parity (Issue #4510).

Phase 2a of epic #4431 -- the *validation-layer* half.  Phase 1 (#4454) taught
only the Python validator about net-pair creepage requirements, so with the C++
backend active the authoritative geometric validator could not see them at all.
This module covers:

* the dormant-by-default ``Grid3D.set_pairwise_domains`` /
  ``Grid3D.set_attach_zones`` setters (never called -> pre-#4510 behaviour);
* ``max(effective_scalar, matrix[dom_a][dom_b])`` across every copper pair
  ``validate_route`` walks -- segment-vs-pad, segment-vs-segment,
  segment-vs-via, via-vs-segment and via-vs-via;
* attach-zone exemption parity with the Python fixtures from #4506, including
  the floor guarantee (an exemption waives the *widening*, never the DRU);
* diff-pair partner precedence over the matrix widening (#2559 / #2556); and
* the ``cpp_backend.py`` plumbing that translates net *names* to the net *ids*
  ``Grid3D`` speaks.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.cpp_backend import CppGrid, CppPathfinder, is_cpp_available
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.pairwise_clearance import (
    AttachZone,
    attach_zones_to_net_ids,
    build_cpp_domain_matrix,
    build_pairwise_clearance_table,
    find_pairwise_violations,
)
from kicad_tools.router.primitives import Pad, Route, Segment
from kicad_tools.router.rules import DesignRules, NetClassRouting

requires_cpp = pytest.mark.skipif(
    not is_cpp_available(),
    reason="C++ router backend not available",
)

# IEC 60664-1, PD2, material group IIIa @ 150 V -> 1.6 mm (same constant the
# Python-side suite pins in ``test_pairwise_clearance.py``).
IEC_150V_PD2_IIIA_MM = 1.6
DRU = 0.2
TRACE_WIDTH = 0.2

HV_NET = 1  # /AC_LINE
LV_NET = 2  # /GND
HV_TAP_NET = 3  # /AC_LINE_TAP -- same domain as HV_NET
NET_NAMES = {"/AC_LINE": HV_NET, "/GND": LV_NET, "/AC_LINE_TAP": HV_TAP_NET}
VOLTAGES = {"/AC_LINE": 150.0, "/GND": 0.0, "/AC_LINE_TAP": 150.0}


# ---------------------------------------------------------------------------
# Fixtures / helpers
# ---------------------------------------------------------------------------


def _table(dru: float = DRU):
    return build_pairwise_clearance_table(VOLTAGES, dru=dru)


def _cpp_grid():
    from kicad_tools.router import router_cpp

    # 20 x 20 mm at 0.1 mm resolution; validate_route only consults the
    # explicitly registered pads / stored segments / stored vias.
    return router_cpp.Grid3D(200, 200, 2, 0.1, 0.0, 0.0)


def _cpp_segment(x1, y1, x2, y2, net, layer=0, width=TRACE_WIDTH):
    from kicad_tools.router import router_cpp

    seg = router_cpp.Segment()
    seg.x1, seg.y1, seg.x2, seg.y2 = x1, y1, x2, y2
    seg.width = width
    seg.layer = layer
    seg.net = net
    return seg


def _cpp_via(x, y, net, layer_from=0, layer_to=1, drill=0.3, diameter=0.6):
    from kicad_tools.router import router_cpp

    via = router_cpp.Via()
    via.x, via.y = x, y
    via.drill = drill
    via.diameter = diameter
    via.layer_from = layer_from
    via.layer_to = layer_to
    via.net = net
    return via


def _cpp_zone(min_x, min_y, max_x, max_y, net_ids):
    from kicad_tools.router import router_cpp

    zone = router_cpp.AttachZone()
    zone.min_x, zone.min_y, zone.max_x, zone.max_y = min_x, min_y, max_x, max_y
    zone.net_ids = list(net_ids)
    return zone


def _install_domains(grid, dru: float = DRU) -> None:
    domains = build_cpp_domain_matrix(_table(dru), NET_NAMES)
    assert domains is not None
    grid.set_pairwise_domains(domains.net_to_domain, domains.matrix)


def _validate(grid, segments, vias=(), exclude_net=HV_NET, trace_clearance=DRU, **kwargs):
    return grid.validate_route(
        list(segments),
        list(vias),
        exclude_net,
        [],
        trace_clearance,
        kwargs.get("via_clearance", DRU),
        kwargs.get("min_drill_clearance", 0.102),
        kwargs.get("partner_net", -1),
        kwargs.get("intra_pair_clearance", 0.0),
    )


# ---------------------------------------------------------------------------
# Dormancy / backward compatibility
# ---------------------------------------------------------------------------


@requires_cpp
def test_setters_dormant_by_default() -> None:
    grid = _cpp_grid()
    assert grid.pairwise_active is False
    assert grid.attach_zone_count == 0
    # Every net is domain-less, so the matrix lookup can never widen anything.
    assert grid.pairwise_required_clearance(HV_NET, LV_NET) == pytest.approx(0.0)
    assert grid.attach_zone_exempts(0.0, 0.0, HV_NET, LV_NET) is False


@requires_cpp
def test_uninstalled_matrix_leaves_hv_lv_pair_valid() -> None:
    """Without the setter, a 0.2 mm HV<->LV gap passes at the scalar floor."""
    grid = _cpp_grid()
    grid.add_stored_segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, 0, LV_NET)
    result = _validate(grid, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)])
    assert result.valid


@requires_cpp
def test_empty_matrix_returns_grid_to_dormant_state() -> None:
    grid = _cpp_grid()
    _install_domains(grid)
    assert grid.pairwise_active is True

    grid.set_pairwise_domains([], [])
    assert grid.pairwise_active is False
    grid.add_stored_segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, 0, LV_NET)
    assert _validate(grid, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)]).valid


# ---------------------------------------------------------------------------
# Matrix widening -- parity with the Python validator
# ---------------------------------------------------------------------------


@requires_cpp
def test_seg_seg_parity_with_python_find_pairwise_violations() -> None:
    """C++ and Python agree: HV<->LV below requirement fails, HV<->HV at DRU passes."""
    table = _table()
    py_hv = Segment(0.0, 0.0, 5.0, 0.0, TRACE_WIDTH, Layer.F_CU, net=HV_NET, net_name="/AC_LINE")
    py_lv = Segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, Layer.F_CU, net=LV_NET, net_name="/GND")
    py_tap = Segment(
        0.0, -0.4, 5.0, -0.4, TRACE_WIDTH, Layer.F_CU, net=HV_TAP_NET, net_name="/AC_LINE_TAP"
    )

    python_violations = find_pairwise_violations(
        [
            Route(net=HV_NET, net_name="/AC_LINE", segments=[py_hv]),
            Route(net=LV_NET, net_name="/GND", segments=[py_lv]),
            Route(net=HV_TAP_NET, net_name="/AC_LINE_TAP", segments=[py_tap]),
        ],
        table,
    )
    flagged = {tuple(sorted((v.net_a, v.net_b))) for v in python_violations}
    # Both HV nets are too close to LV; the two equal-potential HV nets are not.
    assert flagged == {("/AC_LINE", "/GND"), ("/AC_LINE_TAP", "/GND")}

    grid = _cpp_grid()
    _install_domains(grid)
    candidate = [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)]

    # Cross-domain foreign copper at the same 0.2 mm gap -> rejected.
    grid.add_stored_segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, 0, LV_NET)
    cross = _validate(grid, candidate)
    assert not cross.valid
    assert cross.violation_type == 2  # seg-seg

    # Same-domain foreign copper at the same gap -> accepted (DRU governs).
    same_domain = _cpp_grid()
    _install_domains(same_domain)
    same_domain.add_stored_segment(0.0, -0.4, 5.0, -0.4, TRACE_WIDTH, 0, HV_TAP_NET)
    assert _validate(same_domain, candidate).valid


@requires_cpp
def test_matrix_accepts_pair_meeting_requirement() -> None:
    grid = _cpp_grid()
    _install_domains(grid)
    # 2.0 mm centre gap -> 1.8 mm edge gap >= 1.6 mm requirement.
    grid.add_stored_segment(0.0, 2.0, 5.0, 2.0, TRACE_WIDTH, 0, LV_NET)
    assert _validate(grid, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)]).valid


@requires_cpp
def test_matrix_widens_segment_vs_pad() -> None:
    """The Python post-check is trace-vs-trace only; C++ also walks pads."""
    grid = _cpp_grid()
    _install_domains(grid)
    # Circular 0.2 mm pad on the LV net, 0.4 mm off the candidate centreline:
    # edge gap = 0.4 - 0.1 (trace) - 0.1 (pad) = 0.2 mm -> at the DRU floor.
    grid.add_pad(2.5, 0.4, 0.2, 0.2, LV_NET, 0, 0, DRU, False)
    result = _validate(grid, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)])
    assert not result.valid
    assert result.violation_type == 1  # seg-pad


@requires_cpp
def test_matrix_widens_segment_vs_stored_via() -> None:
    grid = _cpp_grid()
    _install_domains(grid)
    # Via radius 0.3 + trace radius 0.1 -> 0.8 mm centre distance = 0.4 mm gap.
    grid.add_stored_via(2.5, 0.8, 0.3, 0.6, LV_NET)
    result = _validate(grid, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)])
    assert not result.valid
    assert result.violation_type == 3  # seg-via


@requires_cpp
def test_matrix_widens_candidate_via_vs_stored_segment() -> None:
    grid = _cpp_grid()
    _install_domains(grid)
    grid.add_stored_segment(0.0, 1.0, 5.0, 1.0, TRACE_WIDTH, 0, LV_NET)
    result = _validate(grid, [], [_cpp_via(2.5, 0.0, HV_NET)])
    assert not result.valid
    assert result.violation_type == 4  # via-seg


@requires_cpp
def test_matrix_widens_candidate_via_vs_stored_via() -> None:
    grid = _cpp_grid()
    _install_domains(grid)
    grid.add_stored_via(2.5, 1.0, 0.3, 0.6, LV_NET)
    result = _validate(grid, [], [_cpp_via(2.5, 0.0, HV_NET)])
    assert not result.valid
    assert result.violation_type == 5  # via-via


@requires_cpp
def test_same_net_drill_spacing_unaffected_by_matrix() -> None:
    """Same-net drill spacing is a same-net rule -- the matrix never applies."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.add_stored_via(2.5, 1.0, 0.3, 0.6, HV_NET)
    assert _validate(grid, [], [_cpp_via(2.5, 0.0, HV_NET)]).valid


# ---------------------------------------------------------------------------
# Attach-zone exemption parity (#4506 fixtures, mirrored against C++)
# ---------------------------------------------------------------------------


@requires_cpp
def test_attach_zone_exempts_only_terminating_pair_inside_zone() -> None:
    """Mirror of the Python fixture of the same name against the C++ path."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones([_cpp_zone(0.0, -1.0, 5.0, 1.0, [HV_NET, LV_NET])])
    candidate = [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)]

    # Rated package owns both nets -> the pair may neck down to the DRU floor.
    grid.add_stored_segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, 0, LV_NET)
    assert _validate(grid, candidate).valid

    # A net merely crossing the courtyard -- not a member of the rated
    # footprint's zone -- receives no waiver.
    outside_membership = _cpp_grid()
    _install_domains(outside_membership)
    outside_membership.set_attach_zones([_cpp_zone(0.0, -1.0, 5.0, 1.0, [HV_NET, HV_TAP_NET])])
    outside_membership.add_stored_segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, 0, LV_NET)
    assert not _validate(outside_membership, candidate).valid


@requires_cpp
def test_attach_zone_does_not_exempt_same_pair_outside_zone() -> None:
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones([_cpp_zone(0.0, -1.0, 5.0, 1.0, [HV_NET, LV_NET])])
    # Both segments run well east of the zone.
    grid.add_stored_segment(10.0, 0.4, 15.0, 0.4, TRACE_WIDTH, 0, LV_NET)
    assert not _validate(grid, [_cpp_segment(10.0, 0.0, 15.0, 0.0, HV_NET)]).valid


@requires_cpp
def test_attach_zone_uses_closest_gap_not_long_segment_midpoint() -> None:
    """The gap point is the closest approach, not either segment's midpoint."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones([_cpp_zone(4.0, 4.0, 6.0, 6.0, [HV_NET, LV_NET])])
    # A long foreign segment whose own midpoint is inside the zone, but whose
    # closest approach to the short candidate is far outside it.
    grid.add_stored_segment(0.0, 5.4, 10.0, 5.4, TRACE_WIDTH, 0, LV_NET)
    assert not _validate(grid, [_cpp_segment(9.0, 5.0, 9.5, 5.0, HV_NET)]).valid

    # The mirrored geometry -- a short in-zone foreign neck -- IS waived even
    # though the candidate extends far outside the zone.
    waived = _cpp_grid()
    _install_domains(waived)
    waived.set_attach_zones([_cpp_zone(4.0, 4.0, 6.0, 6.0, [HV_NET, LV_NET])])
    waived.add_stored_segment(4.8, 5.4, 5.2, 5.4, TRACE_WIDTH, 0, LV_NET)
    assert _validate(waived, [_cpp_segment(0.0, 5.0, 10.0, 5.0, HV_NET)]).valid


@requires_cpp
def test_attach_zone_never_waives_below_dru_floor() -> None:
    """The rated-package neck may reach, but never cross, the scalar floor."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones([_cpp_zone(0.0, -1.0, 5.0, 1.0, [HV_NET, LV_NET])])
    # 0.25 mm centre distance minus two 0.1 mm radii = 0.05 mm edge gap.
    grid.add_stored_segment(0.0, 0.25, 5.0, 0.25, TRACE_WIDTH, 0, LV_NET)
    result = _validate(grid, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)])
    assert not result.valid
    assert result.min_clearance == pytest.approx(0.05, abs=1e-5)


@requires_cpp
def test_attach_zone_exempts_pad_walking_false_positive() -> None:
    """A domain-bridging rated footprint's own pads must not fail validation."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.add_pad(2.5, 0.4, 0.2, 0.2, LV_NET, 0, 0, DRU, False)
    assert not _validate(grid, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)]).valid

    exempt = _cpp_grid()
    _install_domains(exempt)
    exempt.add_pad(2.5, 0.4, 0.2, 0.2, LV_NET, 0, 0, DRU, False)
    exempt.set_attach_zones([_cpp_zone(0.0, -1.0, 5.0, 1.0, [HV_NET, LV_NET])])
    assert _validate(exempt, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)]).valid


@requires_cpp
def test_attach_zone_with_more_than_two_nets_requires_both_members() -> None:
    """A 3-pad rated connector's zone exempts any *member* pair, not just two."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones([_cpp_zone(0.0, -1.0, 5.0, 1.0, [HV_NET, LV_NET, HV_TAP_NET])])
    grid.add_stored_segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, 0, LV_NET)
    assert _validate(grid, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)]).valid
    assert grid.attach_zone_exempts(2.5, 0.2, HV_TAP_NET, LV_NET) is True
    assert grid.attach_zone_exempts(2.5, 0.2, HV_NET, 99) is False


# ---------------------------------------------------------------------------
# Diff-pair partner precedence (#2559 / Epic #2556)
# ---------------------------------------------------------------------------


@requires_cpp
def test_partner_relaxation_takes_precedence_over_matrix_widening() -> None:
    """A net that is both diff-pair partner and cross-domain stays tight."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.add_stored_segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, 0, LV_NET)
    candidate = [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)]

    # Without the partner declaration the matrix rejects the 0.2 mm gap.
    assert not _validate(grid, candidate).valid

    # With LV_NET declared as the partner, the within-pair clearance wins.
    assert _validate(grid, candidate, partner_net=LV_NET, intra_pair_clearance=0.1).valid


@requires_cpp
def test_partner_precedence_does_not_leak_to_other_nets() -> None:
    grid = _cpp_grid()
    _install_domains(grid)
    grid.add_stored_segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, 0, LV_NET)
    # A partner declaration for a DIFFERENT net leaves the LV pair widened.
    assert not _validate(
        grid,
        [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET)],
        partner_net=HV_TAP_NET,
        intra_pair_clearance=0.1,
    ).valid


# ---------------------------------------------------------------------------
# Python -> C++ translation helpers
# ---------------------------------------------------------------------------


def test_build_cpp_domain_matrix_projects_nets_onto_domains() -> None:
    domains = build_cpp_domain_matrix(_table(), NET_NAMES)
    assert domains is not None
    net_to_domain, matrix = domains
    # HV and HV_TAP share no domain (Phase 1: one domain per net), but their
    # matrix entry is 0 -- no widening between equal potentials.
    hv, lv, tap = (net_to_domain[n] for n in (HV_NET, LV_NET, HV_TAP_NET))
    assert -1 not in (hv, lv, tap)
    assert matrix[hv][lv] == pytest.approx(IEC_150V_PD2_IIIA_MM)
    assert matrix[lv][hv] == pytest.approx(IEC_150V_PD2_IIIA_MM)
    assert matrix[hv][tap] == pytest.approx(0.0)
    assert matrix[hv][hv] == pytest.approx(0.0)
    # Net 0 (the unconnected/pour convention) never carries a domain.
    assert net_to_domain[0] == -1


def test_build_cpp_domain_matrix_matches_python_resolver() -> None:
    """The exported matrix reproduces ``required_clearance`` above the floor."""
    table = _table()
    domains = build_cpp_domain_matrix(table, NET_NAMES)
    assert domains is not None
    net_to_domain, matrix = domains
    for name_a, id_a in NET_NAMES.items():
        for name_b, id_b in NET_NAMES.items():
            expected = table.required_clearance(name_a, name_b)
            exported = matrix[net_to_domain[id_a]][net_to_domain[id_b]]
            assert max(table.dru, exported) == pytest.approx(expected)


def test_build_cpp_domain_matrix_normalises_leading_slash() -> None:
    # Voltage map without slashes, board net names with them.
    table = build_pairwise_clearance_table({"AC_LINE": 150.0, "GND": 0.0}, dru=DRU)
    domains = build_cpp_domain_matrix(table, {"/AC_LINE": 1, "/GND": 2})
    assert domains is not None
    net_to_domain, matrix = domains
    assert matrix[net_to_domain[1]][net_to_domain[2]] == pytest.approx(IEC_150V_PD2_IIIA_MM)


def test_build_cpp_domain_matrix_dormant_without_widening_pairs() -> None:
    assert build_cpp_domain_matrix(None, NET_NAMES) is None
    # All nets at the same potential -> no pair needs widening.
    flat = build_pairwise_clearance_table({"/A": 0.0, "/B": 0.0}, dru=DRU)
    assert build_cpp_domain_matrix(flat, {"/A": 1, "/B": 2}) is None
    # Participating nets unresolvable to board ids.
    assert build_cpp_domain_matrix(_table(), {}) is None


def test_attach_zones_to_net_ids_translates_and_drops_unresolvable() -> None:
    zones = (
        AttachZone(0.0, -1.0, 5.0, 1.0, frozenset({"AC_LINE", "GND"})),
        AttachZone(9.0, 9.0, 10.0, 10.0, frozenset({"AC_LINE", "NOT_ON_BOARD"})),
    )
    translated = attach_zones_to_net_ids(zones, NET_NAMES)
    assert translated == [(0.0, -1.0, 5.0, 1.0, [HV_NET, LV_NET])]


def test_attach_zones_to_net_ids_preserves_multi_net_zones() -> None:
    zone = AttachZone(0.0, 0.0, 1.0, 1.0, frozenset({"AC_LINE", "GND", "AC_LINE_TAP"}))
    (translated,) = attach_zones_to_net_ids((zone,), NET_NAMES)
    assert translated[4] == [HV_NET, LV_NET, HV_TAP_NET]


# ---------------------------------------------------------------------------
# cpp_backend plumbing
# ---------------------------------------------------------------------------


def _backend_grid_and_rules():
    rules = DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DRU,
        via_clearance=DRU,
        grid_resolution=0.1,
    )
    grid = RoutingGrid(width=20.0, height=20.0, rules=rules, layer_stack=LayerStack.two_layer())
    return grid, rules


@requires_cpp
def test_backend_threads_domain_matrix_and_zones_into_cpp_grid() -> None:
    py_grid, rules = _backend_grid_and_rules()
    rules.pairwise_clearance = _table()
    cpp_grid = CppGrid.from_routing_grid(py_grid)
    backend = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    backend.set_net_name_to_id(dict(NET_NAMES))
    backend.set_attach_zones((AttachZone(0.0, -1.0, 5.0, 1.0, frozenset({"AC_LINE", "GND"})),))

    assert cpp_grid._impl.pairwise_active is False
    backend._sync_pairwise_domains_to_cpp()

    assert cpp_grid._impl.pairwise_active is True
    assert cpp_grid._impl.attach_zone_count == 1
    assert cpp_grid._impl.pairwise_required_clearance(HV_NET, LV_NET) == pytest.approx(
        IEC_150V_PD2_IIIA_MM
    )
    assert cpp_grid._impl.attach_zone_exempts(2.5, 0.2, HV_NET, LV_NET) is True


@requires_cpp
def test_backend_sync_is_noop_without_voltage_map() -> None:
    py_grid, rules = _backend_grid_and_rules()
    cpp_grid = CppGrid.from_routing_grid(py_grid)
    backend = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    backend.set_net_name_to_id(dict(NET_NAMES))

    backend._sync_pairwise_domains_to_cpp()
    assert cpp_grid._impl.pairwise_active is False
    assert cpp_grid._impl.attach_zone_count == 0


@requires_cpp
def test_backend_reinstalls_domains_after_net_id_remap() -> None:
    """Issue #4530: a net-id remap mid-session must re-push the rebuilt payload.

    ``Grid3D::pairwise_active_`` latches ``True`` after the first install and
    never resets on the same grid, so before the fix the second (rebuilt)
    payload was silently never pushed -- the new ids reported ``0.0`` and the
    stale old ids kept their phantom widening.  This mirrors repro case 1 in the
    issue body.
    """
    py_grid, rules = _backend_grid_and_rules()
    rules.pairwise_clearance = _table()
    cpp_grid = CppGrid.from_routing_grid(py_grid)
    backend = CppPathfinder(cpp_grid, rules, diagonal_routing=True)

    backend.set_net_name_to_id({"/AC_LINE": 1, "/GND": 2})
    backend._sync_pairwise_domains_to_cpp()
    assert cpp_grid._impl.pairwise_active is True
    assert cpp_grid._impl.pairwise_required_clearance(1, 2) == pytest.approx(IEC_150V_PD2_IIIA_MM)

    # Renumber the same nets: the rebuilt payload must reach the C++ grid even
    # though ``pairwise_active`` is already ``True`` from the first push.
    backend.set_net_name_to_id({"/AC_LINE": 7, "/GND": 8})
    backend._sync_pairwise_domains_to_cpp()

    # New ids now carry the widening (this is the assertion that fails today,
    # returning 0.0 instead of 1.6).
    assert cpp_grid._impl.pairwise_required_clearance(7, 8) == pytest.approx(IEC_150V_PD2_IIIA_MM)
    # And the old ids no longer report phantom widening (the stale-values half).
    assert cpp_grid._impl.pairwise_required_clearance(1, 2) == pytest.approx(0.0)


@requires_cpp
def test_backend_reinstalls_attach_zones_after_mid_session_change() -> None:
    """Issue #4530: recomputing attach zones after a validate must re-push them.

    ``set_attach_zones()`` has its own invalidation path independent of the
    net-map setter; a zone recomputed mid-session (after the first sync has
    already latched ``pairwise_active``) must still install on the live grid.
    """
    py_grid, rules = _backend_grid_and_rules()
    rules.pairwise_clearance = _table()
    cpp_grid = CppGrid.from_routing_grid(py_grid)
    backend = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    backend.set_net_name_to_id(dict(NET_NAMES))

    backend.set_attach_zones((AttachZone(0.0, -1.0, 5.0, 1.0, frozenset({"AC_LINE", "GND"})),))
    backend._sync_pairwise_domains_to_cpp()
    assert cpp_grid._impl.pairwise_active is True
    assert cpp_grid._impl.attach_zone_exempts(2.5, 0.0, HV_NET, LV_NET) is True
    assert cpp_grid._impl.attach_zone_exempts(15.0, 15.0, HV_NET, LV_NET) is False

    # Move the rated footprint's zone; the new zone must install even though
    # ``pairwise_active`` latched ``True`` on the first sync.
    backend.set_attach_zones((AttachZone(10.0, 10.0, 20.0, 20.0, frozenset({"AC_LINE", "GND"})),))
    backend._sync_pairwise_domains_to_cpp()
    assert cpp_grid._impl.attach_zone_count == 1
    assert cpp_grid._impl.attach_zone_exempts(15.0, 15.0, HV_NET, LV_NET) is True
    assert cpp_grid._impl.attach_zone_exempts(2.5, 0.0, HV_NET, LV_NET) is False


class _CountingImpl:
    """Wraps the real C++ grid impl to count pairwise/zone push calls."""

    def __init__(self, real) -> None:
        self._real = real
        self.domain_pushes = 0
        self.zone_pushes = 0

    @property
    def pairwise_active(self):
        return self._real.pairwise_active

    def set_pairwise_domains(self, net_to_domain, matrix):
        self.domain_pushes += 1
        return self._real.set_pairwise_domains(net_to_domain, matrix)

    def set_attach_zones(self, zones):
        self.zone_pushes += 1
        return self._real.set_attach_zones(zones)


@requires_cpp
def test_backend_sync_pushes_once_per_distinct_payload() -> None:
    """Issue #4530 steady state: repeated syncs push exactly once per payload.

    Guards against a naive fix that unconditionally re-pushes on every
    ``_sync_pairwise_domains_to_cpp()`` (i.e. every ``validate_route``), which
    would regress the hot path ``Autorouter._prepare_routing()`` exercises with
    an identical net map on every pass.
    """
    py_grid, rules = _backend_grid_and_rules()
    rules.pairwise_clearance = _table()
    cpp_grid = CppGrid.from_routing_grid(py_grid)
    backend = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    # Intercept the C++ push calls after construction so the counter sees only
    # the syncs under test.
    spy = _CountingImpl(cpp_grid._impl)
    cpp_grid._impl = spy
    backend.set_net_name_to_id(dict(NET_NAMES))

    backend._sync_pairwise_domains_to_cpp()
    backend._sync_pairwise_domains_to_cpp()
    backend._sync_pairwise_domains_to_cpp()
    assert spy.domain_pushes == 1
    assert spy.zone_pushes == 1

    # A materially different mapping triggers exactly one additional push, not
    # one per subsequent sync.
    backend.set_net_name_to_id({"/AC_LINE": 7, "/GND": 8})
    backend._sync_pairwise_domains_to_cpp()
    backend._sync_pairwise_domains_to_cpp()
    assert spy.domain_pushes == 2
    assert spy.zone_pushes == 2


@requires_cpp
def test_backend_sync_dormant_payload_never_touches_grid() -> None:
    """Issue #4530: a rebuilt-but-``False`` payload must not flip the flag.

    When there is no widening pair (all nets equal potential), the payload
    resolves to ``False`` and the early-return must leave both the C++ grid and
    ``_pairwise_cpp_installed`` untouched -- byte-identical pre-#4510 behaviour.
    """
    py_grid, rules = _backend_grid_and_rules()
    # A flat voltage map -> build_cpp_domain_matrix returns None -> payload False.
    rules.pairwise_clearance = build_pairwise_clearance_table(
        {"/AC_LINE": 0.0, "/GND": 0.0}, dru=DRU
    )
    cpp_grid = CppGrid.from_routing_grid(py_grid)
    backend = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    spy = _CountingImpl(cpp_grid._impl)
    cpp_grid._impl = spy
    backend.set_net_name_to_id({"/AC_LINE": 1, "/GND": 2})

    backend._sync_pairwise_domains_to_cpp()
    assert backend._pairwise_cpp_payload is False
    assert backend._pairwise_cpp_installed is False
    assert spy.domain_pushes == 0
    assert spy.zone_pushes == 0
    assert spy.pairwise_active is False


@requires_cpp
def test_backend_validate_route_rejects_hv_trace_beside_lv_pad() -> None:
    """End-to-end: the C++ matrix catches a pad the Python post-check cannot.

    ``route_pairwise_violation`` (the Phase 1 Python post-check) walks only
    trace-vs-trace copper, so an HV trace hugging a foreign LV *pad* is
    invisible to it.  This is the gap Phase 2a closes.
    """
    py_grid, rules = _backend_grid_and_rules()
    rules.pairwise_clearance = _table()
    cpp_grid = CppGrid.from_routing_grid(py_grid)
    backend = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    backend.set_net_name_to_id(dict(NET_NAMES))

    # LV pad 0.4 mm off the HV candidate's centreline -> 0.2 mm edge gap.
    cpp_grid._impl.add_pad(2.5, 0.4, 0.2, 0.2, LV_NET, 0, 0, DRU, False)

    route = Route(
        net=HV_NET,
        net_name="/AC_LINE",
        segments=[
            Segment(0.0, 0.0, 5.0, 0.0, TRACE_WIDTH, Layer.F_CU, net=HV_NET, net_name="/AC_LINE")
        ],
    )
    start = Pad(0.0, 0.0, 0.2, 0.2, HV_NET, "/AC_LINE")
    end = Pad(5.0, 0.0, 0.2, 0.2, HV_NET, "/AC_LINE")

    assert backend._validate_route_clearance(route, start, end, 1) is not None

    # The same geometry inside a rated footprint's attach zone validates clean.
    backend.set_attach_zones((AttachZone(0.0, -1.0, 5.0, 1.0, frozenset({"AC_LINE", "GND"})),))
    cpp_grid._impl.set_pairwise_domains([], [])  # force a fresh install
    assert backend._validate_route_clearance(route, start, end, 1) is None


# ---------------------------------------------------------------------------
# Phase 2b (#4511): search-time pairwise avoidance -- domain-aware A* blocking
# ---------------------------------------------------------------------------
#
# Phase 2a (above) taught the authoritative *post-route validator* about
# cross-domain (HV-isolation) clearance, but left the A* search HV-blind: it
# kept proposing HV-through-LV paths that validation then rejected, thrashing
# the negotiator instead of converging.  Phase 2b restores the exact
# search<->validate mirror by widening the search-time blocking kernels
# (``cross_domain_trace_blocked`` / ``cross_domain_via_blocked``) and adds a
# soft avoidance gradient (``pairwise_avoidance_cost``) so an HV board routes
# to completion.


def _cpp_rules(trace_width: float = TRACE_WIDTH, clearance: float = DRU):
    from kicad_tools.router import router_cpp

    rules = router_cpp.DesignRules()
    rules.trace_width = trace_width
    rules.trace_clearance = clearance
    rules.via_diameter = 0.6
    rules.via_clearance = clearance
    rules.grid_resolution = 0.1
    rules.cost_straight = 1.0
    return rules


def _pathfinder(grid):
    from kicad_tools.router import router_cpp

    pf = router_cpp.Pathfinder(grid, _cpp_rules(), True)
    # Copper half-extents the widening measures from (trace_width/2, via/2).
    pf.set_search_pair_widths(TRACE_WIDTH / 2.0, 0.3)
    return pf


# The IEC 150 V / PD2 / IIIa requirement is 1.6 mm; at 0.1 mm resolution the
# scalar trace radius (override) is 3 cells and the widened HV<->LV radius is
# ceil((0.1 + 1.6) / 0.1) = 17 cells.  A foreign cell 8 cells away therefore
# sits in the annulus: inside the widening, outside the scalar disc.
SCALAR_RADIUS = 3


@requires_cpp
def test_search_blocks_hv_trace_beside_lv_copper_in_annulus() -> None:
    """A cross-domain cell in the widened annulus HARD-blocks the placement."""
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    # LV copper 8 cells (0.8 mm) above the candidate centreline: well inside
    # the 17-cell widened radius, well outside the 3-cell scalar radius.
    grid.mark_blocked(100, 108, 0, LV_NET, False, False)
    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is True
    # The public ``is_trace_blocked`` now folds the annulus check in too.
    assert pf.is_trace_blocked(100, 100, 0, HV_NET, False, SCALAR_RADIUS) is True


@requires_cpp
def test_search_allows_hv_trace_clear_of_lv_requirement() -> None:
    """A cross-domain cell beyond the widened radius does not block."""
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    # 18 cells (1.8 mm) away -> edge gap 1.7 mm >= 1.6 mm requirement.
    grid.mark_blocked(100, 118, 0, LV_NET, False, False)
    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is False


@requires_cpp
def test_search_same_domain_copper_not_widened() -> None:
    """Equal-potential (same-domain) copper never trips the widening."""
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 108, 0, HV_TAP_NET, False, False)
    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is False


@requires_cpp
def test_search_net0_copper_never_widened() -> None:
    """Net 0 (pour / unconnected convention) carries no domain -> no widening."""
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 108, 0, 0, False, False)
    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is False


@requires_cpp
def test_search_attach_zone_waives_widening() -> None:
    """A rated footprint's attach zone waives the search-time widening (#4506)."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones([_cpp_zone(0.0, 0.0, 20.0, 20.0, [HV_NET, LV_NET])])
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 108, 0, LV_NET, False, False)
    # Inside the zone: the pair may neck down, so the annulus no longer blocks.
    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is False
    # A zone that does not list BOTH nets grants no waiver.
    other = _cpp_grid()
    _install_domains(other)
    other.set_attach_zones([_cpp_zone(0.0, 0.0, 20.0, 20.0, [HV_NET, HV_TAP_NET])])
    pf2 = _pathfinder(other)
    other.mark_blocked(100, 108, 0, LV_NET, False, False)
    assert pf2.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is True


@requires_cpp
def test_search_dormant_without_matrix() -> None:
    """No domain matrix -> the annulus check is a hard no-op (fast path)."""
    grid = _cpp_grid()  # never install domains
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 108, 0, LV_NET, False, False)
    assert grid.pairwise_active is False
    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is False
    assert pf.is_trace_blocked(100, 100, 0, HV_NET, False, SCALAR_RADIUS) is False
    assert pf.pairwise_avoidance_cost(100, 100, 0, HV_NET) == pytest.approx(0.0)


@requires_cpp
def test_search_via_blocked_by_cross_domain_copper() -> None:
    """The via kernel widens for cross-domain copper the scalar disc misses."""
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    # LV copper 10 cells away on a layer the via column spans.
    grid.mark_blocked(100, 110, 1, LV_NET, False, False)
    assert pf.cross_domain_via_blocked(100, 100, HV_NET) is True
    # Same-domain copper does not.
    clear = _cpp_grid()
    _install_domains(clear)
    pf2 = _pathfinder(clear)
    clear.mark_blocked(100, 110, 1, HV_TAP_NET, False, False)
    assert pf2.cross_domain_via_blocked(100, 100, HV_NET) is False


@requires_cpp
def test_search_avoidance_cost_gradient_near_hv() -> None:
    """The soft gradient is positive just beyond the hard block, zero far away."""
    grid = _cpp_grid()
    _install_domains(grid)
    pf = _pathfinder(grid)
    # Hard radius = 17; band = ceil(1.6*0.5/0.1)=8 -> gradient out to 25 cells.
    grid.mark_blocked(100, 120, 0, LV_NET, False, False)  # 20 cells: in the band
    assert pf.pairwise_avoidance_cost(100, 100, 0, HV_NET) > 0.0
    # Far away (well beyond the band): no soft cost.
    far = _cpp_grid()
    _install_domains(far)
    pf2 = _pathfinder(far)
    far.mark_blocked(100, 150, 0, LV_NET, False, False)  # 50 cells away
    assert pf2.pairwise_avoidance_cost(100, 100, 0, HV_NET) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Convergence proof (headline): a two-domain board routes clean end-to-end
# ---------------------------------------------------------------------------


def _seg_rect_gap(seg, x1, x2, y1, y2) -> float:
    """Min distance (mm) from a segment centreline to an axis-aligned rect."""
    import math

    best = float("inf")
    for i in range(101):
        t = i / 100.0
        px = seg.x1 + t * (seg.x2 - seg.x1)
        py = seg.y1 + t * (seg.y2 - seg.y1)
        cx = min(max(px, x1), x2)
        cy = min(max(py, y1), y2)
        best = min(best, math.hypot(px - cx, py - cy))
    return best


def _route_two_domain_board(with_pairwise: bool):
    """Route one HV net across a board with an LV wall on both layers.

    Returns ``(route, used_python_fallback)``.  The LV wall spans BOTH copper
    layers so the HV net cannot simply dip to the other layer -- it must detour
    in-plane, where the pairwise widening is observable.
    """
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.primitives import Pad as PyPad

    rules = DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DRU,
        via_diameter=0.6,
        via_clearance=DRU,
        grid_resolution=0.1,
    )
    if with_pairwise:
        rules.pairwise_clearance = build_pairwise_clearance_table({"HV": 150.0, "LV": 0.0}, dru=DRU)
    grid = RoutingGrid(width=30.0, height=20.0, rules=rules, layer_stack=LayerStack.two_layer())
    start = PyPad(x=3.0, y=10.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    end = PyPad(x=27.0, y=10.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    grid.add_pad(start)
    grid.add_pad(end)
    for layer in (Layer.F_CU, Layer.B_CU):
        grid.add_pad(
            PyPad(x=15.0, y=10.0, width=6.0, height=0.6, net=2, net_name="LV", layer=layer)
        )
    nc = NetClassRouting(name="HV", trace_width=TRACE_WIDTH, clearance=DRU)
    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True, net_class_map={"HV": nc})
    pf.set_net_name_to_id({"HV": 1, "LV": 2})

    fallback = {"used": False}
    original = pf._try_python_fallback

    def _spy(*args, **kwargs):
        fallback["used"] = True
        return original(*args, **kwargs)

    pf._try_python_fallback = _spy
    route = pf.route(start, end, net_class=nc)
    return route, fallback["used"]


# The LV wall rectangle in world mm (both layers): x in [12, 18], y in [9.7, 10.3].
_LV_RECT = (12.0, 18.0, 9.7, 10.3)
_REQUIRED = IEC_150V_PD2_IIIA_MM  # 1.6 mm


@requires_cpp
def test_two_domain_board_converges_with_search_time_avoidance() -> None:
    """Headline convergence proof (#4511).

    The SAME geometry, routed by the SAME C++ search, with and without the
    pairwise domain matrix installed:

    * WITHOUT the matrix (the pre-#4511 / 2a-only state) the HV-blind search
      hugs the LV wall at the scalar floor -- an isolation violation.
    * WITH the matrix the domain-aware search detours to keep the full 1.6 mm
      creepage -- it CONVERGES to a clean route, no negotiator thrash, no
      Python fallback.
    """
    blind_route, blind_fb = _route_two_domain_board(with_pairwise=False)
    assert blind_route is not None
    assert blind_fb is False, "baseline must be a pure C++ search result"
    blind_gap = min(_seg_rect_gap(s, *_LV_RECT) for s in blind_route.segments)
    blind_edge = blind_gap - TRACE_WIDTH / 2.0
    # The HV-blind route hugs the LV wall: nowhere near the 1.6 mm requirement.
    assert blind_edge < _REQUIRED

    hv_route, hv_fb = _route_two_domain_board(with_pairwise=True)
    assert hv_route is not None, "domain-aware search failed to converge"
    assert hv_fb is False, "convergence must come from the C++ search, not fallback"
    hv_gap = min(_seg_rect_gap(s, *_LV_RECT) for s in hv_route.segments)
    hv_edge = hv_gap - TRACE_WIDTH / 2.0
    # The domain-aware route clears the full creepage requirement.
    assert hv_edge >= _REQUIRED - 1e-3, f"HV edge gap {hv_edge:.3f} < required {_REQUIRED}"


@requires_cpp
def test_route_installs_pairwise_matrix_before_search() -> None:
    """The #4511 critical gap: ``route()`` must push the matrix onto the grid.

    Phase 2a wired ``_sync_pairwise_domains_to_cpp`` only into the post-route
    ``validate_route`` site, so the domain-aware kernels silently no-op'd during
    the search.  After a route with a voltage map the live grid must report the
    matrix active; without one it stays dormant.
    """
    hv_route, _ = _route_two_domain_board(with_pairwise=True)
    assert hv_route is not None
    # Re-run to inspect the grid state directly.
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.primitives import Pad as PyPad

    rules = DesignRules(
        trace_width=TRACE_WIDTH, trace_clearance=DRU, via_clearance=DRU, grid_resolution=0.1
    )
    rules.pairwise_clearance = build_pairwise_clearance_table({"HV": 150.0, "LV": 0.0}, dru=DRU)
    grid = RoutingGrid(width=30.0, height=20.0, rules=rules, layer_stack=LayerStack.two_layer())
    start = PyPad(x=3.0, y=10.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    end = PyPad(x=27.0, y=10.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    grid.add_pad(start)
    grid.add_pad(end)
    nc = NetClassRouting(name="HV", trace_width=TRACE_WIDTH, clearance=DRU)
    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True, net_class_map={"HV": nc})
    pf.set_net_name_to_id({"HV": 1, "LV": 2})
    assert cpp_grid._impl.pairwise_active is False  # not yet synced
    pf.route(start, end, net_class=nc)
    assert cpp_grid._impl.pairwise_active is True  # route() synced it before searching


@requires_cpp
def test_no_voltage_map_route_leaves_grid_dormant() -> None:
    """Backward compat: without a voltage map the search-time path is inert."""
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.primitives import Pad as PyPad

    rules = DesignRules(
        trace_width=TRACE_WIDTH, trace_clearance=DRU, via_clearance=DRU, grid_resolution=0.1
    )
    grid = RoutingGrid(width=30.0, height=20.0, rules=rules, layer_stack=LayerStack.two_layer())
    start = PyPad(x=3.0, y=10.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    end = PyPad(x=27.0, y=10.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    grid.add_pad(start)
    grid.add_pad(end)
    nc = NetClassRouting(name="HV", trace_width=TRACE_WIDTH, clearance=DRU)
    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True, net_class_map={"HV": nc})
    pf.set_net_name_to_id({"HV": 1, "LV": 2})
    route = pf.route(start, end, net_class=nc)
    assert route is not None
    assert cpp_grid._impl.pairwise_active is False
    assert cpp_grid._impl.max_pairwise_clearance == pytest.approx(0.0)
