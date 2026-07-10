"""Unit guard: board 04 main() success gate reflects DRC + copper-LVS (#3839).

The board-04 recipe's ``main()`` return gate previously omitted the
already-computed ``drc_success`` and discarded ``write_lvs_report``'s
``(copper_clean, label_clean)`` return -- so a board with a NEW blocking DRC
error or a copper-LVS short printed SUCCESS and exited 0.  Issue #3839 wires
both into the gate.  ``run_drc`` is now ALLOWLIST-AWARE: it parses the DRC
JSON and tolerates exactly the grandfathered violations the CI gate tolerates.
Since Issue #4017 re-spaced the LQFP-48 west-escape drill pair, the
``dimension_drill_clearance`` allowance is strict-0 -- the only remaining
grandfathered rule is the advisory ``connectivity`` finding; ANY drill error
or any other blocking rule fails the gate.

These tests are hermetic: they monkeypatch ``subprocess.run`` (for
``run_drc``) and ``write_lvs_report`` (for the LVS leg) so no router /
kicad-cli invocation is needed.  They exercise the gate's two halves
directly without re-routing the board.

References:
- ``boards/04-stm32-devboard/generate_design.py`` -- the gate under test.
- ``.github/routed-drc-tolerance.yml`` -- the board-04 strict-0 gate (entry
  removed by #4017; absence == strict-0 per that file's convention).
- ``tests/test_board_04_mfr_gate.py`` -- the committed-artifact DRC pin.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import types
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "04-stm32-devboard"


def _load_board04_module() -> types.ModuleType:
    """Import the board-04 ``generate_design.py`` as a module."""
    gen = BOARD_DIR / "generate_design.py"
    spec = importlib.util.spec_from_file_location("board04_generate_design", gen)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def board04() -> types.ModuleType:
    return _load_board04_module()


def _drc_payload(rule_ids: list[str]) -> dict:
    """Build a minimal ``kct check --format json`` payload from rule ids."""
    violations = [
        {"rule_id": rid, "type": rid, "severity": "error", "message": f"{rid} sample"}
        for rid in rule_ids
    ]
    return {
        "summary": {"errors": len(violations), "passed": not violations},
        "violations": violations,
    }


def _patch_run_drc(
    board04: types.ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    rule_ids: list[str],
) -> None:
    """Make ``run_drc``'s subprocess emit a fabricated DRC report JSON."""
    payload = _drc_payload(rule_ids)

    def fake_run(cmd, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        # ``run_drc`` writes the report via --output; honour that contract so
        # the function's file-first parse path is exercised.
        out_idx = cmd.index("--output")
        report_path = Path(cmd[out_idx + 1])
        report_path.parent.mkdir(parents=True, exist_ok=True)
        report_path.write_text(json.dumps(payload))
        returncode = 2 if payload["violations"] else 0
        return subprocess.CompletedProcess(cmd, returncode, stdout=json.dumps(payload), stderr="")

    monkeypatch.setattr(board04.subprocess, "run", fake_run)


# --------------------------------------------------------------------------
# run_drc allowlist-awareness
# --------------------------------------------------------------------------


def test_run_drc_passes_with_only_advisory_connectivity(
    board04: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The current committed board: 0 drills + 1 advisory connectivity -> PASS.

    Post-#4017 the LQFP-48 west-escape drill pair is re-spaced to
    >= 0.500mm, so the drill-clearance allowance is strict-0.  The only
    grandfathered finding left is the advisory ``connectivity`` GND-stitch
    pad, which is excluded from the gate.
    """
    _patch_run_drc(
        board04,
        monkeypatch,
        tmp_path,
        ["connectivity"],
    )
    pcb = tmp_path / "stm32_devboard_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert board04.run_drc(pcb) is True


def test_run_drc_fails_on_any_drill(
    board04: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any drill-clearance error exceeds the strict-0 allowance (#4017) -> FAIL."""
    _patch_run_drc(
        board04,
        monkeypatch,
        tmp_path,
        ["dimension_drill_clearance"],
    )
    pcb = tmp_path / "stm32_devboard_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert board04.run_drc(pcb) is False


def test_run_drc_fails_on_non_drill_blocking_rule(
    board04: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Any non-drill, non-advisory blocking rule -> FAIL even within drill allowance."""
    _patch_run_drc(
        board04,
        monkeypatch,
        tmp_path,
        ["dimension_drill_clearance", "clearance_segment_zone"],
    )
    pcb = tmp_path / "stm32_devboard_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert board04.run_drc(pcb) is False


def test_run_drc_passes_clean_board(
    board04: types.ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A fully clean board (0 violations) -> PASS."""
    _patch_run_drc(board04, monkeypatch, tmp_path, [])
    pcb = tmp_path / "stm32_devboard_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert board04.run_drc(pcb) is True


def test_run_drc_drill_allowance_matches_yaml() -> None:
    """The recipe's drill allowance must equal the CI tolerance floor (#3847/#4017).

    Since #4017 re-spaced the drill pair, the board-04 entry is REMOVED from
    the tolerance YAML -- absence means strict-0 per that file's convention --
    so the effective floor is 0 and ``_DRILL_CLEARANCE_ALLOWANCE`` must match.
    """
    import yaml

    board04 = _load_board04_module()
    tolerance = yaml.safe_load((REPO_ROOT / ".github" / "routed-drc-tolerance.yml").read_text())
    floor = (tolerance.get("tolerances") or {}).get(
        "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb",
        0,  # absence == strict-0 (see the file's convention)
    )
    assert floor == board04._DRILL_CLEARANCE_ALLOWANCE, (
        "The board-04 recipe's _DRILL_CLEARANCE_ALLOWANCE must stay in lockstep "
        "with .github/routed-drc-tolerance.yml (strict-0 since #4017 re-spaced "
        "the drill pair and removed the tolerance entry)."
    )
