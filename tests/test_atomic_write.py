"""Tests for the shared atomic-write primitive (issue #4898).

Issue #4898 (Part of #4880's Konnect-ideas audit, item 3) hardened the
low-level ``.kicad_pcb``/``.kicad_sch``/``.kicad_pro``/``.kicad_mod``/
``.kicad_dru`` writers -- previously a plain, non-atomic ``write_text`` --
to go through a shared ``atomic_write_text`` helper
(``kicad_tools.core.atomic_write``) that writes to a sibling ``.tmp`` file,
``fsync``s it, then ``os.replace``s it onto the destination. This mirrors
the pattern ``route_cmd.py``'s ``_write_routed_pcb`` already used for the
routed-PCB output path (issue #2808).

Covers:

1. ``atomic_write_text`` itself -- normal write, atomic-failure semantics
   (a failure during ``os.replace`` must leave the pre-existing destination
   file unchanged, never torn/truncated), and the ``.tmp`` sibling naming.
2. Each of the five hardened writers (``save_pcb``, ``save_schematic``,
   ``save_project``, ``save_footprint``, ``save_design_rules``) actually
   goes through the shared helper (verified by patching ``os.replace`` and
   observing the same atomic-failure behavior end-to-end).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.core.atomic_write import atomic_write_text
from kicad_tools.core.project_file import save_project
from kicad_tools.core.sexp_file import (
    save_design_rules,
    save_footprint,
    save_pcb,
    save_schematic,
)
from kicad_tools.sexp import parse_string

# ---------------------------------------------------------------------------
# atomic_write_text unit tests
# ---------------------------------------------------------------------------


class TestAtomicWriteText:
    def test_writes_content_to_destination(self, tmp_path: Path):
        dest = tmp_path / "out.txt"
        atomic_write_text(dest, "hello world")
        assert dest.read_text() == "hello world"

    def test_no_tmp_sibling_left_behind_on_success(self, tmp_path: Path):
        dest = tmp_path / "out.txt"
        atomic_write_text(dest, "hello world")
        assert not dest.with_suffix(dest.suffix + ".tmp").exists()

    def test_replace_failure_leaves_existing_file_unchanged(self, tmp_path: Path):
        """A failure during ``os.replace`` must not clobber pre-existing
        content at the destination -- the write is atomic: either the new
        content lands, or the old content survives untouched. Never torn."""
        dest = tmp_path / "out.txt"
        SENTINEL = "sentinel-content-must-not-be-clobbered"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                atomic_write_text(dest, "new content")

        assert dest.read_text() == SENTINEL

    def test_tmp_file_uses_dot_tmp_suffix(self, tmp_path: Path):
        dest = tmp_path / "out.txt"
        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                atomic_write_text(dest, "new content")

        expected_tmp = dest.with_suffix(dest.suffix + ".tmp")
        assert expected_tmp.exists()
        assert expected_tmp.read_text() == "new content"

    def test_encoding_none_falls_back_to_write_text_default(self, tmp_path: Path):
        """``encoding=None`` preserves the historical unparameterized
        ``Path.write_text(content)`` call shape some callers relied on."""
        dest = tmp_path / "out.txt"
        atomic_write_text(dest, "plain", encoding=None)
        assert dest.read_text() == "plain"


# ---------------------------------------------------------------------------
# The five hardened writers route through atomic_write_text
# ---------------------------------------------------------------------------

_MINIMAL_PCB = '(kicad_pcb\n  (version 20240108)\n  (generator "test")\n)\n'
_MINIMAL_SCH = '(kicad_sch\n  (version 20240108)\n  (generator "test")\n)\n'
_MINIMAL_FOOTPRINT = '(footprint "test:fp"\n  (version 20240108)\n)\n'
_MINIMAL_DRU = '(version 1)\n(rule "test" (constraint clearance (min 0.2mm)))\n'


class TestHardenedWritersAreAtomic:
    """Each writer must leave a pre-existing destination file unchanged if
    the underlying ``os.replace`` fails -- proof that it goes through the
    shared atomic helper rather than a plain ``write_text``."""

    def test_save_pcb_is_atomic(self, tmp_path: Path):
        sexp = parse_string(_MINIMAL_PCB)
        dest = tmp_path / "board.kicad_pcb"
        SENTINEL = "(kicad_pcb (sentinel))"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_pcb(sexp, dest)

        assert dest.read_text() == SENTINEL

    def test_save_pcb_normal_write_roundtrips(self, tmp_path: Path):
        sexp = parse_string(_MINIMAL_PCB)
        dest = tmp_path / "board.kicad_pcb"
        save_pcb(sexp, dest)
        assert dest.exists()
        assert "kicad_pcb" in dest.read_text()
        assert not dest.with_suffix(dest.suffix + ".tmp").exists()

    def test_save_schematic_is_atomic(self, tmp_path: Path):
        sexp = parse_string(_MINIMAL_SCH)
        dest = tmp_path / "board.kicad_sch"
        SENTINEL = "(kicad_sch (sentinel))"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_schematic(sexp, dest)

        assert dest.read_text() == SENTINEL

    def test_save_footprint_is_atomic(self, tmp_path: Path):
        sexp = parse_string(_MINIMAL_FOOTPRINT)
        dest = tmp_path / "fp.kicad_mod"
        SENTINEL = "(footprint (sentinel))"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_footprint(sexp, dest)

        assert dest.read_text() == SENTINEL

    def test_save_design_rules_is_atomic(self, tmp_path: Path):
        from kicad_tools.core.sexp_file import load_design_rules

        # Write once normally so we have a real design_rules SExp to re-save.
        tmp_seed = tmp_path / "seed.kicad_dru"
        tmp_seed.write_text(_MINIMAL_DRU)
        sexp = load_design_rules(tmp_seed)

        dest = tmp_path / "rules.kicad_dru"
        SENTINEL = "(version 999)"
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_design_rules(sexp, dest)

        assert dest.read_text() == SENTINEL

    def test_save_project_is_atomic(self, tmp_path: Path):
        dest = tmp_path / "proj.kicad_pro"
        SENTINEL = '{"sentinel": true}'
        dest.write_text(SENTINEL)

        with patch("kicad_tools.core.atomic_write.os.replace", side_effect=OSError("simulated")):
            with pytest.raises(OSError):
                save_project({"meta": {"version": 1}}, dest)

        assert dest.read_text() == SENTINEL

    def test_save_project_normal_write_roundtrips(self, tmp_path: Path):
        dest = tmp_path / "proj.kicad_pro"
        data = {"meta": {"version": 1, "filename": "proj.kicad_pro"}}
        save_project(data, dest)
        assert json.loads(dest.read_text()) == data
        assert not dest.with_suffix(dest.suffix + ".tmp").exists()
