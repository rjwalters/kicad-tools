#!/usr/bin/env python3
"""Expand-route-shrink, Phases 1-2: does routing an expanded placement help? (Issue #6304).

The proposal under test: place -> scale the placement ~2x so the router has
room -> route -> shrink back in small alternating-axis steps, repairing traces
each step. Before building any shrink loop, two cheap questions decide whether
it is worth building at all:

* **Phase 1 (``route``)** -- scale a board's placement by a factor and run the
  *unchanged* ``kct route``. If a board that is partial at 1x is still partial
  at 2x, nothing downstream of "route the expanded board" can help it.
* **Phase 2 (``gaps``)** -- for a board routed while expanded, count the
  inter-obstacle gaps that carry more copper than the same gap is wide at 1x.
  That number separates "shrinking is geometry" from "shrinking is re-topology".

What scales and what does not
-----------------------------
Scaled (an affine map about the centre of the ``Edge.Cuts`` bounding box):
footprint **positions**, board-outline graphics, every other top-level graphic,
and zone / keepout **outlines** (stale ``filled_polygon`` fills are dropped so
the router refills them).

NOT scaled: anything inside a footprint -- pad pitch, pad size, courtyard,
drills. A footprint moves; it does not grow. That is the whole point of the
experiment: pin-field escape geometry is identical at every scale, so nets that
fail because a pad cannot leave its own footprint are predicted not to benefit.

Two placement modes:

* ``uniform`` -- every footprint position is scaled independently. Simple, but
  it also pulls a decoupling capacitor away from the IC pin it serves, which is
  both electrically wrong and manufactures new long nets.
* ``rigid`` -- footprints are grouped into rigid clusters (a many-pad "anchor"
  plus the two-pad parts hugging it that share a net with it; see
  :func:`rigid_clusters`) and each cluster *translates* by the displacement of
  its centroid. Intra-cluster geometry is byte-for-byte the 1x geometry, only
  inter-cluster distance grows.

Deliberately not modelled: "fixed" parts. Edge connectors and mounting holes
are scaled like everything else, so a connector 2 mm from the edge is 4 mm
from it at 2x. That is harmless for Phase 1's question (the outline scales with
them, and the affine map is undone exactly by a shrink back to 1x), but it does
mean an expanded board is not a mechanically valid board in its own right.

Boards that arrive with copper (pre-routed diff pairs on 06, the 119 routed
connections on 07) are refused: a trace endpoint sits on a *pad*, and pads keep
their offset from the footprint origin, so affinely scaling the copper tears it
off the pads it terminates on. That is the same reason a shrink step needs a
repair pass and not just a coordinate transform.

Research-only: not wired into CI. Inputs are copied into ``--work-dir``; the
repo's ``boards/`` tree is never written.

Results, the measurement traps this harness guards against, and the verdict
(no-go on a shrink loop) are in
``docs/research/expand-route-shrink-experiment.md``. Read its "Four measurement
traps" before trusting a number from a new run: the auto-selected grid changes
with board size (use ``--grid-policy pinned`` for a clean A/B), and a run cut
off by ``--timeout`` leaves an artifact that is graded but is not a result.

Usage::

    uv run kct build-native --check
    uv run python scripts/research/expand_route_experiment.py route \\
        --boards 04 --grid-policy pinned --grid-mm 0.05 \\
        --scales 1.0,1.5,2.0 --modes uniform,rigid --timeout 600 \\
        --work-dir /tmp/expand-route --json /tmp/expand-route/route.json
    uv run python scripts/research/expand_route_experiment.py gaps \\
        --from-json /tmp/expand-route/route.json --json /tmp/expand-route/gaps.json
"""

from __future__ import annotations

import argparse
import contextlib
import json
import math
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]

# ---------------------------------------------------------------------------
# Boards
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Board:
    """One experiment board. Keys match ``krt_compare.BOARDS`` where they overlap."""

    key: str
    src: str  # repo-relative, or absolute for the out-of-tree chorus fixture
    kct_args: tuple[str, ...] = ()
    layers: int = 2  # copper layers, for the ``kct check`` gate
    manufacturer: str = "jlcpcb"
    note: str = ""


# The chorus fixture lives in a sibling repository. ``boards/external`` is a
# relative symlink that only resolves from the primary checkout, never from a
# ``.loom/worktrees/`` checkout, so the path is resolved against the primary
# checkout's parent and can be overridden with ``--chorus-pcb``.
CHORUS_REL = "chorus/hardware/chorus-test-revA/kicad/chorus-test-revA_v21_stripped.kicad_pcb"

# The same main-pass flags as ``scripts/route_chorus.py``'s ``R2_RECIPE_FLAGS``
# minus placement feedback (which moves parts, confounding a placement-scale
# A/B) and minus the completion/rescue stages (which are not ``kct route``).
CHORUS_ARGS = (
    "--manufacturer",
    "jlcpcb-tier1",
    "--backend",
    "cpp",
    "--layers",
    "4",
    "--no-auto-layers",
    "--micro-via-in-pad-fallback",
    "--deterministic-budget",
    "--per-net-iterations",
    "1000000",
    "--iterations",
    "50",
)

BOARDS: dict[str, Board] = {
    "00": Board("00", "boards/00-simple-led/output/simple_led.kicad_pcb", note="control"),
    "01": Board("01", "boards/01-voltage-divider/output/voltage_divider.kicad_pcb", note="control"),
    "02": Board("02", "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"),
    "03": Board("03", "boards/03-usb-joystick/output/usb_joystick.kicad_pcb", layers=4),
    "04": Board("04", "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb"),
    "05": Board("05", "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb", layers=4),
    "chorus": Board(
        "chorus",
        CHORUS_REL,
        kct_args=CHORUS_ARGS,
        layers=4,
        manufacturer="jlcpcb-tier1",
        note="out-of-tree fixture; main-pass recipe only",
    ),
}


def resolve_board_path(board: Board, chorus_pcb: Path | None = None) -> Path | None:
    """Absolute path of a board's unrouted input, or ``None`` when unavailable."""
    if board.key == "chorus":
        if chorus_pcb is not None:
            return chorus_pcb if chorus_pcb.exists() else None
        # REPO may be a worktree (<primary>/.loom/worktrees/issue-N); walk up
        # until a sibling ``chorus`` checkout is found.
        for parent in [REPO, *REPO.parents]:
            cand = parent.parent / CHORUS_REL
            if cand.exists():
                return cand
        return None
    path = REPO / board.src
    return path if path.exists() else None


# ---------------------------------------------------------------------------
# Placement scaling (pure; operates on the parsed S-expression tree)
# ---------------------------------------------------------------------------


class ScaleError(ValueError):
    """The board cannot be scaled honestly by this tool."""


#: Top-level tags that carry no board-space coordinates.
_INERT_TAGS = frozenset(
    {
        "version",
        "generator",
        "generator_version",
        "general",
        "paper",
        "title_block",
        "layers",
        "setup",
        "net",
        "net_class",
        "property",
        "embedded_fonts",
        "group",
    }
)
#: Top-level copper. Present copper means the board cannot be scaled affinely.
_COPPER_TAGS = frozenset({"segment", "via", "arc"})
#: Children of a graphic that hold one board-space point.
_POINT_TAGS = frozenset({"start", "end", "center", "mid", "at"})
#: Graphics whose shape is only preserved by a *uniform* scale.
_ROUND_TAGS = frozenset({"gr_arc", "gr_circle"})


@dataclass
class ScaleReport:
    """What :func:`scale_board_tree` did, for the results table and the tests."""

    scale_x: float
    scale_y: float
    mode: str
    origin: tuple[float, float]
    footprints_moved: int = 0
    graphics_scaled: int = 0
    zones_scaled: int = 0
    fills_dropped: int = 0
    clusters: int = 0
    clustered_footprints: int = 0  # footprints riding in a multi-part cluster
    outline_mm: tuple[float, float] = (0.0, 0.0)  # scaled outline bbox size

    def to_dict(self) -> dict:
        return {
            "scale_x": self.scale_x,
            "scale_y": self.scale_y,
            "mode": self.mode,
            "origin": [round(v, 4) for v in self.origin],
            "footprints_moved": self.footprints_moved,
            "graphics_scaled": self.graphics_scaled,
            "zones_scaled": self.zones_scaled,
            "fills_dropped": self.fills_dropped,
            "clusters": self.clusters,
            "clustered_footprints": self.clustered_footprints,
            "outline_mm": [round(v, 3) for v in self.outline_mm],
        }


def _xy(node) -> tuple[float, float]:
    return float(node.get_value(0)), float(node.get_value(1))


def _set_xy(node, x: float, y: float) -> None:
    node.set_value(0, round(x, 6))
    node.set_value(1, round(y, 6))


def _is_edge_cuts(node) -> bool:
    layer = node.find_child("layer")
    return layer is not None and layer.get_value(0) == "Edge.Cuts"


def _iter_points(node):
    """Every point-bearing child of a graphic / zone outline, recursively into ``pts``."""
    for child in node.iter_children():
        tag = child.tag
        if tag in _POINT_TAGS:
            yield child
        elif tag == "pts":
            for xy in child.iter_children():
                if xy.tag == "xy":
                    yield xy
        elif tag == "polygon":
            yield from _iter_points(child)


def outline_bbox(tree) -> tuple[float, float, float, float]:
    """``(min_x, min_y, max_x, max_y)`` of every top-level ``Edge.Cuts`` graphic.

    Uses the defining points only (an arc's bulge is ignored), which is exact
    for the rectangular and polygonal outlines every board in the fleet has and
    is only ever used to pick a scaling origin and report a size.
    """
    xs: list[float] = []
    ys: list[float] = []
    for child in tree.iter_children():
        if child.tag and child.tag.startswith("gr_") and _is_edge_cuts(child):
            for pt in _iter_points(child):
                x, y = _xy(pt)
                xs.append(x)
                ys.append(y)
    if not xs:
        raise ScaleError("board has no Edge.Cuts outline to scale about")
    return min(xs), min(ys), max(xs), max(ys)


def scale_board_tree(
    tree,
    scale_x: float,
    scale_y: float | None = None,
    *,
    mode: str = "uniform",
    clusters: list[list[int]] | None = None,
    origin: tuple[float, float] | None = None,
) -> ScaleReport:
    """Scale a parsed ``.kicad_pcb`` tree's placement in place.

    Args:
        tree: Root ``(kicad_pcb ...)`` node.
        scale_x / scale_y: Per-axis factors (``scale_y`` defaults to ``scale_x``).
        mode: ``"uniform"`` scales every footprint position; ``"rigid"``
            translates each cluster in *clusters* by its centroid's displacement.
        clusters: Footprint-index groups (indices into the tree's footprints in
            document order), required for ``mode="rigid"``. Every footprint must
            appear in exactly one group.
        origin: Fixed point of the map; defaults to the outline bbox centre.

    Raises:
        ScaleError: the board carries copper, an unrecognised top-level node,
            or a round outline under a non-uniform scale.
    """
    if scale_y is None:
        scale_y = scale_x
    if scale_x <= 0 or scale_y <= 0:
        raise ScaleError(f"scale factors must be positive, got ({scale_x}, {scale_y})")
    if mode not in ("uniform", "rigid"):
        raise ScaleError(f"unknown mode {mode!r}")
    uniform_scale = math.isclose(scale_x, scale_y)

    min_x, min_y, max_x, max_y = outline_bbox(tree)
    if origin is None:
        origin = ((min_x + max_x) / 2.0, (min_y + max_y) / 2.0)
    ox, oy = origin

    def mapped(x: float, y: float) -> tuple[float, float]:
        return ox + (x - ox) * scale_x, oy + (y - oy) * scale_y

    report = ScaleReport(scale_x, scale_y, mode, origin)
    footprints = []
    for child in tree.iter_children():
        tag = child.tag
        if tag in _INERT_TAGS:
            continue
        if tag in _COPPER_TAGS:
            raise ScaleError(
                f"board carries routed copper (top-level {tag!r}); trace endpoints sit on "
                "pads, which keep their footprint-relative offset, so an affine scale "
                "would tear the copper off its pads"
            )
        if tag == "footprint":
            footprints.append(child)
        elif tag and tag.startswith("gr_") or tag in ("dimension", "target", "image"):
            if tag in _ROUND_TAGS and not uniform_scale:
                raise ScaleError(
                    f"{tag} cannot be scaled non-uniformly (it would become elliptical)"
                )
            for pt in _iter_points(child):
                _set_xy(pt, *mapped(*_xy(pt)))
            report.graphics_scaled += 1
        elif tag == "zone":
            for pt in _iter_points(child):
                _set_xy(pt, *mapped(*_xy(pt)))
            while child.remove_child("filled_polygon"):
                report.fills_dropped += 1
            report.zones_scaled += 1
        else:
            raise ScaleError(
                f"unrecognised top-level node {tag!r}; refusing to guess how it scales"
            )

    positions = []
    for fp in footprints:
        at = fp.find_child("at")
        if at is None:
            raise ScaleError("footprint without an (at ...) node")
        positions.append(_xy(at))

    if mode == "rigid":
        if clusters is None:
            raise ScaleError("mode='rigid' needs clusters")
        seen = sorted(i for group in clusters for i in group)
        if seen != list(range(len(footprints))):
            raise ScaleError("clusters must partition the footprint indices exactly once")
        groups = clusters
    else:
        groups = [[i] for i in range(len(footprints))]

    for group in groups:
        cx = sum(positions[i][0] for i in group) / len(group)
        cy = sum(positions[i][1] for i in group) / len(group)
        nx, ny = mapped(cx, cy)
        dx, dy = nx - cx, ny - cy
        for i in group:
            x, y = positions[i]
            _set_xy(footprints[i].find_child("at"), x + dx, y + dy)
            report.footprints_moved += 1
        if len(group) > 1:
            report.clustered_footprints += len(group)
    report.clusters = len(groups)
    report.outline_mm = ((max_x - min_x) * scale_x, (max_y - min_y) * scale_y)
    return report


# ---------------------------------------------------------------------------
# Footprint geometry shared by the clustering and the gap analysis
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Body:
    """A footprint reduced to what blocks routing: its pad bounding box."""

    index: int
    ref: str
    rect: tuple[float, float, float, float]  # min_x, min_y, max_x, max_y (board frame)
    layers: frozenset[str]  # copper layers its pads occupy
    nets: frozenset[str]
    pad_count: int


def _pad_copper_layers(pad, copper_layers: list[str]) -> set[str]:
    out: set[str] = set()
    for layer in pad.layers:
        if layer == "*.Cu":
            out.update(copper_layers)
        elif layer.endswith(".Cu"):
            out.add(layer)
    return out


def footprint_bodies(pcb) -> list[Body]:
    """One :class:`Body` per footprint, in document order (pad-less ones skipped).

    The rectangle is the axis-aligned bounding box of the pad copper, with each
    pad taken as the bounding box of its (rotated) rectangle -- conservative for
    round and oval pads, and independent of courtyard/silkscreen drawing.
    """
    from kicad_tools.core.geometry import rotate_pad_offset

    copper = [layer.name for layer in pcb.copper_layers]
    bodies: list[Body] = []
    for index, fp in enumerate(pcb.footprints):
        if not fp.pads:
            continue
        fx, fy = fp.position
        xs: list[float] = []
        ys: list[float] = []
        layers: set[str] = set()
        nets: set[str] = set()
        for pad in fp.pads:
            dx, dy = rotate_pad_offset(pad.position[0], pad.position[1], fp.rotation or 0.0)
            ax, ay = fx + dx, fy + dy
            w, h = pad.size
            # Pad angle is stored absolute (already includes the footprint's).
            phi = math.radians(getattr(pad, "rotation", 0.0) or 0.0)
            half_w = (abs(w * math.cos(phi)) + abs(h * math.sin(phi))) / 2.0
            half_h = (abs(w * math.sin(phi)) + abs(h * math.cos(phi))) / 2.0
            xs += [ax - half_w, ax + half_w]
            ys += [ay - half_h, ay + half_h]
            layers |= _pad_copper_layers(pad, copper)
            if pad.net_name:
                nets.add(pad.net_name)
        bodies.append(
            Body(
                index,
                fp.reference,
                (min(xs), min(ys), max(xs), max(ys)),
                frozenset(layers),
                frozenset(nets),
                len(fp.pads),
            )
        )
    return bodies


def rect_gap(a, b) -> float:
    """Separating distance between two axis-aligned rectangles (0 when they touch/overlap)."""
    dx = max(a[0] - b[2], b[0] - a[2], 0.0)
    dy = max(a[1] - b[3], b[1] - a[3], 0.0)
    return math.hypot(dx, dy)


def rect_cut(a, b) -> tuple[tuple[float, float], tuple[float, float]]:
    """The shortest segment joining two disjoint axis-aligned rectangles.

    Where the rectangles overlap in one axis the cut is taken at the middle of
    the overlap interval. Any trace that passes *between* the two rectangles
    has to cross this segment, which is what makes it a usable capacity cut.
    """

    def axis(lo_a, hi_a, lo_b, hi_b):
        if hi_a < lo_b:
            return hi_a, lo_b
        if hi_b < lo_a:
            return lo_a, hi_b
        mid = (max(lo_a, lo_b) + min(hi_a, hi_b)) / 2.0
        return mid, mid

    xa, xb = axis(a[0], a[2], b[0], b[2])
    ya, yb = axis(a[1], a[3], b[1], b[3])
    return (xa, ya), (xb, yb)


def _segments_cross(p1, p2, q1, q2) -> bool:
    """Proper-or-touching intersection test for two closed segments."""

    def orient(a, b, c):
        v = (b[0] - a[0]) * (c[1] - a[1]) - (b[1] - a[1]) * (c[0] - a[0])
        return 0 if abs(v) < 1e-12 else (1 if v > 0 else -1)

    def on(a, b, c):
        return (
            min(a[0], b[0]) - 1e-9 <= c[0] <= max(a[0], b[0]) + 1e-9
            and min(a[1], b[1]) - 1e-9 <= c[1] <= max(a[1], b[1]) + 1e-9
        )

    o1, o2, o3, o4 = orient(p1, p2, q1), orient(p1, p2, q2), orient(q1, q2, p1), orient(q1, q2, p2)
    if o1 != o2 and o3 != o4:
        return True
    return (
        (o1 == 0 and on(p1, p2, q1))
        or (o2 == 0 and on(p1, p2, q2))
        or (o3 == 0 and on(q1, q2, p1))
        or (o4 == 0 and on(q1, q2, p2))
    )


def _segment_hits_rect(p1, p2, rect) -> bool:
    """Does the segment intersect the (closed) rectangle's interior or boundary?"""
    x0, y0, x1, y1 = rect
    for px, py in (p1, p2):
        if x0 < px < x1 and y0 < py < y1:
            return True
    corners = [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]
    return any(_segments_cross(p1, p2, corners[i], corners[(i + 1) % 4]) for i in range(4))


def rigid_clusters(
    pcb,
    *,
    anchor_min_pads: int = 6,
    satellite_max_pads: int = 2,
    max_gap_mm: float = 3.0,
) -> list[list[int]]:
    """Partition footprint indices into rigid clusters for ``mode="rigid"``.

    A footprint with at least *anchor_min_pads* pads is an **anchor** (ICs,
    connectors). A footprint with at most *satellite_max_pads* pads (caps,
    resistors, LEDs, ferrites) joins the nearest anchor whose pad bounding box
    is within *max_gap_mm* **and** with which it shares a net. Everything else
    -- including a two-pad part near an IC it is not wired to -- is a cluster
    of one, so it scales exactly as in ``uniform`` mode.

    Sharing a net is what distinguishes "this cap decouples that IC" from "this
    resistor happens to sit beside it"; without it a dense board collapses into
    one cluster and nothing expands at all.
    """
    bodies = footprint_bodies(pcb)
    anchors = [b for b in bodies if b.pad_count >= anchor_min_pads]
    owner: dict[int, int] = {}
    for body in bodies:
        if body.pad_count > satellite_max_pads:
            continue
        best: tuple[float, int] | None = None
        for anchor in anchors:
            if not (body.nets & anchor.nets):
                continue
            gap = rect_gap(body.rect, anchor.rect)
            if gap <= max_gap_mm and (best is None or gap < best[0]):
                best = (gap, anchor.index)
        if best is not None:
            owner[body.index] = best[1]
    groups: dict[int, list[int]] = {}
    for index in range(len(pcb.footprints)):
        key = owner.get(index, index)
        groups.setdefault(key, []).append(index)
    return [sorted(members) for _, members in sorted(groups.items())]


# ---------------------------------------------------------------------------
# Phase 2: gap capacity
# ---------------------------------------------------------------------------


@dataclass
class GapFinding:
    """One inter-obstacle gap that carries more copper than it is wide at 1x."""

    a: str
    b: str
    layer: str
    gap_1x_mm: float
    gap_scaled_mm: float
    traces: int
    through_traces: int  # crossing nets with no pad on either obstacle
    demand_mm: float
    through_demand_mm: float

    def to_dict(self) -> dict:
        return {k: (round(v, 3) if isinstance(v, float) else v) for k, v in vars(self).items()}


@dataclass
class GapReport:
    gaps_examined: int = 0
    loaded: list[GapFinding] = field(default_factory=list)  # every gap carrying copper
    clearance_mm: float = 0.0

    @property
    def overflow(self) -> list[GapFinding]:
        """Gaps whose whole bundle needs more width than the gap has at 1x."""
        return [g for g in self.loaded if g.demand_mm > g.gap_1x_mm + 1e-9]

    @property
    def through_overflow(self) -> int:
        """Gaps over capacity on through traffic alone (the conservative count)."""
        return sum(1 for g in self.loaded if g.through_demand_mm > g.gap_1x_mm + 1e-9)

    def to_dict(self) -> dict:
        tightest = sorted(self.loaded, key=lambda g: g.gap_1x_mm - g.demand_mm)
        return {
            "gaps_examined": self.gaps_examined,
            "gaps_carrying_copper": len(self.loaded),
            "gaps_over_1x_capacity": len(self.overflow),
            "gaps_over_1x_capacity_through_only": self.through_overflow,
            "clearance_mm": self.clearance_mm,
            # Tightest first whether or not anything overflows, so a clean "0"
            # can be read against how close the nearest gap came.
            "tightest": [g.to_dict() for g in tightest[:10]],
        }


def _demand_mm(widths: list[float], clearance: float) -> float:
    """Width a bundle of traces needs between two obstacles: copper + (n+1) clearances."""
    if not widths:
        return 0.0
    return sum(widths) + (len(widths) + 1) * clearance


def gap_overflow(pcb_1x, pcb_routed, *, clearance_mm: float) -> GapReport:
    """Count gaps whose expanded-board traffic would not fit the same gap at 1x.

    For every pair of footprints that share a copper layer and have a clear
    line of sight at 1x (the shortest segment joining their pad bounding boxes
    hits no third footprint on that layer), take the same pair on the routed,
    expanded board, draw the shortest segment between their pad boxes there,
    and collect the routed segments on the shared layer that cross it. One
    trace per net is counted. The pair "overflows" when the width that bundle
    needs -- trace widths plus a clearance each side and between neighbours --
    exceeds the gap's width at 1x.

    Approximations, all stated in the research note: pad bounding boxes stand
    in for obstacles; only footprint-to-footprint gaps are cut (not
    footprint-to-board-edge); vias sitting in a gap are not charged; and a
    layer is only charged where *both* footprints have pads on it, so traffic
    on an inner layer under two SMD parts is correctly free. ``through``
    counts drop nets that have a pad on either footprint, since a trace that
    ends on one of the two obstacles is using its own escape lane rather than
    transiting the gap -- the through-only figure is the conservative one.
    """
    # Keyed by reference, never by document index: ``kct route`` re-emits the
    # footprints in a different order than it read them.
    bodies_1x = {b.ref: b for b in footprint_bodies(pcb_1x)}
    bodies_s = {b.ref: b for b in footprint_bodies(pcb_routed)}
    report = GapReport(clearance_mm=clearance_mm)

    by_layer: dict[str, list[tuple]] = {}
    for seg in pcb_routed.segments:
        by_layer.setdefault(seg.layer, []).append(seg)

    indices = sorted(set(bodies_1x) & set(bodies_s))
    for pos, i in enumerate(indices):
        for j in indices[pos + 1 :]:
            a1, b1 = bodies_1x[i], bodies_1x[j]
            shared = a1.layers & b1.layers
            if not shared:
                continue
            gap_1x = rect_gap(a1.rect, b1.rect)
            if gap_1x <= 0.0:
                continue
            cut_1x = rect_cut(a1.rect, b1.rect)
            a_s, b_s = bodies_s[i], bodies_s[j]
            gap_s = rect_gap(a_s.rect, b_s.rect)
            if gap_s <= 0.0:
                continue
            cut_s = rect_cut(a_s.rect, b_s.rect)
            own_nets = a1.nets | b1.nets
            for layer in sorted(shared):
                blocked = any(
                    k not in (i, j)
                    and layer in bodies_1x[k].layers
                    and _segment_hits_rect(cut_1x[0], cut_1x[1], bodies_1x[k].rect)
                    for k in indices
                )
                if blocked:
                    continue
                report.gaps_examined += 1
                widths: dict[str, float] = {}
                for seg in by_layer.get(layer, ()):
                    if _segments_cross(cut_s[0], cut_s[1], seg.start, seg.end):
                        name = seg.net_name or f"#{seg.net_number}"
                        widths[name] = max(widths.get(name, 0.0), seg.width)
                if not widths:
                    continue
                through = {n: w for n, w in widths.items() if n not in own_nets}
                report.loaded.append(
                    GapFinding(
                        a1.ref,
                        b1.ref,
                        layer,
                        gap_1x,
                        gap_s,
                        len(widths),
                        len(through),
                        _demand_mm(list(widths.values()), clearance_mm),
                        _demand_mm(list(through.values()), clearance_mm),
                    )
                )
    return report


# ---------------------------------------------------------------------------
# Running the router and grading its output
# ---------------------------------------------------------------------------


def _check_same_order(tree, pcb) -> None:
    """Cluster indices come from ``PCB``; they are applied to the raw tree.

    Both are read from the same file, so they should agree footprint for
    footprint -- but an index mismatch would silently move the wrong parts, so
    it is checked (positions differ only by the board origin) rather than
    assumed.
    """
    ox, oy = pcb.board_origin
    nodes = [c for c in tree.iter_children() if c.tag == "footprint"]
    if len(nodes) != len(pcb.footprints):
        raise ScaleError("footprint count differs between the raw tree and the loaded PCB")
    for node, fp in zip(nodes, pcb.footprints, strict=True):
        x, y = _xy(node.find_child("at"))
        if not (
            math.isclose(x - ox, fp.position[0], abs_tol=1e-6)
            and math.isclose(y - oy, fp.position[1], abs_tol=1e-6)
        ):
            raise ScaleError(f"footprint order mismatch at {fp.reference}; cannot apply clusters")


def write_scaled_board(
    src: Path, dest_dir: Path, scale: float, mode: str
) -> tuple[Path, ScaleReport]:
    """Copy *src* into *dest_dir* with its placement scaled; returns the new path."""
    from kicad_tools.core.sexp_file import load_pcb, save_pcb

    dest_dir.mkdir(parents=True, exist_ok=True)
    tree = load_pcb(src)
    clusters = None
    if mode == "rigid":
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(src))
        clusters = rigid_clusters(pcb)
        _check_same_order(tree, pcb)
    report = scale_board_tree(tree, scale, mode=mode, clusters=clusters)
    dst = dest_dir / src.name
    save_pcb(tree, dst)
    for suffix in (".kicad_pro", ".kicad_dru"):
        sib = src.with_suffix(suffix)
        if sib.exists():
            shutil.copyfile(sib, dst.with_suffix(suffix))
    return dst, report


def _kill_group(proc: subprocess.Popen) -> None:
    for sig in (signal.SIGTERM, signal.SIGKILL):
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            return
        try:
            proc.wait(timeout=15)
            return
        except subprocess.TimeoutExpired:
            continue


def run_route(inp: Path, out: Path, extra: list[str], timeout: float, log: Path) -> dict:
    """Run ``kct route`` on *inp*. ``--timeout`` is the router's own hard budget;
    the subprocess is additionally killed (whole group) 5 minutes past it."""
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(inp),
        "-o",
        str(out),
        "--timeout",
        str(int(timeout)),
        *extra,
    ]
    env = dict(os.environ, PYTHONHASHSEED="0")
    killed = False
    started = time.monotonic()
    with log.open("w") as fh:
        proc = subprocess.Popen(
            cmd, cwd=REPO, stdout=fh, stderr=subprocess.STDOUT, env=env, start_new_session=True
        )
        try:
            rc = proc.wait(timeout=timeout + 300)
        except subprocess.TimeoutExpired:
            killed = True
            _kill_group(proc)
            rc = proc.returncode if proc.returncode is not None else -9
    return {
        "cmd": " ".join(cmd[1:]),
        "exit_code": rc,
        "wall_s": round(time.monotonic() - started, 1),
        "killed_by_harness": killed,
    }


_LOG_PATTERNS = {
    "clearance_mm": re.compile(r"^Clearance: ([\d.]+)mm", re.M),
    "coarse_grid_mm": re.compile(r"Coarse grid: ([\d.]+)mm"),
    "cell_estimate": re.compile(r"Total cell estimate: ([\d,]+)"),
    "layers_used": re.compile(r"^\s*Layer count: (\d+)", re.M),
}


def parse_route_log(text: str) -> dict:
    """Pull the facts that confound a scale A/B out of a ``kct route`` log.

    The grid is the important one: an auto-selected grid is cell-budgeted, so a
    4x-area board can be routed on a *coarser* grid than its 1x twin, and a
    completion delta would then be a grid effect, not a room effect.
    """
    out: dict = {}
    for key, pattern in _LOG_PATTERNS.items():
        match = pattern.search(text)
        if match:
            raw = match.group(1).replace(",", "")
            out[key] = float(raw) if "." in raw else int(raw)
    # The router's own tally, last occurrence (it prints one per attempt). Kept
    # beside the referee's count because the two disagree on partial boards.
    tallies = re.findall(r"Nets routed:\s+(\d+)/(\d+)", text)
    if tallies:
        out["router_nets"] = f"{tallies[-1][0]}/{tallies[-1][1]}"
    out["hit_timeout"] = bool(
        re.search(r"deadline (?:reached|exceeded)|stage deadline fired|\(deadline\)", text, re.I)
    )
    return out


#: The classifier's label for a pour net whose zone fill does not join its pads.
POUR_CLASS = "pour_discontinuous"


def grade(routed: Path, board: Board) -> dict:
    """Completion, both DRC engines, copper, and the stuck-class breakdown."""
    from kicad_tools.benchmark.external.metrics import (
        measure_completion,
        measure_copper,
        run_kct_check,
        run_kicad_cli_drc,
    )
    from kicad_tools.router.stuck_classifier import classify_stuck_nets

    comp = measure_completion(routed).to_dict()
    cop = measure_copper(routed).to_dict()
    check = run_kct_check(
        routed, manufacturer=board.manufacturer.split("-")[0], layers=board.layers
    )
    drc = run_kicad_cli_drc(routed, timeout=600)
    row = {
        "nets_complete": comp["nets_complete"],
        "nets_total": comp["nets_total"],
        "nets_blocking_incomplete": comp["nets_blocking_incomplete"],
        "connections_routed": comp["connections_routed"],
        "connections_total": comp["connections_total"],
        "via_count": cop["via_count"],
        "wirelength_mm": round(cop["wirelength_mm"], 1),
        "kct_check_errors": check.error_count if check.ran else None,
        "kct_check_by_rule": dict(sorted(check.errors_by_rule.items())),
        "kicad_drc_errors": drc.violation_count if drc.ran else None,
        "kicad_drc_by_type": dict(sorted(drc.by_type.items())),
        "kicad_drc_note": drc.note,
    }
    try:
        stuck = classify_stuck_nets(routed)
        row["stuck_counts"] = dict(stuck.counts)
        # A pour net whose fill is split is a zone problem, not a routing one;
        # keeping it out of this count is what makes the count comparable
        # across scales (connection totals are dominated by pour connectivity).
        row["signal_nets_unfinished"] = sum(
            1 for d in stuck.diagnoses if d.classification_value != POUR_CLASS
        )
        row["stuck_nets"] = {d.net_name: d.classification_value for d in stuck.diagnoses}
    except Exception as exc:  # the classifier must never sink a measured row
        row["stuck_error"] = f"{type(exc).__name__}: {exc}"
    return row


#: ``kct route``'s default ``--max-cells`` (the auto-grid memory budget).
DEFAULT_MAX_CELLS = 500_000


def grid_args(scale: float, policy: str, grid_mm: float | None = None) -> list[str]:
    """Extra ``kct route`` arguments for the chosen grid policy.

    The auto-selected grid is cell-budgeted, so a 4x-area board is routed on a
    coarser grid than its 1x twin -- or refused outright when the coarser grid
    fails the router's own ``clearance/2`` safety rule. Either way a completion
    delta would then measure grid pitch, not routing room.

    * ``pinned`` -- pass ``--grid <grid_mm>`` at **every** scale, 1x included.
      The only policy under which two rows differ in placement scale alone.
    * ``area`` -- scale the auto-grid cell budget with board area
      (``--max-cells 500000 * scale**2``). Kept because it is the obvious first
      thing to try, and it does not work: the auto-selector still picks a
      different pitch at different scales.
    * ``default`` -- pass nothing: the router exactly as a user would run it.
    """
    if policy == "pinned":
        if grid_mm is None:
            raise ValueError("grid policy 'pinned' needs --grid-mm")
        return ["--grid", f"{grid_mm:g}"]
    if policy == "default" or scale <= 1.0:
        return []
    if policy != "area":
        raise ValueError(f"unknown grid policy {policy!r}")
    return ["--max-cells", str(int(round(DEFAULT_MAX_CELLS * scale * scale)))]


def pick_artifact(routed: Path) -> tuple[Path | None, str | None]:
    """The board to grade: the ``-o`` output, else the deadline's best-so-far.

    When the hard ``--timeout`` fires, ``kct route`` exits 124 without writing
    the requested output. What it leaves depends on the stage it was in:

    * inside the routing stage -- a raw ``<stem>_partial.kicad_pcb`` holding the
      copper committed so far. The ``unverified_output`` its timeout sidecar
      names is, in that case, a snapshot with **no** copper at all (measured on
      chorus: 0 segments vs. 1318 mm in the ``_partial``), so the partial wins;
    * in a later stage -- no ``_partial``; the sidecar's ``unverified_output``
      is the last checkpoint.

    Either is still the honest answer to "what did this budget buy", so it is
    graded -- and labelled ``deadline`` so it is never mistaken for a finished
    run: it has had no optimisation, zone fill or DRC repair, and which stage
    the deadline landed in is itself load-dependent.
    """
    if routed.exists():
        return routed, "final"
    partial = routed.with_name(f"{routed.stem}_partial.kicad_pcb")
    if partial.exists():
        return partial, "deadline"
    sidecar = routed.with_suffix(".timeout.json")
    if sidecar.exists():
        with contextlib.suppress(OSError, ValueError):
            named = json.loads(sidecar.read_text()).get("unverified_output")
            if named and Path(named).exists():
                return Path(named), "deadline"
    return None, None


def run_one(
    board: Board,
    src: Path,
    scale: float,
    mode: str,
    work_dir: Path,
    seed: int,
    timeout: float,
    grid_policy: str = "area",
    grid_mm: float | None = None,
    search_timeout: float | None = None,
    extra_args: tuple[str, ...] = (),
) -> dict:
    suffix = "" if grid_policy == "area" else f"-{grid_policy}"
    run_dir = work_dir / f"{board.key}-{mode}-{scale:g}{suffix}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    row: dict = {
        "board": board.key,
        "scale": scale,
        "mode": mode,
        "grid_policy": grid_policy,
    }
    try:
        inp, report = write_scaled_board(src, run_dir, scale, mode)
    except ScaleError as exc:
        row["error"] = f"could not scale: {exc}"
        return row
    row["scale_report"] = report.to_dict()
    row["input"] = str(inp)
    routed = run_dir / f"{src.stem}_routed.kicad_pcb"
    log = run_dir / "route.log"
    extra = [
        *board.kct_args,
        *extra_args,
        "--seed",
        str(seed),
        *grid_args(scale, grid_policy, grid_mm),
    ]
    if search_timeout is not None:
        # Bound each search stage below the hard total so the run finishes its
        # save / zone fill / DRC instead of dying at the deadline mid-stage.
        extra += ["--search-timeout", str(int(search_timeout))]
        row["search_timeout_s"] = search_timeout
    row.update(run_route(inp, routed, extra, timeout, log))
    row.update(parse_route_log(log.read_text(errors="replace")))
    if grid_policy == "pinned":
        row["pinned_grid_mm"] = grid_mm
    artifact, kind = pick_artifact(routed)
    if artifact is not None:
        row["routed"] = str(artifact)
        row["artifact"] = kind
        row.update(grade(artifact, board))
    else:
        row["error"] = f"no routed output (exit_code={row['exit_code']})"
    with contextlib.suppress(OSError):
        row["loadavg_end"] = [round(x, 2) for x in os.getloadavg()]
    return row


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def _fmt_counts(counts: dict | None) -> str:
    shown = {k: v for k, v in (counts or {}).items() if v}
    if not shown:
        return "--"
    return ", ".join(f"{k} {v}" for k, v in sorted(shown.items()))


def _timeout_cell(row: dict) -> str:
    if row.get("artifact") == "deadline":
        return "yes (graded best-so-far)"
    return "yes" if row.get("hit_timeout") or row.get("killed_by_harness") else "no"


def render_route(rows: list[dict]) -> str:
    out = [
        "| Board | Mode | Scale | Outline mm | Nets | Signal nets unfinished | Router's own tally "
        "| Connections | kct check err "
        "| kicad-cli DRC err | Vias | Wire mm | Grid mm | Timed out | Wall s | Stuck classes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        size = r.get("scale_report", {}).get("outline_mm")
        outline = f"{size[0]:g}x{size[1]:g}" if size else "--"
        if "nets_complete" not in r:
            out.append(
                f"| {r['board']} | {r['mode']} | {r['scale']:g} | {outline} | ERROR: "
                f"{r.get('error', '?')} | -- | -- | -- | -- | -- | -- | -- | -- | -- "
                f"| {r.get('wall_s', '--')} | -- |"
            )
            continue
        grid = r.get("pinned_grid_mm") or r.get("coarse_grid_mm", "--")
        if r.get("pinned_grid_mm"):
            grid = f"{grid} (pinned)"
        out.append(
            f"| {r['board']} | {r['mode']} | {r['scale']:g} | {outline} "
            f"| {r['nets_complete']}/{r['nets_total']} "
            f"| {r.get('signal_nets_unfinished', '--')} | {r.get('router_nets', '--')} "
            f"| {r['connections_routed']}/{r['connections_total']} "
            f"| {r['kct_check_errors']} | {r['kicad_drc_errors']} "
            f"| {r['via_count']} | {r['wirelength_mm']} | {grid} "
            f"| {_timeout_cell(r)} "
            f"| {r['wall_s']} | {_fmt_counts(r.get('stuck_counts'))} |"
        )
    return "\n".join(out)


def stuck_transitions(rows: list[dict]) -> list[dict]:
    """For each board+mode: what happened, at each larger scale, to the nets stuck at 1x.

    The classifier labels a net by why it is stuck *on that board*; the
    prediction under test is about the 1x label, so each net keeps its 1x class
    and is then looked up in every expanded run.
    """
    base = {r["board"]: r for r in rows if math.isclose(r["scale"], 1.0) and "stuck_nets" in r}
    out: list[dict] = []
    for r in rows:
        if math.isclose(r["scale"], 1.0) or "stuck_nets" not in r or r["board"] not in base:
            continue
        per_class: dict[str, Counter] = {}
        for net, cls in base[r["board"]]["stuck_nets"].items():
            fate = "still stuck" if net in r["stuck_nets"] else "completed"
            per_class.setdefault(cls, Counter())[fate] += 1
        newly = sorted(set(r["stuck_nets"]) - set(base[r["board"]]["stuck_nets"]))
        out.append(
            {
                "board": r["board"],
                "mode": r["mode"],
                "scale": r["scale"],
                "by_1x_class": {k: dict(v) for k, v in sorted(per_class.items())},
                "newly_stuck": {n: r["stuck_nets"][n] for n in newly},
            }
        )
    return out


def render_transitions(transitions: list[dict]) -> str:
    out = [
        "| Board | Mode | Scale | 1x stuck class | Stuck at 1x | Completed when expanded "
        "| Still stuck | Newly stuck at this scale (complete at 1x) |",
        "|---|---|---|---|---|---|---|---|",
    ]
    for t in transitions:
        newly = _fmt_counts(Counter(t["newly_stuck"].values())) if t["newly_stuck"] else "0"
        head = f"| {t['board']} | {t['mode']} | {t['scale']:g} "
        if not t["by_1x_class"]:
            out.append(f"{head}| (none) | 0 | -- | -- | {newly} |")
        # "Newly stuck" is a property of the board at this scale, not of a 1x
        # class, so it is printed once -- on the first class row -- not repeated.
        for i, (cls, fates) in enumerate(t["by_1x_class"].items()):
            done, still = fates.get("completed", 0), fates.get("still stuck", 0)
            out.append(
                f"{head}| {cls} | {done + still} | {done} | {still} | {newly if i == 0 else ''} |"
            )
    return "\n".join(out)


def render_gaps(rows: list[dict]) -> str:
    out = [
        "| Board | Mode | Scale | Nets | Gaps examined | Carrying copper | Over 1x capacity "
        "| ...on through traffic alone | Tightest (1x gap mm / demand mm / traces) |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        g = r["gaps"]
        worst = "; ".join(
            f"{w['a']}-{w['b']} {w['layer']} {w['gap_1x_mm']}/{w['demand_mm']}/{w['traces']}"
            for w in g["tightest"][:3]
        )
        out.append(
            f"| {r['board']} | {r['mode']} | {r['scale']:g} | {r['nets']} | {g['gaps_examined']} "
            f"| {g['gaps_carrying_copper']} | {g['gaps_over_1x_capacity']} "
            f"| {g['gaps_over_1x_capacity_through_only']} | {worst or '--'} |"
        )
    return "\n".join(out)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _cmd_route(args) -> int:
    keys = [k.strip() for k in args.boards.split(",") if k.strip()]
    scales = [float(s) for s in args.scales.split(",") if s.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    for key in keys:
        if key not in BOARDS:
            raise SystemExit(f"unknown board {key!r}; known: {','.join(BOARDS)}")
    args.work_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    if args.json and args.append and args.json.exists():
        rows = json.loads(args.json.read_text())["rows"]
    for key in keys:
        board = BOARDS[key]
        src = resolve_board_path(board, args.chorus_pcb)
        for mode in modes:
            for scale in scales:
                # 1x is identical in every mode; measure it once, under "uniform".
                if math.isclose(scale, 1.0) and mode != modes[0]:
                    continue
                print(f"==> board {key}, mode {mode}, scale {scale:g}", flush=True)
                if src is None:
                    row = {"board": key, "scale": scale, "mode": mode, "error": "input not found"}
                else:
                    row = run_one(
                        board,
                        src,
                        scale,
                        mode,
                        args.work_dir,
                        args.seed,
                        args.timeout,
                        args.grid_policy,
                        args.grid_mm,
                        args.search_timeout,
                        tuple(args.extra.split()),
                    )
                rows.append(row)
                print(json.dumps({k: v for k, v in row.items() if k != "stuck_nets"}), flush=True)
                if args.json:
                    args.json.parent.mkdir(parents=True, exist_ok=True)
                    payload = {"rows": rows, "transitions": stuck_transitions(rows)}
                    args.json.write_text(json.dumps(payload, indent=2) + "\n")
    print()
    print(render_route(rows))
    print()
    print(render_transitions(stuck_transitions(rows)))
    return 0


def _cmd_gaps(args) -> int:
    from kicad_tools.schema.pcb import PCB

    rows = json.loads(args.from_json.read_text())["rows"]
    base = {r["board"]: r for r in rows if math.isclose(r["scale"], 1.0) and "input" in r}
    out: list[dict] = []
    for r in rows:
        if "routed" not in r or r["board"] not in base:
            continue
        clearance = args.clearance_mm or r.get("clearance_mm") or 0.15
        report = gap_overflow(
            PCB.load(base[r["board"]]["input"]), PCB.load(r["routed"]), clearance_mm=clearance
        )
        out.append(
            {
                "board": r["board"],
                "mode": r["mode"],
                "scale": r["scale"],
                "nets": f"{r.get('nets_complete')}/{r.get('nets_total')}",
                "gaps": report.to_dict(),
            }
        )
        print(f"==> {r['board']} {r['mode']} {r['scale']:g}: {out[-1]['gaps']}", flush=True)
    if args.json:
        args.json.write_text(json.dumps(out, indent=2) + "\n")
    print()
    print(render_gaps(out))
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="command", required=True)

    route = sub.add_parser("route", help="Phase 1: route each board at each placement scale")
    route.add_argument("--boards", default="02,03,04,05")
    route.add_argument("--scales", default="1.0,1.5,2.0")
    route.add_argument("--modes", default="uniform", help="comma list of uniform,rigid")
    route.add_argument("--work-dir", type=Path, required=True)
    route.add_argument("--json", type=Path)
    route.add_argument("--append", action="store_true", help="extend an existing --json")
    route.add_argument("--seed", type=int, default=42, help="same seed in every arm")
    route.add_argument("--timeout", type=float, default=600.0, help="kct route --timeout, s")
    route.add_argument(
        "--grid-policy",
        choices=("pinned", "area", "default"),
        default="area",
        help="pinned: --grid GRID_MM at every scale (the clean A/B); area: scale "
        "--max-cells with board area; default: the router exactly as shipped",
    )
    route.add_argument("--grid-mm", type=float, help="grid pitch for --grid-policy pinned")
    route.add_argument(
        "--search-timeout",
        type=float,
        help="kct route --search-timeout, s; set below --timeout so a slow board "
        "finishes post-processing instead of being cut off at the deadline",
    )
    route.add_argument(
        "--extra",
        default="",
        help="extra kct route arguments for every run, as one quoted string",
    )
    route.add_argument("--chorus-pcb", type=Path, help="override the chorus fixture path")
    route.set_defaults(func=_cmd_route)

    gaps = sub.add_parser("gaps", help="Phase 2: gaps carrying more copper than fits at 1x")
    gaps.add_argument("--from-json", type=Path, required=True, help="a `route --json` file")
    gaps.add_argument("--json", type=Path)
    gaps.add_argument("--clearance-mm", type=float, help="override the logged clearance")
    gaps.set_defaults(func=_cmd_gaps)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
