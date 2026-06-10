"""Regression guard for board-05's residual ``clearance_pad_segment`` hot-spots.

Issue #3251 — Make board 05 bldc-motor-controller manufacturable.

This test complements :mod:`test_board_05_drc_allowlist` by pinning the
*shape* of the residual violations (which footprints / pins they cluster
on, and the shortfall band they fall into), not just the aggregate count.

Why a separate test:

* :mod:`test_board_05_drc_allowlist` pins the *count* against the
  allowlist (currently 9).  A regression that changes WHICH pins are
  violated but keeps the count under 9 would slip through that gate.
* The post-Wave-3 investigation (PRs #3232 / #3248 / #3249 / #3250)
  identified U3 (HTSSOP-56, 0.5mm pitch) and U10 (LQFP-32, 0.8mm pitch)
  as the dominant residual hot-spots on this board.  Issue #3251 traced
  the cpp-backend mechanism (27µm shortfall band at actual=100µm) to the
  in-pad rescue's "Proceeding anyway" path on tier-1 manufacturers, NOT
  to the auto-grid resolution selector as initially hypothesized.
* Issue #3425 (2026-06-10) switched the board recipe to jlcpcb-tier1 +
  cpp backend + 4 layers with ``--micro-via-in-pad-fallback``, and this
  test now measures against ``--mfr jlcpcb-tier1`` (the profile the
  board routes and is CI-gated against).  The committed routed PCB
  ships with **0 ``clearance_pad_segment`` violations**: the 0.3 mm
  micro-via in-pad rescues fit the DRV8301's 0.3 mm-wide pads without
  clipping neighbours, eliminating the historical U3/U10 escape
  hot-spots.  The single residual blocking violation is 1
  ``clearance_segment_segment`` (ISENSE_A- / ISENSE_B- partial-route
  stub overlap on In1.Cu at (18.75, 53.75)) -- gated by the allowlist
  (= 1) in ``.github/routed-drc-tolerance.yml`` and tracked in the
  #3425 follow-on issues.

The test does NOT pin the exact set of violating pins (that would be
too brittle).  It asserts:

1. The total ``clearance_pad_segment`` count is 0 (the measured floor
   after issue #3425; was 6 under the pre-#3425 2L python recipe).
2. All ``clearance_pad_segment`` violations are at fine-pitch hot-spot
   components (U3, U10, R10-R12 current-sense resistors); a violation
   somewhere ELSE is treated as a new-mechanism regression.  (Vacuous
   at the current 0-count; retained so a future regression that
   reintroduces the rule also gets shape-checked.)
3. The violation shortfalls are within the documented band (< 130µm);
   a violation OUTSIDE that band points at a new mechanism that should
   be triaged before merging.  (Also vacuous at 0.)

Updating this test:

* ``MAX_PAD_SEGMENT`` is 0 -- any reappearance of the rule is a
  regression in the micro-via in-pad fallback / escape paths and
  must be triaged, not allowlisted.
* If a placement / library change legitimately moves the hot-spot to a
  new pin (e.g., U3 footprint switched from HTSSOP-56 to QFN-56), update
  ``HOT_SPOT_REFS`` in the same PR with reviewer justification.
* If a new violation mechanism produces shortfalls > 130µm, update
  ``MAX_SHORTFALL_UM`` in the same PR and file a follow-up to investigate.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "05-bldc-motor-controller"
ROUTED_PCB = BOARD_DIR / "output" / "bldc_controller_routed.kicad_pcb"

# Maximum allowed ``clearance_pad_segment`` violations on the committed
# routed PCB.  History: 6 (Issue #3251, 2026-06-06, 2L python recipe at
# base jlcpcb); 0 since Issue #3425 (2026-06-10, jlcpcb-tier1 + cpp +
# 4L + --micro-via-in-pad-fallback recipe measured at jlcpcb-tier1).
MAX_PAD_SEGMENT = 0

# Component references where ``clearance_pad_segment`` is expected
# (fine-pitch escapes and current-sense passive 0402s on board 05).
# A violation at a reference NOT in this set is a new mechanism.
HOT_SPOT_REFS = frozenset({"U3", "U10", "R10", "R11", "R12"})

# Maximum measured shortfall on the committed PCB.  History:
# * Issue #3251 (2026-06-06): 113um at U10-3 (OSC_OUT vs OSC_IN) -> 130.
# * Issue #3423 (2026-06-09): the U3 rotation + artifact refresh moved
#   the hot-spot entirely onto U3's south edge; worst is 139um at U3-36
#   (GATE_DRV_CH trace overlapping the PHASE_C pad by 12um) -> 150.
# * Issue #3425 (2026-06-10): 0 clearance_pad_segment violations on the
#   tier1 + micro-via-fallback snapshot -- the band check is vacuous;
#   re-tightened to the historical 130 so a regression that
#   reintroduces the rule is also band-checked against the
#   pre-#3423 norm.
MAX_SHORTFALL_UM = 130

# Rule families that the committed PCB at HEAD does NOT exhibit.
#
# History: Issue #3294 (2026-06-07) pinned ``clearance_segment_segment``
# and ``clearance_segment_via`` as absent, because the then-committed
# file was a better-than-average historical snapshot that fresh
# re-routes could not reproduce (fresh = 11 blocking vs committed 6).
#
# Issue #3423 (2026-06-09): the U3 rotation moved all 56 U3 pads, so
# the unreproducible snapshot HAD to be refreshed; the refreshed file
# carried 1 segment_segment + 3 segment_via, so both entries were
# dropped per the refresh policy.
#
# Issue #3425 (2026-06-10): re-derived from the tier1 + cpp + 4L +
# micro-via-fallback snapshot measured at ``--mfr jlcpcb-tier1``:
#
# * ``clearance_pad_via`` / ``clearance_via_via``: absent BECAUSE OF
#   ``--micro-via-in-pad-fallback`` -- without it the 0.6 mm in-pad
#   rescue vias on U3's 0.5 mm-pitch pads produce 21 + 8 violations
#   (clipping neighbouring foreign-net pads).  A regression here means
#   the fallback stopped engaging.
# * ``clearance_segment_via``: absent on the measured snapshot.
#
# Issue #3470 (2026-06-10): ``clearance_segment_segment`` re-pinned
# ABSENT.  The exactly-1 violation the #3425 snapshot carried
# (ISENSE_A-/ISENSE_B- partial-route stub overlap on In1.Cu at
# (18.75, 53.75), actual -0.3135 mm) was traced to the in-pad escape
# generator emitting mutually-overlapping inner stubs for U3 pins
# 31/33.  #3470 made the stub generator conflict-aware
# (escape.py ``_try_in_pad_escape`` + ``_in_pad_stub_conflicts``) and
# the BLOCKED_BY_COMPONENT rip-up transactional (negotiated.py
# ``targeted_ripup`` snapshot/rollback), so partial outputs no longer
# strand overlap copper.  The refreshed committed snapshot measures 0
# blocking violations at jlcpcb-tier1; the board-05 allowlist entry in
# .github/routed-drc-tolerance.yml was removed (strict 0 gate).
ABSENT_RULES_ON_COMMITTED_PCB: frozenset[str] = frozenset(
    {
        "clearance_pad_via",
        "clearance_via_via",
        "clearance_segment_via",
        "clearance_segment_segment",
    }
)


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


def _kct_check_violations(pcb_path: Path) -> list[dict]:
    """Run ``kct check --format json`` and return the violations list."""
    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "check",
        str(pcb_path),
        # Issue #3425: board 05 routes + DRC-gates against jlcpcb-tier1
        # (Capability-Plus legalizes the DRV8301 in-pad rescue vias).
        # Matches design.py route_pcb() and the manufacturers: override
        # in .github/routed-drc-tolerance.yml.
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
    # Exit 0 = no errors; 2 = errors found; 1 = tool failure.
    if proc.returncode == 1:
        raise RuntimeError(
            f"kct check failed on {pcb_path} (exit 1).\nstderr:\n{proc.stderr.strip()}"
        )
    if proc.returncode not in (0, 2):
        raise RuntimeError(
            f"kct check unexpected exit code {proc.returncode}.\nstderr:\n{proc.stderr.strip()}"
        )
    try:
        data = json.loads(proc.stdout)
    except json.JSONDecodeError as e:
        raise RuntimeError(
            f"kct check produced invalid JSON: {e}\nstdout (first 500): {proc.stdout[:500]}"
        ) from e
    return data.get("violations", [])


def _extract_ref_from_items(items: list[str]) -> str | None:
    """Pull a component ref like 'U3' out of an item entry like 'U3-30'.

    Item entries are of the form ``"<ref>-<pin>"`` for pad references and
    ``"Trace-<hex>"`` for trace IDs.  We only return the pad-side ref so
    callers can map a violation to a footprint.
    """
    for it in items:
        if "-" in it and not it.startswith("Trace-"):
            return it.split("-", 1)[0]
    return None


class TestBoard05DRCHotspotRegression:
    """Pin the *shape* of board-05's residual ``clearance_pad_segment``."""

    def test_pad_segment_count_at_or_below_documented_floor(
        self,
        routed_pcb_path: Path,
    ) -> None:
        """Count of ``clearance_pad_segment`` is bounded by ``MAX_PAD_SEGMENT``.

        Distinct from :class:`TestBoard05DRCAllowlistGuard` (which gates
        the *total* error count): this test isolates the rule that the
        Wave-1/2/3 work targeted (#3225 / #3227 / #3232 / #3248 / #3250).
        A regression in any of those mechanisms typically increases this
        rule's count specifically.
        """
        viols = _kct_check_violations(routed_pcb_path)
        pad_seg = [v for v in viols if v.get("rule_id") == "clearance_pad_segment"]
        assert len(pad_seg) <= MAX_PAD_SEGMENT, (
            f"Board 05 routed PCB reports {len(pad_seg)} "
            f"clearance_pad_segment violation(s); max allowed is "
            f"{MAX_PAD_SEGMENT} (Issue #3425 floor under jlcpcb-tier1 + "
            f"micro-via in-pad fallback).  This indicates a regression in "
            f"the fine-pitch escape / pad-halo / clearance-kernel / "
            f"micro-via-fallback paths (#3225 / #3232 / #3248 / #3250 / "
            f"#3118).  Investigate before raising the floor."
        )

    def test_pad_segment_violations_at_known_hotspots_only(
        self,
        routed_pcb_path: Path,
    ) -> None:
        """Every ``clearance_pad_segment`` is at a known hot-spot ref.

        If a violation appears at a ref NOT in ``HOT_SPOT_REFS``, that's
        a new mechanism and should be triaged before merge.  The
        hot-spot list pins U3 (HTSSOP-56), U10 (LQFP-32), and the
        R10-R12 current-sense 0402 resistors that the python-backend
        escape clips against.
        """
        viols = _kct_check_violations(routed_pcb_path)
        pad_seg = [v for v in viols if v.get("rule_id") == "clearance_pad_segment"]
        offenders: list[tuple[str, list[str]]] = []
        for v in pad_seg:
            items = v.get("items", [])
            ref = _extract_ref_from_items(items)
            if ref is None or ref not in HOT_SPOT_REFS:
                offenders.append((ref or "?", items))
        assert not offenders, (
            f"Board 05 has clearance_pad_segment violations at unexpected "
            f"refs (not in {sorted(HOT_SPOT_REFS)!r}): {offenders!r}.  This "
            f"indicates a new violation mechanism — investigate (likely "
            f"a placement / footprint / library change moved the hot-"
            f"spot) before raising the hot-spot allowlist."
        )

    def test_pad_segment_shortfalls_within_documented_band(
        self,
        routed_pcb_path: Path,
    ) -> None:
        """All ``clearance_pad_segment`` shortfalls are ≤ ``MAX_SHORTFALL_UM``.

        Vacuous on the current 0-violation snapshot (Issue #3425);
        retained so a regression that reintroduces the rule is also
        band-checked.  A shortfall above 130um points at a structural
        failure (trace centerline on pad metal, ``actual=0``) rather
        than the historical 13-27um grid-quantization band.  Catch it
        loudly if it appears.
        """
        viols = _kct_check_violations(routed_pcb_path)
        for v in viols:
            if v.get("rule_id") != "clearance_pad_segment":
                continue
            actual = v.get("actual_value")
            required = v.get("required_value")
            if not isinstance(actual, (int, float)) or not isinstance(required, (int, float)):
                # Defensive: skip entries missing the metadata we need.
                continue
            shortfall_um = (required - actual) * 1000.0
            assert shortfall_um <= MAX_SHORTFALL_UM, (
                f"Board 05 clearance_pad_segment shortfall {shortfall_um:.1f}um "
                f"exceeds documented max {MAX_SHORTFALL_UM}um. "
                f"actual={actual * 1000:.0f}um required={required * 1000:.0f}um "
                f"items={v.get('items')!r}.  Likely a new mechanism — "
                f"investigate before merge (Issue #3251 hot-spot table)."
            )

    def test_committed_pcb_absent_rule_families(
        self,
        routed_pcb_path: Path,
    ) -> None:
        """Rule families in ``ABSENT_RULES_ON_COMMITTED_PCB`` stay absent.

        Issue #3425 measurement (2026-06-10): the committed tier1 + cpp
        + 4L + micro-via-fallback snapshot carries NO
        ``clearance_pad_via`` / ``clearance_via_via`` /
        ``clearance_segment_via`` violations.  The first two are the
        signature of the micro-via in-pad fallback NOT engaging: the
        same recipe without ``--micro-via-in-pad-fallback`` produces
        21 pad_via + 8 via_via violations from 0.6 mm rescue vias
        clipping U3's neighbouring 0.5 mm-pitch pads.  Pinning their
        absence makes that regression loud even while the aggregate
        allowlist would otherwise tolerate a count drift.

        Refresh policy: when a recipe/router change legitimately alters
        the committed snapshot's rule mix, re-route AND re-derive
        :data:`ABSENT_RULES_ON_COMMITTED_PCB` in the same PR.
        """
        viols = _kct_check_violations(routed_pcb_path)
        offenders: dict[str, int] = {}
        for v in viols:
            rid = v.get("rule_id")
            if rid in ABSENT_RULES_ON_COMMITTED_PCB:
                offenders[rid] = offenders.get(rid, 0) + 1
        assert not offenders, (
            f"Board 05 committed routed PCB now reports rule families "
            f"that the #3425 baseline snapshot does not have: "
            f"{offenders!r}.  pad_via/via_via reappearing usually means "
            f"the --micro-via-in-pad-fallback rescue stopped engaging "
            f"(0.6 mm in-pad vias clipping U3 neighbours).  Either "
            f"revert the routed-PCB change or, if the re-route is "
            f"intentional and strictly better, re-derive "
            f"ABSENT_RULES_ON_COMMITTED_PCB in the same PR."
        )
