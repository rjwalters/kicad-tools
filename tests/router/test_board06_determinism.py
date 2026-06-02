"""Board 06 (diffpair-test) routing determinism (Issue #3144).

These tests re-route the board 06 PCB N times at the same seed and
assert that the resulting DRC error counts are byte-identical across
runs.  Without the Issue #3144 fixes (A* tie-break in ``CoupledNode``
/ C++ ``AStarNode`` + iteration-budget classifier in
``DifferentialPairConfig``) the run-to-run variance was 35-43 errors
on CI runners (8-error swing); locally it was variable but smaller.

The tests are gated behind ``KICAD_RUN_SLOW_BOARD06_DETERMINISM=1``
because each re-route takes 9-12 minutes wall-clock and the full
N-run loop is 45-60 minutes.  CI invokes them via a dedicated job;
``pnpm check:ci`` does NOT include them.

To run locally::

    KICAD_RUN_SLOW_BOARD06_DETERMINISM=1 uv run pytest \\
      tests/router/test_board06_determinism.py -v --no-cov

The lighter-weight A* tie-break tests in
``test_astar_tiebreak_determinism.py`` MUST pass unconditionally and
catch the bulk of regressions; this file is the integration backstop
that catches anything the unit tests miss (e.g. determinism
properties of downstream pipeline stages not covered by the unit
tests).
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
BOARD_DIR = REPO_ROOT / "boards" / "06-diffpair-test"
GENERATE_SCRIPT = BOARD_DIR / "generate_design.py"
ROUTED_PCB_NAME = "diffpair_test_routed.kicad_pcb"
DRC_CHECKER = REPO_ROOT / "scripts" / "ci" / "check_routed_drc.py"


def _slow_tests_enabled() -> bool:
    return os.environ.get("KICAD_RUN_SLOW_BOARD06_DETERMINISM") == "1"


pytestmark = pytest.mark.skipif(
    not _slow_tests_enabled(),
    reason=(
        "Slow board-06 determinism test (45-60 min total).  Set "
        "KICAD_RUN_SLOW_BOARD06_DETERMINISM=1 to enable."
    ),
)


def _route_and_count_drc(out_dir: Path, seed: int, run_index: int) -> tuple[int, Path]:
    """Re-route board 06 and return ``(error_count, pcb_path)``.

    Mirrors the protocol described in Issue #3144's "Reproduction
    Protocol" section.  The PCB is copied to ``out_dir`` so the
    caller can inspect divergence post-mortem if a regression is
    detected.
    """
    cmd = [
        sys.executable,
        str(GENERATE_SCRIPT),
        "--step",
        "route",
        "--seed",
        str(seed),
    ]
    env = os.environ.copy()
    env.setdefault("PYTHONHASHSEED", "42")
    log_path = out_dir / f"run-{run_index}.log"
    pcb_dst = out_dir / f"run-{run_index}.kicad_pcb"
    with log_path.open("w") as log_fh:
        subprocess.run(
            cmd,
            cwd=REPO_ROOT,
            env=env,
            check=True,
            stdout=log_fh,
            stderr=subprocess.STDOUT,
        )

    src_pcb = BOARD_DIR / "output" / ROUTED_PCB_NAME
    assert src_pcb.exists(), f"Routed PCB not produced: {src_pcb}"
    shutil.copy2(src_pcb, pcb_dst)

    # Use the standard CI DRC checker to count errors.
    drc_cmd = [
        sys.executable,
        str(DRC_CHECKER),
        str(BOARD_DIR / "output"),
        "--pcb",
        str(pcb_dst),
    ]
    drc_result = subprocess.run(drc_cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)
    # ``check_routed_drc.py`` prints "DRC error count: N" somewhere in
    # its output; pull the integer out.  When the script's output
    # format changes, this test will fail loudly with the full output
    # captured below for diagnostics.
    count_line = next(
        (
            line
            for line in drc_result.stdout.splitlines()
            if "DRC error count" in line or "errors" in line.lower()
        ),
        None,
    )
    if count_line is None:
        pytest.fail(
            "Could not find DRC error count line in check_routed_drc.py output.  "
            f"stdout:\n{drc_result.stdout}\nstderr:\n{drc_result.stderr}"
        )

    # Extract trailing integer; the format historically is
    # "DRC error count: 35".
    digits = "".join(c for c in count_line if c.isdigit() or c == " ").split()
    if not digits:
        pytest.fail(f"Could not parse integer from DRC count line: {count_line!r}")
    return int(digits[-1]), pcb_dst


class TestBoard06DRCDeterminism:
    """Multi-run DRC count determinism for board 06 at the same seed."""

    def test_drc_counts_identical_across_five_runs(self, tmp_path: Path) -> None:
        """5 runs at seed 42 must yield identical DRC error counts.

        Acceptance criterion for Issue #3144.  Variance MUST be 0;
        if your run shows variance, the A* tie-break fix or the
        iteration budget classifier has regressed (or some new
        non-determinism vector was introduced by an unrelated PR).
        """
        out_dir = tmp_path / "determinism"
        out_dir.mkdir()

        counts: list[int] = []
        pcbs: list[Path] = []
        for i in range(1, 6):
            count, pcb = _route_and_count_drc(out_dir, seed=42, run_index=i)
            counts.append(count)
            pcbs.append(pcb)
            print(f"  Run {i}: DRC error count = {count}")

        assert len(set(counts)) == 1, (
            f"DRC counts diverged across 5 runs: {counts}.  "
            f"PCBs preserved at {out_dir} for post-mortem."
        )
