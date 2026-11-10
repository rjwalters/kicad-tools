"""Wire-list sidecar for ``kct pcb reinforce`` (Issue #8744)."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

from kicad_tools.cli.commands.pcb import run_pcb_command
from kicad_tools.pcb.reinforcement_sidecar import (
    load_wires,
    merge_wires,
    sidecar_path,
)
from kicad_tools.physics.wire_gauge import bare_copper_diameter_mm
from kicad_tools.schema.pcb import PCB


def _two_net_pcb(length: float = 100.0) -> PCB:
    pcb = PCB.create(width=200, height=100, center=False)
    for net, y in (("NET_A", 30.0), ("NET_B", 70.0)):
        pcb.add_trace((20.0, y), (20.0 + length, y), width=2.0, layer="F.Cu", net=net)
    return pcb


def _args(pcb_path: Path, net: str | None = "NET_A", **kw) -> argparse.Namespace:
    base = {
        "pcb_command": "reinforce",
        "pcb": str(pcb_path),
        "net": net,
        "wire_gauge": 16,
        "layer": None,
        "output": None,
        "format": "text",
        "dry_run": False,
    }
    base.update(kw)
    return argparse.Namespace(**base)


def _board(tmp_path: Path) -> Path:
    p = tmp_path / "board.kicad_pcb"
    _two_net_pcb().save(p)
    return p


def test_straight_run_one_wire(tmp_path):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p)) == 0
    wires = load_wires(sidecar_path(p))
    assert len(wires) == 1
    w = wires[0]
    assert w["net"] == "NET_A" and w["layer"] == "F.Cu" and w["status"] == "full"
    assert len(w["anchors"]) == 2
    assert all(abs(a["y"] - 30.0) < 1e-3 for a in w["anchors"])
    span = math.hypot(
        w["anchors"][1]["x"] - w["anchors"][0]["x"], w["anchors"][1]["y"] - w["anchors"][0]["y"]
    )
    assert abs(w["straight_line_mm"] - span) < 1e-3
    assert 90.0 <= w["length_mm"] <= 100.0
    assert w["wire_gauge_awg"] == 16
    assert w["bare_diameter_mm"] == round(bare_copper_diameter_mm(16), 4)
    assert w["mask_opening"] is None


def test_two_nets_no_duplicates_on_rerun(tmp_path):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, "NET_A")) == 0
    assert run_pcb_command(_args(p, "NET_B")) == 0
    assert {w["net"] for w in load_wires(sidecar_path(p))} == {"NET_A", "NET_B"}
    assert run_pcb_command(_args(p, "NET_A")) == 0
    nets = [w["net"] for w in load_wires(sidecar_path(p))]
    assert sorted(nets) == ["NET_A", "NET_B"]


def test_dry_run_writes_no_sidecar(tmp_path):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, dry_run=True)) == 0
    assert not sidecar_path(p).exists()


def test_deterministic_bytes(tmp_path):
    a, b = tmp_path / "a", tmp_path / "b"
    a.mkdir()
    b.mkdir()
    pa, pb = _board(a), _board(b)
    run_pcb_command(_args(pa))
    run_pcb_command(_args(pb))
    assert sidecar_path(pa).read_bytes() == sidecar_path(pb).read_bytes()


def test_merge_replaces_same_key_and_sorts():
    def w(net, x, status):
        return {
            "net": net,
            "layer": "F.Cu",
            "leg_start": {"x": x, "y": 0.0},
            "leg_end": {"x": x + 5, "y": 0.0},
            "status": status,
        }

    merged = merge_wires([w("B", 1, "old"), w("A", 1, "old")], [w("B", 1, "new")])
    assert [(m["net"], m["status"]) for m in merged] == [("A", "old"), ("B", "new")]


def test_list_wires_read_only(tmp_path, capsys):
    p = _board(tmp_path)
    run_pcb_command(_args(p))
    before = sidecar_path(p).read_bytes()
    pcb_before = p.read_bytes()
    capsys.readouterr()
    assert run_pcb_command(_args(p, net=None, list_wires=True, format="json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert len(doc["wires"]) == 1
    assert sidecar_path(p).read_bytes() == before and p.read_bytes() == pcb_before


def test_list_wires_missing_sidecar(tmp_path):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, net=None, list_wires=True)) == 1


def test_json_embeds_wires(tmp_path, capsys):
    p = _board(tmp_path)
    assert run_pcb_command(_args(p, format="json")) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["wires"] == load_wires(sidecar_path(p))
