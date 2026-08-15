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

import math
import random

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


def _cpp_zone(min_x, min_y, max_x, max_y, net_ids, net_layers=None):
    from kicad_tools.router import router_cpp

    zone = router_cpp.AttachZone()
    zone.min_x, zone.min_y, zone.max_x, zone.max_y = min_x, min_y, max_x, max_y
    zone.net_ids = list(net_ids)
    # Issue #4507: ``net_layers`` maps a net id to the grid layer indices it has
    # PAD copper on here.  Leaving it empty is the layer-agnostic (pre-#4507)
    # verdict, which is what every pre-existing fixture in this module wants.
    if net_layers:
        zone.net_layers = {net: sorted(layers) for net, layers in net_layers.items()}
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
    # Issue #4507: the trailing slot is the per-net layer projection; ``None``
    # here because these hand-built zones record no pad layers.
    assert translated == [(0.0, -1.0, 5.0, 1.0, [HV_NET, LV_NET], None)]


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


# ---------------------------------------------------------------------------
# Phase 2 (#4507): the attach-zone waiver is LAYER-SCOPED on the C++ path too
# ---------------------------------------------------------------------------
#
# PR #4756 (issue #4699) narrowed the #4506 exemption on the PYTHON side: a
# rated footprint waives the pairwise widening only on the copper layers its
# own pads occupy, so copper merely passing *under* an SMD part is no longer
# exempted by an XY box it never touches.  That PR deliberately left the C++
# ``Grid3D`` zone halo layer-agnostic (strictly more permissive), with the
# layer-scoped Python ``route_pairwise_violation`` as the acceptance check.
#
# The residual is a search<->gate DISAGREEMENT, and disagreement is exactly the
# thrash this phase exists to remove: the C++ search proposes copper its own
# validator waives, the layer-scoped Python post-check rejects it, the router
# boosts and retries, and the net either burns its resume budget into the slow
# Python fallback or fails outright.  These tests pin the mirror.

# A rated part whose pads sit on B.Cu only -> its waiver must not reach F.Cu.
_B_CU_ONLY = frozenset({("AC_LINE", frozenset({"B.Cu"})), ("GND", frozenset({"B.Cu"}))})
_LAYER_INDICES = {"F.Cu": 0, "B.Cu": 1}


@requires_cpp
def test_attach_zone_waiver_is_layer_scoped() -> None:
    """A zone waives only on layers where BOTH nets have pad copper."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones(
        [_cpp_zone(0.0, 0.0, 20.0, 20.0, [HV_NET, LV_NET], {HV_NET: [1], LV_NET: [1]})]
    )
    # Layer 1 (B.Cu) -- the part's own pad layer -- still waives.
    assert grid.attach_zone_exempts(5.0, 5.0, HV_NET, LV_NET, 1) is True
    # Layer 0 (F.Cu) -- copper merely passing over the pad field -- does not.
    assert grid.attach_zone_exempts(5.0, 5.0, HV_NET, LV_NET, 0) is False
    # No layer context (via-vs-via) keeps the layer-agnostic verdict, which is
    # also the pre-#4507 answer the defaulted argument preserves.
    assert grid.attach_zone_exempts(5.0, 5.0, HV_NET, LV_NET, -1) is True
    assert grid.attach_zone_exempts(5.0, 5.0, HV_NET, LV_NET) is True


@requires_cpp
def test_attach_zone_without_layer_data_stays_layer_agnostic() -> None:
    """Backward compat: an empty ``net_layers`` map waives on every layer."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones([_cpp_zone(0.0, 0.0, 20.0, 20.0, [HV_NET, LV_NET])])
    for layer in (-1, 0, 1):
        assert grid.attach_zone_exempts(5.0, 5.0, HV_NET, LV_NET, layer) is True


@requires_cpp
def test_layer_scoped_zone_still_requires_both_nets_to_reach_the_layer() -> None:
    """One net short of the layer is enough to withhold the waiver."""
    grid = _cpp_grid()
    _install_domains(grid)
    # HV reaches both layers (through-hole-ish), LV only B.Cu.
    grid.set_attach_zones(
        [_cpp_zone(0.0, 0.0, 20.0, 20.0, [HV_NET, LV_NET], {HV_NET: [0, 1], LV_NET: [1]})]
    )
    assert grid.attach_zone_exempts(5.0, 5.0, HV_NET, LV_NET, 1) is True
    assert grid.attach_zone_exempts(5.0, 5.0, HV_NET, LV_NET, 0) is False


@requires_cpp
def test_through_hole_zone_net_is_unrestricted_on_every_layer() -> None:
    """A net ABSENT from ``net_layers`` is unrestricted (``*.Cu`` barrels)."""
    grid = _cpp_grid()
    _install_domains(grid)
    # Only LV is layer-restricted; HV is a through-hole pad, so Python omits it
    # from the projection and the C++ side must read that as "any layer".
    grid.set_attach_zones([_cpp_zone(0.0, 0.0, 20.0, 20.0, [HV_NET, LV_NET], {LV_NET: [0, 1]})])
    assert grid.attach_zone_exempts(5.0, 5.0, HV_NET, LV_NET, 0) is True
    assert grid.attach_zone_exempts(5.0, 5.0, HV_NET, LV_NET, 1) is True


def test_attach_zones_to_net_ids_projects_pad_layers() -> None:
    """The Python->C++ translation carries the #4699 per-net pad layers."""
    zone = AttachZone(0.0, 0.0, 5.0, 5.0, frozenset({"AC_LINE", "GND"}), _B_CU_ONLY)
    (translated,) = attach_zones_to_net_ids((zone,), NET_NAMES, _LAYER_INDICES)
    assert translated[:5] == (0.0, 0.0, 5.0, 5.0, [HV_NET, LV_NET])
    assert translated[5] == {HV_NET: frozenset({1}), LV_NET: frozenset({1})}

    # No layer map supplied -> layer-agnostic (the pre-#4507 verdict).
    (agnostic,) = attach_zones_to_net_ids((zone,), NET_NAMES)
    assert agnostic[5] is None


def test_attach_zones_to_net_ids_drops_through_hole_nets_from_projection() -> None:
    """``*.Cu`` pad copper is omitted so the barrel keeps waiving everywhere."""
    zone = AttachZone(
        0.0,
        0.0,
        5.0,
        5.0,
        frozenset({"AC_LINE", "GND"}),
        frozenset({("AC_LINE", frozenset({"*.Cu"})), ("GND", frozenset({"F.Cu"}))}),
    )
    (translated,) = attach_zones_to_net_ids((zone,), NET_NAMES, _LAYER_INDICES)
    assert translated[5] == {LV_NET: frozenset({0})}


def test_lattice_and_cpp_projections_share_one_implementation() -> None:
    """The lattice and C++ zone projections cannot drift (#4507).

    ``lattice.pairwise._project_zone_layers`` now delegates to the shared
    ``pairwise_clearance.project_zone_layers`` helper, so a single edit governs
    what every engine -- and the #4588 gate -- considers layer-covered.
    """
    from kicad_tools.router.lattice.pairwise import _project_zone_layers
    from kicad_tools.router.pairwise_clearance import project_zone_layers

    zone = AttachZone(0.0, 0.0, 5.0, 5.0, frozenset({"AC_LINE", "GND"}), _B_CU_ONLY)
    ids_by_key = {"AC_LINE": [HV_NET], "GND": [LV_NET]}
    assert _project_zone_layers(zone, ids_by_key, _LAYER_INDICES) == project_zone_layers(
        zone, ids_by_key, _LAYER_INDICES
    )


@requires_cpp
def test_validate_route_layer_scoped_zone_agrees_with_python_gate() -> None:
    """``validate_route`` and ``AttachZone.exempts`` waive on the same layers."""
    zone_py = AttachZone(0.0, -1.0, 5.0, 1.0, frozenset({"AC_LINE", "GND"}), _B_CU_ONLY)
    (translated,) = attach_zones_to_net_ids((zone_py,), NET_NAMES, _LAYER_INDICES)
    cpp_zone = _cpp_zone(*translated[:5], translated[5])

    for layer_idx, layer_name, waived in ((1, "B.Cu", True), (0, "F.Cu", False)):
        grid = _cpp_grid()
        _install_domains(grid)
        grid.set_attach_zones([cpp_zone])
        # A pair 0.2 mm apart -- at the DRU floor, far below the 1.6 mm HV
        # requirement, so only the zone can make it legal.
        grid.add_stored_segment(0.0, 0.4, 5.0, 0.4, TRACE_WIDTH, layer_idx, LV_NET)
        candidate = [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET, layer=layer_idx)]
        assert _validate(grid, candidate).valid is waived
        # The Python acceptance check reaches the identical verdict.
        assert zone_py.exempts(2.5, 0.2, "AC_LINE", "GND", layer_name) is waived


@requires_cpp
def test_pad_branch_keys_the_waiver_on_the_pads_own_layer() -> None:
    """Only the through-hole sub-case makes the pad branch's key choice visible.

    ``validate_route``'s segment-vs-pad loop is **not** layer-blind: ~40 lines
    above the zone consult it already drops mismatched layers (``grid.cpp:768``,
    ``Skip pads on different layers``: ``if (pad.layer_idx != -1 &&
    pad.layer_idx != seg.layer) continue;``).  Only two shapes survive that
    filter and reach the consult:

    * a **same-layer SMD pad**, where ``pad.layer_idx == seg.layer`` -- the two
      candidate keys are the same value there, so cases (a) and (b) pin that the
      pad branch consults the zone at all and honours its layer scoping, but
      they cannot distinguish the keys; and
    * a **through-hole pad** (``layer_idx == -1``), where the pad's own layer
      keeps the consult layer-agnostic because the barrel really does span every
      copper layer.  Case (c) is the ONLY case that changes verdict if the key
      is switched to ``seg.layer``, so it alone pins the design decision.

    A cross-layer seg-vs-SMD-pad pair can never reach the consult -- the filter
    above drops it first -- so a case built on that shape passes with this fix
    fully reverted, and with no zone installed at all.  An earlier revision of
    this test used exactly that shape for case (a); it is deliberately gone.
    """
    zone_ids = [HV_NET, LV_NET]
    # LV pad copper on B.Cu (index 1) only.
    scoping = {HV_NET: [1], LV_NET: [1]}

    def _pad_case(pad_layer: int, seg_layer: int, zone_layers=None) -> bool:
        """Validate one HV segment against one LV pad; ``None`` installs no zone."""
        grid = _cpp_grid()
        _install_domains(grid)
        if zone_layers is not None:
            grid.set_attach_zones([_cpp_zone(0.0, -1.0, 5.0, 1.0, zone_ids, zone_layers)])
        grid.add_pad(2.5, 0.4, 0.2, 0.2, LV_NET, pad_layer, 0, DRU, False)
        return bool(
            _validate(grid, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET, layer=seg_layer)]).valid
        )

    # (a) Same-layer SMD pad on a layer the zone covers -> waived.  The two
    #     controls are what keep this non-vacuous: with no zone the identical
    #     geometry is rejected, so the waiver is what flips the verdict, and a
    #     zone scoped to the other layer does not waive it.
    assert _pad_case(pad_layer=1, seg_layer=1, zone_layers=scoping) is True
    assert _pad_case(pad_layer=1, seg_layer=1, zone_layers=None) is False
    assert _pad_case(pad_layer=1, seg_layer=1, zone_layers={HV_NET: [0], LV_NET: [0]}) is False

    # (b) Same-layer SMD pad on a layer the zone does NOT cover -> full
    #     widening.  This fails if the consult is layer-agnostic (the pre-#4507
    #     shape, which waives here), so it pins layer scoping reaching the pad
    #     branch at all -- but not the key choice, since the keys are equal.
    assert _pad_case(pad_layer=0, seg_layer=0, zone_layers=scoping) is False

    # (c) Through-hole pad (``layer_idx == -1``) against an F.Cu candidate: the
    #     only case where the two candidate keys differ, so the only one that
    #     pins the decision.  Keying on ``seg.layer`` (0 = F.Cu, a layer the
    #     zone does not cover) would deny the waiver; the pad's own -1 keeps the
    #     layer-agnostic verdict the barrel deserves.
    barrel = _cpp_grid()
    _install_domains(barrel)
    barrel.set_attach_zones([_cpp_zone(0.0, -1.0, 5.0, 1.0, zone_ids, scoping)])
    barrel.add_pad(2.5, 0.4, 0.2, 0.2, LV_NET, -1, 0, DRU, False)
    # The gap point is the pad-centre/closest-approach midpoint, (2.5, 0.2).
    # The two candidate keys genuinely disagree there ...
    assert barrel.attach_zone_exempts(2.5, 0.2, HV_NET, LV_NET, 0) is False
    assert barrel.attach_zone_exempts(2.5, 0.2, HV_NET, LV_NET, -1) is True
    # ... and the pad branch takes the pad's own layer, so the pair is waived.
    assert _validate(barrel, [_cpp_segment(0.0, 0.0, 5.0, 0.0, HV_NET, layer=0)]).valid


@requires_cpp
def test_search_attach_zone_waiver_is_layer_scoped() -> None:
    """The search-time annulus honours the same layer scoping as the validator."""
    grid = _cpp_grid()
    _install_domains(grid)
    grid.set_attach_zones(
        [_cpp_zone(0.0, 0.0, 20.0, 20.0, [HV_NET, LV_NET], {HV_NET: [1], LV_NET: [1]})]
    )
    pf = _pathfinder(grid)
    grid.mark_blocked(100, 108, 0, LV_NET, False, False)
    grid.mark_blocked(100, 108, 1, LV_NET, False, False)
    # B.Cu (the rated part's pad layer): the pair may neck down -> not blocked.
    assert pf.cross_domain_trace_blocked(100, 100, 1, HV_NET, SCALAR_RADIUS) is False
    # F.Cu: copper merely crossing the pad field gets no waiver -> blocked.
    assert pf.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS) is True


@requires_cpp
def test_backend_projects_zone_pad_layers_into_cpp_grid() -> None:
    """``_sync_pairwise_domains_to_cpp`` carries the layer scoping across."""
    py_grid, rules = _backend_grid_and_rules()
    rules.pairwise_clearance = _table()
    cpp_grid = CppGrid.from_routing_grid(py_grid)
    backend = CppPathfinder(cpp_grid, rules, diagonal_routing=True)
    backend.set_net_name_to_id(dict(NET_NAMES))
    backend.set_attach_zones(
        (AttachZone(0.0, -1.0, 5.0, 1.0, frozenset({"AC_LINE", "GND"}), _B_CU_ONLY),)
    )

    # The grid's own index map is the authority for name -> index.
    assert backend._grid_layer_indices() == {"F.Cu": 0, "B.Cu": 1}

    backend._sync_pairwise_domains_to_cpp()
    _, _, cpp_zones = backend._pairwise_cpp_payload
    assert [dict(z.net_layers) for z in cpp_zones] == [{HV_NET: [1], LV_NET: [1]}]
    assert cpp_grid._impl.attach_zone_exempts(2.5, 0.0, HV_NET, LV_NET, 0) is False
    assert cpp_grid._impl.attach_zone_exempts(2.5, 0.0, HV_NET, LV_NET, 1) is True


# ---------------------------------------------------------------------------
# Headline (#4507): layer scoping is what lets this board converge
# ---------------------------------------------------------------------------

# The convergence fixture's own net names (plain ``HV`` / ``LV``, no leading
# slash) scoped to the rated part's B.Cu pad copper.
_HV_LV_B_CU_ONLY = frozenset({("HV", frozenset({"B.Cu"})), ("LV", frozenset({"B.Cu"}))})


def _route_through_a_rated_gap(layer_scoped: bool):
    """Route one HV net whose only cheap path threads a rated part's pad field.

    An LV wall spans the full board height on F.Cu with a single 3 mm gap, and
    a rated footprint's attach zone straddles that gap -- but the part's pads
    are on **B.Cu**, so the waiver does not licence F.Cu copper necking through
    the gap.  The legal answer is to dive to B.Cu (where the waiver does
    apply) and cross there.

    ``layer_scoped=False`` reconstructs the pre-#4507 C++ shape exactly: the
    zone crosses the Python/C++ boundary layer-AGNOSTIC (no layer map) while
    the Python acceptance check stays layer-scoped.

    Returns ``(route, gate_violation, layers_used, used_python_fallback)``.
    """
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.pairwise_clearance import route_pairwise_violation
    from kicad_tools.router.primitives import Pad as PyPad
    from kicad_tools.router.primitives import Route as PyRoute

    zone = AttachZone(13.0, 7.0, 17.0, 13.0, frozenset({"HV", "LV"}), _HV_LV_B_CU_ONLY)
    rules = DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DRU,
        via_diameter=0.6,
        via_clearance=DRU,
        grid_resolution=0.1,
    )
    rules.pairwise_clearance = build_pairwise_clearance_table({"HV": 150.0, "LV": 0.0}, dru=DRU)
    grid = RoutingGrid(width=30.0, height=20.0, rules=rules, layer_stack=LayerStack.two_layer())
    start = PyPad(x=3.0, y=10.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    end = PyPad(x=27.0, y=10.0, width=0.6, height=0.6, net=1, net_name="HV", layer=Layer.F_CU)
    grid.add_pad(start)
    grid.add_pad(end)
    for y1, y2 in ((0.2, 8.5), (11.5, 19.8)):
        wall = PyRoute(
            net=2,
            net_name="LV",
            segments=[Segment(15.0, y1, 15.0, y2, TRACE_WIDTH, Layer.F_CU, net=2, net_name="LV")],
        )
        grid.mark_route(wall)
        grid.routes.append(wall)

    nc = NetClassRouting(name="HV", trace_width=TRACE_WIDTH, clearance=DRU)
    cpp_grid = CppGrid.from_routing_grid(grid)
    pf = CppPathfinder(cpp_grid, rules, diagonal_routing=True, net_class_map={"HV": nc})
    pf.set_net_name_to_id({"HV": 1, "LV": 2})
    pf.set_attach_zones((zone,))
    if not layer_scoped:
        pf._grid_layer_indices = lambda: None

    fallback = {"used": False}
    original = pf._try_python_fallback

    def _spy(*args, **kwargs):
        fallback["used"] = True
        return original(*args, **kwargs)

    pf._try_python_fallback = _spy
    route = pf.route(start, end, net_class=nc)
    violation = None
    layers: set[str] = set()
    if route is not None:
        violation = route_pairwise_violation(
            route,
            1,
            grid.routes,
            rules.pairwise_clearance,
            id_to_name={1: "HV", 2: "LV"},
            attach_zones=(zone,),
        )
        layers = {seg.layer.name for seg in route.segments}
    return route, violation, layers, fallback["used"]


@requires_cpp
def test_rated_gap_board_converges_on_the_licensed_layer() -> None:
    """Headline (#4507): the search crosses on the layer the rated part licences.

    An LV wall with a single 3 mm gap on F.Cu, and a rated footprint whose
    attach zone straddles the gap but whose pads are on B.Cu.  The only
    gate-clean answer is to dive to B.Cu and cross there; necking through the
    F.Cu gap is a pairwise violation the layer-scoped acceptance check
    (#4699) rejects.

    Evolution of this fixture, so a future reader can tell an improvement from
    a regression:

    * pre-#4507, layer-AGNOSTIC: the C++ search kept proposing the F.Cu gap
      the layer-scoped gate rejected, burned its resume budget and dropped
      into the pure-Python fallback, which then failed outright.
    * #4507: the fallback became pairwise-aware and layer-scoped, so the
      agnostic baseline was *rescued* by it -- slowly, but gate-clean.
    * #4848: with the soft avoidance gradient priced off the NEAREST band
      cell instead of the first one in scan order, the nudge away from the
      under-clearance F.Cu gap is strong enough that even the layer-AGNOSTIC
      configuration converges inside the C++ search -- no thrash, no fallback.
      That is the routing-quality payoff #4848 was filed for, so this test now
      pins the *outcome* (converged, gate-clean, on the licensed layer, in the
      C++ search) for both configurations rather than the old contrast.
    """
    for layer_scoped in (True, False):
        route, violation, layers, fallback = _route_through_a_rated_gap(layer_scoped=layer_scoped)
        label = "layer-scoped" if layer_scoped else "layer-agnostic"
        assert route is not None, f"{label} search failed to converge"
        assert violation is None, f"{label} route violates the gate: {violation}"
        assert fallback is False, f"{label} convergence must come from the C++ search"
        # It found the legal answer: cross on the layer the rated part licences.
        assert "B_CU" in layers, f"{label} route never reached the licensed layer"


# ---------------------------------------------------------------------------
# Per-net-class search extents (Issue #4793): Python <-> C++ parity
# ---------------------------------------------------------------------------
#
# The widened radius is measured from the ROUTED net's copper half-extent.  The
# C++ kernels read ``search_trace_half_width_mm_`` / ``search_via_half_diam_mm_``
# (pushed once per net by ``cpp_backend`` via ``set_search_pair_widths``); the
# Python fallback resolves the same extent from the net's ``NetClassRouting``.
# Before #4793 the Python side used the GLOBAL ``rules`` widths, so on a board
# with a wide class the two backends disagreed over a 4-cell-deep band -- the
# fallback under-blocked, proposed a path, and the post-route gate rejected it.
# This fixture sweeps the whole band and pins cell-for-cell agreement.

_WIDE_TRACE_WIDTH = 1.0  # vs the 0.2 mm global default
_WIDE_VIA_SIZE = 1.2  # vs the 0.6 mm global default


def _py_router_with_foreign_cells(
    cells,
    net_class_map=None,
    *,
    origin: tuple[int, int] = (100, 100),
    with_table: bool = True,
):
    """A Python Router on a 20x20 board with foreign LV cells at ``cells``.

    ``cells`` are ``(dy, dx)`` offsets from ``origin`` (the probe point), or
    ``(dy, dx, net)`` to place copper of some other net (``HV_TAP_NET`` for
    same-domain copper, ``0`` for the pour / unconnected convention -- neither
    is a gradient source).  Cell-exact mirror of the C++ fixtures'
    ``Grid3D.mark_blocked`` (there is no public single-cell setter on
    ``RoutingGrid``; the search kernels read these arrays directly).
    """
    from kicad_tools.router.layers import LayerStack
    from kicad_tools.router.pathfinder import Router

    rules = DesignRules(
        trace_width=TRACE_WIDTH,
        trace_clearance=DRU,
        via_diameter=0.6,
        via_clearance=DRU,
        grid_resolution=0.1,
    )
    if with_table:
        rules.pairwise_clearance = _table()
    grid = RoutingGrid(width=20.0, height=20.0, rules=rules, layer_stack=LayerStack.two_layer())
    for cell in cells:
        dy, dx = cell[0], cell[1]
        cell_net = cell[2] if len(cell) > 2 else LV_NET
        grid._blocked[0, origin[1] + dy, origin[0] + dx] = True
        grid._net[0, origin[1] + dy, origin[0] + dx] = cell_net
    router = Router(grid, rules, diagonal_routing=True, net_class_map=net_class_map)
    router.set_net_name_to_id(dict(NET_NAMES))
    return router


def _py_router_with_foreign_cell(dy: int, net_class_map=None, *, with_table: bool = True):
    """Single-cell form of :func:`_py_router_with_foreign_cells` (``dx = 0``)."""
    return _py_router_with_foreign_cells(((dy, 0),), net_class_map, with_table=with_table)


def _cpp_pathfinder_with_foreign_cells(
    cells,
    half_trace_mm: float = TRACE_WIDTH / 2.0,
    half_via_mm: float = 0.3,
    *,
    origin: tuple[int, int] = (100, 100),
):
    grid = _cpp_grid()
    _install_domains(grid)
    from kicad_tools.router import router_cpp

    pf = router_cpp.Pathfinder(grid, _cpp_rules(), True)
    pf.set_search_pair_widths(half_trace_mm, half_via_mm)
    for cell in cells:
        dy, dx = cell[0], cell[1]
        cell_net = cell[2] if len(cell) > 2 else LV_NET
        grid.mark_blocked(origin[0] + dx, origin[1] + dy, 0, cell_net, False, False)
    return pf


def _cpp_pathfinder_with_foreign_cell(dy: int, half_trace_mm: float, half_via_mm: float):
    return _cpp_pathfinder_with_foreign_cells(((dy, 0),), half_trace_mm, half_via_mm)


# Trace: global half 0.10 -> r 17 cells; class half 0.50 -> r 21 cells.
# Via:   global half 0.30 -> r 19 cells; class half 0.60 -> r 22 cells.
# The sweep straddles every one of those thresholds.
_PARITY_DISTANCES = tuple(range(15, 25))


@requires_cpp
@pytest.mark.parametrize(
    ("net_class_map", "half_trace_mm", "half_via_mm"),
    [
        (None, TRACE_WIDTH / 2.0, 0.3),
        (
            {"/AC_LINE": NetClassRouting(name="HV", trace_width=_WIDE_TRACE_WIDTH, clearance=DRU)},
            _WIDE_TRACE_WIDTH / 2.0,
            0.3,
        ),
    ],
    ids=["global-width", "wide-class-width"],
)
def test_trace_annulus_class_width_parity(net_class_map, half_trace_mm, half_via_mm) -> None:
    """Python fallback and C++ agree cell-for-cell on the class-width band."""
    verdicts = []
    for dy in _PARITY_DISTANCES:
        py = _py_router_with_foreign_cell(dy, net_class_map)
        cpp = _cpp_pathfinder_with_foreign_cell(dy, half_trace_mm, half_via_mm)
        py_blocked = py._cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS)
        cpp_blocked = cpp.cross_domain_trace_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS)
        assert py_blocked == cpp_blocked, f"backends disagree at dy={dy}"
        verdicts.append(py_blocked)
    # The sweep must actually straddle the boundary (otherwise agreement is
    # vacuous -- e.g. every probe outside both radii).
    assert True in verdicts and False in verdicts


# At the 1.6 mm requirement and 0.1 mm resolution the global-width hard radius
# is 17 cells and the soft band is ceil(1.6 * 0.5 / 0.1) = 8 cells beyond it.
_GRADIENT_HARD_R = 17
_GRADIENT_BAND = 8


@requires_cpp
def test_avoidance_gradient_parity_across_the_band() -> None:
    """Python and C++ price the soft gradient identically, cell for cell.

    Issue #4507: #4511 Scope 2 armed only the C++ search with the soft
    avoidance gradient; the Python fallback (#4791 shipped its hard blocking
    only) charged nothing, so the two engines hugged the hard limit
    differently on the same HV board.  This sweeps the whole band --
    inside the hard radius, every priced cell, and past the band edge -- and
    pins agreement on the *value*, not just the sign.
    """
    costs = []
    for dy in range(_GRADIENT_HARD_R - 3, _GRADIENT_HARD_R + _GRADIENT_BAND + 4):
        py = _py_router_with_foreign_cell(dy)
        cpp = _cpp_pathfinder_with_foreign_cell(dy, TRACE_WIDTH / 2.0, 0.3)
        py_cost = py._pairwise_avoidance_cost(100, 100, 0, HV_NET)
        cpp_cost = cpp.pairwise_avoidance_cost(100, 100, 0, HV_NET)
        assert py_cost == pytest.approx(cpp_cost, abs=1e-6), f"backends disagree at dy={dy}"
        costs.append(py_cost)
    # Not vacuous: the sweep must straddle both edges of the band.
    assert any(cost > 0.0 for cost in costs)
    assert costs[0] == 0.0 and costs[-1] == 0.0


@requires_cpp
def test_avoidance_gradient_parity_off_axis() -> None:
    """Parity holds for diagonal offsets (the sqrt of a non-square distance)."""
    for dy, dx in ((15, 15), (-20, 3), (0, 22), (-14, -14), (20, -20), (-19, 6)):
        py = _py_router_with_foreign_cells(((dy, dx),))
        cpp = _cpp_pathfinder_with_foreign_cells(((dy, dx),))
        assert py._pairwise_avoidance_cost(100, 100, 0, HV_NET) == pytest.approx(
            cpp.pairwise_avoidance_cost(100, 100, 0, HV_NET), abs=1e-6
        ), f"backends disagree at (dy={dy}, dx={dx})"


@requires_cpp
def test_avoidance_gradient_parity_picks_the_same_cell() -> None:
    """With several band cells, both engines price the SAME (nearest) one.

    Issue #4848: up to build version 19 both kernels broke at the first
    qualifying cell in ROW-MAJOR scan order, so the topmost band cell set the
    price rather than the closest one -- ``((18, 0), (-24, 0))`` priced
    ``1/8`` off the far cell instead of ``7/8`` off the near one.  The
    expected magnitudes below are the nearest-cell prices, so this fails
    loudly (not just on a mutual disagreement) if either engine drifts back
    to scan order.
    """
    halo_r = _GRADIENT_HARD_R + _GRADIENT_BAND  # 25
    for cells, expected in (
        (((18, 0), (-24, 0)), 7.0 / 8.0),  # near cell below wins
        (((-18, 0), (24, 0)), 7.0 / 8.0),  # ... and above
        (((-24, -3), (18, 2), (19, -1)), (halo_r - math.sqrt(328)) / 8.0),
        # Only (-19, 0) is inside the halo at all: the two diagonals sit at
        # sqrt(800) and sqrt(882), both beyond the band edge.
        (((-20, -20), (-19, 0), (21, 21)), 6.0 / 8.0),
    ):
        py = _py_router_with_foreign_cells(cells)
        cpp = _cpp_pathfinder_with_foreign_cells(cells)
        py_cost = py._pairwise_avoidance_cost(100, 100, 0, HV_NET)
        cpp_cost = cpp.pairwise_avoidance_cost(100, 100, 0, HV_NET)
        assert py_cost == pytest.approx(cpp_cost, abs=1e-6), f"backends disagree for {cells}"
        assert py_cost == pytest.approx(expected, abs=1e-6), f"not the nearest cell for {cells}"


@requires_cpp
def test_avoidance_gradient_parity_is_order_free() -> None:
    """Adding FARTHER band copper never changes either engine's price (#4848).

    The scan-order contract this replaced had no such invariant: which cell
    won depended on where in the window it sat, so a distant blob could
    override a near one.  Nearest-in-band pricing makes the gradient a pure
    function of the binding distance, and the two engines agree on it cell
    for cell.
    """
    nearest = ((19, 0),)
    decoys = (
        ((-25, 0),),  # exactly on the halo circle (the old zero-frac trap)
        ((-24, 0), (-23, 3)),
        ((0, 22), (22, 0), (-20, -12)),
        ((-24, -3), (24, 4), (-21, 8), (20, -14)),
    )
    baseline_py = _py_router_with_foreign_cells(nearest)._pairwise_avoidance_cost(
        100, 100, 0, HV_NET
    )
    assert baseline_py == pytest.approx(6.0 / 8.0, abs=1e-6)
    for decoy in decoys:
        cells = nearest + decoy
        py = _py_router_with_foreign_cells(cells)
        cpp = _cpp_pathfinder_with_foreign_cells(cells)
        py_cost = py._pairwise_avoidance_cost(100, 100, 0, HV_NET)
        cpp_cost = cpp.pairwise_avoidance_cost(100, 100, 0, HV_NET)
        assert py_cost == pytest.approx(cpp_cost, abs=1e-6), f"backends disagree for {cells}"
        assert py_cost == pytest.approx(baseline_py, abs=1e-6), f"decoy changed the price: {decoy}"


@requires_cpp
def test_avoidance_gradient_parity_on_circle_cell_never_swallows_the_price() -> None:
    """A cell exactly ON the halo circle prices 0 and never hides a nearer one.

    History: version 19's C++ break was two-level with a CONDITIONAL outer
    ``if (cost > 0.0f) break;``, precisely so a qualifying cell at exactly
    ``halo_r`` (``frac == 0``) would not swallow the price -- the row-major
    scan's very first cell (``dy = -halo_r, dx = 0``) sits exactly on the
    circle, so ANY foreign copper whose topmost band cell lands in the
    candidate's own column tripped it, and a Python mirror that returned
    unconditionally at the first qualifying cell returned 0.0 where C++
    returned a full-magnitude cost (#4849).

    Nearest-in-band pricing (#4848) makes that corner structural rather than
    conditional: an on-circle cell is the FARTHEST cell in the band, so it can
    only ever win when nothing nearer qualifies (and then 0.0 is the right
    answer).  The magnitudes below are unchanged by #4848 -- they were already
    the nearest cell's price -- so this stays the direct regression net for
    both engines.
    """
    halo_r = _GRADIENT_HARD_R + _GRADIENT_BAND  # 25
    for cells, expected in (
        (((-halo_r, 0), (18, 0)), 0.875),
        (((-halo_r, 0), (-18, 0)), 0.875),
        (((-halo_r, 0), (0, 20)), 0.625),
        (((-20, -15), (18, 0)), 0.875),  # the (20, 15, 25) Pythagorean triple
        (((-halo_r, 0),), 0.0),  # alone on the circle -> genuinely free
    ):
        py = _py_router_with_foreign_cells(cells)
        cpp = _cpp_pathfinder_with_foreign_cells(cells)
        py_cost = py._pairwise_avoidance_cost(100, 100, 0, HV_NET)
        cpp_cost = cpp.pairwise_avoidance_cost(100, 100, 0, HV_NET)
        assert py_cost == pytest.approx(cpp_cost, abs=1e-6), f"backends disagree for {cells}"
        # Non-vacuous: the on-circle cell must NOT have swallowed the price
        # (the pre-#4849 Python mirror returned exactly 0.0 for every case
        # here, so an equality-only assertion could pass on a mutual zero).
        assert py_cost == pytest.approx(expected, abs=1e-6), f"wrong magnitude for {cells}"


@requires_cpp
def test_avoidance_gradient_parity_contiguous_bar_slides_nearer() -> None:
    """A solid foreign bar prices STRICTLY higher as it slides one row nearer.

    Issue #4848's routing-quality payoff, in miniature and in both engines.
    Under the version-19 scan-order contract these two positions priced
    *identically* (0.0836 -- both were charged off the same topmost band row,
    ``dy = -24, dx = -4``), so the gradient carried no information about how
    close the copper actually was.  Nearest-in-band pricing charges the bar's
    closest row: 21 cells out, then 20.
    """
    halo_r = _GRADIENT_HARD_R + _GRADIENT_BAND  # 25
    costs = []
    for top in (-halo_r, -halo_r + 1):
        cells = tuple((dy, dx) for dy in range(top, top + 5) for dx in range(-4, 5))
        py = _py_router_with_foreign_cells(cells)
        cpp = _cpp_pathfinder_with_foreign_cells(cells)
        py_cost = py._pairwise_avoidance_cost(100, 100, 0, HV_NET)
        cpp_cost = cpp.pairwise_avoidance_cost(100, 100, 0, HV_NET)
        assert py_cost == pytest.approx(cpp_cost, abs=1e-6), f"backends disagree for bar at {top}"
        costs.append(py_cost)
    # Bar top -25: nearest row is dy = -21 (dx = 0) -> frac = (25 - 21) / 8.
    # Bar top -24: nearest row is dy = -20 (dx = 0) -> frac = (25 - 20) / 8.
    assert costs[0] == pytest.approx(4.0 / 8.0, abs=1e-6)
    assert costs[1] == pytest.approx(5.0 / 8.0, abs=1e-6)
    assert costs[1] > costs[0]


@requires_cpp
def test_avoidance_gradient_parity_at_the_board_edge() -> None:
    """A clipped scan window keeps parity (out-of-bounds is EMPTY, not blocked).

    The Python window is clipped to the array bounds while the C++ scan skips
    out-of-bounds cells; both must therefore price from the same in-bounds
    cell.  Probed hard against x = 0 where the clip removes leading columns
    of every row -- the case that would break a naive flat-index mirror.
    """
    values = []
    for gx in (0, 1, 5, 20):
        origin = (gx, 100)
        py = _py_router_with_foreign_cells(((0, 20),), origin=origin)
        cpp = _cpp_pathfinder_with_foreign_cells(((0, 20),), origin=origin)
        py_cost = py._pairwise_avoidance_cost(gx, 100, 0, HV_NET)
        cpp_cost = cpp.pairwise_avoidance_cost(gx, 100, 0, HV_NET)
        assert py_cost == pytest.approx(cpp_cost, abs=1e-6), f"backends disagree at gx={gx}"
        values.append(py_cost)
    # The same 20-cell separation is priced identically however much of the
    # window the board edge clipped away.
    assert values[0] > 0.0
    assert all(value == pytest.approx(values[0]) for value in values)


@requires_cpp
def test_avoidance_gradient_parity_wide_class_width() -> None:
    """The band is measured from the routed net's class width in BOTH engines."""
    wide = {"/AC_LINE": NetClassRouting(name="HV", trace_width=_WIDE_TRACE_WIDTH, clearance=DRU)}
    costs = []
    for dy in range(_GRADIENT_HARD_R, _GRADIENT_HARD_R + _GRADIENT_BAND + 9):
        py = _py_router_with_foreign_cell(dy, wide)
        cpp = _cpp_pathfinder_with_foreign_cell(dy, _WIDE_TRACE_WIDTH / 2.0, 0.3)
        py_cost = py._pairwise_avoidance_cost(100, 100, 0, HV_NET)
        cpp_cost = cpp.pairwise_avoidance_cost(100, 100, 0, HV_NET)
        assert py_cost == pytest.approx(cpp_cost, abs=1e-6), f"backends disagree at dy={dy}"
        costs.append(py_cost)
    # The wide class moves the hard radius out to 21: cell 18 is now INSIDE
    # it (free) where the global width priced it.
    assert costs[1] == 0.0
    assert any(cost > 0.0 for cost in costs)


@requires_cpp
@pytest.mark.parametrize("seed", [17, 4848, 90210])
def test_avoidance_gradient_parity_randomized_band_population(seed: int) -> None:
    """Seeded fuzz: the two engines agree on every randomly populated band.

    Issue #4848's acceptance criterion.  The hand-written fixtures above pin
    named corners; this sweeps arbitrary copper layouts -- cells inside the
    hard radius, across the whole band, past the halo, plus same-domain and
    net-0 copper that must be ignored -- and asserts the Python mirror and the
    C++ kernel return the SAME value, and that the value is exactly the
    nearest qualifying cell's linear decay (computed independently here, so
    the sweep does not merely pin two implementations of the same bug).

    Origins are drawn near the board edge as well as in open field, so the
    clipped-window path (Python clips the array slice; C++ skips
    out-of-bounds offsets) is fuzzed too.
    """
    rng = random.Random(seed)
    halo_r = _GRADIENT_HARD_R + _GRADIENT_BAND  # 25
    nonzero_seen = 0
    for _ in range(60):
        # Mix open-field origins with ones hard against the board edges.
        origin = (
            rng.choice([rng.randint(0, 3), rng.randint(30, 170), rng.randint(196, 199)]),
            rng.choice([rng.randint(0, 3), rng.randint(30, 170), rng.randint(196, 199)]),
        )
        cells = []
        for _ in range(rng.randint(1, 14)):
            dy = rng.randint(-halo_r - 3, halo_r + 3)
            dx = rng.randint(-halo_r - 3, halo_r + 3)
            gx, gy = origin[0] + dx, origin[1] + dy
            if not (0 <= gx < 200 and 0 <= gy < 200):
                continue  # off-board: neither engine can see it
            # Weighted towards LV (the only gradient source) but with plenty
            # of same-domain / pour copper to exercise the widening filter.
            cell_net = rng.choice([LV_NET, LV_NET, LV_NET, HV_TAP_NET, 0])
            cells.append((dy, dx, cell_net))
        if not cells:
            continue
        cells = tuple(cells)

        py = _py_router_with_foreign_cells(cells, origin=origin)
        cpp = _cpp_pathfinder_with_foreign_cells(cells, origin=origin)
        py_cost = py._pairwise_avoidance_cost(origin[0], origin[1], 0, HV_NET)
        cpp_cost = cpp.pairwise_avoidance_cost(origin[0], origin[1], 0, HV_NET)
        assert py_cost == pytest.approx(cpp_cost, abs=1e-6), (
            f"backends disagree at origin={origin} cells={cells}"
        )

        # Independent oracle: nearest qualifying cell, linear decay.  A later
        # write wins when two draws land on the same coordinate, exactly as it
        # does in both grids.
        by_coord: dict[tuple[int, int], int] = {}
        for dy, dx, cell_net in cells:
            by_coord[(dy, dx)] = cell_net
        qualifying = [
            dy * dy + dx * dx
            for (dy, dx), cell_net in by_coord.items()
            if cell_net == LV_NET and _GRADIENT_HARD_R**2 < dy * dy + dx * dx <= halo_r**2
        ]
        expected = 0.0
        if qualifying:
            expected = (
                py.rules.cost_straight * (halo_r - math.sqrt(min(qualifying))) / _GRADIENT_BAND
            )
            if expected > 0.0:
                nonzero_seen += 1
        assert py_cost == pytest.approx(expected, abs=1e-6), (
            f"not the nearest cell at origin={origin} cells={cells}"
        )
    # Non-vacuous: the sweep must actually have priced something.
    assert nonzero_seen >= 5, f"only {nonzero_seen} priced boards -- sweep is near-vacuous"


@requires_cpp
def test_avoidance_gradient_dormant_parity() -> None:
    """No matrix installed -> both engines price nothing."""
    py = _py_router_with_foreign_cell(20, with_table=False)
    grid = _cpp_grid()  # domains never installed
    from kicad_tools.router import router_cpp

    cpp = router_cpp.Pathfinder(grid, _cpp_rules(), True)
    cpp.set_search_pair_widths(TRACE_WIDTH / 2.0, 0.3)
    grid.mark_blocked(100, 120, 0, LV_NET, False, False)
    assert py._pairwise_avoidance_cost(100, 100, 0, HV_NET) == 0.0
    assert cpp.pairwise_avoidance_cost(100, 100, 0, HV_NET) == pytest.approx(0.0)


@requires_cpp
def test_via_annulus_class_size_parity() -> None:
    """Same parity for the via analogue (``via_size`` vs ``via_diameter``).

    The C++ via kernel derives its own scalar radius and scans every layer;
    the probes here sit far outside any scalar disc and the foreign cell is on
    a single layer, so the two shapes are directly comparable.
    """
    wide = {
        "/AC_LINE": NetClassRouting(
            name="HV", trace_width=TRACE_WIDTH, clearance=DRU, via_size=_WIDE_VIA_SIZE
        )
    }
    verdicts = []
    for dy in _PARITY_DISTANCES:
        py = _py_router_with_foreign_cell(dy, wide)
        cpp = _cpp_pathfinder_with_foreign_cell(dy, TRACE_WIDTH / 2.0, _WIDE_VIA_SIZE / 2.0)
        py_blocked = py._cross_domain_via_blocked(100, 100, 0, HV_NET, SCALAR_RADIUS)
        cpp_blocked = cpp.cross_domain_via_blocked(100, 100, HV_NET)
        assert py_blocked == cpp_blocked, f"backends disagree at dy={dy}"
        verdicts.append(py_blocked)
    assert True in verdicts and False in verdicts
