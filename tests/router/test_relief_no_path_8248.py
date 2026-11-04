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
    assert "blocker's own authored floor" in line.split("freed")[1], line


def test_freed_band_ignores_the_closers_floors_when_the_blocker_has_none():
    """Judge's probe F: an HV victim sealed by HV closers, but the ripped
    blocker N2 is LV.  N2 may legally have sat 0.2 mm from an LV non-closer,
    so the closers' 1.0 mm floor must not widen the band it vacated."""
    router = _sealed_router()
    router.rules.net_clearance_floors = {1: 1.0, 3: 1.0, 4: 1.0, 5: 1.0}
    c = router.rules.trace_clearance
    assert router.rules.clearance_for_nets(2, 6, c) == c  # the legal LV neighbour
    line = router._relief_corridor_need(1, {2: [_seg(0, 0, 5, 0, width=0.2, net=2)]}, {})
    freed = 0.2 + 2 * c
    assert f"freed >= {freed:.3f} mm" in line, line
    assert f"shortfall <= {_need(router) - freed:.3f} mm" in line, line
    assert "no width shortfall" not in line, line


def test_freed_band_uses_the_narrowest_ripped_segment():
    """Judge's probe G: N2 is 0.10 mm where it passes the seal and 0.50 mm on
    a far-away run.  The bound must hold at the seal, so the narrow segment
    sets it -- the wide run must not produce a false "no width shortfall"."""
    router = _sealed_router()
    c = router.rules.trace_clearance
    blocker = _seg(0, 0, 1, 0, width=0.1, net=2)
    blocker.segments += _seg(30, 30, 40, 30, width=0.5, net=2).segments
    line = router._relief_corridor_need(1, {2: [blocker]}, {})
    freed = 0.1 + 2 * c
    assert f"freed >= {freed:.3f} mm" in line, line
    assert "narrowest width 0.100" in line, line
    assert f"shortfall <= {_need(router) - freed:.3f} mm" in line, line
    assert "no width shortfall" not in line, line


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
    assert "ripped N2: narrowest width 0.200" in line, line
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


# --- Issue #8256: every clearance source the blocker's neighbours honour -----


def _diff_pair_blocker_router(intra=0.1):
    """N2 is one leg of a declared diff pair with N6 (class gap ``intra``)."""
    from kicad_tools.router.rules import NetClassRouting

    router = _sealed_router()
    router.add_component(
        "J1",
        [
            {
                "number": "1",
                "x": 2.0,
                "y": 2.0,
                "width": 0.6,
                "height": 0.6,
                "net": 6,
                "net_name": "N6",
            }
        ],
    )
    for net, partner in (("N2", "N6"), ("N6", "N2")):
        router.net_class_map[net] = NetClassRouting(
            name="DP",
            trace_width=0.2,
            clearance=0.2,
            intra_pair_clearance=intra,
            diffpair_partner=partner,
        )
    assert router._diff_pair_partner_net(2) == 6
    return router


def test_freed_band_honours_an_unripped_diff_pair_partners_gap():
    """Judge's probe H: ripped N2's partner N6 stayed put and may legally sit
    at the 0.1 mm intra-pair gap, so the 0.2 mm class clearance does not bound
    the vacated band and "no width shortfall" would be a false fit."""
    router = _diff_pair_blocker_router(intra=0.1)
    line = router._relief_corridor_need(1, {2: [_seg(0, 0, 5, 0, width=0.2, net=2)]}, {})
    freed = 0.2 + 2 * 0.1
    assert f"freed >= {freed:.3f} mm" in line, line
    assert "diff-pair gap to unripped partner N6" in line, line
    assert f"shortfall <= {_need(router) - freed:.3f} mm" in line, line
    assert "no width shortfall" not in line, line


def test_ripped_partner_does_not_lower_the_band():
    """Both legs ripped: the partner's copper is gone, so the gap no longer binds."""
    router = _diff_pair_blocker_router(intra=0.1)
    line = router._relief_corridor_need(
        1,
        {2: [_seg(0, 0, 5, 0, width=0.2, net=2)], 6: [_seg(0, 0.3, 5, 0.3, width=0.2, net=6)]},
        {},
    )
    assert "diff-pair gap" not in line, line
    assert "freed >= 0.600 mm" in line, line


def test_classless_diff_pair_gap_is_not_measured():
    """A detected pair with no class on either leg has no resolvable gap."""
    router = _diff_pair_blocker_router()
    router.net_class_map.clear()
    with patch.object(type(router), "_diff_pair_partner_net", return_value=6):
        line = router._relief_corridor_need(1, {2: [_seg(0, 0, 5, 0, width=0.2, net=2)]}, {})
    assert "freed corridor not measured (diff-pair gap of N2/N6 not resolved" in line, line
    assert "freed >=" not in line and "shortfall" not in line, line


def test_fixed_fill_uses_the_blockers_unraised_class_clearance():
    """Judge's probe J: the fixed-fill check holds a trace to its CLASS
    clearance, not raised to the board's, so a 0.1 mm class lets the blocker
    sit 0.1 mm from a fill on its layer."""
    from shapely.geometry import box

    from kicad_tools.router.fixed_copper import FixedFill, FixedFillObstacles
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.rules import NetClassRouting

    router = _sealed_router()
    router.net_class_map["N2"] = NetClassRouting(name="TIGHT", trace_width=0.2, clearance=0.1)
    blocker = {2: [_seg(0, 0, 5, 0, width=0.2, net=2)]}
    # No fill on the board: the class clearance is never consulted.
    assert "freed >= 0.600 mm" in router._relief_corridor_need(1, blocker, {})
    layer = router.grid.layer_to_index(Layer.F_CU.value)
    router.grid.fixed_fills = FixedFillObstacles(
        (FixedFill("N9", 9, layer, 0.0, box(15.0, 0.0, 19.0, 4.0)),)
    )
    line = router._relief_corridor_need(1, blocker, {})
    assert "freed >= 0.400 mm" in line, line
    assert "fixed-fill clearance" in line, line
    assert "no width shortfall" not in line, line


def test_foreign_pad_closer_than_the_rule_minimum_is_measured():
    """A pad carve-out / fine-pitch clearance can put a foreign pad nearer the
    blocker than any net rule; pads never move, so the real gap is used."""
    router = _sealed_router()
    router.add_component(
        "U9",
        [
            {
                "number": "7",
                "x": 2.0,
                "y": 0.45,
                "width": 0.4,
                "height": 0.4,
                "net": 8,
                "net_name": "N8",
            }
        ],
    )
    # Pad edge at y=0.25, blocker edge at y=0.1: a 0.15 mm gap.
    line = router._relief_corridor_need(1, {2: [_seg(0, 0, 5, 0, width=0.2, net=2)]}, {})
    assert "freed >= 0.500 mm" in line, line
    assert "measured gap to pad U9.7 (N8)" in line, line
    assert "no width shortfall" not in line, line


def test_unripped_copper_sharing_the_blockers_clearance_is_measured():
    """Negotiated sharing can leave a foreign trace inside the blocker's
    clearance; the band it vacated is bounded by that real gap."""
    router = _sealed_router()
    neighbour = _seg(0, 0.35, 5, 0.35, width=0.2, net=7)  # 0.15 mm edge gap
    router.routes.append(neighbour)
    line = router._relief_corridor_need(1, {2: [_seg(0, 0, 5, 0, width=0.2, net=2)]}, {})
    assert "freed >= 0.500 mm" in line, line
    assert "measured gap to Net_7 copper" in line, line
    # The same copper, placed since the rip, is re-occupation -- not the band.
    line = router._relief_corridor_need(
        1, {2: [_seg(0, 0, 5, 0, width=0.2, net=2)]}, {7: [neighbour]}
    )
    assert "freed >= 0.600 mm" in line, line
    assert "re-occupied since the rip by Net_7" in line, line


# --- Issue #8288: end-to-end outcome on the sealed centre pin ----------------


def _real_rescue(router, trace_net, trace_y):
    """Run the real (unpatched) rescue for victim 1 with one rippable trace."""
    from kicad_tools.router.algorithms.negotiated import NegotiatedRouter

    router.add_component(
        "J1",
        [
            {
                "number": str(i),
                "x": x,
                "y": trace_y,
                "width": 0.6,
                "height": 0.6,
                "net": trace_net,
                "net_name": f"N{trace_net}",
            }
            for i, x in ((1, 2.0), (2, 18.0))
        ],
    )
    blocker = _seg(2.0, trace_y, 18.0, trace_y, width=0.2, net=trace_net)
    router._mark_route(blocker)
    router.grid.mark_route_usage(blocker)
    router.routes.append(blocker)
    neg = NegotiatedRouter(
        grid=router.grid, router=router.router, rules=router.rules, net_class_map={}
    )
    net_routes = {1: [], trace_net: [blocker]}
    pads_by_net = {n: [router.pads[p] for p in keys] for n, keys in router.nets.items() if n}
    log: list[str] = []
    ok = router._relief_rescue(
        failed_net=1,
        neg_router=neg,
        net_routes=net_routes,
        pads_by_net=pads_by_net,
        present_factor=1.0,
        per_net_timeout=5.0,
        flush_print_fn=log.append,
        elapsed_fn=lambda: "0s",
        guard=RipupCycleGuard(),
    )
    return ok, log, net_routes


def test_sealed_by_fixed_pads_reports_corridor_and_nothing_to_rip():
    """Four foreign pads close the centre pin on every side.  Pad metal never
    moves, so ripping the foreign trace cannot open a path: the rescue rolls
    back, the printed corridor need is the terminal reason, and the trace is
    restored verbatim."""
    router = _sealed_router()
    ok, log, net_routes = _real_rescue(router, trace_net=6, trace_y=9.5)
    assert ok is False
    line = next(m for m in log if "NO relief path" in m)
    assert "needs a 0.600 mm corridor" in line, line
    assert "no blocker ripped before the probe failed" in line, line
    assert len(net_routes[6]) == 1 and not net_routes[1]  # rolled back


def test_open_side_victim_routes_once_the_blocker_is_ripped():
    """With one side of the centre pin open, the rescue rips the blocker and
    the victim routes (the other branch of the #8288 outcome)."""
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
    router.add_component("Q1", [pad(1, 10.8, 10.0, 3), pad(2, 9.2, 10.0, 4), pad(3, 10.0, 10.8, 5)])
    ok, log, net_routes = _real_rescue(router, trace_net=2, trace_y=8.8)
    assert ok is True, log
    assert net_routes[1], log
    assert any("ROUTED via conflict-free relief path" in m for m in log), log
