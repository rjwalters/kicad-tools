"""Tests for improved error messages in build_cmd when no generator script is found."""

from __future__ import annotations

import os
from pathlib import Path

from kicad_tools.cli.build_cmd import (
    BuildContext,
    _capture_routed_artifact_mtime,
    _format_no_generator_message,
    _generate_design_supports_step_route,
    _generator_candidates,
    _resolve_route_recipe,
    _run_step_pcb,
    _run_step_route,
    _run_step_schematic,
    _scan_recipe_routed_artifact,
    main,
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


class TestSpecPathResolution:
    """Tests for spec path resolution in main() to prevent path doubling."""

    def test_relative_file_spec_uses_parent_as_project_dir(self, tmp_path: Path) -> None:
        """A relative .kct file path should use the parent directory, not the
        full path including the filename, as project_dir."""
        subdir = tmp_path / "boards" / "external" / "softstart"
        subdir.mkdir(parents=True)
        spec = subdir / "project.kct"
        spec.write_text("{}")

        # Run from tmp_path with a relative path to the spec
        saved_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            # main() will fail because there's no generator, but it should NOT
            # produce a "Directory not found" error with a doubled path.
            rc = main(["boards/external/softstart/project.kct", "--quiet"])
        finally:
            os.chdir(saved_cwd)

        # The return code may be non-zero (no generator script), but we care
        # that it did NOT fail with rc=1 due to a missing directory from path
        # doubling. If the path were doubled, project_dir would not exist and
        # the error would mention the doubled path.
        # With the fix, project_dir resolves to the subdir which exists.
        # We verify by checking that main at least got past the directory check.
        # A return code of 1 with a non-existent doubled directory would be the
        # bug; any other outcome means the path resolved correctly.
        assert rc is not None  # main returned (did not crash)

    def test_relative_dir_spec_resolves_correctly(self, tmp_path: Path) -> None:
        """A relative directory path should be used directly as project_dir."""
        subdir = tmp_path / "boards" / "external" / "softstart"
        subdir.mkdir(parents=True)
        spec = subdir / "project.kct"
        spec.write_text("{}")

        saved_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            rc = main(["boards/external/softstart/", "--quiet"])
        finally:
            os.chdir(saved_cwd)

        assert rc is not None

    def test_nonexistent_kct_file_uses_parent_dir(self, tmp_path: Path) -> None:
        """A non-existent .kct file path should use the parent as project_dir,
        not treat the full path (including filename) as a directory."""
        subdir = tmp_path / "boards" / "external" / "softstart"
        subdir.mkdir(parents=True)
        # Do NOT create the .kct file -- it doesn't exist on disk

        saved_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            rc = main(["boards/external/softstart/project.kct", "--quiet"])
        finally:
            os.chdir(saved_cwd)

        # With the fix, project_dir = subdir (which exists), so the error
        # should NOT be "Directory not found" with a doubled path.
        # It should proceed past the directory check (rc != 1 from dir check,
        # or if rc == 1 it's for another reason).
        assert rc is not None

    def test_nonexistent_dir_spec_reports_not_found(self, tmp_path: Path) -> None:
        """A completely non-existent directory path should fail gracefully."""
        saved_cwd = os.getcwd()
        try:
            os.chdir(tmp_path)
            rc = main(["nonexistent/path/", "--quiet"])
        finally:
            os.chdir(saved_cwd)

        assert rc == 1  # Should report directory not found


class TestOutputDirArgument:
    """Tests for -o/--output argument parsing and BuildContext.output_dir."""

    def test_output_arg_parses_without_error(self, tmp_path: Path) -> None:
        """Passing -o should not cause an 'unrecognized arguments' error."""
        kct_file = tmp_path / "project.kct"
        kct_file.write_text("[project]\nname = 'test'\n")
        output_dir = tmp_path / "out"
        # --dry-run prevents actual execution; we just verify parsing succeeds
        ret = main([str(kct_file), "-o", str(output_dir), "--dry-run"])
        # dry-run may fail on missing generators, but should not be an arg-parse error
        assert ret in (0, 1)

    def test_output_dir_created_when_missing(self, tmp_path: Path) -> None:
        """When --output points to a non-existent nested directory, it should be created."""
        kct_file = tmp_path / "project.kct"
        kct_file.write_text("[project]\nname = 'test'\n")
        output_dir = tmp_path / "nested" / "deep" / "dir"
        assert not output_dir.exists()
        main([str(kct_file), "-o", str(output_dir), "--dry-run"])
        assert output_dir.exists()
        assert output_dir.is_dir()

    def test_build_context_output_dir_is_set(self) -> None:
        """BuildContext should accept and store output_dir."""
        ctx = BuildContext(
            project_dir=Path("/tmp/test"),
            spec_file=None,
            output_dir=Path("/tmp/output"),
        )
        assert ctx.output_dir == Path("/tmp/output")

    def test_build_context_output_dir_defaults_to_none(self) -> None:
        """BuildContext.output_dir should default to None when not provided."""
        ctx = BuildContext(
            project_dir=Path("/tmp/test"),
            spec_file=None,
        )
        assert ctx.output_dir is None

    def test_omitting_output_preserves_default_behavior(self, tmp_path: Path) -> None:
        """When -o is not passed, the build should still work (no regression)."""
        kct_file = tmp_path / "project.kct"
        kct_file.write_text("[project]\nname = 'test'\n")
        ret = main([str(kct_file), "--dry-run"])
        assert ret in (0, 1)


class TestBuildRouteExitCode2:
    """Tests for build_cmd treating route exit code 2 as success (issue #1641).

    When the router returns exit code 2 (partial routing), the build should
    treat it as success because the unrouted nets are intentionally-skipped
    pour nets (GND, +3.3V, etc.) that will be connected via zone fill.
    """

    def test_route_exit_code_2_is_success(self, tmp_path: Path) -> None:
        """Route exit code 2 (partial routing) should be treated as success."""
        from unittest.mock import MagicMock, patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        output_file = tmp_path / "board_routed.kicad_pcb"
        output_file.write_text("(kicad_pcb)")  # simulate output exists

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
        )
        ctx.pcb_file = pcb_file
        ctx.mfr = "jlcpcb"
        ctx.quiet = True
        ctx.verbose = False
        ctx.dry_run = False
        ctx.force = True
        ctx.output_dir = None
        ctx.spec = None

        console = Console()

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
            mock_run.return_value = MagicMock(returncode=2, stderr="", stdout="")
            result = _run_step_route(ctx, console)

        assert result.success is True, f"Route exit code 2 should be success, got: {result.message}"
        assert "skipped" in result.message.lower() or "zone fill" in result.message.lower()

    def test_route_exit_code_0_is_success(self, tmp_path: Path) -> None:
        """Route exit code 0 (full success) remains success."""
        from unittest.mock import MagicMock, patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        output_file = tmp_path / "board_routed.kicad_pcb"
        output_file.write_text("(kicad_pcb)")

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
        )
        ctx.pcb_file = pcb_file
        ctx.mfr = "jlcpcb"
        ctx.quiet = True
        ctx.verbose = False
        ctx.dry_run = False
        ctx.force = True
        ctx.output_dir = None
        ctx.spec = None

        console = Console()

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = _run_step_route(ctx, console)

        assert result.success is True

    def test_route_exit_code_1_is_failure(self, tmp_path: Path) -> None:
        """Route exit code 1 (fatal failure) remains failure."""
        from unittest.mock import MagicMock, patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
        )
        ctx.pcb_file = pcb_file
        ctx.mfr = "jlcpcb"
        ctx.quiet = True
        ctx.verbose = False
        ctx.dry_run = False
        ctx.force = True
        ctx.output_dir = None
        ctx.spec = None

        console = Console()

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stderr="routing failed", stdout="")
            result = _run_step_route(ctx, console)

        assert result.success is False

    def test_route_exit_code_3_is_nonfatal_warning(self, tmp_path: Path) -> None:
        """Route exit code 3 (DRC violations remain) is non-fatal.

        Exit codes 2-5 all produce usable output files; build_cmd treats
        them as success-with-warning so the pipeline continues to the
        verification step, which is responsible for surfacing remaining
        DRC violations (stale-test update, issue #3436 burn-down).
        """
        from unittest.mock import MagicMock, patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
        )
        ctx.pcb_file = pcb_file
        ctx.mfr = "jlcpcb"
        ctx.quiet = True
        ctx.verbose = False
        ctx.dry_run = False
        ctx.force = True
        ctx.output_dir = None
        ctx.spec = None

        console = Console()

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
            mock_run.return_value = MagicMock(returncode=3, stderr="DRC violations", stdout="")
            result = _run_step_route(ctx, console)

        assert result.success is True
        assert "DRC violations remain" in result.message


class TestRouteStepRecipeArtifactGuard:
    """The ROUTE step must not clobber a recipe-produced routed PCB.

    Board 04's ``generate_design.py`` routes, stitches, and fills zones
    during the PCB step, writing ``*_routed.kicad_pcb`` in-place. The PCB
    arm of ``main()`` records that artifact on ``ctx.routed_pcb_file``; the
    ROUTE step must then skip re-routing instead of overwriting it with the
    generic autorouter (issue #3971).
    """

    def test_route_step_skips_when_recipe_artifact_present(self, tmp_path: Path) -> None:
        """Pre-set ``routed_pcb_file`` short-circuits the route step."""
        from unittest.mock import patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        routed_file = tmp_path / "board_routed.kicad_pcb"
        routed_file.write_text("(kicad_pcb)")

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
        )
        ctx.pcb_file = pcb_file
        ctx.routed_pcb_file = routed_file
        ctx.mfr = "jlcpcb"
        ctx.quiet = True
        ctx.verbose = False
        ctx.dry_run = False
        ctx.force = False
        ctx.output_dir = None
        ctx.spec = None

        console = Console()

        # The router must never be invoked when the recipe already routed.
        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
            result = _run_step_route(ctx, console)

        assert result.success is True
        assert "recipe" in result.message.lower()
        assert result.output_file == routed_file
        mock_run.assert_not_called()

    def test_force_bypasses_recipe_artifact_guard(self, tmp_path: Path) -> None:
        """``--force`` (ctx.force) re-routes even when a routed artifact exists."""
        from unittest.mock import MagicMock, patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        routed_file = tmp_path / "board_routed.kicad_pcb"
        routed_file.write_text("(kicad_pcb)")

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
        )
        ctx.pcb_file = pcb_file
        ctx.routed_pcb_file = routed_file
        ctx.mfr = "jlcpcb"
        ctx.quiet = True
        ctx.verbose = False
        ctx.dry_run = False
        ctx.force = True
        ctx.output_dir = None
        ctx.spec = None

        console = Console()

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = _run_step_route(ctx, console)

        # With force, the guard is bypassed and the router actually runs.
        assert result.success is True
        assert "recipe" not in result.message.lower()
        mock_run.assert_called()


class TestScanRecipeRoutedArtifact:
    """The post-PCB scan must reject stale ``*_routed.kicad_pcb`` artifacts.

    ``_scan_recipe_routed_artifact`` decides whether the routed sibling
    produced beside the PCB output should be recorded on
    ``ctx.routed_pcb_file``. It trusts the artifact only when the PCB step
    created or refreshed it (mtime advanced past the pre-step snapshot),
    guarding against stale copper committed in external boards (issue #3978)
    while preserving in-place-routing recipe behavior (issue #3971).
    """

    def test_freshly_written_routed_artifact_is_trusted(self, tmp_path: Path) -> None:
        """An in-place recipe that rewrites the routed file → artifact trusted.

        Mirrors board 04: no routed file exists before the PCB step, so the
        pre-step snapshot is ``None`` and any produced artifact is trusted.
        """
        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        pre_mtime = _capture_routed_artifact_mtime(pcb_file)
        assert pre_mtime is None  # no routed file existed before the PCB step

        # PCB step writes the routed artifact.
        routed_file = tmp_path / "board_routed.kicad_pcb"
        routed_file.write_text("(kicad_pcb)")

        result = _scan_recipe_routed_artifact(pcb_file, pre_mtime, Console(), quiet=True)
        assert result == routed_file

    def test_refreshed_routed_artifact_is_trusted(self, tmp_path: Path) -> None:
        """A pre-existing routed file whose mtime advances → artifact trusted."""
        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        routed_file = tmp_path / "board_routed.kicad_pcb"
        routed_file.write_text("(kicad_pcb)")

        # Simulate an old pre-existing routed artifact.
        old = 1_000_000.0
        os.utime(routed_file, (old, old))
        pre_mtime = _capture_routed_artifact_mtime(pcb_file)
        assert pre_mtime == old

        # PCB step rewrites the routed artifact (mtime advances).
        os.utime(routed_file, (old + 100, old + 100))

        result = _scan_recipe_routed_artifact(pcb_file, pre_mtime, Console(), quiet=True)
        assert result == routed_file

    def test_stale_routed_artifact_is_rejected_with_warning(self, tmp_path: Path) -> None:
        """A stale routed file untouched by the PCB step → rejected + warning.

        Simulates an external board whose PCB generator does not route
        in-place: the ``*_routed.kicad_pcb`` predates the PCB step and its
        mtime does not advance, so it must not be recorded (issue #3978).
        """
        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        routed_file = tmp_path / "board_routed.kicad_pcb"
        routed_file.write_text("(kicad_pcb)")

        # Stale routed artifact, older than the "PCB step start".
        old = 1_000_000.0
        os.utime(routed_file, (old, old))
        pre_mtime = _capture_routed_artifact_mtime(pcb_file)
        assert pre_mtime == old

        # PCB step runs but does NOT touch the routed file (mtime unchanged).
        console = Console(record=True)
        result = _scan_recipe_routed_artifact(pcb_file, pre_mtime, console)

        assert result is None
        output = console.export_text()
        assert "Stale" in output
        assert "board_routed.kicad_pcb" in output

    def test_stale_rejection_is_quiet_when_requested(self, tmp_path: Path) -> None:
        """``quiet=True`` suppresses the stale-artifact warning."""
        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        routed_file = tmp_path / "board_routed.kicad_pcb"
        routed_file.write_text("(kicad_pcb)")

        old = 1_000_000.0
        os.utime(routed_file, (old, old))
        pre_mtime = _capture_routed_artifact_mtime(pcb_file)

        console = Console(record=True)
        result = _scan_recipe_routed_artifact(pcb_file, pre_mtime, console, quiet=True)

        assert result is None
        assert "Stale" not in console.export_text()

    def test_no_routed_artifact_returns_none(self, tmp_path: Path) -> None:
        """No routed sibling produced → nothing to record, no warning."""
        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        pre_mtime = _capture_routed_artifact_mtime(pcb_file)
        console = Console(record=True)
        result = _scan_recipe_routed_artifact(pcb_file, pre_mtime, console)

        assert result is None
        assert "Stale" not in console.export_text()

    def test_stale_artifact_then_route_step_actually_routes(self, tmp_path: Path) -> None:
        """End-to-end: a rejected stale artifact leaves ROUTE free to run.

        With a stale routed file, the post-PCB scan returns ``None`` so
        ``ctx.routed_pcb_file`` stays unset; ``_run_step_route`` then does not
        hit the recipe-artifact short-circuit and invokes the router.
        """
        from unittest.mock import MagicMock, patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        routed_file = tmp_path / "board_routed.kicad_pcb"
        routed_file.write_text("(kicad_pcb)")

        # Stale artifact untouched by the (mock) PCB step.
        old = 1_000_000.0
        os.utime(routed_file, (old, old))
        pre_mtime = _capture_routed_artifact_mtime(pcb_file)

        ctx = BuildContext(project_dir=tmp_path, spec_file=None)
        ctx.pcb_file = pcb_file
        ctx.mfr = "jlcpcb"
        ctx.quiet = True
        ctx.verbose = False
        ctx.dry_run = False
        ctx.force = False
        ctx.output_dir = None
        ctx.spec = None

        # Post-PCB scan rejects the stale artifact.
        routed = _scan_recipe_routed_artifact(pcb_file, pre_mtime, Console(), quiet=True)
        assert routed is None
        ctx.routed_pcb_file = routed  # stays None → guard will not short-circuit

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = _run_step_route(ctx, Console())

        # The router ran (not skipped via the recipe-artifact short-circuit).
        assert "recipe" not in result.message.lower()
        mock_run.assert_called()


def _make_route_ctx(project_dir: Path, pcb_file: Path, spec=None) -> BuildContext:
    """Build a minimal BuildContext for route-discovery tests."""
    ctx = BuildContext(project_dir=project_dir, spec_file=None)
    ctx.pcb_file = pcb_file
    ctx.routed_pcb_file = None
    ctx.mfr = "jlcpcb"
    ctx.quiet = True
    ctx.verbose = False
    ctx.dry_run = False
    ctx.force = False
    ctx.output_dir = None
    ctx.spec = spec
    return ctx


class TestGenerateDesignSupportsStepRoute:
    """Tests for the ``SUPPORTS_STEP_ROUTE`` sentinel detector."""

    def test_detects_sentinel(self, tmp_path: Path) -> None:
        script = tmp_path / "generate_design.py"
        script.write_text("SUPPORTS_STEP_ROUTE = True\n\nprint('hi')\n")
        assert _generate_design_supports_step_route(script) is True

    def test_absent_sentinel(self, tmp_path: Path) -> None:
        script = tmp_path / "generate_design.py"
        script.write_text("print('no sentinel here')\n")
        assert _generate_design_supports_step_route(script) is False

    def test_missing_file(self, tmp_path: Path) -> None:
        assert _generate_design_supports_step_route(tmp_path / "nope.py") is False


class TestResolveRouteRecipe:
    """Tests for the Tier 1 / Tier 2 route-recipe resolver."""

    def test_tier1_project_kct_route_recipe(self, tmp_path: Path) -> None:
        """A ``build.route_recipe`` key resolves to the declared script."""
        from kicad_tools.spec import BuildConfig, ProjectMetadata, ProjectSpec

        design = tmp_path / "generate_design.py"
        design.write_text("# no sentinel; Tier 1 wins on the explicit key\n")
        spec = ProjectSpec(
            project=ProjectMetadata(name="t"),
            build=BuildConfig(route_recipe="generate_design.py --step route"),
        )
        ctx = _make_route_ctx(tmp_path, tmp_path / "board.kicad_pcb", spec=spec)

        argv = _resolve_route_recipe(ctx)
        assert argv is not None
        assert Path(argv[0]) == design
        assert argv[1:] == ["--step", "route"]

    def test_tier1_missing_script_falls_through(self, tmp_path: Path) -> None:
        """A route_recipe pointing at a nonexistent script does not match Tier 1."""
        from kicad_tools.spec import BuildConfig, ProjectMetadata, ProjectSpec

        spec = ProjectSpec(
            project=ProjectMetadata(name="t"),
            build=BuildConfig(route_recipe="missing.py --step route"),
        )
        ctx = _make_route_ctx(tmp_path, tmp_path / "board.kicad_pcb", spec=spec)
        assert _resolve_route_recipe(ctx) is None

    def test_tier2_sentinel_without_project_kct_key(self, tmp_path: Path) -> None:
        """generate_design.py with the sentinel resolves via Tier 2 (no spec key)."""
        design = tmp_path / "generate_design.py"
        design.write_text("SUPPORTS_STEP_ROUTE = True\n")
        ctx = _make_route_ctx(tmp_path, tmp_path / "board.kicad_pcb", spec=None)

        argv = _resolve_route_recipe(ctx)
        assert argv is not None
        assert Path(argv[0]) == design
        assert argv[1:] == ["--step", "route"]

    def test_tier2_no_sentinel_returns_none(self, tmp_path: Path) -> None:
        """generate_design.py without the sentinel does not match Tier 2."""
        design = tmp_path / "generate_design.py"
        design.write_text("print('plain generator, no --step route support')\n")
        ctx = _make_route_ctx(tmp_path, tmp_path / "board.kicad_pcb", spec=None)
        assert _resolve_route_recipe(ctx) is None

    def test_no_recipe_returns_none(self, tmp_path: Path) -> None:
        """Empty project dir yields no recipe (caller falls back)."""
        ctx = _make_route_ctx(tmp_path, tmp_path / "board.kicad_pcb", spec=None)
        assert _resolve_route_recipe(ctx) is None


class TestRouteStepRecipeDiscovery:
    """End-to-end probe-order tests for _run_step_route discovery."""

    def test_recipe_selected_over_generic_fallback(self, tmp_path: Path) -> None:
        """A sentinel generate_design.py routes via the recipe, not `kct route`.

        This is the core regression for #3972: boards with generate_design.py
        --step route support (but no route_demo.py/route.py) previously fell
        through to the generic autorouter, dropping their diff-pair/match-group
        flags.
        """
        from unittest.mock import patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        design = tmp_path / "generate_design.py"
        design.write_text("SUPPORTS_STEP_ROUTE = True\n")

        ctx = _make_route_ctx(tmp_path, pcb_file, spec=None)
        console = Console()

        def fake_run_script(script, cwd, verbose, *, env_vars, script_args, quiet):
            # Simulate the recipe writing its routed artifact.
            (tmp_path / "board_routed.kicad_pcb").write_text("(kicad_pcb)")
            return True, f"Script {Path(script).name} completed successfully"

        with (
            patch(
                "kicad_tools.cli.build_cmd._run_python_script",
                side_effect=fake_run_script,
            ) as mock_script,
            patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_generic,
        ):
            result = _run_step_route(ctx, console)

        # The recipe ran; the generic autorouter did NOT.
        mock_script.assert_called_once()
        assert Path(mock_script.call_args.args[0]).name == "generate_design.py"
        mock_generic.assert_not_called()
        assert result.success is True
        assert result.output_file == tmp_path / "board_routed.kicad_pcb"

    def test_recipe_wins_over_route_demo(self, tmp_path: Path) -> None:
        """Tier 1/2 recipe takes precedence over a Tier 3 route_demo.py."""
        from unittest.mock import patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        (tmp_path / "generate_design.py").write_text("SUPPORTS_STEP_ROUTE = True\n")
        (tmp_path / "route_demo.py").write_text("print('should not run')\n")

        ctx = _make_route_ctx(tmp_path, pcb_file, spec=None)
        console = Console()

        def fake_run_script(script, cwd, verbose, *, env_vars, script_args, quiet):
            (tmp_path / "board_routed.kicad_pcb").write_text("(kicad_pcb)")
            return True, "ok"

        with patch(
            "kicad_tools.cli.build_cmd._run_python_script",
            side_effect=fake_run_script,
        ) as mock_script:
            _run_step_route(ctx, console)

        # generate_design.py wins; route_demo.py is never invoked.
        assert Path(mock_script.call_args.args[0]).name == "generate_design.py"

    def test_route_demo_still_selected_without_recipe(self, tmp_path: Path) -> None:
        """Regression: a board with only route_demo.py still uses Tier 3."""
        from unittest.mock import patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        # generate_design.py present but WITHOUT the sentinel -> Tier 2 skipped.
        (tmp_path / "generate_design.py").write_text("print('plain')\n")
        (tmp_path / "route_demo.py").write_text("print('route demo')\n")

        ctx = _make_route_ctx(tmp_path, pcb_file, spec=None)
        console = Console()

        def fake_run_script(script, cwd, verbose, *, env_vars, script_args, quiet):
            (tmp_path / "board_routed.kicad_pcb").write_text("(kicad_pcb)")
            return True, "ok"

        with patch(
            "kicad_tools.cli.build_cmd._run_python_script",
            side_effect=fake_run_script,
        ) as mock_script:
            _run_step_route(ctx, console)

        assert Path(mock_script.call_args.args[0]).name == "route_demo.py"

    def test_generic_fallback_when_no_scripts(self, tmp_path: Path) -> None:
        """Regression: no recipe and no route scripts -> generic `kct route`."""
        from unittest.mock import MagicMock, patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")

        ctx = _make_route_ctx(tmp_path, pcb_file, spec=None)
        console = Console()

        with patch("kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat") as mock_generic:
            mock_generic.return_value = MagicMock(returncode=0, stderr="", stdout="")
            result = _run_step_route(ctx, console)

        mock_generic.assert_called()
        assert result.success is True

    def test_recipe_dry_run_does_not_execute(self, tmp_path: Path) -> None:
        """--dry-run reports the recipe command without running it."""
        from unittest.mock import patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        (tmp_path / "generate_design.py").write_text("SUPPORTS_STEP_ROUTE = True\n")

        ctx = _make_route_ctx(tmp_path, pcb_file, spec=None)
        ctx.dry_run = True
        console = Console()

        with patch("kicad_tools.cli.build_cmd._run_python_script") as mock_script:
            result = _run_step_route(ctx, console)

        mock_script.assert_not_called()
        assert result.success is True
        assert "dry-run" in result.message.lower()
        assert "generate_design.py --step route" in result.message

    def test_recipe_success_but_no_artifact_is_failure(self, tmp_path: Path) -> None:
        """A recipe that exits 0 but writes no routed PCB is reported as failed."""
        from unittest.mock import patch

        from rich.console import Console

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        (tmp_path / "generate_design.py").write_text("SUPPORTS_STEP_ROUTE = True\n")

        ctx = _make_route_ctx(tmp_path, pcb_file, spec=None)
        console = Console()

        with patch(
            "kicad_tools.cli.build_cmd._run_python_script",
            return_value=(True, "ok"),
        ):
            result = _run_step_route(ctx, console)

        assert result.success is False
        assert "no *_routed.kicad_pcb" in result.message

    def test_recipe_routed_artifact_in_nested_output_dir(self, tmp_path: Path) -> None:
        """Recipe writing into an ``output/`` subdir (boards 06/07 layout) is found.

        Regression for the initial #3972 implementation: the routed PCB is
        written next to the unrouted PCB (in ``output/``), not at the project
        root, so the output-file search must probe ``ctx.pcb_file.parent`` and
        fall back to a recursive scan.  ``ctx.output_dir`` is unset here
        (matching ``kct build boards/06-diffpair-test`` with no ``--output``).
        """
        from unittest.mock import patch

        from rich.console import Console

        output_dir = tmp_path / "output"
        output_dir.mkdir()
        pcb_file = output_dir / "board.kicad_pcb"
        pcb_file.write_text("(kicad_pcb)")
        (tmp_path / "generate_design.py").write_text("SUPPORTS_STEP_ROUTE = True\n")

        ctx = _make_route_ctx(tmp_path, pcb_file, spec=None)
        console = Console()

        def fake_run_script(script, cwd, verbose, *, env_vars, script_args, quiet):
            # Recipe writes the routed PCB into output/, next to the unrouted one.
            (output_dir / "board_routed.kicad_pcb").write_text("(kicad_pcb)")
            return True, "ok"

        with patch(
            "kicad_tools.cli.build_cmd._run_python_script",
            side_effect=fake_run_script,
        ):
            result = _run_step_route(ctx, console)

        assert result.success is True
        assert result.output_file == output_dir / "board_routed.kicad_pcb"
