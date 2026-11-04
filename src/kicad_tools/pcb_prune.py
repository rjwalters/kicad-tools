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
* a track end joins a zone when it lies inside the zone outline -- but the
  *fill* can have voids there, so a zone join is never relied on to keep copper
  connected (it only marks the zone as a terminal).

Anything that could be an *unmodelled* join (a track whose end is free, whose
body touches other same-net copper without an endpoint join, or which crosses
a same-net zone / pad / via without an end inside it) is **protected**: it is
never removed or snapped, so an unseen join can only make the pass keep more
copper.  A removal is accepted only when every pad / zone terminal stays in
the same component as before, every live item that touched the removed copper
is still connected to the others through modelled track / via / pad joins
(so a join the model misses can never make a bridge look redundant), **and**
every remaining track end that was anchored stays anchored -- the pass never
creates an overlap-only joint.

A segment protected *only* because its body touches other same-net segments
(away from a shared endpoint) -- e.g. #8216's redundant spur that starts just
beside a via -- may still be removed when ``still_joined`` holds over its
endpoint neighbours **and** those body-touching neighbours, so the overlap
join is never what keeps anything connected.  Such segments are still never
snapped.

Collinear grid-step runs (issue #8236) are merged into one segment: a maximal
run of same-net / same-layer / same-width segments joined end to end in a
straight line, where every joint is a pure two-segment endpoint join (no via,
pad, zone or third segment there).  The merged copper is identical, so a run
is skipped if any member is hard-protected, or if its body touches same-net
copper outside the run that is not otherwise joined to it through modelled
track / via / pad joins (the contact could be an unmodelled join held by an
interior joint; issue #8278).  Merging applies after removals and snaps and
never changes which ends are anchored.
"""

from __future__ import annotations

import math
import shutil
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from kicad_tools.schema.pcb import PCB, Pad, Segment, _mm_atom

#: End-to-centre distance (mm) at/below which an end counts as centred.
_MICRO_BRIDGE = 0.01  # mm: a member this short is a bridge, not a step
CENTRE_TOL = 1e-3
#: Coordinate tolerance (mm) for two track endpoints to count as one point.
JOIN_TOL = 2e-6
#: Issue #8356: collinear same-width track ends closer than this (mm) are one
#: joint for the *model* (router float noise); below ``CENTRE_TOL``.
NEAR_JOIN_TOL = 7.5e-4
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


def _arc_reaches(arc: _Node, a: tuple[float, float], b: tuple[float, float], hw: float) -> bool:
    """Does the capsule ``a``-``b`` (half-width ``hw``) come within reach of the arc?

    ``arc.a`` / ``arc.radius`` give a cheap over-reaching disc; the arc itself
    is then sampled.
    """
    if _pt_seg_dist(arc.a, a, b) > arc.radius + hw + _EPS:
        return False
    (cx, cy), r, start, sweep = arc.ref.circular_geometry()
    hwa = arc.ref.width / 2.0
    n = max(2, int(abs(sweep) * r / 0.1) + 1)
    for k in range(n + 1):
        th = start + sweep * k / n
        if _pt_seg_dist((cx + r * math.cos(th), cy + r * math.sin(th)), a, b) <= hw + hwa + 0.05:
            return True
    return False


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
    kind: str  # "seg" | "via" | "pad" | "zone" | "arc" (protection-only)
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
    if shape == "rect":
        return hx, hy, 0.0
    # trapezoid / custom / unknown: the bounding rectangle can over-reach the
    # real copper, so use the inscribed circle (under-estimates; safe).
    r = min(hx, hy)
    return r, r, r


class _NetModel:
    """Endpoint-anchored connectivity graph of one net's copper."""

    def __init__(self, nodes: list[_Node]):
        self.nodes = nodes
        n = len(nodes)
        self.adj: list[set[int]] = [set() for _ in range(n)]
        #: Issue #8298: anchor-in-body joins (track end / via / pad centre inside
        #: another track's body).  Used only to *find* cycles; never to justify a
        #: removal, because other tools only model endpoint joins.
        self.badj: list[set[int]] = [set() for _ in range(n)]
        #: per segment: tracks whose body holds (start, end) without a shared endpoint
        self.banch: list[list[set[int]]] = [[set(), set()] for _ in range(n)]
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
        #: per segment: same-net segments whose body touches it without a
        #: shared endpoint (an unmodelled possible join)
        self.overlap: list[set[int]] = [set() for _ in range(n)]
        #: hard protection: never removed (free end, locked, zone/pad/via crossing)
        self.hard_protected = [self._is_protected(i) for i in range(n)]
        #: any protection, overlap-only included: never snapped
        self.protected = [self.hard_protected[i] or bool(self.overlap[i]) for i in range(n)]

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
        if a.kind == "arc" or b.kind == "arc":
            return  # copper arcs only protect; they are never a modelled join
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
            # Issue #8298: KiCad also connects a track whose end (anchor) lies
            # inside another same-layer track's body.
            if a.layers == b.layers:
                for x, y, ix, iy in ((a, b, i, j), (b, a, j, i)):
                    for ex, px in enumerate((x.a, x.b)):
                        if any(_same_pt(px, q) for q in (y.a, y.b)):
                            continue
                        if _pt_seg_dist(px, y.a, y.b) <= y.radius:
                            self.badj[ix].add(iy)
                            self.badj[iy].add(ix)
                            self.banch[ix][ex].add(iy)
            if hit:
                self.adj[i].add(j)
                self.adj[j].add(i)
            return
        if a.kind == "seg":
            # Issue #8298: a via / pad centre inside a track body is a join.
            if b.kind in ("via", "pad") and _pt_seg_dist(b.a, a.a, a.b) <= a.radius:
                self.badj[i].add(j)
                self.badj[j].add(i)
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
        for k in list(self.badj[i]):
            self.badj[k].discard(i)
            for ends in self.banch[k]:
                ends.discard(i)
        self.banch[i] = [set(), set()]
        self.badj[i] = set()
        self.adj[i] = set()
        self.anch[i] = [set(), set()]
        for k in range(len(self.nodes)):
            if k != i:
                self._link(i, k)

    # -- protection ---------------------------------------------------------

    def _is_protected(self, i: int) -> bool:
        """May segment ``i`` hide a join the model does not see?

        Returns ``True`` for *hard* protection.  A body that merely touches
        other same-net segments is recorded in ``self.overlap[i]`` instead:
        such a segment may still be removed when the connectivity holds over
        those body-touching neighbours too (issue #8216's spur beside a via).
        """
        s = self.nodes[i]
        if s.kind != "seg":
            return True
        if getattr(s.ref, "locked", False):
            return True
        if not (self.hard_anchors(i, 0) and self.hard_anchors(i, 1)):
            # A free end (could be an unseen join), or an end held only by a
            # zone outline (the fill may have a void there).
            return True
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
                        self.overlap[i].add(j)
            elif o.kind == "zone":
                if _shared_layers(s, o) and any(
                    _in_polygon(p, o.ref.polygon) for p in _seg_samples(s.a, s.b)
                ):
                    return True
            elif o.kind == "arc":
                # Unmodelled copper arc: a body contact may be what feeds it.
                if _shared_layers(s, o) and _arc_reaches(o, s.a, s.b, hw):
                    return True
            elif _shared_layers(s, o):
                reach = o.radius if o.kind == "via" else math.hypot(o.hx, o.hy)
                if _pt_seg_dist(o.a, s.a, s.b) <= hw + reach + _EPS:
                    return True
        # Same-net zones that already anchor an end do not need protecting
        # beyond the above; a zone anchored at an end is a modelled join.
        return False

    # -- queries -------------------------------------------------------------

    def components(self, removed: set[int], body: bool = False) -> list[int]:
        comp = [-1] * len(self.nodes)
        cid = 0
        for s in range(len(self.nodes)):
            if comp[s] != -1 or s in removed:
                continue
            stack = [s]
            comp[s] = cid
            while stack:
                u = stack.pop()
                for v in (self.adj[u] | self.badj[u]) if body else self.adj[u]:
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
        edges = sum(len(a | b) for a, b in zip(self.adj, self.badj, strict=True)) // 2
        comps = max(self.components(set(), body=True), default=-1) + 1
        return edges - len(self.nodes) + comps > 0

    def hard_anchors(self, i: int, end: int) -> set[int]:
        """Anchors of one end of segment ``i`` other than zones (fill may void)."""
        return {k for k in self.anch[i][end] | self.banch[i][end] if self.nodes[k].kind != "zone"}

    def end_ok(self, i: int, removed: set[int]) -> bool:
        """Is each end of segment ``i`` still anchored to something alive
        (a zone outline does not count)?"""
        return all(
            any(k not in removed and self.nodes[k].kind != "zone" for k in a | b)
            for a, b in zip(self.anch[i], self.banch[i], strict=True)
        )

    def clean_end(self, i: int, end: int, removed: set[int]) -> bool:
        """Does this end still meet another track end, or a via / pad centre?

        An end that only sits *inside* a via or pad, off its centre, is the
        overlap-style joint issue #8216 asks this pass to remove.
        """
        p = (self.nodes[i].a, self.nodes[i].b)[end]
        for k in self.anch[i][end]:
            if k in removed:
                continue
            o = self.nodes[k]
            if o.kind == "seg" or (o.kind in ("via", "pad") and math.dist(p, o.a) <= CENTRE_TOL):
                return True
        return False

    def degrades_ends(self, touched: set[int], before: set[int], after: set[int]) -> bool:
        """Would removing ``after - before`` leave a surviving track end
        off-centre-only that was clean before?"""
        for k in touched:
            if k in after or self.nodes[k].kind != "seg":
                continue
            for x in (0, 1):
                if self.clean_end(k, x, before) and not self.clean_end(k, x, after):
                    return True
        return False

    def centred_layers(self, k: int, removed: set[int]) -> set[str]:
        """Layers on which a live track end sits on the centre of via / pad ``k``."""
        o = self.nodes[k]
        out: set[str] = set()
        for j in self.adj[k]:
            if j in removed or self.nodes[j].kind != "seg":
                continue
            n = self.nodes[j]
            if math.dist(n.a, o.a) <= CENTRE_TOL or math.dist(n.b, o.a) <= CENTRE_TOL:
                out.update(n.layers)
        return out

    def decentres(self, touched: set[int], before: set[int], after: set[int]) -> bool:
        """Would removing ``after - before`` leave a via / pad that had a track
        end on its centre, on some layer, joined on that layer only by
        off-centre (overlap) ends?

        :meth:`degrades_ends` looks from the surviving track ends; this looks
        from the via / pad side, per layer (issue #8278: a diagonal stub to a
        via centre was removed as a loop branch, leaving a straight run whose
        end merely sits inside the via disc).
        """
        for k in touched:
            if k in after or self.nodes[k].kind not in ("via", "pad"):
                continue
            lost = self.centred_layers(k, before) - self.centred_layers(k, after)
            # Only a surviving track end resting inside the via / pad with no
            # track-end partner is the off-centre joint this guards against; a
            # track passing over it, or ending at a corner joint (a body
            # join, issue #8298), is not.
            if lost and not any(
                j not in after
                and self.nodes[j].kind == "seg"
                and any(
                    _contains(self.nodes[k], pt)
                    and not any(
                        self.nodes[q].kind == "seg" and q not in after for q in self.anch[j][x]
                    )
                    for x, pt in enumerate((self.nodes[j].a, self.nodes[j].b))
                )
                for j in self.adj[k]
            ):
                continue
            if any(
                j not in after and self.nodes[j].kind == "seg" and lost & set(self.nodes[j].layers)
                for j in self.adj[k]
            ):
                return True
        return False

    def still_joined(self, touched: set[int], removed: set[int], body: bool = False) -> bool:
        """Are all live nodes in ``touched`` in one component once ``removed``
        is gone, using only track / via / pad joins?

        This is the local redundancy test: it does not depend on the model
        seeing every terminal, so a join the model misses can only make a
        removal look *less* redundant.
        """
        zones = {k for k, nd in enumerate(self.nodes) if nd.kind == "zone"}
        live = [k for k in touched if k not in removed and k not in zones]
        if len(live) <= 1:
            return True
        comp = self.components(removed | zones, body=body)
        return len({comp[k] for k in live}) == 1

    def strands_end(self, group: set[int], removed: set[int]) -> bool:
        """Would removing ``group`` leave a surviving track end that rests on a
        removed body with no other non-zone anchor (a dangling end)?"""
        gone = removed | group
        for g in group:
            n = self.nodes[g]
            for k in (self.overlap[g] | self.adj[g]) - gone:
                o = self.nodes[k]
                if o.kind != "seg":
                    continue
                for x, p in enumerate((o.a, o.b)):
                    if _pt_seg_dist(p, n.a, n.b) <= n.radius + o.radius + _EPS and not (
                        self.hard_anchors(k, x) - gone
                    ):
                        return True
        return False

    def _via_layers(self, v: int, gone: set[int]) -> set[str]:
        """Layers a via is still joined on through live segments / pads."""
        out: set[str] = set()
        for k in self.adj[v] - gone:
            o = self.nodes[k]
            if o.kind == "seg":
                out.update(o.layers)
            elif o.kind == "pad":
                out.update(("*a", "*b") if "*" in o.layers else o.layers)
        return out

    def strands_via_layer(self, group: set[int], removed: set[int]) -> bool:
        """Would removing ``group`` leave a via joined on fewer than two layers
        that was joined on two or more (a ``via_dangling``, Issue #8298)?"""
        gone = removed | group
        for g in group:
            for v in self.adj[g]:
                if self.nodes[v].kind != "via" or v in gone:
                    continue
                before = len(self._via_layers(v, removed))
                if before >= 2 and len(self._via_layers(v, gone)) < 2:
                    return True
        return False

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
    for vidx, via in enumerate(pcb.vias):
        if via.net_number <= 0:
            continue
        layers = ["*"] if via.via_type is None else list(via.layers)
        by_net[via.net_number].append(
            _Node("via", layers, a=via.position, radius=via.size / 2.0, ref=via, index=vidx)
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
    for arc in pcb.arcs:
        if arc.net_number <= 0 or arc.net_number not in by_net or not _is_cu(arc.layer):
            continue
        centre, rad, _st, _sw = arc.circular_geometry()
        by_net[arc.net_number].append(
            _Node("arc", [arc.layer], a=centre, radius=rad + arc.width / 2.0, ref=arc)
        )
    for nodes in by_net.values():
        _snap_near_joints(nodes)
    return {net: _NetModel(nodes) for net, nodes in by_net.items()}


def _snap_near_joints(nodes: list[_Node]) -> None:
    """Issue #8356: make sub-micron end mismatches of straight-through joints exact.

    Only the in-memory model changes: two same-layer, same-width segment ends
    within ``NEAR_JOIN_TOL`` (but not ``JOIN_TOL``) that continue in one
    straight line are pulled together so collinear-run merging sees a joint
    instead of a body overlap.  Output geometry changes only via the merge.
    """
    segs = [n for n in nodes if n.kind == "seg"]
    cell = 1e-3
    grid: dict[tuple[int, int], list[tuple[_Node, int]]] = defaultdict(list)
    for n in segs:
        for e in (0, 1):
            p = _end_pt(n, e)
            grid[(int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell)))].append((n, e))
    for n in segs:
        for e in (0, 1):
            p = _end_pt(n, e)
            cx, cy = int(math.floor(p[0] / cell)), int(math.floor(p[1] / cell))
            near = [
                (o, f)
                for dx in (-1, 0, 1)
                for dy in (-1, 0, 1)
                for o, f in grid.get((cx + dx, cy + dy), ())
                if o is not n
            ]
            near = [
                (o, f)
                for o, f in near
                if max(abs(_end_pt(o, f)[0] - p[0]), abs(_end_pt(o, f)[1] - p[1])) <= NEAR_JOIN_TOL
            ]
            if len(near) != 1:
                continue  # none, or ambiguous (a branch point): leave alone
            o, f = near[0]
            q = _end_pt(o, f)
            if _same_pt(p, q) or n.layers != o.layers or abs(n.radius - o.radius) > 1e-9:
                continue
            vi = (_end_pt(n, 1 - e)[0] - p[0], _end_pt(n, 1 - e)[1] - p[1])
            vj = (_end_pt(o, 1 - f)[0] - q[0], _end_pt(o, 1 - f)[1] - q[1])
            li, lj = math.hypot(*vi), math.hypot(*vj)
            if li < _EPS or lj < _EPS:
                continue
            if (vi[0] * vj[0] + vi[1] * vj[1]) >= 0:
                continue  # not straight-through
            if abs(vi[0] * vj[1] - vi[1] * vj[0]) / (li * lj) > 1e-3:
                continue
            _set_end(o, f, p)  # o's end moves; n's own end is handled from o's side


# ---------------------------------------------------------------------------
# Planning
# ---------------------------------------------------------------------------


@dataclass
class NetPlan:
    """Planned edits for one net, by index into ``pcb.segments``."""

    net_number: int
    net_name: str = ""
    remove: list[int] = field(default_factory=list)
    #: vias (index into ``pcb.vias``) a loop cut left with nothing attached
    remove_vias: list[int] = field(default_factory=list)
    #: (segment index, "start"|"end", new xy)
    snaps: list[tuple[int, str, tuple[float, float]]] = field(default_factory=list)
    reasons: dict[int, str] = field(default_factory=dict)
    #: collinear merges: (kept segment index, "start"|"end", new xy); the other
    #: run members are listed in ``remove``
    extends: list[tuple[int, str, tuple[float, float]]] = field(default_factory=list)
    #: loops on a declared net left in place because every segment is at/above
    #: ``--wide`` (issue #8313): ``{"segments": [...], "min_width": mm}``
    uncut_loops: list[dict[str, Any]] = field(default_factory=list)

    @property
    def empty(self) -> bool:
        return not self.remove and not self.remove_vias and not self.snaps and not self.extends


def _group_for(model: _NetModel, seed: int, removed: set[int]) -> set[int] | None:
    """Removal group for ``seed``: the segment plus the chain it orphans.

    ``seed`` must keep both ends anchored after removal (i.e. sit on a cycle).
    Returns ``None`` when it does not, or when the removal would leave a
    protected segment with a free end.
    """
    if model.hard_protected[seed]:
        return None
    trial = removed | {seed}
    if not model.end_ok(seed, trial):
        return None
    group = {seed}
    work = [
        j
        for j in model.adj[seed] | model.badj[seed]
        if model.nodes[j].kind == "seg" and j not in trial
    ]
    while work:
        j = work.pop()
        if j in group or j in removed:
            continue
        cur = removed | group
        if not model.end_ok(j, cur):
            if model.hard_protected[j]:
                return None
            group.add(j)
            work.extend(
                k
                for k in model.adj[j] | model.badj[j]
                if model.nodes[k].kind == "seg" and k not in cur
            )
    return group


def _end_pt(n: _Node, e: int) -> tuple[float, float]:
    return n.a if e == 0 else n.b


def _set_end(n: _Node, e: int, p: tuple[float, float]) -> None:
    if e == 0:
        n.a = p
    else:
        n.b = p


def _try_snap(
    model: _NetModel,
    plan: NetPlan,
    i: int,
    e: int,
    removed: set[int],
    baseline: frozenset[frozenset[int]],
) -> None:
    """Snap end ``e`` of segment ``i`` -- and every track end sharing that
    point -- onto the nearest anchoring via/pad centre, if that is safe.

    Moving the whole coincident group keeps the shared joint shared; moving
    one end alone would strand its neighbours off-centre.
    """
    n = model.nodes[i]
    p = _end_pt(n, e)
    target = None
    for j in model.anch[i][e]:
        o = model.nodes[j]
        if o.kind in ("via", "pad") and j not in removed:
            d = math.dist(p, o.a)
            if d > CENTRE_TOL and (target is None or d < target[0]):
                target = (d, o.a)
    if target is None:
        return
    c = target[1]
    movers = [(i, e)]
    for k in model.anch[i][e]:
        if k in removed or model.nodes[k].kind != "seg":
            continue
        for x in (0, 1):
            if _same_pt(_end_pt(model.nodes[k], x), p):
                movers.append((k, x))
    for k, x in movers:
        if model.protected[k]:
            return  # a sharer may hide a join: leave the joint alone
        if math.dist(_end_pt(model.nodes[k], 1 - x), c) <= CENTRE_TOL:
            return  # would collapse a segment to zero length
    for k, x in movers:
        nk = model.nodes[k]
        new = (c, nk.b) if x == 0 else (nk.a, c)
        if _tilts(nk, *new):
            return  # a snap must not take a track off the 45-degree set
    moved = {k for k, _ in movers}
    touched = set(moved)
    for k in moved:
        touched |= model.adj[k]
    saved = {k: (set(model.adj[k]), [set(a) for a in model.anch[k]]) for k in touched}
    old = {k: (model.nodes[k].a, model.nodes[k].b) for k in moved}
    clean_before = {
        (k, x): model.clean_end(k, x, removed)
        for k in touched
        if k not in moved and model.nodes[k].kind == "seg"
        for x in (0, 1)
    }
    for k, x in movers:
        _set_end(model.nodes[k], x, c)
    for k in moved:
        model.relink(k)
    ok = (
        model.partition(removed) == baseline
        and all(model.end_ok(k, removed) for k in moved)
        and model.still_joined(touched, removed)
        # no neighbour's end may be left off-centre-only (#8216)
        and not any(
            was and not model.clean_end(k, x, removed) for (k, x), was in clean_before.items()
        )
        # every hard-anchored end stays hard-anchored
        and all(
            not any(model.nodes[m].kind != "zone" for m in saved[k][1][x])
            or bool(model.hard_anchors(k, x) - removed)
            for k in touched
            if model.nodes[k].kind == "seg"
            for x in (0, 1)
        )
    )
    if ok:
        for k, x in movers:
            plan.snaps.append((model.nodes[k].index, ("start", "end")[x], c))
        return
    for k in moved:
        model.nodes[k].a, model.nodes[k].b = old[k]
        model.relink(k)
    for k, (adj_k, anch_k) in saved.items():
        model.adj[k] = adj_k
        model.anch[k] = anch_k


def _aligned(a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Is ``a -> b`` on the 0/45/90/135 set as the fleet census reads it
    (4-decimal serialized coordinates)?  Zero length counts as aligned."""
    from kicad_tools.router.quantize import _is_quantum_aligned

    return _is_quantum_aligned(round(b[0], 4) - round(a[0], 4), round(b[1], 4) - round(a[1], 4))


def _tilts(n: _Node, a: tuple[float, float], b: tuple[float, float]) -> bool:
    """Would re-ending segment ``n`` as ``a -> b`` take an on-angle track off
    the 45-degree set (issue #8278, the fleet angle census)?"""
    return _aligned(n.a, n.b) and not _aligned(a, b)


#: Max perpendicular deviation (mm) for a run to count as one straight line.
_COLLINEAR_TOL = 1e-6


def _joint_partner(model: _NetModel, i: int, e: int, removed: set[int]) -> tuple[int, int] | None:
    """Return ``(j, f)`` when end ``e`` of segment ``i`` is a pure two-segment
    straight-through join with end ``f`` of ``j`` (same layer / width)."""
    if i in removed:
        return None
    anchors = model.anch[i][e] - removed
    if len(anchors) != 1:
        return None
    j = next(iter(anchors))
    n, o = model.nodes[i], model.nodes[j]
    if o.kind != "seg" or j in removed or n.layers != o.layers:
        return None
    if abs(n.radius - o.radius) > 1e-9:
        return None
    p = _end_pt(n, e)
    f = next((x for x in (0, 1) if _same_pt(_end_pt(o, x), p)), None)
    if f is None or model.anch[j][f] - removed != {i}:
        return None
    # The other ends must not also meet (a degenerate two-segment loop).
    qi, qj = _end_pt(n, 1 - e), _end_pt(o, 1 - f)
    vi, vj = (qi[0] - p[0], qi[1] - p[1]), (qj[0] - p[0], qj[1] - p[1])
    li, lj = math.hypot(*vi), math.hypot(*vj)
    if li < _EPS or lj < _EPS:
        return None
    if (vi[0] * vj[0] + vi[1] * vj[1]) >= 0 or abs(vi[0] * vj[1] - vi[1] * vj[0]) / lj > (
        _COLLINEAR_TOL
    ):
        return None
    return j, f


def _existing_copy(
    model: _NetModel,
    s: int,
    start: tuple[float, float],
    far: tuple[float, float],
    members: set[int],
    gone: set[int],
) -> int | None:
    """A live segment outside the run that already spans ``start``-``far``
    on the same layer with the same width (issue #8278: a 0.4 mm track laid
    back over four 0.1 mm steps).  Merging would create an exact duplicate,
    which KiCad reports as a dangling track.  Returns ``-1`` when such a copy
    exists but would be left with an unanchored end without the run (the two
    form a closed loop to nowhere): the run must then stay as it is."""
    n = model.nodes[s]
    for m in members:
        for k in model.adj[m] | model.overlap[m]:
            if k in members or k in gone:
                continue
            o = model.nodes[k]
            if o.kind != "seg" or o.layers != n.layers or abs(o.radius - n.radius) > 1e-9:
                continue
            if (_same_pt(o.a, start) and _same_pt(o.b, far)) or (
                _same_pt(o.a, far) and _same_pt(o.b, start)
            ):
                return k if model.end_ok(k, gone | members) else -1
    return None


def _plan_collinear_merges(model: _NetModel, plan: NetPlan, removed: set[int]) -> set[int]:
    """Merge maximal straight runs of segments into their first member.

    Returns the model ids of every segment in a merged run.
    """
    merged: set[int] = set()
    seg_ids = [
        i
        for i, n in enumerate(model.nodes)
        if n.kind == "seg" and i not in removed and not model.hard_protected[i]
    ]
    eligible = set(seg_ids)
    link: dict[tuple[int, int], tuple[int, int]] = {}
    for i in seg_ids:
        for e in (0, 1):
            hit = _joint_partner(model, i, e, removed)
            if hit is not None and hit[0] in eligible:
                link[(i, e)] = hit
    visited: set[int] = set()
    for s in seg_ids:
        if s in visited:
            continue
        ends = [e for e in (0, 1) if (s, e) in link]
        if len(ends) != 1:
            continue  # not the end of a run (no joints, or mid-run)
        out = ends[0]
        run, cur = [s], s
        while (cur, out) in link:
            cur, f = link[(cur, out)]
            run.append(cur)
            out = 1 - f
        visited.update(run)
        far = _end_pt(model.nodes[cur], out)
        start = _end_pt(model.nodes[s], 1 - ends[0])
        members = set(run)
        # Merging drops only the run's interior joints; the copper itself is
        # unchanged.  Same-net copper that touches the run's body may be held
        # by one of those joints (an unmodelled join), so the run is skipped
        # unless every such neighbour is already joined to the run through
        # modelled track / via / pad joins -- then a lost contact is redundant
        # (issue #8278: the diagonal corner of a grid-step route grazes the
        # first few 0.1 mm steps of the straight run it turns into).
        outside = set().union(*(model.overlap[m] for m in run)) - members - removed
        # A micro-bridge member (~um, issue #8298) holds a neighbour only at
        # a coincidence the optimizer does not model: leave such runs alone.
        if outside and any(
            math.dist(model.nodes[m].a, model.nodes[m].b) < _MICRO_BRIDGE for m in run
        ):
            continue
        if outside and not model.still_joined(outside | {s}, removed):
            continue
        if any(
            _pt_seg_dist(_end_pt(model.nodes[m], x), start, far) > _COLLINEAR_TOL
            for m in run
            for x in (0, 1)
        ):
            continue
        if (
            _tilts(model.nodes[s], start, far)
            or any(_aligned(model.nodes[m].a, model.nodes[m].b) for m in run)
            and not _aligned(start, far)
        ):
            continue
        copy = _existing_copy(model, s, start, far, members, removed)
        if copy == -1:
            continue
        merged |= members
        if copy is not None:
            for m in run:
                plan.remove.append(model.nodes[m].index)
                plan.reasons[model.nodes[m].index] = "collinear run duplicates a track"
            continue
        plan.extends.append((model.nodes[s].index, ("start", "end")[ends[0]], far))
        for m in run[1:]:
            plan.remove.append(model.nodes[m].index)
            plan.reasons[model.nodes[m].index] = "collinear run merged"
    return merged


def _anchored_partner(
    model: _NetModel, i: int, e: int, removed: set[int]
) -> tuple[int, int] | None:
    """Like :func:`_joint_partner`, but the joint may also sit on same-net
    vias, pads or zones: exactly one other *segment* ends there, straight
    through, with the same layer and width."""
    if i in removed:
        return None
    segs = {k for k in model.anch[i][e] - removed if model.nodes[k].kind == "seg"}
    if len(segs) != 1:
        return None
    j = next(iter(segs))
    n, o = model.nodes[i], model.nodes[j]
    if n.layers != o.layers or abs(n.radius - o.radius) > 1e-9:
        return None
    p = _end_pt(n, e)
    f = next((x for x in (0, 1) if _same_pt(_end_pt(o, x), p)), None)
    if f is None:
        return None
    if {k for k in model.anch[j][f] - removed if model.nodes[k].kind == "seg"} != {i}:
        return None
    qi, qj = _end_pt(n, 1 - e), _end_pt(o, 1 - f)
    vi, vj = (qi[0] - p[0], qi[1] - p[1]), (qj[0] - p[0], qj[1] - p[1])
    li, lj = math.hypot(*vi), math.hypot(*vj)
    if li < _EPS or lj < _EPS:
        return None
    if (vi[0] * vj[0] + vi[1] * vj[1]) >= 0 or abs(vi[0] * vj[1] - vi[1] * vj[0]) / lj > (
        _COLLINEAR_TOL
    ):
        return None
    return j, f


def _merge_unsafe_member(model: _NetModel, i: int) -> bool:
    """Locked, or touching a copper arc (arcs are never modelled joins, so a
    lost interior joint could be what fed one)."""
    n = model.nodes[i]
    if getattr(n.ref, "locked", False):
        return True
    return any(
        o.kind == "arc" and _shared_layers(n, o) and _arc_reaches(o, n.a, n.b, n.radius)
        for o in model.nodes
    )


def _plan_anchored_merges(
    model: _NetModel, plan: NetPlan, removed: set[int], done: set[int]
) -> None:
    """Merge straight runs whose joints also sit on vias / pads / zones.

    The pure pass (:func:`_plan_collinear_merges`) leaves these alone.  The
    merged copper is identical; only the run's interior anchor points go.  So
    each candidate is merged on the model and kept only when, with the run
    collapsed to one segment (issue #8278):

    * the pad / zone terminal partition is unchanged;
    * everything the run touched (endpoint, via / pad / zone and body
      neighbours) is still joined to it through modelled track / via / pad
      joins -- a contact made only at an interior joint is then redundant;
    * every zone that held a run end still holds one of the merged ends;
    * no via / pad loses a track end on its centre on any layer, and no
      neighbouring track end that met a track end or a centre loses it.

    A failing candidate is restored exactly.
    """
    seg_ids = [
        i
        for i, n in enumerate(model.nodes)
        if n.kind == "seg" and i not in removed and i not in done
    ]
    eligible = set(seg_ids)
    link: dict[tuple[int, int], tuple[int, int]] = {}
    for i in seg_ids:
        for e in (0, 1):
            hit = _anchored_partner(model, i, e, removed)
            if hit is not None and hit[0] in eligible:
                link[(i, e)] = hit
    gone = set(removed)
    visited: set[int] = set()
    for s in seg_ids:
        if s in visited:
            continue
        ends = [e for e in (0, 1) if (s, e) in link]
        if len(ends) != 1:
            continue
        out = ends[0]
        run, cur = [s], s
        while (cur, out) in link:
            cur, f = link[(cur, out)]
            run.append(cur)
            out = 1 - f
        visited.update(run)
        members = set(run)
        far = _end_pt(model.nodes[cur], out)
        start = _end_pt(model.nodes[s], 1 - ends[0])
        if any(
            _pt_seg_dist(_end_pt(model.nodes[m], x), start, far) > _COLLINEAR_TOL
            for m in run
            for x in (0, 1)
        ):
            continue
        if any(_merge_unsafe_member(model, m) for m in run):
            continue
        if any(math.dist(model.nodes[m].a, model.nodes[m].b) < _MICRO_BRIDGE for m in run):
            continue  # a micro-bridge joint stays for optimize-traces (#8298)
        if any(_aligned(model.nodes[m].a, model.nodes[m].b) for m in run) and not _aligned(
            start, far
        ):
            continue  # a merge must not take an on-angle run off the 45-degree set
        touched: set[int] = set()
        for m in run:
            touched |= model.adj[m] | model.overlap[m]
        touched -= members | gone
        zones = {k for k in touched if model.nodes[k].kind == "zone"}
        centred = {
            k: model.centred_layers(k, gone)
            for k in touched
            if model.nodes[k].kind in ("via", "pad")
        }
        clean = {
            (k, x)
            for k in touched
            if model.nodes[k].kind == "seg"
            for x in (0, 1)
            if model.clean_end(k, x, gone)
        }
        baseline = model.partition(gone)
        after = gone | members - {s}
        node = model.nodes[s]
        old = (node.a, node.b)
        _set_end(node, ends[0], far)
        model.relink(s)
        ok = (
            model.partition(after) == baseline
            and model.still_joined((touched - zones) | {s}, after)
            and zones <= model.adj[s]
            and all(model.centred_layers(k, after) >= lay for k, lay in centred.items())
            and all(model.clean_end(k, x, after) for k, x in clean)
        )
        if not ok:
            node.a, node.b = old
            model.relink(s)
            continue
        copy = _existing_copy(model, s, start, far, members, gone)
        if copy is not None:
            # The collapsed run is joined exactly where the existing track
            # is, so dropping it as well leaves the same joins -- unless that
            # track would be left with a free end (copy == -1): keep the run.
            node.a, node.b = old
            model.relink(s)
            if copy == -1:
                continue
            gone = gone | members
            for m in run:
                plan.remove.append(model.nodes[m].index)
                plan.reasons[model.nodes[m].index] = "collinear run duplicates a track"
            continue
        gone = after
        plan.extends.append((node.index, ("start", "end")[ends[0]], far))
        for m in run[1:]:
            plan.remove.append(model.nodes[m].index)
            plan.reasons[model.nodes[m].index] = "collinear run merged"


#: Default width (mm) at/above which a declared-net segment is never cut.
DEFAULT_WIDE_MM = 0.5


def _cut_ok(model: _NetModel, group: set[int], removed: set[int], baseline) -> bool:
    """The shared acceptance test for removing ``group`` (terminals unchanged,
    neighbours still joined, no stranded, degraded or de-centred ends).

    The via / pad-side :meth:`_NetModel.decentres` check applies here exactly
    as in :func:`_plan_net`'s generic loop pass (issue #8358): a via-closed
    loop cut must not leave a via held on a layer only by an off-centre end.
    """
    touched = {k for g in group for k in model.adj[g] | model.overlap[g]} - group
    return not (
        model.partition(removed | group) != baseline
        or not model.still_joined(touched, removed | group)
        or model.strands_end(group, removed)
        or model.degrades_ends(touched, removed, removed | group)
        or model.decentres(touched, removed, removed | group)
    )


def _width(model: _NetModel, i: int) -> float:
    return model.nodes[i].radius * 2.0


def _ampacity_ok(model: _NetModel, group: set[int], required: float) -> bool:
    """Net-relative ``--wide`` (issue #8368): a cut is allowed only when every
    segment still attached where the cut ends (the parallel branch that now
    carries the current) is at least ``required`` mm wide.  ``required`` is
    the IPC-2221 width of the net's declared current; ``0`` accepts any.
    """
    if required <= 0.0:
        return True
    near: set[int] = set()
    for g in group:
        near |= model.adj[g] | model.overlap[g]
        for ends in model.anch[g]:
            for a in ends:
                near.add(a)
                near |= model.adj[a]
    near -= group
    return all(_width(model, k) >= required for k in near if model.nodes[k].kind == "seg")


def _dangling_remnants(
    model: _NetModel, seeds: set[int], removed: set[int], wide: float
) -> set[int]:
    """Segments left dead-ended by a cut, walking outward from ``seeds``.

    A cut inside a via-closed loop leaves the rest of that jog hanging off one
    end (e.g. ``via -> B.Cu run -> via -> pad``); such a stub keeps the run
    ambiguous for the current-path audit, so it goes with the cut.  An end is
    dead when it rests on nothing alive, or only on vias nothing else touches.
    Only narrow, unprotected segments are taken.
    """
    gone: set[int] = set()
    work = [k for g in seeds for k in model.adj[g] | model.overlap[g]]
    # a dead via may expose the segment on its far side: walk through vias too
    seen: set[int] = set()
    while work:
        k = work.pop()
        if k in seen:
            continue
        seen.add(k)
        node = model.nodes[k]
        if node.kind == "via":
            work.extend(model.adj[k])
            continue
        if node.kind != "seg" or k in removed or k in gone:
            continue
        if model.hard_protected[k] or _width(model, k) >= wide:
            continue
        dead = False
        for ends in model.anch[k]:
            alive = [
                a
                for a in ends
                if a not in removed
                and a not in gone
                and model.nodes[a].kind != "zone"
                and not (model.nodes[a].kind == "via" and not (model.adj[a] - removed - gone - {k}))
            ]
            if not alive:
                dead = True
        if dead:
            gone.add(k)
            work.extend(model.adj[k])
    return gone


def _attach_layers(model: _NetModel, live: set[int]) -> int:
    """How many copper layers the live attachments of a via cover (a
    through/all-layer pad counts as two)."""
    layers: set[str] = set()
    for k in live:
        for lyr in model.nodes[k].layers:
            if lyr == "*":
                return 2
            layers.add(lyr)
    return len(layers)


def _via_effects(model: _NetModel, full: set[int], removed: set[int]) -> set[int] | None:
    """Vias a cut of ``full`` leaves with nothing attached (to be removed too),
    or ``None`` when the cut would strand a via on a single layer.

    A via nothing touches is not a layer transition, so dropping it is safe.  A
    via that keeps exactly one attachment layer after having had two is a
    ``via_dangling`` finding, so such a cut is unacceptable (issue #8313).
    """
    gone = removed | full
    orphans: set[int] = set()
    for k in {v for g in full for v in model.adj[g]}:
        if model.nodes[k].kind != "via" or k in gone:
            continue
        before = model.adj[k] - removed
        live = model.adj[k] - gone
        if not live:
            orphans.add(k)
        elif _attach_layers(model, before) >= 2 > _attach_layers(model, live):
            return None
    return orphans


def _expand_cut(
    model: _NetModel, group: set[int], removed: set[int], baseline, wide: float
) -> set[int] | None:
    """``group`` plus dangling remnants and orphaned vias, fully re-checked
    (partition, joins, ends, via layers); ``None`` when no variant is safe."""
    wider = group | _dangling_remnants(model, group, removed | group, wide)
    for cand in (wider, group) if wider != group else (group,):
        orphans = _via_effects(model, cand, removed)
        if orphans is None:
            continue
        full = cand | orphans
        if _cut_ok(model, full, removed, baseline):
            return full
    return None


def _plan_declared_loops(
    model: _NetModel,
    plan: NetPlan,
    removed: set[int],
    baseline,
    wide: float,
    required: float = 0.0,
) -> None:
    """Break loops (same-layer or closed through vias) on a declared net.

    Each pass cuts, among the segments whose removal is safe, the narrowest one
    below ``wide``; repeats until none is left.  A loop all of whose members
    are at/above ``wide`` is not cut but recorded in ``plan.uncut_loops``.
    """
    seg_ids = [i for i, n in enumerate(model.nodes) if n.kind == "seg"]
    for _ in range(len(seg_ids) + 1):
        best = None
        for s in seg_ids:
            if s in removed or model.hard_protected[s] or _width(model, s) >= wide:
                continue
            group = _group_for(model, s, removed)
            if not group or any(_width(model, g) >= wide for g in group):
                continue
            if not _ampacity_ok(model, group, required):
                continue
            full = _expand_cut(model, group, removed, baseline, wide)
            if full is None:
                continue
            key = (_width(model, s), s)
            if best is None or key < best[0]:
                best = (key, full)
        if best is None:
            break
        group = best[1]
        removed |= group
        for g in sorted(group):
            if model.nodes[g].kind == "via":
                plan.remove_vias.append(model.nodes[g].index)
                continue
            plan.remove.append(model.nodes[g].index)
            plan.reasons[model.nodes[g].index] = "declared-net loop (narrowest below --wide)"

    # Report the loops that were deliberately left: cuttable but for width.
    stuck = set()
    for s in seg_ids:
        if s in removed or model.hard_protected[s]:
            continue
        group = _group_for(model, s, removed)
        if (
            group
            and _cut_ok(model, group, removed, baseline)
            and _via_effects(model, group, removed) is not None
        ):
            stuck.add(s)
    relative = math.isinf(wide)
    while stuck:
        comp = {stuck.pop()}
        work = list(comp)
        while work:
            u = work.pop()
            for v in model.adj[u] & stuck:
                stuck.discard(v)
                comp.add(v)
                work.append(v)
        plan.uncut_loops.append(
            {
                "segments": sorted(
                    f"{model.nodes[i].a}->{model.nodes[i].b} {model.nodes[i].layers[0]}"
                    for i in comp
                ),
                "min_width": round(min(_width(model, i) for i in comp), 6),
                "reason": (
                    f"cutting would leave a branch narrower than the declared "
                    f"current needs ({required:.3g} mm)"
                    if relative
                    else "all segments >= --wide"
                ),
            }
        )


def _plan_net(
    model: _NetModel,
    net_number: int,
    *,
    declared: bool = False,
    wide: float = DEFAULT_WIDE_MM,
    required: float = 0.0,
) -> NetPlan:
    plan = NetPlan(net_number=net_number)
    baseline = model.partition(set())
    removed: set[int] = set()
    seg_ids = [i for i, n in enumerate(model.nodes) if n.kind == "seg"]

    # The declared-net pass shares the partition baseline, so it needs the same
    # two-terminal floor as the generic pass below (Issue #8333): with fewer
    # terminals the guard is vacuous and every loop would be deleted.
    if declared and len(model.terminals) >= 2 and model.has_cycle():
        _plan_declared_loops(model, plan, removed, baseline, wide, required)

    # A net with fewer than two terminals has at most one partition group, so
    # the "partition unchanged" guard is vacuous and a loop would be deleted
    # wholesale, leaving the net with no copper (Issue #8333).
    if len(model.terminals) >= 2 and model.has_cycle():

        def _key(group: set[int]) -> tuple[int, float]:
            # Prefer the branch with an off-centre end, then the longer detour.
            off = int(any(model.offcentre_end(g) for g in group))
            return (-off, -sum(math.dist(model.nodes[g].a, model.nodes[g].b) for g in group))

        for _ in range(4):
            cands = []
            for s in seg_ids:
                if s in removed or model.hard_protected[s]:
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
                if declared and any(_width(model, g) >= wide for g in group):
                    continue  # never cut deliberate wide copper on a declared net
                if declared and not _ampacity_ok(model, group, required):
                    continue
                # Endpoint neighbours *and* body-touching neighbours: an
                # unmodelled overlap join must not be what holds anything.
                strict = {k for g in group for k in model.adj[g]} - group
                touched = {k for g in group for k in model.adj[g] | model.overlap[g]} - group
                if (
                    model.partition(removed | group) != baseline
                    # Endpoint joins alone must keep the neighbours joined (a
                    # removal may never leave a joint held only by overlap);
                    # anchor-in-body joins may only add to that (Issue #8298).
                    or not model.still_joined(strict, removed | group)
                    or not model.still_joined(touched, removed | group, body=True)
                    or model.strands_end(group, removed)
                    or model.strands_via_layer(group, removed)
                    or model.degrades_ends(touched, removed, removed | group)
                    or model.decentres(touched, removed, removed | group)
                ):
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
        for e in (0, 1):
            _try_snap(model, plan, i, e, removed, baseline)

    done = _plan_collinear_merges(model, plan, removed)
    _plan_anchored_merges(model, plan, removed, done)
    return plan


def plan_prune(
    pcb: PCB,
    *,
    declared_nets: set[str] | None = None,
    wide: float | None = DEFAULT_WIDE_MM,
    uncut_out: list[dict[str, Any]] | None = None,
    required_widths: dict[str, float] | None = None,
) -> list[NetPlan]:
    """Compute per-net prune/snap plans without touching the board.

    ``declared_nets`` (net names from ``--current-paths``) enables the
    loop-breaking pass on those nets only; loops left because every segment is
    at/above ``wide`` are appended to ``uncut_out`` (issue #8313).

    ``wide=None`` is the net-relative mode (issue #8368): the narrowest member
    of each loop is cut whatever its width, provided every branch left joined
    to the cut is at least ``required_widths[net]`` mm (the IPC-2221 width of
    the net's declared current).  An explicit ``wide`` keeps absolute semantics.
    """
    eff_wide = math.inf if wide is None else wide
    names = (
        {net.number: net.name for net in getattr(pcb, "nets", {}).values()}
        if isinstance(getattr(pcb, "nets", None), dict)
        else {}
    )
    plans = []
    for net_number, model in sorted(_build_net_models(pcb).items()):
        seg0 = next((n.ref for n in model.nodes if n.kind == "seg"), None)
        net_name = names.get(net_number) or (seg0.net_name if seg0 else "")
        declared = declared_nets is not None and net_name in declared_nets
        plan = _plan_net(
            model,
            net_number,
            declared=declared,
            wide=eff_wide,
            required=(required_widths or {}).get(net_name, 0.0) if wide is None else 0.0,
        )
        if uncut_out is not None:
            uncut_out.extend({"net": net_name or net_number, **u} for u in plan.uncut_loops)
        if not plan.empty:
            plan.net_name = net_name
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
    has_snaps = any(plan.snaps or plan.extends for plan in plans)
    if has_snaps and len(seg_nodes) != len(segs):
        raise RuntimeError(
            f"cannot snap segment ends: board has {len(seg_nodes)} (segment ...) nodes "
            f"but {len(segs)} parsed segments"
        )
    snapped = 0
    for plan in plans:
        for idx, which, (x, y) in [*plan.snaps, *plan.extends]:
            node = seg_nodes[idx].find(which)
            if node is None:
                raise RuntimeError(f"segment {idx} has no ({which} ...) node to snap")
            seg: Segment = segs[idx]
            if which == "start":
                seg.start = (x, y)
            else:
                seg.end = (x, y)
            # Written on KiCad's nm grid: a bare float goes through the
            # serializer's 6-significant-digit format, which turned 133.4125
            # into 133.412 and opened a 0.5 um gap at the joint (issue #8278).
            while len(node.children) < 2:
                node.children.append(_mm_atom(0.0))
            node.children[0] = _mm_atom(x + ox)
            node.children[1] = _mm_atom(y + oy)
        snapped += len(plan.snaps)
    to_remove = [segs[i] for plan in plans for i in plan.remove]
    vias = list(pcb.vias)
    via_remove = [vias[i] for plan in plans for i in plan.remove_vias]
    removed = pcb.remove_segments(to_remove) if to_remove else 0
    vias_removed = pcb.remove_vias(via_remove) if via_remove else 0
    stats = {"segments_removed": removed, "ends_snapped": snapped}
    if vias_removed:  # key appears only when a loop cut orphaned vias (issue #8313)
        stats["vias_removed"] = vias_removed
    return stats


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
        # Key on the rule and nets as well as the type: a custom ``.kicad_dru``
        # clearance error between two nets must not be masked by an unrelated
        # clearance error that the edit happened to fix (issue #8323).
        sig[
            (
                v.type_str,
                v.severity.value if hasattr(v.severity, "value") else str(v.severity),
                v.rule or "",
                tuple(sorted(n for n in v.nets if n)),
            )
        ] += 1
    sig[("unconnected", "error", "", ())] += len(report.unconnected_items)
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
    uncut_loops: list[dict[str, Any]] = field(default_factory=list)


def prune_board(
    pcb_path: Path,
    *,
    verify_drc: bool = True,
    apply: bool = True,
    declared_nets: set[str] | None = None,
    wide: float | None = DEFAULT_WIDE_MM,
    required_widths: dict[str, float] | None = None,
) -> tuple[PCB, PruneResult]:
    """Plan (and optionally apply) pruning for ``pcb_path``.

    Returns the board (edited in memory when ``apply``) and a summary.  With
    ``kicad-cli`` available each net's plan is trial-applied to a scratch copy
    and kept only when ``pcb drc --refill-zones`` reports nothing new.
    """
    import tempfile

    pcb = PCB.load(pcb_path)
    uncut: list[dict[str, Any]] = []
    plans = plan_prune(
        pcb,
        declared_nets=declared_nets,
        wide=wide,
        uncut_out=uncut,
        required_widths=required_widths,
    )
    accepted = list(plans)
    rejected: list[NetPlan] = []
    drc_verified, note = False, "DRC verification skipped (--no-drc)"

    if verify_drc and plans:
        with tempfile.TemporaryDirectory(prefix="kct_prune_") as tmp:
            trial_path = Path(tmp) / "trial.kicad_pcb"
            # DRC must run with the board's own rules (clearances, netclasses).
            src = Path(pcb_path)
            missing: list[str] = []
            for ext in (".kicad_pro", ".kicad_dru"):
                if src.with_suffix(ext).exists():
                    shutil.copy(src.with_suffix(ext), Path(tmp) / f"trial{ext}")
                else:
                    missing.append(src.with_suffix(ext).name)
            trial_path.write_text(src.read_text())
            baseline = _drc_signature(trial_path)
            if baseline is None:
                note = (
                    "kicad-cli not available (or DRC failed): DRC was NOT run, result is unverified"
                )
            else:
                drc_verified, note = True, "verified with kicad-cli pcb drc --refill-zones"
                if missing:
                    note += (
                        f" (no {' / '.join(missing)} next to the board: checked against "
                        "default rules only, custom clearances were NOT evaluated)"
                    )

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
        if n_vias := sum(len(p.remove_vias) for p in accepted):
            stats["vias_removed"] = n_vias
    return pcb, PruneResult(plans, accepted, rejected, drc_verified, note, stats, uncut)
