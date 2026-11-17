"""Build the mixed-side board for the Specctra back-side reader check (#7518).

The board is made **in KiCad itself**: stock library footprints are loaded
with ``pcbnew.FootprintLoad`` and the back-side ones are moved to B.Cu with
KiCad's own ``FOOTPRINT.Flip``, so the back-side pad data in the file is what
KiCad writes, not what we think it writes.

Parts (positions in mm, board 0..40 x 0..30):

* ``U1`` -- ``Package_TO_SOT_SMD:SOT-23`` on F.Cu at (8, 8), rotation 0
* ``U2`` -- the same SOT-23, flipped to B.Cu at (28, 8), rotation 30
* ``U3`` -- the same SOT-23, flipped to B.Cu at (28, 22), rotation 0
* ``J1`` -- ``Connector_PinHeader_2.54mm:PinHeader_1x02_P2.54mm_Vertical``,
  flipped to B.Cu at (8, 20) (a back-side through-hole part)

Nets, each chosen so the router's idea of the pad layers shows in its wires:

* ``BB`` -- U2.1 to U3.1: back SMD to back SMD
* ``FB`` -- U1.1 to U2.2: front SMD to back SMD (needs a via)
* ``TB`` -- J1.1 to U3.2: back through-hole to back SMD
* ``FT`` -- U1.2 to J1.2: front SMD to back through-hole

Run with KiCad's bundled Python (KiCad is not needed at test time; the board
and the recorded DSN/SES are committed):

    /Applications/KiCad/KiCad.app/Contents/Frameworks/Python.framework/\
Versions/Current/bin/python3 tests/fixtures/dsn_back_side_7518/generate_board.py [OUT_DIR]

Generated with KiCad 10.0.7 (macOS).
"""

from __future__ import annotations

import sys
from pathlib import Path

import pcbnew  # type: ignore[import-not-found]
from pcbnew import EDA_ANGLE, VECTOR2I  # type: ignore[import-not-found]

FOOTPRINT_DIR = Path("/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints")
SOT23 = ("Package_TO_SOT_SMD", "SOT-23")
HEADER = ("Connector_PinHeader_2.54mm", "PinHeader_1x02_P2.54mm_Vertical")

#: (reference, (library, footprint), x mm, y mm, rotation deg, back side?)
PARTS = [
    ("U1", SOT23, 8.0, 8.0, 0.0, False),
    ("U2", SOT23, 28.0, 8.0, 30.0, True),
    ("U3", SOT23, 28.0, 22.0, 0.0, True),
    ("J1", HEADER, 8.0, 20.0, 0.0, True),
]

#: net name -> [(reference, pad number), ...]
NETS = {
    "BB": [("U2", "1"), ("U3", "1")],
    "FB": [("U1", "1"), ("U2", "2")],
    "TB": [("J1", "1"), ("U3", "2")],
    "FT": [("U1", "2"), ("J1", "2")],
}


def mm(v: float) -> int:
    return pcbnew.FromMM(v)


def main(out_dir: Path) -> int:
    out = out_dir / "back_side_board.kicad_pcb"
    board = pcbnew.NewBoard(str(out))

    outline = pcbnew.PCB_SHAPE(board)
    outline.SetShape(pcbnew.SHAPE_T_RECTANGLE)
    outline.SetStart(VECTOR2I(mm(0), mm(0)))
    outline.SetEnd(VECTOR2I(mm(40), mm(30)))
    outline.SetLayer(pcbnew.Edge_Cuts)
    outline.SetWidth(mm(0.1))
    board.Add(outline)

    nets = {}
    for name in NETS:
        net = pcbnew.NETINFO_ITEM(board, name)
        board.Add(net)
        nets[name] = net

    footprints = {}
    for ref, (lib, name), x, y, rot, back in PARTS:
        fp = pcbnew.FootprintLoad(str(FOOTPRINT_DIR / f"{lib}.pretty"), name)
        fp.SetFPID(pcbnew.LIB_ID(lib, name))
        fp.SetReference(ref)
        board.Add(fp)
        fp.SetPosition(VECTOR2I(mm(x), mm(y)))
        fp.SetOrientation(EDA_ANGLE(rot, pcbnew.DEGREES_T))
        if back:
            fp.Flip(fp.GetPosition(), pcbnew.FLIP_DIRECTION_LEFT_RIGHT)
        footprints[ref] = fp

    for name, pins in NETS.items():
        for ref, number in pins:
            footprints[ref].FindPadByNumber(number).SetNet(nets[name])

    pcbnew.SaveBoard(str(out), board)
    for suffix in (".kicad_prl",):
        sidecar = out.with_suffix(suffix)
        if sidecar.exists():
            sidecar.unlink()
    print(f"wrote {out} (KiCad {pcbnew.GetBuildVersion()})")
    return 0


if __name__ == "__main__":
    target = Path(sys.argv[1]) if len(sys.argv) > 1 else Path(__file__).resolve().parent
    sys.exit(main(target))
