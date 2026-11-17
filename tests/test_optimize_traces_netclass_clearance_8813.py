"""Issue #8813: the --drc-aware referee counts ``netclass_clearance_copper``."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.router.optimizer.pcb import _run_drc_error_count_by_category
from tests.router import test_authored_netclass_board as wall
from tests.test_netclass_clearance_copper import DETOUR, STRAIGHT, _board

RULE = "netclass_clearance_copper"


def _count(pcb: Path, *, with_project: bool = True) -> int:
    counts = _run_drc_error_count_by_category(
        pcb.read_text(),
        manufacturer="jlcpcb",
        layers=2,
        copper_oz=1.0,
        project_path=pcb if with_project else None,
    )
    return counts.get(RULE, 0)


def test_referee_counts_netclass_floor_violation(tmp_path: Path) -> None:
    assert _count(_board(tmp_path, STRAIGHT)) > 0


def test_referee_clean_detour_not_counted(tmp_path: Path) -> None:
    assert _count(_board(tmp_path, DETOUR)) == 0


def test_referee_without_project_path_is_noop(tmp_path: Path) -> None:
    assert _count(_board(tmp_path, STRAIGHT), with_project=False) == 0


def test_board_without_kicad_pro_is_noop(tmp_path: Path) -> None:
    pcb = _board(tmp_path, STRAIGHT)
    pcb.with_suffix(".kicad_pro").unlink()
    assert _count(pcb) == 0


def test_half_micron_shortfall_counted_and_tolerance_respected(tmp_path: Path) -> None:
    # STRAIGHT leaves a 0.45 mm gap.  Raise the HV floor to 0.5 um above it
    # (flagged), then to 0.05 um above it (inside DRC_TOLERANCE, kept).
    def count_for_floor(floor: float, name: str) -> int:
        d = tmp_path / name
        d.mkdir()
        project = wall.project("hv")
        for cls in project["net_settings"]["classes"]:
            if cls["name"] == "HV":
                cls["clearance"] = floor
        return _count(_board(d, STRAIGHT, project))

    assert count_for_floor(0.4505, "short") > 0
    assert count_for_floor(0.45005, "within_tol") == 0
