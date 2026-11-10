"""Tests for ``kct pcb prune`` / ``kicad_tools.pcb_prune`` (issue #8216)."""

from __future__ import annotations

import json
import math
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


def test_tilting_snap_adds_on_grid_stub_to_via_centre(tmp_path):
    # Issue #8437: snapping the end onto the via centre would tilt a horizontal
    # track, so prune keeps the end and joins it to the centre with a 45-degree
    # piece plus an axis-aligned piece instead of leaving it overlap-held.
    from kicad_tools.pcb_prune import _aligned

    segs = [((100, 100), (104.82, 100))]
    p = _board(tmp_path, [("A", 100, 100)], segs, vias=[(105, 100.08)])
    pcb, res = _run(p)
    assert res.stats.get("stubs_added") == 2
    assert all(_aligned(s.start, s.end) for s in pcb.segments)
    stubs = [s for s in pcb.segments if math.dist(s.start, s.end) < 0.5]
    lengths = sorted(round(math.dist(s.start, s.end), 3) for s in stubs)
    assert lengths == [0.1, 0.113]
    ends = {pt for s in pcb.segments for pt in (s.start, s.end)}
    assert (104.82, 100.0) in ends and (105, 100.08) in ends


def test_tilt_refused_end_segment_count_is_pinned_and_stable(tmp_path):
    # Issue #8458: a routed board gained ~4% more segments after the first
    # ``prune`` than at c9a45f80a.  Bisecting the first-prune count pinned the
    # whole delta on 41e2387f9 (#8437): a tilt-refused via end now keeps its
    # off-centre end and is joined to the via centre by on-grid stubs instead
    # of being left overlap-held.  The extra segments are intended; pin the
    # exact count (original + 2 stubs) and that a second prune adds no more,
    # so the delta cannot silently grow or stop converging.
    segs = [((100, 100), (104.82, 100))]
    p = _board(tmp_path, [("A", 100, 100)], segs, vias=[(105, 100.08)])
    pcb, res = _run(p)
    assert len(pcb.segments) == 3
    assert res.stats.get("stubs_added") == 2
    pcb.save(p)
    pcb2, res2 = _run(p)
    assert len(pcb2.segments) == 3
    assert not res2.stats.get("stubs_added")


def test_single_pad_net_loop_is_kept(tmp_path):
    # Issue #8333: one-terminal nets have a vacuous partition guard; the whole
    # loop used to be deleted, leaving the net with no copper.
    loop = [
        ((100, 100), (105, 100)),
        ((105, 100), (105, 105)),
        ((105, 105), (100, 105)),
        ((100, 105), (100, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100)], loop)
    pcb, res = _run(p)
    assert len(pcb.segments) == 4
    assert res.stats["segments_removed"] == 0


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
        # already off the 45-degree set, so moving its end cannot make it worse
        ((105.1, 100), (105.5, 105)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 105.5, 105)], segs, vias=[(105, 100)])
    pcb, res = _run(p)
    ends = [s.end for s in pcb.segments if s.start == (100, 100)] + [
        s.start for s in pcb.segments if s.end == (105.5, 105)
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
    # the inner-layer end that sat 0.35 mm off the via centre is NOT snapped:
    # that would tilt an on-angle track off the 45-degree set (#8278); an
    # on-grid stub from the via centre to the end is added instead (#8437)
    in1 = [s for s in pcb.segments if s.layer == "In1.Cu"]
    assert (105, 100.35) in {pt for s in in1 for pt in (s.start, s.end)}
    assert (105, 100) in {pt for s in in1 for pt in (s.start, s.end)}


def _overlap_held_board(tmp_path: Path, trunk: bool = True, extra_pad: bool = False) -> Path:
    """#8259: a redundant B.Cu branch V2 -> P -> Q whose free end Q rests on V1
    by capsule overlap only (0.45 mm via, 0.4 mm track, Q 0.35 mm off centre),
    plus an In2.Cu track whose only join to V1 is that same overlap."""
    header = HEADER.replace(
        '(0 "F.Cu" signal) (31 "B.Cu" signal)',
        '(0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)',
    )
    x0, y0, w = 100.0, 100.0, 0.4

    def o(x, y):
        return (round(x0 + x, 4), round(y0 + y, 4))

    body = header + _fp("P1", *o(0, 0)) + _fp("P2", *o(3.0, 1.45))
    if extra_pad:  # a pad touched only by Q's overlap (never by a modelled join)
        px, py = o(0.35, -0.25)
        body += (
            f'  (footprint "X:P" (layer "F.Cu") (uuid "f-P3") (at {px} {py})\n'
            '    (property "Reference" "P3" (at 0 0 0) (layer "F.SilkS"))\n'
            '    (pad "1" thru_hole rect (at 0 0) (size 0.2 0.2) (drill 0.1) '
            '(layers "*.Cu") (net 1 "SIG"))\n  )\n'
        )
    if trunk:
        body += _seg(o(0, 0), o(1.45, 1.45), "B.Cu", w)
    body += _seg(o(1.45, 1.45), o(0.35, 0.35), "B.Cu", w)
    body += _seg(o(0.35, 0.35), o(0.35, 0), "B.Cu", w)
    body += _seg(o(1.45, 1.45), o(3.0, 1.45), "F.Cu", w)
    if not extra_pad:
        body += _seg(o(0.35, 0), o(1.70, -1.35), "In2.Cu", w)
        body += _seg(o(1.70, -1.35), o(3.0, -1.35), "In2.Cu", w)
        body += _seg(o(3.0, -1.35), o(3.0, 1.45), "In2.Cu", w)
    for xy in (o(0, 0), o(1.45, 1.45), o(3.0, 1.45)):
        body += (
            f'  (via (at {xy[0]} {xy[1]}) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") '
            f'(net 1) (uuid "v-{next(_uid)}"))\n'
        )
    p = tmp_path / "held.kicad_pcb"
    p.write_text(body + ")\n")
    return p


def test_overlap_held_branch_is_removed_and_inner_end_gets_a_stub(tmp_path):
    p = _overlap_held_board(tmp_path)
    pcb, res = _run(p)
    assert res.stats["segments_removed"] == 2
    b_cu = [(s.start, s.end) for s in pcb.segments if s.layer == "B.Cu"]
    assert b_cu == [((100.0, 100.0), (101.45, 101.45))]  # trunk kept
    # The 45-degree In2.Cu track cannot be tilted onto the via centre, so a
    # 0.35 mm stub from the centre to its end is added instead.
    assert res.stats["stubs_added"] == 1
    stubs = [s for s in pcb.segments if s.layer == "In2.Cu" and s.start == (100.0, 100.0)]
    assert [s.end for s in stubs] == [(100.35, 100.0)]
    assert stubs[0].width == pytest.approx(0.4)
    # the result is saved and reloads with the stub in place
    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)
    assert any(s.layer == "In2.Cu" and s.start == (100.0, 100.0) for s in PCB.load(out).segments)


def test_overlap_held_prune_is_drc_clean_with_kicad_cli(tmp_path):
    import shutil

    if shutil.which("kicad-cli") is None:
        pytest.skip("kicad-cli not available")
    from kicad_tools.pcb_prune import _drc_signature

    p = _overlap_held_board(tmp_path)
    before = _drc_signature(p)
    if before is None:
        pytest.skip("kicad-cli DRC unavailable")
    pcb, res = prune_board(p, allow_default_rules=True)
    assert res.drc_verified and res.rejected == []
    assert res.stats["segments_removed"] == 2
    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)
    after = _drc_signature(out)
    assert after is not None
    assert after.get("unconnected_items", 0) == 0
    assert sum(v for k, v in after.items() if k != "unconnected_items") <= sum(
        v for k, v in before.items() if k != "unconnected_items"
    )


def test_overlap_held_branch_is_kept_when_it_is_the_only_path(tmp_path):
    # No trunk V1 -> V2: the branch is the only link, nothing may be removed.
    pcb, res = _run(_overlap_held_board(tmp_path, trunk=False))
    assert res.stats["segments_removed"] == 0


def test_overlap_held_branch_is_kept_when_a_terminal_hangs_on_the_overlap(tmp_path):
    # A pad that Q touches by overlap alone is a terminal the model cannot see
    # joined: removing the branch must not strand it.
    pcb, res = _run(_overlap_held_board(tmp_path, extra_pad=True))
    assert res.stats["segments_removed"] == 0
    assert any(s.end == (100.35, 100.0) for s in pcb.segments if s.layer == "B.Cu")


def test_spur_is_kept_when_removal_would_disconnect_the_main_track(tmp_path):
    # Branch no longer reaches V2: it is not redundant, nothing may go.
    pcb, res = _run(_spur_board(tmp_path, branch_end=(109, 105.5)))
    assert res.stats["segments_removed"] == 0


def test_removal_never_leaves_an_end_dangling_off_a_removed_body(tmp_path):
    """#8249: a B.Cu track resting by its end on the removed branch's body must
    not be left free (``--no-drc --unverified`` invariant: keep MORE copper)."""
    p = _spur_board(tmp_path)
    text = p.read_text().rstrip()
    assert text.endswith(")")
    text = text[:-1]
    text += _seg((106.35, 103.5), (108, 102), "B.Cu", 0.4)
    text += _seg((108, 102), (110, 105), "F.Cu", 0.4)
    text += (
        f'  (via (at 108 102) (size 0.8) (drill 0.4) (layers "F.Cu" "B.Cu") '
        f'(net 1) (uuid "v-{next(_uid)}"))\n)\n'
    )
    p.write_text(text)
    pcb, res = _run(p)
    # the branch diagonal the new track's end rests on must survive (the main
    # diagonal may legitimately go instead); never the 3-segment branch removal
    assert any(s.start == (104.2, 101.5) and s.end == (108.5, 105.5) for s in pcb.segments)
    assert res.stats["segments_removed"] <= 1
    # and the via at (108, 102) keeps its B.Cu stub: never joined on one layer
    assert any(s.start == (106.35, 103.5) for s in pcb.segments)


def test_copper_arc_touching_a_detour_keeps_it(tmp_path):
    """#8249: a copper arc feeding a pad is unmodelled; a detour whose body is
    its only contact must be kept."""
    body = HEADER + _fp("A", 100, 100) + _fp("B", 110, 100) + _fp("C", 104.5, 102.5)
    for a, b in (
        ((100, 100), (102, 100)),
        ((102, 100), (103, 100)),
        ((103, 100), (110, 100)),
        ((102, 100), (102, 101)),
        ((102, 101), (103, 101)),
        ((103, 101), (103, 100)),
    ):
        body += _seg(a, b)
    body += (
        "  (arc (start 102.5 101) (mid 103.94 101.06) (end 104.5 102.5) (width 0.2) "
        f'(layer "F.Cu") (net 1) (uuid "a-{next(_uid)}"))\n'
    )
    p = tmp_path / "arc.kicad_pcb"
    p.write_text(body + ")\n")
    pcb, _res = _run(p)

    def _has_detour(b) -> bool:
        return any(s.start == (102, 101) and s.end == (103, 101) for s in b.segments)

    assert _has_detour(pcb)
    # control: without the arc the detour is redundant and goes
    q = tmp_path / "noarc.kicad_pcb"
    q.write_text(body.split("  (arc ")[0] + ")\n")
    pcb2, _res2 = _run(q)
    assert not _has_detour(pcb2)


# --- declared-net loop breaking (issue #8313) --------------------------------


def _via_loop_board(tmp_path: Path, trunk_w: float = 0.6, jog_w: float = 0.2) -> Path:
    """A--B closed through vias (F.Cu/B.Cu/F.Cu jog) and a F.Cu detour trunk."""
    segs = [
        # wide detour trunk (kept)
        ((100, 100), (100, 103), "F.Cu", trunk_w),
        ((100, 103), (110, 103), "F.Cu", trunk_w),
        ((110, 103), (110, 100), "F.Cu", trunk_w),
        # via-closed jog
        ((100, 100), (102, 100), "F.Cu", jog_w),
        ((102, 100), (108, 100), "B.Cu", jog_w),
        ((108, 100), (110, 100), "F.Cu", jog_w),
    ]
    return _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs, vias=[(102, 100), (108, 100)])


def _specs(tmp_path: Path) -> Path:
    f = tmp_path / "cp.json"
    f.write_text(
        json.dumps(
            {
                "paths": [
                    {
                        "name": "p",
                        "net": "SIG",
                        "source": {"ref": "A", "pad": "1"},
                        "sink": {"ref": "B", "pad": "1"},
                        "continuous_a": 1.0,
                    }
                ]
            }
        )
    )
    return f


def _has_cycle_connected(pcb) -> bool:
    return _connected(pcb, (100, 100), (110, 100))


def test_declared_net_single_pad_loop_is_kept(tmp_path):
    # Issue #8333 guard must also hold for the declared-net pass (#8313).
    loop = [
        ((100, 100), (105, 100)),
        ((105, 100), (105, 105)),
        ((105, 105), (100, 105)),
        ((100, 105), (100, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100)], loop)
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.5)
    assert len(pcb.segments) == 4
    assert res.stats["segments_removed"] == 0


_VIA_LOOP_SEGS = [
    ((100, 100), (100, 103), "F.Cu", 0.6),
    ((100, 103), (110, 103), "F.Cu", 0.6),
    ((110, 103), (110, 100), "F.Cu", 0.6),
    ((100, 100), (102, 100), "F.Cu", 0.2),
    ((102, 100), (108, 100), "B.Cu", 0.2),
    ((108, 100), (110, 100), "F.Cu", 0.2),
]


def test_declared_net_single_pad_via_loop_is_kept(tmp_path):
    # wide=1.0 > the 0.6 trunk, so every segment is a cut candidate; only the
    # two-terminal guard keeps the copper (fails if the guard is reverted).
    p = _board(tmp_path, [("A", 100, 100)], _VIA_LOOP_SEGS, vias=[(102, 100), (108, 100)])
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=1.0)
    assert len(pcb.segments) == 6
    assert len(pcb.vias) == 2
    assert res.stats["segments_removed"] == 0


def test_declared_net_zero_pad_loop_is_kept(tmp_path):
    loop = [
        ((100, 100), (105, 100)),
        ((105, 100), (105, 105)),
        ((105, 105), (100, 105)),
        ((100, 105), (100, 100)),
    ]
    p = _board(tmp_path, [], loop)
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.5)
    assert len(pcb.segments) == 4
    assert res.stats["segments_removed"] == 0


def test_declared_net_zero_pad_via_loop_is_kept(tmp_path):
    p = _board(tmp_path, [], _VIA_LOOP_SEGS, vias=[(102, 100), (108, 100)])
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=1.0)
    assert len(pcb.segments) == 6
    assert len(pcb.vias) == 2
    assert res.stats["segments_removed"] == 0


def test_declared_net_via_closed_loop_is_broken_at_narrowest(tmp_path):
    p = _via_loop_board(tmp_path)
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.5)
    widths = sorted(round(s.width, 3) for s in pcb.segments)
    assert res.stats["segments_removed"] >= 1
    assert widths.count(0.6) == 3  # wide trunk untouched
    assert res.uncut_loops == []
    # pads still connected through the trunk
    assert _connected(pcb, (100, 100), (110, 100))
    # the loop is gone: the audit no longer calls the run ambiguous
    from kicad_tools.router.current_paths import load_current_path_specs, resolve_current_path

    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)
    (spec,) = load_current_path_specs(_specs(tmp_path))
    assert resolve_current_path(PCB.load(out), spec).status == "resolved"


def test_loop_is_untouched_without_declaring_the_net(tmp_path):
    p = _via_loop_board(tmp_path)
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"OTHER"}, wide=0.5)
    assert res.uncut_loops == []
    # undeclared => the declared-net pass did not run
    assert not any("declared-net" in r for pl in res.plans for r in pl.reasons.values())


def test_all_wide_loop_is_reported_not_cut(tmp_path):
    p = _via_loop_board(tmp_path, trunk_w=0.6, jog_w=0.6)
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.5)
    assert res.stats["segments_removed"] == 0
    assert len(pcb.segments) == 6
    assert res.uncut_loops and res.uncut_loops[0]["net"] == "SIG"
    assert res.uncut_loops[0]["min_width"] == pytest.approx(0.6)


def test_wide_smaller_than_every_segment_cuts_nothing(tmp_path):
    p = _via_loop_board(tmp_path)
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.1)
    assert res.stats["segments_removed"] == 0
    assert res.uncut_loops


def test_cli_current_paths_breaks_loop_and_reports_json(tmp_path, capsys):
    p = _via_loop_board(tmp_path)
    cp = _specs(tmp_path)
    rc = _cli(tmp_path, p, "--dry-run", "--no-drc", "--current-paths", str(cp), "--wide", "0.5")
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["removed"]["segments"] >= 1 and out["uncut_loops"] == []


def test_via_closed_cut_removes_orphaned_vias(tmp_path):
    """Cutting the jog must not leave vias with nothing attached (issue #8313)."""
    p = _via_loop_board(tmp_path)
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.5)
    assert pcb.vias == []
    assert res.stats["vias_removed"] == 2
    assert _connected(pcb, (100, 100), (110, 100))


def test_via_closed_cut_passes_real_drc(tmp_path):
    """The default (DRC-verified) path accepts the plan and resolves the run."""
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    from kicad_tools.router.current_paths import load_current_path_specs, resolve_current_path

    p = _via_loop_board(tmp_path)
    pcb, res = prune_board(
        p, verify_drc=True, declared_nets={"SIG"}, wide=0.5, allow_default_rules=True
    )
    assert res.drc_verified
    assert [pl.net_name for pl in res.accepted] == ["SIG"] and res.rejected == []
    assert pcb.vias == []
    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)
    (spec,) = load_current_path_specs(_specs(tmp_path))
    assert resolve_current_path(PCB.load(out), spec).status == "resolved"


# ---------------------------------------------------------------------------
# Issue #8278: grid-step runs Board 05's router left behind
# ---------------------------------------------------------------------------


def test_run_grazed_by_its_diagonal_corner_is_merged(tmp_path):
    """A 0.1 mm grid-step run turning into a diagonal: the diagonal's capsule
    grazes the first steps (body contact outside the run), but it is joined to
    the run at the run's end, so the contact is redundant and both straight
    runs merge."""
    w = 0.2
    straight = [((100 + 0.1 * k, 100), (100 + 0.1 * (k + 1), 100), "F.Cu", w) for k in range(20)]
    diag = [
        ((102 + 0.1 * k, 100 + 0.1 * k), (102 + 0.1 * (k + 1), 100 + 0.1 * (k + 1)), "F.Cu", w)
        for k in range(10)
    ]
    pads = [("A", 100, 100, 0.2, 0.2), ("B", 103, 101, 0.2, 0.2)]
    p = _board(tmp_path, pads, straight + diag)
    pcb, res = _run(p)
    # The pad's reach splits each straight at most once (pure pass + the
    # anchored pass); no 0.1 mm fragment survives and the pads stay joined.
    assert len(pcb.segments) <= 4
    assert min(math.dist(s.start, s.end) for s in pcb.segments) > 0.1 + 1e-6
    assert _connected(pcb, (100, 100), (103, 101))


def test_run_inside_same_net_zone_is_merged(tmp_path):
    """Every joint also sits inside a same-net zone outline (not a pure
    joint); the collapsed run keeps both pad joins and its zone anchors."""
    zone = (
        '  (zone (net 1) (net_name "SIG") (layer "F.Cu") (uuid "z1") (hatch edge 0.5)\n'
        "    (connect_pads (clearance 0.2)) (min_thickness 0.25)\n"
        "    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
        "    (polygon (pts (xy 98 98) (xy 104 98) (xy 104 102) (xy 98 102))))\n"
    )
    segs = [((100 + 0.25 * k, 100), (100 + 0.25 * (k + 1), 100)) for k in range(8)]
    p = _board(tmp_path, [("A", 100, 100, 0.2, 0.2), ("B", 102, 100, 0.2, 0.2)], segs)
    p.write_text(p.read_text()[: -len(")\n")] + zone + ")\n")
    pcb, res = _run(p)
    assert len(pcb.segments) == 1
    s = pcb.segments[0]
    assert sorted([s.start, s.end]) == [pytest.approx((100, 100)), pytest.approx((102, 100))]


def test_loop_removal_keeps_the_via_centre_joint(tmp_path):
    """A diagonal stub to the via centre and a straight run whose free end
    merely sits inside the via disc form a loop.  Removing the stub would
    leave the via joined on F.Cu only off-centre (an overlap-only joint), so
    the run's off-centre end piece goes instead.

    This pins the planner's *preference* for the off-centre branch, which wins
    on ordering before ``decentres`` is consulted;
    ``test_loop_removal_refuses_to_decentre_a_via`` is the fixture that
    reaches ``decentres`` itself."""
    segs = [
        ((100, 100), (100.1, 99.9)),  # centred stub
        ((100.1, 99.8), (100.1, 99.9)),  # end inside the via, 0.224 mm off
        ((100.1, 99.9), (100.1, 102)),
    ]
    p = _board(tmp_path, [("A", 100.1, 102, 0.2, 0.2)], segs, vias=[(100, 100)])
    pcb, res = _run(p)
    kept = {
        (tuple(round(v, 3) for v in s.start), tuple(round(v, 3) for v in s.end))
        for s in pcb.segments
    }
    assert ((100.0, 100.0), (100.1, 99.9)) in kept
    assert ((100.1, 99.8), (100.1, 99.9)) not in kept


def test_run_that_a_longer_track_already_spans_is_dropped_not_duplicated(tmp_path):
    """Four 0.1 mm steps with a 0.4 mm track laid back over them between a pad
    and a via: merging would leave an exact (reversed) duplicate, which KiCad
    reports as a dangling track.  The steps go; the 0.4 mm track stays."""
    steps = [((100, 100 + 0.1 * k), (100, 100 + 0.1 * (k + 1))) for k in range(4)]
    back = ((100, 100.4), (100, 100))
    p = _board(tmp_path, [("A", 100, 100, 0.2, 0.2)], [*steps, back], vias=[(100, 100.4)])
    pcb, res = _run(p)
    assert len(pcb.segments) == 1
    s = pcb.segments[0]
    assert sorted([s.start, s.end]) == [pytest.approx((100, 100)), pytest.approx((100, 100.4))]


def test_run_looping_back_to_nowhere_is_kept(tmp_path):
    """The same loop, but nothing else at the far point: without the steps the
    0.4 mm track would dangle there, so the run is left untouched."""
    steps = [((100, 100 + 0.1 * k), (100, 100 + 0.1 * (k + 1))) for k in range(4)]
    back = ((100, 100.4), (100, 100))
    p = _board(tmp_path, [("A", 100, 100, 0.2, 0.2)], [*steps, back])
    pcb, res = _run(p)
    # No covered step goes either (issue #8368): removing any of them leaves a
    # neighbouring step's end landing mid-span on the 0.4 mm track, which
    # kicad-cli reports as dangling.
    assert len(pcb.segments) == 5


def test_rewritten_ends_keep_full_nm_precision(tmp_path):
    """A merged/snapped end is written on KiCad's nm grid, not truncated to six
    significant digits (133.4125 used to become 133.412, a 0.5 um gap)."""
    segs = [((133.4125 - 0.1 * k, 140.1), (133.4125 - 0.1 * (k + 1), 140.1)) for k in range(5)]
    p = _board(tmp_path, [("A", 133.4125, 140.1, 0.2, 0.2), ("B", 132.9125, 140.1, 0.2, 0.2)], segs)
    board, res = prune_board(p, verify_drc=False)
    assert res.stats["segments_removed"] == 4
    out = tmp_path / "out.kicad_pcb"
    board.save(out)
    text = out.read_text()
    assert "133.4125" in text and "132.9125" in text
    assert "133.412 " not in text and "132.912 " not in text


# ---------------------------------------------------------------------------
# Issue #8278 review: one fixture per safety predicate.  Each test below fails
# when the guard it names is deleted from ``pcb_prune`` (mutation-checked).
# ---------------------------------------------------------------------------


def _append(p: Path, *items: str) -> Path:
    text = p.read_text().rstrip()
    assert text.endswith(")")
    p.write_text(text[:-1] + "".join(items) + ")\n")
    return p


def _via_at(x, y, size=0.6) -> str:
    return (
        f'  (via (at {x} {y}) (size {size}) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) '
        f'(uuid "v-{next(_uid)}"))\n'
    )


def _seg_pairs(pcb) -> set:
    return {
        (tuple(round(v, 4) for v in s.start), tuple(round(v, 4) for v in s.end))
        for s in pcb.segments
    }


def _count_decentres(monkeypatch) -> dict[str, int]:
    """Wrap ``_NetModel.decentres`` with a call / refusal counter, so a test
    that relies on the guard fails loudly once its fixture stops reaching it
    (issue #8358: the earlier fixture silently stopped reaching it)."""
    from kicad_tools.pcb_prune import _NetModel

    seen = {"calls": 0, "refused": 0}
    orig = _NetModel.decentres

    def counted(self, *args):
        out = orig(self, *args)
        seen["calls"] += 1
        seen["refused"] += int(out)
        return out

    monkeypatch.setattr(_NetModel, "decentres", counted)
    return seen


def test_loop_removal_refuses_to_decentre_a_via(tmp_path, monkeypatch):
    """``decentres`` in :func:`_plan_net`'s generic loop pass (issue #8358).

    Fixture: a centred stub V->J and a detour J->V whose end lands 0.28 mm off
    V's centre form a loop, with pads A and B on the J->A->B trunk.  The
    second pad is what makes the loop pass run at all: with one terminal the
    #8333 two-terminal floor skips it, which is how the earlier one-pad
    fixture stopped reaching ``decentres``.  The detour cannot go (it passes
    the hard-protected via V2, so it may hide a join), and the trunk steps
    fail ``partition`` / ``still_joined``.  That leaves the stub, which clears
    ``partition``, ``still_joined``, ``strands_end``, ``strands_via_layer``
    and ``degrades_ends`` (its far end J stays track-joined, V keeps its only
    layer); only ``decentres`` sees that V would be joined on F.Cu only
    off-centre."""
    seen = _count_decentres(monkeypatch)
    segs = [
        ((100, 100), (102, 100)),  # the centred stub
        ((102, 100), (105, 100)),  # on to pad A
        ((105, 100), (108, 100)),  # on to pad B (second terminal)
        ((102, 100), (102, 101)),  # detour ...
        ((102, 101), (100.2, 101)),  # ... past V2 (hard-protected)
        ((100.2, 101), (100.2, 100.2)),  # ... ending inside V, off-centre
    ]
    pads = [("A", 105, 100, 0.2, 0.2), ("B", 108, 100, 0.2, 0.2)]
    p = _board(tmp_path, pads, segs, vias=[(100, 100), (101.2, 101.3)])
    pcb, res = _run(p)
    assert seen["refused"] >= 1, "fixture no longer reaches decentres"
    assert ((100, 100), (102, 100)) in _seg_pairs(pcb)
    assert res.stats["segments_removed"] == 0


def test_via_closed_cut_refuses_to_decentre_a_via(tmp_path, monkeypatch):
    """``decentres`` in :func:`_cut_ok`, the declared-net / via-closed path
    (issues #8313, #8358).

    Fixture: on declared net SIG, a 0.2 mm centred stub V->J on F.Cu and a
    0.2 mm detour J->W (F.Cu), W->X (B.Cu), X->inside V 0.2 mm off-centre
    (F.Cu) close a loop through vias W and X; the J->A->B trunk is 0.6 mm, at
    or above ``--wide``, so it is never cut.  The declared pass tries the
    narrowest seed first, by index, so the stub comes first.  It clears
    ``partition``, ``still_joined``, ``strands_end``, ``degrades_ends`` and
    the via-layer check (V only ever had F.Cu); only ``decentres`` refuses
    it.  The pass then cuts the detour with its orphaned vias W and X
    instead, leaving the stub centred on V."""
    seen = _count_decentres(monkeypatch)
    segs = [
        ((100, 100), (102, 100), "F.Cu", 0.2),  # centred stub V->J
        ((102, 100), (105, 100), "F.Cu", 0.6),  # wide trunk to pad A
        ((105, 100), (108, 100), "F.Cu", 0.6),  # ... and pad B
        ((102, 100), (102, 102), "F.Cu", 0.2),  # detour J->W
        ((102, 102), (100.2, 102), "B.Cu", 0.2),  # W->X
        ((100.2, 102), (100.2, 100.2), "F.Cu", 0.2),  # X->inside V, off-centre
    ]
    pads = [("A", 105, 100, 0.2, 0.2), ("B", 108, 100, 0.2, 0.2)]
    p = _board(tmp_path, pads, segs, vias=[(100, 100), (102, 102), (100.2, 102)])
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.5)
    assert seen["refused"] >= 1, "fixture no longer reaches decentres"
    assert ((100, 100), (102, 100)) in _seg_pairs(pcb)
    assert [tuple(v.position) for v in pcb.vias] == [(100, 100)]
    assert res.stats["segments_removed"] == 3
    assert res.stats["vias_removed"] == 2
    assert _connected(pcb, (100, 100), (108, 100))


def _t_junction_board(tmp_path: Path) -> Path:
    """Eight 0.25 mm steps A->B, and a track from pad C whose end rests on the
    body of one step (a T-junction KiCad joins, but the model does not)."""
    segs = [((100 + 0.25 * k, 100), (100 + 0.25 * (k + 1), 100)) for k in range(8)]
    segs.append(((101.1, 101), (101.1, 100.05)))
    pads = [("A", 100, 100, 0.2, 0.2), ("B", 102, 100, 0.2, 0.2), ("C", 101.1, 101, 0.2, 0.2)]
    return _board(tmp_path, pads, segs)


def test_run_touched_by_an_unjoined_track_is_not_merged(tmp_path):
    """Pure pass body-touch guard, and the anchored pass's ``still_joined``:
    the T-junction track touches the run only by its body, so a run interior
    joint could be what holds it.  Neither pass may merge the run."""
    pcb, res = _run(_t_junction_board(tmp_path))
    assert res.stats["segments_removed"] == 0
    assert len(pcb.segments) == 9


def test_pure_pass_merges_a_run_whose_grazing_track_is_otherwise_joined(tmp_path):
    """The *relaxed* body-touch guard: in the pure pass alone (not rescued by
    the anchored pass), a diagonal that grazes the last straight steps but is
    joined to the run's end does not block the merge."""
    from kicad_tools.pcb_prune import NetPlan, _build_net_models, _plan_collinear_merges

    w = 0.2
    straight = [((100 + 0.1 * k, 100), (100 + 0.1 * (k + 1), 100), "F.Cu", w) for k in range(20)]
    diag = [
        ((102 + 0.1 * k, 100 + 0.1 * k), (102 + 0.1 * (k + 1), 100 + 0.1 * (k + 1)), "F.Cu", w)
        for k in range(10)
    ]
    # Pads well clear of the run so no step is hard-protected by a pad.
    pads = [("A", 99, 100, 0.2, 0.2), ("B", 104, 102, 0.2, 0.2)]
    lead_in = [((99, 100), (100, 100), "F.Cu", 0.3), ((103, 101), (104, 102), "F.Cu", 0.3)]
    p = _board(tmp_path, pads, straight + diag + lead_in)
    pcb = PCB.load(p)
    (model,) = _build_net_models(pcb).values()
    grazed = next(
        i
        for i, n in enumerate(model.nodes)
        if n.kind == "seg" and _seg_ends(n) == ((101.9, 100), (102.0, 100))
    )
    assert model.overlap[grazed], "fixture: the diagonal must graze this step"
    merged = _plan_collinear_merges(model, NetPlan(1), set())
    assert grazed in merged


def _seg_ends(n) -> tuple:
    return tuple(tuple(round(v, 4) for v in pt) for pt in (n.a, n.b))


def test_anchored_merge_keeps_a_zone_held_only_at_an_interior_joint(tmp_path):
    """Anchored pass ``zones <= adj``: the run's interior joint is the only
    track end inside a same-net zone (the zone also reaches pad C, so the
    terminal partition alone would not notice).  Collapsing the run would
    leave the zone with no track end in it."""
    zone = (
        '  (zone (net 1) (net_name "SIG") (layer "F.Cu") (uuid "z1") (hatch edge 0.5)\n'
        "    (connect_pads (clearance 0.2)) (min_thickness 0.25)\n"
        "    (fill (thermal_gap 0.5) (thermal_bridge_width 0.5))\n"
        "    (polygon (pts (xy 101.8 99.5) (xy 102.2 99.5) (xy 102.2 103) (xy 101.8 103))))\n"
    )
    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (104, 100)),
        ((102, 102.5), (104, 102.5)),  # pad C -> pad B, the other way round
        ((104, 102.5), (104, 100)),
    ]
    pads = [("A", 100, 100), ("B", 104, 100), ("C", 102, 102.5)]
    p = _append(_board(tmp_path, pads, segs), zone)
    pcb, res = _run(p)
    assert {((100, 100), (102, 100)), ((102, 100), (104, 100))} <= _seg_pairs(pcb)
    assert res.stats["segments_removed"] == 0


def test_anchored_merge_keeps_a_via_centred_on_its_layer(tmp_path):
    """Anchored pass ``centred_layers``: V sits on the run's interior joint
    and stays connected through a B.Cu track to V2 (at pad B), so the run
    could collapse without disconnecting anything -- but V would lose its
    only F.Cu track end on centre."""
    segs = [
        ((100, 100), (102, 100)),
        ((102, 100), (104, 100)),
        ((102, 100), (104, 100), "B.Cu"),
    ]
    # V3 grazes both 102->104 tracks, so loop removal may take neither.
    p = _board(
        tmp_path,
        [("A", 100, 100), ("B", 104, 100)],
        segs,
        vias=[(102, 100), (104, 100), (103, 100.3)],
    )
    pcb, res = _run(p)
    assert {((100, 100), (102, 100)), ((102, 100), (104, 100))} <= _seg_pairs(pcb)
    assert res.stats["segments_removed"] == 0


def test_anchored_merge_skips_a_run_touching_a_copper_arc(tmp_path):
    """``_merge_unsafe_member`` (arc): both run members touch a copper arc,
    an unmodelled join a lost interior joint could be feeding."""
    segs = [((100, 100), (101, 100)), ((101, 100), (102, 100))]
    p = _board(tmp_path, [("A", 100, 100, 0.2, 0.2), ("B", 102, 100, 0.2, 0.2)], segs)
    _append(
        p,
        "  (arc (start 101 100.15) (mid 101.5 100.6) (end 102 101.5) (width 0.2) "
        f'(layer "F.Cu") (net 1) (uuid "a-{next(_uid)}"))\n',
    )
    pcb, res = _run(p)
    assert res.stats["segments_removed"] == 0
    assert len(pcb.segments) == 2


@pytest.mark.parametrize("syntax", ["(locked yes)", "legacy"])
def test_locked_tracks_are_never_merged_or_removed(tmp_path, syntax):
    """``_merge_unsafe_member`` (locked) and ``_is_protected``: a locked run is
    left exactly as drawn, in both KiCad's ``(locked yes)`` and the KiCad 6
    ``(segment locked ...)`` syntax."""
    steps = [((100 + 0.25 * k, 100), (100 + 0.25 * (k + 1), 100)) for k in range(4)]
    p = _board(tmp_path, [("A", 100, 100, 0.2, 0.2), ("B", 101, 100, 0.2, 0.2)], steps)
    text = p.read_text()
    if syntax == "legacy":
        text = text.replace("  (segment (start", "  (segment locked (start")
    else:
        text = text.replace(
            '(layer "F.Cu") (net 1) (uuid "s-', '(locked yes) (layer "F.Cu") (net 1) (uuid "s-'
        )
    p.write_text(text)
    assert all(s.locked for s in PCB.load(p).segments)
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 0}
    assert len(pcb.segments) == 4


def test_pure_pass_keeps_a_run_whose_copy_loops_back_to_nowhere(tmp_path):
    """Pure pass ``copy == -1``: four 0.1 mm steps away from any pad, with a
    (locked, so loop removal cannot take it) 0.4 mm track laid back over
    them.  Merging would leave that track with a free end at the far point,
    so the steps stay as they are."""
    steps = [((100, 100 + 0.1 * k), (100, 100 + 0.1 * (k + 1))) for k in range(4)]
    lead = ((100, 98), (100, 100))
    back = ((100, 100.4), (100, 100))
    p = _board(tmp_path, [("A", 100, 98, 0.2, 0.2)], [lead, *steps, back])
    p.write_text(
        p.read_text().replace(
            "(start 100 100.4) (end 100 100)", "(start 100 100.4) (end 100 100) (locked yes)"
        )
    )
    pcb, res = _run(p)
    assert res.stats == {"segments_removed": 0, "ends_snapped": 0}
    assert len(pcb.segments) == 6


# ---------------------------------------------------------------------------
# Issue #8323: the DRC referee must honour the board's own .kicad_dru
# ---------------------------------------------------------------------------


def _hv_board(tmp_path: Path, name: str = "hv") -> Path:
    """Net HV track with an off-centre end beside its via, plus a SIG via.

    The HV track is snapped onto its via centre by prune.  A custom DRU rule
    (see ``_DRU``) governs the HV/SIG clearance.
    """
    header = HEADER.replace('(net 1 "SIG")', '(net 1 "SIG")\n  (net 2 "HV")')
    pad = (
        '  (footprint "X:P" (layer "F.Cu") (uuid "f-{r}") (at {x} 100)\n'
        '    (property "Reference" "{r}" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 2 "HV"))\n  )\n'
    )
    body = header + pad.format(r="A", x=100) + pad.format(r="B", x=110)
    body += (
        '  (segment (start 100 100) (end 104.8 100) (width 0.2) (layer "F.Cu") (net 2) '
        '(uuid "s-hv1"))\n'
        '  (segment (start 110 100) (end 105.2 100) (width 0.2) (layer "F.Cu") (net 2) '
        '(uuid "s-hv2"))\n'
        '  (via (at 105 100) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) '
        '(uuid "v-hv"))\n'
        '  (via (at 103 98) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) '
        '(uuid "v-sig"))\n)\n'
    )
    p = tmp_path / f"{name}.kicad_pcb"
    p.write_text(body)
    return p


_DRU = '(version 1)\n(rule "Reviewed clearance - HV" (constraint clearance (min 0.4mm)))\n'


def test_worse_distinguishes_rule_and_nets_not_only_type():
    from collections import Counter

    from kicad_tools.pcb_prune import _worse

    before = Counter({("clearance", "error", "default", ("A", "B")): 1})
    swapped = Counter({("clearance", "error", "Reviewed clearance - HV", ("A", "B")): 1})
    assert _worse(swapped, before)
    assert not _worse(before, before)


def _fake_drc(monkeypatch, tmp_path, violations):
    """Make ``_drc_signature`` run without kicad-cli, on canned violations."""
    from kicad_tools.cli import runner
    from kicad_tools.drc.report import DRCReport
    from kicad_tools.drc.violation import Severity, ViolationType  # noqa: F401

    out = tmp_path / "fake-drc.json"
    out.write_text("{}")
    monkeypatch.setattr(runner, "find_kicad_cli", lambda: Path("kicad-cli"))
    monkeypatch.setattr(
        runner,
        "run_drc",
        lambda *a, **k: runner.KiCadCLIResult(success=True, output_path=out),
    )
    report = DRCReport(source_file="x", created_at=None, pcb_name="x", violations=list(violations))
    monkeypatch.setattr(DRCReport, "load", classmethod(lambda cls, path: report))


def _clearance(rule: str, nets: list[str]):
    from kicad_tools.drc.violation import DRCViolation, Severity, ViolationType

    return DRCViolation(
        type=ViolationType.CLEARANCE,
        type_str="clearance",
        severity=Severity.ERROR,
        message="clearance",
        rule=rule,
        nets=nets,
    )


def test_drc_signature_keeps_same_type_violations_with_different_rule_or_nets_apart(
    monkeypatch, tmp_path
):
    """Fails on the old ``(type, severity)`` key: both collapse to one entry."""
    from kicad_tools.pcb_prune import _drc_signature, _worse

    board = tmp_path / "t.kicad_pcb"
    board.write_text("")

    _fake_drc(monkeypatch, tmp_path, [_clearance("default", ["A", "B"])])
    before = _drc_signature(board)
    # One violation fixed, a differently-ruled one on other nets introduced.
    _fake_drc(monkeypatch, tmp_path, [_clearance("Reviewed clearance - HV", ["HV", "SIG"])])
    after = _drc_signature(board)

    assert before is not None and after is not None
    assert set(before) != set(after)
    assert sum(before.values()) == sum(after.values()) == 1
    assert _worse(after, before)
    assert not _worse(before, before)


def test_missing_dru_is_reported_in_drc_note(tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli

    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    _, res = prune_board(_hv_board(tmp_path), allow_default_rules=True)
    assert res.drc_verified
    assert "hv.kicad_dru" in res.drc_note
    assert "custom clearances were NOT evaluated" in res.drc_note


def test_dru_present_is_not_flagged_and_output_has_no_new_error(tmp_path):
    from kicad_tools.cli.runner import find_kicad_cli
    from kicad_tools.pcb_prune import _drc_signature

    if find_kicad_cli() is None:
        pytest.skip("kicad-cli not available")
    p = _hv_board(tmp_path)
    p.with_suffix(".kicad_dru").write_text(_DRU)
    pcb, res = prune_board(p, allow_default_rules=True)
    assert res.drc_verified
    assert "hv.kicad_dru" not in res.drc_note
    out = tmp_path / "out" / "hv.kicad_pcb"
    out.parent.mkdir()
    pcb.save(out)
    out.with_suffix(".kicad_dru").write_text(_DRU)
    before, after = _drc_signature(p), _drc_signature(out)
    assert before is not None and after is not None
    assert not any(after[k] > before[k] for k in after if k[1] == "error")


def test_anchor_in_body_loop_is_removed_8298(tmp_path):
    """GNDD triangle: a trunk passes 0.05 mm from V1's centre; a stub from V1's
    centre ends 0.05 mm from the trunk's centre line, on its body.  KiCad sees a
    loop (via centre in trunk body + stub end in trunk body)."""
    w = 0.2
    body = HEADER + _fp("P1", 100, 100) + _fp("P2", 110, 100)
    body += _seg((100, 100), (105, 100), "F.Cu", w)
    body += _seg((105, 100), (110, 100), "F.Cu", w)
    # V1 at (103, 100.05); stub down 0.5 and left 0.81 would end near the trunk
    # only on a bent trunk; keep it simple: stub rises from V1 and ends on trunk
    body += _seg((103, 100.05), (103, 100.5), "F.Cu", w)
    body += _seg((103, 100.5), (103.8, 100.05), "F.Cu", w)
    body += (
        f'  (via (at 103 100.05) (size 0.4) (drill 0.2) (layers "F.Cu" "B.Cu") '
        f'(net 1) (uuid "v-{next(_uid)}"))\n'
    )
    p = tmp_path / "gndd.kicad_pcb"
    p.write_text(body + ")\n")
    pcb, res = _run(p)
    assert res.stats["segments_removed"] >= 1
    assert any(s.start == (100, 100) for s in pcb.segments)


def test_prune_keeps_micro_bridge_joint_for_optimize_8298(tmp_path):
    """Two chains joined only by a ~1 um bridge: each chain end also lies in the
    other's body, so a body-join cycle exists.  Prune must not trade the
    endpoint join for an overlap-only one (optimize-traces models endpoint
    joins only and would leave a dangling duplicate)."""
    segs = [
        ((100, 100), (105, 100)),
        ((105, 100), (105.001, 100)),  # 1 um bridge
        ((105.001, 100), (110, 105)),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 105)], segs)
    pcb, res = _run(p)
    assert res.stats["segments_removed"] == 0
    assert len(pcb.segments) == 3

    from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer
    from kicad_tools.router.optimizer.pcb import optimize_pcb

    out = tmp_path / "opt.kicad_pcb"
    pcb.save(out)
    cfg = OptimizationConfig()
    opt = TraceOptimizer(cfg)
    optimize_pcb(str(out), str(out), opt.optimize_segments, cfg)
    after = PCB.load(str(out))
    # still one connected run between the pads: no duplicate / dangling piece
    xs = sorted({round(v, 3) for s in after.segments for v in (s.start[0], s.end[0])})
    assert xs[0] == 100 and xs[-1] == 110
    assert len(after.segments) <= 3
    # every segment end is shared with another segment or sits on a pad
    ends = [e for sg in after.segments for e in (sg.start, sg.end)]
    for e in ends:
        assert (
            e in ((100, 100), (110, 105))
            or sum(1 for o in ends if abs(o[0] - e[0]) < 2e-3 and abs(o[1] - e[1]) < 2e-3) >= 2
        )


def test_loop_closed_by_t_join_and_via_under_track_is_removed(tmp_path):
    # Issue #8298, the board geometry: a trunk from pad A passes 0.05 mm from
    # via V1's centre on its way to pad B; a stub leaves V1's centre and ends
    # on the trunk's body (a T-join without a shared vertex).  KiCad joins
    # both, so trunk + stub enclose a loop; the stub is the redundant part.
    trunk = [
        ((168.98, 100.0), (168.98, 98.5)),
        ((168.98, 98.5), (169.48, 98.0)),
        ((169.48, 98.0), (170.54, 98.0)),
    ]
    stub = [((169.74, 98.05), (169.74, 98.55)), ((169.74, 98.55), (168.93, 98.55))]
    p = _board(
        tmp_path,
        [("A", 168.98, 100.0, 0.6, 0.6), ("B", 170.54, 98.0, 0.6, 0.6)],
        trunk + stub,
        vias=[(169.74, 98.05)],
    )
    pcb, res = _run(p)
    assert res.stats["segments_removed"] == 2
    left = {(s.start, s.end) for s in pcb.segments}
    assert left == {(a, b) for a, b in trunk}
    assert _connected(pcb, (168.98, 100.0), (170.54, 98.0))


def test_t_join_end_is_not_a_clean_joint_for_removal(tmp_path):
    # A run of short pieces whose ends each lie inside the piece after next
    # (piece length below half the width) must not be thinned to
    # overlap-only joints.
    pts = [(100 + 0.07 * k, 100.0) for k in range(8)]
    segs = [((pts[k][0], 100.0), (pts[k + 1][0], 100.0)) for k in range(7)]
    segs += [((pts[-1][0], 100.0), (101.0, 100.0)), ((101.0, 100.0), (101.0, 101.0))]
    p = _board(tmp_path, [("A", 100, 100, 0.3, 0.3), ("B", 101, 101, 0.3, 0.3)], segs)
    pcb, res = _run(p)
    ends = [s.start for s in pcb.segments] + [s.end for s in pcb.segments]
    # every surviving joint is a shared vertex (or a pad)
    for s in pcb.segments:
        for e in (s.start, s.end):
            assert ends.count(e) >= 2 or e in {(100, 100), (101, 101)}


def test_removal_never_leaves_a_via_joined_on_one_layer(tmp_path):
    # A loop A -> V (F.Cu), V -> W (B.Cu), W -> A (F.Cu): removing any part
    # leaves a via joined on only one layer (KiCad via_dangling).
    segs = [
        ((100, 100), (105, 100)),
        ((105, 100), (105, 105), "B.Cu"),
        ((105, 105), (100, 105), "B.Cu"),
        ((100, 105), (100, 100)),
    ]
    p = _board(tmp_path, [("A", 100, 100)], segs, vias=[(105, 100), (100, 105)])
    _pcb, res = _run(p)
    assert res.stats["segments_removed"] == 0


def test_snap_never_takes_an_on_angle_track_off_the_45_degree_set(tmp_path):
    """#8278 (fleet angle census): snapping the end of a horizontal track onto
    a via centre 0.1 mm away would tilt it ~1 degree; the snap is refused and
    (#8437) a 0.1 mm axis-aligned stub reaches the centre instead."""
    segs = [((100, 100), (105, 100))]
    p = _board(tmp_path, [("A", 100, 100)], segs, vias=[(105, 100.1)])
    pcb, res = _run(p)
    assert res.stats["ends_snapped"] == 0
    track, stub = sorted(pcb.segments, key=lambda s: math.dist(s.start, s.end), reverse=True)
    assert track.end == (105, 100)
    assert {stub.start, stub.end} == {(105, 100), (105, 100.1)}


def test_collinear_merge_tolerates_half_micron_joint_mismatch(tmp_path):
    """Issue #8356: a 0.5 um end gap merges like the exact-coincidence run."""
    pads = [("A", 100, 100, 0.2, 0.2), ("B", 100.75, 100, 0.2, 0.2)]
    exact = [
        ((100, 100), (100.25, 100)),
        ((100.25, 100), (100.5, 100)),
        ((100.5, 100), (100.75, 100)),
    ]
    noisy = [
        ((100, 100), (100.25, 100)),
        ((100.25, 100), (100.5, 100)),
        ((100.5005, 100), (100.75, 100)),
    ]
    pcb_e, res_e = _run(_board(tmp_path, pads, exact))
    (tmp_path / "n").mkdir()
    pcb_n, res_n = _run(_board(tmp_path / "n", pads, noisy))
    assert res_n.stats == res_e.stats == {"segments_removed": 2, "ends_snapped": 0}
    assert len(pcb_n.segments) == 1
    s = pcb_n.segments[0]
    assert sorted([s.start, s.end]) == [pytest.approx((100, 100)), pytest.approx((100.75, 100))]


def test_collinear_merge_tolerates_mismatch_at_pad_neck(tmp_path):
    pads = [("A", 100, 100, 0.2, 0.2), ("B", 100.75, 100, 0.2, 0.2)]
    segs = [
        ((100, 100), (100.25, 100)),
        ((100.2505, 100), (100.5, 100)),
        ((100.5, 100), (100.75, 100)),
    ]
    pcb, res = _run(_board(tmp_path, pads, segs))
    assert len(pcb.segments) == 1


def test_collinear_merge_ignores_gap_above_tolerance_and_perpendicular(tmp_path):
    pads = [("A", 100, 100, 0.2, 0.2), ("B", 100.75, 100, 0.2, 0.2)]
    for dx, dy in ((0.002, 0.0), (0.0, 0.0005)):
        d = tmp_path / f"c{dx}{dy}"
        d.mkdir()
        segs = [
            ((100, 100), (100.25, 100)),
            ((100.25, 100), (100.5, 100)),
            ((100.5 + dx, 100 + dy), (100.75, 100)),
        ]
        pcb, _ = _run(_board(d, pads, segs))
        assert len(pcb.segments) >= 2


# --- net-relative --wide default + audit witness (issue #8368) ---------------


def _wide_loop_with_duplicate_board(tmp_path: Path, w: float = 2.6) -> Path:
    """Wide declared net: via-closed jog, detour trunk and a parallel duplicate run."""
    segs = [
        ((100, 100), (100, 103), "F.Cu", w),
        ((100, 103), (110, 103), "F.Cu", w),
        ((110, 103), (110, 100), "F.Cu", w),
        ((100, 100), (102, 100), "F.Cu", w),
        ((102, 100), (108, 100), "B.Cu", w),
        ((108, 100), (110, 100), "F.Cu", w),
        ((100, 103), (110, 103), "F.Cu", 1.0),  # parallel duplicate run
    ]
    return _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs, vias=[(102, 100), (108, 100)])


def _audit(pcb, tmp_path: Path):
    from kicad_tools.router.current_paths import load_current_path_specs, resolve_current_path

    out = tmp_path / "audited.kicad_pcb"
    pcb.save(out)
    (spec,) = load_current_path_specs(_specs(tmp_path))
    return spec, resolve_current_path(PCB.load(out), spec)


def test_default_wide_is_absolute_and_stalls_on_wide_net(tmp_path):
    p = _wide_loop_with_duplicate_board(tmp_path)
    pcb, res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.5)
    assert res.stats["segments_removed"] == 0
    assert res.uncut_loops


def test_relative_wide_resolves_wide_net_and_reinforce_anchors(tmp_path):
    from kicad_tools.pcb.reinforce import reinforce_net

    p = _wide_loop_with_duplicate_board(tmp_path)
    pcb, res = prune_board(
        p, verify_drc=False, declared_nets={"SIG"}, wide=None, required_widths={"SIG": 0.3}
    )
    assert res.stats["segments_removed"] >= 1
    assert res.uncut_loops == []
    spec, resolution = _audit(pcb, tmp_path)
    assert resolution.status == "resolved", resolution.reason
    from dataclasses import replace

    spec = replace(spec, reinforcement_eligible=True)
    out = reinforce_net(pcb, "SIG", spacing_mm=2.0, current_paths=[spec], anchor_mode="spaced")
    assert len(out.placed) > 0


def test_relative_wide_refuses_cut_that_breaks_ampacity(tmp_path):
    p = _wide_loop_with_duplicate_board(tmp_path, w=0.4)
    # remaining branch is 0.4 mm but the declared current needs 5 mm
    pcb, res = prune_board(
        p, verify_drc=False, declared_nets={"SIG"}, wide=None, required_widths={"SIG": 5.0}
    )
    # Only the 0.4 mm run covered by the 1 mm duplicate may go (no copper is
    # lost, issue #8368); no loop member is cut.
    assert res.stats["segments_removed"] <= 1
    assert all(r == "covered by a collinear track" for pl in res.plans for r in pl.reasons.values())
    assert res.uncut_loops and "declared current" in res.uncut_loops[0]["reason"]


def test_audit_names_loop_witness(tmp_path):
    segs = [
        ((100, 100), (110, 100), "F.Cu", 0.5),
        ((100, 100), (100, 103), "F.Cu", 0.5),
        ((100, 103), (110, 103), "F.Cu", 0.5),
        ((110, 103), (110, 100), "F.Cu", 0.5),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs)
    _spec, resolution = _audit(PCB.load(p), tmp_path)
    assert resolution.status == "ambiguous"
    assert resolution.witness["kind"] in ("segment", "via")
    assert "witness:" in resolution.reason


# --- cleanup_copper (#8399) -------------------------------------------------


class _FakePCB:
    """Stands in for ``PCB`` so ``cleanup_copper`` loop logic is tested alone."""

    dup_script: list[dict[str, int]] = []

    @classmethod
    def load(cls, _path):
        return cls()

    def dedupe_copper(self):
        return self.dup_script.pop(0) if self.dup_script else {"segments": 0, "vias": 0}

    def save(self, _path):
        pass


def _fake_result(accepted, verified=True, removed=0, snapped=0):
    from types import SimpleNamespace

    return SimpleNamespace(
        accepted=accepted,
        drc_verified=verified,
        drc_note="note",
        stats={"segments_removed": removed, "ends_snapped": snapped},
    )


def _patch_cleanup(monkeypatch, results, dups=()):
    import kicad_tools.pcb_prune as mod

    _FakePCB.dup_script = list(dups)
    queue = list(results)
    monkeypatch.setattr("kicad_tools.schema.pcb.PCB", _FakePCB)
    monkeypatch.setattr(mod, "prune_board", lambda _p: (_FakePCB(), queue.pop(0)))
    return mod


def test_cleanup_copper_runs_to_fixed_point(monkeypatch, tmp_path):
    mod = _patch_cleanup(
        monkeypatch,
        [_fake_result([1], removed=2, snapped=1), _fake_result([])],
        dups=[{"segments": 1, "vias": 0}],
    )
    totals = mod.cleanup_copper(tmp_path / "r.kicad_pcb")
    assert totals == {"duplicates": 1, "segments_removed": 2, "ends_snapped": 1}


def test_cleanup_copper_raises_on_unverified_prune(monkeypatch, tmp_path):
    mod = _patch_cleanup(monkeypatch, [_fake_result([1], verified=False)])
    with pytest.raises(RuntimeError, match="not DRC-verified"):
        mod.cleanup_copper(tmp_path / "r.kicad_pcb")


def test_cleanup_copper_raises_when_not_converging(monkeypatch, tmp_path):
    mod = _patch_cleanup(monkeypatch, [_fake_result([1], removed=1)] * 3)
    with pytest.raises(RuntimeError, match="did not converge"):
        mod.cleanup_copper(tmp_path / "r.kicad_pcb", max_passes=3)


# --- prune / audit loop-model agreement (issue #8368, criterion 1) -----------
#
# Synthetic stand-ins for the two edge kinds the real-board check found behind
# every ``ambiguous: loop/parallel`` path left after ``prune --current-paths``.


def _cp_specs(tmp_path: Path, source: str = "A", sink: str = "B"):
    from kicad_tools.router.current_paths import load_current_path_specs

    f = tmp_path / "cp8368.json"
    f.write_text(
        json.dumps(
            {
                "paths": [
                    {
                        "name": "p",
                        "net": "SIG",
                        "source": {"ref": source, "pad": "1"},
                        "sink": {"ref": sink, "pad": "1"},
                        "continuous_a": 1.0,
                    }
                ]
            }
        )
    )
    return load_current_path_specs(f)


def _resolve(pcb, specs, tmp_path: Path):
    from kicad_tools.router.current_paths import resolve_current_path

    out = tmp_path / "resolved8368.kicad_pcb"
    pcb.save(out)
    return resolve_current_path(PCB.load(out), specs[0])


def _prune_declared(p: Path, specs):
    return prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=None, current_paths=specs)


def _assert_agree(res, resolution) -> None:
    """The acceptance criterion: no uncut loop reported => audit not ambiguous."""
    if not res.uncut_loops:
        assert resolution.status == "resolved", resolution.reason


def test_contained_collinear_subsegment_is_removed(tmp_path):
    """Kind 1: S (0,0)->(0.8,0) lies inside L (0,0)->(3.2,0), same width."""
    p = _board(
        tmp_path,
        [("A", 100, 100), ("B", 103.2, 100)],
        [((100, 100), (103.2, 100), "F.Cu", 0.5), ((100, 100), (100.8, 100), "F.Cu", 0.5)],
    )
    specs = _cp_specs(tmp_path)
    before = _resolve(PCB.load(p), specs, tmp_path)
    assert before.status == "ambiguous"
    pcb, res = _prune_declared(p, specs)
    assert [(s.start, s.end) for s in pcb.segments] == [((100, 100), (103.2, 100))]
    assert res.uncut_loops == []
    after = _resolve(pcb, specs, tmp_path)
    assert after.status == "resolved", after.reason
    _assert_agree(res, after)


def test_contained_subsegment_with_both_ends_interior_and_offaxis_continuation(tmp_path):
    """Kind 1, both ends interior: L (0,0)->(10,0) at 2.6 mm, S (4,0)->(6,0) at
    0.5 mm continuing off-axis from (6,0).  The audit splits L at S's ends ->
    parallel edges.  Dropping S alone would leave the continuation landing
    mid-span on L (``track_dangling``, PR #8392 review), so L is split at (6,0)
    first (issue #8406): the continuation then shares an endpoint with both
    pieces and S goes, resolving the audit."""
    segs = [
        ((100, 100), (110, 100), "F.Cu", 2.6),
        ((104, 100), (106, 100), "F.Cu", 0.5),
        ((106, 100), (108, 102), "F.Cu", 0.5),
    ]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100), ("C", 108, 102)], segs)
    specs = _cp_specs(tmp_path)
    before = _resolve(PCB.load(p), specs, tmp_path)
    assert before.status == "ambiguous"
    assert before.witness["start"] == [104.0, 100.0]
    pcb, res = _prune_declared(p, specs)
    assert {(s.start, s.end) for s in pcb.segments} == {
        ((100, 100), (106, 100)),
        ((106, 100), (110, 100)),
        ((106, 100), (108, 102)),
    }
    assert not res.uncut_loops
    _assert_agree(res, _resolve(pcb, specs, tmp_path))
    assert _resolve(pcb, specs, tmp_path).status == "resolved"


@pytest.mark.parametrize("knee", [0.2, 0.4])
def test_covered_track_with_short_45_degree_landing_is_split_not_kept(tmp_path, knee):
    """PR #8392 review / issue #8406: L (0,0)->(10.4,0); S (0,0)->(6,0) inside
    it; a 45 deg stub from S's end (6,0) to (6+k,-k), then a 1.2 mm leg to a
    pad.  With S simply gone the stub's end lands mid-span on L (pinned
    kicad-cli 10.0.6: ``track_dangling``), so the pass splits L at (6,0) --
    same width and layer, no copper change -- and then drops S.  The anchored
    merge pass must not glue the pieces back across the three-way joint."""
    k = (106 + knee, 100 - knee)
    segs = [
        ((100, 100), (110.4, 100), "F.Cu", 0.5),
        ((100, 100), (106, 100), "F.Cu", 0.5),
        ((106, 100), k, "F.Cu", 0.5),
        (k, (k[0] + 1.2, k[1]), "F.Cu", 0.5),
    ]
    pads = [("A", 100, 100), ("B", 110.4, 100), ("C", k[0] + 1.2, k[1])]
    p = _board(tmp_path, pads, segs)
    pcb, res = _run(p)
    ends = {(s.start, s.end, s.width, s.layer) for s in pcb.segments}
    assert ends == {
        ((100, 100), (106, 100), 0.5, "F.Cu"),
        ((106, 100), (110.4, 100), 0.5, "F.Cu"),
        ((106, 100), k, 0.5, "F.Cu"),
        (k, (k[0] + 1.2, k[1]), 0.5, "F.Cu"),
    }
    assert res.stats["segments_removed"] == 1
    assert res.stats["segments_split"] == 1
    assert _connected(pcb, (100, 100), (k[0] + 1.2, k[1]))
    assert _connected(pcb, (110.4, 100), (k[0] + 1.2, k[1]))

    specs = _cp_specs(tmp_path)
    pcb, res = _prune_declared(p, specs)
    assert not res.uncut_loops
    assert _resolve(pcb, specs, tmp_path).status == "resolved"


def test_covered_track_split_and_overlap_held_stub_on_the_same_net(tmp_path):
    """Issues #8406 + #8259 on one net: the overlap-held board's branch removal
    (which adds a stub) and a covered-track split elsewhere on the same net.
    The split trial grows then shrinks the model's per-node lists, which must
    stay in step with the held-end bookkeeping (``IndexError`` in
    ``_NetModel.components`` on the merge of both)."""
    p = _overlap_held_board(tmp_path)
    k = (106.2, 119.8)
    items = [
        _fp("A", 100, 120),
        _fp("B", 110.4, 120),
        _fp("C", k[0] + 1.2, k[1]),
        _seg((100, 120), (110.4, 120), "F.Cu", 0.5),
        _seg((100, 120), (106, 120), "F.Cu", 0.5),
        _seg((106, 120), k, "F.Cu", 0.5),
        _seg(k, (k[0] + 1.2, k[1]), "F.Cu", 0.5),
    ]
    _append(p, *items)
    pcb, res = _run(p)
    assert res.stats["stubs_added"] == 1
    assert res.stats["segments_split"] == 1
    f_cu = {(s.start, s.end) for s in pcb.segments if s.layer == "F.Cu" and s.start[1] == 120}
    assert ((100, 120), (106, 120)) in f_cu and ((106, 120), (110.4, 120)) in f_cu
    assert any(s.layer == "In2.Cu" and s.start == (100.0, 100.0) for s in pcb.segments)


def test_covered_track_split_skips_locked_container(tmp_path):
    """A locked container is never split, so S stays."""
    k = (106.2, 99.8)
    segs = [
        ((100, 100), (110.4, 100), "F.Cu", 0.5),
        ((100, 100), (106, 100), "F.Cu", 0.5),
        ((106, 100), k, "F.Cu", 0.5),
        (k, (k[0] + 1.2, k[1]), "F.Cu", 0.5),
    ]
    pads = [("A", 100, 100), ("B", 110.4, 100), ("C", k[0] + 1.2, k[1])]
    p = _board(tmp_path, pads, segs)
    lines = p.read_text().splitlines(keepends=True)
    for i, ln in enumerate(lines):
        if "(end 110.4 100)" in ln:
            lines[i] = ln.replace("(uuid", "(locked yes) (uuid", 1)
    p.write_text("".join(lines))
    pcb, res = _run(p)
    assert res.stats["segments_removed"] == 0
    assert "segments_split" not in res.stats


def test_contained_subsegment_starting_on_a_via_the_container_passes_over(tmp_path):
    """Kind 1 with a via: S starts on a via that L runs straight over (the
    real board's net G).  The via stays joined by its centre on L."""
    segs = [
        ((100, 100), (103, 100), "F.Cu", 0.5),
        ((103, 100), (103, 103), "B.Cu", 0.5),  # S, starts on via 1
        ((103, 99), (103, 103), "B.Cu", 0.5),  # L, passes over via 1
        ((103, 99), (106, 99), "F.Cu", 0.5),
        ((103, 103), (106, 103), "F.Cu", 0.5),
    ]
    p = _board(
        tmp_path,
        [("A", 100, 100), ("B", 106, 103), ("C", 106, 99)],
        segs,
        vias=[(103, 100), (103, 99), (103, 103)],
    )
    specs = _cp_specs(tmp_path)
    assert _resolve(PCB.load(p), specs, tmp_path).status == "ambiguous"
    pcb, res = _prune_declared(p, specs)
    kept = {(s.start, s.end, s.layer) for s in pcb.segments}
    assert ((103, 100), (103, 103), "B.Cu") not in kept
    assert ((103, 99), (103, 103), "B.Cu") in kept
    assert len(pcb.vias) == 3
    after = _resolve(pcb, specs, tmp_path)
    assert after.status == "resolved", after.reason
    _assert_agree(res, after)


def test_contained_subsegment_wider_than_track_is_kept(tmp_path):
    """A wider "contained" track carries copper the narrower one lacks."""
    segs = [((100, 100), (103.2, 100), "F.Cu", 0.5), ((100, 100), (100.8, 100), "F.Cu", 1.0)]
    p = _board(tmp_path, [("A", 100, 100, 2, 2), ("B", 103.2, 100)], segs)
    _pcb, res = _run(p)
    assert all(r != "covered by a collinear track" for pl in res.plans for r in pl.reasons.values())


def test_explicit_wide_keeps_contained_wide_subsegment_on_declared_net(tmp_path):
    segs = [((100, 100), (110, 100), "F.Cu", 2.6), ((104, 100), (106, 100), "F.Cu", 0.6)]
    p = _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs)
    pcb, _res = prune_board(p, verify_drc=False, declared_nets={"SIG"}, wide=0.5)
    assert len(pcb.segments) == 2


def _crossing_board(tmp_path: Path) -> Path:
    """Kind 2: pad A -> 1.2 mm stub -> 2.6 mm trunk to pad B; a 0.5 mm
    diagonal from inside pad A to pad C crosses the trunk mid-span."""
    segs = [
        ((100, 104), (100, 100), "F.Cu", 1.2),
        ((100, 100), (104, 100), "F.Cu", 2.6),
        ((100.06, 103.8), (109.4, 94.4), "F.Cu", 0.5),
    ]
    return _board(tmp_path, [("A", 100, 104), ("B", 104, 100), ("C", 109.4, 94.4)], segs)


def test_midspan_crossing_closes_a_loop_in_the_prune_model(tmp_path):
    from kicad_tools.pcb_prune import _build_net_models

    (model,) = _build_net_models(PCB.load(_crossing_board(tmp_path))).values()
    assert model.has_cycle()


def test_midspan_crossing_loop_is_reported_not_silently_kept(tmp_path):
    """No segment removal breaks this loop without disconnecting a pad, so
    prune keeps the copper -- but reports the loop with the audit's witness
    instead of claiming there is none."""
    p = _crossing_board(tmp_path)
    specs = _cp_specs(tmp_path)
    before = _resolve(PCB.load(p), specs, tmp_path)
    assert before.status == "ambiguous"
    pcb, res = _prune_declared(p, specs)
    assert len(pcb.segments) == 3
    (loop,) = res.uncut_loops
    assert loop["net"] == "SIG"
    assert loop["witness"] == before.witness
    assert "audit" in loop["reason"]
    _assert_agree(res, _resolve(pcb, specs, tmp_path))


def test_cli_prune_reports_audit_loop_in_json(tmp_path, capsys):
    from kicad_tools.cli import main

    p = _crossing_board(tmp_path)
    specs_path = tmp_path / "cp.json"
    _cp_specs(tmp_path)
    specs_path.write_text((tmp_path / "cp8368.json").read_text())
    rc = main(
        [
            "pcb",
            "prune",
            str(p),
            "--current-paths",
            str(specs_path),
            "--no-drc",
            "--dry-run",
            "--format",
            "json",
        ]
    )
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["uncut_loops"] and out["uncut_loops"][0]["witness"]["kind"] == "segment"


def test_required_widths_follow_layer_and_stackup_copper_weight(tmp_path):
    """The net-relative ``--wide`` ampacity check sized every layer as an
    internal 1 oz layer (28 mm for 13.4 A); outer layers of a 2 oz board need
    far less."""
    from kicad_tools.pcb_prune import required_widths_for
    from kicad_tools.physics.ampacity import width_for_current

    p = _board(tmp_path, [("A", 100, 100)], [((100, 100), (101, 100))])
    internal_1oz = width_for_current(13.4, 1.0, layer="internal")
    plain = required_widths_for(PCB.load(p), 13.4)
    assert plain["F.Cu"] == pytest.approx(width_for_current(13.4, 1.0, layer="external"))
    assert plain["F.Cu"] < internal_1oz
    p.write_text(
        p.read_text().replace(
            '(paper "A4")',
            '(paper "A4")\n  (setup (stackup (layer "F.Cu" (type "copper") (thickness 0.07))'
            ' (layer "dielectric 1" (type "core") (thickness 1.5))'
            ' (layer "B.Cu" (type "copper") (thickness 0.07))))',
        )
    )
    two_oz = required_widths_for(PCB.load(p), 13.4)
    assert two_oz["B.Cu"] == pytest.approx(width_for_current(13.4, 2.0, layer="external"))
    assert two_oz["B.Cu"] < plain["B.Cu"]


def test_ampacity_check_uses_each_branch_layer(tmp_path):
    """A per-layer requirement: the same loop cut is allowed when the branch
    left is wide enough on *its* layer, refused when it is not."""
    ok_dir, bad_dir = tmp_path / "ok", tmp_path / "bad"
    ok_dir.mkdir()
    bad_dir.mkdir()
    for d, need in ((ok_dir, {"F.Cu": 0.3, "B.Cu": 9.0}), (bad_dir, {"F.Cu": 9.0, "B.Cu": 0.3})):
        p = _wide_loop_with_duplicate_board(d)
        _pcb, res = prune_board(
            p, verify_drc=False, declared_nets={"SIG"}, wide=None, required_widths={"SIG": need}
        )
        loop_cuts = [
            r for pl in res.plans for r in pl.reasons.values() if r.startswith("declared-net loop")
        ]
        if d is ok_dir:
            assert loop_cuts
        else:
            assert not loop_cuts and res.uncut_loops


def _multi_net_board(tmp_path: Path, nets: int = 12) -> Path:
    """Many nets, each with two redundant branches around a via (issue #8438)."""
    head = HEADER.replace(
        '  (net 1 "SIG")\n', "".join(f'  (net {i} "N{i}")\n' for i in range(1, nets + 1))
    )
    body = head
    for i in range(1, nets + 1):
        y = 100 + i * 4
        for ref, x in (("A", 100), ("B", 110)):
            body += (
                f'  (footprint "X:P" (layer "F.Cu") (uuid "f-{ref}{i}") (at {x} {y})\n'
                f'    (property "Reference" "{ref}{i}" (at 0 0 0) (layer "F.SilkS"))\n'
                f'    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {i} "N{i}"))\n'
                "  )\n"
            )
        body += (
            f'  (via (at 105 {y}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {i}) '
            f'(uuid "v-{i}"))\n'
        )
        pts = [
            ((100, y), (102, y)),
            ((102, y), (105, y)),
            ((105, y), (110, y)),
            ((102, y), (102, y + 1.5)),
            ((102, y + 1.5), (105.15, y + 1.5)),
            ((105.15, y + 1.5), (105.15, y + 0.25)),
            ((102, y), (102, y - 1.5)),
            ((102, y - 1.5), (105, y - 1.5)),
            ((105, y - 1.5), (105, y)),
        ]
        for a, b in pts:
            body += _seg(a, b).replace("(net 1)", f"(net {i})")
    p = tmp_path / "multi.kicad_pcb"
    p.write_text(body + ")\n")
    return p


def test_prune_output_is_byte_identical_across_hash_seeds(tmp_path):
    """``PYTHONHASHSEED`` must not change what gets pruned (issue #8438)."""
    import os
    import subprocess
    import sys

    src = _multi_net_board(tmp_path)
    outs = []
    for seed in ("0", "1", "2"):
        out = tmp_path / f"out{seed}.kicad_pcb"
        env = {**os.environ, "PYTHONHASHSEED": seed}
        r = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "pcb",
                "prune",
                str(src),
                "--no-drc",
                "--unverified",
                "-o",
                str(out),
            ],
            env=env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert r.returncode == 0, r.stderr
        outs.append(out.read_bytes())
    assert len(outs[0]) > 0
    assert outs[0] == outs[1] == outs[2]


def test_dedupe_output_is_byte_identical_across_hash_seeds(tmp_path):
    """``kct pcb dedupe`` must not depend on ``PYTHONHASHSEED`` (issue #8438)."""
    import os
    import re
    import subprocess
    import sys

    src = _multi_net_board(tmp_path)
    text = src.read_text()
    extra = ""
    for n, m in enumerate(
        re.finditer(
            r"  \(segment \(start ([\d.]+) ([\d.]+)\) \(end ([\d.]+) ([\d.]+)\)(.*?)\(uuid \"[^\"]*\"\)\)\n",
            text,
        )
    ):
        x1, y1, x2, y2, rest = m.groups()
        # Reversed endpoints + fresh uuid: an exact duplicate by dedup key.
        extra += f'  (segment (start {x2} {y2}) (end {x1} {y1}){rest}(uuid "dup-{n}"))\n'
    assert extra
    dup = tmp_path / "dup.kicad_pcb"
    dup.write_text(text.rstrip().removesuffix(")") + extra + ")\n")

    outs = []
    for seed in ("0", "1", "2"):
        out = tmp_path / f"dedupe{seed}.kicad_pcb"
        r = subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "pcb", "dedupe", str(dup), "-o", str(out)],
            env={**os.environ, "PYTHONHASHSEED": seed},
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert r.returncode == 0, r.stderr
        outs.append(out.read_bytes())
    assert len(outs[0]) > 0
    assert outs[0] == outs[1] == outs[2]
    assert b"dup-" not in outs[0]


def _offaxis_via_loop_board(tmp_path: Path, via_dy: float = 0.25) -> Path:
    """Issue #8436: trunk A -> B runs past a same-net via whose centre lies
    outside the trunk's body (disc overlap only); a detour tapped off the trunk lands on the
    via's centre.  The loop exists only through the overlap join."""
    segs = [
        ((100, 100), (110, 100), "F.Cu", 0.2),
        ((102, 100), (102, 102), "F.Cu", 0.2),
        ((102, 102), (105, 102), "F.Cu", 0.2),
        ((105, 102), (105, 100 + via_dy), "F.Cu", 0.2),
    ]
    return _board(tmp_path, [("A", 100, 100), ("B", 110, 100)], segs, vias=[(105, 100 + via_dy)])


def test_offaxis_via_overlap_closes_a_loop_in_the_prune_model(tmp_path):
    from kicad_tools.pcb_prune import _build_net_models

    (model,) = _build_net_models(PCB.load(_offaxis_via_loop_board(tmp_path))).values()
    assert model.has_cycle()


def test_offaxis_via_overlap_loop_never_justifies_removal(tmp_path):
    """The overlap join only finds the loop: every segment stays."""
    pcb, _res = _run(_offaxis_via_loop_board(tmp_path))
    assert len(pcb.segments) == 4


def test_offaxis_via_clear_of_the_trunk_is_not_a_loop(tmp_path):
    from kicad_tools.pcb_prune import _build_net_models

    p = _offaxis_via_loop_board(tmp_path, via_dy=0.6)  # disc clears the body
    (model,) = _build_net_models(PCB.load(p)).values()
    assert not model.has_cycle()


# Issue #8501: board 05's copper cleanup never converged.  A pad centre built
# from footprint position + pad offset is exactly x = 40.9125, while the same
# point reloaded from a segment end (absolute 154.4125 - origin 113.5) is
# 40.912499999999994.  That is a half-quantum of ``_segment_dedup_key``, so the
# bucket keys differ and ``add_trace`` appended a copy of a stub the board
# already had (walked the other way); ``dedupe_copper`` then removed it, and
# the stub-only plan was "accepted" on every pass.  Prune now compares
# existing copper by distance and queues no stub the net already has.
_ORIGIN = (113.5, 60.0)
# the existing on-grid stub pieces, pad centre -> track end, walked backwards
_REVERSED_STUB = [
    ((154.9, 111.2), (154.7, 111.0)),
    ((154.7, 111.0), (154.4125, 111.0)),
]


def _half_quantum_board(tmp_path: Path, stub) -> Path:
    ox, oy = _ORIGIN
    body = HEADER
    body += (
        f"  (gr_rect (start {ox} {oy}) (end {ox + 60} {oy + 60}) "
        '(stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts") '
        '(uuid "edge-1"))\n'
    )
    # pad R.2 centre: (153.5 + 0.9125) - 113.5 == 40.9125 exactly
    body += (
        '  (footprint "X:R" (layer "F.Cu") (uuid "f-R") (at 153.5 111)\n'
        '    (property "Reference" "R" (at 0 0 0) (layer "F.SilkS"))\n'
        '    (pad "2" smd rect (at 0.9125 0) (size 1.025 1.4) (layers "F.Cu") '
        '(net 1 "SIG"))\n'
        "  )\n"
    )
    body += _fp("B", 160.0, 111.2)
    # track end (41.4, 51.2) rests in the pad; snapping it onto the centre
    # would tilt the horizontal track, so prune joins it with a stub (#8437)
    body += _seg((160.0, 111.2), (154.9, 111.2))
    body += "".join(_seg(a, b) for a, b in stub) + ")\n"
    p = tmp_path / "b.kicad_pcb"
    p.write_text(body)
    return p


def _keys(pcb: PCB) -> list:
    from kicad_tools.schema.pcb import _segment_dedup_key

    return sorted(
        _segment_dedup_key(s.net_number, s.layer, s.start, s.end, s.width) for s in pcb.segments
    )


def test_existing_reversed_half_quantum_stub_is_not_re_added(tmp_path):
    p = _half_quantum_board(tmp_path, _REVERSED_STUB)
    before = PCB.load(p)
    stored = [pt for s in before.segments for pt in (s.start, s.end) if abs(pt[0] - 40.9125) < 1e-9]
    assert stored and all(pt[0] != 40.9125 for pt in stored)  # the half-quantum miss
    for _ in range(2):  # prune is idempotent on its own output
        pcb, res = _run(p)
        assert res.accepted == []
        assert not res.stats.get("stubs_added")
        assert len(pcb.segments) == 3 and _keys(pcb) == _keys(before)
        pcb.save(p)


@pytest.mark.parametrize("shape", ["tail", "split_trunk", "perp", "diag", "loop"])
@pytest.mark.parametrize("length", [0.01, 0.028, 0.04, 0.07])
def test_prune_converges_on_sub_tenth_mm_in_pad_segments(tmp_path, shape, length):
    """Issue #8719: prune on its own output is a no-op (no add/remove cycle)."""
    pads = [("A", 100, 100), ("B", 110, 100)]
    near = 110 - length
    segs = {
        "tail": [((100, 100), (110, 100)), ((near, 100), (110, 100))],
        "split_trunk": [((100, 100), (near, 100)), ((near, 100), (110, 100))],
        "perp": [((100, 100), (110, 100)), ((110, 100 + length), (110, 100))],
        "diag": [((100, 100), (near, 100 + length)), ((near, 100 + length), (110, 100))],
        "loop": [
            ((100, 100), (110, 100)),
            ((109, 100), (109, 101)),
            ((109, 101), (near, 101)),
            ((near, 101), (110, 100)),
        ],
    }[shape]
    p = _board(tmp_path, pads, segs)
    pcb, _res = _run(p)  # first pass may change the board
    pcb.save(p)
    settled = _keys(pcb)
    for _ in range(3):
        pcb, res = _run(p)
        assert res.accepted == []
        assert not res.stats.get("segments_removed")
        assert not res.stats.get("ends_snapped")
        assert not res.stats.get("stubs_added")
        assert not res.stats.get("segments_split")
        assert _keys(pcb) == settled
        pcb.save(p)


def test_only_the_missing_stub_piece_is_queued(tmp_path):
    from kicad_tools.pcb_prune import plan_prune

    p = _half_quantum_board(tmp_path, _REVERSED_STUB[1:])
    plans = plan_prune(PCB.load(p))
    stubs = [s for pl in plans for s in pl.stubs]
    assert len(stubs) == 1
    _layer, a, b, _w = stubs[0]
    ends = {(round(a[0], 4), round(a[1], 4)), (round(b[0], 4), round(b[1], 4))}
    assert ends == {(41.2, 51.0), (41.4, 51.2)}


def test_missing_half_quantum_stub_is_still_added(tmp_path):
    p = _half_quantum_board(tmp_path, [])
    pcb, res = _run(p)
    assert res.stats.get("stubs_added") == 2
    assert len(pcb.segments) == 3


# ---------------------------------------------------------------------------
# --nets / --region scoping (issue #8547)
# ---------------------------------------------------------------------------


def _two_net_board(tmp_path: Path, x_offset_b: float = 0.0) -> Path:
    """Two nets (SIG at y=100, SIG2 at y=120), each with a prunable loop."""

    def net_block(net: int, name: str, y: float, dx: float) -> str:
        pads = "".join(
            f'  (footprint "X:P" (layer "F.Cu") (uuid "f-{name}{r}") (at {x + dx} {y})\n'
            f'    (property "Reference" "{name}{r}" (at 0 0 0) (layer "F.SilkS"))\n'
            f'    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net {net} "{name}"))\n'
            "  )\n"
            for r, x in (("A", 100), ("B", 110))
        )
        pts = [
            ((100, y), (102, y)),
            ((102, y), (103, y)),
            ((103, y), (110, y)),
            ((102, y), (102, y + 1)),
            ((102, y + 1), (103, y + 1)),
            ((103, y + 1), (103, y)),
        ]
        segs = "".join(
            f"  (segment (start {a[0] + dx} {a[1]}) (end {b[0] + dx} {b[1]}) (width 0.2) "
            f'(layer "F.Cu") (net {net}) (uuid "s-{next(_uid)}"))\n'
            for a, b in pts
        )
        return pads + segs

    body = HEADER.replace('(net 1 "SIG")', '(net 1 "SIG")\n  (net 2 "SIG2")')
    body += net_block(1, "SIG", 100, 0.0) + net_block(2, "SIG2", 120, x_offset_b) + ")\n"
    p = tmp_path / "two.kicad_pcb"
    p.write_text(body)
    return p


def _seg_keys(pcb: PCB, net: int):
    return sorted((s.start, s.end, s.layer) for s in pcb.segments if s.net_number == net)


def test_nets_scope_changes_only_named_net(tmp_path):
    p = _two_net_board(tmp_path)
    full, full_res = prune_board(p, verify_drc=False)
    assert {pl.net_name for pl in full_res.accepted} == {"SIG", "SIG2"}

    pcb, res = prune_board(p, verify_drc=False, nets=["SIG"])
    orig = PCB.load(p)
    assert [pl.net_name for pl in res.accepted] == ["SIG"]
    assert _seg_keys(pcb, 2) == _seg_keys(orig, 2)  # net 2 copper untouched
    assert _seg_keys(pcb, 1) == _seg_keys(full, 1)  # net 1 pruned as in full run
    assert len(_seg_keys(pcb, 1)) < len(_seg_keys(orig, 1))


def test_region_applies_inside_and_skips_straddling_plans(tmp_path):
    p = _two_net_board(tmp_path)
    # Box covers net SIG (y 100..101) but not SIG2 (y 120..121).
    region = (95.0, 95.0, 115.0, 105.0)
    pcb, res = prune_board(p, verify_drc=False, region=region)
    assert [pl.net_name for pl in res.accepted] == ["SIG"]
    assert [pl.net_name for pl in res.region_skipped] == ["SIG2"]
    orig = PCB.load(p)
    assert _seg_keys(pcb, 2) == _seg_keys(orig, 2)

    # A box that clips part of SIG's edits skips it entirely (never partial).
    pcb2, res2 = prune_board(p, verify_drc=False, nets=["SIG"], region=(95, 95, 102.5, 105))
    assert res2.accepted == [] and [pl.net_name for pl in res2.region_skipped] == ["SIG"]
    assert _seg_keys(pcb2, 1) == _seg_keys(orig, 1)


def _cli_prune(p: Path, *extra: str) -> int:
    from kicad_tools.cli.commands.pcb import _run_prune_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(
        ["pcb", "prune", str(p), "--dry-run", "--no-drc", "--format", "json", *extra]
    )
    return _run_prune_command(args, p)


def test_cli_nets_and_region_json_scope(tmp_path, capsys):
    p = _two_net_board(tmp_path)
    assert _cli_prune(p, "--nets", "SIG2", "--region", "95,115,115,125") == 0
    out = json.loads(capsys.readouterr().out)
    assert [n["net"] for n in out["nets"]] == ["SIG2"]
    assert out["scope"] == {"nets": ["SIG2"], "region": [95.0, 115.0, 115.0, 125.0]}
    assert out["region_skipped"] == []

    assert _cli_prune(p, "--region", "95,95,115,105") == 0
    out = json.loads(capsys.readouterr().out)
    assert [n["net"] for n in out["region_skipped"]] == ["SIG2"]


def test_cli_rejects_unknown_net_and_bad_region(tmp_path, capsys):
    p = _two_net_board(tmp_path)
    assert _cli_prune(p, "--nets", "NOPE") == 1
    assert "NOPE" in capsys.readouterr().err
    assert _cli_prune(p, "--region", "10,10,5,20") == 1
    assert "x1 < x2" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Issue #8687: the prune plan must not turn a resolved declared-current net
# ambiguous (a stub that runs parallel to the trunk it is anchored to).
# ---------------------------------------------------------------------------


def _looping_plan(net_number: int = 1, net_name: str = "SIG"):
    from kicad_tools.pcb_prune import NetPlan

    # Two pieces from pad A's centre to B's: a second path beside the trunk.
    return NetPlan(
        net_number,
        net_name,
        stubs=[
            ("F.Cu", (100, 100), (102, 102), 0.2),
            ("F.Cu", (102, 102), (105, 100), 0.2),
        ],
    )


def _gate_board(tmp_path: Path) -> tuple[Path, Path]:
    p = _board(tmp_path, [("A", 100, 100), ("B", 105, 100)], [((100, 100), (105, 100))])
    return p, _specs(tmp_path)


@pytest.fixture
def forced_looping_plan(monkeypatch):
    import kicad_tools.pcb_prune as mod

    monkeypatch.setattr(mod, "plan_prune", lambda *a, **k: [_looping_plan()])


def test_audit_gate_rejects_plan_that_resolves_to_ambiguous(tmp_path, forced_looping_plan):
    from kicad_tools.router.current_paths import load_current_path_specs

    p, cp = _gate_board(tmp_path)
    pcb, res = prune_board(
        p, verify_drc=False, current_paths=load_current_path_specs(cp), apply=True
    )
    assert not res.accepted
    assert len(res.rejected) == 1
    assert "resolved to ambiguous" in res.rejected[0].reject_reason
    assert len(pcb.segments) == 1  # nothing was added


def test_audit_gate_uses_discovered_sidecar(tmp_path, forced_looping_plan):
    p, cp = _gate_board(tmp_path)
    cp.rename(tmp_path / "current_paths.json")
    pcb, res = prune_board(p, verify_drc=False)
    assert len(res.rejected) == 1 and not res.accepted
    # Discovery only feeds the gate: no loop-audit report is produced.
    assert res.uncut_loops == []


def test_audit_gate_inactive_without_specs(tmp_path, forced_looping_plan):
    p, _ = _gate_board(tmp_path)
    pcb, res = prune_board(p, verify_drc=False)
    assert len(res.accepted) == 1 and not res.rejected


def test_audit_gate_warns_and_continues_on_unparsable_sidecar(tmp_path, forced_looping_plan):
    p, _ = _gate_board(tmp_path)
    (tmp_path / "current_paths.json").write_text("{not json")
    _, res = prune_board(p, verify_drc=False)
    assert len(res.accepted) == 1 and not res.rejected


def test_audit_gate_is_per_net(tmp_path, monkeypatch):
    import kicad_tools.pcb_prune as mod
    from kicad_tools.pcb_prune import NetPlan
    from kicad_tools.router.current_paths import load_current_path_specs

    p, cp = _gate_board(tmp_path)
    other = NetPlan(2, "OTHER", stubs=[("F.Cu", (90, 90), (91, 90), 0.2)])
    monkeypatch.setattr(mod, "plan_prune", lambda *a, **k: [_looping_plan(), other])
    _, res = prune_board(
        p, verify_drc=False, current_paths=load_current_path_specs(cp), apply=False
    )
    assert [x.net_name for x in res.rejected] == ["SIG"]
    assert [x.net_name for x in res.accepted] == ["OTHER"]


def test_cli_json_lists_added_stubs_per_net(tmp_path, capsys):
    segs = [((100, 100), (104.82, 100))]
    p = _board(tmp_path, [("A", 100, 100)], segs, vias=[(105, 100.08)])
    assert _cli(tmp_path, p, "--no-drc", "--dry-run") == 0
    out = json.loads(capsys.readouterr().out)
    stubs = out["nets"][0]["stubs_added"]
    assert len(stubs) == 2
    assert {"layer", "start", "end", "width"} == set(stubs[0])


def test_cli_json_reports_audit_rejection_reason(tmp_path, capsys, forced_looping_plan):
    p, cp = _gate_board(tmp_path)
    assert _cli(tmp_path, p, "--no-drc", "--dry-run", "--current-paths", str(cp)) == 0
    out = json.loads(capsys.readouterr().out)
    assert "resolved to ambiguous" in out["rejected_by_drc"][0]["reject_reason"]
