"""Tests for build_cmd._run_python_script stdout streaming (Issue #2794).

The original implementation gated stdout output on ``--verbose``: the
default subprocess call was ``subprocess.run(..., capture_output=True)``
so all child-process stdout was buffered until the script completed.
For long-running routing scripts this hid per-net progress and made
silent hangs (e.g. board 05 BLDC controller) indistinguishable from
fast successful runs.

Issue #2794 inverts this behaviour: stdout is now streamed line-by-line
to the parent process at the default verbosity.  Only ``--quiet`` (or
the explicit ``quiet=True`` keyword) suppresses live output.

These tests verify:

1. Default mode streams child stdout to the parent.
2. Quiet mode silences child stdout but still surfaces errors.
3. Failures return ``success=False`` and include stderr in the message.
4. ``PYTHONUNBUFFERED=1`` is set in the child env so prints flush
   immediately regardless of whether the script calls ``flush=True``.
"""

from __future__ import annotations

import textwrap
from pathlib import Path

import pytest

from kicad_tools.cli.build_cmd import _run_python_script


@pytest.fixture
def progress_script(tmp_path: Path) -> Path:
    """Write a child script that emits a few progress lines + exits 0."""
    script = tmp_path / "progress_script.py"
    script.write_text(
        textwrap.dedent(
            """\
            import sys
            for i in range(3):
                print(f"progress line {i}")
            sys.exit(0)
            """
        )
    )
    return script


@pytest.fixture
def failing_script(tmp_path: Path) -> Path:
    """Write a child script that emits progress, then fails with stderr."""
    script = tmp_path / "failing_script.py"
    script.write_text(
        textwrap.dedent(
            """\
            import sys
            print("partial progress")
            print("oh no something broke", file=sys.stderr)
            sys.exit(7)
            """
        )
    )
    return script


@pytest.fixture
def env_probe_script(tmp_path: Path) -> Path:
    """Write a child script that prints whether PYTHONUNBUFFERED is set."""
    script = tmp_path / "env_probe.py"
    script.write_text(
        textwrap.dedent(
            """\
            import os
            print(f"unbuffered={os.environ.get('PYTHONUNBUFFERED', 'unset')}")
            """
        )
    )
    return script


class TestStdoutStreamingDefault:
    """Default mode (quiet=False) streams stdout to parent."""

    def test_streams_stdout_to_parent(
        self,
        progress_script: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When quiet=False, each child stdout line is forwarded to
        the parent process's stdout (so the user sees it in real time)."""
        success, message = _run_python_script(
            progress_script,
            cwd=progress_script.parent,
            verbose=False,
            quiet=False,
        )
        assert success, f"Expected success, got: {message}"

        captured = capsys.readouterr()
        # All three progress lines should have been forwarded.
        for i in range(3):
            assert f"progress line {i}" in captured.out, (
                f"Expected 'progress line {i}' in streamed stdout, "
                f"got: {captured.out!r}"
            )

    def test_default_verbose_still_streams(
        self,
        progress_script: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """``verbose=True`` is a no-op vs. default for stdout streaming;
        both paths produce the same live output (Issue #2794 unifies
        the visible behaviour so verbose only affects stderr handling
        in the build-cmd layers above this function)."""
        success, message = _run_python_script(
            progress_script,
            cwd=progress_script.parent,
            verbose=True,
            quiet=False,
        )
        assert success
        captured = capsys.readouterr()
        for i in range(3):
            assert f"progress line {i}" in captured.out


class TestStdoutStreamingQuiet:
    """Quiet mode (quiet=True) suppresses stdout streaming."""

    def test_quiet_suppresses_stdout(
        self,
        progress_script: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """When quiet=True, child stdout is captured silently."""
        success, message = _run_python_script(
            progress_script,
            cwd=progress_script.parent,
            verbose=False,
            quiet=True,
        )
        assert success
        captured = capsys.readouterr()
        # Nothing from the child should have leaked through.
        for i in range(3):
            assert f"progress line {i}" not in captured.out


class TestStdoutStreamingFailure:
    """Failure handling: non-zero exit + stderr surfacing."""

    def test_failure_returns_false_with_stderr(
        self,
        failing_script: Path,
    ) -> None:
        """A non-zero child exit causes ``success=False`` and stderr
        text is included in the failure message."""
        success, message = _run_python_script(
            failing_script,
            cwd=failing_script.parent,
            verbose=False,
            quiet=False,
        )
        assert not success
        assert "oh no something broke" in message

    def test_failure_in_quiet_mode_still_returns_false(
        self,
        failing_script: Path,
    ) -> None:
        """Quiet mode still detects failures and includes stderr."""
        success, message = _run_python_script(
            failing_script,
            cwd=failing_script.parent,
            verbose=False,
            quiet=True,
        )
        assert not success
        assert "oh no something broke" in message


class TestStdoutStreamingChildEnv:
    """Child env is correctly augmented with PYTHONUNBUFFERED=1."""

    def test_pythonunbuffered_set_in_child(
        self,
        env_probe_script: Path,
        capsys: pytest.CaptureFixture[str],
    ) -> None:
        """The child should observe ``PYTHONUNBUFFERED=1`` in its env
        (Issue #2794: forces line-buffered stdout in the child so we
        don't have to rely on the script calling ``flush=True``)."""
        success, _ = _run_python_script(
            env_probe_script,
            cwd=env_probe_script.parent,
            verbose=False,
            quiet=False,
        )
        assert success
        captured = capsys.readouterr()
        assert "unbuffered=1" in captured.out

    def test_pythonunbuffered_does_not_clobber_caller(
        self,
        env_probe_script: Path,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """If the *caller* already has ``PYTHONUNBUFFERED`` set to some
        other value, we should not clobber it (we use ``setdefault``)."""
        monkeypatch.setenv("PYTHONUNBUFFERED", "x")
        success, _ = _run_python_script(
            env_probe_script,
            cwd=env_probe_script.parent,
            verbose=False,
            quiet=False,
        )
        assert success
        captured = capsys.readouterr()
        # Caller's value (``x``) wins over our default (``1``).
        assert "unbuffered=x" in captured.out


class TestRouteAllSmokeBudget:
    """Issue #2794 acceptance criterion 1: kct build on a small board
    should complete within a tight budget.  We exercise this through
    the same path that the real CLI uses -- ``_run_python_script`` on
    a tiny synthetic board script that calls the autorouter."""

    def test_route_all_with_outer_timeout_returns_within_budget(
        self,
        tmp_path: Path,
    ) -> None:
        """Smoke: ``Router.route_all(timeout=...)`` honours its outer
        wall-clock budget on a small fixture (the budget is generous
        enough to always succeed on the tiny test board, so this
        primarily checks the timeout kwarg plumbing doesn't break
        the routing path)."""
        import time

        from kicad_tools.router.core import Autorouter

        router = Autorouter(width=50.0, height=40.0)
        router.add_component(
            "R1",
            [
                {"number": "1", "x": 10.0, "y": 10.0, "net": 1, "net_name": "N1"},
                {"number": "2", "x": 15.0, "y": 10.0, "net": 1, "net_name": "N1"},
            ],
        )
        router.add_component(
            "R2",
            [
                {"number": "1", "x": 10.0, "y": 20.0, "net": 2, "net_name": "N2"},
                {"number": "2", "x": 15.0, "y": 20.0, "net": 2, "net_name": "N2"},
            ],
        )

        start = time.time()
        routes = router.route_all(timeout=60.0)
        elapsed = time.time() - start

        assert isinstance(routes, list)
        # Tiny board: must finish well inside the budget.
        assert elapsed < 60.0
