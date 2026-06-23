"""Tests for the disk-free footprint heuristics (issue #3866).

These cover :func:`kicad_tools.footprints.heuristics.guess_chip_footprint`,
the deterministic value+package mapping used by
``sch assign-footprints --assign-missing`` to resolve standard two-pin SMD
passives without any installed KiCad library.
"""

from __future__ import annotations

import pytest

from kicad_tools.footprints.heuristics import (
    STANDARD_CHIP_SIZES,
    guess_chip_footprint,
)


class TestGuessChipFootprint:
    def test_resistor_0402_from_package(self):
        m = guess_chip_footprint(value="10k", lib_id="Device:R", package="0402", pin_count=2)
        assert m is not None
        assert m.footprint == "Resistor_SMD:R_0402_1005Metric"
        assert m.component_class == "R"
        assert m.chip_size == "0402"

    def test_capacitor_0805_from_lib_id_keyword(self):
        # Package keyword inferred (e.g. via guess_standard_library) may be a
        # library name, so chip size is taken from the value/package token.
        m = guess_chip_footprint(value="100nF", lib_id="Device:C", package="C_0805", pin_count=2)
        assert m is not None
        assert m.footprint == "Capacitor_SMD:C_0805_2012Metric"

    def test_inductor_0603(self):
        m = guess_chip_footprint(value="10uH", lib_id="Device:L", package="0603", pin_count=2)
        assert m is not None
        assert m.footprint == "Inductor_SMD:L_0603_1608Metric"

    def test_ferrite_bead_maps_to_inductor_chip(self):
        m = guess_chip_footprint(
            value="600", lib_id="Device:FerriteBead", package="0402", pin_count=2
        )
        assert m is not None
        assert m.footprint == "Inductor_SMD:L_0402_1005Metric"

    def test_led_0603(self):
        m = guess_chip_footprint(value="RED", lib_id="Device:LED", package="0603", pin_count=2)
        assert m is not None
        assert m.footprint == "LED_SMD:LED_0603_1608Metric"

    def test_diode_0805(self):
        m = guess_chip_footprint(
            value="1N4148", lib_id="Diode:1N4148W", package="0805", pin_count=2
        )
        assert m is not None
        assert m.footprint == "Diode_SMD:D_0805_2012Metric"

    def test_class_from_reference_prefix_when_libid_blank(self):
        m = guess_chip_footprint(value="4.7k", lib_id="", reference="R7", package="0603")
        assert m is not None
        assert m.footprint == "Resistor_SMD:R_0603_1608Metric"

    def test_chip_size_extracted_from_value(self):
        # No explicit package -- the size is embedded in a free-form value.
        m = guess_chip_footprint(value="10k 0402", lib_id="Device:R", pin_count=2)
        assert m is not None
        assert m.chip_size == "0402"

    def test_unknown_class_returns_none(self):
        # A 3-pin IC is not a chip passive: must fail loud (None).
        m = guess_chip_footprint(
            value="LM358", lib_id="Amplifier_Operational:LM358", package="SOIC-8", pin_count=8
        )
        assert m is None

    def test_unknown_package_returns_none(self):
        # Recognised class but no recognised chip size -> None.
        m = guess_chip_footprint(value="10k", lib_id="Device:R", package="weird-pkg", pin_count=2)
        assert m is None

    def test_non_two_pin_known_count_rejected(self):
        # Even a resistor-class symbol with a non-2 pin count is rejected.
        m = guess_chip_footprint(value="10k", lib_id="Device:R", package="0402", pin_count=3)
        assert m is None

    def test_unknown_pin_count_allowed(self):
        m = guess_chip_footprint(value="10k", lib_id="Device:R", package="0402", pin_count=None)
        assert m is not None

    @pytest.mark.parametrize("size", sorted(STANDARD_CHIP_SIZES))
    def test_all_standard_sizes_resolve(self, size):
        m = guess_chip_footprint(value="x", lib_id="Device:R", package=size, pin_count=2)
        assert m is not None
        assert m.footprint == f"Resistor_SMD:R_{size}_{STANDARD_CHIP_SIZES[size]}"
