"""Footprint geometry the parametric model generators derive bodies from.

Reads a footprint -- a ``.kicad_mod`` file, or a footprint placed on a
``.kicad_pcb`` -- and reduces it to what a package body needs: the pads (in
the footprint's own frame), the fabrication-layer outline and the courtyard
(issue #7687).  Everything is pure Python; no CAD kernel is needed.

Coordinates are KiCad footprint-local millimetres: ``+x`` right, ``+y`` *down*
(board view).  The STEP writers flip ``y`` when they emit model geometry,
because a KiCad 3D model is authored ``+y`` up.
"""

from __future__ import annotations

import hashlib
import math
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

__all__ = [
    "BBox",
    "FootprintGeometry",
    "PadGeom",
    "find_footprint_file",
    "footprint_from_node",
    "load_footprint_file",
    "load_footprint_from_pcb",
]


@dataclass(frozen=True)
class BBox:
    """Axis-aligned rectangle in footprint-local mm."""

    x0: float
    y0: float
    x1: float
    y1: float

    @property
    def width(self) -> float:
        return self.x1 - self.x0

    @property
    def height(self) -> float:
        return self.y1 - self.y0

    @property
    def cx(self) -> float:
        return (self.x0 + self.x1) / 2

    @property
    def cy(self) -> float:
        return (self.y0 + self.y1) / 2

    def contains(self, other: BBox, tol: float = 1e-6) -> bool:
        return (
            other.x0 >= self.x0 - tol
            and other.y0 >= self.y0 - tol
            and other.x1 <= self.x1 + tol
            and other.y1 <= self.y1 + tol
        )

    def inset(self, d: float) -> BBox:
        return BBox(self.x0 + d, self.y0 + d, self.x1 - d, self.y1 - d)

    @staticmethod
    def around(points: Iterable[tuple[float, float]]) -> BBox | None:
        pts = list(points)
        if not pts:
            return None
        xs = [p[0] for p in pts]
        ys = [p[1] for p in pts]
        return BBox(min(xs), min(ys), max(xs), max(ys))


@dataclass(frozen=True)
class PadGeom:
    """One pad, reduced to its axis-aligned extent in the footprint frame."""

    number: str
    kind: str  # smd | thru_hole | np_thru_hole | connect
    x: float
    y: float
    w: float  # extent along x after the pad's own rotation
    h: float  # extent along y after the pad's own rotation
    drill: float | None = None

    @property
    def bbox(self) -> BBox:
        return BBox(
            self.x - self.w / 2, self.y - self.h / 2, self.x + self.w / 2, self.y + self.h / 2
        )

    @property
    def is_mounting(self) -> bool:
        """``MP``/unnumbered/mechanical pads (hold-downs, shield tabs)."""
        return self.number in ("", "MP") or not any(c.isdigit() for c in self.number)


@dataclass
class FootprintGeometry:
    """The parts of a footprint a package body is derived from."""

    lib_id: str
    name: str
    pads: list[PadGeom] = field(default_factory=list)
    fab: BBox | None = None
    courtyard: BBox | None = None
    attrs: set[str] = field(default_factory=set)
    #: Axis-aligned F.Fab segments ``(x0, y0, x1, y1)`` (rects split into edges).
    fab_segments: list[tuple[float, float, float, float]] = field(default_factory=list)
    description: str = ""
    source: str = ""
    sha256: str = ""

    @property
    def signal_pads(self) -> list[PadGeom]:
        """Electrical pads: numbered and not plain mechanical holes."""
        return [p for p in self.pads if p.kind != "np_thru_hole" and not p.is_mounting]

    @property
    def mounting_pads(self) -> list[PadGeom]:
        return [p for p in self.pads if p.kind != "np_thru_hole" and p.is_mounting]

    @property
    def pad_extent(self) -> BBox | None:
        return BBox.around(
            corner
            for p in self.pads
            if p.kind != "np_thru_hole"
            for corner in ((p.bbox.x0, p.bbox.y0), (p.bbox.x1, p.bbox.y1))
        )

    @property
    def is_tht(self) -> bool:
        return "through_hole" in self.attrs or any(p.kind == "thru_hole" for p in self.signal_pads)

    def body_outline(self) -> BBox | None:
        """Best available body rectangle: fab outline, else courtyard inset."""
        if self.fab is not None:
            return self.fab
        if self.courtyard is not None:
            return self.courtyard.inset(0.25)
        return None

    def fab_core(self) -> BBox | None:
        """The dominant body rectangle of the fab outline.

        A fab outline is often a rectangle with ears, lead stubs or a pin-1
        notch.  For each side this takes the coordinate of the *longest*
        axis-aligned fab segment parallel to that side (on that half of the
        outline), which recovers the housing rectangle of e.g. a JST
        side-entry connector whose outline has two hold-down ears.  Falls back
        to :meth:`body_outline` when the outline has no usable segments.
        """
        box = self.body_outline()
        if box is None or not self.fab_segments:
            return box
        cx, cy = box.cx, box.cy

        def pick(horizontal: bool, low: bool, default: float) -> float:
            best, best_len = default, 0.0
            for x0, y0, x1, y1 in self.fab_segments:
                if horizontal and abs(y0 - y1) < 1e-6:
                    coord, length = y0, abs(x1 - x0)
                    side_ok = coord < cy if low else coord > cy
                elif not horizontal and abs(x0 - x1) < 1e-6:
                    coord, length = x0, abs(y1 - y0)
                    side_ok = coord < cx if low else coord > cx
                else:
                    continue
                if side_ok and length > best_len + 1e-9:
                    best, best_len = coord, length
            return best

        return BBox(
            pick(False, True, box.x0),
            pick(True, True, box.y0),
            pick(False, False, box.x1),
            pick(True, False, box.y1),
        )

    def pad_axis(self) -> str:
        """``"x"`` or ``"y"``: the axis with the larger signal-pad-centre spread.

        For a two-terminal part (chip, shunt, power inductor) this is the axis
        joining the terminals, i.e. the part's long axis.
        """
        pads = self.signal_pads or self.pads
        if len(pads) < 2:
            return "x"
        xs = [p.x for p in pads]
        ys = [p.y for p in pads]
        return "x" if (max(xs) - min(xs)) >= (max(ys) - min(ys)) else "y"


def _xy(node: Any, tag: str) -> tuple[float, float] | None:
    sub = node.find(tag) if node is not None else None
    if sub is None:
        return None
    x, y = sub.get_float(0), sub.get_float(1)
    if x is None or y is None:
        return None
    return x, y


def _layer_names(node: Any) -> set[str]:
    names: set[str] = set()
    for child in node.iter_children():
        if child.name in ("layer", "layers"):
            names.update(str(a) for a in child.get_atoms())
    return names


def _graphic_points(node: Any) -> list[tuple[float, float]]:
    """Corner/extreme points of one ``fp_*`` graphic, footprint-local."""
    kind = node.name
    if kind in ("fp_line", "fp_rect"):
        pts = [_xy(node, "start"), _xy(node, "end")]
        return [p for p in pts if p is not None]
    if kind == "fp_circle":
        c, e = _xy(node, "center"), _xy(node, "end")
        if c is None or e is None:
            return []
        r = math.hypot(e[0] - c[0], e[1] - c[1])
        return [(c[0] - r, c[1] - r), (c[0] + r, c[1] + r)]
    if kind == "fp_arc":
        pts = [_xy(node, "start"), _xy(node, "mid"), _xy(node, "end")]
        return [p for p in pts if p is not None]
    if kind == "fp_poly":
        out = []
        pts = node.find("pts")
        if pts is not None:
            for xy in pts.find_all("xy"):
                x, y = xy.get_float(0), xy.get_float(1)
                if x is not None and y is not None:
                    out.append((x, y))
        return out
    return []


def _fab_segments(fp: Any) -> list[tuple[float, float, float, float]]:
    segs: list[tuple[float, float, float, float]] = []
    for child in fp.iter_children():
        if child.name not in ("fp_line", "fp_rect"):
            continue
        if not any(n.endswith(".Fab") and n.startswith("F.") for n in _layer_names(child)):
            continue
        a, b = _xy(child, "start"), _xy(child, "end")
        if a is None or b is None:
            continue
        if child.name == "fp_rect":
            (x0, y0), (x1, y1) = a, b
            segs += [(x0, y0, x1, y0), (x1, y0, x1, y1), (x1, y1, x0, y1), (x0, y1, x0, y0)]
        elif abs(a[0] - b[0]) < 1e-6 or abs(a[1] - b[1]) < 1e-6:
            segs.append((a[0], a[1], b[0], b[1]))
    return segs


def _layer_bbox(fp: Any, suffix: str) -> BBox | None:
    pts: list[tuple[float, float]] = []
    for child in fp.iter_children():
        if not child.name or not child.name.startswith("fp_"):
            continue
        layers = _layer_names(child)
        if any(name.endswith(suffix) and name.startswith("F.") for name in layers):
            pts.extend(_graphic_points(child))
    return BBox.around(pts)


def footprint_from_node(
    fp: Any, *, lib_id: str | None = None, source: str = "", text: str | None = None
) -> FootprintGeometry:
    """Reduce a parsed ``footprint`` node to :class:`FootprintGeometry`.

    Works for library footprints and for footprints placed on a board: pad
    ``(at x y rot)`` on a board carries the *absolute* rotation, so the
    footprint's own rotation is subtracted to get the pad's local rotation.
    """
    raw_id = str(fp.get_first_atom() or "")
    lib_id = lib_id or raw_id
    name = lib_id.split(":", 1)[-1]
    fp_rot = 0.0
    at = _direct(fp, "at")
    if at is not None:
        fp_rot = at.get_float(2) or 0.0

    attrs: set[str] = set()
    attr = _direct(fp, "attr")
    if attr is not None:
        attrs = {str(a) for a in attr.get_atoms()}
    descr = _direct(fp, "descr")
    description = str(descr.get_first_atom() or "") if descr is not None else ""

    pads: list[PadGeom] = []
    for pad in fp.iter_children():
        if pad.name != "pad":
            continue
        number = pad.get_string(0) or ""
        kind = pad.get_string(1) or "smd"
        pat = pad.find("at")
        size = pad.find("size")
        if pat is None or size is None:
            continue
        px, py = pat.get_float(0) or 0.0, pat.get_float(1) or 0.0
        rot = (pat.get_float(2) or 0.0) - fp_rot
        sw, sh = size.get_float(0) or 0.0, size.get_float(1) or 0.0
        if kind != "smd" and sh == 0.0:
            sh = sw
        theta = math.radians(rot)
        w = abs(sw * math.cos(theta)) + abs(sh * math.sin(theta))
        h = abs(sw * math.sin(theta)) + abs(sh * math.cos(theta))
        drill_node = pad.find("drill")
        drill = drill_node.get_float(0) if drill_node is not None else None
        if drill is None and drill_node is not None:
            drill = drill_node.get_float(1)
        pads.append(PadGeom(number, kind, px, py, round(w, 6), round(h, 6), drill))

    digest = hashlib.sha256((text or "").encode()).hexdigest() if text else ""
    return FootprintGeometry(
        lib_id=lib_id,
        name=name,
        pads=pads,
        fab=_layer_bbox(fp, ".Fab"),
        courtyard=_layer_bbox(fp, ".CrtYd"),
        attrs=attrs,
        fab_segments=_fab_segments(fp),
        description=description,
        source=source,
        sha256=digest,
    )


def _direct(node: Any, tag: str) -> Any:
    for child in node.iter_children():
        if child.name == tag:
            return child
    return None


def load_footprint_file(path: Path | str, lib_id: str | None = None) -> FootprintGeometry:
    """Load a ``.kicad_mod`` file."""
    from kicad_tools.sexp import parse_string

    path = Path(path)
    text = path.read_text(encoding="utf-8")
    doc = parse_string(text)
    if doc.name not in ("footprint", "module"):
        raise ValueError(f"{path}: not a footprint (root is {doc.name!r})")
    if lib_id is None:
        lib = path.parent.name.removesuffix(".pretty") if path.parent.suffix == ".pretty" else ""
        lib_id = f"{lib}:{path.stem}" if lib else path.stem
    return footprint_from_node(
        doc, lib_id=lib_id, source=f"{path.parent.name}/{path.name}", text=text
    )


def load_footprint_from_pcb(
    pcb_path: Path | str, *, reference: str | None = None, lib_id: str | None = None
) -> FootprintGeometry:
    """Load a footprint placed on *pcb_path*, by reference or by ``lib_id``."""
    from kicad_tools.cli.runner import _get_fp_reference
    from kicad_tools.schema.pcb import _is_footprint_tag
    from kicad_tools.sexp import parse_file

    if reference is None and lib_id is None:
        raise ValueError("need a reference or a lib_id to pick a footprint")
    pcb_path = Path(pcb_path)
    doc = parse_file(pcb_path)
    for fp in doc.children:
        if not _is_footprint_tag(fp.name):
            continue
        fp_id = str(fp.get_first_atom() or "")
        if reference is not None and _get_fp_reference(fp) != reference:
            continue
        if lib_id is not None and fp_id != lib_id:
            continue
        text = fp.to_string()
        # a portable label (no absolute paths): the sha256 pins the content
        label = f"{pcb_path.name}#{_get_fp_reference(fp) or fp_id}"
        return footprint_from_node(fp, lib_id=fp_id, source=label, text=text)
    what = f"reference {reference!r}" if reference else f"lib_id {lib_id!r}"
    raise LookupError(f"{pcb_path}: no footprint with {what}")


def find_footprint_file(lib_id: str, *, project_dir: Path | None = None) -> Path | None:
    """Locate the ``.kicad_mod`` for *lib_id* (project fp-lib-table, then KiCad)."""
    from kicad_tools.footprints.library_path import detect_kicad_library_path, parse_library_id

    library, name = parse_library_id(lib_id)
    if project_dir is not None and library:
        from kicad_tools.footprints.fp_lib_table import (
            find_project_fp_lib_table,
            parse_fp_lib_table,
        )

        table = find_project_fp_lib_table(project_dir)
        if table is not None:
            for entry in parse_fp_lib_table(table):
                if entry.name != library or entry.resolved_path is None:
                    continue
                candidate = entry.resolved_path / f"{name}.kicad_mod"
                if candidate.is_file():
                    return candidate
    paths = detect_kicad_library_path()
    if not paths.found:
        return None
    return paths.get_footprint_file(library or "", name, fallback_search=library is None)
