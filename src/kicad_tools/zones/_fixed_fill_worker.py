"""Run under KiCad's Python interpreter; temporary copper never leaves this stage."""

import json
import os
import sys
from pathlib import Path


def main():
    import pcbnew  # type: ignore[import-not-found]  # Supplied by KiCad Python.
    import wx  # type: ignore[import-not-found]  # Supplied by KiCad Python.

    _app = wx.App(False)
    project = Path(sys.argv[1]).with_suffix(".kicad_pro")
    if project.exists():
        pcbnew.GetSettingsManager().LoadProject(str(project))
    board = pcbnew.LoadBoard(sys.argv[1])
    protected = set(json.loads(sys.argv[3]))
    selected = pcbnew.ZONES()
    retained = []
    for zone in list(board.Zones()):
        if zone.GetIsRuleArea():
            continue
        if zone.GetNetname() not in protected:
            selected.append(zone)
            continue
        for layer in zone.GetLayerSet().Seq():
            if not zone.HasFilledPolysForLayer(layer):
                continue
            footprint = pcbnew.FOOTPRINT(board)
            pad = pcbnew.PAD(footprint)
            pad.SetShape(pcbnew.PAD_SHAPE_CUSTOM)
            pad.SetSize(pcbnew.VECTOR2I(0, 0))
            pad.SetAttribute(pcbnew.PAD_ATTRIB_SMD)
            layers = pcbnew.LSET()
            layers.AddLayer(layer)
            pad.SetLayerSet(layers)
            pad.AddPrimitivePoly(layer, zone.GetFilledPolysList(layer), 0, True)
            pad.SetNetCode(zone.GetNetCode())
            footprint.Add(pad)
            board.Add(footprint)
        board.Remove(zone)
        retained.append(zone)
    if selected and not pcbnew.ZONE_FILLER(board).Fill(selected):
        raise RuntimeError("Native selective zone fill failed")
    pcbnew.SaveBoard(sys.argv[2], board)
    # wx teardown can enter its GUI event loop in command-line Python builds.
    # SaveBoard has closed the output; the parent checks it before publishing.
    sys.stdout.flush()
    sys.stderr.flush()
    os._exit(0)


if __name__ == "__main__":
    try:
        main()
    except BaseException:
        import traceback

        traceback.print_exc()
        sys.stderr.flush()
        os._exit(1)
