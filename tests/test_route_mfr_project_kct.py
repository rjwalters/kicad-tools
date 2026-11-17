"""``kct route`` defaults ``--manufacturer`` from ``project.kct`` (Issue #6155).

``kct check`` already resolved its tier as explicit flag > ``fab_profile.json``
> ``project.kct`` ``target_fab`` > ``jlcpcb``.  ``kct route`` hard-defaulted to
``jlcpcb``, so board 03 (``target_fab: jlcpcb-tier1``) routed under base-tier
rules, rejected its tier1-scoped ``fabrication_overrides.json`` and produced
phantom ``hole_to_hole`` errors.  Route now shares the check resolver, minus
the sidecar tier (route writes that sidecar itself).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import _resolve_effective_check_mfr
from kicad_tools.cli.route_cmd import _resolve_route_manufacturer, _route_parser
from kicad_tools.cli.route_lint_gate import LintGate


def _write_project_kct(directory: Path, target_fab: str) -> Path:
    kct = directory / "project.kct"
    kct.write_text(
        'kct_version: "1.0"\n'
        "project:\n"
        "  name: T\n"
        "requirements:\n"
        "  manufacturing:\n"
        f"    target_fab: {target_fab}\n"
    )
    return kct


def _resolve(argv: list[str]):
    args = _route_parser().parse_args(argv)
    _resolve_route_manufacturer(args, argv)
    return args


@pytest.fixture
def pcb(tmp_path: Path) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text("(kicad_pcb (version 20240108))\n")
    return path


class TestRouteManufacturerResolution:
    def test_project_kct_target_fab_is_the_default(self, pcb, capsys) -> None:
        _write_project_kct(pcb.parent, "jlcpcb-tier1")
        args = _resolve([str(pcb)])
        assert args.manufacturer == "jlcpcb-tier1"
        assert args._manufacturer_source == "project_kct"
        err = capsys.readouterr().err
        assert "[INFO] auto-loaded fab profile: jlcpcb-tier1 (from project.kct target_fab)" in err

    def test_project_kct_one_directory_up(self, tmp_path: Path) -> None:
        _write_project_kct(tmp_path, "jlcpcb-tier1")
        out = tmp_path / "output"
        out.mkdir()
        board = out / "board.kicad_pcb"
        board.write_text("(kicad_pcb)\n")
        assert _resolve([str(board)]).manufacturer == "jlcpcb-tier1"

    @pytest.mark.parametrize(
        "flag", [["--manufacturer", "jlcpcb"], ["--mfr", "jlcpcb"], ["--manufacturer=jlcpcb"]]
    )
    def test_explicit_flag_wins_even_when_it_names_the_default(self, pcb, flag, capsys) -> None:
        _write_project_kct(pcb.parent, "jlcpcb-tier1")
        args = _resolve([str(pcb), *flag])
        assert args.manufacturer == "jlcpcb"
        assert args._manufacturer_source == "cli"
        assert "auto-loaded" not in capsys.readouterr().err

    def test_no_project_kct_keeps_jlcpcb(self, pcb, capsys) -> None:
        args = _resolve([str(pcb)])
        assert args.manufacturer == "jlcpcb"
        assert args._manufacturer_source == "default"
        assert capsys.readouterr().err == ""

    def test_unknown_target_fab_warns_and_falls_back(self, pcb, capsys) -> None:
        _write_project_kct(pcb.parent, "no-such-fab")
        args = _resolve([str(pcb)])
        assert args.manufacturer == "jlcpcb"
        assert args._manufacturer_source == "default"
        assert "ignoring project.kct target_fab 'no-such-fab'" in capsys.readouterr().err

    def test_route_ignores_its_own_fab_profile_sidecar(self, pcb) -> None:
        """A previous run's ``fab_profile.json`` must not pin the next run's tier."""
        _write_project_kct(pcb.parent, "jlcpcb")
        (pcb.parent / "fab_profile.json").write_text(json.dumps({"mfr": "jlcpcb-tier1"}))
        args = _resolve([str(pcb)])
        assert args.manufacturer == "jlcpcb"
        assert args._manufacturer_source == "project_kct"
        # ...while kct check still honours it (its precedence is unchanged).
        assert _resolve_effective_check_mfr(None, pcb).mfr == "jlcpcb-tier1"


class TestLintGateForwarding:
    def test_project_kct_tier_is_forwarded_to_lint(self, pcb, tmp_path: Path) -> None:
        _write_project_kct(pcb.parent, "jlcpcb-tier1")
        argv = [str(pcb), "-o", str(tmp_path / "elsewhere" / "out.kicad_pcb")]
        gate = LintGate.from_route_args(_resolve(argv), argv)
        flags = gate.check_flags
        assert flags[flags.index("--mfr") + 1] == "jlcpcb-tier1"

    def test_default_tier_is_not_forwarded(self, pcb) -> None:
        argv = [str(pcb)]
        gate = LintGate.from_route_args(_resolve(argv), argv)
        assert "--mfr" not in gate.check_flags
