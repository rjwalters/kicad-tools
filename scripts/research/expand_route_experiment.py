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

Issue #6312 added three subcommands that route nothing, and they are the
cheap checks to run before spending route time on a new comparison:

* ``clusters`` -- what a rigid-clustering rule does to each board;
* ``plan`` -- the grid plan (every fine zone) each scaled board would get;
* ``summary`` -- the per-row table of one or more ``route --json`` files, with
  a zone-checked unfinished-net count and an excluded-net column.

``route`` always passes ``--no-cache``: without it a repeated row replays the
first run's copper instead of routing.

Grading (Issue #6420). Each row's DRC columns are meant to equal what a
person would get by hand, so the harness does not under-count:

* ``kct_check_*`` runs the ``kct check`` engine with the run dir's
  ``net_class_map.json`` (diff-pair and match-group skew rules engage,
  net-class clearances apply) and the manufacturer ``kct route`` recorded in
  ``fab_profile.json`` -- the same numbers as ``kct check --mfr <tier>
  --allow-incomplete`` on the routed file, including the CLI-level rules
  that read the sibling ``.kicad_pro`` (``netclass_clearance_copper``,
  ``netclass_floor``): ``run_kct_check`` shares the CLI's rule dispatch
  rather than calling ``DRCChecker.check_all()``. With no sidecar the row says so
  (``kct_check_net_class_map: null``) and lists ``kct_check_inactive_rules``.
* ``kicad_drc_*`` runs ``kicad-cli pcb drc --refill-zones --all-track-errors``
  and records ``kicad_drc_unconnected``.
* ``manufacturable`` is ``true`` only when ``kct route`` exited 0, both
  engines report 0 errors, kicad-cli reports 0 unconnected items and the
  graded board is the finished output; ``null`` when either engine did not run.
  For connectivity the verdict trusts kicad-cli's unconnected count, not the
  ``Nets`` / ``Connections`` columns. Those come from the in-process
  ``NetStatusAnalyzer`` and can disagree with KiCad on exposed-pad
  thermal-via footprints: board 05 lattice reads 36/37 (GND split at U2/U3
  EPs) where kicad-cli and ``kct check`` both report 0 unconnected
  (issue #6435).
* A grid-engine route under ``--deterministic-budget`` or
  ``--per-net-iterations`` also gets ``--deterministic-rescue``, so the
  relief-rescue sub-searches are iteration-bounded and a row does not depend
  on host load; every row records ``deterministic_rescue``, and
  ``rescue_wall_clock`` / ``sweep_wall_clock`` say which wall clocks the
  route log reports as live.

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
    #: Regex of nets whose copper is ripped from the input before routing
    #: (``06b``: ``LVDS\\d_[PN]``). Only the ``pairs`` subcommand honours it.
    strip_nets: str | None = None
    #: Repo-relative committed ``net_class_map.json`` handed to ``kct route
    #: --net-class-map`` by the ``pairs`` and ``route`` subcommands, so the
    #: router and ``kct check`` resolve the same per-pair thresholds
    #: (Issues #6442, #6829).
    net_class_map: str | None = None
    #: ``True`` for rows that only make sense under ``pairs`` (they carry
    #: copper the ``route`` scaler would tear off its pads, or need a strip).
    pair_bench_only: bool = False


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
    "03": Board(
        "03",
        "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
        layers=4,
        # Route under the same tier-1 sidecars the recipe fills with, so the
        # plan's pour links hold under the recipe's 0.3 mm native zone
        # clearance and not just under a plain ``jlcpcb`` fill (Issue #7026).
        # ``--native-zone-clearance`` writes that native block before the fill;
        # the tier alone does not (Issue #7026).
        kct_args=("--manufacturer", "jlcpcb-tier1", "--native-zone-clearance"),
        manufacturer="jlcpcb-tier1",
        # USB_D carries a 25 % continuity threshold here that the classifier's
        # own map does not, and a map-less route holds it to the generic
        # 0.5 mm skew default instead of USB2_FS (Issue #6829).
        net_class_map="boards/03-usb-joystick/output/net_class_map.json",
    ),
    # Diff-pair benchmark rows (Issue #6442; ``pairs`` subcommand only).
    # 06b: the committed LVDS board with every ``LVDS<n>_[PN]`` track and via
    # ripped (ported from ``krt_compare.BOARDS["06b"]``); GND/+3V3 plane vias
    # stay, hence ``--preserve-existing``.
    "06b": Board(
        "06b",
        "boards/06-diffpair-test/output/diffpair_test.kicad_pcb",
        kct_args=("--preserve-existing",),
        layers=4,
        note="LVDS copper ripped; 4 pairs + 8 LVTTL nets",
        strip_nets=r"LVDS\d_[PN]",
        net_class_map="boards/06-diffpair-test/output/net_class_map.json",
        pair_bench_only=True,
    ),
    # 06rf: the historical synthetic USB / PCIe / MIPI regression fixture
    # (9 pairs, no copper on any of them).
    "06rf": Board(
        "06rf",
        "boards/06-diffpair-test/regression-fixture/diffpair_test.kicad_pcb",
        layers=4,
        note="regression fixture: 9 USB/PCIe/MIPI pairs",
        net_class_map="boards/06-diffpair-test/regression-fixture/net_class_map.json",
        pair_bench_only=True,
    ),
    # 07rf: the match-group regression fixture (no copper on any pair).
    "07rf": Board(
        "07rf",
        "boards/07-matchgroup-test/regression-fixture/matchgroup_test.kicad_pcb",
        layers=4,
        note="regression fixture: match-group pairs",
        net_class_map="boards/07-matchgroup-test/regression-fixture/net_class_map.json",
        pair_bench_only=True,
    ),
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


def net_class_map_args(board: Board) -> list[str]:
    """``--net-class-map <committed map>`` for *board*, or ``[]`` if it has none.

    A board that declares a committed map must route with it: without it the
    router falls back to the generic pair limits (Issue #6829). A declared map
    that is missing on disk is a loud failure, never a silent generic fallback.
    """
    if not board.net_class_map:
        return []
    path = REPO / board.net_class_map
    if not path.is_file():
        raise FileNotFoundError(f"board {board.key}: committed net-class map not found: {path}")
    return ["--net-class-map", str(path)]


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
    cluster_rule: dict | None = None  # rigid_clusters() parameters; None in uniform mode

    def to_dict(self) -> dict:
        return {
            "cluster_rule": self.cluster_rule,
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


def _satellite_owners(
    bodies: list[Body], anchor_min_pads: int, satellite_max_pads: int, max_gap_mm: float
) -> dict[int, int]:
    """Footprint index of each satellite -> footprint index of the anchor it joins."""
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
    return owner


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
    owner = _satellite_owners(
        footprint_bodies(pcb), anchor_min_pads, satellite_max_pads, max_gap_mm
    )
    groups: dict[int, list[int]] = {}
    for index in range(len(pcb.footprints)):
        key = owner.get(index, index)
        groups.setdefault(key, []).append(index)
    return [sorted(members) for _, members in sorted(groups.items())]


@dataclass(frozen=True)
class ClusterRule:
    """The three :func:`rigid_clusters` parameters, as one value (Issue #6312).

    The defaults are the rule every row in the research note up to #6311 was
    run with; they exclude nearly all of chorus-test, whose ICs have five pads.
    """

    anchor_min_pads: int = 6
    satellite_max_pads: int = 2
    max_gap_mm: float = 3.0

    def to_dict(self) -> dict:
        return {
            "anchor_min_pads": self.anchor_min_pads,
            "satellite_max_pads": self.satellite_max_pads,
            "max_gap_mm": self.max_gap_mm,
        }


DEFAULT_RULE = ClusterRule()


def cluster_report(pcb, rule: ClusterRule = DEFAULT_RULE) -> dict:
    """What *rule* does to one board, without routing anything (Issue #6312).

    Reports the funnel (footprints -> anchors -> small parts -> small parts
    near an anchor -> ...that also share a net with it) and each multi-part
    cluster by reference, with the nets every satellite shares with its
    anchor. That last column is what tells "a cap on this IC's own pin" from
    "a part that only shares ``GND`` with it": when the shared net is always a
    plane net, the shared-net test filters nothing and nearest-anchor is doing
    all the work.
    """
    bodies = footprint_bodies(pcb)
    by_index = {b.index: b for b in bodies}
    anchors = [b for b in bodies if b.pad_count >= rule.anchor_min_pads]
    small = [b for b in bodies if b.pad_count <= rule.satellite_max_pads]

    def near(body: Body) -> list[Body]:
        return [
            a
            for a in anchors
            if a.index != body.index and rect_gap(body.rect, a.rect) <= rule.max_gap_mm
        ]

    small_near = [b for b in small if near(b)]
    small_near_sharing = [b for b in small_near if any(b.nets & a.nets for a in near(b))]
    groups = rigid_clusters(pcb, **rule.to_dict())
    multi = [g for g in groups if len(g) > 1]
    owners = _satellite_owners(
        bodies, rule.anchor_min_pads, rule.satellite_max_pads, rule.max_gap_mm
    )
    by_anchor: dict[int, list[int]] = {}
    for satellite, anchor in owners.items():
        by_anchor.setdefault(anchor, []).append(satellite)
    detail = [
        {
            "anchor": by_index[anchor].ref,
            "anchor_pads": by_index[anchor].pad_count,
            "satellites": {
                by_index[i].ref: sorted(by_index[i].nets & by_index[anchor].nets)
                for i in sorted(satellites)
            },
        }
        for anchor, satellites in sorted(by_anchor.items())
    ]
    histogram = Counter(b.pad_count for b in bodies)
    return {
        "rule": rule.to_dict(),
        "footprints": len(pcb.footprints),
        "pad_count_histogram": {str(k): histogram[k] for k in sorted(histogram)},
        "anchors": len(anchors),
        "small_parts": len(small),
        "small_parts_near_an_anchor": len(small_near),
        "small_parts_near_and_sharing_a_net": len(small_near_sharing),
        "multi_part_clusters": len(multi),
        "cluster_sizes": sorted((len(g) for g in multi), reverse=True),
        "footprints_in_multi_part_clusters": sum(len(g) for g in multi),
        "clusters": detail,
    }


def render_clusters(rows: list[dict]) -> str:
    out = [
        "| Board | anchor_min_pads, satellite_max_pads, max_gap_mm | Footprints | Anchors "
        "| Small parts | ...near an anchor | ...and sharing a net | Multi-part clusters (sizes) "
        "| Footprints in them |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "error" in r:
            out.append(f"| {r['board']} | -- | ERROR: {r['error']} | -- | -- | -- | -- | -- | -- |")
            continue
        rule = r["rule"]
        sizes = ", ".join(str(n) for n in r["cluster_sizes"])
        out.append(
            f"| {r['board']} | {rule['anchor_min_pads']}, {rule['satellite_max_pads']}, "
            f"{rule['max_gap_mm']:g} | {r['footprints']} | {r['anchors']} | {r['small_parts']} "
            f"| {r['small_parts_near_an_anchor']} | {r['small_parts_near_and_sharing_a_net']} "
            f"| {r['multi_part_clusters']} ({sizes or '--'}) "
            f"| {r['footprints_in_multi_part_clusters']} |"
        )
    return "\n".join(out)


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
    src: Path,
    dest_dir: Path,
    scale: float,
    mode: str,
    cluster_rule: ClusterRule = DEFAULT_RULE,
) -> tuple[Path, ScaleReport]:
    """Copy *src* into *dest_dir* with its placement scaled; returns the new path.

    *cluster_rule* is used (and recorded in the report) in ``rigid`` mode only.
    """
    from kicad_tools.core.sexp_file import load_pcb, save_pcb

    dest_dir.mkdir(parents=True, exist_ok=True)
    tree = load_pcb(src)
    clusters = None
    if mode == "rigid":
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(src))
        clusters = rigid_clusters(pcb, **cluster_rule.to_dict())
        _check_same_order(tree, pcb)
    report = scale_board_tree(tree, scale, mode=mode, clusters=clusters)
    if mode == "rigid":
        report.cluster_rule = cluster_rule.to_dict()
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


_FINE_ZONE = re.compile(r"^\s+(\S+): ([\d.]+)mm \([\d.]+x[\d.]+mm, ~([\d,]+) cells\)", re.M)


def parse_grid_plan(text: str) -> dict:
    """The auto-selected grid plan from a ``kct route`` log: coarse pitch and every fine zone.

    The coarse pitch alone does not make two rows grid-matched. The plan also
    gives each dense part its own finer zone, and the pitch chosen for a zone
    depends on where the part lands: on board 05 the driver IC's zone is
    0.05 mm at 1x and 0.005 mm -- a hundred times the cells -- at some scales
    and not others, at the same 0.065 mm coarse pitch (Issue #6312).
    """
    out: dict = {}
    for key in ("coarse_grid_mm", "cell_estimate"):
        match = _LOG_PATTERNS[key].search(text)
        if match:
            raw = match.group(1).replace(",", "")
            out[key] = float(raw) if "." in raw else int(raw)
    out["fine_zones"] = {
        ref: {"pitch_mm": float(pitch), "cells": int(cells.replace(",", ""))}
        for ref, pitch, cells in _FINE_ZONE.findall(text)
    }
    return out


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
        # Every row must be a route, not a replay of an earlier row's cached
        # copper: without this a same-seed replicate is a cache hit.
        "--no-cache",
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
    # "stage deadline fired --" with its dashes: under --deterministic-budget
    # the router prints a startup WARNING that *quotes* the marker ("a
    # '[deterministic-budget] stage deadline fired' log line will mark ..."),
    # and matching the bare phrase flagged every such run as timed out.
    out["hit_timeout"] = bool(
        re.search(r"deadline (?:reached|exceeded)|stage deadline fired --|\(deadline\)", text, re.I)
    )
    # ``kct route`` keeps a result cache keyed on the board bytes and the CLI
    # recipe. A hit replays stored copper and routes nothing, so a "repeat" of
    # a row is not a second sample. The harness passes --no-cache; this records
    # the fact per row so a hit can never pass silently (Issue #6312).
    out["cache_hit"] = bool(re.search(r"^\s*Cache HIT", text, re.M))
    # Wall-clock bounds that were live in this route. Either one makes the
    # copper load-dependent, so two rows that differ only in host load can
    # land different boards (Issue #6420). ``rescue_wall_clock``: the
    # relief-rescue probe / victim re-land sub-searches kept their wall clock
    # (no --deterministic-rescue). ``sweep_wall_clock``: the post-negotiation
    # sweep ran under a wall clock rather than the iteration cap (#4159) --
    # recorded, not fixed; it binds only on a slow enough host. Both are read
    # from the router's own bound lines, which it prints only when the stage
    # runs; the CLI's up-front "[deterministic-budget] NOTE: ... keep their
    # 10s wall clock" is printed for every engine, lattice included, so it is
    # not evidence. A key is omitted when the log says nothing either way.
    if re.search(r"Relief-rescue sub-search cap: [\d.]+s wall clock", text):
        out["rescue_wall_clock"] = True
    elif "Relief-rescue sub-search cap: iteration-bounded" in text:
        out["rescue_wall_clock"] = False
    if re.search(r"Post-negotiation sweep bound: [\d.]+s whole-sweep", text):
        out["sweep_wall_clock"] = True
    elif "Post-negotiation sweep bound: iteration-bounded" in text:
        out["sweep_wall_clock"] = False
    return out


def manufacturable(row: dict) -> bool | None:
    """The one-word verdict: is this routed board fit to send to fab?

    ``True`` only when every gate agrees: ``kct route`` exited 0, ``kct
    check`` (every error-severity rule) and kicad-cli both report 0 errors,
    kicad-cli reports 0 unconnected items, and the graded board is the
    finished output, not a deadline best-so-far. ``None`` -- never ``False``
    -- when either DRC engine did not run: "not checked" is not a verdict
    (Issue #6420).

    kicad-cli is trusted over the in-process ``Nets`` count: that count comes
    from ``NetStatusAnalyzer`` and can disagree with kicad-cli on exposed-pad
    / thermal-via footprints, so it is deliberately not a gate here.
    """
    kct = row.get("kct_check_errors_total", row.get("kct_check_errors"))
    cli = row.get("kicad_drc_errors")
    unconnected = row.get("kicad_drc_unconnected")
    if kct is None or cli is None or unconnected is None:
        return None
    return (
        row.get("exit_code") == 0
        and kct == 0
        and cli == 0
        and unconnected == 0
        and row.get("artifact") == "final"
    )


def _flag_value(args: list[str], flag: str) -> str | None:
    """The value of ``flag`` in an argv list (``--flag v`` or ``--flag=v``), else ``None``."""
    for i, arg in enumerate(args):
        if arg == flag and i + 1 < len(args):
            return args[i + 1]
        if arg.startswith(f"{flag}="):
            return arg.split("=", 1)[1]
    return None


def with_deterministic_rescue(extra: list[str]) -> list[str]:
    """Add ``--deterministic-rescue`` to a deterministic grid-engine route.

    Under ``--deterministic-budget`` / ``--per-net-iterations`` the A* loop is
    iteration-bounded, but the relief-rescue probe and victim re-land
    sub-searches keep a 10 s wall clock unless ``--deterministic-rescue`` is
    passed (#5870), so the #6342 protocol's grid rows depended on host load:
    board 05 measured 32/33 in one run and 29-30/33 in another. With it, two
    runs at a 4x different load were byte-identical (Issue #6420). Lattice
    rows are left untouched.
    """
    if "--deterministic-rescue" in extra:
        return list(extra)
    if _flag_value(extra, "--route-engine") == "lattice":
        return list(extra)
    deterministic = (
        "--deterministic-budget" in extra or _flag_value(extra, "--per-net-iterations") is not None
    )
    return [*extra, "--deterministic-rescue"] if deterministic else list(extra)


#: The classifier's label for a pour net whose zone fill does not join its pads.
POUR_CLASS = "pour_discontinuous"

#: Keys of ``<output>.timeout.json`` copied into a row as ``timeout_<key>``
#: (``snapshot_*`` keep their own names). ``snapshot_state`` says what the
#: graded best-so-far board actually is: ``live`` (the router's copper as it
#: stood), ``last-accepted`` (a placement-delta trial was discarded) or
#: ``inconsistent`` (copper and footprints may disagree -- Issue #6314).
SIDECAR_KEYS = (
    "stage",
    "snapshot_saved",
    "snapshot_state",
    "snapshot_placement",
    "snapshot_discarded_trial",
)


def timeout_sidecar_facts(routed: Path) -> dict:
    """What the hard-timeout sidecar says about the board that was left behind.

    Empty when the run did not hit the deadline (no sidecar) or the sidecar is
    unreadable. A key the sidecar does not carry is omitted, never invented:
    a sidecar written before PR #6323 has no ``snapshot_state`` at all, and
    that absence must not read as ``live``. Nor is it only old sidecars: a
    worker killed after writing ``_partial`` but before publishing the
    ``partial-save`` stage leaves ``snapshot_saved: false`` and no
    ``snapshot_state`` beside a board that does exist (seen on board 05 at
    1.5x with feedback on, Issue #6312) -- hence ``snapshot_saved`` is copied
    too, so "a snapshot of unknown state" is distinguishable from "none".
    """
    sidecar = routed.with_suffix(".timeout.json")
    try:
        state = json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return {}
    if not isinstance(state, dict):
        return {}
    out: dict = {"timeout_sidecar": True}
    for key in SIDECAR_KEYS:
        if key in state:
            out[key if key.startswith("snapshot_") else f"timeout_{key}"] = state[key]
    return out


def vias_by_net(pcb) -> dict[str, int]:
    """Via count per net name on a routed board, nets without vias omitted.

    #6304's gap metric does not charge vias, so it cannot see the hypothesis
    that room helps by making space for them; this is the per-net number that
    can (Issue #6312).
    """
    counts = Counter(via.net_name or f"#{via.net_number}" for via in pcb.vias)
    return dict(sorted(counts.items()))


def fab_profile_mfr(routed: Path) -> str | None:
    """The manufacturer ``kct route`` used, from ``fab_profile.json`` beside *routed*.

    ``kct route`` writes the sidecar next to ``-o``; ``kct check`` resolves
    it ahead of ``project.kct``. Grading against it is what makes the
    harness's ``kct check`` columns match a manual ``kct check`` on the same
    file (Issue #6420). ``None`` when there is no readable sidecar.
    """
    try:
        data = json.loads((routed.parent / "fab_profile.json").read_text())
    except (OSError, ValueError):
        return None
    mfr = data.get("mfr") if isinstance(data, dict) else None
    return mfr if isinstance(mfr, str) and mfr else None


def find_net_class_map(routed: Path) -> Path | None:
    """The net-class map ``kct check`` would auto-discover for *routed*, or ``None``."""
    from kicad_tools.sidecars import net_class_map_sidecar_candidates

    return next((c for c in net_class_map_sidecar_candidates(routed) if c.is_file()), None)


def grade(routed: Path, board: Board) -> dict:
    """Completion, both DRC engines, copper, and the stuck-class breakdown.

    The ``kct check`` columns are graded the way ``kct check --mfr <tier>
    --allow-incomplete`` grades the same file: with the run dir's
    ``net_class_map.json`` (so the diff-pair and match-group rules engage and
    net-class clearances apply) and the manufacturer ``kct route`` recorded
    in ``fab_profile.json``. kicad-cli runs with ``--all-track-errors`` and
    its unconnected-item count is recorded (Issue #6420).
    """
    from kicad_tools.benchmark.external.metrics import (
        measure_completion,
        measure_copper,
        run_kct_check,
        run_kicad_cli_drc,
    )
    from kicad_tools.router.stuck_classifier import classify_stuck_nets

    comp = measure_completion(routed).to_dict()
    cop = measure_copper(routed).to_dict()
    mfr = fab_profile_mfr(routed) or board.manufacturer
    ncm = find_net_class_map(routed)
    # ``layers=None``: detect from the routed board, as ``kct check`` does.
    check = run_kct_check(routed, manufacturer=mfr, layers=None, net_class_map=ncm)
    drc = run_kicad_cli_drc(routed, timeout=600, all_track_errors=True)
    check_ran = bool(getattr(check, "ran", False))
    drc_ran = bool(getattr(drc, "ran", False))
    kct_errors = check.error_count if check_ran else None
    row = {
        "nets_complete": comp["nets_complete"],
        "nets_total": comp["nets_total"],
        "nets_blocking_incomplete": comp["nets_blocking_incomplete"],
        "connections_routed": comp["connections_routed"],
        "connections_total": comp["connections_total"],
        "via_count": cop["via_count"],
        # None, not 0.0, when nothing routed: a ratio over zero connections is
        # not "no vias".
        "vias_per_connection": (
            round(cop["via_count"] / comp["connections_routed"], 3)
            if comp["connections_routed"]
            else None
        ),
        "wirelength_mm": round(cop["wirelength_mm"], 1),
        # ``kct_check_errors`` is kept for old readers; it is now the same
        # number as ``kct_check_errors_total``: every error-severity rule,
        # the sidecar-gated diff-pair / match-group rules included.
        "kct_check_errors": kct_errors,
        "kct_check_errors_total": kct_errors,
        "kct_check_by_rule": dict(sorted(check.errors_by_rule.items())),
        "kct_check_mfr": mfr,
        "kct_check_net_class_map": str(ncm) if ncm is not None else None,
        # The rules that could not engage (no sidecar): they report nothing,
        # which is not the same as passing.
        "kct_check_inactive_rules": list(getattr(check, "inactive_rules", ()) or ()),
        "kicad_drc_errors": drc.violation_count if drc_ran else None,
        "kicad_drc_unconnected": getattr(drc, "unconnected_count", None) if drc_ran else None,
        "kicad_drc_all_track_errors": True,
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
    try:
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(routed))
        row["vias_by_net"] = vias_by_net(pcb)
        # Which nets actually have a zone. The classifier calls any incomplete
        # power-typed net ``pour_discontinuous`` whether or not a pour exists
        # for it (board 05's ``VM`` has none), and ``signal_nets_unfinished``
        # inherits that; this is what lets a reader count such a net back in.
        row["zone_nets"] = sorted({z.net_name for z in pcb.zones if z.net_name})
    except Exception as exc:  # same rule: a bookkeeping failure must not sink the row
        row["vias_by_net_error"] = f"{type(exc).__name__}: {exc}"
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


#: ``kct route`` exits after which a file at ``-o`` is a leftover periodic
#: checkpoint, not a finished route (1: error e.g. refused save; 5: failure).
CRASHED_EXIT_CODES = (1, 5)


def pick_artifact(routed: Path, exit_code: int | None = None) -> tuple[Path | None, str | None]:
    """The board to grade: the ``-o`` output, else the deadline's best-so-far.

    When the hard ``--timeout`` fires, ``kct route`` exits 124 without writing
    the requested output, and its ``.timeout.json`` sidecar names the best
    board: ``snapshot`` (the raw ``_partial`` holding the copper committed so
    far, recovered by the supervisor even if the worker was hard-killed before
    recording it; issue #6308), else ``unverified_output`` (the last checkpoint,
    which is what a deadline in a later stage leaves).

    Either is still the honest answer to "what did this budget buy", so it is
    graded -- and labelled ``deadline`` so it is never mistaken for a finished
    run: it has had no optimisation, zone fill or DRC repair, and which stage
    the deadline landed in is itself load-dependent.
    """
    if routed.exists():
        # Issue #6475: after a crashed route the file is the last checkpoint.
        if exit_code in CRASHED_EXIT_CODES:
            return routed, "checkpoint"
        return routed, "final"
    sidecar = routed.with_suffix(".timeout.json")
    if sidecar.exists():
        with contextlib.suppress(OSError, ValueError):
            report = json.loads(sidecar.read_text())
            for key in ("snapshot", "unverified_output"):
                named = report.get(key)
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
    cluster_rule: ClusterRule = DEFAULT_RULE,
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
        "seed": seed,
    }
    # Sampled before the board is scaled, so it is the load the route starts
    # under and not the load the route itself has already added.
    with contextlib.suppress(OSError):
        row["loadavg_start"] = [round(x, 2) for x in os.getloadavg()]
    try:
        inp, report = write_scaled_board(src, run_dir, scale, mode, cluster_rule)
    except ScaleError as exc:
        row["error"] = f"could not scale: {exc}"
        return row
    row["scale_report"] = report.to_dict()
    row["input"] = str(inp)
    routed = run_dir / f"{src.stem}_routed.kicad_pcb"
    log = run_dir / "route.log"
    extra = [
        *board.kct_args,
        *net_class_map_args(board),
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
    extra = with_deterministic_rescue(extra)
    row["deterministic_rescue"] = "--deterministic-rescue" in extra
    row.update(run_route(inp, routed, extra, timeout, log))
    row.update(parse_route_log(log.read_text(errors="replace")))
    if grid_policy == "pinned":
        row["pinned_grid_mm"] = grid_mm
    row.update(timeout_sidecar_facts(routed))
    artifact, kind = pick_artifact(routed, row.get("exit_code"))
    if artifact is not None:
        row["routed"] = str(artifact)
        row["artifact"] = kind
        row.update(grade(artifact, board))
        row["manufacturable"] = manufacturable(row)
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


def _kct_errors(row: dict):
    """Every error-severity ``kct check`` rule; old rows only have ``kct_check_errors``."""
    return row.get("kct_check_errors_total", row.get("kct_check_errors"))


def _manufacturable_cell(row: dict) -> str:
    verdict = row["manufacturable"] if "manufacturable" in row else manufacturable(row)
    return "unknown" if verdict is None else ("yes" if verdict else "no")


def render_route(rows: list[dict]) -> str:
    out = [
        "| Board | Mode | Scale | Outline mm | Nets | Signal nets unfinished | Router's own tally "
        "| Connections | kct check err "
        "| kicad-cli DRC err | Vias | Wire mm | Grid mm | Timed out | Wall s | Stuck classes "
        "| kicad-cli unconnected | Manufacturable |",
        "|" + "---|" * 18,
    ]
    for r in rows:
        size = r.get("scale_report", {}).get("outline_mm")
        outline = f"{size[0]:g}x{size[1]:g}" if size else "--"
        if "nets_complete" not in r:
            out.append(
                f"| {r['board']} | {r['mode']} | {r['scale']:g} | {outline} | ERROR: "
                f"{r.get('error', '?')} | -- | -- | -- | -- | -- | -- | -- | -- | -- "
                f"| {r.get('wall_s', '--')} | -- | -- | -- |"
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
            f"| {_kct_errors(r)} | {r['kicad_drc_errors']} "
            f"| {r['via_count']} | {r['wirelength_mm']} | {grid} "
            f"| {_timeout_cell(r)} "
            f"| {r['wall_s']} | {_fmt_counts(r.get('stuck_counts'))} "
            f"| {r.get('kicad_drc_unconnected')} | {_manufacturable_cell(r)} |"
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


def unfinished_signal_nets(
    row: dict,
    exclude: frozenset[str] = frozenset(),
    zone_nets: frozenset[str] | None = None,
) -> list[str] | None:
    """Unfinished non-pour nets of a graded row, minus *exclude*; ``None`` if ungraded.

    With *zone_nets* ``None`` this is the classifier's reading, the one
    ``signal_nets_unfinished`` and every table up to #6311 use: a net classed
    ``pour_discontinuous`` is a pour net. Given the set of nets that really
    have a zone, a ``pour_discontinuous`` net **without** one is counted as
    unfinished: nothing but routed copper can connect it.

    *exclude* exists for nets a known router defect leaves unfinished whatever
    the placement (board 05's ``PHASE_A/B/C``, Issue #6315): counted in, they
    can mask a placement effect in either direction.
    """
    stuck = row.get("stuck_nets")
    if stuck is None:
        return None
    return sorted(
        n
        for n, cls in stuck.items()
        if n not in exclude
        and (cls != POUR_CLASS or (zone_nets is not None and n not in zone_nets))
    )


def row_zone_nets(row: dict, fallback: frozenset[str] | None = None) -> frozenset[str] | None:
    """The row's own ``zone_nets`` if it recorded them, else *fallback* (``--zone-nets``)."""
    recorded = row.get("zone_nets")
    return frozenset(recorded) if recorded is not None else fallback


def render_summary(
    labelled: list[tuple[str, dict]],
    exclude: frozenset[str],
    zone_nets: frozenset[str] | None = None,
) -> str:
    """One line per row of several ``route --json`` files, for the research note.

    Two unfinished-net counts, each also given without *exclude*: the
    classifier's ("signal") and the zone-checked one ("routed"), which adds
    back nets the classifier calls ``pour_discontinuous`` that have no zone.
    The second is ``--`` for a row with no zone information.
    """
    less = f"without {'/'.join(sorted(exclude))}" if exclude else "no nets excluded"
    out = [
        "| File | Scale | Mode | Feedback | Seed | Signal nets unfinished "
        f"| ...{less} | Routed nets unfinished (zone-checked) | ...{less} "
        "| kct check err | kicad-cli DRC err | Vias | Vias / routed connection "
        "| Grid mm | Cell estimate | rc | Timed out | Cache hit | Wall s "
        "| Load 1 min, start -> end | Snapshot state | kicad-cli unconnected | Manufacturable |",
        "|" + "---|" * 23,
    ]

    def count(nets: list[str] | None) -> str:
        return "--" if nets is None else str(len(nets))

    for label, r in labelled:
        head = f"| {label} | {r['scale']:g} | {r['mode']} "
        if "nets_complete" not in r:
            out.append(f"{head}| ERROR: {r.get('error', '?')} |" + " -- |" * 19)
            continue
        feedback = "off" if "--no-placement-delta-feedback" in r.get("cmd", "") else "default"
        zones = row_zone_nets(r, zone_nets)
        checked = (None, None)
        if zones is not None:
            checked = (
                unfinished_signal_nets(r, zone_nets=zones),
                unfinished_signal_nets(r, exclude, zones),
            )
        start, end = r.get("loadavg_start"), r.get("loadavg_end")
        load = f"{start[0]:g} -> {end[0]:g}" if start and end else "--"
        cells = r.get("cell_estimate")
        out.append(
            f"{head}| {feedback} | {r.get('seed', '--')} "
            f"| {count(unfinished_signal_nets(r))} | {count(unfinished_signal_nets(r, exclude))} "
            f"| {count(checked[0])} | {count(checked[1])} "
            f"| {_kct_errors(r)} | {r['kicad_drc_errors']} | {r['via_count']} "
            f"| {r.get('vias_per_connection', '--')} | {r.get('coarse_grid_mm', '--')} "
            f"| {'--' if cells is None else f'{cells:,}'} "
            f"| {r['exit_code']} | {_timeout_cell(r)} | {'yes' if r.get('cache_hit') else 'no'} "
            f"| {r['wall_s']} | {load} | {r.get('snapshot_state', '--')} "
            f"| {r.get('kicad_drc_unconnected', '--')} | {_manufacturable_cell(r)} |"
        )
    return "\n".join(out)


def render_net_matrix(labelled: list[tuple[str, dict]]) -> str:
    """Net by row: each net that is unfinished in any row, and its state in every row.

    A cell is ``ok`` or the classifier's class, followed by that net's via
    count on that board. Pour-classed nets are listed too -- which of them
    have a zone is ``zone_nets`` in the row, not a judgement made here.
    """
    graded = [(label, r) for label, r in labelled if "stuck_nets" in r]
    nets = sorted({n for _, r in graded for n in r["stuck_nets"]})
    head = [f"{label} {r['mode']} {r['scale']:g}" for label, r in graded]
    out = ["| Net | " + " | ".join(head) + " |", "|---|" + "---|" * len(head)]
    for net in nets:
        cells = [
            f"{r['stuck_nets'].get(net, 'ok')} ({r.get('vias_by_net', {}).get(net, 0)} v)"
            for _, r in graded
        ]
        out.append(f"| {net} | " + " | ".join(cells) + " |")
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
        if BOARDS[key].pair_bench_only:
            raise SystemExit(
                f"board {key!r} is a diff-pair benchmark row; use the `pairs` subcommand"
            )
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
                        _rule_from_args(args),
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


def _rule_from_args(args) -> ClusterRule:
    return ClusterRule(args.anchor_min_pads, args.satellite_max_pads, args.max_gap_mm)


def _add_rule_args(parser) -> None:
    default = DEFAULT_RULE
    parser.add_argument(
        "--anchor-min-pads",
        type=int,
        default=default.anchor_min_pads,
        help="rigid mode: a footprint with at least this many pads is a cluster anchor",
    )
    parser.add_argument(
        "--satellite-max-pads",
        type=int,
        default=default.satellite_max_pads,
        help="rigid mode: a footprint with at most this many pads may join an anchor",
    )
    parser.add_argument(
        "--max-gap-mm",
        type=float,
        default=default.max_gap_mm,
        help="rigid mode: largest pad-bbox-to-pad-bbox distance from satellite to anchor",
    )


def _cmd_clusters(args) -> int:
    """Cluster counts per board under the given rule. Routes nothing."""
    from kicad_tools.schema.pcb import PCB

    keys = [k.strip() for k in args.boards.split(",") if k.strip()]
    rule = _rule_from_args(args)
    rows: list[dict] = []
    for key in keys:
        if key not in BOARDS:
            raise SystemExit(f"unknown board {key!r}; known: {','.join(BOARDS)}")
        src = resolve_board_path(BOARDS[key], args.chorus_pcb)
        if src is None:
            rows.append({"board": key, "error": "input not found"})
            continue
        rows.append({"board": key, "input": str(src), **cluster_report(PCB.load(str(src)), rule)})
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"rows": rows}, indent=2) + "\n")
    print(render_clusters(rows))
    if args.members:
        for r in rows:
            for c in r.get("clusters", ()):
                sats = "; ".join(
                    f"{ref} via {', '.join(nets)}" for ref, nets in c["satellites"].items()
                )
                print(f"{r['board']}: {c['anchor']} ({c['anchor_pads']} pads) <- {sats}")
    return 0


def render_plans(rows: list[dict], finer_than_mm: float) -> str:
    out = [
        "| Board | Mode | Scale | Coarse grid mm | Cell estimate | Fine zones "
        f"| Zones finer than {finer_than_mm:g} mm (pitch mm, cells) |",
        "|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        head = f"| {r['board']} | {r['mode']} | {r['scale']:g} "
        if "fine_zones" not in r:
            out.append(f"{head}| ERROR: {r.get('error', '?')} | -- | -- | -- |")
            continue
        fine = "; ".join(
            f"{ref} {z['pitch_mm']:g}, {z['cells']:,}"
            for ref, z in r["fine_zones"].items()
            if z["pitch_mm"] < finer_than_mm
        )
        cells = r.get("cell_estimate")
        out.append(
            f"{head}| {r.get('coarse_grid_mm', '--')} | {'--' if cells is None else f'{cells:,}'} "
            f"| {len(r['fine_zones'])} | {fine or '--'} |"
        )
    return "\n".join(out)


def _cmd_plan(args) -> int:
    """The grid plan ``kct route`` would pick for each scaled board. Routes nothing.

    Runs ``kct route --dry-run`` (about 3 s) on the same scaled input and with
    the same grid arguments a ``route`` row would get, so which rows are
    grid-matched can be known before any route time is spent.
    """
    keys = [k.strip() for k in args.boards.split(",") if k.strip()]
    scales = [float(v) for v in args.scales.split(",") if v.strip()]
    modes = [m.strip() for m in args.modes.split(",") if m.strip()]
    rule = _rule_from_args(args)
    args.work_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for key in keys:
        if key not in BOARDS:
            raise SystemExit(f"unknown board {key!r}; known: {','.join(BOARDS)}")
        board = BOARDS[key]
        src = resolve_board_path(board, args.chorus_pcb)
        for mode in modes:
            for scale in scales:
                if math.isclose(scale, 1.0) and mode != modes[0]:
                    continue
                row: dict = {"board": key, "mode": mode, "scale": scale}
                rows.append(row)
                if src is None:
                    row["error"] = "input not found"
                    continue
                run_dir = args.work_dir / f"plan-{key}-{mode}-{scale:g}"
                if run_dir.exists():
                    shutil.rmtree(run_dir)
                try:
                    inp, report = write_scaled_board(src, run_dir, scale, mode, rule)
                except ScaleError as exc:
                    row["error"] = f"could not scale: {exc}"
                    continue
                row["scale_report"] = report.to_dict()
                cmd = [sys.executable, "-m", "kicad_tools.cli", "route", str(inp)]
                cmd += ["-o", str(run_dir / "unused.kicad_pcb"), "--dry-run", "--no-cache"]
                cmd += [*board.kct_args, *net_class_map_args(board), *args.extra.split()]
                cmd += grid_args(scale, args.grid_policy, args.grid_mm)
                done = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=300)
                row["cmd"] = " ".join(cmd[1:])
                row.update(parse_grid_plan(done.stdout + done.stderr))
                if not row["fine_zones"] and "coarse_grid_mm" not in row:
                    row["error"] = f"no grid plan in the dry-run output (exit {done.returncode})"
                    del row["fine_zones"]
    if args.json:
        args.json.parent.mkdir(parents=True, exist_ok=True)
        args.json.write_text(json.dumps({"rows": rows}, indent=2) + "\n")
    print(render_plans(rows, args.finer_than_mm))
    return 0


def _cmd_summary(args) -> int:
    """The note's per-row table, straight from committed ``route --json`` files."""
    exclude = frozenset(n.strip() for n in args.exclude_nets.split(",") if n.strip())
    fallback = None
    if args.zone_nets is not None:
        fallback = frozenset(n.strip() for n in args.zone_nets.split(",") if n.strip())
    labelled = [
        (path.stem, row) for path in args.json_files for row in json.loads(path.read_text())["rows"]
    ]
    print(render_summary(labelled, exclude, fallback))
    if args.net_matrix:
        print()
        print(render_net_matrix(labelled))
    if args.nets:
        print()
        for label, row in labelled:
            nets = unfinished_signal_nets(row, zone_nets=row_zone_nets(row, fallback))
            if nets is not None:
                shown = ", ".join(f"{n} ({row['stuck_nets'][n]})" for n in nets) or "(none)"
                print(f"{label} {row['mode']} {row['scale']:g}: {shown}")
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
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw and raw[0] == "pairs":
        # Issue #6442: the diff-pair benchmark mode lives in its own module
        # (it needs the strip / guard / pair-metric machinery) but is invoked
        # from here so there is one entry point for the experiment harness.
        sys.path.insert(0, str(Path(__file__).resolve().parent))
        import diffpair_bench

        return diffpair_bench.main(raw[1:])
    ap = argparse.ArgumentParser(
        description=__doc__.split("\n\n")[0],
        epilog="Also: `pairs ...` runs the router-produced diff-pair benchmark "
        "(Issue #6442); see `pairs --help`.",
    )
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
    _add_rule_args(route)
    route.set_defaults(func=_cmd_route)

    clusters = sub.add_parser(
        "clusters", help="rigid-cluster counts per board under a rule; routes nothing"
    )
    clusters.add_argument("--boards", default="03,05,chorus")
    clusters.add_argument("--json", type=Path)
    clusters.add_argument(
        "--members", action="store_true", help="also list each cluster and the shared nets"
    )
    clusters.add_argument("--chorus-pcb", type=Path, help="override the chorus fixture path")
    _add_rule_args(clusters)
    clusters.set_defaults(func=_cmd_clusters)

    plan = sub.add_parser(
        "plan", help="the grid plan each scaled board would be routed on; routes nothing"
    )
    plan.add_argument("--boards", default="05")
    plan.add_argument("--scales", default="1.0,1.25,1.5")
    plan.add_argument("--modes", default="uniform,rigid")
    plan.add_argument("--work-dir", type=Path, required=True)
    plan.add_argument("--json", type=Path)
    plan.add_argument("--grid-policy", choices=("pinned", "area", "default"), default="area")
    plan.add_argument("--grid-mm", type=float)
    plan.add_argument("--extra", default="", help="extra kct route arguments, as for `route`")
    plan.add_argument(
        "--finer-than-mm",
        type=float,
        default=0.03,
        help="list fine zones below this pitch in the printed table (all are in --json)",
    )
    plan.add_argument("--chorus-pcb", type=Path, help="override the chorus fixture path")
    _add_rule_args(plan)
    plan.set_defaults(func=_cmd_plan)

    summary = sub.add_parser(
        "summary", help="one table line per row of one or more `route --json` files"
    )
    summary.add_argument("json_files", type=Path, nargs="+")
    summary.add_argument(
        "--exclude-nets",
        default="",
        help="comma list of nets to leave out of the second unfinished-net column",
    )
    summary.add_argument(
        "--zone-nets",
        help="comma list of the nets that have a zone, for rows that did not record "
        "`zone_nets` themselves (a row's own record always wins)",
    )
    summary.add_argument("--nets", action="store_true", help="also list each row's unfinished nets")
    summary.add_argument(
        "--net-matrix",
        action="store_true",
        help="also print net x row: the state and via count of every net unfinished in any row",
    )
    summary.set_defaults(func=_cmd_summary)

    gaps = sub.add_parser("gaps", help="Phase 2: gaps carrying more copper than fits at 1x")
    gaps.add_argument("--from-json", type=Path, required=True, help="a `route --json` file")
    gaps.add_argument("--json", type=Path)
    gaps.add_argument("--clearance-mm", type=float, help="override the logged clearance")
    gaps.set_defaults(func=_cmd_gaps)

    args = ap.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
