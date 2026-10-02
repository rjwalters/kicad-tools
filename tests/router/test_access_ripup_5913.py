"""Access-loss rip-up targeting: the witness names which committed nets to rip.

Epic #5508 / Phase 3a (issue #5913).  Phase 1 computed each pad's access set and
named the copper that closed it; Phase 2 (#5891) refused a commit that would
empty another unrouted pad's access set.  Neither helps once a pad's access set
IS empty: the enclosing copper is legal and unshared, so overuse-based rip-up
cannot see it at all, and the Bresenham blocker scan
(``find_blocking_nets_for_connection``) only looks along the direct line.

This module pins the third half of the mechanism:
:mod:`kicad_tools.router.access_ripup` reads the witness's ``closing_copper``
for the *failed* net's own terminals and hands the committed nets it names to
the existing :meth:`NegotiatedRouter.targeted_ripup` as ``blocking_nets``.

Geometry is the same 4-pad Kelvin cluster Phase 1a and Phase 2 use
(``tests/router/test_pad_access_5508.py``,
``tests/router/test_pad_access_invariant_5891.py``): U3.1's only surviving exit
is a north corridor, and ``COMP``'s track through that corridor empties its
access set.
"""

from __future__ import annotations

import pytest

from kicad_tools.router.access_ripup import (
    MAX_TARGETING_EVALUATIONS,
    RIPPABLE_CLOSING_KINDS,
    AccessLossTargeter,
    AccessLossWitness,
    format_access_loss_report,
    rippable_closing_nets,
)
from kicad_tools.router.access_witness import PASS_INITIAL
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.pad_access import AccessSet, ClosingCopper, compute_access_set
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

# Verbatim from test_pad_access_invariant_5891 so the three phases share one
# geometry.
KELVIN_POSITIONS = {"R10": (5, 3), "Q1": (7, 11), "U2": (9, 14), "U3": (5, 15)}
U9_PADS = {"1": (3.6, 15.0), "2": (6.4, 15.0), "3": (5.0, 16.4)}
COMP_Y = 14.2
COMP_X1, COMP_X2 = 1.0, 6.5

ISENSE_NET = 1
COMP_NET = 2
FOREIGN_NET = 3


def _rules(**overrides) -> DesignRules:
    params = {"grid_resolution": 0.1, "trace_width": 0.2, "trace_clearance": 0.2}
    params.update(overrides)
    return DesignRules(**params)


def _add_pad(router, ref, pin, x, y, net, net_name, **extra):
    router.add_component(
        ref,
        [
            {
                "number": pin,
                "x": x,
                "y": y,
                "width": extra.pop("width", 1),
                "height": extra.pop("height", 1),
                "net": net,
                "net_name": net_name,
                "layer": Layer.F_CU,
                **extra,
            }
        ],
    )


def _kelvin_cluster(**rule_overrides) -> Autorouter:
    """The 4-pad Kelvin cluster plus the U9 flankers.  Nothing is routed."""
    router = Autorouter(
        20,
        20,
        rules=_rules(**rule_overrides),
        force_python=True,
        physics_enabled=False,
    )
    for ref in ("R10", "U3", "Q1", "U2"):
        x, y = KELVIN_POSITIONS[ref]
        _add_pad(router, ref, "1", x, y, ISENSE_NET, "ISENSE_A+")
    for pin, (x, y) in U9_PADS.items():
        _add_pad(router, "U9", pin, x, y, FOREIGN_NET, "FOREIGN")
    router._commit_journal.set_context(PASS_INITIAL, 0)
    return router


def _comp_route(net: int = COMP_NET, net_name: str = "COMP") -> Route:
    return Route(
        net=net,
        net_name=net_name,
        segments=[
            Segment(
                x1=COMP_X1,
                y1=COMP_Y,
                x2=COMP_X2,
                y2=COMP_Y,
                width=0.2,
                layer=Layer.F_CU,
                net=net,
                net_name=net_name,
            )
        ],
    )


def _sealed_cluster() -> tuple[Autorouter, Route]:
    """The cluster with ``COMP``'s track already committed -- U3.1 is stranded.

    Committed through the *un-gated* ``_mark_route`` path on purpose: Phase 2's
    gate exists to refuse exactly this commit, and Phase 3a's job starts where
    the gate could not act (a budget-truncated gate, a victim net that already
    had copper, a strand produced by several individually-admissible commits).
    """
    router = _kelvin_cluster()
    route = _comp_route()
    assert router._mark_route(route) is True
    assert compute_access_set(router.pads[("U3", "1")], router.grid, router.rules).is_empty()
    return router, route


def _closing(
    *,
    kind: str = "route_segment",
    net: int = COMP_NET,
    net_name: str = "COMP",
    ref: str = "COMP",
    pin: str = "",
) -> ClosingCopper:
    return ClosingCopper(
        kind=kind,  # type: ignore[arg-type]
        net=net,
        net_name=net_name,
        ref=ref,
        pin=pin,
        layer=Layer.F_CU,
        marking=frozenset(),
        shareable=False,
        bbox=(0.0, 0.0, 1.0, 1.0),
    )


def _empty_access(*items: ClosingCopper) -> AccessSet:
    return AccessSet(
        pad_key=("U3", "1"),
        net=ISENSE_NET,
        origin=(5.0, 15.0),
        origin_layer=Layer.F_CU,
        origin_is_escape_terminal=False,
        stubs=(),
        via_sites=(),
        closing_copper=items,
        bbox=(0.0, 0.0, 1.0, 1.0),
        fingerprint=(0.2, 0.2, 0.2, 0.2, None, 2),
    )


# ---------------------------------------------------------------------------
# The target filter: which named closing copper is a legitimate rip-up target
# ---------------------------------------------------------------------------


def test_committed_route_copper_of_a_rippable_net_is_a_target():
    targets, held = rippable_closing_nets(
        _empty_access(_closing()), failed_net=ISENSE_NET, rippable_nets={COMP_NET}
    )
    assert targets == {COMP_NET: "COMP"}
    assert held == set()


def test_route_via_copper_is_a_target_too():
    targets, _held = rippable_closing_nets(
        _empty_access(_closing(kind="route_via")),
        failed_net=ISENSE_NET,
        rippable_nets={COMP_NET},
    )
    assert targets == {COMP_NET: "COMP"}


def test_net_outside_the_rippable_set_is_held_not_ripped():
    """Escape stubs, preserved copper and coupled bodies are not in ``net_routes``.

    The negotiated loop's ``net_routes`` holds only the nets it routed itself,
    so restricting targets to it is what keeps this phase inside the epic's
    binding scope guard -- the same ``net_routes.get(v)`` test
    ``_relief_rescue_txn`` already uses to pick its victims.
    """
    targets, held = rippable_closing_nets(
        _empty_access(_closing()), failed_net=ISENSE_NET, rippable_nets=set()
    )
    assert targets == {}
    assert held == {COMP_NET}


def test_own_net_copper_is_never_a_target():
    targets, held = rippable_closing_nets(
        _empty_access(_closing(net=ISENSE_NET, net_name="ISENSE_A+")),
        failed_net=ISENSE_NET,
        rippable_nets={ISENSE_NET},
    )
    assert targets == {}
    assert held == set()


def test_kelvin_isolated_sibling_copper_is_never_a_target():
    """Same-family isolation: a Kelvin sibling branch is not rip-up material.

    ``kelvin_isolated`` is the kind
    :func:`~kicad_tools.router.pad_access.compute_access_set` gives copper that
    is same-net but was treated as a hard obstacle for one edge search.  Ripping
    it would delete the failed net's own committed branch.
    """
    targets, held = rippable_closing_nets(
        _empty_access(_closing(kind="kelvin_isolated", net=ISENSE_NET, net_name="ISENSE_A+")),
        failed_net=ISENSE_NET,
        rippable_nets={ISENSE_NET, COMP_NET},
    )
    assert targets == {}
    assert held == set()


@pytest.mark.parametrize("kind", ["foreign_pad", "fixed_fill", "keepout", "board_edge"])
def test_unrippable_copper_kinds_are_never_targets(kind):
    """A pad, a fill, a keepout and the board edge cannot be ripped up."""
    targets, held = rippable_closing_nets(
        _empty_access(_closing(kind=kind, net=COMP_NET)),
        failed_net=ISENSE_NET,
        rippable_nets={COMP_NET},
    )
    assert targets == {}
    assert held == {COMP_NET}
    assert kind not in RIPPABLE_CLOSING_KINDS


def test_reserved_hard_copper_is_held():
    targets, held = rippable_closing_nets(
        _empty_access(_closing(kind="reserved_hard", net=COMP_NET)),
        failed_net=ISENSE_NET,
        rippable_nets={COMP_NET},
    )
    assert targets == {}
    assert held == {COMP_NET}


def test_netless_closing_copper_is_neither_target_nor_held():
    targets, held = rippable_closing_nets(
        _empty_access(_closing(kind="board_edge", net=0, net_name="", ref="<board-edge>")),
        failed_net=ISENSE_NET,
        rippable_nets={COMP_NET},
    )
    assert targets == {}
    assert held == set()


# ---------------------------------------------------------------------------
# The targeter: witness -> blocking nets
# ---------------------------------------------------------------------------


def test_targeter_names_the_committed_net_that_sealed_the_pad():
    router, _route = _sealed_cluster()
    targeter = AccessLossTargeter(router)

    witness = targeter.blockers_for(ISENSE_NET, {COMP_NET})

    assert isinstance(witness, AccessLossWitness)
    assert witness.failed_net == ISENSE_NET
    assert witness.failed_net_name == "ISENSE_A+"
    assert witness.blocking_nets == (COMP_NET,)
    assert witness.blocking_net_names == ("COMP",)
    assert ("U3", "1") in witness.stranded_pads
    assert "COMP" in witness.closing_refs
    assert "route_segment" in witness.closing_kinds
    assert witness.pass_name == PASS_INITIAL
    assert witness.iteration == 0
    assert "U3.1" in witness.one_line()
    assert "COMP" in witness.one_line()
    assert witness.to_dict()["blocking_nets"] == [COMP_NET]
    assert format_access_loss_report([witness]) == [witness.one_line()]


def test_targeter_records_the_witness_for_the_run_summary():
    router, _route = _sealed_cluster()
    targeter = AccessLossTargeter(router)
    targeter.blockers_for(ISENSE_NET, {COMP_NET})

    assert len(targeter.witnesses) == 1
    assert targeter.queries == 1
    assert targeter.hits == 1
    assert targeter.evaluations > 0
    assert targeter.truncated is False
    assert "1 net(s)" in targeter.summary_line()
    assert targeter.to_dict()["witnesses"][0]["failed_net"] == ISENSE_NET


def test_a_net_whose_pads_all_still_have_a_way_out_produces_no_witness():
    """The cheap existence test answers the common case and nothing is named."""
    router = _kelvin_cluster()
    targeter = AccessLossTargeter(router)

    assert targeter.blockers_for(ISENSE_NET, {COMP_NET}) is None
    assert targeter.witnesses == []
    assert targeter.hits == 0
    # One cheap ``has_access`` per terminal, no full access-set sweep.
    assert targeter.evaluations == 4


def test_witness_with_no_rippable_target_is_reported_but_rips_nothing():
    router, _route = _sealed_cluster()
    targeter = AccessLossTargeter(router)

    witness = targeter.blockers_for(ISENSE_NET, set())

    assert witness is not None
    assert witness.blocking_nets == ()
    # COMP's track closed it, and so did the U9 flanker pads -- both are
    # reported as held, neither is a rip-up target.
    assert COMP_NET in witness.held_nets
    assert FOREIGN_NET in witness.held_nets
    assert "nothing rippable" in witness.one_line()


def test_targeter_is_inert_when_disabled():
    router, _route = _sealed_cluster()
    targeter = AccessLossTargeter(router, enabled=False)

    assert targeter.blockers_for(ISENSE_NET, {COMP_NET}) is None
    assert targeter.evaluations == 0


def test_exhausted_evaluation_budget_fails_open_and_says_so():
    router, _route = _sealed_cluster()
    targeter = AccessLossTargeter(router, max_evaluations=0)

    assert targeter.blockers_for(ISENSE_NET, {COMP_NET}) is None
    assert targeter.truncated is True
    assert "budget truncated" in targeter.summary_line()
    assert MAX_TARGETING_EVALUATIONS > 0


def test_targeter_never_mutates_the_grid():
    router, _route = _sealed_cluster()
    routes_before = list(router.grid.routes)
    cells_before = int(router.grid._blocked.sum())

    AccessLossTargeter(router).blockers_for(ISENSE_NET, {COMP_NET})

    assert list(router.grid.routes) == routes_before
    assert int(router.grid._blocked.sum()) == cells_before


def test_unknown_net_is_not_a_query():
    router, _route = _sealed_cluster()
    targeter = AccessLossTargeter(router)

    assert targeter.blockers_for(999, {COMP_NET}) is None
    assert targeter.queries == 0


# ---------------------------------------------------------------------------
# The Autorouter wiring
# ---------------------------------------------------------------------------


def test_router_hands_the_witness_named_nets_to_the_rip_up_path():
    router, route = _sealed_cluster()
    net_routes = {COMP_NET: [route]}

    assert router._access_loss_blockers(ISENSE_NET, net_routes) == {COMP_NET}
    assert router.access_loss_targeter is not None
    assert [w.blocking_nets for w in router.access_loss_witnesses] == [(COMP_NET,)]


def test_router_holds_copper_the_negotiated_loop_does_not_own():
    """``--preserve-existing`` copper and escape stubs are not in ``net_routes``."""
    router, _route = _sealed_cluster()

    assert router._access_loss_blockers(ISENSE_NET, {}) == set()
    # The query was never even made: with nothing rippable there is no target
    # set to compute, so the targeter costs the run nothing.
    assert router.access_loss_witnesses == []


def test_router_does_not_target_an_emptied_net_routes_entry():
    """A ripped-then-failed net leaves ``net_routes[n] == []`` -- not rippable."""
    router, route = _sealed_cluster()

    assert router._access_loss_blockers(ISENSE_NET, {COMP_NET: []}) == set()
    assert route in router.grid.routes


def test_disabling_the_pad_access_invariant_disables_access_loss_targeting():
    """One knob for the whole witness mechanism: the Phase 2 opt-out.

    Phase 3a consumes Phase 1's witness through the same default-on switch
    Phase 2 introduced, so ``--no-pad-access-invariant`` restores the
    pre-#5913 rip-up targeting exactly.
    """
    router, route = _sealed_cluster()
    router.enable_pad_access_invariant = False

    assert router._access_loss_blockers(ISENSE_NET, {COMP_NET: [route]}) == set()
    assert router.access_loss_targeter is None
    assert router.access_loss_witnesses == []


# ---------------------------------------------------------------------------
# The rip-up itself: through the existing transactional guard
# ---------------------------------------------------------------------------


def _negotiated(router: Autorouter):
    from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

    return NegotiatedRouter(router.grid, router.router, router.rules, router.net_class_map)


def test_witness_named_net_is_ripped_and_the_failed_net_lands():
    """The end of the chain: the named net yields its copper and ISENSE routes."""
    router, route = _sealed_cluster()
    neg = _negotiated(router)
    net_routes: dict[int, list[Route]] = {COMP_NET: [route]}
    router.routes.append(route)
    pads_by_net = {
        ISENSE_NET: [router.pads[(ref, "1")] for ref in ("Q1", "R10", "U2", "U3")],
        COMP_NET: [],
    }

    blocking = router._access_loss_blockers(ISENSE_NET, net_routes)
    assert blocking == {COMP_NET}

    ripup_history: dict[int, int] = {}
    success = neg.targeted_ripup(
        failed_net=ISENSE_NET,
        blocking_nets=blocking,
        net_routes=net_routes,
        routes_list=router.routes,
        pads_by_net=pads_by_net,
        present_cost_factor=1.0,
        mark_route_callback=router._mark_route,
        ripup_history=ripup_history,
        per_net_timeout=20.0,
    )

    assert success is True
    # The existing per-net rip-up budget was charged, not bypassed.
    assert ripup_history[COMP_NET] == 1
    # COMP's sealing copper is gone and U3.1 has a way out again.
    assert route not in router.grid.routes
    assert not compute_access_set(router.pads[("U3", "1")], router.grid, router.rules).is_empty()
    assert net_routes[ISENSE_NET]


def test_failed_retry_rolls_back_the_witness_named_rip_up_verbatim():
    """A rip-up whose reroute does not converge must restore the exact copper.

    ``targeted_ripup`` is the transaction (#3470); Phase 3a only changes which
    nets go into ``blocking_nets``, so the all-or-nothing rollback has to hold
    for a witness-named target exactly as it does for a Bresenham-named one.
    Here the failed net is given a single pad, so its reroute cannot run at all
    and the transaction must fail closed.
    """
    router, route = _sealed_cluster()
    neg = _negotiated(router)
    net_routes: dict[int, list[Route]] = {COMP_NET: [route]}
    router.routes.append(route)
    cells_before = int(router.grid._blocked.sum())

    success = neg.targeted_ripup(
        failed_net=ISENSE_NET,
        blocking_nets={COMP_NET},
        net_routes=net_routes,
        routes_list=router.routes,
        pads_by_net={ISENSE_NET: [router.pads[("U3", "1")]], COMP_NET: []},
        present_cost_factor=1.0,
        mark_route_callback=router._mark_route,
        ripup_history={},
        per_net_timeout=5.0,
    )

    assert success is False
    # Verbatim restore: the same Route object, back on the grid and in the list.
    assert net_routes[COMP_NET] == [route]
    assert route in router.grid.routes
    assert route in router.routes
    assert int(router.grid._blocked.sum()) == cells_before


# ---------------------------------------------------------------------------
# End to end through the negotiated loop
# ---------------------------------------------------------------------------


@pytest.mark.slow
def test_negotiated_run_recovers_a_strand_the_commit_gate_could_not_refuse():
    """Phase 3a's reason to exist, on a real negotiated run.

    The commit gate fails **open** when its evaluation budget is exhausted
    (``PadAccessInvariant.truncated``), which is exactly the state Phase 2
    documents as "says it ran out of budget" rather than silently enforcing.
    With the gate budget pinned to zero, ``COMP`` commits first and strands
    ``U3.1``.  A sealed pad does not hard-fail the negotiated A* -- the search
    prices its way out through the blocking copper instead, which is why the
    board-05 failure mode is *shorts*, not opens -- so the net arrives in the
    targeted rip-up's conflicting-net cohort.  Pre-#5913 the Bresenham scan
    could not name ``COMP`` there; the access-loss witness does.
    """
    router = _kelvin_cluster()
    _add_pad(router, "R20", "1", 3.0, COMP_Y, COMP_NET, "COMP")
    _add_pad(router, "R21", "1", 7.0, COMP_Y, COMP_NET, "COMP")
    # Starve the commit gate so the stranding commit is admitted (fail-open).
    from kicad_tools.router.pad_access_invariant import PadAccessInvariant

    router._pad_access_invariant = PadAccessInvariant(router, max_evaluations=0)
    router.route_all_negotiated(
        max_iterations=3,
        timeout=120,
        per_net_timeout=15,
        use_targeted_ripup=True,
    )

    assert router._pad_access_invariant.truncated is True
    # The access-loss witness fired and named COMP as the net to rip.
    witnesses = router.access_loss_witnesses
    assert witnesses, "the sealed ISENSE net should have produced a witness"
    assert COMP_NET in {net for w in witnesses for net in w.blocking_nets}
    assert ("U3", "1") in {pad for w in witnesses for pad in w.stranded_pads}
