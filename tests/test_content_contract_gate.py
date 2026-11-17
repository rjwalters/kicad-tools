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
    steps = [step for step in lint["steps"] if step.get("run") == f"bash {RUNNER}"]
    assert len(steps) == 1
    assert "if" not in steps[0]
    assert not steps[0].get("continue-on-error", False)
    assert not lint.get("continue-on-error", False)


def test_local_lint_propagates_content_failure(tmp_path: Path) -> None:
    """Execute the real local dispatcher and shared runner with a failing pytest."""
    for relative in (RUNNER, "scripts/ci/local-gate.sh"):
        target = tmp_path / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(ROOT / relative, target)
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
