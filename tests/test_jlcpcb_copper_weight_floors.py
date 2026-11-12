"""JLCPCB floors scale with outer copper weight (Issue #8836)."""

import pytest

from kicad_tools.manufacturers import generate_dru, get_profile
from kicad_tools.manufacturers.base import UnsupportedCopperWeightError


@pytest.fixture
def profile():
    return get_profile("jlcpcb")


def test_two_oz_outer_clearance_floor_is_tighter_than_one_oz(profile):
    two = profile.get_design_rules(4, 2.0)
    one = profile.get_design_rules(4, 1.0)
    assert two.min_clearance_mm >= 0.15
    assert two.min_clearance_mm > 0.13  # a 0.13 mm different-net gap must fail
    assert one.min_clearance_mm < 0.13  # ... but passes at 1 oz
    assert two.min_inner_clearance_mm == pytest.approx(0.1016)


def test_emitted_dru_scopes_outer_and_inner_clearance(profile):
    dru = generate_dru(profile.get_design_rules(4, 2.0), manufacturer_name="JLCPCB")
    assert "(layer outer)" in dru and "(layer inner)" in dru
    assert "Clearance (outer)" in dru
    assert "(constraint clearance (min 0.1524mm))" in dru


def test_one_oz_dru_keeps_single_unscoped_clearance(profile):
    dru = generate_dru(profile.get_design_rules(4, 1.0), manufacturer_name="JLCPCB")
    assert '(rule "Clearance - JLCPCB"' in dru
    assert "(layer outer)" not in dru


def test_unsupported_heavy_copper_raises_not_one_oz_fallback(profile):
    with pytest.raises(UnsupportedCopperWeightError, match="3 oz"):
        profile.get_design_rules(4, 3.0, strict=True)


def test_two_layer_two_oz_clearance(profile):
    assert profile.get_design_rules(2, 2.0).min_clearance_mm >= 0.15


def test_non_strict_lookup_keeps_legacy_fallback(profile):
    assert profile.get_design_rules(4, 3.0).min_clearance_mm == pytest.approx(0.1016)
