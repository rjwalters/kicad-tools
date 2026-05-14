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
2. Final ``args.manufacturer`` is ``jlcpcb-tier1`` (visible in stdout).
3. The cost-note recommendation line is emitted to stdout.
4. Without ``--auto-mfr-tier``, the same invocation fails (regression-anchor):
   the ``missed_via_in_pad_rescues`` signal is non-zero OR the route falls
   short of 9/9.

Marked ``@pytest.mark.slow`` -- the chain exercises real routing on the
full LQFP-48 + crystal + LDO + SWD-header board.  PR-time CI excludes
``-m slow``; the nightly slow-tests workflow at
``.github/workflows/slow-tests.yml`` picks this up.
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
REQUIRED_NETS_ROUTED = 9
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


def _run_route(
    unrouted_pcb_path: Path,
    *,
    auto_mfr_tier: bool,
    timeout_seconds: int = 300,
) -> subprocess.CompletedProcess[str]:
    """Run ``kct route`` on a copy of the unrouted board-04 PCB.

    Args:
        unrouted_pcb_path: Source unrouted PCB (board-04 committed artifact).
        auto_mfr_tier: When True, pass ``--auto-mfr-tier`` so the full
            mfr-tier escalation chain runs.  When False, run plain
            ``kct route`` (the regression-anchor case).
        timeout_seconds: Per-tier router timeout passed via ``--timeout``.
            With escalation enabled and 2 tiers in the default ``jlcpcb``
            ladder, total wall-clock can reach 2x this.

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
            "--no-auto-layers",
            "--layers",
            "2",
            "--manufacturer",
            "jlcpcb",
            "--timeout",
            str(timeout_seconds),
            "--backend",
            "python",
        ]
        if auto_mfr_tier:
            cmd.append("--auto-mfr-tier")
        # Total wall-clock: ~2x timeout_seconds for the 2-tier ladder, plus
        # some setup overhead; give 1.5x slack.
        wall_clock = int(timeout_seconds * (3 if auto_mfr_tier else 1.5))
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

    Each test re-runs the routing as a subprocess so the same path the
    user invokes interactively is exercised.  We keep two separate routes
    (with / without ``--auto-mfr-tier``) so the contrast between them is
    visible from the test report alone.
    """

    @pytest.fixture(scope="class")
    def auto_mfr_tier_result(
        self, unrouted_pcb_path: Path
    ) -> subprocess.CompletedProcess[str]:
        """Run with ``--auto-mfr-tier`` and capture stdout/stderr.

        Use a per-tier timeout of 180s to keep the total under the slow-
        tests budget (the test is excluded from PR-time CI).
        """
        proc = _run_route(unrouted_pcb_path, auto_mfr_tier=True, timeout_seconds=180)
        if proc.returncode in (1, 5):
            pytest.fail(
                f"kct route --auto-mfr-tier returned fatal exit code "
                f"{proc.returncode}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
            )
        return proc

    @pytest.fixture(scope="class")
    def no_auto_mfr_tier_result(
        self, unrouted_pcb_path: Path
    ) -> subprocess.CompletedProcess[str]:
        """Run WITHOUT ``--auto-mfr-tier`` as the regression-anchor.

        Without escalation the chain hits PIN_ACCESS on inner LQFP-48 pins
        and cannot complete on plain jlcpcb (no via-in-pad capability).
        """
        proc = _run_route(unrouted_pcb_path, auto_mfr_tier=False, timeout_seconds=180)
        if proc.returncode in (1, 5):
            pytest.fail(
                f"kct route (no auto-mfr-tier, anchor case) returned fatal "
                f"exit code {proc.returncode}\n"
                f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
                f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
            )
        return proc

    # ------------------------------------------------------------------
    # AC #1: Routing succeeds after escalation
    # ------------------------------------------------------------------

    def test_routes_all_nets_with_auto_mfr_tier(
        self, auto_mfr_tier_result: subprocess.CompletedProcess[str]
    ) -> None:
        """With ``--auto-mfr-tier`` the chain completes 9/9 nets after
        escalating to ``jlcpcb-tier1``.

        Without via-in-pad, inner LQFP-48 pins surrounded by ground / VDD
        plane pads cannot escape on the surface; the tier-1 escalation
        unlocks in-pad vias and the chain closes.
        """
        parsed = _parse_routed_net_count(auto_mfr_tier_result.stdout)
        assert parsed is not None, (
            "Could not find 'Nets routed: N/M' line in --auto-mfr-tier "
            "stdout.  Last 2000 chars:\n"
            f"{auto_mfr_tier_result.stdout[-2000:]}"
        )
        routed, total = parsed
        assert routed >= REQUIRED_NETS_ROUTED, (
            f"--auto-mfr-tier escalation completed only {routed}/{total} "
            f"nets (expected >= {REQUIRED_NETS_ROUTED}/{REQUIRED_NETS_TOTAL}).\n"
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
    # AC #2: Final manufacturer tier is jlcpcb-tier1
    # ------------------------------------------------------------------

    def test_final_tier_is_jlcpcb_tier1(
        self, auto_mfr_tier_result: subprocess.CompletedProcess[str]
    ) -> None:
        """The CLI should advance to the ``jlcpcb-tier1`` attempt.

        ``route_with_mfr_tier_escalation`` prints a per-tier banner of the
        form ``Tier N/M: <tier-name>``.  We assert that the
        ``jlcpcb-tier1`` banner appears, indicating the escalation step
        actually fired (not that it was short-circuited at the convergence
        guard).
        """
        stdout = auto_mfr_tier_result.stdout
        # Per-tier banner: "Tier 2/2: jlcpcb-tier1"
        tier1_banner = re.search(r"Tier\s+\d+/\d+:\s+jlcpcb-tier1", stdout)
        assert tier1_banner is not None, (
            "Expected per-tier banner 'Tier N/M: jlcpcb-tier1' in stdout, "
            "indicating the mfr-tier escalation actually advanced off the "
            "starting jlcpcb tier.  Without this, the loop short-circuited "
            "(e.g. the convergence guard suppressed the step, or the inner "
            "jlcpcb attempt returned 0).\n"
            f"\nLast 3000 chars of stdout:\n{stdout[-3000:]}"
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
    # AC #4: Regression-anchor -- without --auto-mfr-tier the route fails
    # ------------------------------------------------------------------

    def test_without_auto_mfr_tier_route_falls_short(
        self,
        auto_mfr_tier_result: subprocess.CompletedProcess[str],
        no_auto_mfr_tier_result: subprocess.CompletedProcess[str],
    ) -> None:
        """Regression-anchor: without ``--auto-mfr-tier`` the chain falls
        short (or at minimum produces missed via-in-pad rescue signals).

        This is the contrast assertion: if both runs succeed identically,
        the escalation path is not actually doing anything useful and the
        feature is silently dead.

        We tolerate two failure modes for the anchor run:

          1. Fewer nets routed than the --auto-mfr-tier run.
          2. ``missed_via_in_pad_rescues`` signal is logged (proves the
             jlcpcb attempt would have benefited from via-in-pad).

        Either is sufficient evidence that the escalation chain is on the
        critical path.
        """
        anchor_parsed = _parse_routed_net_count(no_auto_mfr_tier_result.stdout)
        escalated_parsed = _parse_routed_net_count(auto_mfr_tier_result.stdout)
        assert escalated_parsed is not None, "escalated run should report a summary"

        escalated_routed, _ = escalated_parsed
        anchor_routed = anchor_parsed[0] if anchor_parsed else 0

        # Look for the missed-via-in-pad signal in the anchor stdout.  The
        # escape router logs a message like "missed via-in-pad" when it
        # encountered a pin that would have benefited from in-pad escape
        # but the manufacturer does not support it.
        anchor_stdout = no_auto_mfr_tier_result.stdout
        has_missed_signal = bool(
            re.search(
                r"missed[\s_-]?via[\s_-]?in[\s_-]?pad",
                anchor_stdout,
                re.IGNORECASE,
            )
        )

        contrast_holds = (anchor_routed < escalated_routed) or has_missed_signal
        assert contrast_holds, (
            "Regression-anchor failed: without --auto-mfr-tier the chain "
            f"routed {anchor_routed} nets, with --auto-mfr-tier it routed "
            f"{escalated_routed} nets, and no 'missed via-in-pad' signal "
            "was observed in the anchor stdout.\n"
            "\nThis means either:\n"
            "  (a) The escalation path is not exercising any new capability "
            "      vs the base tier (the feature is effectively dead).\n"
            "  (b) The fine-pitch LQFP-48 + jlcpcb chain has become trivially "
            "      routable on the base tier (capability has shifted; this "
            "      test fixture is no longer the right anchor and should be "
            "      replaced with a tighter case).\n"
            f"\nAnchor stdout (last 2000 chars):\n{anchor_stdout[-2000:]}"
        )
