"""Tests for the board-05 blocking-net CI gate (issue #3822).

``scripts/ci/check_board_05_blocking.py`` regenerates + routes board 05 in
CI and asserts ``blocking_incomplete_count <= --max-blocking`` (default 0:
board-05's CI regen measures 0 blocking nets on main, matching the committed
artifact -- issue #7547; the earlier Phase-5 bound of 7 is retired).

The pass/fail VERDICT against a real route is CI-only (the macOS host routes
board 05 to more blocking nets than the Linux CI router -- the documented
host-vs-CI reach divergence, #3822). These tests therefore exercise the
script's *threshold logic* against a synthetic blocking count
(``count_blocking`` is monkeypatched) so the comparison / exit-code
behaviour is verified without needing a real route. They also cover argument
parsing and the missing-file tool-error path.

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


def test_default_threshold_is_zero() -> None:
    helper = _load_helper()
    assert helper.DEFAULT_MAX_BLOCKING == 0


def test_default_fails_on_a_single_blocking_net(tmp_path, monkeypatch) -> None:
    """Issue #7547: with the default, 0 blocking passes and ONE blocking net
    fails (exit 2) -- the old Phase-5 bound of 7 would have let it through."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    def route_to(count: int):
        monkeypatch.setattr(
            helper,
            "count_blocking",
            lambda _p: (count, [f"NET{i}" for i in range(count)]),
        )

    route_to(0)
    assert helper.check_pcb(pcb, max_blocking=helper.DEFAULT_MAX_BLOCKING)[0] == 0

    route_to(1)
    exit_code, message = helper.check_pcb(pcb, max_blocking=helper.DEFAULT_MAX_BLOCKING)
    assert exit_code == 2
    assert "regression" in message.lower()
    assert "NET0" in message

    # The retired Phase-5 ceiling of 6 now fails too.
    route_to(6)
    assert helper.check_pcb(pcb, max_blocking=helper.DEFAULT_MAX_BLOCKING)[0] == 2


def test_main_default_fails_on_one_blocking_net(tmp_path, monkeypatch, capsys) -> None:
    """``main`` with no --max-blocking (the CI default) exits 2 on 1 net."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (1, ["PHASE_A"]))

    assert helper.main([str(pcb)]) == 2
    out = capsys.readouterr().out
    assert "MEASURED blocking_incomplete_count = 1 (threshold <= 0)" in out
    assert "PHASE_A" in out


def test_check_pcb_passes_at_ci_ceiling(tmp_path, monkeypatch) -> None:
    """An explicit --max-blocking 7 still passes 6 blocking nets (threshold logic)."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(
        helper,
        "count_blocking",
        lambda _p: (6, [f"NET{i}" for i in range(6)]),
    )

    exit_code, message = helper.check_pcb(pcb, max_blocking=7)
    assert exit_code == 0
    assert "6 blocking incomplete net(s)" in message


def test_check_pcb_passes_when_below_threshold(tmp_path, monkeypatch) -> None:
    """5 blocking nets pass an explicit --max-blocking 7 (threshold logic)."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (5, ["A", "B", "C", "D", "E"]))

    exit_code, _ = helper.check_pcb(pcb, max_blocking=7)
    assert exit_code == 0


def test_check_pcb_fails_on_gross_regression(tmp_path, monkeypatch) -> None:
    """12 blocking nets beyond an explicit bound of 7 -> exit 2."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(
        helper,
        "count_blocking",
        lambda _p: (12, [f"NET{i}" for i in range(12)]),
    )

    exit_code, message = helper.check_pcb(pcb, max_blocking=7)
    assert exit_code == 2
    assert "regression" in message.lower()
    assert "12 blocking incomplete net(s)" in message


def test_check_pcb_fails_when_threshold_below_actual(tmp_path, monkeypatch) -> None:
    """--max-blocking 0 against 6 blocking nets -> exit 2."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (6, [f"NET{i}" for i in range(6)]))

    exit_code, _ = helper.check_pcb(pcb, max_blocking=0)
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

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (0, []))

    assert helper.main([str(pcb)]) == 0
    out = capsys.readouterr().out
    assert "MEASURED blocking_incomplete_count = 0" in out
    assert "blocking nets: (none)" in out


def test_main_prints_measured_count_on_regression(tmp_path, monkeypatch, capsys) -> None:
    """The measured blocking count is surfaced on stdout EVEN when the gate fails.

    Requirement from issue #3822 follow-up: CI must be able to read the real
    count from the log even on the failing (> threshold) path so a result above
    the threshold is observable rather than papered over.
    """
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(
        helper,
        "count_blocking",
        lambda _p: (12, [f"NET{i}" for i in range(12)]),
    )

    assert helper.main([str(pcb)]) == 2
    out = capsys.readouterr().out
    assert "MEASURED blocking_incomplete_count = 12" in out


def test_main_parses_max_blocking_arg(tmp_path, monkeypatch) -> None:
    """The --max-blocking CLI arg is wired through to check_pcb."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (6, ["A", "B"]))

    # default 0 -> 6 fails
    assert helper.main([str(pcb)]) == 2
    # explicit 7 -> 6 passes; 5 -> 6 fails
    assert helper.main([str(pcb), "--max-blocking", "7"]) == 0
    assert helper.main([str(pcb), "--max-blocking", "5"]) == 2


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
