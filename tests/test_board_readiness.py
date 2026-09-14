"""Readiness must describe the actual inputs, not stale saved success."""

import hashlib
import json

import pytest

from kicad_tools.cli.board_metrics_cmd import extract_board_metrics
from kicad_tools.cli.board_readiness import read_readiness


def report_fixture(root):
    files = [
        "output/demo_routed.kicad_pcb",
        "output/demo.kicad_sch",
        "output/demo.kicad_pro",
        "output/net_class_map.json",
        "output/manufacturing/manifest.json",
        "output/manufacturing/bom_jlcpcb.csv",
    ]
    inputs = {}
    for name in files:
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(name)
        inputs[name] = hashlib.sha256(path.read_bytes()).hexdigest()
    report = {
        "schema_version": 1,
        "checked_at": "2026-09-09T10:00:00Z",
        "mode": "pcb_only",
        "status": "ready",
        "blockers": [],
        "inputs": inputs,
        "checks": [
            {"name": n, "status": "passed"} for n in ["kct_check", "native_drc", "artifacts", "bom"]
        ],
    }
    save(root, report)
    return report


def save(root, report):
    (root / "output/readiness.json").write_text(json.dumps(report))


def test_fresh_report_and_blocked_report(tmp_path):
    report = report_fixture(tmp_path)
    assert read_readiness(tmp_path)["status"] == "ready"
    report.update(status="blocked", blockers=["Native DRC: 13 opens"])
    save(tmp_path, report)
    assert read_readiness(tmp_path)["blockers"] == ["Native DRC: 13 opens"]
    assert extract_board_metrics(tmp_path)["status"] == "partial"


@pytest.mark.parametrize(
    "name",
    [
        "demo_routed.kicad_pcb",
        "net_class_map.json",
        "manufacturing/bom_jlcpcb.csv",
        "manufacturing/manifest.json",
    ],
)
def test_changed_sources_or_bundle_invalidates_success(tmp_path, name):
    report_fixture(tmp_path)
    (tmp_path / "output" / name).write_text("changed")
    result = read_readiness(tmp_path)
    assert result["status"] == "unverified"
    assert "stale" in result["blockers"][0]


@pytest.mark.parametrize(
    "mutation",
    ["missing_hash", "missing_check", "failed_check", "missing_report", "malformed", "traversal"],
)
def test_incomplete_evidence_never_passes(tmp_path, mutation):
    report = report_fixture(tmp_path)
    if mutation == "missing_hash":
        del report["inputs"]["output/demo.kicad_pro"]
    elif mutation == "missing_check":
        report["checks"].pop()
    elif mutation == "failed_check":
        report["checks"][0]["status"] = "not_run"
    elif mutation == "traversal":
        report["inputs"]["../outside"] = "0" * 64
    save(tmp_path, report)
    if mutation == "missing_report":
        (tmp_path / "output/readiness.json").unlink()
    elif mutation == "malformed":
        (tmp_path / "output/readiness.json").write_text("[]")
    assert read_readiness(tmp_path)["status"] == "unverified"


def test_metrics_ready_requires_evidence(tmp_path):
    report_fixture(tmp_path)
    (tmp_path / "output/manufacturing/report.md").write_text("## DRC Status\n| Errors | 0 |")
    (tmp_path / "output/lvs.json").write_text('{"clean":true}')
    assert extract_board_metrics(tmp_path)["status"] == "ok"
    (tmp_path / "output/readiness.json").unlink()
    assert extract_board_metrics(tmp_path)["status"] == "partial"


def test_fresh_metrics_override_old_export_but_stale_metrics_do_not(tmp_path):
    report = report_fixture(tmp_path)
    report.update(
        status="blocked",
        blockers=["Native opens"],
        metrics={
            "drc_violations": 13,
            "nets_routed_pct": 93.5,
            "lvs_clean": False,
            "lvs_mismatches": 2,
        },
    )
    save(tmp_path, report)
    (tmp_path / "output/manufacturing/report.md").write_text("## DRC Status\n| Errors | 0 |")
    metrics = extract_board_metrics(tmp_path)
    assert metrics["drc_violations"] == 13
    assert metrics["nets_routed_pct"] == 93.5
    assert metrics["lvs_clean"] is False
    (tmp_path / "output/demo_routed.kicad_pcb").write_text("changed")
    metrics = extract_board_metrics(tmp_path)
    assert metrics["drc_violations"] == 0
    assert metrics["readiness"]["status"] == "unverified"
    assert "metrics" not in metrics["readiness"]


@pytest.mark.parametrize(
    "markdown", [None, "Reviewed manufacturing package; see check-report.json."]
)
def test_verified_metrics_do_not_require_markdown_summary(tmp_path, caplog, markdown):
    report = report_fixture(tmp_path)
    report["metrics"] = {
        "drc_violations": 0,
        "nets_routed_pct": 100,
        "lvs_clean": True,
        "lvs_mismatches": 0,
    }
    save(tmp_path, report)
    if markdown is not None:
        (tmp_path / "output/manufacturing/report.md").write_text(markdown)
    metrics = extract_board_metrics(tmp_path)
    assert metrics["status"] == "ok"
    assert metrics["drc_violations"] == 0
    assert metrics["nets_routed_pct"] == 100
    assert metrics["lvs_clean"] is True
    assert "could not parse drc_violations" not in caplog.text

    (tmp_path / "output/demo_routed.kicad_pcb").write_text("changed")
    stale = extract_board_metrics(tmp_path)
    assert stale["status"] == "partial"
    assert stale["readiness"]["status"] == "unverified"
    assert stale.get("drc_violations") is None
