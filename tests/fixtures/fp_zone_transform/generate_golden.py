"""Generate KiCad-transformed goldens for footprint-embedded zones and texts.

KiCad writes the zones inside a ``(footprint ...)`` block -- an RF module's
antenna keepout, a footprint copper pour -- in **sheet-absolute** coordinates
with the footprint's rotation and flip already applied.  Moving, rotating or
flipping the footprint therefore has to rewrite those polygons too.  This
script pins what "the same transform KiCad uses" means by letting pcbnew do
each move itself:

* ``base.kicad_pcb``       -- ``AE1`` on ``F.Cu`` at (120, 109), rotation 0,
  carrying an asymmetric keepout on ``F.Cu`` and a filled copper zone;
* ``move.kicad_pcb``       -- ``SetPosition(125, 120)``;
* ``rot30.kicad_pcb``      -- ``SetOrientation(30)``;
* ``move_rot90.kicad_pcb`` -- ``SetPosition(125, 120)`` + ``SetOrientation(90)``
  (the issue's repro);
* ``flip_lr.kicad_pcb``    -- ``SetOrientation(30)`` then
  ``Flip(anchor, FLIP_DIRECTION_LEFT_RIGHT)`` (what ``kct pcb modify flip`` /
  the mirror strategy do);
* ``back_rot90.kicad_pcb`` -- ``flip_lr`` then ``SetOrientation(90)`` (rotating
  a footprint that is already on the back);
* ``rotate_tool30.kicad_pcb`` / ``rotate_tool200.kicad_pcb`` --
  ``Rotate(anchor, 30)`` / ``Rotate(anchor, 200)``, the interactive rotate (``kct pcb modify rotate``, the recovery rotate strategy), which,
  unlike ``SetOrientation``, also re-uprights "keep upright" texts.

``AE1`` also carries texts (Issue #6126): KiCad stores a ``property`` /
``fp_text`` ``(at x y ANGLE)`` angle **board-absolute** like a pad angle, while
the position stays footprint-local.  The user texts mix keep-upright and
``(unlocked yes)`` texts, upright and upside-down angles, and left/right/
top/bottom justification, which the keep-upright step swaps.

``tests/test_footprint_zone_transform.py`` replays each operation through kct
from ``base.kicad_pcb`` and asserts the zone geometry equals these files.
KiCad is NOT required at test time; the outputs are committed.

Run manually with KiCad's bundled Python:

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/\
Versions/Current/bin/python3 tests/fixtures/fp_zone_transform/generate_golden.py

Generated with KiCad 10.0.1.
"""

from __future__ import annotations

import sys
from collections.abc import Callable
from pathlib import Path

import pcbnew  # type: ignore[import-not-found]
from pcbnew import EDA_ANGLE, VECTOR2I  # type: ignore[import-not-found]

OUT_DIR = Path(__file__).resolve().parent
BASE = OUT_DIR / "base.kicad_pcb"


def mm(v: float) -> int:
    return pcbnew.FromMM(v)


def deg(v: float) -> EDA_ANGLE:
    return EDA_ANGLE(v, pcbnew.DEGREES_T)


def add_zone(fp: pcbnew.FOOTPRINT, pts: list[tuple[float, float]], keepout: bool) -> pcbnew.ZONE:
    zone = pcbnew.ZONE(fp)
    zone.SetLayer(pcbnew.F_Cu)
    if keepout:
        zone.SetIsRuleArea(True)
        zone.SetDoNotAllowTracks(True)
        zone.SetDoNotAllowVias(True)
        zone.SetDoNotAllowZoneFills(True)
        zone.SetDoNotAllowPads(False)
        zone.SetDoNotAllowFootprints(False)
        zone.SetZoneName("ANT_KEEPOUT")
    else:
        zone.SetZoneName("FP_POUR")
        zone.SetMinThickness(mm(0.25))
    outline = zone.Outline()
    outline.NewOutline()
    for x, y in pts:
        outline.Append(mm(x), mm(y))
    fp.Add(zone)
    return zone


def build_base() -> pcbnew.BOARD:
    board = pcbnew.NewBoard(str(OUT_DIR / "_scratch.kicad_pcb"))

    # Board outline, so kct's board-origin offset is exercised (the outline's
    # top-left corner is not the sheet origin).
    outline = pcbnew.PCB_SHAPE(board)
    outline.SetShape(pcbnew.SHAPE_T_RECTANGLE)
    outline.SetStart(VECTOR2I(mm(100), mm(95)))
    outline.SetEnd(VECTOR2I(mm(145), mm(135)))
    outline.SetLayer(pcbnew.Edge_Cuts)
    outline.SetWidth(mm(0.1))
    board.Add(outline)

    fp = pcbnew.FOOTPRINT(board)
    fp.SetReference("AE1")
    fp.SetValue("ANTENNA")
    board.Add(fp)
    fp.SetPosition(VECTOR2I(mm(120), mm(109)))

    pad = pcbnew.PAD(fp)
    pad.SetNumber("1")
    pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
    pad.SetShape(pcbnew.PAD_SHAPE_RECT)
    pad.SetSize(VECTOR2I(mm(1.0), mm(0.5)))
    pad.SetLayerSet(pad.SMDMask())
    pad.SetFPRelativePosition(VECTOR2I(mm(-2.0), mm(1.0)))
    pad.SetOrientation(deg(20.0))
    fp.Add(pad)

    # Texts (Issue #6126).  Reference/Value are keep-upright fields; the user
    # texts cover both keep-upright states and justifications.
    ref = fp.Reference()
    ref.SetFPRelativePosition(VECTOR2I(mm(0.0), mm(-3.0)))
    val = fp.Value()
    val.SetFPRelativePosition(VECTOR2I(mm(0.5), mm(3.0)))
    val.SetTextAngle(deg(90.0))
    for text, angle, keep_upright, h_just, v_just in (
        ("KU_L45", 45.0, True, pcbnew.GR_TEXT_H_ALIGN_LEFT, pcbnew.GR_TEXT_V_ALIGN_TOP),
        ("FREE_R100", 100.0, False, pcbnew.GR_TEXT_H_ALIGN_RIGHT, pcbnew.GR_TEXT_V_ALIGN_BOTTOM),
        ("KU_C170", 170.0, True, pcbnew.GR_TEXT_H_ALIGN_CENTER, pcbnew.GR_TEXT_V_ALIGN_CENTER),
        ("FREE_0", 0.0, False, pcbnew.GR_TEXT_H_ALIGN_LEFT, pcbnew.GR_TEXT_V_ALIGN_CENTER),
        ("KU_R0", 0.0, True, pcbnew.GR_TEXT_H_ALIGN_RIGHT, pcbnew.GR_TEXT_V_ALIGN_CENTER),
    ):
        user = pcbnew.PCB_TEXT(fp)
        user.SetText(text)
        user.SetLayer(pcbnew.F_SilkS)
        fp.Add(user)
        user.SetFPRelativePosition(VECTOR2I(mm(1.5), mm(-1.0)))
        user.SetTextAngle(deg(angle))
        user.SetKeepUpright(keep_upright)
        user.SetHorizJustify(h_just)
        user.SetVertJustify(v_just)

    # Asymmetric (chirality-revealing) keepout: a pentagon with one cut
    # corner, offset from the anchor.  Coordinates are absolute while the
    # footprint sits at rotation 0.
    add_zone(
        fp,
        [(117.0, 105.0), (123.0, 105.0), (123.0, 108.0), (119.0, 111.0), (117.0, 111.0)],
        keepout=True,
    )
    pour = add_zone(
        fp,
        [(121.0, 110.0), (124.0, 110.0), (124.0, 113.0), (121.0, 112.0)],
        keepout=False,
    )
    filler = pcbnew.ZONE_FILLER(board)
    filler.Fill([pour])
    return board


def save(board: pcbnew.BOARD, path: Path) -> None:
    pcbnew.SaveBoard(str(path), board)
    for suffix in (".kicad_pro", ".kicad_prl"):
        sidecar = path.with_suffix(suffix)
        if sidecar.exists():
            sidecar.unlink()


def ae1(board: pcbnew.BOARD) -> pcbnew.FOOTPRINT:
    return board.FindFootprintByReference("AE1")


def op_move(fp: pcbnew.FOOTPRINT) -> None:
    fp.SetPosition(VECTOR2I(mm(125), mm(120)))


def op_rot30(fp: pcbnew.FOOTPRINT) -> None:
    fp.SetOrientation(deg(30.0))


def op_move_rot90(fp: pcbnew.FOOTPRINT) -> None:
    fp.SetPosition(VECTOR2I(mm(125), mm(120)))
    fp.SetOrientation(deg(90.0))


def op_flip_lr(fp: pcbnew.FOOTPRINT) -> None:
    fp.SetOrientation(deg(30.0))
    fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)


def op_back_rot90(fp: pcbnew.FOOTPRINT) -> None:
    op_flip_lr(fp)
    fp.SetOrientation(deg(90.0))


def op_rotate_tool30(fp: pcbnew.FOOTPRINT) -> None:
    fp.Rotate(fp.GetPosition(), deg(30.0))


def op_rotate_tool200(fp: pcbnew.FOOTPRINT) -> None:
    fp.Rotate(fp.GetPosition(), deg(200.0))


CASES: dict[str, Callable[[pcbnew.FOOTPRINT], None]] = {
    "move": op_move,
    "rot30": op_rot30,
    "move_rot90": op_move_rot90,
    "flip_lr": op_flip_lr,
    "back_rot90": op_back_rot90,
    "rotate_tool30": op_rotate_tool30,
    "rotate_tool200": op_rotate_tool200,
}


def main() -> int:
    save(build_base(), BASE)
    for name, op in CASES.items():
        board = pcbnew.LoadBoard(str(BASE))
        op(ae1(board))
        save(board, OUT_DIR / f"{name}.kicad_pcb")
    for stray in OUT_DIR.glob("_scratch.*"):
        stray.unlink()
    print(f"wrote base + {len(CASES)} cases (KiCad {pcbnew.GetBuildVersion()})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
