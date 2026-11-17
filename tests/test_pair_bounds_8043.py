"""Device-bounded pair |ΔV| (``_pair_bounds``) in the voltage map (#8043)."""

from __future__ import annotations

import json

import pytest

from kicad_tools.creepage.engine import (
    pair_bounds_from_dict,
    pair_bounds_unmapped_nets,
)
from kicad_tools.placement.hv_domains import build_required_by_domain_pair
from kicad_tools.router.pairwise_clearance import (
    build_pairwise_clearance_table,
    load_pair_bounds,
)

VMAP = {"/FUSED_LINE": 150, "/SRC_NEG": -150, "/GATE_BUS_NEG": -150}
GOOD = {"nets": ["/FUSED_LINE", "/SRC_NEG"], "max_dv": 100, "why": "Q2B V_DS"}


def test_parse_valid_is_order_and_slash_insensitive():
    b = pair_bounds_from_dict({"_pair_bounds": [GOOD]})
    assert b == {frozenset({"FUSED_LINE", "SRC_NEG"}): (100.0, "Q2B V_DS")}
    assert pair_bounds_from_dict({}) == {}


@pytest.mark.parametrize(
    "entry",
    [
        {**GOOD, "nets": ["/A"]},
        {**GOOD, "nets": ["/A", "A"]},
        {**GOOD, "max_dv": 0},
        {**GOOD, "max_dv": -5},
        {**GOOD, "max_dv": True},
        {**GOOD, "max_dv": float("inf")},
        {**GOOD, "max_dv": "100"},
        {**GOOD, "why": ""},
        {k: v for k, v in GOOD.items() if k != "why"},
        "nope",
    ],
)
def test_parse_malformed(entry):
    with pytest.raises(ValueError):
        pair_bounds_from_dict({"_pair_bounds": [entry]})


def test_parse_not_list_and_duplicate():
    with pytest.raises(ValueError):
        pair_bounds_from_dict({"_pair_bounds": {}})
    rev = {**GOOD, "nets": ["SRC_NEG", "/FUSED_LINE"]}
    with pytest.raises(ValueError):
        pair_bounds_from_dict({"_pair_bounds": [GOOD, rev]})


def test_unmapped_net_warning_helper():
    b = pair_bounds_from_dict({"_pair_bounds": [{**GOOD, "nets": ["/FUSED_LINE", "/TYPO"]}]})
    assert pair_bounds_unmapped_nets(b, VMAP) == ["TYPO"]


def _table(bounds=None):
    return build_pairwise_clearance_table(VMAP, dru=0.2, pair_bounds=bounds)


def test_router_table_bound_lowers_requirement():
    base = _table()
    bounded = _table(pair_bounds_from_dict({"_pair_bounds": [GOOD]}))
    unb = base.required_clearance("/FUSED_LINE", "/SRC_NEG")
    low = bounded.required_clearance("/FUSED_LINE", "/SRC_NEG")
    assert low < unb
    # the 100 V row, i.e. identical to an unbounded 100 V pair
    ref = build_required_by_domain_pair({"a": 100.0, "b": 0.0})[("a", "b")]
    assert low == pytest.approx(ref)
    # unrelated pair untouched (same-potential cluster stays 0 V)
    assert bounded.required_clearance("/GATE_BUS_NEG", "/SRC_NEG") == 0.2


def test_bound_above_worst_case_has_no_effect_and_none_is_identical():
    big = pair_bounds_from_dict({"_pair_bounds": [{**GOOD, "max_dv": 1000}]})
    assert _table(big).required_by_pair == _table().required_by_pair
    assert _table(None).required_by_pair == _table({}).required_by_pair


def test_bound_below_threshold_drops_pair():
    b = pair_bounds_from_dict({"_pair_bounds": [{**GOOD, "max_dv": 10}]})
    assert ("FUSED_LINE", "SRC_NEG") not in _table(b).required_by_pair


def test_post_route_audit_uses_lowered_requirement():
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.pairwise_clearance import segment_pair_violation
    from kicad_tools.router.primitives import Segment

    def seg(y, net, name):
        return Segment(0.0, y, 5.0, y, 0.2, Layer.F_CU, net=net, net_name=name)

    base = _table()
    bounded = _table(pair_bounds_from_dict({"_pair_bounds": [GOOD]}))
    req_hi = base.required_clearance("/FUSED_LINE", "/SRC_NEG")
    req_lo = bounded.required_clearance("/FUSED_LINE", "/SRC_NEG")
    gap = (req_hi + req_lo) / 2
    a, b = seg(0.0, 1, "/FUSED_LINE"), seg(gap + 0.2, 2, "/SRC_NEG")  # edge gap == gap
    assert segment_pair_violation(a, b, base) is not None
    assert segment_pair_violation(a, b, bounded) is None


def test_load_pair_bounds_from_file(tmp_path):
    p = tmp_path / "vm.json"
    p.write_text(json.dumps({**VMAP, "_pair_bounds": [GOOD]}))
    assert frozenset({"FUSED_LINE", "SRC_NEG"}) in load_pair_bounds(p)
