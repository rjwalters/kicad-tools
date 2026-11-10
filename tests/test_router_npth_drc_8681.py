"""End-to-end ``hole_clearance`` check for an NPTH between two pads (Issue #8681).

Routes the #8675 fixture and runs ``kicad-cli pcb drc``. Skipped without
``kicad-cli``; run with ``KCT_KICAD_DOCKER=1`` for the pinned toolchain.
"""

import json
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_router_npth_obstacle_8675 import PCB

pytestmark = pytest.mark.skipif(shutil.which("kicad-cli") is None, reason="kicad-cli not installed")


def _hole_clearance_count(tmp_path: Path, extra: list[str]) -> int:
    src = tmp_path / "in.kicad_pcb"
    src.write_text(PCB)
    out = tmp_path / "out.kicad_pcb"
    cmd = [sys.executable, "-m", "kicad_tools.cli", "route", str(src), "-o", str(out)]
    subprocess.run([*cmd, "--grid", "0.1", *extra], check=True, capture_output=True, timeout=600)
    report = tmp_path / "drc.json"
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--refill-zones",
            "--format",
            "json",
            "--severity-all",
            "-o",
            str(report),
            str(out),
        ],
        check=True,
        capture_output=True,
        timeout=300,
    )
    violations = json.loads(report.read_text())["violations"]
    return sum(v["type"] == "hole_clearance" for v in violations)


def test_grid_engine_has_no_hole_clearance_violation(tmp_path):
    assert _hole_clearance_count(tmp_path, ["--route-engine", "grid"]) == 0


@pytest.mark.xfail(strict=True, reason="lattice leaves 2 hole_clearance violations (#8684)")
def test_lattice_engine_has_no_hole_clearance_violation(tmp_path):
    n = _hole_clearance_count(tmp_path, ["--route-engine", "lattice", "--strategy", "basic"])
    assert n == 0
