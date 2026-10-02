"""Commit-time pad-access invariant: a commit that would strand a pad is refused.

Epic #5508 / Phase 2 (issue #5891).  Exercises
:mod:`kicad_tools.router.pad_access_invariant` on the same 4-pad Kelvin cluster
Phase 1a used (``tests/router/test_pad_access_5508.py``, itself derived from
``tests/router/test_lattice_kelvin_physical_5444.py::_route_fixture``): U3.1's
only surviving exit is a north corridor, and a competing net's track through
that corridor empties its access set.

Phase 1a proved the *read-only* verdict (``AccessSet.is_empty()`` flips).  This
module proves the *rule*: with the invariant on, the competing commit is refused
and U3.1 keeps its way out; with it off, the commit lands and U3.1 is stranded,
byte-for-byte as before #5891.
"""

import pytest

from kicad_tools.router.access_witness import PASS_INITIAL
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.pad_access import compute_access_set, has_access
from kicad_tools.router.pad_access_invariant import (
    AccessVeto,
    PadAccessInvariant,
    conservative_access_bbox,
    format_veto_report,
)
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.rules import DesignRules

# Verbatim from test_pad_access_5508 so the two phases share one geometry.
KELVIN_POSITIONS = {"R10": (5, 3), "Q1": (7, 11), "U2": (9, 14), "U3": (5, 15)}
U9_PADS = {"1": (3.6, 15.0), "2": (6.4, 15.0), "3": (5.0, 16.4)}
COMP_Y = 14.2

#: COMP's track stops short of U2 (whose own access bbox starts at x ~= 7.29) so
#: the first terminal the gate reaches in sorted ``(ref, pin)`` order is U3.1 --
#: the pad this fixture is about.  Phase 1a's longer x1..x9 track also closes
#: U2.1, which would make the veto's identity depend on scan order.
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


def _u3_access(router):
    return compute_access_set(router.pads[("U3", "1")], router.grid, router.rules)


def _marked_cell_count(router) -> int:
    """Blocked-cell total -- the coarse fingerprint of "copper landed"."""
    return int(router.grid._blocked.sum())


# ---------------------------------------------------------------------------
# Fixture sanity: the Phase 1a precondition still holds with the shorter track
# ---------------------------------------------------------------------------


def test_fixture_precondition_u3_is_reachable_and_comp_would_strand_it():
    router = _kelvin_cluster()
    assert not _u3_access(router).is_empty()

    # Marked WITHOUT the gate (no opt-in): the pre-#5891 path.
    assert router._mark_route(_comp_route()) is True
    assert _u3_access(router).is_empty()
    # And the un-opted-in path never even builds the gate.
    assert router.pad_access_invariant is None


# ---------------------------------------------------------------------------
# The headline assertion: the commit is refused
# ---------------------------------------------------------------------------


def test_commit_that_would_strand_u3_is_refused():
    router = _kelvin_cluster()
    before_cells = _marked_cell_count(router)
    before_routes = len(router.grid.routes)

    committed = router._mark_route(_comp_route(), enforce_pad_access=True)

    assert committed is False
    # Nothing landed: no copper, no grid route, and U3 still has its exit.
    assert _marked_cell_count(router) == before_cells
    assert len(router.grid.routes) == before_routes
    assert not _u3_access(router).is_empty()


def test_refusal_names_the_pad_and_the_candidate_net():
    router = _kelvin_cluster()
    router._mark_route(_comp_route(), enforce_pad_access=True)

    vetoes = router.pad_access_vetoes
    assert len(vetoes) == 1
    veto = vetoes[0]
    assert isinstance(veto, AccessVeto)
    assert veto.pad_key == ("U3", "1")
    assert veto.label == "U3.1"
    assert veto.pad_net == ISENSE_NET
    assert veto.pad_net_name == "ISENSE_A+"
    assert veto.candidate_net == COMP_NET
    assert veto.candidate_net_name == "COMP"
    assert veto.pass_name == PASS_INITIAL
    assert veto.iteration == 0
    # U3's last way out was the single north stub, plus the one layer transition
    # reachable from that stub's far end -- both of which COMP's track removes.
    assert veto.stubs_before == 1
    assert veto.via_sites_before == 1
    # The witness names the copper that closed it: COMP's own track.
    assert "COMP" in veto.closing_refs
    assert "route_segment" in veto.closing_kinds
    assert "U3.1" in veto.one_line()
    assert veto.to_dict()["pad"] == "U3.1"
    assert format_veto_report(vetoes) == [veto.one_line()]


def test_gate_summary_counts_the_refusal():
    router = _kelvin_cluster()
    router._mark_route(_comp_route(), enforce_pad_access=True)

    gate = router.pad_access_invariant
    assert gate is not None
    assert gate.armed is True
    assert gate.truncated is False
    assert gate.checks == 1
    assert gate.evaluations >= 2  # at least one "before" and one "after"
    # Every terminal of the two multi-terminal nets is protected; COMP has no
    # pads at all, so it contributes none.
    assert set(gate.protected) == {
        ("R10", "1"),
        ("Q1", "1"),
        ("U2", "1"),
        ("U3", "1"),
        ("U9", "1"),
        ("U9", "2"),
        ("U9", "3"),
    }
    assert "1 commit(s) refused" in gate.summary_line()
    assert gate.vetoed_pads() == (("U3", "1"),)


# ---------------------------------------------------------------------------
# The disable option: byte-identical to pre-#5891
# ---------------------------------------------------------------------------


def test_disabled_invariant_commits_exactly_as_before():
    enabled = _kelvin_cluster()
    disabled = _kelvin_cluster()
    disabled.enable_pad_access_invariant = False

    assert enabled._mark_route(_comp_route(), enforce_pad_access=True) is False
    assert disabled._mark_route(_comp_route(), enforce_pad_access=True) is True

    # The disabled router took the pre-#5891 path verbatim: copper landed, U3 is
    # stranded, and the gate was never even constructed.
    assert disabled.pad_access_invariant is None
    assert disabled.pad_access_vetoes == []
    assert _u3_access(disabled).is_empty()

    # ...and the resulting grid matches a router that never heard of the flag.
    control = _kelvin_cluster()
    control._mark_route(_comp_route())
    assert _marked_cell_count(disabled) == _marked_cell_count(control)
    assert len(disabled.grid.routes) == len(control.grid.routes)


def test_cli_opt_out_clears_the_flag():
    from argparse import Namespace

    from kicad_tools.cli.route_cmd import _apply_pad_access_invariant

    router = _kelvin_cluster()
    _apply_pad_access_invariant(router, Namespace())
    assert router.enable_pad_access_invariant is True

    _apply_pad_access_invariant(router, Namespace(no_pad_access_invariant=False))
    assert router.enable_pad_access_invariant is True

    _apply_pad_access_invariant(router, Namespace(no_pad_access_invariant=True))
    assert router.enable_pad_access_invariant is False


# ---------------------------------------------------------------------------
# What must NOT be vetoed
# ---------------------------------------------------------------------------


def test_own_net_copper_is_never_vetoed():
    """A net's own copper closing its own pad is that net's business."""
    router = _kelvin_cluster()
    assert (
        router._mark_route(
            _comp_route(net=ISENSE_NET, net_name="ISENSE_A+"), enforce_pad_access=True
        )
        is True
    )
    assert router.pad_access_vetoes == []


def test_already_stranded_pad_does_not_veto_a_later_commit():
    """The #5639 transition rule: only a non-empty -> empty flip is a stranding."""
    router = _kelvin_cluster()
    # Strand U3 first, un-gated (as the pre-#5891 router would have).
    router._mark_route(_comp_route())
    assert _u3_access(router).is_empty()

    # A second, parallel foreign track now commits freely: it did not strand U3.
    second = Route(
        net=4,
        net_name="OTHER",
        segments=[
            Segment(
                x1=COMP_X1,
                y1=COMP_Y - 0.5,
                x2=COMP_X2,
                y2=COMP_Y - 0.5,
                width=0.2,
                layer=Layer.F_CU,
                net=4,
                net_name="OTHER",
            )
        ],
    )
    assert router._mark_route(second, enforce_pad_access=True) is True
    assert router.pad_access_vetoes == []


def test_escape_copper_is_the_baseline_not_a_candidate():
    router = _kelvin_cluster()
    escape = _comp_route()
    escape.is_escape = True
    assert router._mark_route(escape, enforce_pad_access=True) is True
    assert router.pad_access_vetoes == []


def test_pad_of_an_already_committed_net_is_not_protected():
    """A net that already landed copper is being served, not waiting its turn."""
    router = _kelvin_cluster()
    # Give the ISENSE net a committed route far from U3 so the net counts as
    # "has copper" without touching U3's access.
    seeded = Route(
        net=ISENSE_NET,
        net_name="ISENSE_A+",
        segments=[
            Segment(
                x1=5.0,
                y1=3.0,
                x2=5.0,
                y2=5.0,
                width=0.2,
                layer=Layer.F_CU,
                net=ISENSE_NET,
                net_name="ISENSE_A+",
            )
        ],
    )
    router._mark_route(seeded)
    assert not _u3_access(router).is_empty()

    assert router._mark_route(_comp_route(), enforce_pad_access=True) is True
    assert router.pad_access_vetoes == []
    assert _u3_access(router).is_empty()


def test_empty_route_is_not_a_candidate():
    router = _kelvin_cluster()
    assert router._mark_route(Route(net=COMP_NET, net_name="COMP"), enforce_pad_access=True) is True
    assert router.pad_access_vetoes == []


# ---------------------------------------------------------------------------
# Cost bounds -- the gate must stay affordable inside the commit path
# ---------------------------------------------------------------------------


def test_candidate_far_from_every_pad_costs_no_access_evaluation():
    router = _kelvin_cluster()
    far = Route(
        net=COMP_NET,
        net_name="COMP",
        segments=[
            Segment(
                x1=18.0,
                y1=1.0,
                x2=19.0,
                y2=1.0,
                width=0.2,
                layer=Layer.F_CU,
                net=COMP_NET,
                net_name="COMP",
            )
        ],
    )
    assert router._mark_route(far, enforce_pad_access=True) is True
    gate = router.pad_access_invariant
    assert gate is not None
    assert gate.checks == 1
    assert gate.evaluations == 0  # the bbox prefilter alone answered it


def test_before_set_is_cached_against_the_grids_own_mutation_counter():
    router = _kelvin_cluster()
    gate = router._pad_access_gate()
    assert gate is not None

    candidate = _comp_route()
    first = gate.veto_for(candidate)
    assert first is None or first.pad_key == ("U3", "1")
    after_first = gate.evaluations

    # No copper moved, so every "before" set is reusable: the second check pays
    # only for the speculative "after" evaluations.
    gate.veto_for(candidate)
    assert gate.evaluations < after_first * 2


def test_distant_copper_does_not_invalidate_a_cached_before_verdict():
    """The cache is invalidated by geometry, not by the journal's length.

    A commit on the far side of the board appends a journal record, which a
    counter-keyed cache would read as "everything is stale".  Only the
    terminals that commit's copper could actually have touched may be dropped.
    """
    router = _kelvin_cluster()
    gate = router._pad_access_gate()
    assert gate is not None

    candidate = _comp_route()
    gate.veto_for(candidate)
    cached_keys = set(gate._before)
    assert cached_keys, "the first check should have cached at least one verdict"

    far = Route(
        net=FOREIGN_NET,
        net_name="FAR",
        segments=[
            Segment(
                x1=18.0,
                y1=1.0,
                x2=19.0,
                y2=1.0,
                width=0.2,
                layer=Layer.F_CU,
                net=FOREIGN_NET,
                net_name="FAR",
            )
        ],
    )
    router._mark_route(far)
    before_second = gate.evaluations
    gate.veto_for(candidate)

    # Every cached verdict survived the distant commit, so the second check
    # paid only for its speculative "after" evaluations -- at most one per
    # terminal it reached, never two.
    assert cached_keys <= set(gate._before)
    assert gate.evaluations - before_second <= len(cached_keys)


def test_nearby_copper_does_invalidate_the_cached_before_verdict():
    """The counterpart: copper inside a terminal's box must drop its verdict."""
    router = _kelvin_cluster()
    gate = router._pad_access_gate()
    assert gate is not None

    gate.veto_for(_comp_route())
    assert ("U3", "1") in gate._before

    near = Route(
        net=FOREIGN_NET,
        net_name="NEAR",
        segments=[
            Segment(
                x1=4.0,
                y1=COMP_Y,
                x2=6.0,
                y2=COMP_Y,
                width=0.2,
                layer=Layer.F_CU,
                net=FOREIGN_NET,
                net_name="NEAR",
            )
        ],
    )
    router._mark_route(near)
    gate._sync_cache()
    assert ("U3", "1") not in gate._before


def test_truncated_journal_disables_the_cache_rather_than_serving_it_stale():
    """A journal at its record cap stops appending, so it stops being a feed."""
    router = _kelvin_cluster()
    gate = router._pad_access_gate()
    assert gate is not None

    gate.veto_for(_comp_route())
    assert gate._before

    router._commit_journal.truncated = True
    gate._sync_cache()

    assert gate._before == {}
    assert gate._cache_disabled is True

    # And nothing is cached from here on -- every verdict is recomputed.
    gate.veto_for(_comp_route())
    assert gate._before == {}


def test_fine_prefilter_skips_terminals_the_envelope_only_flew_past():
    """A long L-shaped route's envelope over-selects; its copper must not.

    The whole-route bbox of an L covers the empty quadrant inside it.  A
    terminal sitting there is reachable only by the envelope, never by the
    copper, so the second prefilter stage has to drop it -- that is the
    difference between one access evaluation and none.
    """
    router = _kelvin_cluster()
    gate = router._pad_access_gate()
    assert gate is not None

    # An L hugging the board's west and south edges.  U3 (5, 15) sits inside
    # its bbox but far from both arms.
    elbow = Route(
        net=COMP_NET,
        net_name="COMP",
        segments=[
            Segment(
                x1=0.5,
                y1=0.5,
                x2=0.5,
                y2=19.5,
                width=0.2,
                layer=Layer.F_CU,
                net=COMP_NET,
                net_name="COMP",
            ),
            Segment(
                x1=0.5,
                y1=19.5,
                x2=19.5,
                y2=19.5,
                width=0.2,
                layer=Layer.F_CU,
                net=COMP_NET,
                net_name="COMP",
            ),
        ],
    )
    assert gate.veto_for(elbow) is None
    assert gate.coarse_hits > gate.fine_hits, "the envelope must over-select here"
    assert gate.evaluations <= gate.fine_hits * 2


def test_exhausted_evaluation_budget_fails_open_and_says_so():
    router = _kelvin_cluster()
    gate = PadAccessInvariant(router, max_evaluations=0)
    router._pad_access_invariant = gate

    assert gate.veto_for(_comp_route()) is None
    assert gate.truncated is True


def test_protected_pad_cap_is_deterministic_and_flagged():
    router = _kelvin_cluster()
    gate = PadAccessInvariant(router, max_protected_pads=2)
    gate.arm()

    assert gate.truncated is True
    # Sorted (ref, pin) order, so the truncation is reproducible.
    assert sorted(gate.protected) == [("Q1", "1"), ("R10", "1")]


# ---------------------------------------------------------------------------
# Soundness of the early-exit existence test
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("comp_x2", [0.5, 3.0, 5.0, 6.5, 9.0])
def test_has_access_agrees_with_the_full_access_set(comp_x2):
    """``has_access`` is the hot path; it must never disagree with Phase 1a.

    Walks a competing track across the cluster so the sweep covers both
    verdicts for several terminals -- including the x2 that strands U3.1,
    which is exactly where a cheap test disagreeing with the expensive one
    would turn into a wrong veto.
    """
    router = _kelvin_cluster()
    track = Route(
        net=COMP_NET,
        net_name="COMP",
        segments=[
            Segment(
                x1=COMP_X1,
                y1=COMP_Y,
                x2=comp_x2,
                y2=COMP_Y,
                width=0.2,
                layer=Layer.F_CU,
                net=COMP_NET,
                net_name="COMP",
            )
        ],
    )
    router._mark_route(track)

    for key, pad in sorted(router.pads.items()):
        full = compute_access_set(pad, router.grid, router.rules)
        cheap = has_access(pad, router.grid, router.rules)
        assert cheap is (not full.is_empty()), f"{key} disagreed at x2={comp_x2}"


# ---------------------------------------------------------------------------
# Soundness of the prefilter
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "width,height,rotation,shape",
    [
        (1.0, 1.0, 0.0, "rect"),
        (0.3, 1.8, 0.0, "rect"),
        (1.8, 0.3, 37.0, "rect"),
        (0.9, 0.9, 0.0, "circle"),
        (2.0, 0.4, 90.0, "rect"),
    ],
)
def test_conservative_bbox_contains_the_real_access_bbox(width, height, rotation, shape):
    """The prefilter box must never be smaller than the box it stands in for.

    :func:`~kicad_tools.router.pad_access.affected_pads`' soundness guarantee is
    what lets the gate skip a terminal without evaluating it, and that guarantee
    only transfers to :func:`conservative_access_bbox` if the cheap box
    *contains* the real one -- for every pad shape, rotation and fab tier.
    """
    router = Autorouter(
        20,
        20,
        rules=_rules(),
        force_python=True,
        physics_enabled=False,
    )
    _add_pad(
        router,
        "P1",
        "1",
        8.0,
        8.0,
        ISENSE_NET,
        "ISENSE_A+",
        width=width,
        height=height,
        rotation=rotation,
        shape=shape,
    )
    pad = router.pads[("P1", "1")]
    real = compute_access_set(pad, router.grid, router.rules).bbox
    cheap = conservative_access_bbox(pad, router.rules, resolution=router.grid.resolution)
    assert cheap[0] <= real[0], (cheap, real)
    assert cheap[1] <= real[1], (cheap, real)
    assert cheap[2] >= real[2], (cheap, real)
    assert cheap[3] >= real[3], (cheap, real)


# ---------------------------------------------------------------------------
# End to end: the epic's own success criterion, on a real negotiated run
# ---------------------------------------------------------------------------


def _routable_kelvin(*, invariant: bool) -> Autorouter:
    """The Phase 1b routed fixture: the cluster plus a 2-pad COMP net.

    Verbatim geometry from ``tests/router/test_access_witness_replay_5508.py``'s
    ``_kelvin_fixture``, whose COMP span (3.0 -> 7.0) makes the negotiated loop
    route COMP FIRST -- the commit that historically closed U3.1's last exit.
    """
    router = _kelvin_cluster()
    _add_pad(router, "R20", "1", 3.0, COMP_Y, COMP_NET, "COMP")
    _add_pad(router, "R21", "1", 7.0, COMP_Y, COMP_NET, "COMP")
    router.enable_pad_access_invariant = invariant
    router.route_all_negotiated(max_iterations=2, timeout=60, per_net_timeout=10)
    return router


@pytest.mark.slow
def test_negotiated_run_no_longer_strands_u3_with_the_invariant_on():
    """Epic #5508 success criterion 1, end to end.

    "A regression test with a synthetic 4-pad Kelvin cluster and one competing
    net proves the competing net is refused rather than stranding the sense
    pad."  The control run (invariant off) is the pre-#5891 behaviour: COMP
    commits first and U3.1 ends with an empty access set.
    """
    control = _routable_kelvin(invariant=False)
    assert compute_access_set(control.pads[("U3", "1")], control.grid, control.rules).is_empty(), (
        "control run must reproduce the stranding this rule prevents"
    )
    assert control.pad_access_invariant is None

    guarded = _routable_kelvin(invariant=True)
    assert not compute_access_set(guarded.pads[("U3", "1")], guarded.grid, guarded.rules).is_empty()

    gate = guarded.pad_access_invariant
    assert gate is not None
    assert gate.vetoed_pads() == (("U3", "1"),)
    assert {veto.candidate_net_name for veto in gate.vetoes} == {"COMP"}

    # ...and the refusal costs no reach: the competing net still lands, because
    # a refused connection is a failed connection, which the negotiated loop's
    # existing targeted rip-up retries.  This is the epic's success criterion in
    # full -- "COMP's candidate is refused and both nets connect".
    def _reached(router: Autorouter) -> set[int]:
        return {route.net for route in router.routes if route.segments or route.vias}

    assert _reached(guarded) == _reached(control)
    assert COMP_NET in _reached(guarded)
    assert ISENSE_NET in _reached(guarded)
    assert not guarded.get_failed_nets()


def test_speculative_evaluation_leaves_the_grid_untouched():
    router = _kelvin_cluster()
    gate = router._pad_access_gate()
    assert gate is not None
    routes_before = list(router.grid.routes)
    cells_before = _marked_cell_count(router)

    gate.veto_for(_comp_route())

    assert list(router.grid.routes) == routes_before
    assert _marked_cell_count(router) == cells_before
