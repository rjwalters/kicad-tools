"""Tests for the build pipeline ERC step (issue #2742).

The build pipeline previously had no ``BuildStep.ERC`` entry, so:

1. ``kct build`` ran DRC + sync but never invoked ``kicad-cli sch erc``.
2. The export-time preflight (``export/preflight.py:_check_erc``) auto-
   searches for ``erc_report.json`` next to the schematic, found none,
   and emitted ``"No ERC report found; run kicad-cli first"``.

These tests pin the new behavior: ``_run_step_erc`` writes
``erc_report.json`` to ``ctx.output_dir`` (or the schematic's parent
when ``output_dir`` is unset) so the preflight discovers it.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from rich.console import Console

from kicad_tools.cli.build_cmd import (
    BuildContext,
    BuildStep,
    _run_step_erc,
    main,
)

# ---------------------------------------------------------------------------
# Enum + pipeline wiring
# ---------------------------------------------------------------------------


class TestBuildStepEnum:
    """The ERC enum entry and CLI choice are part of the public surface."""

    def test_erc_is_in_buildstep_enum(self) -> None:
        """A new BuildStep.ERC value exists and round-trips through str()."""
        assert BuildStep.ERC.value == "erc"
        assert BuildStep("erc") is BuildStep.ERC

    def test_erc_is_a_cli_step_choice(self) -> None:
        """``--step erc`` must be accepted by the argument parser.

        argparse rejects unknown choices with SystemExit(2); the rest of
        the build pipeline returns an int exit code instead. We just
        need to confirm that an unknown choice raises and "erc" does
        not.
        """
        # Unknown step value must be rejected.
        with pytest.raises(SystemExit) as exc_info:
            main(["--step", "this-step-does-not-exist", "/tmp"])
        assert exc_info.value.code == 2

        # Valid step value must be accepted (i.e. argparse does not
        # raise). Failure later in the pipeline returns a non-zero int.
        rc = main(["--step", "erc", "/nonexistent-project-path-for-test"])
        assert isinstance(rc, int)
        assert rc != 0  # bogus path -> error int, but not argparse rejection


# ---------------------------------------------------------------------------
# _run_step_erc behavior
# ---------------------------------------------------------------------------


@pytest.fixture
def schematic_file(tmp_path: Path) -> Path:
    """Create a minimal placeholder schematic file."""
    sch = tmp_path / "design.kicad_sch"
    sch.write_text("(kicad_sch (version 20240108) (generator test))\n")
    return sch


def _make_ctx(
    schematic: Path | None,
    output_dir: Path | None = None,
    dry_run: bool = False,
) -> BuildContext:
    return BuildContext(
        project_dir=Path("/tmp"),
        spec_file=None,
        schematic_file=schematic,
        output_dir=output_dir,
        dry_run=dry_run,
        quiet=True,
    )


class TestRunStepERC:
    """Tests for the new ``_run_step_erc`` pipeline step."""

    def test_missing_schematic_fails(self, tmp_path: Path) -> None:
        """No schematic in the context -> step fails with a clear message."""
        ctx = _make_ctx(schematic=None)
        result = _run_step_erc(ctx, Console(quiet=True))
        assert result.success is False
        assert "schematic" in result.message.lower()

    def test_schematic_file_does_not_exist_fails(self, tmp_path: Path) -> None:
        ctx = _make_ctx(schematic=tmp_path / "missing.kicad_sch")
        result = _run_step_erc(ctx, Console(quiet=True))
        assert result.success is False
        assert "schematic" in result.message.lower()

    def test_dry_run_reports_planned_command(self, schematic_file: Path) -> None:
        """Dry-run does not actually invoke kicad-cli."""
        ctx = _make_ctx(schematic=schematic_file, dry_run=True)
        result = _run_step_erc(ctx, Console(quiet=True))
        assert result.success is True
        assert "dry-run" in result.message.lower()
        # The planned output path is exposed for downstream visibility.
        assert result.output_file is not None
        assert result.output_file.name == "erc_report.json"

    def test_writes_report_to_schematic_parent_by_default(
        self, schematic_file: Path, tmp_path: Path
    ) -> None:
        """Without ctx.output_dir, the report lands next to the schematic.

        ``export/preflight.py:_check_erc`` auto-discovers ``erc_report.json``
        in the schematic's parent, so this is the path that closes the
        "No ERC report found" warning loop.
        """
        ctx = _make_ctx(schematic=schematic_file)

        # Fabricate a successful kicad-cli result that drops a parseable
        # report at the expected path.
        report_path = schematic_file.parent / "erc_report.json"
        report_payload = {
            "source": str(schematic_file),
            "violations": [],
            "coordinate_units": "mm",
        }

        def fake_run_erc(sch, output_path, **kwargs):
            output_path.write_text(json.dumps(report_payload))
            return MagicMock(success=True, output_path=output_path, stderr="")

        with (
            patch(
                "kicad_tools.cli.runner.find_kicad_cli",
                return_value=Path("/usr/bin/kicad-cli"),
            ),
            patch("kicad_tools.cli.runner.run_erc", side_effect=fake_run_erc),
        ):
            result = _run_step_erc(ctx, Console(quiet=True))

        assert result.success is True, result.message
        assert report_path.exists(), (
            f"ERC report should land at {report_path} so export preflight can auto-discover it"
        )
        assert result.output_file == report_path

    def test_writes_report_to_ctx_output_dir_when_set(
        self, schematic_file: Path, tmp_path: Path
    ) -> None:
        """``ctx.output_dir`` is preferred over the schematic's parent."""
        output_dir = tmp_path / "build-output"
        ctx = _make_ctx(schematic=schematic_file, output_dir=output_dir)

        def fake_run_erc(sch, output_path, **kwargs):
            output_path.write_text(json.dumps({"violations": []}))
            return MagicMock(success=True, output_path=output_path, stderr="")

        with (
            patch(
                "kicad_tools.cli.runner.find_kicad_cli",
                return_value=Path("/usr/bin/kicad-cli"),
            ),
            patch("kicad_tools.cli.runner.run_erc", side_effect=fake_run_erc),
        ):
            result = _run_step_erc(ctx, Console(quiet=True))

        assert result.success is True, result.message
        expected = output_dir / "erc_report.json"
        assert expected.exists()
        assert result.output_file == expected

    def test_kicad_cli_unavailable_is_non_blocking(self, schematic_file: Path) -> None:
        """When kicad-cli is not installed, the step warns but does not fail.

        Failing here would block every developer machine without KiCad
        installed; the export preflight will subsequently emit its own
        "no report" warning, which is the existing pre-fix behavior.
        """
        ctx = _make_ctx(schematic=schematic_file)
        with patch("kicad_tools.cli.runner.find_kicad_cli", return_value=None):
            result = _run_step_erc(ctx, Console(quiet=True))
        assert result.success is True
        assert "kicad-cli" in result.message.lower()
