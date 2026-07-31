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
# Real-PCB fixture with a non-180-symmetric rotated-pad footprint (#4518)      #
# --------------------------------------------------------------------------- #

# A footprint (UB) whose pads carry a nonzero ABSOLUTE ``(at x y ANGLE)`` third
# token (45 deg) on an oval/roundrect shape -- so a stale pad angle after a
# footprint rotation is a real geometry error, not a 180-symmetric no-op.  A
# routable net (N1: A<->B) plus a persistently-unroutable net (BOX, boxed in by
# its own copper) let the composed delta loop reach the apply/keep-or-revert
# branch on a REAL ``Autorouter`` instead of short-circuiting on convergence.
_ROTATED_PAD_BOARD = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (general (thickness 1.6))
  (layers (0 "F.Cu" signal) (44 "Edge.Cuts" user))
  (net 0 "")
  (net 1 "N1")
  (net 2 "BOX")
  (footprint "R" (layer "F.Cu") (at 2 10)
    (property "Reference" "A")
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "N1"))
  )
  (footprint "R" (layer "F.Cu") (at 10 10)
    (property "Reference" "B")
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "N1"))
  )
  (footprint "BOX" (layer "F.Cu") (at 10 10)
    (property "Reference" "BX")
    (pad "1" smd rect (at 0 -1.5) (size 1.4 1.4) (layers "F.Cu") (net 2 "BOX"))
    (pad "2" smd rect (at 0 1.5) (size 1.4 1.4) (layers "F.Cu") (net 2 "BOX"))
    (pad "3" smd rect (at -1.5 0) (size 1.4 1.4) (layers "F.Cu") (net 2 "BOX"))
    (pad "4" smd rect (at 1.5 0) (size 1.4 1.4) (layers "F.Cu") (net 2 "BOX"))
  )
  (footprint "chamf" (layer "F.Cu") (at 16 16)
    (property "Reference" "UB")
    (pad "1" smd roundrect (at 0 -0.6 45) (size 0.6 0.3) (layers "F.Cu") (net 0 ""))
    (pad "2" smd roundrect (at 0 0.6 45) (size 0.6 0.3) (layers "F.Cu") (net 0 ""))
  )
)
"""


def _rotated_pad_board(tmp_path: Path):
    """Load ``_ROTATED_PAD_BOARD`` as a real :class:`PCB`."""
    return _load(tmp_path, _ROTATED_PAD_BOARD)


# --------------------------------------------------------------------------- #
# Lightweight test doubles                                                     #
# --------------------------------------------------------------------------- #


class MockPad:
    """Minimal pad stub carrying only the ``.rotation`` the applicator and the
    snapshot/restore path read (issue #4518)."""

    def __init__(self, rotation: float = 0.0):
        self.rotation = rotation


class MockFootprint:
    def __init__(
        self,
        reference: str,
        x: float,
        y: float,
        rotation: float = 0.0,
        pad_rotations: list[float] | None = None,
    ):
        self.reference = reference
        self.position = (x, y)
        self.rotation = rotation
        # Default empty (existing tests): the applicator/snapshot pad loops are
        # no-ops.  Pass ``pad_rotations`` to exercise the #4518 pad-angle sync.
        self.pads: list = [MockPad(r) for r in (pad_rotations or [])]


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

    # ----- #4518: absolute pad-angle sync ---------------------------------- #

    @staticmethod
    def _rotate_strategy(ref: str, delta: float) -> ResolutionStrategy:
        return ResolutionStrategy(
            type=StrategyType.ROTATE_COMPONENT,
            difficulty=Difficulty.MEDIUM,
            confidence=1.0,
            actions=[Action(type="rotate", target=ref, params={"rotation_delta": delta})],
        )

    def test_rotate_bumps_mock_pad_rotations(self):
        """#4518: every pad's ``.rotation`` advances by the same delta as the
        footprint's, in lockstep."""
        applicator = StrategyApplicator()
        fp = MockFootprint("U5", 10.0, 20.0, rotation=0.0, pad_rotations=[45.0, 90.0])
        pcb = MockPCB([fp])
        applicator.apply_strategy(pcb, self._rotate_strategy("U5", 180.0))
        assert fp.rotation == 180.0
        assert [pad.rotation for pad in fp.pads] == [225.0, 270.0]

    def test_rotate_pad_rotation_wraps_modulo_360(self):
        """A pad starting at a nonzero angle wraps past 360 like the footprint."""
        applicator = StrategyApplicator()
        fp = MockFootprint("U5", 0.0, 0.0, rotation=270.0, pad_rotations=[200.0])
        pcb = MockPCB([fp])
        applicator.apply_strategy(pcb, self._rotate_strategy("U5", 180.0))
        assert fp.rotation == 90.0  # (270 + 180) % 360
        assert fp.pads[0].rotation == 20.0  # (200 + 180) % 360

    def test_rotate_tolerates_pad_without_rotation_attr(self):
        """A pad stub lacking ``.rotation`` (older schema / test double) must not
        raise via the ``hasattr`` guard, and a zero-pad footprint is fine too."""
        applicator = StrategyApplicator()

        class _BarePad:  # no ``.rotation`` attribute
            pass

        fp = MockFootprint("U5", 0.0, 0.0, rotation=0.0)
        fp.pads = [_BarePad()]
        pcb = MockPCB([fp])
        result = applicator.apply_strategy(pcb, self._rotate_strategy("U5", 180.0))
        assert result.success is True
        assert fp.rotation == 180.0
        assert not hasattr(fp.pads[0], "rotation")

        empty = MockFootprint("U6", 0.0, 0.0)
        result2 = applicator.apply_strategy(MockPCB([empty]), self._rotate_strategy("U6", 90.0))
        assert result2.success is True
        assert empty.rotation == 90.0

    def test_rotate_syncs_absolute_pad_angles_on_real_pcb(self, tmp_path: Path):
        """#4518 core: on a REAL ``PCB``/``Pad``, rotating UB 180 deg advances
        every pad's absolute ``(at x y ANGLE)`` third token, and the change
        survives a save + reload round-trip (the serialization path #3902 warned
        about).  UB's pads start at 45 deg on a non-180-symmetric roundrect."""
        applicator = StrategyApplicator()
        pcb = _rotated_pad_board(tmp_path)
        ub = next(fp for fp in pcb.footprints if fp.reference == "UB")
        assert [pad.rotation for pad in ub.pads] == [45.0, 45.0]

        result = applicator.apply_strategy(pcb, self._rotate_strategy("UB", 180.0))
        assert result.success is True
        assert ub.rotation == 180.0
        assert [pad.rotation for pad in ub.pads] == [225.0, 225.0]

        # Serialize + reparse: the absolute pad angles must persist (proving the
        # dataclass write reached the S-expression tree, not just memory).
        out = tmp_path / "rotated_out.kicad_pcb"
        pcb.save(str(out))
        from kicad_tools.schema.pcb import PCB

        reloaded = PCB.load(str(out))
        ub2 = next(fp for fp in reloaded.footprints if fp.reference == "UB")
        assert ub2.rotation == 180.0
        assert [pad.rotation for pad in ub2.pads] == [225.0, 225.0]


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

    def test_reverted_delta_restores_pad_rotations(self):
        """#4518 coupled defect: a reverted (non-improving) delta must restore
        each pad's absolute ``.rotation`` -- not only ``fp.rotation``.  Without
        the snapshot/restore fix the pad angles would stay bumped (225) while the
        footprint reverts to 0, leaving the board stale in the *other* direction.
        """
        fps = [MockFootprint("UB", 10.0, 10.0, rotation=0.0, pad_rotations=[45.0, 45.0])]
        pcb = MockPCB(fps)
        router = FakeRouter([FakePad(12.0, 10.0, "UB", "2")], 2, _rotate_never_helps)
        loop = PlacementDeltaFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=False,
            delta_proposer=lambda _pcb: [_rotate_delta()],
        )
        result = loop.run_delta(max_adjustments=3)
        assert result.applied_deltas == []
        assert result.exit_reason == "pd_reverted"
        # Both the footprint AND every pad angle are back to pre-delta values.
        assert pcb.footprints[0].rotation == 0.0
        assert [pad.rotation for pad in pcb.footprints[0].pads] == [45.0, 45.0]

    def test_kept_delta_bumps_pad_rotations(self):
        """#4518: when a rotate delta is KEPT, the applicator's pad-angle bump
        survives through the loop (footprint 180 -> pads +180)."""
        fps = [MockFootprint("UB", 10.0, 10.0, rotation=0.0, pad_rotations=[45.0, 90.0])]
        pcb = MockPCB(fps)
        router = FakeRouter([FakePad(12.0, 10.0, "UB", "2")], 2, _rotate_fixes_net2)
        loop = PlacementDeltaFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=False,
            delta_proposer=lambda _pcb: [_rotate_delta()],
        )
        result = loop.run_delta(max_adjustments=3)
        assert result.success is True
        assert [d.target_ref for d in result.applied_deltas] == ["UB"]
        assert pcb.footprints[0].rotation == 180.0
        assert [pad.rotation for pad in pcb.footprints[0].pads] == [225.0, 270.0]


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


# --------------------------------------------------------------------------- #
# Composed path: real Autorouter + real PCB (#4518 AC integration)             #
# --------------------------------------------------------------------------- #


class TestComposedDeltaFeedbackIntegration:
    """End-to-end: drive ``route_with_placement_delta_feedback`` with a REAL
    ``Autorouter`` and a REAL parsed ``PCB`` (not ``FakeRouter``/``MockPCB``),
    apply a ``rotate_180`` to UB, and confirm the composed apply ->
    ``_apply_delta_to_router_pads`` -> ``_reset_for_new_trial`` grid rebuild ->
    keep-or-revert cycle leaves UB's pad angles consistent with ``fp.rotation``
    -- and that the board round-trips through serialization.  This closes the
    "composed path is never exercised end to end" gap from the issue body.
    """

    @staticmethod
    def _build_router(pcb):
        import math

        from kicad_tools.router.core import Autorouter
        from kicad_tools.router.layers import Layer

        # Bound each per-net A* to a fixed node-expansion budget (issue #3881).
        # The BOX net is deliberately unroutable (boxed in by its own copper);
        # without a cap the C++ pathfinder gives up and falls back to pure-Python
        # A*, which exhaustively scans the grid and blows past CI's 60s per-test
        # timeout (10-100x slower -- the fallback path from the #3438 loop).  A
        # positive ``per_net_iterations`` makes the C++ search give up
        # DETERMINISTICALLY at the cap (returning FAILURE_ITERATION_LIMIT) and
        # tells the loop to SKIP the Python fallback for the capped net -- so BOX
        # still fails (the loop reaches the apply/keep-or-revert branch as
        # intended) but does so in milliseconds, machine-independently.  20k is
        # far more than the short straight N1 net needs to route.
        router = Autorouter(width=20, height=20, per_net_iterations=20_000)
        for fp in pcb.footprints:
            # KiCad applies footprint orientation as a negated angle vs standard
            # CCW math (see router/io.py::route_pcb); mirror that so the router's
            # flat board-frame pads match what the classifier/PCB view expects.
            rot_rad = math.radians(-fp.rotation)
            cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
            pads = []
            for pad in fp.pads:
                px, py = pad.position
                rx = px * cos_r - py * sin_r
                ry = px * sin_r + py * cos_r
                pads.append(
                    {
                        "number": pad.number,
                        "x": fp.position[0] + rx,
                        "y": fp.position[1] + ry,
                        "width": pad.size[0],
                        "height": pad.size[1],
                        "net": pad.net_number,
                        "net_name": pad.net_name,
                        "layer": Layer.F_CU,
                    }
                )
            router.add_component(fp.reference, pads)
        return router

    def test_composed_rotate_preserves_pad_angle_consistency(self, tmp_path: Path):
        from kicad_tools.schema.pcb import PCB

        pcb = _rotated_pad_board(tmp_path)
        router = self._build_router(pcb)

        ub = next(fp for fp in pcb.footprints if fp.reference == "UB")
        fp_before = ub.rotation
        pad_before = [pad.rotation for pad in ub.pads]
        assert pad_before == [45.0, 45.0]

        # The BOX net is boxed in by its own copper and never routes, so the loop
        # does not short-circuit on convergence -- it reaches the apply branch,
        # rotates UB, re-routes, and (since UB is off-net) reverts.
        result = router.route_with_placement_delta_feedback(
            pcb=pcb,
            max_adjustments=2,
            verbose=False,
            delta_proposer=lambda _pcb: [
                PlacementDelta(
                    net_name="BOX",
                    target_ref="UB",
                    kind="rotate_180",
                    rotation_delta=180.0,
                    source_action="de_reverse_bundle",
                )
            ],
        )
        assert isinstance(result, PlacementDeltaFeedbackResult)
        # AC(a): the delta was actually proposed/considered (composed path ran),
        # and the outcome is either a strict improvement or an atomic revert.
        assert len(result.proposed_deltas) >= 1
        assert result.applied_deltas or result.exit_reason == "pd_reverted"

        # AC(b): for EVERY pad, the applied absolute-angle delta equals the
        # applied footprint-rotation delta -- consistent whether kept or reverted.
        fp_delta = (ub.rotation - fp_before) % 360.0
        for pad, before in zip(ub.pads, pad_before, strict=True):
            assert (pad.rotation - before) % 360.0 == fp_delta

        # And the consistency survives serialize + reparse (the KiCad view).
        out = tmp_path / "composed_out.kicad_pcb"
        pcb.save(str(out))
        reloaded = PCB.load(str(out))
        ub2 = next(fp for fp in reloaded.footprints if fp.reference == "UB")
        assert ub2.rotation == ub.rotation
        assert [pad.rotation for pad in ub2.pads] == [pad.rotation for pad in ub.pads]
