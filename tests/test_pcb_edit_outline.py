"""Tests for pcb edit-outline command and Edge.Cuts contour methods."""

import json

import pytest

# PCB with a gr_rect outline and gr_line outline (two overlapping outlines)
PCB_TWO_OUTLINES = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_rect
    (start 100 100)
    (end 150 130)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "rect-outline-1")
  )
  (gr_line
    (start 100 100)
    (end 160 100)
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts")
    (uuid "line-1")
  )
  (gr_line
    (start 160 100)
    (end 160 140)
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts")
    (uuid "line-2")
  )
  (gr_line
    (start 160 140)
    (end 100 140)
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts")
    (uuid "line-3")
  )
  (gr_line
    (start 100 140)
    (end 100 100)
    (stroke (width 0.1) (type default))
    (layer "Edge.Cuts")
    (uuid "line-4")
  )
)
"""

# PCB with an outline and a small mounting hole circle
PCB_WITH_MOUNTING_HOLE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_rect
    (start 100 100)
    (end 200 160)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "main-outline")
  )
  (gr_circle
    (center 110 110)
    (end 112 110)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "mounting-hole-1")
  )
)
"""

# PCB with no Edge.Cuts
PCB_NO_OUTLINE = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
  )
  (net 0 "")
)
"""

# PCB with a single rect outline
PCB_SINGLE_RECT = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (gr_rect
    (start 100 100)
    (end 150 130)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "single-rect")
  )
)
"""


class TestListEdgeContours:
    """Tests for PCB.list_edge_contours()."""

    def test_two_contours_detected(self, tmp_path):
        """Two overlapping outlines (rect + lines) should yield two contours."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)
        pcb = PCB.load(pcb_file)

        contours = pcb.list_edge_contours()
        assert len(contours) == 2

    def test_contour_bbox(self, tmp_path):
        """Contour bounding boxes should cover the correct area."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_SINGLE_RECT)
        pcb = PCB.load(pcb_file)

        contours = pcb.list_edge_contours()
        assert len(contours) == 1
        c = contours[0]
        assert c.bbox[0] == pytest.approx(100.0)
        assert c.bbox[1] == pytest.approx(100.0)
        assert c.bbox[2] == pytest.approx(150.0)
        assert c.bbox[3] == pytest.approx(130.0)
        assert c.bbox_width == pytest.approx(50.0)
        assert c.bbox_height == pytest.approx(30.0)

    def test_mounting_hole_detection(self, tmp_path):
        """Small circles on Edge.Cuts should be flagged as mounting holes."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_WITH_MOUNTING_HOLE)
        pcb = PCB.load(pcb_file)

        contours = pcb.list_edge_contours()
        assert len(contours) == 2
        holes = [c for c in contours if c.is_mounting_hole]
        outlines = [c for c in contours if not c.is_mounting_hole]
        assert len(holes) == 1
        assert len(outlines) == 1

    def test_no_outline(self, tmp_path):
        """Board with no Edge.Cuts should return empty list."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_NO_OUTLINE)
        pcb = PCB.load(pcb_file)

        contours = pcb.list_edge_contours()
        assert contours == []

    def test_line_contour_chaining(self, tmp_path):
        """Connected gr_line segments should form a single contour."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)
        pcb = PCB.load(pcb_file)

        contours = pcb.list_edge_contours()
        # The rect is standalone; the 4 lines are chained into one contour
        rect_contours = [c for c in contours if c.element_count == 1]
        line_contours = [c for c in contours if c.element_count == 4]
        assert len(rect_contours) == 1
        assert len(line_contours) == 1


class TestRemoveEdgeContour:
    """Tests for PCB.remove_edge_contour()."""

    def test_remove_rect_contour(self, tmp_path):
        """Removing a rect contour should leave only line contours."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)
        pcb = PCB.load(pcb_file)

        contours = pcb.list_edge_contours()
        rect_idx = next(c.index for c in contours if c.element_count == 1)

        ok = pcb.remove_edge_contour(rect_idx)
        assert ok is True

        remaining = pcb.list_edge_contours()
        assert len(remaining) == 1
        assert remaining[0].element_count == 4

    def test_remove_line_contour(self, tmp_path):
        """Removing a line contour should leave only the rect contour."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)
        pcb = PCB.load(pcb_file)

        contours = pcb.list_edge_contours()
        line_idx = next(c.index for c in contours if c.element_count == 4)

        ok = pcb.remove_edge_contour(line_idx)
        assert ok is True

        remaining = pcb.list_edge_contours()
        assert len(remaining) == 1
        assert remaining[0].element_count == 1

    def test_remove_invalid_index(self, tmp_path):
        """Removing a non-existent contour should return False."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_SINGLE_RECT)
        pcb = PCB.load(pcb_file)

        ok = pcb.remove_edge_contour(999)
        assert ok is False

    def test_remove_persists_after_save(self, tmp_path):
        """Removal should persist after save and reload."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)
        pcb = PCB.load(pcb_file)

        contours = pcb.list_edge_contours()
        rect_idx = next(c.index for c in contours if c.element_count == 1)
        pcb.remove_edge_contour(rect_idx)

        out_file = tmp_path / "saved.kicad_pcb"
        pcb.save(out_file)

        pcb2 = PCB.load(out_file)
        remaining = pcb2.list_edge_contours()
        assert len(remaining) == 1
        assert remaining[0].element_count == 4


class TestReplaceOutline:
    """Tests for PCB.replace_outline()."""

    def test_replace_creates_new_rect(self, tmp_path):
        """Replace should remove old outlines and insert a new rect."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)
        pcb = PCB.load(pcb_file)

        removed = pcb.replace_outline(50, 50, 80, 40)
        assert removed == 2  # rect + line contour

        contours = pcb.list_edge_contours()
        assert len(contours) == 1
        c = contours[0]
        assert c.bbox[0] == pytest.approx(50.0)
        assert c.bbox[1] == pytest.approx(50.0)
        assert c.bbox[2] == pytest.approx(130.0)
        assert c.bbox[3] == pytest.approx(90.0)

    def test_replace_preserves_mounting_holes(self, tmp_path):
        """Replace should keep mounting hole circles."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_WITH_MOUNTING_HOLE)
        pcb = PCB.load(pcb_file)

        removed = pcb.replace_outline(50, 50, 100, 60)
        assert removed == 1  # only the main outline, not the mounting hole

        contours = pcb.list_edge_contours()
        # Should have: new rect outline + preserved mounting hole
        assert len(contours) == 2
        holes = [c for c in contours if c.is_mounting_hole]
        assert len(holes) == 1

    def test_replace_persists_after_save(self, tmp_path):
        """Replaced outline should persist after save and reload."""
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_SINGLE_RECT)
        pcb = PCB.load(pcb_file)

        pcb.replace_outline(200, 200, 60, 40)

        out_file = tmp_path / "replaced.kicad_pcb"
        pcb.save(out_file)

        pcb2 = PCB.load(out_file)
        contours = pcb2.list_edge_contours()
        assert len(contours) == 1
        c = contours[0]
        assert c.bbox[0] == pytest.approx(200.0)
        assert c.bbox[1] == pytest.approx(200.0)
        assert c.bbox[2] == pytest.approx(260.0)
        assert c.bbox[3] == pytest.approx(240.0)


class TestEditOutlineCLI:
    """Tests for the pcb edit-outline CLI command."""

    def test_list_text_output(self, tmp_path, capsys):
        """--list should print human-readable contour info."""
        from kicad_tools.cli.commands.pcb import _run_edit_outline_command

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)

        class Args:
            pcb = str(pcb_file)
            list_contours = True
            remove_outline = None
            keep_only = None
            set_outline = None
            format = "text"
            dry_run = False
            output = None
            origin = None
            size = None

        result = _run_edit_outline_command(Args(), pcb_file)
        assert result == 0
        captured = capsys.readouterr()
        assert "2 Edge.Cuts contour(s)" in captured.out

    def test_list_json_output(self, tmp_path, capsys):
        """--list --format json should emit valid JSON."""
        from kicad_tools.cli.commands.pcb import _run_edit_outline_command

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)

        class Args:
            pcb = str(pcb_file)
            list_contours = True
            remove_outline = None
            keep_only = None
            set_outline = None
            format = "json"
            dry_run = False
            output = None
            origin = None
            size = None

        result = _run_edit_outline_command(Args(), pcb_file)
        assert result == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert len(data["contours"]) == 2

    def test_list_no_contours(self, tmp_path, capsys):
        """--list on a board with no Edge.Cuts should report none."""
        from kicad_tools.cli.commands.pcb import _run_edit_outline_command

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_NO_OUTLINE)

        class Args:
            pcb = str(pcb_file)
            list_contours = True
            remove_outline = None
            keep_only = None
            set_outline = None
            format = "text"
            dry_run = False
            output = None
            origin = None
            size = None

        result = _run_edit_outline_command(Args(), pcb_file)
        assert result == 0
        captured = capsys.readouterr()
        assert "No Edge.Cuts contours found" in captured.out

    def test_remove_dry_run(self, tmp_path, capsys):
        """--remove-outline --dry-run should not modify the file."""
        from kicad_tools.cli.commands.pcb import _run_edit_outline_command

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)
        original_content = pcb_file.read_text()

        class Args:
            pcb = str(pcb_file)
            list_contours = False
            remove_outline = 0
            keep_only = None
            set_outline = None
            format = "text"
            dry_run = True
            output = None
            origin = None
            size = None

        result = _run_edit_outline_command(Args(), pcb_file)
        assert result == 0
        captured = capsys.readouterr()
        assert "Would remove" in captured.out
        # File should be unchanged
        assert pcb_file.read_text() == original_content

    def test_set_outline_rect(self, tmp_path, capsys):
        """--set-outline rect should replace outlines with a new rectangle."""
        from kicad_tools.cli.commands.pcb import _run_edit_outline_command

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_SINGLE_RECT)

        class Args:
            pcb = str(pcb_file)
            list_contours = False
            remove_outline = None
            keep_only = None
            set_outline = "rect"
            format = "text"
            dry_run = False
            output = None
            origin = [50, 50]
            size = [80, 40]

        result = _run_edit_outline_command(Args(), pcb_file)
        assert result == 0
        captured = capsys.readouterr()
        assert "Replaced" in captured.out
        assert "80" in captured.out

    def test_set_outline_missing_params(self, tmp_path, capsys):
        """--set-outline rect without --origin/--size should error."""
        from kicad_tools.cli.commands.pcb import _run_edit_outline_command

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_SINGLE_RECT)

        class Args:
            pcb = str(pcb_file)
            list_contours = False
            remove_outline = None
            keep_only = None
            set_outline = "rect"
            format = "text"
            dry_run = False
            output = None
            origin = None
            size = None

        result = _run_edit_outline_command(Args(), pcb_file)
        assert result == 1

    def test_keep_only(self, tmp_path, capsys):
        """--keep-only should remove all contours except the specified one."""
        from kicad_tools.cli.commands.pcb import _run_edit_outline_command
        from kicad_tools.schema.pcb import PCB

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text(PCB_TWO_OUTLINES)

        # First find which index has the line contour (4 elements)
        pcb = PCB.load(pcb_file)
        contours = pcb.list_edge_contours()
        line_idx = next(c.index for c in contours if c.element_count == 4)

        class Args:
            pcb = str(pcb_file)
            list_contours = False
            remove_outline = None
            keep_only = line_idx
            set_outline = None
            format = "text"
            dry_run = False
            output = None
            origin = None
            size = None

        result = _run_edit_outline_command(Args(), pcb_file)
        assert result == 0

        pcb2 = PCB.load(pcb_file)
        remaining = pcb2.list_edge_contours()
        assert len(remaining) == 1
        assert remaining[0].element_count == 4
