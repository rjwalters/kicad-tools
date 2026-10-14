"""Access-witness replay survives a released routing grid (issue #6346).

``kct route`` calls ``RoutingGrid.release_arrays()`` (via
``_release_routing_engine_state``) *before* it writes the access-witness
sidecars, so the replay runs against zero-size ``(0, 0, 0)`` occupancy planes.
The descriptive raster-labelling helper ``pad_access._marking_at`` used to
index those planes directly and raise ``IndexError``, which dropped the
``witness`` block from every partial-exit sidecar.

Markings only LABEL closing copper -- they never decide legality -- so after a
release the replay must return the same verdicts and attribution, with the
markings simply absent.
"""

from __future__ import annotations

import dataclasses

import pytest

from kicad_tools.router.access_witness import AccessWitness, replay
from kicad_tools.router.grid import RoutingGrid
from kicad_tools.router.pad_access import _marking_at
from kicad_tools.router.rules import DesignRules

pytestmark = pytest.mark.timeout(180)


def test_marking_at_on_released_grid_returns_empty() -> None:
    rules = DesignRules(grid_resolution=0.1, trace_width=0.2, trace_clearance=0.2)
    grid = RoutingGrid(5, 5, rules)
    gx, gy = grid.world_to_grid(2.5, 2.5)
    grid._pad_blocked[0, gy, gx] = True
    grid._is_obstacle[0, gy, gx] = True
    grid._usage_count[0, gy, gx] = 1
    live = _marking_at(grid, 2.5, 2.5, 0)
    assert {"pad_blocked", "is_obstacle", "usage_shared"} <= live

    grid.release_arrays()
    assert _marking_at(grid, 2.5, 2.5, 0) == frozenset()
    # Idempotent, and out-of-range points stay label-free rather than raising.
    grid.release_arrays()
    assert _marking_at(grid, 4.9, 4.9, grid.num_layers - 1) == frozenset()


def _without_markings(witness: AccessWitness) -> dict:
    return {pad.pad_key: dataclasses.replace(pad, closing_markings=()) for pad in witness.pads}


def test_replay_after_release_matches_live_replay() -> None:
    # A FRESH router, not the module-cached ``_routed_kelvin()`` from the 5508
    # suite: releasing the grid is destructive and must not leak into other
    # tests that read the cached run.
    from tests.router.test_access_witness_replay_5508 import U3, _route_once

    router = _route_once((3.0, 7.0))
    journal = router.commit_journal

    live = replay(journal, router, pad_keys=[U3])
    live_u3 = live.for_pad(*U3)
    assert live_u3 is not None
    assert live_u3.closing_markings, "live grid must still label closing copper"
    assert live_u3.first_closed_at is not None

    router.grid.release_arrays()
    released = replay(journal, router, pad_keys=[U3])  # used to raise IndexError

    released_u3 = released.for_pad(*U3)
    assert released_u3 is not None
    assert released_u3.closing_markings == ()
    for field in (
        "final_access",
        "access_at_escape_end",
        "first_closed_at",
        "first_closed_index",
        "first_closed_kind",
        "closing_nets",
        "closing_refs",
        "closing_copper_class",
        "reopened",
    ):
        assert getattr(released_u3, field) == getattr(live_u3, field), field
    assert _without_markings(released) == _without_markings(live)


# ---------------------------------------------------------------------------
# Issue #6391: prime the witness BEFORE the release, so the sidecars written
# after it keep ``closing_markings``.
# ---------------------------------------------------------------------------


def _populated_rtree_router():
    """COMP's closing track as 40 indexed segments: the segment R-tree is live.

    40 >= ``RTREE_SEGMENT_THRESHOLD``, so ``validate_segment_clearance`` would
    answer from the R-tree were the replay not to suspend it (#6303) -- the
    case where priming before the release could have changed a verdict.
    """
    from kicad_tools.router.grid import RTREE_AVAILABLE, RTREE_SEGMENT_THRESHOLD
    from tests.router.test_access_witness_replay_5508 import _comp_as_n_segments

    router = _comp_as_n_segments(40)
    if RTREE_AVAILABLE:
        assert router.grid._seg_rtree_count >= RTREE_SEGMENT_THRESHOLD
    return router


def _no_timeout_args():
    from types import SimpleNamespace

    return SimpleNamespace(_wall_clock_deadline=None, _post_route_reserve=0.0)


def test_primed_witness_keeps_closing_markings_in_post_release_sidecars(tmp_path) -> None:
    from kicad_tools.cli.route_cmd import (
        _prime_access_witness,
        _release_routing_engine_state,
        _write_access_witness_sidecar,
    )
    from kicad_tools.router.access_witness import load_access_witness_sidecar
    from tests.router.test_access_witness_replay_5508 import U3

    live = replay(_populated_rtree_router().commit_journal, _populated_rtree_router())
    live_u3 = live.for_pad(*U3)
    assert live_u3 is not None and live_u3.closing_markings

    router = _populated_rtree_router()
    _prime_access_witness(router, _no_timeout_args())
    _release_routing_engine_state(router)
    assert router.grid._blocked.size == 0  # the planes really are gone

    # Both post-release writers: the normal save and the partial save.
    for stem, journal_first in (("board_routed", False), ("board_routed_partial", True)):
        pcb = tmp_path / f"{stem}.kicad_pcb"
        sidecar = _write_access_witness_sidecar(
            pcb, router, quiet=True, journal_first=journal_first
        )
        assert sidecar is not None
        written = load_access_witness_sidecar(sidecar)
        assert written is not None
        u3 = written.for_pad(*U3)
        assert u3 is not None
        assert u3.closing_markings, stem
        assert u3.closing_markings == live_u3.closing_markings
        assert written.to_dict() == live.to_dict()


def test_primed_and_post_release_witness_agree_on_verdicts() -> None:
    """Priming on a populated R-tree changes labels only, never a verdict."""
    from kicad_tools.cli.route_cmd import _prime_access_witness, _release_routing_engine_state
    from kicad_tools.router.access_witness import witness_for_router
    from tests.router.test_access_witness_replay_5508 import U3

    primed_router = _populated_rtree_router()
    _prime_access_witness(primed_router, _no_timeout_args())
    _release_routing_engine_state(primed_router)
    primed = witness_for_router(primed_router)

    released_router = _populated_rtree_router()
    _release_routing_engine_state(released_router)
    released = witness_for_router(released_router)

    assert primed is not None and released is not None
    primed_u3, released_u3 = primed.for_pad(*U3), released.for_pad(*U3)
    assert primed_u3 is not None and released_u3 is not None
    assert primed_u3.first_closed_at == ("initial", 0)
    assert primed_u3.closing_markings and released_u3.closing_markings == ()
    for field in (
        "final_access",
        "access_at_escape_end",
        "first_closed_at",
        "first_closed_index",
        "first_closed_kind",
        "closing_nets",
        "closing_refs",
        "closing_copper_class",
        "reopened",
    ):
        assert getattr(primed_u3, field) == getattr(released_u3, field), field
    assert _without_markings(primed) == _without_markings(released)


def _fake_clock(monkeypatch, now: float) -> None:
    from types import SimpleNamespace

    import kicad_tools.cli.route_cmd as route_cmd

    monkeypatch.setattr(route_cmd, "time", SimpleNamespace(monotonic=lambda: now))


def test_prime_deadline_stays_inside_the_finishing_reserve(monkeypatch) -> None:
    from types import SimpleNamespace

    import kicad_tools.router.access_witness as access_witness
    from kicad_tools.cli.route_cmd import (
        _FINISHING_EXIT_MARGIN_SEC,
        _prime_access_witness,
    )

    _fake_clock(monkeypatch, 1000.0)
    seen: list = []
    monkeypatch.setattr(
        access_witness,
        "witness_for_router",
        lambda router, deadline=None: seen.append(deadline),
    )
    args = SimpleNamespace(_wall_clock_deadline=1000.0 + 300.0, _post_route_reserve=60.0)
    _prime_access_witness(_populated_rtree_router(), args)
    # 300 s left, minus the exit margin, minus the save/zone-fill/DRC reserve.
    assert seen == [1000.0 + 300.0 - _FINISHING_EXIT_MARGIN_SEC - 60.0]


def test_prime_deadline_leaves_the_auto_fix_reserve_alone(monkeypatch) -> None:
    """``--auto-fix`` runs after the release, so the prime must not eat its reserve (#3238)."""
    from types import SimpleNamespace

    import kicad_tools.router.access_witness as access_witness
    from kicad_tools.cli.route_cmd import (
        _FINISHING_EXIT_MARGIN_SEC,
        _prime_access_witness,
        _set_wall_clock_deadline,
    )

    _fake_clock(monkeypatch, 1000.0)
    seen: list = []
    monkeypatch.setattr(
        access_witness,
        "witness_for_router",
        lambda router, deadline=None: seen.append(deadline),
    )
    args = SimpleNamespace(timeout=900.0, auto_fix=True)
    _set_wall_clock_deadline(args)
    assert args._auto_fix_reserve > 0.0
    reserves = _FINISHING_EXIT_MARGIN_SEC + args._post_route_reserve + args._auto_fix_reserve

    # Routing ends exactly at its deadline -- the stranded-pad case the prime
    # is for.  Everything left belongs to auto-fix and the finishing stages.
    _fake_clock(monkeypatch, args._routing_deadline)
    _prime_access_witness(_populated_rtree_router(), args)
    assert seen == []

    # Routing finished 100 s early: the prime may spend that slack, and no more.
    early = args._routing_deadline - 100.0
    _fake_clock(monkeypatch, early)
    _prime_access_witness(_populated_rtree_router(), args)
    assert seen == [pytest.approx(args._wall_clock_deadline - reserves)]
    assert seen[0] <= args._wall_clock_deadline - args._auto_fix_reserve - args._post_route_reserve


def test_prime_is_skipped_when_auto_fix_and_finishing_reserves_eat_the_budget(monkeypatch) -> None:
    from types import SimpleNamespace

    import kicad_tools.router.access_witness as access_witness
    from kicad_tools.cli.route_cmd import _prime_access_witness

    _fake_clock(monkeypatch, 1000.0)
    seen: list = []
    monkeypatch.setattr(
        access_witness,
        "witness_for_router",
        lambda router, deadline=None: seen.append(deadline),
    )
    # 200 s left: enough for the 60 s finishing reserve alone, not with 180 s auto-fix.
    args = SimpleNamespace(
        _wall_clock_deadline=1000.0 + 200.0,
        _post_route_reserve=60.0,
        _auto_fix_reserve=180.0,
    )
    _prime_access_witness(_populated_rtree_router(), args)
    assert seen == []


def test_prime_is_skipped_when_the_reserve_has_eaten_the_budget(monkeypatch, tmp_path) -> None:
    """Budget gone: no prime, no cache, and the sidecar is still journal-only (#6273)."""
    from types import SimpleNamespace

    import kicad_tools.router.access_witness as access_witness
    from kicad_tools.cli.route_cmd import (
        _prime_access_witness,
        _release_routing_engine_state,
        _write_access_witness_sidecar,
    )

    _fake_clock(monkeypatch, 1000.0)
    called: list = []
    real = access_witness.witness_for_router
    monkeypatch.setattr(
        access_witness,
        "witness_for_router",
        lambda router, deadline=None: called.append(deadline) or real(router, deadline=deadline),
    )
    router = _populated_rtree_router()
    # 50 s left; the reserve alone wants 60 s.
    args = SimpleNamespace(_wall_clock_deadline=1000.0 + 50.0, _post_route_reserve=60.0)
    _prime_access_witness(router, args)
    assert called == []
    assert getattr(router, "_access_witness_cache", None) is None

    _release_routing_engine_state(router)
    import json

    sidecar = _write_access_witness_sidecar(
        tmp_path / "board_routed.kicad_pcb", router, quiet=True, replay=False
    )
    assert sidecar is not None
    payload = json.loads(sidecar.read_text())
    assert "journal" in payload and "witness" not in payload


def test_prime_is_skipped_after_an_interrupt_and_without_a_journal(monkeypatch) -> None:
    from types import SimpleNamespace

    import kicad_tools.cli.route_cmd as route_cmd

    router = _populated_rtree_router()
    monkeypatch.setitem(route_cmd._interrupt_state, "interrupted", True)
    route_cmd._prime_access_witness(router, _no_timeout_args())
    assert getattr(router, "_access_witness_cache", None) is None

    monkeypatch.setitem(route_cmd._interrupt_state, "interrupted", False)
    # A non-grid engine / stub: no journal at all is a silent no-op.
    route_cmd._prime_access_witness(SimpleNamespace(), _no_timeout_args())


def test_journal_truncation_after_a_prime_drops_the_cache() -> None:
    """#6320: a cut journal must not be answered from the primed witness."""
    from kicad_tools.cli.route_cmd import _prime_access_witness
    from kicad_tools.router.placement_feedback import _truncate_journal_to

    router = _populated_rtree_router()
    _prime_access_witness(router, _no_timeout_args())
    assert router._access_witness_cache is not None
    _truncate_journal_to(router, (0, False))
    assert getattr(router, "_access_witness_cache", None) is None


def test_cap_truncated_witness_is_memoized_but_deadline_truncated_is_not(monkeypatch) -> None:
    """Only a deadline truncation forces a later replay; a structural cap does not (#6391).

    An unbounded replay stops at the same 64-pad / evaluation / journal cap, so
    caching a cap-truncated witness from the bounded prime loses nothing -- and
    discarding it would replay twice and lose the primed ``closing_markings``.
    """
    from types import SimpleNamespace

    import kicad_tools.router.access_witness as access_witness

    clock = {"now": 1000.0}
    monkeypatch.setattr(access_witness, "time", SimpleNamespace(monotonic=lambda: clock["now"]))
    calls: list = []

    def fake_replay(journal, router, deadline=None):
        calls.append(deadline)
        return AccessWitness(record_count=len(journal), truncated=True)

    monkeypatch.setattr(access_witness, "replay", fake_replay)

    # Cap-truncated: the deadline is still in the future when the replay ends.
    router = _populated_rtree_router()
    first = access_witness.witness_for_router(router, deadline=2000.0)
    assert first.truncated
    assert router._access_witness_cache is first
    assert access_witness.witness_for_router(router) is first
    assert calls == [2000.0]

    # Deadline-truncated: the clock has reached the deadline -- not memoized.
    calls.clear()
    router = _populated_rtree_router()
    clock["now"] = 3000.0
    access_witness.witness_for_router(router, deadline=2500.0)
    assert getattr(router, "_access_witness_cache", None) is None
    access_witness.witness_for_router(router)
    assert calls == [2500.0, None]
