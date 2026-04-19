"""Silkscreen generator: ensure ref des visibility and add board markings.

Operates on the raw SExp tree so that modifications can be written back to
disk, following the same pattern as ``repair_silkscreen.py``.

First-pass scope:
- Ensure every footprint reference designator on F.SilkS/B.SilkS is visible
  (unhide hidden references).
- Add board-level ``gr_text`` elements for project name, revision, and date
  sourced from the project spec metadata.

Deferred to follow-up issues:
- Polarity marks (IC pin 1, diode cathode)
- Intelligent auto-placement with pad/copper collision avoidance
"""

from __future__ import annotations

import uuid as uuid_module
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.core.sexp_file import save_pcb
from kicad_tools.sexp.builders import gr_text_node
from kicad_tools.sexp.parser import SExp, parse_file

# Silkscreen layer names recognised by KiCad (pre-8.0 and 8.0+).
SILKSCREEN_LAYERS = frozenset(("F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen"))

# Board-level gr_text marker attribute used for idempotency detection.
_MARKING_PREFIX = "kct:"


@dataclass
class SilkscreenResult:
    """Result of a silkscreen generation pass."""

    refs_unhidden: int = 0
    markings_added: int = 0
    markings_skipped: int = 0
    messages: list[str] = field(default_factory=list)

    @property
    def total_changes(self) -> int:
        return self.refs_unhidden + self.markings_added


class SilkscreenGenerator:
    """Generate silkscreen content for a KiCad PCB.

    Usage::

        gen = SilkscreenGenerator(Path("board.kicad_pcb"))
        result = gen.ensure_ref_des_visible()
        result += gen.add_board_markings(name="MyBoard", revision="A")
        gen.save()
    """

    def __init__(self, pcb_path: Path):
        self.path = pcb_path
        self.doc: SExp = parse_file(pcb_path)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_ref_des_visible(self) -> SilkscreenResult:
        """Unhide footprint reference designators on silkscreen layers.

        Walks every footprint and checks its reference text node (either
        ``fp_text reference`` for KiCad 7 or ``property "Reference"`` for
        KiCad 8+). If the text is on a silkscreen layer and hidden, it is
        unhidden.

        Returns:
            SilkscreenResult with the count of references unhidden.
        """
        result = SilkscreenResult()

        for fp_node in self.doc.find_all("footprint"):
            ref_text = self._find_ref_text(fp_node)
            if ref_text is None:
                continue

            if not self._is_silkscreen(ref_text):
                continue

            if self._is_hidden(ref_text):
                self._unhide(ref_text)
                ref_name = self._extract_ref_name(fp_node)
                result.refs_unhidden += 1
                result.messages.append(f"Unhid reference {ref_name}")

        return result

    def add_board_markings(
        self,
        name: str | None = None,
        revision: str | None = None,
        date: str | None = None,
        layer: str = "F.SilkS",
        font_size: float = 1.0,
        font_thickness: float = 0.15,
    ) -> SilkscreenResult:
        """Add board-level text markings (project name, revision, date).

        Places ``gr_text`` elements near the bottom-left of the board
        outline (or at a default position if no outline exists).

        This method is idempotent: it will not add duplicate markings if
        they already exist.

        Args:
            name: Project name (e.g. "LED Driver")
            revision: Revision string (e.g. "A", "1.0")
            date: Date string (e.g. "2026-04-19")
            layer: Silkscreen layer for markings
            font_size: Font size in mm
            font_thickness: Font stroke thickness in mm

        Returns:
            SilkscreenResult with counts of markings added/skipped.
        """
        result = SilkscreenResult()

        # Find board extents from Edge.Cuts outline for positioning
        base_x, base_y = self._get_marking_position()

        markings: list[tuple[str, str]] = []
        if name:
            label = f"{name} Rev {revision}" if revision else name
            markings.append(("name", label))
        if date:
            markings.append(("date", date))

        y_offset = 0.0
        for tag, text in markings:
            marker = f"{_MARKING_PREFIX}{tag}"
            if self._has_marking(marker):
                result.markings_skipped += 1
                result.messages.append(f"Marking '{tag}' already exists, skipped")
                continue

            # Embed the marker tag as a second line so we can detect duplicates
            # later; KiCad renders multi-line gr_text fine.
            node = gr_text_node(
                text,
                base_x,
                base_y + y_offset,
                layer=layer,
                font_size=font_size,
                font_thickness=font_thickness,
                uuid_str=str(uuid_module.uuid4()),
            )

            # Store the idempotency marker as a custom child node so that
            # re-running the generator can detect existing markings without
            # relying on fragile text matching.
            node.append(SExp.list("kct_marking", marker))

            self.doc.append(node)
            result.markings_added += 1
            result.messages.append(f"Added marking '{tag}': {text}")
            y_offset += font_size + 0.5  # spacing between lines

        return result

    def save(self, output_path: Path | None = None) -> None:
        """Write the (possibly modified) SExp tree to disk."""
        save_pcb(self.doc, output_path or self.path)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    @staticmethod
    def _find_ref_text(fp_node: SExp) -> SExp | None:
        """Find the reference text node in a footprint.

        Handles both KiCad 7 (fp_text reference) and KiCad 8+ (property
        "Reference") formats.
        """
        # KiCad 7: (fp_text reference "R1" ...)
        for child in fp_node.children:
            if not child.is_atom and child.name == "fp_text":
                atoms = child.get_atoms()
                if atoms and str(atoms[0]) == "reference":
                    return child
        # KiCad 8+: (property "Reference" "R1" ...)
        for child in fp_node.children:
            if not child.is_atom and child.name == "property":
                atoms = child.get_atoms()
                if atoms and str(atoms[0]) == "Reference":
                    return child
        return None

    @staticmethod
    def _extract_ref_name(fp_node: SExp) -> str:
        """Extract the reference designator string from a footprint."""
        for child in fp_node.children:
            if not child.is_atom and child.name == "fp_text":
                atoms = child.get_atoms()
                if atoms and str(atoms[0]) == "reference" and len(atoms) >= 2:
                    return str(atoms[1])
        for child in fp_node.children:
            if not child.is_atom and child.name == "property":
                atoms = child.get_atoms()
                if atoms and str(atoms[0]) == "Reference" and len(atoms) >= 2:
                    return str(atoms[1])
        return "?"

    @staticmethod
    def _is_silkscreen(text_node: SExp) -> bool:
        """Return True if *text_node* is on a silkscreen layer."""
        layer_node = text_node.find("layer")
        if layer_node is None:
            return False
        layer_name = layer_node.get_first_atom()
        return str(layer_name) in SILKSCREEN_LAYERS if layer_name is not None else False

    @staticmethod
    def _is_hidden(text_node: SExp) -> bool:
        """Return True if a text node is hidden.

        KiCad uses several conventions:
        - ``(hide yes)`` sub-node on the text or property node
        - A bare ``hide`` atom inside the ``effects`` node
        - A ``(hide)`` sub-node inside ``effects``
        """
        # Check for (hide yes) directly on text node
        hide_node = text_node.find("hide")
        if hide_node is not None:
            atom = hide_node.get_first_atom()
            if atom is not None and str(atom) == "yes":
                return True
            if atom is None:
                return True
        # Bare "hide" atom among direct children
        for child in text_node.children:
            if child.is_atom and str(child.value) == "hide":
                return True
        # Inside (effects ...) node
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
    def _unhide(text_node: SExp) -> None:
        """Remove all hide directives from a text node."""
        # Remove (hide yes) or (hide) from the text node
        hide_node = text_node.find("hide")
        if hide_node is not None:
            text_node.remove(hide_node)
        # Remove bare "hide" atoms from direct children
        text_node.children = [
            c for c in text_node.children
            if not (c.is_atom and str(c.value) == "hide")
        ]
        # Remove from effects node
        effects = text_node.find("effects")
        if effects is not None:
            effects.children = [
                c for c in effects.children
                if not (c.is_atom and str(c.value) == "hide")
            ]
            hide_in_effects = effects.find("hide")
            if hide_in_effects is not None:
                effects.remove(hide_in_effects)

    def _get_marking_position(self) -> tuple[float, float]:
        """Determine a suitable position for board markings.

        Tries to place markings just below the board outline on Edge.Cuts.
        Falls back to a default position if no outline exists.

        Returns:
            (x, y) position in mm for the first marking line.
        """
        min_x = None
        max_y = None

        for tag in ("gr_line", "gr_rect", "gr_arc"):
            for node in self.doc.find_all(tag):
                layer = node.find("layer")
                if not layer:
                    continue
                layer_name = layer.get_first_atom()
                if layer_name is None or str(layer_name) != "Edge.Cuts":
                    continue

                # Extract coordinates from start/end nodes
                for pos_tag in ("start", "end"):
                    pos = node.find(pos_tag)
                    if pos is None:
                        continue
                    atoms = pos.get_atoms()
                    if len(atoms) >= 2:
                        x_val = float(atoms[0])
                        y_val = float(atoms[1])
                        if min_x is None or x_val < min_x:
                            min_x = x_val
                        if max_y is None or y_val > max_y:
                            max_y = y_val

        if min_x is not None and max_y is not None:
            # Place markings 1.5mm below the bottom-left of the board
            return min_x, max_y + 1.5

        # Fallback: default KiCad origin area
        return 100.0, 115.0

    def _has_marking(self, marker: str) -> bool:
        """Check if a marking with the given idempotency tag already exists."""
        for gr_text in self.doc.find_all("gr_text"):
            kct_node = gr_text.find("kct_marking")
            if kct_node is not None:
                atom = kct_node.get_first_atom()
                if atom is not None and str(atom) == marker:
                    return True
        return False
