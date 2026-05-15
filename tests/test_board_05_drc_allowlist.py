"""Regression guard: board-05 routed DRC error count stays at-or-below the allowlist.

Issue #2901 (umbrella #2746 child 4) — pin the post-#2904 DRC state so a
future change that increases the error count above the per-board
allowlist trips CI even when the count is still within the historic
tolerance.

The allowlist lives at ``.github/routed-drc-tolerance.yml`` and tracks
the per-board maximum allowed error count under JLCPCB rules.  The CI
job ``routed-pcb-drc-check`` (``scripts/ci/check_routed_drc.py``) is the
binding gate in CI; this test mirrors the same comparison so a unit-test
run on the developer's laptop catches the regression too.

**Why a separate test file rather than extending
``test_board_05_routing_regression.py``**: that file is marked
``@pytest.mark.slow`` because it re-runs ``kct route`` (4-minute wall
clock).  The DRC count check only invokes ``kct check`` on the committed
``_routed.kicad_pcb`` artifact (~1-2 seconds), so it belongs in the
fast-PR-CI lane.  Keeping the files split lets the slow regression
remain nightly while this guard runs on every PR.

**Auto-tightening**: the test reads the allowlist file rather than
hardcoding the value, so when a router improvement drops the allowlist
from 53 → 30, this test automatically pins the new lower floor without
requiring a code change here.  The complement (drift warning when the
actual count goes BELOW the allowlist) is implemented by the CI script's
``annotate_drift_warning``; this test only asserts the upper bound.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
ROUTED_PCB = BOARD_DIR / "output" / "bldc_controller_routed.kicad_pcb"
ALLOWLIST_PATH = REPO_ROOT / ".github" / "routed-drc-tolerance.yml"

# Repo-relative key used inside the allowlist YAML for board 05.
BOARD_05_ALLOWLIST_KEY = "boards/05-bldc-motor-controller/output/bldc_controller_routed.kicad_pcb"


@pytest.fixture(scope="module")
def routed_pcb_path() -> Path:
    """Resolve the committed routed PCB or skip if absent."""
    if not ROUTED_PCB.exists():
        pytest.skip(
            f"Board 05 routed PCB not found at {ROUTED_PCB!s}; "
            "regenerate via "
            "`uv run python boards/05-bldc-motor-controller/design.py`"
        )
    return ROUTED_PCB


@pytest.fixture(scope="module")
def board_05_allowlist_value() -> int:
    """Read the board-05 entry from ``.github/routed-drc-tolerance.yml``.

    Returns the integer max-allowed error count.  Failures are explicit
    so a misconfigured allowlist surfaces as a clear test failure rather
    than a confusing "key error" stack trace.
    """
    if not ALLOWLIST_PATH.exists():
        pytest.skip(f"Allowlist file not found at {ALLOWLIST_PATH!s}")

    data = yaml.safe_load(ALLOWLIST_PATH.read_text())
    if not isinstance(data, dict) or "tolerances" not in data:
        pytest.fail(
            f"Allowlist {ALLOWLIST_PATH!s} missing top-level 'tolerances' "
            f"mapping; got {type(data).__name__}"
        )

    tolerances = data["tolerances"]
    if BOARD_05_ALLOWLIST_KEY not in tolerances:
        pytest.fail(
            f"Board 05 entry {BOARD_05_ALLOWLIST_KEY!r} not found in "
            f"allowlist {ALLOWLIST_PATH!s}.  The entry was present at the "
            f"time this test was written (issue #2901); if board 05 "
            f"reaches zero errors the entry should be REMOVED entirely "
            f"(per the file's policy header) and this test updated to "
            f"assert the strict 0-error gate."
        )

    value = tolerances[BOARD_05_ALLOWLIST_KEY]
    assert isinstance(value, int) and value >= 0, (
        f"Board 05 allowlist value must be a non-negative int, "
        f"got {value!r} ({type(value).__name__})"
    )
    return value


def _run_kct_check(pcb_path: Path) -> int:
    """Run ``kct check`` on *pcb_path* and return the error count.

    Mirrors ``scripts/ci/check_routed_drc.py::count_errors`` -- uses
    ``--mfr jlcpcb --errors-only --format json`` to get a machine-parsable
    count.  Tool-level failures (exit 1) raise ``RuntimeError`` so a
    misconfigured environment surfaces clearly rather than masquerading
    as a zero-error count.
    """
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "check",
        str(pcb_path),
        "--mfr",
        "jlcpcb",
        "--errors-only",
        "--format",
        "json",
    ]
    proc = subprocess.run(
        cmd,
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )
    # Exit 1 = tool-level failure (file not found, parse error).
    # Exit 0 = no errors.  Exit 2 = errors found.  Both 0 and 2 produce
    # valid JSON on stdout.
    if proc.returncode == 1:
        raise RuntimeError(
            f"kct check failed on {pcb_path} (exit 1).\nstderr:\n{proc.stderr.strip()}"
        )
    if proc.returncode not in (0, 2):
        raise RuntimeError(
            f"kct check returned unexpected exit code {proc.returncode} "
            f"on {pcb_path}.\nstderr:\n{proc.stderr.strip()}"
        )

    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"kct check produced invalid JSON on {pcb_path}: {e}\n"
            f"stdout (first 500 chars):\n{proc.stdout[:500]}"
        ) from e

    summary = data.get("summary", {})
    errors = summary.get("errors")
    if not isinstance(errors, int):
        raise RuntimeError(
            f"kct check JSON missing summary.errors field for {pcb_path}: keys={list(summary)!r}"
        )
    return errors


class TestBoard05DRCAllowlistGuard:
    """Acceptance criterion 3 of issue #2901."""

    def test_routed_drc_error_count_at_or_below_allowlist(
        self,
        routed_pcb_path: Path,
        board_05_allowlist_value: int,
    ) -> None:
        """Routed PCB DRC error count must be ≤ allowlist (currently 53).

        Reads the allowlist value from
        ``.github/routed-drc-tolerance.yml`` so this test auto-tightens
        when the allowlist drops -- no need to update a hard-coded
        constant here when a router improvement reduces the floor.

        A failure here typically indicates one of:

        * A real routing regression that introduced new DRC violations
          (e.g., a planner change that emits traces clipping pads).
        * A footprint or library change that altered pad geometry without
          re-running the router.
        * The committed routed PCB drifted from the source unrouted PCB
          (someone edited ``_routed.kicad_pcb`` directly).

        Re-route the board via
        ``uv run python boards/05-bldc-motor-controller/design.py`` to
        regenerate and re-check, OR update the allowlist value with
        reviewer sign-off if the new floor is the new reality.
        """
        errors = _run_kct_check(routed_pcb_path)
        assert errors <= board_05_allowlist_value, (
            f"Board 05 routed PCB reports {errors} DRC error(s) under "
            f"JLCPCB rules; allowlist max is {board_05_allowlist_value} "
            f"(from {ALLOWLIST_PATH.relative_to(REPO_ROOT)!s}).  This is "
            f"a routing regression -- either revert the offending change "
            f"or raise the allowlist value with reviewer justification "
            f"and a tracking-issue link in the PR description."
        )

    def test_allowlist_value_matches_documented_floor(self, board_05_allowlist_value: int) -> None:
        """The allowlist value is sane (>= 0 and not absurdly large).

        Sanity check on the YAML parse.  If the allowlist accidentally
        gets bumped to 1000 by a botched merge, the upper-bound assertion
        in :meth:`test_routed_drc_error_count_at_or_below_allowlist`
        would silently pass even with serious routing damage.  This test
        catches the "allowlist itself regressed" case.

        The 200 upper bound is generous (the highest historic value
        across all boards in the file is 70 for board 07) but tight
        enough to flag a typo like 530 vs 53.
        """
        assert 0 <= board_05_allowlist_value <= 200, (
            f"Board 05 allowlist value {board_05_allowlist_value} is "
            f"outside the expected 0..200 range.  If a routing regression "
            f"genuinely requires loosening above 200, update this test's "
            f"sanity bound in the same PR."
        )
