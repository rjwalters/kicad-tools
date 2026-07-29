"""Tests for the classifier-driven placement-delta feedback loop (issue #4467).

Phase 2 of the board-07 router<->placement epic (#3438): apply a Phase-1
:class:`~kicad_tools.router.placement_delta.PlacementDelta`, re-route, and keep
the change only on a strict routed-net improvement (else revert atomically).

The driver's keep/revert/skip logic is exercised with a lightweight
:class:`FakeRouter` whose routing outcome is a deterministic function of the
current pad geometry, plus an injected ``delta_proposer`` -- this isolates the
loop's transaction semantics from real routing physics.  A separate test drives
the *real* classifier -> translator pipeline on the reversed-bundle fixture to
confirm the driver's default proposer emits the expected ``rotate_180`` delta.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.recovery import (
    Action,
    Difficulty,
    ResolutionStrategy,
    StrategyApplicator,
    StrategyType,
)
from kicad_tools.router import (
    PlacementDelta,
    PlacementDeltaFeedbackLoop,
    PlacementDeltaFeedbackResult,
    write_placement_delta_json,
)

# Reuse the Phase-1 reversed-bundle fixture verbatim so the classifier emits a
# genuine DE_REVERSE_BUNDLE -> rotate_180 delta on UB.
from tests.router.test_placement_delta import _facing_rows_bundle_board, _load

# --------------------------------------------------------------------------- #
# Lightweight test doubles                                                     #
# --------------------------------------------------------------------------- #


class MockFootprint:
    def __init__(self, reference: str, x: float, y: float, rotation: float = 0.0):
        self.reference = reference
        self.position = (x, y)
        self.rotation = rotation
        self.pads: list = []


class MockPCB:
    def __init__(self, footprints):
        self.footprints = list(footprints)
        self.graphic_items = []
        self.segments = []
        self.vias = []
        self.zones = []
        self.nets = {}


class FakePad:
    def __init__(self, x: float, y: float, ref: str, pin: str = "1"):
        self.x = x
        self.y = y
        self.ref = ref
        self.pin = pin


class FakeRouter:
    """Minimal Autorouter stand-in for the delta-feedback loop.

    ``failed_predicate(router) -> list[int]`` computes the currently-failing net
    ids from the live pad geometry, so a pad move applied by the loop changes the
    routed count exactly the way a real re-route would.  ``nets`` has ``n + 1``
    entries (net 0 is the no-net sentinel) so ``total_nets == n``.
    """

    def __init__(self, pads, total_nets, failed_predicate):
        self.all_pads = list(pads)
        self.pads = {(p.ref, p.pin): p for p in pads}
        self.nets = {i: [] for i in range(total_nets + 1)}
        self.routes: list = []
        self._failed_predicate = failed_predicate
        self.route_calls = 0

    def _reset_for_new_trial(self):
        self.routes = []

    def route_all_negotiated(self, **kwargs):
        self.route_calls += 1
        # A dummy route object per routed net keeps deep-copy/restore honest.
        self.routes = [object() for _ in range(len(self.nets) - 1 - len(self.get_failed_nets()))]
        return self.routes

    def route_all(self):
        return self.route_all_negotiated()

    def get_failed_nets(self):
        return list(self._failed_predicate(self))


def _ub_pad(router: FakeRouter) -> FakePad:
    return router.pads[("UB", "2")]


def _rotate_fixes_net2(router: FakeRouter) -> list[int]:
    """Net 2 fails while UB's pad "2" sits at its original x (12.0)."""
    return [2] if abs(_ub_pad(router).x - 12.0) < 1e-6 else []


def _rotate_never_helps(_router: FakeRouter) -> list[int]:
    return [2]


def _make_loop(footprint_refs, pads, total_nets, failed_predicate, proposer, **kw):
    fps = [MockFootprint(ref, x, y) for ref, x, y in footprint_refs]
    pcb = MockPCB(fps)
    router = FakeRouter(pads, total_nets, failed_predicate)
    loop = PlacementDeltaFeedbackLoop(
        router=router,
        pcb=pcb,
        verbose=False,
        delta_proposer=proposer,
        **kw,
    )
    return loop, router, pcb


def _rotate_delta(ref: str = "UB", net: str = "DQ2") -> PlacementDelta:
    return PlacementDelta(
        net_name=net,
        target_ref=ref,
        kind="rotate_180",
        rotation_delta=180.0,
        source_action="de_reverse_bundle",
    )


# --------------------------------------------------------------------------- #
# Applicator: rotate_180 support (#4467)                                       #
# --------------------------------------------------------------------------- #


class TestApplicatorRotate:
    def test_rotate_component_flips_footprint_rotation(self):
        applicator = StrategyApplicator()
        pcb = MockPCB([MockFootprint("U5", 10.0, 20.0, rotation=0.0)])
        strategy = ResolutionStrategy(
            type=StrategyType.ROTATE_COMPONENT,
            difficulty=Difficulty.MEDIUM,
            confidence=1.0,
            actions=[Action(type="rotate", target="U5", params={"rotation_delta": 180.0})],
        )
        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is True
        assert result.components_moved == ["U5"]
        assert pcb.footprints[0].rotation == 180.0
        # Rotation keeps the centre fixed.
        assert pcb.footprints[0].position == (10.0, 20.0)

    def test_rotate_wraps_modulo_360(self):
        applicator = StrategyApplicator()
        pcb = MockPCB([MockFootprint("U5", 0.0, 0.0, rotation=270.0)])
        strategy = ResolutionStrategy(
            type=StrategyType.ROTATE_COMPONENT,
            difficulty=Difficulty.MEDIUM,
            confidence=1.0,
            actions=[Action(type="rotate", target="U5", params={"rotation_delta": 180.0})],
        )
        applicator.apply_strategy(pcb, strategy)
        assert pcb.footprints[0].rotation == 90.0  # (270 + 180) % 360

    def test_rotate_missing_component_fails(self):
        applicator = StrategyApplicator()
        pcb = MockPCB([])
        strategy = ResolutionStrategy(
            type=StrategyType.ROTATE_COMPONENT,
            difficulty=Difficulty.MEDIUM,
            confidence=1.0,
            actions=[Action(type="rotate", target="U5", params={"rotation_delta": 180.0})],
        )
        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is False

    def test_rotate_is_safe_to_apply(self):
        applicator = StrategyApplicator()
        pcb = MockPCB([MockFootprint("U5", 10.0, 20.0)])
        strategy = ResolutionStrategy(
            type=StrategyType.ROTATE_COMPONENT,
            difficulty=Difficulty.MEDIUM,
            confidence=1.0,
            actions=[Action(type="rotate", target="U5", params={"rotation_delta": 180.0})],
        )
        assert applicator.is_safe_to_apply(strategy, pcb) is True
        # A rotation targeting a missing ref is not safe.
        assert applicator.is_safe_to_apply(strategy, MockPCB([])) is False


# --------------------------------------------------------------------------- #
# PlacementDelta.from_dict round-trip (#4467)                                  #
# --------------------------------------------------------------------------- #


class TestPlacementDeltaRoundTrip:
    def test_rotate_delta_round_trips(self):
        d = _rotate_delta()
        assert PlacementDelta.from_dict(d.to_dict()) == d

    def test_translate_delta_round_trips(self):
        d = PlacementDelta(
            net_name="SDA",
            target_ref="U3",
            kind="translate",
            dx=1.25,
            dy=-0.5,
            source_action="move_part",
            rationale="crowded",
            confidence="medium",
        )
        assert PlacementDelta.from_dict(d.to_dict()) == d

    def test_from_dict_tolerates_missing_optional_keys(self):
        d = PlacementDelta.from_dict({"net_name": "N", "target_ref": "U1", "kind": "rotate_180"})
        assert d.rotation_delta == 0.0
        assert d.confidence == ""


# --------------------------------------------------------------------------- #
# Driver: keep / revert semantics                                             #
# --------------------------------------------------------------------------- #


class TestDeltaFeedbackKeepRevert:
    def test_applied_rotate_that_improves_reach_is_kept(self):
        # UB pad "2" starts at x=12; rotating UB 180 about its centre (10,10)
        # point-reflects it to x=8, which the predicate reads as "net 2 routed".
        loop, router, pcb = _make_loop(
            footprint_refs=[("UB", 10.0, 10.0)],
            pads=[FakePad(12.0, 10.0, "UB", "2")],
            total_nets=2,
            failed_predicate=_rotate_fixes_net2,
            proposer=lambda _pcb: [_rotate_delta()],
        )
        result = loop.run_delta(max_adjustments=3)
        assert isinstance(result, PlacementDeltaFeedbackResult)
        assert result.success is True
        assert [d.target_ref for d in result.applied_deltas] == ["UB"]
        # Placement change kept: footprint rotated and pad reflected.
        assert pcb.footprints[0].rotation == 180.0
        assert _ub_pad(router).x == pytest.approx(8.0)
        # Diff records the kept rotation.
        assert any(e.ref == "UB" and abs(e.rotation_delta) == 180.0 for e in result.placement_diff)

    def test_non_improving_delta_is_reverted_atomically(self):
        loop, router, pcb = _make_loop(
            footprint_refs=[("UB", 10.0, 10.0)],
            pads=[FakePad(12.0, 10.0, "UB", "2")],
            total_nets=2,
            failed_predicate=_rotate_never_helps,
            proposer=lambda _pcb: [_rotate_delta()],
        )
        result = loop.run_delta(max_adjustments=3)
        assert result.applied_deltas == []
        assert result.exit_reason == "pd_reverted"
        # Placement fully restored: rotation back to 0, pad back to x=12.
        assert pcb.footprints[0].rotation == 0.0
        assert _ub_pad(router).x == pytest.approx(12.0)
        # Routed count unchanged (net 2 still failing).
        assert result.failed_nets == [2]
        # No spurious diff entry for a reverted move.
        assert result.placement_diff == []


# --------------------------------------------------------------------------- #
# Driver: anchor / budget / kind skips (with logged reason)                    #
# --------------------------------------------------------------------------- #


class TestDeltaFeedbackSkips:
    def test_anchored_target_is_skipped_with_reason(self):
        loop, router, pcb = _make_loop(
            footprint_refs=[("UB", 10.0, 10.0)],
            pads=[FakePad(12.0, 10.0, "UB", "2")],
            total_nets=2,
            failed_predicate=_rotate_fixes_net2,
            proposer=lambda _pcb: [_rotate_delta()],
            fixed_refs={"UB"},
        )
        result = loop.run_delta(max_adjustments=3)
        assert result.applied_deltas == []
        assert result.exit_reason == "pd_no_delta"
        assert result.skipped_deltas and result.skipped_deltas[0].target_ref == "UB"
        assert any("anchored" in r for r in result.skip_reasons)
        # Anchored footprint never rotated.
        assert pcb.footprints[0].rotation == 0.0

    def test_translate_over_max_movement_is_skipped(self):
        far = PlacementDelta(net_name="DQ2", target_ref="UB", kind="translate", dx=10.0, dy=0.0)
        loop, router, pcb = _make_loop(
            footprint_refs=[("UB", 10.0, 10.0)],
            pads=[FakePad(12.0, 10.0, "UB", "2")],
            total_nets=2,
            failed_predicate=_rotate_fixes_net2,
            proposer=lambda _pcb: [far],
            max_movement=5.0,
        )
        result = loop.run_delta(max_adjustments=3)
        assert result.applied_deltas == []
        assert result.exit_reason == "pd_no_delta"
        assert any("max_movement" in r for r in result.skip_reasons)
        # Pad untouched.
        assert _ub_pad(router).x == pytest.approx(12.0)

    def test_reorder_pins_delta_is_skipped(self):
        reorder = PlacementDelta(net_name="DQ2", target_ref="UB", kind="reorder_pins")
        loop, router, pcb = _make_loop(
            footprint_refs=[("UB", 10.0, 10.0)],
            pads=[FakePad(12.0, 10.0, "UB", "2")],
            total_nets=2,
            failed_predicate=_rotate_fixes_net2,
            proposer=lambda _pcb: [reorder],
        )
        result = loop.run_delta(max_adjustments=3)
        assert result.applied_deltas == []
        assert any("reorder_pins" in r for r in result.skip_reasons)

    def test_ladder_omission_no_delta_applies_nothing(self):
        # Classifier suppressed every move (empty proposal) -> driver is a no-op.
        loop, router, pcb = _make_loop(
            footprint_refs=[("UB", 10.0, 10.0)],
            pads=[FakePad(12.0, 10.0, "UB", "2")],
            total_nets=2,
            failed_predicate=_rotate_fixes_net2,
            proposer=lambda _pcb: [],
        )
        result = loop.run_delta(max_adjustments=3)
        assert result.applied_deltas == []
        assert result.exit_reason == "pd_no_delta"
        assert pcb.footprints[0].rotation == 0.0
        assert _ub_pad(router).x == pytest.approx(12.0)


# --------------------------------------------------------------------------- #
# Real classifier -> translator emits the rotate_180 delta the driver consumes #
# --------------------------------------------------------------------------- #


class TestClassifierEmitsDelta:
    def test_reversed_bundle_default_proposer_emits_rotate_180(self, tmp_path: Path):
        pcb = _load(tmp_path, _facing_rows_bundle_board(reversed_rows=True))
        # A loop with the DEFAULT proposer (classifier -> translator); no router
        # call needed -- we only exercise proposal.
        loop = PlacementDeltaFeedbackLoop(
            router=FakeRouter([], 0, lambda _r: []), pcb=pcb, verbose=False
        )
        deltas = loop._propose_deltas()
        rotate = [d for d in deltas if d.kind == "rotate_180"]
        assert rotate, f"expected a rotate_180 delta, got {[d.kind for d in deltas]}"
        assert rotate[0].target_ref == "UB"
        assert rotate[0].rotation_delta == 180.0


# --------------------------------------------------------------------------- #
# JSON artifact round-trip (#4467)                                             #
# --------------------------------------------------------------------------- #


class TestPlacementDeltaJson:
    def test_written_artifact_round_trips(self, tmp_path: Path):
        loop, router, pcb = _make_loop(
            footprint_refs=[("UB", 10.0, 10.0)],
            pads=[FakePad(12.0, 10.0, "UB", "2")],
            total_nets=2,
            failed_predicate=_rotate_fixes_net2,
            proposer=lambda _pcb: [_rotate_delta()],
        )
        result = loop.run_delta(max_adjustments=3)
        out = tmp_path / "board_routed_placement_delta.json"
        write_placement_delta_json(out, result.applied_deltas, result.proposed_deltas)

        data = json.loads(out.read_text())
        assert [PlacementDelta.from_dict(d) for d in data["applied"]] == result.applied_deltas
        assert [PlacementDelta.from_dict(d) for d in data["proposed"]] == result.proposed_deltas
        # The kept rotate_180 is present in the applied artifact.
        assert data["applied"][0]["kind"] == "rotate_180"
        assert data["applied"][0]["target_ref"] == "UB"


# --------------------------------------------------------------------------- #
# Autorouter entry point: default-on toggle + bisection guard                  #
# --------------------------------------------------------------------------- #


class TestAutorouterEntryPoint:
    def test_toggle_on_runs_delta_loop_and_writes_json(self, tmp_path: Path):
        from kicad_tools.router.core import Autorouter

        router = Autorouter(50, 50)  # no components -> trivially converged
        out = tmp_path / "delta.json"
        result = router.route_with_placement_delta_feedback(
            pcb=None,
            max_adjustments=1,
            verbose=False,
            delta_output_path=str(out),
        )
        assert isinstance(result, PlacementDeltaFeedbackResult)
        assert out.exists()
        payload = json.loads(out.read_text())
        assert payload == {"applied": [], "proposed": []}

    def test_toggle_off_delegates_to_legacy_loop(self, tmp_path: Path):
        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.placement_feedback import PlacementFeedbackResult

        router = Autorouter(50, 50)
        out = tmp_path / "delta.json"
        result = router.route_with_placement_delta_feedback(
            pcb=None,
            max_adjustments=1,
            verbose=False,
            enable_placement_delta_feedback=False,
            delta_output_path=str(out),
        )
        # Bisection guard: legacy result type, no delta artifact written.
        assert isinstance(result, PlacementFeedbackResult)
        assert not out.exists()
