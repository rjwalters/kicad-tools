"""Pose-based centerline search for coupled differential pairs (Issue #5786).

Epic #5784 Phase 2.  The joint-state coupled A* (``CoupledPathfinder.route_coupled``)
searches the product of two head positions and stalls on dense escapes: on
board 06b it plateaued on all four LVDS pairs and the pairs fell back to
independent legs, 0 % coupled.  KiCadRoutingTools (KRT) instead routes ONE
centerline over ``(x, y, theta, layer)`` poses with 45-degree heading steps and
a Dubins-length heuristic, then derives P and N as perpendicular offsets, and
finishes the pad ends with short single-ended legs ("hybrid").  This module is
that construction on our grid and our clearance model:

* the search itself lives in C++ (``CoupledPathfinder::route_centerline`` in
  ``cpp/src/coupled_pathfinder.cpp``); every step is judged by offsetting the
  centerline into the P and N rails and calling the Epic #5509 clearance
  kernel (``rail_clear_world``) on each, plus the raster gate for pad and
  keepout cells;
* the Dubins length is ``cpp/include/dubins.hpp``, a port of KRT's
  ``rust_router/src/dubins.rs`` (MIT, see ``THIRD_PARTY_NOTICES.md``);
* this module chooses the trunk setbacks (scanning every heading and a ladder
  of setback distances), builds the end legs, derives the miter-compensated
  rails, and re-verifies the finished copper before returning it.

Design credit: the construction follows KRT's documented design
(``py_router/diff_pair_routing.py``); no KRT Python code is copied.

Backend note: the search is **C++-only**.  There is no pure-Python
implementation of the pose search; when the C++ backend is unavailable
:func:`route_centerline_pose` returns ``None`` and the caller keeps its
existing fallback (independent legs), exactly as before this module existed.

Selection: on by default since Issue #5895.  ``DiffPairRouter.enable_pose_centerline``
(``KCT_POSE_CENTERLINE=0`` opts out) gates it, and the rescue runs only after the
joint-state search (and the shadow constructor, if it is on) have failed.  A
committed pose trunk is a corridor-yield candidate (#4463/#5895): if it seals a
net the main strategy then strands, it yields and is re-routed single-ended.
Before the pair is committed, the router also applies its exact foreign-pad gate
and the intra-pair clearance audit to it (``DiffPairRouter._pose_copper_rejection``).

Scope (v1): single-layer trunks.  Pairs whose two ends sit on different layers
or need a via between them are left to the joint-state search.
"""

from __future__ import annotations

import logging
import math
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import TYPE_CHECKING

from .layers import Layer
from .primitives import Pad, Route, Segment

if TYPE_CHECKING:
    from .diffpair_routing import CoupledPathfinder

logger = logging.getLogger(__name__)

# ``(x1, y1, x2, y2, layer_index, net, width) -> ok``: an extra exact gate a
# caller applies to every candidate rail segment on top of the kernel + raster
# verdict (the diff-pair router passes its #4571 foreign-pad gate).
SpanCheck = Callable[[float, float, float, float, int, int, float], bool]

# Heading k points along (cos(k*45deg), sin(k*45deg)) in grid space.
_HEADING_DIRS: tuple[tuple[float, float], ...] = tuple(
    (round(math.cos(k * math.pi / 4), 12), round(math.sin(k * math.pi / 4), 12)) for k in range(8)
)

# Setback ladder, mm from the pad-pair midpoint to the trunk start.  The
# per-heading scan keeps the first legal rung and a few later ones spaced
# ``_SETBACK_KEEP_SPACING_MM`` apart, so the search can choose among them.
_SETBACK_STEP_MM = 0.1
_SETBACK_MAX_MM = 3.0
_SETBACK_KEEP = 3
_SETBACK_KEEP_SPACING_MM = 0.5

# An end leg is uncoupled copper, so a cell of leg costs this many cells of
# trunk.  Without it a setback is cost-neutral (a longer leg just shortens the
# trunk by the same amount) and ties resolve toward the LONGEST leg.
_LEG_COST_FACTOR = 3.0

_EPS = 1e-6

# Safety margin above the exact width + clearance floor (referee rounding).
_PITCH_MARGIN_MM = 0.005


@dataclass
class _Endpoint:
    """One legal trunk endpoint: a pose plus the two end legs that reach it."""

    cell: tuple[int, int]
    heading: int
    p_side: int
    cost_cells: float
    # World polylines from the PAD to the trunk rail point, per rail.
    p_leg: list[tuple[float, float]]
    n_leg: list[tuple[float, float]]


def _unit(h: int) -> tuple[float, float]:
    return _HEADING_DIRS[h % 8]


def _normal(h: int) -> tuple[float, float]:
    ux, uy = _unit(h)
    return (-uy, ux)


def _polyline_len(pts: list[tuple[float, float]]) -> float:
    return sum(math.dist(pts[i], pts[i + 1]) for i in range(len(pts) - 1))


def _leg(
    q: tuple[float, float], t: tuple[float, float], u: tuple[float, float]
) -> list[tuple[float, float]] | None:
    """Polyline from pad ``q`` to rail point ``t``, leaving along ``u``.

    Straight along ``u`` first, then one 45-degree diagonal that absorbs the
    lateral offset.  Needs forward travel >= lateral travel; otherwise the leg
    would have to start backwards or steeper than 45 degrees, and the setback
    is rejected.
    """
    dx, dy = t[0] - q[0], t[1] - q[1]
    fwd = dx * u[0] + dy * u[1]
    lat = -dx * u[1] + dy * u[0]
    if fwd < -_EPS:
        return None
    if abs(lat) > fwd + _EPS:
        return None
    s1 = fwd - abs(lat)
    pts = [q]
    if s1 > _EPS and abs(lat) > _EPS:
        pts.append((q[0] + u[0] * s1, q[1] + u[1] * s1))
    pts.append(t)
    return pts


def _simplify(pts: list[tuple[float, float]]) -> list[tuple[float, float]]:
    """Drop duplicate and collinear interior points."""
    out: list[tuple[float, float]] = []
    for p in pts:
        if out and math.dist(out[-1], p) < 1e-4:
            continue
        out.append(p)
    changed = True
    while changed and len(out) > 2:
        changed = False
        for i in range(1, len(out) - 1):
            ax, ay = out[i][0] - out[i - 1][0], out[i][1] - out[i - 1][1]
            bx, by = out[i + 1][0] - out[i][0], out[i + 1][1] - out[i][1]
            cross = ax * by - ay * bx
            dot = ax * bx + ay * by
            if abs(cross) < 1e-9 and dot > 0:
                del out[i]
                changed = True
                break
    return out


class _Ctx:
    """Everything the construction needs, resolved once per pair."""

    def __init__(
        self,
        pf: CoupledPathfinder,
        impl,
        p_start: Pad,
        p_end: Pad,
        n_start: Pad,
        n_end: Pad,
        span_check: SpanCheck | None = None,
    ):
        self.pf = pf
        self.impl = impl
        self.span_check = span_check
        self.grid = pf.grid
        self.res = float(pf.grid.resolution)
        self.layer = pf.grid.layer_to_index(p_start.layer.value)
        self.p_net = p_start.net
        self.n_net = n_start.net
        self.p_width = pf._get_trace_width_for_net(p_start.net_name)
        self.n_width = pf._get_trace_width_for_net(n_start.net_name)
        self.p_gap = self._gap(p_start.net_name)
        self.n_gap = self._gap(n_start.net_name)
        # Centre-to-centre pitch: the joint search's grid-quantised target,
        # floored at width + the net-class/global clearance.  The pair's
        # declared intra-pair spacing can be NARROWER than the clearance the
        # board's design-rule referee enforces between the two rails (board
        # 06b: 0.30 mm target vs 0.35 mm = 0.2 + 0.15), and the derived rails
        # are world-space offsets, so unlike the joint search they are not
        # forced onto a whole number of cells.
        floor = (self.p_width + self.n_width) / 2.0 + max(self.p_gap, self.n_gap) + _PITCH_MARGIN_MM
        self.pitch = max(max(pf.target_spacing_cells, pf.min_spacing_cells, 1) * self.res, floor)
        self.half_pitch = self.pitch / 2.0

    def _gap(self, net_name: str) -> float:
        klass = self.pf.net_class_map.get(net_name)
        return float(klass.clearance if klass else self.pf.rules.trace_clearance)

    def rail_params(self, rail: str) -> tuple[int, int, float, float]:
        if rail == "p":
            return self.p_net, self.n_net, self.p_width / 2.0, self.p_gap
        return self.n_net, self.p_net, self.n_width / 2.0, self.n_gap

    def poly_clear(self, pts: list[tuple[float, float]], rail: str) -> bool:
        """Kernel + raster verdict for a rail polyline.

        Uses the C++ ``rail_segment_clear`` -- the same function the search
        judges its steps with -- so the search and the verification of the
        finished copper cannot round a cell tie differently.  That gate waives
        the partner net, partner PADS included, and sees foreign pads only
        through the raster, so the optional ``span_check`` (the caller's exact
        pad gate) is applied on top: an end leg must not graze the partner's
        pad or a neighbouring pin.
        """
        net, partner, half, gap = self.rail_params(rail)
        for a, b in zip(pts, pts[1:], strict=False):
            if not self.impl.rail_segment_clear(
                a[0], a[1], b[0], b[1], self.layer, net, partner, half, gap
            ):
                logger.debug("pose rail %s blocked at %s -> %s", rail, a, b)
                return False
            if self.span_check is not None and not self.span_check(
                a[0], a[1], b[0], b[1], self.layer, net, 2.0 * half
            ):
                logger.debug("pose rail %s fails the exact pad gate at %s -> %s", rail, a, b)
                return False
        return True


def _scan_endpoints(
    ctx: _Ctx, q_p: tuple[float, float], q_n: tuple[float, float], *, arriving: bool
) -> list[_Endpoint]:
    """Scan headings x setbacks for legal trunk endpoints at one pad pair.

    ``arriving=False`` yields START poses (trunk heading = exit direction);
    ``arriving=True`` yields GOAL poses, whose heading is the direction of
    travel on arrival, i.e. the reverse of the pad's exit direction.
    """
    mid = ((q_p[0] + q_n[0]) / 2.0, (q_p[1] + q_n[1]) / 2.0)
    found: list[_Endpoint] = []
    steps = int(_SETBACK_MAX_MM / _SETBACK_STEP_MM)
    for h in range(8):
        # ``exit_h`` is the direction the legs leave the pads along.
        exit_h = (h + 4) % 8 if arriving else h
        e = _unit(exit_h)
        trunk_h = h
        n_trunk = _normal(trunk_h)
        sep = (q_p[0] - q_n[0]) * n_trunk[0] + (q_p[1] - q_n[1]) * n_trunk[1]
        if abs(sep) < ctx.half_pitch * 0.5:
            continue  # pads in line with the heading: no coupled geometry
        p_side = 1 if sep > 0 else -1
        kept: list[float] = []
        for k in range(steps + 1):
            d = k * _SETBACK_STEP_MM
            if kept and d - kept[-1] < _SETBACK_KEEP_SPACING_MM - _EPS:
                continue
            if len(kept) >= _SETBACK_KEEP:
                break
            c_world = (mid[0] + e[0] * d, mid[1] + e[1] * d)
            cell = ctx.grid.world_to_grid(*c_world)
            cw = ctx.grid.grid_to_world(*cell)
            t_p = (
                cw[0] + p_side * ctx.half_pitch * n_trunk[0],
                cw[1] + p_side * ctx.half_pitch * n_trunk[1],
            )
            t_n = (
                cw[0] - p_side * ctx.half_pitch * n_trunk[0],
                cw[1] - p_side * ctx.half_pitch * n_trunk[1],
            )
            leg_p = _leg(q_p, t_p, e)
            leg_n = _leg(q_n, t_n, e)
            if leg_p is None or leg_n is None:
                continue
            if not (ctx.poly_clear(leg_p, "p") and ctx.poly_clear(leg_n, "n")):
                continue
            kept.append(d)
            cost = _LEG_COST_FACTOR * (_polyline_len(leg_p) + _polyline_len(leg_n)) / 2.0 / ctx.res
            found.append(_Endpoint(cell, trunk_h, p_side, cost, leg_p, leg_n))
    return found


def _derive_rail(
    cells: list[tuple[int, int, int, int]],
    start_heading: int,
    grid,
    offset: float,
) -> list[tuple[float, float]]:
    """Miter-compensated offset of the centerline, as a world polyline."""
    pts = [grid.grid_to_world(c[0], c[1]) for c in cells]
    hs = [c[2] for c in cells]  # heading used to ARRIVE at each pose
    # Collapse to corner vertices: (point, incoming_heading, outgoing_heading).
    verts: list[list] = [[pts[0], start_heading, None]]
    for i in range(1, len(pts)):
        h = hs[i]
        if verts[-1][2] is None:
            verts[-1][2] = h
        if i < len(pts) - 1 and hs[i + 1] == h:
            continue  # collinear: keep walking
        verts.append([pts[i], h, None])
    verts[-1][2] = verts[-1][1]
    out: list[tuple[float, float]] = []
    for pt, h_in, h_out in verts:
        n1, n2 = _normal(h_in), _normal(h_out)
        dot = n1[0] * n2[0] + n1[1] * n2[1]
        k = offset / (1.0 + dot) if (1.0 + dot) > 1e-6 else offset
        out.append((pt[0] + k * (n1[0] + n2[0]), pt[1] + k * (n1[1] + n2[1])))
    return out


def route_centerline_pose(
    pf: CoupledPathfinder,
    p_start: Pad,
    p_end: Pad,
    n_start: Pad,
    n_end: Pad,
    *,
    timeout_seconds: float | None = None,
    max_iterations: int = 400_000,
    heuristic_weight: float = 1.5,
    span_check: SpanCheck | None = None,
) -> tuple[Route, Route] | None:
    """Route one coupled pair as a centerline trunk with single-ended end legs.

    Returns ``(p_route, n_route)`` (pad to pad, no vias) or ``None`` when the
    construction does not apply or finds no legal trunk.  ``None`` is the
    caller's cue to keep its existing fallback.  ``span_check`` is an extra
    exact gate applied to every end leg and to the finished rails (see
    :data:`SpanCheck`).
    """
    t0 = time.monotonic()
    pf.last_pose_report = {"applied": False}
    # ``getattr``: pathfinder doubles in unit tests implement only the
    # joint-state surface; treat them as "no C++ backend".
    available = getattr(pf, "_cpp_coupled_available", None)
    if available is None or not available():
        pf.last_pose_report = {"applied": False, "reason": "cpp-unavailable"}
        return None
    cpp = pf._get_cpp_coupled_impl()
    if cpp is None or not hasattr(cpp, "route_centerline"):
        pf.last_pose_report = {"applied": False, "reason": "cpp-unavailable"}
        return None
    layers = {pf.grid.layer_to_index(p.layer.value) for p in (p_start, p_end, n_start, n_end)}
    if len(layers) != 1:
        pf.last_pose_report = {"applied": False, "reason": "multi-layer"}
        return None

    ctx = _Ctx(pf, cpp, p_start, p_end, n_start, n_end, span_check)
    starts = _scan_endpoints(ctx, (p_start.x, p_start.y), (n_start.x, n_start.y), arriving=False)
    goals = _scan_endpoints(ctx, (p_end.x, p_end.y), (n_end.x, n_end.y), arriving=True)
    if not starts or not goals:
        pf.last_pose_report = {
            "applied": False,
            "reason": "no-legal-setback",
            "starts": len(starts),
            "goals": len(goals),
        }
        return None

    best = None  # (cost, centerline cells, start ep, goal ep, diagnostics)
    diag_last: dict = {}
    for side in (1, -1):
        s_eps = [e for e in starts if e.p_side == side]
        g_eps = [e for e in goals if e.p_side == side]
        if not s_eps or not g_eps:
            continue
        remaining = None
        if timeout_seconds is not None and timeout_seconds > 0:
            remaining = max(0.5, timeout_seconds - (time.monotonic() - t0))
        path, diag = cpp.route_centerline(
            starts=[(e.cell[0], e.cell[1], e.heading, ctx.layer, e.cost_cells) for e in s_eps],
            goals=[(e.cell[0], e.cell[1], e.heading, ctx.layer, e.cost_cells) for e in g_eps],
            p_net=ctx.p_net,
            n_net=ctx.n_net,
            half_pitch=ctx.half_pitch,
            p_side=side,
            p_half=ctx.p_width / 2.0,
            p_gap=ctx.p_gap,
            n_half=ctx.n_width / 2.0,
            n_gap=ctx.n_gap,
            min_radius_cells=1.0,
            turn_penalty=float(pf.rules.cost_turn),
            heuristic_weight=heuristic_weight,
            max_iterations_budget=max_iterations,
            timeout_seconds=float(remaining) if remaining else 0.0,
        )
        diag_last = diag
        if path is None:
            continue
        cost = len(path)
        if best is None or cost < best[0]:
            best = (cost, path, s_eps[diag["start_index"]], g_eps[diag["goal_index"]], diag)
    if best is None:
        pf.last_pose_report = {
            "applied": False,
            "reason": "search-failed",
            "iterations": diag_last.get("iterations", 0),
            "starts": len(starts),
            "goals": len(goals),
        }
        return None

    _, path, s_ep, g_ep, diag = best
    side = s_ep.p_side
    centre = [(c[0], c[1], c[2], c[3]) for c in path]
    p_mid = _derive_rail(centre, s_ep.heading, pf.grid, side * ctx.half_pitch)
    n_mid = _derive_rail(centre, s_ep.heading, pf.grid, -side * ctx.half_pitch)
    # End legs run pad -> trunk; the goal leg is reversed to run trunk -> pad.
    p_pts = _simplify(s_ep.p_leg[:-1] + p_mid + list(reversed(g_ep.p_leg[:-1])))
    n_pts = _simplify(s_ep.n_leg[:-1] + n_mid + list(reversed(g_ep.n_leg[:-1])))
    # The leg/trunk join points are computed two ways (cell-centre offset vs
    # miter offset); they coincide only when the first/last step keeps the
    # endpoint heading.  Snap-check, and refuse a mismatch rather than commit
    # a kinked join.
    if math.dist(s_ep.p_leg[-1], p_mid[0]) > 1e-3 or math.dist(s_ep.n_leg[-1], n_mid[0]) > 1e-3:
        pf.last_pose_report = {"applied": False, "reason": "start-join-mismatch"}
        return None
    if math.dist(g_ep.p_leg[-1], p_mid[-1]) > 1e-3 or math.dist(g_ep.n_leg[-1], n_mid[-1]) > 1e-3:
        pf.last_pose_report = {"applied": False, "reason": "goal-join-mismatch"}
        return None

    # Re-verify the FINISHED copper with the kernel + raster, not just the
    # per-step approximation the search used.
    if not (ctx.poly_clear(p_pts, "p") and ctx.poly_clear(n_pts, "n")):
        logger.debug("pose derived rails P=%s N=%s", p_pts, n_pts)
        pf.last_pose_report = {"applied": False, "reason": "derived-rails-blocked"}
        return None

    def _route(pts: list[tuple[float, float]], pad: Pad, width: float) -> Route:
        route = Route(net=pad.net, net_name=pad.net_name)
        layer = Layer(pf.grid.index_to_layer(ctx.layer))
        for a, b in zip(pts, pts[1:], strict=False):
            if math.dist(a, b) < 1e-4:
                continue
            route.segments.append(
                Segment(
                    x1=a[0],
                    y1=a[1],
                    x2=b[0],
                    y2=b[1],
                    width=width,
                    layer=layer,
                    net=pad.net,
                    net_name=pad.net_name,
                )
            )
        return route

    p_route = _route(p_pts, p_start, ctx.p_width)
    n_route = _route(n_pts, n_start, ctx.n_width)
    pf.last_pose_report = {
        "applied": True,
        "iterations": diag["iterations"],
        "centerline_poses": len(path),
        "start_setback_cost": s_ep.cost_cells,
        "goal_setback_cost": g_ep.cost_cells,
        "elapsed_s": time.monotonic() - t0,
    }
    return p_route, n_route
