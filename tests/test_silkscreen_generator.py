"""Tests for silkscreen generation (ref des visibility + board markings)."""

from __future__ import annotations

import json
import textwrap
from pathlib import Path

import pytest

from kicad_tools.sexp.builders import gr_text_node
from kicad_tools.sexp.parser import SExp, parse_string


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _minimal_pcb(extra: str = "") -> str:
    """Return a minimal KiCad PCB string with optional extra content."""
    return textwrap.dedent(f"""\
        (kicad_pcb (version 20240108) (generator "test")
          (general (thickness 1.6) (legacy_teardrops no))
          (layers (0 "F.Cu" signal))
          {extra}
        )
    """)


def _write_pcb(tmp_path: Path, content: str) -> Path:
    pcb_path = tmp_path / "test.kicad_pcb"
    pcb_path.write_text(content)
    return pcb_path


# ---------------------------------------------------------------------------
# gr_text_node builder tests
# ---------------------------------------------------------------------------


class TestGrTextNode:
    def test_basic_structure(self):
        node = gr_text_node("Hello", 10.0, 20.0, uuid_str="test-uuid")
        s = node.to_string()
        assert "(gr_text" in s
        assert '"Hello"' in s or "Hello" in s
        assert "(at 10 20)" in s
        assert '(layer "F.SilkS")' in s
        assert '(uuid "test-uuid")' in s
        assert "(effects" in s
        assert "(font" in s
        assert "(thickness" in s

    def test_custom_layer(self):
        node = gr_text_node("Back", 5.0, 5.0, layer="B.SilkS", uuid_str="u")
        s = node.to_string()
        assert '(layer "B.SilkS")' in s

    def test_custom_font_size(self):
        node = gr_text_node("Big", 0.0, 0.0, font_size=2.0, uuid_str="u")
        s = node.to_string()
        assert "(size 2 2)" in s

    def test_roundtrip(self):
        """gr_text_node output should be re-parseable."""
        node = gr_text_node("Test", 100.0, 200.0, uuid_str="rt-uuid")
        reparsed = parse_string(node.to_string())
        assert reparsed.name == "gr_text"


# ---------------------------------------------------------------------------
# SilkscreenGenerator tests
# ---------------------------------------------------------------------------


class TestRefDesVisibility:
    """Test ensure_ref_des_visible()."""

    def test_unhides_hidden_fp_text_reference(self, tmp_path):
        """A hidden fp_text reference on F.SilkS should be unhidden."""
        pcb = _minimal_pcb("""
          (footprint "Package_SO:SOIC-8" (at 100 100) (layer "F.Cu")
            (fp_text reference "U1" (at 0 -2) (layer "F.SilkS")
              (hide yes)
              (effects (font (size 1 1) (thickness 0.15)))
            )
          )
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result = gen.ensure_ref_des_visible()

        assert result.refs_unhidden == 1
        assert "U1" in result.messages[0]

        # Verify the hide node was removed
        fp = gen.doc.find_all("footprint")[0]
        ref_text = None
        for child in fp.children:
            if not child.is_atom and child.name == "fp_text":
                atoms = child.get_atoms()
                if atoms and str(atoms[0]) == "reference":
                    ref_text = child
                    break
        assert ref_text is not None
        assert ref_text.find("hide") is None

    def test_already_visible_noop(self, tmp_path):
        """A visible fp_text reference should not be modified."""
        pcb = _minimal_pcb("""
          (footprint "R:0805" (at 110 100) (layer "F.Cu")
            (fp_text reference "R1" (at 0 -2) (layer "F.SilkS")
              (effects (font (size 1 1) (thickness 0.15)))
            )
          )
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result = gen.ensure_ref_des_visible()

        assert result.refs_unhidden == 0

    def test_hidden_via_bare_hide_atom(self, tmp_path):
        """A ref hidden via bare 'hide' atom should be unhidden."""
        pcb = _minimal_pcb("""
          (footprint "C:0402" (at 120 100) (layer "F.Cu")
            (fp_text reference "C1" (at 0 -2) (layer "F.SilkS") hide
              (effects (font (size 1 1) (thickness 0.15)))
            )
          )
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result = gen.ensure_ref_des_visible()

        assert result.refs_unhidden == 1

    def test_hidden_via_effects_hide(self, tmp_path):
        """A ref hidden via hide inside effects should be unhidden."""
        pcb = _minimal_pcb("""
          (footprint "C:0402" (at 120 100) (layer "F.Cu")
            (fp_text reference "C2" (at 0 -2) (layer "F.SilkS")
              (effects (font (size 1 1) (thickness 0.15)) hide)
            )
          )
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result = gen.ensure_ref_des_visible()

        assert result.refs_unhidden == 1

    def test_kicad8_property_reference(self, tmp_path):
        """KiCad 8+ property Reference format should also be handled."""
        pcb = _minimal_pcb("""
          (footprint "R:0805" (at 110 100) (layer "F.Cu")
            (property "Reference" "R2" (at 0 -2) (layer "F.SilkS")
              (hide yes)
              (effects (font (size 1 1) (thickness 0.15)))
            )
          )
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result = gen.ensure_ref_des_visible()

        assert result.refs_unhidden == 1

    def test_non_silkscreen_layer_ignored(self, tmp_path):
        """Hidden ref on non-silkscreen layer should NOT be unhidden."""
        pcb = _minimal_pcb("""
          (footprint "R:0805" (at 110 100) (layer "F.Cu")
            (fp_text reference "R3" (at 0 -2) (layer "F.Fab")
              (hide yes)
              (effects (font (size 1 1) (thickness 0.15)))
            )
          )
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result = gen.ensure_ref_des_visible()

        assert result.refs_unhidden == 0


class TestBoardMarkings:
    """Test add_board_markings()."""

    def test_adds_name_and_date(self, tmp_path):
        """Board markings should be added with name and date."""
        pcb = _minimal_pcb("""
          (gr_line (start 100 100) (end 150 100) (stroke (width 0.1) (type default))
            (layer "Edge.Cuts") (uuid "e1"))
          (gr_line (start 150 100) (end 150 120) (stroke (width 0.1) (type default))
            (layer "Edge.Cuts") (uuid "e2"))
          (gr_line (start 150 120) (end 100 120) (stroke (width 0.1) (type default))
            (layer "Edge.Cuts") (uuid "e3"))
          (gr_line (start 100 120) (end 100 100) (stroke (width 0.1) (type default))
            (layer "Edge.Cuts") (uuid "e4"))
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result = gen.add_board_markings(name="TestBoard", revision="B", date="2026-04-19")

        assert result.markings_added == 2  # name+revision combined, date
        assert result.markings_skipped == 0

        # Check gr_text nodes were added
        gr_texts = [n for n in gen.doc.find_all("gr_text")]
        marking_texts = [n for n in gr_texts if str(n.get_first_atom() or "").startswith(("TestBoard", "MyBoard", "PosTest", "Fallback", "2026-"))]
        assert len(marking_texts) == 2

    def test_idempotent(self, tmp_path):
        """Running add_board_markings twice should not duplicate."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result1 = gen.add_board_markings(name="MyBoard", revision="A")
        assert result1.markings_added == 1

        result2 = gen.add_board_markings(name="MyBoard", revision="A")
        assert result2.markings_added == 0
        assert result2.markings_skipped == 1

    def test_no_metadata_no_markings(self, tmp_path):
        """Calling with no metadata should add nothing."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result = gen.add_board_markings()

        assert result.markings_added == 0

    def test_position_near_outline(self, tmp_path):
        """Markings should be positioned near the board outline."""
        pcb = _minimal_pcb("""
          (gr_line (start 100 100) (end 150 100) (stroke (width 0.1) (type default))
            (layer "Edge.Cuts") (uuid "e1"))
          (gr_line (start 150 100) (end 150 130) (stroke (width 0.1) (type default))
            (layer "Edge.Cuts") (uuid "e2"))
          (gr_line (start 150 130) (end 100 130) (stroke (width 0.1) (type default))
            (layer "Edge.Cuts") (uuid "e3"))
          (gr_line (start 100 130) (end 100 100) (stroke (width 0.1) (type default))
            (layer "Edge.Cuts") (uuid "e4"))
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        gen.add_board_markings(name="PosTest", revision="A")

        marking_texts = [n for n in gen.doc.find_all("gr_text") if str(n.get_first_atom() or "").startswith(("TestBoard", "MyBoard", "PosTest", "Fallback", "2026-"))]
        assert len(marking_texts) == 1

        at_node = marking_texts[0].find("at")
        assert at_node is not None
        atoms = at_node.get_atoms()
        y = float(atoms[1])
        # Should be below the board bottom edge (130) + 1.5mm offset
        assert y == pytest.approx(131.5, abs=0.5)

    def test_fallback_position_no_outline(self, tmp_path):
        """Without Edge.Cuts, markings should use fallback position."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        gen.add_board_markings(name="Fallback")

        marking_texts = [n for n in gen.doc.find_all("gr_text") if str(n.get_first_atom() or "").startswith(("TestBoard", "MyBoard", "PosTest", "Fallback", "2026-"))]
        assert len(marking_texts) == 1

        at_node = marking_texts[0].find("at")
        atoms = at_node.get_atoms()
        # Fallback position
        assert float(atoms[0]) == pytest.approx(100.0)
        assert float(atoms[1]) == pytest.approx(115.0)


class TestSaveAndReload:
    """Test save/reload roundtrip."""

    def test_save_and_reload(self, tmp_path):
        """Changes should persist after save and reload."""
        pcb = _minimal_pcb("""
          (footprint "R:0805" (at 110 100) (layer "F.Cu")
            (fp_text reference "R1" (at 0 -2) (layer "F.SilkS")
              (hide yes)
              (effects (font (size 1 1) (thickness 0.15)))
            )
          )
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        gen.ensure_ref_des_visible()
        gen.add_board_markings(name="SaveTest", revision="C", date="2026-01-01")
        gen.save()

        # Reload and verify
        gen2 = SilkscreenGenerator(pcb_path)

        # Ref should still be visible
        ref_result = gen2.ensure_ref_des_visible()
        assert ref_result.refs_unhidden == 0

        # Markings should already exist (idempotent)
        mark_result = gen2.add_board_markings(name="SaveTest", revision="C", date="2026-01-01")
        assert mark_result.markings_added == 0
        assert mark_result.markings_skipped == 2  # name and date


class TestBuildStepEnum:
    """Verify SILKSCREEN is registered in the build pipeline."""

    def test_silkscreen_in_build_steps(self):
        from kicad_tools.cli.build_cmd import BuildStep

        assert hasattr(BuildStep, "SILKSCREEN")
        assert BuildStep.SILKSCREEN.value == "silkscreen"

    def test_silkscreen_step_order(self):
        """SILKSCREEN should come after OUTLINE and before ROUTE."""
        from kicad_tools.cli.build_cmd import BuildStep

        members = list(BuildStep)
        outline_idx = members.index(BuildStep.OUTLINE)
        silk_idx = members.index(BuildStep.SILKSCREEN)
        route_idx = members.index(BuildStep.ROUTE)

        assert outline_idx < silk_idx < route_idx


# ---------------------------------------------------------------------------
# Sidecar idempotency tests (issue #2494)
# ---------------------------------------------------------------------------


def _name_marking_texts(doc: SExp) -> list[str]:
    """Return visible text of every gr_text whose value looks like a marking."""
    out: list[str] = []
    for n in doc.find_all("gr_text"):
        atom = n.get_first_atom()
        if atom is None:
            continue
        out.append(str(atom))
    return out


class TestMarkingSidecar:
    """Sidecar-backed idempotency for board markings (issue #2494)."""

    def test_revision_bump_does_not_duplicate(self, tmp_path):
        """Bumping revision should replace, not duplicate, the name marking."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        r1 = gen.add_board_markings(name="Foo", revision="A")
        assert r1.markings_added == 1
        gen.save()

        # Reload (simulates a separate build run) and bump the revision.
        gen2 = SilkscreenGenerator(pcb_path)
        r2 = gen2.add_board_markings(name="Foo", revision="B")
        assert r2.markings_added == 1
        assert r2.markings_skipped == 0

        gr_text_atoms = [
            str(n.get_first_atom() or "") for n in gen2.doc.find_all("gr_text")
        ]
        name_markings = [t for t in gr_text_atoms if t.startswith("Foo")]
        assert len(name_markings) == 1
        assert name_markings[0] == "Foo Rev B"

    def test_rename_does_not_duplicate(self, tmp_path):
        """Renaming the project should replace, not duplicate, the name marking."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        gen.add_board_markings(name="Foo")
        gen.save()

        gen2 = SilkscreenGenerator(pcb_path)
        r2 = gen2.add_board_markings(name="Bar")
        assert r2.markings_added == 1
        assert r2.markings_skipped == 0

        atoms = [str(n.get_first_atom() or "") for n in gen2.doc.find_all("gr_text")]
        # No old "Foo" remains; exactly one "Bar".
        assert "Foo" not in atoms
        assert atoms.count("Bar") == 1

    def test_sidecar_survives_save_and_reload(self, tmp_path):
        """Sidecar JSON next to PCB enables idempotent re-runs across reloads."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        gen.add_board_markings(name="Persist", revision="1", date="2026-05-04")
        gen.save()

        sidecar = pcb_path.with_name(pcb_path.name + ".kct.json")
        assert sidecar.exists()
        data = json.loads(sidecar.read_text(encoding="utf-8"))
        assert data["version"] == 1
        tags = sorted(e["tag"] for e in data["markings"])
        assert tags == ["kct:date", "kct:name"]
        # Each entry has a UUID matching a gr_text in the PCB.
        gen2 = SilkscreenGenerator(pcb_path)
        r2 = gen2.add_board_markings(
            name="Persist", revision="1", date="2026-05-04"
        )
        assert r2.markings_added == 0
        assert r2.markings_skipped == 2

    def test_missing_sidecar_graceful(self, tmp_path):
        """No sidecar at construction must not error."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        # Registry should start empty; first run should add cleanly.
        result = gen.add_board_markings(name="FreshBoard")
        assert result.markings_added == 1

    def test_corrupt_sidecar_graceful(self, tmp_path, caplog):
        """Corrupt sidecar JSON should log a warning and proceed empty."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        sidecar = pcb_path.with_name(pcb_path.name + ".kct.json")
        sidecar.write_text("{this is not valid json", encoding="utf-8")

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        with caplog.at_level("WARNING"):
            gen = SilkscreenGenerator(pcb_path)
        # Generator constructed successfully; no markings tracked.
        result = gen.add_board_markings(name="AfterCorrupt")
        assert result.markings_added == 1

    def test_user_edited_gr_text_with_colliding_text_not_treated_as_marking(
        self, tmp_path
    ):
        """User-added gr_text whose visible text matches must not be skipped.

        Sidecar disambiguates by UUID — even if a user puts a gr_text on the
        board whose value happens to match the future marking string, the
        generator should still add a fresh marking with its own UUID.
        """
        # User has a (gr_text "Foo") already, but the sidecar does not list it.
        pcb = _minimal_pcb("""
          (gr_text "Foo" (at 90 90) (layer "F.SilkS")
            (uuid "user-added-uuid")
            (effects (font (size 1 1) (thickness 0.15))))
        """)
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        result = gen.add_board_markings(name="Foo")

        assert result.markings_added == 1
        # User text still present; generator added its own marking with a
        # distinct UUID and recorded it in the registry.
        gr_texts = gen.doc.find_all("gr_text")
        foo_nodes = [n for n in gr_texts if str(n.get_first_atom() or "") == "Foo"]
        assert len(foo_nodes) == 2

        uuids = []
        for n in foo_nodes:
            uuid_node = n.find("uuid")
            assert uuid_node is not None
            uuids.append(str(uuid_node.get_first_atom()))
        assert "user-added-uuid" in uuids
        # The other UUID was generated for the marking.
        assert len([u for u in uuids if u != "user-added-uuid"]) == 1

    def test_no_kct_subfields_on_gr_text(self, tmp_path):
        """Regression guard: no kct_* custom S-expression children on gr_text."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        gen.add_board_markings(name="NoCustom", revision="A", date="2026-05-04")
        gen.save()

        text = pcb_path.read_text(encoding="utf-8")
        assert "kct_marking" not in text
        assert "kct_" not in text

    def test_user_deleted_gr_text_re_adds_marking(self, tmp_path):
        """If the registry knows a UUID but the gr_text is gone, re-add it."""
        pcb = _minimal_pcb()
        pcb_path = _write_pcb(tmp_path, pcb)

        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(pcb_path)
        gen.add_board_markings(name="ReAdd")
        gen.save()

        # Remove the gr_text from the PCB, but leave the sidecar in place.
        gen.doc.children = [
            c for c in gen.doc.children
            if not (not c.is_atom and c.name == "gr_text")
        ]
        from kicad_tools.core.sexp_file import save_pcb
        save_pcb(gen.doc, pcb_path)

        gen2 = SilkscreenGenerator(pcb_path)
        result = gen2.add_board_markings(name="ReAdd")
        assert result.markings_added == 1
        assert result.markings_skipped == 0


class TestDeadConstantRemoved:
    """Dead `_MARKING_PREFIX` constant should be gone (issue #2494)."""

    def test_marking_prefix_removed(self):
        from kicad_tools.silkscreen import generator

        assert not hasattr(generator, "_MARKING_PREFIX")
