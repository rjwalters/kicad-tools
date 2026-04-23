"""Tests for the sch re-annotate command.

Covers _parse_reference(), _extract_symbols_from_text(),
_apply_reference_rename(), run_re_annotate() with dry-run,
backup, prefix filtering, start-from, per-sheet mode,
multi-unit components, power symbol exclusion, and
hierarchical schematic traversal.
"""

import json
from pathlib import Path

import pytest

from kicad_tools.cli.sch_re_annotate import (
    _add_project_instance,
    _apply_reference_rename,
    _apply_uuid_reference_rename,
    _assign_numbers,
    _build_continuous_mapping,
    _detect_indent,
    _detect_project_info,
    _extract_symbols_from_text,
    _format_reference,
    _parse_reference,
    run_re_annotate,
)

# ---------------------------------------------------------------------------
# Minimal schematic content for testing
# ---------------------------------------------------------------------------

MINIMAL_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0402_1005Metric"
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R5"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0402_1005Metric"
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R5") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C3"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "C3") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Schematic with gaps: R1, R5 (gap), C3 (gap) -> should become R1, R2, C1
GAPPED_SCHEMATIC = MINIMAL_SCHEMATIC

# Schematic with power symbols that should be excluded
POWER_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R3"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R3") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 100 70 0)
\t\t(property "Reference" "#PWR01"
\t\t\t(at 100 76 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "GND"
\t\t\t(at 100 73 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 70 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "44444444-4444-4444-4444-444444444444")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "#PWR01") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Schematic without instances blocks (simpler format)
NO_INSTANCES_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(property "Reference" "R5"
\t\t\t(at 102 48 0)
\t\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 102 50 0)
\t\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0603_1608Metric"
\t\t\t(at 100 50 0)
\t\t\t(effects (font (size 1.27 1.27)) hide)
\t\t)
\t\t(property "Datasheet" "~"
\t\t\t(at 100 50 0)
\t\t\t(effects (font (size 1.27 1.27)) hide)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-r5-1")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-r5-2")
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(unit 1)
\t\t(in_bom yes)
\t\t(on_board yes)
\t\t(dnp no)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(property "Reference" "R10"
\t\t\t(at 102 78 0)
\t\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 102 80 0)
\t\t\t(effects (font (size 1.27 1.27)) (justify left))
\t\t)
\t\t(property "Footprint" "Resistor_SMD:R_0603_1608Metric"
\t\t\t(at 100 80 0)
\t\t\t(effects (font (size 1.27 1.27)) hide)
\t\t)
\t\t(property "Datasheet" "~"
\t\t\t(at 100 80 0)
\t\t\t(effects (font (size 1.27 1.27)) hide)
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-r10-1")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-r10-2")
\t\t)
\t)
)
"""

# Hierarchical schematic (parent with sub-sheet)
PARENT_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R3"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R3") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R7"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R7") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet
\t\t(at 150 50)
\t\t(size 20 20)
\t\t(property "Sheetname" "SubSheet"
\t\t\t(at 150 48 0)
\t\t)
\t\t(property "Sheetfile" "sub.kicad_sch"
\t\t\t(at 150 68 0)
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

CHILD_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "44444444-4444-4444-4444-444444444444")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 100 50 0)
\t\t(property "Reference" "C5"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1uF"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/33333333-3333-3333-3333-333333333333" (reference "C5") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R14"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "66666666-6666-6666-6666-666666666666")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/33333333-3333-3333-3333-333333333333" (reference "R14") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/33333333-3333-3333-3333-333333333333" (page "2"))
\t)
)
"""

# Already sequential schematic
SEQUENTIAL_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


# Schematic with unannotated components (multi-project scenario)
UNANNOTATED_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R?"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "R?") (unit 1))
\t\t\t)
\t\t\t(project "other-project"
\t\t\t\t(path "/aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa/00000000-0000-0000-0000-000000000001" (reference "R6") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:C")
\t\t(at 120 50 0)
\t\t(property "Reference" "C?"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "100nF"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "C?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


def _tabs_to_spaces(text: str, width: int = 2) -> str:
    """Convert tab-indented schematic text to space-indented."""
    lines = []
    for line in text.splitlines(True):
        # Count leading tabs
        stripped = line.lstrip('\t')
        n_tabs = len(line) - len(stripped)
        lines.append(' ' * (n_tabs * width) + stripped)
    return ''.join(lines)


# Space-indented variants of key test fixtures
SPACE_INDENTED_SCHEMATIC = _tabs_to_spaces(MINIMAL_SCHEMATIC)
SPACE_INDENTED_POWER_SCHEMATIC = _tabs_to_spaces(POWER_SCHEMATIC)
SPACE_INDENTED_NO_INSTANCES_SCHEMATIC = _tabs_to_spaces(NO_INSTANCES_SCHEMATIC)
SPACE_INDENTED_UNANNOTATED_SCHEMATIC = _tabs_to_spaces(UNANNOTATED_SCHEMATIC)
SPACE_INDENTED_SEQUENTIAL_SCHEMATIC = _tabs_to_spaces(SEQUENTIAL_SCHEMATIC)

# Mixed indentation: first symbol uses tabs, second uses spaces
MIXED_INDENTATION_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
  (symbol
    (lib_id "Device:R")
    (at 100 80 0)
    (property "Reference" "R5"
      (at 100 78 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Value" "4.7k"
      (at 100 82 0)
      (effects (font (size 1.27 1.27)))
    )
    (property "Footprint" ""
      (at 100 84 0)
      (effects (font (size 1.27 1.27)) (hide yes))
    )
    (uuid "22222222-2222-2222-2222-222222222222")
    (instances
      (project "test"
        (path "/" (reference "R5") (unit 1))
      )
    )
  )
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


def _write_sch(
    tmp_path: Path,
    content: str = MINIMAL_SCHEMATIC,
    name: str = "test.kicad_sch",
) -> Path:
    p = tmp_path / name
    p.write_text(content)
    return p


# ---------------------------------------------------------------------------
# _parse_reference() unit tests
# ---------------------------------------------------------------------------


class TestParseReference:
    def test_simple_resistor(self):
        assert _parse_reference("R1") == ("R", 1, "")

    def test_simple_capacitor(self):
        assert _parse_reference("C32") == ("C", 32, "")

    def test_multi_unit(self):
        assert _parse_reference("U1A") == ("U", 1, "A")

    def test_multi_unit_b(self):
        assert _parse_reference("U1B") == ("U", 1, "B")

    def test_power_symbol(self):
        assert _parse_reference("#PWR01") == ("#PWR", 1, "")

    def test_flag_symbol(self):
        assert _parse_reference("#FLG01") == ("#FLG", 1, "")

    def test_unannotated(self):
        assert _parse_reference("R?") == ("R", None, "")

    def test_large_number(self):
        assert _parse_reference("R100") == ("R", 100, "")

    def test_two_letter_prefix(self):
        assert _parse_reference("SW1") == ("SW", 1, "")


# ---------------------------------------------------------------------------
# _extract_symbols_from_text() unit tests
# ---------------------------------------------------------------------------


class TestExtractSymbols:
    def test_extracts_all_symbols(self):
        symbols = _extract_symbols_from_text(MINIMAL_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R1", "R5", "C3"}

    def test_extracts_positions(self):
        symbols = _extract_symbols_from_text(MINIMAL_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R1"]["position_x"] == 100
        assert by_ref["R1"]["position_y"] == 50
        assert by_ref["R5"]["position_y"] == 80

    def test_extracts_lib_id(self):
        symbols = _extract_symbols_from_text(MINIMAL_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R1"]["lib_id"] == "Device:R"
        assert by_ref["C3"]["lib_id"] == "Device:C"

    def test_extracts_prefix_and_number(self):
        symbols = _extract_symbols_from_text(MINIMAL_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R5"]["prefix"] == "R"
        assert by_ref["R5"]["number"] == 5
        assert by_ref["C3"]["prefix"] == "C"
        assert by_ref["C3"]["number"] == 3

    def test_power_symbols_extracted(self):
        symbols = _extract_symbols_from_text(POWER_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert "#PWR01" in refs

    def test_no_instances_format(self):
        symbols = _extract_symbols_from_text(NO_INSTANCES_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R5", "R10"}


# ---------------------------------------------------------------------------
# _apply_reference_rename() unit tests
# ---------------------------------------------------------------------------


class TestApplyRename:
    def test_renames_property(self):
        result = _apply_reference_rename(MINIMAL_SCHEMATIC, "R5", "R2")
        assert '"Reference" "R2"' in result
        assert '"Reference" "R5"' not in result
        # R1 should be unchanged
        assert '"Reference" "R1"' in result

    def test_renames_instances(self):
        result = _apply_reference_rename(MINIMAL_SCHEMATIC, "R5", "R2")
        assert '(reference "R2")' in result
        assert '(reference "R5")' not in result
        # R1 instance should be unchanged
        assert '(reference "R1")' in result

    def test_no_match_unchanged(self):
        result = _apply_reference_rename(MINIMAL_SCHEMATIC, "U99", "U1")
        assert result == MINIMAL_SCHEMATIC


# ---------------------------------------------------------------------------
# _assign_numbers() unit tests
# ---------------------------------------------------------------------------


def _old_new_map(uuid_mapping: dict[str, dict]) -> dict[str, str]:
    """Convert UUID-keyed mapping to old->new for simpler test assertions."""
    return {info["old"]: info["new"] for info in uuid_mapping.values()}


class TestAssignNumbers:
    def test_closes_gaps(self):
        symbols = [
            {"reference": "R1", "prefix": "R", "number": 1, "unit_suffix": ""},
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
            {"reference": "C3", "prefix": "C", "number": 3, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 1))
        assert mapping == {"R1": "R1", "R5": "R2", "C3": "C1"}

    def test_prefix_filter(self):
        symbols = [
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
            {"reference": "C3", "prefix": "C", "number": 3, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, ["R"], 1))
        assert mapping == {"R5": "R1"}
        assert "C3" not in mapping

    def test_start_from(self):
        symbols = [
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 100))
        assert mapping == {"R5": "R100"}

    def test_multi_unit_shares_number(self):
        symbols = [
            {"reference": "U1A", "prefix": "U", "number": 1, "unit_suffix": "A"},
            {"reference": "U1B", "prefix": "U", "number": 1, "unit_suffix": "B"},
            {"reference": "U5A", "prefix": "U", "number": 5, "unit_suffix": "A"},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 1))
        assert mapping["U1A"] == "U1A"
        assert mapping["U1B"] == "U1B"
        assert mapping["U5A"] == "U2A"

    def test_excludes_power_symbols(self):
        symbols = [
            {"reference": "#PWR01", "prefix": "#PWR", "number": 1, "unit_suffix": ""},
            {"reference": "R3", "prefix": "R", "number": 3, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 1))
        assert "#PWR01" not in mapping
        assert mapping["R3"] == "R1"

    def test_annotates_unannotated(self):
        """Unannotated refs (R?) should be assigned numbers."""
        symbols = [
            {"reference": "R?", "prefix": "R", "number": None, "unit_suffix": "",
             "uuid": "uuid-r-question"},
            {"reference": "R3", "prefix": "R", "number": 3, "unit_suffix": ""},
        ]
        raw = _assign_numbers(symbols, None, 1)
        mapping = _old_new_map(raw)
        assert mapping["R?"] == "R1"
        assert mapping["R3"] == "R2"
        # Verify the unannotated flag is set
        assert raw["uuid-r-question"]["unannotated"] is True

    def test_multiple_unannotated_get_unique_numbers(self):
        """Multiple R? components should each get a unique number."""
        symbols = [
            {"reference": "R?", "prefix": "R", "number": None, "unit_suffix": "",
             "uuid": "uuid-r1"},
            {"reference": "R?", "prefix": "R", "number": None, "unit_suffix": "",
             "uuid": "uuid-r2"},
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
        ]
        raw = _assign_numbers(symbols, None, 1)
        new_refs = {info["new"] for info in raw.values()}
        assert new_refs == {"R1", "R2", "R3"}


# ---------------------------------------------------------------------------
# run_re_annotate() integration tests
# ---------------------------------------------------------------------------


class TestRunReAnnotate:
    def test_dry_run_does_not_modify(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(schematic_path=sch, dry_run=True, backup=False)
        assert ret == 0
        assert sch.read_text() == original

    def test_closes_gaps(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # R1 stays R1, R5 becomes R2, C3 becomes C1
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "C1"' in text
        # Old refs should be gone
        assert '"Reference" "R5"' not in text
        assert '"Reference" "C3"' not in text

    def test_updates_instances(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '(reference "R2")' in text
        assert '(reference "C1")' in text
        assert '(reference "R5")' not in text
        assert '(reference "C3")' not in text

    def test_creates_backup(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=True)
        assert ret == 0
        backups = list(tmp_path.glob("test_backup_*"))
        assert len(backups) == 1

    def test_prefix_filter(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False, prefixes=["R"]
        )
        assert ret == 0
        text = sch.read_text()
        # R5 -> R2 (renumbered)
        assert '"Reference" "R2"' in text
        # C3 should stay unchanged (not in prefix filter)
        assert '"Reference" "C3"' in text

    def test_start_from(self, tmp_path):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False, start_from=10
        )
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R10"' in text
        assert '"Reference" "R11"' in text
        assert '"Reference" "C10"' in text

    def test_already_sequential(self, tmp_path):
        sch = _write_sch(tmp_path, SEQUENTIAL_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        # File should be unchanged (no effective renames)
        assert sch.read_text() == original

    def test_power_symbols_excluded(self, tmp_path):
        sch = _write_sch(tmp_path, POWER_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # R3 -> R1
        assert '"Reference" "R1"' in text
        # Power symbol should be unchanged
        assert '"Reference" "#PWR01"' in text

    def test_missing_schematic(self, tmp_path):
        ret = run_re_annotate(
            schematic_path=tmp_path / "nonexistent.kicad_sch"
        )
        assert ret == 1

    def test_json_output(self, tmp_path, capsys):
        sch = _write_sch(tmp_path, GAPPED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=True, backup=False, format="json"
        )
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["dry_run"] is True
        assert data["total"] > 0

    def test_no_instances_format(self, tmp_path):
        sch = _write_sch(tmp_path, NO_INSTANCES_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # R5 -> R1, R10 -> R2
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "R5"' not in text
        assert '"Reference" "R10"' not in text


# ---------------------------------------------------------------------------
# Hierarchical schematic support
# ---------------------------------------------------------------------------


class TestHierarchicalReAnnotate:
    def test_continuous_across_sheets(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        ret = run_re_annotate(
            schematic_path=parent, dry_run=False, backup=False
        )
        assert ret == 0

        parent_text = parent.read_text()
        child_text = child.read_text()

        # Parent: R3 -> R1, R7 -> R2
        assert '"Reference" "R1"' in parent_text
        assert '"Reference" "R2"' in parent_text

        # Child: R14 -> R3, C5 -> C1
        assert '"Reference" "R3"' in child_text
        assert '"Reference" "C1"' in child_text

    def test_per_sheet_mode(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        ret = run_re_annotate(
            schematic_path=parent, dry_run=False, backup=False, per_sheet=True
        )
        assert ret == 0

        parent_text = parent.read_text()
        child_text = child.read_text()

        # Parent: R3 -> R1, R7 -> R2
        assert '"Reference" "R1"' in parent_text
        assert '"Reference" "R2"' in parent_text

        # Child (per-sheet restart): R14 -> R1, C5 -> C1
        assert '"Reference" "R1"' in child_text
        assert '"Reference" "C1"' in child_text

    def test_dry_run_hierarchical(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)
        original_parent = parent.read_text()
        original_child = child.read_text()

        ret = run_re_annotate(
            schematic_path=parent, dry_run=True, backup=False
        )
        assert ret == 0
        assert parent.read_text() == original_parent
        assert child.read_text() == original_child

    def test_backup_hierarchical(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(PARENT_SCHEMATIC)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(CHILD_SCHEMATIC)

        ret = run_re_annotate(
            schematic_path=parent, dry_run=False, backup=True
        )
        assert ret == 0
        # Both files should have backups
        backups = list(tmp_path.glob("*_backup_*"))
        assert len(backups) == 2


# ---------------------------------------------------------------------------
# Unannotated component support
# ---------------------------------------------------------------------------


class TestUnannotatedComponents:
    def test_annotates_unannotated_refs(self, tmp_path):
        """R? and C? should get assigned numbers."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # R1 stays R1, R? becomes R2, C? becomes C1
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "C1"' in text
        # No more unannotated refs
        assert '"Reference" "R?"' not in text
        assert '"Reference" "C?"' not in text

    def test_adds_project_instance(self, tmp_path):
        """Unannotated components should get project instance entries."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        # Should have a project instance for the test project
        assert '(project "test"' in text
        # The new ref should appear in an instance entry
        assert '(reference "R2")' in text
        assert '(reference "C1")' in text

    def test_preserves_other_project_instances(self, tmp_path):
        """Existing project instances (other-project) should be preserved."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '(project "other-project"' in text
        assert '(reference "R6")' in text

    def test_dry_run_shows_unannotated(self, tmp_path, capsys):
        """Dry run should show unannotated components in the mapping."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=True, backup=False, format="json"
        )
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        # Should include mappings for unannotated refs
        old_refs = {m["old"] for m in data["mappings"]}
        assert "R?" in old_refs
        assert "C?" in old_refs

    def test_extract_uuid(self):
        """_extract_symbols_from_text should extract UUIDs."""
        symbols = _extract_symbols_from_text(UNANNOTATED_SCHEMATIC)
        by_uuid = {s["uuid"]: s for s in symbols}
        assert "11111111-1111-1111-1111-111111111111" in by_uuid
        assert "22222222-2222-2222-2222-222222222222" in by_uuid
        assert by_uuid["22222222-2222-2222-2222-222222222222"]["reference"] == "R?"


# ---------------------------------------------------------------------------
# Space-indented schematic support
# ---------------------------------------------------------------------------


class TestDetectIndent:
    def test_detects_tabs(self):
        assert _detect_indent(MINIMAL_SCHEMATIC) == '\t'

    def test_detects_spaces(self):
        assert _detect_indent(SPACE_INDENTED_SCHEMATIC) == '  '

    def test_detects_four_spaces(self):
        text = _tabs_to_spaces(MINIMAL_SCHEMATIC, width=4)
        assert _detect_indent(text) == '    '


class TestSpaceIndentedExtractSymbols:
    def test_extracts_all_symbols(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R1", "R5", "C3"}

    def test_extracts_positions(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R1"]["position_x"] == 100
        assert by_ref["R1"]["position_y"] == 50
        assert by_ref["R5"]["position_y"] == 80

    def test_extracts_lib_id(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_SCHEMATIC)
        by_ref = {s["reference"]: s for s in symbols}
        assert by_ref["R1"]["lib_id"] == "Device:R"
        assert by_ref["C3"]["lib_id"] == "Device:C"

    def test_power_symbols_extracted(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_POWER_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert "#PWR01" in refs

    def test_no_instances_format(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_NO_INSTANCES_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R5", "R10"}

    def test_unannotated_symbols(self):
        symbols = _extract_symbols_from_text(SPACE_INDENTED_UNANNOTATED_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert "R?" in refs
        assert "C?" in refs
        assert "R1" in refs


class TestSpaceIndentedRunReAnnotate:
    def test_closes_gaps(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "C1"' in text
        assert '"Reference" "R5"' not in text
        assert '"Reference" "C3"' not in text

    def test_updates_instances(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '(reference "R2")' in text
        assert '(reference "C1")' in text

    def test_already_sequential(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_SEQUENTIAL_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        assert sch.read_text() == original

    def test_power_symbols_excluded(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_POWER_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "#PWR01"' in text

    def test_no_instances_format(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_NO_INSTANCES_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text

    def test_annotates_unannotated_refs(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "C1"' in text
        assert '"Reference" "R?"' not in text
        assert '"Reference" "C?"' not in text


class TestSpaceIndentedApplyUuidRename:
    def test_renames_in_space_indented_file(self):
        text = _apply_uuid_reference_rename(
            SPACE_INDENTED_SCHEMATIC,
            "22222222-2222-2222-2222-222222222222",
            "R99",
        )
        assert '"Reference" "R99"' in text
        # R1 should be unchanged
        assert '"Reference" "R1"' in text

    def test_renames_in_tab_indented_file(self):
        text = _apply_uuid_reference_rename(
            MINIMAL_SCHEMATIC,
            "22222222-2222-2222-2222-222222222222",
            "R99",
        )
        assert '"Reference" "R99"' in text
        assert '"Reference" "R1"' in text


class TestSpaceIndentedDetectProjectInfo:
    def test_finds_root_uuid_in_space_indented(self, tmp_path):
        sch = _write_sch(tmp_path, SPACE_INDENTED_SCHEMATIC)
        project_name, root_uuid, file_paths = _detect_project_info(sch, [sch])
        assert root_uuid == "00000000-0000-0000-0000-000000000001"
        assert project_name == "test"

    def test_finds_root_uuid_in_tab_indented(self, tmp_path):
        sch = _write_sch(tmp_path, MINIMAL_SCHEMATIC)
        project_name, root_uuid, file_paths = _detect_project_info(sch, [sch])
        assert root_uuid == "00000000-0000-0000-0000-000000000001"


class TestSpaceIndentedAddProjectInstance:
    def test_adds_instance_with_space_indentation(self):
        text = _add_project_instance(
            SPACE_INDENTED_UNANNOTATED_SCHEMATIC,
            "33333333-3333-3333-3333-333333333333",
            "newproject",
            "/root-uuid",
            "C1",
        )
        assert '(project "newproject"' in text
        assert '(reference "C1")' in text
        # Instance entry should use space indentation matching the file
        assert '\t\t\t(project "newproject"' not in text


class TestMixedIndentation:
    def test_extracts_both_tab_and_space_symbols(self):
        symbols = _extract_symbols_from_text(MIXED_INDENTATION_SCHEMATIC)
        refs = {s["reference"] for s in symbols}
        assert refs == {"R1", "R5"}

    def test_run_re_annotate_on_mixed(self, tmp_path):
        sch = _write_sch(tmp_path, MIXED_INDENTATION_SCHEMATIC)
        ret = run_re_annotate(schematic_path=sch, dry_run=False, backup=False)
        assert ret == 0
        text = sch.read_text()
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        assert '"Reference" "R5"' not in text


class TestSpaceIndentedHierarchical:
    def test_continuous_across_space_indented_sheets(self, tmp_path):
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(_tabs_to_spaces(PARENT_SCHEMATIC))
        child = tmp_path / "sub.kicad_sch"
        child.write_text(_tabs_to_spaces(CHILD_SCHEMATIC))

        ret = run_re_annotate(
            schematic_path=parent, dry_run=False, backup=False
        )
        assert ret == 0

        parent_text = parent.read_text()
        child_text = child.read_text()

        # Parent: R3 -> R1, R7 -> R2
        assert '"Reference" "R1"' in parent_text
        assert '"Reference" "R2"' in parent_text

        # Child: R14 -> R3, C5 -> C1
        assert '"Reference" "R3"' in child_text
        assert '"Reference" "C1"' in child_text


# ---------------------------------------------------------------------------
# --unannotated-only mode tests
# ---------------------------------------------------------------------------

# Schematic with R1, R2, and R? -- R? should become R3 (avoiding 1 and 2)
COLLISION_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 65 0)
\t\t(property "Reference" "R2"
\t\t\t(at 100 63 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 67 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 69 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "22222222-2222-2222-2222-222222222222")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R2") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 80 0)
\t\t(property "Reference" "R?"
\t\t\t(at 100 78 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "1k"
\t\t\t(at 100 82 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 84 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "R?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


class TestUnannotatedOnly:
    def test_skips_assigned(self, tmp_path):
        """With --unannotated-only, R1 stays R1 and R? becomes R2."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # R1 must remain unchanged
        assert '"Reference" "R1"' in text
        # R? should be assigned R2 (next after R1)
        assert '"Reference" "R2"' in text
        # C? should be assigned C1
        assert '"Reference" "C1"' in text
        # No unannotated refs remain
        assert '"Reference" "R?"' not in text
        assert '"Reference" "C?"' not in text

    def test_avoids_collisions(self, tmp_path):
        """R1, R2 exist; R? must become R3, not R1 or R2."""
        sch = _write_sch(tmp_path, COLLISION_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # Existing refs unchanged
        assert '"Reference" "R1"' in text
        assert '"Reference" "R2"' in text
        # R? -> R3 (skipping 1 and 2)
        assert '"Reference" "R3"' in text
        assert '"Reference" "R?"' not in text

    def test_no_unannotated(self, tmp_path, capsys):
        """All refs already assigned -> no changes needed."""
        sch = _write_sch(tmp_path, SEQUENTIAL_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            unannotated_only=True,
        )
        assert ret == 0
        assert sch.read_text() == original
        captured = capsys.readouterr()
        assert "No references to renumber" in captured.out

    def test_with_prefix_filter(self, tmp_path):
        """--unannotated-only with --prefix R: only unannotated R refs assigned."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            prefixes=["R"], unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # R? should be assigned
        assert '"Reference" "R?"' not in text
        assert '"Reference" "R2"' in text
        # C? should remain unannotated (prefix filter excludes C)
        assert '"Reference" "C?"' in text

    def test_with_start_from(self, tmp_path):
        """--unannotated-only with --start-from should respect start value."""
        sch = _write_sch(tmp_path, UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            start_from=10, unannotated_only=True,
        )
        assert ret == 0
        text = sch.read_text()
        # R1 stays R1 (already assigned)
        assert '"Reference" "R1"' in text
        # R? should become R10 (start_from=10, no collision with R1)
        assert '"Reference" "R10"' in text
        # C? should become C10
        assert '"Reference" "C10"' in text

    def test_assign_numbers_unit_level(self):
        """_assign_numbers with unannotated_only preserves existing refs."""
        symbols = [
            {"reference": "R1", "prefix": "R", "number": 1, "unit_suffix": ""},
            {"reference": "R5", "prefix": "R", "number": 5, "unit_suffix": ""},
            {"reference": "R?", "prefix": "R", "number": None, "unit_suffix": "",
             "uuid": "uuid-rq"},
        ]
        raw = _assign_numbers(symbols, None, 1, unannotated_only=True)
        # Only the unannotated ref should be in the mapping
        assert len(raw) == 1
        assert raw["uuid-rq"]["old"] == "R?"
        # Should get R2 (1 and 5 are reserved, next available from 1 is 2)
        assert raw["uuid-rq"]["new"] == "R2"
        assert raw["uuid-rq"]["unannotated"] is True

    def test_per_sheet_mode(self, tmp_path):
        """--unannotated-only with --per-sheet scopes reserved numbers per sheet."""
        # Create a parent with R1 assigned and a child with R? unannotated
        parent_sch = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet
\t\t(at 150 50)
\t\t(size 20 20)
\t\t(property "Sheetname" "SubSheet"
\t\t\t(at 150 48 0)
\t\t)
\t\t(property "Sheetfile" "sub.kicad_sch"
\t\t\t(at 150 68 0)
\t\t)
\t\t(uuid "33333333-3333-3333-3333-333333333333")
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""
        child_sch = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "44444444-4444-4444-4444-444444444444")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R?"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "4.7k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "55555555-5555-5555-5555-555555555555")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001/33333333-3333-3333-3333-333333333333" (reference "R?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/33333333-3333-3333-3333-333333333333" (page "2"))
\t)
)
"""
        parent = tmp_path / "parent.kicad_sch"
        parent.write_text(parent_sch)
        child = tmp_path / "sub.kicad_sch"
        child.write_text(child_sch)

        ret = run_re_annotate(
            schematic_path=parent, dry_run=False, backup=False,
            per_sheet=True, unannotated_only=True,
        )
        assert ret == 0

        parent_text = parent.read_text()
        child_text = child.read_text()

        # Parent: R1 stays R1 (already assigned, unannotated_only)
        assert '"Reference" "R1"' in parent_text

        # Child: R? gets assigned R1 (per-sheet restart, no reserved in child sheet)
        assert '"Reference" "R1"' in child_text
        assert '"Reference" "R?"' not in child_text


# ---------------------------------------------------------------------------
# --include-power flag tests
# ---------------------------------------------------------------------------

# Schematic with unannotated power and flag symbols
POWER_UNANNOTATED_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "Device:R")
\t\t(at 100 50 0)
\t\t(property "Reference" "R1"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "10k"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 54 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "11111111-1111-1111-1111-111111111111")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "R1") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "power:+5V")
\t\t(at 100 30 0)
\t\t(property "Reference" "#PWR?"
\t\t\t(at 100 26 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "+5V"
\t\t\t(at 100 23 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 30 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "#PWR?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "power:PWR_FLAG")
\t\t(at 120 30 0)
\t\t(property "Reference" "#FLG?"
\t\t\t(at 120 26 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "PWR_FLAG"
\t\t\t(at 120 23 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 120 30 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "bbbb1111-bbbb-bbbb-bbbb-bbbbbbbbbbbb")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "#FLG?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""

# Schematic with existing #PWR01 and unannotated #PWR?
POWER_COLLISION_SCHEMATIC = """\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "00000000-0000-0000-0000-000000000001")
\t(paper "A4")
\t(lib_symbols
\t)
\t(symbol
\t\t(lib_id "power:GND")
\t\t(at 100 70 0)
\t\t(property "Reference" "#PWR01"
\t\t\t(at 100 76 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "GND"
\t\t\t(at 100 73 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 70 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "44444444-4444-4444-4444-444444444444")
\t\t(instances
\t\t\t(project "test"
\t\t\t\t(path "/" (reference "#PWR01") (unit 1))
\t\t\t)
\t\t)
\t)
\t(symbol
\t\t(lib_id "power:+5V")
\t\t(at 100 30 0)
\t\t(property "Reference" "#PWR?"
\t\t\t(at 100 26 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(property "Value" "+5V"
\t\t\t(at 100 23 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Footprint" ""
\t\t\t(at 100 30 0)
\t\t\t(effects (font (size 1.27 1.27)) (hide yes))
\t\t)
\t\t(uuid "aaaa1111-aaaa-aaaa-aaaa-aaaaaaaaaaaa")
\t\t(instances
\t\t\t(project ""
\t\t\t\t(path "/00000000-0000-0000-0000-000000000001" (reference "#PWR?") (unit 1))
\t\t\t)
\t\t)
\t)
\t(sheet_instances
\t\t(path "/" (page "1"))
\t)
)
"""


class TestFormatReference:
    def test_normal_prefix(self):
        assert _format_reference("R", 1) == "R1"
        assert _format_reference("C", 10) == "C10"

    def test_power_prefix_zero_padded(self):
        assert _format_reference("#PWR", 1) == "#PWR01"
        assert _format_reference("#PWR", 10) == "#PWR10"
        assert _format_reference("#PWR", 99) == "#PWR99"
        assert _format_reference("#PWR", 100) == "#PWR100"

    def test_flag_prefix_zero_padded(self):
        assert _format_reference("#FLG", 1) == "#FLG01"
        assert _format_reference("#FLG", 12) == "#FLG12"

    def test_unit_suffix(self):
        assert _format_reference("U", 1, "A") == "U1A"
        assert _format_reference("#PWR", 1, "") == "#PWR01"


class TestIncludePower:
    def test_power_symbols_annotated_with_flag(self, tmp_path):
        """--include-power annotates #PWR? and #FLG? symbols."""
        sch = _write_sch(tmp_path, POWER_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            include_power=True,
        )
        assert ret == 0
        text = sch.read_text()
        # Power and flag symbols should be annotated with zero-padding
        assert '"Reference" "#PWR01"' in text
        assert '"Reference" "#FLG01"' in text
        assert '"Reference" "#PWR?"' not in text
        assert '"Reference" "#FLG?"' not in text
        # R1 should still be present (renumbered to R1)
        assert '"Reference" "R1"' in text

    def test_power_symbols_excluded_without_flag(self, tmp_path):
        """Without --include-power, #PWR? and #FLG? remain unannotated."""
        sch = _write_sch(tmp_path, POWER_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            include_power=False,
        )
        assert ret == 0
        text = sch.read_text()
        # Power/flag symbols should remain unannotated
        assert '"Reference" "#PWR?"' in text
        assert '"Reference" "#FLG?"' in text

    def test_power_annotation_collision_avoidance(self, tmp_path):
        """#PWR? avoids collision with existing #PWR01."""
        sch = _write_sch(tmp_path, POWER_COLLISION_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            unannotated_only=True, include_power=True,
        )
        assert ret == 0
        text = sch.read_text()
        # Existing #PWR01 should remain unchanged
        assert '"Reference" "#PWR01"' in text
        # #PWR? should become #PWR02 (avoiding collision with 01)
        assert '"Reference" "#PWR02"' in text
        assert '"Reference" "#PWR?"' not in text

    def test_power_annotation_zero_padding(self):
        """Number 1 produces #PWR01, number 10 produces #PWR10."""
        symbols = [
            {"reference": "#PWR?", "prefix": "#PWR", "number": None,
             "unit_suffix": "", "uuid": "uuid-pwr1"},
        ]
        raw = _assign_numbers(symbols, None, 1, include_power=True)
        assert raw["uuid-pwr1"]["new"] == "#PWR01"

        symbols2 = [
            {"reference": "#PWR?", "prefix": "#PWR", "number": None,
             "unit_suffix": "", "uuid": "uuid-pwr1"},
        ]
        raw2 = _assign_numbers(symbols2, None, 10, include_power=True)
        assert raw2["uuid-pwr1"]["new"] == "#PWR10"

    def test_include_power_without_unannotated_only(self, tmp_path):
        """Full renumber mode with --include-power re-sequences power symbols."""
        sch = _write_sch(tmp_path, POWER_COLLISION_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            include_power=True,
        )
        assert ret == 0
        text = sch.read_text()
        # Both power symbols should be sequentially numbered
        assert '"Reference" "#PWR01"' in text
        assert '"Reference" "#PWR02"' in text
        assert '"Reference" "#PWR?"' not in text

    def test_sym_prefix_always_excluded(self):
        """#SYM symbols are never annotated even with --include-power."""
        symbols = [
            {"reference": "#SYM1", "prefix": "#SYM", "number": 1,
             "unit_suffix": ""},
            {"reference": "R3", "prefix": "R", "number": 3, "unit_suffix": ""},
        ]
        mapping = _old_new_map(_assign_numbers(symbols, None, 1,
                                               include_power=True))
        assert "#SYM1" not in mapping
        assert mapping["R3"] == "R1"

    def test_json_output_includes_power_mappings(self, tmp_path, capsys):
        """--format json output includes power symbol mappings."""
        sch = _write_sch(tmp_path, POWER_UNANNOTATED_SCHEMATIC)
        ret = run_re_annotate(
            schematic_path=sch, dry_run=True, backup=False,
            format="json", include_power=True,
        )
        assert ret == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        new_refs = {m["new"] for m in data["mappings"]}
        assert "#PWR01" in new_refs
        assert "#FLG01" in new_refs

    def test_unannotated_only_without_include_power_skips_power(self, tmp_path):
        """--unannotated-only without --include-power skips #PWR? symbols."""
        sch = _write_sch(tmp_path, POWER_UNANNOTATED_SCHEMATIC)
        original = sch.read_text()
        ret = run_re_annotate(
            schematic_path=sch, dry_run=False, backup=False,
            unannotated_only=True, include_power=False,
        )
        assert ret == 0
        text = sch.read_text()
        # Power symbols remain unannotated
        assert '"Reference" "#PWR?"' in text
        assert '"Reference" "#FLG?"' in text
