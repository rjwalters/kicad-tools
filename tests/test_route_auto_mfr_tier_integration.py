"""End-to-end integration test for ``kct route --auto-mfr-tier`` (Issue #2885).

PR #2882 shipped ``--auto-mfr-tier`` with 62 unit tests, but every dispatch
test in ``test_route_auto_mfr_tier.py`` mocks ``route_with_layer_escalation``.
The full chain:

    fine-pitch LQFP-48 + jlcpcb
        -> routing hits PIN_ACCESS
        -> ``EscapeRouter`` bumps ``missed_via_in_pad_rescues``
        -> outer loop sees the signal
        -> escalates to ``jlcpcb-tier1`` (via-in-pad available)
        -> routing succeeds

is **not exercised by any test prior to this one**.  Judge follow-up
observation D on PR #2882 explicitly flagged the gap as deliberate scope-cut
for follow-up.

This test pins the post-#2882 behavior by driving the full CLI subprocess
path on the board-04 STM32 development board (LQFP-48 0.5mm pitch + ground
planes), which is the canonical real-board fixture for the chain.

Acceptance criteria (from issue #2885):

1. Routing succeeds (>= REQUIRED_NETS_ROUTED) after escalation, not before.
2. The CLI advances to the ``jlcpcb-tier1`` tier (per-tier banner visible
   in stdout); this is the AC#2 equivalent of "final ``args.manufacturer``
   is ``jlcpcb-tier1``".
3. The cost-note recommendation line is emitted to stdout.
4. The jlcpcb tier attempt (within the same ``--auto-mfr-tier`` run) falls
   short of the jlcpcb-tier1 attempt's completion -- the contrast acts as
   the regression-anchor that the escalation is actually doing work.

Marked ``@pytest.mark.slow`` -- the chain exercises real routing on the
full LQFP-48 + crystal + LDO + SWD-header board (~3-5 minutes wall-clock).
PR-time CI excludes ``-m slow``; the nightly slow-tests workflow at
``.github/workflows/slow-tests.yml`` picks this up.

We constrain layer escalation to ``--max-layers 2`` so each tier attempt
runs once at 2L rather than iterating 2L -> 4L -> 6L per tier (which
exhausts the wall-clock budget before the second tier can start).  This
is the minimum surface needed to exercise the mfr-tier escalation chain:
2L jlcpcb fails on inner LQFP-48 pins (no via-in-pad), 2L jlcpcb-tier1
succeeds with in-pad vias landing on B.Cu.
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

# Board 04 has 9 routable nets after schematic / PCB sync.  The chain is
# bottlenecked on inner-pin escapes for U2 (STM32F103C8T6 LQFP-48 0.5mm).
# On ``jlcpcb-tier1`` (via-in-pad capable) the chain completes 9/9.
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

    Returns ``(routed, total)`` from the LAST occurrence since escalation
    mode produces multiple summary blocks (one per tier).  Returns ``None``
    if no summary line is present.
    """
    pattern = re.compile(r"Nets routed:\s+(\d+)/(\d+)")
    matches = pattern.findall(stdout)
    if not matches:
        return None
    routed, total = matches[-1]
    return int(routed), int(total)


def _split_by_tier(stdout: str) -> dict[str, str]:
    """Split the ``--auto-mfr-tier`` stdout into per-tier sub-strings.

    ``route_with_mfr_tier_escalation`` prints a banner of the form
    ``Tier N/M: <tier-name>`` before each inner attempt.  We slice the
    stdout on those banners so individual assertions can inspect just
    one tier's output (e.g. "did the jlcpcb attempt produce a missed-
    via-in-pad signal?").

    Returns a dict mapping tier-name -> sub-stdout.  Tiers that never
    ran (because the loop terminated early) are absent from the dict.
    """
    # Match "Tier 1/2: jlcpcb" and "Tier 2/2: jlcpcb-tier1" style banners.
    banner_re = re.compile(r"Tier\s+\d+/\d+:\s+(\S+)")
    matches = list(banner_re.finditer(stdout))
    if not matches:
        return {}
    result: dict[str, str] = {}
    for i, m in enumerate(matches):
        start = m.end()
        end = matches[i + 1].start() if i + 1 < len(matches) else len(stdout)
        result[m.group(1)] = stdout[start:end]
    return result


def _run_route_auto_mfr_tier(
    unrouted_pcb_path: Path,
    *,
    timeout_seconds: int = 480,
) -> subprocess.CompletedProcess[str]:
    """Run ``kct route --auto-mfr-tier`` on a copy of the board-04 PCB.

    Args:
        unrouted_pcb_path: Source unrouted PCB (board-04 committed artifact).
        timeout_seconds: Total wall-clock budget passed via ``--timeout``.
            The two-tier ladder (jlcpcb + jlcpcb-tier1) at ``--max-layers 2``
            typically completes in ~150-300s; 480s gives generous slack
            for slow runners.

    Returns the completed subprocess so callers can inspect both the
    return code and the captured stdout/stderr.
    """
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
            "--max-layers",
            "2",
            "--manufacturer",
            "jlcpcb",
            "--timeout",
            str(timeout_seconds),
            "--backend",
            "python",
            "--auto-mfr-tier",
        ]
        # subprocess timeout = wall-clock budget + setup overhead slack.
        wall_clock = timeout_seconds + 120
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=wall_clock,
            check=False,
        )


@pytest.mark.slow
class TestAutoMfrTierIntegration:
    """End-to-end chain test: jlcpcb fails -> escalate to jlcpcb-tier1 -> success.

    A single ``--auto-mfr-tier`` subprocess invocation produces both:
      - the failing jlcpcb tier attempt (AC #4 regression-anchor evidence)
      - the successful jlcpcb-tier1 escalation (AC #1, #2, #3 evidence)

    Running a single subprocess (vs separate auto-mfr-tier + anchor runs)
    keeps the slow-tests budget tractable while still proving the full
    chain works.
    """

    @pytest.fixture(scope="class")
    def auto_mfr_tier_result(
        self, unrouted_pcb_path: Path
    ) -> subprocess.CompletedProcess[str]:
        """Run with ``--auto-mfr-tier --max-layers 2`` and capture output."""
        proc = _run_route_auto_mfr_tier(unrouted_pcb_path, timeout_seconds=480)
        if proc.returncode in (1, 5):
            pytest.fail(
                f"kct route --auto-mfr-tier returned fatal exit code "
                f"{proc.returncode}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
            )
        return proc

    @pytest.fixture(scope="class")
    def per_tier_stdout(
        self, auto_mfr_tier_result: subprocess.CompletedProcess[str]
    ) -> dict[str, str]:
        """Split the captured stdout by per-tier banners."""
        return _split_by_tier(auto_mfr_tier_result.stdout)

    # ------------------------------------------------------------------
    # AC #1: Routing succeeds after escalation
    # ------------------------------------------------------------------

    def test_routes_all_nets_with_auto_mfr_tier(
        self, auto_mfr_tier_result: subprocess.CompletedProcess[str]
    ) -> None:
        """With ``--auto-mfr-tier`` the chain completes all nets after
        escalating to ``jlcpcb-tier1``.

        Without via-in-pad, inner LQFP-48 pins surrounded by ground / VDD
        plane pads cannot escape on the surface; the tier-1 escalation
        unlocks in-pad vias and the chain closes.  We require >= 8/9
        nets routed -- the last partial net (OSC_OUT 2/3 pads on
        per-pad measure) is still an open item tracked under #2695 even
        post-#2889, but tier-level routing should reach >= 8/9 connected
        nets on the 2L stack.
        """
        parsed = _parse_routed_net_count(auto_mfr_tier_result.stdout)
        assert parsed is not None, (
            "Could not find 'Nets routed: N/M' line in --auto-mfr-tier "
            "stdout.  Last 2000 chars:\n"
            f"{auto_mfr_tier_result.stdout[-2000:]}"
        )
        routed, total = parsed
        # We require >= 8/9 because the OSC_OUT 2/3 pad completion gap
        # tracked in #2695 is a per-pad (not per-net) residual on 2L.
        # Tier-1 escalation should still close enough nets that the
        # contrast with the failing tier-0 attempt (typically 2-3/9
        # connected) is unambiguous.
        assert routed >= 8, (
            f"--auto-mfr-tier escalation completed only {routed}/{total} "
            f"nets (expected >= 8/{REQUIRED_NETS_TOTAL}).\n"
            "This is the issue #2881 chain regression: the outer mfr-tier "
            "escalation loop should walk jlcpcb -> jlcpcb-tier1 and the "
            "tier-1 attempt should engage in-pad vias for the inner LQFP-48 "
            "pins.  Check that:\n"
            "  - ``missed_via_in_pad_rescues`` is incremented on the jlcpcb "
            "attempt (see router/escape.py).\n"
            "  - ``route_with_mfr_tier_escalation`` reads that counter from "
            "``args._last_router._escape_router`` after the inner call.\n"
            "  - The convergence guard allows the jlcpcb -> jlcpcb-tier1 "
            "step (capability gain via via_in_pad_supported).\n"
            f"\nLast 2000 chars of stdout:\n"
            f"{auto_mfr_tier_result.stdout[-2000:]}"
        )

    # ------------------------------------------------------------------
    # AC #2: CLI advances to jlcpcb-tier1
    # ------------------------------------------------------------------

    def test_escalation_advances_to_tier1(
        self,
        auto_mfr_tier_result: subprocess.CompletedProcess[str],
        per_tier_stdout: dict[str, str],
    ) -> None:
        """The CLI must advance to the ``jlcpcb-tier1`` attempt.

        ``route_with_mfr_tier_escalation`` prints a per-tier banner of the
        form ``Tier N/M: <tier-name>``.  We assert that the
        ``jlcpcb-tier1`` banner appears, indicating the escalation step
        actually fired (not that it was short-circuited at the convergence
        guard or the deadline).
        """
        assert "jlcpcb-tier1" in per_tier_stdout, (
            "Expected per-tier banner 'Tier N/M: jlcpcb-tier1' in stdout, "
            "indicating the mfr-tier escalation actually advanced off the "
            "starting jlcpcb tier.  Without this, the loop short-circuited "
            "(e.g. convergence guard suppressed the step, wall-clock "
            "deadline expired before the second tier started, or the inner "
            "jlcpcb attempt returned 0 trivially).  Per-tier banners found: "
            f"{list(per_tier_stdout.keys())}\n"
            f"\nLast 3000 chars of stdout:\n"
            f"{auto_mfr_tier_result.stdout[-3000:]}"
        )

    # ------------------------------------------------------------------
    # AC #3: Cost-note recommendation line is emitted
    # ------------------------------------------------------------------

    def test_cost_note_emitted_on_successful_escalation(
        self, auto_mfr_tier_result: subprocess.CompletedProcess[str]
    ) -> None:
        """When escalation moves off the starting tier and the final tier
        has a registered ``cost_note``, the CLI emits a
        ``Recommendation: order from <tier>. <cost-note>.`` line.

        For ``jlcpcb-tier1`` the cost note is the Capability-Plus surcharge
        notice (see ``mfr_limits.MFR_JLCPCB_TIER1.cost_note``).
        """
        stdout = auto_mfr_tier_result.stdout
        recommendation = re.search(
            r"Recommendation:\s+order from\s+jlcpcb-tier1\.\s+.+",
            stdout,
        )
        assert recommendation is not None, (
            "Expected a 'Recommendation: order from jlcpcb-tier1. ...' "
            "line in stdout once escalation succeeds on the tier-1 attempt. "
            "This is the cost-note acceptance criterion from issue #2885 "
            "(and the Judge follow-up D on PR #2882). Check that "
            "``route_with_mfr_tier_escalation`` prints the cost-note when "
            "``tier_idx > 0`` and ``final_limits.cost_note`` is non-None.\n"
            f"\nLast 3000 chars of stdout:\n{stdout[-3000:]}"
        )

    # ------------------------------------------------------------------
    # AC #4: Regression-anchor -- the jlcpcb tier attempt falls short
    # ------------------------------------------------------------------

    def test_jlcpcb_tier_falls_short_of_tier1(
        self,
        auto_mfr_tier_result: subprocess.CompletedProcess[str],
        per_tier_stdout: dict[str, str],
    ) -> None:
        """Regression-anchor: the jlcpcb tier attempt within the same
        ``--auto-mfr-tier`` run must fall short of the jlcpcb-tier1 tier.

        If the jlcpcb attempt already routes everything trivially, then
        the escalation feature is doing no work and the test fixture is
        no longer the right anchor.  We require a contrast either as:

          1. The jlcpcb-tier1 attempt routes strictly more nets, OR
          2. The jlcpcb attempt logs a "missed via-in-pad" signal,
             proving via-in-pad would have helped.

        Either signal is sufficient evidence that the escalation chain
        is on the critical path for board 04.
        """
        assert "jlcpcb" in per_tier_stdout, (
            "Expected a 'Tier N/M: jlcpcb' banner in stdout (the starting "
            "tier of the default ladder).  Per-tier banners found: "
            f"{list(per_tier_stdout.keys())}"
        )

        jlcpcb_stdout = per_tier_stdout["jlcpcb"]
        tier1_stdout = per_tier_stdout.get("jlcpcb-tier1", "")

        jlcpcb_parsed = _parse_routed_net_count(jlcpcb_stdout)
        tier1_parsed = _parse_routed_net_count(tier1_stdout) if tier1_stdout else None

        jlcpcb_routed = jlcpcb_parsed[0] if jlcpcb_parsed else 0
        tier1_routed = tier1_parsed[0] if tier1_parsed else 0

        # Anchor signal #1: per-tier routed-net delta is strictly positive.
        nets_improved = tier1_routed > jlcpcb_routed

        # Anchor signal #2: missed via-in-pad rescues logged on jlcpcb.
        # The escape router emits a per-pad warning of the form:
        # "Warning: pin <ref>.<pin> would benefit from via-in-pad..."
        # We accept any "missed via-in-pad" / "would benefit from
        # via-in-pad" wording (case-insensitive).
        has_missed_signal = bool(
            re.search(
                r"(missed\s+via[\s_-]?in[\s_-]?pad|"
                r"would\s+benefit\s+from\s+via[\s_-]?in[\s_-]?pad|"
                r"via[\s_-]?in[\s_-]?pad\s+rescue)",
                jlcpcb_stdout,
                re.IGNORECASE,
            )
        )

        contrast_holds = nets_improved or has_missed_signal
        assert contrast_holds, (
            "Regression-anchor failed: within the --auto-mfr-tier run the "
            f"jlcpcb tier routed {jlcpcb_routed} nets, the jlcpcb-tier1 "
            f"tier routed {tier1_routed} nets, and no 'missed via-in-pad' "
            "signal was observed in the jlcpcb tier stdout.\n"
            "\nThis means either:\n"
            "  (a) The escalation path is not exercising any new capability "
            "      vs the base tier (the feature is effectively dead).\n"
            "  (b) The fine-pitch LQFP-48 + jlcpcb chain has become trivially "
            "      routable on the base tier (capability has shifted; this "
            "      test fixture is no longer the right anchor and should be "
            "      replaced with a tighter case).\n"
            f"\nLast 1500 chars of jlcpcb tier stdout:\n"
            f"{jlcpcb_stdout[-1500:]}"
        )
