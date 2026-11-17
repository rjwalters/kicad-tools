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

Generated with KiCad 10.0.5.
"""

from __future__ import annotations

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
