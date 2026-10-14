#!/usr/bin/env python3
"""Expand-route-shrink, the one shrink (Issue #6310).

Takes a board routed at a rigid-cluster expansion (``expand_route_experiment.py
route --modes rigid``), maps every footprint and its copper back to the 1x
placement, counts the trace ends the inverse map tears off their pads, repairs
with existing machinery only, and grades the result with the harness referee.

Stages (each is graded and written to ``--out-dir``):

* ``s0_naive``  -- the literal inverse of ``scale_board_tree``'s rigid map.
  Copper whose connected component touches pads of exactly one cluster
  translates with that cluster; every other vertex (inter-cluster copper,
  free vias) scales by 1/scale about the outline centre. Trace ends that sit
  on a pad at the expanded scale but off it after the map are the repair
  sites; this is the count the issue asks for.
* ``s1_snap``   -- same, but each pad-attached vertex is translated with its
  pad's cluster (so it lands on the pad it left), free vertices stay scaled.
  This is the cheapest "repair" of a displaced end that does not re-route.
* ``s2_reroute`` -- nets that are still open or still named in a kicad-cli DRC
  violation are ripped; ``kct route --preserve-existing`` routes them again.

Research only; not wired into CI. Usage::

    uv run python scripts/research/shrink_experiment.py \
        --src boards/03-usb-joystick/output/usb_joystick.kicad_pcb \
        --routed /tmp/ers-03/03-rigid-1.5-pinned/usb_joystick_routed.kicad_pcb \
        --scale 1.5 --out-dir /tmp/ers-03/shrink --json /tmp/ers-03/shrink.json
"""

from __future__ import annotations

import argparse
import json
import math
import re
import shutil
import subprocess
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import expand_route_experiment as ere  # noqa: E402

TOL = 0.01  # mm slack when asking "is this point on that pad"


def _pad_boxes(pcb) -> list[list[dict]]:
    return _pad_boxes_for(pcb.footprints, pcb)


def _pad_boxes_for(footprints, pcb) -> list[list[dict]]:
    """Per footprint, per pad: absolute bbox, net, copper layers."""
    from kicad_tools.core.geometry import rotate_pad_offset

    copper = [layer.name for layer in pcb.copper_layers]
    out = []
    for fp in footprints:
        boxes = []
        # PCB positions are board-relative; the raw tree (and copper) is absolute.
        fx, fy = fp.position[0] + pcb.board_origin[0], fp.position[1] + pcb.board_origin[1]
        for pad in fp.pads:
            dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation or 0.0)
            ax, ay = fx + dx, fy + dy
            w, h = pad.size
            phi = math.radians(getattr(pad, "rotation", 0.0) or 0.0)
            hw = (abs(w * math.cos(phi)) + abs(h * math.sin(phi))) / 2.0
            hh = (abs(w * math.sin(phi)) + abs(h * math.cos(phi))) / 2.0
            boxes.append(
                {
                    "ref": fp.reference,
                    "num": pad.number,
                    "c": (ax, ay),
                    "box": (ax - hw, ay - hh, ax + hw, ay + hh),
                    "net": pad.net_name,
                    "layers": ere._pad_copper_layers(pad, copper),
                }
            )
        out.append(boxes)
    return out


def _inside(pt, box, tol=TOL) -> bool:
    return box[0] - tol <= pt[0] <= box[2] + tol and box[1] - tol <= pt[1] <= box[3] + tol


def _key(pt) -> tuple[int, int]:
    return round(pt[0] * 1000), round(pt[1] * 1000)


class _UF:
    def __init__(self):
        self.p: dict = {}

    def find(self, a):
        self.p.setdefault(a, a)
        while self.p[a] != a:
            self.p[a] = self.p[self.p[a]]
            a = self.p[a]
        return a

    def union(self, a, b):
        self.p[self.find(a)] = self.find(b)


def _copper_nodes(tree):
    return [c for c in tree.iter_children() if c.tag in ("segment", "via")]


def _ends(node):
    """(tag-node, xy) for each point-bearing child of a copper node."""
    if node.tag == "segment":
        return [node.find_child("start"), node.find_child("end")]
    return [node.find_child("at")]


def _layers_of(node) -> set[str]:
    if node.tag == "segment":
        return {node.find_child("layer").get_value(0)}
    return set(map(str, node.find_child("layers").values))


def shrink(src: Path, routed: Path, scale: float, out_dir: Path) -> dict:
    """Build the s0 / s1 boards; return the displaced-end census."""
    from kicad_tools.core.sexp_file import load_pcb, save_pcb
    from kicad_tools.schema.pcb import PCB

    out_dir.mkdir(parents=True, exist_ok=True)
    pcb1 = PCB.load(str(src))
    pcb15 = PCB.load(str(routed))
    # The router re-emits footprints in a different order: pair by reference.
    by_ref = {f.reference: f for f in pcb15.footprints}
    if sorted(by_ref) != sorted(f.reference for f in pcb1.footprints) or len(by_ref) != len(
        pcb15.footprints
    ):
        raise SystemExit("footprint sets differ between the 1x source and the routed board")
    fps15 = [by_ref[f.reference] for f in pcb1.footprints]
    tree1 = load_pcb(src)
    t_min = ere.outline_bbox(tree1)
    origin = ((t_min[0] + t_min[2]) / 2.0, (t_min[1] + t_min[3]) / 2.0)

    # Rigid translation of each footprint (a cluster shares one delta).
    delta = []
    for f1, f15 in zip(pcb1.footprints, fps15, strict=True):
        # Positions are relative to each board's own origin (derived from the
        # outline, which scaled): convert both to the absolute file frame.
        delta.append(
            (
                f15.position[0] + pcb15.board_origin[0] - f1.position[0] - pcb1.board_origin[0],
                f15.position[1] + pcb15.board_origin[1] - f1.position[1] - pcb1.board_origin[1],
            )
        )
    moved = sum(1 for d in delta if abs(d[0]) > 1e-6 or abs(d[1]) > 1e-6)
    pads1 = _pad_boxes(pcb1)
    pads15 = _pad_boxes_for(fps15, pcb15)

    def inv_scale(pt):
        return (
            origin[0] + (pt[0] - origin[0]) / scale,
            origin[1] + (pt[1] - origin[1]) / scale,
        )

    # Net numbering must agree between the routed tree and the 1x tree.
    tree15 = load_pcb(routed)

    def nets_of(tree):
        return {
            int(n.get_value(0)): str(n.get_value(1)) for n in tree.iter_children() if n.tag == "net"
        }

    n1, n15 = nets_of(tree1), nets_of(tree15)
    if n1 != n15:
        raise SystemExit("net tables differ between the 1x source and the routed board")

    copper = _copper_nodes(tree15)
    # Pre-existing copper in the 1x source would be double counted: refuse.
    if _copper_nodes(tree1):
        raise SystemExit("1x source already carries copper")

    # Which pad (footprint index, pad index) does each end sit on at the expanded scale?
    def pad_at(pt, net, layers):
        for fi, boxes in enumerate(pads15):
            for pi, b in enumerate(boxes):
                if b["net"] == net and (b["layers"] & layers) and _inside(pt, b["box"]):
                    return fi, pi
        return None

    uf = _UF()
    info = []  # per copper node: [(end-node, xy, pad or None)]
    for ci, node in enumerate(copper):
        net = n15[int(node.find_child("net").get_value(0))]
        layers = _layers_of(node)
        rows = []
        for e in _ends(node):
            pt = (float(e.get_value(0)), float(e.get_value(1)))
            rows.append((e, pt, pad_at(pt, net, layers)))
            uf.union(("n", ci), ("v", net, _key(pt)))
        info.append((net, rows))

    # Component -> set of clusters (identified by the delta of an attached pad).
    comp_clusters: dict = defaultdict(set)
    for ci, (_net, rows) in enumerate(info):
        for _e, _pt, pad in rows:
            if pad is not None:
                d = delta[pad[0]]
                comp_clusters[uf.find(("n", ci))].add((round(d[0], 3), round(d[1], 3)))

    def build(mode: str):
        tree = load_pcb(src)
        t15 = load_pcb(routed)
        nodes = _copper_nodes(t15)
        census = Counter()
        displaced = []
        pad_ends = 0
        for ci, node in enumerate(nodes):
            net, rows = info[ci]
            comp = uf.find(("n", ci))
            clusters = comp_clusters.get(comp, set())
            single = len(clusters) == 1
            for e, (_old, pt, pad) in zip(_ends(node), rows, strict=True):
                if single:
                    d = next(iter(clusters))
                    new = (pt[0] - d[0], pt[1] - d[1])
                    census["translated_with_cluster"] += 1
                elif pad is not None and mode == "snap":
                    d = delta[pad[0]]
                    new = (pt[0] - d[0], pt[1] - d[1])
                    census["pad_end_translated"] += 1
                else:
                    new = inv_scale(pt)
                    census["scaled"] += 1
                if pad is not None:
                    pad_ends += 1
                    b1 = pads1[pad[0]][pad[1]]
                    if not _inside(new, b1["box"]):
                        displaced.append(
                            {
                                "net": net,
                                "ref": b1["ref"],
                                "pad": b1["num"],
                                "miss_mm": round(math.dist(new, b1["c"]), 3),
                            }
                        )
                ere._set_xy(e, *new)
            tree.append(node)
        return tree, census, displaced, pad_ends

    result: dict = {
        "scale": scale,
        "origin": [round(v, 3) for v in origin],
        "footprints": len(delta),
        "footprints_moved": moved,
        "segments": sum(1 for n in copper if n.tag == "segment"),
        "vias": sum(1 for n in copper if n.tag == "via"),
        "components": len({uf.find(("n", i)) for i in range(len(copper))}),
    }
    for mode, name in (("naive", "s0_naive"), ("snap", "s1_snap")):
        tree, census, displaced, pad_ends = build(mode)
        dst = out_dir / name / src.name
        dst.parent.mkdir(parents=True, exist_ok=True)
        save_pcb(tree, dst)
        for suffix in (".kicad_pro", ".kicad_dru"):
            sib = src.with_suffix(suffix)
            if sib.exists():
                shutil.copyfile(sib, dst.with_suffix(suffix))
        result[name] = {
            "board": str(dst),
            "vertex_census": dict(census),
            "ends_on_pads_expanded": pad_ends,
            "ends_displaced": len(displaced),
            "displaced_nets": sorted({d["net"] for d in displaced}),
            "miss_mm_max": max((d["miss_mm"] for d in displaced), default=0.0),
            "miss_mm_median": (
                sorted(d["miss_mm"] for d in displaced)[len(displaced) // 2] if displaced else 0.0
            ),
            "displaced_detail": displaced,
        }
    return result


def kicad_violation_nets(board: Path) -> dict:
    """Run ``kicad-cli pcb drc --refill-zones`` (JSON); map violations to net names."""
    if shutil.which("kicad-cli") is None:
        return {"ran": False, "nets": []}
    with tempfile.TemporaryDirectory() as td:
        rep = Path(td) / "drc.json"
        subprocess.run(
            [
                "kicad-cli", "pcb", "drc", "--refill-zones", "--all-track-errors",
                "--format", "json", "--severity-error", "-o", str(rep), str(board),
            ],
            capture_output=True, text=True, timeout=600, check=False,
        )  # fmt: skip
        if not rep.exists():
            return {"ran": False, "nets": []}
        data = json.loads(rep.read_text())
    nets: Counter = Counter()
    for v in data.get("violations", []):
        names = set()
        for it in v.get("items", []):
            names.update(re.findall(r"\[([^\]]+)\]", it.get("description", "")))
        for n in names:
            nets[n] += 1
    for u in data.get("unconnected_items", []):
        for it in u.get("items", []):
            for n in re.findall(r"\[([^\]]+)\]", it.get("description", "")):
                nets[n] += 1
    return {"ran": True, "nets": dict(nets), "violations": len(data.get("violations", []))}


def rip_nets(board: Path, nets: set[str], dest: Path) -> int:
    from kicad_tools.core.sexp_file import load_pcb, save_pcb

    tree = load_pcb(board)
    names = {
        int(n.get_value(0)): str(n.get_value(1)) for n in tree.iter_children() if n.tag == "net"
    }
    kill = [
        c for c in _copper_nodes(tree) if names.get(int(c.find_child("net").get_value(0))) in nets
    ]
    for c in kill:
        tree.remove(c)
    dest.parent.mkdir(parents=True, exist_ok=True)
    save_pcb(tree, dest)
    for suffix in (".kicad_pro", ".kicad_dru"):
        sib = board.with_suffix(suffix)
        if sib.exists():
            shutil.copyfile(sib, dest.with_suffix(suffix))
    return len(kill)


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--src", type=Path, required=True, help="1x unrouted board")
    ap.add_argument("--routed", type=Path, required=True, help="board routed when expanded")
    ap.add_argument("--scale", type=float, default=1.5)
    ap.add_argument("--out-dir", type=Path, required=True)
    ap.add_argument("--board", default="03")
    ap.add_argument("--grid-mm", type=float, default=0.05)
    ap.add_argument("--timeout", type=float, default=600)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--skip-reroute", action="store_true")
    args = ap.parse_args(argv)

    board = ere.BOARDS[args.board]
    res = shrink(args.src, args.routed, args.scale, args.out_dir)
    for name in ("s0_naive", "s1_snap"):
        res[name]["grade"] = ere.grade(Path(res[name]["board"]), board)
        res[name]["kicad_nets"] = kicad_violation_nets(Path(res[name]["board"]))
        print(
            name, json.dumps({k: v for k, v in res[name].items() if k != "displaced_detail"})[:1500]
        )

    if not args.skip_reroute:
        s1 = res["s1_snap"]
        bad = set(s1["kicad_nets"].get("nets", {})) | set(
            (s1["grade"].get("stuck_nets") or {}).keys()
        )
        zone_nets = set(s1["grade"].get("zone_nets") or [])
        bad -= {""}
        inp = args.out_dir / "s2_reroute" / "in" / args.src.name
        ripped = rip_nets(Path(s1["board"]), bad, inp)
        out = args.out_dir / "s2_reroute" / args.src.stem
        out = out.parent / (args.src.stem + "_routed.kicad_pcb")
        run = ere.run_route(
            inp,
            out,
            ["--preserve-existing", "--grid", str(args.grid_mm), "--seed", "42"],
            args.timeout,
            out.parent / "route.log",
        )
        s2 = {"ripped_nets": sorted(bad), "ripped_copper_items": ripped, "route": run}
        if out.exists():
            s2["board"] = str(out)
            s2["grade"] = ere.grade(out, board)
            s2["kicad_nets"] = kicad_violation_nets(out)
        s2["zone_nets"] = sorted(zone_nets)
        res["s2_reroute"] = s2
        print("s2_reroute", json.dumps(s2)[:2500])

    if args.json:
        args.json.write_text(json.dumps(res, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
