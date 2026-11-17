"""Tests for kicad_tools.optim.fom_thermal.

Issue #3186 -- thermal FOM soft term.
"""

from __future__ import annotations

from kicad_tools.optim.fom_features import BoardFeatures, FootprintFeature
from kicad_tools.optim.fom_thermal import _power_for_footprint, thermal_spread
from kicad_tools.schema.pcb import PCB


def _empty_pcb() -> PCB:
    return PCB.create(width=100, height=100)


def test_power_for_footprint_explicit_W():
    assert _power_for_footprint({"Power_W": "0.5"}) == 0.5


def test_power_for_footprint_case_insensitive_match():
    assert _power_for_footprint({"power_w": "1.2"}) == 1.2


def test_power_for_footprint_tdp():
    assert _power_for_footprint({"TDP_W": "5.0"}) == 5.0


def test_power_for_footprint_none_when_missing():
    assert _power_for_footprint({}) is None
    assert _power_for_footprint({"Foo": "1.0"}) is None


def test_power_for_footprint_handles_bad_value():
    # Non-numeric value returns None (skips invalid).
    assert _power_for_footprint({"Power_W": "abc"}) is None


def test_thermal_spread_empty_pcb():
    pcb = _empty_pcb()
    features = BoardFeatures()
    # No zones, no dissipating parts.
    assert thermal_spread(features, pcb) == 0.0


def test_thermal_spread_no_zones_returns_zero():
    pcb = _empty_pcb()
    features = BoardFeatures()
    features.footprints = [FootprintFeature("U1", "LDO", "U", 10.0, 10.0, 0, "F.Cu", False, False)]
    # Even with dissipating parts, no zones -> 0.
    assert thermal_spread(features, pcb) == 0.0


def test_thermal_spread_no_metadata_no_signal():
    # PCB has a zone but no parts declare power -> 0.
    pcb = _empty_pcb()
    features = BoardFeatures()
    features.footprints = [FootprintFeature("R1", "10k", "R", 10.0, 10.0, 0, "F.Cu", False, False)]
    # Manually inject a zone (use private list since PCB's setter guards it).
    from kicad_tools.schema.pcb import Zone

    z = Zone(
        name="GND",
        net_number=1,
        net_name="GND",
        layer="F.Cu",
        polygon=[(0, 0), (10, 0), (10, 10), (0, 10)],
    )
    pcb._zones = [z]
    # No declared power -> we expect 0 unless the library hint fires; R1/10k is not classified
    # as a heat source.
    val = thermal_spread(features, pcb)
    assert val == 0.0
