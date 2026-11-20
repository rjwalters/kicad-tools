"""Generate the KiCad-flipped golden fixture pair for the mirror strategy (#4560).

The mirror/layer-flip applicator (``StrategyApplicator._apply_mirror_component``)
pins its semantics *empirically against KiCad*, not by argument: this script
builds a small board with one asymmetric footprint on the front, saves it, then
flips that footprint **in KiCad itself** (``FOOTPRINT.Flip`` about its own
anchor, ``FLIP_DIRECTION_LEFT_RIGHT``) and saves the result.  The committed
pair:

* ``mirror_front.kicad_pcb``  -- U1 on ``F.Cu`` (rotation 30 -- deliberately
  not a fixed point of the candidate orientation transforms -- with mixed SMD +
  through-hole pads at distinct local offsets and non-symmetric shapes)
* ``mirror_back_lr.kicad_pcb`` -- the SAME board after KiCad's left/right flip

is asserted equal (absolute pad positions, layer lists, angles, footprint
layer/position) to what the applicator produces from the front board -- see
``tests/test_mirror_golden.py``.

Run manually with KiCad's bundled Python (KiCad is NOT needed at test time --
the outputs are committed):

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/\
Versions/Current/bin/python3 tests/fixtures/mirror_flip/generate_golden.py

Originally generated with KiCad 10.0.5; the custom pads 5 and 6 (Issue #9530)
were added and the pair regenerated with KiCad 10.0.6 (the CI-pinned image),
as was the layer-asymmetric padstack pad 7 (Issue #9752) and the one-sided
outer pads 8-11 (Issue #9773).
"""

from __future__ import annotations

import math
import sys
from pathlib import Path

import pcbnew  # type: ignore[import-not-found]
from pcbnew import (  # type: ignore[import-not-found]
    EDA_ANGLE,
    PAD_ATTRIB_PTH,
    PAD_ATTRIB_SMD,
    PAD_SHAPE_CIRCLE,
    PAD_SHAPE_RECT,
    PAD_SHAPE_ROUNDRECT,
    VECTOR2I,
)

OUT_DIR = Path(__file__).resolve().parent
FRONT = OUT_DIR / "mirror_front.kicad_pcb"
BACK = OUT_DIR / "mirror_back_lr.kicad_pcb"


def mm(v: float) -> int:
    return pcbnew.FromMM(v)


def deg(v: float) -> EDA_ANGLE:
    return EDA_ANGLE(v, pcbnew.DEGREES_T)


def add_pad(
    fp: pcbnew.FOOTPRINT,
    number: str,
    x_mm: float,
    y_mm: float,
    w_mm: float,
    h_mm: float,
    angle_deg: float,
    shape: int,
    attrib: int,
    drill_mm: float = 0.0,
) -> None:
    """Add a pad at footprint-local (x, y) while the footprint is unrotated."""
    pad = pcbnew.PAD(fp)
    pad.SetNumber(number)
    pad.SetAttribute(attrib)
    pad.SetShape(shape)
    pad.SetSize(VECTOR2I(mm(w_mm), mm(h_mm)))
    if attrib == PAD_ATTRIB_PTH:
        pad.SetDrillSize(VECTOR2I(mm(drill_mm), mm(drill_mm)))
        pad.SetLayerSet(pad.PTHMask())
    else:
        pad.SetLayerSet(pad.SMDMask())
    # Footprint is at rotation 0 when pads are added, so FP-relative == the
    # local offsets that end up in the file.
    pad.SetFPRelativePosition(VECTOR2I(mm(x_mm), mm(y_mm)))
    pad.SetOrientation(deg(angle_deg))
    fp.Add(pad)


def _poly_set(poly_mm: list[tuple[float, float]]) -> pcbnew.SHAPE_POLY_SET:
    poly = pcbnew.SHAPE_POLY_SET()
    poly.NewOutline()
    for px, py in poly_mm:
        poly.Append(mm(px), mm(py))
    return poly


def add_custom_pad(
    fp: pcbnew.FOOTPRINT,
    number: str,
    x_mm: float,
    y_mm: float,
    poly_mm: list[tuple[float, float]],
) -> None:
    """Add an SMD custom pad with a small anchor plus one asymmetric ``gr_poly``.

    The polygon is NOT symmetric about the pad's x axis (an L-shape reaching
    mostly toward +y), so a missing y-mirror of ``(primitives ...)`` changes the
    copper and the golden can tell (Issue #9530).  Coordinates are pad-local.
    """
    pad = pcbnew.PAD(fp)
    pad.SetNumber(number)
    pad.SetAttribute(PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_CUSTOM)
    pad.SetAnchorPadShape(pcbnew.F_Cu, PAD_SHAPE_CIRCLE)
    pad.SetSize(VECTOR2I(mm(0.4), mm(0.4)))
    pad.SetLayerSet(pad.SMDMask())
    pad.SetFPRelativePosition(VECTOR2I(mm(x_mm), mm(y_mm)))
    pad.AddPrimitivePoly(pcbnew.F_Cu, _poly_set(poly_mm), 0, True)
    fp.Add(pad)


def add_padstack_custom_pad(
    fp: pcbnew.FOOTPRINT,
    number: str,
    x_mm: float,
    y_mm: float,
    poly_mm: list[tuple[float, float]],
) -> None:
    """Add a through-hole custom pad with a ``front_inner_back`` padstack.

    The same asymmetric polygon is added on F.Cu, In1.Cu and B.Cu so each
    layer's nested ``(primitives ...)`` block must be y-mirrored (Issue #9530).
    Each layer block also carries a three-point ``gr_arc`` whose endpoint order
    KiCad's Flip reverses.  The padstack is layer-symmetric by construction;
    the front/back block *exchange* is covered separately by pad 7 (Issue
    #9752), because this golden cannot distinguish exchange from mirror-in-place.
    """
    pad = pcbnew.PAD(fp)
    pad.SetNumber(number)
    pad.SetAttribute(PAD_ATTRIB_PTH)
    pad.SetShape(pcbnew.PAD_SHAPE_CUSTOM)
    pad.SetSize(VECTOR2I(mm(0.6), mm(0.6)))
    pad.SetDrillSize(VECTOR2I(mm(0.3), mm(0.3)))
    pad.SetLayerSet(pad.PTHMask())
    pad.Padstack().SetMode(pcbnew.PADSTACK.MODE_FRONT_INNER_BACK)
    for layer in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.B_Cu):
        pad.SetShape(layer, pcbnew.PAD_SHAPE_CUSTOM)
        pad.SetSize(layer, VECTOR2I(mm(0.6), mm(0.6)))
        pad.SetAnchorPadShape(layer, PAD_SHAPE_CIRCLE)
    pad.SetFPRelativePosition(VECTOR2I(mm(x_mm), mm(y_mm)))
    for layer in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.B_Cu):
        pad.AddPrimitivePoly(layer, _poly_set(poly_mm), 0, True)
    # The arc is added on every layer with its mid point at the arc's angle
    # bisector, so KiCad's flip-time arc re-derivation (it normalizes ``mid``
    # to the bisector) round-trips our value exactly.
    for layer in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.B_Cu):
        arc = pcbnew.PCB_SHAPE(fp)
        arc.SetShape(pcbnew.SHAPE_T_ARC)
        arc.SetArcGeometry(
            VECTOR2I(mm(0.3), mm(0.2)), VECTOR2I(mm(0.2), mm(0.5)), VECTOR2I(mm(-0.1), mm(0.6))
        )
        arc.SetWidth(mm(0.1))
        arc.SetFilled(False)
        arc.SetLayer(layer)
        pad.AddPrimitiveShape(layer, arc)
        arc.thisown = False  # the pad now owns the shape; do not let Python free it
    fp.Add(pad)


def _bisector_arc_points(
    center_mm: tuple[float, float],
    radius_mm: float,
    start_deg: float,
    end_deg: float,
) -> tuple[tuple[float, float], tuple[float, float], tuple[float, float]]:
    """Three-point arc (start, mid, end) with *mid* at the angle bisector.

    KiCad's ``Flip`` re-derives a flipped arc's mid point as the geometric
    angle bisector, so arcs whose mid already sits there round-trip our
    y-negate-and-swap-endpoints transform exactly (Issue #9752).
    """
    cx, cy = center_mm

    def at(deg: float) -> tuple[float, float]:
        r = math.radians(deg)
        return (cx + radius_mm * math.cos(r), cy + radius_mm * math.sin(r))

    return at(start_deg), at((start_deg + end_deg) / 2), at(end_deg)


def add_asymmetric_padstack_custom_pad(
    fp: pcbnew.FOOTPRINT, number: str, x_mm: float, y_mm: float
) -> None:
    """Add a through-hole ``front_inner_back`` custom pad whose layers DIFFER.

    F.Cu carries a triangle, In1.Cu a quadrilateral reaching toward -x and
    B.Cu an L-shape, each asymmetric about the pad's x axis, plus a different
    three-point arc per layer.  Because the per-layer blocks are distinct,
    the committed golden pair distinguishes "each block mirrored in place"
    from "the F.Cu (top-level) and B.Cu (padstack) blocks also exchanged" --
    the behaviour KiCad 10.0.6's ``Flip`` really has (Issue #9752).
    """
    polys = {
        pcbnew.F_Cu: [(-0.2, -0.2), (0.8, 0.1), (0.1, 0.7)],  # triangle
        pcbnew.In1_Cu: [(-0.9, -0.1), (-0.1, -0.1), (-0.3, 0.8), (-0.8, 0.6)],  # quad toward -x
        pcbnew.B_Cu: [  # L-shape toward +y
            (-0.4, -0.3),
            (0.7, -0.3),
            (0.7, 0.2),
            (0.0, 0.2),
            (0.0, 0.9),
            (-0.4, 0.9),
        ],
    }
    arcs = {
        pcbnew.F_Cu: _bisector_arc_points((0.0, 0.1), 0.4, 0.0, 120.0),
        pcbnew.In1_Cu: _bisector_arc_points((-0.1, 0.0), 0.3, 30.0, 150.0),
        pcbnew.B_Cu: _bisector_arc_points((0.1, 0.2), 0.35, 210.0, 330.0),
    }
    pad = pcbnew.PAD(fp)
    pad.SetNumber(number)
    pad.SetAttribute(PAD_ATTRIB_PTH)
    pad.SetShape(pcbnew.PAD_SHAPE_CUSTOM)
    pad.SetSize(VECTOR2I(mm(0.6), mm(0.6)))
    pad.SetDrillSize(VECTOR2I(mm(0.3), mm(0.3)))
    pad.SetLayerSet(pad.PTHMask())
    pad.Padstack().SetMode(pcbnew.PADSTACK.MODE_FRONT_INNER_BACK)
    for layer in (pcbnew.F_Cu, pcbnew.In1_Cu, pcbnew.B_Cu):
        pad.SetShape(layer, pcbnew.PAD_SHAPE_CUSTOM)
        pad.SetSize(layer, VECTOR2I(mm(0.6), mm(0.6)))
        pad.SetAnchorPadShape(layer, PAD_SHAPE_CIRCLE)
    pad.SetFPRelativePosition(VECTOR2I(mm(x_mm), mm(y_mm)))
    for layer, poly_mm in polys.items():
        pad.AddPrimitivePoly(layer, _poly_set(poly_mm), 0, True)
    for layer, (a, m, b) in arcs.items():
        arc = pcbnew.PCB_SHAPE(fp)
        arc.SetShape(pcbnew.SHAPE_T_ARC)
        arc.SetArcGeometry(
            VECTOR2I(mm(a[0]), mm(a[1])), VECTOR2I(mm(m[0]), mm(m[1])), VECTOR2I(mm(b[0]), mm(b[1]))
        )
        arc.SetWidth(mm(0.1))
        arc.SetFilled(False)
        arc.SetLayer(layer)
        pad.AddPrimitiveShape(layer, arc)
        arc.thisown = False  # the pad now owns the shape; do not let Python free it
    fp.Add(pad)


_ONE_SIDED_TRI_F = [(-0.2, -0.2), (0.8, 0.1), (0.1, 0.7)]  # triangle, front
_ONE_SIDED_TRI_B = [(-0.4, -0.3), (0.7, 0.1), (0.0, 0.9)]  # distinct triangle, back
_ONE_SIDED_TRI_I = [(-0.9, -0.1), (-0.1, -0.1), (-0.3, 0.8)]  # distinct triangle, inner


def add_one_sided_padstack_custom_pad(
    fp: pcbnew.FOOTPRINT,
    number: str,
    x_mm: float,
    y_mm: float,
    front: str,
    back: str,
) -> pcbnew.PAD:
    """Add a through-hole ``front_inner_back`` custom pad with a ONE-SIDED outer.

    ``front``/``back`` pick each outer layer's definition: ``"poly"`` (custom
    shape carrying an asymmetric triangle), ``"empty"`` (custom shape with no
    primitive -- KiCad serializes a literal empty ``(primitives)`` block) or
    ``"circle"`` (a plain shape, so no primitives block exists at all, at a
    deliberately different size -- large enough that the 0.3 mm drill keeps
    the DRC cross-gate's minimum annular width once the circle lands on F.Cu
    after the flip).  Inner always carries a third distinct
    triangle.  Pads 8-11 cover the four one-sided combinations, pinning what
    KiCad 10.0.6's ``Flip`` does when only one side has a primitives block:
    the whole front/back shape definition (shape, size, custom options,
    primitives) exchanges, not just two existing blocks (Issue #9773).
    """
    pad = pcbnew.PAD(fp)
    pad.SetNumber(number)
    pad.SetAttribute(PAD_ATTRIB_PTH)
    pad.SetSize(VECTOR2I(mm(0.6), mm(0.6)))
    pad.SetDrillSize(VECTOR2I(mm(0.3), mm(0.3)))
    pad.SetLayerSet(pad.PTHMask())
    pad.Padstack().SetMode(pcbnew.PADSTACK.MODE_FRONT_INNER_BACK)
    spec = {
        pcbnew.F_Cu: front,
        pcbnew.In1_Cu: "poly",
        pcbnew.B_Cu: back,
    }
    tris = {
        pcbnew.F_Cu: _ONE_SIDED_TRI_F,
        pcbnew.In1_Cu: _ONE_SIDED_TRI_I,
        pcbnew.B_Cu: _ONE_SIDED_TRI_B,
    }
    for layer, kind in spec.items():
        if kind == "circle":
            pad.SetShape(layer, pcbnew.PAD_SHAPE_CIRCLE)
            pad.SetSize(layer, VECTOR2I(mm(0.55), mm(0.55)))
        else:
            pad.SetShape(layer, pcbnew.PAD_SHAPE_CUSTOM)
            pad.SetSize(layer, VECTOR2I(mm(0.6), mm(0.6)))
            pad.SetAnchorPadShape(layer, PAD_SHAPE_CIRCLE)
            if kind == "poly":
                pad.AddPrimitivePoly(layer, _poly_set(tris[layer]), 0, True)
    pad.SetFPRelativePosition(VECTOR2I(mm(x_mm), mm(y_mm)))
    fp.Add(pad)
    return pad


def add_silk_line(fp: pcbnew.FOOTPRINT, x1: float, y1: float, x2: float, y2: float) -> None:
    line = pcbnew.PCB_SHAPE(fp)
    line.SetShape(pcbnew.SHAPE_T_SEGMENT)
    line.SetStart(VECTOR2I(mm(x1), mm(y1)))
    line.SetEnd(VECTOR2I(mm(x2), mm(y2)))
    line.SetLayer(pcbnew.F_SilkS)
    line.SetWidth(mm(0.12))
    fp.Add(line)
    line.Move(fp.GetPosition())


def main() -> int:
    print("pcbnew build:", pcbnew.GetBuildVersion())
    board = pcbnew.NewBoard(str(OUT_DIR / "_scratch.kicad_pcb"))

    # Board outline so kicad-cli DRC has an edge to work with.
    outline = pcbnew.PCB_SHAPE(board)
    outline.SetShape(pcbnew.SHAPE_T_RECTANGLE)
    outline.SetStart(VECTOR2I(mm(90), mm(90)))
    outline.SetEnd(VECTOR2I(mm(110), mm(110)))
    outline.SetLayer(pcbnew.Edge_Cuts)
    outline.SetWidth(mm(0.1))
    board.Add(outline)

    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("U1")
    fp.SetValue("MIRROR_GOLDEN")
    board.Add(fp)
    fp.SetPosition(VECTOR2I(mm(100), mm(100)))

    # Deliberately chirality-revealing pad set: distinct local offsets, a
    # non-square roundrect at a non-axis angle, and one through-hole pad.
    add_pad(fp, "1", -1.6, -1.0, 1.2, 0.6, 0.0, PAD_SHAPE_ROUNDRECT, PAD_ATTRIB_SMD)
    add_pad(fp, "2", -1.6, 1.0, 1.2, 0.6, 45.0, PAD_SHAPE_ROUNDRECT, PAD_ATTRIB_SMD)
    add_pad(fp, "3", 0.4, 0.0, 1.0, 0.5, 20.0, PAD_SHAPE_RECT, PAD_ATTRIB_SMD)
    add_pad(fp, "4", 2.0, -0.8, 1.1, 1.1, 0.0, PAD_SHAPE_CIRCLE, PAD_ATTRIB_PTH, drill_mm=0.6)

    # Asymmetric custom pad (L-shaped ``gr_poly`` reaching toward +y) -- the
    # applicator must y-mirror its (primitives ...) like KiCad's Flip (#9530).
    add_custom_pad(
        fp,
        "5",
        0.4,
        1.6,
        [(-0.3, -0.2), (0.9, -0.2), (0.9, 0.3), (0.2, 0.3), (0.2, 1.1), (-0.3, 1.1)],
    )

    # Layer-specific padstack custom pad: nested per-layer primitives plus a
    # three-point arc (Issue #9530 review).
    add_padstack_custom_pad(
        fp,
        "6",
        -0.3,
        -2.8,
        [(-0.3, -0.2), (0.9, -0.2), (0.9, 0.3), (0.2, 0.3), (0.2, 1.1), (-0.3, 1.1)],
    )

    # Layer-ASYMMETRIC padstack custom pad: F.Cu/In/B.Cu primitives differ, so
    # the golden pins the front/back block exchange of KiCad's Flip (#9752).
    add_asymmetric_padstack_custom_pad(fp, "7", 2.0, 2.6)

    # One-sided outer padstack pads: only one outer carries primitives (or a
    # custom shape at all), pinning the whole-definition exchange (#9773).
    add_one_sided_padstack_custom_pad(fp, "8", -2.0, 4.6, "poly", "empty")
    add_one_sided_padstack_custom_pad(fp, "9", -0.5, 4.6, "empty", "poly")
    add_one_sided_padstack_custom_pad(fp, "10", 1.0, 4.6, "poly", "circle")
    add_one_sided_padstack_custom_pad(fp, "11", 2.5, 4.6, "circle", "poly")

    rounded = add_one_sided_padstack_custom_pad(fp, "12", -3, 7, "poly", "circle")
    rounded.SetShape(pcbnew.B_Cu, PAD_SHAPE_ROUNDRECT)
    rounded.SetSize(pcbnew.B_Cu, VECTOR2I(mm(1.0), mm(0.9)))
    rounded.SetRoundRectRadiusRatio(pcbnew.B_Cu, 0.4)
    rounded.SetOffset(pcbnew.B_Cu, VECTOR2I(mm(0.10), mm(0.05)))
    rounded.SetCustomShapeInZoneOpt(pcbnew.CUSTOM_SHAPE_ZONE_MODE_CONVEXHULL)
    chamfered = add_one_sided_padstack_custom_pad(fp, "13", 0, 7, "poly", "circle")
    chamfered.SetShape(pcbnew.B_Cu, pcbnew.PAD_SHAPE_CHAMFERED_RECT)
    chamfered.SetChamferRectRatio(pcbnew.B_Cu, 0.2)
    chamfered.SetChamferPositions(pcbnew.B_Cu, 1)  # top left
    trapezoid = add_one_sided_padstack_custom_pad(fp, "14", 3, 7, "circle", "poly")
    trapezoid.SetShape(pcbnew.F_Cu, pcbnew.PAD_SHAPE_TRAPEZOID)
    trapezoid.SetSize(pcbnew.F_Cu, VECTOR2I(mm(0.9), mm(0.8)))
    trapezoid.SetDelta(pcbnew.F_Cu, VECTOR2I(mm(0.08), mm(0.03)))

    # An asymmetric silk mark (pin-1 tick) so the cosmetic side-flip is
    # observable in the golden too.
    add_silk_line(fp, -2.6, -1.6, -2.0, -1.6)

    # A rotated footprint exercises the orientation sign handling.  30 deg is
    # deliberately NOT a fixed point of either candidate transform
    # (``-theta`` vs ``180 - theta``), so the golden discriminates them.
    fp.SetOrientation(deg(30.0))

    pcbnew.SaveBoard(str(FRONT), board)

    # KiCad's own flip, about the footprint's own anchor, left/right.
    fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
    pcbnew.SaveBoard(str(BACK), board)

    # pcbnew drops project sidecars (.kicad_pro/.kicad_prl) beside every
    # SaveBoard target; only the two .kicad_pcb goldens are committed.
    for stray in OUT_DIR.glob("_scratch.*"):
        stray.unlink()
    for board_path in (FRONT, BACK):
        for suffix in (".kicad_pro", ".kicad_prl"):
            sidecar = board_path.with_suffix(suffix)
            if sidecar.exists():
                sidecar.unlink()
    print(f"wrote {FRONT.name} and {BACK.name} (KiCad {pcbnew.GetBuildVersion()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
