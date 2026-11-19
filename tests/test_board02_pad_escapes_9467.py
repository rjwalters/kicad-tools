"""Board 02 recipe: pad escapes keep their bends out of the mask-opening zone (#9467).

``mask_to_copper`` (#8858) grades copper that stays under the solder mask beside
a pad opening.  The seed-42 route leaves dog-legs right at some pads (a stub, a
short 45-degree step, then the run); ``finalize_routing._square_pad_escapes``
replaces each with an axis-aligned exit plus a 90- or 45-degree finish that
bends at least ``_ESCAPE_BEND_MM`` from the pad, and ``_slide_ring_vias`` moves
a same-net via whose ring overlaps its own pad.  The functions are pure
geometry on a loaded ``PCB``, so these tests use a two-footprint board and need
no KiCad.
"""

import math
import runpy
import tempfile
from pathlib import Path

import pytest

from kicad_tools.schema.pcb import PCB

RECIPE = runpy.run_path(
    str(Path(__file__).resolve().parents[1] / "boards/02-charlieplex-led/finalize_routing.py")
)
square_pad_escapes = RECIPE["_square_pad_escapes"]
slide_ring_vias = RECIPE["_slide_ring_vias"]
BEND_MM = RECIPE["_ESCAPE_BEND_MM"]
bbox_distance = RECIPE["_bbox_distance"]

# R1.1 (net A) is a 1.0 x 1.4 mm roundrect at (20, 20): x 19.5..20.5, y 19.3..20.7.
PAD_BBOX = (19.5, 19.3, 20.5, 20.7)
PCB_TEXT = """(kicad_pcb (version 20240108) (generator "pcbnew") (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers (0 "F.Cu" signal) (31 "B.Cu" signal) (37 "F.SilkS" user "F.Silkscreen")
          (39 "F.Mask" user) (44 "Edge.Cuts" user))
  (setup (pad_to_mask_clearance 0))
  (net 0 "") (net 1 "A") (net 2 "B")
  (footprint "R" (layer "F.Cu") (at 20 20) (uuid "11111111-1111-1111-1111-111111111111")
    (property "Reference" "R1" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at 0 0) (size 1 1.4) (layers "F.Cu" "F.Mask" "F.Paste")
         (roundrect_rratio 0.25) (net 1 "A")))
  (footprint "R" (layer "F.Cu") (at 40 20) (uuid "22222222-2222-2222-2222-222222222222")
    (property "Reference" "R2" (at 0 0) (layer "F.SilkS"))
    (pad "1" smd roundrect (at 0 0) (size 1 1.4) (layers "F.Cu" "F.Mask" "F.Paste")
         (roundrect_rratio 0.25) (net 2 "B")))
{items}
)
"""


def _segment(n, start, end, net=1, layer="F.Cu"):
    return (
        f"(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) (width 0.2) "
        f'(layer "{layer}") (net {net}) (uuid "aaaaaaaa-0000-0000-0000-{n:012d}"))'
    )


def _board(*items):
    path = Path(tempfile.mkdtemp()) / "board.kicad_pcb"
    path.write_text(PCB_TEXT.format(items="\n".join(items)))
    return PCB.load(str(path))


def _chain(pcb, net_name, start):
    """Vertices of the same-net track chain that begins at ``start``."""
    unused = [s for s in pcb.segments if s.net_name == net_name]
    points = [start]
    while True:
        seg = next(
            (s for s in unused if any(math.dist(e, points[-1]) < 1e-6 for e in (s.start, s.end))),
            None,
        )
        if seg is None:
            return points
        unused.remove(seg)
        points.append(seg.end if math.dist(seg.start, points[-1]) < 1e-6 else seg.start)


DOGLEG = (
    _segment(1, (20, 20), (20.5, 19.5)),
    _segment(2, (20.5, 19.5), (20.5, 19.2)),
    _segment(3, (20.5, 19.2), (21, 18.7)),
    _segment(4, (21, 18.7), (30, 18.7)),
)


def test_dogleg_escape_is_replaced_by_an_axis_aligned_exit():
    pcb = _board(*DOGLEG)
    assert square_pad_escapes(pcb) == 1
    route = _chain(pcb, "A", (20.0, 20.0))
    # Pad centre -> vertical exit -> bend -> the original run, nothing else.
    assert route[0] == (20.0, 20.0)
    assert route[-1] == (30.0, 18.7)
    assert route[1][0] == 20.0, "first leg leaves the pad centre along an axis"
    for bend in route[1:-1]:
        assert bbox_distance(bend, PAD_BBOX) >= BEND_MM - 1e-6, bend
    assert all(s.net_name == "A" and s.layer == "F.Cu" for s in pcb.segments)


def test_squaring_is_stable_on_rerun():
    pcb = _board(*DOGLEG)
    square_pad_escapes(pcb)
    before = sorted((s.start, s.end) for s in pcb.segments)
    assert square_pad_escapes(pcb) == 0
    assert sorted((s.start, s.end) for s in pcb.segments) == before


def test_clean_straight_exit_is_left_alone():
    pcb = _board(_segment(1, (20, 20), (20, 15)), _segment(2, (20, 15), (30, 15)))
    before = sorted((s.start, s.end) for s in pcb.segments)
    assert square_pad_escapes(pcb) == 0
    assert sorted((s.start, s.end) for s in pcb.segments) == before


def test_other_net_copper_in_the_way_keeps_the_original_route():
    blockers = (
        # Across the vertical exit (x = 20) and across the horizontal-first L (x = 21).
        _segment(11, (20.2, 18.0), (20.2, 19.0), net=2),
        _segment(12, (21.2, 19.0), (21.2, 20.2), net=2),
    )
    pcb = _board(*DOGLEG, *blockers)
    before = sorted((s.start, s.end) for s in pcb.segments)
    assert square_pad_escapes(pcb) == 0
    assert sorted((s.start, s.end) for s in pcb.segments) == before


def test_via_ring_overlapping_its_own_pad_slides_clear_and_takes_its_track():
    via = (
        '(via (at 20.6 20) (size 0.8) (drill 0.3) (layers "F.Cu" "B.Cu") (net 1) '
        '(uuid "bbbbbbbb-0000-0000-0000-000000000001"))'
    )
    pcb = _board(via, _segment(1, (20.6, 20), (25, 20)))
    assert slide_ring_vias(pcb) == 1
    (moved,) = pcb.vias
    assert bbox_distance(moved.position, PAD_BBOX) - moved.size / 2 >= 0.3 - 1e-6
    (track,) = pcb.segments
    assert math.dist(track.start, moved.position) < 1e-6, "the track follows its via"
    assert track.end == (25.0, 20.0)
    assert slide_ring_vias(pcb) == 0


def test_via_of_another_net_is_not_moved():
    via = (
        '(via (at 20.6 20) (size 0.8) (drill 0.3) (layers "F.Cu" "B.Cu") (net 2) '
        '(uuid "bbbbbbbb-0000-0000-0000-000000000001"))'
    )
    pcb = _board(via)
    assert slide_ring_vias(pcb) == 0
    assert pcb.vias[0].position == pytest.approx((20.6, 20.0))
