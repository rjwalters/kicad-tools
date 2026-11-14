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


# --------------------------------------------------------------------------
# Issue #8915: a leg below the minimum wire length is not on the cut list
# --------------------------------------------------------------------------


def _short_board(tmp_path: Path, length: float) -> Path:
    p = tmp_path / "short.kicad_pcb"
    _two_net_pcb(length=length).save(p)
    return p


def test_leg_below_min_wire_length_writes_no_record(tmp_path):
    """The owner does not want a wire on a sub-15 mm leg, so it is absent from
    the cut-and-bend list -- not listed with an empty ``anchors``."""
    p = _short_board(tmp_path, 12.0)
    assert run_pcb_command(_args(p)) == 0
    # No wire at all on the net -> nothing to write, so no sidecar is created.
    assert not sidecar_path(p).exists()


def test_min_wire_length_zero_still_writes_the_record(tmp_path):
    """Control for the test above: the record's absence is the minimum's doing,
    not something else about a 12 mm leg."""
    p = _short_board(tmp_path, 12.0)
    assert run_pcb_command(_args(p, min_wire_length=0.0)) == 0
    wires = load_wires(sidecar_path(p))
    assert [(w["net"], w["status"], len(w["anchors"])) for w in wires] == [("NET_A", "full", 2)]


def test_short_leg_does_not_displace_another_net_s_wire(tmp_path):
    """A skipped leg must not be written, and must not disturb the entries a
    previous pass wrote for a different net."""
    p = tmp_path / "mixed.kicad_pcb"
    pcb = PCB.create(width=200, height=100, center=False)
    # NET_A is long enough for a wire; NET_B is below the minimum.
    pcb.add_trace((20.0, 30.0), (120.0, 30.0), width=2.0, layer="F.Cu", net="NET_A")
    pcb.add_trace((20.0, 70.0), (32.0, 70.0), width=2.0, layer="F.Cu", net="NET_B")
    pcb.save(p)

    assert run_pcb_command(_args(p, net="NET_A")) == 0
    after_a = load_wires(sidecar_path(p))
    assert [w["net"] for w in after_a] == ["NET_A"]

    assert run_pcb_command(_args(p, net="NET_B")) == 0
    after_b = load_wires(sidecar_path(p))
    assert after_b == after_a, "NET_B has no wire; NET_A's entry must be untouched"


def test_unanchored_leg_that_still_wants_a_wire_keeps_its_record(tmp_path):
    """Only the length filter removes a record. A leg left unanchored for any
    other reason still names copper that WANTS a wire, so it stays on the list
    with its reason -- the pre-#8915 behaviour."""
    p = tmp_path / "unselected.kicad_pcb"
    pcb = PCB.create(width=200, height=100, center=False)
    # Two legs on one net, both over the minimum; without --all-runs only the
    # longest is anchored and the other is reported ``unanchored``.
    pcb.add_trace((20.0, 30.0), (120.0, 30.0), width=2.0, layer="F.Cu", net="NET_A")
    pcb.add_trace((20.0, 70.0), (60.0, 70.0), width=2.0, layer="F.Cu", net="NET_A")
    pcb.save(p)

    assert run_pcb_command(_args(p, net="NET_A")) == 0
    wires = load_wires(sidecar_path(p))
    statuses = sorted(w["status"] for w in wires)
    assert statuses == ["full", "unanchored"], wires
    unanchored = next(w for w in wires if w["status"] == "unanchored")
    assert unanchored["anchors"] == []
    assert "only the longest leg" in unanchored["reason"]


# --------------------------------------------------------------------------
# Issue #8916: parallel-copy records
# --------------------------------------------------------------------------


def test_absent_strategy_reads_as_wire():
    """Pre-#8916 records carry no ``strategy``; they stay wires (no version bump)."""
    from kicad_tools.pcb.reinforcement_sidecar import SIDECAR_VERSION, is_parallel

    assert SIDECAR_VERSION == 1
    assert not is_parallel({"net": "N", "layer": "F.Cu"})
    assert is_parallel({"net": "N", "layer": "F.Cu", "strategy": "parallel"})


def test_parallel_record_key_ignores_which_side_it_was_met_from():
    """A re-run that meets the leg from its copy's side replaces the record."""
    from kicad_tools.pcb.reinforcement_sidecar import wire_key

    def w(layer, copy_layer):
        return {
            "net": "N",
            "layer": layer,
            "copy_layer": copy_layer,
            "strategy": "parallel",
            "leg_start": {"x": 0.0, "y": 0.0},
            "leg_end": {"x": 12.0, "y": 0.0},
        }

    assert wire_key(w("B.Cu", "F.Cu")) == wire_key(w("F.Cu", "B.Cu"))
    assert len(merge_wires([w("B.Cu", "F.Cu")], [w("F.Cu", "B.Cu")])) == 1
    wire = {k: v for k, v in w("B.Cu", "F.Cu").items() if k not in ("strategy", "copy_layer")}
    assert wire_key(wire) != wire_key(w("B.Cu", "F.Cu"))


def test_parallel_short_leg_is_recorded_and_cleared(tmp_path):
    """A 12 mm leg below the #8915 minimum: a wire gets no record, a copy does,
    and --clear removes the copy's copper and its record."""
    p = _short_board(tmp_path, 12.0)
    assert run_pcb_command(_args(p, strategy="parallel")) == 0
    wires = load_wires(sidecar_path(p))
    assert [(w["net"], w["strategy"], w["copy_layer"]) for w in wires] == [
        ("NET_A", "parallel", "B.Cu")
    ]
    assert any(s.layer == "B.Cu" for s in PCB.load(p).segments)
    assert run_pcb_command(_args(p, clear=True)) == 0
    assert not any(s.layer == "B.Cu" for s in PCB.load(p).segments)
    assert load_wires(sidecar_path(p)) == []
