"""Read native copper components under KiCad Python; never save the board."""

import json
import os
import sys
from pathlib import Path


def extract(board, pcbnew):
    """Give individual fill islands identities without changing filled copper."""
    originals = list(board.Zones())
    proxies = []
    islands = {}
    occupied = set()

    def identity(item):
        return str(item.m_Uuid.AsString())

    # Copper graphics are outside CONNECTIVITY_DATA's connected-item model.
    graphics = list(board.GetDrawings())
    for footprint in board.GetFootprints():
        graphics.extend(footprint.GraphicalItems())
    if any(item.IsOnCopperLayer() for item in graphics):
        raise ValueError("Unmodeled copper graphic in native component proof")

    for zone in originals:
        if zone.GetIsRuleArea():
            continue
        for layer in zone.GetLayerSet().Seq():
            if not zone.HasFilledPolysForLayer(layer):
                continue
            polygons = zone.GetFill(layer)
            for index in range(polygons.OutlineCount()):
                single = polygons.UnitSet(index)
                proxy = pcbnew.Cast_to_ZONE(zone.Duplicate(False))
                key = identity(proxy)
                if key in occupied:
                    raise ValueError("Duplicate island identity")
                occupied.add(key)

                def points(chain):
                    return [
                        [chain.CPoint(i).x, chain.CPoint(i).y] for i in range(chain.PointCount())
                    ]

                geometry = pcbnew.SHAPE_POLY_SET(single)
                # Native Unfracture restores bridged-ring holes but discards
                # already-explicit holes. Preserve that valid representation;
                # mixed/invalid rings will be refused by the parent geometry check.
                if geometry.HoleCount(0) == 0:
                    geometry.Unfracture()
                if geometry.OutlineCount() != 1:
                    raise ValueError("Native island decomposition changed component count")
                islands[key] = {
                    "zone": identity(zone),
                    "layer": layer,
                    "outer": points(geometry.COutline(0)),
                    "holes": [points(geometry.CHole(0, h)) for h in range(geometry.HoleCount(0))],
                }
                layers = pcbnew.LSET()
                layers.AddLayer(layer)
                proxy.SetLayerSetAndRemoveUnusedFills(layers)
                proxy.SetFilledPolysList(layer, single)
                proxy.SetIsFilled(True)
                proxies.append(proxy)
    for zone in originals:
        board.Remove(zone)
    for proxy in proxies:
        board.Add(proxy)
    items = list(board.GetTracks()) + list(board.GetPads()) + proxies
    inventory = {}
    for item in items:
        key = identity(item)
        if key in inventory:
            raise ValueError("Duplicate native copper identity")
        inventory[key] = {"kind": item.GetClass(), "net": item.GetNetname()}
    connectivity = pcbnew.CONNECTIVITY_DATA()
    if not connectivity.Build(board):
        raise ValueError("Native connectivity build failed")
    groups = set()
    for item in items:
        group = frozenset(
            identity(other) for other in connectivity.GetConnectedItems(item, pcbnew.IGNORE_NETS)
        )
        if identity(item) not in group or not group <= inventory.keys():
            raise ValueError("Native connectivity omitted or introduced an object")
        groups.add(group)
    if sum(map(len, groups)) != len(inventory):
        raise ValueError("Native component memberships overlap")
    return {
        "version": pcbnew.GetBuildVersion(),
        "inventory": inventory,
        "islands": islands,
        "groups": sorted(sorted(group) for group in groups),
    }


def main():
    import pcbnew

    if sys.platform != "linux":
        import wx

        _app = wx.App(False)
    board_path = Path(sys.argv[1]).resolve()
    output_path = Path(sys.argv[2]).resolve()
    project = board_path.with_suffix(".kicad_pro")
    if project.exists():
        pcbnew.GetSettingsManager().LoadProject(str(project))
    result = extract(pcbnew.LoadBoard(str(board_path)), pcbnew)
    output_path.write_text(json.dumps(result))
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
