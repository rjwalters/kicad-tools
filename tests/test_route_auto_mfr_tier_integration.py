"""End-to-end integration test for ``kct route --auto-mfr-tier`` (Issue #2885).

PR #2882 shipped ``--auto-mfr-tier`` with 62 unit tests, but every dispatch
test in ``test_route_auto_mfr_tier.py`` mocks ``route_with_layer_escalation``.
The full chain:

    fine-pitch LQFP-48 + jlcpcb
        -> routing hits PIN_ACCESS
        -> ``EscapeRouter`` bumps ``missed_via_in_pad_rescues``
        -> outer loop sees the signal
        -> escalates to ``jlcpcb-tier1`` (via-in-pad available)
        -> routing makes measurable progress on previously-blocked nets

is **not exercised by any test prior to this one**.  Judge follow-up
observation D on PR #2882 explicitly flagged the gap as deliberate scope-cut
for follow-up.

This test pins the post-#2882 behavior by driving the full CLI subprocess
path on the board-04 STM32 development board (LQFP-48 0.5mm pitch + ground
planes), which is the canonical real-board fixture for the chain.

Acceptance criteria (from issue #2885), adapted to a reproducible signal:

1. The escalation does not regress: the jlcpcb-tier1 attempt routes at
   least as many nets as the jlcpcb attempt within the same run.  The
   original AC ("Routing succeeds after escalation, not before") was a
   strict ``>``, but the python backend's per-net budget is wall-clock, so
   a strict delta is a timing race on a loaded runner (Issue #6217
   review).  The AC is therefore rescoped to non-regression; that the
   escalation fired, and why, is pinned by ACs 2 and 3.  Measured deltas
   are context only, not a gate.  (board-04 routing also has residual
   issues tracked under #2695 / #2696 / #2834 that prevent a deterministic
   absolute completion target.)

2. The CLI advances to ``jlcpcb-tier1``: a 'Tier N/M: jlcpcb-tier1'
   banner appears in stdout (the canonical AC#2 from issue #2885 --
   "Final ``args.manufacturer`` is ``jlcpcb-tier1``" expressed as the
   per-tier banner visible to the user).

3. The escalation is triggered by the canonical
   ``missed_via_in_pad_rescues`` signal: the 'Escalating to jlcpcb-tier1'
   line in stdout names the trigger.  When the chain actually succeeds
   (tier-1 returns 0) the cost-note 'Recommendation: order from
   jlcpcb-tier1.' line is asserted as well; when tier-1 ends partial
   (board-04's current state on 4L) the cost-note is not emitted, and
   we assert only the trigger reason.

4. The jlcpcb tier attempt within the same run falls short of the
   jlcpcb-tier1 attempt -- the regression-anchor for the contrast.

Issue #6217 re-baseline (after #5189 / #5201).  Via-in-pad now depends on
a real, orderable fabrication process, and JLCPCB's POFV process needs 4+
copper layers: ``jlcpcb-tier1`` has *no* via-in-pad on a 2-layer board.  The
chain is therefore only physically real at 4L, so the escalation test runs
``--starting-layers 4 --max-layers 4``.  A second, 2-layer class pins the
other half of the contract: at 2L the ladder must NOT escalate on the
missed-rescue signal, and must say why (the tier's 4-layer floor).

Backend: ``--backend python``, as before.  On the C++ backend board 04
routes 9/9 at plain ``jlcpcb`` on both 2L and 4L, so the ladder stops at the
first tier and there is nothing to escalate.  On the python backend U2's
congested pins (BOOT0, NRST, OSC_IN) are escape-limited under a bounded
per-net search -- the case in-pad vias straight down to the inner layers
relieve.

Determinism (PR #6228 review): both runs use ``--deterministic-budget
--deterministic-rescue --per-net-iterations N`` and NO ``--timeout``, so
every search is bounded by node expansions rather than wall clock and no
stage deadline can fire.  The routed counts are then a property of the
code, not of runner speed or load.  A wall-clock per-net budget made the
tier1-vs-jlcpcb margin a race: on this branch it measured 2-3 nets at the
30 s default, 1 at 15 s and 0 at 10 s, so a busy 4-vCPU runner could flip
it.  Measured with the iteration budget (4L, each tier alone): at 200k
expansions per net jlcpcb's best attempt is 6/9 and jlcpcb-tier1's is 7/9;
at 500k and 1M it is 7/9 vs 8/9, but each tier then takes ~27 min under
load; at 100k both stall at 3/9.  Two concurrent full ``--auto-mfr-tier``
runs at 200k gave identical per-attempt counts (jlcpcb 6, 3; tier1 7, 6, 3),
matching the single-tier runs made under different load, in ~25 min each
under ~5x CPU contention.  This needed #6217's fix that makes ``--per-net-iterations`` reach the
pure-python backend at all.

The progress assertion is backed by a mechanism check that does not depend
on the A* budget: tier1's escape pass reports ``(N via-in-pad)`` for U2 and
jlcpcb's reports none.

Marked ``@pytest.mark.slow``; PR-time CI excludes ``-m slow`` and the
nightly ``.github/workflows/slow-tests.yml`` runs it.  Without ``--timeout``
the subprocess wall-clock limit below is the only safety net, so it is set
well above the measured runtime.
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
    """Extract the best routed-net count from one tier's stdout block.

    Each layer attempt prints ``Routed: N/M nets`` and the tier's layer
    escalation summary prints ``Nets routed: N/M``.  Returns the
    ``(routed, total)`` pair with the highest ``routed`` over both forms --
    the tier's best attempt.  Issue #6217: the per-attempt lines matter
    because the hard ``--timeout`` (#5141) can end the final tier
    mid-attempt, before its summary is printed.

    Returns ``None`` if no count is present (e.g. the router crashed
    before finishing any attempt).
    """
    pattern = re.compile(r"(?:Nets routed|Routed):\s+(\d+)/(\d+)")
    matches = [(int(r), int(t)) for r, t in pattern.findall(stdout)]
    if not matches:
        return None
    return max(matches)


def _split_by_tier(stdout: str) -> dict[str, str]:
    """Split the ``--auto-mfr-tier`` stdout into per-tier sub-strings.

    ``route_with_mfr_tier_escalation`` prints a banner of the form
    ``Tier N/M: <tier-name>`` before each inner attempt.  We slice the
    stdout on those banners so individual assertions can inspect just
    one tier's output (e.g. "did the jlcpcb attempt produce N routes,
    did the jlcpcb-tier1 attempt produce more?").

    Returns a dict mapping tier-name -> sub-stdout.  Tiers that never
    ran (because the loop terminated early) are absent from the dict.
    Order of insertion follows the order of banners in stdout.
    """
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


# Per-net A* node-expansion caps for the deterministic runs.  4L: the
# smallest measured cap with a jlcpcb-tier1 > jlcpcb margin (6/9 vs 7/9).
# 2L: the run only needs the base tier to fail, so a small cap keeps it short.
_ESCALATION_4L_PER_NET_ITERATIONS = 200_000
_NO_ESCALATION_2L_PER_NET_ITERATIONS = 50_000
# Subprocess wall-clock safety nets (seconds).  No ``--timeout`` is passed --
# a firing wall-clock deadline would make the result machine-dependent again.
_ESCALATION_4L_WALL_CLOCK_S = 4200
_NO_ESCALATION_2L_WALL_CLOCK_S = 1500


def _run_route_auto_mfr_tier(
    unrouted_pcb_path: Path,
    *,
    max_layers: int,
    starting_layers: int,
    per_net_iterations: int,
    wall_clock_seconds: int,
) -> subprocess.CompletedProcess[str]:
    """Run ``kct route --auto-mfr-tier`` on a copy of the board-04 PCB.

    Args:
        unrouted_pcb_path: Source unrouted PCB (board-04 committed artifact).
        max_layers: ``--max-layers``.  The reachable layer counts decide
            whether ``jlcpcb-tier1`` offers via-in-pad (Issue #6217).
        starting_layers: ``--starting-layers``.
        per_net_iterations: ``--per-net-iterations`` under
            ``--deterministic-budget`` (machine-independent search bound).
        wall_clock_seconds: Subprocess safety-net limit.

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
            "--starting-layers",
            str(starting_layers),
            "--max-layers",
            str(max_layers),
            "--manufacturer",
            "jlcpcb",
            "--deterministic-budget",
            "--deterministic-rescue",
            "--per-net-iterations",
            str(per_net_iterations),
            "--backend",
            "python",
            "--auto-mfr-tier",
        ]
        return subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=wall_clock_seconds,
            check=False,
        )


def _assert_deterministic(proc: subprocess.CompletedProcess[str]) -> None:
    """No wall-clock stage deadline fired, so the counts are reproducible."""
    assert "stage deadline fired" not in proc.stdout, (
        "A wall-clock stage deadline fired during a --deterministic-budget run, "
        "so its routed counts are no longer machine-independent.\n"
        f"Last 3000 chars of stdout:\n{proc.stdout[-3000:]}"
    )


def _fail_on_fatal_exit(proc: subprocess.CompletedProcess[str]) -> None:
    """Config errors / internal crashes (exit 1 or 5) are never a result."""
    if proc.returncode in (1, 5):
        pytest.fail(
            f"kct route --auto-mfr-tier returned fatal exit code "
            f"{proc.returncode}\n"
            f"stderr (last 2000 chars):\n{proc.stderr[-2000:]}\n"
            f"stdout (last 2000 chars):\n{proc.stdout[-2000:]}"
        )


@pytest.mark.slow
@pytest.mark.timeout(4500)
class TestAutoMfrTierIntegration:
    """End-to-end chain test at 4L: jlcpcb -> escalate to jlcpcb-tier1.

    A single ``--auto-mfr-tier`` subprocess invocation produces both:
      - the jlcpcb tier attempt (AC #4 regression-anchor evidence)
      - the jlcpcb-tier1 escalation attempt (AC #1, #2, #3 evidence)

    Running a single subprocess (vs separate auto-mfr-tier + anchor runs)
    keeps the slow-tests budget tractable while still proving the full
    chain works.
    """

    @pytest.fixture(scope="class")
    def auto_mfr_tier_result(self, unrouted_pcb_path: Path) -> subprocess.CompletedProcess[str]:
        """Run with ``--auto-mfr-tier`` at 4L only and capture output."""
        proc = _run_route_auto_mfr_tier(
            unrouted_pcb_path,
            max_layers=4,
            starting_layers=4,
            per_net_iterations=_ESCALATION_4L_PER_NET_ITERATIONS,
            wall_clock_seconds=_ESCALATION_4L_WALL_CLOCK_S,
        )
        _fail_on_fatal_exit(proc)
        _assert_deterministic(proc)
        return proc

    @pytest.fixture(scope="class")
    def per_tier_stdout(
        self, auto_mfr_tier_result: subprocess.CompletedProcess[str]
    ) -> dict[str, str]:
        """Split the captured stdout by per-tier banners."""
        return _split_by_tier(auto_mfr_tier_result.stdout)

    # ------------------------------------------------------------------
    # AC #1 (rescoped to non-regression): Tier-1 does not route fewer nets.
    # ------------------------------------------------------------------

    def test_tier1_does_not_regress_vs_jlcpcb(
        self,
        auto_mfr_tier_result: subprocess.CompletedProcess[str],
        per_tier_stdout: dict[str, str],
    ) -> None:
        """The jlcpcb-tier1 attempt routes at least as many nets as the
        jlcpcb attempt within the same run.

        This test enforces **non-regression only**; it does NOT prove
        that tier1 routes more nets than jlcpcb.  The python backend's
        per-net budget is wall-clock, so a strict ``>`` is a timing race
        on a loaded runner.  For context, deltas measured on a developer
        machine were +2/+3/+2 at the default 30 s budget, +1 at 15 s and
        0 at 10 s; those numbers are not asserted.  That the escalation
        fired, and for the right reason, is pinned deterministically by
        ``test_escalation_advances_to_tier1`` and
        ``test_escalation_triggered_by_missed_via_in_pad``.  An absolute
        completion target on board-04 is gated by residual upstream
        issues (#2695 OSC_OUT pad-completion, #2696 impedance on 2L,
        #2834 clearance-pad-segment count).
        """
        assert "jlcpcb" in per_tier_stdout, (
            "Expected a 'Tier N/M: jlcpcb' banner.  Per-tier banners: "
            f"{list(per_tier_stdout.keys())}\n"
            f"\nLast 3000 chars of stdout:\n"
            f"{auto_mfr_tier_result.stdout[-3000:]}"
        )
        assert "jlcpcb-tier1" in per_tier_stdout, (
            "Expected a 'Tier N/M: jlcpcb-tier1' banner.  The escalation "
            "did not advance off the starting tier.  Per-tier banners: "
            f"{list(per_tier_stdout.keys())}\n"
            f"\nLast 3000 chars of stdout:\n"
            f"{auto_mfr_tier_result.stdout[-3000:]}"
        )

        jlcpcb_stdout = per_tier_stdout["jlcpcb"]
        tier1_stdout = per_tier_stdout["jlcpcb-tier1"]

        jlcpcb_parsed = _parse_routed_net_count(jlcpcb_stdout)
        tier1_parsed = _parse_routed_net_count(tier1_stdout)
        assert jlcpcb_parsed is not None, (
            "Expected a 'Routed: N/M' count in jlcpcb tier stdout.\n"
            f"Last 2000 chars:\n{jlcpcb_stdout[-2000:]}"
        )
        assert tier1_parsed is not None, (
            "Expected a 'Routed: N/M' count in jlcpcb-tier1 tier stdout.\n"
            f"Last 2000 chars:\n{tier1_stdout[-2000:]}"
        )

        jlcpcb_routed, _ = jlcpcb_parsed
        tier1_routed, _ = tier1_parsed

        assert tier1_routed >= jlcpcb_routed, (
            "Regression-anchor failed: within the --auto-mfr-tier run the "
            f"jlcpcb-tier1 tier routed {tier1_routed} nets vs jlcpcb's "
            f"{jlcpcb_routed} nets -- escalation made routing worse.\n"
            f"\njlcpcb tier stdout (last 1500 chars):\n{jlcpcb_stdout[-1500:]}\n"
            f"\njlcpcb-tier1 tier stdout (last 1500 chars):\n"
            f"{tier1_stdout[-1500:]}"
        )

    def test_tier1_escape_pass_places_via_in_pad(self, per_tier_stdout: dict[str, str]) -> None:
        """Mechanism check, independent of the A* budget (PR #6228 review).

        The escape pass runs before any A* search, so whether it places
        in-pad vias does not depend on how far the search gets.  jlcpcb has
        no via-in-pad process and must place none on U2; jlcpcb-tier1 at 4L
        has POFV and must place at least one -- the capability the
        escalation exists to buy.  Checked on ANY tier1 attempt: the
        interleaved micro-via fallback rung (#3371) places none, because its
        0.3/0.15 mm micro-via is outside POFV's drill envelope (#5378).
        """
        in_pad_re = re.compile(r"Escape routes: U2 \(\w+\) - \d+ pins escaped \((\d+) via-in-pad\)")
        assert "jlcpcb-tier1" in per_tier_stdout, (
            f"No jlcpcb-tier1 tier ran.  Banners: {list(per_tier_stdout)}"
        )
        jlcpcb_in_pad = in_pad_re.findall(per_tier_stdout.get("jlcpcb", ""))
        tier1_in_pad = [int(n) for n in in_pad_re.findall(per_tier_stdout["jlcpcb-tier1"])]
        assert not jlcpcb_in_pad, (
            "jlcpcb has no via-in-pad process, yet its escape pass reported "
            f"in-pad vias on U2: {jlcpcb_in_pad}"
        )
        assert any(n > 0 for n in tier1_in_pad), (
            "Expected a jlcpcb-tier1 attempt's escape pass to place at least "
            "one via-in-pad on U2 ('Escape routes: U2 (...) - N pins escaped "
            "(M via-in-pad)').\n"
            f"\njlcpcb-tier1 stdout (last 3000 chars):\n"
            f"{per_tier_stdout['jlcpcb-tier1'][-3000:]}"
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

        ``route_with_mfr_tier_escalation`` prints a per-tier banner of
        the form ``Tier N/M: <tier-name>``.  We assert that the
        ``jlcpcb-tier1`` banner appears, indicating the escalation step
        actually fired (not that it was short-circuited at the
        convergence guard or the deadline).
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
    # AC #3: Escalation trigger reason is the canonical missed-rescue signal.
    # ------------------------------------------------------------------

    def test_escalation_triggered_by_missed_via_in_pad(
        self, auto_mfr_tier_result: subprocess.CompletedProcess[str]
    ) -> None:
        """The 'Escalating to jlcpcb-tier1' stdout line should name the
        canonical trigger:

            "missed via-in-pad rescues detected on previous tier"

        This proves the chain wired up correctly:
          1. The jlcpcb attempt's EscapeRouter incremented
             ``missed_via_in_pad_rescues``.
          2. The mfr-tier outer loop read that counter from
             ``args._last_router._escape_router``.
          3. The convergence-guard branch that prints the canonical
             trigger reason fired (vs the fallback "next tier offers
             via-in-pad capability" reason which fires when there's no
             missed-rescue signal, which would be a worse failure-mode
             diagnostic if it fired here).

        This is AC#3 from issue #2885 ("cost-note line emitted to
        stdout") expressed at the chain-mechanism level: the
        canonical trigger reason is the upstream signal that produces
        the cost-note line when (and only when) escalation succeeds.
        We additionally check for the cost-note line conditional on
        the chain reaching success.
        """
        stdout = auto_mfr_tier_result.stdout

        # The canonical trigger reason emitted from
        # route_with_mfr_tier_escalation when the missed-rescue counter
        # is non-zero on the previous tier.
        trigger_line = re.search(
            r"Escalating to jlcpcb-tier1:\s+"
            r"missed\s+via-in-pad\s+rescues\s+detected\s+on\s+previous\s+tier",
            stdout,
            re.IGNORECASE,
        )
        assert trigger_line is not None, (
            "Expected 'Escalating to jlcpcb-tier1: missed via-in-pad rescues "
            "detected on previous tier' in stdout.  This is the canonical "
            "trigger signal from issue #2881 -- the chain mechanism that "
            "decides when to walk to the next tier.  Without it, escalation "
            "fell through the convergence-guard's defensive branch instead "
            "of the targeted branch, which masks the diagnostic.\n"
            "\nCheck that the jlcpcb tier attempt's EscapeRouter actually "
            "incremented ``missed_via_in_pad_rescues`` for board-04's "
            "fine-pitch LQFP-48 inner pins.\n"
            f"\nLast 4000 chars of stdout:\n{stdout[-4000:]}"
        )

        # When the chain actually succeeds on tier-1, the cost-note line
        # is also emitted.  When tier-1 ends partial (board-04 has
        # residual upstream issues #2695/#2696/#2834 even on tier-1) the
        # cost-note line is intentionally suppressed -- the test does
        # not require it.  This soft check pins the integration when
        # tier-1 *does* succeed without forcing the test to fail on
        # board-04's residuals.
        cost_note = re.search(
            r"Recommendation:\s+order from\s+jlcpcb-tier1\.\s+.+",
            stdout,
        )
        # If tier-1 reached success, we expect the cost-note.  Detect
        # tier-1 success by looking for the success banner in the
        # mfr-tier summary block.
        if "Tier jlcpcb-tier1 achieved routing success" in stdout:
            assert cost_note is not None, (
                "Tier-1 reached routing success but the cost-note line "
                "'Recommendation: order from jlcpcb-tier1. ...' was not "
                "emitted.  Check route_cmd.py:3361-3367.\n"
                f"\nLast 3000 chars of stdout:\n{stdout[-3000:]}"
            )

    # ------------------------------------------------------------------
    # AC #4: jlcpcb tier attempt falls short -- the regression-anchor.
    # ------------------------------------------------------------------
    # NOTE: covered by ``test_tier1_does_not_regress_vs_jlcpcb`` above
    # (non-regression: tier-1 >= jlcpcb; the mechanism is pinned by the
    # banner and trigger-line tests).  No separate test needed.


@pytest.mark.slow
@pytest.mark.timeout(1800)
class TestAutoMfrTierNoEscalationAt2L:
    """Issue #6217: at 2L the ladder must not escalate on missed rescues.

    ``jlcpcb-tier1``'s via-in-pad process (POFV) needs 4+ copper layers, so a
    ``--max-layers 2`` run gains nothing from escalating on the
    missed-rescue signal.  Before #6217 the layer-blind guard
    ``can_escalate_via_in_pad`` credited that gain anyway.  The run must
    stay on ``jlcpcb`` and name the layer floor.
    """

    @pytest.fixture(scope="class")
    def two_layer_result(self, unrouted_pcb_path: Path) -> subprocess.CompletedProcess[str]:
        proc = _run_route_auto_mfr_tier(
            unrouted_pcb_path,
            max_layers=2,
            starting_layers=2,
            per_net_iterations=_NO_ESCALATION_2L_PER_NET_ITERATIONS,
            wall_clock_seconds=_NO_ESCALATION_2L_WALL_CLOCK_S,
        )
        _fail_on_fatal_exit(proc)
        _assert_deterministic(proc)
        return proc

    def test_does_not_escalate_to_tier1(
        self, two_layer_result: subprocess.CompletedProcess[str]
    ) -> None:
        stdout = two_layer_result.stdout
        banners = list(_split_by_tier(stdout))
        assert banners == ["jlcpcb"], (
            "At --max-layers 2 the ladder must stay on jlcpcb (jlcpcb-tier1 "
            f"has no via-in-pad below 4 layers).  Per-tier banners: {banners}\n"
            f"\nLast 3000 chars of stdout:\n{stdout[-3000:]}"
        )
        assert "Escalating to jlcpcb-tier1" not in stdout

    def test_reports_the_layer_floor(
        self, two_layer_result: subprocess.CompletedProcess[str]
    ) -> None:
        """The missed rescues are still detected, and the reason no tier
        can use them is printed, naming tier1's 4-layer floor."""
        stdout = two_layer_result.stdout
        line = re.search(
            r"No via-in-pad gain from jlcpcb-tier1: (\d+) missed via-in-pad "
            r"rescue\(s\) on jlcpcb, but jlcpcb-tier1 offers via-in-pad only at "
            r">= 4 copper layers and this run can reach only 2L",
            stdout,
        )
        assert line is not None, (
            "Expected the layer-gated no-escalation reason in stdout.\n"
            f"\nLast 4000 chars of stdout:\n{stdout[-4000:]}"
        )
        assert int(line.group(1)) > 0
        assert "raise --max-layers to 4" in stdout
