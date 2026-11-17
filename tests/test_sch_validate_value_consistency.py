"""Tests for capacitor value consistency check (mixed voltage-rating formatting)."""

from __future__ import annotations

import pytest

from kicad_tools.cli.sch_validate import (
    _split_cap_value,
    check_value_consistency,
)

# ---------------------------------------------------------------------------
# Unit tests for _split_cap_value helper
# ---------------------------------------------------------------------------


class TestSplitCapValue:
    def test_value_with_voltage(self):
        assert _split_cap_value("100nF 25V") == ("100nF", "25V")

    def test_value_without_voltage(self):
        assert _split_cap_value("100nF") == ("100nF", None)

    def test_microfarad_with_voltage(self):
        assert _split_cap_value("10uF 16V") == ("10uF", "16V")

    def test_picofarad(self):
        assert _split_cap_value("22pF") == ("22pF", None)

    def test_decimal_value(self):
        assert _split_cap_value("4.7uF 25V") == ("4.7uF", "25V")

    def test_millifarad(self):
        assert _split_cap_value("1mF") == ("1mF", None)

    def test_mu_symbol(self):
        assert _split_cap_value("10\u00b5F 50V") == ("10\u00b5F", "50V")

    def test_plain_F(self):
        assert _split_cap_value("1F") == ("1F", None)

    def test_non_cap_value(self):
        # Resistor value -- no match, returns as-is
        assert _split_cap_value("10k") == ("10k", None)

    def test_tilde(self):
        assert _split_cap_value("~") == ("~", None)

    def test_whitespace_stripping(self):
        assert _split_cap_value("  100nF  25V  ") == ("100nF", "25V")

    def test_case_insensitive(self):
        assert _split_cap_value("100NF 25V") == ("100NF", "25V")

    def test_complex_qualifier(self):
        assert _split_cap_value("100nF X7R 25V") == ("100nF", "X7R 25V")


# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics with capacitors
# ---------------------------------------------------------------------------


def _cap_lib_symbol(lib_id: str = "Device:C") -> str:
    """Generate a minimal lib_symbols entry for a capacitor."""
    part = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    return f"""(symbol "{lib_id}"
        (pin_names (offset 0.254))
        (symbol "{part}_0_1"
            (rectangle
                (start -1.27 -1.27)
                (end 1.27 1.27)
                (stroke (width 0.254))
                (fill (type background))
            )
        )
        (symbol "{part}_1_1"
            (pin passive line
                (at 0 2.54 270)
                (length 2.54)
                (name "1")
                (number "1")
            )
            (pin passive line
                (at 0 -2.54 90)
                (length 2.54)
                (name "2")
                (number "2")
            )
        )
    )"""


def _resistor_lib_symbol(lib_id: str = "Device:R") -> str:
    """Generate a minimal lib_symbols entry for a resistor."""
    part = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    return f"""(symbol "{lib_id}"
        (pin_names (offset 0.254))
        (symbol "{part}_0_1"
            (rectangle
                (start -1.27 -1.27)
                (end 1.27 1.27)
                (stroke (width 0.254))
                (fill (type background))
            )
        )
        (symbol "{part}_1_1"
            (pin passive line
                (at 0 2.54 270)
                (length 2.54)
                (name "1")
                (number "1")
            )
            (pin passive line
                (at 0 -2.54 90)
                (length 2.54)
                (name "2")
                (number "2")
            )
        )
    )"""


def _make_symbol(
    ref: str,
    lib_id: str,
    value: str,
    footprint: str,
    x: float,
    dnp: bool = False,
) -> str:
    """Generate a symbol instance S-expression."""
    dnp_str = "yes" if dnp else "no"
    return f"""(symbol
        (lib_id "{lib_id}")
        (at {x} 50 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp {dnp_str})
        (uuid "uuid-{ref.lower()}")
        (property "Reference" "{ref}"
            (at {x + 2} 48 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Value" "{value}"
            (at {x + 2} 50 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Footprint" "{footprint}"
            (at {x} 50 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        (property "Datasheet" "~"
            (at {x} 50 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        (pin "1" (uuid "pin-{ref.lower()}-1"))
        (pin "2" (uuid "pin-{ref.lower()}-2"))
    )"""


def _make_schematic(
    components: list[dict],
    lib_symbols: str = "",
) -> str:
    """Build a minimal KiCad schematic with the given components.

    Each component dict: {"ref", "lib_id", "value", "footprint", "dnp"(optional)}
    """
    # Collect unique lib_ids and generate lib_symbols if not provided
    if not lib_symbols:
        seen = set()
        lib_parts = []
        for comp in components:
            lid = comp["lib_id"]
            if lid not in seen:
                seen.add(lid)
                part = lid.split(":")[-1] if ":" in lid else lid
                if part == "R" or part.startswith("R_"):
                    lib_parts.append(_resistor_lib_symbol(lid))
                else:
                    lib_parts.append(_cap_lib_symbol(lid))
        lib_symbols = "\n".join(lib_parts)

    syms = []
    for i, comp in enumerate(components):
        syms.append(
            _make_symbol(
                ref=comp["ref"],
                lib_id=comp["lib_id"],
                value=comp["value"],
                footprint=comp.get("footprint", "Capacitor_SMD:C_0402_1005Metric"),
                x=100.0 + i * 20.0,
                dnp=comp.get("dnp", False),
            )
        )

    sym_block = "\n".join(syms)
    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-value-consistency-uuid")
    (paper "A4")
    (lib_symbols
        {lib_symbols}
    )
    {sym_block}
)
"""


# ---------------------------------------------------------------------------
# Integration tests for check_value_consistency
# ---------------------------------------------------------------------------


@pytest.fixture()
def tmp_sch(tmp_path):
    """Factory fixture that writes a schematic string and returns the path."""

    def _write(content: str) -> str:
        p = tmp_path / "test.kicad_sch"
        p.write_text(content)
        return str(p)

    return _write


class TestCheckValueConsistency:
    """Tests for check_value_consistency() end-to-end."""

    def test_mixed_group_produces_warning(self, tmp_sch):
        """Some caps '100nF', some '100nF 25V' -> 1 warning."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "100nF"},
                {"ref": "C2", "lib_id": "Device:C", "value": "100nF"},
                {"ref": "C3", "lib_id": "Device:C", "value": "100nF 25V"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 1
        assert "100nF" in warnings[0].message
        assert "C3" in warnings[0].message
        assert "C1" in warnings[0].message
        assert warnings[0].category == "value_consistency"

    def test_uniform_no_voltage_no_warning(self, tmp_sch):
        """All caps '100nF' -> 0 warnings."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "100nF"},
                {"ref": "C2", "lib_id": "Device:C", "value": "100nF"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 0

    def test_uniform_with_voltage_no_warning(self, tmp_sch):
        """All caps '100nF 25V' -> 0 warnings."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "100nF 25V"},
                {"ref": "C2", "lib_id": "Device:C", "value": "100nF 25V"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 0

    def test_different_voltages_no_warning(self, tmp_sch):
        """'100nF 25V' vs '100nF 50V' -> 0 warnings (both have qualifiers)."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "100nF 25V"},
                {"ref": "C2", "lib_id": "Device:C", "value": "100nF 50V"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 0

    def test_dnp_excluded(self, tmp_sch):
        """DNP cap with voltage, active cap without -> 0 warnings."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "100nF"},
                {"ref": "C2", "lib_id": "Device:C", "value": "100nF 25V", "dnp": True},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 0

    def test_non_capacitor_ignored(self, tmp_sch):
        """Resistors with mixed formatting -> 0 warnings."""
        sch = _make_schematic(
            [
                {"ref": "R1", "lib_id": "Device:R", "value": "10k"},
                {"ref": "R2", "lib_id": "Device:R", "value": "10k 0.25W"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 0

    def test_multiple_base_values_multiple_warnings(self, tmp_sch):
        """Both '100nF' and '10uF' have mixed groups -> 2 warnings."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "100nF"},
                {"ref": "C2", "lib_id": "Device:C", "value": "100nF 25V"},
                {"ref": "C3", "lib_id": "Device:C", "value": "10uF"},
                {"ref": "C4", "lib_id": "Device:C", "value": "10uF 16V"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 2

    def test_single_component_no_warning(self, tmp_sch):
        """Single component -> 0 warnings."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "100nF"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 0

    def test_tilde_value_ignored(self, tmp_sch):
        """Value '~' is skipped."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "~"},
                {"ref": "C2", "lib_id": "Device:C", "value": "100nF 25V"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 0

    def test_missing_footprint_ignored(self, tmp_sch):
        """Component with empty footprint is skipped."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "100nF", "footprint": ""},
                {"ref": "C2", "lib_id": "Device:C", "value": "100nF 25V"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 0

    def test_warning_message_format(self, tmp_sch):
        """Verify the warning message contains expected substrings."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "100nF"},
                {"ref": "C3", "lib_id": "Device:C", "value": "100nF 25V"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 1
        msg = warnings[0].message
        assert "with voltage: C3 (100nF 25V)" in msg
        assert "without voltage: C1" in msg
        assert "consider standardizing" in msg

    def test_polarized_capacitor_included(self, tmp_sch):
        """C_Polarized is also a capacitor and should be checked."""
        sch = _make_schematic(
            [
                {"ref": "C1", "lib_id": "Device:C", "value": "10uF"},
                {"ref": "C2", "lib_id": "Device:C_Polarized", "value": "10uF 25V"},
            ]
        )
        issues = check_value_consistency(tmp_sch(sch))
        warnings = [i for i in issues if i.severity == "warning"]
        assert len(warnings) == 1
