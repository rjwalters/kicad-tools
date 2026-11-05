"""Tests for the pour-slot cleanup pass (Issue #8440)."""

from __future__ import annotations

from dataclasses import dataclass, field

from kicad_tools.optim.pour_slot_cleanup import cleanup_pour_slots
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.rules.pour_slot import PourSlotRule, iter_pour_slot_pairs


@dataclass
class _Zone:
    layer: str = "F.Cu"
    layers: list = field(default_factory=list)
    keepout: object = None
    clearance: float = 0.3
    min_thickness: float = 0.25


# Threshold with the defaults above: 2 * 0.3 + 0.25 = 0.85 mm.


def _board(gap, *, b_end=(10.0, None), extra=None):
    pcb = PCB.create(width=50, height=50, center=False)
    pcb.zones.append(_Zone())
    pitch = gap + 0.2
    pcb.add_trace((10, 10), (20, 10), width=0.2, layer="F.Cu", net="A")
    by = 10 + pitch
    pcb.add_trace((10, by), (b_end[0] + 10, by), width=0.2, layer="F.Cu", net="B")
    for fn in extra or []:
        fn(pcb)
    return pcb


def _ok(_pcb):
    return {"clearance": 0, "unconnected": 0}


def _gaps(pcb):
    return sorted(round(p.gap, 3) for p in iter_pour_slot_pairs(pcb))


def _seg(pcb, name):
    return next(s for s in pcb.segments if s.net_name == name)


def test_spread_widens_gap_to_threshold():
    pcb = _board(0.7)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.spread == 1 and res.left_alone == 0
    assert list(iter_pour_slot_pairs(pcb)) == []
    gap = abs(_seg(pcb, "B").start[1] - _seg(pcb, "A").start[1]) - 0.2
    assert gap >= 0.85


def test_pack_when_spread_blocked_by_third_trace():
    def blocker(pcb):  # a net-C via just beyond where spread would land
        pcb.add_via(15, 11.4, size=0.6, net="C")  # blocks spreading B
        pcb.add_via(15, 9.3, size=0.6, net="C")  # blocks spreading A

    pcb = _board(0.7, extra=[blocker])
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.packed == 1 and res.spread == 0
    gap = abs(_seg(pcb, "B").start[1] - _seg(pcb, "A").start[1]) - 0.2
    assert abs(gap - 0.3) < 1e-3


def test_left_alone_when_neither_is_clean():
    def blocker(pcb):
        pcb.add_via(15, 11.4, size=0.6, net="C")  # blocks spreading B
        pcb.add_via(15, 9.3, size=0.6, net="C")  # blocks spreading A
        pcb.add_via(15, 10.4, size=0.6, net="D")  # blocks packing either run

    pcb = _board(0.7, extra=[blocker])
    before = [(s.start, s.end) for s in pcb.segments]
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.left_alone >= 1
    assert [(s.start, s.end) for s in pcb.segments] == before


def test_referee_regression_reverts_edit():
    calls = []

    def referee(_pcb):
        calls.append(1)
        return {"clearance": 0, "unconnected": 0} if len(calls) == 1 else {"unconnected": 1}

    pcb = _board(0.7)
    before = [(s.start, s.end) for s in pcb.segments]
    res = cleanup_pour_slots(pcb, referee=referee)
    assert not res.changed and res.left_alone == 1
    assert sorted((s.start, s.end) for s in pcb.segments) == sorted(before)


def test_unavailable_referee_refuses_without_editing():
    pcb = _board(0.7)
    before = [(s.start, s.end) for s in pcb.segments]
    res = cleanup_pour_slots(pcb, referee=lambda _p: None)
    assert res.refused_reason and not res.changed
    assert [(s.start, s.end) for s in pcb.segments] == before


def test_connected_neighbour_follows_moved_run():
    def stub(pcb):  # B continues perpendicular from its end
        pcb.add_trace((20, 10.9), (20, 14), width=0.2, layer="F.Cu", net="B")

    pcb = _board(0.7, extra=[stub])
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.spread == 1
    run = next(s for s in pcb.segments if s.net_name == "B" and s.start[0] != s.end[0])
    stub_seg = next(s for s in pcb.segments if s.net_name == "B" and s.start[0] == s.end[0])
    assert stub_seg.start == run.end  # connectivity preserved


def _via_ends(pcb):
    pcb.add_via(20, 10.9, net="B")
    pcb.add_via(20, 10.0, net="A")
    pcb.add_via(10, 10.9, net="B")
    pcb.add_via(10, 10.0, net="A")


def test_via_ended_runs_are_spread_with_45_degree_jogs():
    pcb = _board(0.7, extra=[_via_ends])
    vias = sorted(v.position for v in pcb.vias)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert res.changed and res.left_alone == 0
    assert sorted(v.position for v in pcb.vias) == vias  # vias never move
    assert list(iter_pour_slot_pairs(pcb)) == []
    for name in ("A", "B"):
        for via_xy in [v.position for v in pcb.vias if v.net_name == name]:
            # some same-net segment still ends exactly on the via
            assert any(
                s.net_name == name
                and (
                    (abs(s.start[0] - via_xy[0]) < 1e-3 and abs(s.start[1] - via_xy[1]) < 1e-3)
                    or (abs(s.end[0] - via_xy[0]) < 1e-3 and abs(s.end[1] - via_xy[1]) < 1e-3)
                )
                for s in pcb.segments
            )
    diag = [
        s
        for s in pcb.segments
        if abs(abs(s.end[0] - s.start[0]) - abs(s.end[1] - s.start[1])) < 1e-3
        and abs(s.end[0] - s.start[0]) > 1e-3
    ]
    assert len(diag) >= 2


def test_via_ended_run_left_alone_when_jog_blocked():
    def blocker(pcb):
        _via_ends(pcb)
        for y in (9.3, 11.4):  # blocks every spread leg
            pcb.add_via(17, y, size=0.6, net="C")
            pcb.add_via(13, y, size=0.6, net="C")
        pcb.add_via(15, 10.45, size=0.6, net="D")  # blocks packing

    pcb = _board(0.7, extra=[blocker])
    before = [(s.start, s.end) for s in pcb.segments]
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert not res.changed and res.left_alone >= 1
    assert [(s.start, s.end) for s in pcb.segments] == before


def test_different_net_via_at_endpoint_refuses():
    def via(pcb):
        pcb.add_via(20, 10.9, net="C")
        pcb.add_via(20, 10.0, net="C")

    pcb = _board(0.7, extra=[via])
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert not res.changed and res.left_alone == 1


def test_via_jog_referee_regression_reverts():
    calls = []

    def referee(_pcb):
        calls.append(1)
        return {"unconnected": 0} if len(calls) == 1 else {"unconnected": 1}

    pcb = _board(0.7, extra=[_via_ends])
    before = sorted((s.start, s.end) for s in pcb.segments)
    res = cleanup_pour_slots(pcb, referee=referee)
    assert not res.changed
    assert sorted((s.start, s.end) for s in pcb.segments) == before


def test_detection_shared_with_rule():
    pcb = _board(0.7)
    pairs = list(iter_pour_slot_pairs(pcb))
    violations = PourSlotRule().check(pcb, None).violations
    assert len(pairs) == len(violations) == 1
    assert pairs[0].location == violations[0].location


def test_idempotent():
    pcb = _board(0.7)
    cleanup_pour_slots(pcb, referee=_ok)
    snap = sorted((s.start, s.end) for s in pcb.segments)
    res = cleanup_pour_slots(pcb, referee=_ok)
    assert not res.changed
    assert sorted((s.start, s.end) for s in pcb.segments) == snap
