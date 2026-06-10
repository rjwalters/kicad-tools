"""Tests for the initial-pass budget-cliff grace pass (Issue #3452).

Structural hazard (board 05, 4L jlcpcb, seed 42 — the #3452 controlled
A/B): the sequential initial pass routes nets in a difficulty-agnostic
order and HARD-BREAKS the moment ``check_timeout()`` fires.  A block of
pathological searches early in ``net_order`` (five ISENSE escapes, ~190s
of a 420s budget) can therefore exhaust the wall clock before the cheap
majority gets ANY attempt — on board 05 the 24 nets after the ISENSE
block route in under 10 seconds, so whether the timeout line lands
before or after that 10s window decides 28/32 vs 6/32 at the initial
pass.  The #3452 "12/32 -> 3/32 in-loop regression" is this cliff,
load-modulated: in the collapsing runs ``timed_out`` skips the rip-up
iteration loop entirely, so the #3442 in-loop pieces (seg-seg lex tuple,
rip-up feed) never even execute.

The fix: when the budget expires mid-list, each starved net gets ONE
A* attempt capped at ``GRACE_PASS_PER_NET_S`` seconds, with the whole
pass bounded by ``GRACE_PASS_BUDGET_S``.  Cheap nets complete in
milliseconds; pathological searches fail fast; the overrun past
``--timeout`` is small and bounded.

Twin implementations:
- ``Autorouter.route_all_negotiated`` sequential initial pass (core.py)
- ``TwoPhaseRouter._route_detailed_negotiated`` initial pass (two_phase.py)

These tests drive the core.py path end-to-end (the two_phase twin is
line-for-line the same policy against the same shared constants).
"""

from __future__ import annotations

from kicad_tools.router.algorithms import (
    GRACE_PASS_BUDGET_S,
    GRACE_PASS_PER_NET_S,
    GRACE_PASS_TIER_CAPS_S,
    run_initial_pass_grace,
)
from kicad_tools.router.core import Autorouter


def _build_trivial_router() -> Autorouter:
    """Two easy 2-pad nets on an open 20x20 grid (routes in ms each)."""
    router = Autorouter(width=20.0, height=20.0)
    router.add_component(
        "R1",
        [
            {"number": "1", "x": 2.0, "y": 10.0, "net": 1, "net_name": "NET1"},
            {"number": "2", "x": 18.0, "y": 10.0, "net": 1, "net_name": "NET1"},
        ],
    )
    router.add_component(
        "R2",
        [
            {"number": "1", "x": 10.0, "y": 2.0, "net": 2, "net_name": "NET2"},
            {"number": "2", "x": 10.0, "y": 18.0, "net": 2, "net_name": "NET2"},
        ],
    )
    return router


class TestGracePassConstants:
    """The shared bounds must stay small: the grace pass exists to be a
    cheap tail sweep, not a second routing budget."""

    def test_per_call_caps_are_small_and_escalating(self):
        assert all(0 < cap <= 5.0 for cap in GRACE_PASS_TIER_CAPS_S)
        assert list(GRACE_PASS_TIER_CAPS_S) == sorted(GRACE_PASS_TIER_CAPS_S)
        assert GRACE_PASS_TIER_CAPS_S[-1] == GRACE_PASS_PER_NET_S

    def test_first_tier_is_tiny(self):
        """Tier 1 must stay tiny: a failing net's wall time is a large
        multiple of the per-call cap (RSMT edges x escape-hint deadline
        x relaxed retries, ~15x measured on board 05's ISENSE family),
        and tier 1 is what guarantees the cheap majority gets attempts
        before any pathological net can drain the budget."""
        assert GRACE_PASS_TIER_CAPS_S[0] <= 0.5

    def test_total_budget_is_bounded(self):
        assert 0 < GRACE_PASS_BUDGET_S <= 60.0


class TestRunInitialPassGraceHelper:
    """Direct tests of the shared sweep (tier escalation + accounting)."""

    def test_all_cheap_nets_routed_in_tier_one(self):
        calls: list[tuple[int, float]] = []
        committed: dict[int, list] = {}
        routed, attempted, skipped = run_initial_pass_grace(
            [1, 2, 3],
            route_fn=lambda net, cap: (calls.append((net, cap)) or [f"r{net}"]),
            commit_fn=lambda net, routes: committed.__setitem__(net, routes),
            per_net_timeout=30.0,
        )
        assert (routed, attempted, skipped) == (3, 3, 0)
        assert sorted(committed) == [1, 2, 3]
        # Every net succeeded on tier 1; tier 2 must not re-run them.
        assert [c[1] for c in calls] == [GRACE_PASS_TIER_CAPS_S[0]] * 3

    def test_tier_two_retries_only_failures_with_bigger_cap(self):
        calls: list[tuple[int, float]] = []

        def route_fn(net: int, cap: float) -> list:
            calls.append((net, cap))
            if net == 2 and cap < GRACE_PASS_TIER_CAPS_S[-1]:
                return []  # Net 2 needs the tier-2 cap.
            return [f"r{net}"]

        routed, attempted, skipped = run_initial_pass_grace(
            [1, 2, 3],
            route_fn=route_fn,
            commit_fn=lambda net, routes: None,
            per_net_timeout=30.0,
        )
        assert (routed, attempted, skipped) == (3, 3, 0)
        tier2_calls = [c for c in calls if c[1] == GRACE_PASS_TIER_CAPS_S[-1]]
        assert tier2_calls == [(2, GRACE_PASS_TIER_CAPS_S[-1])]

    def test_caps_clamped_to_caller_per_net_timeout(self):
        """The grace pass must never grant a net MORE per-call time
        than the caller's own per-net budget."""
        seen_caps: set[float] = set()
        run_initial_pass_grace(
            [1],
            route_fn=lambda net, cap: (seen_caps.add(cap) or []),
            commit_fn=lambda net, routes: None,
            per_net_timeout=0.1,
        )
        assert seen_caps == {0.1}

    def test_budget_exhaustion_skips_remaining_nets(self, monkeypatch):
        """A single pathological net that blows through the budget (the
        board-05 ISENSE measurement: 31.2s under a flat 2.0s cap) must
        not cause an unbounded sweep -- remaining nets are reported as
        skipped."""
        import kicad_tools.router.algorithms.negotiated as neg_mod

        clock = {"now": 0.0}
        monkeypatch.setattr(neg_mod.time, "monotonic", lambda: clock["now"])

        def route_fn(net: int, cap: float) -> list:
            # First net silently devours the whole budget.
            clock["now"] += GRACE_PASS_BUDGET_S + 5.0
            return []

        routed, attempted, skipped = run_initial_pass_grace(
            [1, 2, 3],
            route_fn=route_fn,
            commit_fn=lambda net, routes: None,
            per_net_timeout=30.0,
        )
        assert routed == 0
        assert attempted == 1
        assert skipped == 2


class TestInitialPassGraceEndToEnd:
    """An immediately-expired wall clock must no longer zero the board."""

    def test_expired_timeout_still_routes_cheap_nets(self):
        """timeout so small that ``check_timeout()`` fires at net 0/N:
        pre-#3452 the initial pass hard-broke and returned ZERO routes;
        with the grace pass both trivial nets must still be attempted
        (and on this open grid, routed)."""
        ar = _build_trivial_router()
        routes = ar.route_all_negotiated(
            max_iterations=2,
            timeout=1e-9,
            adaptive=False,
            perturbation=False,
        )
        routed_nets = {r.net for r in routes}
        assert routed_nets == {1, 2}, (
            f"Grace pass must rescue the starved tail; got routes for "
            f"{sorted(routed_nets)}"
        )

    def test_expired_timeout_respects_per_net_timeout_floor(self):
        """A caller-supplied ``per_net_timeout`` SMALLER than the grace
        cap must win (the grace pass must never grant a net more time
        than the caller's own per-net budget)."""
        ar = _build_trivial_router()
        routes = ar.route_all_negotiated(
            max_iterations=2,
            timeout=1e-9,
            per_net_timeout=0.5,
            adaptive=False,
            perturbation=False,
        )
        # Trivial nets route far inside 0.5s; the assertion is that the
        # combination does not crash and still rescues the tail.
        routed_nets = {r.net for r in routes}
        assert routed_nets == {1, 2}

    def test_normal_timeout_unchanged(self):
        """With a comfortable budget the grace pass must never engage
        (``grace_nets`` stays empty) and behavior is identical to
        pre-#3452: both nets route through the normal initial pass."""
        ar = _build_trivial_router()
        routes = ar.route_all_negotiated(
            max_iterations=2,
            timeout=30.0,
            adaptive=False,
            perturbation=False,
        )
        routed_nets = {r.net for r in routes}
        assert routed_nets == {1, 2}

    def test_grace_pass_logs_summary(self, capsys):
        """The grace pass must announce itself so CI logs show when the
        budget cliff fired (observability parity with the timeout
        warning it follows)."""
        ar = _build_trivial_router()
        ar.route_all_negotiated(
            max_iterations=2,
            timeout=1e-9,
            adaptive=False,
            perturbation=False,
        )
        out = capsys.readouterr().out
        assert "Grace pass:" in out
        assert "Issue #3452" in out
