"""Tests for the board-05 blocking-net CI gate (issue #3822).

``scripts/ci/check_board_05_blocking.py`` regenerates + routes board 05 in
CI and asserts ``blocking_incomplete_count <= --max-blocking`` (default 11,
a TEMPORARY loose bound above the observed nondeterministic CI ceiling of
10; board-05's CI re-route is nondeterministic at 9-10 and diverges from the
committed artifact of 7 -- the gap is tracked in #3775/#3766/#3829, and the
flaky-gate consequence in #3836).

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


def test_default_threshold_is_temporary_loose_bound() -> None:
    helper = _load_helper()
    assert helper.DEFAULT_MAX_BLOCKING == 11


def test_default_clears_nondeterministic_ci_floor(tmp_path, monkeypatch) -> None:
    """Rationale for the default of 11 (issue #3836): board-05's CI re-route is
    nondeterministic at 9-10 blocking, so the gate must pass at BOTH 9 and 10
    and only fail above the observed ceiling.

    This documents WHY 11 was chosen: 9 (main) and 10 (PR #3835 branch, twice,
    identical router code) must both be green so the gate stops flaking, while
    12 (a gross regression beyond the ceiling) must still red.
    """
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    def route_to(count: int):
        monkeypatch.setattr(
            helper,
            "count_blocking",
            lambda _p: (count, [f"NET{i}" for i in range(count)]),
        )

    # 9 and 10 -- the observed nondeterministic CI floor/ceiling -- both pass.
    route_to(9)
    assert helper.check_pcb(pcb, max_blocking=helper.DEFAULT_MAX_BLOCKING)[0] == 0
    route_to(10)
    assert helper.check_pcb(pcb, max_blocking=helper.DEFAULT_MAX_BLOCKING)[0] == 0

    # 11 (the exact bound) still passes; 12 (gross regression) fails.
    route_to(11)
    assert helper.check_pcb(pcb, max_blocking=helper.DEFAULT_MAX_BLOCKING)[0] == 0
    route_to(12)
    exit_code, message = helper.check_pcb(pcb, max_blocking=helper.DEFAULT_MAX_BLOCKING)
    assert exit_code == 2
    assert "regression" in message.lower()


def test_check_pcb_passes_at_ci_ceiling(tmp_path, monkeypatch) -> None:
    """10 blocking nets (observed CI ceiling) with --max-blocking 11 -> exit 0."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(
        helper,
        "count_blocking",
        lambda _p: (10, [f"NET{i}" for i in range(10)]),
    )

    exit_code, message = helper.check_pcb(pcb, max_blocking=11)
    assert exit_code == 0
    assert "10 blocking incomplete net(s)" in message


def test_check_pcb_passes_when_below_threshold(tmp_path, monkeypatch) -> None:
    """A future improvement (5 blocking) still passes the <= 11 gate."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (5, ["A", "B", "C", "D", "E"]))

    exit_code, _ = helper.check_pcb(pcb, max_blocking=11)
    assert exit_code == 0


def test_check_pcb_fails_on_gross_regression(tmp_path, monkeypatch) -> None:
    """12 blocking nets (beyond the observed ceiling) with default -> exit 2."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(
        helper,
        "count_blocking",
        lambda _p: (12, [f"NET{i}" for i in range(12)]),
    )

    exit_code, message = helper.check_pcb(pcb, max_blocking=11)
    assert exit_code == 2
    assert "regression" in message.lower()
    assert "12 blocking incomplete net(s)" in message


def test_check_pcb_fails_when_threshold_below_actual(tmp_path, monkeypatch) -> None:
    """--max-blocking 0 against the measured 10 -> exit 2 (test-plan check)."""
    helper = _load_helper()
    pcb = tmp_path / "bldc_controller_routed.kicad_pcb"
    pcb.write_text("(kicad_pcb)")

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (10, [f"NET{i}" for i in range(10)]))

    exit_code, _ = helper.check_pcb(pcb, max_blocking=0)
    assert exit_code == 2


def test_check_pcb_fails_when_tightened_toward_committed(tmp_path, monkeypatch) -> None:
    """Tightening --max-blocking to 7 (the committed artifact) fails at the
    current nondeterministic floor of 9-10 -- this is the lever
    #3775/#3766/#3829 will pull once the re-route is deterministic and the
    floor drops back to 7."""
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

    monkeypatch.setattr(helper, "count_blocking", lambda _p: (10, ["A", "B"]))

    # default 11 -> 10 passes
    assert helper.main([str(pcb)]) == 0
    # tightened to 7 -> 10 fails
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
