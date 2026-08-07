"""Composed-path tests for the placement-delta feedback loop (issue #4468).

Phase 3 of the board-07 router<->placement epic (#3438).  Phase 2 (#4467)
exercised the driver's transaction semantics against a ``FakeRouter`` +
``MockPCB``; the pieces that only appear when the loop is composed with a REAL
``Autorouter`` and a REAL ``PCB`` -- and that #4518 flagged as never exercised
end to end -- are covered here:

1. **Classification input.**  The stuck-net classifier is a *routed-board*
   diagnostic.  The loop holds the UNROUTED input board (the CLI loads it from
   the staged input so footprints can be mutated), so it must merge the router's
   current copper into a throwaway view before classifying.  Without that, every
   signal net reads as stuck with zero blockers and the proposals describe a
   board that does not exist.

2. **Baseline reuse.**  The CLI invokes the loop straight after a completed
   routing pass; re-routing an identical baseline is pure duplicated wall clock
   (~11 min on board-07).

3. **Placement persistence.**  A kept delta means the routed copper is
   geometrically valid only against the MOVED footprints, so the CLI must write
   the mutated board out as the placement source for the routed artifact --
   otherwise the saved board pairs moved-pad copper with original footprint
   positions ("copper to nowhere").
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.router import PlacementDelta, PlacementDeltaFeedbackLoop
from kicad_tools.schema.pcb import PCB

# Reuse the Phase-1/2 fixtures verbatim.
from tests.router.test_placement_delta import _facing_rows_bundle_board, _load
from tests.router.test_placement_delta_feedback import (
    FakePad,
    FakeRouter,
    MockFootprint,
    MockPCB,
)

_SEGMENT_SEXP = '(segment (start 30 49) (end 50 49) (width 0.2) (layer "F.Cu") (net 1))'


class RoutedFakeRouter(FakeRouter):
    """``FakeRouter`` that also serializes copper, like a real ``Autorouter``."""

    def __init__(self, pads, total_nets, failed_predicate, route_sexp: str = _SEGMENT_SEXP):
        super().__init__(pads, total_nets, failed_predicate)
        self._route_sexp = route_sexp
        self.routes = [object()]

    def to_sexp(self, *, skip_cleanup: bool = False, name_only: bool = False) -> str:
        return self._route_sexp


# --------------------------------------------------------------------------- #
# 1. The classifier sees the ROUTED state, not the unrouted input board        #
# --------------------------------------------------------------------------- #


class TestRoutedPcbView:
    def test_view_carries_the_routers_copper(self, tmp_path: Path):
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        # The fixture ships one pre-existing segment; the view must carry the
        # ROUTER's copper instead of stacking the router's on top of the stale
        # board copper (which would double-count blocker geometry).
        stale = list(pcb.segments)
        assert len(stale) == 1

        loop = PlacementDeltaFeedbackLoop(
            router=RoutedFakeRouter([], 0, lambda _r: []), pcb=pcb, verbose=False
        )
        view = loop._routed_pcb_view()

        assert view is not None
        assert view is not pcb, "the view must not alias the board the applicator mutates"
        assert len(view.segments) == 1
        assert view.segments[0].start != stale[0].start
        assert list(pcb.segments) == stale, "building the view must not mutate the loop's board"

    def test_view_reflects_placement_mutated_so_far(self, tmp_path: Path):
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        target = next(fp for fp in pcb.footprints if fp.reference == "UB")
        target.rotation = 180.0

        loop = PlacementDeltaFeedbackLoop(
            router=RoutedFakeRouter([], 0, lambda _r: []), pcb=pcb, verbose=False
        )
        view = loop._routed_pcb_view()

        assert view is not None
        assert next(fp for fp in view.footprints if fp.reference == "UB").rotation == 180.0

    def test_view_degrades_to_the_board_when_nothing_is_routed(self, tmp_path: Path):
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        loop = PlacementDeltaFeedbackLoop(
            router=FakeRouter([], 0, lambda _r: []), pcb=pcb, verbose=False
        )
        # FakeRouter has no routes -> nothing to merge -> pre-#4468 behaviour.
        assert loop._routed_pcb_view() is pcb

    def test_proposer_classifies_the_view_and_still_emits_mirror(self, tmp_path: Path):
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        loop = PlacementDeltaFeedbackLoop(
            router=RoutedFakeRouter([], 0, lambda _r: []), pcb=pcb, verbose=False
        )
        deltas = loop._propose_deltas()
        # #4560: the reversed bundle's proposal is a mirror (layer flip), not
        # the chirality-preserving rotate_180.
        mirror = [d for d in deltas if d.kind == "mirror"]
        assert mirror, f"expected a mirror delta, got {[d.kind for d in deltas]}"
        assert mirror[0].target_ref == "UB"

    def test_excluded_nets_are_not_diagnosed(self, tmp_path: Path):
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        loop = PlacementDeltaFeedbackLoop(
            router=RoutedFakeRouter([], 0, lambda _r: []),
            pcb=pcb,
            verbose=False,
            excluded_nets={"DQ2", "DQ0", "DQ1"},
        )
        assert loop._propose_deltas() == []


# --------------------------------------------------------------------------- #
# 2. Baseline reuse                                                            #
# --------------------------------------------------------------------------- #


class TestBaselineReuse:
    def _loop(self, reuse: bool):
        pads = [FakePad(12.0, 10.0, "UB", "2")]
        router = FakeRouter(pads, 2, lambda _r: [])
        router.routes = [object()]  # a completed routing pass already happened
        loop = PlacementDeltaFeedbackLoop(
            router=router,
            pcb=MockPCB([MockFootprint("UB", 10.0, 10.0)]),
            verbose=False,
            delta_proposer=lambda _pcb: [],
        )
        return loop, router

    def test_reuse_skips_the_duplicate_baseline_reroute(self):
        loop, router = self._loop(reuse=True)
        loop.run_delta(max_adjustments=1, reuse_existing_routes=True)
        assert router.route_calls == 0

    def test_default_still_routes_the_baseline(self):
        loop, router = self._loop(reuse=False)
        loop.run_delta(max_adjustments=1)
        assert router.route_calls == 1

    def test_reuse_with_no_existing_routes_still_routes(self):
        pads = [FakePad(12.0, 10.0, "UB", "2")]
        router = FakeRouter(pads, 2, lambda _r: [])
        assert router.routes == []
        loop = PlacementDeltaFeedbackLoop(
            router=router,
            pcb=MockPCB([MockFootprint("UB", 10.0, 10.0)]),
            verbose=False,
            delta_proposer=lambda _pcb: [],
        )
        loop.run_delta(max_adjustments=1, reuse_existing_routes=True)
        assert router.route_calls == 1


# --------------------------------------------------------------------------- #
# 2b. Revert also restores the ROUTING GRID                                     #
# --------------------------------------------------------------------------- #


class GridFakeRouter(FakeRouter):
    """``FakeRouter`` with the grid internals the real ``Autorouter`` exposes."""

    def __init__(self, pads, total_nets, failed_predicate):
        super().__init__(pads, total_nets, failed_predicate)
        self.grid_marks: list = []

    def _reset_for_new_trial(self):
        self.grid_marks = []
        self.routes = []

    def _mark_route(self, route):
        self.grid_marks.append(route)


class TestRevertRestoresGrid:
    def test_reverted_delta_rebuilds_the_grid_from_the_restored_routes(self):
        """A revert must leave the grid describing the routes the caller keeps.

        The CLI's post-loop stages (optimize, DRC nudge, pre-save clearance)
        read ``router.grid``.  Measured on board-07: a stale grid cost a net
        after optimize even though the routes themselves were restored.
        """
        pads = [FakePad(12.0, 10.0, "UB", "2")]
        router = GridFakeRouter(pads, 2, _rotate_never_helps)
        baseline = [object()]
        router.routes = list(baseline)

        loop = PlacementDeltaFeedbackLoop(
            router=router,
            pcb=MockPCB([MockFootprint("UB", 10.0, 10.0)]),
            verbose=False,
            delta_proposer=lambda _pcb: [
                PlacementDelta(
                    net_name="DQ2", target_ref="UB", kind="rotate_180", rotation_delta=180.0
                )
            ],
        )
        result = loop.run_delta(max_adjustments=1, reuse_existing_routes=True)

        assert result.exit_reason == "pd_reverted"
        assert result.applied_deltas == []
        # Grid rebuilt from exactly the restored routes, and the routes survive
        # the ``_reset_for_new_trial`` that rebuilding performs.
        assert router.grid_marks == router.routes
        assert len(router.routes) == len(baseline)


def _rotate_never_helps(_router) -> list[int]:
    return [2]


# --------------------------------------------------------------------------- #
# 2c. A reverted probe advances down the ladder                                 #
# --------------------------------------------------------------------------- #


class TestCandidateLadder:
    """The classifier ranks a LADDER; the top rung failing says nothing about
    the rungs below it.  A reverted probe must therefore advance to the next
    UNPROBED move while budget remains -- under the same keep-if-improves
    guarantee, so a wider search can cost wall clock but never reach."""

    def _pads(self):
        return [FakePad(12.0, 10.0, "UA", "1"), FakePad(20.0, 10.0, "UB", "2")]

    def _loop(self, deltas, failed_predicate):
        router = GridFakeRouter(self._pads(), 2, failed_predicate)
        router.routes = [object()]
        loop = PlacementDeltaFeedbackLoop(
            router=router,
            pcb=MockPCB([MockFootprint("UA", 10.0, 10.0), MockFootprint("UB", 22.0, 10.0)]),
            verbose=False,
            delta_proposer=lambda _pcb: list(deltas),
        )
        return loop, router

    def test_second_candidate_is_probed_after_the_first_reverts(self):
        # Net 2 is routed only once UB's pad has moved off x=20.
        def failed(router):
            return [] if abs(router.pads[("UB", "2")].x - 20.0) > 1e-6 else [2]

        first = PlacementDelta(net_name="DQ3", target_ref="UA", kind="translate", dx=1.0, dy=0.0)
        second = PlacementDelta(net_name="DQ4", target_ref="UB", kind="translate", dx=2.0, dy=0.0)
        loop, router = self._loop([first, second], failed)

        result = loop.run_delta(max_adjustments=3, reuse_existing_routes=True)

        assert [d.target_ref for d in result.applied_deltas] == ["UB"]
        assert result.exit_reason != "pd_no_delta"
        # UA's probe was reverted, so its footprint is back where it started.
        assert next(f for f in loop.pcb.footprints if f.reference == "UA").position == (10.0, 10.0)

    def test_identical_moves_are_probed_once(self):
        # Board-07's DQ3 and DQ4 both propose the SAME 180-degree rotation of
        # U2; the second is not a new experiment.
        dup = [
            PlacementDelta(net_name=net, target_ref="UB", kind="rotate_180", rotation_delta=180.0)
            for net in ("DQ3", "DQ4")
        ]
        loop, router = self._loop(dup, _rotate_never_helps)

        result = loop.run_delta(max_adjustments=5, reuse_existing_routes=True)

        assert result.applied_deltas == []
        assert result.exit_reason == "pd_reverted"
        # One probe: one re-route, not five.
        assert router.route_calls == 1

    def test_reverted_probes_carry_their_measurement(self, tmp_path: Path):
        """A run that keeps nothing must still say WHAT it tried and what it measured.

        Without this a plateau claim is unfalsifiable: "0 kept" reads the same
        whether the loop probed four placements or never ran.
        """
        import json

        from kicad_tools.router import write_placement_delta_json

        delta = PlacementDelta(
            net_name="DQ3", target_ref="UB", kind="rotate_180", rotation_delta=180.0
        )
        loop, router = self._loop([delta], _rotate_never_helps)
        result = loop.run_delta(max_adjustments=2, reuse_existing_routes=True)

        assert [d.target_ref for d in result.reverted_deltas] == ["UB"]
        assert result.reverted_counts == [(1, 1)]
        assert "reverted: UB rotate_180" in result.summary()

        out = tmp_path / "board_routed_placement_delta.json"
        write_placement_delta_json(
            out, result.applied_deltas, result.proposed_deltas, result.reverted_evidence()
        )
        payload = json.loads(out.read_text())
        assert payload["applied"] == []
        assert payload["reverted"][0]["target_ref"] == "UB"
        assert payload["reverted"][0]["routed_before"] == 1
        assert payload["reverted"][0]["routed_after"] == 1

    def test_a_reach_gain_that_costs_clearance_is_rejected(self, monkeypatch):
        """Reach alone is the wrong acceptance test for a placement change.

        Measured on board-07: a kept 2 mm translate bought one net
        (26/31 -> 27/31) while pushing two DDR-channel clearances under the
        jlcpcb floor and breaking the ADDR_BUS length match -- a trade the
        routed-DRC allowlist would have had to be WIDENED to absorb.
        """

        def improves(router):
            return [] if abs(router.pads[("UB", "2")].x - 20.0) > 1e-6 else [2]

        delta = PlacementDelta(net_name="DQ3", target_ref="UB", kind="translate", dx=2.0, dy=0.0)
        loop, router = self._loop([delta], improves)

        # Clearance count climbs the moment the delta lands.
        counts = iter([4, 6])
        monkeypatch.setattr(
            PlacementDeltaFeedbackLoop,
            "_clearance_violation_count",
            lambda _self: next(counts, 6),
        )

        result = loop.run_delta(max_adjustments=1, reuse_existing_routes=True)

        assert result.applied_deltas == [], "a DRC-regressing delta must not be kept"
        assert result.reverted_reasons == ["clearance violations 4 -> 6"]
        # Reverted atomically despite the reach gain.
        assert next(f for f in loop.pcb.footprints if f.reference == "UB").position == (
            22.0,
            10.0,
        )
        assert router.pads[("UB", "2")].x == pytest.approx(20.0)

    def test_the_guard_can_be_turned_off(self, monkeypatch):
        def improves(router):
            return [] if abs(router.pads[("UB", "2")].x - 20.0) > 1e-6 else [2]

        delta = PlacementDelta(net_name="DQ3", target_ref="UB", kind="translate", dx=2.0, dy=0.0)
        loop, router = self._loop([delta], improves)
        monkeypatch.setattr(
            PlacementDeltaFeedbackLoop, "_clearance_violation_count", lambda _self: 99
        )

        result = loop.run_delta(
            max_adjustments=1,
            reuse_existing_routes=True,
            require_no_clearance_regression=False,
        )
        assert [d.target_ref for d in result.applied_deltas] == ["UB"]

    def test_unmeasurable_clearance_does_not_block_a_keep(self, monkeypatch):
        """A router that cannot report clearances disables the guard, not the loop."""

        def improves(router):
            return [] if abs(router.pads[("UB", "2")].x - 20.0) > 1e-6 else [2]

        delta = PlacementDelta(net_name="DQ3", target_ref="UB", kind="translate", dx=2.0, dy=0.0)
        loop, router = self._loop([delta], improves)
        monkeypatch.setattr(
            PlacementDeltaFeedbackLoop, "_clearance_violation_count", lambda _self: None
        )

        result = loop.run_delta(max_adjustments=1, reuse_existing_routes=True)
        assert [d.target_ref for d in result.applied_deltas] == ["UB"]

    def test_budget_one_preserves_stop_at_first_revert(self):
        first = PlacementDelta(net_name="DQ3", target_ref="UA", kind="translate", dx=1.0, dy=0.0)
        second = PlacementDelta(net_name="DQ4", target_ref="UB", kind="translate", dx=2.0, dy=0.0)
        loop, router = self._loop([first, second], _rotate_never_helps)

        result = loop.run_delta(max_adjustments=1, reuse_existing_routes=True)

        assert router.route_calls == 1
        assert result.exit_reason == "pd_reverted"


# --------------------------------------------------------------------------- #
# 2d. Board bounds are compared in the footprints' own frame                    #
# --------------------------------------------------------------------------- #


_OFFSET_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (gr_rect (start 93.5 40) (end 203.5 135) (layer "Edge.Cuts") (width 0.1))
  (net 0 "")
  (net 1 "DQ2")
  (footprint "U_QFN" (layer "F.Cu") (at 113.5 65)
    (property "Reference" "U1")
    (pad "1" smd rect (at 0 0) (size 0.6 0.6) (layers "F.Cu") (net 1 "DQ2"))
  )
)
"""


class TestBoardBoundsFrame:
    """``fp.position`` is board-relative; Edge.Cuts is sheet-absolute.

    Comparing the two directly (the #3861 defect class) made every footprint on
    a board with a non-zero origin read as out of bounds, which silently
    rejected EVERY translate strategy with "unsafe (board bounds)" -- measured
    on board-07, 8/8 footprints falsely outside their own outline.
    """

    def test_bounds_are_board_relative(self, tmp_path: Path):
        from kicad_tools.recovery import StrategyApplicator

        pcb = _load(tmp_path, _OFFSET_BOARD)
        assert pcb.board_origin == (93.5, 40.0)

        bounds = StrategyApplicator()._get_board_bounds(pcb)
        assert bounds is not None
        assert (bounds.min_x, bounds.min_y) == (0.0, 0.0)
        assert (bounds.max_x, bounds.max_y) == (110.0, 95.0)

    def test_translate_on_an_offset_board_is_safe_to_apply(self, tmp_path: Path):
        from kicad_tools.recovery import (
            Action,
            Difficulty,
            ResolutionStrategy,
            StrategyApplicator,
            StrategyType,
        )

        pcb = _load(tmp_path, _OFFSET_BOARD)
        fp = next(f for f in pcb.footprints if f.reference == "U1")
        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.MEDIUM,
            confidence=1.0,
            actions=[
                Action(
                    type="move",
                    target="U1",
                    params={"x": fp.position[0] + 2.0, "y": fp.position[1] - 1.0},
                )
            ],
        )
        assert StrategyApplicator().is_safe_to_apply(strategy, pcb) is True

    def test_a_move_off_the_board_is_still_rejected(self, tmp_path: Path):
        from kicad_tools.recovery import (
            Action,
            Difficulty,
            ResolutionStrategy,
            StrategyApplicator,
            StrategyType,
        )

        pcb = _load(tmp_path, _OFFSET_BOARD)
        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.MEDIUM,
            confidence=1.0,
            actions=[Action(type="move", target="U1", params={"x": -5.0, "y": 20.0})],
        )
        assert StrategyApplicator().is_safe_to_apply(strategy, pcb) is False


# --------------------------------------------------------------------------- #
# 3. CLI wiring: flags + placement persistence                                 #
# --------------------------------------------------------------------------- #


def _args(output: Path, **kw) -> SimpleNamespace:
    base = {
        "output": str(output),
        "placement_delta_feedback": True,
        "placement_delta_feedback_budget": 3,
        "placement_feedback_max_movement": 5.0,
        "placement_feedback_anchor": None,
        "placement_feedback_no_anchor": None,
        "strategy": "negotiated",
        "timeout": None,
        "per_net_timeout": None,
        "skip_nets": "GND,+1V2",
        "verbose": False,
        "quiet": True,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class _StubRouter:
    """Stands in for ``Autorouter`` at the CLI boundary."""

    def __init__(self, applied: list[PlacementDelta], mutate_ref: str | None = None):
        self._applied = applied
        self._mutate_ref = mutate_ref
        self.kwargs: dict = {}

    def get_failed_nets(self):
        return [1]

    def route_with_placement_delta_feedback(self, **kwargs):
        self.kwargs = kwargs
        pcb = kwargs.get("pcb")
        if self._mutate_ref and pcb is not None:
            fp = next(f for f in pcb.footprints if f.reference == self._mutate_ref)
            fp.rotation = (fp.rotation + 180.0) % 360.0
        from kicad_tools.router.placement_feedback import PlacementDeltaFeedbackResult

        return PlacementDeltaFeedbackResult(
            success=False,
            routes=[],
            iterations=1,
            applied_deltas=list(self._applied),
            proposed_deltas=list(self._applied),
            failed_nets=[1],
            exit_reason="pd_converged",
        )


class TestCliPlacementDeltaWiring:
    def test_flag_parses(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["route", "b.kicad_pcb", "--placement-delta-feedback-budget", "2"])
        assert args.placement_delta_feedback is False  # opt-in
        args = parser.parse_args(["route", "b.kicad_pcb", "--placement-delta-feedback"])
        assert args.placement_delta_feedback is True
        assert args.placement_delta_feedback_budget == 3
        args = parser.parse_args(
            ["route", "b.kicad_pcb", "--placement-delta-feedback", "--no-placement-delta-feedback"]
        )
        assert args.placement_delta_feedback is False

    def test_delta_artifact_path_is_beside_the_output(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _placement_delta_path

        out = tmp_path / "board_routed.kicad_pcb"
        assert (
            _placement_delta_path(_args(out), tmp_path / "board.kicad_pcb")
            == tmp_path / "board_routed_placement_delta.json"
        )

    def test_kept_delta_persists_the_moved_placement(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _run_placement_delta_feedback

        src = tmp_path / "board.kicad_pcb"
        src.write_text(_facing_rows_bundle_board(reversed_rows=True))
        out = tmp_path / "board_routed.kicad_pcb"
        router = _StubRouter(
            [
                PlacementDelta(
                    net_name="DQ2", target_ref="UB", kind="rotate_180", rotation_delta=180.0
                )
            ],
            mutate_ref="UB",
        )

        returned = _run_placement_delta_feedback(
            router=router, pcb_path=src, output_path=out, args=_args(out), quiet=True
        )

        assert returned == out, "caller must re-source placement from the moved board"
        assert out.exists()
        saved = PCB.load(out)
        assert next(fp for fp in saved.footprints if fp.reference == "UB").rotation == 180.0
        # The pour/plane nets the router skipped are excluded from classification.
        assert router.kwargs["excluded_nets"] == frozenset({"GND", "+1V2"})
        assert router.kwargs["reuse_existing_routes"] is True
        assert (tmp_path / "board_routed_placement_delta.json").parent.exists()

    def test_no_kept_delta_leaves_the_placement_source_alone(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _run_placement_delta_feedback

        src = tmp_path / "board.kicad_pcb"
        src.write_text(_facing_rows_bundle_board(reversed_rows=True))
        out = tmp_path / "board_routed.kicad_pcb"

        returned = _run_placement_delta_feedback(
            router=_StubRouter([]), pcb_path=src, output_path=out, args=_args(out), quiet=True
        )

        assert returned is None
        assert not out.exists()

    def test_anchors_exclude_connectors(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _run_placement_delta_feedback

        src = tmp_path / "board.kicad_pcb"
        src.write_text(_facing_rows_bundle_board(reversed_rows=True))
        out = tmp_path / "board_routed.kicad_pcb"
        router = _StubRouter([])
        _run_placement_delta_feedback(
            router=router, pcb_path=src, output_path=out, args=_args(out), quiet=True
        )
        # Auto-anchored connectors never move; the QFN columns stay movable.
        assert "UB" not in router.kwargs["fixed_refs"]

    def test_loop_failure_is_tolerated(self, tmp_path: Path):
        from kicad_tools.cli.route_cmd import _run_placement_delta_feedback

        class _Boom(_StubRouter):
            def route_with_placement_delta_feedback(self, **kwargs):
                raise RuntimeError("classifier exploded")

        src = tmp_path / "board.kicad_pcb"
        src.write_text(_facing_rows_bundle_board(reversed_rows=True))
        out = tmp_path / "board_routed.kicad_pcb"
        assert (
            _run_placement_delta_feedback(
                router=_Boom([]), pcb_path=src, output_path=out, args=_args(out), quiet=True
            )
            is None
        )


@pytest.mark.parametrize("reversed_rows", [True, False])
def test_view_build_is_idempotent(tmp_path: Path, reversed_rows: bool):
    """Repeated view builds neither accumulate copper nor mutate the board."""
    pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=reversed_rows))
    loop = PlacementDeltaFeedbackLoop(
        router=RoutedFakeRouter([], 0, lambda _r: []), pcb=pcb, verbose=False
    )
    first = loop._routed_pcb_view()
    second = loop._routed_pcb_view()
    assert first is not None and second is not None
    assert len(first.segments) == len(second.segments) == 1
