"""Silkscreen repair: line widths, text heights and pad clearance.

Walks the raw SExp tree to find silkscreen graphic elements (fp_line, fp_rect,
fp_circle, fp_arc, fp_poly, gr_line, gr_rect, gr_circle, gr_arc) whose stroke width is
below the manufacturer minimum, and sets the width to the minimum.

Also walks text elements (fp_text, property, gr_text) on silkscreen layers and
scales undersized font heights (and proportionally widths/thickness) up to the
manufacturer minimum.

Finally, :meth:`SilkscreenRepairer.repair_pad_clearance` clips footprint
``fp_line`` silk strokes back from same-side pad apertures, using the same
geometry as the ``silk_pad_clearance`` check so the fix and the check agree.

:meth:`SilkscreenRepairer.repair_hole_clearance` clips footprint silk
``fp_line`` / ``fp_poly`` / ``fp_rect`` graphics off drilled holes (vias of any
tenting, PTH and NPTH), using the ``silk_hole_overlap`` check's own hole set
and threshold (Issue #9054).  It is an explicit post-routing recipe step: the
router never calls it, so routing never changes footprint silk (#5744).

This operates on the raw SExp tree (not the read-only schema dataclasses) so
that modifications can be written back to disk, following the same pattern as
``repair_clearance.py`` and ``fix_vias_cmd.py``.
"""

from __future__ import annotations

import copy
import math
import uuid as _uuid
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.core.sexp_file import save_pcb
from kicad_tools.schema.pcb import _find_all_footprints
from kicad_tools.sexp.parser import SExp, parse_file

# Silkscreen layer names recognised by KiCad (pre-8.0 and 8.0+).
SILKSCREEN_LAYERS = frozenset(("F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen"))

# Graphic element types that live inside footprints.
FP_GRAPHIC_TYPES = ("fp_line", "fp_rect", "fp_circle", "fp_arc", "fp_poly")

# Graphic element types at board level.
GR_GRAPHIC_TYPES = ("gr_line", "gr_rect", "gr_circle", "gr_arc")


@dataclass
class SilkscreenFix:
    """Record of a single silkscreen element that was (or would be) fixed."""

    element_type: str  # e.g. "fp_line", "gr_rect"
    layer: str
    old_width: float
    new_width: float
    footprint_ref: str  # empty string for board-level graphics


@dataclass
class SilkscreenRepairResult:
    """Aggregate result of a silkscreen repair pass."""

    min_width_mm: float = 0.0
    fixes: list[SilkscreenFix] = field(default_factory=list)

    @property
    def total_fixed(self) -> int:
        return len(self.fixes)


# Text element types inside footprints.
FP_TEXT_TYPES = ("fp_text", "property")

# Text element types at board level.
GR_TEXT_TYPES = ("gr_text",)


@dataclass
class TextHeightFix:
    """Record of a single text element that was (or would be) fixed."""

    element_type: str  # e.g. "fp_text", "property", "gr_text"
    layer: str
    old_height: float
    new_height: float
    old_width: float
    new_width: float
    old_thickness: float | None
    new_thickness: float | None
    footprint_ref: str  # empty string for board-level text


@dataclass
class TextHeightRepairResult:
    """Aggregate result of a text height repair pass."""

    min_height_mm: float = 0.0
    fixes: list[TextHeightFix] = field(default_factory=list)

    @property
    def total_fixed(self) -> int:
        return len(self.fixes)


@dataclass
class PadClearanceFix:
    """Record of one ``fp_line`` that was (or would be) trimmed or removed."""

    footprint_ref: str
    element_type: str  # always "fp_line" today
    layer: str
    action: str  # "trimmed" or "removed"
    old_length: float
    new_length: float  # total length of the kept part(s); 0.0 when removed
    pieces: int = 0  # number of fp_line segments left in the file


@dataclass
class PadClearanceSkip:
    """A violating element left alone (e.g. a pin-1 / polarity marker)."""

    footprint_ref: str
    element_type: str
    layer: str
    reason: str


@dataclass
class PadClearanceRepairResult:
    """Aggregate result of a silk-to-pad clearance repair pass."""

    min_clearance_mm: float = 0.0
    fixes: list[PadClearanceFix] = field(default_factory=list)
    skipped: list[PadClearanceSkip] = field(default_factory=list)

    @property
    def total_fixed(self) -> int:
        return len(self.fixes)


@dataclass
class HoleClearanceFix:
    """Record of one footprint silk graphic clipped (or removed) off a drill."""

    footprint_ref: str
    element_type: str  # "fp_line" | "fp_poly" | "fp_rect"
    layer: str
    action: str  # "trimmed" or "removed"
    holes: list[str] = field(default_factory=list)  # drill labels it crossed
    #: Printed ink area (mm^2) before and after the clip, from the same
    #: geometry ``silk_hole_overlap`` measures -- how much silk was removed.
    old_area: float = 0.0
    new_area: float = 0.0
    pieces: int = 0  # graphics left in the file in its place


@dataclass
class HoleClearanceRepairResult:
    """Aggregate result of a silk-to-drill clip pass (Issue #9054)."""

    min_clearance_mm: float = 0.0
    fixes: list[HoleClearanceFix] = field(default_factory=list)
    skipped: list[PadClearanceSkip] = field(default_factory=list)
    #: ``silk_hole_overlap`` findings on *graphics* still present after the
    #: pass, re-measured with the real rule (empty on a dry run).
    residual: list[str] = field(default_factory=list)
    #: ``silk_hole_overlap`` findings on *text* -- never clipped; move the
    #: text with ``kct place-silk-refs`` instead.
    text_residual: list[str] = field(default_factory=list)

    @property
    def total_fixed(self) -> int:
        return len(self.fixes)


# Polygon remnants smaller than this (mm^2) are dropped: with the outline
# stroke they print as a dot, not a shape.
_MIN_POLY_AREA_MM2 = 1e-3

# Deterministic UUIDs for graphics the hole clip has to split, so the same
# input always yields byte-identical output (uuid4 would not).
_HOLE_CLIP_NAMESPACE = _uuid.UUID("9054c1ea-0000-4000-8000-000000009054")

# Extra margin (mm) added to the keep-out so clipped endpoints land strictly
# on the clear side of the check's tolerance despite polygonal buffers.
_CLIP_EPSILON_MM = 1e-3

_FP_GRAPHIC_KINDS = {
    "fp_line": "line",
    "fp_rect": "rect",
    "fp_circle": "circle",
    "fp_arc": "arc",
    "fp_poly": "poly",
}


class SilkscreenRepairer:
    """Repair silkscreen line widths below a manufacturer minimum.

    Usage::

        repairer = SilkscreenRepairer(Path("board.kicad_pcb"))
        result = repairer.repair_line_widths(min_width_mm=0.15)
        repairer.save()
    """

    def __init__(self, pcb_path: str | Path) -> None:
        self.path = Path(pcb_path)
        self.doc: SExp = parse_file(self.path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def repair_line_widths(
        self,
        min_width_mm: float,
        dry_run: bool = False,
    ) -> SilkscreenRepairResult:
        """Widen all silkscreen strokes below *min_width_mm* to that minimum.

        Args:
            min_width_mm: The minimum acceptable stroke width in mm.
            dry_run: If ``True``, collect fixes but do not mutate the tree.

        Returns:
            A :class:`SilkscreenRepairResult` with every fix recorded.
        """
        result = SilkscreenRepairResult(min_width_mm=min_width_mm)

        # --- Footprint-level graphics ---
        for fp_node in _find_all_footprints(self.doc):
            fp_ref = self._footprint_reference(fp_node)
            for gtype in FP_GRAPHIC_TYPES:
                for graphic_node in fp_node.find_all(gtype):
                    self._maybe_fix(
                        graphic_node,
                        element_type=gtype,
                        footprint_ref=fp_ref,
                        min_width_mm=min_width_mm,
                        dry_run=dry_run,
                        result=result,
                    )

        # --- Board-level graphics ---
        for gtype in GR_GRAPHIC_TYPES:
            for graphic_node in self.doc.find_all(gtype):
                self._maybe_fix(
                    graphic_node,
                    element_type=gtype,
                    footprint_ref="",
                    min_width_mm=min_width_mm,
                    dry_run=dry_run,
                    result=result,
                )

        return result

    def repair_text_heights(
        self,
        min_height_mm: float,
        dry_run: bool = False,
    ) -> TextHeightRepairResult:
        """Scale up silkscreen text whose font height is below *min_height_mm*.

        Font width and thickness are scaled proportionally to preserve the
        original aspect ratio.

        Args:
            min_height_mm: The minimum acceptable font height in mm.
            dry_run: If ``True``, collect fixes but do not mutate the tree.

        Returns:
            A :class:`TextHeightRepairResult` with every fix recorded.
        """
        result = TextHeightRepairResult(min_height_mm=min_height_mm)

        # --- Footprint-level text ---
        for fp_node in _find_all_footprints(self.doc):
            fp_ref = self._footprint_reference(fp_node)
            for ttype in FP_TEXT_TYPES:
                for text_node in fp_node.find_all(ttype):
                    self._maybe_fix_text(
                        text_node,
                        element_type=ttype,
                        footprint_ref=fp_ref,
                        min_height_mm=min_height_mm,
                        dry_run=dry_run,
                        result=result,
                    )

        # --- Board-level text ---
        for ttype in GR_TEXT_TYPES:
            for text_node in self.doc.find_all(ttype):
                self._maybe_fix_text(
                    text_node,
                    element_type=ttype,
                    footprint_ref="",
                    min_height_mm=min_height_mm,
                    dry_run=dry_run,
                    result=result,
                )

        return result

    def repair_pad_clearance(
        self,
        min_clearance_mm: float,
        dry_run: bool = False,
    ) -> PadClearanceRepairResult:
        """Trim or remove footprint silk lines too close to same-side pads.

        Uses the geometry of the ``silk_pad_clearance`` check (stroke buffered
        by half its width versus the pad's mask aperture), so a clipped line
        no longer trips that check.  A line that crosses the limit is
        shortened to its clear part(s) (a line cut in the middle becomes two
        lines); a line wholly inside the limit is removed.  Elements the
        ``pin1_marker`` rule accepts as the footprint's pin-1 / polarity mark
        are never edited and are reported in ``result.skipped``.  Only
        ``fp_line`` strokes are clipped.

        Args:
            min_clearance_mm: Required silk-to-pad clearance in mm.
            dry_run: If ``True``, collect fixes but do not mutate the tree.
        """
        from kicad_tools._shapely import require_shapely

        require_shapely("silk pad clearance repair")
        from shapely.geometry import LineString  # type: ignore[import-untyped]
        from shapely.ops import unary_union  # type: ignore[import-untyped]
        from shapely.strtree import STRtree  # type: ignore[import-untyped]

        from kicad_tools.schema.pcb import Footprint, FootprintGraphic
        from kicad_tools.validate.rules.clearance import _pad_polygon
        from kicad_tools.validate.rules.pin1_marker import Pin1MarkerRule
        from kicad_tools.validate.rules.silkscreen import _fp_transform, _silk_side

        result = PadClearanceRepairResult(min_clearance_mm=min_clearance_mm)

        setup = self.doc.find("setup")
        default_margin = 0.0
        if setup is not None:
            node = setup.find("pad_to_mask_clearance")
            if node is not None and node.get_float(0) is not None:
                default_margin = float(node.get_float(0))  # type: ignore[arg-type]

        footprints = [(node, Footprint.from_sexp(node)) for node in _find_all_footprints(self.doc)]

        # Pad apertures per side, exactly as check_silk_pad_clearance builds them.
        per_side: dict[str, list] = {"F": [], "B": []}
        for _node, fp in footprints:
            for pad in fp.pads:
                if pad.type not in ("smd", "thru_hole", "connect"):
                    continue
                margin = pad.solder_mask_margin
                if margin is None:
                    margin = default_margin
                copper = _pad_polygon(pad, fp)
                if copper is None:
                    continue
                for side in ("F", "B"):
                    if "*.Cu" not in pad.layers and f"{side}.Cu" not in pad.layers:
                        continue
                    exposed = "*.Mask" in pad.layers or f"{side}.Mask" in pad.layers
                    geom = copper.buffer(max(margin or 0, 0)) if exposed else copper
                    if not geom.is_empty:
                        per_side[side].append(geom)
        trees = {side: STRtree(geoms) for side, geoms in per_side.items() if geoms}
        if not trees:
            return result

        rule = Pin1MarkerRule()
        for fp_node, fp in footprints:
            nodes = [
                n
                for n in fp_node.children
                if not n.is_atom
                and n.name is not None
                and n.name in _FP_GRAPHIC_KINDS
                and self._is_silkscreen(n)
            ]
            if not any(n.name == "fp_line" for n in nodes):
                continue
            graphics = [
                FootprintGraphic.from_sexp(n, _FP_GRAPHIC_KINDS[str(n.name)]) for n in nodes
            ]
            fwd = _fp_transform(fp)
            theta = math.radians(fp.rotation)  # inverse of the -rotation forward map
            cos_t, sin_t = math.cos(theta), math.sin(theta)

            def inverse(pt, fp=fp, cos_t=cos_t, sin_t=sin_t):
                dx, dy = pt[0] - fp.position[0], pt[1] - fp.position[1]
                return (dx * cos_t - dy * sin_t, dx * sin_t + dy * cos_t)

            markers: set[int] | None = None
            fp_ref = self._footprint_reference(fp_node)
            for idx, (node, graphic) in enumerate(zip(nodes, graphics, strict=True)):
                if node.name != "fp_line" or graphic.stroke_width <= 0:
                    continue
                gside = _silk_side(graphic.layer)
                if gside is None or gside not in trees:
                    continue
                p0, p1 = fwd(graphic.start), fwd(graphic.end)
                if p0 == p1:
                    continue
                line = LineString([p0, p1])
                width = graphic.stroke_width
                reach = min_clearance_mm + width / 2.0
                near = trees[gside].query(line.buffer(reach + _CLIP_EPSILON_MM))
                if len(near) == 0:
                    continue
                keepout = unary_union([per_side[gside][int(i)] for i in near]).buffer(
                    reach + _CLIP_EPSILON_MM, quad_segs=16
                )
                if not line.intersects(keepout):
                    continue

                if markers is None:
                    markers = rule.marker_graphic_indices(fp, graphics)
                if idx in markers:
                    result.skipped.append(
                        PadClearanceSkip(
                            footprint_ref=fp_ref,
                            element_type="fp_line",
                            layer=graphic.layer,
                            reason="pin-1 / polarity marker",
                        )
                    )
                    continue

                kept = line.difference(keepout)
                parts = (
                    [kept] if kept.geom_type == "LineString" else list(getattr(kept, "geoms", []))
                )
                # A sliver shorter than the stroke prints as a blob, not a line.
                parts = [g for g in parts if not g.is_empty and g.length >= width]
                new_length = sum(g.length for g in parts)
                result.fixes.append(
                    PadClearanceFix(
                        footprint_ref=fp_ref,
                        element_type="fp_line",
                        layer=graphic.layer,
                        action="trimmed" if parts else "removed",
                        old_length=line.length,
                        new_length=new_length,
                        pieces=len(parts),
                    )
                )
                if dry_run:
                    continue
                self._rewrite_line(fp_node, node, [[inverse(c) for c in g.coords] for g in parts])
        return result

    def repair_hole_clearance(
        self,
        min_clearance_mm: float | None = None,
        dry_run: bool = False,
    ) -> HoleClearanceRepairResult:
        """Clip footprint silk graphics off drilled holes (Issue #9054).

        The holes are :func:`~kicad_tools.validate.rules.silkscreen.iter_drill_holes`
        -- every via drill whatever its tenting, fill or capping, plus PTH
        and NPTH pad holes -- and the default clearance is
        :data:`~kicad_tools.silkscreen._silk_defaults.SILK_HOLE_CLEARANCE_MM`,
        the very set and threshold ``silk_hole_overlap`` uses, so a clipped
        board passes that check.  Each hole clips only the silk side(s) it
        actually pierces: a blind/micro via reaches one outer side, a
        buried via neither (Issue #9076).

        Only the silk that has to go is removed: each graphic's printed ink
        must stay ``min_clearance_mm`` (plus a 1 um safety margin) from the
        drill, so

        * an ``fp_line`` keeps the part(s) of its centreline outside the
          drill buffered by ``min_clearance_mm + width / 2`` (a line crossed
          in the middle becomes two lines; remnants shorter than the stroke
          width are dropped);
        * a filled ``fp_poly`` / ``fp_rect`` becomes the polygon minus that
          buffered drill, written as ``fp_poly`` (several when the cut splits
          it; a drill wholly inside the shape is cut out through a
          zero-width keyhole bridge, since ``fp_poly`` has no holes);
        * an unfilled ``fp_poly`` / ``fp_rect`` outline is no longer closed
          once cut, so its surviving runs are written as ``fp_line`` graphics.

        Graphics that do not reach a hole are left byte-identical.  Arcs and
        circles are not clipped and are reported in ``result.skipped``.  Text
        is never touched: text over a hole lands in
        ``result.text_residual`` for ``kct place-silk-refs``.  Pin-1 markers
        are clipped like any other silk -- a fab cannot print ink over a
        hole -- so re-run the ``pin1_marker`` check afterwards.

        This is an explicit recipe step, run after routing so it sees the
        routed vias.  The router never calls it (#5744).

        Args:
            min_clearance_mm: Required silk-to-hole clearance in mm; defaults
                to ``SILK_HOLE_CLEARANCE_MM``.
            dry_run: If ``True``, collect fixes but do not mutate the tree.
        """
        from kicad_tools._shapely import require_shapely

        require_shapely("silk hole clearance repair")
        from shapely.geometry import LineString, Polygon
        from shapely.ops import linemerge, unary_union
        from shapely.strtree import STRtree

        from kicad_tools.schema.pcb import Footprint, FootprintGraphic, Via
        from kicad_tools.silkscreen._silk_defaults import SILK_HOLE_CLEARANCE_MM
        from kicad_tools.validate.rules.silkscreen import (
            _fp_transform,
            _silk_side,
            _stroke_geometry,
            iter_drill_holes,
            silk_hits_hole,
        )

        clearance = SILK_HOLE_CLEARANCE_MM if min_clearance_mm is None else min_clearance_mm
        if not math.isfinite(clearance) or clearance < 0:
            raise ValueError("min_clearance_mm must be finite and nonnegative")
        result = HoleClearanceRepairResult(min_clearance_mm=clearance)

        # Everything in the file's own frame: footprints and vias parsed
        # straight from the tree (no board-origin shift), as the pad clip does.
        footprints = [(node, Footprint.from_sexp(node)) for node in _find_all_footprints(self.doc)]
        vias = [Via.from_sexp(node) for node in self.doc.find_all("via")]
        holes = list(iter_drill_holes(vias, [fp for _n, fp in footprints]))
        if holes:
            tree = STRtree([g for g, _label, _sides in holes])

            for fp_node, fp in footprints:
                fp_ref = self._footprint_reference(fp_node)
                fwd = _fp_transform(fp)
                theta = math.radians(fp.rotation)
                cos_t, sin_t = math.cos(theta), math.sin(theta)

                def inverse(pt, fp=fp, cos_t=cos_t, sin_t=sin_t):
                    dx, dy = pt[0] - fp.position[0], pt[1] - fp.position[1]
                    return (dx * cos_t - dy * sin_t, dx * sin_t + dy * cos_t)

                for node in [
                    n
                    for n in fp_node.children
                    if not n.is_atom and n.name in _FP_GRAPHIC_KINDS and self._is_silkscreen(n)
                ]:
                    kind = _FP_GRAPHIC_KINDS[str(node.name)]
                    graphic = FootprintGraphic.from_sexp(node, kind)
                    silk_side = _silk_side(graphic.layer)
                    if silk_side is None:
                        continue
                    ink = _stroke_geometry(graphic, fwd)
                    if ink is None or ink.is_empty:
                        continue
                    query = ink.buffer(clearance) if clearance > 0 else ink
                    hit = [
                        holes[int(i)]
                        for i in tree.query(query)
                        if silk_side in holes[int(i)][2]
                        and silk_hits_hole(ink, holes[int(i)][0], clearance)
                    ]
                    if not hit:
                        continue
                    labels = sorted(label for _g, label, _sides in hit)
                    if kind not in ("line", "poly", "rect"):
                        result.skipped.append(
                            PadClearanceSkip(
                                footprint_ref=fp_ref,
                                element_type=str(node.name),
                                layer=graphic.layer,
                                reason=f"fp_{kind} is not clipped (over {', '.join(labels)})",
                            )
                        )
                        continue

                    width = max(graphic.stroke_width, 0.0)
                    keepout = unary_union([g for g, _label, _sides in hit]).buffer(
                        clearance + width / 2.0 + _CLIP_EPSILON_MM, quad_segs=16
                    )
                    if kind == "line":
                        outline = [fwd(graphic.start), fwd(graphic.end)]
                        filled = False
                    elif kind == "rect":
                        (sx, sy), (ex, ey) = graphic.start, graphic.end
                        outline = [fwd(p) for p in ((sx, sy), (ex, sy), (ex, ey), (sx, ey))]
                        filled = graphic.is_filled
                    else:
                        outline = [fwd(p) for p in graphic.points]
                        filled = graphic.is_filled

                    new_lines: list[list[tuple[float, float]]] = []
                    new_polys: list[list[tuple[float, float]]] = []
                    if filled:
                        shape = Polygon(outline)
                        if not shape.is_valid:
                            shape = shape.buffer(0)
                        kept = shape.difference(keepout)
                        rings = [
                            _keyhole_ring(part)
                            for part in getattr(kept, "geoms", [kept])
                            if part.geom_type == "Polygon" and part.area >= _MIN_POLY_AREA_MM2
                        ]
                        if any(r is None for r in rings):
                            result.skipped.append(
                                PadClearanceSkip(
                                    footprint_ref=fp_ref,
                                    element_type=str(node.name),
                                    layer=graphic.layer,
                                    reason="clipped shape has a hole no bridge can reach",
                                )
                            )
                            continue
                        new_polys = [r for r in rings if r is not None]
                    else:
                        closed = kind != "line"
                        path = LineString(outline + [outline[0]] if closed else outline)
                        kept = path.difference(keepout)
                        if closed and kept.geom_type == "MultiLineString":
                            kept = linemerge(kept)
                        for part in getattr(kept, "geoms", [kept]):
                            if part.geom_type != "LineString" or part.is_empty:
                                continue
                            if part.length < max(width, 1e-6):
                                continue
                            new_lines.append(list(part.coords))

                    new_ink = unary_union(
                        [LineString(c).buffer(width / 2.0) for c in new_lines]
                        + [Polygon(c).buffer(0).buffer(width / 2.0) for c in new_polys]
                    )
                    pieces = (
                        sum(len(c) - 1 for c in new_lines) if kind != "line" else len(new_lines)
                    ) + len(new_polys)
                    result.fixes.append(
                        HoleClearanceFix(
                            footprint_ref=fp_ref,
                            element_type=str(node.name),
                            layer=graphic.layer,
                            action="trimmed" if pieces else "removed",
                            holes=labels,
                            old_area=float(ink.area),
                            new_area=float(new_ink.area) if not new_ink.is_empty else 0.0,
                            pieces=pieces,
                        )
                    )
                    if dry_run:
                        continue
                    replacements = self._hole_clip_nodes(
                        node,
                        fp_ref,
                        kind,
                        [[inverse(p) for p in c] for c in new_lines],
                        [[inverse(p) for p in c] for c in new_polys],
                    )
                    index = fp_node.children.index(node)
                    fp_node.children[index : index + 1] = replacements

        if not dry_run:
            self._measure_hole_residual(result)
        return result

    def _measure_hole_residual(self, result: HoleClearanceRepairResult) -> None:
        """Re-measure ``silk_hole_overlap`` with the real rule on the edited tree."""
        import re

        from kicad_tools.manufacturers.base import DesignRules
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.sexp.parser import parse_string, serialize_sexp
        from kicad_tools.validate.rules.silkscreen import check_silk_hole_overlap

        rules = DesignRules(
            min_trace_width_mm=0.0,
            min_clearance_mm=0.0,
            min_via_drill_mm=0.0,
            min_via_diameter_mm=0.0,
            min_annular_ring_mm=0.0,
        )
        pcb = PCB(parse_string(serialize_sexp(self.doc)))
        graphic = re.compile(r"(\((fp|gr)_[a-z]+\)$)|(^gr_(line|rect|circle|arc|poly|curve)$)")
        for violation in check_silk_hole_overlap(pcb, rules).violations:
            text = " / ".join(violation.items)
            if graphic.search(violation.items[0]):
                result.residual.append(text)
            else:
                result.text_residual.append(text)

    @staticmethod
    def _hole_clip_nodes(
        node: SExp,
        fp_ref: str,
        kind: str,
        lines: list[list[tuple[float, float]]],
        polys: list[list[tuple[float, float]]],
    ) -> list[SExp]:
        """Build the graphics that replace ``node`` (footprint-local coords).

        A clipped ``fp_line`` keeps its own node (and UUID) for its first
        piece; everything else is new, with a UUID derived from the source
        UUID and the piece geometry so the output is reproducible.
        """
        from kicad_tools.sexp.parser import parse_string, serialize_sexp

        source_uuid = ""
        if (uid := node.find("uuid")) is not None:
            source_uuid = str(uid.get_first_atom() or "")
        layer = str(node.find("layer").get_first_atom())  # type: ignore[union-attr]
        stroke = node.find("stroke")
        stroke_text = (
            serialize_sexp(stroke) if stroke is not None else "(stroke (width 0) (type solid))"
        )

        def fmt(value: float) -> str:
            return f"{round(value, 6):g}" if abs(value) >= 1e-6 else "0"

        def new_uuid(tag: str, coords: list[tuple[float, float]]) -> str:
            key = f"{fp_ref}:{source_uuid}:{tag}:{[(fmt(x), fmt(y)) for x, y in coords]}"
            return str(_uuid.uuid5(_HOLE_CLIP_NAMESPACE, key))

        out: list[SExp] = []
        for index, coords in enumerate(lines):
            segments = list(zip(coords, coords[1:], strict=False))
            for seg_index, (start, end) in enumerate(segments):
                if kind == "line" and index == 0 and seg_index == 0:
                    clone = node
                    for tag, (x, y) in (("start", start), ("end", end)):
                        child = clone.find(tag)
                        assert child is not None
                        child.set_atom(0, round(x, 6))
                        child.set_atom(1, round(y, 6))
                    out.append(clone)
                    continue
                out.append(
                    parse_string(
                        f"(fp_line (start {fmt(start[0])} {fmt(start[1])}) "
                        f"(end {fmt(end[0])} {fmt(end[1])}) {stroke_text} "
                        f'(layer "{layer}") (uuid "{new_uuid("line", [start, end])}"))'
                    )
                )
        for coords in polys:
            xys = " ".join(f"(xy {fmt(x)} {fmt(y)})" for x, y in coords)
            out.append(
                parse_string(
                    f"(fp_poly (pts {xys}) {stroke_text} (fill yes) "
                    f'(layer "{layer}") (uuid "{new_uuid("poly", coords)}"))'
                )
            )
        return out

    @staticmethod
    def _rewrite_line(parent: SExp, node: SExp, pieces: list[list[tuple[float, float]]]) -> None:
        """Replace ``node`` in ``parent`` by one fp_line per piece (none = delete)."""
        index = parent.children.index(node)
        if not pieces:
            parent.children.pop(index)
            return
        originals = [copy.deepcopy(node) for _ in pieces[1:]]
        targets = [node, *originals]
        for target, coords in zip(targets, pieces, strict=True):
            (sx, sy), (ex, ey) = coords[0], coords[-1]
            start, end = target.find("start"), target.find("end")
            assert start is not None and end is not None
            start.set_atom(0, round(sx, 6))
            start.set_atom(1, round(sy, 6))
            end.set_atom(0, round(ex, 6))
            end.set_atom(1, round(ey, 6))
        for offset, clone in enumerate(originals, start=1):
            uid = clone.find("uuid")
            if uid is not None:
                uid.set_atom(0, str(_uuid.uuid4()))
            parent.children.insert(index + offset, clone)

    def save(self, output_path: str | Path | None = None) -> None:
        """Write the (possibly modified) SExp tree to disk."""
        save_pcb(self.doc, Path(output_path) if output_path else self.path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _footprint_reference(fp_node: SExp) -> str:
        """Extract the reference designator from a footprint SExp node."""
        for child in fp_node.children:
            if not child.is_atom and child.name == "fp_text" and child.children:
                # (fp_text reference "U1" ...)
                atoms = child.get_atoms()
                if atoms and str(atoms[0]) == "reference" and len(atoms) >= 2:
                    return str(atoms[1])
        # KiCad 8+: (property "Reference" "U1" ...)
        for child in fp_node.children:
            if not child.is_atom and child.name == "property" and child.children:
                atoms = child.get_atoms()
                if atoms and str(atoms[0]) == "Reference" and len(atoms) >= 2:
                    return str(atoms[1])
        return ""

    @staticmethod
    def _is_silkscreen(graphic_node: SExp) -> bool:
        """Return True if *graphic_node* is on a silkscreen layer."""
        layer_node = graphic_node.find("layer")
        if layer_node is None:
            return False
        layer_name = layer_node.get_first_atom()
        return str(layer_name) in SILKSCREEN_LAYERS if layer_name is not None else False

    @staticmethod
    def _get_stroke_width(graphic_node: SExp) -> float | None:
        """Return the stroke width of a graphic node, or None if absent."""
        stroke_node = graphic_node.find("stroke")
        if stroke_node is None:
            return None
        width_node = stroke_node.find("width")
        if width_node is None:
            return None
        val = width_node.get_first_atom()
        if val is None:
            return None
        return float(val)

    def _maybe_fix(
        self,
        graphic_node: SExp,
        *,
        element_type: str,
        footprint_ref: str,
        min_width_mm: float,
        dry_run: bool,
        result: SilkscreenRepairResult,
    ) -> None:
        """Check one graphic node and fix it if below minimum."""
        if not self._is_silkscreen(graphic_node):
            return

        current_width = self._get_stroke_width(graphic_node)
        if current_width is None:
            return

        # Zero-width strokes are special KiCad "inherit from style" markers;
        # the existing checker already excludes them (stroke_width > 0).
        if current_width == 0:
            return

        if current_width >= min_width_mm:
            return

        result.fixes.append(
            SilkscreenFix(
                element_type=element_type,
                layer=str(graphic_node.find("layer").get_first_atom()),  # type: ignore[union-attr]
                old_width=current_width,
                new_width=min_width_mm,
                footprint_ref=footprint_ref,
            )
        )

        if not dry_run:
            stroke_node = graphic_node.find("stroke")
            assert stroke_node is not None
            width_node = stroke_node.find("width")
            assert width_node is not None
            width_node.set_atom(0, min_width_mm)

    @staticmethod
    def _is_hidden(text_node: SExp) -> bool:
        """Return True if a text node is hidden.

        KiCad uses several conventions:
        - ``(hide yes)`` sub-node on the text or property node
        - A bare ``hide`` atom inside the ``effects`` node
        - A ``(hide)`` sub-node inside ``effects``
        """
        # Check for (hide yes) sub-node directly on text node
        hide_node = text_node.find("hide")
        if hide_node is not None:
            atom = hide_node.get_first_atom()
            if atom is not None and str(atom) == "yes":
                return True
            # (hide) with no atoms is also considered hidden
            if atom is None:
                return True
        # Check for bare "hide" atom among direct children
        for child in text_node.children:
            if child.is_atom and str(child.value) == "hide":
                return True
        # Check inside (effects ...) for bare "hide" atom or (hide) sub-node
        effects = text_node.find("effects")
        if effects is not None:
            for child in effects.children:
                if child.is_atom and str(child.value) == "hide":
                    return True
            hide_in_effects = effects.find("hide")
            if hide_in_effects is not None:
                return True
        return False

    @staticmethod
    def _get_font_size(text_node: SExp) -> tuple[float, float] | None:
        """Return (width, height) from a text node's font size, or None."""
        effects = text_node.find("effects")
        if effects is None:
            return None
        font = effects.find("font")
        if font is None:
            return None
        size = font.find("size")
        if size is None:
            return None
        atoms = size.get_atoms()
        if len(atoms) < 2:
            return None
        return (float(atoms[0]), float(atoms[1]))

    @staticmethod
    def _get_font_thickness(text_node: SExp) -> float | None:
        """Return the font thickness from a text node, or None."""
        effects = text_node.find("effects")
        if effects is None:
            return None
        font = effects.find("font")
        if font is None:
            return None
        thickness = font.find("thickness")
        if thickness is None:
            return None
        val = thickness.get_first_atom()
        return float(val) if val is not None else None

    def _maybe_fix_text(
        self,
        text_node: SExp,
        *,
        element_type: str,
        footprint_ref: str,
        min_height_mm: float,
        dry_run: bool,
        result: TextHeightRepairResult,
    ) -> None:
        """Check one text node and fix it if font height is below minimum."""
        if not self._is_silkscreen(text_node):
            return

        if self._is_hidden(text_node):
            return

        font_size = self._get_font_size(text_node)
        if font_size is None:
            return

        font_width, font_height = font_size

        # Zero-height text is a special marker; skip it.
        if font_height == 0:
            return

        if font_height >= min_height_mm:
            return

        # Compute proportional scale factor.
        scale = min_height_mm / font_height
        new_height = min_height_mm
        new_width = font_width * scale

        # Round to avoid floating-point noise in the output.
        new_width = round(new_width, 6)
        new_height = round(new_height, 6)

        old_thickness = self._get_font_thickness(text_node)
        new_thickness: float | None = None
        if old_thickness is not None and old_thickness > 0:
            new_thickness = round(old_thickness * scale, 6)

        result.fixes.append(
            TextHeightFix(
                element_type=element_type,
                layer=str(text_node.find("layer").get_first_atom()),  # type: ignore[union-attr]
                old_height=font_height,
                new_height=new_height,
                old_width=font_width,
                new_width=new_width,
                old_thickness=old_thickness,
                new_thickness=new_thickness,
                footprint_ref=footprint_ref,
            )
        )

        if not dry_run:
            effects = text_node.find("effects")
            assert effects is not None
            font = effects.find("font")
            assert font is not None
            size_node = font.find("size")
            assert size_node is not None
            size_node.set_atom(0, new_width)
            size_node.set_atom(1, new_height)
            if new_thickness is not None:
                thickness_node = font.find("thickness")
                assert thickness_node is not None
                thickness_node.set_atom(0, new_thickness)


def _keyhole_ring(polygon) -> list[tuple[float, float]] | None:
    """One vertex ring for *polygon*, bridging any interior holes.

    ``fp_poly`` stores a single outline, so a polygon with an interior ring
    (a drill cut out of the middle of a filled shape) is written the way
    KiCad's own fractured polygons are: the exterior walks to the nearest
    hole vertex along a zero-width bridge, round the hole, and back.  The
    bridge is chosen between the closest vertex pair whose segment stays
    inside the shape.  Returns ``None`` if no such bridge exists.
    """
    from shapely.geometry import LineString, Polygon
    from shapely.geometry.polygon import orient  # type: ignore[import-untyped]

    polygon = orient(polygon, 1.0)  # exterior CCW, interiors CW
    ring = list(polygon.exterior.coords)[:-1]
    if not polygon.interiors:
        return ring
    inside = polygon.buffer(1e-7)
    for interior in polygon.interiors:
        hole = list(interior.coords)[:-1]
        pairs = sorted(
            ((math.dist(a, b), i, j) for i, a in enumerate(ring) for j, b in enumerate(hole)),
        )
        for _d, i, j in pairs:
            if inside.contains(LineString([ring[i], hole[j]])):
                ring = ring[: i + 1] + hole[j:] + hole[: j + 1] + ring[i:]
                break
        else:
            return None
    if Polygon(ring).buffer(0).symmetric_difference(polygon).area > 1e-6:
        return None
    return ring
