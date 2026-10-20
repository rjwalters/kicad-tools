"""BOM extraction for a sheet file placed more than once (issue #7616)."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.schema.bom import extract_bom

_FIXTURES = Path(__file__).parent / "fixtures" / "sheet_qualified_lvs"
_REPEAT_ROOT = _FIXTURES / "repeat_root.kicad_sch"
_SINGLE_ROOT = _FIXTURES / "root_sq.kicad_sch"


def test_repeated_sheet_lists_each_placement_with_its_own_reference():
    refs = sorted(i.reference for i in extract_bom(str(_REPEAT_ROOT)).items)
    # MCU_A keeps R1/R2; MCU_B is annotated R11/R12 (verified vs kicad-cli).
    assert refs == ["R1", "R11", "R12", "R2"]


def test_no_duplicate_designators_on_repeated_sheet():
    refs = [i.reference for i in extract_bom(str(_REPEAT_ROOT)).items]
    assert len(refs) == len(set(refs))


def test_singly_placed_sheets_unchanged():
    from kicad_tools.schema.bom import extract_bom_from_schematic
    from kicad_tools.schema.schematic import Schematic

    bom = extract_bom(str(_SINGLE_ROOT))
    assert bom.items  # fixture has components
    root_only = extract_bom_from_schematic(Schematic.load(str(_SINGLE_ROOT)))
    assert {i.reference for i in root_only} <= {i.reference for i in bom.items}
    refs = [i.reference for i in bom.items]
    assert len(refs) == len(set(refs))
