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

import pytest

from kicad_tools.creepage.standards import StandardLookupError
from kicad_tools.router.layers import Layer
from kicad_tools.router.pairwise_clearance import (
    PairwiseClearanceTable,
    build_pairwise_clearance_table,
    find_pairwise_violations,
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
# Backward compatibility
# ---------------------------------------------------------------------------


def test_design_rules_pairwise_clearance_defaults_none() -> None:
    assert DesignRules().pairwise_clearance is None


def test_route_pairwise_violation_none_table_noop() -> None:
    # A ``None`` table means the scalar path -- the helper is a no-op.
    moving = Route(net=1, net_name="/AC_LINE", segments=[_seg(0, 0, 5, 0, 1, "/AC_LINE")])
    foreign = Route(net=2, net_name="/GND", segments=[_seg(0, 0.1, 5, 0.1, 2, "/GND")])
    assert route_pairwise_violation(moving, 1, [foreign], None) is None  # type: ignore[arg-type]


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
