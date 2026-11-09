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


_CHECK_PCB = """(kicad_pcb (version 20240108) (generator "test_fixture")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
)
"""


def _check_board(tmp_path: Path, *, stock: bool) -> Path:
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_CHECK_PCB)
    if stock:
        shutil.copy(FIXTURE, pcb.with_suffix(".kicad_pro"))
    else:
        data = json.loads(FIXTURE.read_text())
        data["board"]["design_settings"]["rules"]["min_clearance"] = 0.127
        pcb.with_suffix(".kicad_pro").write_text(json.dumps(data))
    return pcb


def test_check_cmd_warns_and_reports_stock_default(tmp_path, capsys):
    from kicad_tools.cli.check_cmd import main

    pcb = _check_board(tmp_path, stock=True)
    main([str(pcb), "--only", "single_pad_net", "--format", "json"])
    captured = capsys.readouterr()
    assert "WARNING: project_default:" in captured.err
    codes = [f["code"] for f in json.loads(captured.out)["project_rules"]]
    assert codes == ["project_default"]


def test_check_cmd_clean_project_has_no_warning(tmp_path, capsys):
    from kicad_tools.cli.check_cmd import main

    pcb = _check_board(tmp_path, stock=False)
    main([str(pcb), "--only", "single_pad_net", "--format", "json"])
    captured = capsys.readouterr()
    assert "WARNING: project_" not in captured.err
    assert json.loads(captured.out)["project_rules"] == []


def _readiness_report(pcb: Path) -> dict:
    from kicad_tools.cli.readiness_cmd import (
        CheckOutcome,
        EngineFingerprint,
        ReadinessOptions,
        build_report,
    )

    options = ReadinessOptions(
        board_dir=pcb.parent,
        pcb=pcb,
        schematic=None,
        project=pcb.with_suffix(".kicad_pro"),
        manufacturer="jlcpcb",
        mode="pcb_only",
        output_dir=pcb.parent / "manufacturing",
        evidence_dir=pcb.parent / "readiness",
    )
    checks = [CheckOutcome(name="kct_check", status="passed", detail="ok")]
    return build_report(options, checks, {}, EngineFingerprint(kicad_tools_version="test"))


def test_readiness_report_records_stock_default_warning(tmp_path):
    report = _readiness_report(_check_board(tmp_path, stock=True))
    assert [w["code"] for w in report["warnings"]] == ["project_default"]


def test_readiness_report_clean_project_has_no_warnings_key(tmp_path):
    report = _readiness_report(_check_board(tmp_path, stock=False))
    assert "warnings" not in report
