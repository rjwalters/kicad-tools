"""Silkscreen generator: ensure ref des visibility and add board markings.

Operates on the raw SExp tree so that modifications can be written back to
disk, following the same pattern as ``repair_silkscreen.py``.

First-pass scope:
- Ensure every footprint reference designator on F.SilkS/B.SilkS is visible
  (unhide hidden references).
- Add board-level ``gr_text`` elements for project name, revision, and date
  sourced from the project spec metadata.

Marking idempotency: ``add_board_markings`` is robust to project rename and
revision bump. Marking identity is persisted in a sibling ``*.kct.json``
sidecar file keyed by the ``gr_text`` UUID. The sidecar is read on
construction and rewritten on ``save()``. PCB content remains strictly
KiCad-conformant (no ``kct_*`` custom S-expression children).

Deferred to follow-up issues:
- Polarity marks (IC pin 1, diode cathode)
- Intelligent auto-placement with pad/copper collision avoidance
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import tempfile
import uuid as uuid_module
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from kicad_tools.core.sexp_file import save_pcb
from kicad_tools.sexp.builders import gr_text_node
from kicad_tools.sexp.parser import SExp, parse_file

logger = logging.getLogger(__name__)

# Silkscreen layer names recognised by KiCad (pre-8.0 and 8.0+).
SILKSCREEN_LAYERS = frozenset(("F.SilkS", "B.SilkS", "F.Silkscreen", "B.Silkscreen"))

# Sidecar schema version.
_SIDECAR_VERSION = 1


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
        self.path = Path(pcb_path)
        self.doc: SExp = parse_file(pcb_path)
        # Sidecar registry: list of {"uuid": str, "tag": str, "added_at": iso8601}.
        self._registry: list[dict[str, Any]] = self._load_sidecar(self.sidecar_path)

    # ------------------------------------------------------------------
    # Sidecar properties
    # ------------------------------------------------------------------

    @property
    def sidecar_path(self) -> Path:
        """Return the sibling ``<pcb>.kct.json`` path used to track markings."""
        return self.path.with_name(self.path.name + ".kct.json")

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

        Idempotency is tracked in a ``*.kct.json`` sidecar keyed by
        ``gr_text`` UUID. Re-running with a different name or revision
        replaces the existing marking (the prior ``gr_text`` is removed
        and a fresh one written), so renames and revision bumps do not
        accumulate stale text on the board.

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

        # Build the desired markings list as (tag, visible_text) pairs.
        markings: list[tuple[str, str]] = []
        if name:
            label = f"{name} Rev {revision}" if revision else name
            markings.append(("kct:name", label))
        if date:
            markings.append(("kct:date", date))

        if not markings:
            return result

        # Determine starting position.
        base_x, base_y = self._get_marking_position()

        # Compute y offset based on existing markings already at the base
        # position (so a fresh marking added alongside a pre-existing one
        # stacks below it).
        y_offset = 0.0

        for tag, text in markings:
            existing_uuid = self._lookup_registry_uuid(tag)
            existing_node = (
                self._find_gr_text_by_uuid(existing_uuid)
                if existing_uuid is not None
                else None
            )

            if existing_node is not None:
                existing_text = existing_node.get_first_atom()
                if existing_text is not None and str(existing_text) == text:
                    # Same content already on the board: skip.
                    result.markings_skipped += 1
                    result.messages.append(
                        f"Marking '{tag}' already exists, skipped"
                    )
                    continue
                # Content differs (rename or revision bump): drop the old
                # gr_text and registry row, then re-add below.
                self.doc.remove(existing_node)
                self._drop_registry_entry(tag)
                result.messages.append(
                    f"Replaced stale marking '{tag}'"
                )
            elif existing_uuid is not None:
                # Registry knows about this tag but the gr_text is gone
                # (user deleted it). Drop the stale row and re-add.
                self._drop_registry_entry(tag)

            new_uuid = str(uuid_module.uuid4())
            node = gr_text_node(
                text,
                base_x,
                base_y + y_offset,
                layer=layer,
                font_size=font_size,
                font_thickness=font_thickness,
                uuid_str=new_uuid,
            )

            self.doc.append(node)
            self._registry.append(
                {
                    "uuid": new_uuid,
                    "tag": tag,
                    "added_at": datetime.now(timezone.utc).isoformat(),
                }
            )
            result.markings_added += 1
            result.messages.append(f"Added marking '{tag}': {text}")
            y_offset += font_size + 0.5  # spacing between lines

        return result

    def save(self, output_path: Path | None = None) -> None:
        """Write the (possibly modified) SExp tree to disk.

        Also writes the ``*.kct.json`` sidecar next to the PCB so that
        marking identity (UUIDs + tags) survives reloads.
        """
        target = Path(output_path) if output_path is not None else self.path
        save_pcb(self.doc, target)

        sidecar = target.with_name(target.name + ".kct.json")
        self._write_sidecar(sidecar, self._registry)

    # ------------------------------------------------------------------
    # Sidecar I/O
    # ------------------------------------------------------------------

    @staticmethod
    def _load_sidecar(sidecar_path: Path) -> list[dict[str, Any]]:
        """Load the sidecar registry. Missing/corrupt files yield an empty list."""
        if not sidecar_path.exists():
            return []
        try:
            raw = sidecar_path.read_text(encoding="utf-8")
            data = json.loads(raw)
        except (OSError, json.JSONDecodeError) as exc:
            logger.warning(
                "Could not read marking sidecar %s (%s); proceeding with empty registry.",
                sidecar_path,
                exc,
            )
            return []

        if not isinstance(data, dict):
            logger.warning(
                "Sidecar %s has unexpected shape; ignoring.", sidecar_path
            )
            return []

        markings = data.get("markings")
        if not isinstance(markings, list):
            return []

        clean: list[dict[str, Any]] = []
        for entry in markings:
            if not isinstance(entry, dict):
                continue
            uuid_val = entry.get("uuid")
            tag_val = entry.get("tag")
            if not isinstance(uuid_val, str) or not isinstance(tag_val, str):
                continue
            row: dict[str, Any] = {"uuid": uuid_val, "tag": tag_val}
            added_at = entry.get("added_at")
            if isinstance(added_at, str):
                row["added_at"] = added_at
            clean.append(row)
        return clean

    @staticmethod
    def _write_sidecar(
        sidecar_path: Path, registry: list[dict[str, Any]]
    ) -> None:
        """Atomically write the sidecar registry to disk.

        If the registry is empty and no sidecar exists, nothing is
        written (avoids littering boards that have no markings). If a
        sidecar already exists it is rewritten so that deleted entries
        propagate.
        """
        if not registry and not sidecar_path.exists():
            return

        payload = {
            "version": _SIDECAR_VERSION,
            "markings": registry,
        }
        serialised = json.dumps(payload, indent=2, sort_keys=False) + "\n"

        sidecar_path.parent.mkdir(parents=True, exist_ok=True)
        # Atomic write via tempfile + os.replace so a crash mid-write
        # cannot leave a half-written sidecar next to the PCB.
        fd, tmp_name = tempfile.mkstemp(
            prefix=sidecar_path.name + ".",
            suffix=".tmp",
            dir=str(sidecar_path.parent),
        )
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as fh:
                fh.write(serialised)
            os.replace(tmp_name, sidecar_path)
        except Exception:
            # Best-effort cleanup of the temp file on failure.
            with contextlib.suppress(OSError):
                os.unlink(tmp_name)
            raise

    # ------------------------------------------------------------------
    # Registry helpers
    # ------------------------------------------------------------------

    def _lookup_registry_uuid(self, tag: str) -> str | None:
        """Return the UUID recorded for *tag*, or None if absent."""
        for entry in self._registry:
            if entry.get("tag") == tag:
                uuid_val = entry.get("uuid")
                if isinstance(uuid_val, str):
                    return uuid_val
        return None

    def _drop_registry_entry(self, tag: str) -> None:
        """Remove all registry rows with the given tag (in place)."""
        self._registry = [e for e in self._registry if e.get("tag") != tag]

    def _find_gr_text_by_uuid(self, target_uuid: str) -> SExp | None:
        """Return the gr_text node whose ``(uuid ...)`` matches, or None."""
        if not target_uuid:
            return None
        for gr_text in self.doc.find_all("gr_text"):
            uuid_node = gr_text.find("uuid")
            if uuid_node is None:
                continue
            atom = uuid_node.get_first_atom()
            if atom is not None and str(atom) == target_uuid:
                return gr_text
        return None

    # ------------------------------------------------------------------
    # Other internals
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
