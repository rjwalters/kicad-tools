"""``DesignRules`` enforces ``min_inner_clearance_mm <= min_clearance_mm`` (Issue #9113)."""

from __future__ import annotations

from dataclasses import replace

import pytest

from kicad_tools.manufacturers.base import (
    _DATA_DIR,
    DesignRules,
    load_design_rules_from_yaml,
)

_SHIPPED_YAML_IDS = sorted(p.stem for p in _DATA_DIR.glob("*.yaml"))

_BASE = {
    "min_trace_width_mm": 0.1,
    "min_clearance_mm": 0.10,
    "min_via_drill_mm": 0.2,
    "min_via_diameter_mm": 0.4,
    "min_annular_ring_mm": 0.1,
}


def test_inner_stricter_than_outer_raises_naming_both_values():
    with pytest.raises(ValueError) as exc:
        DesignRules(**{**_BASE, "min_inner_clearance_mm": 0.20})
    assert str(exc.value) == (
        "min_inner_clearance_mm (0.2) must not exceed min_clearance_mm (0.1): "
        "the inner-layer floor is the looser one (lighter inner copper)"
    )


@pytest.mark.parametrize("inner", [None, 0.10, 0.0762])
def test_none_equal_or_lower_inner_constructs(inner):
    rules = DesignRules(**{**_BASE, "min_inner_clearance_mm": inner})
    assert rules.min_inner_clearance_mm == inner


def test_dataclasses_replace_cannot_invert_floors():
    rules = DesignRules(**{**_BASE, "min_inner_clearance_mm": 0.0762})
    with pytest.raises(ValueError):
        replace(rules, min_inner_clearance_mm=0.25)
    with pytest.raises(ValueError):
        replace(rules, min_clearance_mm=0.05)


@pytest.mark.parametrize("manufacturer_id", _SHIPPED_YAML_IDS)
def test_every_shipped_profile_loads(manufacturer_id):
    configs = load_design_rules_from_yaml(manufacturer_id)
    for rules in configs.values():
        if rules.min_inner_clearance_mm is not None:
            assert rules.min_inner_clearance_mm <= rules.min_clearance_mm
