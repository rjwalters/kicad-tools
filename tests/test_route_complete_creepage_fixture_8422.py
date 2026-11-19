"""``--complete`` on the synthetic mains fixture adds no creepage failures (#8422).

Unlike ``test_route_complete_creepage_8396`` (a hand-placed segment on a stub router),
this drives the real ``kct route --complete`` on a checked-in synthetic mains-style
board (``boards/synthetic-mains-complete``) from a real negotiated-route output, and
referees the result with ``kct creepage`` using the same maps.  A counterfactual run
with the rollback disabled proves the fixture still has teeth: if lattice completion
stops adding violating copper there, the fixture no longer exercises the gate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

import kicad_tools.cli.route_cmd as route_cmd

FIXTURE = Path(__file__).resolve().parent.parent / "boards" / "synthetic-mains-complete"
STAGE = FIXTURE / "negotiated_stage.kicad_pcb"
HV_FLAGS = [
    "--net-class-map",
    str(FIXTURE / "net_class_map.json"),
    "--voltage-map",
    str(FIXTURE / "voltage_map.json"),
    "--creepage-standard",
    "iec60664",
    "--pollution-degree",
    "2",
    "--material-group",
    "IIIa",
    "--hv-threshold",
    "0",
    "--skip-drc",
    "--no-auto-pour",
]


def _complete(tmp_path: Path, name: str) -> tuple[int, Path, Path]:
    out = tmp_path / f"{name}.kicad_pcb"
    report = tmp_path / f"{name}.json"
    rc = route_cmd.main(
        [str(STAGE), "-o", str(out), *HV_FLAGS, "--complete", "--complete-report", str(report)]
    )
    return rc, out, report


def _failing_pairs(pcb: Path) -> list[dict]:
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "creepage",
            str(pcb),
            "--net-class-map",
            str(FIXTURE / "net_class_map.json"),
            "--voltage-map",
            str(FIXTURE / "voltage_map.json"),
            "--standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
            "--waive-same-footprint",
            "--waive-attach-zone",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    return [p for p in json.loads(proc.stdout)["pairs"] if not p["pass"]]


def test_negotiated_stage_is_creepage_clean():
    assert _failing_pairs(STAGE) == []


def test_complete_leaves_no_new_creepage_failures_and_reports_rollbacks(tmp_path):
    _rc, out, report = _complete(tmp_path, "guarded")
    assert _failing_pairs(out) == []
    refusals = json.loads(report.read_text())["unroutable_links"]
    rolled = [r for r in refusals if r["reason"] == "creepage-rolled-back"]
    assert rolled, "fixture no longer makes --complete propose a creepage-violating link"
    for r in rolled:
        assert r["measured_mm"] < r["required_mm"] == 1.6
        assert r["blocking_net"]


@pytest.mark.slow
def test_fixture_has_teeth_unguarded_complete_adds_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(route_cmd, "_rollback_complete_creepage", lambda *a, **k: [])
    _rc, out, _report = _complete(tmp_path, "unguarded")
    assert len(_failing_pairs(out)) >= 1


# ---------------------------------------------------------------------------
# Issue #9685: the copper-vs-board-edge column of the same gate.
# ---------------------------------------------------------------------------

EDGE_STAGE = FIXTURE / "edge_fixture.kicad_pcb"
EDGE_NCM = FIXTURE / "edge_net_class_map.json"


def _complete_edge(tmp_path: Path, name: str) -> tuple[int, Path, Path]:
    out = tmp_path / f"{name}.kicad_pcb"
    report = tmp_path / f"{name}.json"
    rc = route_cmd.main(
        [
            str(EDGE_STAGE),
            "-o",
            str(out),
            "--net-class-map",
            str(EDGE_NCM),
            "--voltage-map",
            str(FIXTURE / "voltage_map.json"),
            "--creepage-standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
            "--hv-threshold",
            "0",
            "--skip-drc",
            "--no-auto-pour",
            "--complete",
            "--complete-report",
            str(report),
        ]
    )
    return rc, out, report


def _failing_edge_pairs(pcb: Path) -> list[dict]:
    """Census referee with the #9685 consumer's flags (--waive-same-footprint
    only -- no attach-zone waiver), so the referee is at least as strict as a
    real consumer's post-route check."""
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "creepage",
            str(pcb),
            "--net-class-map",
            str(EDGE_NCM),
            "--voltage-map",
            str(FIXTURE / "voltage_map.json"),
            "--standard",
            "iec60664",
            "--pollution-degree",
            "2",
            "--material-group",
            "IIIa",
            "--waive-same-footprint",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    return [p for p in json.loads(proc.stdout)["pairs"] if not p["pass"]]


def test_edge_stage_input_is_creepage_clean_including_board_edge():
    assert _failing_edge_pairs(EDGE_STAGE) == []


def test_complete_rolls_back_edge_creepage_and_output_stays_clean(tmp_path):
    rc, out, report = _complete_edge(tmp_path, "edge-guarded")
    # The edge-violating link is rolled back and reported per link -- never a
    # silent exit 0 with census-failing copper (issue #9685).
    assert rc != 0
    data = json.loads(report.read_text())
    rolled = [r for r in data["unroutable_links"] if r["reason"] == "creepage-rolled-back"]
    assert any(r["blocking_net"] == "<board edge>" for r in rolled)
    for r in rolled:
        assert r["measured_mm"] < r["required_mm"] == 1.6
    gate = data["creepage_gate"]
    assert gate["rolled_back"] == len(rolled)
    assert gate["committed_links_verified"] >= 1
    assert "copper vs board edge" in gate["checks"]
    assert _failing_edge_pairs(out) == []


@pytest.mark.slow
def test_edge_fixture_has_teeth_unguarded_complete_adds_edge_failures(tmp_path, monkeypatch):
    monkeypatch.setattr(route_cmd, "_rollback_complete_creepage", lambda *a, **k: [])
    _rc, out, _report = _complete_edge(tmp_path, "edge-unguarded")
    failing = _failing_edge_pairs(out)
    edge_rows = [p for p in failing if p["kind"] == "edge" and p["net_b"] == "<board edge>"]
    assert edge_rows, "edge fixture no longer makes --complete propose edge-violating copper"
