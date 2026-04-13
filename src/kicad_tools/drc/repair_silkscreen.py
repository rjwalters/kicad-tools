"""Silkscreen line width repair.

Walks the raw SExp tree to find silkscreen graphic elements (fp_line, fp_rect,
fp_circle, fp_arc, gr_line, gr_rect, gr_circle, gr_arc) whose stroke width is
below the manufacturer minimum, and sets the width to the minimum.

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
