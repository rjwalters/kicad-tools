"""Tests for board outline generation in the build pipeline.

Verifies that:
- PCBEditor.add_board_outline() creates gr_line segments on Edge.Cuts
- add_board_outline() is idempotent (skips if outline already exists)
- _run_step_outline() reads dimensions from the spec and writes outline
- _run_step_outline() skips gracefully when no dimensions are available
- gr_line_node builder produces valid KiCad S-expressions
"""

import tempfile
import textwrap
from pathlib import Path

import pytest

from kicad_tools.cli.build_cmd import BuildContext, BuildResult, _run_step_outline
from kicad_tools.pcb.editor import PCBEditor
from kicad_tools.sexp.builders import gr_line_node
from kicad_tools.spec.schema import (
    MechanicalRequirements,
    MountingHole,
    ProjectMetadata,
    ProjectSpec,
    Requirements,
)

# Minimal valid KiCad PCB content (no Edge.Cuts outline)
MINIMAL_PCB = textwrap.dedent("""\
    (kicad_pcb
        (version 20241229)
        (generator "test")
        (general
            (thickness 1.6)
        )
        (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
            (44 "Edge.Cuts" user)
        )
        (setup
            (pad_to_mask_clearance 0)
        )
        (net 0 "")
    )
""")

# PCB content with an existing Edge.Cuts outline
PCB_WITH_OUTLINE = textwrap.dedent("""\
    (kicad_pcb
        (version 20241229)
        (generator "test")
        (general
            (thickness 1.6)
        )
        (layers
            (0 "F.Cu" signal)
            (31 "B.Cu" signal)
            (44 "Edge.Cuts" user)
        )
        (setup
            (pad_to_mask_clearance 0)
        )
        (net 0 "")
        (gr_line
            (start 100 100)
            (end 200 100)
            (stroke (width 0.1) (type default))
            (layer "Edge.Cuts")
            (uuid "test-uuid-1")
        )
        (gr_line
            (start 200 100)
            (end 200 200)
            (stroke (width 0.1) (type default))
            (layer "Edge.Cuts")
            (uuid "test-uuid-2")
        )
        (gr_line
            (start 200 200)
            (end 100 200)
            (stroke (width 0.1) (type default))
            (layer "Edge.Cuts")
            (uuid "test-uuid-3")
        )
        (gr_line
            (start 100 200)
            (end 100 100)
            (stroke (width 0.1) (type default))
            (layer "Edge.Cuts")
            (uuid "test-uuid-4")
        )
    )
""")


def _make_spec(width_str=None, height_str=None, mounting_holes=None):
    """Create a ProjectSpec with optional mechanical dimensions."""
    dims = None
    if width_str or height_str:
        dims = {}
        if width_str:
            dims["width"] = width_str
        if height_str:
            dims["height"] = height_str

    mech = None
    if dims or mounting_holes:
        mech = MechanicalRequirements(dimensions=dims, mounting_holes=mounting_holes)

    reqs = Requirements(mechanical=mech) if mech else None

    return ProjectSpec(
        project=ProjectMetadata(name="test-project"),
        requirements=reqs,
    )


class TestGrLineNodeBuilder:
    """Tests for the gr_line_node S-expression builder."""

    def test_basic_gr_line(self):
        node = gr_line_node(100, 100, 200, 100, "Edge.Cuts", uuid_str="test-uuid")
        text = node.to_string()
        assert "gr_line" in text
        assert "Edge.Cuts" in text
        assert "100" in text
        assert "200" in text

    def test_custom_width(self):
        node = gr_line_node(0, 0, 50, 50, "F.SilkS", width=0.2, uuid_str="uuid-1")
        text = node.to_string()
        assert "F.SilkS" in text
        assert "0.2" in text


class TestPCBEditorBoardOutline:
    """Tests for PCBEditor.add_board_outline() and has_board_outline()."""

    def test_has_board_outline_false_on_bare_pcb(self, tmp_path):
        pcb_file = tmp_path / "bare.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB)
        editor = PCBEditor(str(pcb_file))
        assert not editor.has_board_outline()

    def test_has_board_outline_true_when_outline_exists(self, tmp_path):
        pcb_file = tmp_path / "with_outline.kicad_pcb"
        pcb_file.write_text(PCB_WITH_OUTLINE)
        editor = PCBEditor(str(pcb_file))
        assert editor.has_board_outline()

    def test_add_board_outline_creates_four_lines(self, tmp_path):
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB)
        editor = PCBEditor(str(pcb_file))

        nodes = editor.add_board_outline(150.0, 100.0)
        assert len(nodes) == 4

        # The outline should now be detectable
        assert editor.has_board_outline()

    def test_add_board_outline_dimensions_correct(self, tmp_path):
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB)
        editor = PCBEditor(str(pcb_file))

        nodes = editor.add_board_outline(150.0, 100.0, origin_x=100.0, origin_y=100.0)
        editor.save()

        # Re-read and verify the outline matches spec dimensions
        content = pcb_file.read_text()
        # Should contain lines from (100,100) to (250,100), (250,200), (100,200), back
        assert "100" in content
        assert "250" in content
        assert "200" in content

    def test_add_board_outline_idempotent(self, tmp_path):
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(PCB_WITH_OUTLINE)
        editor = PCBEditor(str(pcb_file))

        nodes = editor.add_board_outline(150.0, 100.0)
        assert nodes == []  # No new nodes created

    def test_add_board_outline_saves_and_parses(self, tmp_path):
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB)
        editor = PCBEditor(str(pcb_file))
        editor.add_board_outline(80.0, 60.0)
        editor.save()

        # Verify that re-loading detects the outline
        editor2 = PCBEditor(str(pcb_file))
        assert editor2.has_board_outline()

    def test_add_board_outline_closed_polygon(self, tmp_path):
        """The four gr_line segments should form a closed rectangle."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.write_text(MINIMAL_PCB)
        editor = PCBEditor(str(pcb_file))
        editor.add_board_outline(50.0, 30.0, origin_x=100.0, origin_y=100.0)
        editor.save()

        # Use the editor's _get_board_outline to verify closure
        outline = editor._get_board_outline()
        assert len(outline) >= 4
        # First and last points should be close (closed polygon)
        assert editor._points_close(outline[0], outline[-1])


class TestRunStepOutline:
    """Tests for _run_step_outline() build step."""

    def _make_context(self, tmp_path, pcb_content=MINIMAL_PCB, spec=None):
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(pcb_content)

        return BuildContext(
            project_dir=tmp_path,
            spec_file=tmp_path / "project.kct",
            spec=spec,
            pcb_file=pcb_file,
        )

    def test_skips_when_no_pcb_file(self, tmp_path):
        from rich.console import Console

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=None,
        )
        result = _run_step_outline(ctx, Console(quiet=True))
        assert result.success
        assert "No PCB file" in result.message

    def test_skips_when_no_dimensions(self, tmp_path):
        from rich.console import Console

        spec = _make_spec()  # no dimensions
        ctx = self._make_context(tmp_path, spec=spec)
        result = _run_step_outline(ctx, Console(quiet=True))
        assert result.success
        assert "No mechanical dimensions" in result.message

    def test_skips_when_only_width(self, tmp_path):
        from rich.console import Console

        spec = _make_spec(width_str="150mm")  # no height
        ctx = self._make_context(tmp_path, spec=spec)
        result = _run_step_outline(ctx, Console(quiet=True))
        assert result.success
        assert "No mechanical dimensions" in result.message

    def test_adds_outline_from_spec(self, tmp_path):
        from rich.console import Console

        spec = _make_spec(width_str="150mm", height_str="100mm")
        ctx = self._make_context(tmp_path, spec=spec)
        result = _run_step_outline(ctx, Console(quiet=True))
        assert result.success
        assert "150" in result.message
        assert "100" in result.message
        assert result.output_file is not None

        # Verify the PCB now has an outline
        editor = PCBEditor(str(ctx.pcb_file))
        assert editor.has_board_outline()

    def test_skips_existing_outline(self, tmp_path):
        from rich.console import Console

        spec = _make_spec(width_str="150mm", height_str="100mm")
        ctx = self._make_context(tmp_path, pcb_content=PCB_WITH_OUTLINE, spec=spec)
        result = _run_step_outline(ctx, Console(quiet=True))
        assert result.success
        assert "already exists" in result.message

    def test_dry_run(self, tmp_path):
        from rich.console import Console

        spec = _make_spec(width_str="150mm", height_str="100mm")
        ctx = self._make_context(tmp_path, spec=spec)
        ctx.dry_run = True
        result = _run_step_outline(ctx, Console(quiet=True))
        assert result.success
        assert "dry-run" in result.message

        # PCB should not be modified
        editor = PCBEditor(str(ctx.pcb_file))
        assert not editor.has_board_outline()

    def test_dimension_with_spaces(self, tmp_path):
        from rich.console import Console

        spec = _make_spec(width_str="80 mm", height_str="60 mm")
        ctx = self._make_context(tmp_path, spec=spec)
        result = _run_step_outline(ctx, Console(quiet=True))
        assert result.success
        assert "80" in result.message
        assert "60" in result.message
