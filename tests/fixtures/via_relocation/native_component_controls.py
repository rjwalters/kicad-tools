"""Native hole, layer, and label controls, run by a matching KiCad interpreter."""

import importlib.util
import json
import os
import sys

import pcbnew

if sys.platform != "linux":
    import wx

    app = wx.App(False)
spec = importlib.util.spec_from_file_location("worker", sys.argv[1])
worker = importlib.util.module_from_spec(spec)
spec.loader.exec_module(worker)
mm = pcbnew.FromMM


def v(x, y):
    return pcbnew.VECTOR2I(mm(x), mm(y))


def board():
    b = pcbnew.BOARD()
    for code in [1, 2]:
        b.Add(pcbnew.NETINFO_ITEM(b, "N" + str(code), code))
    return b


def track(b, start, end, net=1, layer=pcbnew.F_Cu):
    t = pcbnew.PCB_TRACK(b)
    t.SetStart(v(*start))
    t.SetEnd(v(*end))
    t.SetWidth(mm(0.2))
    t.SetLayer(layer)
    t.SetNetCode(net)
    b.Add(t)
    return t


def zone(b, hole=False, multi=False):
    p = pcbnew.SHAPE_POLY_SET()
    i = p.NewOutline()
    for x, y in [(0, 0), (10, 0), (10, 10), (0, 10)]:
        p.Append(mm(x), mm(y), i)
    if hole:
        h = p.NewHole(i)
        for x, y in [(4, 4), (4, 6), (6, 6), (6, 4)]:
            p.Append(mm(x), mm(y), i, h)
    z = pcbnew.ZONE(b)
    z.SetNetCode(1)
    z.SetLayer(pcbnew.F_Cu)
    layers = pcbnew.LSET()
    layers.AddLayer(pcbnew.F_Cu)
    if multi:
        layers.AddLayer(pcbnew.B_Cu)
    z.SetLayerSet(layers)
    z.Outline().Append(p)
    for layer in layers.Seq():
        z.SetFilledPolysList(layer, p)
    z.SetIsFilled(True)
    b.Add(z)
    return z


results = {}
b = board()
track(b, (0, 0), (2, 0), 1)
track(b, (2, 0), (4, 0), 2)
r = worker.extract(b, pcbnew)
assert len(r["groups"]) == 1, r
results["touching_foreign_net_tracks_ignore_labels"] = True
b = board()
track(b, (0, 0), (2, 0))
track(b, (0, 0), (2, 0), layer=pcbnew.B_Cu)
r = worker.extract(b, pcbnew)
assert len(r["groups"]) == 2, r
results["separate_layers_do_not_connect"] = True
b = board()
holezone = zone(b, hole=True)
print("input_native_holes", holezone.GetFill(pcbnew.F_Cu).HoleCount(0), flush=True)
track(b, (4.5, 5), (5.5, 5))
track(b, (1, 2), (2, 2), 2)
r = worker.extract(b, pcbnew)
assert sorted(map(len, r["groups"])) == [1, 2], r
print("exported_islands", json.dumps(r["islands"]), flush=True)
assert len(next(iter(r["islands"].values()))["holes"]) == 1, (
    "native explicit hole was lost in exported correspondence geometry"
)
results["hole_excluded_and_foreign_net_zone_contact_connected"] = True
for via in [False, True]:
    b = board()
    zone(b, multi=True)
    track(b, (1, 1), (2, 1))
    track(b, (1, 1), (2, 1), layer=pcbnew.B_Cu)
    if via:
        t = pcbnew.PCB_VIA(b)
        t.SetPosition(v(1, 1))
        t.SetWidth(mm(0.6))
        t.SetDrill(mm(0.3))
        t.SetLayerPair(pcbnew.F_Cu, pcbnew.B_Cu)
        t.SetNetCode(1)
        b.Add(t)
    r = worker.extract(b, pcbnew)
    assert len(r["islands"]) == 2, r
    assert len(r["groups"]) == (1 if via else 2), r
    results["multilayer_zone_via_" + str(via)] = True
print(json.dumps({"version": pcbnew.GetBuildVersion(), "controls": results}, indent=2))
sys.stdout.flush()
os._exit(0)
