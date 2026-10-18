"""``spec_conformance`` coverage category (Issue #7292, PR-B of #7250)."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.spec_conformance import (
    check_spec_conformance,
    coverage_for,
)

REPO = Path(__file__).resolve().parent.parent
BOARD02 = REPO / "boards" / "02-charlieplex-led"
PCB_NAME = "charlieplex_3x3_routed.kicad_pcb"


@pytest.fixture
def board(tmp_path: Path) -> Path:
    """Scratch copy of board 02; returns the routed PCB path."""
    shutil.copytree(BOARD02 / "output", tmp_path / "output")
    shutil.copy(BOARD02 / "project.kct", tmp_path / "project.kct")
    return tmp_path / "output" / PCB_NAME


def _edit(pcb_path: Path, *pairs: tuple[str, str]) -> None:
    kct = pcb_path.parent.parent / "project.kct"
    text = kct.read_text()
    for old, new in pairs:
        assert old in text, old
        text = text.replace(old, new)
    kct.write_text(text)


def _run(pcb_path: Path, mfr: str | None = None):
    return check_spec_conformance(PCB.load(str(pcb_path)), pcb_path, mfr)


def _ids(results, severity: str) -> list[str]:
    return sorted(v.rule_id for v in results.violations if v.severity == severity)


def test_unmodified_board_has_no_findings(board: Path) -> None:
    assert _run(board, "jlcpcb").violations == []


def test_mutation_yields_two_errors_and_trace_warning(board: Path) -> None:
    _edit(
        board,
        ('width: "50mm"', 'width: "20mm"'),
        ('height: "55mm"', 'height: "20mm"'),
        ("preferred: 2", "preferred: 6"),
        ('min_trace: "0.2mm"', 'min_trace: "0.5mm"'),
    )
    results = _run(board, "jlcpcb")
    assert _ids(results, "error") == [
        "spec_conformance_dimensions",
        "spec_conformance_layers",
    ]
    assert _ids(results, "warning") == ["spec_conformance_min_trace"]


def test_rotated_outline_is_not_a_mismatch(board: Path) -> None:
    _edit(board, ('width: "50mm"', 'width: "55mm"'), ('height: "55mm"', 'height: "50mm"'))
    assert _run(board, "jlcpcb").violations == []


def test_layers_max_exceeded(board: Path) -> None:
    _edit(board, ("preferred: 2", "preferred: 2\n      max: 1"))
    results = _run(board, "jlcpcb")
    assert _ids(results, "error") == ["spec_conformance_layers"]
    assert "max" in results.violations[0].message


def test_target_fab_mismatch_is_warning(board: Path) -> None:
    results = _run(board, "jlcpcb-tier1")
    assert _ids(results, "warning") == ["spec_conformance_target_fab"]
    assert _ids(results, "error") == []


def test_no_spec_is_skipped_and_silent(board: Path) -> None:
    (board.parent.parent / "project.kct").unlink()
    assert coverage_for("spec_conformance", board).status == "skipped:no_project_kct"
    assert _run(board).violations == []


def test_spec_without_mechanical_does_not_check_dimensions(board: Path) -> None:
    _edit(board, ('width: "50mm"', 'width: "1mm"'))
    kct = board.parent.parent / "project.kct"
    text = kct.read_text().replace("  mechanical:\n    dimensions:\n", "  _x:\n    dimensions:\n")
    kct.write_text(text)
    assert "spec_conformance_dimensions" not in _ids(_run(board, "jlcpcb"), "error")


def test_unknown_requirement_key_makes_coverage_unknown(board: Path) -> None:
    _edit(board, ("  compliance:", "  board_size: 50x55\n  compliance:"))
    status = coverage_for("spec_conformance", board).status
    assert status.startswith("unknown:unread_spec_keys:")
    assert "requirements.board_size" in status


def test_category_is_not_blocking() -> None:
    from kicad_tools.validate.coverage import BLOCKING_CATEGORIES

    assert "spec_conformance" not in BLOCKING_CATEGORIES
    assert coverage_for("spec_conformance", None).blocking is False
