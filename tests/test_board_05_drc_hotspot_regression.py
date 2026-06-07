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
* The committed routed PCB at HEAD ships with **6 ``clearance_pad_segment``
  violations** under jlcpcb rules, all at U3-30 (ISENSE_B+),
  U10-12 (HALL_B), and U10-3 (OSC_OUT).  A future fix targeting either
  hot-spot should drop the count for THAT hot-spot; a fix that
  accidentally moves the violations to a NEW pin instead of removing
  them should be caught loudly.

The test does NOT pin the exact set of violating pins (that would be
too brittle).  It asserts:

1. The total ``clearance_pad_segment`` count is at-or-below 6 (the
   measured floor at the time issue #3251 closed).
2. All ``clearance_pad_segment`` violations are at fine-pitch hot-spot
   components (U3, U10, R10-R12 current-sense resistors); a violation
   somewhere ELSE is treated as a new-mechanism regression.
3. The violation shortfalls are within the documented band (< 130µm);
   a violation OUTSIDE that band points at a new mechanism that should
   be triaged before merging.

Updating this test:

* If a router improvement drops the count below 6, tighten ``MAX_PAD_SEGMENT``
  in the same PR so the new floor is enforced.
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
# routed PCB.  The current measurement (Issue #3251, 2026-06-06) is 6.
# Lower this when a router fix drops the count.
MAX_PAD_SEGMENT = 6

# Component references where ``clearance_pad_segment`` is expected
# (fine-pitch escapes and current-sense passive 0402s on board 05).
# A violation at a reference NOT in this set is a new mechanism.
HOT_SPOT_REFS = frozenset({"U3", "U10", "R10", "R11", "R12"})

# Maximum measured shortfall on the committed PCB (Issue #3251, 2026-06-06):
# 113um at U10-3 (OSC_OUT vs OSC_IN at the LQFP-32 oscillator pins).
# Bump in the same PR if a new mechanism legitimately produces a larger
# shortfall, with a tracking-issue link in the PR description.
MAX_SHORTFALL_UM = 130


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
            f"{MAX_PAD_SEGMENT} (Issue #3251 floor).  This indicates a "
            f"regression in the fine-pitch escape / pad-halo / clearance-"
            f"kernel paths (#3225 / #3232 / #3248 / #3250).  Investigate "
            f"before raising the floor."
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

        The committed file's largest shortfall is 113um at U10-3
        (OSC_OUT / OSC_IN at the oscillator load network).  A shortfall
        above 130um points at a structural failure (trace centerline on
        pad metal, ``actual=0``), which is the cpp-backend's
        ``U3-33 ISENSE_A+ vs ISENSE_A-`` mechanism — that path should
        not exist on the python-routed committed file.  Catch it
        loudly if it appears.
        """
        viols = _kct_check_violations(routed_pcb_path)
        for v in viols:
            if v.get("rule_id") != "clearance_pad_segment":
                continue
            actual = v.get("actual_value")
            required = v.get("required_value")
            if not isinstance(actual, (int, float)) or not isinstance(
                required, (int, float)
            ):
                # Defensive: skip entries missing the metadata we need.
                continue
            shortfall_um = (required - actual) * 1000.0
            assert shortfall_um <= MAX_SHORTFALL_UM, (
                f"Board 05 clearance_pad_segment shortfall {shortfall_um:.1f}um "
                f"exceeds documented max {MAX_SHORTFALL_UM}um. "
                f"actual={actual*1000:.0f}um required={required*1000:.0f}um "
                f"items={v.get('items')!r}.  Likely a new mechanism — "
                f"investigate before merge (Issue #3251 hot-spot table)."
            )
