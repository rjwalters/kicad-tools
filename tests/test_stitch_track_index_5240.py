"""Equivalence tests for the stitch track index (Issue #5240).

The pad-aware stitch pass (phase 10 of the board recipes) re-scans *every*
foreign-net track segment for *every* candidate via position tried by
:func:`~kicad_tools.cli.stitch_cmd.calculate_via_position`,
:func:`~kicad_tools.cli.stitch_cmd.calculate_dogleg_via_position` and
:func:`~kicad_tools.cli.stitch_cmd.calculate_extended_escape_position`.
Issue #5240 bins those segments into a uniform grid
(:class:`kicad_tools.router.track_index.TrackSpatialIndex`) so a candidate
only meets the segments whose copper could possibly violate its clearance.

The optimisation is admissible **only** if it is exactly output-preserving:
the issue forbids "a faster result obtained by dropping work".  Every test
here therefore pins the indexed answer to the pre-#5240 linear answer on
the same geometry.  The linear path is forced by monkeypatching
``stitch_cmd.build_track_index`` to return ``None``, which is precisely the
"no index -- keep the original full scan" branch every call site retains.

Geometry deliberately includes what a real routed board presents: dense
fine-pitch pad fields, tracks on several copper layers, wide and hairline
widths, zero-length (dot) segments, and a board-spanning segment that
overflows the index's per-segment cell-span cap.
"""

from __future__ import annotations

import random
from pathlib import Path

import pytest

from kicad_tools.cli import stitch_cmd
from kicad_tools.cli.stitch_cmd import (
    FilledPolygon,
    PadInfo,
    TrackSegment,
    _check_dogleg_path_clearance,
    _check_multileg_path_clearance,
    calculate_dogleg_via_position,
    calculate_extended_escape_position,
    calculate_via_position,
    point_to_segment_distance,
    run_stitch,
    segment_to_segment_distance,
)
from kicad_tools.router.track_index import (
    TRACK_INDEX_MIN_SEGMENTS,
    TrackSpatialIndex,
    build_track_index,
)

# --- geometry fixtures --------------------------------------------------------

_LAYERS = ("F.Cu", "In1.Cu", "In2.Cu", "B.Cu")
_WIDTHS = (0.09, 0.1, 0.2, 0.35, 0.8)


def _random_pool(seed: int, count: int = 140) -> list[TrackSegment]:
    """A board-like foreign-net track pool inside a 40x30mm window."""
    rng = random.Random(seed)
    pool: list[TrackSegment] = []
    for i in range(count):
        sx = rng.uniform(0.0, 40.0)
        sy = rng.uniform(0.0, 30.0)
        # Mostly short routed runs, with a few long haul segments.
        span = rng.choice([0.3, 0.8, 1.5, 4.0, 12.0])
        pool.append(
            TrackSegment(
                start_x=sx,
                start_y=sy,
                end_x=sx + rng.uniform(-span, span),
                end_y=sy + rng.uniform(-span, span),
                width=rng.choice(_WIDTHS),
                layer=rng.choice(_LAYERS),
                net_number=10 + (i % 7),
            )
        )
    # A zero-length "dot" segment (KiCad emits these after nudges) and a
    # board-spanning segment that trips the per-segment cell-span cap.
    pool.append(
        TrackSegment(
            start_x=17.0,
            start_y=13.0,
            end_x=17.0,
            end_y=13.0,
            width=0.2,
            layer="F.Cu",
            net_number=9,
        )
    )
    pool.append(
        TrackSegment(
            start_x=-5.0,
            start_y=-5.0,
            end_x=45.0,
            end_y=35.0,
            width=0.2,
            layer="F.Cu",
            net_number=8,
        )
    )
    return pool


@pytest.fixture
def pool() -> list[TrackSegment]:
    return _random_pool(0x5240)


@pytest.fixture
def force_linear(monkeypatch):
    """Force every call site back onto its pre-#5240 full linear scan."""

    def _apply():
        monkeypatch.setattr(stitch_cmd, "build_track_index", lambda *a, **k: None)

    return _apply


def _pad(x: float, y: float, net: int = 1, layer: str = "F.Cu") -> PadInfo:
    return PadInfo(
        reference="U1",
        pad_number="1",
        net_number=net,
        net_name="GND",
        x=x,
        y=y,
        layer=layer,
        width=0.25,
        height=0.25,
    )


# --- index-level: the query is a conservative superset ------------------------


def test_board_sized_pool_is_indexed(pool):
    """The fixture is large enough that the real code path indexes it."""
    assert len(pool) >= TRACK_INDEX_MIN_SEGMENTS
    assert isinstance(build_track_index(pool), TrackSpatialIndex)


def test_small_pool_is_not_indexed():
    """Below the threshold the linear scan is cheaper -- no index is built."""
    assert build_track_index(_random_pool(1, count=4)) is None
    assert build_track_index([]) is None
    assert build_track_index(None) is None


def test_point_query_returns_every_segment_the_exact_test_could_reject(pool):
    """No segment that violates the exact point clearance is pruned away."""
    index = build_track_index(pool)
    assert index is not None
    rng = random.Random(7)
    narrowed_somewhere = False
    for _ in range(250):
        x = rng.uniform(-2.0, 42.0)
        y = rng.uniform(-2.0, 32.0)
        radius = rng.choice([0.1, 0.225, 0.425, 0.9])
        returned = index.near_point(x, y, radius)
        exact = [
            seg
            for seg in pool
            if point_to_segment_distance(x, y, seg.start_x, seg.start_y, seg.end_x, seg.end_y)
            < radius + seg.width / 2
        ]
        for seg in exact:
            assert seg in returned, (x, y, radius)
        if len(returned) < len(pool):
            narrowed_somewhere = True
    # A query that always returned the whole pool would make this vacuous.
    assert narrowed_somewhere


def test_segment_query_returns_every_segment_the_exact_test_could_reject(pool):
    """No segment that violates the exact segment clearance is pruned away."""
    index = build_track_index(pool)
    assert index is not None
    rng = random.Random(11)
    for _ in range(200):
        sx = rng.uniform(-2.0, 42.0)
        sy = rng.uniform(-2.0, 32.0)
        ex = sx + rng.uniform(-6.0, 6.0)
        ey = sy + rng.uniform(-6.0, 6.0)
        radius = rng.choice([0.1, 0.3, 0.75])
        returned = index.near_segment(sx, sy, ex, ey, radius)
        exact = [
            seg
            for seg in pool
            if segment_to_segment_distance(
                sx, sy, ex, ey, seg.start_x, seg.start_y, seg.end_x, seg.end_y
            )
            < radius + seg.width / 2
        ]
        for seg in exact:
            assert seg in returned, (sx, sy, ex, ey, radius)


def test_board_spanning_segment_is_always_returned(pool):
    """The oversize overflow list is consulted by every query."""
    index = build_track_index(pool)
    assert index is not None
    spanning = pool[-1]
    # Far from the diagonal, in a corner it never passes through.
    assert spanning in index.near_point(41.0, 1.0, 0.1)
    assert spanning in index.near_segment(41.0, 1.0, 41.5, 1.5, 0.1)


def test_query_preserves_insertion_order(pool):
    """Short-circuiting callers see the same relative segment order."""
    index = build_track_index(pool)
    assert index is not None
    positions = {id(seg): i for i, seg in enumerate(pool)}
    for x, y in ((12.0, 9.0), (3.5, 27.0), (33.0, 4.5)):
        returned = index.near_segment(x, y, x + 3.0, y + 3.0, 0.5)
        order = [positions[id(seg)] for seg in returned]
        assert order == sorted(order)
        assert len(order) == len(set(order))  # no duplicates across cells


def test_huge_radius_query_degrades_to_the_whole_pool(pool):
    """A radius covering the board returns everything -- still exact."""
    index = build_track_index(pool)
    assert index is not None
    assert len(index.near_point(20.0, 15.0, 100.0)) == len(pool)


# --- placement-strategy equivalence (indexed vs. forced-linear) --------------


@pytest.mark.parametrize("seed", [0x5240, 0xBEEF])
def test_calculate_via_position_matches_linear(seed, force_linear):
    tracks = _random_pool(seed)
    vias = [(3.0, 3.0, 2), (21.0, 16.0, 2)]
    pads = [(px, py, 0.2, 2) for px, py in ((10.5, 9.5), (11.0, 9.5), (10.5, 10.0))]
    drills = [(7.0, 7.0, 0.3, 4), (30.0, 20.0, 0.3, 4)]
    samples = [_pad(x / 2.0, y / 2.0) for x in range(2, 80, 11) for y in range(2, 60, 13)]

    indexed = [
        calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=vias,
            clearance=0.2,
            other_net_tracks=tracks,
            other_net_pads=pads,
            trace_width=0.2,
            other_net_drills=drills,
            via_drill=0.2,
        )
        for pad in samples
    ]
    force_linear()
    linear = [
        calculate_via_position(
            pad,
            offset=0.5,
            via_size=0.45,
            existing_vias=vias,
            clearance=0.2,
            other_net_tracks=tracks,
            other_net_pads=pads,
            trace_width=0.2,
            other_net_drills=drills,
            via_drill=0.2,
        )
        for pad in samples
    ]
    assert indexed == linear
    # Non-vacuity: the sample must contain both placements and refusals.
    assert any(p is not None for p in linear)
    assert any(p is None for p in linear)


@pytest.mark.parametrize("seed", [0x5240, 0xBEEF])
def test_calculate_dogleg_via_position_matches_linear(seed, force_linear):
    tracks = _random_pool(seed)
    pads = [(px, py, 0.2, 2) for px, py in ((10.5, 9.5), (11.0, 9.5), (10.5, 10.0))]
    drills = [(7.0, 7.0, 0.3, 4)]
    samples = [_pad(x / 2.0, y / 2.0) for x in range(2, 80, 17) for y in range(2, 60, 19)]

    def _run():
        return [
            calculate_dogleg_via_position(
                pad,
                offset=0.5,
                via_size=0.45,
                existing_vias=[(3.0, 3.0, 2)],
                clearance=0.2,
                other_net_tracks=tracks,
                other_net_pads=pads,
                trace_width=0.2,
                other_net_drills=drills,
                via_drill=0.2,
            )
            for pad in samples
        ]

    indexed = _run()
    force_linear()
    linear = _run()
    assert indexed == linear
    assert any(p is not None for p in linear)


@pytest.mark.parametrize("seed", [0x5240])
def test_calculate_extended_escape_position_matches_linear(seed, force_linear):
    tracks = _random_pool(seed)
    pads = [(px, py, 0.2, 2) for px, py in ((10.5, 9.5), (11.0, 9.5), (10.5, 10.0))]
    drills = [(7.0, 7.0, 0.3, 4)]
    samples = [_pad(x / 2.0, y / 2.0) for x in range(2, 80, 23) for y in range(2, 60, 29)]

    def _run():
        return [
            calculate_extended_escape_position(
                pad,
                offset=0.5,
                via_size=0.45,
                existing_vias=[(3.0, 3.0, 2)],
                clearance=0.2,
                other_net_tracks=tracks,
                other_net_pads=pads,
                trace_width=0.2,
                other_net_drills=drills,
                via_drill=0.2,
            )
            for pad in samples
        ]

    indexed = _run()
    force_linear()
    linear = _run()
    assert indexed == linear
    assert any(p is not None for p in linear)


def test_check_dogleg_path_clearance_matches_linear(pool, force_linear):
    fills = [
        FilledPolygon(
            net_number=2,
            net_name="+3V3",
            layer="In2.Cu",
            points=[(0.0, 0.0), (40.0, 0.0), (40.0, 2.0), (0.0, 2.0)],
        )
    ]
    vias = [(6.0, 6.0, 0.45, 2)]
    pads = [(12.0, 12.0, 0.2, 2)]
    rng = random.Random(5240)
    samples = []
    for _ in range(120):
        px = rng.uniform(0.0, 40.0)
        py = rng.uniform(2.5, 30.0)
        ix = px + rng.uniform(-1.2, 1.2)
        iy = py + rng.uniform(-1.2, 1.2)
        samples.append((px, py, ix, iy, ix + rng.uniform(-1.2, 1.2), iy + rng.uniform(-1.2, 1.2)))

    def _run(index):
        return [
            _check_dogleg_path_clearance(
                px, py, ix, iy, vx, vy, 0.1, pool, vias, pads, 0.2, fills, index
            )
            for px, py, ix, iy, vx, vy in samples
        ]

    indexed = _run(build_track_index(pool))
    linear = _run(None)
    assert indexed == linear
    assert True in linear and False in linear


def test_check_multileg_path_clearance_matches_linear(pool):
    vias = [(6.0, 6.0, 0.45, 2)]
    pads = [(12.0, 12.0, 0.2, 2)]
    rng = random.Random(99)
    samples = []
    for _ in range(120):
        px = rng.uniform(0.0, 40.0)
        py = rng.uniform(0.0, 30.0)
        wx = px + rng.uniform(-1.2, 1.2)
        wy = py + rng.uniform(-1.2, 1.2)
        samples.append(
            [
                (px, py),
                (wx, wy),
                (wx + rng.uniform(-1.2, 1.2), wy + rng.uniform(-1.2, 1.2)),
            ]
        )

    def _run(index):
        return [
            _check_multileg_path_clearance(pts, 0.1, pool, vias, pads, 0.2, index)
            for pts in samples
        ]

    indexed = _run(build_track_index(pool))
    linear = _run(None)
    assert indexed == linear
    assert True in linear and False in linear


# --- end-to-end: the written board is byte-identical -------------------------


def _dense_stitch_board() -> str:
    """A 4-layer board with a dense GND pad field under heavy foreign copper.

    Sized so the foreign-net track pool clears ``TRACK_INDEX_MIN_SEGMENTS``
    (an unindexed pool would make the comparison vacuous) and so the pads
    exercise the straight / dog-leg / extended-escape ladder rather than
    all succeeding on the first candidate.
    """
    lines = [
        "(kicad_pcb",
        "  (version 20240108)",
        '  (generator "test")',
        '  (generator_version "8.0")',
        "  (general (thickness 1.6) (legacy_teardrops no))",
        '  (paper "A4")',
        "  (layers",
        '    (0 "F.Cu" signal)',
        '    (1 "In1.Cu" signal)',
        '    (2 "In2.Cu" signal)',
        '    (31 "B.Cu" signal)',
        '    (44 "Edge.Cuts" user)',
        "  )",
        "  (setup (pad_to_mask_clearance 0))",
        '  (net 0 "")',
        '  (net 1 "GND")',
    ]
    for n in range(2, 10):
        lines.append(f'  (net {n} "SIG{n}")')

    # 6x6 fine-pitch field: alternating GND / signal pads at 0.5mm pitch.
    lines += [
        '  (footprint "Package_DFN_QFN:QFN-36"',
        '    (layer "F.Cu")',
        '    (uuid "00000000-0000-0000-0000-0000000000aa")',
        "    (at 20 15)",
        '    (property "Reference" "U1" (at 0 -4 0) (layer "F.SilkS") (uuid "ref-u1"))',
    ]
    pad_no = 0
    for row in range(6):
        for col in range(6):
            pad_no += 1
            net = 1 if (row + col) % 2 == 0 else 2 + ((row * 6 + col) % 8)
            name = "GND" if net == 1 else f"SIG{net}"
            px = -1.25 + 0.5 * col
            py = -1.25 + 0.5 * row
            lines.append(
                f'    (pad "{pad_no}" smd rect (at {px} {py}) (size 0.25 0.25) '
                f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{name}"))'
            )
    lines.append("  )")

    # Foreign-net copper: a fence of short F.Cu runs boxing the pad field in,
    # plus inner-layer clutter that the layer-aware strategy must ignore.
    uid = 0
    for i in range(24):
        uid += 1
        t = i * 0.5
        lines.append(
            f"  (segment (start {16.0 + t} 11.4) (end {16.2 + t} 11.4) "
            f'(width 0.2) (layer "F.Cu") (net 3) (uuid "seg-n-{uid:04d}"))'
        )
    for i in range(24):
        uid += 1
        t = i * 0.5
        lines.append(
            f"  (segment (start {16.0 + t} 18.6) (end {16.2 + t} 18.6) "
            f'(width 0.2) (layer "F.Cu") (net 4) (uuid "seg-s-{uid:04d}"))'
        )
    for i in range(20):
        uid += 1
        t = i * 0.6
        lines.append(
            f"  (segment (start 16.6 {12.0 + t}) (end 16.6 {12.2 + t}) "
            f'(width 0.2) (layer "F.Cu") (net 5) (uuid "seg-w-{uid:04d}"))'
        )
        uid += 1
        lines.append(
            f"  (segment (start 23.4 {12.0 + t}) (end 23.4 {12.2 + t}) "
            f'(width 0.2) (layer "F.Cu") (net 6) (uuid "seg-e-{uid:04d}"))'
        )
    for i in range(12):
        uid += 1
        t = i * 1.0
        lines.append(
            f"  (segment (start 17.0 {13.0 + t * 0.2}) (end 24.0 {13.4 + t * 0.2}) "
            f'(width 0.15) (layer "In2.Cu") (net 7) (uuid "seg-i-{uid:04d}"))'
        )

    # GND plane on In1.Cu so the stitcher auto-detects a target layer.
    lines += [
        '  (zone (net 1) (net_name "GND") (layer "In1.Cu") (uuid "zone-gnd")',
        "    (hatch edge 0.5)",
        "    (connect_pads (clearance 0.2))",
        "    (min_thickness 0.25)",
        "    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.3))",
        "    (polygon (pts (xy 10 5) (xy 30 5) (xy 30 25) (xy 10 25)))",
        "  )",
        ")",
        "",
    ]
    return "\n".join(lines)


@pytest.fixture
def dense_board_text() -> str:
    return _dense_stitch_board()


def test_dense_board_pool_is_large_enough_to_be_indexed(dense_board_text, tmp_path):
    """Guard against a vacuous end-to-end comparison."""
    from kicad_tools.core.sexp_file import load_pcb

    pcb = tmp_path / "dense.kicad_pcb"
    pcb.write_text(dense_board_text)
    sexp = load_pcb(pcb)
    pool = stitch_cmd.find_all_track_segments(sexp, exclude_nets={1})
    assert len(pool) >= TRACK_INDEX_MIN_SEGMENTS
    assert build_track_index(pool) is not None


def _stitch(pcb: Path):
    return run_stitch(
        pcb_path=pcb,
        net_names=["GND"],
        clearance=0.2,
        offset=0.5,
        trace_width=0.2,
        avoid_pad_overlap=True,
    )


def test_run_stitch_writes_an_identical_board_with_and_without_the_index(
    dense_board_text, tmp_path, force_linear
):
    """The whole phase-10 invocation is byte-for-byte output-preserving."""
    indexed_pcb = tmp_path / "indexed.kicad_pcb"
    linear_pcb = tmp_path / "linear.kicad_pcb"
    indexed_pcb.write_text(dense_board_text)
    linear_pcb.write_text(dense_board_text)

    indexed_result = _stitch(indexed_pcb)
    force_linear()
    linear_result = _stitch(linear_pcb)

    # Non-vacuity: the run must actually place copper and exercise the
    # avoid-pad-overlap / skip bookkeeping.
    assert len(linear_result.vias_added) > 0

    assert [
        (v.pad.reference, v.pad.pad_number, v.via_x, v.via_y, v.size, v.drill, v.layers, v.via_type)
        for v in indexed_result.vias_added
    ] == [
        (v.pad.reference, v.pad.pad_number, v.via_x, v.via_y, v.size, v.drill, v.layers, v.via_type)
        for v in linear_result.vias_added
    ]
    assert [
        (t.pad.pad_number, t.via_x, t.via_y, t.width, t.layer, tuple(t.waypoints or ()))
        for t in indexed_result.traces_added
    ] == [
        (t.pad.pad_number, t.via_x, t.via_y, t.width, t.layer, tuple(t.waypoints or ()))
        for t in linear_result.traces_added
    ]
    assert indexed_result.already_connected == linear_result.already_connected
    assert indexed_result.via_in_pad_filtered == linear_result.via_in_pad_filtered
    assert indexed_result.micro_vias_placed == linear_result.micro_vias_placed
    assert [(p.pad_number, reason) for p, reason in indexed_result.pads_skipped] == [
        (p.pad_number, reason) for p, reason in linear_result.pads_skipped
    ]
    assert indexed_pcb.read_text() == linear_pcb.read_text()
