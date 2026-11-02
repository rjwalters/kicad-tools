"""Post-hoc copper pruning for routed boards (issue #8216).

``kct pcb dedupe`` only removes *exact* duplicate copper.  A DRC-clean board
can still carry copper that adds no connectivity: a redundant branch parallel
to another path, a same-layer loop, or segment ends that join their neighbour
only by overlap (off-centre of the via/pad they land on).

Connectivity here follows what KiCad treats as connected, and is deliberately
*under*-estimated wherever the two could differ:

* track-to-track joins need **coincident endpoints** -- capsule overlap or
  tangency is never a join;
* a track end joins a via / pad only when it lies inside the via's disc or the
  pad's *full* shape (rect / roundrect / oval / circle, rotation-aware);
* a track end joins a zone when it lies inside the zone outline.

Anything that could be an *unmodelled* join (a track whose end is free, whose
body touches other same-net copper without an endpoint join, or which crosses
a same-net zone / pad / via without an end inside it) is **protected**: it is
never removed or snapped, so an unseen join can only make the pass keep more
copper.  A removal is accepted only when every pad / zone terminal stays in
the same component as before **and** every remaining track end that was
anchored stays anchored -- the pass never creates an overlap-only joint.
"""

from __future__ import annotations

import math
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.schema.pcb import PCB, Pad, Segment

#: End-to-centre distance (mm) at/below which an end counts as centred.
CENTRE_TOL = 1e-3
#: Coordinate tolerance (mm) for two track endpoints to count as one point.
JOIN_TOL = 2e-6
_EPS = 1e-6
#: Spatial-hash cell size (mm) used to find nearby segments.
_CELL = 1.0


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


def _same_pt(p: tuple[float, float], q: tuple[float, float]) -> bool:
    return abs(p[0] - q[0]) <= JOIN_TOL and abs(p[1] - q[1]) <= JOIN_TOL


def _seg_samples(a: tuple[float, float], b: tuple[float, float], step: float = 0.25):
    n = max(1, int(math.dist(a, b) / step))
    return [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]


# ---------------------------------------------------------------------------
# Per-net model
# ---------------------------------------------------------------------------


@dataclass
class _Node:
    kind: str  # "seg" | "via" | "pad" | "zone"
    layers: list[str]  # copper layers ("*" => every layer)
    a: tuple[float, float] = (0.0, 0.0)  # seg start / via / pad centre
    b: tuple[float, float] = (0.0, 0.0)  # seg end
    radius: float = 0.0  # half-width (seg) or radius (via)
    ref: Any = None  # the Segment / Via / Pad / Zone
    index: int = -1  # index into pcb.segments for segments
    label: str = ""
    # pad shape: half extents, absolute rotation (deg), corner radius
    hx: float = 0.0
    hy: float = 0.0
    rot: float = 0.0
    rr: float = 0.0

    @property
    def terminal(self) -> bool:
        return self.kind in ("pad", "zone")


def _shared_layers(a: _Node, b: _Node) -> bool:
    if "*" in a.layers or "*" in b.layers:
        return True
    return bool(set(a.layers) & set(b.layers))


def _contains(n: _Node, p: tuple[float, float], margin: float = 0.0) -> bool:
    """Is ``p`` inside the via disc / the pad's full shape (grown by ``margin``)?"""
    dx, dy = p[0] - n.a[0], p[1] - n.a[1]
    if n.kind == "via":
        return math.hypot(dx, dy) <= n.radius + margin
    if n.rot:
        th = math.radians(n.rot)
        c, s = math.cos(th), math.sin(th)
        dx, dy = dx * c + dy * s, -dx * s + dy * c
    qx = abs(dx) - (n.hx - n.rr)
    qy = abs(dy) - (n.hy - n.rr)
    d = math.hypot(max(qx, 0.0), max(qy, 0.0)) + min(max(qx, qy), 0.0) - n.rr
    return d <= margin


def _pad_geometry(pad: Pad) -> tuple[float, float, float]:
    """(half-x, half-y, corner radius) of the pad's full shape."""
    hx, hy = pad.size[0] / 2.0, pad.size[1] / 2.0
    shape = (pad.shape or "").lower()
    if shape == "circle":
        return min(hx, hy), min(hx, hy), min(hx, hy)
    if shape == "oval":
        return hx, hy, min(hx, hy)
    if shape == "roundrect":
        return hx, hy, pad.roundrect_rratio * min(pad.size)
    return hx, hy, 0.0  # rect / trapezoid / custom: bounding rectangle


class _NetModel:
    """Endpoint-anchored connectivity graph of one net's copper."""

    def __init__(self, nodes: list[_Node]):
        self.nodes = nodes
        n = len(nodes)
        self.adj: list[set[int]] = [set() for _ in range(n)]
        #: per segment: nodes anchored at (start, end)
        self.anch: list[list[set[int]]] = [[set(), set()] for _ in range(n)]
        self.terminals = [i for i, nd in enumerate(nodes) if nd.terminal]
        seg_ids = [i for i, nd in enumerate(nodes) if nd.kind == "seg"]
        # Candidate seg-seg pairs via a spatial hash on (inflated) bboxes.
        grid: dict[tuple[int, int], list[int]] = defaultdict(list)
        for i in seg_ids:
            for cell in self._cells(nodes[i]):
                grid[cell].append(i)
        pairs: set[tuple[int, int]] = set()
        for members in grid.values():
            for x in range(len(members)):
                for y in range(x + 1, len(members)):
                    pairs.add((members[x], members[y]))
        self._grid_pairs = pairs
        for i, j in pairs:
            self._link(i, j)
        for i in range(n):
            if nodes[i].kind != "seg":
                for j in range(n):
                    if j != i and (nodes[j].kind == "seg" or j > i):
                        self._link(i, j)
        self.protected = [self._is_protected(i) for i in range(n)]

    @staticmethod
    def _cells(nd: _Node):
        r = nd.radius + _EPS
        x0, x1 = sorted((nd.a[0], nd.b[0]))
        y0, y1 = sorted((nd.a[1], nd.b[1]))
        for cx in range(int(math.floor((x0 - r) / _CELL)), int(math.floor((x1 + r) / _CELL)) + 1):
            for cy in range(
                int(math.floor((y0 - r) / _CELL)), int(math.floor((y1 + r) / _CELL)) + 1
            ):
                yield (cx, cy)

    # -- edges -------------------------------------------------------------

    def _link(self, i: int, j: int) -> None:
        a, b = self.nodes[i], self.nodes[j]
        if a.kind != "seg" and b.kind == "seg":
            i, j, a, b = j, i, b, a
        if not _shared_layers(a, b):
            return
        if a.kind == "seg" and b.kind == "seg":
            hit = False
            for ea, pa in enumerate((a.a, a.b)):
                for eb, pb in enumerate((b.a, b.b)):
                    if _same_pt(pa, pb):
                        self.anch[i][ea].add(j)
                        self.anch[j][eb].add(i)
                        hit = True
            if hit:
                self.adj[i].add(j)
                self.adj[j].add(i)
            return
        if a.kind == "seg":
            for ea, pa in enumerate((a.a, a.b)):
                if b.kind == "zone":
                    ok = _in_polygon(pa, b.ref.polygon)
                else:
                    ok = _contains(b, pa)
                if ok:
                    self.anch[i][ea].add(j)
                    self.adj[i].add(j)
                    self.adj[j].add(i)
            return
        # non-segment pairs: the second node's centre inside the first's shape
        if a.kind == "zone" or b.kind == "zone":
            z, o = (a, b) if a.kind == "zone" else (b, a)
            if o.kind != "zone" and _in_polygon(o.a, z.ref.polygon):
                self.adj[i].add(j)
                self.adj[j].add(i)
            return
        for x, y, ix, iy in ((a, b, i, j), (b, a, j, i)):
            if x.kind == "pad" and y.kind in ("via", "pad") and _contains(x, y.a):
                if x.kind == "pad" and y.kind == "pad":
                    continue  # abutting pads are not a join
                self.adj[ix].add(iy)
                self.adj[iy].add(ix)
                return

    def relink(self, i: int) -> None:
        """Recompute every edge of node ``i`` after its geometry changed."""
        for k in list(self.adj[i]):
            self.adj[k].discard(i)
            for ends in self.anch[k]:
                ends.discard(i)
        self.adj[i] = set()
        self.anch[i] = [set(), set()]
        for k in range(len(self.nodes)):
            if k != i:
                self._link(i, k)

    # -- protection ---------------------------------------------------------

    def _is_protected(self, i: int) -> bool:
        """May segment ``i`` hide a join the model does not see?"""
        s = self.nodes[i]
        if s.kind != "seg":
            return True
        if getattr(s.ref, "locked", False):
            return True
        if not (self.anch[i][0] and self.anch[i][1]):
            return True  # a free end: could be an unseen join
        hw = s.radius
        for j, o in enumerate(self.nodes):
            if j == i or j in self.adj[i]:
                continue
            if o.kind == "seg":
                if (i, j) in self._grid_pairs or (j, i) in self._grid_pairs:
                    if (
                        o.layers == s.layers
                        and _seg_seg_dist(s.a, s.b, o.a, o.b) <= hw + o.radius + _EPS
                    ):
                        return True
            elif o.kind == "zone":
                if _shared_layers(s, o) and any(
                    _in_polygon(p, o.ref.polygon) for p in _seg_samples(s.a, s.b)
                ):
                    return True
            elif _shared_layers(s, o):
                reach = o.radius if o.kind == "via" else math.hypot(o.hx, o.hy)
                if _pt_seg_dist(o.a, s.a, s.b) <= hw + reach + _EPS:
                    return True
        # Same-net zones that already anchor an end do not need protecting
        # beyond the above; a zone anchored at an end is a modelled join.
        return False

    # -- queries -------------------------------------------------------------

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

    def has_cycle(self) -> bool:
        edges = sum(len(a) for a in self.adj) // 2
        comps = max(self.components(set()), default=-1) + 1
        return edges - len(self.nodes) + comps > 0

    def end_ok(self, i: int, removed: set[int]) -> bool:
        """Is each end of segment ``i`` still anchored to something alive?"""
        return all(any(k not in removed for k in ends) for ends in self.anch[i])

    def offcentre_end(self, i: int) -> bool:
        s = self.nodes[i]
        for end, ends in zip((s.a, s.b), self.anch[i], strict=True):
            for j in ends:
                o = self.nodes[j]
                if o.kind in ("via", "pad") and math.dist(end, o.a) > CENTRE_TOL:
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
            hx, hy, rr = _pad_geometry(pad)
            by_net[pad.net_number].append(
                _Node(
                    "pad",
                    layers,
                    a=_pad_position(fp, pad),
                    ref=pad,
                    label=f"{fp.reference}.{pad.number}",
                    hx=hx,
                    hy=hy,
                    rot=pad.rotation,
                    rr=rr,
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

    ``seed`` must keep both ends anchored after removal (i.e. sit on a cycle).
    Returns ``None`` when it does not, or when the removal would leave a
    protected segment with a free end.
    """
    if model.protected[seed]:
        return None
    trial = removed | {seed}
    if not model.end_ok(seed, trial):
        return None
    group = {seed}
    work = [j for j in model.adj[seed] if model.nodes[j].kind == "seg" and j not in trial]
    while work:
        j = work.pop()
        if j in group or j in removed:
            continue
        cur = removed | group
        if not model.end_ok(j, cur):
            if model.protected[j]:
                return None
            group.add(j)
            work.extend(k for k in model.adj[j] if model.nodes[k].kind == "seg" and k not in cur)
    return group


def _plan_net(model: _NetModel, net_number: int) -> NetPlan:
    plan = NetPlan(net_number=net_number)
    baseline = model.partition(set())
    removed: set[int] = set()
    seg_ids = [i for i, n in enumerate(model.nodes) if n.kind == "seg"]

    if model.has_cycle():

        def _key(group: set[int]) -> tuple[int, float]:
            # Prefer the branch with an off-centre end, then the longer detour.
            off = int(any(model.offcentre_end(g) for g in group))
            return (-off, -sum(math.dist(model.nodes[g].a, model.nodes[g].b) for g in group))

        for _ in range(4):
            cands = []
            for s in seg_ids:
                if s in removed or model.protected[s]:
                    continue
                group = _group_for(model, s, removed)
                if group:
                    cands.append((_key(group), s, group))
            cands.sort(key=lambda c: (c[0], c[1]))
            progressed = False
            for _k, s, group in cands:
                if s in removed:
                    continue
                if group & removed:
                    group = _group_for(model, s, removed)
                    if not group:
                        continue
                if model.partition(removed | group) != baseline:
                    continue
                off = any(model.offcentre_end(g) for g in group)
                removed |= group
                progressed = True
                for g in sorted(group):
                    plan.remove.append(model.nodes[g].index)
                    plan.reasons[model.nodes[g].index] = (
                        "redundant branch" if off else "redundant path or loop"
                    )
            if not progressed:
                break

    # Snap remaining off-centre ends onto same-net via/pad centres.
    for i in seg_ids:
        if i in removed or model.protected[i]:
            continue
        n = model.nodes[i]
        for which, e in (("start", 0), ("end", 1)):
            end = n.a if e == 0 else n.b
            target = None
            for j in model.anch[i][e]:
                o = model.nodes[j]
                if o.kind in ("via", "pad") and j not in removed:
                    d = math.dist(end, o.a)
                    if d > CENTRE_TOL and (target is None or d < target[0]):
                        target = (d, o.a)
            if target is None:
                continue
            affected = {i} | set(model.adj[i])
            saved = {k: (set(model.adj[k]), [set(x) for x in model.anch[k]]) for k in affected}
            old = (n.a, n.b)
            if e == 0:
                n.a = target[1]
            else:
                n.b = target[1]
            model.relink(i)
            ok = model.partition(removed) == baseline and model.end_ok(i, removed)
            ok = ok and all(
                (not any(saved[k][1][x]) or any(m not in removed for m in model.anch[k][x]))
                for k in affected
                if model.nodes[k].kind == "seg"
                for x in (0, 1)
            )
            if ok:
                plan.snaps.append((n.index, which, target[1]))
            else:
                n.a, n.b = old
                model.relink(i)
                for k, (adj_k, anch_k) in saved.items():
                    model.adj[k] = adj_k
                    model.anch[k] = anch_k
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
    # Snaps must hit the sexp tree before removal changes matching keys; do it
    # by position among ``(segment ...)`` children, which mirrors pcb.segments.
    seg_nodes = [c for c in pcb._sexp.children if not c.is_atom and c.name == "segment"]
    has_snaps = any(plan.snaps for plan in plans)
    if has_snaps and len(seg_nodes) != len(segs):
        raise RuntimeError(
            f"cannot snap segment ends: board has {len(seg_nodes)} (segment ...) nodes "
            f"but {len(segs)} parsed segments"
        )
    snapped = 0
    for plan in plans:
        for idx, which, (x, y) in plan.snaps:
            node = seg_nodes[idx].find(which)
            if node is None:
                raise RuntimeError(f"segment {idx} has no ({which} ...) node to snap")
            seg: Segment = segs[idx]
            if which == "start":
                seg.start = (x, y)
            else:
                seg.end = (x, y)
            node.set_value(0, x + ox)
            node.set_value(1, y + oy)
            snapped += 1
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
            trial_path = Path(tmp) / "trial.kicad_pcb"
            # DRC must run with the board's own rules (clearances, netclasses).
            src = Path(pcb_path)
            for ext in (".kicad_pro", ".kicad_dru"):
                if src.with_suffix(ext).exists():
                    shutil.copy(src.with_suffix(ext), Path(tmp) / f"trial{ext}")
            trial_path.write_text(src.read_text())
            baseline = _drc_signature(trial_path)
            if baseline is None:
                note = (
                    "kicad-cli not available (or DRC failed): DRC was NOT run, result is unverified"
                )
            else:
                drc_verified, note = True, "verified with kicad-cli pcb drc --refill-zones"

                def _trial_ok(group: list[NetPlan]) -> bool:
                    trial = PCB.load(pcb_path)
                    apply_plans(trial, group)
                    trial.save(trial_path)
                    after = _drc_signature(trial_path)
                    return after is not None and not _worse(after, baseline)

                # One DRC for everything; fall back to per-net only if it fails.
                if _trial_ok(plans):
                    accepted, rejected = list(plans), []
                else:
                    accepted, rejected = [], []
                    for plan in plans:
                        if _trial_ok([*accepted, plan]):
                            accepted.append(plan)
                        else:
                            rejected.append(plan)
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
