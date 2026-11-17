"""apply_design_template clamps netclass values to manufacturer floors (#7392)."""

import pytest

from kicad_tools.core.netclass_templates import (
    apply_design_template,
    get_available_design_types,
)
from kicad_tools.core.project_file import get_netclass_definitions
from kicad_tools.manufacturers import get_manufacturer_ids, get_profile


def _by_name(data):
    return {nc["name"]: nc for nc in get_netclass_definitions(data)}


# (netclass key, floor attribute)
_FLOORS = [
    ("track_width", "min_trace_width_mm"),
    ("clearance", "min_clearance_mm"),
    ("via_diameter", "min_via_diameter_mm"),
    ("via_drill", "min_via_drill_mm"),
]


@pytest.mark.parametrize("layers", [2, 4])
@pytest.mark.parametrize("mfr", get_manufacturer_ids())
@pytest.mark.parametrize("design_type", get_available_design_types())
def test_no_netclass_value_below_floor(mfr, layers, design_type):
    rules = get_profile(mfr).get_design_rules(layers)
    data: dict = {}
    apply_design_template(data, design_type, rules=rules)
    classes = _by_name(data)
    assert "Default" in classes
    for name, nc in classes.items():
        for key, attr in _FLOORS:
            assert nc[key] >= getattr(rules, attr), (mfr, layers, name, key)
        assert nc["diff_pair_width"] >= rules.min_trace_width_mm
        assert nc["diff_pair_gap"] >= rules.min_clearance_mm


def test_clamp_never_lowers_and_none_is_unchanged():
    base: dict = {}
    apply_design_template(base, "audio")
    rules = get_profile("jlcpcb").get_design_rules(4)
    clamped: dict = {}
    apply_design_template(clamped, "audio", rules=rules)
    b, c = _by_name(base), _by_name(clamped)
    assert b["Default"]["clearance"] == 0.15
    for name in b:
        for key, _ in _FLOORS:
            assert c[name][key] >= b[name][key]


def test_seeed_default_clearance_meets_floor():
    rules = get_profile("seeed").get_design_rules(2)
    data: dict = {}
    apply_design_template(data, "audio", rules=rules)
    assert _by_name(data)["Default"]["clearance"] >= 0.1524
