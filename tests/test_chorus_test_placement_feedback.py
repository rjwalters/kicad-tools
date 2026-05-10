"""Integration regression test for the chorus-test placement-feedback pipeline.

This module is the nightly-CI safety net for Issue #2604 / PR #2609,
which fixed two latent bugs in the closed-loop placement-routing
feedback path:

1. ``BlockingElement.ref`` was always ``None`` because ``GridCell`` had
   no ``ref`` field.  ``RoutingGrid.find_pad_ref_at`` (added by #2609)
   recovers the owning component reference via a spatial lookup so the
   strategy generator's ``if blocker.ref is None: continue`` short-circuit
   no longer drops every candidate.

2. ``StrategyGenerator._find_move_candidates`` ignored the ``max_movement``
   budget and emitted corridor-derived offsets (often 20-30mm) that the
   loop's ``_strategy_within_movement_budget`` filter then rejected
   wholesale.  PR #2609 threads ``max_movement`` through so candidates
   are inside the loop's budget by construction.

A pure-mock test (see ``tests/test_placement_feedback.py``) cannot
exercise either fix because it does not populate a real
``RoutingGrid``.  The synthetic miniature here uses a real
``Autorouter`` with hand-placed ``Pad`` objects forming a deliberate
``BLOCKED_PATH`` topology so the analyzer -> generator -> filter ->
applicator path runs end-to-end.

Two test classes are provided:

* ``TestPlacementFeedbackOnSyntheticBlockedPath`` (always runs nightly):
  builds a 30 x 30mm board with three SSOP-pad clusters arranged so a
  signal net's pad-pair can only route by displacing the middle
  cluster.  Asserts that the feedback loop fires a
  ``MOVE_COMPONENT``/``MOVE_MULTIPLE`` strategy, records at least one
  ``PlacementDiffEntry``, and emits the verbose "Applying strategy:
  move_component" log line that proves the pipeline reached the
  applicator stage.  Each assertion carries a dedicated message that
  distinguishes the four pre-#2604 failure modes -- ref-population,
  candidate-generation, movement-budget filter, board-bounds filter --
  so a regression is loudly attributable.

* ``TestPlacementFeedbackOnRealChorusTest`` (skip-if-missing):
  drives the ``kct route --placement-feedback`` CLI against the real
  chorus-test-revA fixture under ``boards/external/``.  The fixture
  is not vendored into the repo (size + license); the test skips
  cleanly when absent so the slow-tests CI runner just records a
  skip while a developer/self-hosted runner that has vendored the
  board files exercises the full board-scale gate.

Both classes are marked ``@pytest.mark.slow`` so PR-time CI excludes
them (``-m "not slow"``) and the nightly workflow at
``.github/workflows/slow-tests.yml`` (``-m slow``) picks them up
automatically.  No workflow change is required.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

import pytest

from kicad_tools.recovery import StrategyType
from kicad_tools.router.core import Autorouter
from kicad_tools.router.layers import Layer, LayerStack
from kicad_tools.router.rules import DesignRules

# ---------------------------------------------------------------------------
# Minimal PCB stand-in.
#
# ``StrategyApplicator`` and ``StrategyGenerator`` iterate ``pcb.footprints``
# looking for ``reference`` and ``position`` attributes and a
# ``pcb.graphic_items`` list for board-bounds detection.  A trivial pair of
# dataclass-ish stand-ins is enough; see the ``MockPCB`` pattern in
# ``tests/test_placement_feedback.py``.
# ---------------------------------------------------------------------------


class _StubFootprint:
    """Footprint stand-in: just ``reference`` and a mutable ``position``."""

    def __init__(self, reference: str, x: float, y: float):
        self.reference = reference
        self.position = (x, y)
        self.rotation = 0.0
        self.locked = False
        # The strategy applicator reads .pads on the footprint when
        # repositioning groups; an empty list keeps it inert.
        self.pads: list = []


class _StubEdgeCut:
    """Edge-cut graphic item used by ``_get_board_bounds``."""

    def __init__(self, start: tuple[float, float], end: tuple[float, float]):
        # ``_get_board_bounds`` matches ``"Edge"`` in ``str(item.layer)``.
        self.layer = "Edge.Cuts"
        self.start = start
        self.end = end


class _StubPCB:
    """PCB stand-in: ``footprints`` + ``graphic_items`` are all that
    ``StrategyApplicator`` / ``StrategyGenerator`` read on this code path.
    """

    def __init__(
        self,
        footprints: list[_StubFootprint],
        edge_cuts: list[_StubEdgeCut],
    ):
        self.footprints = list(footprints)
        self.graphic_items = list(edge_cuts)
        # The remaining fields are touched only by the broader CLI path,
        # not by ``PlacementFeedbackLoop`` -- leave them empty.
        self.segments: list = []
        self.vias: list = []
        self.zones: list = []
        self.layers: dict = {}
        self.nets: dict = {}


# ---------------------------------------------------------------------------
# Synthetic miniature -- always runs.
# ---------------------------------------------------------------------------


def _build_blocked_path_scenario() -> tuple[Autorouter, _StubPCB]:
    """Build a 30 x 30mm board with a deliberately BLOCKED_PATH topology.

    Layout (all on F.Cu):

        U1 .......... U2     <- signal net SIG1 needs U1.1 -> U2.1
                |
                U3
                (obstructing cluster)

    U3 is a small SMD package planted directly in the routing corridor
    between U1 and U2 with pads that completely span the corridor on
    BOTH copper layers (so via-escape can't bypass it).  U1 and U2 are
    on the edges of the board.

    The analyzer must:
    * detect U3 (the cluster of SMD pads) as a movable BLOCKED_PATH
      blocker,
    * via ``find_pad_ref_at`` recover ``blocker.ref == "U3"``,
    * pass U3 through the generator's ``max_movement``-aware
      ``_find_move_candidates`` to produce in-budget move offsets,
    * survive the loop's ``_strategy_within_movement_budget`` filter
      so a MOVE_COMPONENT strategy reaches the applicator.

    Returns:
        Tuple of (router, pcb) ready to drive ``route_with_placement_feedback``.
    """
    rules = DesignRules(
        trace_width=0.2,
        trace_clearance=0.15,
        grid_resolution=0.1,
    )
    router = Autorouter(
        width=30.0,
        height=30.0,
        rules=rules,
        layer_stack=LayerStack.two_layer(),
    )

    # U1 -- single pad at left edge of corridor.
    router.add_component(
        "U1",
        [
            {
                "number": "1",
                "x": 4.0,
                "y": 15.0,
                "width": 0.8,
                "height": 0.8,
                "net": 1,
                "net_name": "SIG1",
                "layer": Layer.F_CU,
            },
        ],
    )

    # U2 -- single pad at right edge of corridor (same net).
    router.add_component(
        "U2",
        [
            {
                "number": "1",
                "x": 26.0,
                "y": 15.0,
                "width": 0.8,
                "height": 0.8,
                "net": 1,
                "net_name": "SIG1",
                "layer": Layer.F_CU,
            },
        ],
    )

    # U3 -- obstructing cluster in the middle of the corridor.  A dense
    # column of SMD pads at x=15 spanning the FULL y-extent of the
    # board (1.5 to 28.5) on BOTH copper layers so the corridor is
    # blocked on F.Cu AND B.Cu and the router cannot escape via the
    # top/bottom rails either.  Pad pitch is 1.5mm centre-to-centre
    # with 1.4mm pad width so consecutive clearance envelopes overlap
    # and the column is one continuous wall.
    u3_pads: list[dict] = []
    pin = 1
    y_positions = [1.5 + 1.5 * i for i in range(19)]  # 1.5, 3.0, ..., 28.5
    for y in y_positions:
        for layer in (Layer.F_CU, Layer.B_CU):
            u3_pads.append(
                {
                    "number": str(pin),
                    "x": 15.0,  # dead-centre of the U1->U2 corridor
                    "y": y,
                    "width": 1.4,
                    "height": 1.4,
                    # Distinct (non-SIG1) net so these pads aren't
                    # treated as connection endpoints.
                    "net": 2,
                    "net_name": "U3_BLOCK",
                    "layer": layer,
                }
            )
            pin += 1
    router.add_component("U3", u3_pads)

    # Build a stub PCB so the feedback loop has something to mutate.
    footprints = [
        _StubFootprint("U1", 4.0, 15.0),
        _StubFootprint("U2", 26.0, 15.0),
        _StubFootprint("U3", 15.0, 15.0),
    ]
    edge_cuts = [
        _StubEdgeCut(start=(0.0, 0.0), end=(30.0, 0.0)),
        _StubEdgeCut(start=(30.0, 0.0), end=(30.0, 30.0)),
        _StubEdgeCut(start=(30.0, 30.0), end=(0.0, 30.0)),
        _StubEdgeCut(start=(0.0, 30.0), end=(0.0, 0.0)),
    ]
    pcb = _StubPCB(footprints=footprints, edge_cuts=edge_cuts)

    return router, pcb


@pytest.mark.slow
class TestPlacementFeedbackOnSyntheticBlockedPath:
    """Regression gate for Issue #2604 on a synthetic miniature.

    The synthetic exercises the full analyzer -> generator -> filter ->
    applicator path the chorus-test repro produced and which a pure-mock
    test cannot reach (because no real ``RoutingGrid`` is populated).
    """

    def test_pre_feedback_route_all_leaves_sig1_unrouted(self) -> None:
        """Sanity check: with U3 in the corridor, SIG1 must fail to route.

        If this assertion fails the scenario is no longer a reliable
        regression gate -- something changed about routing capability
        that lets the corridor be threaded without moving U3.  Investigate
        before relaxing.
        """
        router, _pcb = _build_blocked_path_scenario()
        router.route_all()
        failed = router.get_failed_nets()
        assert failed, (
            "Synthetic scenario produced no routing failures; the "
            "corridor between U1/U2 must be blocked by U3 to make this "
            "a regression gate for Issue #2604.  Adjust U3 geometry."
        )

    def test_placement_feedback_fires_move_component_strategy(
        self, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """End-to-end: feedback loop must produce a MOVE_COMPONENT adjustment.

        Each assertion carries a dedicated message identifying which of
        the four pre-#2604 failure modes broke (ref-population,
        candidate-generation, movement-budget, board-bounds) so the
        rejection is loudly attributable.  See
        ``_log_strategy_rejection_breakdown`` at
        ``src/kicad_tools/router/placement_feedback.py:447``.
        """
        router, pcb = _build_blocked_path_scenario()

        result = router.route_with_placement_feedback(
            pcb=pcb,
            max_adjustments=3,
            use_negotiated=False,  # synthetic doesn't need negotiated
            min_confidence=0.5,
            verbose=True,
            max_movement=5.0,
        )

        log = capsys.readouterr().out

        # ----------------------------------------------------------------
        # Assertion 1: total_components_moved -- proves the applicator
        # actually ran (covers fix #1: ref-population) AND that strategies
        # survived the movement-budget filter (covers fix #2).
        # ----------------------------------------------------------------
        assert result.total_components_moved >= 1, (
            "PlacementFeedbackResult.total_components_moved == 0 "
            "(Issue #2604 regression): the feedback loop reached the "
            "applicator zero times.  Diagnose by checking which stage "
            "broke:\n"
            "  (a) zero failure analyses produced => router.analyze_routing_failure returned None\n"
            "  (b) zero movable blockers => find_pad_ref_at didn't populate BlockingElement.ref (PR #2609 grid.py:2070)\n"
            "  (c) zero MOVE_COMPONENT candidates => StrategyGenerator._find_move_candidates dropped them (PR #2609 strategy.py:434)\n"
            "  (d) all candidates over-budget => _strategy_within_movement_budget filtered them (max_movement plumbing)\n"
            f"Loop log:\n{log}"
        )

        # ----------------------------------------------------------------
        # Assertion 2: placement_diff is populated -- proves the
        # PlacementDiffEntry collector saw the move and the loop is
        # emitting a useful diff artifact.
        # ----------------------------------------------------------------
        assert len(result.placement_diff) >= 1, (
            "PlacementFeedbackResult.placement_diff is empty even though "
            f"total_components_moved={result.total_components_moved}.  "
            "The loop applied a strategy but did NOT collect a "
            "PlacementDiffEntry -- check _build_placement_diff / "
            "_snapshot_positions wiring in placement_feedback.py.\n"
            f"Loop log:\n{log}"
        )

        # ----------------------------------------------------------------
        # Assertion 3: at least one adjustment is a MOVE_COMPONENT /
        # MOVE_MULTIPLE strategy.  Catches the case where the loop fired
        # but the strategy was a degenerate fallback (e.g. manual
        # intervention) instead of an actual component move.
        # ----------------------------------------------------------------
        move_adjustments = [
            adj
            for adj in result.adjustments
            if adj.strategy.type in {StrategyType.MOVE_COMPONENT, StrategyType.MOVE_MULTIPLE}
        ]
        assert move_adjustments, (
            "PlacementFeedbackResult.adjustments contains zero "
            "MOVE_COMPONENT or MOVE_MULTIPLE strategies (Issue #2604 "
            "acceptance criterion #2.c).  "
            f"adjustments={[a.strategy.type.value for a in result.adjustments]}.\n"
            f"Loop log:\n{log}"
        )

        # ----------------------------------------------------------------
        # Assertion 4: the verbose log line that announces strategy
        # application appears in stdout.  Mirrors the issue's repro
        # criterion: ``grep "Re-placing"`` (and the in-code analogue
        # ``Applying strategy: move_component`` at line 446 of
        # placement_feedback.py).
        # ----------------------------------------------------------------
        assert (
            "Applying strategy: move_component" in log or "Applying strategy: move_multiple" in log
        ), (
            "Verbose log does NOT contain 'Applying strategy: "
            "move_component' / 'move_multiple' even though "
            f"total_components_moved={result.total_components_moved}.  "
            "The loop bypassed the verbose announcement path -- check "
            "placement_feedback.py:444-448.\n"
            f"Loop log:\n{log}"
        )

        # ----------------------------------------------------------------
        # Assertion 5: the diff artifact is dict-serializable with the
        # expected schema.  ``kct route --placement-feedback`` persists
        # this as ``<output>_placement_diff.json`` (route_cmd.py:796),
        # so a malformed entry would silently corrupt the CI artifact.
        # ----------------------------------------------------------------
        first_entry_dict = result.placement_diff[0].to_dict()
        assert set(first_entry_dict.keys()) == {
            "ref",
            "old_xy",
            "new_xy",
            "rotation_delta",
            "distance_mm",
        }, (
            f"PlacementDiffEntry.to_dict() returned unexpected keys: "
            f"{set(first_entry_dict.keys())}.  The CLI persists this "
            "verbatim as placement_diff.json; a schema drift breaks the "
            "downstream tooling."
        )
        assert first_entry_dict["distance_mm"] >= 0.0

    def test_each_adjustment_respects_max_movement_budget(self) -> None:
        """Every per-iteration adjustment must satisfy distance <= max_movement.

        This is the budget-aware guarantee from PR #2609's second fix:
        ``_find_move_candidates`` now generates candidates inside the
        loop's ``max_movement`` cap.  If a regression re-introduces
        out-of-budget candidates the filter might still drop them
        (good), but if the filter and generator drift apart this guard
        catches it.

        Note: ``placement_diff`` records the CUMULATIVE displacement
        across all iterations and CAN exceed ``max_movement`` (the
        budget is per-move, not cumulative).  This test checks the
        per-step ``adjustments`` instead.
        """
        router, pcb = _build_blocked_path_scenario()

        max_movement = 5.0
        result = router.route_with_placement_feedback(
            pcb=pcb,
            max_adjustments=3,
            use_negotiated=False,
            min_confidence=0.5,
            verbose=False,
            max_movement=max_movement,
        )

        # Skip if no moves happened -- the main test already asserts
        # at least one move; this test is purely about the budget.
        if not result.adjustments:
            pytest.skip("No adjustments to budget-check.")

        # For each per-iteration adjustment, inspect every ``move``
        # action in the strategy and confirm the proposed new position
        # is within ``max_movement`` of the position the loop saw at
        # decision time (i.e. the original-or-previously-applied
        # position).  Since the test's PCB state mutates between
        # iterations and the loop's adjustments record the strategy
        # state at decision time, we re-derive the per-step pre-move
        # position from the strategy's affected components by tracing
        # adjustments in order.
        positions: dict[str, tuple[float, float]] = {
            fp.reference: fp.position for fp in pcb.footprints
        }
        # Walk adjustments backwards to restore the original positions
        # the loop saw before applying any of them.  ``placement_diff``
        # records (old_xy, new_xy) for each ref; ``old_xy`` is the
        # original position before the FIRST move.
        diff_old: dict[str, tuple[float, float]] = {
            entry.ref: entry.old_xy for entry in result.placement_diff
        }
        for ref, old_xy in diff_old.items():
            positions[ref] = old_xy

        for adjustment in result.adjustments:
            for action in adjustment.strategy.actions:
                if action.type != "move":
                    continue
                ref = action.target
                new_x = action.params.get("x")
                new_y = action.params.get("y")
                if new_x is None or new_y is None:
                    continue
                if ref not in positions:
                    continue
                old_x, old_y = positions[ref]
                step_distance = math.hypot(new_x - old_x, new_y - old_y)
                assert step_distance <= max_movement + 1e-6, (
                    f"Per-iteration adjustment for {ref} (iter "
                    f"{adjustment.iteration}) moved {step_distance:.4f}mm "
                    f"which exceeds the per-step max_movement budget of "
                    f"{max_movement}mm (Issue #2604: "
                    "budget-aware _find_move_candidates must not emit "
                    f"out-of-budget candidates).  pre=({old_x}, {old_y}), "
                    f"new=({new_x}, {new_y})"
                )
                # Advance the tracked position so the next adjustment
                # for the same ref is measured against the post-move
                # state, mirroring the loop's per-step semantics.
                positions[ref] = (new_x, new_y)


# ---------------------------------------------------------------------------
# Real chorus-test-revA gate -- skipped if the board fixture is absent.
# ---------------------------------------------------------------------------


def _chorus_test_pcb_path() -> Path:
    """Return the expected path to the chorus-test-revA PCB fixture.

    Mirrors the path baked into the benchmark suite at
    ``src/kicad_tools/benchmark/cases.py:103``.  We resolve relative to
    the repository root (this file is two levels under it).
    """
    return (
        Path(__file__).resolve().parent.parent
        / "boards"
        / "external"
        / "chorus-test-revA"
        / "kicad"
        / "chorus-test-revA_v9_grid50.kicad_pcb"
    )


@pytest.mark.slow
class TestPlacementFeedbackOnRealChorusTest:
    """Real-board placement-feedback gate.

    Skipped when ``boards/external/chorus-test-revA`` is not vendored
    locally (the typical state on CI runners).  When the fixture IS
    present, drives the ``kct route --placement-feedback`` CLI
    end-to-end and asserts the emitted ``*_placement_diff.json`` is
    populated.
    """

    @pytest.fixture
    def chorus_test_pcb(self) -> Path:
        """Resolve the chorus-test-revA PCB path or skip the test cleanly."""
        path = _chorus_test_pcb_path()
        if not path.exists():
            pytest.skip(
                f"chorus-test-revA fixture not vendored at {path}; "
                "skipping real-board placement-feedback gate.  Set up "
                "the board files locally (or on a self-hosted runner) "
                "to exercise this test."
            )
        return path

    def test_placement_diff_artifact_is_populated(
        self,
        chorus_test_pcb: Path,
        tmp_path: Path,
    ) -> None:
        """Driving the CLI with --placement-feedback emits a non-empty diff.

        Runs the route command as if a human had invoked it from the
        shell -- via ``route_cmd.main`` so the helper layer (anchor
        resolution, ``_placement_diff_path``, JSON serialization) is
        all exercised.

        Asserts:
        * The diff file exists at the expected path beside the routed PCB.
        * The diff list contains at least one entry with non-zero
          ``distance_mm`` (proves an actual move, not an empty file
          written-to-signal-feedback-ran).
        """
        from kicad_tools.cli import route_cmd

        output_pcb = tmp_path / "chorus_test_routed.kicad_pcb"
        # 1800s timeout matches the issue's repro command.  This test
        # runs only on nightly CI / dev workstations that have the
        # fixture vendored, never on the default-CI runner.
        argv = [
            str(chorus_test_pcb),
            "-o",
            str(output_pcb),
            "--strategy",
            "negotiated",
            "--layers",
            "4",
            "--manufacturer",
            "jlcpcb-tier1",
            "--no-cache",
            "--timeout",
            "1800",
            "--per-net-timeout",
            "120",
            "--placement-feedback",
        ]

        rc = route_cmd.main(argv)
        # We do not assert rc == 0: the placement-feedback path may
        # still leave some nets unrouted, which returns a non-zero exit
        # code.  We only care that the diff artifact was emitted.
        assert rc in (0, 2, 3), (
            f"route_cmd.main returned unexpected exit code {rc}; "
            "expected 0/2/3 (success or partial)."
        )

        diff_path = output_pcb.with_suffix("").with_name(output_pcb.stem + "_placement_diff.json")
        assert diff_path.exists(), (
            f"--placement-feedback did not emit {diff_path}; check "
            "route_cmd._run_placement_feedback / _placement_diff_path."
        )

        diff_data = json.loads(diff_path.read_text())
        assert isinstance(diff_data, list), (
            f"placement_diff.json must be a JSON array; got {type(diff_data).__name__}"
        )

        non_trivial = [entry for entry in diff_data if entry.get("distance_mm", 0.0) > 1e-6]
        assert non_trivial, (
            "placement_diff.json contains zero entries with non-zero "
            "distance_mm.  Either the feedback loop did not fire (no "
            "blocker classified as movable -- Issue #2604 regression) "
            "or every emitted diff is a no-op move.  Diff contents: "
            f"{diff_data!r}"
        )

        # Also verify the schema on at least one entry so a future
        # CLI refactor that drops a field is caught here.
        sample = non_trivial[0]
        for key in ("ref", "old_xy", "new_xy", "distance_mm"):
            assert key in sample, f"placement_diff.json entry missing key {key!r}: {sample!r}"

        # And the move distance must be finite (NaN/inf would slip
        # through json.loads but break downstream tooling).
        assert math.isfinite(sample["distance_mm"])
