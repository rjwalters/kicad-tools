"""``kct validate --sync`` compares component values (issue #7150).

``NetlistValidator`` used to check only presence and nets, so a footprint
value that drifted from its schematic symbol (board 04 J1: ``SWD`` vs
``SWD-6``) still reported ``in_sync``.  A ``value_mismatch`` error now
covers refs present on both sides whose values are both non-empty.
"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest

from kicad_tools.cli.validate_sync_cmd import main as sync_main
from kicad_tools.validate.netlist import NetlistValidator
from tests.test_validate_hierarchical_sync import (
    HIERARCHICAL_PCB_MATCHING,
    HIERARCHICAL_ROOT_SCH,
)

BOARD00 = Path(__file__).resolve().parents[1] / "boards" / "00-simple-led" / "output"
SCH = BOARD00 / "simple_led.kicad_sch"
PCB = BOARD00 / "simple_led.kicad_pcb"

needs_board00 = pytest.mark.skipif(
    not (SCH.exists() and PCB.exists()), reason="board 00 artifacts not present"
)


def _board00(tmp_path: Path, old: str | None = None, new: str = "") -> tuple[Path, Path]:
    sch = tmp_path / SCH.name
    pcb = tmp_path / PCB.name
    shutil.copy(SCH, sch)
    text = PCB.read_text()
    if old is not None:
        assert old in text
        text = text.replace(old, new, 1)
    pcb.write_text(text)
    return sch, pcb


@needs_board00
def test_matching_values_are_in_sync(tmp_path: Path) -> None:
    sch, pcb = _board00(tmp_path)
    result = NetlistValidator(sch, pcb).validate()
    assert result.value_mismatches == []
    assert result.in_sync


@needs_board00
def test_single_value_difference_is_one_error(tmp_path: Path) -> None:
    sch, pcb = _board00(tmp_path, '(fp_text value "330"', '(fp_text value "470"')
    result = NetlistValidator(sch, pcb).validate()
    assert len(result.value_mismatches) == 1
    issue = result.value_mismatches[0]
    assert issue.is_error
    assert issue.reference == "R1"
    assert '"330"' in issue.message and '"470"' in issue.message
    assert not result.in_sync


@needs_board00
def test_empty_pcb_value_is_skipped(tmp_path: Path) -> None:
    sch, pcb = _board00(tmp_path, '(fp_text value "330"', '(fp_text value ""')
    result = NetlistValidator(sch, pcb).validate()
    assert result.value_mismatches == []


@needs_board00
def test_cli_exits_1_and_reports_value_mismatches(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    sch, pcb = _board00(tmp_path, '(fp_text value "330"', '(fp_text value "470"')

    assert sync_main([str(sch), str(pcb)]) == 1
    out = capsys.readouterr().out
    assert "VALUE MISMATCHES (1)" in out
    assert '"330"' in out and '"470"' in out

    assert sync_main([str(sch), str(pcb), "--format", "json"]) == 1
    data = json.loads(capsys.readouterr().out)
    assert data["summary"]["value_mismatches"] == 1
    assert [i["category"] for i in data["issues"]] == ["value_mismatch"]
    assert data["issues"][0]["reference"] == "R1"


def test_sub_sheet_value_is_compared(tmp_path: Path) -> None:
    pcb = tmp_path / "hier.kicad_pcb"
    pcb.write_text(HIERARCHICAL_PCB_MATCHING)
    assert NetlistValidator(HIERARCHICAL_ROOT_SCH, pcb).validate().value_mismatches == []

    # C2 lives on the nested sub-sheet (sub_a -> nested).
    assert '(property "Value" "10uF"' in HIERARCHICAL_PCB_MATCHING
    pcb.write_text(
        HIERARCHICAL_PCB_MATCHING.replace('(property "Value" "10uF"', '(property "Value" "22uF"', 1)
    )
    result = NetlistValidator(HIERARCHICAL_ROOT_SCH, pcb).validate()
    assert [i.reference for i in result.value_mismatches] == ["C2"]
    assert not result.in_sync
