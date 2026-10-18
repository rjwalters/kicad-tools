"""``kct drc`` must say when kicad-cli could not run schematic parity (Issue #7077).

kicad-cli prints ``Failed to fetch schematic netlist for parity tests.``, exits
0 and writes ``"schematic_parity": []`` when ``--schematic-parity`` was asked
for but no annotated schematic netlist exists.  Before the fix ``kct drc`` read
that as a parity-clean board.  These tests mock the kicad-cli subprocess.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli import drc_cmd, runner

PARITY_LINE = (
    "Failed to fetch schematic netlist for parity tests.\n"
    "Schematic parity tests require a fully annotated schematic.\n"
)

REPORT = {
    "source": "noparity.kicad_pcb",
    "date": "2026-10-17T00:00:00+0000",
    "kicad_version": "10.0.6",
    "coordinate_units": "mm",
    "violations": [],
    "unconnected_items": [],
    "schematic_parity": [],
}

WARNING_VIOLATION = {
    "type": "track_dangling",
    "description": "Track has unconnected end",
    "severity": "warning",
    "items": [{"description": "Track [A] on F.Cu", "pos": {"x": 5.0, "y": 10.0}}],
}


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    path = tmp_path / "noparity.kicad_pcb"
    path.write_text("(kicad_pcb (version 20240108))\n")
    return path


def _fake_kicad_cli(monkeypatch, *, stdout: str = "", stderr: str = "", violations=None):
    """Patch the kicad-cli subprocess; record each argv it was given."""
    calls: list[list[str]] = []

    def fake_run(build_cmd, kicad_cli, *, refill_zones, timeout=None, text=False):
        argv = build_cmd(False)
        calls.append(argv)
        out = Path(argv[argv.index("--output") + 1])
        report = dict(REPORT, violations=list(violations or []))
        out.write_text(json.dumps(report))
        return subprocess.CompletedProcess(argv, 0, stdout=stdout, stderr=stderr), False

    monkeypatch.setattr(runner, "run_kicad_drc_command", fake_run)
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
    monkeypatch.setattr(drc_cmd, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
    return calls


# --- run_drc -----------------------------------------------------------------


@pytest.mark.parametrize("stream", ["stdout", "stderr"])
def test_run_drc_flags_parity_skipped_on_either_stream(monkeypatch, pcb, stream):
    _fake_kicad_cli(monkeypatch, **{stream: PARITY_LINE})
    result = runner.run_drc(pcb)
    assert result.success is True  # success is unchanged
    assert result.parity_skipped is True


def test_run_drc_not_flagged_when_netlist_fetched(monkeypatch, pcb):
    _fake_kicad_cli(monkeypatch, stdout="Found 0 violations\n")
    result = runner.run_drc(pcb)
    assert result.success is True
    assert result.parity_skipped is False


def test_run_drc_never_flags_without_parity_requested(monkeypatch, pcb):
    calls = _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    result = runner.run_drc(pcb, schematic_parity=False)
    assert "--schematic-parity" not in calls[0]
    assert result.parity_skipped is False


def test_parity_skipped_in_output_ignores_non_strings():
    assert runner.parity_skipped_in_output(None, b"Failed to fetch schematic netlist") is False
    assert runner.parity_skipped_in_output("", PARITY_LINE) is True


# --- kct drc -----------------------------------------------------------------


@pytest.mark.parametrize("fmt", ["table", "summary"])
def test_kct_drc_text_formats_show_notice(monkeypatch, capsys, pcb, fmt):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    rc = drc_cmd.main([str(pcb), "--format", fmt])
    out = capsys.readouterr().out
    assert rc == 0  # exit code unchanged: a schematic-less board is normal
    assert drc_cmd.PARITY_NOT_CHECKED_NOTICE in out


def test_kct_drc_table_verdict_is_qualified(monkeypatch, capsys, pcb):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    drc_cmd.main([str(pcb)])
    out = capsys.readouterr().out
    assert "DRC PASSED - No violations found (schematic parity NOT checked)" in out


def test_kct_drc_table_with_warnings_shows_notice(monkeypatch, capsys, pcb):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE, violations=[WARNING_VIOLATION])
    rc = drc_cmd.main([str(pcb)])
    out = capsys.readouterr().out
    assert rc == 0
    assert drc_cmd.PARITY_NOT_CHECKED_NOTICE in out
    assert "DRC WARNING - Review warnings (schematic parity NOT checked)" in out


def test_kct_drc_json_reports_parity_not_checked(monkeypatch, capsys, pcb):
    _fake_kicad_cli(monkeypatch, stderr=PARITY_LINE)
    rc = drc_cmd.main([str(pcb), "--format", "json"])
    data = json.loads(capsys.readouterr().out)
    assert rc == 0
    assert data["schematic_parity_checked"] is False


def test_kct_drc_mfr_shows_notice(monkeypatch, capsys, pcb):
    _fake_kicad_cli(monkeypatch, stdout=PARITY_LINE)
    rc = drc_cmd.main([str(pcb), "--mfr", "jlcpcb"])
    assert rc == 0
    assert drc_cmd.PARITY_NOT_CHECKED_NOTICE in capsys.readouterr().out


@pytest.mark.parametrize("fmt", ["table", "summary", "json"])
def test_kct_drc_no_notice_when_parity_ran(monkeypatch, capsys, pcb, fmt):
    _fake_kicad_cli(monkeypatch, stdout="Found 0 violations\n")
    rc = drc_cmd.main([str(pcb), "--format", fmt])
    out = capsys.readouterr().out
    assert rc == 0
    assert drc_cmd.PARITY_NOT_CHECKED_NOTICE not in out
    assert "NOT checked" not in out
    if fmt == "json":
        assert json.loads(out)["schematic_parity_checked"] is True


def test_kct_drc_uses_shared_kicad_cli_locator(monkeypatch, pcb):
    """The kicad-cli found by find_kicad_cli() is the one invoked."""
    calls = _fake_kicad_cli(monkeypatch)
    monkeypatch.setattr(drc_cmd, "find_kicad_cli", lambda: Path("/located/kicad-cli"))
    drc_cmd.main([str(pcb), "--format", "json"])
    assert calls[0][0] == "/located/kicad-cli"


@pytest.mark.parametrize("fmt", ["table", "summary", "json"])
def test_kct_drc_report_input_emits_no_notice(monkeypatch, capsys, tmp_path, fmt):
    report_path = tmp_path / "report.json"
    report_path.write_text(json.dumps(REPORT))

    def boom(*_a, **_k):  # a pre-existing report must not run kicad-cli
        raise AssertionError("kicad-cli must not run for .json input")

    monkeypatch.setattr(runner, "run_kicad_drc_command", boom)
    rc = drc_cmd.main([str(report_path), "--format", fmt])
    out = capsys.readouterr().out
    assert rc == 0
    assert "NOT checked" not in out
    if fmt == "json":
        assert "schematic_parity_checked" not in json.loads(out)
