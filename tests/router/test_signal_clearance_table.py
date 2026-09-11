"""Tests for the non-voltage signal-clearance builder (#5021).

Generalises the HV pairwise-clearance resolver (#4431,
``tests/router/test_pairwise_clearance.py``) past voltage-derived requirements
to a flat signal-integrity spacing rule such as ST AN4488 Section 8.4.2's SDRAM
clock-to-signal guideline (three 0.18 mm trace widths -> 0.54 mm edge-to-edge).

Covers the concrete board07 finding that motivated this issue:

* a staged B.Cu SDCLK route cleared foreign TRACKS by the full 0.54 mm
  requirement, but passed a foreign (BA1) through-VIA at the ordinary 0.15 mm
  fabrication clearance -- the via's copper was never widened;
* the requirement must hold regardless of which net is "moving" and which is
  "foreign" at check time (the reciprocal-guard problem: routing BA1 *after*
  SDCLK must catch the same shortfall that routing SDCLK after BA1 catches);
* no package-escape exemption is applied automatically -- a pad within the
  same footprint field as the clock's own escape is still numerically
  checked unless the caller supplies an explicit, measured ``AttachZone``.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.layers import Layer
from kicad_tools.router.pairwise_clearance import (
    AttachZone,
    PadGeometry,
    build_signal_clearance_table,
    find_pairwise_violations,
    route_pairwise_violation,
)
from kicad_tools.router.primitives import Route, Segment, Via

# ST AN4488 Section 8.4.2: three 0.18 mm trace widths, edge-to-edge.
CLOCK_REQUIRED_MM = 0.54
DRU = 0.15


def _seg(x1, y1, x2, y2, net, name, layer=Layer.B_CU, width=0.18) -> Segment:
    return Segment(x1, y1, x2, y2, width, layer, net=net, net_name=name)


def _via(x, y, net, name, diameter=0.5, drill=0.25, layers=(Layer.F_CU, Layer.B_CU)) -> Via:
    return Via(x=x, y=y, drill=drill, diameter=diameter, layers=layers, net=net, net_name=name)


def _pad(net_name: str, x: float, y: float, half: float = 0.15, layers=("F.Cu",)) -> PadGeometry:
    import shapely

    box = shapely.box(x - half, y - half, x + half, y + half)
    return PadGeometry(net_name=net_name, layers=frozenset(layers), polygon=box)


# ---------------------------------------------------------------------------
# Resolver: flat mm requirement, order-independent, DRU-floored.
# ---------------------------------------------------------------------------


def test_widened_pair_gets_the_flat_requirement() -> None:
    table = build_signal_clearance_table(["SDCLK"], ["BA0", "BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    assert table.required_clearance("SDCLK", "BA1") == pytest.approx(CLOCK_REQUIRED_MM)
    assert table.required_clearance("SDCLK", "BA0") == pytest.approx(CLOCK_REQUIRED_MM)


def test_resolver_is_order_independent() -> None:
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    assert table.required_clearance("SDCLK", "BA1") == table.required_clearance("BA1", "SDCLK")


def test_unwidened_pair_falls_back_to_dru_floor() -> None:
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    # BA0 was never listed in ``other_nets`` for this table -- no widening.
    assert table.required_clearance("BA1", "BA0") == pytest.approx(DRU)


def test_same_net_returns_dru() -> None:
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    assert table.required_clearance("SDCLK", "SDCLK") == pytest.approx(DRU)


def test_widened_net_never_widens_against_itself_even_if_listed_in_other_nets() -> None:
    # A caller passing an overlapping widened/other set must not self-widen.
    table = build_signal_clearance_table(["SDCLK"], ["SDCLK", "BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    assert ("SDCLK", "SDCLK") not in table.required_by_pair
    assert table.required_clearance("SDCLK", "SDCLK") == pytest.approx(DRU)


def test_multiple_widened_nets_each_get_the_requirement() -> None:
    table = build_signal_clearance_table(["SDCLK", "SDNCS"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    assert table.required_clearance("SDCLK", "BA1") == pytest.approx(CLOCK_REQUIRED_MM)
    assert table.required_clearance("SDNCS", "BA1") == pytest.approx(CLOCK_REQUIRED_MM)
    # The two widened nets are not "other" to each other unless explicitly listed.
    assert table.required_clearance("SDCLK", "SDNCS") == pytest.approx(DRU)


def test_normalises_leading_slash() -> None:
    table = build_signal_clearance_table(["/SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    assert table.required_clearance("SDCLK", "/BA1") == pytest.approx(CLOCK_REQUIRED_MM)


def test_dru_floor_wins_when_larger_than_the_flat_requirement() -> None:
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=0.8)
    assert table.required_clearance("SDCLK", "BA1") == pytest.approx(0.8)


def test_net_voltages_is_empty_for_a_flat_table() -> None:
    """This builder has no voltage concept; provenance stays empty, not faked."""
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    assert dict(table.net_voltages) == {}


# ---------------------------------------------------------------------------
# The concrete #5021 defect: via participation + reciprocal route order.
#
# SDCLK clears the foreign BA1 TRACK by the full 0.54 mm requirement, but a
# BA1 THROUGH-VIA sits only 0.15 mm (the fab DRU) from the same SDCLK track.
# The via's copper must be widened exactly like the track's -- and the
# shortfall must be caught whichever net is treated as "moving" at check
# time, or the router can pass it going one direction and only discover it
# when the other net is routed (the original board07 failure mode).
# ---------------------------------------------------------------------------


def _sdclk_route() -> Route:
    # A single B.Cu segment, 0.18 mm wide (AN4488's bus trace width).
    return Route(net=1, net_name="SDCLK", segments=[_seg(0.0, 0.0, 10.0, 0.0, 1, "SDCLK")])


def _ba1_track_route() -> Route:
    # Centreline 0.72 mm away: 0.72 - 0.09 - 0.09 = 0.54 mm edge gap -> clears.
    return Route(net=2, net_name="BA1", segments=[_seg(0.0, 0.72, 10.0, 0.72, 2, "BA1")])


def _ba1_via_route() -> Route:
    # Via centre 0.49 mm from the track centreline, 0.5 mm via diameter
    # (0.25 mm radius): edge gap = 0.49 - 0.09 (seg half-width) - 0.25
    # (via radius) = 0.15 mm -- exactly the ordinary fabrication DRU used in
    # this table, matching the report ("passed an existing BA1 through-via
    # at the ordinary 0.15mm clearance").  0.15 mm is well short of the
    # 0.54 mm AN4488 signal-clearance requirement.
    return Route(net=2, net_name="BA1", vias=[_via(5.0, 0.49, 2, "BA1")])


def test_clock_track_clears_foreign_track_but_not_foreign_via() -> None:
    """Reproduces the exact board07 finding at the module level.

    The SDCLK-vs-BA1-track gap satisfies the 0.54 mm requirement; the
    SDCLK-vs-BA1-via gap does not.  A rule that only widened trace-vs-trace
    (the original one-off census' bug) would silently pass this board.
    """
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    sdclk = _sdclk_route()

    track_violation = route_pairwise_violation(sdclk, 1, [_ba1_track_route()], table)
    assert track_violation is None, "the foreign TRACK already clears the requirement"

    via_violation = route_pairwise_violation(sdclk, 1, [_ba1_via_route()], table)
    assert via_violation is not None, "the foreign VIA must be widened identically to a track"
    assert {via_violation.net_a, via_violation.net_b} == {"SDCLK", "BA1"}
    assert via_violation.required_mm == pytest.approx(CLOCK_REQUIRED_MM)


def test_reversed_route_order_catches_the_same_via_shortfall() -> None:
    """The reciprocal-clock-guard case: BA1 routed (checked) AFTER SDCLK.

    In the board07 incident the BA1 via already existed and the CLOCK was
    checked against it; the fix also had to catch the case where BA1 (or its
    via) is what gets routed/re-checked *after* SDCLK is already down --
    otherwise a routing-order-dependent tool could re-introduce exactly this
    defect from the other direction.  Because ``required_clearance`` is a
    symmetric lookup and the via/segment geometry is symmetric, the SAME
    table must report the SAME shortfall with the roles swapped.
    """
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    ba1_via_route = _ba1_via_route()
    sdclk = _sdclk_route()

    # BA1's via is "moving"; SDCLK's track is "foreign".
    reciprocal = route_pairwise_violation(ba1_via_route, 2, [sdclk], table)
    assert reciprocal is not None
    assert {reciprocal.net_a, reciprocal.net_b} == {"SDCLK", "BA1"}

    # SDCLK's track is "moving"; BA1's via is "foreign" (the forward direction
    # from the previous test) -- must agree on both the fact of a violation
    # and its magnitude.
    forward = route_pairwise_violation(sdclk, 1, [ba1_via_route], table)
    assert forward is not None
    assert forward.actual_mm == pytest.approx(reciprocal.actual_mm)
    assert forward.required_mm == pytest.approx(reciprocal.required_mm)


def test_board_scan_is_independent_of_route_list_order() -> None:
    """Consistent PLANNING and post-route validation (#5021 acceptance).

    A whole-board audit (:func:`find_pairwise_violations`) must report the
    identical violation whether SDCLK or BA1 appears first in the route
    list -- routing order (or scan order) cannot change the effective
    outcome of the rule.
    """
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    sdclk = _sdclk_route()
    ba1_via_route = _ba1_via_route()

    forward_scan = find_pairwise_violations([sdclk, ba1_via_route], table)
    reversed_scan = find_pairwise_violations([ba1_via_route, sdclk], table)

    assert len(forward_scan) == 1
    assert len(reversed_scan) == 1
    assert {forward_scan[0].net_a, forward_scan[0].net_b} == {
        reversed_scan[0].net_a,
        reversed_scan[0].net_b,
    }
    assert forward_scan[0].actual_mm == pytest.approx(reversed_scan[0].actual_mm)
    assert forward_scan[0].required_mm == pytest.approx(reversed_scan[0].required_mm)


def test_clearing_the_via_gap_restores_a_clean_scan() -> None:
    """Moving the via out to the 0.54 mm requirement clears both directions.

    Confirms the check is a genuine pass/fail gate, not merely one that
    always fires -- matching the incident's resolution ("rerouting the
    clock restored BA1/BA0 routability").
    """
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    sdclk = _sdclk_route()
    # 0.72 mm from the track centreline: 0.72 - 0.09 (seg half-width) - 0.25
    # (via radius) = 0.38 mm... use the track's own clearing geometry (0.72 mm
    # gives the same edge gap as the track case: 0.72-0.09-0.25=0.38 which is
    # still short). Use 0.88 mm so the edge gap is exactly the requirement:
    # 0.88 - 0.09 - 0.25 = 0.54 mm.
    repaired_via = Route(net=2, net_name="BA1", vias=[_via(5.0, 0.88, 2, "BA1")])

    assert route_pairwise_violation(sdclk, 1, [repaired_via], table) is None
    assert find_pairwise_violations([sdclk, repaired_via], table) == []
    assert find_pairwise_violations([repaired_via, sdclk], table) == []


# ---------------------------------------------------------------------------
# No global package-escape exemption (#5021 acceptance).
# ---------------------------------------------------------------------------


def test_pad_within_a_footprint_field_is_not_exempted_by_default() -> None:
    """A clock-adjacent pad is checked exactly like any other foreign copper.

    Unlike the HV epic's rated-package necking waiver (``AttachZone``, #4506),
    this rule ships with NO automatic same-footprint or front-layer
    exemption.  Without an explicit, measured ``AttachZone`` the pad
    proximity is flagged -- the caller must justify any waiver with real
    geometry, never accept a blanket rule.
    """
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    sdclk = _sdclk_route()
    # 0.3 mm edge gap: above DRU (0.15 mm) but well below the 0.54 mm
    # requirement -- exactly the "package escape" geometry a same-footprint
    # exemption would otherwise silently wave through.
    pad = _pad("BA1", 5.0, 0.54, layers=("F.Cu", "B.Cu"))
    violation = route_pairwise_violation(sdclk, 1, [], table, foreign_pads=(pad,))
    assert violation is not None
    assert {violation.net_a, violation.net_b} == {"SDCLK", "BA1"}


def test_pad_is_waived_only_by_an_explicit_measured_attach_zone() -> None:
    """The ONLY way to waive package-escape copper is an explicit AttachZone.

    This mirrors the HV epic's mechanism (reused verbatim, not reinvented)
    but the point of #5021 is that nothing constructs this zone
    automatically for the clock-spacing rule -- a caller must build it from
    measured pad geometry for the SPECIFIC package under review.
    """
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    sdclk = _sdclk_route()
    pad = _pad("BA1", 5.0, 0.54, layers=("F.Cu", "B.Cu"))

    # Without an explicit zone: flagged (the default from the test above).
    assert route_pairwise_violation(sdclk, 1, [], table, foreign_pads=(pad,)) is not None

    # With an explicit, caller-supplied, measured zone covering both nets:
    # waived -- but this is an opt-in the caller must justify, not a rule
    # default.
    zone = AttachZone(-1.0, -1.0, 11.0, 1.0, frozenset({"SDCLK", "BA1"}))
    assert (
        route_pairwise_violation(sdclk, 1, [], table, attach_zones=(zone,), foreign_pads=(pad,))
        is None
    )


def test_front_layer_pad_gets_no_automatic_pass() -> None:
    """A pad on the front layer is checked identically to an inner-layer pad.

    Guards against reintroducing a "front layer is safe" shortcut: the only
    thing that exempts a layer is the pad's OWN copper not reaching it
    (``PadGeometry.layers``), never its identity as F.Cu.
    """
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], CLOCK_REQUIRED_MM, dru=DRU)
    sdclk = _sdclk_route()  # routed on B.Cu
    front_pad = _pad("BA1", 5.0, 0.54, layers=("F.Cu", "B.Cu"))
    violation = route_pairwise_violation(sdclk, 1, [], table, foreign_pads=(front_pad,))
    assert violation is not None


@pytest.mark.parametrize("field", ["required_mm", "dru"])
@pytest.mark.parametrize("invalid", [float("nan"), float("inf"), float("-inf"), -1.0, True, False])
@pytest.mark.parametrize("empty", [False, True])
def test_builder_rejects_invalid_distances_even_without_pairs(field, invalid, empty):
    values = {"required_mm": CLOCK_REQUIRED_MM, "dru": DRU}
    values[field] = invalid
    with pytest.raises(ValueError, match=field):
        build_signal_clearance_table([] if empty else ["SDCLK"], ["BA1"], **values)


@pytest.mark.parametrize(
    "required,dru,expected", [(0, 0.15, 0.15), (0.05, 0.15, 0.15), (0, 0, 0), (0.54, 0, 0.54)]
)
def test_zero_and_subfloor_distances_remain_valid(required, dru, expected):
    table = build_signal_clearance_table(["SDCLK"], ["BA1"], required, dru=dru)
    assert table.required_clearance("SDCLK", "BA1") == expected
    assert table.required_clearance("BA1", "SDCLK") == expected
