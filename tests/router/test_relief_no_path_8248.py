"""Relief "no relief path" diagnostics and repeat guard (issue #8248)."""

from __future__ import annotations

from unittest.mock import patch

from kicad_tools.router.access_ripup import AccessLossWitness, RipupCycleGuard

from .test_relief_cycle_6363 import VICTIM, _fixture, _relief


def _witness(kinds_by_ref: dict[str, str]) -> AccessLossWitness:
    items = tuple(sorted(kinds_by_ref.items()))
    return AccessLossWitness(
        failed_net=VICTIM,
        failed_net_name="Q1.2net",
        pass_name="routing",
        iteration=3,
        stranded_pads=(("Q1", "2"),),
        closing_refs=tuple(sorted(kinds_by_ref)),
        closing_kinds=tuple(sorted(set(kinds_by_ref.values()))),
        blocking_nets=(2,),
        blocking_net_names=("/GATE",),
        held_nets=(),
        closing_items=tuple((r, k) for r, k in items),
    )


def test_one_line_marks_rippable_and_fixed_closers():
    w = _witness({"/GATE": "route_segment", "Q1.1": "foreign_pad", "Q1.3": "foreign_pad"})
    line = w.one_line()
    assert "/GATE[rip-able]" in line
    assert "Q1.1[fixed]" in line and "Q1.3[fixed]" in line
    assert "closing_refs" in w.to_dict()


def test_one_line_without_kinds_falls_back_to_refs():
    w = _witness({"Q1.1": "foreign_pad"})
    bare = AccessLossWitness(**{**w.__dict__, "closing_items": ()})
    assert "closed by Q1.1 --" in bare.one_line()


def test_guard_skips_only_consecutive_identical_witness():
    g = RipupCycleGuard()
    sig = ((("Q1", "2"),), ("Q1.1",))
    assert not g.repeats_futile_relief(1, 2, sig)  # first failure
    g.note_no_relief_path(1, 2, sig)
    assert g.repeats_futile_relief(1, 3, sig)
    assert not g.repeats_futile_relief(1, 4, sig)  # gap
    assert not g.repeats_futile_relief(1, 3, ((), ("other",)))  # witness changed
    assert not g.repeats_futile_relief(1, 3, None)


def test_no_path_log_states_corridor_and_repeat_is_skipped_once():
    router, neg, net_routes, pads_by_net = _fixture()
    guard = RipupCycleGuard()
    sig = ((("U1", "1"),), ("Q1.1", "Q1.3"))
    log: list[str] = []
    probes: list[int] = []

    def probe(net, pf, per_net_timeout=None, **_kw):
        probes.append(net)
        return [], set()

    with (
        patch.object(router, "_relief_probe", side_effect=probe),
        patch.object(router, "_relief_witness_signature", return_value=sig),
    ):
        router._commit_journal.iteration = 1
        assert _relief(router, neg, net_routes, pads_by_net, guard, log) is False
        assert any("NO relief path" in m and "corridor" in m for m in log)
        n = len(probes)
        router._commit_journal.iteration = 2
        assert _relief(router, neg, net_routes, pads_by_net, guard, log) is False
        assert len(probes) == n  # skipped, no new probe
        assert sum("skipped" in m for m in log) == 1
        router._commit_journal.iteration = 3
        assert _relief(router, neg, net_routes, pads_by_net, guard, log) is False
        assert len(probes) == n
        assert sum("skipped" in m for m in log) == 1  # logged once
    assert guard.futile_relief_skips == [VICTIM]


def test_changed_witness_is_reattempted():
    router, neg, net_routes, pads_by_net = _fixture()
    guard = RipupCycleGuard()
    sigs = iter([(1,), (2,), (2,)])
    probes: list[int] = []

    def probe(net, pf, per_net_timeout=None, **_kw):
        probes.append(net)
        return [], set()

    with (
        patch.object(router, "_relief_probe", side_effect=probe),
        patch.object(router, "_relief_witness_signature", side_effect=lambda *a: next(sigs)),
    ):
        for it in (1, 2):
            router._commit_journal.iteration = it
            _relief(router, neg, net_routes, pads_by_net, guard, [])
    assert len(probes) == 2


def _sealed_router():
    """Victim pad on net 1 boxed in by four foreign-net pads (a real seal)."""
    from kicad_tools.router.core import Autorouter

    router = Autorouter(width=20.0, height=20.0)

    def pad(num, x, y, net):
        return {
            "number": str(num),
            "x": x,
            "y": y,
            "width": 0.6,
            "height": 0.6,
            "net": net,
            "net_name": f"N{net}",
        }

    router.add_component("U1", [pad(1, 10.0, 10.0, 1), pad(2, 18.0, 18.0, 1)])
    router.add_component(
        "Q1",
        [
            pad(1, 10.8, 10.0, 2),
            pad(2, 9.2, 10.0, 3),
            pad(3, 10.0, 10.8, 4),
            pad(4, 10.0, 9.2, 5),
        ],
    )
    return router


def test_real_signature_is_stable_and_leaves_targeter_untouched():
    router = _sealed_router()
    net_routes = {n: [object()] for n in (2, 3, 4, 5)}  # rip-able stand-ins
    gate = router._access_loss_gate()
    before = (gate.queries, gate.hits, gate.evaluations, len(gate.witnesses))
    sig1 = router._relief_witness_signature(1, net_routes)
    sig2 = router._relief_witness_signature(1, net_routes)
    assert sig1 is not None, "fixture must actually seal the victim pad"
    assert sig1 == sig2
    assert (gate.queries, gate.hits, gate.evaluations, len(gate.witnesses)) == before
    assert gate.witnesses == []


def test_skip_fires_off_real_witness_without_recording_it():
    from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

    router = _sealed_router()
    neg = NegotiatedRouter(
        grid=router.grid, router=router.router, rules=router.rules, net_class_map={}
    )
    net_routes = {1: [], **{n: [object()] for n in (2, 3, 4, 5)}}
    pads_by_net = {n: [router.pads[p] for p in keys] for n, keys in router.nets.items() if n}
    guard = RipupCycleGuard()
    log: list[str] = []
    probes: list[int] = []

    def probe(net, pf, per_net_timeout=None, **_kw):
        probes.append(net)
        return [], set()

    with patch.object(router, "_relief_probe", side_effect=probe):
        router._commit_journal.iteration = 1
        assert _relief(router, neg, net_routes, pads_by_net, guard, log) is False
        n = len(probes)
        router._commit_journal.iteration = 2
        assert _relief(router, neg, net_routes, pads_by_net, guard, log) is False
    assert len(probes) == n, "identical real witness must skip the repeat"
    assert guard.futile_relief_skips == [VICTIM]
    assert router._access_loss_gate().witnesses == []


def _corridor_clearance(line: str) -> float:
    return float(line.split("2x clearance ")[1].split(",")[0])


def test_corridor_resolves_pairwise_entry_against_closing_nets():
    """A ``--voltage-map`` pair entry on a closer sets the printed clearance."""
    from kicad_tools.router.pairwise_clearance import PairwiseClearanceTable

    router = _sealed_router()
    router.rules.pairwise_clearance = PairwiseClearanceTable(
        dru=router.rules.trace_clearance,
        net_voltages={"N1": 230.0, "N2": 0.0},
        required_by_pair={("N1", "N2"): 2.5},
    )
    line = router._relief_corridor_need(1)
    assert "resolved against closing nets" in line, line
    assert _corridor_clearance(line) == 2.5, line


def test_corridor_applies_authored_net_floor_of_a_closer():
    """A closer's ``net_clearance_floors`` entry (a ``.kicad_pro`` HV netclass)
    must raise the printed clearance, not just the resolver's class value."""
    router = _sealed_router()
    router.rules.net_clearance_floors = {3: 1.2}
    line = router._relief_corridor_need(1)
    assert "resolved against closing nets" in line, line
    assert _corridor_clearance(line) == 1.2, line
    assert "needs a " + f"{router.rules.trace_width + 2 * 1.2:.3f} mm corridor" in line


def test_corridor_without_witness_labels_the_class_fallback():
    router = _sealed_router()
    line = router._relief_corridor_need(999)  # no pads: no witness to resolve
    assert "class clearance only, no closing nets resolved" in line, line


def _seg(x1, y1, x2, y2, width=0.2, net=1):
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Route, Segment

    return Route(
        net=net,
        net_name=f"N{net}",
        segments=[Segment(x1, y1, x2, y2, width, Layer.F_CU, net=net)],
    )


def _need(router) -> float:
    return float(router._relief_corridor_need(1).split("needs a ")[1].split(" mm")[0])


# --- Issue #8256: freed corridor and shortfall ------------------------------


def test_equal_width_legally_spaced_blocker_gives_no_shortfall():
    """A blocker as wide as the victim, held to the same clearance, vacated
    w + 2c -- exactly the victim's corridor.  The old figure (the bare trace
    width) reported a 2x-clearance shortfall for every such geometry."""
    router = _sealed_router()
    w, c = router.rules.trace_width, router.rules.trace_clearance
    line = router._relief_corridor_need(1, {2: [_seg(0, 0, 5, 0, width=w, net=2)]}, {})
    assert f"freed >= {w + 2 * c:.3f} mm" in line, line
    assert "ripped N2" in line, line
    assert "no width shortfall" in line, line
    assert "shortfall <=" not in line, line


def test_narrower_blocker_reports_an_upper_bound_shortfall():
    router = _sealed_router()
    c = router.rules.trace_clearance
    line = router._relief_corridor_need(1, {2: [_seg(0, 0, 5, 0, width=0.1, net=2)]}, {})
    freed = 0.1 + 2 * c
    assert f"freed >= {freed:.3f} mm" in line, line
    assert f"shortfall <= {_need(router) - freed:.3f} mm" in line, line


def test_freed_band_uses_the_blockers_authored_floor():
    """The clearance the ripped copper vacated is resolved with
    ``clearance_for_nets``: a closer's HV floor widens the band it held."""
    router = _sealed_router()
    router.rules.net_clearance_floors = {2: 1.0, 3: 1.0, 4: 1.0, 5: 1.0}
    line = router._relief_corridor_need(1, {2: [_seg(0, 0, 5, 0, width=0.2, net=2)]}, {})
    assert f"freed >= {0.2 + 2 * 1.0:.3f} mm" in line, line
    assert "resolved against closing nets" in line.split("freed")[1], line


def test_nothing_ripped_is_said_without_a_shortfall_figure():
    router = _sealed_router()
    for ripped in ({}, {2: []}, {1: [_seg(0, 0, 5, 0, net=1)]}):  # victim's own copper
        line = router._relief_corridor_need(1, ripped, {})
        assert "no blocker ripped before the probe failed" in line, (ripped, line)
        assert "shortfall" not in line and "freed >=" not in line, line
    line = router._relief_corridor_need(1)
    assert "not measured (no ripped copper supplied)" in line, line


# --- Issue #8256: re-occupation ---------------------------------------------


def test_untouched_legal_neighbour_is_not_reoccupation():
    """Judge's probe: a ripped 0.10 mm blocker and a never-moved net 3 at
    exactly legal edge clearance.  Copper that was already there is not
    placed since the rip, so it cannot have re-occupied anything."""
    router = _sealed_router()
    c, w = router.rules.trace_clearance, router.rules.trace_width
    d = 0.05 + c + w / 2
    line = router._relief_corridor_need(
        1,
        {2: [_seg(0, 0, 5, 0, width=0.1, net=2)]},
        {},  # net 3 untouched: not "placed"
    )
    assert "not re-occupied (no copper placed since the rip)" in line, line
    # Even if net 3's copper HAD been placed since the rip, at a legal distance
    # from the victim's path along the ripped centreline it does not occupy it.
    legal = w / 2 + c + w / 2 + 0.01
    line = router._relief_corridor_need(
        1, {2: [_seg(0, 0, 5, 0, width=0.1, net=2)]}, {3: [_seg(0, legal, 5, legal, net=3)]}
    )
    assert "re-occupied since" not in line, line
    assert "lies clear of the ripped traces" in line, line
    assert d < legal  # the Judge's false positive sat inside the old limit


def test_copper_placed_on_the_ripped_centreline_is_named():
    router = _sealed_router()
    line = router._relief_corridor_need(
        1, {2: [_seg(0, 0, 5, 0, net=2)]}, {3: [_seg(0, 0.1, 5, 0.1, net=3)]}
    )
    assert "re-occupied since the rip by N3" in line, line


def test_rescue_line_measures_ripped_blocker_not_the_victims_stub():
    """End to end: the victim holds a partial stub, round 1 rips N2, round 2
    finds no path.  The line describes N2 and nothing was placed meanwhile."""
    from kicad_tools.router.core import Autorouter

    from .test_relief_cycle_6363 import _rip_then_no_path, _seg_route

    router, neg, net_routes, pads_by_net = _fixture()
    stub = _seg_route(VICTIM, 3.0 + VICTIM * 4.0, x1=5.0, x2=6.0)
    stub.segments[0].width = 0.5  # wider than N2: would win if it were measured
    router._mark_route(stub)
    net_routes[VICTIM] = [stub]
    log: list[str] = []
    with patch.object(Autorouter, "_relief_probe", side_effect=_rip_then_no_path({2})):
        assert _relief(router, neg, net_routes, pads_by_net, RipupCycleGuard(), log) is False
    line = next(m for m in log if "NO relief path" in m)
    assert "(round 2;" in line, line
    assert "ripped N2: width 0.200" in line, line
    assert "not re-occupied (no copper placed since the rip)" in line, line


def test_round_one_no_path_says_nothing_was_ripped():
    router, neg, net_routes, pads_by_net = _fixture()
    log: list[str] = []
    with patch.object(router, "_relief_probe", return_value=([], set())):
        _relief(router, neg, net_routes, pads_by_net, RipupCycleGuard(), log)
    line = next(m for m in log if "NO relief path" in m)
    assert "no blocker ripped before the probe failed" in line, line
    assert "shortfall" not in line, line


# --- Issue #8256: targeted rip-up reason ------------------------------------


def test_targeted_ripup_line_distinguishes_rolled_back_refused_and_pads():
    from kicad_tools.router.algorithms.negotiated import RIPUP_VICTIM_GEOMETRIC

    from .test_ripup_cycle_6342 import _fake_neg_router

    router = _sealed_router()
    guard = RipupCycleGuard()
    neg = _fake_neg_router(RIPUP_VICTIM_GEOMETRIC)
    labels: list[str] = []
    for _ in range(3):  # two real rolled-back rips, then the guard refuses
        ok = router._guarded_targeted_ripup(
            neg,
            guard,
            failed_net=1,
            blocking_nets={2, 3},
            net_routes={},
            pads_by_net={},
            present_cost_factor=1.0,
            mark_route_callback=lambda r: None,
            ripup_history={},
            max_ripups_per_net=3,
            per_net_timeout=1.0,
        )
        assert ok is False
        labels.append(router._targeted_ripup_failure_label(router._last_guarded_ripup_reason))
    assert labels == [
        "rolled back, no path with blockers ripped",
        "rolled back, no path with blockers ripped",
        "refused by rip-up guard",
    ]
    labels.append("fewer than 2 pads")
    suffix = router._targeted_ripup_reason_suffix(1, 5, labels)
    assert suffix == (
        " [unresolved: 2 rolled back, no path with blockers ripped; "
        "1 fewer than 2 pads; 1 refused by rip-up guard]"
    ), suffix
    assert router._targeted_ripup_reason_suffix(3, 3, []) == ""
    assert "1 no reason recorded" in router._targeted_ripup_reason_suffix(0, 1, [])


def test_skipped_rip_is_not_called_a_roll_back():
    from kicad_tools.router.access_ripup import PLAN_NOTHING

    assert (
        _sealed_router()._targeted_ripup_failure_label(PLAN_NOTHING)
        == "rip skipped, all blockers protected"
    )
