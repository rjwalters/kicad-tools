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

import pytest

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


# --- --lvs-only mode (issue #3779) -----------------------------------------
#
# Boards 06/07 carry allowlisted DRC residuals -> board.json is
# status='partial' / drc_violations>0.  The full asserter would reject those
# fields; --lvs-only asserts only artifact presence + lvs.json clean=true
# (+ board.json lvs_clean=true), so a copper-LVS-clean but DRC-partial board
# passes while a copper-dirty board still fails.


def _stage_partial_board(root: Path, stem: str) -> Path:
    """Build a staging dir mimicking boards 06/07: lvs clean, DRC partial."""
    helper = _load_helper()
    board_dir = root / "board"
    output = board_dir / "output"
    (output / "manufacturing").mkdir(parents=True)
    for rel in helper.required_artifacts(stem):
        p = output / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("placeholder")
    (output / "lvs.json").write_text(json.dumps({"clean": True, "mismatches": []}))
    # status='partial' + drc_violations>0 (allowlisted residuals), but
    # copper-LVS clean -> lvs_clean=True.
    (output / "board.json").write_text(
        json.dumps({"status": "partial", "drc_violations": 18, "lvs_clean": True})
    )
    return board_dir


def test_lvs_only_passes_partial_board(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_partial_board(tmp_path, "diffpair_test")
    # --lvs-only must NOT reject status='partial' / drc_violations=18.
    rc = helper.main([str(board_dir), "--stem", "diffpair_test", "--lvs-only"])
    assert rc == 0


def test_full_asserter_rejects_partial_board(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_partial_board(tmp_path, "diffpair_test")
    # Without --lvs-only, the same partial board fails on status/drc_violations.
    rc = helper.main([str(board_dir), "--stem", "diffpair_test"])
    assert rc == 2


def test_lvs_only_still_fails_on_dirty_lvs(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_partial_board(tmp_path, "diffpair_test")
    (board_dir / "output" / "lvs.json").write_text(
        json.dumps(
            {
                "clean": False,
                "mismatches": [],
                "copper_mismatches": [
                    {"kind": "short", "net_a": "A", "net_b": "B", "pad_a": "U1.1", "pad_b": "U2.1"}
                ],
            }
        )
    )
    rc = helper.main([str(board_dir), "--stem", "diffpair_test", "--lvs-only"])
    assert rc == 2


def test_lvs_only_rejects_board_json_lvs_clean_false(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_partial_board(tmp_path, "diffpair_test")
    # board.json lvs_clean=False is still gated even in --lvs-only mode.
    (board_dir / "output" / "board.json").write_text(
        json.dumps({"status": "partial", "drc_violations": 18, "lvs_clean": False})
    )
    rc = helper.main([str(board_dir), "--stem", "diffpair_test", "--lvs-only"])
    assert rc == 2


# --- --lvs-not-run / --lvs-vacuous modes (#4006) ----------------------------
#
# Unwired fixture schematics make LVS unavailable: the copper comparator
# binds zero pins and any 'clean' verdict is zero-evidence (PR #4005
# review).  Board 06's recipe now SKIPS the LVS step (no lvs.json at all:
# --lvs-not-run) while board 07's still emits a vacuity-guard-marked
# lvs.json (--lvs-vacuous).  Both modes also require board.json to OMIT
# lvs_clean ("LVS not run" on the gallery).


def _stage_lvs_not_run_board(root: Path, stem: str) -> Path:
    """Staging dir mimicking board 06 post-#4006: no lvs.json at all."""
    helper = _load_helper()
    board_dir = root / "board"
    output = board_dir / "output"
    (output / "manufacturing").mkdir(parents=True)
    for rel in helper.required_artifacts(stem):
        if rel == "lvs.json":
            continue
        p = output / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text("placeholder")
    (output / "board.json").write_text(json.dumps({"status": "partial", "drc_violations": 18}))
    return board_dir


def _stage_lvs_vacuous_board(root: Path, stem: str) -> Path:
    """Staging dir mimicking board 07 post-#4006: vacuous lvs.json emitted."""
    board_dir = _stage_lvs_not_run_board(root, stem)
    (board_dir / "output" / "lvs.json").write_text(
        json.dumps(
            {
                "clean": False,
                "mismatches": [],
                "copper_mismatches": [
                    {
                        "kind": "vacuous",
                        "net_a": "<no-schematic-evidence>",
                        "net_b": "<no-schematic-evidence>",
                        "pad_a": "bound_pads=0",
                        "pad_b": "board_pads=223",
                    }
                ],
                "copper_vacuous": True,
                "copper_bound_pad_count": 0,
            }
        )
    )
    return board_dir


def test_lvs_not_run_passes_honest_state(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_lvs_not_run_board(tmp_path, "diffpair_test")
    rc = helper.main([str(board_dir), "--stem", "diffpair_test", "--lvs-not-run"])
    assert rc == 0


def test_lvs_not_run_rejects_present_lvs_json(tmp_path: Path) -> None:
    # Any emitted lvs.json (even a "clean" one) is zero-evidence on an
    # unwired fixture board -- its presence must trip the gate.
    helper = _load_helper()
    board_dir = _stage_lvs_not_run_board(tmp_path, "diffpair_test")
    (board_dir / "output" / "lvs.json").write_text(json.dumps({"clean": True, "mismatches": []}))
    rc = helper.main([str(board_dir), "--stem", "diffpair_test", "--lvs-not-run"])
    assert rc == 2


def test_lvs_not_run_rejects_board_json_claiming_lvs_clean(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_lvs_not_run_board(tmp_path, "diffpair_test")
    (board_dir / "output" / "board.json").write_text(
        json.dumps({"status": "partial", "drc_violations": 18, "lvs_clean": True})
    )
    rc = helper.main([str(board_dir), "--stem", "diffpair_test", "--lvs-not-run"])
    assert rc == 2


def test_lvs_vacuous_passes_honest_state(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_lvs_vacuous_board(tmp_path, "matchgroup_test")
    rc = helper.main([str(board_dir), "--stem", "matchgroup_test", "--lvs-vacuous"])
    assert rc == 0


def test_lvs_vacuous_rejects_clean_true(tmp_path: Path) -> None:
    # clean=true under --lvs-vacuous means the vacuity guard regressed.
    helper = _load_helper()
    board_dir = _stage_lvs_vacuous_board(tmp_path, "matchgroup_test")
    (board_dir / "output" / "lvs.json").write_text(json.dumps({"clean": True, "mismatches": []}))
    rc = helper.main([str(board_dir), "--stem", "matchgroup_test", "--lvs-vacuous"])
    assert rc == 2


def test_lvs_vacuous_rejects_non_vacuous_dirty(tmp_path: Path) -> None:
    # A real (non-vacuous) dirty report means the schematic binds pins now;
    # the job must graduate to --lvs-only instead of blessing the mismatch.
    helper = _load_helper()
    board_dir = _stage_lvs_vacuous_board(tmp_path, "matchgroup_test")
    (board_dir / "output" / "lvs.json").write_text(
        json.dumps(
            {
                "clean": False,
                "mismatches": [],
                "copper_mismatches": [
                    {"kind": "open", "net_a": "N", "net_b": "N", "pad_a": "R1.1", "pad_b": "R2.1"}
                ],
            }
        )
    )
    rc = helper.main([str(board_dir), "--stem", "matchgroup_test", "--lvs-vacuous"])
    assert rc == 2


def test_lvs_vacuous_rejects_missing_lvs_json(tmp_path: Path) -> None:
    # --lvs-vacuous is for recipes that still EMIT lvs.json; absence is a
    # regression of that contract (the board-07 CI job asserts the file).
    helper = _load_helper()
    board_dir = _stage_lvs_vacuous_board(tmp_path, "matchgroup_test")
    (board_dir / "output" / "lvs.json").unlink()
    rc = helper.main([str(board_dir), "--stem", "matchgroup_test", "--lvs-vacuous"])
    assert rc == 2


def test_lvs_vacuous_rejects_board_json_claiming_lvs_clean(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_lvs_vacuous_board(tmp_path, "matchgroup_test")
    (board_dir / "output" / "board.json").write_text(
        json.dumps({"status": "partial", "drc_violations": 18, "lvs_clean": False})
    )
    rc = helper.main([str(board_dir), "--stem", "matchgroup_test", "--lvs-vacuous"])
    assert rc == 2


def test_lvs_modes_are_mutually_exclusive(tmp_path: Path) -> None:
    helper = _load_helper()
    board_dir = _stage_lvs_not_run_board(tmp_path, "diffpair_test")
    with pytest.raises(SystemExit):
        helper.main([str(board_dir), "--lvs-only", "--lvs-not-run"])


def test_default_mode_rejects_vacuous_clean_true_lvs_json(tmp_path: Path) -> None:
    # Belt-and-braces: even the default clean assertion refuses a clean=true
    # report that self-identifies as vacuous (#4006).
    helper = _load_helper()
    board_dir = _stage_clean_board(tmp_path, "voltage_divider")
    (board_dir / "output" / "lvs.json").write_text(
        json.dumps(
            {"clean": True, "mismatches": [], "copper_vacuous": True, "copper_bound_pad_count": 0}
        )
    )
    rc = helper.main([str(board_dir), "--stem", "voltage_divider"])
    assert rc == 2
