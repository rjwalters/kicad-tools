"""Tests for the ``kct optim fom-debug`` CLI command.

Issue #3186 -- AC 8.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.optim_fom_cmd import run_optim_fom_debug


@pytest.fixture
def voltage_divider_pcb_path() -> Path:
    here = Path(__file__).parent.parent
    return here / "boards" / "01-voltage-divider" / "output" / "voltage_divider_routed.kicad_pcb"


def test_fom_debug_missing_pcb_returns_error_code(tmp_path: Path):
    code = run_optim_fom_debug(str(tmp_path / "does-not-exist.kicad_pcb"))
    assert code == 2


def test_fom_debug_text_output(capsys, voltage_divider_pcb_path: Path):
    if not voltage_divider_pcb_path.exists():
        pytest.skip("voltage divider routed PCB not present in checkout")
    code = run_optim_fom_debug(str(voltage_divider_pcb_path))
    assert code == 0
    captured = capsys.readouterr()
    assert "FOM breakdown" in captured.out
    assert "trace_length_excess" in captured.out
    assert "weighted_via_count" in captured.out


def test_fom_debug_json_output_parses(capsys, voltage_divider_pcb_path: Path):
    if not voltage_divider_pcb_path.exists():
        pytest.skip("voltage divider routed PCB not present in checkout")
    code = run_optim_fom_debug(str(voltage_divider_pcb_path), output_format="json")
    assert code == 0
    out = capsys.readouterr().out
    data = json.loads(out)
    assert "score" in data
    assert "soft_terms" in data
    assert "hard_gate_passed" in data
    assert isinstance(data["soft_terms"], dict)


def test_fom_debug_verbose_includes_feature_summary(capsys, voltage_divider_pcb_path: Path):
    if not voltage_divider_pcb_path.exists():
        pytest.skip("voltage divider routed PCB not present in checkout")
    code = run_optim_fom_debug(str(voltage_divider_pcb_path), verbose=True)
    assert code == 0
    out = capsys.readouterr().out
    assert "Features:" in out
    assert "footprints:" in out


def test_fom_debug_with_weights_file(capsys, voltage_divider_pcb_path: Path, tmp_path: Path):
    if not voltage_divider_pcb_path.exists():
        pytest.skip("voltage divider routed PCB not present in checkout")
    weights = tmp_path / "weights.yaml"
    weights.write_text("trace_length_excess: 2.5\n")
    code = run_optim_fom_debug(
        str(voltage_divider_pcb_path),
        weights_path=str(weights),
    )
    assert code == 0
    out = capsys.readouterr().out
    assert "2.500" in out or "2.5" in out


def test_fom_debug_missing_weights_returns_error_code(capsys, voltage_divider_pcb_path: Path):
    if not voltage_divider_pcb_path.exists():
        pytest.skip("voltage divider routed PCB not present in checkout")
    code = run_optim_fom_debug(
        str(voltage_divider_pcb_path),
        weights_path="/nonexistent/path/weights.yaml",
    )
    assert code == 2


def test_fom_debug_bad_pcb_file_returns_error(tmp_path: Path):
    # Create a bogus file that isn't a valid PCB.
    bogus = tmp_path / "bogus.kicad_pcb"
    bogus.write_text("not a real pcb file")
    code = run_optim_fom_debug(str(bogus))
    assert code == 2
