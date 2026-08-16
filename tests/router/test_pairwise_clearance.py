"""Tests for the HV-isolation pairwise clearance resolver + validator (#4431).

Phase 1 of the "scalar clearance -> pairwise clearance" epic (mirrors #2556).
Covers:

* the :class:`PairwiseClearanceTable` resolver (``max(dru, creepage@|ΔV|)``,
  same-domain -> DRU, cross-domain -> IEC, absent-net -> DRU floor);
* cross-consumer agreement -- the router and placement produce byte-identical
  matrices from the same voltage map (both go through
  ``build_required_by_domain_pair``);
* the Python post-route validator (segment-pair + route-level) flagging an
  HV↔LV pair below its pairwise requirement and accepting an HV↔HV pair at DRU;
* backward compatibility (``DesignRules.pairwise_clearance`` defaults to
  ``None`` and the scalar path is untouched);
* the fail-loud out-of-table contract and the DRU floor clamp.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from kicad_tools.creepage.engine import resolve_hv_nets
from kicad_tools.creepage.standards import StandardLookupError
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairwise_clearance import (
    AttachZone,
    PairwiseClearanceTable,
    PairwisePathChecker,
    build_attach_zones,
    build_pairwise_clearance_table,
    find_pairwise_violations,
    load_signed_voltage_map,
    path_pairwise_violation,
    route_pairwise_violation,
    segment_pair_violation,
)
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

# A 150 V mains net vs ground: IEC 60664-1, PD2, material group IIIa -> 1.6 mm.
IEC_150V_PD2_IIIA_MM = 1.6
DRU = 0.2


def _table(voltages: dict[str, float], dru: float = DRU) -> PairwiseClearanceTable:
    return build_pairwise_clearance_table(voltages, dru=dru)


# ---------------------------------------------------------------------------
# Resolver
# ---------------------------------------------------------------------------


def test_resolver_cross_domain_returns_iec_creepage() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    assert table.required_clearance("/AC_LINE", "/GND") == pytest.approx(IEC_150V_PD2_IIIA_MM)


def test_resolver_is_order_independent() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    assert table.required_clearance("/GND", "/AC_LINE") == table.required_clearance(
        "/AC_LINE", "/GND"
    )


def test_resolver_same_domain_returns_dru() -> None:
    # Two nets at the same potential (same cluster) get only the DRU floor.
    table = _table({"/AC_LINE": 150.0, "/AC_LINE_TAP": 150.0})
    assert table.required_clearance("/AC_LINE", "/AC_LINE_TAP") == pytest.approx(DRU)


def test_resolver_same_net_returns_dru() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    assert table.required_clearance("/AC_LINE", "/AC_LINE") == pytest.approx(DRU)


def test_resolver_absent_net_treated_as_dru_floor() -> None:
    # A net not in the voltage map is LV (no widening) -> DRU floor.
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    assert table.required_clearance("/AC_LINE", "/UNMAPPED_SIG") == pytest.approx(DRU)


def test_resolver_normalises_leading_slash() -> None:
    # Map keyed with a leading slash; query without one (and vice versa).
    table = _table({"/AC_LINE": 150.0, "GND": 0.0})
    assert table.required_clearance("AC_LINE", "/GND") == pytest.approx(IEC_150V_PD2_IIIA_MM)


def test_resolver_max_of_dru_and_lookup() -> None:
    # A tiny sub-DRU |ΔV| lookup would still be floored at DRU; and a real HV
    # pair exceeds DRU.  Verify the ``max`` explicitly with a large DRU.
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0}, dru=2.0)
    # required creepage (1.6) < dru (2.0) -> floored up to dru.
    assert table.required_clearance("/AC_LINE", "/GND") == pytest.approx(2.0)


# ---------------------------------------------------------------------------
# Cross-consumer agreement with placement (#4373)
# ---------------------------------------------------------------------------


def test_router_and_placement_produce_identical_matrices() -> None:
    """The router and placement derive the SAME domain-pair matrix (#4431 AC).

    Both consumers go through ``build_required_by_domain_pair`` -- there is no
    forked lookup.  With disjoint refs per mapped net, placement's derived
    ``domain_voltages`` coincide with the router's per-net voltage map, so the
    full pipelines produce byte-identical matrices.
    """
    from kicad_tools.placement.cost import Net
    from kicad_tools.placement.hv_domains import (
        build_required_by_domain_pair,
        derive_ref_domains_from_voltage_map,
    )

    voltage_map = {"AC_LINE": 150.0, "GND": 0.0, "V12": 12.0}
    # Disjoint refs: each ref touches exactly one mapped net, so its domain is
    # that net and placement's domain_voltages == the router's per-net map.
    nets = [
        Net(name="AC_LINE", pins=[("R1", "1"), ("R1", "2")]),
        Net(name="GND", pins=[("R2", "1"), ("R2", "2")]),
        Net(name="V12", pins=[("R3", "1"), ("R3", "2")]),
    ]

    _ref_domains, domain_voltages = derive_ref_domains_from_voltage_map(nets, voltage_map)
    placement_matrix = build_required_by_domain_pair(domain_voltages)

    router_table = build_pairwise_clearance_table(voltage_map, dru=DRU)

    assert dict(router_table.required_by_pair) == dict(placement_matrix)
    # And the router's builder is literally the placement builder over the same
    # domain_voltages input (no fork).
    assert dict(router_table.required_by_pair) == build_required_by_domain_pair(domain_voltages)


# ---------------------------------------------------------------------------
# Fail-loud + edge cases
# ---------------------------------------------------------------------------


def test_out_of_table_delta_v_raises() -> None:
    # A |ΔV| above the highest tabulated IEC row must raise, never extrapolate.
    with pytest.raises(StandardLookupError):
        build_pairwise_clearance_table({"/HVDC": 100_000.0, "/GND": 0.0}, dru=DRU)


def test_below_hv_threshold_pair_absent_from_matrix() -> None:
    # A 12 V vs 0 V pair is below the 30 V default threshold -> no widening.
    table = _table({"/V12": 12.0, "/GND": 0.0})
    assert ("GND", "V12") not in table.required_by_pair
    assert table.required_clearance("/V12", "/GND") == pytest.approx(DRU)


# ---------------------------------------------------------------------------
# Signed potentials (#4867)
# ---------------------------------------------------------------------------
#
# ``build_pairwise_clearance_table`` used to normalise its input with
# ``abs(float(v))`` BEFORE differencing, so a +150 V net and a -150 V net
# differenced to ``|150| - |150| = 0 V``, fell below ``hv_threshold`` and
# dropped out of the matrix entirely -- invisible to search-time avoidance AND
# to the post-route audit, which can only report on pairs its own table holds.
# The census (``creepage/engine.py``) reads the same sidecar signed, so router
# and gate disagreed by construction on every bipolar (bank) topology.


def test_bipolar_pair_differences_to_the_full_span() -> None:
    """+150 V vs -150 V is a 300 V pair (3.20 mm), not 0 V (absent)."""
    table = _table({"/BANK_POS": 150.0, "/BANK_NEG": -150.0})
    assert ("BANK_NEG", "BANK_POS") in table.required_by_pair
    assert table.required_by_pair[("BANK_NEG", "BANK_POS")] == pytest.approx(3.2)
    assert table.required_clearance("/BANK_POS", "/BANK_NEG") == pytest.approx(3.2)


def test_bipolar_pair_is_wider_than_either_net_against_ground() -> None:
    """The bank span binds harder than either leg's own potential."""
    table = _table({"/BANK_POS": 150.0, "/BANK_NEG": -150.0, "/GND": 0.0})
    span = table.required_clearance("/BANK_POS", "/BANK_NEG")
    to_gnd = table.required_clearance("/BANK_POS", "/GND")
    assert span > to_gnd
    assert to_gnd == pytest.approx(IEC_150V_PD2_IIIA_MM)
    # The magnitude reading collapsed POS<->NEG to nothing; the signed reading
    # makes it the *widest* pair on the board.
    assert table.max_required_clearance() == pytest.approx(span)


def test_asymmetric_bipolar_pair_uses_the_signed_difference() -> None:
    """-90 V vs +150 V is 240 V (2.50 mm), not the 60 V of the magnitudes."""
    table = _table({"/SCAP_NEG": -90.0, "/SRC_POS": 150.0})
    # 240 V under IEC 60664-1 / PD2 / IIIa.
    assert table.required_by_pair[("SCAP_NEG", "SRC_POS")] == pytest.approx(2.5)
    # The magnitude reading would have differenced |150| - |90| = 60 V, which is
    # a strictly smaller (and therefore unsafe) requirement.
    magnitude_only = _table({"/SCAP_NEG": 90.0, "/SRC_POS": 150.0})
    assert magnitude_only.required_by_pair[("SCAP_NEG", "SRC_POS")] < 2.5


def test_signed_pair_below_hv_threshold_stays_out_of_the_matrix() -> None:
    """No over-correction: a signed span under the threshold is still absent.

    +10 V vs -10 V is a genuine 20 V span -- signed differencing must see it as
    20 V (not 0 V), but 20 V is below the 30 V default threshold, so the pair
    keeps only the DRU floor exactly as an all-positive 20 V span would.
    """
    table = _table({"/AUX_POS": 10.0, "/AUX_NEG": -10.0})
    assert table.required_by_pair == {}
    assert table.required_clearance("/AUX_POS", "/AUX_NEG") == pytest.approx(DRU)


def test_all_positive_map_is_unchanged_by_signed_differencing() -> None:
    """Regression guard: the common non-bipolar case must not move (#4867 AC).

    ``abs()`` was the identity on a non-negative map, so dropping it has to be
    a strict no-op there -- asserted against the explicitly magnitude-normalised
    input the pre-#4867 code built internally.
    """
    voltages = {"/AC_LINE": 150.0, "/GND": 0.0, "/V12": 12.0, "/HVDC": 400.0}
    signed = build_pairwise_clearance_table(voltages, dru=DRU)
    magnitudes = build_pairwise_clearance_table({k: abs(v) for k, v in voltages.items()}, dru=DRU)
    assert dict(signed.required_by_pair) == dict(magnitudes.required_by_pair)
    assert dict(signed.net_voltages) == dict(magnitudes.net_voltages)


def test_all_negative_map_matches_its_mirror_image() -> None:
    """A uniformly negative map differences identically to its positive mirror."""
    negative = build_pairwise_clearance_table({"/A": -150.0, "/B": -0.0}, dru=DRU)
    positive = build_pairwise_clearance_table({"/A": 150.0, "/B": 0.0}, dru=DRU)
    assert dict(negative.required_by_pair) == dict(positive.required_by_pair)


def test_signed_potentials_are_retained_for_provenance() -> None:
    """``net_voltages`` keeps the sign so diagnostics can explain the span."""
    table = _table({"/BANK_POS": 150.0, "/BANK_NEG": -150.0})
    assert dict(table.net_voltages) == {"BANK_POS": 150.0, "BANK_NEG": -150.0}


# ---------------------------------------------------------------------------
# The router's signed sidecar loader (#4867)
# ---------------------------------------------------------------------------


def _write_map(tmp_path, payload) -> str:
    import json

    p = tmp_path / "vmap.json"
    p.write_text(json.dumps(payload))
    return str(p)


def test_load_signed_voltage_map_preserves_sign(tmp_path) -> None:
    """The router's loader must NOT collapse to a magnitude (placement's does)."""
    from kicad_tools.placement.hv_domains import load_voltage_map

    path = _write_map(tmp_path, {"/BANK_POS": 150, "/BANK_NEG": -150, "/GND": 0})
    assert load_signed_voltage_map(path) == {
        "/BANK_POS": 150.0,
        "/BANK_NEG": -150.0,
        "/GND": 0.0,
    }
    # Placement's magnitude-only loader is the thing that used to be reused here.
    assert load_voltage_map(path)["/BANK_NEG"] == 150.0


def test_load_signed_voltage_map_skips_reserved_metadata_keys(tmp_path) -> None:
    """Same parse contract as ``kct creepage``: ``_``-prefixed keys are not nets."""
    path = _write_map(
        tmp_path,
        {"_comment": "signed volts about GND", "_edge_voltage": 0, "/HV": -150},
    )
    assert load_signed_voltage_map(path) == {"/HV": -150.0}


def test_load_signed_voltage_map_collapses_a_range_to_its_signed_extreme(
    tmp_path,
) -> None:
    """A swinging node (#4411) keeps the SIGN of its worst-case endpoint."""
    path = _write_map(tmp_path, {"/SW": {"min": -170, "max": 90}, "/GND": 0})
    assert load_signed_voltage_map(path) == {"/SW": -170.0, "/GND": 0.0}


def test_load_signed_voltage_map_matches_placement_on_a_positive_map(
    tmp_path,
) -> None:
    """All-non-negative maps read identically through either loader (#4867 AC)."""
    from kicad_tools.placement.hv_domains import load_voltage_map

    path = _write_map(tmp_path, {"/AC_LINE": 150, "/GND": 0, "/V12": 12})
    assert load_signed_voltage_map(path) == load_voltage_map(path)


def test_load_signed_voltage_map_rejects_a_non_object(tmp_path) -> None:
    """The CLI catches ``ValueError``; a JSON array must not escape as TypeError."""
    path = _write_map(tmp_path, [1, 2, 3])
    with pytest.raises(ValueError):
        load_signed_voltage_map(path)


# ---------------------------------------------------------------------------
# Parity with the creepage census over a bipolar map (#4867 AC)
# ---------------------------------------------------------------------------


def _bipolar_board_source() -> str:
    """Three single-pad footprints: +150 V, -150 V and 0 V, well separated."""
    pads = [("BANK_POS", 1, 105.0), ("BANK_NEG", 2, 115.0), ("GND", 3, 125.0)]
    fps = "".join(
        f"""  (footprint "test:pad" (layer "F.Cu") (at {x} 110)
    (pad "1" smd rect (at 0 0) (size 2 2) (layers "F.Cu")
      (net {num} "{name}"))
  )
"""
        for name, num, x in pads
    )
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test_4867")
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "BANK_POS")
  (net 2 "BANK_NEG")
  (net 3 "GND")
  (gr_line (start 100 100) (end 130 100) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 130 100) (end 130 120) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 130 120) (end 100 120) (layer "Edge.Cuts") (width 0.1))
  (gr_line (start 100 120) (end 100 100) (layer "Edge.Cuts") (width 0.1))
{fps})
"""


def test_router_table_matches_the_creepage_census_on_a_bipolar_map(tmp_path) -> None:
    """The router and ``kct creepage`` must require the SAME mm on every pair.

    This is the #4867 acceptance criterion asserted as parity over a bipolar
    fixture rather than a spot check: the census differences the sidecar's
    signed endpoints, so any magnitude collapse on the router side shows up
    here as a missing pair (POS<->NEG) or a smaller requirement.
    """
    from kicad_tools._shapely import has_shapely

    if not has_shapely():
        pytest.skip("creepage census requires shapely")

    from kicad_tools.creepage.engine import (
        compute_creepage_census,
        voltage_map_from_dict,
    )
    from kicad_tools.creepage.standards import get_standard
    from kicad_tools.schema.pcb import PCB

    payload = {"BANK_POS": 150, "BANK_NEG": -150, "GND": 0}
    board = tmp_path / "bipolar.kicad_pcb"
    board.write_text(_bipolar_board_source())
    pcb = PCB.load(board)

    intervals = voltage_map_from_dict(payload)[0]
    hv_nets = resolve_hv_nets(pcb, "HV", None, voltage_map=intervals, census_threshold=30.0)
    report = compute_creepage_census(
        pcb,
        hv_nets,
        voltage_map=intervals,
        standard_obj=get_standard("iec60664"),
        pollution_degree=2,
        material_group="IIIa",
    )
    census_required = {
        tuple(sorted((p.net_a, p.net_b))): p.required_creepage_mm
        for p in report.pairs
        if p.kind == "conductor"
    }
    assert census_required, "census produced no conductor pairs"

    router_map = load_signed_voltage_map(_write_map(tmp_path, payload))
    table = build_pairwise_clearance_table(router_map, dru=DRU)

    for pair, census_mm in census_required.items():
        assert census_mm is not None
        if census_mm <= DRU:
            continue  # below the DRU floor -- the router keeps the scalar path
        assert table.required_by_pair.get(pair) == pytest.approx(census_mm), (
            f"router/census disagree on {pair}: {table.required_by_pair.get(pair)} vs {census_mm}"
        )
    # And specifically the pair the magnitude collapse used to erase.
    assert ("BANK_NEG", "BANK_POS") in census_required
    assert ("BANK_NEG", "BANK_POS") in table.required_by_pair


# ---------------------------------------------------------------------------
# Segment-pair validator
# ---------------------------------------------------------------------------


def _seg(x1, y1, x2, y2, net, name, layer=Layer.F_CU, width=0.2) -> Segment:
    return Segment(x1, y1, x2, y2, width, layer, net=net, net_name=name)


def test_segment_pair_flags_hv_lv_below_requirement() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    # Two parallel same-layer segments 0.4 mm centre-to-centre -> 0.2 mm edge
    # gap, far below the 1.6 mm requirement.
    hv = _seg(0.0, 0.0, 5.0, 0.0, net=1, name="/AC_LINE")
    lv = _seg(0.0, 0.4, 5.0, 0.4, net=2, name="/GND")
    v = segment_pair_violation(hv, lv, table)
    assert v is not None
    assert v.required_mm == pytest.approx(IEC_150V_PD2_IIIA_MM)
    assert v.actual_mm == pytest.approx(0.2, abs=1e-6)


def test_segment_pair_accepts_hv_lv_meeting_requirement() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    # 2.0 mm centre gap -> 1.8 mm edge gap >= 1.6 mm requirement -> OK.
    hv = _seg(0.0, 0.0, 5.0, 0.0, net=1, name="/AC_LINE")
    lv = _seg(0.0, 2.0, 5.0, 2.0, net=2, name="/GND")
    assert segment_pair_violation(hv, lv, table) is None


def test_segment_pair_accepts_same_domain_at_dru() -> None:
    table = _table({"/AC_LINE": 150.0, "/AC_LINE_TAP": 150.0})
    # Same potential (own cluster): 0.4 mm centre / 0.2 mm edge is fine at DRU.
    a = _seg(0.0, 0.0, 5.0, 0.0, net=1, name="/AC_LINE")
    b = _seg(0.0, 0.4, 5.0, 0.4, net=2, name="/AC_LINE_TAP")
    assert segment_pair_violation(a, b, table) is None


def test_segment_pair_ignores_different_layers() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    hv = _seg(0.0, 0.0, 5.0, 0.0, net=1, name="/AC_LINE", layer=Layer.F_CU)
    lv = _seg(0.0, 0.4, 5.0, 0.4, net=2, name="/GND", layer=Layer.B_CU)
    assert segment_pair_violation(hv, lv, table) is None


# ---------------------------------------------------------------------------
# Route-level validator (the in-loop hook shape)
# ---------------------------------------------------------------------------


def test_route_pairwise_violation_flags_foreign_hv_proximity() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    moving = Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE")])
    foreign = Route(net=2, net_name="/GND", segments=[_seg(0, 0.3, 5, 0.3, 2, "/GND")])
    id_to_name = {1: "/AC_LINE", 2: "/GND"}
    v = route_pairwise_violation(moving, 1, [foreign], table, id_to_name=id_to_name)
    assert v is not None
    assert v.net_b == "/GND"


def test_route_pairwise_violation_skips_same_net() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    moving = Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE")])
    # A foreign route on the SAME net id must be skipped (same-net copper).
    same = Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0.3, 5, 0.3, 1, "/AC_LINE")])
    id_to_name = {1: "/AC_LINE"}
    assert route_pairwise_violation(moving, 1, [same], table, id_to_name=id_to_name) is None


def test_route_pairwise_violation_resolves_names_from_ids() -> None:
    # Segment/route name strings unset; names come from the id map (the live
    # in-loop condition where mid-route net-name strings are not yet populated).
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    moving = Route(net=1, net_name="", segments=[_seg(0, 0, 5, 0, 1, "")])
    foreign = Route(net=2, net_name="", segments=[_seg(0, 0.3, 5, 0.3, 2, "")])
    id_to_name = {1: "/AC_LINE", 2: "/GND"}
    v = route_pairwise_violation(moving, 1, [foreign], table, id_to_name=id_to_name)
    assert v is not None


def test_attach_zone_exempts_only_terminating_pair_inside_zone() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0, "/SENSE": 0.0})
    zone = AttachZone(0.0, -1.0, 5.0, 1.0, frozenset({"AC_LINE", "GND"}))
    moving = Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE")])
    attached = Route(net=2, net_name="/GND", segments=[_seg(0, 0.4, 5, 0.4, 2, "/GND")])

    # Rated package owns both nets: the pair may neck down to the DRU floor.
    assert route_pairwise_violation(moving, 1, [attached], table, attach_zones=(zone,)) is None

    # An unrelated LV net merely crossing the courtyard receives no waiver.
    crossing = Route(net=3, net_name="/SENSE", segments=[_seg(0, 0.3, 5, 0.3, 3, "/SENSE")])
    assert route_pairwise_violation(moving, 1, [crossing], table, attach_zones=(zone,)) is not None


def test_attach_zone_never_waives_below_dru_floor() -> None:
    """The rated-package neck may reach, but never cross, the 0.2 mm DRU."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    zone = AttachZone(0.0, -1.0, 5.0, 1.0, frozenset({"AC_LINE", "GND"}))
    moving = Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE")])
    # 0.25 mm centre distance minus two 0.1 mm radii = 0.05 mm edge gap.
    foreign = Route(net=2, net_name="/GND", segments=[_seg(0, 0.25, 5, 0.25, 2, "/GND")])

    violation = route_pairwise_violation(moving, 1, [foreign], table, dru=0.2, attach_zones=(zone,))
    assert violation is not None
    assert violation.actual_mm == pytest.approx(0.05)


def test_attach_zone_uses_closest_gap_not_long_segment_midpoint() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    zone = AttachZone(-1.0, -1.0, 1.0, 1.0, frozenset({"AC_LINE", "GND"}))

    # The foreign midpoint is inside the zone, but the entire closest overlap
    # with the short candidate is outside: this must not receive a waiver.
    outside = Route(net=1, net_name="/AC_LINE", segments=[_seg(8, 0, 9, 0, 1, "/AC_LINE")])
    long_foreign = Route(net=2, net_name="/GND", segments=[_seg(-10, 0.4, 10, 0.4, 2, "/GND")])
    assert (
        route_pairwise_violation(outside, 1, [long_foreign], table, attach_zones=(zone,))
        is not None
    )

    # Reversing the geometry proves a legitimate short in-zone neck is waived
    # even though the other segment extends far outside the zone.
    long_candidate = Route(
        net=1,
        net_name="/AC_LINE",
        segments=[_seg(-10, 0, 10, 0, 1, "/AC_LINE")],
    )
    inside = Route(net=2, net_name="/GND", segments=[_seg(-0.5, 0.4, 0.5, 0.4, 2, "/GND")])
    assert (
        route_pairwise_violation(long_candidate, 1, [inside], table, attach_zones=(zone,)) is None
    )


def test_attach_zone_does_not_exempt_same_pair_outside_zone() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    zone = AttachZone(0.0, -1.0, 5.0, 1.0, frozenset({"AC_LINE", "GND"}))
    moving = Route(net=1, net_name="/AC_LINE", segments=[_seg(10, 0, 15, 0, 1, "/AC_LINE")])
    foreign = Route(net=2, net_name="/GND", segments=[_seg(10, 0.3, 15, 0.3, 2, "/GND")])
    assert route_pairwise_violation(moving, 1, [foreign], table, attach_zones=(zone,)) is not None


def test_build_attach_zones_falls_back_to_pad_bounds_without_courtyard() -> None:
    footprint = SimpleNamespace(
        position=(10.0, 20.0),
        rotation=0.0,
        graphics=[],
        pads=[
            SimpleNamespace(position=(-0.4, 0.0), size=(0.4, 0.6), net_name="/AC_LINE"),
            SimpleNamespace(position=(0.4, 0.0), size=(0.4, 0.6), net_name="/GND"),
        ],
    )
    zones = build_attach_zones([footprint], margin=0.5)
    assert len(zones) == 1
    zone = zones[0]
    assert zone.net_names == frozenset({"AC_LINE", "GND"})
    assert zone.min_x == pytest.approx(8.9)
    assert zone.max_x == pytest.approx(11.1)
    assert zone.min_y == pytest.approx(19.2)
    assert zone.max_y == pytest.approx(20.8)


def test_build_attach_zones_rotates_pad_extents_without_courtyard() -> None:
    footprint = SimpleNamespace(
        position=(10.0, 20.0),
        rotation=90.0,
        graphics=[],
        pads=[
            SimpleNamespace(
                position=(-1.0, 0.0),
                size=(2.0, 0.4),
                rotation=90.0,
                net_name="/AC_LINE",
            ),
            SimpleNamespace(
                position=(1.0, 0.0),
                size=(2.0, 0.4),
                rotation=90.0,
                net_name="/GND",
            ),
        ],
    )

    (zone,) = build_attach_zones([footprint], margin=0.5)
    assert (zone.min_x, zone.min_y, zone.max_x, zone.max_y) == pytest.approx(
        (9.3, 17.5, 10.7, 22.5)
    )


def test_build_attach_zones_ignores_the_courtyard_and_spans_the_pads() -> None:
    """Issue #4699: the region is the PAD field, never the courtyard.

    Pre-#4699 a resolvable courtyard polygon won outright, so the exemption
    covered the part's whole body outline (here 2 x 4 mm + margin) -- copper
    merely passing under the body was waived along with the copper that
    genuinely necks down to the rated pins.  ``kct creepage`` waives no such
    thing, which is how a gate-clean board could still fail the census.  The
    zone now spans the connected pads (+ margin) only: the same 1.2 x 1.6 mm
    box the pad-bounds path above produces, courtyard notwithstanding.
    """
    footprint = SimpleNamespace(
        position=(10.0, 20.0),
        rotation=0.0,
        graphics=[
            SimpleNamespace(
                layer="F.CrtYd",
                graphic_type="rect",
                start=(-1.0, -2.0),
                end=(1.0, 2.0),
            )
        ],
        pads=[
            SimpleNamespace(position=(-0.4, 0.0), size=(0.4, 0.6), net_name="/AC_LINE"),
            SimpleNamespace(position=(0.4, 0.0), size=(0.4, 0.6), net_name="/GND"),
        ],
    )
    (zone,) = build_attach_zones([footprint], margin=0.5)
    assert (zone.min_x, zone.min_y, zone.max_x, zone.max_y) == pytest.approx(
        (8.9, 19.2, 11.1, 20.8)
    )


def test_find_pairwise_violations_board_scan() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    routes = [
        Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE")]),
        Route(net=2, net_name="/GND", segments=[_seg(0, 0.3, 5, 0.3, 2, "/GND")]),
    ]
    violations = find_pairwise_violations(routes, table)
    assert len(violations) == 1
    assert {violations[0].net_a, violations[0].net_b} == {"/AC_LINE", "/GND"}


# ---------------------------------------------------------------------------
# Issue #4588: the board scan must honour #4506 attach zones
# ---------------------------------------------------------------------------


def _hv_lv_board_routes() -> list[Route]:
    """Two parallel HV/LV segments 0.3 mm apart -- above DRU, below creepage."""
    return [
        Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE")]),
        Route(net=2, net_name="/GND", segments=[_seg(0, 0.5, 5, 0.5, 2, "/GND")]),
    ]


def test_find_pairwise_violations_without_attach_zone_reports() -> None:
    """Baseline for the exemption test: the pair IS a violation unexempted."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    violations = find_pairwise_violations(_hv_lv_board_routes(), table)
    assert len(violations) == 1
    # Edge-to-edge gap is 0.3 mm (0.5 centre - two 0.1 half-widths), which is
    # comfortably above the 0.2 mm DRU floor -- so an attach zone can waive it.
    assert violations[0].actual_mm == pytest.approx(0.3)


def test_find_pairwise_violations_honours_attach_zone_exemption() -> None:
    """A rated-footprint attach zone covering the span suppresses the finding.

    Without this (issue #4588), the post-route gate would fire on every
    deliberately-exempted domain-bridging footprint and make HV boards
    unroutable by construction -- a false fail as bad as the false pass.
    """
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    zone = AttachZone(-1.0, -1.0, 6.0, 1.5, frozenset({"AC_LINE", "GND"}))
    assert find_pairwise_violations(_hv_lv_board_routes(), table, attach_zones=(zone,)) == []


def test_find_pairwise_violations_attach_zone_outside_span_still_reports() -> None:
    """An attach zone that does not cover the gap point exempts nothing."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    far = AttachZone(50.0, 50.0, 60.0, 60.0, frozenset({"AC_LINE", "GND"}))
    assert len(find_pairwise_violations(_hv_lv_board_routes(), table, attach_zones=(far,))) == 1


def test_find_pairwise_violations_attach_zone_needs_both_nets() -> None:
    """A zone listing only one of the two nets cannot waive the pair."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    partial = AttachZone(-1.0, -1.0, 6.0, 1.5, frozenset({"AC_LINE"}))
    assert len(find_pairwise_violations(_hv_lv_board_routes(), table, attach_zones=(partial,))) == 1


def test_find_pairwise_violations_attach_zone_never_waives_below_dru() -> None:
    """Attach zones waive the HV *widening* only, never the scalar DRU floor."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    routes = [
        Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE")]),
        # 0.25 mm centre-to-centre -> 0.05 mm edge gap, below the 0.2 mm DRU.
        Route(net=2, net_name="/GND", segments=[_seg(0, 0.25, 5, 0.25, 2, "/GND")]),
    ]
    zone = AttachZone(-1.0, -1.0, 6.0, 1.5, frozenset({"AC_LINE", "GND"}))
    assert len(find_pairwise_violations(routes, table, attach_zones=(zone,))) == 1


# ---------------------------------------------------------------------------
# Issue #4699: the attach-zone waiver is layer-scoped
# ---------------------------------------------------------------------------


def _smd_zone() -> AttachZone:
    """A rated bridging footprint whose pads are SMD copper on ``F.Cu`` only."""
    return AttachZone(
        -1.0,
        -1.0,
        6.0,
        1.5,
        frozenset({"AC_LINE", "GND"}),
        frozenset(
            {("AC_LINE", frozenset({"F.Cu"})), ("GND", frozenset({"F.Cu"}))},
        ),
    )


def _inner_layer_board_routes() -> list[Route]:
    """The same HV/LV proximity as ``_hv_lv_board_routes``, but on ``In1.Cu``."""
    return [
        Route(
            net=1,
            net_name="/AC_LINE",
            segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE", layer=Layer.IN1_CU)],
        ),
        Route(
            net=2,
            net_name="/GND",
            segments=[_seg(0, 0.5, 5, 0.5, 2, "/GND", layer=Layer.IN1_CU)],
        ),
    ]


def test_attach_zone_still_waives_on_a_layer_its_pads_occupy() -> None:
    """The waiver survives where it is physically justified (pad layer)."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    assert find_pairwise_violations(_hv_lv_board_routes(), table, attach_zones=(_smd_zone(),)) == []


def test_attach_zone_does_not_waive_copper_passing_underneath() -> None:
    """Issue #4699: an XY bbox must not exempt a layer the part has no copper on.

    ``AttachZone`` was layer-agnostic, so inner-layer copper merely crossing
    *beneath* a rated SMD footprint was waived by a courtyard box it never
    touches -- while ``kct creepage`` (which has no such waiver) condemned the
    very same pair.  Two of the nine violations in the #4699 report were on
    ``In1.Cu``.
    """
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    violations = find_pairwise_violations(
        _inner_layer_board_routes(), table, attach_zones=(_smd_zone(),)
    )
    assert len(violations) == 1, violations
    assert violations[0].actual_mm == pytest.approx(0.3)


def test_through_hole_pads_still_waive_on_every_layer() -> None:
    """A ``*.Cu`` (through-hole) pad genuinely has copper on all layers."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    tht = AttachZone(
        -1.0,
        -1.0,
        6.0,
        1.5,
        frozenset({"AC_LINE", "GND"}),
        frozenset({("AC_LINE", frozenset({"*.Cu"})), ("GND", frozenset({"*.Cu"}))}),
    )
    assert find_pairwise_violations(_inner_layer_board_routes(), table, attach_zones=(tht,)) == []


def test_zone_without_recorded_layers_keeps_the_agnostic_verdict() -> None:
    """Hand-built / pre-#4699 zones (no ``net_layers``) are unrestricted."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    legacy = AttachZone(-1.0, -1.0, 6.0, 1.5, frozenset({"AC_LINE", "GND"}))
    assert (
        find_pairwise_violations(_inner_layer_board_routes(), table, attach_zones=(legacy,)) == []
    )


def test_build_attach_zones_records_per_net_pad_layers() -> None:
    """The resolver captures each net's pad layers, wildcard included."""
    footprint = SimpleNamespace(
        position=(10.0, 20.0),
        rotation=0.0,
        graphics=[],
        pads=[
            SimpleNamespace(
                position=(-0.4, 0.0),
                size=(0.4, 0.6),
                net_name="/AC_LINE",
                layers=["F.Cu", "F.Paste", "F.Mask"],
            ),
            SimpleNamespace(
                position=(0.4, 0.0),
                size=(0.4, 0.6),
                net_name="/GND",
                layers=["*.Cu", "*.Mask"],
            ),
        ],
    )
    (zone,) = build_attach_zones([footprint], margin=0.5)
    assert dict(zone.net_layers) == {
        "AC_LINE": frozenset({"F.Cu"}),
        "GND": frozenset({"*.Cu"}),
    }
    assert zone.covers_layer("/AC_LINE", Layer.F_CU)
    assert not zone.covers_layer("/AC_LINE", Layer.IN1_CU)
    # The through-hole net reaches every layer.
    assert zone.covers_layer("/GND", Layer.IN1_CU)
    # A pair is waived only where BOTH nets have pad copper.
    assert zone.exempts(10.0, 20.0, "/AC_LINE", "/GND", Layer.F_CU)
    assert not zone.exempts(10.0, 20.0, "/AC_LINE", "/GND", Layer.IN1_CU)


# ---------------------------------------------------------------------------
# Backward compatibility
# ---------------------------------------------------------------------------


def test_design_rules_pairwise_clearance_defaults_none() -> None:
    assert DesignRules().pairwise_clearance is None


def test_route_pairwise_violation_none_table_noop() -> None:
    # A ``None`` table means the scalar path -- the helper is a no-op.
    moving = Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE")])
    foreign = Route(net=2, net_name="/GND", segments=[_seg(0, 0.1, 5, 0.1, 2, "/GND")])
    zone = AttachZone(0, 0, 5, 1, frozenset({"AC_LINE", "GND"}))
    assert (
        route_pairwise_violation(
            moving,
            1,
            [foreign],
            None,
            attach_zones=(zone,),  # type: ignore[arg-type]
        )
        is None
    )


# ---------------------------------------------------------------------------
# CLI attach wiring (_apply_pairwise_clearance)
# ---------------------------------------------------------------------------


def test_apply_pairwise_clearance_attaches_table_with_router_dru() -> None:
    from types import SimpleNamespace

    from kicad_tools.cli.route_cmd import _apply_pairwise_clearance

    rules = DesignRules(trace_clearance=0.25)
    router = SimpleNamespace(rules=rules)
    args = SimpleNamespace(
        _pairwise_voltages={"AC_LINE": 150.0, "GND": 0.0},
        _pairwise_required={("AC_LINE", "GND"): 1.6},
    )
    _apply_pairwise_clearance(router, args, quiet=True)
    table = rules.pairwise_clearance
    assert isinstance(table, PairwiseClearanceTable)
    # DRU floor is the router's actual trace_clearance, not a hardcoded default.
    assert table.dru == pytest.approx(0.25)
    assert table.required_clearance("AC_LINE", "GND") == pytest.approx(IEC_150V_PD2_IIIA_MM)


def test_apply_pairwise_clearance_noop_without_voltage_map() -> None:
    from types import SimpleNamespace

    from kicad_tools.cli.route_cmd import _apply_pairwise_clearance

    rules = DesignRules()
    router = SimpleNamespace(rules=rules)
    args = SimpleNamespace(_pairwise_voltages=None, _pairwise_required=None)
    _apply_pairwise_clearance(router, args, quiet=True)
    assert rules.pairwise_clearance is None


# ---------------------------------------------------------------------------
# Path-level predicate + bound checker (#4507; consumed by #4766)
# ---------------------------------------------------------------------------


def _foreign_gnd_route(y: float = 0.4, layer: Layer = Layer.F_CU) -> Route:
    return Route(net=2, net_name="/GND", segments=[_seg(0, y, 5, y, 2, "/GND", layer=layer)])


def test_path_predicate_flags_hv_lv_below_requirement() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    v = path_pairwise_violation(
        0.0,
        0.0,
        5.0,
        0.0,
        Layer.F_CU,
        0.2,
        1,
        [_foreign_gnd_route()],
        table,
        net_name="/AC_LINE",
    )
    assert v is not None
    assert v.required_mm == pytest.approx(IEC_150V_PD2_IIIA_MM)
    assert v.actual_mm == pytest.approx(0.2, abs=1e-6)
    assert (v.net_a, v.net_b) == ("/AC_LINE", "/GND")


def test_path_predicate_accepts_same_domain_at_dru() -> None:
    table = _table({"/AC_LINE": 150.0, "/AC_LINE_TAP": 150.0})
    tap = Route(net=2, net_name="/AC_LINE_TAP", segments=[_seg(0, 0.4, 5, 0.4, 2, "/AC_LINE_TAP")])
    assert (
        path_pairwise_violation(
            0.0, 0.0, 5.0, 0.0, Layer.F_CU, 0.2, 1, [tap], table, net_name="/AC_LINE"
        )
        is None
    )


def test_path_predicate_ignores_other_layer_copper() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    assert (
        path_pairwise_violation(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.F_CU,
            0.2,
            1,
            [_foreign_gnd_route(layer=Layer.B_CU)],
            table,
            net_name="/AC_LINE",
        )
        is None
    )


def test_path_predicate_skips_own_net_copper() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    own = Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0.3, 5, 0.3, 1, "/AC_LINE")])
    assert (
        path_pairwise_violation(
            0.0, 0.0, 5.0, 0.0, Layer.F_CU, 0.2, 1, [own], table, net_name="/AC_LINE"
        )
        is None
    )


def test_path_predicate_resolves_moving_name_from_ids() -> None:
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    anonymous = Route(net=2, net_name="", segments=[_seg(0, 0.4, 5, 0.4, 2, "")])
    v = path_pairwise_violation(
        0.0,
        0.0,
        5.0,
        0.0,
        Layer.F_CU,
        0.2,
        1,
        [anonymous],
        table,
        id_to_name={1: "/AC_LINE", 2: "/GND"},
    )
    assert v is not None
    assert (v.net_a, v.net_b) == ("/AC_LINE", "/GND")


def test_path_predicate_unresolvable_moving_net_matches_gate_semantics() -> None:
    """An unnamed moving net resolves no pair -- exactly the gate's verdict."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    foreign = [_foreign_gnd_route()]
    assert path_pairwise_violation(0.0, 0.0, 5.0, 0.0, Layer.F_CU, 0.2, 1, foreign, table) is None
    # The gate reaches the same verdict for a route whose net_name is unset.
    unnamed = Route(net=1, net_name="", segments=[_seg(0, 0, 5, 0, 1, "")])
    assert route_pairwise_violation(unnamed, 1, foreign, table) is None


def test_path_predicate_dormant_without_table() -> None:
    assert (
        path_pairwise_violation(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.F_CU,
            0.2,
            1,
            [_foreign_gnd_route()],
            None,
            net_name="/AC_LINE",
        )
        is None
    )


def test_path_predicate_agrees_with_route_gate() -> None:
    """Predicate and acceptance gate return the SAME finding on the same copper.

    They share ``_segments_pairwise_violation`` by construction; this pins the
    contract #4766 relies on -- a moved segment the predicate accepts cannot be
    flagged by the audit backstop, and vice versa.
    """
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    foreign = [_foreign_gnd_route()]
    for y in (0.0, 2.5):  # violating and clean geometries
        as_route = Route(net=1, net_name="/AC_LINE", segments=[_seg(0, y, 5, y, 1, "/AC_LINE")])
        assert path_pairwise_violation(
            0.0, y, 5.0, y, Layer.F_CU, 0.2, 1, foreign, table, net_name="/AC_LINE"
        ) == route_pairwise_violation(as_route, 1, foreign, table)


def test_path_predicate_honours_layer_scoped_attach_zone() -> None:
    """The #4506/#4699 layer-scoped waiver applies to candidate paths too."""
    table = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    zone = _smd_zone()  # pads on F.Cu only
    # On the pad layer the zone waives the (above-DRU) proximity...
    assert (
        path_pairwise_violation(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.F_CU,
            0.2,
            1,
            [_foreign_gnd_route(y=0.5)],
            table,
            net_name="/AC_LINE",
            attach_zones=(zone,),
        )
        is None
    )
    # ...but the same XY geometry on an inner layer stays a violation.
    assert (
        path_pairwise_violation(
            0.0,
            0.0,
            5.0,
            0.0,
            Layer.IN1_CU,
            0.2,
            1,
            [_foreign_gnd_route(y=0.5, layer=Layer.IN1_CU)],
            table,
            net_name="/AC_LINE",
            attach_zones=(zone,),
        )
        is not None
    )


def test_checker_from_router_dormant_without_table() -> None:
    router = SimpleNamespace(rules=DesignRules(), grid=SimpleNamespace(routes=[]))
    assert PairwisePathChecker.from_router(router) is None


def test_checker_from_router_builds_live_checker() -> None:
    rules = DesignRules(trace_clearance=DRU)
    rules.pairwise_clearance = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    grid = SimpleNamespace(routes=[])
    router = SimpleNamespace(
        rules=rules,
        grid=grid,
        net_names={1: "/AC_LINE", 2: "/GND"},
        _pairwise_attach_zones_cache=None,
    )
    checker = PairwisePathChecker.from_router(router)
    assert checker is not None
    # No foreign copper yet -> clear.
    assert checker.path_is_clear(0.0, 0.0, 5.0, 0.0, Layer.F_CU, 0.2, 1)
    # The checker sees copper committed AFTER construction (live view): the
    # grid-synced optimize loop unmarks/re-marks routes mid-pass, so a
    # construction-time snapshot would go stale.
    grid.routes.append(_foreign_gnd_route())
    assert not checker.path_is_clear(0.0, 0.0, 5.0, 0.0, Layer.F_CU, 0.2, 1)
    v = checker.path_violation(0.0, 0.0, 5.0, 0.0, Layer.F_CU, 0.2, 1)
    assert v is not None and (v.net_a, v.net_b) == ("/AC_LINE", "/GND")
    # Own-net copper never conflicts.
    assert checker.path_is_clear(0.0, 0.0, 5.0, 0.0, Layer.F_CU, 0.2, 2)


def test_checker_from_router_uses_pad_derived_name_fallback() -> None:
    """A router with an empty ``net_names`` map still resolves via its pads."""
    rules = DesignRules(trace_clearance=DRU)
    rules.pairwise_clearance = _table({"/AC_LINE": 150.0, "/GND": 0.0})
    router = SimpleNamespace(
        rules=rules,
        grid=SimpleNamespace(routes=[_foreign_gnd_route()]),
        net_names={},
        _net_name_to_id=lambda: {"/AC_LINE": 1, "/GND": 2},
    )
    checker = PairwisePathChecker.from_router(router)
    assert checker is not None
    assert not checker.path_is_clear(0.0, 0.0, 5.0, 0.0, Layer.F_CU, 0.2, 1)


def test_checker_matches_collision_checker_protocol_signature() -> None:
    """``path_is_clear`` stays call-compatible with the optimizer protocol.

    #4766 composes this checker with ``make_collision_checker``'s output; if
    either side renames or reorders a parameter the composition breaks, so pin
    the positional shape here.
    """
    import inspect

    from kicad_tools.router.optimizer.collision import CollisionChecker

    protocol_params = list(inspect.signature(CollisionChecker.path_is_clear).parameters)
    checker_params = list(inspect.signature(PairwisePathChecker.path_is_clear).parameters)
    assert checker_params == protocol_params
