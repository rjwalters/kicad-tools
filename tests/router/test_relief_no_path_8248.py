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
