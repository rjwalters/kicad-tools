"""Shared helper for tests that run the bench harness as a subprocess (#7944).

Since #7904 ``scripts/research/expand_route_experiment.py`` refuses (exit 2,
``REFUSED (KiCad environment ...)`` on stderr) when kicad-cli is not the pinned
release.  That is an environment limitation, not a regression, so tests skip on
it, matching the in-process pinned-toolchain tests.  CI runs inside the pinned
image and is unaffected.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

import pytest

REFUSED_EXIT = 2
REFUSED_PREFIX = "REFUSED (KiCad environment"


def skip_or_assert_harness_ok(proc: subprocess.CompletedProcess) -> subprocess.CompletedProcess:
    """Skip on a toolchain refusal; fail on any other non-zero exit."""
    stderr = proc.stderr or ""
    if proc.returncode == REFUSED_EXIT and REFUSED_PREFIX in stderr:
        reason = next(line for line in stderr.splitlines() if REFUSED_PREFIX in line)
        pytest.skip(reason.strip())
    assert proc.returncode == 0, (proc.stdout or "")[-4000:] + stderr[-4000:]
    return proc


def run_harness_or_skip_on_refusal(
    cmd: list[str], *, cwd: Path, timeout: float
) -> subprocess.CompletedProcess:
    """Run the harness subprocess; see :func:`skip_or_assert_harness_ok`."""
    proc = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True, timeout=timeout)
    return skip_or_assert_harness_ok(proc)
