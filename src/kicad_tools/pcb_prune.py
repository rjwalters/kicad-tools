"""Post-hoc copper pruning for routed boards (issue #8216).

``kct pcb dedupe`` only removes *exact* duplicate copper.  A DRC-clean board
can still carry copper that adds no connectivity: a redundant branch parallel
to another path, a same-layer loop, or segment ends that join their neighbour
only by overlap (off-centre of the via/pad they land on).

This module builds per-net connectivity from copper *geometry* (pads, vias and
every segment as its full-width capsule, plus pad-to-zone connections), then:

1. removes redundant segments / chains only when every pad and zone terminal
   of the net stays in the same component as before, and
2. snaps off-centre ends onto the same-net via/pad centre when that keeps the
   connectivity partition unchanged.

Connectivity is deliberately *under*-estimated (pads are modelled by their
inscribed circle), so an unrecognised join can only make the pass keep more
copper, never remove a needed segment.
"""

from __future__ import annotations

import math
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.schema.pcb import PCB, Pad, Segment

#: End-to-centre distance (mm) at/below which an end counts as centred.
CENTRE_TOL = 1e-3
_EPS = 1e-6


# ---------------------------------------------------------------------------
# Geometry helpers
# ---------------------------------------------------------------------------


def _pt_seg_dist(p: tuple[float, float], a: tuple[float, float], b: tuple[float, float]) -> float:
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    L2 = dx * dx + dy * dy
    if L2 < 1e-18:
        return math.hypot(p[0] - ax, p[1] - ay)
    t = max(0.0, min(1.0, ((p[0] - ax) * dx + (p[1] - ay) * dy) / L2))
    return math.hypot(p[0] - (ax + t * dx), p[1] - (ay + t * dy))


def _ccw(a: tuple[float, float], b: tuple[float, float], c: tuple[float, float]) -> float:
    return (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])


def _seg_seg_dist(
    a1: tuple[float, float],
    a2: tuple[float, float],
    b1: tuple[float, float],
    b2: tuple[float, float],
) -> float:
    d1, d2 = _ccw(a1, a2, b1), _ccw(a1, a2, b2)
    d3, d4 = _ccw(b1, b2, a1), _ccw(b1, b2, a2)
    if ((d1 > 0) != (d2 > 0)) and ((d3 > 0) != (d4 > 0)) and d1 != 0 and d2 != 0:
        return 0.0
    return min(
        _pt_seg_dist(a1, b1, b2),
        _pt_seg_dist(a2, b1, b2),
        _pt_seg_dist(b1, a1, a2),
        _pt_seg_dist(b2, a1, a2),
    )


def _in_polygon(p: tuple[float, float], poly: list[tuple[float, float]]) -> bool:
    inside = False
    n = len(poly)
    j = n - 1
    for i in range(n):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > p[1]) != (yj > p[1]) and p[0] < (xj - xi) * (p[1] - yi) / (yj - yi) + xi:
            inside = not inside
        j = i
    return inside


def _pad_position(fp, pad: Pad) -> tuple[float, float]:
    px, py = pad.position
    if fp.rotation:
        th = math.radians(-fp.rotation)
        c, s = math.cos(th), math.sin(th)
        px, py = px * c - py * s, px * s + py * c
    return (fp.position[0] + px, fp.position[1] + py)


def _is_cu(layer: str) -> bool:
    return layer.endswith(".Cu")


def _layer_match(item_layers: list[str], layer: str) -> bool:
    return any(lyr == layer or lyr in ("*.Cu", "F&B.Cu") for lyr in item_layers)


# ---------------------------------------------------------------------------
# Per-net model
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    kind: str  # "seg" | "via" | "pad" | "zone"
    layers: list[str]  # copper layers ("*" => every layer)
    # seg
    a: tuple[float, float] = (0.0, 0.0)
    b: tuple[float, float] = (0.0, 0.0)
    radius: float = 0.0  # half-width (seg) or radius (via/pad)
    ref: Any = None  # the Segment / Via / Pad / Zone
    index: int = -1  # index into pcb.segments for segments
    label: str = ""

    @property
    def terminal(self) -> bool:
        return self.kind in ("pad", "zone")


def _on_layer(n: _Node, layer: str) -> bool:
    return "*" in n.layers or layer in n.layers


def _shared_layers(a: _Node, b: _Node) -> bool:
    if "*" in a.layers or "*" in b.layers:
        return True
    return bool(set(a.layers) & set(b.layers))


def _touch(a: _Node, b: _Node) -> bool:
    """Do two copper shapes overlap (same-layer)?"""
    if a.kind == "zone" or b.kind == "zone":
        z, o = (a, b) if a.kind == "zone" else (b, a)
        if o.kind == "zone" or not _shared_layers(z, o):
            return False
        poly = z.ref.polygon
        pts = [o.a] if o.kind != "seg" else [o.a, o.b]
        return any(_in_polygon(p, poly) for p in pts)
    if not _shared_layers(a, b):
        return False
    ra, rb = a.radius, b.radius
    if a.kind == "seg" and b.kind == "seg":
        return _seg_seg_dist(a.a, a.b, b.a, b.b) <= ra + rb + _EPS
    if a.kind == "seg":
        return _pt_seg_dist(b.a, a.a, a.b) <= ra + rb + _EPS
    if b.kind == "seg":
        return _pt_seg_dist(a.a, b.a, b.b) <= ra + rb + _EPS
    return math.hypot(a.a[0] - b.a[0], a.a[1] - b.a[1]) <= ra + rb + _EPS


class _NetModel:
    """Connectivity graph of one net's copper."""

    def __init__(self, nodes: list[_Node]):
        self.nodes = nodes
        n = len(nodes)
        self.adj: list[set[int]] = [set() for _ in range(n)]
        for i in range(n):
            for j in range(i + 1, n):
                if _touch(nodes[i], nodes[j]):
                    self.adj[i].add(j)
                    self.adj[j].add(i)
        self.terminals = [i for i, nd in enumerate(nodes) if nd.terminal]

    def components(self, removed: set[int]) -> list[int]:
        comp = [-1] * len(self.nodes)
        cid = 0
        for s in range(len(self.nodes)):
            if comp[s] != -1 or s in removed:
                continue
            stack = [s]
            comp[s] = cid
            while stack:
                u = stack.pop()
                for v in self.adj[u]:
                    if v not in removed and comp[v] == -1:
                        comp[v] = cid
                        stack.append(v)
            cid += 1
        return comp

    def partition(self, removed: set[int]) -> frozenset[frozenset[int]]:
        comp = self.components(removed)
        groups: dict[int, set[int]] = defaultdict(set)
        for t in self.terminals:
            groups[comp[t]].add(t)
        return frozenset(frozenset(g) for g in groups.values())

    def live_neighbours(self, i: int, removed: set[int]) -> list[int]:
        return [j for j in self.adj[i] if j not in removed]

    def end_connected(self, i: int, end: tuple[float, float], removed: set[int]) -> bool:
        """Does anything alive touch segment ``i`` around the point ``end``?"""
        s = self.nodes[i]
        for j in self.adj[i]:
            if j in removed:
                continue
            o = self.nodes[j]
            if o.kind == "zone":
                if _in_polygon(end, o.ref.polygon) and _shared_layers(s, o):
                    return True
                continue
            if o.kind == "seg":
                d = _pt_seg_dist(end, o.a, o.b)
            else:
                d = math.hypot(end[0] - o.a[0], end[1] - o.a[1])
            if d <= s.radius + o.radius + _EPS:
                return True
        return False

    def offcentre_end(self, i: int) -> bool:
        """Does segment ``i`` end off-centre on a same-net via/pad?"""
        s = self.nodes[i]
        for end in (s.a, s.b):
            for j in self.adj[i]:
                o = self.nodes[j]
                if o.kind in ("via", "pad"):
                    d = math.hypot(end[0] - o.a[0], end[1] - o.a[1])
                    if CENTRE_TOL < d <= o.radius:
                        return True
        return False


def _build_net_models(pcb: PCB) -> dict[int, _NetModel]:
    by_net: dict[int, list[_Node]] = defaultdict(list)
    for idx, seg in enumerate(pcb.segments):
        if seg.net_number <= 0 or not _is_cu(seg.layer):
            continue
        by_net[seg.net_number].append(
            _Node(
                "seg",
                [seg.layer],
                a=seg.start,
                b=seg.end,
                radius=seg.width / 2.0,
                ref=seg,
                index=idx,
                label=f"segment {seg.start}->{seg.end} {seg.layer}",
            )
        )
    for via in pcb.vias:
        if via.net_number <= 0:
            continue
        layers = ["*"] if via.via_type is None else list(via.layers)
        by_net[via.net_number].append(
            _Node("via", layers, a=via.position, radius=via.size / 2.0, ref=via)
        )
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number <= 0 or pad.net_number not in by_net:
                continue
            if pad.type in ("np_thru_hole",):
                continue
            layers = ["*"] if pad.type == "thru_hole" else list(pad.layers)
            if any(lyr in ("*.Cu", "F&B.Cu") for lyr in layers):
                layers = ["*"]
            by_net[pad.net_number].append(
                _Node(
                    "pad",
                    layers,
                    a=_pad_position(fp, pad),
                    radius=min(pad.size) / 2.0,
                    ref=pad,
                    label=f"{fp.reference}.{pad.number}",
                )
            )
    for zone in pcb.zones:
        if zone.keepout is not None or zone.net_number <= 0 or zone.net_number not in by_net:
            continue
        if not zone.polygon:
            continue
        by_net[zone.net_number].append(
            _Node("zone", [zone.layer], ref=zone, label=f"zone {zone.name or zone.layer}")
        )
    return {net: _NetModel(nodes) for net, nodes in by_net.items()}


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class NetPlan:
    """Planned edits for one net, by index into ``pcb.segments``."""

    net_number: int
    net_name: str = ""
    remove: list[int] = field(default_factory=list)
    #: (segment index, "start"|"end", new xy)
    snaps: list[tuple[int, str, tuple[float, float]]] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)

    @property
    def empty(self) -> bool:
        return not self.remove and not self.snaps


def _group_for(model: _NetModel, seed: int, removed: set[int]) -> set[int] | None:
    """Removal group for ``seed``: the segment plus the chain it orphans.

    ``seed`` must sit on a cycle (both ends still attached after removal).
    Returns ``None`` when it does not.
    """
    node = model.nodes[seed]
    if node.kind != "seg":
        return None
    trial = removed | {seed}
    if not (model.end_connected(seed, node.a, trial) and model.end_connected(seed, node.b, trial)):
        return None
    group = {seed}
    # Absorb neighbours left dangling by the removal (the rest of the branch).
    work = [j for j in model.adj[seed] if model.nodes[j].kind == "seg" and j not in trial]
    while work:
        j = work.pop()
        if j in group or j in removed:
            continue
        cur = removed | group
        n = model.nodes[j]
        if not (model.end_connected(j, n.a, cur) and model.end_connected(j, n.b, cur)):
            group.add(j)
            work.extend(k for k in model.adj[j] if model.nodes[k].kind == "seg" and k not in cur)
    return group


def _plan_net(model: _NetModel, net_number: int) -> NetPlan:
    plan = NetPlan(net_number=net_number)
    baseline = model.partition(set())
    removed: set[int] = set()
    seg_ids = [i for i, n in enumerate(model.nodes) if n.kind == "seg"]
    while True:
        best: tuple[tuple[int, float], set[int]] | None = None
        for s in seg_ids:
            if s in removed or getattr(model.nodes[s].ref, "locked", False):
                continue
            group = _group_for(model, s, removed)
            if not group:
                continue
            if any(getattr(model.nodes[g].ref, "locked", False) for g in group):
                continue
            if model.partition(removed | group) != baseline:
                continue
            # Prefer the branch with an off-centre end, then the longer detour.
            off = int(any(model.offcentre_end(g) for g in group))
            length = sum(math.dist(model.nodes[g].a, model.nodes[g].b) for g in group)
            key = (off, length)
            if best is None or key > best[0]:
                best = (key, group)
        if best is None:
            break
        removed |= best[1]
        for g in sorted(best[1]):
            plan.remove.append(model.nodes[g].index)
            plan.reasons[model.nodes[g].index] = (
                "redundant branch" if best[0][0] else "redundant path or loop"
            )

    # Snap remaining off-centre ends onto same-net via/pad centres.
    for i in seg_ids:
        if i in removed or getattr(model.nodes[i].ref, "locked", False):
            continue
        n = model.nodes[i]
        for which, end in (("start", n.a), ("end", n.b)):
            target = None
            for j in model.adj[i]:
                o = model.nodes[j]
                if o.kind in ("via", "pad") and j not in removed:
                    d = math.hypot(end[0] - o.a[0], end[1] - o.a[1])
                    if CENTRE_TOL < d <= o.radius and (target is None or d < target[0]):
                        target = (d, o.a)
            if target is None:
                continue
            old_a, old_b = n.a, n.b
            if which == "start":
                n.a = target[1]
            else:
                n.b = target[1]
            # Re-derive adjacency for this node and require the partition to hold.
            old_adj = {k: set(v) for k, v in enumerate(model.adj)}
            for k in range(len(model.nodes)):
                if k != i:
                    model.adj[k].discard(i)
            model.adj[i] = set()
            for k in range(len(model.nodes)):
                if k != i and _touch(n, model.nodes[k]):
                    model.adj[i].add(k)
                    model.adj[k].add(i)
            if model.partition(removed) == baseline:
                plan.snaps.append((n.index, which, target[1]))
            else:
                n.a, n.b = old_a, old_b
                model.adj = [set(v) for _, v in sorted(old_adj.items())]
    return plan


def plan_prune(pcb: PCB) -> list[NetPlan]:
    """Compute per-net prune/snap plans without touching the board."""
    names = (
        {net.number: net.name for net in getattr(pcb, "nets", {}).values()}
        if isinstance(getattr(pcb, "nets", None), dict)
        else {}
    )
    plans = []
    for net_number, model in sorted(_build_net_models(pcb).items()):
        plan = _plan_net(model, net_number)
        if not plan.empty:
            seg0 = next((n.ref for n in model.nodes if n.kind == "seg"), None)
            plan.net_name = names.get(net_number) or (seg0.net_name if seg0 else "")
            plans.append(plan)
    return plans


# ---------------------------------------------------------------------------
# Applying
# ---------------------------------------------------------------------------


def apply_plans(pcb: PCB, plans: list[NetPlan]) -> dict[str, int]:
    """Apply plans to ``pcb`` (which must be loaded from the planned file)."""
    segs = list(pcb.segments)
    ox, oy = pcb._board_origin
    snapped = 0
    for plan in plans:
        for idx, which, (x, y) in plan.snaps:
            seg: Segment = segs[idx]
            if which == "start":
                seg.start = (x, y)
            else:
                seg.end = (x, y)
            snapped += 1
    # Snaps must hit the sexp tree before removal changes matching keys; do it
    # by position among ``(segment ...)`` children, which mirrors pcb.segments.
    seg_nodes = [c for c in pcb._sexp.children if not c.is_atom and c.name == "segment"]
    if len(seg_nodes) == len(segs):
        for plan in plans:
            for idx, which, (x, y) in plan.snaps:
                node = seg_nodes[idx].find(which)
                if node is not None:
                    node.set_value(0, x + ox)
                    node.set_value(1, y + oy)
    to_remove = [segs[i] for plan in plans for i in plan.remove]
    removed = pcb.remove_segments(to_remove) if to_remove else 0
    return {"segments_removed": removed, "ends_snapped": snapped}


# ---------------------------------------------------------------------------
# DRC verification
# ---------------------------------------------------------------------------


def _drc_signature(pcb_path: Path) -> Counter | None:
    """Violation multiset for a board, or ``None`` when DRC cannot run."""
    from kicad_tools.cli.runner import find_kicad_cli, run_drc
    from kicad_tools.drc.report import DRCReport

    cli = find_kicad_cli()
    if cli is None:
        return None
    result = run_drc(
        pcb_path,
        format="json",
        schematic_parity=False,
        kicad_cli=cli,
        refill_zones=True,
    )
    if not result.success or result.output_path is None:
        return None
    report = DRCReport.load(result.output_path)
    sig: Counter = Counter()
    for v in report.violations:
        sig[
            (v.type_str, v.severity.value if hasattr(v.severity, "value") else str(v.severity))
        ] += 1
    sig[("unconnected", "error")] += len(report.unconnected_items)
    return sig


def _worse(after: Counter, before: Counter) -> bool:
    return any(after[k] > before[k] for k in after)


@dataclass
class PruneResult:
    plans: list[NetPlan]
    accepted: list[NetPlan]
    rejected: list[NetPlan]
    drc_verified: bool
    drc_note: str
    stats: dict[str, int]


def prune_board(
    pcb_path: Path,
    *,
    verify_drc: bool = True,
    apply: bool = True,
) -> tuple[PCB, PruneResult]:
    """Plan (and optionally apply) pruning for ``pcb_path``.

    Returns the board (edited in memory when ``apply``) and a summary.  With
    ``kicad-cli`` available each net's plan is trial-applied to a scratch copy
    and kept only when ``pcb drc --refill-zones`` reports nothing new.
    """
    import tempfile

    pcb = PCB.load(pcb_path)
    plans = plan_prune(pcb)
    accepted = list(plans)
    rejected: list[NetPlan] = []
    drc_verified, note = False, "DRC verification skipped (--no-drc)"

    if verify_drc and plans:
        with tempfile.TemporaryDirectory(prefix="kct_prune_") as tmp:
            base_path = Path(tmp) / "base.kicad_pcb"
            base_path.write_text(Path(pcb_path).read_text())
            baseline = _drc_signature(base_path)
            if baseline is None:
                note = (
                    "kicad-cli not available (or DRC failed): DRC was NOT run, result is unverified"
                )
            else:
                drc_verified, note = True, "verified with kicad-cli pcb drc --refill-zones"
                accepted, rejected = [], []
                for plan in plans:
                    trial = PCB.load(pcb_path)
                    apply_plans(trial, [*accepted, plan])
                    trial_path = Path(tmp) / "trial.kicad_pcb"
                    trial.save(trial_path)
                    after = _drc_signature(trial_path)
                    if after is None or _worse(after, baseline):
                        rejected.append(plan)
                    else:
                        accepted.append(plan)
    elif verify_drc:
        note = "nothing to verify"

    stats = {"segments_removed": 0, "ends_snapped": 0}
    if apply and accepted:
        stats = apply_plans(pcb, accepted)
    else:
        stats = {
            "segments_removed": sum(len(p.remove) for p in accepted),
            "ends_snapped": sum(len(p.snaps) for p in accepted),
        }
    return pcb, PruneResult(plans, accepted, rejected, drc_verified, note, stats)
