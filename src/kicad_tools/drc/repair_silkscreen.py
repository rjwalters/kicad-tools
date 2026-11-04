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
