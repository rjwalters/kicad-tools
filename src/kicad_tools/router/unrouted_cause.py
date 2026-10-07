"""Classify each unrouted connection as *congested* or *blocked* (Issue #5944).

When ``kct route`` leaves a connection unrouted the caller needs to know which
of two opposite fixes applies:

* **congested** -- a legal path exists on this board, but other nets' copper
  occupies it.  Rip-up, reordering, more layers or re-placement can help.
* **blocked** -- there is no legal path even with every other signal trace
  removed.  Only a placement, keepout, rule or footprint change can help.

The classifier answers that with one extra search per connection, run after
routing:

1. Lift every committed route off the routing grid (both the Python grid and
   its C++ mirror, via :meth:`RoutingGrid.resync_route_occupancy`).  Pads,
   keepouts / obstacles, pours (fixed fills) and the board-edge keepout stay,
   because none of them are routes.
2. Re-run the router's own A* for the connection on that "solo" grid.
3. Success -> ``congested``.  The nets whose (lifted) copper lies on the solo
   path are the ``contenders`` -- the rip-up candidates.
4. Failure -> ``blocked``.  A raster flood fill from each endpoint, in growing
   windows, finds the endpoint whose reachable region is closed off; the
   objects on that region's boundary -- the search frontier -- are the
   ``blockers`` (pad, keepout, board edge, pour, ...).
5. The committed copper is put back (same resync path) and the occupancy
   planes snapshotted before the lift are copied back over the result, so
   the grid -- every Python plane and every C++ cell -- ends bit-identical
   to how it started.  This runs in a ``finally`` so an exception cannot
   leave the grid half-lifted.

The pass is bounded: a total wall-clock budget, and a per-connection search
cap.  A connection the budget never reached, or whose solo search ran into its
time cap, is reported as ``unclassified`` rather than guessed.

Prior art (ideas only, both GPL/unlicensed, no code taken): fastroute's
``--diagnose`` congestion/blocked split and circuit-skills' post-route
``diagnose()`` triage.
"""

from __future__ import annotations

import math
import time
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

import numpy as np

from kicad_tools.acceleration import to_numpy
from kicad_tools.router.grid import RoutedNetsUnblocker

if TYPE_CHECKING:
    from .core import Autorouter
    from .primitives import Pad, Route

CAUSE_CONGESTED = "congested"
CAUSE_BLOCKED = "blocked"
CAUSE_UNCLASSIFIED = "unclassified"

#: Default total wall-clock budget for the whole pass, in seconds.
DEFAULT_BUDGET_S = 20.0
#: Default cap on a single connection's solo A* search, in seconds.
DEFAULT_PER_CONNECTION_S = 5.0
#: A solo search that failed after using this fraction of its cap is treated
#: as a timeout (unclassified), not as proof of a blocked connection.
_TIMEOUT_FRACTION = 0.9
#: Flood-fill window half-sizes (mm) tried around an endpoint, smallest first.
_WINDOW_RADII_MM = (2.0, 5.0, 12.0)
#: Hard cap on cells per flood-fill window layer (keeps the raster pass cheap).
_MAX_WINDOW_CELLS = 400 * 400
#: How many blockers / contenders to list per connection.
_MAX_LISTED = 10


@dataclass
class UnroutedConnection:
    """One unrouted pad-to-pad connection and its diagnosed cause."""

    net_id: int
    net_name: str
    source_pad: tuple[str, str]
    target_pad: tuple[str, str]
    cause: str
    blockers: list[dict[str, Any]] = field(default_factory=list)
    contenders: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""
    search_s: float = 0.0
    solo_path: dict[str, Any] | None = None
    frontier: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "net_id": self.net_id,
            "net_name": self.net_name,
            "source_pad": {"ref": self.source_pad[0], "pin": self.source_pad[1]},
            "target_pad": {"ref": self.target_pad[0], "pin": self.target_pad[1]},
            "cause": self.cause,
        }
        if self.cause == CAUSE_CONGESTED:
            out["contenders"] = self.contenders
            if self.solo_path is not None:
                out["solo_path"] = self.solo_path
        elif self.cause == CAUSE_BLOCKED:
            out["blockers"] = self.blockers
            if self.frontier is not None:
                out["frontier"] = self.frontier
        if self.note:
            out["note"] = self.note
        out["search_s"] = round(self.search_s, 3)
        return out


@dataclass
class UnroutedDiagnosis:
    """Result of :func:`diagnose_unrouted` for one routing run."""

    connections: list[UnroutedConnection]
    elapsed_s: float
    budget_s: float
    per_connection_s: float
    routes_lifted: int
    budget_exhausted: bool = False

    def counts(self) -> dict[str, int]:
        counts = {CAUSE_CONGESTED: 0, CAUSE_BLOCKED: 0, CAUSE_UNCLASSIFIED: 0}
        for conn in self.connections:
            counts[conn.cause] = counts.get(conn.cause, 0) + 1
        return counts

    def summary_dict(self) -> dict[str, Any]:
        return {
            "connections": len(self.connections),
            **self.counts(),
            "elapsed_s": round(self.elapsed_s, 3),
            "budget_s": self.budget_s,
            "per_connection_s": self.per_connection_s,
            "budget_exhausted": self.budget_exhausted,
            "routes_lifted": self.routes_lifted,
        }

    def summary_line(self) -> str:
        c = self.counts()
        return (
            f"Unrouted diagnosis: {c[CAUSE_CONGESTED]} congested, "
            f"{c[CAUSE_BLOCKED]} blocked, {c[CAUSE_UNCLASSIFIED]} unclassified "
            f"({len(self.connections)} connection(s), {self.elapsed_s:.1f}s of "
            f"{self.budget_s:g}s budget)"
        )


# ---------------------------------------------------------------------------
# Connection enumeration
# ---------------------------------------------------------------------------


def _mst_pairs(pads: list[Pad]) -> list[tuple[Pad, Pad]]:
    """Euclidean minimum spanning tree edges over ``pads`` (Prim, O(n^2))."""
    if len(pads) < 2:
        return []
    in_tree = [False] * len(pads)
    best = [math.inf] * len(pads)
    parent = [-1] * len(pads)
    best[0] = 0.0
    edges: list[tuple[Pad, Pad]] = []
    for _ in range(len(pads)):
        u = min((i for i in range(len(pads)) if not in_tree[i]), key=lambda i: best[i])
        in_tree[u] = True
        if parent[u] >= 0:
            edges.append((pads[parent[u]], pads[u]))
        for v in range(len(pads)):
            if in_tree[v]:
                continue
            d = math.hypot(pads[u].x - pads[v].x, pads[u].y - pads[v].y)
            if d < best[v]:
                best[v] = d
                parent[v] = u
    return edges


def _pad_key(pad: Pad) -> tuple[str, str]:
    return (str(getattr(pad, "ref", "")), str(getattr(pad, "pin", "")))


def _connections_for(
    router: Autorouter,
    net_ids: Iterable[int],
    *,
    mst_fallback_nets: set[int],
) -> list[tuple[int, Pad, Pad]]:
    """The connections to diagnose: recorded failures first, else MST edges.

    A net with routing-failure records is diagnosed on exactly the pad pairs
    the router reported failing.  A net with none (e.g. it was never attempted)
    falls back to its pads' MST edges -- but only for nets in
    ``mst_fallback_nets`` (fully unrouted nets); a partial net without failure
    records has no way to tell which of its edges are missing.
    """
    pads_by_key: dict[tuple[str, str], Pad] = dict(getattr(router, "pads", {}) or {})
    nets: Mapping[int, list] = getattr(router, "nets", {}) or {}
    failures_by_net: dict[int, list[tuple[tuple[str, str], tuple[str, str]]]] = {}
    for failure in getattr(router, "routing_failures", []) or []:
        src = tuple(getattr(failure, "source_pad", ()) or ())
        dst = tuple(getattr(failure, "target_pad", ()) or ())
        if len(src) != 2 or len(dst) != 2:
            continue
        failures_by_net.setdefault(int(failure.net), []).append(
            ((str(src[0]), str(src[1])), (str(dst[0]), str(dst[1])))
        )

    out: list[tuple[int, Pad, Pad]] = []
    for net in sorted(set(net_ids)):
        seen: set[frozenset[tuple[str, str]]] = set()
        for src_key, dst_key in failures_by_net.get(net, []):
            pair = frozenset((src_key, dst_key))
            if pair in seen or src_key == dst_key:
                continue
            src_pad = pads_by_key.get(src_key)
            dst_pad = pads_by_key.get(dst_key)
            if src_pad is None or dst_pad is None:
                continue
            seen.add(pair)
            out.append((net, src_pad, dst_pad))
        if seen or net not in mst_fallback_nets:
            continue
        pads = [pads_by_key[k] for k in nets.get(net, []) if k in pads_by_key]
        for src_pad, dst_pad in _mst_pairs(pads):
            out.append((net, src_pad, dst_pad))
    return out


# ---------------------------------------------------------------------------
# Lifting / restoring committed copper
# ---------------------------------------------------------------------------


class _CopperLift(RoutedNetsUnblocker):
    """Lift every committed route off the grid for the duration of a ``with``.

    A thin alias for :class:`~kicad_tools.router.grid.RoutedNetsUnblocker`
    (Issue #6009 moved the lift there so the relaxed rip-up blocker search
    shares it): every route in ``grid.routes`` comes off the Python grid, its
    paired C++ grid and the R-trees through ``resync_route_occupancy``; the
    pathfinder's crossing-cost cache is emptied; the access-witness journal is
    detached; and on exit the grid is restored exactly.
    """

    def __init__(self, router: Autorouter) -> None:
        super().__init__(router.grid, router.router)


# ---------------------------------------------------------------------------
# Congested: who occupies the solo path?
# ---------------------------------------------------------------------------


def _route_cells(grid: Any, route: Route, radius: int) -> Iterable[tuple[int, int, int]]:
    """Grid cells (layer, y, x) along ``route``'s centerlines (+ ``radius``)."""
    cols, rows = grid.cols, grid.rows
    for seg in route.segments:
        gx1, gy1 = grid.world_to_grid(seg.x1, seg.y1)
        gx2, gy2 = grid.world_to_grid(seg.x2, seg.y2)
        layer = grid.layer_to_index(seg.layer.value)
        dx, dy = abs(gx2 - gx1), abs(gy2 - gy1)
        sx = 1 if gx1 < gx2 else -1
        sy = 1 if gy1 < gy2 else -1
        err = dx - dy
        gx, gy = gx1, gy1
        while True:
            for oy in range(-radius, radius + 1):
                for ox in range(-radius, radius + 1):
                    cx, cy = gx + ox, gy + oy
                    if 0 <= cx < cols and 0 <= cy < rows:
                        yield (layer, cy, cx)
            if gx == gx2 and gy == gy2:
                break
            e2 = 2 * err
            if e2 > -dy:
                err -= dy
                gx += sx
            if e2 < dx:
                err += dx
                gy += sy
    for via in route.vias:
        vx, vy = grid.world_to_grid(via.x, via.y)
        for layer in range(grid.num_layers):
            for oy in range(-radius, radius + 1):
                for ox in range(-radius, radius + 1):
                    cx, cy = vx + ox, vy + oy
                    if 0 <= cx < cols and 0 <= cy < rows:
                        yield (layer, cy, cx)


def _contenders(
    grid: Any,
    route: Route,
    copper_net: np.ndarray,
    net: int,
    net_names: Mapping[int, str],
    trace_width: float,
) -> list[dict[str, Any]]:
    radius = max(0, math.ceil(round(trace_width / 2 / grid.resolution, 6)))
    counts: dict[int, int] = {}
    seen: set[tuple[int, int, int]] = set()
    for cell in _route_cells(grid, route, radius):
        if cell in seen:
            continue
        seen.add(cell)
        owner = int(copper_net[cell])
        if owner > 0 and owner != net:
            counts[owner] = counts.get(owner, 0) + 1
    ranked = sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))
    return [
        {"net_id": nid, "net_name": net_names.get(nid, f"Net_{nid}"), "cells": n}
        for nid, n in ranked[:_MAX_LISTED]
    ]


def _path_summary(route: Route) -> dict[str, Any]:
    length = sum(math.hypot(s.x2 - s.x1, s.y2 - s.y1) for s in route.segments)
    return {"length_mm": round(length, 3), "vias": len(route.vias)}


# ---------------------------------------------------------------------------
# Blocked: what closes the endpoint's reachable region?
# ---------------------------------------------------------------------------


class _Attributor:
    """Maps a blocked grid cell to the board object that blocks it."""

    def __init__(self, router: Autorouter, net_names: Mapping[int, str]) -> None:
        self.router = router
        self.grid = router.grid
        self.net_names = net_names
        rules = getattr(router, "rules", None)
        self._trace_inflate = (
            float(getattr(rules, "trace_clearance", 0.0) or 0.0)
            + float(getattr(rules, "trace_width", 0.0) or 0.0) / 2
        )
        pads = list((getattr(router, "pads", {}) or {}).values())
        self._pads = [p for p in pads if not getattr(p, "steiner_point", False)]
        self._obstacles = list(getattr(router, "_obstacles", []) or [])
        self._edges = list(getattr(router, "_edge_segments", None) or [])
        self._edge_clearance = float(getattr(router, "_edge_clearance", 0.0) or 0.0)

    def layer_name(self, layer_idx: int) -> str:
        from .layers import Layer

        try:
            return Layer(self.grid.index_to_layer(layer_idx)).kicad_name
        except Exception:  # pragma: no cover - defensive
            return str(layer_idx)

    def _nearest_pad(self, wx: float, wy: float, net: int) -> Pad | None:
        best: Pad | None = None
        best_d = math.inf
        for pad in self._pads:
            if net and pad.net != net:
                continue
            d = math.hypot(pad.x - wx, pad.y - wy) - max(pad.width, pad.height) / 2
            if d < best_d:
                best, best_d = pad, d
        return best

    def _near_edge(self, wx: float, wy: float) -> bool:
        if not self._edges:
            return False
        limit = self._edge_clearance + 2 * self.grid.resolution
        for (x1, y1), (x2, y2) in self._edges:
            vx, vy = x2 - x1, y2 - y1
            seg2 = vx * vx + vy * vy
            t = 0.0 if seg2 == 0 else max(0.0, min(1.0, ((wx - x1) * vx + (wy - y1) * vy) / seg2))
            if math.hypot(wx - (x1 + t * vx), wy - (y1 + t * vy)) <= limit:
                return True
        return False

    def _obstacle_at(self, wx: float, wy: float, layer_idx: int) -> Any | None:
        margin = self._trace_inflate + self.grid.resolution
        for obs in self._obstacles:
            try:
                if self.grid.layer_to_index(obs.layer.value) != layer_idx:
                    continue
            except Exception:  # pragma: no cover - defensive
                continue
            half_w = obs.width / 2 + obs.clearance + margin
            half_h = obs.height / 2 + obs.clearance + margin
            if abs(wx - obs.x) <= half_w and abs(wy - obs.y) <= half_h:
                return obs
        return None

    def attribute(self, layer_idx: int, gy: int, gx: int) -> tuple[tuple, dict[str, Any]]:
        """``(dedup_key, blocker_dict)`` for a blocked cell.

        The key is layer-free so one object seen on several layers is listed
        once; the caller accumulates the layers it was seen on.
        """
        grid = self.grid
        wx, wy = grid.grid_to_world(gx, gy)
        pad_blocked = bool(grid._pad_blocked[layer_idx, gy, gx])
        original_net = int(grid._original_net[layer_idx, gy, gx])
        cell_net = int(grid._net[layer_idx, gy, gx])
        raster_only = grid._raster_only_blocked
        registry_less = bool(raster_only is not None and raster_only[layer_idx, gy, gx])

        if pad_blocked and not registry_less:
            owner = original_net or cell_net
            pad = self._nearest_pad(wx, wy, owner) or self._nearest_pad(wx, wy, 0)
            if pad is not None:
                return (
                    ("pad", pad.ref, pad.pin),
                    {
                        "kind": "pad",
                        "ref": pad.ref,
                        "pin": pad.pin,
                        "net_name": self.net_names.get(pad.net, getattr(pad, "net_name", ""))
                        or None,
                        "at": [round(pad.x, 3), round(pad.y, 3)],
                    },
                )
        if registry_less or not pad_blocked:
            # Issue #6008: a board-file keepout rule area, now on the grid --
            # name it rather than reporting an anonymous keepout.
            rule_area_at = getattr(grid, "rule_area_at", None)
            area = rule_area_at(layer_idx, gx, gy) if rule_area_at is not None else None
            if area is not None:
                bbox = [round(v, 3) for v in area.bbox]
                info: dict[str, Any] = {"kind": "keepout", "source": "rule_area", "bbox": bbox}
                if area.name:
                    info["name"] = area.name
                return (("keepout", "rule_area", area.name, tuple(bbox)), info)
            if self._near_edge(wx, wy):
                return (("board_edge",), {"kind": "board_edge"})
            obs = self._obstacle_at(wx, wy, layer_idx)
            if obs is not None:
                bbox = [
                    round(obs.x - obs.width / 2, 3),
                    round(obs.y - obs.height / 2, 3),
                    round(obs.x + obs.width / 2, 3),
                    round(obs.y + obs.height / 2, 3),
                ]
                return (("keepout", tuple(bbox)), {"kind": "keepout", "bbox": bbox})
            if registry_less:
                return (("keepout", None), {"kind": "keepout"})
        if bool(grid._is_zone[layer_idx, gy, gx]):
            owner = cell_net or original_net
            return (
                ("pour", owner),
                {
                    "kind": "pour",
                    "net_name": self.net_names.get(owner, f"Net_{owner}") if owner else None,
                },
            )
        if cell_net > 0:
            return (
                ("copper", cell_net),
                {"kind": "copper", "net_name": self.net_names.get(cell_net, f"Net_{cell_net}")},
            )
        return (("obstacle",), {"kind": "obstacle"})


def _endpoint_layers(grid: Any, pad: Pad, routable: list[int]) -> list[int]:
    if getattr(pad, "through_hole", False):
        return list(routable)
    try:
        idx = grid.layer_to_index(pad.layer.value)
    except Exception:  # pragma: no cover - defensive
        return list(routable)
    return [idx] if idx in routable else list(routable)


def _flood(free: np.ndarray, seeds: np.ndarray, allow_layer_change: bool) -> np.ndarray:
    """4-connected flood fill of ``seeds`` through ``free`` (layers, rows, cols)."""
    reach: np.ndarray = np.asarray(seeds & free, dtype=bool)
    max_iter = free.shape[1] * free.shape[2] + 1
    for _ in range(max_iter):
        grown = reach.copy()
        grown[:, 1:, :] |= reach[:, :-1, :]
        grown[:, :-1, :] |= reach[:, 1:, :]
        grown[:, :, 1:] |= reach[:, :, :-1]
        grown[:, :, :-1] |= reach[:, :, 1:]
        if allow_layer_change and grown.shape[0] > 1:
            grown |= grown.any(axis=0, keepdims=True)
        grown &= free
        if bool(np.array_equal(grown, reach)):
            break
        reach = grown
    return reach


def _close_filtered_rule_areas(
    grid: Any, free: np.ndarray, layers: list[int], net: int, x0: int, y0: int
) -> None:
    """Mark net-filtered track keepout cells as not free for ``net`` (#6008).

    All-nets rule areas are already blocked cells; an area narrowed by a
    ``spatial_keepouts`` filter lives outside the occupancy planes, so the
    flood fill would otherwise walk straight through one that applies to
    this net.
    """
    for area in getattr(grid, "_rule_area_keepouts", None) or ():
        if not area.blocks_tracks or area.static_tracks or not area.applies_to(net):
            continue
        h, w = area.mask.shape
        _, rows, cols = free.shape
        ax0, ay0 = max(area.gx0, x0), max(area.gy0, y0)
        ax1, ay1 = min(area.gx0 + w, x0 + cols), min(area.gy0 + h, y0 + rows)
        if ax0 >= ax1 or ay0 >= ay1:
            continue
        window = area.mask[ay0 - area.gy0 : ay1 - area.gy0, ax0 - area.gx0 : ax1 - area.gx0]
        for li, layer in enumerate(layers):
            if layer in area.layers:
                free[li, ay0 - y0 : ay1 - y0, ax0 - x0 : ax1 - x0] &= ~window


def _frontier_for_endpoint(
    grid: Any,
    pad: Pad,
    net: int,
    routable: list[int],
    attributor: _Attributor,
) -> tuple[list[dict[str, Any]], dict[str, Any]] | None:
    """Blockers closing ``pad``'s reachable region, or ``None`` if it escapes.

    Grows a window around the pad; at each size, flood-fills the cells this
    net may occupy (free cells and its own pad cells) from the pad.  When the
    reached region never touches the window's interior border, the region is
    closed and its boundary is the frontier the solo search exhausted.
    """
    res = grid.resolution
    pgx, pgy = grid.world_to_grid(pad.x, pad.y)
    layers = list(routable)
    seed_layers = _endpoint_layers(grid, pad, routable)
    for radius_mm in _WINDOW_RADII_MM:
        r = max(4, int(math.ceil(radius_mm / res)))
        x0, x1 = max(0, pgx - r), min(grid.cols - 1, pgx + r)
        y0, y1 = max(0, pgy - r), min(grid.rows - 1, pgy + r)
        if (x1 - x0 + 1) * (y1 - y0 + 1) > _MAX_WINDOW_CELLS:
            break
        blocked = to_numpy(grid._blocked)[layers, y0 : y1 + 1, x0 : x1 + 1]
        cell_net = to_numpy(grid._net)[layers, y0 : y1 + 1, x0 : x1 + 1]
        free = ~blocked | (cell_net == net)
        _close_filtered_rule_areas(grid, free, layers, net, x0, y0)
        seeds = np.zeros_like(free)
        # Seed the whole pad footprint, so a pad whose centre cell happens to
        # sit on a neighbour's halo is still seeded from its free metal.
        hw = max(0, int(math.ceil(pad.width / 2 / res)))
        hh = max(0, int(math.ceil(pad.height / 2 / res)))
        sx0, sx1 = max(x0, pgx - hw), min(x1, pgx + hw)
        sy0, sy1 = max(y0, pgy - hh), min(y1, pgy + hh)
        for li, layer in enumerate(layers):
            if layer in seed_layers:
                seeds[li, sy0 - y0 : sy1 - y0 + 1, sx0 - x0 : sx1 - x0 + 1] = True
        allow_vias = len(layers) > 1
        reach = _flood(free, seeds, allow_vias)
        if not reach.any():
            # Every cell of the pad's own footprint is foreign-blocked: the
            # frontier IS the footprint.
            reach_ring = seeds
        else:
            reach_ring = reach
        # Does the region escape through an interior window side?
        escapes = False
        touches_board_edge = False
        sides = (
            (reach_ring[:, :, 0], x0 > 0),
            (reach_ring[:, :, -1], x1 < grid.cols - 1),
            (reach_ring[:, 0, :], y0 > 0),
            (reach_ring[:, -1, :], y1 < grid.rows - 1),
        )
        for side, interior in sides:
            if side.any():
                if interior:
                    escapes = True
                else:
                    touches_board_edge = True
        if escapes and reach.any():
            continue
        # Frontier: blocked cells 4-adjacent to the reached region.
        grown = reach_ring.copy()
        grown[:, 1:, :] |= reach_ring[:, :-1, :]
        grown[:, :-1, :] |= reach_ring[:, 1:, :]
        grown[:, :, 1:] |= reach_ring[:, :, :-1]
        grown[:, :, :-1] |= reach_ring[:, :, 1:]
        frontier = grown & ~free if reach.any() else seeds & ~free
        found: dict[tuple, dict[str, Any]] = {}
        cells: dict[tuple, int] = {}
        seen_layers: dict[tuple, set[int]] = {}
        for li, fy, fx in zip(*np.nonzero(frontier), strict=True):
            layer = layers[int(li)]
            key, blocker = attributor.attribute(layer, int(fy) + y0, int(fx) + x0)
            if key not in found:
                found[key] = blocker
            cells[key] = cells.get(key, 0) + 1
            seen_layers.setdefault(key, set()).add(layer)
        if touches_board_edge:
            key = ("board_edge",)
            found.setdefault(key, {"kind": "board_edge"})
            cells[key] = cells.get(key, 0) + 1
        ranked = sorted(found, key=lambda k: -cells[k])
        blockers = []
        for k in ranked[:_MAX_LISTED]:
            entry = dict(found[k], cells=cells[k])
            if k in seen_layers and k != ("board_edge",):
                entry["layers"] = [attributor.layer_name(i) for i in sorted(seen_layers[k])]
            blockers.append(entry)
        meta = {
            "endpoint": {"ref": pad.ref, "pin": pad.pin},
            "window_mm": radius_mm,
            "reachable_cells": int(reach.sum()),
        }
        return blockers, meta
    return None


def _blocked_report(
    grid: Any,
    src: Pad,
    dst: Pad,
    net: int,
    routable: list[int],
    attributor: _Attributor,
) -> tuple[list[dict[str, Any]], dict[str, Any] | None, str]:
    for pad in (src, dst):
        found = _frontier_for_endpoint(grid, pad, net, routable, attributor)
        if found is not None:
            return found[0], found[1], ""
    return (
        [],
        None,
        "no closed frontier near either endpoint: the obstruction is a long-range "
        "wall, or clearance geometry finer than the routing grid",
    )


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def diagnose_unrouted(
    router: Autorouter,
    unrouted_net_ids: Iterable[int],
    *,
    partial_net_ids: Iterable[int] = (),
    net_names: Mapping[int, str] | None = None,
    budget_s: float = DEFAULT_BUDGET_S,
    per_connection_s: float = DEFAULT_PER_CONNECTION_S,
    connections: Iterable[tuple[int, Pad, Pad]] | None = None,
) -> UnroutedDiagnosis | None:
    """Classify every unrouted connection as congested / blocked.

    Args:
        router: The :class:`Autorouter` after routing.  Its grid and committed
            copper are restored before this returns.
        unrouted_net_ids: Nets with no copper at all.  Diagnosed on their
            recorded failures, falling back to their MST edges.
        partial_net_ids: Nets with some copper but disconnected pads.
            Diagnosed only on their recorded failures.
        net_names: Net id -> name, for the report.
        budget_s: Total wall-clock budget for the pass (seconds).
        per_connection_s: Cap on one connection's solo search (seconds).
        connections: Explicit ``(net_id, source_pad, target_pad)`` pairs to
            diagnose instead of enumerating them from the router's failure
            records / MST edges.  ``kct route-auto`` (Issue #6001) passes the
            pad islands its strategy left apart; the pads must be the
            ``router``'s own.

    Returns:
        The diagnosis, or ``None`` when the router has no grid-A* engine to
        re-run (e.g. a non-grid strategy) or there is nothing to diagnose.
    """
    t0 = time.monotonic()
    grid = getattr(router, "grid", None)
    pathfinder = getattr(router, "router", None)
    if grid is None or pathfinder is None or not hasattr(pathfinder, "route"):
        return None
    if not hasattr(grid, "resync_route_occupancy"):
        return None
    if getattr(getattr(grid, "_blocked", None), "size", 0) == 0:
        # Occupancy planes already released (``kct route`` frees them after
        # finalize, #4292) -- nothing left to search.
        return None
    names = dict(net_names or {})
    for net_id, pad_keys in (getattr(router, "nets", {}) or {}).items():
        if net_id not in names:
            for key in pad_keys:
                pad = (getattr(router, "pads", {}) or {}).get(key)
                pad_net_name = str(getattr(pad, "net_name", "") or "")
                if pad_net_name:
                    names[net_id] = pad_net_name
                    break

    unrouted = {int(n) for n in unrouted_net_ids}
    partial = {int(n) for n in partial_net_ids} - unrouted
    if connections is not None:
        connections = list(connections)
    else:
        connections = _connections_for(router, unrouted | partial, mst_fallback_nets=unrouted)
    if not connections:
        return None

    rules = getattr(router, "rules", None)
    trace_width = float(getattr(rules, "trace_width", 0.2) or 0.2)
    routable = list(grid.get_routable_indices())
    results: list[UnroutedConnection] = []
    budget_exhausted = False

    with _CopperLift(router) as lift:
        copper_net = lift.copper_net
        attributor = _Attributor(router, names)

        for net, src, dst in connections:
            conn = UnroutedConnection(
                net_id=net,
                net_name=names.get(net, f"Net_{net}"),
                source_pad=_pad_key(src),
                target_pad=_pad_key(dst),
                cause=CAUSE_UNCLASSIFIED,
            )
            results.append(conn)
            remaining = budget_s - (time.monotonic() - t0)
            if remaining <= 0.05:
                budget_exhausted = True
                conn.note = "diagnosis budget exhausted before this connection"
                continue
            cap = max(0.05, min(per_connection_s, remaining))
            s0 = time.monotonic()
            try:
                route = pathfinder.route(src, dst, per_net_timeout=cap)
            except Exception as exc:  # pragma: no cover - defensive
                conn.search_s = time.monotonic() - s0
                conn.note = f"solo search raised {type(exc).__name__}: {exc}"
                continue
            conn.search_s = time.monotonic() - s0
            if route is not None:
                conn.cause = CAUSE_CONGESTED
                conn.contenders = _contenders(grid, route, copper_net, net, names, trace_width)
                conn.solo_path = _path_summary(route)
                if not conn.contenders:
                    conn.note = (
                        "the solo path crosses no other net's copper: the connection "
                        "is routable on the board as it stands (a search-budget or "
                        "ordering failure); retry it"
                    )
                continue
            if conn.search_s >= _TIMEOUT_FRACTION * cap:
                conn.note = f"solo search hit its {cap:.2f}s cap; cause undetermined"
                continue
            conn.cause = CAUSE_BLOCKED
            conn.blockers, conn.frontier, conn.note = _blocked_report(
                grid, src, dst, net, routable, attributor
            )
        routes_lifted = len(lift.lifted)

    return UnroutedDiagnosis(
        connections=results,
        elapsed_s=time.monotonic() - t0,
        budget_s=budget_s,
        per_connection_s=per_connection_s,
        routes_lifted=routes_lifted,
        budget_exhausted=budget_exhausted,
    )


__all__ = [
    "CAUSE_BLOCKED",
    "CAUSE_CONGESTED",
    "CAUSE_UNCLASSIFIED",
    "DEFAULT_BUDGET_S",
    "DEFAULT_PER_CONNECTION_S",
    "UnroutedConnection",
    "UnroutedDiagnosis",
    "diagnose_unrouted",
]
