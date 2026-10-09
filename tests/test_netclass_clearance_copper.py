"""``kct check`` enforces authored netclass clearances on routed copper (#6249).

Uses #6243's wall-corridor board: ``HV`` is a 0.5 mm class, ``Default`` 0.15 mm.
A straight HV trace through corridor A keeps 0.45 mm from the unconnected wall
pads (legal at the base, illegal for HV); the detour corridor keeps 0.7 mm.
"""

from __future__ import annotations

import copy
import json
import subprocess
from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import main
from tests.router import test_authored_netclass_board as wall

PRE_ROUTE = "(kicad_pcb"


def _seg(x1: float, y1: float, x2: float, y2: float, net: int = 1) -> str:
    return f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.2) (layer "F.Cu") (net {net}))\n'


STRAIGHT = _seg(103, wall.HV_Y, 127, wall.HV_Y)
DETOUR = (
    _seg(103, wall.HV_Y, 110, wall.HV_Y)
    + _seg(110, wall.HV_Y, 110, wall.DETOUR_Y)
    + _seg(110, wall.DETOUR_Y, 120, wall.DETOUR_Y)
    + _seg(120, wall.DETOUR_Y, 120, wall.HV_Y)
    + _seg(120, wall.HV_Y, 127, wall.HV_Y)
)


def _board(tmp_path: Path, copper: str, project: dict | None = None) -> Path:
    pcb = tmp_path / "hv.kicad_pcb"
    text = wall.board_text().rstrip()
    assert text.endswith(")")
    pcb.write_text(text[:-1] + copper + ")\n")
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(project or wall.project("hv")))
    return pcb


def _check(pcb: Path, tmp_path: Path, capsys) -> tuple[int, dict]:
    out = tmp_path / "report.json"
    code = main(
        [
            str(pcb),
            "--mfr",
            "jlcpcb",
            "--only",
            "netclass_clearance_copper",
            "--allow-incomplete",
            "--format",
            "json",
            "--output",
            str(out),
        ]
    )
    capsys.readouterr()
    return code, json.loads(out.read_text())


def _hits(report: dict, rule: str = "netclass_clearance_copper") -> list[dict]:
    return [v for v in report["violations"] if v["rule_id"] == rule]


def test_flags_the_045_gap(tmp_path: Path, capsys) -> None:
    code, report = _check(_board(tmp_path, STRAIGHT), tmp_path, capsys)
    assert code != 0
    hits = _hits(report)
    assert hits and all(h["severity"] == "error" for h in hits)
    assert min(h["actual_value"] for h in hits) == pytest.approx(0.45, abs=1e-3)
    assert all(h["required_value"] == pytest.approx(0.5) for h in hits)
    assert "'HV'" in hits[0]["message"] and "KCT_PRESERVE" not in hits[0]["message"]


def test_fab_floor_rule_does_not_see_it(tmp_path: Path, capsys) -> None:
    """The existing family is blind to it -- the gap this category closes."""
    pcb = _board(tmp_path, STRAIGHT)
    out = tmp_path / "c.json"
    main(
        [
            str(pcb),
            "--mfr",
            "jlcpcb",
            "--only",
            "clearance",
            "--allow-incomplete",
            "--format",
            "json",
            "--output",
            str(out),
        ]
    )
    capsys.readouterr()
    assert not [
        v
        for v in json.loads(out.read_text())["violations"]
        if v["rule_id"].startswith("clearance_")
    ]


def test_detour_passes(tmp_path: Path, capsys) -> None:
    code, report = _check(_board(tmp_path, DETOUR), tmp_path, capsys)
    assert _hits(report) == []
    assert code == 0


def test_default_only_project_unchanged(tmp_path: Path, capsys) -> None:
    project = copy.deepcopy(wall.project("hv"))
    project["net_settings"]["classes"] = [
        c for c in project["net_settings"]["classes"] if c["name"] == "Default"
    ]
    project["net_settings"]["netclass_patterns"] = []
    code, report = _check(_board(tmp_path, STRAIGHT, project), tmp_path, capsys)
    assert code == 0 and report["violations"] == []


def test_no_project_file_is_silent(tmp_path: Path, capsys) -> None:
    pcb = _board(tmp_path, STRAIGHT)
    pcb.with_suffix(".kicad_pro").unlink()
    code, report = _check(pcb, tmp_path, capsys)
    assert code == 0 and report["violations"] == []


def test_unsupported_declaration_is_explicit(tmp_path: Path, capsys) -> None:
    project = copy.deepcopy(wall.project("hv"))
    project["net_settings"]["netclass_patterns"] = [{"netclass": "HV", "pattern": "H[V"}]
    project["net_settings"]["netclass_assignments"] = {"SIG": ["NoSuchClass"]}
    code, report = _check(_board(tmp_path, STRAIGHT, project), tmp_path, capsys)
    assert code != 0
    hits = _hits(report, "netclass_clearance_unsupported")
    assert len(hits) == 1 and hits[0]["severity"] == "error"
    assert "NOT checked" in hits[0]["message"]


def test_opt_out_mode_still_reports_and_says_why(tmp_path: Path, capsys) -> None:
    project = copy.deepcopy(wall.project("hv"))
    project["text_variables"] = {"KCT_PRESERVE_BOARD_RULES": "0"}
    code, report = _check(_board(tmp_path, STRAIGHT, project), tmp_path, capsys)
    assert code != 0
    assert "KCT_PRESERVE_BOARD_RULES=0" in _hits(report)[0]["message"]


@pytest.mark.skipif(wall._kicad_cli() is None, reason="kicad-cli not available")
def test_agrees_with_kicad_cli(tmp_path: Path, capsys) -> None:
    """kicad-cli on the source project flags the straight run, passes the detour."""
    cli = wall._kicad_cli()
    for name, copper, expect_error in (("bad", STRAIGHT, True), ("good", DETOUR, False)):
        d = tmp_path / name
        d.mkdir()
        pcb = _board(d, copper)
        report = d / "drc.json"
        subprocess.run(
            [cli, "pcb", "drc", "--format", "json", "--output", str(report), str(pcb)],
            capture_output=True,
            timeout=120,
            check=False,
        )
        errors = [
            v
            for v in json.loads(report.read_text())["violations"]
            if v.get("severity") == "error" and "clearance" in v.get("type", "")
        ]
        assert bool(errors) is expect_error, (name, errors)
        code, rep = _check(pcb, d, capsys)
        assert bool(_hits(rep)) is expect_error


def _unversioned_hv() -> dict:
    project = copy.deepcopy(wall.project("hv"))
    del project["net_settings"]["meta"]
    return project


def test_unversioned_net_settings_is_checked_not_rejected(tmp_path: Path, capsys) -> None:
    """Issue #6262: no ``net_settings.meta`` is KiCad's current schema, not an error."""
    code, report = _check(_board(tmp_path, STRAIGHT, _unversioned_hv()), tmp_path, capsys)
    assert _hits(report, "netclass_clearance_unsupported") == []
    hits = _hits(report)
    assert hits and min(h["actual_value"] for h in hits) == pytest.approx(0.45, abs=1e-3)
    assert code != 0


@pytest.mark.parametrize(
    "project",
    [
        {"meta": {"filename": "hv.kicad_pro", "version": 1}},  # minimal/legacy: no net_settings
        {"meta": {"filename": "hv.kicad_pro", "version": 1}, "net_settings": None},
        {"meta": {"filename": "hv.kicad_pro", "version": 1}, "net_settings": {}},
    ],
    ids=["absent", "null", "empty"],
)
def test_project_without_net_settings_is_silent(tmp_path: Path, capsys, project) -> None:
    """Issue #6262: no authored netclasses -> nothing to check, and no error."""
    code, report = _check(_board(tmp_path, STRAIGHT, project), tmp_path, capsys)
    assert code == 0 and report["violations"] == []


@pytest.mark.skipif(wall._kicad_cli() is None, reason="kicad-cli not available")
def test_kicad_cli_enforces_an_unversioned_net_settings_block(tmp_path: Path, capsys) -> None:
    """The #6262 reading is KiCad's: kicad-cli applies HV with no ``meta.version``."""
    pcb = _board(tmp_path, STRAIGHT, _unversioned_hv())
    report = tmp_path / "drc.json"
    subprocess.run(
        [wall._kicad_cli(), "pcb", "drc", "--format", "json", "--output", str(report), str(pcb)],
        capture_output=True,
        timeout=120,
        check=False,
    )
    errors = [
        v
        for v in json.loads(report.read_text())["violations"]
        if v.get("severity") == "error" and "clearance" in v.get("type", "")
    ]
    assert errors
