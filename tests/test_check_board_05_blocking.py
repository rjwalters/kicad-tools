"""Tests for the board-05 blocking-net CI gate (issue #3822).

``scripts/ci/check_board_05_blocking.py`` regenerates + routes board 05 in
CI and asserts ``blocking_incomplete_count <= --max-blocking`` (default 9,
the measured reproducible CI floor; the committed artifact is 7 but a fresh
CI re-route reaches 9 -- the 7->9 gap is tracked in #3775/#3766).

The pass/fail VERDICT against a real route is CI-only (the macOS host routes
board 05 to ~11 blocking nets, not 7 -- the documented host-vs-CI reach
divergence). These tests therefore exercise the script's *threshold logic*
against a synthetic blocking count (``count_blocking`` is monkeypatched) so
the comparison / exit-code behaviour is verified without needing a real
7-blocking route. They also cover argument parsing and the missing-file
tool-error path.

The script is loaded via importlib (it lives under ``scripts/ci/`` outside
the installed package), mirroring ``tests/test_check_board_e2e.py``.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER_SCRIPT_PATH = REPO_ROOT / "scripts" / "ci" / "check_board_05_blocking.py"


def _load_helper():
    spec = importlib.util.spec_from_file_location("check_board_05_blocking", HELPER_SCRIPT_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["check_board_05_blocking"] = module
    spec.loader.exec_module(module)
    return module


def test_default_threshold_is_measured_ci_floor() -> None:
    helper = _load_helper()
    assert helper.DEFAULT_MAX_BLOCKING == 9


def test_check_pcb_passes_at_ci_floor(tmp_path, monkeypatch) -> None:
    """9 blocking nets with --max-blocking 9 -> exit 0 (the measured CI floor)."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(
        helper,
        "count_blocking",
        lambda _p: (9, [f"NET{i}" for i in range(9)]),
    )

    exit_code, message = helper.check_pcb(pcb, max_blocking=9)
    assert exit_code == 0
    assert "9 blocking incomplete net(s)" in message


def test_check_pcb_passes_when_below_threshold(tmp_path, monkeypatch) -> None:
    """A future improvement (5 blocking) still passes the <= 9 gate."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (5, ["A", "B", "C", "D", "E"]))

    exit_code, _ = helper.check_pcb(pcb, max_blocking=9)
    assert exit_code == 0


def test_check_pcb_fails_on_regression(tmp_path, monkeypatch) -> None:
    """11 blocking nets (the host divergence) with default threshold -> exit 2."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(
        helper,
        "count_blocking",
        lambda _p: (11, [f"NET{i}" for i in range(11)]),
    )

    exit_code, message = helper.check_pcb(pcb, max_blocking=9)
    assert exit_code == 2
    assert "regression" in message.lower()
    assert "11 blocking incomplete net(s)" in message


def test_check_pcb_fails_when_threshold_below_actual(tmp_path, monkeypatch) -> None:
    """--max-blocking 0 against the measured 9 -> exit 2 (test-plan check)."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (9, [f"NET{i}" for i in range(9)]))

    exit_code, _ = helper.check_pcb(pcb, max_blocking=0)
    assert exit_code == 2


def test_check_pcb_fails_when_tightened_toward_committed(tmp_path, monkeypatch) -> None:
    """Tightening --max-blocking to 7 (the committed artifact) fails at the
    current measured floor of 9 -- this is the lever #3775/#3766 will pull as
    routing improves and the floor drops back to 7."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (9, [f"NET{i}" for i in range(9)]))

    exit_code, _ = helper.check_pcb(pcb, max_blocking=7)
    assert exit_code == 2


def test_check_pcb_tool_error_on_missing_file(tmp_path) -> None:
    """Missing routed PCB -> exit 1 (tool error), never a silent pass."""
    helper = _load_helper()
    missing = tmp_path / "does_not_exist.kicad_pcb"
    exit_code, message = helper.check_pcb(missing, max_blocking=7)
    assert exit_code == 1
    assert "not found" in message.lower()


def test_main_prints_measured_count_on_pass(tmp_path, monkeypatch, capsys) -> None:
    """The measured blocking count is surfaced on stdout when the gate passes."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (7, [f"NET{i}" for i in range(7)]))

    assert helper.main([str(pcb)]) == 0
    out = capsys.readouterr().out
    assert "MEASURED blocking_incomplete_count = 7" in out


def test_main_prints_measured_count_on_regression(tmp_path, monkeypatch, capsys) -> None:
    """The measured blocking count is surfaced on stdout EVEN when the gate fails.

    Requirement from issue #3822 follow-up: CI must be able to read the real
    count from the log even on the failing (> threshold) path so a >7 result
    is observable rather than papered over.
    """
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(
        helper,
        "count_blocking",
        lambda _p: (11, [f"NET{i}" for i in range(11)]),
    )

    assert helper.main([str(pcb)]) == 2
    out = capsys.readouterr().out
    assert "MEASURED blocking_incomplete_count = 11" in out


def test_main_parses_max_blocking_arg(tmp_path, monkeypatch) -> None:
    """The --max-blocking CLI arg is wired through to check_pcb."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (8, ["A", "B"]))

    # default 9 -> 8 passes
    assert helper.main([str(pcb)]) == 0
    # tightened to 7 -> 8 fails
    assert helper.main([str(pcb), "--max-blocking", "7"]) == 2


def test_main_rejects_negative_threshold(tmp_path) -> None:
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    assert helper.main([str(pcb), "--max-blocking", "-1"]) == 1


def test_help_exits_zero() -> None:
    helper = _load_helper()
    with pytest.raises(SystemExit) as excinfo:
        helper.main(["--help"])
    assert excinfo.value.code == 0
