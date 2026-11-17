"""Tests for BOM variety detection (same-value passives with different footprints)."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.sch_validate import (
    check_bom_variety,
)

# ---------------------------------------------------------------------------
# Helpers to generate synthetic KiCad schematics
# ---------------------------------------------------------------------------


def _make_lib_symbol(lib_id: str) -> str:
    """Generate a minimal lib_symbols entry for a passive."""
    part_name = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    return f"""(symbol "{lib_id}"
            (pin_names (offset 0.254))
            (symbol "{part_name}_0_1"
                (rectangle
                    (start -1.27 -2.54)
                    (end 1.27 2.54)
                    (stroke (width 0.254))
                    (fill (type background))
                )
            )
            (symbol "{part_name}_1_1"
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
    x: float = 100.0,
    y: float = 50.0,
    dnp: bool = False,
) -> str:
    """Generate a symbol instance S-expression for a passive component."""
    dnp_str = "yes" if dnp else "no"
    return f"""(symbol
        (lib_id "{lib_id}")
        (at {x} {y} 0)
        (unit 1)
        (in_bom yes)
        (on_board yes)
        (dnp {dnp_str})
        (uuid "uuid-{ref.lower()}")
        (property "Reference" "{ref}"
            (at {x + 2} {y - 2} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Value" "{value}"
            (at {x + 2} {y} 0)
            (effects (font (size 1.27 1.27)) (justify left))
        )
        (property "Footprint" "{footprint}"
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        (property "Datasheet" "~"
            (at {x} {y} 0)
            (effects (font (size 1.27 1.27)) hide)
        )
        (pin "1" (uuid "pin-{ref.lower()}-1"))
        (pin "2" (uuid "pin-{ref.lower()}-2"))
    )"""


def _build_schematic(symbols: list[dict]) -> str:
    """Build a complete schematic string from symbol descriptors.

    Each dict has:
        ref: str         - reference designator (e.g. "C1")
        lib_id: str      - library identifier (e.g. "Device:C")
        value: str       - component value (e.g. "100nF")
        footprint: str   - footprint (e.g. "Capacitor_SMD:C_0402_1005Metric")
        dnp: bool        - (optional) Do Not Populate flag
    """
    lib_symbols = []
    symbol_instances = []
    seen_lib_ids: set[str] = set()

    for idx, sym in enumerate(symbols):
        ref = sym["ref"]
        lib_id = sym["lib_id"]
        value = sym["value"]
        footprint = sym["footprint"]
        dnp = sym.get("dnp", False)
        x = 100.0 + idx * 50.0

        if lib_id not in seen_lib_ids:
            lib_symbols.append(_make_lib_symbol(lib_id))
            seen_lib_ids.add(lib_id)

        symbol_instances.append(_make_symbol(ref, lib_id, value, footprint, x=x, dnp=dnp))

    lib_str = "\n".join(lib_symbols)
    sym_str = "\n".join(symbol_instances)

    return f"""(kicad_sch
        (version 20231120)
        (generator "test")
        (generator_version "8.0")
        (uuid "root-uuid")
        (paper "A4")
        (lib_symbols
            {lib_str}
        )
        {sym_str}
        (sheet_instances
            (path "/"
                (page "1")
            )
        )
        (symbol_instances
            {_make_symbol_instances_block(symbols)}
        )
    )"""


def _make_symbol_instances_block(symbols: list[dict]) -> str:
    """Generate the symbol_instances block entries."""
    entries = []
    for sym in symbols:
        ref = sym["ref"]
        entries.append(
            f"""(path "/{ref.lower()}-uuid"
                (reference "{ref}")
                (unit 1)
            )"""
        )
    return "\n".join(entries)


def _write_schematic(tmp_path: Path, symbols: list[dict]) -> str:
    """Write a synthetic schematic file and return its path."""
    sch_path = tmp_path / "test.kicad_sch"
    sch_path.write_text(_build_schematic(symbols))
    return str(sch_path)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestBomVarietySameFootprint:
    """Same value + same footprint should produce no warning."""

    def test_caps_same_footprint(self, tmp_path):
        symbols = [
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C2",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C3",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 0

    def test_resistors_same_footprint(self, tmp_path):
        symbols = [
            {
                "ref": "R1",
                "lib_id": "Device:R",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0402_1005Metric",
            },
            {
                "ref": "R2",
                "lib_id": "Device:R",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0402_1005Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 0


class TestBomVarietyDifferentFootprints:
    """Same value + different footprints should produce warning."""

    def test_caps_mixed_footprints(self, tmp_path):
        symbols = [
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C3",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C5",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C7",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 1
        issue = issues[0]
        assert issue.severity == "warning"
        assert issue.category == "bom_variety"
        assert "100nF" in issue.message
        assert "C_0402_1005Metric" in issue.message
        assert "C_0603_1608Metric" in issue.message
        assert "C1" in issue.message
        assert "C7" in issue.message
        assert "consolidating" in issue.message

    def test_resistors_mixed_footprints(self, tmp_path):
        symbols = [
            {
                "ref": "R1",
                "lib_id": "Device:R_Small",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0402_1005Metric",
            },
            {
                "ref": "R2",
                "lib_id": "Device:R_Small",
                "value": "10k",
                "footprint": "Resistor_SMD:R_0603_1608Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 1
        assert "10k" in issues[0].message


class TestBomVarietyVoltageRating:
    """Different voltage ratings should NOT trigger (they form separate groups)."""

    def test_different_voltage_ratings(self, tmp_path):
        symbols = [
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100nF 25V",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C2",
                "lib_id": "Device:C",
                "value": "100nF 50V",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 0

    def test_same_voltage_different_footprint_triggers(self, tmp_path):
        symbols = [
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100nF 25V",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C2",
                "lib_id": "Device:C",
                "value": "100nF 25V",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 1
        assert "100nF 25V" in issues[0].message


class TestBomVarietyDnpExclusion:
    """DNP components should be excluded from grouping."""

    def test_dnp_excluded(self, tmp_path):
        symbols = [
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C2",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
                "dnp": True,
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        # With C2 excluded as DNP, only one footprint remains -- no warning
        assert len(issues) == 0


class TestBomVarietyNonPassiveIgnored:
    """Non-passive components (ICs, connectors) should be ignored."""

    def test_ic_ignored(self, tmp_path):
        symbols = [
            {
                "ref": "U1",
                "lib_id": "MCU_ST_STM32:STM32C011F6Px",
                "value": "STM32C011F6Px",
                "footprint": "Package_SO:TSSOP-20",
            },
            {
                "ref": "U2",
                "lib_id": "MCU_ST_STM32:STM32C011F6Px",
                "value": "STM32C011F6Px",
                "footprint": "Package_QFP:QFP-32",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 0

    def test_connector_ignored(self, tmp_path):
        symbols = [
            {
                "ref": "J1",
                "lib_id": "Connector:Conn_01x04",
                "value": "Conn_01x04",
                "footprint": "Connector_PinHeader:PinHeader_1x04_P2.54mm_Vertical",
            },
            {
                "ref": "J2",
                "lib_id": "Connector:Conn_01x04",
                "value": "Conn_01x04",
                "footprint": "Connector_PinSocket:PinSocket_1x04_P2.54mm_Vertical",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 0


class TestBomVarietyEdgeCases:
    """Edge cases: single component, missing footprint, empty value."""

    def test_single_component_no_warning(self, tmp_path):
        symbols = [
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 0

    def test_missing_footprint_excluded(self, tmp_path):
        symbols = [
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {"ref": "C2", "lib_id": "Device:C", "value": "100nF", "footprint": ""},
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        # C2 is excluded (no footprint), so only one footprint in group
        assert len(issues) == 0

    def test_missing_value_excluded(self, tmp_path):
        symbols = [
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C2",
                "lib_id": "Device:C",
                "value": "~",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        # C2 is excluded (placeholder value), so no variety
        assert len(issues) == 0

    def test_different_values_different_footprints_no_warning(self, tmp_path):
        """Different values should form separate groups even with different footprints."""
        symbols = [
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100nF",
                "footprint": "Capacitor_SMD:C_0402_1005Metric",
            },
            {
                "ref": "C2",
                "lib_id": "Device:C",
                "value": "10uF",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 0

    def test_different_component_types_same_value_separate_groups(self, tmp_path):
        """R and C with the same value string should not be grouped together."""
        symbols = [
            {
                "ref": "R1",
                "lib_id": "Device:R",
                "value": "100",
                "footprint": "Resistor_SMD:R_0402_1005Metric",
            },
            {
                "ref": "C1",
                "lib_id": "Device:C",
                "value": "100",
                "footprint": "Capacitor_SMD:C_0603_1608Metric",
            },
        ]
        sch_path = _write_schematic(tmp_path, symbols)
        issues = check_bom_variety(sch_path)
        assert len(issues) == 0
