"""Drift guard for the local CI-equivalent gate's job manifest (issue #4671).

``scripts/ci/local-gate.sh`` carries an explicit manifest of the jobs in
``.github/workflows/ci.yml`` (it deliberately does NOT generate its commands
from the workflow YAML -- job steps embed container/matrix/conditional logic
with no local analogue).  That manifest can silently rot when ci.yml gains,
loses, or renames a job, so this test asserts set-equality between the
PyYAML-parsed ci.yml top-level job ids and the script's ``--list`` output.

If this test fails after you edited ci.yml: update the ``CHEAP_JOBS`` /
``LONG_JOBS`` arrays (and, if needed, the job_<id> functions) in
``scripts/ci/local-gate.sh`` to match, or -- for a job that genuinely cannot
be mirrored locally -- add it to ``CI_ONLY_JOBS`` below with a comment
explaining why.

This module also guards the runner loop's failure semantics: a job whose
*non-final* command fails must report FAIL.  ``("$fn") || rc=$?`` silently
broke that (bash disables errexit inside a ``||`` operand, and the
suppression is inherited by the subshell), so a failing ``ruff format
--check`` followed by a passing ``ruff check`` reported PASS.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parents[1]
CI_YML = REPO_ROOT / ".github" / "workflows" / "ci.yml"
LOCAL_GATE = REPO_ROOT / "scripts" / "ci" / "local-gate.sh"

# Jobs that exist in ci.yml but are intentionally NOT mirrored by
# local-gate.sh.  Every entry needs a comment explaining why the job has no
# local analogue.  Currently empty: all 16 ci.yml jobs are mirrored.
CI_ONLY_JOBS: set[str] = set()


def _ci_job_ids() -> set[str]:
    workflow = yaml.safe_load(CI_YML.read_text())
    return set(workflow["jobs"])


def _local_gate_list() -> list[str]:
    result = subprocess.run(
        ["bash", str(LOCAL_GATE), "--list"],
        capture_output=True,
        text=True,
        check=True,
        cwd=REPO_ROOT,
    )
    return [line for line in result.stdout.splitlines() if line.strip()]


def test_local_gate_script_is_executable() -> None:
    """The gate script must be checked in with its executable bit set."""
    assert LOCAL_GATE.exists(), f"missing {LOCAL_GATE}"
    assert os.access(LOCAL_GATE, os.X_OK), (
        f"{LOCAL_GATE} is not executable; run: chmod +x {LOCAL_GATE}"
    )


def test_local_gate_manifest_matches_ci_jobs() -> None:
    """`local-gate.sh --list` must equal the ci.yml job-id set.

    This is the drift guard: adding/removing/renaming a ci.yml job without
    updating the local gate's manifest fails the Test job.
    """
    ci_jobs = _ci_job_ids()
    listed = set(_local_gate_list())

    expected = ci_jobs - CI_ONLY_JOBS
    missing = expected - listed
    extra = listed - expected

    assert not missing and not extra, (
        "scripts/ci/local-gate.sh manifest drifted from .github/workflows/ci.yml.\n"
        f"  ci.yml jobs not in --list output: {sorted(missing) or 'none'}\n"
        f"  --list entries with no ci.yml job: {sorted(extra) or 'none'}\n"
        "Update CHEAP_JOBS/LONG_JOBS (and job_<id> functions) in "
        "scripts/ci/local-gate.sh, or document a CI-only job in "
        "CI_ONLY_JOBS in tests/test_local_gate_manifest.py."
    )


def test_local_gate_list_has_no_duplicates() -> None:
    """Each job id appears exactly once in --list output."""
    listed = _local_gate_list()
    assert len(listed) == len(set(listed)), f"duplicate job ids in --list: {listed}"


def _gate_with_stub_jobs(tmp_path: Path, stub_defs: str) -> Path:
    """Copy the real gate script, overriding some job_<id> bodies with stubs.

    The stub definitions are injected immediately before the runner section
    (bash keeps the *last* definition of a function), so the copy exercises
    the real dispatch loop -- the code under test -- while the job bodies are
    replaced by deterministic no-uv stubs.
    """
    source = LOCAL_GATE.read_text()
    marker = "usage() {"
    assert marker in source, "runner section marker moved; update this test"
    head, sep, tail = source.partition(marker)
    dest_dir = tmp_path / "scripts" / "ci"
    dest_dir.mkdir(parents=True)
    dest = dest_dir / "local-gate.sh"
    dest.write_text(f"{head}{stub_defs}\n{sep}{tail}")
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
    with that dispatch, only a job's last command decided PASS/FAIL.
    """
    script = _gate_with_stub_jobs(
        tmp_path,
        "job_lint() {\n  false\n  true\n}\n",
    )
    result = _run_gate(script, "lint")

    assert ">>> lint: FAIL" in result.stdout, (
        "runner loop swallowed a failing non-final command in job_lint:\n"
        f"{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 1, f"expected blocking-failure exit 1, got {result.returncode}"


@pytest.mark.skipif(shutil.which("uv") is None, reason="gate requires uv on PATH")
def test_skip_sentinel_still_reports_skip(tmp_path: Path) -> None:
    """`return $SKIP_RC` from a job must still report SKIP and exit 0."""
    script = _gate_with_stub_jobs(
        tmp_path,
        'job_lint() {\n  return "$SKIP_RC"\n}\n',
    )
    result = _run_gate(script, "lint")

    assert ">>> lint: SKIP" in result.stdout, (
        f"SKIP sentinel not honored:\n{result.stdout}\n{result.stderr}"
    )
    assert result.returncode == 0, f"SKIP must not flip the exit code, got {result.returncode}"


def test_ci_only_jobs_do_not_leak_stale_entries() -> None:
    """Every CI_ONLY_JOBS entry must still exist in ci.yml (no stale skips)."""
    stale = CI_ONLY_JOBS - _ci_job_ids()
    assert not stale, (
        f"CI_ONLY_JOBS entries no longer present in ci.yml: {sorted(stale)}; "
        "remove them from tests/test_local_gate_manifest.py."
    )
