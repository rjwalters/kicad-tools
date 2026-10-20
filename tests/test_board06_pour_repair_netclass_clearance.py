"""Board06's pour repair must hold its copper off foreign copper at the net-class clearance.

Issue #6748, the board 06 port of board 07's #6736 (PR #6745).  Board 06's
step-7 ``_repair_pour_connectivity`` validated every offset via, pad stub and
bridge leg against a FLAT 0.15 mm (``CLEAR``) instead of the per-pair class
clearance ``max(0.15, class(P), class(N))`` -- 0.2 mm for the board's Power
class nets (``+1V2``, ``GND``, ...), the value KiCad enforces -- and measured
foreign pads by their circumscribed circle.  Board 06 shipped no kicad-cli
``clearance`` error from it yet, so the defect was latent.

The fixtures are constructed directly: one stranded ``+1V2`` SMD pad, a
``+1V2`` In1.Cu fill strip (anchored by a through-hole ``+1V2`` pad so it is
the primary copper) that only the pad's 45-degree via candidates can reach,
and foreign ``GND`` copper (Power class, like ``+1V2``) placed so that the
candidate the stage prefers clears it by between 0.15 and 0.2 mm:

* a ``GND`` pad whose corner faces the diagonal stub (rect and roundrect);
* a ``GND`` F.Cu segment beside the preferred via whose BOUNDING BOX is more
  than 0.15 mm from the via's, so it guards the ``seg_tree`` prefilter
  margin (Issue #5240): a margin left at 0.15 mm would skip the segment
  before the per-pair test ever saw it.

The geometry sits inside board 06's outline (x 99..197.5, y 48..127), which
the repair clamps its candidates to.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from pathlib import Path

import pytest

from kicad_tools.router.clearance_kernel import KSegment, KVia, copper_gap, make_pad

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"

#: The Power class clearance of ``+1V2`` and ``GND`` (``NET_CLASS_POWER``).
POWER_CLEARANCE = 0.2
#: The stage's old flat clearance -- the gap the violating stub DOES clear.
OLD_FLAT_CLEARANCE = 0.15

STRANDED = (140.0, 90.0)
#: ``(shape, rratio, centre)`` of the foreign GND pad, per variant.  Both sit
#: beside the stranded pad's 45-degree stub, with their nearest corner facing
#: it: the circumscribed circle clears the stub by >= 0.15 mm, the true copper
#: by < 0.2 mm.  ``rect`` is Board 07's own U1 land; ``roundrect`` sits closer
#: by the corner relief its 0.075 mm radius buys.
GND_PADS = {
    "rect": ("rect", 0.25, (140.825, 90.165)),
    "roundrect": ("roundrect", 0.25, (140.8075, 90.1825)),
}
GND_SIZE = 0.3


def _load_module(name: str, path: Path):
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:  # pragma: no cover - import guard
        raise ImportError(f"Cannot load module {name!r} from {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def generate_design_mod():
    """Load ``boards/06-diffpair-test/generate_design.py`` as a module."""
    gp = _load_module("board_06_generate_pcb_6748", BOARD_DIR / "generate_pcb.py")
    sys.modules["generate_pcb"] = gp
    gs = _load_module("board_06_generate_schematic_6748", BOARD_DIR / "generate_schematic.py")
    sys.modules["generate_schematic"] = gs
    return _load_module("board_06_generate_design_6748", BOARD_DIR / "generate_design.py")


def _fixture(shape: str, rratio: float, centre: tuple[float, float], extra: str = "") -> str:
    gx, gy = centre[0] - STRANDED[0], centre[1] - STRANDED[1]
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "+1V2")
  (net 2 "GND")
  (footprint "Test:Lands"
    (layer "F.Cu")
    (uuid "fp-u1")
    (at {STRANDED[0]} {STRANDED[1]})
    (property "Reference" "U1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-u1"))
    (pad "36" smd circle (at 0 0) (size 0.25 0.25) (layers "F.Cu" "F.Mask") \
(net 1 "+1V2"))
    (pad "24" smd {shape} (at {gx:.4f} {gy:.4f}) (size {GND_SIZE} {GND_SIZE}) \
(layers "F.Cu" "F.Mask") (roundrect_rratio {rratio}) (net 2 "GND"))
  )
  (footprint "Capacitor_THT:CP_Radial"
    (layer "F.Cu")
    (uuid "fp-c1")
    (at 150 100)
    (property "Reference" "C1" (at 0 0 0) (layer "F.SilkS") (uuid "ref-c1"))
    (pad "1" thru_hole circle (at 0 0) (size 1.0 1.0) (drill 0.5) \
(layers "*.Cu" "*.Mask") (net 1 "+1V2"))
  )
  (zone (net 1) (net_name "+1V2") (layer "In1.Cu")
    (uuid "zone-strip")
    (fill yes)
    (polygon
      (pts (xy 140.96 90.99) (xy 140.99 90.96) (xy 150.03 100) (xy 150 100.03))
    )
    (filled_polygon
      (layer "In1.Cu")
      (pts (xy 140.96 90.99) (xy 140.99 90.96) (xy 150.03 100) (xy 150 100.03))
    )
  )
{extra})
"""


def _gnd_pad(variant: str):
    shape, rratio, (cx, cy) = GND_PADS[variant]
    return make_pad(shape, GND_SIZE, GND_SIZE, rratio, 0.0, cx, cy)


def _new_copper(before: str, after: str) -> tuple[list[KSegment], list[KVia]]:
    added = [line for line in after.splitlines() if line not in before.splitlines()]
    num = r"(-?[\d.]+)"
    segments = [
        KSegment(float(m[1]), float(m[2]), float(m[3]), float(m[4]), float(m[5]))
        for line in added
        if (m := re.search(rf"\(start {num} {num}\) \(end {num} {num}\) \(width {num}\)", line))
        and '"F.Cu"' in line
    ]
    vias = [
        KVia(float(m[1]), float(m[2]), float(m[3]))
        for line in added
        if (m := re.search(rf"\(via \(at {num} {num}\) \(size {num}\)", line))
    ]
    return segments, vias


@pytest.mark.parametrize("variant", sorted(GND_PADS))
def test_the_fixture_reproduces_the_issue_geometry(variant):
    """Control: the preferred stub clears the old model but not the Power rule."""
    pad = _gnd_pad(variant)
    stub = KSegment(*STRANDED, 140.99, 90.99, 0.15)
    # The via at the end of the stub is legal on its own ...
    assert copper_gap(KVia(140.99, 90.99, 0.45), pad) >= POWER_CLEARANCE
    # ... but its diagonal stub passes the GND pad's corner too close.
    assert OLD_FLAT_CLEARANCE <= copper_gap(stub, pad) < POWER_CLEARANCE


@pytest.mark.parametrize("variant", sorted(GND_PADS))
def test_repair_copper_keeps_the_power_clearance_from_a_foreign_pad(
    generate_design_mod, tmp_path, variant, capsys
):
    board = tmp_path / f"netclass_clearance_{variant}.kicad_pcb"
    board.write_text(_fixture(*GND_PADS[variant]))
    before = board.read_text()

    vias, _bridges = generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    capsys.readouterr()

    segments, new_vias = _new_copper(before, board.read_text())
    # The stranded pad still gets its repair: the fix relocates, never drops.
    assert vias >= 1 and len(new_vias) == vias
    assert segments, "the stage emitted no F.Cu stub for the stranded pad"

    pad = _gnd_pad(variant)
    for seg in segments:
        gap = copper_gap(seg, pad)
        assert gap >= POWER_CLEARANCE - 1e-4, (
            f"repair leg ({seg.x1}, {seg.y1}) -> ({seg.x2}, {seg.y2}) is {gap:.4f} mm "
            f"from GND U1.24 (Power clearance {POWER_CLEARANCE} mm)"
        )
    for via in new_vias:
        assert copper_gap(via, pad) >= POWER_CLEARANCE - 1e-4


def test_a_legal_diagonal_stub_is_still_taken(generate_design_mod, tmp_path, capsys):
    """The fix must not drop legal sites: with the GND pad moved off, the
    preferred 45-degree candidate onto the primary strip is placed as before."""
    board = tmp_path / "legal.kicad_pcb"
    board.write_text(_fixture("rect", 0.25, (141.2, 89.8)))
    before = board.read_text()

    assert generate_design_mod._repair_pour_connectivity(board, ["+1V2"]) == (1, 0)
    assert "UNREPAIRED" not in capsys.readouterr().out
    segments, new_vias = _new_copper(before, board.read_text())
    assert [(v.x, v.y) for v in new_vias] == [(140.99, 90.99)]
    assert [(s.x1, s.y1, s.x2, s.y2) for s in segments] == [(140.0, 90.0, 140.99, 90.99)]


#: A ``GND`` F.Cu segment just below-right of the preferred via at
#: (140.99, 90.99): 0.1876 mm from the via's copper (inside the 0.2 mm Power
#: rule) but 0.175 mm from it bounding box to bounding box, so a prefilter
#: margin of 0.15 mm drops it unseen.  The diagonal stub itself clears it by
#: 0.26 mm, and the next diagonal candidate (offset 1.8 mm) is legal.
GND_SEGMENT = (141.1, 90.515, 141.6, 90.515, 0.15)
#: Where the repair lands once the 1.4 mm candidate is (correctly) refused.
RELOCATED_VIA = (141.273, 91.273)


def _gnd_segment_line() -> str:
    x1, y1, x2, y2, w = GND_SEGMENT
    return (
        f'  (segment (start {x1} {y1}) (end {x2} {y2}) (width {w}) (layer "F.Cu") '
        '(net 2) (uuid "seg-gnd"))\n'
    )


def test_the_segment_fixture_hides_inside_the_old_prefilter_margin():
    """Control: copper gap < 0.2 mm, bounding-box gap > 0.15 mm."""
    seg = KSegment(*GND_SEGMENT)
    via = KVia(140.99, 90.99, 0.45)
    assert OLD_FLAT_CLEARANCE < copper_gap(via, seg) < POWER_CLEARANCE
    via_bottom = 90.99 - 0.225
    seg_top = GND_SEGMENT[1] + GND_SEGMENT[4] / 2
    assert via_bottom - seg_top > OLD_FLAT_CLEARANCE
    stub = KSegment(*STRANDED, 140.99, 90.99, 0.15)
    assert copper_gap(stub, seg) >= POWER_CLEARANCE


def test_repair_copper_keeps_the_power_clearance_from_a_foreign_segment(
    generate_design_mod, tmp_path, capsys
):
    board = tmp_path / "netclass_clearance_segment.kicad_pcb"
    # GND pad parked well away; the segment is the only nearby obstacle.
    board.write_text(_fixture("rect", 0.25, (138.0, 88.0), extra=_gnd_segment_line()))
    before = board.read_text()

    vias, _bridges = generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    assert "UNREPAIRED" not in capsys.readouterr().out

    segments, new_vias = _new_copper(before, board.read_text())
    # Relocated, not dropped: the next diagonal candidate onto the strip.
    assert vias == 1 and [(v.x, v.y) for v in new_vias] == [RELOCATED_VIA]
    gnd = KSegment(*GND_SEGMENT)
    for via in new_vias:
        assert copper_gap(via, gnd) >= POWER_CLEARANCE - 1e-4
    for seg in segments:
        assert copper_gap(seg, gnd) >= POWER_CLEARANCE - 1e-4


# --- the copper that SHIPS: step 12 quantizes off-angle chords -------------
#
# Board 06's repair emits straight chords and step 12 (``quantize_pcb_file``)
# later replaces every off-45-degree one with a two-leg dogleg, choosing the
# variant against the project's clearance floor (0.1016 mm), not the pair
# clearance.  The first board 06 regen with the per-pair fix shipped a +1V2
# stub as a dogleg 0.149 mm from GND U3.35 that way.  So an off-angle chord
# is accepted only when the straight chord AND both dogleg variants clear.

#: Two ``+1V2`` In1.Cu fills whose nearest-point bridge is a ~11-degree chord.
_BRIDGE_FIXTURE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (1 "In1.Cu" signal)
    (2 "In2.Cu" signal)
    (31 "B.Cu" signal)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "+1V2")
  (net 2 "SIG")
{blocker}
  (zone (net 1) (net_name "+1V2") (layer "In1.Cu")
    (uuid "zone-primary")
    (fill yes)
    (polygon
      (pts (xy 130 80) (xy 140 80) (xy 141 85) (xy 140 90) (xy 130 90))
    )
    (filled_polygon
      (layer "In1.Cu")
      (pts (xy 130 80) (xy 140 80) (xy 141 85) (xy 140 90) (xy 130 90))
    )
  )
  (zone (net 1) (net_name "+1V2") (layer "In1.Cu")
    (uuid "zone-island")
    (fill yes)
    (polygon
      (pts (xy 144 86.5) (xy 145 87.5) (xy 155 87.5) (xy 155 95) (xy 145 95) (xy 144 87.5))
    )
    (filled_polygon
      (layer "In1.Cu")
      (pts (xy 144 86.5) (xy 145 87.5) (xy 155 87.5) (xy 155 95) (xy 145 95) (xy 144 87.5))
    )
  )
)
"""
#: The bridge sub-stage B proposes (nearest points + 0.35 mm overshoot).
OFF_ANGLE_BRIDGE = ((144.343, 86.569), (140.484, 85.797))
#: A foreign via 0.63 mm from that straight chord but 0.175 mm from the
#: horizontal leg of its default (diagonal-first) dogleg.
DOGLEG_BLOCKER = (142.85, 85.297)


def _sig_via(x: float, y: float) -> str:
    return (
        f'  (via (at {x} {y}) (size 0.45) (drill 0.2) (layers "F.Cu" "B.Cu") '
        '(net 2) (uuid "via-sig"))'
    )


def _shipped_legs(seg: KSegment) -> list[list[KSegment]]:
    """Every leg set step 12 may ship for ``seg`` (straight if 45-aligned)."""
    from kicad_tools.router.quantize import dogleg_points, is_45_aligned

    if is_45_aligned(seg.x2 - seg.x1, seg.y2 - seg.y1):
        return [[seg]]
    variants = []
    for axis_first in (False, True):
        verts = dogleg_points(seg.x1, seg.y1, seg.x2, seg.y2, axis_first=axis_first)
        variants.append(
            [
                KSegment(a[0], a[1], b[0], b[1], seg.width)
                for a, b in zip(verts[:-1], verts[1:], strict=True)
            ]
        )
    return variants


def _bridge_segments(before: str, after: str) -> list[KSegment]:
    added = [line for line in after.splitlines() if line not in before.splitlines()]
    num = r"(-?[\d.]+)"
    return [
        KSegment(float(m[1]), float(m[2]), float(m[3]), float(m[4]), float(m[5]))
        for line in added
        if (m := re.search(rf"\(start {num} {num}\) \(end {num} {num}\) \(width {num}\)", line))
    ]


def test_the_bridge_fixture_grazes_only_through_the_quantized_dogleg():
    """Control: the straight chord clears; its default dogleg does not."""
    straight = KSegment(*OFF_ANGLE_BRIDGE[0], *OFF_ANGLE_BRIDGE[1], 0.2)
    via = KVia(*DOGLEG_BLOCKER, 0.45)
    assert copper_gap(straight, via) >= POWER_CLEARANCE
    diag_first, axis_first = _shipped_legs(straight)
    assert OLD_FLAT_CLEARANCE < min(copper_gap(leg, via) for leg in diag_first) < POWER_CLEARANCE
    assert min(copper_gap(leg, via) for leg in axis_first) >= POWER_CLEARANCE


def test_no_quantized_variant_of_a_repair_chord_grazes_foreign_copper(
    generate_design_mod, tmp_path, capsys
):
    board = tmp_path / "dogleg_graze.kicad_pcb"
    board.write_text(_BRIDGE_FIXTURE.format(blocker=_sig_via(*DOGLEG_BLOCKER)))
    before = board.read_text()

    generate_design_mod._repair_pour_connectivity(board, ["+1V2"])
    capsys.readouterr()

    via = KVia(*DOGLEG_BLOCKER, 0.45)
    for seg in _bridge_segments(before, board.read_text()):
        for legs in _shipped_legs(seg):
            gap = min(copper_gap(leg, via) for leg in legs)
            assert gap >= POWER_CLEARANCE - 1e-4, (
                f"repair chord ({seg.x1}, {seg.y1}) -> ({seg.x2}, {seg.y2}) can ship as a "
                f"dogleg {gap:.4f} mm from the SIG via (Power clearance {POWER_CLEARANCE} mm)"
            )


def test_an_off_angle_bridge_whose_doglegs_all_clear_is_still_taken(
    generate_design_mod, tmp_path, capsys
):
    """The guard must not drop legal bridges: with the via moved clear of
    both dogleg variants, the same off-angle chord is emitted as before."""
    board = tmp_path / "dogleg_clear.kicad_pcb"
    board.write_text(_BRIDGE_FIXTURE.format(blocker=_sig_via(142.85, 84.6)))
    before = board.read_text()

    assert generate_design_mod._repair_pour_connectivity(board, ["+1V2"]) == (0, 1)
    assert "UNREPAIRED" not in capsys.readouterr().out
    segments = _bridge_segments(before, board.read_text())
    assert [(s.x1, s.y1, s.x2, s.y2) for s in segments] == [
        (*OFF_ANGLE_BRIDGE[0], *OFF_ANGLE_BRIDGE[1])
    ]
