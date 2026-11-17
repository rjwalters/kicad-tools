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
import re
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


# NOTE: this gate is applied per-class (not via module-level ``pytestmark``)
# so the fast CLI-interface contract test below always runs in PR CI.
# Issue #3460: the determinism test invoked ``check_routed_drc.py --pcb``
# after the script's CLI had moved to positional ``files``, and because the
# whole module was env-gated, no CI lane ever caught the drift.
_slow_gate = pytest.mark.skipif(
    not _slow_tests_enabled(),
    reason=(
        "Slow board-06 determinism test (45-60 min total).  Set "
        "KICAD_RUN_SLOW_BOARD06_DETERMINISM=1 to enable."
    ),
)


def _drc_checker_cmd(pcb_path: Path) -> list[str]:
    """Build the canonical ``check_routed_drc.py`` invocation for one PCB.

    Shared between the slow determinism test and the fast CLI-interface
    contract test below so the arg shape exercised in PR CI is the SAME
    shape the determinism runs use -- if the script's CLI drifts again
    (issue #3460: the ``--pcb`` flag was dropped in favour of positional
    ``files``), the fast test fails instead of the drift hiding behind the
    ``KICAD_RUN_SLOW_BOARD06_DETERMINISM`` gate.
    """
    return [sys.executable, str(DRC_CHECKER), str(pcb_path)]


def _extract_drc_error_count(stdout: str) -> int | None:
    """Parse the blocking-error count from ``check_routed_drc.py`` output.

    The script prints one of (see ``check_file`` in the script):

    * ``OK: <path> -- 0 errors (strict gate, --mfr jlcpcb).``
    * ``OK: <path> -- N errors (--mfr ..., allowlist max M; ...)``
    * ``::error file=<path>::DRC errors detected ...: N blocking error(s) ...``
    * ``::error file=<path>::DRC regression: N blocking error(s) ...``

    Returns the first ``N <blocking >error`` match, or ``None`` if no line
    matches (caller fails loudly with the full output).
    """
    match = re.search(r"(\d+)\s+(?:blocking\s+)?error", stdout)
    return int(match.group(1)) if match else None


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

    # Use the standard CI DRC checker to count errors.  The PCB path is
    # passed POSITIONALLY -- the script's CLI is ``check_routed_drc.py
    # [--allowlist X] files...`` (issue #3460: the old ``--pcb`` flag no
    # longer exists and argparse exits 2 with "unrecognized arguments").
    drc_cmd = _drc_checker_cmd(pcb_dst)
    drc_result = subprocess.run(drc_cmd, cwd=REPO_ROOT, env=env, capture_output=True, text=True)

    # Fail loudly on interface drift instead of falling through to the
    # count parser with empty stdout (issue #3460's failure mode).
    if "unrecognized arguments" in drc_result.stderr:
        pytest.fail(
            "check_routed_drc.py rejected its arguments -- the CLI drifted "
            f"again (issue #3460).  cmd: {drc_cmd}\nstderr:\n{drc_result.stderr}"
        )
    # Exit 0 = within tolerance, exit 2 = over tolerance; BOTH print a
    # parseable error count and both are fine here (this is a determinism
    # probe, not a pass/fail gate).  Exit 1 = tool failure.
    if drc_result.returncode not in (0, 2):
        pytest.fail(
            f"check_routed_drc.py failed (exit {drc_result.returncode}).\n"
            f"stdout:\n{drc_result.stdout}\nstderr:\n{drc_result.stderr}"
        )

    count = _extract_drc_error_count(drc_result.stdout)
    if count is None:
        pytest.fail(
            "Could not find DRC error count line in check_routed_drc.py output.  "
            f"stdout:\n{drc_result.stdout}\nstderr:\n{drc_result.stderr}"
        )
    return count, pcb_dst


class TestDRCCheckerCLIInterface:
    """Fast contract tests: the checker accepts the determinism arg shape.

    Always runs (PR CI included) -- NOT behind the
    ``KICAD_RUN_SLOW_BOARD06_DETERMINISM`` gate.  Issue #3460: when
    ``check_routed_drc.py`` replaced ``--pcb`` with positional ``files``,
    the determinism test kept passing ``--pcb`` and nothing in CI noticed
    because the only caller was env-gated.  These tests pin the shared
    ``_drc_checker_cmd`` shape against the real script so CLI drift fails
    a fast lane immediately.
    """

    def test_checker_accepts_determinism_invocation_shape(self, tmp_path: Path) -> None:
        """The exact argv shape the determinism test uses parses cleanly.

        Uses a nonexistent PCB path: the script's contract is to warn and
        skip missing files (exit 0), which proves argparse accepted the
        arguments without paying for a real ``kct check`` run.
        """
        missing_pcb = tmp_path / "does_not_exist.kicad_pcb"
        cmd = _drc_checker_cmd(missing_pcb)
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        assert "unrecognized arguments" not in result.stderr, (
            f"check_routed_drc.py rejected the determinism test's arg shape "
            f"(issue #3460 regression).  cmd: {cmd}\nstderr:\n{result.stderr}"
        )
        assert result.returncode == 0, (
            f"Expected exit 0 (missing file is warn-and-skip), got "
            f"{result.returncode}.\nstdout:\n{result.stdout}\nstderr:\n{result.stderr}"
        )

    def test_legacy_pcb_flag_is_rejected(self, tmp_path: Path) -> None:
        """``--pcb`` is gone; pin that so a partial revert can't half-restore it.

        If a future change deliberately restores a ``--pcb`` alias, update
        this test AND ``_drc_checker_cmd`` together.
        """
        missing_pcb = tmp_path / "does_not_exist.kicad_pcb"
        cmd = [sys.executable, str(DRC_CHECKER), "--pcb", str(missing_pcb)]
        result = subprocess.run(cmd, cwd=REPO_ROOT, capture_output=True, text=True)
        assert result.returncode == 2
        assert "unrecognized arguments" in result.stderr

    def test_error_count_parser_handles_all_output_formats(self) -> None:
        """``_extract_drc_error_count`` parses every format the script emits."""
        assert (
            _extract_drc_error_count("OK: x.kicad_pcb -- 0 errors (strict gate, --mfr jlcpcb).")
            == 0
        )
        assert (
            _extract_drc_error_count(
                "OK: x.kicad_pcb -- 4 errors (--mfr jlcpcb, allowlist max 6; reduce ...)."
            )
            == 4
        )
        assert (
            _extract_drc_error_count(
                "::error file=x::DRC errors detected by `kct check --mfr jlcpcb "
                "--errors-only`: 35 blocking error(s) (advisory rules excluded)."
            )
            == 35
        )
        assert (
            _extract_drc_error_count(
                "::error file=x::DRC regression: 7 blocking error(s) (--mfr jlcpcb, "
                "advisory rules excluded) exceeds allowlist value 5 ..."
            )
            == 7
        )
        assert _extract_drc_error_count("no counts here") is None


@_slow_gate
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
