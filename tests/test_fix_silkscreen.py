"""Tests for the fix-silkscreen command and SilkscreenRepairer."""

from pathlib import Path

import pytest

from kicad_tools.cli.fix_silkscreen_cmd import _get_min_height, _get_min_width, main
from kicad_tools.drc.repair_silkscreen import SilkscreenRepairer
from kicad_tools.sexp.parser import parse_file

# --------------------------------------------------------------------------
# Fixtures: synthetic PCB content -- line width tests
# --------------------------------------------------------------------------

# PCB with undersized silkscreen lines in footprints (0.12mm < 0.15mm JLCPCB min).
PCB_WITH_UNDERSIZED_SILK = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (at 100 100)
    (property "Reference" "R1")
    (fp_line (start -0.153641 -0.38) (end 0.153641 -0.38)
      (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
    (fp_line (start -0.153641 0.38) (end 0.153641 0.38)
      (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
    (fp_rect (start -0.5 -0.3) (end 0.5 0.3)
      (stroke (width 0.10) (type solid)) (layer "F.SilkS"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (at 110 100)
    (property "Reference" "C1")
    (fp_line (start -0.153641 -0.38) (end 0.153641 -0.38)
      (stroke (width 0.12) (type solid)) (layer "F.SilkS"))
  )
)
"""

# PCB with a board-level silkscreen graphic that is undersized.
PCB_WITH_BOARD_LEVEL_SILK = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_line (start 10 10) (end 50 10)
    (stroke (width 0.10) (type solid)) (layer "F.SilkS"))
  (gr_rect (start 10 20) (end 50 30)
    (stroke (width 0.08) (type solid)) (layer "B.SilkS"))
)
"""

# PCB with non-silkscreen graphics (should NOT be modified).
PCB_WITH_NON_SILK_GRAPHICS = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Connector:PinHeader_1x02"
    (layer "F.Cu")
    (at 50 50)
    (property "Reference" "J1")
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0.10) (type solid)) (layer "F.Cu"))
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0.10) (type solid)) (layer "F.SilkS"))
  )
  (gr_line (start 0 0) (end 100 0)
    (stroke (width 0.05) (type solid)) (layer "Edge.Cuts"))
)
"""

# PCB with a zero-width stroke (should NOT be widened).
PCB_WITH_ZERO_WIDTH = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:ZeroWidth"
    (layer "F.Cu")
    (at 0 0)
    (property "Reference" "U1")
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0) (type solid)) (layer "F.SilkS"))
  )
)
"""

# PCB with lines already meeting minimum width.
PCB_WITH_OK_SILK = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:OkWidth"
    (layer "F.Cu")
    (at 0 0)
    (property "Reference" "U1")
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0.15) (type solid)) (layer "F.SilkS"))
    (fp_line (start 0 0) (end 0 5)
      (stroke (width 0.20) (type solid)) (layer "F.SilkS"))
  )
)
"""

# --------------------------------------------------------------------------
# Fixtures: synthetic PCB content -- text height tests
# --------------------------------------------------------------------------

# PCB with undersized silkscreen text in footprints.
PCB_WITH_UNDERSIZED_TEXT = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Resistor_SMD:R_0402_1005Metric"
    (layer "F.Cu")
    (at 100 100)
    (fp_text reference "R1"
      (at 0 -1.5)
      (effects (font (size 0.5 0.5) (thickness 0.075)))
      (layer "F.SilkS"))
    (fp_text value "10k"
      (at 0 1.5)
      (effects (font (size 0.5 0.5) (thickness 0.075)))
      (layer "F.SilkS"))
  )
  (footprint "Capacitor_SMD:C_0402_1005Metric"
    (layer "F.Cu")
    (at 110 100)
    (fp_text reference "C1"
      (at 0 -1.5)
      (effects (font (size 0.8 0.8) (thickness 0.12)))
      (layer "F.SilkS"))
  )
)
"""

# PCB with KiCad 8+ property nodes instead of fp_text.
PCB_WITH_UNDERSIZED_PROPERTY_TEXT = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:KiCad8"
    (layer "F.Cu")
    (at 0 0)
    (property "Reference" "U1"
      (at 0 -2)
      (effects (font (size 0.6 0.6) (thickness 0.09)))
      (layer "F.SilkS"))
    (property "Value" "IC1"
      (at 0 2)
      (effects (font (size 0.6 0.6) (thickness 0.09)))
      (layer "F.SilkS"))
  )
)
"""

# PCB with board-level gr_text on silkscreen.
PCB_WITH_BOARD_LEVEL_TEXT = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (36 "B.SilkS" user "B.Silkscreen")
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (gr_text "Rev A"
    (at 10 10)
    (effects (font (size 0.5 0.5) (thickness 0.075)))
    (layer "F.SilkS"))
  (gr_text "Board v1"
    (at 20 20)
    (effects (font (size 0.8 0.8) (thickness 0.12)))
    (layer "B.SilkS"))
)
"""

# PCB with hidden text (should NOT be modified).
PCB_WITH_HIDDEN_TEXT = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:Hidden"
    (layer "F.Cu")
    (at 0 0)
    (fp_text reference "U1"
      (at 0 -1.5)
      (effects (font (size 0.5 0.5) (thickness 0.075)))
      (layer "F.SilkS"))
    (fp_text value "IC"
      (at 0 1.5)
      (effects (font (size 0.5 0.5) (thickness 0.075)) hide)
      (layer "F.SilkS"))
  )
)
"""

# PCB with hidden text using (hide yes) syntax.
PCB_WITH_HIDDEN_TEXT_YES = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:HiddenYes"
    (layer "F.Cu")
    (at 0 0)
    (fp_text value "IC"
      (at 0 1.5)
      (effects (font (size 0.5 0.5) (thickness 0.075)))
      (layer "F.SilkS")
      (hide yes))
  )
)
"""

# PCB with zero-height text (should NOT be modified).
PCB_WITH_ZERO_HEIGHT_TEXT = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:ZeroHeight"
    (layer "F.Cu")
    (at 0 0)
    (fp_text reference "U1"
      (at 0 -1.5)
      (effects (font (size 0 0) (thickness 0)))
      (layer "F.SilkS"))
  )
)
"""

# PCB with non-1:1 aspect ratio text.
PCB_WITH_NONSTANDARD_ASPECT = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:Aspect"
    (layer "F.Cu")
    (at 0 0)
    (fp_text reference "U1"
      (at 0 -1.5)
      (effects (font (size 0.4 0.5) (thickness 0.075)))
      (layer "F.SilkS"))
  )
)
"""

# PCB with text on non-silkscreen layers (should NOT be modified).
PCB_WITH_NON_SILK_TEXT = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (38 "B.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:NonSilk"
    (layer "F.Cu")
    (at 0 0)
    (fp_text reference "U1"
      (at 0 -1.5)
      (effects (font (size 0.5 0.5) (thickness 0.075)))
      (layer "F.Cu"))
    (fp_text value "IC"
      (at 0 1.5)
      (effects (font (size 0.5 0.5) (thickness 0.075)))
      (layer "B.Fab"))
  )
)
"""

# PCB with text already meeting minimum height.
PCB_WITH_OK_TEXT = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:OkText"
    (layer "F.Cu")
    (at 0 0)
    (fp_text reference "U1"
      (at 0 -1.5)
      (effects (font (size 1.0 1.0) (thickness 0.15)))
      (layer "F.SilkS"))
    (fp_text value "IC"
      (at 0 1.5)
      (effects (font (size 1.2 1.2) (thickness 0.18)))
      (layer "F.SilkS"))
  )
)
"""

# PCB with both undersized lines AND undersized text.
PCB_WITH_UNDERSIZED_BOTH = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (footprint "Test:Both"
    (layer "F.Cu")
    (at 0 0)
    (property "Reference" "U1")
    (fp_text reference "U1"
      (at 0 -1.5)
      (effects (font (size 0.5 0.5) (thickness 0.075)))
      (layer "F.SilkS"))
    (fp_line (start 0 0) (end 5 0)
      (stroke (width 0.10) (type solid)) (layer "F.SilkS"))
  )
)
"""


# --------------------------------------------------------------------------
# Pytest fixtures
# --------------------------------------------------------------------------


@pytest.fixture
def pcb_undersized(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "undersized.kicad_pcb"
    pcb_file.write_text(PCB_WITH_UNDERSIZED_SILK)
    return pcb_file


@pytest.fixture
def pcb_board_level(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "board_level.kicad_pcb"
    pcb_file.write_text(PCB_WITH_BOARD_LEVEL_SILK)
    return pcb_file


@pytest.fixture
def pcb_non_silk(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "non_silk.kicad_pcb"
    pcb_file.write_text(PCB_WITH_NON_SILK_GRAPHICS)
    return pcb_file


@pytest.fixture
def pcb_zero_width(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "zero_width.kicad_pcb"
    pcb_file.write_text(PCB_WITH_ZERO_WIDTH)
    return pcb_file


@pytest.fixture
def pcb_ok(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "ok.kicad_pcb"
    pcb_file.write_text(PCB_WITH_OK_SILK)
    return pcb_file


@pytest.fixture
def pcb_undersized_text(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "undersized_text.kicad_pcb"
    pcb_file.write_text(PCB_WITH_UNDERSIZED_TEXT)
    return pcb_file


@pytest.fixture
def pcb_undersized_property(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "undersized_property.kicad_pcb"
    pcb_file.write_text(PCB_WITH_UNDERSIZED_PROPERTY_TEXT)
    return pcb_file


@pytest.fixture
def pcb_board_level_text(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "board_level_text.kicad_pcb"
    pcb_file.write_text(PCB_WITH_BOARD_LEVEL_TEXT)
    return pcb_file


@pytest.fixture
def pcb_hidden_text(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "hidden_text.kicad_pcb"
    pcb_file.write_text(PCB_WITH_HIDDEN_TEXT)
    return pcb_file


@pytest.fixture
def pcb_hidden_text_yes(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "hidden_text_yes.kicad_pcb"
    pcb_file.write_text(PCB_WITH_HIDDEN_TEXT_YES)
    return pcb_file


@pytest.fixture
def pcb_zero_height_text(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "zero_height_text.kicad_pcb"
    pcb_file.write_text(PCB_WITH_ZERO_HEIGHT_TEXT)
    return pcb_file


@pytest.fixture
def pcb_nonstandard_aspect(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "nonstandard_aspect.kicad_pcb"
    pcb_file.write_text(PCB_WITH_NONSTANDARD_ASPECT)
    return pcb_file


@pytest.fixture
def pcb_non_silk_text(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "non_silk_text.kicad_pcb"
    pcb_file.write_text(PCB_WITH_NON_SILK_TEXT)
    return pcb_file


@pytest.fixture
def pcb_ok_text(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "ok_text.kicad_pcb"
    pcb_file.write_text(PCB_WITH_OK_TEXT)
    return pcb_file


@pytest.fixture
def pcb_undersized_both(tmp_path: Path) -> Path:
    pcb_file = tmp_path / "undersized_both.kicad_pcb"
    pcb_file.write_text(PCB_WITH_UNDERSIZED_BOTH)
    return pcb_file


# --------------------------------------------------------------------------
# Tests: _get_min_width
# --------------------------------------------------------------------------


class TestGetMinWidth:
    """Tests for the _get_min_width helper."""

    def test_explicit_width_overrides(self):
        """Explicit --min-width takes precedence over manufacturer."""
        assert _get_min_width("jlcpcb", 2, 1.0, 0.20) == 0.20

    def test_jlcpcb_default(self):
        """JLCPCB 2-layer 1oz returns 0.15mm."""
        assert _get_min_width("jlcpcb", 2, 1.0, None) == 0.15

    def test_no_mfr_fallback(self):
        """No manufacturer returns sensible default."""
        assert _get_min_width(None, 2, 1.0, None) == 0.15


# --------------------------------------------------------------------------
# Tests: _get_min_height
# --------------------------------------------------------------------------


class TestGetMinHeight:
    """Tests for the _get_min_height helper."""

    def test_explicit_height_overrides(self):
        """Explicit --min-height takes precedence over manufacturer."""
        assert _get_min_height("jlcpcb", 2, 1.0, 1.5) == 1.5

    def test_jlcpcb_default(self):
        """JLCPCB 2-layer 1oz returns 1.0mm."""
        assert _get_min_height("jlcpcb", 2, 1.0, None) == 1.0

    def test_no_mfr_fallback(self):
        """No manufacturer returns sensible default."""
        assert _get_min_height(None, 2, 1.0, None) == 1.0


# --------------------------------------------------------------------------
# Tests: SilkscreenRepairer -- line widths
# --------------------------------------------------------------------------


class TestSilkscreenRepairer:
    """Tests for the core line width repair logic."""

    def test_fixes_undersized_fp_lines(self, pcb_undersized: Path):
        """Undersized fp_line and fp_rect strokes are widened."""
        repairer = SilkscreenRepairer(pcb_undersized)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        # R1 has 2 fp_line (0.12) + 1 fp_rect (0.10), C1 has 1 fp_line (0.12) = 4 total
        assert result.total_fixed == 4

        # Verify all fixes are for silkscreen elements
        for fix in result.fixes:
            assert fix.new_width == 0.15
            assert fix.old_width < 0.15

    def test_dry_run_no_mutation(self, pcb_undersized: Path):
        """dry_run=True collects fixes but does not mutate the tree."""
        repairer = SilkscreenRepairer(pcb_undersized)
        result = repairer.repair_line_widths(min_width_mm=0.15, dry_run=True)

        assert result.total_fixed == 4

        # Save and verify original is unchanged
        repairer.save()
        # After save with dry_run, the SExp tree should still have the old widths.
        # Re-parse to check.
        doc = parse_file(pcb_undersized)
        # Find the first fp_line in the first footprint
        fp = doc.find_all("footprint")[0]
        fp_line = fp.find_all("fp_line")[0]
        stroke = fp_line.find("stroke")
        width = stroke.find("width")
        # Should still be 0.12 because dry_run prevented mutation
        assert float(width.get_first_atom()) == 0.12

    def test_mutates_tree_when_not_dry_run(self, pcb_undersized: Path):
        """Without dry_run, the SExp tree is actually modified."""
        repairer = SilkscreenRepairer(pcb_undersized)
        result = repairer.repair_line_widths(min_width_mm=0.15, dry_run=False)

        assert result.total_fixed == 4

        # Verify the tree was mutated
        fp = repairer.doc.find_all("footprint")[0]
        fp_line = fp.find_all("fp_line")[0]
        stroke = fp_line.find("stroke")
        width = stroke.find("width")
        assert float(width.get_first_atom()) == 0.15

    def test_board_level_graphics(self, pcb_board_level: Path):
        """Board-level gr_line and gr_rect on silk layers are widened."""
        repairer = SilkscreenRepairer(pcb_board_level)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        assert result.total_fixed == 2
        assert result.fixes[0].element_type == "gr_line"
        assert result.fixes[0].footprint_ref == ""
        assert result.fixes[1].element_type == "gr_rect"

    def test_non_silk_layers_untouched(self, pcb_non_silk: Path):
        """Graphics on non-silkscreen layers are not modified."""
        repairer = SilkscreenRepairer(pcb_non_silk)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        # Only the fp_line on F.SilkS should be fixed, not F.Cu or Edge.Cuts
        assert result.total_fixed == 1
        assert result.fixes[0].layer == "F.SilkS"

    def test_zero_width_not_widened(self, pcb_zero_width: Path):
        """Zero-width strokes (inherit from style) are skipped."""
        repairer = SilkscreenRepairer(pcb_zero_width)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        assert result.total_fixed == 0

    def test_already_ok_no_fixes(self, pcb_ok: Path):
        """Lines already meeting minimum width are not modified."""
        repairer = SilkscreenRepairer(pcb_ok)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        assert result.total_fixed == 0

    def test_save_to_output_path(self, pcb_undersized: Path, tmp_path: Path):
        """Saving to a different output path leaves original unchanged."""
        output_path = tmp_path / "output.kicad_pcb"
        original_content = pcb_undersized.read_text()

        repairer = SilkscreenRepairer(pcb_undersized)
        repairer.repair_line_widths(min_width_mm=0.15)
        repairer.save(output_path)

        # Original file should be unchanged (we read from it, save elsewhere)
        assert pcb_undersized.read_text() == original_content

        # Output file should exist and have the fixes
        assert output_path.exists()
        doc = parse_file(output_path)
        fp = doc.find_all("footprint")[0]
        fp_line = fp.find_all("fp_line")[0]
        stroke = fp_line.find("stroke")
        width = stroke.find("width")
        assert float(width.get_first_atom()) == 0.15

    def test_footprint_reference_extraction(self, pcb_undersized: Path):
        """Fix records include the correct footprint reference."""
        repairer = SilkscreenRepairer(pcb_undersized)
        result = repairer.repair_line_widths(min_width_mm=0.15)

        refs = {f.footprint_ref for f in result.fixes}
        assert "R1" in refs
        assert "C1" in refs


# --------------------------------------------------------------------------
# Tests: SilkscreenRepairer -- text heights
# --------------------------------------------------------------------------


class TestSilkscreenRepairerTextHeight:
    """Tests for the text height repair logic."""

    def test_fixes_undersized_text_height(self, pcb_undersized_text: Path):
        """fp_text with height < min is scaled up."""
        repairer = SilkscreenRepairer(pcb_undersized_text)
        result = repairer.repair_text_heights(min_height_mm=1.0)

        # R1 has 2 undersized fp_text (0.5mm), C1 has 1 (0.8mm) = 3 total
        assert result.total_fixed == 3

        for fix in result.fixes:
            assert fix.new_height == 1.0
            assert fix.old_height < 1.0

    def test_text_height_preserves_aspect_ratio(self, pcb_nonstandard_aspect: Path):
        """Width scales proportionally with height."""
        repairer = SilkscreenRepairer(pcb_nonstandard_aspect)
        result = repairer.repair_text_heights(min_height_mm=1.0)

        assert result.total_fixed == 1
        fix = result.fixes[0]
        # Original: W=0.4, H=0.5, ratio W/H = 0.8
        # New: H=1.0, so W should be 0.4 * (1.0/0.5) = 0.8
        assert fix.old_width == 0.4
        assert fix.old_height == 0.5
        assert fix.new_height == 1.0
        assert fix.new_width == pytest.approx(0.8, abs=1e-6)

        # Thickness should also scale proportionally
        assert fix.old_thickness == 0.075
        assert fix.new_thickness == pytest.approx(0.15, abs=1e-6)

    def test_text_height_mutates_tree(self, pcb_undersized_text: Path):
        """Without dry_run, the SExp tree is actually modified."""
        repairer = SilkscreenRepairer(pcb_undersized_text)
        repairer.repair_text_heights(min_height_mm=1.0)

        # Verify the tree was mutated -- check the first footprint's first fp_text
        fp = repairer.doc.find_all("footprint")[0]
        fp_text = fp.find_all("fp_text")[0]
        effects = fp_text.find("effects")
        font = effects.find("font")
        size = font.find("size")
        atoms = size.get_atoms()
        assert float(atoms[0]) == 1.0  # width scaled from 0.5 to 1.0
        assert float(atoms[1]) == 1.0  # height scaled from 0.5 to 1.0

        # Also check thickness was scaled
        thickness = font.find("thickness")
        assert float(thickness.get_first_atom()) == pytest.approx(0.15, abs=1e-6)

    def test_hidden_text_skipped(self, pcb_hidden_text: Path):
        """Hidden fp_text is not modified."""
        repairer = SilkscreenRepairer(pcb_hidden_text)
        result = repairer.repair_text_heights(min_height_mm=1.0)

        # Only the visible reference text should be fixed, not the hidden value
        assert result.total_fixed == 1
        assert result.fixes[0].element_type == "fp_text"

    def test_hidden_text_yes_skipped(self, pcb_hidden_text_yes: Path):
        """Hidden fp_text with (hide yes) syntax is not modified."""
        repairer = SilkscreenRepairer(pcb_hidden_text_yes)
        result = repairer.repair_text_heights(min_height_mm=1.0)

        assert result.total_fixed == 0

    def test_zero_height_text_skipped(self, pcb_zero_height_text: Path):
        """Zero-height text is not modified."""
        repairer = SilkscreenRepairer(pcb_zero_height_text)
        result = repairer.repair_text_heights(min_height_mm=1.0)

        assert result.total_fixed == 0

    def test_board_level_text_height(self, pcb_board_level_text: Path):
        """gr_text on silk layers is fixed."""
        repairer = SilkscreenRepairer(pcb_board_level_text)
        result = repairer.repair_text_heights(min_height_mm=1.0)

        # Both gr_text elements are undersized (0.5 and 0.8)
        assert result.total_fixed == 2
        for fix in result.fixes:
            assert fix.element_type == "gr_text"
            assert fix.footprint_ref == ""

    def test_property_nodes_handled(self, pcb_undersized_property: Path):
        """KiCad 8 property nodes on silkscreen are fixed."""
        repairer = SilkscreenRepairer(pcb_undersized_property)
        result = repairer.repair_text_heights(min_height_mm=1.0)

        assert result.total_fixed == 2
        for fix in result.fixes:
            assert fix.element_type == "property"
            assert fix.new_height == 1.0

    def test_non_silk_text_untouched(self, pcb_non_silk_text: Path):
        """Text on non-silkscreen layers is not modified."""
        repairer = SilkscreenRepairer(pcb_non_silk_text)
        result = repairer.repair_text_heights(min_height_mm=1.0)

        assert result.total_fixed == 0

    def test_already_ok_text_no_fixes(self, pcb_ok_text: Path):
        """Text already meeting minimum height is not modified."""
        repairer = SilkscreenRepairer(pcb_ok_text)
        result = repairer.repair_text_heights(min_height_mm=1.0)

        assert result.total_fixed == 0

    def test_text_height_dry_run(self, pcb_undersized_text: Path):
        """dry run collects fixes without mutation."""
        repairer = SilkscreenRepairer(pcb_undersized_text)
        result = repairer.repair_text_heights(min_height_mm=1.0, dry_run=True)

        assert result.total_fixed == 3

        # Verify the tree was NOT mutated
        fp = repairer.doc.find_all("footprint")[0]
        fp_text = fp.find_all("fp_text")[0]
        effects = fp_text.find("effects")
        font = effects.find("font")
        size = font.find("size")
        atoms = size.get_atoms()
        assert float(atoms[1]) == 0.5  # still original height

    def test_text_height_idempotent(self, pcb_undersized_text: Path):
        """Running twice produces same result."""
        repairer = SilkscreenRepairer(pcb_undersized_text)
        result1 = repairer.repair_text_heights(min_height_mm=1.0)
        assert result1.total_fixed == 3

        # Run again on the same (already-mutated) tree
        result2 = repairer.repair_text_heights(min_height_mm=1.0)
        assert result2.total_fixed == 0


# --------------------------------------------------------------------------
# Tests: CLI main()
# --------------------------------------------------------------------------


class TestCLI:
    """Tests for the fix-silkscreen CLI entry point."""

    def test_dry_run_no_file_change(self, pcb_undersized: Path):
        """--dry-run prints summary and exits 0 without modifying the file."""
        original = pcb_undersized.read_text()
        exit_code = main([str(pcb_undersized), "--dry-run"])
        assert exit_code == 0
        assert pcb_undersized.read_text() == original

    def test_apply_fixes(self, pcb_undersized: Path):
        """Running without --dry-run modifies the file."""
        exit_code = main([str(pcb_undersized)])
        assert exit_code == 0

        # Verify the file was modified
        doc = parse_file(pcb_undersized)
        fp = doc.find_all("footprint")[0]
        fp_line = fp.find_all("fp_line")[0]
        stroke = fp_line.find("stroke")
        width = stroke.find("width")
        assert float(width.get_first_atom()) == 0.15

    def test_output_flag(self, pcb_undersized: Path, tmp_path: Path):
        """--output writes to a different file, leaving original intact."""
        output = tmp_path / "fixed.kicad_pcb"
        original = pcb_undersized.read_text()
        exit_code = main([str(pcb_undersized), "-o", str(output)])
        assert exit_code == 0
        assert pcb_undersized.read_text() == original
        assert output.exists()

    def test_json_output(self, pcb_undersized: Path, capsys):
        """--format json produces valid JSON output."""
        exit_code = main([str(pcb_undersized), "--dry-run", "--format", "json"])
        assert exit_code == 0
        import json

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total_fixed"] == 4
        assert data["dry_run"] is True
        assert "text_height_fixes" in data

    def test_summary_output(self, pcb_undersized: Path, capsys):
        """--format summary produces compact output."""
        exit_code = main([str(pcb_undersized), "--dry-run", "--format", "summary"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert "4" in captured.out
        assert "Would fix" in captured.out

    def test_missing_file_error(self, tmp_path: Path):
        """Non-existent PCB file returns exit code 1."""
        exit_code = main([str(tmp_path / "nonexistent.kicad_pcb")])
        assert exit_code == 1

    def test_wrong_extension_error(self, tmp_path: Path):
        """Non-.kicad_pcb file returns exit code 1."""
        bad_file = tmp_path / "test.txt"
        bad_file.write_text("hello")
        exit_code = main([str(bad_file)])
        assert exit_code == 1

    def test_no_violations_returns_zero(self, pcb_ok: Path):
        """When no violations exist, exit code is still 0."""
        exit_code = main([str(pcb_ok)])
        assert exit_code == 0

    def test_min_width_override(self, pcb_undersized: Path):
        """--min-width overrides manufacturer defaults."""
        exit_code = main([str(pcb_undersized), "--min-width", "0.20", "--dry-run"])
        assert exit_code == 0

    def test_min_height_override(self, pcb_undersized_text: Path):
        """--min-height overrides manufacturer defaults."""
        exit_code = main([str(pcb_undersized_text), "--min-height", "0.8", "--dry-run"])
        assert exit_code == 0

    def test_quiet_flag(self, pcb_undersized: Path, capsys):
        """--quiet suppresses output."""
        exit_code = main([str(pcb_undersized), "--dry-run", "--quiet"])
        assert exit_code == 0
        captured = capsys.readouterr()
        assert captured.out == ""

    def test_text_height_fix_applied(self, pcb_undersized_text: Path):
        """CLI integrates text height repair."""
        exit_code = main([str(pcb_undersized_text)])
        assert exit_code == 0

        # Verify text heights were fixed
        doc = parse_file(pcb_undersized_text)
        fp = doc.find_all("footprint")[0]
        fp_text = fp.find_all("fp_text")[0]
        effects = fp_text.find("effects")
        font = effects.find("font")
        size = font.find("size")
        atoms = size.get_atoms()
        assert float(atoms[1]) >= 1.0  # height was scaled up

    def test_both_line_and_text_fixes(self, pcb_undersized_both: Path, capsys):
        """CLI fixes both line widths and text heights in a single run."""
        exit_code = main([str(pcb_undersized_both), "--dry-run", "--format", "json"])
        assert exit_code == 0

        import json

        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["total_line_width_fixed"] == 1
        assert data["total_text_height_fixed"] == 1
        assert data["total_fixed"] == 2
