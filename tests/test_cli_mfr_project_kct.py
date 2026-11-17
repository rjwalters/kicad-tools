"""Remaining jlcpcb-defaulted commands read ``project.kct`` target_fab (Issue #6169)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.parser import create_parser
from kicad_tools.manufacturers.resolve import resolve_cli_manufacturer


def _kct(directory: Path, target_fab: str) -> None:
    (directory / "project.kct").write_text(
        'kct_version: "1.0"\nproject:\n  name: T\nrequirements:\n  manufacturing:\n'
        f"    target_fab: {target_fab}\n"
    )


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    p = tmp_path / "board.kicad_pcb"
    p.write_text("(kicad_pcb (version 20240108))\n")
    return p


class TestResolver:
    def test_directory_input_uses_its_own_project_kct(self, tmp_path, capsys):
        _kct(tmp_path, "jlcpcb-tier1")
        assert resolve_cli_manufacturer(None, tmp_path) == "jlcpcb-tier1"
        assert "from project.kct target_fab" in capsys.readouterr().err

    def test_explicit_wins_and_is_silent(self, pcb, capsys):
        _kct(pcb.parent, "jlcpcb-tier1")
        assert resolve_cli_manufacturer("jlcpcb", pcb) == "jlcpcb"
        assert capsys.readouterr().err == ""

    def test_sidecar_toggle(self, pcb):
        _kct(pcb.parent, "jlcpcb")
        (pcb.parent / "fab_profile.json").write_text(json.dumps({"mfr": "jlcpcb-tier1"}))
        assert resolve_cli_manufacturer(None, pcb) == "jlcpcb-tier1"
        assert resolve_cli_manufacturer(None, pcb, consult_sidecar=False) == "jlcpcb"


def _capture(monkeypatch, module: str, attr: str) -> list[list[str]]:
    seen: list[list[str]] = []

    def fake(argv):
        seen.append(list(argv))
        return 0

    monkeypatch.setattr(f"kicad_tools.cli.{module}.{attr}", fake)
    return seen


CASES = [
    # (argv builder, module, attr, runner import)
    ("fix-vias", "fix_vias_cmd"),
    ("fix-silkscreen", "fix_silkscreen_cmd"),
    ("fix-drc", "fix_drc_cmd"),
    ("reason", "reason_cmd"),
    ("export", "export_cmd"),
    ("audit", "audit_cmd"),
    ("pipeline", "pipeline_cmd"),
]


def _dispatch(argv: list[str]) -> int:
    from kicad_tools.cli import _run_export_command
    from kicad_tools.cli.commands.pipeline import run_pipeline_command
    from kicad_tools.cli.commands.reasoning import run_reason_command
    from kicad_tools.cli.commands.validation import (
        run_audit_command,
        run_fix_drc_command,
        run_fix_silkscreen_command,
        run_fix_vias_command,
    )

    args = create_parser().parse_args(argv)
    runner = {
        "fix-vias": run_fix_vias_command,
        "fix-silkscreen": run_fix_silkscreen_command,
        "fix-drc": run_fix_drc_command,
        "reason": run_reason_command,
        "export": _run_export_command,
        "audit": run_audit_command,
        "pipeline": run_pipeline_command,
    }[argv[0]]
    return runner(args)


def _mfr_of(sub_argv: list[str]) -> str:
    return sub_argv[sub_argv.index("--mfr") + 1]


@pytest.mark.parametrize(("command", "module"), CASES)
def test_bare_run_picks_target_fab_and_flag_wins(command, module, pcb, monkeypatch, capsys):
    _kct(pcb.parent, "jlcpcb-tier1")
    seen = _capture(monkeypatch, module, "main")

    assert _dispatch([command, str(pcb)]) == 0
    assert _mfr_of(seen[-1]) == "jlcpcb-tier1"
    assert "[INFO] auto-loaded fab profile: jlcpcb-tier1" in capsys.readouterr().err

    assert _dispatch([command, str(pcb), "--mfr", "jlcpcb"]) == 0
    assert _mfr_of(seen[-1]) == "jlcpcb"
    assert "auto-loaded" not in capsys.readouterr().err


@pytest.mark.parametrize("command", ["fix-vias", "export", "audit", "pipeline"])
def test_no_project_kct_keeps_jlcpcb(command, pcb, monkeypatch):
    module = dict(CASES)[command]
    seen = _capture(monkeypatch, module, "main")
    _dispatch([command, str(pcb)])
    assert _mfr_of(seen[-1]) == "jlcpcb"


def test_pipeline_and_export_accept_project_directory(tmp_path, monkeypatch):
    _kct(tmp_path, "jlcpcb-tier1")
    for command, module in (("pipeline", "pipeline_cmd"), ("export", "export_cmd")):
        seen = _capture(monkeypatch, module, "main")
        _dispatch([command, str(tmp_path)])
        assert _mfr_of(seen[-1]) == "jlcpcb-tier1"


def test_pipeline_ignores_fab_profile_sidecar(pcb, monkeypatch):
    _kct(pcb.parent, "jlcpcb")
    (pcb.parent / "fab_profile.json").write_text(json.dumps({"mfr": "jlcpcb-tier1"}))
    seen = _capture(monkeypatch, "pipeline_cmd", "main")
    _dispatch(["pipeline", str(pcb)])
    assert _mfr_of(seen[-1]) == "jlcpcb"
    seen = _capture(monkeypatch, "fix_vias_cmd", "main")
    _dispatch(["fix-vias", str(pcb)])
    assert _mfr_of(seen[-1]) == "jlcpcb-tier1"


def test_estimate_cost_resolves(pcb, monkeypatch, capsys):
    _kct(pcb.parent, "jlcpcb-tier1")
    import kicad_tools.cost as cost

    got: dict = {}

    class Boom(Exception):
        pass

    class Est:
        def __init__(self, manufacturer, use_lcsc_pricing):
            got["mfr"] = manufacturer
            raise Boom

    monkeypatch.setattr(cost, "ManufacturingCostEstimator", Est)
    monkeypatch.setattr("kicad_tools.schema.pcb.PCB.load", lambda *_: object())
    from kicad_tools.cli.commands.estimate import run_estimate_command

    for extra, want in (([], "jlcpcb-tier1"), (["--mfr", "jlcpcb"], "jlcpcb")):
        args = create_parser().parse_args(["estimate", "cost", str(pcb), *extra])
        with pytest.raises(Boom):
            run_estimate_command(args)
        assert got["mfr"] == want
