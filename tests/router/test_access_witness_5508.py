"""Ordered commit journal: what copper landed, when, and in what order.

Epic #5508 / Phase 1b (issue #5517), first half.  Phase 1a
(``tests/router/test_pad_access_5508.py``) reads the access set of a pad
against *whatever copper happens to be on the grid*.  To turn that into a
witness -- "commit #3, net ``COMP``, iteration 0, is what closed U3.1" -- the
router has to remember the order copper landed in, which is what
:mod:`kicad_tools.router.access_witness` records.

The fixture is the same 4-pad Kelvin cluster (``ISENSE_A+`` across R10 / Q1 /
U2 / U3, flanked by the ``FOREIGN`` U9 pads) the Phase 1a tests use, plus a
``COMP`` net that the negotiated loop routes *first* -- so the journal's commit
order is the AC's "``COMP`` at iteration 0, then ``ISENSE_A+`` segments".
Phase 1a pre-marked ``COMP``'s copper by hand; here the router commits it, and
the journal is what proves the ordering.
"""

from __future__ import annotations

import json
from unittest import mock

import pytest

from kicad_tools.router.access_witness import (
    ACCESS_WITNESS_SIDECAR_SUFFIX,
    JOURNAL_SCHEMA_VERSION,
    MAX_JOURNAL_RECORDS,
    PASS_ESCAPE,
    PASS_INITIAL,
    PASS_ITERATION,
    CommitJournal,
    CommitRecord,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.cpp_backend import is_cpp_available
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

# Verbatim from tests/router/test_pad_access_5508.py (Phase 1a).
KELVIN_POSITIONS = {"R10": (5, 3), "Q1": (7, 11), "U2": (9, 14), "U3": (5, 15)}
U9_PADS = {"1": (3.6, 15.0), "2": (6.4, 15.0), "3": (5.0, 16.4)}

# COMP's two pads.  Phase 1a's COMP sits 0.3 mm above U3 so its committed
# copper closes U3's north exit; here it is moved clear of the cluster so the
# negotiated loop actually *routes* it -- the journal-order AC needs a COMP
# that lands, not one that strands a pad (that is Phase 1b's second half).
COMP_PADS = {"R20": (1.0, 1.0), "R21": (6.0, 1.0)}


def _rules(**overrides) -> DesignRules:
    params = {"grid_resolution": 0.1, "trace_width": 0.2, "trace_clearance": 0.2}
    params.update(overrides)
    return DesignRules(**params)


def _add_pad(router: Autorouter, ref: str, pin: str, x: float, y: float, net: int, net_name: str):
    router.add_component(
        ref,
        [
            {
                "number": pin,
                "x": x,
                "y": y,
                "width": 1,
                "height": 1,
                "net": net,
                "net_name": net_name,
                "layer": Layer.F_CU,
            }
        ],
    )


def _kelvin_fixture(*, force_python: bool = True) -> Autorouter:
    """The Phase 1a Kelvin cluster plus a routable COMP net."""
    router = Autorouter(
        20,
        20,
        rules=_rules(),
        force_python=force_python,
        physics_enabled=False,
    )
    for ref in ("R10", "U3", "Q1", "U2"):
        x, y = KELVIN_POSITIONS[ref]
        _add_pad(router, ref, "1", x, y, 1, "ISENSE_A+")
    for pin, (x, y) in U9_PADS.items():
        _add_pad(router, "U9", pin, x, y, 3, "FOREIGN")
    for ref, (x, y) in COMP_PADS.items():
        _add_pad(router, ref, "1", x, y, 2, "COMP")
    return router


def _route(router: Autorouter, *, max_iterations: int = 2) -> None:
    router.route_all_negotiated(
        max_iterations=max_iterations,
        timeout=60,
        per_net_timeout=10,
    )


def _segment(x1: float, y1: float, x2: float, y2: float, net: int = 7) -> Segment:
    return Segment(
        x1=x1,
        y1=y1,
        x2=x2,
        y2=y2,
        width=0.2,
        layer=Layer.F_CU,
        net=net,
        net_name=f"N{net}",
    )


def _route_obj(net: int = 7, *, y: float = 5.0) -> Route:
    return Route(net=net, net_name=f"N{net}", segments=[_segment(2.0, y, 8.0, y, net=net)])


# ---------------------------------------------------------------------------
# The observer itself
# ---------------------------------------------------------------------------


class TestGridObserver:
    """Every copper mutation on the grid reaches the journal, in order."""

    def test_mark_and_unmark_are_recorded_in_order(self):
        router = _kelvin_fixture()
        journal = router.commit_journal
        before = len(journal)

        route = _route_obj()
        router.grid.mark_route(route)
        router.grid.unmark_route(route)
        router.grid.mark_route(route)

        new = journal.records[before:]
        assert [r.kind for r in new] == ["commit", "rip", "restore"]
        assert [r.added for r in new] == [True, False, True]
        # A re-mark of a route object that was ripped is a *restore*, not a
        # fresh commit -- the replay must not double-count it as new copper.
        assert {r.route_id for r in new} == {id(route)}
        assert [r.index for r in new] == list(range(before, before + 3))

    def test_records_snapshot_geometry_not_references(self):
        """In-place mutation of a Route must not rewrite journal history."""
        router = _kelvin_fixture()
        journal = router.commit_journal
        route = _route_obj()
        router.grid.mark_route(route)
        recorded = journal.records[-1]

        route.segments[0].x2 = 19.0

        assert recorded.segments[0].x2 == pytest.approx(8.0)
        assert recorded.route.segments[0].x2 == pytest.approx(8.0)

    def test_unmark_of_unknown_route_is_not_journaled(self):
        """``grid.routes`` membership is what the access set reads."""
        router = _kelvin_fixture()
        journal = router.commit_journal
        before = len(journal)
        router.grid.unmark_route(_route_obj())
        assert len(journal) == before

    def test_resync_is_journaled_as_removal_then_addition(self):
        router = _kelvin_fixture()
        journal = router.commit_journal
        old = _route_obj(y=5.0)
        router.grid.mark_route(old)
        before = len(journal)

        new = _route_obj(y=6.0)
        router.grid.resync_route_occupancy([(old, new)])

        records = journal.records[before:]
        assert [r.kind for r in records] == ["rollback", "rollback"]
        assert [r.added for r in records] == [False, True]
        assert records[0].segments[0].y1 == pytest.approx(5.0)
        assert records[1].segments[0].y1 == pytest.approx(6.0)

    def test_observer_survives_a_trial_reset(self):
        """A discarded grid gets a fresh observer, and its copper is retired."""
        router = _kelvin_fixture()
        journal = router.commit_journal
        route = _route_obj()
        router.grid.mark_route(route)

        old_grid = router.grid
        router._reset_for_new_trial()
        assert router.grid is not old_grid
        assert router.grid.commit_observer is not None

        # The wholesale grid discard is journaled as the copper going away.
        retired = [r for r in journal.records if r.pass_name == "reset"]
        assert [r.added for r in retired] == [False]
        assert retired[0].net == route.net

        before = len(journal)
        router.grid.mark_route(_route_obj(net=8))
        assert len(journal) == before + 1

    def test_journal_is_capped(self):
        journal = CommitJournal()
        journal.records = [
            CommitRecord(
                index=i,
                kind="commit",
                added=True,
                pass_name=PASS_INITIAL,
                iteration=0,
                net=1,
                net_name="N",
                route_id=i,
                is_escape=False,
                segments=(),
                vias=(),
            )
            for i in range(MAX_JOURNAL_RECORDS)
        ]
        journal.observe("mark", _route_obj())
        assert len(journal) == MAX_JOURNAL_RECORDS
        assert journal.truncated is True
        assert "truncated" in journal.summary_line()


# ---------------------------------------------------------------------------
# Commit order on the Phase 1a fixture
# ---------------------------------------------------------------------------


class TestJournalOrderOnKelvinFixture:
    def test_comp_lands_at_iteration_zero_before_isense(self):
        router = _kelvin_fixture()
        _route(router)
        journal = router.commit_journal

        assert journal.net_commit_order()[:2] == ["COMP", "ISENSE_A+"]

        initial = [r for r in journal.records if r.pass_name == PASS_INITIAL]
        assert initial, "the initial pass must produce journal records"
        assert all(r.iteration == 0 for r in initial)
        assert all(r.kind == "commit" for r in initial)
        assert initial[0].net_name == "COMP"
        # ISENSE_A+ follows COMP, still at iteration 0.
        isense = [r for r in initial if r.net_name == "ISENSE_A+"]
        assert isense
        assert isense[0].index > initial[0].index
        assert isense[0].segments, "committed copper carries geometry"

    def test_initial_pass_precedes_every_ripup_iteration(self):
        router = _kelvin_fixture()
        _route(router)
        journal = router.commit_journal

        initial_max = max(r.index for r in journal.records if r.pass_name == PASS_INITIAL)
        loop = [r for r in journal.records if r.pass_name == PASS_ITERATION]
        assert loop, "the fixture must reach the rip-up loop"
        assert min(r.index for r in loop) > initial_max
        # Iteration numbers never go backwards.
        iterations = [r.iteration for r in loop]
        assert iterations == sorted(iterations)

    def test_ripups_are_journaled_and_reopen_the_net(self):
        """A commit-only journal cannot reconstruct copper after a rip-up."""
        router = _kelvin_fixture()
        _route(router)
        journal = router.commit_journal

        rips = journal.rips()
        assert rips, "the rip-up loop must produce rip records"
        first_rip = rips[0]
        assert first_rip.added is False
        assert first_rip.kind in {"rip", "rollback"}

        # Every ripped net gets copper back (commit or restore) afterwards,
        # or ends the run unrouted -- either way the journal says which.
        later = [
            r
            for r in journal.records
            if r.index > first_rip.index and r.net == first_rip.net and r.added
        ]
        assert later, "the ripped net's reroute must be journaled too"

    def test_journal_commit_order_matches_the_grid(self):
        """Net-level commit order is reproducible from the journal alone."""
        router = _kelvin_fixture()
        _route(router)
        journal = router.commit_journal

        # Replaying added/removed records yields exactly the routes the grid
        # ended up holding -- the invariant the Phase 1b replay depends on.
        present: list[int] = []
        for record in journal.records:
            if record.added:
                present.append(record.route_id)
            elif record.route_id in present:
                present.remove(record.route_id)
        assert sorted(present) == sorted(id(r) for r in router.grid.routes)


# ---------------------------------------------------------------------------
# Escape pre-phase
# ---------------------------------------------------------------------------


def _dense_package_router() -> Autorouter:
    """A dense dual-row package, as in tests/router/test_escape_terminal_copper_5398."""
    rules = DesignRules(trace_width=0.2, trace_clearance=0.2, via_drill=0.3, via_diameter=0.6)
    router = Autorouter(width=30.0, height=30.0, rules=rules, physics_enabled=False)
    pads = []
    net = 1
    for row_y in (13.0, 17.0):
        for column in range(10):
            pads.append(
                {
                    "number": str(net),
                    "x": 10.0 + column * 0.65,
                    "y": row_y,
                    "width": 0.3,
                    "height": 1.55,
                    "net": net,
                    "net_name": f"NET_{net}",
                    "layer": Layer.F_CU,
                }
            )
            net += 1
    router.add_component("U1", pads)
    return router


class TestEscapePass:
    def test_escape_stubs_are_journaled_as_the_escape_pass(self):
        """``apply_escape_routes`` marks the grid directly, bypassing ``_mark_route``.

        The grid-level observer is what catches them; without it the escape
        stubs -- the replay's baseline, i.e. Epic #5508's "access at
        escape-prephase end" -- would be missing from the journal entirely.
        """
        router = _dense_package_router()
        packages = router.detect_dense_packages()
        assert packages, "fixture must be classified dense so escapes are generated"

        routes = router.generate_escape_routes(packages)
        assert routes

        journal = router.commit_journal
        escape = [r for r in journal.records if r.pass_name == PASS_ESCAPE]
        assert escape, "escape stubs must be journaled"
        assert all(r.added for r in escape)
        assert all(r.iteration == 0 for r in escape)
        # Escape is the FIRST stage: no negotiated record precedes it.
        assert min(r.index for r in escape) == 0
        assert router.commit_journal.pass_name != PASS_ESCAPE, "stage tag is scoped"


# ---------------------------------------------------------------------------
# Serialization / sidecar
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_round_trip_preserves_records(self):
        router = _kelvin_fixture()
        route = Route(
            net=4,
            net_name="RT",
            segments=[_segment(1.0, 2.0, 3.0, 4.0, net=4)],
            vias=[
                Via(
                    x=3.0,
                    y=4.0,
                    drill=0.3,
                    diameter=0.6,
                    layers=(Layer.F_CU, Layer.B_CU),
                    net=4,
                    net_name="RT",
                )
            ],
        )
        router.grid.mark_route(route)
        router.grid.unmark_route(route)

        payload = router.commit_journal.to_dict()
        assert payload["schema_version"] == JOURNAL_SCHEMA_VERSION
        assert payload["record_count"] == len(payload["records"])
        # Must survive an actual JSON encode (the sidecar's real constraint).
        restored = CommitJournal.from_dict(json.loads(json.dumps(payload)))

        assert len(restored) == len(router.commit_journal)
        assert [r.kind for r in restored] == [r.kind for r in router.commit_journal]
        original = router.commit_journal.records[-2]
        copy = restored.records[-2]
        assert copy.net_name == original.net_name
        assert copy.segments == original.segments
        assert copy.vias == original.vias

    def test_unknown_schema_version_is_rejected(self):
        with pytest.raises(ValueError, match="schema_version"):
            CommitJournal.from_dict({"schema_version": 999, "records": []})

    def test_sidecar_suffix_is_stem_keyed(self):
        assert ACCESS_WITNESS_SIDECAR_SUFFIX == ".access_witness.json"


class TestSidecar:
    def test_route_step_writes_the_sidecar(self, tmp_path):
        from kicad_tools.cli.route_cmd import _write_access_witness_sidecar

        router = _kelvin_fixture()
        _route(router)

        pcb = tmp_path / "kelvin_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        _write_access_witness_sidecar(pcb, router, quiet=True)

        sidecar = tmp_path / f"kelvin_routed{ACCESS_WITNESS_SIDECAR_SUFFIX}"
        assert sidecar.exists()
        payload = json.loads(sidecar.read_text())
        assert payload["schema_version"] == JOURNAL_SCHEMA_VERSION
        assert payload["source"]["pcb"] == pcb.name
        journal = CommitJournal.from_dict(payload["journal"])
        assert journal.net_commit_order()[:2] == ["COMP", "ISENSE_A+"]

    def test_no_journal_means_no_sidecar(self, tmp_path):
        from kicad_tools.cli.route_cmd import _write_access_witness_sidecar

        pcb = tmp_path / "empty_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        _write_access_witness_sidecar(pcb, object(), quiet=True)
        assert not (tmp_path / f"empty_routed{ACCESS_WITNESS_SIDECAR_SUFFIX}").exists()

    def test_unwritable_directory_is_a_warning_not_a_failure(self, tmp_path, capsys):
        from kicad_tools.cli.route_cmd import _write_access_witness_sidecar

        router = _kelvin_fixture()
        router.grid.mark_route(_route_obj())
        pcb = tmp_path / "ro" / "board_routed.kicad_pcb"
        # Parent directory does not exist -> the write raises OSError, which
        # must degrade to a warning (a diagnostic sidecar never fails a route).
        _write_access_witness_sidecar(pcb, router, quiet=False)
        assert "could not write" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# Backend parity
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not is_cpp_available(), reason="C++ backend not built")
class TestBackendParity:
    def test_initial_pass_journal_order_matches_across_backends(self):
        """The journal is backend-agnostic: commits are Python-side either way.

        Scoped to the initial pass and the net-level commit order on purpose.
        The two backends' A* produce *different route geometry* (the C++
        search emits far more, shorter segments), so the rip-up loop diverges
        after iteration 0 and a record-for-record comparison would be
        comparing routing outcomes, not journal fidelity.
        """
        orders = []
        initial_shapes = []
        for force_python in (True, False):
            router = _kelvin_fixture(force_python=force_python)
            _route(router)
            journal = router.commit_journal
            orders.append(journal.net_commit_order())
            initial_shapes.append(
                [
                    (r.pass_name, r.iteration, r.kind, r.net_name)
                    for r in journal.records
                    if r.pass_name == PASS_INITIAL
                ]
            )

        assert orders[0] == orders[1]
        assert initial_shapes[0] == initial_shapes[1]


# ---------------------------------------------------------------------------
# Journaling must not perturb routing
# ---------------------------------------------------------------------------


def _copper(router: Autorouter) -> list[tuple]:
    return sorted(
        (s.x1, s.y1, s.x2, s.y2, s.width, s.layer.value, s.net)
        for route in router.routes
        for s in route.segments
    )


def test_recording_does_not_perturb_the_routed_copper():
    """The observer is a pure sink: silencing it changes nothing on the board.

    This is the in-suite proxy for Epic #5508's byte-identical-copper guard
    (boards 00-03, checked by SHA256 at PR time).  ``observe`` is stubbed to a
    no-op rather than detached, because the router re-installs the observer
    every time it rebuilds the grid -- a detach would silently be undone.
    """
    with_journal = _kelvin_fixture()
    _route(with_journal)
    assert len(with_journal.commit_journal) > 0

    # Built INSIDE the patch: ``attach`` captures a bound ``observe``, so a
    # router constructed beforehand would keep the real method.
    with mock.patch.object(CommitJournal, "observe", lambda self, event, route: None):
        silent = _kelvin_fixture()
        _route(silent)
    assert len(silent.commit_journal) == 0, "the stub must really have silenced recording"

    assert _copper(silent) == _copper(with_journal)
    assert len(silent.grid.routes) == len(with_journal.grid.routes)
