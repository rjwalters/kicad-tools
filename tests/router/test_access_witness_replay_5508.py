"""Offline access-loss witness: which commit closed which pad, and when.

Epic #5508 / Phase 1b (issue #5517), second half.  The first half
(``tests/router/test_access_witness_5508.py``) proved the router *remembers*
the order copper landed in.  This module proves that memory can be replayed
into an answer: for a terminal that ends a run unrouted, the pass and iteration
at which its access set became empty, and the nets whose copper did it --
replacing the ``PLACEMENT_BOUND`` / ``CONGESTION_SATURATED`` guesses #4507
calls "emitted hypotheses".

The fixture is the Phase 1a Kelvin cluster (``tests/router/test_pad_access_5508
.py``): ``ISENSE_A+`` across R10 / Q1 / U2 / U3, the ``FOREIGN`` U9 pads
flanking U3 west / east / south, and a ``COMP`` net whose committed copper runs
through the one corridor U3 has left.  Phase 1a hand-marked COMP's copper and
read the resulting access set; here the router commits it and the *journal* is
what says COMP did it, at which pass, and in which iteration.

Two cases are load-bearing and pull in opposite directions:

* U3 is closed **by a commit** -- the witness must name it.
* The #5410 ``DQ3`` pad is rejected by a coarse raster halo while its true
  copper gap clears the rule.  Its access set is **non-empty**, so the witness
  must report exactly that and blame nobody: a search that refused legal copper
  is #5509's category, not this epic's.
"""

from __future__ import annotations

import json

import pytest

# Issue #5508 / PR #5619 Doctor cycle (fleet-Judge verdict 2026-09-20): the
# two cached fixtures below route real boards lazily inside whichever test
# first touches them, and the stranding variant's negotiated loop (relief
# rescue + rip-ups) runs close to its own 60 s router budget.  pytest's
# default per-test timeout is ALSO 60 s, so under CI's shared-runner
# contention (-n auto bulk, KCT_NATIVE_MAX_CONCURRENCY=1) the first-touch
# test blows the default with zero margin -- all ten Test-job failures in
# run 35537834854 were this module timing out.  Per the repo convention
# (.github/workflows/ci.yml ~195: every test whose estimated duration
# exceeds ~30 s carries an explicit 300-900 s override), raise the whole
# module's ceiling to 180 s: fast tests are unaffected (a timeout is an
# upper bound), and both fixtures' first-touch tests get 3x the router's
# internal budget for setup + replay + assertions.
pytestmark = pytest.mark.timeout(180)

from kicad_tools.router.access_witness import (
    ACCESS_EMPTY,
    ACCESS_NON_EMPTY,
    ACCESS_WITNESS_SIDECAR_SUFFIX,
    NO_NET_LABEL,
    WITNESS_SCHEMA_VERSION,
    AccessWitness,
    CommitJournal,
    PadWitness,
    load_access_witness_sidecar,
    replay,
    stranded_terminals,
    witness_for_router,
)
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment, Via
from kicad_tools.router.rules import DesignRules

# Verbatim from tests/router/test_pad_access_5508.py (Phase 1a).
KELVIN_POSITIONS = {"R10": (5, 3), "Q1": (7, 11), "U2": (9, 14), "U3": (5, 15)}
U9_PADS = {"1": (3.6, 15.0), "2": (6.4, 15.0), "3": (5.0, 16.4)}

#: COMP's centreline: 0.3 mm above U3's top edge (y = 14.5).  A legal 0.2 mm
#: copper gap for COMP itself, and exactly the corridor U3's only surviving
#: exit stub runs through.
COMP_Y = 14.2

U3 = ("U3", "1")


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


def _kelvin_fixture(comp_x: tuple[float, float] = (3.0, 7.0)) -> Autorouter:
    """The Phase 1a cluster with COMP spanning ``comp_x`` at :data:`COMP_Y`.

    The default span is short enough that the negotiated loop routes COMP
    FIRST, at iteration 0 of the initial pass -- which is what makes it the
    commit that closes U3.
    """
    router = Autorouter(
        20,
        20,
        rules=_rules(),
        force_python=True,
        physics_enabled=False,
    )
    for ref in ("R10", "U3", "Q1", "U2"):
        x, y = KELVIN_POSITIONS[ref]
        _add_pad(router, ref, "1", x, y, 1, "ISENSE_A+")
    for pin, (x, y) in U9_PADS.items():
        _add_pad(router, "U9", pin, x, y, 3, "FOREIGN")
    _add_pad(router, "R20", "1", comp_x[0], COMP_Y, 2, "COMP")
    _add_pad(router, "R21", "1", comp_x[1], COMP_Y, 2, "COMP")
    return router


def _route(router: Autorouter, *, max_iterations: int = 2) -> None:
    router.route_all_negotiated(max_iterations=max_iterations, timeout=60, per_net_timeout=10)


def _route_once(comp_x: tuple[float, float]) -> Autorouter:
    router = _kelvin_fixture(comp_x=comp_x)
    _route(router)
    return router


#: Routing the fixture costs tens of seconds, and every assertion below is a
#: READ of the finished run (the replay restores whatever it rebinds), so the
#: two routed boards are built once per module rather than per test.
_CACHE: dict[tuple[float, float], Autorouter] = {}


def _cached(comp_x: tuple[float, float]) -> Autorouter:
    # NOT ``setdefault``: its default argument is evaluated eagerly, which
    # would re-route the fixture on every call and cache the result nobody
    # uses.
    if comp_x not in _CACHE:
        _CACHE[comp_x] = _route_once(comp_x)
    return _CACHE[comp_x]


def _routed_kelvin() -> Autorouter:
    return _cached((3.0, 7.0))


def _stranding_kelvin() -> Autorouter:
    """COMP stretched wide enough that it is the net that ENDS unrouted.

    R21 lands beside U2's pad, so COMP has no legal first move out of one of
    its own terminals and the negotiated loop cannot land it -- which is what
    puts COMP's pads in :func:`stranded_terminals` without any hand-holding.
    """
    return _cached((1.0, 9.0))


# ---------------------------------------------------------------------------
# The headline assertion: the replay names COMP, at initial[0], for U3.
# ---------------------------------------------------------------------------


class TestReplayOnTheKelvinFixture:
    def test_comp_closes_u3_at_iteration_zero_of_the_initial_pass(self):
        router = _routed_kelvin()
        assert router.commit_journal.net_commit_order()[0] == "COMP"

        witness = replay(router.commit_journal, router, pad_keys=[U3])
        pad = witness.for_pad(*U3)

        assert pad is not None
        assert pad.first_closed_at == ("initial", 0)
        assert "COMP" in pad.closing_nets
        assert pad.final_access == ACCESS_EMPTY
        assert pad.stranded is True
        # The FIRST journal record is COMP's commit, so the witness points at
        # the very first copper the negotiated loop laid down -- not at some
        # later record that merely happened to be re-evaluated.
        assert pad.first_closed_index == 0
        assert pad.first_closed_kind == "commit"
        assert router.commit_journal[pad.first_closed_index].net_name == "COMP"

    def test_u3_still_had_a_way_out_when_the_escape_prephase_ended(self):
        """The epic's Phase 2 reference point: access at escape-prephase end.

        Non-empty here is what makes the closure *attributable* -- a pad that
        was already stranded before the search began was stranded by
        placement, and no commit can be blamed for it.
        """
        router = _routed_kelvin()
        pad = replay(router.commit_journal, router, pad_keys=[U3]).for_pad(*U3)
        assert pad is not None
        assert pad.access_at_escape_end == ACCESS_NON_EMPTY

    def test_closing_copper_class_names_the_flanking_components_pads(self):
        """Phase 1a's classification survives the replay unchanged.

        U9's three pads wall U3 west / east / south (``foreign_pad``); COMP's
        committed track takes the north corridor (``route_segment``).  Both
        kinds must appear, and the U9 pads must be nameable individually --
        a witness that only said "route_segment" would lose the fact that the
        pad was already three-quarters walled before COMP arrived.
        """
        router = _routed_kelvin()
        pad = replay(router.commit_journal, router, pad_keys=[U3]).for_pad(*U3)

        assert pad is not None
        assert set(pad.closing_copper_class) == {"foreign_pad", "route_segment"}
        assert {"U9.1", "U9.2", "U9.3"} <= set(pad.closing_refs)
        assert "COMP" in pad.closing_refs
        assert {"COMP", "FOREIGN"} <= set(pad.closing_nets)
        # Raster markings are carried for labelling; they must never be empty
        # for copper that actually rejected a candidate.
        assert pad.closing_markings

    def test_the_resolver_clearances_are_reported(self):
        """#5509's caveat is only flaggable if the numbers travel with the verdict."""
        router = _routed_kelvin()
        witness = replay(router.commit_journal, router, pad_keys=[U3])
        values = dict(witness.clearance)
        assert values["trace_clearance"] == router.rules.trace_clearance
        assert values["via_clearance"] == router.rules.via_clearance
        assert values["trace_width"] == router.rules.trace_width

    def test_replay_leaves_the_grid_exactly_as_it_found_it(self):
        """The replay rebinds ``grid.routes`` to step copper; it must restore it.

        A witness that mutated the board it describes would be worse than no
        witness at all -- every later consumer (DRC, export, a second replay)
        reads the same grid.
        """
        router = _routed_kelvin()
        before = list(router.grid.routes)
        before_id = id(router.grid.routes)

        replay(router.commit_journal, router, pad_keys=[U3])

        assert id(router.grid.routes) == before_id
        assert router.grid.routes == before

    def test_replay_is_reproducible(self):
        router = _routed_kelvin()
        first = replay(router.commit_journal, router, pad_keys=[U3])
        second = replay(router.commit_journal, router, pad_keys=[U3])
        assert first.to_dict() == second.to_dict()

    def test_evaluation_budget_truncates_instead_of_running_away(self):
        router = _routed_kelvin()
        witness = replay(router.commit_journal, router, pad_keys=[U3], max_evaluations=2)
        assert witness.truncated is True
        assert witness.evaluations <= 2

    def test_pad_cap_truncates_deterministically(self):
        router = _routed_kelvin()
        keys = [U3, ("R10", "1"), ("Q1", "1")]
        witness = replay(router.commit_journal, router, pad_keys=keys, max_pads=1)
        assert [p.pad_key for p in witness] == [U3]
        assert witness.truncated is True


# ---------------------------------------------------------------------------
# Negative control (#5410 DQ3): the halo rejects, the geometry accepts.
# ---------------------------------------------------------------------------


def _coarse_halo_fixture() -> Autorouter:
    """Verbatim geometry from Phase 1a's ``DQ3`` negative control.

    ``_mark_via`` blocks a 0.7 mm square of raster around the foreign via, so
    the east stub's end cell reads BLOCKED -- but the true copper gap there is
    0.213 mm against a 0.200 mm rule, so the stub is legal.
    """
    router = Autorouter(20, 20, rules=_rules(), force_python=True, physics_enabled=False)
    _add_pad(router, "U1", "1", 5, 5, 5, "DQ3")
    router._mark_route(
        Route(
            net=4,
            net_name="DQS_N",
            segments=[],
            vias=[
                Via(
                    x=6.763,
                    y=5.0,
                    drill=0.35,
                    diameter=0.7,
                    layers=(Layer.F_CU, Layer.B_CU),
                    net=4,
                    net_name="DQS_N",
                )
            ],
        )
    )
    return router


class TestNegativeControl:
    def test_a_halo_rejected_but_legal_via_leaves_access_open_and_blames_nobody(self):
        router = _coarse_halo_fixture()
        # Precondition: the raster really does read blocked at the stub end.
        gx, gy = router.grid.world_to_grid(6.1, 5.0)
        assert router.grid.is_blocked(gx, gy, Layer.F_CU, 5) is True
        # ...and the commit really was journaled, so the replay had something
        # to replay.  A vacuous journal would make this test pass for the
        # wrong reason.
        assert len(router.commit_journal) == 1

        pad = replay(router.commit_journal, router, pad_keys=[("U1", "1")]).for_pad("U1", "1")

        assert pad is not None
        assert pad.final_access == ACCESS_NON_EMPTY
        assert pad.access_at_escape_end == ACCESS_NON_EMPTY
        assert pad.closing_nets == ()
        assert pad.first_closed_at is None
        assert pad.stranded is False
        assert "no commit closed it" in pad.one_line()


# ---------------------------------------------------------------------------
# Rip-ups: a commit-only replay would get this wrong.
# ---------------------------------------------------------------------------


class TestRipUpsReopenAccess:
    def test_ripping_the_closing_route_reopens_the_pad(self):
        """The whole reason :attr:`CommitRecord.added` exists.

        A replay that only followed commits would still be counting COMP's
        copper after it was ripped, and would report U3 as stranded by a
        route that is no longer on the board.
        """
        router = _kelvin_fixture()
        comp = Route(
            net=2,
            net_name="COMP",
            segments=[
                Segment(
                    x1=1.0,
                    y1=COMP_Y,
                    x2=9.0,
                    y2=COMP_Y,
                    width=0.2,
                    layer=Layer.F_CU,
                    net=2,
                    net_name="COMP",
                )
            ],
        )
        with router.commit_journal.context("initial", 0):
            router.grid.mark_route(comp)
        with router.commit_journal.context("iteration", 1):
            router.grid.unmark_route(comp)

        pad = replay(router.commit_journal, router, pad_keys=[U3]).for_pad(*U3)

        assert pad is not None
        assert pad.first_closed_at == ("initial", 0)
        assert "COMP" in pad.closing_nets
        # ...and the rip put the corridor back.
        assert pad.final_access == ACCESS_NON_EMPTY
        assert pad.reopened is True
        assert pad.stranded is False
        assert "reopened" in pad.one_line()


# ---------------------------------------------------------------------------
# Which terminals a replay tracks when the caller does not say
# ---------------------------------------------------------------------------


class TestStrandedTerminals:
    def test_pads_of_a_net_that_never_landed_are_tracked(self):
        router = _stranding_kelvin()
        assert 2 in router.get_failed_nets(), "fixture must leave COMP unrouted"

        keys = stranded_terminals(router)
        assert ("R20", "1") in keys and ("R21", "1") in keys
        assert keys == sorted(keys)

        witness = witness_for_router(router)
        assert witness is not None
        assert {p.net_name for p in witness} == {"COMP"}

    def test_a_router_without_pads_yields_nothing(self):
        assert stranded_terminals(object()) == []

    def test_replay_without_a_grid_returns_an_empty_witness(self):
        journal = CommitJournal()
        witness = replay(journal, object())
        assert not witness
        assert witness.record_count == 0

    def test_witness_is_memoized_per_router(self):
        router = _stranding_kelvin()
        first = witness_for_router(router)
        second = witness_for_router(router)
        assert first is second

    def test_a_fully_routed_run_produces_an_empty_but_present_witness(self):
        """ "Nothing stranded" and "no witness available" must stay distinguishable."""
        router = _routed_kelvin()
        assert router.get_failed_nets() == []
        witness = witness_for_router(router)
        assert witness is not None
        assert not witness
        assert len(witness) == 0


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestWitnessSerialization:
    def test_round_trip_through_json_preserves_every_field(self):
        router = _routed_kelvin()
        witness = replay(router.commit_journal, router, pad_keys=[U3])

        restored = AccessWitness.from_dict(json.loads(json.dumps(witness.to_dict())))

        assert restored.to_dict() == witness.to_dict()
        pad = restored.for_pad(*U3)
        assert pad is not None
        assert pad.first_closed_at == ("initial", 0)
        assert pad.closing_nets == witness.for_pad(*U3).closing_nets
        assert restored.clearance == witness.clearance

    def test_unknown_schema_version_is_rejected(self):
        with pytest.raises(ValueError, match="schema_version"):
            AccessWitness.from_dict({"schema_version": 999, "pads": []})

    def test_for_net_keeps_the_metadata_and_drops_other_nets(self):
        witness = AccessWitness(
            pads=(
                PadWitness("A", "1", 1, "N1", ACCESS_NON_EMPTY, ACCESS_EMPTY),
                PadWitness("B", "1", 2, "N2", ACCESS_NON_EMPTY, ACCESS_EMPTY),
            ),
            record_count=7,
            truncated=True,
            clearance=(("trace_clearance", 0.2),),
        )
        only = witness.for_net("N2")
        assert [p.ref for p in only] == ["B"]
        assert only.record_count == 7
        assert only.truncated is True
        assert only.clearance == (("trace_clearance", 0.2),)

    def test_summary_line_counts_closed_terminals(self):
        router = _routed_kelvin()
        witness = replay(router.commit_journal, router, pad_keys=[U3])
        assert "1 terminal(s)" in witness.summary_line()
        assert "1 closed by a commit" in witness.summary_line()


# ---------------------------------------------------------------------------
# The sidecar: how the verdict survives the death of the router
# ---------------------------------------------------------------------------


def _write_sidecar(tmp_path, witness: AccessWitness, stem: str = "board_routed"):
    """A sidecar in the exact shape ``kct route`` writes."""
    pcb = tmp_path / f"{stem}.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    (tmp_path / f"{stem}{ACCESS_WITNESS_SIDECAR_SUFFIX}").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {"pcb": pcb.name},
                "journal": CommitJournal().to_dict(),
                "witness": witness.to_dict(),
            },
            separators=(",", ":"),
        )
    )
    return pcb


class TestSidecar:
    def test_route_step_writes_the_witness_block_when_something_stranded(self, tmp_path):
        from kicad_tools.cli.route_cmd import _write_access_witness_sidecar

        router = _stranding_kelvin()
        pcb = tmp_path / "kelvin_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        _write_access_witness_sidecar(pcb, router, quiet=True)

        payload = json.loads(
            (tmp_path / f"kelvin_routed{ACCESS_WITNESS_SIDECAR_SUFFIX}").read_text()
        )
        assert payload["witness"]["schema_version"] == WITNESS_SCHEMA_VERSION
        witness = AccessWitness.from_dict(payload["witness"])
        assert {p.net_name for p in witness} == {"COMP"}

    def test_a_fully_routed_board_writes_no_witness_key(self, tmp_path):
        """PR 1's sidecar shape is unchanged for the common case."""
        from kicad_tools.cli.route_cmd import _write_access_witness_sidecar

        router = _routed_kelvin()
        pcb = tmp_path / "clean_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        _write_access_witness_sidecar(pcb, router, quiet=True)

        payload = json.loads(
            (tmp_path / f"clean_routed{ACCESS_WITNESS_SIDECAR_SUFFIX}").read_text()
        )
        assert "witness" not in payload
        assert "journal" in payload

    def test_the_sidecar_is_discovered_from_the_board_path(self, tmp_path):
        router = _routed_kelvin()
        witness = replay(router.commit_journal, router, pad_keys=[U3])
        pcb = _write_sidecar(tmp_path, witness)

        loaded = load_access_witness_sidecar(pcb)
        assert loaded is not None
        assert loaded.to_dict() == witness.to_dict()

    def test_the_sidecar_can_also_be_named_directly(self, tmp_path):
        router = _routed_kelvin()
        witness = replay(router.commit_journal, router, pad_keys=[U3])
        _write_sidecar(tmp_path, witness)

        direct = load_access_witness_sidecar(
            tmp_path / f"board_routed{ACCESS_WITNESS_SIDECAR_SUFFIX}"
        )
        assert direct is not None
        assert len(direct) == 1

    def test_an_absent_sidecar_is_not_an_error(self, tmp_path):
        assert load_access_witness_sidecar(tmp_path / "nothing.kicad_pcb") is None

    def test_a_malformed_sidecar_is_not_an_error(self, tmp_path):
        """A broken diagnostic must never break output that worked without it."""
        pcb = tmp_path / "bad_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (tmp_path / f"bad_routed{ACCESS_WITNESS_SIDECAR_SUFFIX}").write_text("{not json")
        assert load_access_witness_sidecar(pcb) is None

    def test_a_journal_only_sidecar_yields_no_witness(self, tmp_path):
        pcb = tmp_path / "j_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        (tmp_path / f"j_routed{ACCESS_WITNESS_SIDECAR_SUFFIX}").write_text(
            json.dumps({"schema_version": 1, "journal": CommitJournal().to_dict()})
        )
        assert load_access_witness_sidecar(pcb) is None


# ---------------------------------------------------------------------------
# Consumers: StuckNetDiagnosis, classify_stuck_nets, net-status --why
# ---------------------------------------------------------------------------

_HEADER = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
"""


def _ring_pads(cx: float, cy: float, radius: float, net: int, name: str) -> str:
    """A tight wall of foreign pads -- verbatim shape from test_stuck_classifier."""
    import math

    out = []
    for i in range(16):
        ang = 2 * math.pi * i / 16
        out.append(
            f'  (footprint "wall{i}" (layer "F.Cu") '
            f"(at {cx + radius * math.cos(ang):.4f} {cy + radius * math.sin(ang):.4f})\n"
            f'    (property "Reference" "W{i}")\n'
            f'    (pad "1" smd circle (at 0 0) (size 0.1 0.1) '
            f'(layers "F.Cu") (net {net} "{name}"))\n'
            f"  )\n"
        )
    return "".join(out)


def _comp_stuck_board() -> str:
    """A board whose net ``COMP`` is incomplete with ``R21.1`` walled in.

    Deliberately shares its net and pad NAMES with :func:`_stranding_kelvin`
    so a witness replayed from that real routing run describes this board's
    terminals.  The board is synthetic; the witness is not.
    """
    return (
        _HEADER
        + '  (net 0 "")\n'
        + '  (net 1 "COMP")\n'
        + '  (net 2 "WALL")\n'
        + '  (footprint "R_0402" (layer "F.Cu") (at 10 10)\n'
        + '    (property "Reference" "R20")\n'
        + '    (pad "1" smd rect (at -0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "COMP"))\n'
        + '    (pad "2" smd rect (at 0.5 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "COMP"))\n'
        + "  )\n"
        + '  (footprint "U_QFN" (layer "F.Cu") (at 50 50)\n'
        + '    (property "Reference" "R21")\n'
        + '    (pad "1" smd circle (at 0 0) (size 0.1 0.1) (layers "F.Cu") (net 1 "COMP"))\n'
        + "  )\n"
        + _ring_pads(50, 50, 0.15, 2, "WALL")
        + '  (segment (start 9.5 10) (end 10.5 10) (width 0.25) (layer "F.Cu") (net 1))\n'
        + ")\n"
    )


@pytest.fixture(scope="module")
def stranding_witness() -> AccessWitness:
    witness = witness_for_router(_stranding_kelvin())
    assert witness, "fixture must strand COMP"
    return witness


def _board_with_witness(tmp_path, witness: AccessWitness):
    pcb = tmp_path / "comp_routed.kicad_pcb"
    pcb.write_text(_comp_stuck_board())
    (tmp_path / f"comp_routed{ACCESS_WITNESS_SIDECAR_SUFFIX}").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "source": {"pcb": pcb.name},
                "journal": CommitJournal().to_dict(),
                "witness": witness.to_dict(),
            }
        )
    )
    return pcb


class TestStuckNetDiagnosisSchema:
    def test_to_dict_is_unchanged_when_no_witness_is_attached(self):
        from kicad_tools.router.stuck_classifier import StuckClass, StuckNetDiagnosis

        diag = StuckNetDiagnosis(
            net_name="N",
            net_number=1,
            classification=StuckClass.ESCAPE_BLOCKED,
            unconnected_pads=["U1.1"],
        )
        assert diag.access_witness is None
        assert "access_witness" not in diag.to_dict()
        # ...and one_line() gains nothing either.
        assert "\n" not in diag.one_line()

    def test_the_key_appears_only_once_a_witness_is_attached(self, stranding_witness):
        from kicad_tools.router.stuck_classifier import StuckClass, StuckNetDiagnosis

        diag = StuckNetDiagnosis(
            net_name="COMP",
            net_number=1,
            classification=StuckClass.ESCAPE_BLOCKED,
            unconnected_pads=["R21.1"],
            access_witness=stranding_witness.for_net("COMP"),
        )
        payload = diag.to_dict()
        assert payload["access_witness"]["schema_version"] == WITNESS_SCHEMA_VERSION
        assert payload["access_witness"]["pads"]
        assert "witness:" in diag.one_line()


class TestClassifierAutoDiscovery:
    def test_the_sidecar_is_picked_up_next_to_the_board(self, tmp_path, stranding_witness):
        from kicad_tools.router.stuck_classifier import classify_stuck_nets

        pcb = _board_with_witness(tmp_path, stranding_witness)
        result = classify_stuck_nets(pcb)

        comp = [d for d in result.diagnoses if d.net_name == "COMP"]
        assert comp, "fixture board must classify COMP as stuck"
        assert comp[0].access_witness is not None
        assert {p.net_name for p in comp[0].access_witness} == {"COMP"}

    def test_a_board_without_a_sidecar_classifies_exactly_as_before(self, tmp_path):
        from kicad_tools.router.stuck_classifier import classify_stuck_nets

        pcb = tmp_path / "bare.kicad_pcb"
        pcb.write_text(_comp_stuck_board())
        result = classify_stuck_nets(pcb)

        assert result.diagnoses
        assert all(d.access_witness is None for d in result.diagnoses)
        assert all("access_witness" not in d.to_dict() for d in result.diagnoses)

    def test_an_explicit_path_overrides_discovery(self, tmp_path, stranding_witness):
        from kicad_tools.router.stuck_classifier import classify_stuck_nets

        pcb = tmp_path / "bare2.kicad_pcb"
        pcb.write_text(_comp_stuck_board())
        elsewhere = tmp_path / "other"
        elsewhere.mkdir()
        _write_sidecar(elsewhere, stranding_witness, stem="other_routed")

        result = classify_stuck_nets(
            pcb,
            access_witness_path=elsewhere / f"other_routed{ACCESS_WITNESS_SIDECAR_SUFFIX}",
        )
        comp = [d for d in result.diagnoses if d.net_name == "COMP"]
        assert comp and comp[0].access_witness is not None


class TestNetStatusWhy:
    def test_json_output_carries_the_witness_block(self, tmp_path, capsys, stranding_witness):
        from kicad_tools.cli.net_status_cmd import output_why

        pcb = _board_with_witness(tmp_path, stranding_witness)
        code = output_why(pcb, "json")
        payload = json.loads(capsys.readouterr().out)

        assert code == 2
        comp = [d for d in payload["nets"] if d["net_name"] == "COMP"]
        assert comp
        block = comp[0]["access_witness"]
        assert block["schema_version"] == WITNESS_SCHEMA_VERSION
        pad = block["pads"][0]
        assert {"access_at_escape_end", "final_access", "closing_nets"} <= set(pad)
        assert block["clearance"]["trace_clearance"] == 0.2

    def test_text_output_documents_the_fields(self, tmp_path, capsys, stranding_witness):
        from kicad_tools.cli.net_status_cmd import output_why

        pcb = _board_with_witness(tmp_path, stranding_witness)
        output_why(pcb, "text")
        out = capsys.readouterr().out

        assert "access witness:" in out
        assert "access at escape end:" in out
        assert "access now:" in out
        assert "first closed at:" in out
        assert "resolver clearances:" in out

    def test_text_output_without_a_sidecar_has_no_witness_section(self, tmp_path, capsys):
        from kicad_tools.cli.net_status_cmd import output_why

        pcb = tmp_path / "bare3.kicad_pcb"
        pcb.write_text(_comp_stuck_board())
        output_why(pcb, "text")
        assert "access witness" not in capsys.readouterr().out


class TestStrandingReport:
    def test_format_stranding_report_renders_the_witness(self, tmp_path, stranding_witness):
        from kicad_tools.router.rescue_diagnostics import format_stranding_report

        pcb = _board_with_witness(tmp_path, stranding_witness)
        report = format_stranding_report(pcb)
        assert "witness:" in report

    def test_format_stranding_report_is_unchanged_without_a_sidecar(self, tmp_path):
        from kicad_tools.router.rescue_diagnostics import format_stranding_report

        pcb = tmp_path / "bare4.kicad_pcb"
        pcb.write_text(_comp_stuck_board())
        report = format_stranding_report(pcb)
        assert "COMP" in report
        assert "witness" not in report


class TestRoutingDiagnosticsJson:
    def test_the_routing_report_gains_the_witness_when_something_stranded(self):
        from kicad_tools.router.output import get_routing_diagnostics_json

        router = _stranding_kelvin()
        payload = get_routing_diagnostics_json(router, {"ISENSE_A+": 1, "COMP": 2, "FOREIGN": 3}, 3)
        assert payload["access_witness"]["pads"]

    def test_the_key_is_absent_for_a_clean_run(self):
        from kicad_tools.router.output import get_routing_diagnostics_json

        router = _routed_kelvin()
        payload = get_routing_diagnostics_json(router, {"ISENSE_A+": 1, "COMP": 2, "FOREIGN": 3}, 3)
        assert "access_witness" not in payload


# ---------------------------------------------------------------------------
# Issue #5639: verdict shapes the format note has to be able to explain
# ---------------------------------------------------------------------------


def _walled_by_the_board_edge(width: float, height: float) -> Autorouter:
    """One pad on a board too small for its own exit stubs.

    ``compute_access_set`` rejects every one of the eight stub directions as
    out-of-bounds, so the access set is empty with ``board_edge`` closing
    copper (KiCad net 0, ``<board-edge>``) -- the exact shape board-07's
    ``U5.1``..``U5.8`` reported in the Phase 1c evidence package.
    """
    router = Autorouter(width, height, rules=_rules(), force_python=True, physics_enabled=False)
    _add_pad(router, "U1", "1", 0.75, 0.75, 7, "A0")
    return router


def _commit_other_net(
    router: Autorouter, seg: Segment, *, pass_name="routing", iteration=0
) -> None:
    route = Route(net=9, net_name="OTHER", segments=[seg], vias=[])
    with router.commit_journal.context(pass_name, iteration):
        router.grid.mark_route(route)


class TestPlacementStrandingIsNeverBlamedOnACommit:
    """#5639 defect 2: ``empty`` / non-null / ``empty`` was not a real verdict.

    A pad with no way out *before the search committed anything* is stranded by
    placement.  The replay used to record the first record whose envelope
    happened to bring such a pad up for re-evaluation as its closure -- which
    is how board-07 reported eight pads "closed" by the board outline, copper
    no commit can place.
    """

    def _tiny_board_with_one_later_commit(self) -> Autorouter:
        router = _walled_by_the_board_edge(1.5, 1.5)
        # Precondition: the pad is already stranded with NO copper committed.
        from kicad_tools.router.pad_access import compute_access_set

        before = compute_access_set(router.pads[("U1", "1")], router.grid, router.rules)
        assert before.is_empty()
        assert [c.kind for c in before.closing_copper] == ["board_edge"]

        # A record from another net, whose envelope covers the pad's access
        # bbox -- i.e. the record that used to be blamed.
        _commit_other_net(
            router,
            Segment(
                x1=0.1, y1=0.1, x2=1.4, y2=0.1, width=0.2, layer=Layer.F_CU, net=9, net_name="OTHER"
            ),
        )
        assert len(router.commit_journal) == 1
        return router

    def test_a_pad_stranded_before_the_first_record_blames_no_commit(self):
        router = self._tiny_board_with_one_later_commit()

        pad = replay(router.commit_journal, router, pad_keys=[("U1", "1")]).for_pad("U1", "1")

        assert pad is not None
        # Row 3 of the format note: empty / null / empty = placement stranded.
        assert pad.access_at_escape_end == ACCESS_EMPTY
        assert pad.final_access == ACCESS_EMPTY
        assert pad.first_closed_at is None
        assert pad.first_closed_index is None
        assert pad.first_closed_kind is None
        assert pad.closing_nets == ()
        assert pad.reopened is False

    def test_the_one_line_rendering_says_placement_not_refused_copper(self):
        router = self._tiny_board_with_one_later_commit()
        pad = replay(router.commit_journal, router, pad_keys=[("U1", "1")]).for_pad("U1", "1")
        assert pad is not None
        assert "stranded before the first commit" in pad.one_line()

    def test_a_real_non_empty_to_empty_transition_is_still_attributed(self):
        """The fix must not silence the verdict the epic exists to produce."""
        router = _walled_by_the_board_edge(2.6, 1.5)
        from kicad_tools.router.pad_access import compute_access_set

        before = compute_access_set(router.pads[("U1", "1")], router.grid, router.rules)
        assert not before.is_empty(), "fixture must leave exactly one way out"

        _commit_other_net(
            router,
            Segment(
                x1=1.45,
                y1=0.2,
                x2=1.45,
                y2=1.3,
                width=0.2,
                layer=Layer.F_CU,
                net=9,
                net_name="OTHER",
            ),
        )
        pad = replay(router.commit_journal, router, pad_keys=[("U1", "1")]).for_pad("U1", "1")

        assert pad is not None
        assert pad.access_at_escape_end == ACCESS_NON_EMPTY
        assert pad.final_access == ACCESS_EMPTY
        assert pad.first_closed_at == ("routing", 0)
        assert pad.first_closed_index == 0
        assert "OTHER" in pad.closing_nets


class TestNetlessClosingCopperIsNotANet:
    """#5639 defect 3: ``closing_nets: ["net0"]`` for board-outline copper."""

    def test_board_edge_copper_reports_the_sentinel_not_net0(self):
        router = _walled_by_the_board_edge(2.6, 1.5)
        _commit_other_net(
            router,
            Segment(
                x1=1.45,
                y1=0.2,
                x2=1.45,
                y2=1.3,
                width=0.2,
                layer=Layer.F_CU,
                net=9,
                net_name="OTHER",
            ),
        )
        pad = replay(router.commit_journal, router, pad_keys=[("U1", "1")]).for_pad("U1", "1")

        assert pad is not None
        assert "board_edge" in pad.closing_copper_class
        assert "<board-edge>" in pad.closing_refs
        # The information the net-name field must NOT invent:
        assert "net0" not in pad.closing_nets
        assert NO_NET_LABEL in pad.closing_nets
        # ...while a real net beside it is still named normally.
        assert "OTHER" in pad.closing_nets

    def test_no_closing_net_looks_like_a_synthetic_net_index(self):
        """A script filtering ``closing_nets`` must not see a fabricated name."""
        import re

        router = _walled_by_the_board_edge(2.6, 1.5)
        _commit_other_net(
            router,
            Segment(
                x1=1.45,
                y1=0.2,
                x2=1.45,
                y2=1.3,
                width=0.2,
                layer=Layer.F_CU,
                net=9,
                net_name="OTHER",
            ),
        )
        pad = replay(router.commit_journal, router, pad_keys=[("U1", "1")]).for_pad("U1", "1")

        assert pad is not None
        assert not [n for n in pad.closing_nets if re.fullmatch(r"net0+", n)]

    def test_net_commit_order_labels_netless_copper_the_same_way(self):
        journal = CommitJournal()
        with journal.context("fixed", 0):
            journal.observe(
                "mark",
                Route(
                    net=0,
                    net_name="",
                    segments=[
                        Segment(
                            x1=0.0,
                            y1=0.0,
                            x2=1.0,
                            y2=0.0,
                            width=0.2,
                            layer=Layer.F_CU,
                            net=0,
                            net_name="",
                        )
                    ],
                    vias=[],
                ),
            )
        assert journal.net_commit_order() == [NO_NET_LABEL]
