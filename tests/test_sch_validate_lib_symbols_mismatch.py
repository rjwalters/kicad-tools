"""Tests for lib_symbols definition mismatch detection."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.sch_validate import (
    check_lib_symbols_mismatch,
    validate_schematic,
)
from tests._sch_sexp_builders import (
    make_lib_symbol as _make_lib_symbol,
)
from tests._sch_sexp_builders import (
    make_symbol_instance as _make_symbol_instance,
)

# ---------------------------------------------------------------------------
# Helpers: synthetic KiCad schematic generation
# ---------------------------------------------------------------------------


def _make_schematic(*blocks: str, lib_symbols: str = "") -> str:
    """Wrap lib_symbols and symbol instances into a minimal schematic."""
    body = "\n".join(blocks)
    return f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "test-lib-sym-mismatch-uuid")
    (paper "A4")
    (lib_symbols
        {lib_symbols}
    )
    {body}
    (sheet_instances
        (path "/"
            (page "1")
        )
    )
)"""


def _write_sch(tmp_path: Path, content: str, name: str = "test.kicad_sch") -> str:
    """Write schematic content to a file and return the path as string."""
    p = tmp_path / name
    p.write_text(content)
    return str(p)


# ---------------------------------------------------------------------------
# Test: mismatch detected
# ---------------------------------------------------------------------------


class TestLibSymbolsMismatch:
    """Tests for check_lib_symbols_mismatch()."""

    def test_mismatch_detected(self, tmp_path):
        """Placed symbol lib_id not in lib_symbols -> error."""
        # lib_symbols has AP2204K-1.5 but instance references AP2112K-3.3
        wrong_pins = [
            ("1", "VIN", "input"),
            ("2", "GND", "passive"),
            ("3", "EN", "input"),
            ("4", "NC", "passive"),
            ("5", "VOUT", "output"),
        ]
        lib_sym = _make_lib_symbol("Regulator_Linear:AP2204K-1.5", wrong_pins)
        inst = _make_symbol_instance(
            "U1",
            "Regulator_Linear:AP2112K-3.3",
            wrong_pins,
            footprint="Package_TO_SOT_SMD:SOT-23-5",
        )
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        issues = check_lib_symbols_mismatch(sch_path)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 1
        assert "U1" in errors[0].message
        assert "AP2112K-3.3" in errors[0].message
        assert errors[0].category == "lib_symbols_mismatch"

    def test_no_false_positives(self, tmp_path):
        """All placed lib_ids have matching lib_symbols entries -> no issues."""
        pins = [
            ("1", "VIN", "input"),
            ("2", "GND", "passive"),
            ("3", "EN", "input"),
            ("4", "NC", "passive"),
            ("5", "VOUT", "output"),
        ]
        lib_id = "Regulator_Linear:AP2204K-1.5"
        lib_sym = _make_lib_symbol(lib_id, pins)
        inst = _make_symbol_instance("U1", lib_id, pins, footprint="Package_TO_SOT_SMD:SOT-23-5")
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        issues = check_lib_symbols_mismatch(sch_path)
        assert len(issues) == 0

    def test_power_symbol_skipped(self, tmp_path):
        """Power symbols with no lib_symbols entry should not be flagged."""
        pins = [("1", "GND", "power_in")]
        # No lib_symbols entry for power:GND
        inst = _make_symbol_instance("PWR1", "power:GND", pins, footprint="~")
        content = _make_schematic(inst, lib_symbols="")
        sch_path = _write_sch(tmp_path, content)

        issues = check_lib_symbols_mismatch(sch_path)
        assert len(issues) == 0

    def test_multiple_mismatches(self, tmp_path):
        """Multiple placed symbols with missing lib_symbols entries."""
        pins = [("1", "A", "passive"), ("2", "B", "passive")]
        # lib_symbols is empty but we have two non-power instances
        inst1 = _make_symbol_instance(
            "U1", "IC:ChipA", pins, footprint="Package_SO:SOIC-8_3.9x4.9mm", x=100.0
        )
        inst2 = _make_symbol_instance(
            "U2", "IC:ChipB", pins, footprint="Package_SO:SOIC-8_3.9x4.9mm", x=200.0
        )
        content = _make_schematic(inst1, inst2, lib_symbols="")
        sch_path = _write_sch(tmp_path, content)

        issues = check_lib_symbols_mismatch(sch_path)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 2
        refs = {e.message.split(":")[0] for e in errors}
        assert refs == {"U1", "U2"}

    def test_multi_sheet_hierarchy(self, tmp_path):
        """Mismatch in sub-sheet is detected."""
        # Root sheet references a sub-sheet
        sub_pins = [("1", "A", "passive"), ("2", "B", "passive")]
        sub_inst = _make_symbol_instance(
            "R1", "Device:R_Missing", sub_pins, footprint="Resistor_SMD:R_0603_1608Metric"
        )
        sub_content = f"""(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "sub-sheet-uuid")
    (paper "A4")
    (lib_symbols
    )
    {sub_inst}
    (sheet_instances
        (path "/sub-sheet-uuid"
            (page "2")
        )
    )
)"""
        sub_path = tmp_path / "sub.kicad_sch"
        sub_path.write_text(sub_content)

        # Root sheet with a sheet reference to sub
        root_content = """(kicad_sch
    (version 20231120)
    (generator "kicadtools_test")
    (uuid "root-uuid")
    (paper "A4")
    (lib_symbols
    )
    (sheet
        (at 100 50)
        (size 20 10)
        (uuid "sub-sheet-uuid")
        (property "Sheetname" "sub"
            (at 100 50 0)
            (effects (font (size 1.27 1.27)))
        )
        (property "Sheetfile" "sub.kicad_sch"
            (at 100 60 0)
            (effects (font (size 1.27 1.27)))
        )
    )
    (sheet_instances
        (path "/"
            (page "1")
        )
    )
)"""
        root_path = tmp_path / "root.kicad_sch"
        root_path.write_text(root_content)

        issues = check_lib_symbols_mismatch(str(root_path))
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) >= 1
        assert any("R1" in e.message for e in errors)

    def test_error_message_includes_sheet_location(self, tmp_path):
        """Error message should include the sheet location."""
        pins = [("1", "A", "passive")]
        inst = _make_symbol_instance(
            "U1", "IC:Missing", pins, footprint="Package_SO:SOIC-8_3.9x4.9mm"
        )
        content = _make_schematic(inst, lib_symbols="")
        sch_path = _write_sch(tmp_path, content)

        issues = check_lib_symbols_mismatch(sch_path)
        errors = [i for i in issues if i.severity == "error"]
        assert len(errors) == 1
        assert errors[0].location != ""


# ---------------------------------------------------------------------------
# Test: integration with validate_schematic
# ---------------------------------------------------------------------------


class TestValidateSchematicIntegration:
    """Verify the check is registered in validate_schematic."""

    def test_check_registered(self, tmp_path):
        """lib_symbols_mismatch appears in checks_run."""
        pins = [("1", "A", "passive"), ("2", "B", "passive")]
        lib_id = "Device:R"
        lib_sym = _make_lib_symbol(lib_id, pins)
        inst = _make_symbol_instance("R1", lib_id, pins, footprint="Resistor_SMD:R_0603_1608Metric")
        content = _make_schematic(inst, lib_symbols=lib_sym)
        sch_path = _write_sch(tmp_path, content)

        result = validate_schematic(sch_path)
        assert "lib_symbols_mismatch" in result.checks_run

    def test_check_skippable(self, tmp_path):
        """lib_symbols_mismatch can be skipped via skip_checks."""
        pins = [("1", "A", "passive")]
        inst = _make_symbol_instance(
            "U1", "IC:Missing", pins, footprint="Package_SO:SOIC-8_3.9x4.9mm"
        )
        content = _make_schematic(inst, lib_symbols="")
        sch_path = _write_sch(tmp_path, content)

        result = validate_schematic(sch_path, skip_checks={"lib_symbols_mismatch"})
        assert "lib_symbols_mismatch" not in result.checks_run
        # Should have no lib_symbols_mismatch errors since check was skipped
        mismatch_errors = [i for i in result.issues if i.category == "lib_symbols_mismatch"]
        assert len(mismatch_errors) == 0
