"""Keep content validation mandatory when the expensive CI jobs are skipped."""

from __future__ import annotations

import os
import shutil
import subprocess
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parents[1]
RUNNER = "scripts/ci/check-content-contracts.sh"


def test_ci_content_gate_is_unconditional() -> None:
    workflow = yaml.safe_load((ROOT / ".github/workflows/ci.yml").read_text())
    lint = workflow["jobs"]["lint"]
    assert "if" not in lint
    assert "needs" not in lint
    # Issue #9340: the lint job's gating commands moved into one script the
    # local gate also runs (2am#7561 ruling); the workflow calls it from a
    # single unconditional step.
    steps = [step for step in lint["steps"] if step.get("run") == "bash scripts/ci/jobs/lint.sh"]
    assert len(steps) == 1
    assert "if" not in steps[0]
    assert not steps[0].get("continue-on-error", False)
    assert not lint.get("continue-on-error", False)
    # The content-contract runner is invoked by that script, unconditionally
    # (the script is a linear ruff/ruff/contracts sequence).
    job_script = (ROOT / "scripts/ci/jobs/lint.sh").read_text()
    assert job_script.count(f"bash {RUNNER}") == 1, (
        "scripts/ci/jobs/lint.sh must invoke the content-contract runner exactly once"
    )


def test_local_lint_propagates_content_failure(tmp_path: Path) -> None:
    """Execute the real local dispatcher and shared runner with a failing pytest."""
    for relative in (RUNNER, "scripts/ci/local-gate.sh"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
    # Issue #9340: the dispatcher runs the real per-job script; copy the
    # whole jobs directory (its lint.sh sources _lib.sh).
    shutil.copytree(ROOT / "scripts/ci/jobs", tmp_path / "scripts/ci/jobs")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    uv = bin_dir / "uv"
    uv.write_text(
        "#!/usr/bin/env bash\n"
        'case " $* " in\n'
        '  *" pytest "*) echo "content-pytest-reached"; exit 1 ;;\n'
        '  *" ruff "*) exit 0 ;;\n'
        "  *) exit 2 ;;\n"
        "esac\n"
    )
    uv.chmod(0o755)
    result = subprocess.run(
        ["bash", str(tmp_path / "scripts/ci/local-gate.sh"), "lint"],
        env={**os.environ, "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
        capture_output=True,
        text=True,
        check=False,
    )
    assert "content-pytest-reached" in result.stdout
    assert ">>> lint: FAIL" in result.stdout
    assert result.returncode == 1
