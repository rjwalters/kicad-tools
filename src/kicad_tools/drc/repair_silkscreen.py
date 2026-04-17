"""Silkscreen repair: line widths and text heights.

Walks the raw SExp tree to find silkscreen graphic elements (fp_line, fp_rect,
fp_circle, fp_arc, gr_line, gr_rect, gr_circle, gr_arc) whose stroke width is
below the manufacturer minimum, and sets the width to the minimum.

Also walks text elements (fp_text, property, gr_text) on silkscreen layers and
scales undersized font heights (and proportionally widths/thickness) up to the
manufacturer minimum.

This operates on the raw SExp tree (not the read-only schema dataclasses) so
that modifications can be written back to disk, following the same pattern as
``repair_clearance.py`` and ``fix_vias_cmd.py``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.core.sexp_file import save_pcb
from kicad_tools.sexp.parser import SExp, parse_file

# Silkscreen layer names recognised by KiCad (pre-8.0 and 8.0+).
SILKSCREEN_LAYERS = frozenset(("F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen"))

# Graphic element types that live inside footprints.
FP_GRAPHIC_TYPES = ("fp_line", "fp_rect", "fp_circle", "fp_arc")

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
        for fp_node in self.doc.find_all("footprint"):
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
        for fp_node in self.doc.find_all("footprint"):
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
