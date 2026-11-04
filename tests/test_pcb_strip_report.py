"""Tests for report-driven strip and dangling removal (issue #8309)."""

import json
from argparse import Namespace
from pathlib import Path

import pytest

from kicad_tools.cli.commands.pcb import run_pcb_command
from kicad_tools.pcb_strip_report import (
    Finding,
    load_findings,
    plan_from_report,
    strip_dangling,
)
from kicad_tools.schema.pcb import PCB


def _seg(uid, x1, y1, x2, y2, net, name, layer="F.Cu"):
    return (
        f"  (segment (start {x1} {y1}) (end {x2} {y2}) (width 0.25) "
        f'(layer "{layer}") (net {net}) (uuid "{uid}"))\n'
    )


HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "MAINS")
  (net 2 "GND")
"""


@pytest.fixture
def board(tmp_path: Path) -> Path:
    body = (
        _seg("aaaaaaaa-0000-0000-0000-000000000001", 110, 110, 120, 110, 1, "MAINS")
        + _seg("bbbbbbbb-0000-0000-0000-000000000002", 110, 110.4, 120, 110.4, 2, "GND")
        + _seg("cccccccc-0000-0000-0000-000000000003", 120, 110, 125, 110, 1, "MAINS")
    )
    p = tmp_path / "b.kicad_pcb"
    p.write_text(HEADER + body + ")\n")
    return p


@pytest.fixture
def spur_board(tmp_path: Path) -> Path:
    # 2-deep dangling chain: A-B-C, nothing at either end -> pass 1 removes
    # both free ends' tracks, pass 2 the rest.
    body = (
        _seg("aaaaaaaa-0000-0000-0000-000000000011", 110, 110, 115, 110, 1, "MAINS")
        + _seg("aaaaaaaa-0000-0000-0000-000000000012", 115, 110, 120, 110, 1, "MAINS")
        + _seg("aaaaaaaa-0000-0000-0000-000000000013", 120, 110, 125, 110, 1, "MAINS")
    )
    p = tmp_path / "s.kicad_pcb"
    p.write_text(HEADER + body + ")\n")
    return p


def _drc_json(path: Path, uuids, loc=(115.0, 110.2)):
    path.write_text(
        json.dumps(
            {
                "violations": [
                    {
                        "type": "clearance",
                        "severity": "error",
                        "description": "Clearance violation",
                        "items": [
                            {
                                "description": "Track [MAINS]",
                                "uuid": u,
                                "pos": {"x": loc[0], "y": loc[1]},
                            }
                            for u in uuids
                        ],
                    }
                ],
                "unconnected_items": [],
                "schematic_parity": [],
            }
        )
    )
    return path


UA = "aaaaaaaa-0000-0000-0000-000000000001"
UB = "bbbbbbbb-0000-0000-0000-000000000002"


def test_one_victim_per_finding(board, tmp_path):
    pcb = PCB.load(board)
    findings = load_findings(_drc_json(tmp_path / "r.json", [UA, UB]))
    plan = plan_from_report(pcb, findings)
    assert [s.uuid for s in plan.segments] == [UA]
    assert plan.nets_cut == ["MAINS"]


def test_protected_net_never_cut(board, tmp_path):
    pcb = PCB.load(board)
    findings = load_findings(_drc_json(tmp_path / "r.json", [UA, UB]))
    plan = plan_from_report(pcb, findings, protect={"MAINS"})
    assert [s.uuid for s in plan.segments] == [UB]


def test_both_protected_skipped(board, tmp_path):
    pcb = PCB.load(board)
    findings = load_findings(_drc_json(tmp_path / "r.json", [UA, UB]))
    plan = plan_from_report(pcb, findings, protect={"MAINS", "GND"})
    assert not plan.segments
    assert plan.results[0].status == "skipped" and plan.results[0].reason == "protected"


def test_missing_uuid_skipped(board, tmp_path):
    pcb = PCB.load(board)
    findings = load_findings(_drc_json(tmp_path / "r.json", ["dead-beef"], loc=(0.0, 0.0)))
    plan = plan_from_report(pcb, findings)
    assert plan.results[0].reason == "item not found"


def test_radius_removes_adjacent_same_net(board, tmp_path):
    pcb = PCB.load(board)
    findings = load_findings(_drc_json(tmp_path / "r.json", [UA, UB], loc=(120.0, 110.0)))
    plan = plan_from_report(pcb, findings, radius=1.0)
    assert {s.uuid[:8] for s in plan.segments} == {"aaaaaaaa", "cccccccc"}


def test_creepage_rows_witness(board):
    pcb = PCB.load(board)
    data = {
        "rows": [
            {"net_a": "MAINS", "net_b": "GND"},
            {"net_a": "MAINS", "net_b": "GND", "witness": {"point": [115.0, 110.0], "uuid": UA}},
        ]
    }
    findings = [Finding("creepage", "creepage", None, [], [])] + load_findings_from(data)
    plan = plan_from_report(pcb, findings)
    reasons = [r.reason for r in plan.results if r.status == "skipped"]
    assert "no location" in reasons
    assert [s.uuid for s in plan.segments] == [UA]


def load_findings_from(data):
    from kicad_tools.pcb_strip_report import _creepage_findings

    return _creepage_findings(data)[1:]


def test_dangling_two_passes(spur_board):
    pcb = PCB.load(spur_board)
    res = strip_dangling(pcb)
    assert res.segments == 3
    assert res.passes >= 2
    assert not pcb.segments


def _ns(pcb, **kw):
    base = {
        "pcb": str(pcb),
        "pcb_command": "strip",
        "output": None,
        "nets": None,
        "layers": None,
        "region": None,
        "keep_zones": True,
        "format": "json",
        "dry_run": False,
        "from_report": None,
        "dangling": False,
        "protect_nets": None,
        "radius": 0.0,
        "keep_via_min": None,
        "unverified": True,
    }
    base.update(kw)
    return Namespace(**base)


def test_cli_dry_run_writes_nothing(board, tmp_path, capsys):
    rpt = _drc_json(tmp_path / "r.json", [UA, UB])
    rc = run_pcb_command(_ns(board, from_report=str(rpt), dry_run=True))
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["removed"] == {"segments": 1, "vias": 0}
    assert not list(tmp_path.glob("*-stripped*"))
    assert len(PCB.load(board).segments) == 3


def test_cli_real_run(board, tmp_path):
    rpt = _drc_json(tmp_path / "r.json", [UA, UB])
    out = tmp_path / "o.kicad_pcb"
    assert run_pcb_command(_ns(board, from_report=str(rpt), output=str(out))) == 0
    assert {s.uuid[:8] for s in PCB.load(out).segments} == {"bbbbbbbb", "cccccccc"}


def test_cli_mutually_exclusive(board, tmp_path, capsys):
    rpt = _drc_json(tmp_path / "r.json", [UA])
    assert run_pcb_command(_ns(board, from_report=str(rpt), nets="GND")) == 2
    assert run_pcb_command(_ns(board, from_report=str(rpt), dangling=True)) == 2


def test_cli_refuses_without_kicad_cli(board, tmp_path, monkeypatch):
    import kicad_tools.pcb_prune as pp

    monkeypatch.setattr(pp, "_drc_signature", lambda p: None)
    rpt = _drc_json(tmp_path / "r.json", [UA])
    out = tmp_path / "o.kicad_pcb"
    rc = run_pcb_command(_ns(board, from_report=str(rpt), output=str(out), unverified=False))
    assert rc == 1 and not out.exists()


def _via(uid, x, y, net, size=0.8, drill=0.4):
    return (
        f"  (via (at {x} {y}) (size {size}) (drill {drill}) "
        f'(layers "F.Cu" "B.Cu") (net {net}) (uuid "{uid}"))\n'
    )


@pytest.fixture
def via_spur_board(tmp_path: Path) -> Path:
    # A lone via at the end of a track: the via (and track) are dangling.
    body = _seg("aaaaaaaa-0000-0000-0000-000000000021", 110, 110, 115, 110, 1, "MAINS") + _via(
        "dddddddd-0000-0000-0000-000000000022", 115, 110, 1
    )
    p = tmp_path / "v.kicad_pcb"
    p.write_text(HEADER + body + ")\n")
    return p


def test_keep_via_min_preserves_large_vias(via_spur_board):
    kept = PCB.load(via_spur_board)
    res = strip_dangling(kept, keep_via_min=0.3)
    assert res.vias == 0
    assert res.kept_vias >= 1
    assert len(kept.vias) == 1

    removed = PCB.load(via_spur_board)
    res = strip_dangling(removed, keep_via_min=None)
    assert res.kept_vias == 0
    assert res.vias == 1
    assert not removed.vias


def test_cli_rollback_when_drc_worse(board, tmp_path, monkeypatch, capsys):
    """New DRC violations after the edit: refuse, write nothing."""
    import kicad_tools.cli.commands.pcb as pcb_cmd

    monkeypatch.setattr(pcb_cmd, "_verify_strip_drc", lambda p, pcb: (True, "NEW violations", True))
    rpt = _drc_json(tmp_path / "r.json", [UA, UB])
    out = tmp_path / "o.kicad_pcb"
    rc = run_pcb_command(_ns(board, from_report=str(rpt), output=str(out), unverified=False))
    assert rc == 1
    assert not out.exists()
    assert len(PCB.load(board).segments) == 3
    assert "Refusing to write" in capsys.readouterr().err


def _sig(**counts):
    from collections import Counter

    return Counter({(t, "error", "", ()): n for t, n in counts.items()})


def _run_verify(monkeypatch, tmp_path, before, after):
    from kicad_tools import pcb_prune
    from kicad_tools.cli.commands.pcb import _verify_strip_drc

    seq = iter([before, after])
    monkeypatch.setattr(pcb_prune, "_drc_signature", lambda _p: next(seq))
    src = tmp_path / "b.kicad_pcb"
    src.write_text("(kicad_pcb)")

    class _P:
        def save(self, path):
            Path(path).write_text("(kicad_pcb)")

    return _verify_strip_drc(src, _P())


def test_verify_strip_ignores_dangling_and_unconnected_growth(monkeypatch, tmp_path):
    before = _sig(clearance=11, track_dangling=3, unconnected=57)
    after = _sig(clearance=0, track_dangling=6, unconnected=61, via_dangling=2)
    verified, _note, worse = _run_verify(monkeypatch, tmp_path, before, after)
    assert verified is True
    assert worse is False


def test_verify_strip_still_refuses_new_clearance(monkeypatch, tmp_path):
    before = _sig(clearance=1, track_dangling=3)
    after = _sig(clearance=2, track_dangling=6)
    _verified, _note, worse = _run_verify(monkeypatch, tmp_path, before, after)
    assert worse is True
