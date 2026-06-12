"""Tests for the footprint ``locked`` save form (issue #3457).

KiCad 10's kicad-cli rejects boards whose footprints carry the legacy
KiCad-6 in-attr ``locked`` token (``(attr smd locked)``) with "Failed to
load board" -- which silently breaks zone fill, DRC and gerber export.
The schema save path must therefore:

1. Emit ``(locked yes)`` as a top-level footprint child and NEVER write
   the in-attr token (``Footprint._sync_attr_node``).
2. Keep PARSING both forms so KiCad 6 files still load
   (``Footprint.from_sexp``).
3. MIGRATE the legacy form to the modern form on a load -> save
   round-trip, even when no footprint field is modified
   (``PCB._link_footprint_sexp_nodes``).
"""

from __future__ import annotations

import re
from pathlib import Path

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp.parser import parse_string

# Regex for the legacy in-attr 'locked' token: any (attr ...) block whose
# atom list contains the bare token 'locked'.
LEGACY_IN_ATTR_LOCKED = re.compile(r"\(attr\s[^()]*\blocked\b")


def _pcb_text(footprint_extra: str) -> str:
    """Minimal loadable PCB with one footprint carrying ``footprint_extra``."""
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (gr_line (start 0 0) (end 10 0) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 10 0) (end 10 10) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 10 10) (end 0 10) (layer "Edge.Cuts") (width 0.05))
  (gr_line (start 0 10) (end 0 0) (layer "Edge.Cuts") (width 0.05))
  (net 0 "")
  (footprint "Test:R_0805"
    (layer "F.Cu")
    (uuid "fp-r1")
    (at 5 5)
{footprint_extra}
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    (pad "1" smd rect (at -1 0) (size 1 1) (layers "F.Cu") (net 0 ""))
    (pad "2" smd rect (at 1 0) (size 1 1) (layers "F.Cu") (net 0 ""))
  )
)
"""


def _footprint_node(saved_text: str):
    """Return the (footprint ...) SExp node from saved board text."""
    doc = parse_string(saved_text)
    fp_node = doc.find("footprint")
    assert fp_node is not None, "saved board lost its footprint"
    return fp_node


def _assert_modern_locked_form(saved_text: str) -> None:
    """Assert top-level ``(locked yes)`` present and in-attr token absent."""
    assert not LEGACY_IN_ATTR_LOCKED.search(saved_text), (
        "Saved board carries the legacy in-attr 'locked' token "
        "(e.g. '(attr smd locked)'). KiCad 10's kicad-cli rejects this "
        "form with 'Failed to load board' (issue #3457). The schema save "
        "path must emit a top-level (locked yes) instead -- see "
        "Footprint._sync_attr_node in src/kicad_tools/schema/pcb.py."
    )
    fp_node = _footprint_node(saved_text)
    locked_children = fp_node.find_children("locked")
    assert locked_children, (
        "Saved board is missing the top-level (locked yes) child on the "
        "locked footprint -- the lock state was silently dropped."
    )
    assert (locked_children[0].get_string(0) or "") == "yes"


class TestLockedSaveForm:
    """Setting ``fp.locked`` through the schema layer emits the modern form."""

    def test_lock_emits_top_level_locked_yes(self, tmp_path: Path) -> None:
        path = tmp_path / "board.kicad_pcb"
        path.write_text(_pcb_text("    (attr smd)"))

        board = PCB.load(path)
        fp = board.get_footprint("R1")
        assert fp.locked is False
        fp.locked = True
        board.save(path)

        saved = path.read_text()
        _assert_modern_locked_form(saved)

        # The footprint type token must survive the attr rebuild.
        assert "(attr smd)" in saved

        # Reload: the lock must still be visible through the schema layer.
        assert PCB.load(path).get_footprint("R1").locked is True

    def test_unlock_removes_top_level_locked(self, tmp_path: Path) -> None:
        path = tmp_path / "board.kicad_pcb"
        path.write_text(_pcb_text("    (attr smd)\n    (locked yes)"))

        board = PCB.load(path)
        fp = board.get_footprint("R1")
        assert fp.locked is True
        fp.locked = False
        board.save(path)

        saved = path.read_text()
        fp_node = _footprint_node(saved)
        assert not fp_node.find_children("locked"), (
            "Unlocking a footprint must remove the top-level (locked yes) node"
        )
        assert not LEGACY_IN_ATTR_LOCKED.search(saved)
        assert PCB.load(path).get_footprint("R1").locked is False

    def test_pad_locked_nodes_untouched(self, tmp_path: Path) -> None:
        """Footprint-level lock sync must not strip pad-level (locked yes)."""
        path = tmp_path / "board.kicad_pcb"
        path.write_text(
            _pcb_text("    (attr smd)").replace(
                '(pad "1" smd rect (at -1 0)',
                '(pad "1" smd rect (locked yes) (at -1 0)',
            )
        )

        board = PCB.load(path)
        fp = board.get_footprint("R1")
        fp.locked = True
        board.save(path)

        saved = path.read_text()
        _assert_modern_locked_form(saved)
        fp_node = _footprint_node(saved)
        pad_locked = [pad for pad in fp_node.find_children("pad") if pad.find_children("locked")]
        assert pad_locked, "Footprint-level lock sync stripped the pad-level (locked yes) node"


class TestLegacyFormParsing:
    """Both lock forms must still parse (KiCad 6 files load)."""

    def test_parses_legacy_in_attr_form(self, tmp_path: Path) -> None:
        path = tmp_path / "board.kicad_pcb"
        path.write_text(_pcb_text("    (attr smd locked)"))
        assert PCB.load(path).get_footprint("R1").locked is True

    def test_parses_modern_top_level_form(self, tmp_path: Path) -> None:
        path = tmp_path / "board.kicad_pcb"
        path.write_text(_pcb_text("    (attr smd)\n    (locked yes)"))
        assert PCB.load(path).get_footprint("R1").locked is True


class TestLegacyFormMigration:
    """Loading a legacy-form file and saving must migrate to the modern form."""

    def test_load_save_migrates_without_field_changes(self, tmp_path: Path) -> None:
        """Pure load -> save (no modification) must not echo the legacy token."""
        path = tmp_path / "board.kicad_pcb"
        path.write_text(_pcb_text("    (attr smd locked)"))

        board = PCB.load(path)
        board.save(path)

        saved = path.read_text()
        _assert_modern_locked_form(saved)
        assert "(attr smd)" in saved
        assert PCB.load(path).get_footprint("R1").locked is True

    def test_migration_preserves_other_attr_tokens(self, tmp_path: Path) -> None:
        """Modeled + unknown attr tokens survive the legacy-lock migration."""
        path = tmp_path / "board.kicad_pcb"
        path.write_text(_pcb_text("    (attr smd locked exclude_from_bom allow_missing_courtyard)"))

        board = PCB.load(path)
        fp = board.get_footprint("R1")
        assert fp.locked is True
        assert fp.exclude_from_bom is True
        board.save(path)

        saved = path.read_text()
        _assert_modern_locked_form(saved)
        attr_node = _footprint_node(saved).find_children("attr")[0]
        tokens = [c.value for c in attr_node.children if c.is_atom]
        assert "smd" in tokens
        assert "exclude_from_bom" in tokens
        assert "allow_missing_courtyard" in tokens
        assert "locked" not in tokens

    def test_modern_form_round_trips_unchanged(self, tmp_path: Path) -> None:
        """A modern-form file keeps its lock through load -> save."""
        path = tmp_path / "board.kicad_pcb"
        path.write_text(_pcb_text("    (attr smd)\n    (locked yes)"))

        board = PCB.load(path)
        board.save(path)

        _assert_modern_locked_form(path.read_text())
