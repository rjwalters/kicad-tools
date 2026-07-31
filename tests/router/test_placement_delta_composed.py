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

    def test_proposer_classifies_the_view_and_still_emits_rotate_180(self, tmp_path: Path):
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        loop = PlacementDeltaFeedbackLoop(
            router=RoutedFakeRouter([], 0, lambda _r: []), pcb=pcb, verbose=False
        )
        deltas = loop._propose_deltas()
        rotate = [d for d in deltas if d.kind == "rotate_180"]
        assert rotate, f"expected a rotate_180 delta, got {[d.kind for d in deltas]}"
        assert rotate[0].target_ref == "UB"

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
