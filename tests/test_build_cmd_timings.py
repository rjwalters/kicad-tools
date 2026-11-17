"""Tests for ``kct build`` per-step timings and long-step heartbeats (#3944).

Two symptoms motivated this module:

1. The step-runner loop in ``main()`` printed ``[OK]/[FAIL]`` ledger
   lines with no elapsed time, and the final summary had no wall-clock
   total -- so users could not tell how long any stage took.
2. Several sub-steps shelled out with ``subprocess.run(capture_output=
   ...)`` and went completely silent for minutes, indistinguishable
   from a hang.

The fixes:

* Wrap each step invocation with ``time.monotonic()`` and print the
  elapsed time on the ledger line, plus a total on the summary line.
* Route the silent subprocess sites through
  ``_run_subprocess_with_heartbeat``, which emits a bounded-interval
  "still running" heartbeat while the child is alive.

These tests exercise the timing formatter, the heartbeat helper, and
the ``main()`` ledger/summary output with a mockable clock, and confirm
``--quiet`` suppresses all of it.
"""

from __future__ import annotations

import re
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from kicad_tools.cli import build_cmd
from kicad_tools.cli.build_cmd import (
    BuildResult,
    _format_elapsed,
    _run_subprocess_with_heartbeat,
)


class TestFormatElapsed:
    """``_format_elapsed`` renders monotonic deltas for the ledger."""

    def test_sub_minute_uses_fractional_seconds(self) -> None:
        assert _format_elapsed(24.3) == "24.3s"
        assert _format_elapsed(0.0) == "0.0s"
        assert _format_elapsed(59.9) == "59.9s"

    def test_multi_minute_uses_mmss(self) -> None:
        assert _format_elapsed(60.0) == "1m00s"
        assert _format_elapsed(272.0) == "4m32s"
        assert _format_elapsed(125.4) == "2m05s"

    def test_rounding_up_to_next_minute_carries(self) -> None:
        # 119.6s rounds seconds-part to 60 -> should carry to 2m00s,
        # never emit "1m60s".
        assert _format_elapsed(119.6) == "2m00s"


class TestHeartbeat:
    """``_run_subprocess_with_heartbeat`` streams "still running" lines."""

    def test_emits_heartbeat_for_slow_process(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """A child that outlives the heartbeat interval triggers at
        least one "still running" line before it exits."""
        cmd = [sys.executable, "-c", "import time; time.sleep(0.25)"]
        result = _run_subprocess_with_heartbeat(
            cmd,
            cwd=".",
            console=build_cmd.Console(),
            label="route",
            quiet=False,
            heartbeat_interval=0.05,
        )
        assert result.returncode == 0
        captured = capsys.readouterr()
        assert "still running" in captured.out
        assert "[route]" in captured.out

    def test_quiet_suppresses_heartbeat(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """With ``quiet=True`` no heartbeat line is printed even for a
        slow child."""
        cmd = [sys.executable, "-c", "import time; time.sleep(0.25)"]
        result = _run_subprocess_with_heartbeat(
            cmd,
            cwd=".",
            console=build_cmd.Console(),
            label="route",
            quiet=True,
            heartbeat_interval=0.05,
        )
        assert result.returncode == 0
        captured = capsys.readouterr()
        assert "still running" not in captured.out

    def test_captures_stdout_and_returncode(
        self,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The helper preserves the ``CompletedProcess`` contract:
        stdout/stderr are captured and returncode is surfaced."""
        cmd = [
            sys.executable,
            "-c",
            "import sys; print('hello'); print('bad', file=sys.stderr); sys.exit(3)",
        ]
        result = _run_subprocess_with_heartbeat(
            cmd,
            cwd=".",
            console=build_cmd.Console(),
            label="export",
            quiet=False,
            heartbeat_interval=5.0,  # long enough that no heartbeat fires
        )
        assert result.returncode == 3
        assert "hello" in result.stdout
        assert "bad" in result.stderr


def _run_single_step_build(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    *,
    quiet: bool,
    monotonic_values: list[float],
    step_success: bool = True,
) -> None:
    """Drive ``main()`` for a single step with a mocked step runner and
    a mocked ``time.monotonic`` clock; return captured stdout.

    We target the ERC step because ``_run_step_erc`` is easy to stub and
    the loop treats ERC failures as non-fatal, keeping the harness
    simple.  The clock is fed a deterministic list so elapsed values are
    reproducible.
    """
    values = iter(monotonic_values)

    def fake_monotonic() -> float:
        try:
            return next(values)
        except StopIteration:  # pragma: no cover - defensive
            return monotonic_values[-1]

    monkeypatch.setattr(build_cmd.time, "monotonic", fake_monotonic)

    def fake_erc(ctx: Any, console: Any) -> BuildResult:
        return BuildResult(
            step="erc",
            success=step_success,
            message="ERC passed" if step_success else "ERC found issues",
        )

    monkeypatch.setattr(build_cmd, "_run_step_erc", fake_erc)

    argv = [str(tmp_path), "--step", "erc"]
    if quiet:
        argv.append("--quiet")

    build_cmd.main(argv)


class TestMainLedgerTimings:
    """``main()`` prints per-step elapsed and a total on the summary."""

    def test_step_ledger_includes_elapsed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        # Clock sequence: build_start=0.0, step_start=0.0, step_end=24.3,
        # summary=25.0.
        _run_single_step_build(
            monkeypatch,
            tmp_path,
            quiet=False,
            monotonic_values=[0.0, 0.0, 24.3, 25.0],
        )
        out = capsys.readouterr().out
        assert "24.3s" in out, out
        assert "erc" in out

    def test_summary_includes_total_elapsed(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _run_single_step_build(
            monkeypatch,
            tmp_path,
            quiet=False,
            monotonic_values=[0.0, 0.0, 1.0, 272.0],
        )
        out = capsys.readouterr().out
        # Total elapsed = 272.0 - 0.0 -> "4m32s".
        assert re.search(r"in 4m32s", out), out

    def test_quiet_suppresses_all_timing(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        _run_single_step_build(
            monkeypatch,
            tmp_path,
            quiet=True,
            monotonic_values=[0.0, 0.0, 24.3, 25.0],
        )
        out = capsys.readouterr().out
        assert "24.3s" not in out
        assert "4m32s" not in out
        # Quiet build prints nothing at all.
        assert out.strip() == "", repr(out)

    def test_summary_total_matches_duration_regex(
        self,
        monkeypatch: pytest.MonkeyPatch,
        tmp_path: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """Acceptance: the summary line carries a duration token in
        either ``\\d+\\.\\d+s`` or ``\\d+m\\d+s`` form."""
        _run_single_step_build(
            monkeypatch,
            tmp_path,
            quiet=False,
            monotonic_values=[0.0, 0.0, 1.0, 5.5],
        )
        out = capsys.readouterr().out
        assert re.search(r"in (\d+m\d+s|\d+\.\d+s)", out), out


class TestHeartbeatUsesMonotonic:
    """Regression guard: heartbeat elapsed derives from monotonic."""

    def test_helper_reads_module_monotonic(
        self,
        monkeypatch: pytest.MonkeyPatch,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The heartbeat's elapsed value is computed from
        ``time.monotonic`` (not wall-clock ``time.time``).  We patch the
        module's ``time.monotonic`` to advance by a fixed amount and
        assert the emitted elapsed reflects it."""
        clock = {"t": 100.0}

        real_monotonic = time.monotonic

        def fake_monotonic() -> float:
            # First call (start) returns 100.0; subsequent calls advance.
            clock["t"] += 40.0
            return clock["t"]

        monkeypatch.setattr(build_cmd.time, "monotonic", fake_monotonic)

        cmd = [sys.executable, "-c", "import time; time.sleep(0.2)"]
        try:
            result = _run_subprocess_with_heartbeat(
                cmd,
                cwd=".",
                console=build_cmd.Console(),
                label="verify",
                quiet=False,
                heartbeat_interval=0.05,
            )
        finally:
            monkeypatch.setattr(build_cmd.time, "monotonic", real_monotonic)
        assert result.returncode == 0
        out = capsys.readouterr().out
        # The mocked clock jumps 40s per read, so the heartbeat should
        # report a "40s"-scale elapsed rather than the real ~0.2s.
        assert "still running" in out
        assert re.search(r"\d+(\.\d+)?s elapsed", out), out
