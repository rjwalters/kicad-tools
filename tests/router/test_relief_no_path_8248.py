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


def test_freed_width_and_shortfall_are_measured_in_mm():
    """Issue #8256: no more ``freed width unknown`` once ripped copper is in hand."""
    router = _sealed_router()
    line = router._relief_corridor_need(1, {1: [_seg(0, 0, 5, 0, width=0.1)]}, {})
    assert "unknown" not in line, line
    need = router.rules.trace_width + 2 * router.rules.trace_clearance
    assert "freed 0.100 mm" in line, line
    assert f"shortfall {need - 0.1:.3f} mm" in line, line
    assert "not re-occupied" in line, line


def test_nothing_ripped_and_unsupplied_are_stated_not_unknown():
    router = _sealed_router()
    assert "nothing ripped" in router._relief_corridor_need(1, {1: []}, {})
    line = router._relief_corridor_need(1)
    assert "not measured (no ripped copper supplied)" in line, line


def test_reoccupation_names_the_net_in_the_corridor():
    router = _sealed_router()
    freed = {1: [_seg(0, 0, 5, 0)]}
    near = {7: [_seg(0, 0.1, 5, 0.1, net=7)]}
    far = {7: [_seg(0, 5, 5, 5, net=7)]}
    assert "re-occupied by net 7 before retry" in router._relief_corridor_need(1, freed, near)
    assert "not re-occupied" in router._relief_corridor_need(1, freed, far)
