"""Tests for the per-step kicad-cli smoke check in :mod:`kicad_tools.cli.build_cmd`.

The smoke check runs ``kicad-cli pcb drc --schematic-parity off`` after
every PCB-write step and surfaces "Failed to load board" rejections at
the writer that produced them, rather than letting them surface much
later at the EXPORT step.

These tests stub :func:`kicad_tools.cli.runner.find_kicad_cli` and
:func:`kicad_tools.cli.runner.run_drc` (re-exported via the
``kicad_tools.cli.build_cmd`` module's import-from-runner pattern) so
that they exercise the helper logic without ever invoking a real
``kicad-cli`` binary.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from rich.console import Console

from kicad_tools.cli import build_cmd
from kicad_tools.cli.build_cmd import (
    _PCB_WRITE_STEPS,
    BuildContext,
    BuildResult,
    BuildStep,
    _smoke_check_pcb,
)
from kicad_tools.cli.runner import KiCadCLIResult


def _make_ctx(tmp_path: Path) -> BuildContext:
    """Construct a minimal BuildContext for smoke-check tests."""
    return BuildContext(project_dir=tmp_path, spec_file=None)


def _make_pcb(tmp_path: Path, name: str = "board.kicad_pcb") -> Path:
    """Create a stub PCB file on disk."""
    pcb = tmp_path / name
    pcb.write_text("(kicad_pcb)\n")
    return pcb


class _StubRunDRC:
    """Callable that records calls and returns a stubbed KiCadCLIResult.

    Mimics ``kicad_tools.cli.runner.run_drc`` for monkeypatching.
    """

    def __init__(self, result: KiCadCLIResult) -> None:
        self.result = result
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self,
        pcb_path: Path,
        output_path: Path | None = None,
        format: str = "json",
        schematic_parity: bool = True,
        kicad_cli: Path | None = None,
    ) -> KiCadCLIResult:
        self.calls.append(
            {
                "pcb_path": pcb_path,
                "output_path": output_path,
                "format": format,
                "schematic_parity": schematic_parity,
                "kicad_cli": kicad_cli,
            }
        )
        # Reflect the requested output_path back into the result (so
        # callers that inspect ``result.output_path`` get a sane value),
        # but don't *create* the file — the caller cleans it up via
        # ``Path.unlink(missing_ok=True)``.
        if output_path is not None and self.result.output_path is None:
            return KiCadCLIResult(
                success=self.result.success,
                output_path=output_path,
                stdout=self.result.stdout,
                stderr=self.result.stderr,
                return_code=self.result.return_code,
            )
        return self.result


def _patch_kicad_cli(
    monkeypatch: pytest.MonkeyPatch,
    *,
    cli_path: Path | None,
    drc_result: KiCadCLIResult | None = None,
) -> _StubRunDRC | None:
    """Patch ``find_kicad_cli`` and ``run_drc`` inside build_cmd.

    The helper imports both lazily inside ``_smoke_check_pcb``, so we
    must patch them on the originating module (``kicad_tools.cli.runner``).
    """
    monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda: cli_path)

    if drc_result is None:
        return None

    stub = _StubRunDRC(drc_result)
    monkeypatch.setattr("kicad_tools.cli.runner.run_drc", stub)
    return stub


class TestSmokeCheckHelper:
    """Tests for :func:`_smoke_check_pcb`."""

    def test_returns_none_on_valid_pcb(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """When kicad-cli loads the PCB cleanly the helper returns None."""
        pcb = _make_pcb(tmp_path)
        ctx = _make_ctx(tmp_path)
        stub = _patch_kicad_cli(
            monkeypatch,
            cli_path=Path("/fake/kicad-cli"),
            drc_result=KiCadCLIResult(
                success=True,
                stdout="DRC complete",
                stderr="",
                return_code=0,
            ),
        )
        assert stub is not None  # for type narrowing

        console = Console(quiet=True)
        result = _smoke_check_pcb(pcb, "pcb", console, ctx)
        assert result is None
        # The check should have happened with schematic_parity disabled.
        assert len(stub.calls) == 1
        assert stub.calls[0]["schematic_parity"] is False

    def test_returns_failure_on_load_error(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A "Failed to load board" stderr produces a failed BuildResult
        whose message attributes the failure to the producing step."""
        pcb = _make_pcb(tmp_path, "silkscreen_out.kicad_pcb")
        ctx = _make_ctx(tmp_path)
        _patch_kicad_cli(
            monkeypatch,
            cli_path=Path("/fake/kicad-cli"),
            drc_result=KiCadCLIResult(
                success=True,  # report file produced even on load failure
                stdout="",
                stderr="IO_ERROR: Failed to load board: bad token at line 1",
                return_code=3,
            ),
        )

        console = Console(quiet=True)
        result = _smoke_check_pcb(pcb, "silkscreen", console, ctx)

        assert isinstance(result, BuildResult)
        assert result.success is False
        assert result.step == "silkscreen"
        assert "silkscreen" in result.message
        assert "rejected by kicad-cli" in result.message
        assert "Failed to load board" in result.message
        # The hint should point the user at the broken file.
        assert f"head -10 {pcb}" in result.message

    def test_ignores_drc_violations(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Real DRC rule violations exit non-zero but lack the load-failure
        marker; the smoke check must NOT misattribute them to the writer."""
        pcb = _make_pcb(tmp_path)
        ctx = _make_ctx(tmp_path)
        _patch_kicad_cli(
            monkeypatch,
            cli_path=Path("/fake/kicad-cli"),
            drc_result=KiCadCLIResult(
                success=True,
                stdout="DRC found 4 violations",
                stderr="",
                return_code=5,  # non-zero, but no load-failure marker
            ),
        )

        console = Console(quiet=True)
        result = _smoke_check_pcb(pcb, "route", console, ctx)
        assert result is None

    def test_skipped_when_kicad_cli_missing(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When kicad-cli is not installed the helper returns None and
        prints a warning (without invoking run_drc)."""
        pcb = _make_pcb(tmp_path)
        ctx = _make_ctx(tmp_path)

        # Sentinel for run_drc — must NOT be called.
        called = {"count": 0}

        def _never(*_args: Any, **_kwargs: Any) -> KiCadCLIResult:
            called["count"] += 1
            return KiCadCLIResult(success=False)

        monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda: None)
        monkeypatch.setattr("kicad_tools.cli.runner.run_drc", _never)

        # Use a non-quiet console so the warning is captured.
        console = Console()
        result = _smoke_check_pcb(pcb, "pcb", console, ctx)

        assert result is None
        assert called["count"] == 0
        out = capsys.readouterr().out
        assert "kicad-cli not installed" in out
        assert ctx._kicad_cli_warning_emitted is True

    def test_warning_emitted_only_once(
        self,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Repeated calls with kicad-cli missing must print the warning
        exactly once per build."""
        pcb = _make_pcb(tmp_path)
        ctx = _make_ctx(tmp_path)
        monkeypatch.setattr("kicad_tools.cli.runner.find_kicad_cli", lambda: None)

        console = Console()
        for _ in range(3):
            assert _smoke_check_pcb(pcb, "pcb", console, ctx) is None

        out = capsys.readouterr().out
        # The warning string should appear exactly once.
        assert out.count("kicad-cli not installed") == 1


class TestPipelineIntegration:
    """Tests for smoke-check integration with the build pipeline."""

    def test_pcb_write_steps_constant_excludes_terminal_steps(self) -> None:
        """The constant covers PCB-emitting steps and skips the rest."""
        # PCB-write steps that should be smoke-checked.
        assert BuildStep.PCB in _PCB_WRITE_STEPS
        assert BuildStep.OUTLINE in _PCB_WRITE_STEPS
        assert BuildStep.PLACEMENT in _PCB_WRITE_STEPS
        assert BuildStep.ZONES in _PCB_WRITE_STEPS
        assert BuildStep.SILKSCREEN in _PCB_WRITE_STEPS
        assert BuildStep.ROUTE in _PCB_WRITE_STEPS
        # Schematic produces no PCB; verify is read-only;
        # export is terminal and surfaces kicad-cli errors directly.
        assert BuildStep.SCHEMATIC not in _PCB_WRITE_STEPS
        assert BuildStep.VERIFY not in _PCB_WRITE_STEPS
        assert BuildStep.EXPORT not in _PCB_WRITE_STEPS

    def test_smoke_check_skipped_on_dry_run(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """In ``--dry-run`` mode the pipeline must not call the smoke
        check (no PCB was actually written).

        We exercise this by monkeypatching ``_smoke_check_pcb`` to a
        sentinel that records every call, then running ``main()``
        against a project directory that has no generators (so all
        steps fail-soft with skip messages and no PCB is produced).
        """
        # Set up a project dir with neither a spec nor any generator
        # script — every step will succeed with a "skipping..." message.
        # Also write a dummy *_routed.kicad_pcb so the export step can
        # find something to export (its own subprocess call will be
        # stubbed anyway).
        pcb = _make_pcb(tmp_path, "test_routed.kicad_pcb")
        sch = tmp_path / "test.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        called = {"count": 0}

        def _record(*_args: Any, **_kwargs: Any) -> None:
            called["count"] += 1
            return None

        monkeypatch.setattr(build_cmd, "_smoke_check_pcb", _record)

        # Stub kicad-cli detection to a fake path so the smoke check
        # would otherwise run.
        _patch_kicad_cli(
            monkeypatch,
            cli_path=Path("/fake/kicad-cli"),
            drc_result=KiCadCLIResult(success=True, return_code=0),
        )

        # Run main with --dry-run.
        rc = build_cmd.main(
            [
                str(tmp_path),
                "--step",
                "pcb",
                "--dry-run",
                "--quiet",
            ]
        )

        # Regardless of return code, the smoke check should NOT have run.
        assert called["count"] == 0, "smoke check must be skipped on --dry-run"
        # Use the rc to silence "unused" hints; main returns 0 or 1 depending
        # on environment but neither outcome should invoke the smoke check.
        assert rc in (0, 1)
        # Reference pcb to keep static analyzers from flagging the helper.
        assert pcb.exists()

    def test_no_smoke_check_flag_disables_helper(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """``--no-smoke-check`` opts out of all per-step smoke checks."""
        pcb = _make_pcb(tmp_path, "demo_routed.kicad_pcb")
        sch = tmp_path / "demo.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        called = {"count": 0}

        def _record(*_args: Any, **_kwargs: Any) -> None:
            called["count"] += 1
            return None

        monkeypatch.setattr(build_cmd, "_smoke_check_pcb", _record)

        rc = build_cmd.main(
            [
                str(tmp_path),
                "--step",
                "pcb",
                "--no-smoke-check",
                "--quiet",
            ]
        )

        assert called["count"] == 0
        assert rc in (0, 1)
        assert pcb.exists()

    def test_pipeline_breaks_on_smoke_check_failure(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A smoke-check failure must halt the pipeline before later
        steps run, returning exit code 1."""
        # An existing PCB lets ``_run_step_pcb`` short-circuit to
        # success when no generator script is present.
        _make_pcb(tmp_path, "demo.kicad_pcb")
        sch = tmp_path / "demo.kicad_sch"
        sch.write_text("(kicad_sch)\n")

        # The smoke check returns a failure on the first call.
        def _fail(
            pcb_path: Path,
            producing_step: str,
            _console: Console,
            _ctx: BuildContext,
        ) -> BuildResult | None:
            return BuildResult(
                step=producing_step,
                success=False,
                message=f"Output of '{producing_step}' rejected by kicad-cli",
                output_file=pcb_path,
            )

        monkeypatch.setattr(build_cmd, "_smoke_check_pcb", _fail)

        # Running step=pcb only (so we can directly observe the
        # smoke check halting after that single step).  Note: the
        # build_cmd ``main`` only returns non-zero from the summary
        # block, which is *inside* ``if not args.quiet`` — so we
        # deliberately do NOT pass --quiet here.
        rc = build_cmd.main(
            [
                str(tmp_path),
                "--step",
                "pcb",
            ]
        )

        # Either the step itself succeeds and the smoke check fails
        # (rc=1), or the step fails on its own (also rc=1).  In both
        # cases the contract is that we exit non-zero.
        assert rc == 1
