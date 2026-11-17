#!/usr/bin/env python3
"""Predict, off-line and post-route, whether a zone refill can still join its pads.

The experiment of issue #9002 (idea from the WeaveEngine evaluation, #8993,
ideas-only): the router already excludes poured nets from trace routing
(``auto_pour.py``) and pour integrity is verified only *after* the refill by
the kicad-cli ``isolated_copper`` cross-gate.  This script measures the gap
in between -- whether the placed foreign-net copper leaves room for a minimal
connectivity skeleton of each zone, i.e. straight corridors at the zone's own
``min_thickness`` and ``clearance`` joining the zone's pads.  A refill whose
skeleton does not fit is cut into pad-less islands.

Model (deliberately cheap; precision/recall against the refill gate is the
experiment's output, not a given):

* **Subjects** are board-level pour zones (non-keepout, netted, with a
  boundary polygon), per copper layer they span.
* **Anchors** per zone-layer are the net's pads on that layer (excluding
  ``(zone_connect 0)`` pads, which are deliberately unconnected) plus
  *live* vias: same-net vias spanning the layer whose same-net copper
  cluster reaches a pad somewhere on the board.  A zone-layer with **no**
  anchors is predicted unfit -- every island of such a fill is isolated
  copper by definition.
* **Pre-connected** anchors (one transitive same-net copper cluster grown
  through touching pads/tracks/vias) need no corridor.
* **Skeleton edges** are straight corridors between anchors; an edge *fits*
  when the copper corridor ``LineString(a, b).buffer(min_thickness / 2)``
  lies inside the *available region*: the zone outline minus foreign-net
  copper (pads, vias, track segments, via ``fill_clearance._collect_obstacles``
  -- the existing clearance machinery) grown by the zone ``clearance``, minus
  copper keepouts.  Foreign *zone fills* are not blockers: the refill
  negotiates those itself.
* A net is **predicted-unfit** when some zone-layer's anchors cannot all be
  joined (clusters + fitting corridors leave >= 2 groups), or a zone-layer
  has no anchors.  Unfit witness edges are reported with their blocking
  object (net, pad pair, layer).

With ``--refill-gate`` each board is additionally run through
``kicad-cli pcb drc --refill-zones`` on the pinned evidence toolchain
(scratch ``KICAD_CONFIG_HOME``, refusal on any other kicad-cli version or a
``lib_footprint_issues`` report -- same contract as committed native
evidence), the actual ``isolated_copper`` zone nets are extracted, and
per-board precision/recall of the prediction are printed.

Usage:
    uv run python scripts/pour_skeleton_fit.py <pcb> [<pcb> ...] [--json PATH]
                                                [--refill-gate] [--timeout S]

Exit code 0 with results (this is a measurement, not a gate); 2 on a
toolchain/evidence failure.
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass, field
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from kicad_tools._shapely import require_shapely  # noqa: E402
from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError  # noqa: E402
from kicad_tools.core.layers import via_spans_layer  # noqa: E402
from kicad_tools.schema.pcb import PCB  # noqa: E402
from kicad_tools.sexp import parse_file  # noqa: E402
from kicad_tools.zones.fill_clearance import (  # noqa: E402
    _build_net_name_map,
    _collect_obstacles,
)

#: Maximum unfit witness edges reported per zone-layer.
MAX_WITNESSES = 3


@dataclass
class SkeletonEdge:
    """One straight corridor between two anchors (unfit, with its blocker)."""

    a_label: str
    b_label: str
    a_pos: tuple[float, float]
    b_pos: tuple[float, float]
    blocker: str


@dataclass
class ZoneLayerResult:
    """Fit verdict for one zone on one copper layer."""

    net: str
    layer: str
    anchors: int
    fit: bool
    reason: str = ""
    witnesses: list[SkeletonEdge] = field(default_factory=list)


@dataclass
class BoardResult:
    """Prediction (and, with --refill-gate, ground truth) for one board."""

    path: str
    zones_checked: int = 0
    zone_layers: list[ZoneLayerResult] = field(default_factory=list)
    refill_isolated_copper: dict[str, int] = field(default_factory=dict)
    unconnected_items: int = 0
    kicad_cli: str | None = None

    @property
    def predicted_unfit_nets(self) -> set[str]:
        return {r.net for r in self.zone_layers if not r.fit}

    @property
    def actual_unfit_nets(self) -> set[str]:
        return set(self.refill_isolated_copper)


@dataclass(frozen=True)
class _Copper:
    """One net-assigned copper shape, labelled, with its layer span."""

    shape: object
    kind: str  # "pad" | "via" | "segment"
    net_key: str
    layers: tuple[str, ...]
    on_layer: bool  # via-span and wildcard semantics already applied


def _pad_on_layer(pad, layer: str) -> bool:
    """Whether a pad's copper exists on *layer* (``*.Cu``/``F&B.Cu`` wildcards)."""
    names = set(pad.layers)
    if "*.*" in names or "*.Cu" in names:
        return True
    if "F&B.Cu" in names and layer in ("F.Cu", "B.Cu"):
        return True
    return layer in names


def _zone_copper_layers(zone, copper: list[str]) -> list[str]:
    """Expand a zone's ``layer``/``layers`` (with wildcards) to copper layers."""
    names = list(zone.layers) if zone.layers else [zone.layer]
    out: list[str] = []
    for name in names:
        if name in ("*.Cu", "*.*"):
            out.extend(copper)
        elif name == "F&B.Cu":
            out.extend(n for n in ("F.Cu", "B.Cu") if n in copper)
        elif name in copper:
            out.append(name)
    return list(dict.fromkeys(out))


def _pad_position(fp, pad) -> tuple[float, float]:
    """Sheet-absolute pad centre (KiCad negated-angle convention)."""
    from kicad_tools.core.geometry import rotate_pad_offset

    dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation)
    return (fp.position[0] + dx, fp.position[1] + dy)


def _zone_net_key(zone, pcb: PCB) -> str:
    if zone.net_name:
        return zone.net_name
    net = pcb.get_net(zone.net_number) if zone.net_number else None
    return net.name if net and net.name else f"#{zone.net_number}"


def _pad_matches_zone(pad, zone) -> bool:
    """Net identity between a schema pad and a schema zone."""
    if zone.net_number:
        return pad.net_number == zone.net_number
    return bool(zone.net_name) and pad.net_name == zone.net_name


def _collect_net_copper(doc, name_map: dict[int, str]) -> list[_Copper]:
    """All net-assigned copper as labelled shapes (reuses the fill machinery)."""
    require_shapely("pour skeleton fit")
    import shapely as shapely_mod  # type: ignore[import-untyped]

    out: list[_Copper] = []
    for obs in _collect_obstacles(doc, shapely_mod, name_map):
        if obs.source_pad is not None:
            kind = "pad"
        elif len(obs.layers) >= 2:
            kind = "via"
        else:
            kind = "segment"
        out.append(
            _Copper(
                shape=obs.shape,
                kind=kind,
                net_key=obs.net_key,
                layers=tuple(obs.layers),
                on_layer=False,
            )
        )
    return out


def _copper_on_layer(copper: _Copper, layer: str) -> bool:
    """Whether the copper exists on *layer* (pad wildcards + via-span rules)."""
    if "*.Cu" in copper.layers or "*.*" in copper.layers:
        return True
    if layer in copper.layers:
        return True
    return via_spans_layer(list(copper.layers), layer)


class _UnionFind:
    def __init__(self, n: int) -> None:
        self.parent = list(range(n))

    def find(self, i: int) -> int:
        while self.parent[i] != i:
            self.parent[i] = self.parent[self.parent[i]]
            i = self.parent[i]
        return i

    def union(self, i: int, j: int) -> None:
        ri, rj = self.find(i), self.find(j)
        if ri != rj:
            self.parent[rj] = ri


def _same_net_clusters(shapes: list) -> _UnionFind:
    """Union-find over same-net copper shapes that touch each other."""
    import shapely.strtree  # type: ignore[import-untyped]

    uf = _UnionFind(len(shapes))
    tree = shapely.strtree.STRtree(shapes)
    for i, shape in enumerate(shapes):
        for j in tree.query(shape):
            j = int(j)
            if j > i and shape.intersects(shapes[j]):
                uf.union(i, j)
    return uf


def analyse_board(path: Path) -> BoardResult:
    """Run the skeleton-fit prediction over one routed board."""
    from shapely.geometry import Polygon  # type: ignore[import-untyped]
    from shapely.validation import make_valid  # type: ignore[import-untyped]

    pcb = PCB.load(path)
    doc = parse_file(path)
    name_map = _build_net_name_map(doc)
    copper = [layer.name for layer in pcb.copper_layers] or ["F.Cu", "B.Cu"]

    # Schema coordinates are board-relative; ``_collect_obstacles`` shapes are
    # sheet-absolute.  Everything below runs in the sheet-absolute frame.
    ox, oy = pcb.board_origin

    pour_zones = [
        z
        for z in pcb.zones
        if z.keepout is None
        and z.is_filled
        and (z.net_number != 0 or z.net_name)
        and len(z.polygon) >= 3
    ]
    keepouts = [
        make_valid(Polygon([(x + ox, y + oy) for x, y in k.polygon]))
        for k in pcb.zones
        if k.keepout is not None and not k.keepout.copperpour_allowed and len(k.polygon) >= 3
    ]

    result = BoardResult(path=str(path), zones_checked=len(pour_zones))
    all_copper = _collect_net_copper(doc, name_map)

    for zone in pour_zones:
        net_key = _zone_net_key(zone, pcb)
        own = [c for c in all_copper if c.net_key == net_key]
        foreign = [c for c in all_copper if c.net_key != net_key]
        clusters = _same_net_clusters([c.shape for c in own])
        pad_roots = {clusters.find(i) for i, c in enumerate(own) if c.kind == "pad"}
        outline = make_valid(Polygon([(x + ox, y + oy) for x, y in zone.polygon]))

        for layer in _zone_copper_layers(zone, copper):
            result.zone_layers.append(
                _analyse_zone_layer(
                    pcb=pcb,
                    zone=zone,
                    outline=outline,
                    layer=layer,
                    net_key=net_key,
                    own=own,
                    clusters=clusters,
                    pad_roots=pad_roots,
                    foreign=foreign,
                    keepouts=keepouts,
                    origin=(ox, oy),
                )
            )
    return result


def _analyse_zone_layer(
    *,
    pcb: PCB,
    zone,
    outline,
    layer: str,
    net_key: str,
    own: list[_Copper],
    clusters: _UnionFind,
    pad_roots: set[int],
    foreign: list[_Copper],
    keepouts: list,
    origin: tuple[float, float],
) -> ZoneLayerResult:
    import shapely.strtree  # type: ignore[import-untyped]
    from shapely.geometry import LineString, Point  # type: ignore[import-untyped]
    from shapely.ops import unary_union  # type: ignore[import-untyped]
    from shapely.validation import make_valid  # type: ignore[import-untyped]

    ox, oy = origin
    own_tree = shapely.strtree.STRtree([c.shape for c in own])

    def _own_shape_at(pos: tuple[float, float]) -> int | None:
        """Index of the own copper shape covering *pos* (pad or via centre)."""
        point = Point(pos)
        for i in (int(k) for k in own_tree.query(point)):
            if own[i].shape.covers(point):
                return i
        return None

    # Anchors: the net's pads on this layer (minus zone_connect 0 pads) and
    # live same-net vias spanning it.  Each anchor keeps the index of its own
    # copper shape so pre-connection is a union-find lookup.
    anchors: list[tuple[str, tuple[float, float], int | None]] = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if not _pad_matches_zone(pad, zone) or pad.zone_connect == 0:
                continue
            if not _pad_on_layer(pad, layer):
                continue
            px, py = _pad_position(fp, pad)
            pos = (px + ox, py + oy)
            anchors.append((f"{fp.reference}.{pad.number}", pos, _own_shape_at(pos)))
    for via in pcb.vias:
        if via.net_number != zone.net_number or not via_spans_layer(list(via.layers), layer):
            continue
        pos = (via.position[0] + ox, via.position[1] + oy)
        idx = _own_shape_at(pos)
        if idx is None or clusters.find(idx) not in pad_roots:
            continue  # stitch via whose cluster reaches no pad
        anchors.append((f"via({pos[0]:.1f},{pos[1]:.1f})", pos, idx))

    if not anchors:
        return ZoneLayerResult(
            net=net_key,
            layer=layer,
            anchors=0,
            fit=False,
            reason="no same-net anchor on this layer (every fill island would be isolated)",
        )
    if len(anchors) == 1:
        return ZoneLayerResult(net=net_key, layer=layer, anchors=1, fit=True)

    # Available region: zone outline minus layer's foreign copper grown by
    # the zone clearance, minus copper keepouts.
    minx, miny, maxx, maxy = outline.bounds
    slack = zone.clearance + zone.min_thickness
    window = (minx - slack, miny - slack, maxx + slack, maxy + slack)
    pieces = [
        c.shape.buffer(zone.clearance)
        for c in foreign
        if _copper_on_layer(c, layer)
        and _bbox_overlaps(c.shape.bounds, window)
        and not c.shape.is_empty
    ]
    pieces.extend(k for k in keepouts if _bbox_overlaps(k.bounds, outline.bounds))
    available = outline if not pieces else make_valid(outline.difference(unary_union(pieces)))

    half = max(zone.min_thickness, 0.0) / 2.0

    def fits(a: tuple[float, float], b: tuple[float, float]) -> bool:
        return available.contains(LineString([a, b]).buffer(half))

    def blocker_for(a: tuple[float, float], b: tuple[float, float]) -> str:
        corridor = LineString([a, b]).buffer(half)
        if not outline.contains(corridor):
            return "leaves the zone outline"
        if any(corridor.intersects(k) for k in keepouts):
            return "copper keepout"
        clearance_corridor = LineString([a, b]).buffer(half + zone.clearance)
        for c in foreign:
            if _copper_on_layer(c, layer) and clearance_corridor.intersects(c.shape):
                return f"{c.kind} net '{c.net_key}'"
        return "available region too fragmented"

    # Kruskal over anchors: pre-connected anchors join for free; otherwise
    # the straight corridor must fit.
    pairs = sorted(
        (math.dist(a[1], b[1]), i, j)
        for i, a in enumerate(anchors)
        for j, b in enumerate(anchors)
        if i < j
    )
    uf = _UnionFind(len(anchors))
    unfit: list[tuple[float, int, int]] = []
    for dist, i, j in pairs:
        ri, rj = uf.find(i), uf.find(j)
        if ri == rj:
            continue
        a, b = anchors[i], anchors[j]
        pre_connected = (
            a[2] is not None and b[2] is not None and clusters.find(a[2]) == clusters.find(b[2])
        )
        if pre_connected or fits(a[1], b[1]):
            uf.union(i, j)
        else:
            unfit.append((dist, i, j))

    group_of = [uf.find(i) for i in range(len(anchors))]
    groups = len(set(group_of))
    res = ZoneLayerResult(net=net_key, layer=layer, anchors=len(anchors), fit=groups <= 1)
    if groups > 1:
        res.reason = f"{groups} anchor groups that no fitting corridor joins"
        for _dist, i, j in unfit:
            if group_of[i] == group_of[j]:
                continue
            a, b = anchors[i], anchors[j]
            res.witnesses.append(
                SkeletonEdge(
                    a_label=a[0],
                    b_label=b[0],
                    a_pos=a[1],
                    b_pos=b[1],
                    blocker=blocker_for(a[1], b[1]),
                )
            )
            if len(res.witnesses) >= MAX_WITNESSES:
                break
    return res


def _bbox_overlaps(a: tuple, b: tuple) -> bool:
    return not (a[2] < b[0] or b[2] < a[0] or a[3] < b[1] or b[3] < a[1])


def _zone_net_from_violation(violation: dict) -> str | None:
    """Net name of an ``isolated_copper`` finding, from its item description.

    kicad-cli describes the offending item as ``Zone [GND] on F.Cu, ...``;
    boards without serialised uuids (test fixtures) get synthetic uuids in
    the report, so the description is the only stable attribution.
    """
    import re

    for item in violation.get("items", []):
        match = re.search(r"Zone \[([^\]]*)\]", str(item.get("description", "")))
        if match:
            return match.group(1) or None
    return None


def refill_gate(result: BoardResult, timeout: float) -> None:
    """Run the kicad-cli refill gate; record actual ``isolated_copper`` nets."""
    import tempfile

    from kicad_tools.cli.kicad_toolchain import (
        evidence_toolchain,
        require_library_environment,
    )
    from kicad_tools.cli.runner import find_kicad_cli, run_kicad_drc_command

    path = Path(result.path)
    with evidence_toolchain(mounts=[path]) as chain:
        binary = find_kicad_cli()
        assert binary is not None
        with tempfile.NamedTemporaryFile(suffix=".json") as report_file:
            proc, refilled = run_kicad_drc_command(
                lambda refill: [
                    str(binary),
                    "pcb",
                    "drc",
                    *(["--refill-zones"] if refill else []),
                    "--format",
                    "json",
                    "-o",
                    report_file.name,
                    str(path),
                ],
                binary,
                refill_zones=True,
                timeout=timeout,
                text=True,
            )
            if proc.returncode not in (0, 5):
                print(
                    f"  refill gate: kicad-cli exited {proc.returncode}; "
                    f"{(proc.stderr or '').strip()[:200]}",
                    file=sys.stderr,
                )
                return
            report_file.seek(0)
            report = json.load(report_file)
        if not refilled:
            print(
                "  refill gate: ran WITHOUT --refill-zones; not a refill measurement",
                file=sys.stderr,
            )
            return
        require_library_environment(report)
        result.kicad_cli = chain.version
        counts: dict[str, int] = {}
        for violation in report.get("violations", []):
            if violation.get("type") != "isolated_copper":
                continue
            net = _zone_net_from_violation(violation)
            if net is None:
                continue
            counts[net] = counts.get(net, 0) + 1
        result.refill_isolated_copper = counts
        result.unconnected_items = len(report.get("unconnected_items", []))


def _fmt_ratio(value: float | None) -> str:
    return "n/a" if value is None else f"{value:.2f}"


def _print_board(result: BoardResult) -> None:
    print(f"== {result.path}")
    if not result.zone_layers:
        print("   no pour zones with nets (nothing to predict)")
        return
    for r in result.zone_layers:
        state = "FIT" if r.fit else "UNFIT"
        line = f"   net '{r.net}' on {r.layer}: {r.anchors} anchor(s) -> {state}"
        if r.reason:
            line += f" ({r.reason})"
        print(line)
        for w in r.witnesses:
            print(
                f"       witness {w.a_label} ({w.a_pos[0]:.1f},{w.a_pos[1]:.1f}) -> "
                f"{w.b_label} ({w.b_pos[0]:.1f},{w.b_pos[1]:.1f}): blocked by {w.blocker}"
            )
    predicted = sorted(result.predicted_unfit_nets)
    print(f"   predicted-unfit nets: {predicted if predicted else 'none'}")
    if result.kicad_cli is not None:
        actual = sorted(result.actual_unfit_nets)
        findings = dict(sorted(result.refill_isolated_copper.items()))
        print(
            f"   refill-gate isolated_copper (kicad-cli {result.kicad_cli}): "
            f"{findings or 'none'} -> nets {actual or '[]'}; "
            f"unconnected_items={result.unconnected_items}"
        )
        hits = set(predicted) & set(actual)
        precision = len(hits) / len(predicted) if predicted else None
        recall = len(hits) / len(actual) if actual else None
        print(
            f"   precision={_fmt_ratio(precision)} recall={_fmt_ratio(recall)} (hits={len(hits)})"
        )


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("pcb", nargs="+", help="routed .kicad_pcb file(s)")
    ap.add_argument("--json", type=Path, help="write machine-readable results here")
    ap.add_argument(
        "--refill-gate",
        action="store_true",
        help="also run kicad-cli pcb drc --refill-zones on the pinned toolchain "
        "and report precision/recall vs actual isolated_copper",
    )
    ap.add_argument("--timeout", type=float, default=600.0, help="per-board refill gate budget (s)")
    args = ap.parse_args()

    results: list[BoardResult] = []
    try:
        for raw in args.pcb:
            result = analyse_board(Path(raw))
            if args.refill_gate:
                refill_gate(result, args.timeout)
            results.append(result)
            _print_board(result)
    except KiCadEnvironmentError as exc:
        print(f"evidence toolchain refusal: {exc}", file=sys.stderr)
        return 2

    if args.json:
        payload = {
            "boards": [
                {
                    "path": b.path,
                    "zones_checked": b.zones_checked,
                    "zone_layers": [
                        {
                            "net": r.net,
                            "layer": r.layer,
                            "anchors": r.anchors,
                            "fit": r.fit,
                            "reason": r.reason,
                            "witnesses": [
                                {
                                    "from": w.a_label,
                                    "to": w.b_label,
                                    "blocker": w.blocker,
                                    "from_pos": list(w.a_pos),
                                    "to_pos": list(w.b_pos),
                                }
                                for w in r.witnesses
                            ],
                        }
                        for r in b.zone_layers
                    ],
                    "refill_isolated_copper": b.refill_isolated_copper,
                    "unconnected_items": b.unconnected_items,
                    "kicad_cli": b.kicad_cli,
                }
                for b in results
            ]
        }
        args.json.write_text(json.dumps(payload, indent=2) + "\n")
        print(f"json written: {args.json}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
