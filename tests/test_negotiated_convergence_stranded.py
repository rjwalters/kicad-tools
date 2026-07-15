"""Tests for the negotiated-loop convergence predicate (Issue #3448).

The negotiated routing loop previously treated
``overflow == 0 and len(net_routes) == total_nets`` as "Convergence
achieved" and exited.  That count predicate is wrong on two counts:

1. ``rip_up_nets`` leaves ``net_routes[n] == []`` behind, so a net that
   was ripped up and then hard-failed its re-route (instant empty A*
   frontier => ZERO overflow, e.g. board-07 DQ3 in 0.04s) still counted
   toward the total.
2. A multi-pad net whose main A* failed can sit in ``net_routes`` with
   partial fragments (a subset of its MST edges) that connect only some
   of its pads.

Either way the loop declared convergence while nets were stranded and
most of the wall-clock budget was unused (board 07: exit at ~820s of a
1500s budget with 3 nets unrouted).

These tests pin the fixed semantics:

- A partially-routed net must block the iteration-0 "No conflicts -
  routing complete!" early return AND the in-loop "Convergence
  achieved" break; the loop must keep working (more iterations) until
  an explicit stall criterion or the budget fires.
- An empty-list (ripped-then-hard-failed) entry must block convergence
  the same way.
- A genuinely fully-routed board must still complete/converge.
- The final progress-callback status must never say ``converged`` while
  stranded nets remain (it says ``stranded`` instead).

The fixtures monkeypatch ``Autorouter._route_net_negotiated`` rather
than relying on board geometry: negotiated mode can route THROUGH
obstacle cells at overflow cost, so geometric walls cannot produce a
deterministic hard failure, while the patch reproduces the exact
board-07 mechanism (a net whose re-route fails instantly with zero
overflow).
"""

from __future__ import annotations

import pytest

from kicad_tools.router.core import Autorouter


def _add_two_pad_net(ar: Autorouter, net: int, name: str, p1, p2, refs) -> None:
    ar.add_component(
        refs[0],
        [{"number": "1", "x": p1[0], "y": p1[1], "net": net, "net_name": name}],
    )
    ar.add_component(
        refs[1],
        [{"number": "1", "x": p2[0], "y": p2[1], "net": net, "net_name": name}],
    )


def _build_two_net_router_with_three_pad_net2() -> Autorouter:
    """Net 1: trivial 2-pad net.  Net 2: three pads on separate
    components (A, B, C) so a "partial" result (A-B fragment only) is
    expressible.  Corridors are disjoint, so overflow stays 0.
    """
    ar = Autorouter(width=20.0, height=20.0)
    _add_two_pad_net(ar, 1, "NET1", (2.0, 2.0), (18.0, 2.0), ("R1", "R2"))
    ar.add_component(
        "U1",
        [{"number": "1", "x": 2.0, "y": 10.0, "net": 2, "net_name": "NET2"}],
    )
    ar.add_component(
        "U2",
        [{"number": "1", "x": 8.0, "y": 10.0, "net": 2, "net_name": "NET2"}],
    )
    ar.add_component(
        "U3",
        [{"number": "1", "x": 17.0, "y": 15.0, "net": 2, "net_name": "NET2"}],
    )
    return ar


def _patch_net2(ar: Autorouter, fail_after: int):
    """Replace ``_route_net_negotiated`` for net 2.

    The first ``fail_after`` attempts return a PARTIAL route (only the
    A-B MST edge, produced by temporarily shrinking the net's pad list
    to two pads) so the net lands in ``net_routes`` with fragments that
    do not connect pad C.  Every later attempt returns ``[]`` -- the
    instant-empty-frontier hard fail that produces ZERO overflow.

    Returns a dict tracking the per-net-2 attempt count.
    """
    orig = ar._route_net_negotiated
    attempts = {"net2": 0}

    def fake(net: int, present_factor: float, per_net_timeout=None):
        if net != 2:
            return orig(net, present_factor, per_net_timeout=per_net_timeout)
        attempts["net2"] += 1
        if attempts["net2"] <= fail_after:
            saved = ar.nets[2]
            ar.nets[2] = saved[:2]
            try:
                return orig(net, present_factor, per_net_timeout=per_net_timeout)
            finally:
                ar.nets[2] = saved
        return []

    ar._route_net_negotiated = fake
    return attempts


class TestPartialNetBlocksCompletion:
    """A partially-routed net must never satisfy the convergence /
    completion predicates, no matter what ``len(net_routes)`` says.
    """

    def test_partial_net_blocks_initial_complete_and_convergence(self, capsys):
        ar = _build_two_net_router_with_three_pad_net2()
        # Net 2 is ALWAYS partial: every attempt routes only A-B.
        attempts = _patch_net2(ar, fail_after=10_000)
        routes = ar.route_all_negotiated(
            max_iterations=3,
            timeout=60.0,
            perturbation=False,
        )
        out = capsys.readouterr().out

        # The old iteration-0 early return: count matched (2 == 2) at
        # overflow 0, so the loop printed "No conflicts - routing
        # complete!" and returned with NET2 at 2/3 pads.
        assert "No conflicts - routing complete!" not in out

        # The in-loop break must not fire either.
        assert "Convergence achieved" not in out

        # The loop must say WHY it kept going and actually keep going
        # (consume budget on recovery iterations instead of exiting).
        assert "Issue #3448" in out
        assert "--- Iteration 1: Rip-up and reroute ---" in out
        assert attempts["net2"] >= 2, (
            f"net 2 only attempted {attempts['net2']} time(s); the loop "
            "exited instead of continuing to work the stranded net"
        )

        # Net 1 must still be routed.
        assert any(r.net == 1 for r in routes)

    def test_final_status_is_stranded_not_converged(self):
        """The terminal progress-callback status must not claim
        ``converged`` while a net is stranded at overflow == 0.
        """
        ar = _build_two_net_router_with_three_pad_net2()
        # Issue #4159: disable the post-negotiation rescue sweep here.  The
        # sweep re-attempts NET2 SOLO via the REAL ``route_net`` (all three
        # pads, not the patched partial ``_route_net_negotiated``) and would
        # legitimately rescue it -- flipping the terminal status to
        # ``converged`` and defeating this test's purpose, which is to pin the
        # RAW negotiated status-string logic for a genuinely stranded net.
        ar._post_negotiation_rescue = False
        _patch_net2(ar, fail_after=10_000)
        messages: list[str] = []

        def cb(progress: float, message: str, cancellable: bool) -> bool:
            messages.append(message)
            return True

        ar.route_all_negotiated(
            max_iterations=2,
            timeout=60.0,
            perturbation=False,
            progress_callback=cb,
        )
        final = [m for m in messages if m.startswith("Routing complete")]
        assert final, f"no terminal progress message in {messages!r}"
        assert "(converged)" not in final[-1]
        assert "(stranded)" in final[-1]


class TestEmptyEntryBlocksConvergence:
    """The board-07 trap: a net that routes (partially) at iteration 0,
    is ripped up by the recovery loop (leaving ``net_routes[n] == []``),
    and then hard-fails every re-route with an instant empty frontier
    (zero overflow).  ``len(net_routes)`` then equals ``total_nets`` and
    the old loop printed "Convergence achieved" with the net stranded
    and wall budget remaining.
    """

    def test_hard_failed_net_with_budget_keeps_loop_running(self, capsys):
        ar = _build_two_net_router_with_three_pad_net2()
        # First attempt partial (so the #2475 hook rips it up on
        # iteration 1, leaving net_routes[2] == []), then hard fail.
        attempts = _patch_net2(ar, fail_after=1)

        routes = ar.route_all_negotiated(
            max_iterations=4,
            timeout=60.0,
            perturbation=False,
        )
        out = capsys.readouterr().out

        # Net 2 is stranded with budget remaining: convergence must NOT
        # be declared (the old code printed it here -- the empty-list
        # entry kept len(net_routes) == total_nets at overflow 0).
        assert "Convergence achieved" not in out
        assert "No conflicts - routing complete!" not in out

        # The loop must keep working: net 2 re-attempted after the
        # iteration-0 pass instead of being declared converged.
        assert attempts["net2"] >= 2, (
            f"net 2 only attempted {attempts['net2']} time(s); "
            "the loop gave up instead of continuing"
        )
        assert "Issue #3448" in out

        # Net 1 must survive.
        assert any(r.net == 1 for r in routes)


class TestGenuineConvergenceStillFires:
    """The stricter predicate must not break genuine completion."""

    def test_fully_routable_board_completes(self, capsys):
        ar = Autorouter(width=20.0, height=20.0)
        _add_two_pad_net(ar, 1, "NET1", (2.0, 10.0), (18.0, 10.0), ("R1", "R2"))
        _add_two_pad_net(ar, 2, "NET2", (10.0, 2.0), (10.0, 18.0), ("R3", "R4"))
        routes = ar.route_all_negotiated(
            max_iterations=5,
            timeout=30.0,
            perturbation=False,
        )
        out = capsys.readouterr().out
        # Either the iteration-0 early return or an in-loop convergence
        # break must fire -- the new predicate must not block a genuinely
        # complete result.
        assert "No conflicts - routing complete!" in out or "Convergence achieved" in out
        routed_nets = {r.net for r in routes}
        assert {1, 2} <= routed_nets


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
