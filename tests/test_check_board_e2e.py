"""Tests for the generalized board end-to-end CI asserter (issue #3762).

``scripts/ci/check_board_00_e2e.py`` was board-00-only (its
``REQUIRED_ARTIFACTS`` hardcoded ``simple_led*``).  Issue #3762 parametrized
it with a ``--stem`` flag so it asserts artifact correctness for any board.
These tests cover:

* the default stem reproduces board 00's required-artifact list;
* a non-default stem (board 01's ``voltage_divider``) derives the right
  schematic/PCB names;
* ``main()`` exits 0 on a synthetic clean staging dir;
* ``main()`` exits 2 when ``lvs.json`` reports ``clean:false``.

The script is loaded via importlib (it lives under ``scripts/ci/`` outside
the installed package), mirroring ``tests/test_check_routed_drc_advisory.py``.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_board_00_e2e.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("check_board_00_e2e", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_board_00_e2e"] = module
    spec.loader.exec_module(module)
    return module


def _stage_clean_board(root: Path, stem: str) -> Path:
    """Build a synthetic ``<board_dir>/output/`` with all required artifacts."""
    helper = _load_helper()
    board_dir = root / "board"
    output = board_dir / "output"
    (output / "manufacturing").mkdir(parents=True)
    for rel in helper.required_artifacts(stem):
        p = output / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("placeholder")
    (output / "lvs.json").write_text(json.dumps({"clean": True, "mismatches": []}))
    (output / "board.json").write_text(
        json.dumps({"status": "ok", "drc_violations": 0, "lvs_clean": True})
    )
    return board_dir


def test_required_artifacts_default_stem_matches_board_00() -> None:
    helper = _load_helper()
    artifacts = helper.required_artifacts(helper.DEFAULT_STEM)
    assert "simple_led.kicad_sch" in artifacts
    assert "simple_led.kicad_pcb" in artifacts
    assert "simple_led_routed.kicad_pcb" in artifacts
    assert "lvs.json" in artifacts
    assert "manufacturing/manifest.json" in artifacts
    assert "board.json" in artifacts


def test_required_artifacts_board_01_stem() -> None:
    helper = _load_helper()
    artifacts = helper.required_artifacts("voltage_divider")
    assert "voltage_divider.kicad_sch" in artifacts
    assert "voltage_divider_routed.kicad_pcb" in artifacts
    assert "simple_led.kicad_sch" not in artifacts


def test_main_passes_on_clean_staging(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_clean_board(tmp_path, "voltage_divider")
    rc = helper.main([str(board_dir), "--stem", "voltage_divider"])
    assert rc == 0


def test_main_exits_2_on_dirty_lvs(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_clean_board(tmp_path, "voltage_divider")
    (board_dir / "output" / "lvs.json").write_text(
        json.dumps(
            {
                "clean": False,
                "mismatches": [{"ref": "D1", "pad": "1", "schematic_net": "A", "pcb_net": "B"}],
            }
        )
    )
    rc = helper.main([str(board_dir), "--stem", "voltage_divider"])
    assert rc == 2


def test_main_default_stem_still_works_for_board_00_shape(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_clean_board(tmp_path, helper.DEFAULT_STEM)
    # No --stem passed: must default to board 00's artifact set and pass.
    rc = helper.main([str(board_dir)])
    assert rc == 0
