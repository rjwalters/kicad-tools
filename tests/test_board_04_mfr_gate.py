"""Regression guard: board 04 (stm32-devboard) passes the CI manufacturability gate.

Issue #3261 (tracking) -- "Make board 04 manufacturable: verify fresh
artifacts + close any residual gaps".

This test pins the post-#3218 / post-#3128 manufacturability state of
``boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb`` against
the same gate the CI ``routed-pcb-drc-check`` job uses
(``scripts/ci/check_routed_drc.py``).  Specifically it asserts:

1. The committed routed PCB still passes the gate against the
   ``jlcpcb-tier1`` profile declared in
   ``.github/routed-drc-tolerance.yml``'s ``manufacturers:`` block.

2. The gate's blocking error count stays at or below the floor declared
   for board 04 in the same YAML (currently 1, post-#3118 micro-via
   in-pad rescue).

3. The advisory ``connectivity`` finding documented in
   ``.github/routed-drc-tolerance.yml`` (a single stranded GND-stitch
   pad) does NOT inflate to non-trivial regression -- if a future
   router change strands more GND pads, the audit count rises and we
   surface the issue at PR time rather than discovering it in fleet
   status weeks later.

The fleet-status fingerprint is the higher-level invariant this pin is
protecting.  Today board 04 ships at (post-#3433 / post-#3434 refresh):

  * 9/9 signal nets routed -- NRST is RECOVERED by the #3434
    target-aware in-pad stub (the prior #3286 stranding is gone).
  * 0 blocking errors.  The #3433 fixes close the environment-
    sensitive SWCLK/SWO -0.200 mm trace-overlap commits (seg-seg
    violators in the negotiated quality tuple, demote-to-partial
    safety net, overused-cells-only optimizer overflow tolerance)
    and the stitch straight-trace pad crossing (U2.8 -> U2.9).
  * 1 advisory connectivity finding: GND stitch with 3/18 pads
    stranded (U2.8/U2.23/U2.35, the LQFP-48 corner cluster of #3267;
    U2.23 is newly blocked by the recovered NRST B.Cu track -- a
    deliberate signal-over-stitch trade).
  * CI gate: PASS (0 <= floor of 0; advisory excluded per ``DRCChecker``).

This test is a stateless source pin -- it does NOT re-route the board.
It checks the committed routed PCB against the gate that ships with the
repository.  Re-routing is exercised by
``tests/test_board_04_routing_regression.py`` under the ``@slow`` mark.

References:
- ``scripts/ci/check_routed_drc.py`` -- the CI gate this test mirrors.
- ``.github/routed-drc-tolerance.yml`` -- the per-board allowlist + the
  ``manufacturers:`` override block.
- PR #3218 -- ``--mfr jlcpcb-tier1`` in the recipe (closes #3208).
- PR #3128 -- micro-via in-pad rescue (#3118), tightened floor 4 -> 1.
- PR #3286 -- documented NRST clearance regression, refreshed routed PCB,
  tightened floor 1 -> 0 (closes #3266).
- PR #3288 -- narrowed NC-pin plane-net classifier so stripped 2L recipe
  also reaches 8/9 (closes #3281, #3268).
- PR #3434 -- target-aware in-pad stub recovers NRST (9/9 reach,
  closes #3428, refs #3411).
- Issue #3433 -- seg-seg violators in the negotiated quality tuple +
  demote-to-partial safety net + scoped optimizer overflow tolerance +
  rect-aware stitch pad check; artifact refreshed at 9/9 with 0
  blocking errors (last blocker for #3411).
- Issue #2834 -- the manufacturing-ready cluster the residuals ride with.
- Issue #3298 -- post-#3286 refresh verification against current main.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_04_ROUTED_PCB = (
    REPO_ROOT / "boards" / "04-stm32-devboard" / "output" / "stm32_devboard_routed.kicad_pcb"
)
TOLERANCE_YAML = REPO_ROOT / ".github" / "routed-drc-tolerance.yml"
BOARD_04_RELPATH = "boards/04-stm32-devboard/output/stm32_devboard_routed.kicad_pcb"


def _load_tolerance_yaml() -> dict:
    assert TOLERANCE_YAML.exists(), (
        f"Tolerance YAML missing at {TOLERANCE_YAML}; the CI gate cannot "
        f"function without it."
    )
    return yaml.safe_load(TOLERANCE_YAML.read_text()) or {}


def _run_kct_check(pcb_path: Path, manufacturer: str) -> dict:
    """Invoke ``kct check`` and return the parsed JSON payload.

    Mirrors the CI gate's invocation in ``scripts/ci/check_routed_drc.py``
    so the verdict here matches what CI would see.
    """
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "check",
        str(pcb_path),
        "--mfr",
        manufacturer,
        "--errors-only",
        "--format",
        "json",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120, check=False)
    # ``kct check`` exit codes (see ``src/kicad_tools/cli/drc_cmd.py``):
    #   0 = clean (no errors), 1 = warnings only, 2 = errors present.
    # We accept 0, 1, and 2: the JSON payload is on stdout regardless of
    # exit code and we extract the verdict ourselves below.  Higher exit
    # codes indicate a tool crash.
    if proc.returncode not in (0, 1, 2):
        pytest.fail(
            f"kct check returned unexpected exit code {proc.returncode}\n"
            f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
            f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
        )
    # Strip any leading warning lines kct emits to stdout (e.g. "WARNING: PCB
    # out of sync with schematic").  The JSON payload starts at the first '{'.
    stdout = proc.stdout
    brace = stdout.find("{")
    assert brace >= 0, (
        f"kct check stdout has no JSON payload.  Full stdout:\n{stdout}"
    )
    try:
        return json.loads(stdout[brace:])
    except json.JSONDecodeError as exc:
        pytest.fail(f"Could not parse kct check JSON output: {exc}\n{stdout}")
        raise  # unreachable, satisfies typecheck


@pytest.fixture(scope="module")
def board_04_mfr_check() -> dict:
    """Invoke ``kct check`` once per session against the committed routed PCB."""
    if not BOARD_04_ROUTED_PCB.exists():
        pytest.skip(
            f"Board 04 routed PCB not found at {BOARD_04_ROUTED_PCB}; "
            "regenerate via `uv run python boards/04-stm32-devboard/generate_design.py`."
        )
    return _run_kct_check(BOARD_04_ROUTED_PCB, "jlcpcb-tier1")


def test_board_04_blocking_errors_within_ci_floor(board_04_mfr_check: dict) -> None:
    """Committed board 04 PCB must stay at or below the CI gate's allowlist floor.

    This is the primary regression guard: if a future router change adds
    a blocking DRC error that the CI gate does not allowlist, this pin
    fires immediately at PR time rather than after a fleet-status sweep.

    The current floor is 1 (post-#3118).  If the floor is tightened, the
    YAML and this test must move in lockstep -- the test reads the floor
    from the YAML so a YAML edit is sufficient.
    """
    summary = board_04_mfr_check.get("summary", {})
    violations = board_04_mfr_check.get("violations", [])

    # Count blocking (non-advisory) errors the same way the CI gate does.
    blocking = [
        v
        for v in violations
        if v.get("severity") == "error" and v.get("rule_id") != "connectivity"
    ]
    blocking_count = len(blocking)

    # Read the floor from the YAML rather than hard-coding it -- this
    # keeps the two sources of truth in lockstep when the floor is
    # tightened.
    tolerance = _load_tolerance_yaml()
    floor = (tolerance.get("tolerances") or {}).get(BOARD_04_RELPATH, 0)

    assert blocking_count <= floor, (
        f"Board 04 routed PCB has {blocking_count} blocking errors against "
        f"jlcpcb-tier1; the CI gate's allowlist floor is {floor}.\n\n"
        f"Blocking violations:\n"
        + "\n".join(
            f"  - {v.get('rule_id')}: {v.get('message')} at {v.get('location')}"
            for v in blocking
        )
        + f"\n\nIf a router change has legitimately added a new error,"
        f" bump the floor in {TOLERANCE_YAML.relative_to(REPO_ROOT)} and"
        f" file a follow-on issue to track the regression.  If the error"
        f" was inadvertent, revert the router change.\n\n"
        f"Total summary errors: {summary.get('errors')}"
    )


def test_board_04_advisory_connectivity_does_not_balloon(
    board_04_mfr_check: dict,
) -> None:
    """Advisory connectivity findings must stay within the documented bound.

    The post-#3433/#3434 committed PCB carries ONE advisory
    connectivity finding:

      1. The GND stitch gap (#3267): U2.8/U2.23/U2.35 LQFP-48 corner
         pads -- the standard 0.6 mm and 0.3 mm micro-vias both
         collide with neighbouring escape traces (U2.23's slot is
         occupied by the NRST B.Cu track that #3434 recovered).

    The prior NRST stranding finding (#3286) is GONE -- NRST is routed
    9/9 since #3434.

    Both are documented in ``.github/routed-drc-tolerance.yml``. If a
    future router change strands additional pads, the fleet-status
    manufacturer-readiness percentage drops -- catching this at PR time
    avoids silently regressing the board's functional completeness even
    though the CI gate (which filters advisories) still passes.

    The threshold is intentionally lenient (<= 3 connectivity findings)
    to avoid flapping on small router state changes; this catches
    multi-pad strandings (e.g., NRST + multiple GND pads dropping out)
    without breaking on every micro-routing tweak.
    """
    violations = board_04_mfr_check.get("violations", [])
    connectivity = [v for v in violations if v.get("rule_id") == "connectivity"]
    connectivity_count = len(connectivity)

    # Soft cap: 3 advisory findings.  Post-#3433 count is 1 (the GND
    # stitch gap; NRST is routed since #3434).  A jump to 4+ signals a
    # multi-net stranding event worth investigating.
    SOFT_CAP = 3
    assert connectivity_count <= SOFT_CAP, (
        f"Board 04 has {connectivity_count} advisory connectivity findings; "
        f"the soft cap is {SOFT_CAP}.  This signals a multi-net stranding "
        f"event -- check the recent router changes for a regression in "
        f"escape, stitching, or rip-up handling.\n\n"
        f"Advisory findings:\n"
        + "\n".join(
            f"  - {v.get('message')} at items={v.get('items')}"
            for v in connectivity
        )
    )


def test_board_04_tolerance_yaml_floor_matches_committed_state(
    board_04_mfr_check: dict,
) -> None:
    """The tolerance YAML's floor for board 04 must not silently drift.

    If a router improvement drops the committed PCB's blocking error
    count below the floor, the slack is harmless for CI (the gate still
    passes) but the floor is stale.  This test fails when the slack
    exceeds 2 (a single-step tightening window), prompting the next PR
    to tighten the floor in lockstep with the improvement.

    A tightening PR updates the YAML and this test continues to pass; a
    stale floor (slack > 2) prompts the maintainer to investigate why
    the floor is too loose.
    """
    violations = board_04_mfr_check.get("violations", [])
    blocking = [
        v
        for v in violations
        if v.get("severity") == "error" and v.get("rule_id") != "connectivity"
    ]
    blocking_count = len(blocking)

    tolerance = _load_tolerance_yaml()
    floor = (tolerance.get("tolerances") or {}).get(BOARD_04_RELPATH, 0)

    slack = floor - blocking_count
    SLACK_BUDGET = 2

    assert slack <= SLACK_BUDGET, (
        f"Board 04 floor in {TOLERANCE_YAML.relative_to(REPO_ROOT)} is "
        f"{floor} but the committed routed PCB only has {blocking_count} "
        f"blocking errors -- slack is {slack}, budget is {SLACK_BUDGET}.\n\n"
        f"Tighten the floor in the YAML to lock in the improvement, "
        f"e.g.:\n"
        f"  {BOARD_04_RELPATH}: {blocking_count}\n\n"
        f"See the comment block above this entry in the YAML for the "
        f"existing tightening history (#3028, #3118 etc.)."
    )


def test_board_04_tolerance_yaml_declares_jlcpcb_tier1() -> None:
    """The board 04 entry in the manufacturers: block must still target jlcpcb-tier1.

    The committed routed PCB uses ``--micro-via`` GND stitching that the
    default ``jlcpcb`` (tier-0) profile forbids.  The CI gate measures
    board 04 against ``jlcpcb-tier1`` via the ``manufacturers:`` block.
    If this entry is dropped or retargeted to a different tier, the
    floor logic above is measuring the wrong profile and would silently
    pass or fail for the wrong reasons.
    """
    tolerance = _load_tolerance_yaml()
    manufacturers = tolerance.get("manufacturers") or {}
    assert manufacturers.get(BOARD_04_RELPATH) == "jlcpcb-tier1", (
        f"{TOLERANCE_YAML.relative_to(REPO_ROOT)} no longer declares "
        f"board 04 against jlcpcb-tier1 in its manufacturers: block "
        f"(currently: {manufacturers.get(BOARD_04_RELPATH)!r}).\n\n"
        f"This board's --micro-via GND stitching requires jlcpcb-tier1's "
        f"Capability-Plus process.  If the board was retargeted to a "
        f"different fab tier, update:\n"
        f"  1. The ``manufacturers:`` block in this YAML.\n"
        f"  2. The ``--mfr`` argv in ``boards/04-stm32-devboard/"
        f"generate_design.py::run_drc``.\n"
        f"  3. This test (or remove it if board 04 leaves tier-1)."
    )


if __name__ == "__main__":  # pragma: no cover -- manual debugging convenience.
    pytest.main([__file__, "-v"])
