"""Tests for ``kct pcb prune`` / ``kicad_tools.pcb_prune`` (issue #8216)."""

from __future__ import annotations

import json
from itertools import count
from pathlib import Path

import pytest

from kicad_tools.pcb_prune import prune_board
from kicad_tools.schema.pcb import PCB

HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "SIG")
"""

_uid = count(1)


def _fp(ref: str, x: float, y: float, w: float = 1, h: float = 1) -> str:
    return (
        f'  (footprint "X:P" (layer "F.Cu") (uuid "f-{ref}") (at {x} {y})\n'
        f'    (property "Reference" "{ref}" (at 0 0 0) (layer "F.SilkS"))\n'
        f'    (pad "1" smd rect (at 0 0) (size {w} {h}) (layers "F.Cu") (net 1 "SIG"))\n'
        "  )\n"
    )


def _seg(a, b, layer="F.Cu", w=0.2) -> str:
    return (
        f"  (segment (start {a[0]} {a[1]}) (end {b[0]} {b[1]}) (width {w}) "
        f'(layer "{layer}") (net 1) (uuid "s-{next(_uid)}"))\n'
    )


def _via(x, y) -> str:
    return (
        f'  (via (at {x} {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) '
        f'(uuid "v-{next(_uid)}"))\n'
    )


def _board(tmp_path: Path, pads, segs, vias=()) -> Path:
    body = HEADER + "".join(_fp(*pad) for pad in pads)
    body += "".join(_seg(*s) if isinstance(s[0], tuple) else _seg(s) for s in segs)
    body += "".join(_via(*v) for v in vias) + ")\n"
    p = tmp_path / "b.kicad_pcb"
    p.write_text(body)
    return p


def _run(p: Path):
    pcb, res = prune_board(p, verify_drc=False)
    return pcb, res


def test_redundant_branch_next_to_via_is_removed_and_main_kept(tmp_path):
    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (105, 100)),
        ((105, 100), (110, 100)),
        # branch with an off-centre end landing on the via
        ((102, 100), (102, 102)),
        ((102, 102), (105.15, 102)),
        ((105.15, 102), (105.15, 100.25)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs, vias=[(105, 100)])
    pcb, res = _run(p)
    # 3 branch segments + 1 from merging the collinear 100->102->105 main run
    assert res.stats["segments_removed"] == 4
    assert len(pcb.segments) == 2
    assert {s.start for s in pcb.segments} | {s.end for s in pcb.segments} >= {
        (100, 100),
        (110, 100),
    }


def test_overlap_only_off_centre_ends_are_snapped(tmp_path):
    segs = [((100, 100), (104.8, 100)), ((110, 100), (105.2, 100))]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs, vias=[(105, 100)])
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 2}
    ends = sorted(s.end for s in pcb.segments)
    assert ends == [(105, 100), (105, 100)]
    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)
    assert sorted(s.end for s in PCB.load(out).segments) == [(105, 100), (105, 100)]


def test_same_layer_loop_removes_longer_detour(tmp_path):
    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (103, 100)),
        ((103, 100), (110, 100)),
        ((102, 100), (102, 101)),
        ((102, 101), (103, 101)),
        ((103, 101), (103, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs)
    pcb, res = _run(p)
    # detour removed (3), then the remaining collinear main run merges (2 more)
    assert res.stats["segments_removed"] == 5
    assert [(s.start, s.end) for s in pcb.segments] == [((100, 100), (110, 100))]


def test_removal_that_would_disconnect_a_pad_is_kept(tmp_path):
    segs = [
        ((100, 100), (110, 100)),
        ((105, 100), (105, 103)),  # spur to pad C: not redundant
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100), ("C", 105, 103)], segs)
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 0}
    assert len(pcb.segments) == 2


def test_cli_dry_run_json_leaves_file_untouched(tmp_path, capsys):
    from kicad_tools.cli.parser import create_parser

    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (103, 100)),
        ((103, 100), (110, 100)),
        ((102, 100), (102, 101)),
        ((102, 101), (103, 101)),
        ((103, 101), (103, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs)
    before = p.read_text()
    args = create_parser().parse_args(
        ["pcb", "prune", str(p), "--dry-run", "--no-drc", "--format", "json"]
    )
    from kicad_tools.cli.commands.pcb import _run_prune_command

    assert _run_prune_command(args, p) == 0
    out = json.loads(capsys.readouterr().out)
    assert out["removed"]["segments"] == 5 and out["drc_verified"] is False
    assert p.read_text() == before


def _connected(pcb, a, b) -> bool:
    """Endpoint union-find: is ``a`` linked to ``b`` through the segments?"""
    adj: dict = {}
    for s in pcb.segments:
        adj.setdefault(s.start, set()).add(s.end)
        adj.setdefault(s.end, set()).add(s.start)
    seen, stack = {a}, [a]
    while stack:
        for v in adj.get(stack.pop(), ()):
            if v not in seen:
                seen.add(v)
                stack.append(v)
    return b in seen


def test_wide_pad_off_centre_entry_keeps_whole_net(tmp_path):
    """A track entering a 3x0.5 pad 1.2 mm off-centre is anchored by the full
    pad shape, not the inscribed circle; the net must survive intact."""
    segs = [
        ((101.2, 100), (105, 100)),
        ((105, 100), (107, 100)),
        ((107, 100), (110, 100)),
        ((105, 100), (105, 102)),
        ((105, 102), (107, 102)),
        ((107, 102), (107, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100, 3, 0.5), ("B", 110, 100)], segs)
    pcb, _ = _run(p)
    # The entry end sits inside the pad, so it is anchored (it may be snapped
    # to the pad centre) and the pad stays connected to B.
    entry = min(pcb.segments, key=lambda s: s.start[0]).start
    assert entry in {(101.2, 100), (100, 100)}
    assert _connected(pcb, entry, (110, 100))


def test_tangent_capsule_jog_is_kept(tmp_path):
    """Neighbours whose capsules are exactly tangent are not joined by KiCad."""
    segs = [
        ((100, 100), (101, 100)),
        ((101, 100), (101, 100.2)),  # the jog
        ((101, 100.2), (102, 100.2)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 102, 100.2)], segs)
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 0}
    assert len(pcb.segments) == 3


def test_collinear_step_run_is_merged_into_one_segment(tmp_path):
    """Ten 0.25 mm steps become one 2.5 mm segment spanning the same copper."""
    segs = [((100 + 0.25 * k, 100), (100 + 0.25 * (k + 1), 100)) for k in range(10)]
    p = _board(tmp_path, [("A", 100, 100, 0.2, 0.2), ("B", 102.5, 100, 0.2, 0.2)], segs)
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 9, "ends_snapped": 0}
    assert len(pcb.segments) == 1
    s = pcb.segments[0]
    assert sorted([s.start, s.end]) == [pytest.approx((100, 100)), pytest.approx((102.5, 100))]


def test_collinear_merge_keeps_joint_with_via(tmp_path):
    segs = [((100, 100), (101, 100)), ((101, 100), (102, 100))]
    p = _board(tmp_path, [("A", 100, 100), ("B", 102, 100)], segs, vias=[(101, 100)])
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 0}
    assert len(pcb.segments) == 2


def test_collinear_merge_keeps_joint_with_third_segment(tmp_path):
    segs = [
        ((100, 100), (101, 100)),
        ((101, 100), (102, 100)),
        ((101, 100), (101, 101)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 102, 100), ("C", 101, 101)], segs)
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 0}
    assert len(pcb.segments) == 3


def test_non_collinear_or_mixed_width_joint_is_not_merged(tmp_path):
    segs = [((100, 100), (101, 100)), ((101, 100), (102, 100.5))]
    p = _board(tmp_path, [("A", 100, 100), ("B", 102, 100.5)], segs)
    pcb, res = _run(p)
    assert len(pcb.segments) == 2
    segs = [((100, 100), (101, 100), "F.Cu", 0.2), ((101, 100), (102, 100), "F.Cu", 0.3)]
    p = _board(tmp_path, [("A", 100, 100), ("B", 102, 100)], segs)
    pcb, res = _run(p)
    assert len(pcb.segments) == 2


def _cli(tmp_path, p, *extra):
    from kicad_tools.cli.commands.pcb import _run_prune_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(["pcb", "prune", str(p), "--format", "json", *extra])
    return _run_prune_command(args, p)


def test_cli_refuses_to_write_unverified_result(tmp_path, capsys):
    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (103, 100)),
        ((103, 100), (110, 100)),
        ((102, 100), (102, 101)),
        ((102, 101), (103, 101)),
        ((103, 101), (103, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs)
    before = p.read_text()
    assert _cli(tmp_path, p, "--no-drc") == 1
    assert p.read_text() == before
    capsys.readouterr()
    assert _cli(tmp_path, p, "--no-drc", "--unverified") == 0
    assert p.read_text() != before


def test_bridge_behind_unmodelled_join_is_kept(tmp_path):
    """The pad-to-via stub is the pad's only connection.  The far side reaches
    pad B through a mid-span T-junction the model does not treat as a join, so
    the terminal partition already splits A from B; removing the stub must
    still be refused because it disconnects the pad from the via."""
    segs = [
        ((100, 100), (100, 102)),  # the stub: pad A -> via V
        ((100, 102), (106, 102), "B.Cu"),
        ((104, 105), (104, 102), "B.Cu"),  # ends mid-span: unmodelled join
        ((104, 105), (106, 105)),
        ((106, 105), (108, 105)),
        ((108, 105), (110, 105)),
        # a loop so the planner runs at all
        ((106, 105), (106, 107)),
        ((106, 107), (108, 107)),
        ((108, 107), (108, 105)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 105)], segs, vias=[(100, 102), (104, 105)])
    pcb, _ = _run(p)
    assert ((100, 100), (100, 102)) in {(s.start, s.end) for s in pcb.segments}


def test_zone_outline_is_not_relied_on_to_keep_copper_connected(tmp_path):
    """Every end sits inside a same-net zone outline, but the fill can void
    there; the zone must not make the pad-to-pad track look redundant."""
    zone = (
        '  (zone (net 1) (net_name "SIG") (layer "F.Cu") (uuid "z1") (hatch edge 0.5)\n'
        "    (connect_pads (clearance 0.2)) (min_thickness 0.25)\n"
        "    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
        "    (polygon (pts (xy 98 98) (xy 112 98) (xy 112 106) (xy 98 106))))\n"
    )
    segs = [
        ((100, 100), (102, 102)),
        ((102, 102), (106, 102)),
        ((106, 102), (110, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs)
    p.write_text(p.read_text()[: -len(")\n")] + zone + ")\n")
    pcb, res = _run(p)
    assert res.stats["segments_removed"] == 0
    assert len(pcb.segments) == 3


def test_snap_never_collapses_a_segment(tmp_path):
    """A stub with both ends off-centre inside one via must not be snapped to
    a zero-length segment."""
    segs = [
        ((100, 100), (105, 100)),
        ((104.9, 100.1), (105.1, 100.1), "B.Cu"),
    ]
    p = _board(tmp_path, [("A", 100, 100)], segs, vias=[(105, 100)])
    pcb, _ = _run(p)
    assert all(s.start != s.end for s in pcb.segments)


def test_snap_moves_a_shared_off_centre_joint_together(tmp_path):
    """Two tracks meet at one off-centre point inside a via: both ends move to
    the centre together, so neither is left joined only by overlap."""
    segs = [
        ((100, 100), (105.1, 100)),
        ((105.1, 100), (105.1, 105)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 105.1, 105)], segs, vias=[(105, 100)])
    pcb, res = _run(p)
    ends = [s.end for s in pcb.segments if s.start == (100, 100)] + [
        s.start for s in pcb.segments if s.end == (105.1, 105)
    ]
    assert res.stats["ends_snapped"] == 2
    assert ends == [(105, 100), (105, 100)]


def _spur_board(tmp_path: Path, branch_end=(110, 105)) -> Path:
    """#8216 pattern 1 (geometry from #8236 §1): a 4-layer board whose redundant
    B.Cu branch starts 0.35 mm off V1's centre, so its body touches the main
    0.4 mm diagonal without a shared endpoint."""
    header = HEADER.replace(
        '(0 "F.Cu" signal) (31 "B.Cu" signal)',
        '(0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)',
    )
    body = header + _fp("P1", 100, 100.35) + _fp("P2", 112, 105)
    w = 0.4
    body += _seg((105, 100), (110, 105), "B.Cu", w)  # main diagonal V1 -> V2
    body += _seg((110, 105), (112, 105), "F.Cu", w)
    body += _seg((100, 100.35), (105, 100.35), "In1.Cu", w)  # ends 0.35 off V1
    body += _seg((105, 100.35), (104.2, 101.5), "B.Cu", w)  # redundant branch
    body += _seg((104.2, 101.5), (108.5, 105.5), "B.Cu", w)
    body += _seg((108.5, 105.5), branch_end, "B.Cu", w)
    for xy in ((100, 100.35), (105, 100), (110, 105)):
        body += (
            f'  (via (at {xy[0]} {xy[1]}) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") '
            f'(net 1) (uuid "v-{next(_uid)}"))\n'
        )
    p = tmp_path / "spur.kicad_pcb"
    p.write_text(body + ")\n")
    return p


def test_spur_beside_via_is_removed_and_main_diagonal_kept(tmp_path):
    pcb, res = _run(_spur_board(tmp_path))
    assert res.stats["segments_removed"] == 3
    b_cu = [(s.start, s.end) for s in pcb.segments if s.layer == "B.Cu"]
    assert b_cu == [((105, 100), (110, 105))]
    # the inner-layer end that sat 0.35 mm off the via centre is snapped on
    in1 = [s for s in pcb.segments if s.layer == "In1.Cu"]
    assert [s.end for s in in1] == [(105, 100)]


def test_spur_is_kept_when_removal_would_disconnect_the_main_track(tmp_path):
    # Branch no longer reaches V2: it is not redundant, nothing may go.
    pcb, res = _run(_spur_board(tmp_path, branch_end=(109, 105.5)))
    assert res.stats["segments_removed"] == 0
