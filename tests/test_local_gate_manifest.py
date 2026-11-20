"""Runner-behaviour guards for the local CI-equivalent gate (issue #4671).

``scripts/ci/local-gate.sh`` is the thin runner of the Actions-outage
fallback gate: it dispatches to ``scripts/ci/jobs/<job-id>.sh`` -- the single
copy of each job's check commands that the same-named job in
``.github/workflows/ci.yml`` also calls (issue #9340, operator ruling
written into 2AMLogic/2am#7561: the commands are written once, in a script in
the repository, and the workflow calls that script).

There is deliberately NO manifest-drift test any more: the gate runs the
same scripts the workflow calls, so there is no second command list to pin.
The genuinely CI-only jobs (`kicad_pin`, `changes`, `windows-smoke`) have no
script and are not part of the gate.

This module guards the runner loop's failure semantics: a job whose
*non-final* command fails must report FAIL.  ``("$fn") || rc=$?`` silently
broke that (bash disables errexit inside a ``||`` operand, and the
suppression is inherited by the subshell), so a failing ``ruff format
--check`` followed by a passing ``ruff check`` reported PASS. The same guard
now exercises the external-script dispatch: each job script runs under its
own ``set -euo pipefail``, and the runner wraps it in an errexit-live
subshell.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCAL_GATE = REPO_ROOT / "scripts" / "ci" / "local-gate.sh"
JOBS_DIR = REPO_ROOT / "scripts" / "ci" / "jobs"


def test_local_gate_script_is_executable() -> None:
    """The gate script must be checked in with its executable bit set."""
    assert LOCAL_GATE.exists(), f"missing {LOCAL_GATE}"
    assert os.access(LOCAL_GATE, os.X_OK), (
        f"{LOCAL_GATE} is not executable; run: chmod +x {LOCAL_GATE}"
    )


def test_every_gate_job_has_a_script() -> None:
    """Every job id `--list` offers (plus the release extras) must have a
    matching scripts/ci/jobs/<id>.sh the runner can dispatch to."""
    listed = subprocess.run(
        ["bash", str(LOCAL_GATE), "--list"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    ).stdout.split()
    extras = ("release-board03-baseline", "release-changelog-gap")
    for job in [*listed, *extras]:
        script = JOBS_DIR / f"{job}.sh"
        assert script.is_file(), f"local-gate job {job!r} has no {script}"
        assert os.access(script, os.X_OK), f"{script} is not executable; run: chmod +x {script}"


def _gate_with_stub_jobs(tmp_path: Path, stub_script: str) -> Path:
    """Copy the real gate script plus its jobs directory into tmp_path,
    overriding the ``lint`` job script with a deterministic no-uv stub, so
    the copy exercises the real dispatch loop -- the code under test --
    while the job body is replaced."""
    dest_dir = tmp_path / "scripts" / "ci"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "local-gate.sh"
    shutil.copyfile(LOCAL_GATE, dest)
    jobs_dir = dest_dir / "jobs"
    shutil.copytree(JOBS_DIR, jobs_dir)
    lint = jobs_dir / "lint.sh"
    lint.write_text(stub_script)
    lint.chmod(0o755)
    return dest


def _run_gate(script: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["bash", str(script), *args],
        capture_output=True,
        text=True,
        cwd=script.parent,
    )


@pytest.mark.skipif(shutil.which("uv") is None, reason="gate requires uv on PATH")
def test_job_with_failing_intermediate_command_reports_fail(tmp_path: Path) -> None:
    """A job whose non-final command fails must FAIL, not PASS.

    Regression guard for the ``("$fn") || rc=$?`` errexit-suppression bug:
    with that dispatch, only a job's last command decided PASS/FAIL. The
    stub mirrors the real job scripts' ``set -euo pipefail`` header because
    the runner now dispatches to external scripts (each script's own errexit
    is what stops at the failing non-final command).
    """
    script = _gate_with_stub_jobs(
        tmp_path,
        "#!/usr/bin/env bash\nset -euo pipefail\nfalse\ntrue\n",
    )
    result = _run_gate(script, "lint")

    assert ">>> lint: FAIL" in result.stdout, (
        "runner loop swallowed a failing non-final command in the lint job:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 1, f"expected blocking-failure exit 1, got {result.returncode}"


@pytest.mark.skipif(shutil.which("uv") is None, reason="gate requires uv on PATH")
def test_skip_sentinel_still_reports_skip(tmp_path: Path) -> None:
    """A job script exiting $SKIP_RC (75) must report SKIP and exit 0.

    The real job scripts use this sentinel (die_skip) when a tool they
    strictly require is absent locally -- e.g. kicad-cli or npm -- and the
    runner must map it to SKIP rather than FAIL.
    """
    script = _gate_with_stub_jobs(
        tmp_path,
        "#!/usr/bin/env bash\nset -euo pipefail\nexit 75\n",
    )
    result = _run_gate(script, "lint")

    assert ">>> lint: SKIP" in result.stdout, (
        f"SKIP sentinel not honored:\n{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 0, f"SKIP must not flip the exit code, got {result.returncode}"
