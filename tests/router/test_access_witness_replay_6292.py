"""Issue #6292: a cheaper access-witness replay with identical verdicts.

Board 05's replay took 143-267 s of its 900 s ``kct route --timeout`` budget.
Two changes make it cheaper, and neither may change a verdict:

* the replay asks :func:`compute_access_set` for *presence* only
  (``presence_only=True``): once an exit stub is legal the access set is
  non-empty and its ``bbox`` is already fixed, so the via-site tests -- each a
  walk of every committed segment -- are skipped;
* a record that **adds** copper to a terminal whose access set is already
  empty is not re-evaluated: adding copper can only reject more candidates,
  so the set stays empty, and an empty set's bbox is fixed.

Plus :class:`DefaultAccessLegality` memoizes its foreign-copper inventories for
the one read-only call it lives for.

Every test here compares against :func:`_reference_replay`, a verbatim copy of
the pre-#6292 replay loop (full :func:`compute_access_set` with an
un-memoized adapter, every affected pad re-evaluated on every record).
``evaluations`` is compared separately: dropping it is the point.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import pytest

from kicad_tools.router.access_witness import (
    ACCESS_EMPTY,
    ACCESS_NON_EMPTY,
    CommitJournal,
    _closing_summary,
    _ReplayCopper,
    _state_label,
    replay,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.pad_access import (
    DefaultAccessLegality,
    affected_pads,
    compute_access_set,
    route_envelope,
)
from kicad_tools.router.primitives import Route, Segment, Via
from tests.router.test_access_witness_replay_5508 import (
    COMP_Y,
    U3,
    _kelvin_fixture,
    _routed_kelvin,
    _stranding_kelvin,
    _walled_by_the_board_edge,
)

pytestmark = pytest.mark.timeout(180)


# ---------------------------------------------------------------------------
# The pre-#6292 replay, kept verbatim as the equivalence oracle.
# ---------------------------------------------------------------------------


def _reference_replay(
    journal: CommitJournal, router: Any, pad_keys: Sequence[tuple[str, str]]
) -> dict[str, Any]:
    """The replay loop as it stood before issue #6292, reduced to its verdicts.

    Returns ``{"evaluations": n, "pads": [...]}`` where each pad entry carries
    every verdict field :meth:`PadWitness.to_dict` serializes.
    """
    grid = router.grid
    rules = router.rules
    overrides = getattr(router, "_escape_pad_overrides", {}) or {}
    pads = {key: overrides.get(key, router.pads[key]) for key in pad_keys}
    copper = _ReplayCopper()
    evaluations = 0
    saved = grid.routes
    try:
        grid.routes = copper.routes

        def evaluate(key):
            nonlocal evaluations
            evaluations += 1
            # An un-memoized adapter and the full enumeration: exactly the
            # pre-#6292 behaviour.
            return compute_access_set(
                pads[key], grid, rules, legality=DefaultAccessLegality(grid, rules)
            )

        state = {key: evaluate(key) for key in pad_keys}
        first_closed: dict[tuple[str, str], Any] = {}
        baseline: dict[tuple[str, str], str] = {}
        seen_search = False
        for record in journal.records:
            if not seen_search and record.pass_name not in ("fixed", "escape"):
                baseline = {k: _state_label(a) for k, a in state.items()}
                seen_search = True
            copper.apply(record)
            for key in affected_pads(state, route_envelope(record.route, rules)):
                access = evaluate(key)
                previous = state.get(key)
                state[key] = access
                if (
                    access.is_empty()
                    and key not in first_closed
                    and previous is not None
                    and not previous.is_empty()
                ):
                    first_closed[key] = (record, _closing_summary(access))
        if not seen_search:
            baseline = {k: _state_label(a) for k, a in state.items()}
    finally:
        grid.routes = saved

    out = []
    for key in pad_keys:
        closed = first_closed.get(key)
        final = _state_label(state.get(key))
        attribution = closed[1] if closed is not None else {}
        out.append(
            {
                "ref": key[0],
                "pin": key[1],
                "access_at_escape_end": baseline.get(key),
                "final_access": final,
                "first_closed_at": (
                    {"pass": closed[0].pass_name, "iteration": closed[0].iteration}
                    if closed is not None
                    else None
                ),
                "first_closed_index": closed[0].index if closed is not None else None,
                "first_closed_kind": closed[0].kind if closed is not None else None,
                "closing_nets": list(attribution.get("closing_nets", ())),
                "closing_refs": list(attribution.get("closing_refs", ())),
                "closing_copper_class": list(attribution.get("closing_copper_class", ())),
                "closing_markings": list(attribution.get("closing_markings", ())),
                "reopened": closed is not None and final == ACCESS_NON_EMPTY,
            }
        )
    return {"evaluations": evaluations, "pads": out}


_VERDICT_FIELDS = (
    "ref",
    "pin",
    "access_at_escape_end",
    "final_access",
    "first_closed_at",
    "first_closed_index",
    "first_closed_kind",
    "closing_nets",
    "closing_refs",
    "closing_copper_class",
    "closing_markings",
    "reopened",
)


def _verdicts(pads: Sequence[dict[str, Any]]) -> list[dict[str, Any]]:
    return [{field: pad[field] for field in _VERDICT_FIELDS} for pad in pads]


def _assert_identical(router: Any, keys: Sequence[tuple[str, str]]) -> tuple[int, int]:
    """Assert new == reference on every verdict; return both evaluation counts."""
    reference = _reference_replay(router.commit_journal, router, keys)
    witness = replay(router.commit_journal, router, pad_keys=keys)
    assert witness.truncated is False
    assert _verdicts(witness.to_dict()["pads"]) == _verdicts(reference["pads"])
    assert witness.evaluations <= reference["evaluations"]
    return witness.evaluations, reference["evaluations"]


# ---------------------------------------------------------------------------
# A multi-record journal: close, pile on, rip, re-close.
# ---------------------------------------------------------------------------


def _seg(x1, y1, x2, y2, net, name, layer=Layer.F_CU) -> Segment:
    return Segment(x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=layer, net=net, net_name=name)


def _comp_route() -> Route:
    return Route(net=2, net_name="COMP", segments=[_seg(1.0, COMP_Y, 9.0, COMP_Y, 2, "COMP")])


def _multi_record_router():
    """Kelvin cluster with a hand-written journal that exercises every branch.

    1. ``COMP`` closes U3 (the one transition the witness must attribute);
    2. three more commits whose envelopes cover U3 land while it is empty --
       the records the #6292 skip is for;
    3. ``COMP`` is ripped (a removal: always re-evaluated, reopens U3);
    4. a via-bearing commit lands near U3 while it is open again (exercises
       the via-site tests the presence path skips);
    5. ``COMP`` is restored and closes U3 a second time (not re-attributed).
    """
    router = _kelvin_fixture()
    comp = _comp_route()
    journal = router.commit_journal
    with journal.context("initial", 0):
        router.grid.mark_route(comp)
        for y in (13.2, 13.0, 12.8):
            router.grid.mark_route(
                Route(net=9, net_name="OTHER", segments=[_seg(3.5, y, 6.5, y, 9, "OTHER")])
            )
    with journal.context("iteration", 1):
        router.grid.unmark_route(comp)
        router.grid.mark_route(
            Route(
                net=10,
                net_name="VIAS",
                segments=[_seg(7.6, 15.0, 8.4, 15.0, 10, "VIAS")],
                vias=[
                    Via(
                        x=8.4,
                        y=15.0,
                        drill=0.3,
                        diameter=0.6,
                        layers=(Layer.F_CU, Layer.B_CU),
                        net=10,
                        net_name="VIAS",
                    )
                ],
            )
        )
        router.grid.mark_route(comp)
    return router


class TestMultiRecordJournal:
    KEYS = [U3, ("R20", "1"), ("U9", "1"), ("U9", "3"), ("Q1", "1")]

    def test_verdicts_match_the_pre_6292_replay(self):
        router = _multi_record_router()
        _assert_identical(router, self.KEYS)

    def test_the_fixture_exercises_close_skip_reopen_and_reclose(self):
        """Guard against a vacuous fixture: the verdicts must be the rich ones."""
        router = _multi_record_router()
        pad = replay(router.commit_journal, router, pad_keys=[U3]).for_pad(*U3)
        assert pad is not None
        assert pad.first_closed_index == 0
        assert pad.first_closed_at == ("initial", 0)
        assert "COMP" in pad.closing_nets
        assert pad.final_access == ACCESS_EMPTY
        # Closed again by the restore, so not "reopened" -- but the reference
        # agrees on that (checked above); here we only need the rip to have
        # reopened U3 in between, or the skip was never exercised against a
        # removal.
        state_after_rip = _state_after(router, U3, upto=5)
        assert not state_after_rip.is_empty()

    def test_skipping_closed_pads_strictly_lowers_the_evaluation_count(self):
        router = _multi_record_router()
        new, reference = _assert_identical(router, [U3])
        # Records 1-3 add copper to an already-empty U3 and are skipped.
        assert new < reference
        assert reference - new >= 3


def _state_after(router, key, *, upto: int):
    """U3's full access set after the first ``upto`` journal records."""
    copper = _ReplayCopper()
    for record in router.commit_journal.records[:upto]:
        copper.apply(record)
    saved = router.grid.routes
    try:
        router.grid.routes = copper.routes
        return compute_access_set(router.pads[key], router.grid, router.rules)
    finally:
        router.grid.routes = saved


# ---------------------------------------------------------------------------
# The routed fixtures of the #5517 suite: real journals from a real run.
# ---------------------------------------------------------------------------


class TestRoutedFixtures:
    def test_routed_kelvin_matches_the_reference(self):
        router = _routed_kelvin()
        _assert_identical(router, [U3, ("R10", "1"), ("Q1", "1"), ("U2", "1")])

    def test_stranding_kelvin_matches_the_reference(self):
        router = _stranding_kelvin()
        keys = sorted(set(router.pads))
        _assert_identical(router, keys)

    def test_board_edge_stranding_matches_the_reference(self):
        router = _walled_by_the_board_edge(2.6, 1.5)
        router.grid.mark_route(
            Route(net=9, net_name="OTHER", segments=[_seg(1.45, 0.2, 1.45, 1.3, 9, "OTHER")])
        )
        router.grid.mark_route(
            Route(net=9, net_name="OTHER", segments=[_seg(0.2, 1.3, 1.3, 1.3, 9, "OTHER")])
        )
        new, reference = _assert_identical(router, [("U1", "1")])
        assert new < reference


# ---------------------------------------------------------------------------
# The two pad_access building blocks, checked directly.
# ---------------------------------------------------------------------------


def _access_states(router):
    """Every (pad, copper-prefix) access set of the multi-record fixture."""
    copper = _ReplayCopper()
    saved = router.grid.routes
    try:
        router.grid.routes = copper.routes
        for record in [None, *router.commit_journal.records]:
            if record is not None:
                copper.apply(record)
            for key in sorted(router.pads):
                yield key, router.pads[key]
    finally:
        router.grid.routes = saved


class TestPresenceOnly:
    def test_presence_only_preserves_emptiness_bbox_and_closing_copper(self):
        router = _multi_record_router()
        saw_open = saw_closed = False
        for _key, pad in _access_states(router):
            full = compute_access_set(pad, router.grid, router.rules)
            fast = compute_access_set(pad, router.grid, router.rules, presence_only=True)
            assert fast.is_empty() == full.is_empty()
            assert fast.bbox == full.bbox
            assert fast.closing_copper == full.closing_copper
            assert fast.stubs == full.stubs
            if full.is_empty():
                saw_closed = True
                assert fast == full
            elif full.via_sites:
                saw_open = True
                assert fast.via_sites == ()
        assert saw_open and saw_closed, "fixture must cover both branches"

    def test_memoized_inventories_change_nothing(self):
        router = _multi_record_router()
        for _key, pad in _access_states(router):
            memoized = compute_access_set(pad, router.grid, router.rules)
            fresh = compute_access_set(
                pad,
                router.grid,
                router.rules,
                legality=DefaultAccessLegality(router.grid, router.rules),
            )
            assert memoized == fresh

    def test_memoized_drill_registry_is_never_shared_with_the_caller(self):
        router = _multi_record_router()
        adapter = DefaultAccessLegality(router.grid, router.rules, memoize_inventories=True)
        first = adapter.drill_registry()
        first.append((0.0, 0.0, 99.0))
        assert (0.0, 0.0, 99.0) not in adapter.drill_registry()
