"""Tests for the ``fiducials_missing`` / ``board_id_missing`` rules and readiness wiring (#8532)."""

import argparse
from pathlib import Path

import pytest

from kicad_tools.cli.pcb_add_board_info import run_add_board_info
from kicad_tools.cli.pcb_add_fiducials import run_add_fiducials
from kicad_tools.cli.readiness_cmd import (
    ASSEMBLY_AFFECTING_RULES,
    _assembly_prep,
)
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.checker import DRCChecker
from kicad_tools.validate.rules.board_id import BoardIdRule
from kicad_tools.validate.rules.fiducials import FiducialsRule

pytest.importorskip("shapely")


def _board(*, layer="F.Cu", rev="B", extra="", part=True) -> str:
    edge = "".join(
        f'(gr_line (start {a} {b}) (end {c} {d}) (layer "Edge.Cuts") (stroke (width 0.1) (type default)))\n'
        for a, b, c, d in [(0, 0, 60, 0), (60, 0, 60, 40), (60, 40, 0, 40), (0, 40, 0, 0)]
    )
    fp = (
        f"""(footprint "Resistor_SMD:R_0402" (layer "{layer}") (uuid "fp-r1") (at 30 20)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd roundrect (at -0.5 0) (size 0.5 0.5) (layers "{layer}") (net 0 ""))
    (pad "2" smd roundrect (at 0.5 0) (size 0.5 0.5) (layers "{layer}") (net 0 "")))"""
        if part
        else ""
    )
    return f"""(kicad_pcb (version 20240108) (generator "test")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (title_block (title "Demo") (date "2026-11-07") (rev "{rev}"))
  (net 0 "")
  {edge}
  {fp}
  {extra}
)
"""


def _load(tmp_path: Path, text: str) -> tuple[Path, PCB]:
    p = tmp_path / "b.kicad_pcb"
    p.write_text(text)
    return p, PCB.load(str(p))


def test_rules_are_assembly_affecting():
    assert {"fiducials_missing", "board_id_missing"} <= ASSEMBLY_AFFECTING_RULES


def test_both_warn_on_bare_board(tmp_path):
    _, pcb = _load(tmp_path, _board())
    fid = FiducialsRule().check(pcb, None).violations
    bid = BoardIdRule().check(pcb, None).violations
    assert [(v.rule_id, v.severity) for v in fid] == [("fiducials_missing", "warning")]
    assert [(v.rule_id, v.severity) for v in bid] == [("board_id_missing", "warning")]


def test_clean_after_add_commands(tmp_path):
    p, _ = _load(tmp_path, _board())
    assert run_add_fiducials(p, sides="populated") == 0
    assert run_add_board_info(p, auto=True) == 0
    pcb = PCB.load(str(p))
    assert FiducialsRule().check(pcb, None).violations == []
    assert BoardIdRule().check(pcb, None).violations == []


def test_fiducials_only_on_unpopulated_side_do_not_count(tmp_path):
    p, _ = _load(tmp_path, _board())
    assert run_add_fiducials(p, sides="back") == 0
    pcb = PCB.load(str(p))
    found = FiducialsRule().check(pcb, None).violations
    assert [v.layer for v in found] == ["F.Cu"]


def test_single_sided_populated_back_only_needs_back(tmp_path):
    p, _ = _load(tmp_path, _board(layer="B.Cu"))
    assert run_add_fiducials(p, sides="back") == 0
    assert FiducialsRule().check(PCB.load(str(p)), None).violations == []


def test_unpopulated_board_raises_neither(tmp_path):
    _, pcb = _load(tmp_path, _board(part=False))
    assert FiducialsRule().check(pcb, None).violations == []
    assert BoardIdRule().check(pcb, None).violations == []


def test_board_id_requires_the_revision(tmp_path):
    text = (
        '(gr_text "hello" (at 5 5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))'
    )
    _, pcb = _load(tmp_path, _board(extra=text))
    assert len(BoardIdRule().check(pcb, None).violations) == 1
    text = '(gr_text "Demo revB" (at 5 5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))'
    _, pcb = _load(tmp_path, _board(extra=text))
    assert BoardIdRule().check(pcb, None).violations == []


def test_checker_methods_registered(tmp_path):
    assert "check_fiducials" in DRCChecker.CHECK_ALL_METHODS
    assert "check_board_id" in DRCChecker.CHECK_ALL_METHODS
    _, pcb = _load(tmp_path, _board())
    checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=2)
    assert checker.check_fiducials().violations[0].rule_id == "fiducials_missing"
    assert checker.check_board_id().violations[0].rule_id == "board_id_missing"


def test_assembly_prep_generate_verify_and_skip(tmp_path):
    p, _ = _load(tmp_path, _board())
    original = p.read_text()
    opts = argparse.Namespace(
        pcb=p, operation="verify", skip_fiducials=False, skip_board_info=False
    )
    # verify never mutates
    assert _assembly_prep(opts) == {
        "fiducials": "verify-not-modified",
        "board_info": "verify-not-modified",
    }
    assert p.read_text() == original
    # opt-outs are recorded and leave the board alone
    opts = argparse.Namespace(
        pcb=p, operation="generate", skip_fiducials=True, skip_board_info=True
    )
    assert _assembly_prep(opts) == {
        "fiducials": "skipped (--skip-fiducials)",
        "board_info": "skipped (--skip-board-info)",
    }
    assert p.read_text() == original
    # generate applies both
    opts = argparse.Namespace(
        pcb=p, operation="generate", skip_fiducials=False, skip_board_info=False
    )
    assert _assembly_prep(opts) == {"fiducials": "applied", "board_info": "applied"}
    pcb = PCB.load(str(p))
    assert FiducialsRule().check(pcb, None).violations == []
    assert BoardIdRule().check(pcb, None).violations == []


def _silk(text: str) -> str:
    return f'(gr_text "{text}" (at 5 5) (layer "F.SilkS") (effects (font (size 1 1) (thickness 0.15))))'


@pytest.mark.parametrize("text", ["batt", "swd data"])
def test_board_id_short_revision_is_not_a_substring_match(tmp_path, text):
    _, pcb = _load(tmp_path, _board(rev="A", extra=_silk(text)))
    assert len(BoardIdRule().check(pcb, None).violations) == 1


@pytest.mark.parametrize("text", ["REV A", "proj A 2026-01-01"])
def test_board_id_short_revision_whole_token_passes(tmp_path, text):
    _, pcb = _load(tmp_path, _board(rev="A", extra=_silk(text)))
    assert BoardIdRule().check(pcb, None).violations == []
