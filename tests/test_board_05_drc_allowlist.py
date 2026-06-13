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

**Advisory-rule filter (Issue #3294)**: the CI gate
(``scripts/ci/check_routed_drc.py::_count_blocking_errors``) filters
advisory rules (currently just ``connectivity``) out of the count it
compares against the allowlist, mirroring
``DRCChecker.ADVISORY_RULE_IDS`` and the audit pipeline's
``ManufacturingAudit._check_drc``.  This test now applies the same filter
so the developer-laptop result matches the CI verdict.  Before the fix
this test was failing on board 05 (29 unfiltered errors vs allowlist 9)
even though CI happily passed (6 blocking vs allowlist 9) — a noisy
false positive that masked real regressions.  See PR #3258's notes
"pre-existing failure in ``test_board_05_drc_allowlist`` (advisory rule
filter mismatch with CI) is unchanged by this PR".
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from kicad_tools.validate.checker import DRCChecker

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


# Issue #3527 (2026-06-11): the new ``clearance_segment_zone`` DRC rule
# (segments vs foreign-net zone *fill* copper) revealed 10 pre-existing
# stale-fill defects in board 05's committed artifact (6 shorts + 4
# sub-clearance grazes: SW_OUT/PWM_AL/PWM_BH/GATE_CL/SWDIO vs the
# +24V/+3V3/GND fills).  These were NOT a routing regression introduced
# by a code change -- they were always in the copper; the gate simply
# could not see them before the rule existed.
#
# Issue #3553 (2026-06-11) fixed the artifact: the fills were stale
# (computed before the offending traces existed), so regenerating the
# zone fills against the final copper (``kct zones fill``, same recipe
# as board 06 / PR #3548) cleared all 10 findings with zero trace, via,
# or zone-outline changes.  The allowlist entry was removed, restoring
# the #3470 strict-0 gate (``None`` = entry absent = 0 blocking errors).
#
# Issue #3556 (2026-06-13) re-added a tolerance of 30: the new
# ``clearance_via_zone`` / ``clearance_pad_zone`` rule (the via/pad
# sibling of #3527, vias/pads vs foreign-net zone *fill* copper) surfaced
# 30 pre-existing ``clearance_pad_zone`` defects in the committed artifact
# (foreign-net pad-vs-pour gaps: PHASE_A/B/C vs GND, +24V vs GND, SWDIO/
# SWO vs +3V3, ...).  Like the #3527 findings these were always in the
# copper -- the gate simply could not see pad-vs-fill spacing before the
# rule existed; #3553-style zone refill against the final placement clears
# them.  This fixture pins the entry to EXACTLY 30: any other value fails
# loudly and requires an explicit update here with reviewer sign-off.
BOARD_05_EXPECTED_TOLERANCE: int | None = 30


@pytest.fixture(scope="module")
def board_05_allowlist_value() -> int:
    """Resolve board 05's effective allowlist value.

    Issue #3470 (2026-06-10) removed the board-05 ``tolerances:`` entry
    from ``.github/routed-drc-tolerance.yml`` -- per the file's policy
    header, the ABSENCE of an entry is the strict 0-blocking-error gate.
    The single residual blocking violation (ISENSE_A-/ISENSE_B- escape
    stub overlap on In1.Cu at (18.75, 53.75)) was fixed at the source:
    conflict-aware in-pad escape stub direction (escape.py) plus the
    transactional rip-up rollback (negotiated.py ``targeted_ripup``).

    Issue #3527 (2026-06-11) re-added a tolerance of 10 because the new
    ``clearance_segment_zone`` rule surfaced 10 pre-existing stale-fill
    defects in the committed artifact (tracked in Issue #3553 -- see
    ``BOARD_05_EXPECTED_TOLERANCE`` above).  This fixture pins the entry
    to EXACTLY that value: any other value (loosening beyond 10, or a
    stale entry after #3553 fixes the artifact) fails loudly and requires
    an explicit update to this test with reviewer sign-off.
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
    actual = tolerances.get(BOARD_05_ALLOWLIST_KEY)
    if actual != BOARD_05_EXPECTED_TOLERANCE:
        pytest.fail(
            f"Board 05 allowlist entry {BOARD_05_ALLOWLIST_KEY!r} is "
            f"{actual!r} but this test pins "
            f"{BOARD_05_EXPECTED_TOLERANCE!r} (None = entry absent).  "
            f"Changing the board-05 tolerance requires reviewer sign-off "
            f"AND an update to ``BOARD_05_EXPECTED_TOLERANCE`` in this "
            f"test with a tracking-issue link (see Issues #3470/#3527/"
            f"#3553 for the history)."
        )

    # Absence of the entry = strict 0-blocking-error gate.
    return 0 if actual is None else actual


def _run_kct_check(pcb_path: Path) -> int:
    """Run ``kct check`` on *pcb_path* and return the *blocking* error count.

    Mirrors ``scripts/ci/check_routed_drc.py::_count_blocking_errors`` --
    uses ``--mfr jlcpcb-tier1 --errors-only --format json`` to get a
    machine-parsable count and applies the same advisory-rule filter
    (currently ``connectivity``) the CI gate uses.  Without the filter
    this test diverged from CI on boards that ship with non-zero
    partial-route reports (board 05's committed PCB had 23 connectivity
    advisories that the CI gate correctly excluded but this test was
    counting against the allowlist).

    Tool-level failures (exit 1) raise ``RuntimeError`` so a
    misconfigured environment surfaces clearly rather than masquerading
    as a zero-error count.
    """
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "check",
        str(pcb_path),
        # Issue #3425: board 05 routes + DRC-gates against jlcpcb-tier1
        # (Capability-Plus legalizes the DRV8301 in-pad rescue vias).
        # The CI gate reads the same profile from the manufacturers:
        # override in .github/routed-drc-tolerance.yml; this test pins
        # the profile inline to stay aligned with that verdict.
        "--mfr",
        "jlcpcb-tier1",
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

    # Filter advisory rules (e.g. ``connectivity``) out of the count so
    # the test's comparison matches the CI gate's verdict.  Prefer the
    # per-violation ``violations`` list (richer payload) and fall back to
    # the unfiltered ``summary.errors`` for legacy/malformed payloads.
    violations = data.get("violations")
    if isinstance(violations, list):
        blocking = 0
        for v in violations:
            if not isinstance(v, dict):
                continue
            if v.get("severity", "error") != "error":
                continue
            rule_id = v.get("rule_id", "")
            if not isinstance(rule_id, str):
                continue
            if DRCChecker.is_advisory_rule(rule_id):
                continue
            blocking += 1
        return blocking

    summary = data.get("summary", {})
    errors = summary.get("errors")
    if not isinstance(errors, int):
        raise RuntimeError(
            f"kct check JSON missing both violations array and summary.errors "
            f"field for {pcb_path}: keys={list(summary)!r}"
        )
    return errors


class TestBoard05DRCAllowlistGuard:
    """Acceptance criterion 3 of issue #2901."""

    def test_routed_drc_error_count_at_or_below_allowlist(
        self,
        routed_pcb_path: Path,
        board_05_allowlist_value: int,
    ) -> None:
        """Routed PCB DRC blocking-error count must be ≤ allowlist.

        Reads the allowlist value from
        ``.github/routed-drc-tolerance.yml`` so this test auto-tightens
        when the allowlist drops -- no need to update a hard-coded
        constant here when a router improvement reduces the floor.

        The count compared here is the *blocking* error count (advisory
        rules such as ``connectivity`` are excluded), matching
        ``scripts/ci/check_routed_drc.py::_count_blocking_errors`` and
        the audit pipeline's classifier
        (``DRCChecker.is_advisory_rule``).  This keeps the developer-
        laptop verdict aligned with what CI gates the build on.

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
            f"Board 05 routed PCB reports {errors} blocking DRC error(s) "
            f"under JLCPCB rules (excluding advisory rules per "
            f"DRCChecker.ADVISORY_RULE_IDS); allowlist max is "
            f"{board_05_allowlist_value} (from "
            f"{ALLOWLIST_PATH.relative_to(REPO_ROOT)!s}).  This is "
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

        The 200 upper bound is generous (the highest value across all
        boards in the file is 120 for board 07 after Issue #3556 added the
        via/pad-vs-zone-fill rule) but tight enough to flag a typo like
        530 vs 53.
        """
        assert 0 <= board_05_allowlist_value <= 200, (
            f"Board 05 allowlist value {board_05_allowlist_value} is "
            f"outside the expected 0..200 range.  If a routing regression "
            f"genuinely requires loosening above 200, update this test's "
            f"sanity bound in the same PR."
        )
