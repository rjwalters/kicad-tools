"""Tests for silkscreen generation (ref des visibility + board markings)."""

from __future__ import annotations

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
        marking_texts = [n for n in gr_texts if n.find("kct_marking") is not None]
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

        marking_texts = [n for n in gen.doc.find_all("gr_text") if n.find("kct_marking")]
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

        marking_texts = [n for n in gen.doc.find_all("gr_text") if n.find("kct_marking")]
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
