"""Issue #8285: ``kct optimize-traces`` accept/discard decisions must be reproducible.

Runs the optimizer plus the #8253 connectivity check in subprocesses under
different ``PYTHONHASHSEED`` values and asserts the discarded-net lists and the
written boards (UUIDs excluded) are identical.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

BOARD = (
    Path(__file__).parent.parent
    / "boards"
    / "06-diffpair-test"
    / "output"
    / "diffpair_test_routed.kicad_pcb"
)
_UUID = re.compile(r'\(uuid "[^"]*"\)')


def _run(tmp_path: Path, seed: str) -> tuple[list[str], str]:
    out = tmp_path / f"out{seed}.kicad_pcb"
    env = {**os.environ, "PYTHONHASHSEED": seed}
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "optimize-traces",
            str(BOARD),
            "-o",
            str(out),
            "--format",
            "json",
        ],
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
    )
    assert proc.returncode == 0, proc.stderr
    stats = json.loads(proc.stdout)["stats"]
    return stats["connectivity_discarded_nets"], _UUID.sub("", out.read_text())


@pytest.mark.skipif(not BOARD.exists(), reason="board 06 routed fixture missing")
def test_connectivity_decisions_identical_across_hash_seeds(tmp_path):
    discarded0, text0 = _run(tmp_path, "0")
    discarded1, text1 = _run(tmp_path, "1")
    assert discarded0 == discarded1
    assert text0 == text1
