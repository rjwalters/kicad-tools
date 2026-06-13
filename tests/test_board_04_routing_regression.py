"""Regression test for ``boards/04-stm32-devboard/`` OSC_OUT stagnation.

Issue #2745 — Board 04 OSC_OUT stagnated at 8/9 (89%) on both 2L and 4L
attempts because the two-phase BLOCKED_BY_COMPONENT recovery gate was
keyed on ``overflow == 0``.  The OSC_IN escape produced ``overflow = 1``,
which gated recovery off; OSC_OUT had zero placed segments so the
standard rip-up scheduler (``find_nets_through_overused_cells``) could
not see it, and every iteration deterministically replayed the same
failure.  Layer escalation 2L -> 4L produced identical 8/9 results.

PR for #2745 drops the ``overflow == 0`` gate, so the
BLOCKED_BY_COMPONENT helper fires whenever ``stall_failed`` is non-empty.
Per-net ``stall_budget = 3`` prevents thrash.

This test pins the post-fix behavior:

- Board 04 must route at least 8/9 nets on the stripped 2L recipe
  (see ``REQUIRED_NETS_ROUTED`` for the current floor).
- The 2L attempt must succeed (no layer escalation should be needed).

Marked ``@pytest.mark.slow`` (single 2L attempt is ~60-90s; we set a
240s budget to leave generous slack for slower runners).  Nightly slow-
tests workflow at ``.github/workflows/slow-tests.yml`` (``-m slow``)
picks this up; PR-time CI excludes it.

Issue #3268 (2026-06-06) — the python-backend variant of this test
regressed from 9/9 to 4/9 (or 3/9 — minor nondeterminism observed) on
the stripped recipe.  Investigation showed the C++ backend on the same
stripped recipe also produces 4/9, so this is not python-specific; it
is a broader regression that surfaces here because the test deliberately
omits ``--micro-via-in-pad-fallback`` and the other production-pipeline
flags to isolate the #2745 recovery gate.

Issue #3281 (2026-06-07) — bisect identified PR #2931's
``_is_plane_net_pad`` classifier as the regression source: the
``pad.net == 0`` shortcut misclassified NC pins (which have
``net == 0 AND net_name == ""`` inherently, NOT because they were
rewritten by ``--skip-nets``) as plane pads, blocking same-component
signal escapes that ran past NC pins on board 04's STM32 LQFP-48 east
edge.  Narrowing the classifier to require an explicit plane-net
``net_name`` restored 8/9 on the C++ backend (matching the production-
recipe baseline).  This test now runs against the C++ backend (the
documented default); the residual NRST gap is tracked independently
per #3281's acceptance criteria.

Issue #3582 (2026-06-11) — the floor dropped 8/9 -> 7/9 at PR #3565
(``85b631f6``, closes #3545: reject routes through static foreign-pad
halos).  Bisect showed the prior 8/9 (last seen at ``c7cd574f``) was
achieved with ILLEGAL copper: the run carried 26 unresolved clearance
violations, including SWDIO and NRST traces overlapping foreign GND
pad metal at -0.337mm (negative distance = trace inside the pad) on
the STM32 LQFP-48 east edge — exactly the #3545 defect class.  With
static pad halos now non-negotiable, those corridors are gone and
SWDIO/NRST are honestly dropped instead of shipped as sub-clearance
copper.  This is a correctness win exposing a 2L capacity gap, NOT a
router defect, so the floor is re-pinned to 7/9 (do NOT revert #3565).
The legal-capacity recovery (>= 8/9 WITH zero pad-clearance
violations) is tracked in issue #3588; when it lands, restore
``REQUIRED_NETS_ROUTED = 8``.  This test now ALSO asserts the routed
result contains zero pad-OVERLAP violations (negative clearance) and
zero trace-vs-pad clearance violations
(``test_no_pad_overlap_violations``), so a future "fix" cannot
reclaim 8/9 by re-shipping illegal copper.

Issue #3592 (2026-06-13) — the single tolerated OSC_OUT-vs-GND
trace-vs-pad near-miss (reported at 0.163mm) was a FALSE POSITIVE in
``router/io.py::validate_routes``: it modelled every pad as a circle
of radius ``max(width, height) / 2``, so the STM32 LQFP-48 GND land
U2.8 (1.475 x 0.3mm) was treated as a 0.7375mm-radius disc instead of
its true 0.15mm half-height along the axis the OSC_OUT escape passes.
The validator now measures distance to the pad's true axis-aligned
rectangle (pad dimensions are already rotated into PCB space at load
time), the phantom violation is gone, and ``MAX_PAD_CLEARANCE_VIOLATIONS``
is tightened 1 -> 0.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
BOARD_DIR = REPO_ROOT / "boards" / "04-stm32-devboard"
UNROUTED_PCB = BOARD_DIR / "output" / "stm32_devboard.kicad_pcb"


# Issue #2745 acceptance criterion: originally 9/9 nets routed.
#
# Issue #3281 update: the post-#3128 unrouted PCB (regenerated 2026-05-26
# to incorporate the micro-via in-pad rescue support) shifted the
# routing surface enough that NRST no longer routes on the stripped 2L
# recipe with either backend.  The OSC_OUT regression that motivated
# #2745 is once again clear after fixing the NC-pin plane-net
# misclassification at ``router/grid.py::_is_plane_net_pad``
# (issue #3281), so this test now pins the post-fix floor of 8/9.
# The residual NRST gap on the stripped 2L recipe is the SAME defect
# as the C++ backend's NRST gap on the full production recipe
# (``--mfr jlcpcb-tier1 --auto-fix --auto-layers --auto-mfr-tier
# --placement-feedback --micro-via-in-pad-fallback``), and is tracked
# independently per the #3281 acceptance criteria.
#
# Issue #3582 update (2026-06-11): floor re-pinned 8 -> 7.  PR #3565
# (merge ``85b631f6``, closes #3545) made static foreign-pad halos
# non-negotiable in the C++ sharing A*.  Bisect proved the previous
# 8/9 baseline (last green at ``c7cd574f``, pre-#3565) was achieved
# with ILLEGAL copper: 26 unresolved clearance violations, including
# SWDIO at -0.337mm vs a GND pad and NRST at -0.337mm vs a GND pad
# (negative = trace overlapping foreign pad metal) on the STM32
# LQFP-48 east edge.  This test never asserted DRC-clean output, so
# the illegal 8/9 passed.  With the halos enforced, SWDIO/NRST have
# no legal 2L escape on this stripped recipe within the 240s budget
# and are honestly demoted -> 7/9 (deterministic).  Do NOT "fix" this
# by reverting #3565 — that re-ships copper overlapping pad metal.
# The legal-capacity recovery is tracked in issue #3588, whose
# acceptance criteria restore >= 8/9 WITH zero pad-clearance
# violations; bump this back to 8 when #3588 lands.
#
# Board 04 has 9 routable nets after schematic / PCB sync.
REQUIRED_NETS_ROUTED = 7
REQUIRED_NETS_TOTAL = 9


@pytest.fixture(scope="module")
def unrouted_pcb_path() -> Path:
    """Verify the committed unrouted board 04 PCB exists."""
    if not UNROUTED_PCB.exists():
        pytest.skip(
            f"Board 04 unrouted PCB not found at {UNROUTED_PCB!s}; "
            "regenerate via `uv run kct build boards/04-stm32-devboard --step pcb`"
        )
    return UNROUTED_PCB


def _parse_routed_net_count(stdout: str) -> tuple[int, int] | None:
    """Extract the final ``Nets routed: N/M`` count from kct route output.

    Returns ``(routed, total)`` or ``None`` if the line is absent
    (e.g., the router crashed before producing a summary).  Returns the
    LAST occurrence since escalation mode may produce multiple summary
    blocks.
    """
    pattern = re.compile(r"Nets routed:\s+(\d+)/(\d+)")
    matches = pattern.findall(stdout)
    if not matches:
        return None
    routed, total = matches[-1]
    return int(routed), int(total)


def _parse_pad_clearance_violations(stdout: str) -> tuple[int, list[tuple[str, float]]]:
    """Extract unresolved trace-vs-pad clearance violations from route output.

    ``kct route`` runs a pre-save validation pass
    (``router/io.py::validate_routes``) and prints a summary under the
    ``--- Pre-save Clearance Validation ---`` header via
    ``format_clearance_violations``::

        --- Pre-save Clearance Validation ---
          Found 5 clearance violation(s):
          pad: 1
          segment: 4
          [pad] OSC_OUT vs GND at (126.84, 122.75) on F.Cu: 0.163mm (required 0.200mm)

    The ``pad: N`` line is the per-obstacle-type breakdown of
    routing-caused (non-component-inherent) violations; the ``[pad]``
    detail lines carry the actual distances (negative = trace
    overlapping foreign pad metal).  The header is only printed when
    at least one violation exists, so an absent section means zero.

    Returns ``(pad_count, details)`` from the LAST validation section
    (escalation may print multiple), where ``details`` is a list of
    ``(detail_line, distance_mm)`` for each ``[pad]`` line.  Note the
    detail listing is capped at 20 lines by the formatter, so
    ``pad_count`` is authoritative for the total.
    """
    header = "Pre-save Clearance Validation"
    idx = stdout.rfind(header)
    if idx == -1:
        return 0, []
    section = stdout[idx:]
    # Stop at the next "--- ... ---" stage header to avoid matching
    # unrelated output further down (e.g. save / DRC summaries).
    next_stage = re.search(r"\n--- ", section)
    if next_stage:
        section = section[: next_stage.start()]
    count_match = re.search(r"^\s*pad:\s+(\d+)\s*$", section, re.MULTILINE)
    pad_count = int(count_match.group(1)) if count_match else 0
    details = [
        (m.group(0).strip(), float(m.group(1)))
        for m in re.finditer(
            r"^\s*\[pad\][^\n]*?:\s+(-?[\d.]+)mm \(required [\d.]+mm\)\s*$",
            section,
            re.MULTILINE,
        )
    ]
    return pad_count, details


# Issue #3582: the 7/9 result previously carried exactly ONE trace-vs-pad
# near-miss — OSC_OUT vs GND reported at 0.163mm (required 0.200mm) — and
# this bound tolerated it at 1.
#
# Issue #3592 (2026-06-13): that near-miss was a FALSE POSITIVE.  The
# segment-to-pad clearance check in ``router/io.py::validate_routes``
# modelled every pad as a circle of radius ``max(width, height) / 2``.
# The offending GND pad (STM32 LQFP-48 land U2.8, 1.475 x 0.3 mm) was
# therefore treated as a 0.7375 mm-radius disc, ~0.6 mm larger than its
# true 0.15 mm half-height along the axis the OSC_OUT escape passes.
# The real rectangular clearance is ~0.75 mm — well above the 0.200 mm
# requirement.  ``validate_routes`` now measures distance to the pad's
# true axis-aligned rectangle, so the phantom violation no longer
# appears and the recipe reports ZERO ``[pad]`` clearance violations.
# Pinned to 0 so any future regression that re-introduces a real
# trace-vs-pad encroachment (or reverts the rectangular pad model)
# fails immediately.  Note the 4 OSC_IN-vs-OSC_OUT crystal coupling
# near-misses are SEGMENT-segment, a different class not counted here.
MAX_PAD_CLEARANCE_VIOLATIONS = 0


@pytest.mark.slow
class TestBoard04OscOutRouting:
    """Pin >= ``REQUIRED_NETS_ROUTED``/9 routing on board 04 against the
    #2745 BLOCKED_BY_COMPONENT recovery fix (and the #3281 NC-pin
    plane-net misclassification fix), plus zero pad-overlap copper
    (issue #3582 / #3545).

    These tests run the full ``kct route`` CLI as a subprocess to
    exercise the same path the user invokes interactively.  The fixture
    runs once per session; each test asserts a different aspect to keep
    failure attribution sharp.

    Issue #3281: The test was previously skipped because the python
    backend regressed from 9/9 to 4/9 on this stripped recipe (and the
    C++ backend matched it at 4/9).  The root cause was the NC-pin
    misclassification at ``router/grid.py::_is_plane_net_pad`` (post
    PR #2931): NC pins inherit ``net == 0`` AND ``net_name == ""`` from
    the netlist parser, but the original classifier returned ``True``
    for any ``net == 0`` pad, which made the validator reject every
    same-component-perimeter signal escape that passed an NC pin --
    blocking SWDIO / SWO / NRST / BOOT0 escapes on board 04's STM32
    LQFP-48 east edge.  The fix narrows the classifier to require an
    explicit plane-net ``net_name``.  Both backends then routed 8/9 on
    this recipe (cpp) or 7/9 (python -- pure-Python backend is weaker
    than C++ and has additional cluster-routing limitations).

    The test now runs on the C++ backend (the documented default per
    #3268 commentary) so the assertion floor reflects production reality.

    Issue #3582: the cpp floor moved to 7/9 when PR #3565 stopped the
    router from shipping illegal copper through static pad halos; see
    the module docstring and ``REQUIRED_NETS_ROUTED`` commentary.
    """

    @pytest.fixture(scope="class")
    def route_stdout(self, unrouted_pcb_path: Path) -> str:
        """Run ``kct route --seed 42 ... --layers 2`` and capture stdout."""
        with tempfile.TemporaryDirectory() as td:
            pcb_copy = Path(td) / "stm32_devboard.kicad_pcb"
            shutil.copy2(unrouted_pcb_path, pcb_copy)
            cmd = [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "route",
                str(pcb_copy),
                "--seed",
                "42",
                "--no-auto-layers",
                "--layers",
                "2",
                "--manufacturer",
                "jlcpcb",
                "--timeout",
                "240",
                "--backend",
                "cpp",
            ]
            proc = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=360,
                check=False,
            )
            # ``kct route`` exit codes (see ``cli/route_cmd.py``):
            #   0 = full route + DRC clean
            #   2 = partial routing below --min-completion
            #   3 = >= min-completion but DRC violations remain
            #   4 = partial routing AND segment-segment clearance violations
            # We accept any non-fatal exit; specific assertions below
            # check the stdout for the actual net count.
            if proc.returncode in (1, 5):
                pytest.fail(
                    f"kct route returned fatal exit code {proc.returncode}\n"
                    f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                    f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
                )
            return proc.stdout

    def test_routes_all_nets_on_2l(self, route_stdout: str) -> None:
        """Board 04 must route at least 7/9 nets on the 2L attempt.

        Issue #2745: Before the BLOCKED_BY_COMPONENT recovery gate was
        relaxed, OSC_OUT stagnated at 8/9 routed.  After the fix, the
        initial pass's stall recovery sees OSC_OUT (zero placed
        segments) and engages destination-component sibling rip-up
        regardless of the OSC_IN-driven ``overflow = 1``.

        Issue #3281: The same recipe regressed to 4/9 after PR #2931
        (the validator's NC-pin plane-net misclassification rejected
        SWDIO / SWO / NRST / BOOT0 escapes).  Narrowing the classifier
        at ``router/grid.py::_is_plane_net_pad`` restores 8/9 on the
        C++ backend (matching the production-recipe baseline).  The
        residual NRST gap is tracked independently.

        Issue #3582: Floor re-pinned 8 -> 7 after PR #3565 (#3545)
        made static foreign-pad halos non-negotiable.  The prior 8/9
        relied on SWDIO/NRST overlapping foreign GND pad metal at
        -0.337mm; the legal-capacity recovery to >= 8/9 (with zero
        pad-clearance violations) is tracked in issue #3588.
        """
        parsed = _parse_routed_net_count(route_stdout)
        assert parsed is not None, (
            "Could not find 'Nets routed: N/M' line in kct route output. "
            f"Last 2000 chars of stdout:\n{route_stdout[-2000:]}"
        )
        routed, total = parsed
        assert routed >= REQUIRED_NETS_ROUTED, (
            f"Board 04 routed only {routed}/{total} nets (expected "
            f"{REQUIRED_NETS_ROUTED}/{REQUIRED_NETS_TOTAL}).  This is the "
            "issue #2745 OSC_OUT stagnation pattern: a net with zero "
            "placed segments is invisible to the standard rip-up "
            "scheduler, and the BLOCKED_BY_COMPONENT recovery is the "
            "only mechanism that can free it.  Check that the recovery "
            "gate in TwoPhaseRouter._detailed_negotiated still fires "
            "for stall_failed regardless of overflow."
        )
        assert total == REQUIRED_NETS_TOTAL, (
            f"Board 04 reported {total} routable nets but the test "
            f"expected {REQUIRED_NETS_TOTAL}.  If the schematic or "
            "placement changed and the net count drifted, update "
            "REQUIRED_NETS_TOTAL in this test."
        )

    def test_no_pad_overlap_violations(self, route_stdout: str) -> None:
        """The routed result must contain ZERO pad-OVERLAP violations.

        Issue #3582: The pre-#3565 8/9 baseline shipped 26 unresolved
        clearance violations, including SWDIO and NRST traces
        overlapping foreign GND pad metal at -0.337mm (negative
        distance = trace INSIDE the pad) — the test never asserted
        clean copper, so the illegal result passed.  This assertion
        closes that hole: a future change cannot reclaim a higher net
        count by routing through static pad halos again (the #3545
        defect class).  Issue #3588's recovery to >= 8/9 must keep
        this clean.

        Two checks against the kct route pre-save clearance validation
        summary (routing-caused violations only; component-inherent
        pad spacings are already excluded by
        ``format_clearance_violations``):

        1. No ``[pad]`` violation with negative distance (trace
           overlapping foreign pad metal) — the #3545 illegal-copper
           signature.
        2. Total pad-violation count <= ``MAX_PAD_CLEARANCE_VIOLATIONS``
           (now 0 — issue #3592 fixed the circular-pad false positive
           that previously reported the OSC_OUT-vs-GND near-miss at
           0.163mm; the real rectangular clearance is ~0.75mm).  This
           pins against growth AND guarantees every pad violation
           appears in the capped detail listing, so check 1 cannot be
           evaded by volume.
        """
        pad_count, details = _parse_pad_clearance_violations(route_stdout)

        overlaps = [(line, dist) for line, dist in details if dist < 0]
        assert not overlaps, (
            f"Routed result contains {len(overlaps)} trace-vs-pad OVERLAP "
            "violation(s) (negative clearance = copper inside foreign pad "
            "metal) — the #3545 defect class that PR #3565's static-halo "
            "enforcement eliminated.  A net-count improvement achieved this "
            "way is ILLEGAL copper, not a routing win; see issues "
            "#3582/#3588.  Offending violations:\n" + "\n".join(f"  {line}" for line, _ in overlaps)
        )

        assert pad_count <= MAX_PAD_CLEARANCE_VIOLATIONS, (
            f"Routed result reports {pad_count} unresolved trace-vs-pad "
            f"clearance violation(s); the pinned maximum is "
            f"{MAX_PAD_CLEARANCE_VIOLATIONS} (the known OSC_OUT-vs-GND "
            "0.163mm near-miss).  New pad-clearance violations suggest "
            "traces are encroaching on foreign pads again — see issues "
            "#3545/#3582/#3588 before relaxing this bound.  Detail lines:\n"
            + "\n".join(f"  {line}" for line, _ in details)
            + "\nPre-save validation section (last 3000 chars of stdout):\n"
            + route_stdout[-3000:]
        )
