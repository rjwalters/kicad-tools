"""Issue #6668: pre-existing tracks/vias of SIBLING stitch nets are obstacles.

Board 07 stitches ``--net GND --net +1V2 --net +1V8`` in one call.  Before the
fix, ``run_stitch`` built its foreign track/via pools with
``exclude_nets=net_numbers`` -- which dropped the pre-existing copper of EVERY
target net, not just the pad's own.  ``kct route`` had already fanned U3.11
(GND) out with a stub (136.2,152)->(136.2,153.1)->(137,153.1) and a 0.6/0.3
via at (137,153.1), 1.1 mm in front of U3.12 (+1V8, pad at (137,152)).  The
+1V8 escape for U3.12 was then drawn straight through that GND via and stub:
a real GND/+1V8 short (kicad-cli: ``tracks_crossing`` / ``shorting_items``).

The fixture reproduces that geometry.  As on board 07, U3.12 has no straight
or dog-leg site: U3.13 (a signal pad 0.8 mm to the right) blocks +x, the GND
via's drill blocks straight down (the drill registry already saw sibling
holes, #4177), and a foreign signal bus above the pad row blocks every upward
site.  Pre-fix, the extended escape then ran (137,152)->(137,152.95)->
(137.8,152.95), straight through the invisible GND via and stub.

Distances are real copper EDGE distances (centre distance minus the via radius
and/or half track width), not centre distances.
"""

from __future__ import annotations

import math
from pathlib import Path

import pytest

from kicad_tools.cli import stitch_cmd
from kicad_tools.cli.stitch_cmd import (
    TrackSegment,
    point_to_segment_distance,
    run_stitch,
    segment_to_segment_distance,
    trace_to_track_segments,
)
from kicad_tools.router.track_index import TRACK_INDEX_MIN_SEGMENTS

CLEARANCE = 0.2

# Pre-existing GND fanout of U3.11 (the board-07 ``kct route`` output).
GND_SEGMENTS = [
    TrackSegment(136.2, 152.0, 136.2, 153.1, 0.2, "F.Cu", 1),
    TrackSegment(136.2, 153.1, 137.0, 153.1, 0.2, "F.Cu", 1),
]
GND_VIA = (137.0, 153.1, 0.6)  # (x, y, size); drill 0.3


def _pad(num: str, x: float, net: int, name: str) -> str:
    """A 0.4 x 0.9 mm QFN-style SMD pad of U3, absolute position (x, 152)."""
    return (
        f'    (pad "{num}" smd roundrect (at {x - 140:.1f} 0) (size 0.4 0.9) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (roundrect_rratio 0.25) '
        f'(net {net} "{name}"))\n'
    )


def _board(extra_copper: str) -> str:
    return (
        "(kicad_pcb\n"
        "  (version 20240108)\n"
        '  (generator "test")\n'
        '  (generator_version "8.0")\n'
        "  (general (thickness 1.6) (legacy_teardrops no))\n"
        '  (paper "A4")\n'
        "  (layers\n"
        '    (0 "F.Cu" signal)\n'
        '    (1 "In1.Cu" signal)\n'
        '    (2 "In2.Cu" signal)\n'
        '    (31 "B.Cu" signal)\n'
        "  )\n"
        "  (setup (pad_to_mask_clearance 0))\n"
        '  (net 0 "")\n'
        '  (net 1 "GND")\n'
        '  (net 2 "+1V8")\n'
        '  (net 3 "SIG_A")\n'
        '  (net 4 "SIG_B")\n'
        '  (footprint "Package_DFN_QFN:QFN-test"\n'
        '    (layer "F.Cu")\n'
        '    (uuid "00000000-0000-0000-0000-000000006603")\n'
        "    (at 140 152)\n"
        '    (property "Reference" "U3" (at 0 -3 0) (layer "F.SilkS") (uuid "ref-u3"))\n'
        + _pad("10", 135.4, 3, "SIG_A")
        + _pad("11", 136.2, 1, "GND")
        + _pad("12", 137.0, 2, "+1V8")
        + _pad("13", 137.8, 4, "SIG_B")
        + "  )\n"
        + extra_copper
        + ")\n"
    )


def _segment(seg: TrackSegment, net_name: str, uid: str) -> str:
    return (
        f"  (segment (start {seg.start_x} {seg.start_y}) (end {seg.end_x} {seg.end_y}) "
        f'(width {seg.width}) (layer "{seg.layer}") (net {seg.net_number}) (uuid "{uid}"))\n'
    )


GND_FANOUT = (
    _segment(GND_SEGMENTS[0], "GND", "gnd-seg-1")
    + _segment(GND_SEGMENTS[1], "GND", "gnd-seg-2")
    + f"  (via (at {GND_VIA[0]} {GND_VIA[1]}) (size {GND_VIA[2]}) (drill 0.3) "
    '(layers "F.Cu" "B.Cu") (net 1) (uuid "gnd-via-1"))\n'
)


def _stitch(tmp_path: Path, board: str, nets: list[str], **kwargs):
    pcb = tmp_path / "u3_fanout.kicad_pcb"
    pcb.write_text(board)
    return run_stitch(
        pcb_path=pcb,
        net_names=nets,
        via_size=0.45,
        drill=0.2,
        clearance=CLEARANCE,
        offset=0.5,
        trace_width=0.2,
        dry_run=True,
        **kwargs,
    )


def _seg_edge_gap(a: TrackSegment, b: TrackSegment) -> float:
    centre = segment_to_segment_distance(
        a.start_x, a.start_y, a.end_x, a.end_y, b.start_x, b.start_y, b.end_x, b.end_y
    )
    return centre - a.width / 2 - b.width / 2


def _via_seg_edge_gap(vx: float, vy: float, vsize: float, seg: TrackSegment) -> float:
    centre = point_to_segment_distance(vx, vy, seg.start_x, seg.start_y, seg.end_x, seg.end_y)
    return centre - vsize / 2 - seg.width / 2


def _plus_1v8_copper_gaps(result) -> list[tuple[str, float]]:
    """Every (description, copper-edge gap) between new +1V8 copper and the
    pre-existing GND stub/via."""
    gx, gy, gsize = GND_VIA
    gaps: list[tuple[str, float]] = []
    for via in result.vias_added:
        if via.pad.net_name != "+1V8":
            continue
        for i, seg in enumerate(GND_SEGMENTS):
            gaps.append(
                (f"+1V8 via -> GND seg {i}", _via_seg_edge_gap(via.via_x, via.via_y, via.size, seg))
            )
        gaps.append(
            (
                "+1V8 via -> GND via",
                math.hypot(via.via_x - gx, via.via_y - gy) - via.size / 2 - gsize / 2,
            )
        )
    for trace in result.traces_added:
        if trace.pad.net_name != "+1V8":
            continue
        for leg in trace_to_track_segments(trace):
            for i, seg in enumerate(GND_SEGMENTS):
                if leg.layer == seg.layer:
                    gaps.append((f"+1V8 leg -> GND seg {i}", _seg_edge_gap(leg, seg)))
            gaps.append(("+1V8 leg -> GND via", _via_seg_edge_gap(gx, gy, gsize, leg)))
    return gaps


# A wide foreign-net (SIG_A) bus above the U3 pad row: it blocks every
# upward via site for U3.12, forcing the extended escape that shorted board 07.
SIGNAL_BUS = _segment(TrackSegment(130.0, 150.3, 144.0, 150.3, 1.0, "F.Cu", 3), "SIG_A", "bus")


def test_plus_1v8_escape_clears_sibling_gnd_fanout(tmp_path: Path) -> None:
    """``nets=[GND, +1V8]``: no +1V8 via or trace leg comes within clearance of
    U3.11's pre-existing GND stub or via (copper-edge distance).

    Here the GND fanout leaves U3.12 no legal site at all, so the correct
    outcome is a skip whose diagnostic names the sibling GND copper -- a
    stranded pad is a visible, fixable DRC ``unconnected``; the pre-fix
    escape was a silent GND/+1V8 short.
    """
    result = _stitch(tmp_path, _board(GND_FANOUT + SIGNAL_BUS), ["GND", "+1V8"])

    violations = [
        (what, round(gap, 4))
        for what, gap in _plus_1v8_copper_gaps(result)
        if gap < CLEARANCE - 1e-6
    ]
    assert violations == [], (
        f"+1V8 stitch copper within {CLEARANCE}mm of pre-existing sibling GND copper "
        f"(vias {[(v.via_x, v.via_y) for v in result.vias_added]}, traces "
        f"{[(t.waypoints, t.intermediate_x, t.intermediate_y) for t in result.traces_added]}): "
        f"{violations}"
    )

    skipped = {f"{p.reference}.{p.pad_number}": reason for p, reason in result.pads_skipped}
    placed = {f"{v.pad.reference}.{v.pad.pad_number}" for v in result.vias_added}
    assert "U3.12" in skipped or "U3.12" in placed
    if "U3.12" in skipped:
        # The skip diagnostic sees the sibling copper too (same per-net pools).
        assert "'GND'" in skipped["U3.12"], skipped["U3.12"]


def test_plus_1v8_still_placed_when_a_clear_site_exists(tmp_path: Path) -> None:
    """No over-rejection: without the signal bus, U3.12 gets a via clear of GND."""
    result = _stitch(tmp_path, _board(GND_FANOUT), ["GND", "+1V8"])

    placed = [v for v in result.vias_added if v.pad.net_name == "+1V8"]
    assert placed, f"U3.12 must still be stitched; skipped: {result.pads_skipped}"
    gaps = _plus_1v8_copper_gaps(result)
    assert gaps
    assert min(gap for _, gap in gaps) >= CLEARANCE - 1e-6, gaps


@pytest.mark.parametrize("extra", ["", SIGNAL_BUS], ids=["open", "signal-bus"])
def test_single_net_and_multi_net_agree_for_plus_1v8(tmp_path: Path, extra: str) -> None:
    """Stitching +1V8 alone already saw GND as foreign copper; a multi-net call
    must make the same decision for U3.12 (same site, or the same skip)."""
    single = _stitch(tmp_path, _board(GND_FANOUT + extra), ["+1V8"])
    multi = _stitch(tmp_path, _board(GND_FANOUT + extra), ["GND", "+1V8"])

    def _outcome(result):
        sites = [
            (round(v.via_x, 4), round(v.via_y, 4))
            for v in result.vias_added
            if v.pad.net_name == "+1V8"
        ]
        skips = sorted(
            f"{p.reference}.{p.pad_number}" for p, _ in result.pads_skipped if p.net_name == "+1V8"
        )
        return sites, skips

    assert _outcome(multi) == _outcome(single)


def test_own_net_stubs_do_not_block_own_escape(tmp_path: Path) -> None:
    """Negative control: a pad's OWN pre-existing copper must not be an obstacle.

    U3.11 (GND) carries a star of eight GND stubs radiating from the pad --
    one along every candidate direction -- so if own-net copper leaked into
    the obstacle pool every candidate site would be rejected and the pad
    skipped.  ``force_pads`` bypasses the "already connected" skip so the
    placement cascade actually runs.
    """
    star = ""
    cx, cy = 136.2, 152.0
    for k in range(8):
        ang = k * math.pi / 4
        seg = TrackSegment(
            cx,
            cy,
            round(cx + 2.5 * math.cos(ang), 4),
            round(cy + 2.5 * math.sin(ang), 4),
            0.2,
            "F.Cu",
            1,
        )
        star += _segment(seg, "GND", f"gnd-star-{k}")
    result = _stitch(
        tmp_path,
        _board(star),
        ["GND", "+1V8"],
        only_pads=frozenset({"U3.11", "U3.12"}),
        force_pads=frozenset({"U3.11"}),
    )

    gnd = [v for v in result.vias_added if v.pad.reference == "U3" and v.pad.pad_number == "11"]
    assert gnd, f"own-net GND stubs blocked U3.11's own escape: {result.pads_skipped}"


def test_base_track_index_built_once_per_net_not_per_pad(tmp_path: Path, monkeypatch) -> None:
    """#5240 is preserved: the pre-existing foreign track pool is indexed once
    per target NET, never once per pad.

    The board carries enough far-away signal copper (>= the index threshold)
    that a real index is built, and four stitched pads over two nets.  A pool
    holding only pre-existing board copper is a per-net base pool; per-pad
    pools that add just-placed cross-net stitch legs are allowed (unchanged
    #3633/#5240 behaviour) and are told apart because they contain segments
    that are not on the board.
    """
    filler = "".join(
        _segment(
            TrackSegment(100.0, 170.0 + 0.5 * k, 110.0, 170.0 + 0.5 * k, 0.2, "F.Cu", 3),
            "",
            f"sig-{k}",
        )
        for k in range(TRACK_INDEX_MIN_SEGMENTS + 6)
    )
    board = (
        _board(GND_FANOUT + filler)
        .replace('(net 3 "SIG_A"))', '(net 2 "+1V8"))')
        .replace('(net 4 "SIG_B"))', '(net 1 "GND"))')
    )
    board_segments = {(s.start_x, s.start_y, s.end_x, s.end_y) for s in GND_SEGMENTS} | {
        (100.0, 170.0 + 0.5 * k, 110.0, 170.0 + 0.5 * k)
        for k in range(TRACK_INDEX_MIN_SEGMENTS + 6)
    }

    base_pool_calls: list[int] = []
    real = stitch_cmd.build_track_index

    def _counting(segments, *args, **kwargs):
        segs = list(segments or [])
        if segs and all(
            (round(t.start_x, 4), round(t.start_y, 4), round(t.end_x, 4), round(t.end_y, 4))
            in board_segments
            for t in segs
        ):
            base_pool_calls.append(len(segs))
        return real(segments, *args, **kwargs)

    monkeypatch.setattr(stitch_cmd, "build_track_index", _counting)
    result = _stitch(tmp_path, board, ["GND", "+1V8"], force_pads=frozenset({"U3.11"}))

    assert len(result.vias_added) >= 3, result.pads_skipped
    # One base pool per target net (GND's holds the filler only; +1V8's also
    # holds the sibling GND fanout) -- regardless of the four pads.
    assert sorted(base_pool_calls) == [
        TRACK_INDEX_MIN_SEGMENTS + 6,
        TRACK_INDEX_MIN_SEGMENTS + 6 + len(GND_SEGMENTS),
    ], base_pool_calls
