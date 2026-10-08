"""
Gerber export using kicad-cli.

Wraps kicad-cli for generating Gerber files with manufacturer presets.
"""

from __future__ import annotations

import logging
import math
import re
import subprocess
import tempfile
import time
import zipfile
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.progress import ProgressCallback

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.exceptions import (
    ConfigurationError,
    ExportError,
)
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.sexp.vscore import VSCORE_UUID_MARKER

logger = logging.getLogger(__name__)


def _pcb_has_unfilled_zones(pcb_path: Path) -> bool:
    """Return True if the PCB defines copper-pour zones with no fill data.

    Cheap text scan that avoids parsing the full S-expression: a properly
    filled zone contains ``(filled_polygon ...)`` children; an unfilled zone
    has only the outline definition.  Used by :meth:`GerberExporter._export_gerbers`
    to decide whether to invoke the safety-net fill pass before exporting
    Gerbers (issue #2516).

    The serializer wraps zones in two forms -- ``(zone (net ...)`` on a
    single line, or ``(zone\\n  (net ...)`` with a newline after the
    opening token -- so we accept either.
    """
    try:
        text = pcb_path.read_text()
    except OSError:
        return False
    has_zone = "(zone " in text or "(zone\n" in text or "(zone\t" in text
    if not has_zone:
        return False
    # If we have zones but no filled_polygon, the zones are unfilled and
    # the resulting Gerbers would lack G36..G37 polygon-fill regions.
    return "filled_polygon" not in text


def _pcb_has_filled_zones(pcb_path: Path) -> bool:
    """Return True if the PCB carries saved zone fill copper (Issue #6078).

    Cheap text scan, as in :func:`_pcb_has_unfilled_zones`.  Boards with no
    ``filled_polygon`` have no saved fill to verify, so no kicad-cli DRC run.
    """
    try:
        return "(filled_polygon" in pcb_path.read_text()
    except OSError:
        return False


# Board-level copper layer definitions inside the top-level ``(layers ...)``
# table look like ``(0 "F.Cu" signal)`` / ``(4 "In1.Cu" signal)`` -- an
# integer ordinal, a quoted layer name, then a layer type token.  The integer
# ordinal distinguishes these from per-pad/per-via ``(layers "F.Cu" ...)``
# lists, and the type token guards against ``(net 4 "...")`` entries.
_COPPER_LAYER_DEF_RE = re.compile(
    r'\(\s*\d+\s+"((?:F|B|In\d+)\.Cu)"\s+(?:signal|power|mixed|jumper)\b'
)


# Any entry of the board-level ``(layers ...)`` table: ordinal, canonical
# name, type and an optional user name (``(19 "Cmts.User" user "Notes")``).
_LAYER_DEF_RE = re.compile(
    r'\(\s*\d+\s+"?([^"\s()]+)"?\s+(?:signal|power|mixed|jumper|user)\b(?:\s+"([^"]*)")?'
)


def pcb_layer_names(pcb_path: Path) -> set[str] | None:
    """Canonical and user names in the board's layer table, or None if unknown.

    Returns None when the file cannot be read or declares no layer table, so
    callers only warn about a missing layer when the table was actually seen.
    """
    try:
        text = pcb_path.read_text()
    except OSError:
        return None
    names: set[str] = set()
    for canonical, user in _LAYER_DEF_RE.findall(text):
        names.add(canonical)
        if user:
            names.add(user)
    return names or None


def missing_vscore_layers(pcb_path: Path, requested: list[str]) -> list[str]:
    """Explicitly requested V-score layers the board's layer table lacks (#6193).

    kicad-cli plots nothing for a layer the board does not define and exits
    successfully, so an opt-in typo would otherwise ship no score data.
    """
    if not requested:
        return []
    names = pcb_layer_names(pcb_path)
    if names is None:
        return []
    return [layer for layer in requested if layer not in names]


def _inner_copper_index(name: str) -> int:
    """Return the ``N`` from ``InN.Cu`` (e.g. 1 for ``In1.Cu``)."""
    return int(name[2:].split(".")[0])


def _pcb_copper_layers(pcb_path: Path) -> list[str]:
    """Return the copper layers declared by the PCB's stackup, in plot order.

    Parses the board-level ``(layers ...)`` table with a cheap text scan
    (same approach as :func:`_pcb_has_unfilled_zones`) and returns the
    copper layers ordered front-to-back: ``F.Cu``, ``In1.Cu`` .. ``InN.Cu``,
    ``B.Cu``.  Ordering is derived from the layer *names* rather than the
    file's numeric ordinals because KiCad changed copper-layer numbering
    between versions (legacy: F.Cu=0 .. B.Cu=31; KiCad 9+: F.Cu=0, B.Cu=2,
    In1.Cu=4, ...) while the canonical names are stable.

    Falls back to ``["F.Cu", "B.Cu"]`` if the file cannot be read or no
    copper layer definitions are found, preserving the previous 2-layer
    behaviour (issue #3559: the old hardcode silently dropped inner copper
    on 4-layer boards).
    """
    fallback = ["F.Cu", "B.Cu"]
    try:
        text = pcb_path.read_text()
    except OSError:
        logger.warning(
            "Could not read %s to detect copper layers; assuming 2-layer (F.Cu, B.Cu)",
            pcb_path,
        )
        return fallback

    found = set(_COPPER_LAYER_DEF_RE.findall(text))
    if not found:
        logger.warning(
            "No copper layer definitions found in %s; assuming 2-layer (F.Cu, B.Cu)",
            pcb_path,
        )
        return fallback

    inner = sorted(
        (name for name in found if name.startswith("In")),
        key=_inner_copper_index,
    )

    layers: list[str] = []
    if "F.Cu" in found:
        layers.append("F.Cu")
    layers.extend(inner)
    if "B.Cu" in found:
        layers.append("B.Cu")
    return layers


# Layers a V-score drawing may live on.  ``kct panel --cut vcut`` draws its
# score lines on ``Cmts.User`` by default (``--vcut-layer`` picks another of
# these), never on Edge.Cuts, where an open line breaks the outline (#6143).
_VSCORE_CANDIDATE_RE = re.compile(r"^(?:(?:Dwgs|Cmts|Eco1|Eco2)\.User|User\.\d+)$")

# How close (mm) a score line's ends must come to the board outline's
# bounding box to count as spanning it.  V-scores are cut edge to edge.
_VSCORE_SPAN_TOL_MM = 0.01

# Partial and jump scores (#6193) are only recognised when they complete a
# full separation: walking the line across the outline extent, every stretch
# of board material must be covered by score pieces, and every gap must lie
# off the board (in a slot or cutout, or between board outlines).  A score
# end may stop this close (mm) to the boundary where the material begins.
# Layouts the geometry cannot prove -- scores interrupted by tabs, slots
# drawn inside footprints -- need the explicit opt-in
# (``GerberConfig.vscore_layers`` / ``--vscore-layer``).
_VSCORE_END_TOL_MM = 0.1

# Arcs and Bezier curves on Edge.Cuts are flattened into this many chords
# when intersecting them with a candidate score line.
_EDGE_CURVE_STEPS = 64

_Pt = tuple[float, float]


def _is_tagged_vscore(node: Any) -> bool:
    uuid_node = node.find_child("uuid") or node.find_child("tstamp")
    value = uuid_node.get_string(0) if uuid_node is not None else None
    parts = (value or "").split("-")
    return len(parts) == 5 and parts[3].lower() == VSCORE_UUID_MARKER


def _xy(node: Any) -> tuple[float, float] | None:
    """Return a ``(start|end|mid|center|xy x y)`` node's point, or None."""
    if node is None:
        return None
    x, y = node.get_float(0), node.get_float(1)
    if x is None or y is None:
        return None
    return x, y


def _edge_cuts_nodes(root: Any) -> list[Any]:
    """Top-level ``gr_*`` graphics on Edge.Cuts."""
    nodes = []
    for child in root.children:
        if child.is_atom or not child.name.startswith("gr_"):
            continue
        layer = child.find_child("layer")
        if layer is not None and layer.get_string(0) == "Edge.Cuts":
            nodes.append(child)
    return nodes


def _poly_pts(node: Any) -> list[_Pt]:
    poly = node.find_child("pts")
    pts: list[_Pt] = []
    if poly is not None:
        for xy in poly.children:
            if not xy.is_atom and xy.name == "xy":
                pt = _xy(xy)
                if pt is not None:
                    pts.append(pt)
    return pts


def _edge_cuts_bbox(root: Any) -> tuple[float, float, float, float] | None:
    """Bounding box of the top-level Edge.Cuts graphics of a parsed board."""
    pts: list[tuple[float, float]] = []
    for child in _edge_cuts_nodes(root):
        for tag in ("start", "end", "mid", "center"):
            pt = _xy(child.find_child(tag))
            if pt is not None:
                pts.append(pt)
        if child.name == "gr_circle":
            center = _xy(child.find_child("center"))
            rim = _xy(child.find_child("end"))
            if center is not None and rim is not None:
                cx, cy = center
                r = math.hypot(rim[0] - cx, rim[1] - cy)
                pts.extend(((cx - r, cy - r), (cx + r, cy + r)))
        pts.extend(_poly_pts(child))
    if not pts:
        return None
    xs = [p[0] for p in pts]
    ys = [p[1] for p in pts]
    return min(xs), min(ys), max(xs), max(ys)


def _arc_points(a: _Pt, m: _Pt, b: _Pt) -> list[_Pt]:
    """Flatten the arc through ``a``, ``m``, ``b`` into a polyline."""
    (ax, ay), (mx, my), (bx, by) = a, m, b
    d = 2 * (ax * (my - by) + mx * (by - ay) + bx * (ay - my))
    if abs(d) < 1e-12:  # collinear: a straight segment
        return [a, b]
    a2, m2, b2 = ax * ax + ay * ay, mx * mx + my * my, bx * bx + by * by
    cx = (a2 * (my - by) + m2 * (by - ay) + b2 * (ay - my)) / d
    cy = (a2 * (bx - mx) + m2 * (ax - bx) + b2 * (mx - ax)) / d
    r = math.hypot(ax - cx, ay - cy)
    ta = math.atan2(ay - cy, ax - cx)
    tm = math.atan2(my - cy, mx - cx)
    tb = math.atan2(by - cy, bx - cx)
    sweep = (tb - ta) % (2 * math.pi)
    if (tm - ta) % (2 * math.pi) > sweep:  # the arc runs the other way round
        sweep -= 2 * math.pi
    return [
        (
            cx + r * math.cos(ta + sweep * i / _EDGE_CURVE_STEPS),
            cy + r * math.sin(ta + sweep * i / _EDGE_CURVE_STEPS),
        )
        for i in range(_EDGE_CURVE_STEPS + 1)
    ]


def _bezier_points(ctrl: list[_Pt]) -> list[_Pt]:
    p0, p1, p2, p3 = ctrl
    out = []
    for i in range(_EDGE_CURVE_STEPS + 1):
        t = i / _EDGE_CURVE_STEPS
        u = 1 - t
        out.append(
            (
                u**3 * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t**3 * p3[0],
                u**3 * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t**3 * p3[1],
            )
        )
    return out


def _edge_cuts_geometry(
    root: Any,
) -> tuple[list[tuple[_Pt, _Pt]], list[tuple[float, float, float]]]:
    """Top-level Edge.Cuts as straight segments plus ``(cx, cy, r)`` circles."""
    segs: list[tuple[_Pt, _Pt]] = []
    circles: list[tuple[float, float, float]] = []

    def chain(pts: list[_Pt], closed: bool = False) -> None:
        if closed and len(pts) > 2:
            pts = [*pts, pts[0]]
        segs.extend(zip(pts, pts[1:], strict=False))

    for node in _edge_cuts_nodes(root):
        start, end = _xy(node.find_child("start")), _xy(node.find_child("end"))
        if node.name == "gr_line" and start and end:
            segs.append((start, end))
        elif node.name == "gr_rect" and start and end:
            (x0, y0), (x1, y1) = start, end
            chain([(x0, y0), (x1, y0), (x1, y1), (x0, y1)], closed=True)
        elif node.name == "gr_arc":
            mid = _xy(node.find_child("mid"))
            if start and mid and end:
                chain(_arc_points(start, mid, end))
        elif node.name == "gr_circle":
            center = _xy(node.find_child("center"))
            if center and end:
                circles.append(
                    (center[0], center[1], math.hypot(end[0] - center[0], end[1] - center[1]))
                )
        elif node.name == "gr_poly":
            chain(_poly_pts(node), closed=True)
        elif node.name == "gr_curve":
            ctrl = _poly_pts(node)
            if len(ctrl) == 4:
                chain(_bezier_points(ctrl))
    return segs, circles


def _edge_crossings(
    segs: list[tuple[_Pt, _Pt]],
    circles: list[tuple[float, float, float]],
    axis: str,
    coord: float,
) -> list[float]:
    """Positions where a score line crosses the Edge.Cuts boundary, sorted.

    ``axis`` "h" is the horizontal line ``y = coord`` (positions are x);
    "v" is the vertical line ``x = coord`` (positions are y).  Crossings use
    the half-open ray-casting rule, so a line through a shared vertex counts
    it once and a line running along a boundary edge does not count that edge.
    Between consecutive crossings the line is alternately off and on board.
    """

    def swap(p: _Pt) -> _Pt:
        return p if axis == "h" else (p[1], p[0])

    out: list[float] = []
    for p, q in segs:
        (px, py), (qx, qy) = swap(p), swap(q)
        if (py <= coord < qy) or (qy <= coord < py):
            out.append(px + (coord - py) * (qx - px) / (qy - py))
    for cx, cy, r in circles:
        u, v = swap((cx, cy))
        h = r * r - (coord - v) ** 2
        if h > 0:
            s = math.sqrt(h)
            out.extend((u - s, u + s))
    return sorted(out)


def _completes_separation(
    pieces: list[tuple[float, float]],
    lo_edge: float,
    hi_edge: float,
    crossings: list[float],
) -> bool:
    """Whether collinear untagged pieces cut all the board material on a line.

    The board material along the line is every stretch between Edge.Cuts
    crossings that lies inside an odd number of boundaries (inside an
    outline and not inside a cutout).  Each such stretch must be covered by
    the merged pieces, to within :data:`_VSCORE_END_TOL_MM` at its ends, so
    the pieces plus the off-board stretches (slots, cutouts, the space
    between boards) span the whole outline extent.  A solid piece covering
    the extent edge to edge is accepted as before (#6156).

    A line ending on a hole rim or slot with solid board beyond it cuts
    nothing off and is rejected, as are dividers and dashed lines.  A line
    broken around a hole, or one ending on the wall of an L-shaped board's
    notch, does cross all of the board's material on that line; it is
    accepted, which is the same risk as the full-span solid line main
    already accepts.  An ambiguous crossing sequence (odd count, or
    near-coincident crossings from duplicated or overlapping Edge.Cuts
    segments) is rejected; only the full-span shortcut or ``--vscore-layer``
    can then enable the layer.
    """
    tol = _VSCORE_SPAN_TOL_MM
    end_tol = _VSCORE_END_TOL_MM

    runs: list[list[float]] = []
    for lo, hi in sorted(pieces):
        if runs and lo <= runs[-1][1] + tol:
            runs[-1][1] = max(runs[-1][1], hi)
        else:
            runs.append([lo, hi])
    if any(lo <= lo_edge + tol and hi >= hi_edge - tol for lo, hi in runs):
        return True

    # Pairing crossings into material stretches assumes they alternate
    # off-board / on-board.  An overlapping or duplicated Edge.Cuts segment
    # adds a spurious crossing that flips the parity (turning a hole's
    # interior into "material"), so fail closed when the sequence is
    # ambiguous: an odd count, or two crossings closer than the end tolerance.
    if len(crossings) % 2 or any(
        b - a <= end_tol for a, b in zip(crossings, crossings[1:], strict=False)
    ):
        return False

    # Board material: every other stretch between crossings, starting on
    # board after the first one.  Clip to the outline extent.
    material = [
        (max(a, lo_edge), min(b, hi_edge))
        for a, b in zip(crossings[0::2], crossings[1::2], strict=False)
        if min(b, hi_edge) - max(a, lo_edge) > end_tol
    ]
    if not material:
        return False
    return all(any(lo <= a + end_tol and hi >= b - end_tol for lo, hi in runs) for a, b in material)


def pcb_vscore_layers(pcb_path: Path) -> list[str]:
    """Return the user layers that carry V-score lines, in file order (#6156).

    A V-score line is a top-level ``gr_line`` on a user drawing layer
    (``Cmts.User``, ``Dwgs.User``, ``Eco1/2.User``, ``User.N``) that runs
    straight across the board outline: horizontal or vertical, reaching or
    passing the Edge.Cuts bounding box at both ends (KiKit overshoots the
    frame by ~3 mm).  Lines tagged by ``kct panel`` (see
    :data:`VSCORE_UUID_MARKER`) are always accepted.

    Untagged lines must lie strictly inside the outline on the perpendicular
    axis; borders on or outside the outline and dimension extension lines are
    not scores.  Partial and jump scores (#6193) are accepted only when they
    complete a separation along their line: the collinear pieces on one layer
    must cover every stretch of board material between the top-level
    Edge.Cuts crossings, so that only slots, cutouts and the space between
    boards are left uncut (see :func:`_completes_separation`).  Dividers,
    fold lines, dashed lines and lines ending on a hole or slot with board
    beyond it therefore add no Gerber.  A line broken around a hole or ending
    on an L-board's notch wall still crosses all the material on its line and
    is accepted, the same risk as a full-span solid line.  A layout the
    geometry cannot prove needs ``GerberConfig.vscore_layers``
    (``--vscore-layer``).
    Quoted and unquoted layer names (old file formats) are both accepted.

    Returns an empty list if the file cannot be read or has no outline.
    """
    try:
        text = pcb_path.read_text()
    except OSError:
        return []
    # Cheap guard: skip the full parse for boards with no user-layer lines.
    if "gr_line" not in text or not re.search(
        r'\(layer\s+"?(?:(?:Dwgs|Cmts|Eco1|Eco2)\.User|User\.\d+)\b', text
    ):
        return []

    from kicad_tools.sexp.parser import parse_sexp

    try:
        root = parse_sexp(text)
    except Exception:  # pragma: no cover - a malformed board fails elsewhere
        logger.debug("Could not parse %s to look for V-score lines", pcb_path)
        return []
    bbox = _edge_cuts_bbox(root)
    tol = _VSCORE_SPAN_TOL_MM

    # Layer -> file index of its first line that is a score.
    found: dict[str, int] = {}
    # Untagged axis-aligned candidates keyed by (layer, axis): (line
    # coordinate, lo, hi, file index).  ``axis`` "h" = horizontal (constant y).
    cands: dict[tuple[str, str], list[tuple[float, float, float, int]]] = {}
    for index, child in enumerate(root.children):
        if child.is_atom or child.name != "gr_line":
            continue
        layer_node = child.find_child("layer")
        layer = layer_node.get_string(0) if layer_node is not None else None
        if layer is None or not _VSCORE_CANDIDATE_RE.match(layer):
            continue
        if _is_tagged_vscore(child):
            found.setdefault(layer, index)
            continue
        if bbox is None:
            continue
        a, b = _xy(child.find_child("start")), _xy(child.find_child("end"))
        if a is None or b is None:
            continue
        (x0, y0), (x1, y1) = a, b
        if abs(y0 - y1) <= tol:
            cand = ((y0 + y1) / 2, min(x0, x1), max(x0, x1), index)
            cands.setdefault((layer, "h"), []).append(cand)
        elif abs(x0 - x1) <= tol:
            cand = ((x0 + x1) / 2, min(y0, y1), max(y0, y1), index)
            cands.setdefault((layer, "v"), []).append(cand)

    if bbox is not None and cands:
        min_x, min_y, max_x, max_y = bbox
        edge_segs, edge_circles = _edge_cuts_geometry(root)
        for (layer, axis), items in cands.items():
            if axis == "h":
                perp_lo, perp_hi, lo_edge, hi_edge = min_y, max_y, min_x, max_x
            else:
                perp_lo, perp_hi, lo_edge, hi_edge = min_x, max_x, min_y, max_y
            # Group the pieces lying on one line coordinate.
            groups: list[list[tuple[float, float, float, int]]] = []
            for item in sorted(items):
                if groups and item[0] - groups[-1][-1][0] <= tol:
                    groups[-1].append(item)
                else:
                    groups.append([item])
            for group in groups:
                coord = sum(g[0] for g in group) / len(group)
                if not perp_lo + tol < coord < perp_hi - tol:
                    continue
                crossings = _edge_crossings(edge_segs, edge_circles, axis, coord)
                pieces = [(g[1], g[2]) for g in group]
                if _completes_separation(pieces, lo_edge, hi_edge, crossings):
                    index = min(g[3] for g in group)
                    found[layer] = min(found.get(layer, index), index)
    return sorted(found, key=found.__getitem__)


@dataclass
class GerberConfig:
    """Configuration for Gerber export."""

    # Output settings
    output_dir: Path | None = None
    create_zip: bool = True
    # False skips the Gerber (copper/mask/silk/...) plot entirely, so only the
    # drill step runs.  ``layers=[]`` cannot mean "none" -- it means "default
    # layers" -- hence the explicit switch (Issue #6180).
    generate_gerbers: bool = True
    zip_name: str = "gerbers.zip"

    # Layer selection
    layers: list[str] = field(default_factory=list)  # Empty = all copper + required
    include_edge_cuts: bool = True
    include_silkscreen: bool = True
    include_soldermask: bool = True
    include_solderpaste: bool = False
    # Issue #6156: plot the user layer(s) holding V-score lines (see
    # :func:`pcb_vscore_layers`).  A V-cut panel exported without them
    # tells the fab nothing about where to score.
    include_vscore: bool = True
    # Issue #6193: user layers the caller declares as V-score layers.  They
    # are always plotted, whether or not detection can prove them -- for a
    # third-party panel whose partial or jump scores the Edge.Cuts geometry
    # does not explain.  ``kct export --vscore-layer`` fills this in.
    vscore_layers: list[str] = field(default_factory=list)

    # Format options
    use_protel_extensions: bool = True  # .GTL/.GBL vs .gbr
    use_aux_origin: bool = True
    subtract_soldermask: bool = False
    disable_aperture_macros: bool = False

    # Drill options (Issue #6167: each maps onto a real
    # ``kicad-cli pcb export drill`` flag -- see ``_drill_command``).
    generate_drill: bool = True
    # "excellon" or "gerber" ("gerber_x2" is accepted as an alias).
    drill_format: str = "excellon"
    # False -> separate ``<board>-PTH.drl`` + ``<board>-NPTH.drl``
    # (``--excellon-separate-th``); True -> one merged ``<board>.drl``
    # (kicad-cli's default).  Gerber-format drill is always split.
    merge_pth_npth: bool = False
    minimal_header: bool = False  # --excellon-min-header
    drill_units: str = "mm"  # --excellon-units: "mm" or "in" ("inch" alias)
    # --excellon-zeros-format: decimal, suppressleading, suppresstrailing, keep
    drill_zeros_format: str = "decimal"

    # Post-zip cleanup: when True (default), remove individual gerber and
    # drill files after creating the zip archive so only the zip remains.
    clean_after_zip: bool = True

    # Issue #6078: kicad-cli plots the zone fill SAVED in the board (we do
    # not pass ``--check-zones``).  Before plotting, check that the saved
    # copper is no less connected than a fresh fill, and refuse to export a
    # fill that is split where a refill is not.  Set False only to export a
    # deliberately fixed fill.
    verify_zone_fill: bool = True


@dataclass
class GerberManufacturerPreset:
    """Preset configuration for a specific manufacturer.

    File naming is deliberately **not** part of a preset (Issue #6163).
    Every preset ships kicad-cli's own names -- ``<board>-<Layer>.<ext>``
    with Protel extensions (``board-F_Cu.gtl``, ``board-Edge_Cuts.gm1``,
    ``board-In1_Cu.g1``) plus ``<board>-job.gbrjob`` -- and keeps the X2
    ``%TF.FileFunction`` attributes, which is what JLCPCB, PCBWay, Seeed
    Fusion and OSH Park all document accepting for KiCad uploads.  An
    earlier ``layer_rename`` map was never applied; it was removed rather
    than wired up because no supported fab requires a rename and the
    downstream tools (MCP layer detection, readiness checks) parse the
    kicad-cli names.  Every field here must be read by the exporter --
    ``tests/test_gerber_preset_fields.py`` fails on dead config.
    """

    name: str
    config: GerberConfig


# Manufacturer presets
JLCPCB_PRESET = GerberManufacturerPreset(
    name="JLCPCB",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=True,
        include_solderpaste=False,
        generate_drill=True,
        merge_pth_npth=False,
        minimal_header=False,
    ),
)

PCBWAY_PRESET = GerberManufacturerPreset(
    name="PCBWay",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=True,
        include_solderpaste=True,
        generate_drill=True,
        merge_pth_npth=False,
    ),
)

OSHPARK_PRESET = GerberManufacturerPreset(
    name="OSH Park",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=False,
        include_solderpaste=False,
        generate_drill=True,
        # OSH Park's KiCad guide asks for plated and non-plated holes merged
        # into a single drill file; the other presets ship KiCad's default
        # separate PTH/NPTH files, which their KiCad guides show.
        merge_pth_npth=True,
    ),
)

SEEED_PRESET = GerberManufacturerPreset(
    name="Seeed Fusion",
    config=GerberConfig(
        use_protel_extensions=True,
        use_aux_origin=True,
        include_solderpaste=True,
        generate_drill=True,
        merge_pth_npth=False,
        minimal_header=False,
    ),
)

MANUFACTURER_PRESETS: dict[str, GerberManufacturerPreset] = {
    "jlcpcb": JLCPCB_PRESET,
    "pcbway": PCBWAY_PRESET,
    "oshpark": OSHPARK_PRESET,
    "seeed": SEEED_PRESET,
}


_KICAD_CLI_VERSION_CACHE: dict[str, str] = {}


def clear_kicad_cli_version_cache() -> None:
    """Forget memoised ``kicad-cli version`` results (for tests)."""
    from kicad_tools.cli import runner

    _KICAD_CLI_VERSION_CACHE.clear()
    runner.PROBED_KICAD_CLI_VERSIONS.clear()


def get_kicad_cli_version(kicad_cli: Path) -> str | None:
    """Get the version string from kicad-cli.

    Successful results are memoised per interpreter and per ``kicad_cli``
    path (a launch costs seconds and the answer cannot change within a
    process, #5910).  Failures are not cached, so a transient error is
    retried.  A version already captured by the discovery probe in
    ``cli.runner`` is reused without spawning anything.

    Args:
        kicad_cli: Path to the kicad-cli executable.

    Returns:
        Version string like ``"10.0.1"`` or ``None`` if the version
        could not be determined.
    """
    from kicad_tools.cli import runner

    key = str(kicad_cli)
    cached = _KICAD_CLI_VERSION_CACHE.get(key) or runner.PROBED_KICAD_CLI_VERSIONS.get(key)
    if cached:
        return cached
    try:
        result = subprocess.run(
            [key, "version"],
            capture_output=True,
            text=True,
        )
        if result.returncode == 0:
            version = result.stdout.strip()
            if version:
                _KICAD_CLI_VERSION_CACHE[key] = version
            return version
    except Exception:
        pass
    return None


def get_drill_origin_value(kicad_cli: Path) -> str:
    """Return the correct ``--drill-origin`` value for the installed kicad-cli.

    KiCad 10 renamed the auxiliary-axis origin flag value from ``aux``
    to ``plot``.  Passing the wrong value causes kicad-cli to exit with
    *"Invalid origin mode specified"*.

    Returns:
        ``"plot"`` for KiCad 10+ or when the version cannot be
        determined (safe default), ``"aux"`` for KiCad 9 and earlier.
    """
    version_str = get_kicad_cli_version(kicad_cli)
    if version_str is None:
        # Cannot determine version; default to the modern value.
        return "plot"

    try:
        major = int(version_str.split(".")[0])
    except (ValueError, IndexError):
        return "plot"

    return "plot" if major >= 10 else "aux"


# ``kicad-cli pcb export drill`` option values (Issue #6167).
_DRILL_FORMATS: dict[str, str] = {
    "excellon": "excellon",
    "gerber": "gerber",
    "gerber_x2": "gerber",
}
_DRILL_UNITS: dict[str, str] = {"mm": "mm", "in": "in", "inch": "in"}
_DRILL_ZEROS_FORMATS: tuple[str, ...] = ("decimal", "suppressleading", "suppresstrailing", "keep")


class GerberExporter:
    """
    Export Gerbers using kicad-cli.

    Example::

        exporter = GerberExporter("board.kicad_pcb")
        exporter.export_for_manufacturer("jlcpcb", "output/")

        # Or with custom config
        config = GerberConfig(include_solderpaste=True)
        exporter.export(config, "output/")
    """

    def __init__(self, pcb_path: str | Path):
        """
        Initialize the exporter.

        Args:
            pcb_path: Path to KiCad PCB file

        Raises:
            FileNotFoundError: If PCB file doesn't exist
            RuntimeError: If kicad-cli is not found
        """
        self.pcb_path = Path(pcb_path)
        if not self.pcb_path.exists():
            raise KiCadFileNotFoundError(
                "PCB file not found",
                context={"file": str(pcb_path)},
                suggestions=["Check that the file path is correct"],
            )

        self.kicad_cli = find_kicad_cli()
        if not self.kicad_cli:
            raise ConfigurationError(
                "kicad-cli not found",
                context={"searched": ["PATH", "/Applications/KiCad/KiCad.app/Contents/MacOS"]},
                suggestions=[
                    "Install KiCad 7.0 or later",
                    "Add kicad-cli to your PATH",
                ],
            )

    def export(
        self,
        config: GerberConfig | None = None,
        output_dir: str | Path | None = None,
        progress_callback: ProgressCallback | None = None,
    ) -> Path:
        """
        Export Gerbers with given configuration.

        Args:
            config: Export configuration
            output_dir: Output directory (overrides config.output_dir)
            progress_callback: Optional callback for progress reporting.
                Signature: (progress: float, message: str, cancelable: bool) -> bool
                Returns False to cancel, True to continue.

        Returns:
            Path to output (directory or zip file)
        """
        config = config or GerberConfig()
        out_dir = Path(output_dir) if output_dir else config.output_dir
        if out_dir is None:
            out_dir = self.pcb_path.parent / "gerbers"

        out_dir.mkdir(parents=True, exist_ok=True)

        # Calculate total steps for progress
        total_steps = 1 if config.generate_gerbers else 0  # Gerbers
        if config.generate_drill:
            total_steps += 1
        if config.create_zip:
            total_steps += 1
        current_step = 0

        # Export Gerbers
        if config.generate_gerbers:
            if progress_callback is not None:
                if not progress_callback(
                    current_step / total_steps, "Exporting Gerber files", True
                ):
                    return out_dir
            self._export_gerbers(config, out_dir)
            current_step += 1

        # Export drill files
        if config.generate_drill:
            if progress_callback is not None:
                if not progress_callback(current_step / total_steps, "Exporting drill files", True):
                    return out_dir
            self._export_drill(config, out_dir)
            current_step += 1

        # Create zip if requested
        if config.create_zip:
            if progress_callback is not None:
                if not progress_callback(current_step / total_steps, "Creating zip archive", True):
                    return out_dir
            zip_path = out_dir / config.zip_name
            self._create_zip(out_dir, zip_path)

            # Clean up individual files after zipping when requested
            if config.clean_after_zip:
                self._clean_after_zip(out_dir, zip_path)

            if progress_callback is not None:
                progress_callback(1.0, f"Export complete: {zip_path.name}", False)
            return zip_path

        if progress_callback is not None:
            progress_callback(1.0, "Export complete", False)
        return out_dir

    def export_for_manufacturer(
        self,
        manufacturer: str,
        output_dir: str | Path | None = None,
        progress_callback: ProgressCallback | None = None,
        vscore_layers: list[str] | None = None,
    ) -> Path:
        """
        Export Gerbers using manufacturer preset.

        Args:
            manufacturer: Manufacturer ID (jlcpcb, pcbway, oshpark)
            output_dir: Output directory
            progress_callback: Optional callback for progress reporting.
            vscore_layers: User layers to plot as V-score layers on top of
                the preset (see :attr:`GerberConfig.vscore_layers`).

        Returns:
            Path to output (directory or zip file)

        Raises:
            ValueError: If manufacturer is not supported
        """
        preset = MANUFACTURER_PRESETS.get(manufacturer.lower())
        if preset is None:
            available = list(MANUFACTURER_PRESETS.keys())
            raise ConfigurationError(
                f"Unknown manufacturer: {manufacturer}",
                context={"manufacturer": manufacturer, "available": available},
                suggestions=[f"Use one of: {', '.join(available)}"],
            )

        logger.info(f"Exporting Gerbers for {preset.name}")
        config = preset.config
        if vscore_layers:
            config = replace(config, vscore_layers=list(vscore_layers))
        return self.export(config, output_dir, progress_callback=progress_callback)

    def _export_gerbers(self, config: GerberConfig, output_dir: Path) -> None:
        """Export Gerber files using kicad-cli."""
        # Safety-net fill (issue #2516): if the PCB has zone definitions
        # but no fill polygons, the resulting Gerbers would contain zero
        # G36..G37 polygon-fill regions and the manufactured board would
        # lack plane copper.  Fill zones into a temp PCB so we never
        # silently mutate the user's file, then export Gerbers from the
        # temp file.
        pcb_for_export = self.pcb_path
        tmpdir: tempfile.TemporaryDirectory[str] | None = None
        try:
            if _pcb_has_unfilled_zones(self.pcb_path):
                from kicad_tools.cli.runner import run_fill_zones

                tmpdir = tempfile.TemporaryDirectory(prefix="kct_gerber_fill_")
                # Reuse the original filename so kicad-cli's per-layer
                # output naming is unaffected.
                filled_pcb = Path(tmpdir.name) / self.pcb_path.name
                logger.info(
                    "Gerber export: PCB has unfilled zones; filling to temp file %s",
                    filled_pcb,
                )
                fill_result = run_fill_zones(
                    self.pcb_path,
                    output_path=filled_pcb,
                    kicad_cli=self.kicad_cli,
                )
                if fill_result.success and filled_pcb.exists():
                    pcb_for_export = filled_pcb
                else:
                    # Non-fatal: fall back to the unfilled PCB and let the
                    # downstream Gerber export proceed.  The user will see
                    # missing plane copper in the Gerbers but the export
                    # itself will still succeed.
                    logger.warning(
                        "Gerber export: zone fill failed (%s); Gerbers may lack plane copper",
                        fill_result.stderr or "(no stderr)",
                    )

            if config.verify_zone_fill and _pcb_has_filled_zones(pcb_for_export):
                self._verify_saved_fill(pcb_for_export)
            self._export_gerbers_impl(config, output_dir, pcb_for_export)
        finally:
            if tmpdir is not None:
                tmpdir.cleanup()

    def _verify_saved_fill(self, pcb_path: Path) -> None:
        """Refuse to plot a saved zone fill that a refill would join (Issue #6078).

        Raises :class:`ExportError` when ``kicad-cli pcb drc`` on the saved
        fill finds more unconnected items, ``isolated_copper`` or
        ``copper_sliver`` than the same board after ``--refill-zones``.  A
        check that cannot run (kicad-cli DRC unavailable) only logs a
        warning, as the safety-net fill above does.
        """
        from kicad_tools.drc.geometric import check_saved_fill

        check = check_saved_fill(pcb_path, kicad_cli=self.kicad_cli)
        if not check.ran:
            logger.warning(
                "Gerber export: saved zone fill not verified (%s)", check.note or "no DRC report"
            )
            return
        if check.regressions:
            raise ExportError(
                "Saved zone fill is split where a refill is not; refusing to export Gerbers",
                context={"pcb": str(self.pcb_path), "saved_fill": "; ".join(check.regressions)},
                suggestions=[
                    f"Re-run `kct zones fill {self.pcb_path}` to rewrite the fill, then export again",
                    "Compare `kicad-cli pcb drc` with and without --refill-zones on the board",
                ],
            )

    def _export_gerbers_impl(
        self,
        config: GerberConfig,
        output_dir: Path,
        pcb_path: Path,
    ) -> None:
        """Invoke ``kicad-cli pcb export gerbers`` against ``pcb_path``."""
        cmd = [
            str(self.kicad_cli),
            "pcb",
            "export",
            "gerbers",
            str(pcb_path),
            "--output",
            str(output_dir) + "/",
        ]

        # Add options
        if not config.use_protel_extensions:
            cmd.append("--no-protel-ext")

        if config.use_aux_origin:
            cmd.append("--use-drill-file-origin")

        if config.subtract_soldermask:
            cmd.append("--subtract-soldermask")

        if config.disable_aperture_macros:
            cmd.append("--disable-aperture-macros")

        # Layer selection - kicad-cli 9.x requires comma-separated list
        layers = config.layers if config.layers else self._get_default_layers(config)
        if layers:
            cmd.extend(["--layers", ",".join(layers)])

        logger.debug(f"Running: {' '.join(cmd)}")

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            logger.debug(f"kicad-cli output: {result.stdout}")
        except subprocess.CalledProcessError as e:
            # Capture both stdout and stderr - kicad-cli may output errors to either
            error_output = e.stderr.strip() if e.stderr else ""
            stdout_output = e.stdout.strip() if e.stdout else ""
            combined_output = error_output or stdout_output or "No error output captured"

            logger.error(f"kicad-cli failed (exit code {e.returncode}): {combined_output}")
            raise ExportError(
                "Gerber export failed",
                context={
                    "pcb": str(self.pcb_path),
                    "exit_code": e.returncode,
                    "stderr": error_output or "(empty)",
                    "stdout": stdout_output or "(empty)",
                },
                suggestions=[
                    "Check the KiCad log for details",
                    "Verify the PCB file is valid and can be opened in KiCad",
                    "Ensure all layers referenced exist in the PCB",
                ],
            )

    def _drill_command(self, config: GerberConfig, output_dir: Path) -> list[str]:
        """Build the ``kicad-cli pcb export drill`` argv for ``config``.

        Every option maps onto a flag ``kicad-cli pcb export drill --help``
        lists (KiCad 7 through 10).  ``--merge-npth`` and
        ``--minimal-header`` never existed and made kicad-cli reject the
        whole command (Issue #6167): kicad-cli merges PTH and NPTH by
        default, ``--excellon-separate-th`` splits them, and the minimal
        header is ``--excellon-min-header``.  Values kicad-cli would reject
        raise :class:`ConfigurationError` before anything runs.
        """
        fmt = _DRILL_FORMATS.get(config.drill_format.lower())
        if fmt is None:
            raise ConfigurationError(
                f"Unknown drill format: {config.drill_format}",
                context={"drill_format": config.drill_format},
                suggestions=["Use 'excellon' or 'gerber'"],
            )
        units = _DRILL_UNITS.get(config.drill_units.lower())
        if units is None:
            raise ConfigurationError(
                f"Unknown drill units: {config.drill_units}",
                context={"drill_units": config.drill_units},
                suggestions=["Use 'mm' or 'in'"],
            )
        zeros = config.drill_zeros_format.lower()
        if zeros not in _DRILL_ZEROS_FORMATS:
            raise ConfigurationError(
                f"Unknown drill zeros format: {config.drill_zeros_format}",
                context={"drill_zeros_format": config.drill_zeros_format},
                suggestions=[f"Use one of: {', '.join(_DRILL_ZEROS_FORMATS)}"],
            )
        if fmt == "gerber" and config.merge_pth_npth:
            raise ConfigurationError(
                "Gerber-format drill files cannot merge PTH and NPTH holes",
                context={"drill_format": config.drill_format, "merge_pth_npth": True},
                suggestions=[
                    "Use drill_format='excellon' for a merged drill file",
                    "Or set merge_pth_npth=False",
                ],
            )

        cmd = [
            str(self.kicad_cli),
            "pcb",
            "export",
            "drill",
            str(self.pcb_path),
            "--output",
            str(output_dir) + "/",
            "--format",
            fmt,
        ]
        if fmt == "excellon":
            if not config.merge_pth_npth:
                cmd.append("--excellon-separate-th")
            if config.minimal_header:
                cmd.append("--excellon-min-header")
            cmd.extend(["--excellon-units", units, "--excellon-zeros-format", zeros])

        if config.use_aux_origin:
            origin_value = get_drill_origin_value(self.kicad_cli)
            cmd.append("--drill-origin")
            cmd.append(origin_value)
        return cmd

    def _expected_drill_files(self, config: GerberConfig) -> list[str]:
        """kicad-cli's drill file names for ``config`` (Excellon only).

        Gerber-format drill writes one ``<board>-<span>-drl.gbr`` per
        non-empty hole class, so there is no fixed set to expect.
        """
        stem = self.pcb_path.stem
        if _DRILL_FORMATS.get(config.drill_format.lower()) != "excellon":
            return []
        if config.merge_pth_npth:
            return [f"{stem}.drl"]
        return [f"{stem}-PTH.drl", f"{stem}-NPTH.drl"]

    def _export_drill(self, config: GerberConfig, output_dir: Path) -> None:
        """Export drill files using kicad-cli.

        Raises :class:`ExportError` when kicad-cli fails *or* exits cleanly
        without writing the drill files the config asks for -- a fab package
        must never ship without drill data (Issue #6167).
        """
        cmd = self._drill_command(config, output_dir)
        expected = self._expected_drill_files(config)

        # A drill file from an earlier export in the other PTH/NPTH mode
        # (``board.drl`` next to a fresh ``board-PTH.drl``) would be zipped
        # and sent to the fab as extra holes.  Remove kicad-cli's drill
        # names for this board that this export will not rewrite.
        stem = self.pcb_path.stem
        for name in (f"{stem}.drl", f"{stem}-PTH.drl", f"{stem}-NPTH.drl"):
            stale = output_dir / name
            if name not in expected and stale.is_file():
                logger.info("Drill export: removing stale %s", stale)
                stale.unlink()

        logger.debug(f"Running: {' '.join(cmd)}")
        started = time.time()

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                check=True,
            )
            logger.debug(f"kicad-cli output: {result.stdout}")
        except subprocess.CalledProcessError as e:
            # Capture both stdout and stderr - kicad-cli may output errors to either
            error_output = e.stderr.strip() if e.stderr else ""
            stdout_output = e.stdout.strip() if e.stdout else ""
            combined_output = error_output or stdout_output or "No error output captured"

            logger.error(f"kicad-cli failed (exit code {e.returncode}): {combined_output}")
            raise ExportError(
                "Drill export failed",
                context={
                    "pcb": str(self.pcb_path),
                    "command": " ".join(cmd),
                    "exit_code": e.returncode,
                    "stderr": error_output or "(empty)",
                    "stdout": stdout_output or "(empty)",
                },
                suggestions=[
                    "Check the KiCad log for details",
                    "Verify the PCB file is valid and can be opened in KiCad",
                    "Compare the flags with `kicad-cli pcb export drill --help`",
                ],
            ) from e

        # kicad-cli can exit 0 without writing anything; check the files.
        # Allow 2 s of slack for filesystems with coarse mtimes.
        def fresh(path: Path) -> bool:
            return path.is_file() and path.stat().st_mtime >= started - 2

        if expected:
            missing = [name for name in expected if not fresh(output_dir / name)]
        elif any(fresh(f) for f in output_dir.glob(f"{stem}*-drl.gbr")):
            missing = []
        else:
            missing = [f"{stem}-*-drl.gbr"]
        if missing:
            raise ExportError(
                "Drill export wrote no drill file",
                context={
                    "pcb": str(self.pcb_path),
                    "command": " ".join(cmd),
                    "missing": ", ".join(missing),
                    "stdout": result.stdout.strip() or "(empty)",
                },
                suggestions=["Run the command above by hand and check its output"],
            )

    def _get_default_layers(self, config: GerberConfig) -> list[str]:
        """Get default layers to export based on config.

        Copper layers are derived from the PCB's actual stackup (issue
        #3559): a 4-layer board yields F.Cu, In1.Cu, In2.Cu, B.Cu so the
        inner plane copper is never silently dropped from the Gerbers.
        """
        layers = _pcb_copper_layers(self.pcb_path)

        if config.include_silkscreen:
            layers.extend(["F.SilkS", "B.SilkS"])

        if config.include_soldermask:
            layers.extend(["F.Mask", "B.Mask"])

        if config.include_solderpaste:
            layers.extend(["F.Paste", "B.Paste"])

        if config.include_edge_cuts:
            layers.append("Edge.Cuts")

        vscore = list(config.vscore_layers)
        for layer in missing_vscore_layers(self.pcb_path, vscore):
            logger.warning(
                "Gerber export: --vscore-layer %s is not in %s's layer table; "
                "kicad-cli will plot nothing for it",
                layer,
                self.pcb_path.name,
            )
        if config.include_vscore:
            vscore.extend(pcb_vscore_layers(self.pcb_path))
        for layer in vscore:
            if layer not in layers:
                logger.info("Gerber export: including V-score layer %s", layer)
                layers.append(layer)

        return layers

    @staticmethod
    def _clean_after_zip(source_dir: Path, zip_path: Path) -> None:
        """Remove individual gerber/drill files after creating the zip.

        Removes all files in *source_dir* except the zip archive itself,
        leaving only the zip in the output directory.
        """
        for file in list(source_dir.iterdir()):
            if file.is_file() and file != zip_path:
                file.unlink()
                logger.debug(f"Cleaned up: {file.name}")
        logger.info(f"Cleaned individual gerber files, kept {zip_path.name}")

    def _create_zip(self, source_dir: Path, zip_path: Path) -> None:
        """Create zip file from directory contents."""
        # Remove existing zip
        if zip_path.exists():
            zip_path.unlink()

        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
            for file in source_dir.iterdir():
                if file.is_file() and file != zip_path:
                    zf.write(file, file.name)

        logger.info(f"Created {zip_path}")


def export_gerbers(
    pcb_path: str | Path,
    manufacturer: str = "jlcpcb",
    output_dir: str | Path | None = None,
    progress_callback: ProgressCallback | None = None,
) -> Path:
    """
    Convenience function to export Gerbers.

    Args:
        pcb_path: Path to KiCad PCB file
        manufacturer: Manufacturer ID or "generic"
        output_dir: Output directory
        progress_callback: Optional callback for progress reporting.
            Signature: (progress: float, message: str, cancelable: bool) -> bool
            Returns False to cancel, True to continue.

    Returns:
        Path to output (directory or zip file)
    """
    exporter = GerberExporter(pcb_path)

    if manufacturer.lower() in MANUFACTURER_PRESETS:
        return exporter.export_for_manufacturer(
            manufacturer, output_dir, progress_callback=progress_callback
        )
    else:
        return exporter.export(output_dir=output_dir, progress_callback=progress_callback)
