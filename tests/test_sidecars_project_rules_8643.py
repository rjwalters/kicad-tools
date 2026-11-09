"""Tests for project_rules_findings (Issue #8643)."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from kicad_tools.sidecars import _STOCK_DESIGN_RULES, project_rules_findings

FIXTURE = Path(__file__).parent / "fixtures" / "pcbnew_default.kicad_pro"


def _board(tmp_path: Path) -> Path:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    return pcb


def _codes(pcb: Path) -> list[str]:
    return [f["code"] for f in project_rules_findings(pcb)]


def test_stock_rules_match_real_fixture():
    data = json.loads(FIXTURE.read_text())
    assert data["board"]["design_settings"]["rules"] == _STOCK_DESIGN_RULES


def test_missing(tmp_path):
    f = project_rules_findings(_board(tmp_path))
    assert [x["code"] for x in f] == ["project_missing"]
    assert f[0]["severity"] == "warning"


def test_stock_default(tmp_path):
    pcb = _board(tmp_path)
    shutil.copy(FIXTURE, pcb.with_suffix(".kicad_pro"))
    assert _codes(pcb) == ["project_default"]


def test_stock_with_dru_is_clean(tmp_path):
    pcb = _board(tmp_path)
    shutil.copy(FIXTURE, pcb.with_suffix(".kicad_pro"))
    pcb.with_suffix(".kicad_dru").write_text("(version 1)")
    assert _codes(pcb) == []


def test_customised_rules_clean(tmp_path):
    pcb = _board(tmp_path)
    data = json.loads(FIXTURE.read_text())
    data["board"]["design_settings"]["rules"]["min_clearance"] = 0.127
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(data))
    assert _codes(pcb) == []


def test_extra_net_class_clean(tmp_path):
    pcb = _board(tmp_path)
    data = json.loads(FIXTURE.read_text())
    data["net_settings"]["classes"].append({"name": "Power"})
    pcb.with_suffix(".kicad_pro").write_text(json.dumps(data))
    assert _codes(pcb) == []


def test_invalid_json_does_not_crash(tmp_path):
    pcb = _board(tmp_path)
    pcb.with_suffix(".kicad_pro").write_text("{not json")
    assert _codes(pcb) == []


def test_output_json_always_has_project_rules(capsys):
    from kicad_tools.cli.check_cmd import output_json
    from kicad_tools.validate import DRCResults

    output_json([], DRCResults(), Path("b.kicad_pcb"), "jlcpcb", 2)
    assert json.loads(capsys.readouterr().out)["project_rules"] == []
