"""Tests for improved error messages in build_cmd when no generator script is found."""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.build_cmd import (
    BuildContext,
    _format_no_generator_message,
    _generator_candidates,
    _run_step_pcb,
    _run_step_schematic,
)


class TestGeneratorCandidates:
    """Tests for _generator_candidates helper."""

    def test_schematic_candidates_include_type_specific_and_combined(self) -> None:
        candidates = _generator_candidates("schematic")
        assert "generate_schematic.py" in candidates
        assert "gen_schematic.py" in candidates
        assert "schematic_gen.py" in candidates
        assert "generate_design.py" in candidates
        assert "design.py" in candidates

    def test_pcb_candidates_include_type_specific_and_combined(self) -> None:
        candidates = _generator_candidates("pcb")
        assert "generate_pcb.py" in candidates
        assert "gen_pcb.py" in candidates
        assert "pcb_gen.py" in candidates
        assert "generate_design.py" in candidates
        assert "design.py" in candidates

    def test_design_candidates_no_combined_extras(self) -> None:
        candidates = _generator_candidates("design")
        assert "generate_design.py" in candidates
        assert "gen_design.py" in candidates
        assert "design_gen.py" in candidates
        # 'design' type should NOT add itself again as a combined candidate
        assert len(candidates) == 3


class TestFormatNoGeneratorMessage:
    """Tests for _format_no_generator_message helper."""

    def test_message_contains_all_candidate_filenames(self) -> None:
        msg = _format_no_generator_message("schematic", Path("/some/project"))
        for candidate in _generator_candidates("schematic"):
            assert candidate in msg, f"Expected '{candidate}' in error message"

    def test_message_contains_searched_directory(self) -> None:
        directory = Path("/my/project/dir")
        msg = _format_no_generator_message("schematic", directory)
        assert str(directory) in msg

    def test_message_contains_example_reference(self) -> None:
        msg = _format_no_generator_message("pcb", Path("/tmp"))
        assert "boards/00-simple-led/generate_design.py" in msg

    def test_message_contains_hint(self) -> None:
        msg = _format_no_generator_message("schematic", Path("/tmp"))
        assert "Hint" in msg or "hint" in msg.lower()

    def test_pcb_message_contains_pcb_candidates(self) -> None:
        msg = _format_no_generator_message("pcb", Path("/tmp"))
        for candidate in _generator_candidates("pcb"):
            assert candidate in msg, f"Expected '{candidate}' in PCB error message"


class TestRunStepSchematicError:
    """Tests for _run_step_schematic error path."""

    def test_error_message_lists_candidates(self, tmp_path: Path) -> None:
        """When no generator exists and no schematic file exists, the error
        message must list all candidate filenames."""
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            schematic_file=None,
            pcb_file=None,
        )
        from rich.console import Console

        console = Console(quiet=True)
        result = _run_step_schematic(ctx, console)

        assert not result.success
        for candidate in _generator_candidates("schematic"):
            assert candidate in result.message
        assert str(tmp_path) in result.message
        assert "boards/00-simple-led/generate_design.py" in result.message

    def test_existing_schematic_skips_without_error(self, tmp_path: Path) -> None:
        """When schematic already exists, return success even with no generator."""
        sch_file = tmp_path / "test.kicad_sch"
        sch_file.touch()
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            schematic_file=sch_file,
            pcb_file=None,
        )
        from rich.console import Console

        console = Console(quiet=True)
        result = _run_step_schematic(ctx, console)
        assert result.success
        assert "already exists" in result.message


class TestRunStepPcbError:
    """Tests for _run_step_pcb error path."""

    def test_error_message_lists_candidates(self, tmp_path: Path) -> None:
        """When no generator exists and no PCB file exists, the error
        message must list all candidate filenames."""
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            schematic_file=None,
            pcb_file=None,
        )
        from rich.console import Console

        console = Console(quiet=True)
        result = _run_step_pcb(ctx, console)

        assert not result.success
        for candidate in _generator_candidates("pcb"):
            assert candidate in result.message
        assert str(tmp_path) in result.message
        assert "boards/00-simple-led/generate_design.py" in result.message

    def test_existing_pcb_skips_without_error(self, tmp_path: Path) -> None:
        """When PCB already exists, return success even with no generator."""
        pcb_file = tmp_path / "test.kicad_pcb"
        pcb_file.touch()
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            schematic_file=None,
            pcb_file=pcb_file,
        )
        from rich.console import Console

        console = Console(quiet=True)
        result = _run_step_pcb(ctx, console)
        assert result.success
        assert "already exists" in result.message
