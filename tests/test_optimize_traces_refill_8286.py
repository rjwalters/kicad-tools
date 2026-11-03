"""optimize-traces refills zones after writing (Issue #8286). No kicad-cli needed."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.cli.optimize_cmd import main
from kicad_tools.cli.runner import KiCadCLIResult
from tests.test_optimize_cmd import SIMPLE_PCB

TARGET = "kicad_tools.cli.runner.run_refill_zones"


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    p = tmp_path / "in.kicad_pcb"
    p.write_text(SIMPLE_PCB)
    return p


def test_refill_invoked_on_written_output(pcb, tmp_path, capsys):
    out = tmp_path / "out.kicad_pcb"
    with patch(TARGET, return_value=KiCadCLIResult(success=True)) as m:
        assert main([str(pcb), "-o", str(out), "--format", "json"]) == 0
    m.assert_called_once_with(out)
    data = json.loads(capsys.readouterr().out)
    assert data["zones_refilled"] is True
    assert data["zones_refill_error"] is None


def test_refill_targets_input_when_no_output(pcb, capsys):
    with patch(TARGET, return_value=KiCadCLIResult(success=True)) as m:
        assert main([str(pcb)]) == 0
    m.assert_called_once_with(pcb)
    assert "Zones refilled" in capsys.readouterr().out


def test_dry_run_does_not_refill(pcb, capsys):
    with patch(TARGET) as m:
        assert main([str(pcb), "--dry-run", "--format", "json"]) == 0
    m.assert_not_called()
    assert json.loads(capsys.readouterr().out)["zones_refilled"] is None


def test_refill_failure_warns_and_keeps_exit_code(pcb, capsys):
    res = KiCadCLIResult(success=False, stderr="kicad-cli not found")
    with patch(TARGET, return_value=res):
        assert main([str(pcb)]) == 0
    cap = capsys.readouterr()
    assert "zones were NOT refilled" in cap.err
    assert "kicad-cli not found" in cap.err


def test_refill_failure_in_json(pcb, capsys):
    res = KiCadCLIResult(success=False, stderr="boom")
    with patch(TARGET, return_value=res):
        assert main([str(pcb), "--format", "json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["zones_refilled"] is False
    assert data["zones_refill_error"] == "boom"


def test_refill_failure_warns_under_quiet(pcb, capsys):
    res = KiCadCLIResult(success=False, stderr="kicad-cli not found")
    with patch(TARGET, return_value=res):
        assert main([str(pcb), "--quiet"]) == 0
    cap = capsys.readouterr()
    assert "zones were NOT refilled" in cap.err
    assert "kicad-cli not found" in cap.err
