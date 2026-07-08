"""Tests for the VERIFY step's exit-code -> message mapping (issue #3970).

Bug B in issue #3970: ``_run_step_verify`` mapped *every* nonzero ``kct check``
exit to ``"DRC found issues"``.  Exit 2 from an ``INCOMPLETE`` meta-check
rollup (a sub-check such as the manufacturing manifest is ``NOT RUN``) is not a
DRC failure, and reporting it as one produced a self-contradicting log line
(``DRC: PASSED`` alongside ``verify: DRC found issues``).
"""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import patch

from rich.console import Console

from kicad_tools.cli.build_cmd import (
    BuildContext,
    _classify_verify_result,
    _run_step_verify,
)


class TestClassifyVerifyResult:
    """Unit tests for the pure exit-code -> message classifier."""

    def test_exit_zero_is_drc_passed(self) -> None:
        assert _classify_verify_result(0, "Overall: PASSED") == "DRC passed"

    def test_exit_zero_ignores_stdout(self) -> None:
        # A clean exit wins regardless of stdout noise.
        assert _classify_verify_result(0, "") == "DRC passed"

    def test_exit_one_is_tool_error(self) -> None:
        assert _classify_verify_result(1, "file not found") == "Verification tool error"

    def test_exit_two_incomplete_rollup_is_manifest_message(self) -> None:
        stdout = (
            "DRC:      PASSED\n"
            "ERC:      PASSED\n"
            "LVS:      PASSED\n"
            "Manifest: NOT RUN\n"
            "Overall:  INCOMPLETE\n"
        )
        message = _classify_verify_result(2, stdout)
        assert "DRC found issues" not in message
        assert "Manifest" in message

    def test_exit_two_not_run_token_alone_is_manifest_message(self) -> None:
        # Even without the word INCOMPLETE, a NOT RUN sub-check should not be
        # attributed to DRC.
        message = _classify_verify_result(2, "Manifest: NOT RUN")
        assert "DRC found issues" not in message
        assert "Manifest" in message

    def test_exit_two_real_drc_failure_is_drc_found_issues(self) -> None:
        stdout = "DRC:      FAILED\n  clearance violation: 0.15mm < 0.20mm\nOverall:  FAILED\n"
        assert _classify_verify_result(2, stdout) == "DRC found issues"

    def test_exit_two_empty_stdout_defaults_to_drc_found_issues(self) -> None:
        assert _classify_verify_result(2, "") == "DRC found issues"


class TestRunStepVerifyMessages:
    """Integration tests: _run_step_verify wired to a mocked subprocess."""

    def _make_ctx(self, tmp_path: Path) -> tuple[BuildContext, Path]:
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            pcb_file=pcb,
            routed_pcb_file=None,
            # No schematic -> skip the sync sub-check so we isolate the DRC
            # message under test.
            schematic_file=None,
            quiet=True,
        )
        return ctx, pcb

    def test_incomplete_rollup_reports_manifest_not_drc(self, tmp_path: Path) -> None:
        ctx, _pcb = self._make_ctx(tmp_path)
        console = Console(quiet=True)

        fake = subprocess.CompletedProcess(
            args=["kct", "check"],
            returncode=2,
            stdout="Manifest: NOT RUN\nOverall:  INCOMPLETE\n",
            stderr="",
        )
        with patch(
            "kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat",
            return_value=fake,
        ):
            result = _run_step_verify(ctx, console)

        assert not result.success
        assert "DRC found issues" not in result.message
        assert "Manifest" in result.message

    def test_clean_check_reports_drc_passed(self, tmp_path: Path) -> None:
        ctx, _pcb = self._make_ctx(tmp_path)
        console = Console(quiet=True)

        fake = subprocess.CompletedProcess(
            args=["kct", "check"],
            returncode=0,
            stdout="Overall: PASSED\n",
            stderr="",
        )
        with patch(
            "kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat",
            return_value=fake,
        ):
            result = _run_step_verify(ctx, console)

        assert result.success
        assert "DRC passed" in result.message

    def test_real_drc_failure_reports_drc_found_issues(self, tmp_path: Path) -> None:
        ctx, _pcb = self._make_ctx(tmp_path)
        console = Console(quiet=True)

        fake = subprocess.CompletedProcess(
            args=["kct", "check"],
            returncode=2,
            stdout="DRC: FAILED\nOverall: FAILED\n",
            stderr="",
        )
        with patch(
            "kicad_tools.cli.build_cmd._run_subprocess_with_heartbeat",
            return_value=fake,
        ):
            result = _run_step_verify(ctx, console)

        assert not result.success
        assert "DRC found issues" in result.message
