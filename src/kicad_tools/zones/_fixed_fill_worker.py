"""Run under KiCad's Python interpreter; temporary copper never leaves this stage."""

import json
import os
import sys
from pathlib import Path


def main():
    import pcbnew  # type: ignore[import-not-found]  # Supplied by KiCad Python.

    # Linux native filling works without a GUI; wx.App requires an X display.
    if sys.platform != "linux":
        import wx  # type: ignore[import-not-found]  # Supplied by KiCad Python.

        _app = wx.App(False)
    project = Path(sys.argv[1]).with_suffix(".kicad_pro")
    if project.exists():
        pcbnew.GetSettingsManager().LoadProject(str(project))
    board = pcbnew.LoadBoard(sys.argv[1])
    protected = set(json.loads(sys.argv[3]))
    selected = pcbnew.ZONES()
    retained = []
    proxies = []
    fixed_priority = max((z.GetAssignedPriority() for z in board.Zones()), default=0) + 1
    for zone in list(board.Zones()):
        if zone.GetIsRuleArea():
            continue
        if zone.GetNetname() not in protected:
            selected.append(zone)
            continue
        for layer in zone.GetLayerSet().Seq():
            if not zone.HasFilledPolysForLayer(layer):
                continue
            # Preserve zone identity/type for custom clearance predicates.
            # Its temporary outline is the existing copper, not the pour intent.
            # Higher priority makes eligible zones clear this immutable outline.
            fixed = pcbnew.Cast_to_ZONE(zone.Duplicate(False))
            layers = pcbnew.LSET()
            layers.AddLayer(layer)
            fixed.SetLayerSetAndRemoveUnusedFills(layers)
            outline = pcbnew.SHAPE_POLY_SET(zone.GetFilledPolysList(layer))
            fixed.SetOutline(outline)
            outline.thisown = False
            fixed.SetAssignedPriority(fixed_priority)
            board.Add(fixed)
            proxies.append(fixed)
        board.Remove(zone)
        retained.append(zone)
    if selected and not pcbnew.ZONE_FILLER(board).Fill(selected):
        raise RuntimeError("Native selective zone fill failed")
    for fixed in proxies:
        board.Remove(fixed)
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
