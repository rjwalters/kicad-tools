"""
Unit tests for placement-routing feedback loop.

Tests cover:
- StrategyApplicator class
- ApplicationResult dataclass
- PlacementFeedbackLoop class
- PlacementFeedbackResult dataclass
- PlacementAdjustment dataclass
"""

from kicad_tools.recovery import (
    Action,
    ApplicationResult,
    Difficulty,
    Rectangle,
    ResolutionStrategy,
    StrategyApplicator,
    StrategyType,
)
from kicad_tools.router import (
    PlacementAdjustment,
    PlacementFeedbackResult,
    detect_pf_stagnation,
)


class MockFootprint:
    """Mock footprint for testing."""

    def __init__(self, reference: str, x: float, y: float):
        self.reference = reference
        self.position = (x, y)
        self.pads = []


class MockGraphicItem:
    """Mock graphic item for testing board bounds."""

    def __init__(self, layer: str, start: tuple, end: tuple):
        self.layer = layer
        self.start = start
        self.end = end


class MockPCB:
    """Mock PCB for testing."""

    def __init__(self):
        self.footprints = []
        self.graphic_items = []
        self.segments = []
        self.vias = []
        self.zones = []
        self.layers = {}
        self.nets = {}


class TestApplicationResult:
    """Tests for ApplicationResult dataclass."""

    def test_application_result_creation(self):
        """Test creating an ApplicationResult."""
        result = ApplicationResult(
            success=True,
            components_moved=["C1", "C2"],
            message="Moved 2 components",
            conflicts_created=0,
        )
        assert result.success is True
        assert result.components_moved == ["C1", "C2"]
        assert result.message == "Moved 2 components"
        assert result.conflicts_created == 0

    def test_application_result_failure(self):
        """Test ApplicationResult for failed application."""
        result = ApplicationResult(
            success=False,
            components_moved=[],
            message="Component not found",
        )
        assert result.success is False
        assert result.components_moved == []


class TestStrategyApplicator:
    """Tests for StrategyApplicator class."""

    def test_applicator_creation(self):
        """Test creating a StrategyApplicator."""
        applicator = StrategyApplicator()
        assert applicator is not None
        assert applicator.BOARD_EDGE_MARGIN == 1.0
        assert applicator.MAX_MOVE_DISTANCE == 10.0

    def test_apply_move_component_strategy(self):
        """Test applying a single component move strategy."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 20.0))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 15.0, "y": 25.0})],
        )

        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is True
        assert result.components_moved == ["C1"]
        assert "C1" in result.message
        assert pcb.footprints[0].position == (15.0, 25.0)

    def test_apply_move_multiple_strategy(self):
        """Test applying a multi-component move strategy."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 20.0))
        pcb.footprints.append(MockFootprint("C2", 30.0, 40.0))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_MULTIPLE,
            difficulty=Difficulty.HARD,
            confidence=0.7,
            actions=[
                Action(type="move", target="C1", params={"x": 12.0, "y": 22.0}),
                Action(type="move", target="C2", params={"x": 32.0, "y": 42.0}),
            ],
        )

        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is True
        assert set(result.components_moved) == {"C1", "C2"}
        assert pcb.footprints[0].position == (12.0, 22.0)
        assert pcb.footprints[1].position == (32.0, 42.0)

    def test_apply_strategy_component_not_found(self):
        """Test applying strategy for non-existent component."""
        applicator = StrategyApplicator()
        pcb = MockPCB()

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C99", params={"x": 15.0, "y": 25.0})],
        )

        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is False
        assert "not found" in result.message

    def test_apply_non_placement_strategy(self):
        """Test that non-placement strategies are rejected."""
        applicator = StrategyApplicator()
        pcb = MockPCB()

        strategy = ResolutionStrategy(
            type=StrategyType.ADD_VIA,
            difficulty=Difficulty.MEDIUM,
            confidence=0.75,
            actions=[Action(type="add_via", target="CLK", params={"x": 10.0, "y": 10.0})],
        )

        result = applicator.apply_strategy(pcb, strategy)
        assert result.success is False
        assert "cannot be applied to placement" in result.message

    def test_is_safe_to_apply_valid(self):
        """Test safety check for valid strategy."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 50.0, 50.0))
        # Add board outline
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 0.0), (100.0, 0.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (100.0, 0.0), (100.0, 100.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (100.0, 100.0), (0.0, 100.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 100.0), (0.0, 0.0)))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 55.0, "y": 55.0})],
        )

        assert applicator.is_safe_to_apply(strategy, pcb) is True

    def test_is_safe_to_apply_out_of_bounds(self):
        """Test safety check rejects out-of-bounds moves."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 50.0, 50.0))
        # Add board outline
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 0.0), (100.0, 0.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (100.0, 0.0), (100.0, 100.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (100.0, 100.0), (0.0, 100.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 100.0), (0.0, 0.0)))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 150.0, "y": 50.0})],
        )

        assert applicator.is_safe_to_apply(strategy, pcb) is False

    def test_is_safe_to_apply_excessive_distance(self):
        """Test safety check rejects excessive move distances."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 50.0, 50.0))
        # Add large board outline
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 0.0), (200.0, 0.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (200.0, 0.0), (200.0, 200.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (200.0, 200.0), (0.0, 200.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 200.0), (0.0, 0.0)))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            # Move > 10mm (default MAX_MOVE_DISTANCE)
            actions=[Action(type="move", target="C1", params={"x": 100.0, "y": 100.0})],
        )

        assert applicator.is_safe_to_apply(strategy, pcb) is False

    def test_calculate_move_vector(self):
        """Test calculating move vector for a component."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 10.0))

        failure_area = Rectangle(min_x=5.0, min_y=5.0, max_x=15.0, max_y=15.0)

        vector = applicator.calculate_move_vector(pcb, "C1", failure_area)
        assert vector is not None
        dx, dy = vector
        # Component at (10,10), center at (10,10), should get arbitrary direction
        # Just verify we get some non-zero vector
        assert abs(dx) > 0 or abs(dy) > 0

    def test_calculate_move_vector_with_direction(self):
        """Test calculating move vector with specified direction."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 10.0))

        failure_area = Rectangle(min_x=5.0, min_y=5.0, max_x=15.0, max_y=15.0)

        # Specify direction to the right
        vector = applicator.calculate_move_vector(pcb, "C1", failure_area, direction=(1.0, 0.0))
        assert vector is not None
        dx, dy = vector
        assert dx > 0  # Should move right
        assert abs(dy) < 0.01  # Should not move vertically

    def test_calculate_spread_vector(self):
        """Test calculating spread vector for multi-component spreading."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 12.0, 15.0))

        center = (10.0, 10.0)
        vector = applicator.calculate_spread_vector(pcb, "C1", center)
        assert vector is not None
        dx, dy = vector
        # Component should spread away from center
        # C1 is at (12, 15), center at (10, 10), so should move right and up
        assert dx > 0  # Moving right (away from center)
        assert dy > 0  # Moving up (away from center)

    def test_simulate_placement_change(self):
        """Test simulating placement changes without applying them."""
        applicator = StrategyApplicator()
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("C1", 10.0, 20.0))
        pcb.footprints.append(MockFootprint("C2", 30.0, 40.0))

        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_MULTIPLE,
            difficulty=Difficulty.HARD,
            confidence=0.7,
            actions=[
                Action(type="move", target="C1", params={"x": 15.0, "y": 25.0}),
                Action(type="move", target="C2", params={"x": 35.0, "y": 45.0}),
            ],
        )

        positions = applicator.simulate_placement_change(pcb, strategy)

        # Verify simulation returns expected positions
        assert "C1" in positions
        assert "C2" in positions
        assert positions["C1"] == (15.0, 25.0)
        assert positions["C2"] == (35.0, 45.0)

        # Verify original PCB is not modified
        assert pcb.footprints[0].position == (10.0, 20.0)
        assert pcb.footprints[1].position == (30.0, 40.0)


class TestPlacementAdjustment:
    """Tests for PlacementAdjustment dataclass."""

    def test_placement_adjustment_creation(self):
        """Test creating a PlacementAdjustment."""
        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 15.0, "y": 25.0})],
        )
        result = ApplicationResult(
            success=True,
            components_moved=["C1"],
            message="Moved C1",
        )
        adjustment = PlacementAdjustment(
            iteration=0,
            strategy=strategy,
            result=result,
            failed_nets_before=[1, 2, 3],
            failed_nets_after=[2],
        )
        assert adjustment.iteration == 0
        assert adjustment.strategy == strategy
        assert adjustment.result == result
        assert len(adjustment.failed_nets_before) == 3
        assert len(adjustment.failed_nets_after) == 1


class TestPlacementFeedbackResult:
    """Tests for PlacementFeedbackResult dataclass."""

    def test_feedback_result_creation(self):
        """Test creating a PlacementFeedbackResult."""
        result = PlacementFeedbackResult(
            success=True,
            routes=[],
            iterations=2,
            adjustments=[],
            failed_nets=[],
            total_components_moved=3,
        )
        assert result.success is True
        assert result.iterations == 2
        assert result.total_components_moved == 3

    def test_feedback_result_summary(self):
        """Test PlacementFeedbackResult summary method."""
        strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.85,
            actions=[Action(type="move", target="C1", params={"x": 15.0, "y": 25.0})],
        )
        app_result = ApplicationResult(
            success=True,
            components_moved=["C1"],
            message="Moved C1",
        )
        adjustment = PlacementAdjustment(
            iteration=0,
            strategy=strategy,
            result=app_result,
            failed_nets_before=[1, 2, 3],
            failed_nets_after=[2],
        )
        result = PlacementFeedbackResult(
            success=False,
            routes=[],
            iterations=2,
            adjustments=[adjustment],
            failed_nets=[2],
            total_components_moved=1,
        )
        summary = result.summary()
        assert "Placement-Routing Feedback Result" in summary
        assert "Success: False" in summary
        assert "Iterations: 2" in summary
        assert "Components moved: 1" in summary
        assert "Failed nets: 1" in summary
        assert "Adjustments:" in summary
        # Issue #2606: summary surfaces exit_reason on its own line.
        assert "Exit reason: pf_max_iter" in summary

    def test_feedback_result_exit_reason_default(self):
        """Issue #2606: exit_reason defaults to ``pf_max_iter`` for back-compat."""
        result = PlacementFeedbackResult(
            success=False,
            routes=[],
            iterations=4,
        )
        assert result.exit_reason == "pf_max_iter"

    def test_feedback_result_exit_reason_explicit(self):
        """Callers can set exit_reason to any of the four canonical values."""
        for reason in (
            "pf_converged",
            "pf_max_iter",
            "pf_stagnated",
            "pf_timeout",
        ):
            result = PlacementFeedbackResult(
                success=False,
                routes=[],
                iterations=1,
                exit_reason=reason,
            )
            assert result.exit_reason == reason


class TestModuleExports:
    """Tests for module exports."""

    def test_recovery_exports(self):
        """Test that new classes are exported from recovery module."""
        from kicad_tools.recovery import (
            ApplicationResult,
            StrategyApplicator,
        )

        assert ApplicationResult is not None
        assert StrategyApplicator is not None

    def test_router_exports(self):
        """Test that new classes are exported from router module."""
        from kicad_tools.router import (
            PlacementAdjustment,
            PlacementFeedbackLoop,
            PlacementFeedbackResult,
        )

        assert PlacementAdjustment is not None
        assert PlacementFeedbackLoop is not None
        assert PlacementFeedbackResult is not None


class TestDetectPfStagnation:
    """Unit tests for the pure-function ``detect_pf_stagnation`` helper.

    Issue #2606: detect when ``PlacementFeedbackLoop`` is no longer making
    progress between outer iterations so the loop can exit cleanly instead
    of burning a multi-minute negotiated-router run on every iteration.
    """

    def test_empty_history(self):
        assert detect_pf_stagnation([], patience=3) is False

    def test_single_entry(self):
        assert detect_pf_stagnation([46], patience=3) is False

    def test_two_entries_insufficient(self):
        """Need patience+1 entries -- two flat values is not enough at patience=3."""
        assert detect_pf_stagnation([46, 46], patience=3) is False

    def test_three_entries_insufficient(self):
        """Three flat values is still too short at patience=3 (needs four)."""
        assert detect_pf_stagnation([46, 46, 46], patience=3) is False

    def test_four_flat_entries_triggers(self):
        """Four consecutive identical entries at patience=3 => stagnated."""
        assert detect_pf_stagnation([46, 46, 46, 46], patience=3) is True

    def test_improvement_followed_by_three_flat(self):
        """[40, 46, 46, 46, 46] -- the trailing 4 entries are flat => stagnated."""
        assert detect_pf_stagnation([40, 46, 46, 46, 46], patience=3) is True

    def test_still_improving(self):
        """Monotonically increasing history is never stagnated."""
        assert detect_pf_stagnation([40, 43, 44, 45], patience=3) is False

    def test_last_entry_improves(self):
        """A final improvement breaks stagnation (window is not all equal)."""
        assert detect_pf_stagnation([46, 46, 46, 47], patience=3) is False

    def test_strict_equality_semantics(self):
        """Window [47, 46, 46, 46] has 2 distinct values => not stagnated.

        Confirms the helper's strict ``len(set(window)) == 1`` semantics:
        even though the most-recent three entries are flat, the inclusion
        of the patience+1th entry differing keeps stagnation off.
        """
        assert detect_pf_stagnation([46, 47, 46, 46, 46], patience=3) is False

    def test_configurable_patience_short(self):
        """patience=2 fires on three flat entries."""
        assert detect_pf_stagnation([46, 46, 46], patience=2) is True

    def test_configurable_patience_long(self):
        """patience=5 needs six flat entries -- four is not enough."""
        assert detect_pf_stagnation([46, 46, 46, 46], patience=5) is False

    def test_configurable_patience_long_satisfied(self):
        """patience=5 satisfied by six flat entries."""
        assert detect_pf_stagnation([46, 46, 46, 46, 46, 46], patience=5) is True

    def test_zero_patience_disabled(self):
        """patience<1 disables the detector."""
        assert detect_pf_stagnation([46, 46, 46, 46], patience=0) is False
        assert detect_pf_stagnation([], patience=0) is False


class _FakeAutorouter:
    """Minimal Autorouter stand-in for PlacementFeedbackLoop integration tests.

    Mocks just the attributes / methods the loop calls:

    - ``nets`` -- dict whose ``len()`` is ``total_nets + 1`` (the loop
      subtracts 1 for net 0).
    - ``routes`` -- list of (returned-by-route-all) routes; not actually
      validated by the loop, so an empty list is fine.
    - ``route_all_negotiated`` / ``route_all`` -- both return ``routes``.
    - ``get_failed_nets`` -- returns the configured list (constant across
      calls by default).
    - ``_reset_for_new_trial`` -- no-op.
    - ``analyze_routing_failure`` -- returns None (so the strategy
      generator pipeline produces zero strategies and the loop's
      ``_find_best_placement_strategy`` returns None).
    """

    def __init__(
        self,
        total_nets: int,
        failed_nets_sequence: list[list[int]] | None = None,
        failed_nets_constant: list[int] | None = None,
        per_call_delay: float = 0.0,
    ):
        # ``nets`` is a dict[int, ...]; the loop only inspects ``len()``.
        self.nets: dict[int, list] = {i: [] for i in range(total_nets + 1)}
        self.routes: list = []
        self._call_index = 0
        self._failed_sequence = failed_nets_sequence
        self._failed_constant = failed_nets_constant if failed_nets_constant is not None else []
        self._per_call_delay = per_call_delay
        self.route_all_negotiated_calls = 0
        self.route_all_calls = 0

    def route_all_negotiated(self, **kwargs):
        self.route_all_negotiated_calls += 1
        if self._per_call_delay > 0:
            import time as _t

            _t.sleep(self._per_call_delay)
        self._call_index += 1
        return self.routes

    def route_all(self, **kwargs):
        self.route_all_calls += 1
        if self._per_call_delay > 0:
            import time as _t

            _t.sleep(self._per_call_delay)
        self._call_index += 1
        return self.routes

    def get_failed_nets(self) -> list[int]:
        # Return failures relative to the *last* call (the loop calls
        # get_failed_nets immediately after route_all_*).
        idx = max(self._call_index - 1, 0)
        if self._failed_sequence is not None:
            i = min(idx, len(self._failed_sequence) - 1)
            return list(self._failed_sequence[i])
        return list(self._failed_constant)

    def _reset_for_new_trial(self) -> None:
        pass

    def analyze_routing_failure(self, net_id):
        # No analysis => strategy generator yields nothing => loop
        # exits with "No suitable placement strategy found".
        return None


class _AlwaysApplyLoop:
    """Subclass-helper: bypass the strategy-generation pipeline.

    For integration tests we want the outer loop to actually iterate
    multiple times so we can observe the stagnation/timeout exits.
    Real strategy generation requires a populated ``Autorouter`` and a
    realistic PCB; the helpers below let us drive the loop directly by
    monkey-patching ``_find_best_placement_strategy`` to always return
    a no-op move strategy, and the applicator to always succeed.
    """

    @staticmethod
    def patch(loop):
        # Always produce a dummy strategy so the loop never short-circuits
        # with "No suitable placement strategy found".
        dummy_strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.99,
            actions=[Action(type="move", target="C1", params={"x": 0.0, "y": 0.0})],
        )

        def _dummy_strategy(_failed, _conf):
            return dummy_strategy

        # The applicator unconditionally succeeds but moves nothing,
        # preserving the routed-count plateau the stagnation detector
        # is meant to catch.
        class _DummyApplicator:
            def apply_strategy(self, _pcb, _strategy):
                return ApplicationResult(
                    success=True,
                    components_moved=[],
                    message="(dummy: no-op move)",
                )

            def is_safe_to_apply(self, _strategy, _pcb):
                return True

        loop._find_best_placement_strategy = _dummy_strategy  # type: ignore[method-assign]
        loop._strategy_applicator = _DummyApplicator()
        # _snapshot_positions calls _find_footprint which iterates
        # pcb.footprints -- our MockPCB has none, so this is a no-op.
        return loop


class TestPlacementFeedbackLoopExitReasons:
    """Issue #2606: integration tests covering each ``exit_reason`` path.

    Each test uses a monkey-patched ``Autorouter`` plus an
    ``_AlwaysApplyLoop``-patched ``PlacementFeedbackLoop`` so the loop's
    routing call is fast and deterministic and the iteration actually
    proceeds.  We exercise the four exit reasons --
    ``pf_converged``, ``pf_max_iter``, ``pf_stagnated``,
    ``pf_timeout`` -- by manipulating the fake router's
    ``failed_nets`` sequence and the loop's configuration.
    """

    def test_pf_converged(self):
        """Empty failed_nets on iteration 0 => pf_converged."""
        from kicad_tools.router import PlacementFeedbackLoop

        router = _FakeAutorouter(total_nets=10, failed_nets_constant=[])
        loop = PlacementFeedbackLoop(router=router, pcb=None, verbose=False, stagnation_patience=3)
        result = loop.run(max_adjustments=5)
        assert result.exit_reason == "pf_converged"
        assert result.success is True
        assert result.iterations == 1
        # Only one routing call was made.
        assert router.route_all_negotiated_calls == 1

    def test_pf_stagnated_exits_before_max_iter(self):
        """Constant failed_nets across iterations triggers pf_stagnated.

        The loop must NOT burn ``max_adjustments+1`` iterations when
        the fully-routed-net count has plateaued.  With ``patience=3``
        we expect at most 4 iterations (1 baseline + 3 unchanged) before
        the detector fires.
        """
        from kicad_tools.router import PlacementFeedbackLoop

        # 5 nets always failing => routed_count = 5 (constant).
        # total_nets in the loop = len(nets)-1 = 10, failed = 5,
        # so routed_count = 5 every iteration.
        router = _FakeAutorouter(total_nets=10, failed_nets_constant=[1, 2, 3, 4, 5])
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(router=router, pcb=pcb, verbose=False, stagnation_patience=3)
        _AlwaysApplyLoop.patch(loop)
        result = loop.run(max_adjustments=10)
        assert result.exit_reason == "pf_stagnated"
        assert result.success is False
        # 1 baseline + patience(3) unchanged = 4 iterations total.
        assert result.iterations <= 4
        # And we did NOT burn the full max_adjustments+1 (=11) budget.
        assert router.route_all_negotiated_calls <= 4

    def test_pf_stagnated_disabled_when_patience_zero(self):
        """stagnation_patience=0 disables the detector; loop runs to max_iter.

        With patience=0 and a strategy-always-applies stub, the loop
        runs the full ``max_adjustments + 1`` iterations and exits via
        the legacy ``iteration >= max_adjustments`` branch.
        """
        from kicad_tools.router import PlacementFeedbackLoop

        router = _FakeAutorouter(total_nets=10, failed_nets_constant=[1, 2, 3])
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(router=router, pcb=pcb, verbose=False, stagnation_patience=0)
        _AlwaysApplyLoop.patch(loop)
        result = loop.run(max_adjustments=3)
        assert result.exit_reason == "pf_max_iter"
        # Full budget consumed: 1 baseline + 3 adjustments = 4 iters.
        assert result.iterations == 4

    def test_pf_max_iter_progress_not_stagnated(self):
        """Monotonically improving routed_count => pf_max_iter (not stagnated).

        Fake router returns shrinking failed-net lists across calls,
        so routed_count grows every iteration.  The stagnation detector
        must NOT fire, and the loop must exit via the max_adjustments
        cap instead.
        """
        from kicad_tools.router import PlacementFeedbackLoop

        # Sequence so routed_count = 6, 7, 8, 9 across 4 iterations.
        failed_sequence = [
            [1, 2, 3, 4],  # routed=6
            [1, 2, 3],  # routed=7
            [1, 2],  # routed=8
            [1],  # routed=9 (still not converged)
        ]
        router = _FakeAutorouter(total_nets=10, failed_nets_sequence=failed_sequence)
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(router=router, pcb=pcb, verbose=False, stagnation_patience=3)
        _AlwaysApplyLoop.patch(loop)
        # max_adjustments=3 => 4 total iterations.  Progress every step
        # means stagnation must NOT fire.
        result = loop.run(max_adjustments=3)
        assert result.exit_reason == "pf_max_iter"
        assert result.success is False
        # Full budget consumed because the detector didn't fire.
        assert result.iterations == 4
        # Sanity check the detector with the observed history:
        assert detect_pf_stagnation([6, 7, 8, 9], patience=3) is False

    def test_pf_max_iter_no_pcb(self):
        """Legacy behaviour preserved: pcb=None => single iteration, pf_max_iter."""
        from kicad_tools.router import PlacementFeedbackLoop

        router = _FakeAutorouter(total_nets=10, failed_nets_constant=[1, 2, 3])
        loop = PlacementFeedbackLoop(router=router, pcb=None, verbose=False, stagnation_patience=10)
        result = loop.run(max_adjustments=3)
        assert result.exit_reason == "pf_max_iter"
        assert result.iterations == 1
        assert router.route_all_negotiated_calls == 1

    def test_pf_timeout(self):
        """outer_timeout shorter than the per-iteration delay => pf_timeout.

        Fake router sleeps 0.4s per call.  outer_timeout=0.3s means the
        second iteration's pre-route guard will trip and exit cleanly.
        """
        from kicad_tools.router import PlacementFeedbackLoop

        router = _FakeAutorouter(
            total_nets=10,
            failed_nets_constant=[1, 2, 3],
            per_call_delay=0.4,
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=False,
            stagnation_patience=10,  # prevent stagnated-exit
            outer_timeout=0.3,
        )
        _AlwaysApplyLoop.patch(loop)
        result = loop.run(max_adjustments=10)
        assert result.exit_reason == "pf_timeout"
        # First iteration always runs (the timeout is checked at the
        # top of each iteration); after that the guard trips.
        assert result.iterations <= 2
        assert router.route_all_negotiated_calls <= 2

    def test_pf_summary_includes_exit_reason(self):
        """Issue #2606: summary() surfaces exit_reason for stagnated runs."""
        from kicad_tools.router import PlacementFeedbackLoop

        router = _FakeAutorouter(total_nets=10, failed_nets_constant=[1, 2])
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(router=router, pcb=pcb, verbose=False, stagnation_patience=2)
        _AlwaysApplyLoop.patch(loop)
        result = loop.run(max_adjustments=10)
        assert result.exit_reason == "pf_stagnated"
        assert "Exit reason: pf_stagnated" in result.summary()


class TestPlacementFeedbackLoopBuilderHandoff:
    """Issue #2604: integration tests for the analyzer -> generator -> loop pipeline.

    The pre-existing tests in this file synthesize ``ResolutionStrategy``
    objects directly, which bypasses the broken handoff between
    ``RootCauseAnalyzer`` and ``StrategyGenerator``.  These tests exercise
    the full path on a synthetic router so the bug stays caught.
    """

    def _make_pcb_with_footprint(self, ref: str, x: float, y: float):
        pcb = MockPCB()
        pcb.footprints.append(MockFootprint(ref, x, y))
        # Add a generous board outline so is_safe_to_apply doesn't reject.
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 0.0), (200.0, 0.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (200.0, 0.0), (200.0, 200.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (200.0, 200.0), (0.0, 200.0)))
        pcb.graphic_items.append(MockGraphicItem("Edge.Cuts", (0.0, 200.0), (0.0, 0.0)))
        return pcb

    def _make_analysis_with_blocker(self, ref: str | None):
        """Build a router-style FailureAnalysis with a single component blocker."""
        from kicad_tools.router.failure_analysis import (
            BlockingElement,
            FailureAnalysis,
            FailureCause,
            Rectangle,
        )

        return FailureAnalysis(
            root_cause=FailureCause.BLOCKED_PATH,
            confidence=0.85,
            failure_location=(50.0, 50.0),
            failure_area=Rectangle(45.0, 45.0, 55.0, 55.0),
            blocking_elements=[
                BlockingElement(
                    type="component",
                    ref=ref,
                    net=None,
                    bounds=Rectangle(48.0, 48.0, 52.0, 52.0),
                    movable=True,
                    layer=0,
                )
            ],
        )

    def _make_router(self, analysis, failed_nets=(1,)):
        class FakeRouter:
            def __init__(self, analysis):
                self._analysis = analysis
                self._failed = list(failed_nets)
                self.nets = {0: [], 1: []}
                self.routes = []

            def get_failed_nets(self):
                return list(self._failed)

            def analyze_routing_failure(self, net_id):
                return self._analysis

            def route_all_negotiated(self, **kwargs):
                return []

            def route_all(self):
                return []

            def _reset_for_new_trial(self):
                return None

        return FakeRouter(analysis)

    def test_loop_invokes_move_component_when_blockers_have_refs(self, capsys):
        """Issue #2604 primary fix: with refs populated the loop emits MOVE_COMPONENT.

        Before the fix, ``BlockingElement.ref`` was always None and the
        strategy generator dropped every candidate, so the loop logged
        "No suitable placement strategy found" without ever calling the
        applicator.
        """
        from kicad_tools.router.placement_feedback import PlacementFeedbackLoop

        pcb = self._make_pcb_with_footprint("U1", 50.0, 50.0)
        analysis = self._make_analysis_with_blocker(ref="U1")
        router = self._make_router(analysis)

        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=True,
            max_movement=5.0,
        )
        strategy = loop._find_best_placement_strategy(failed_nets=[1], min_confidence=0.5)

        assert strategy is not None, (
            "Issue #2604 regression: loop returned no strategy when "
            "BlockingElement.ref is populated"
        )
        assert strategy.type.value in ("move_component", "move_multiple")
        # The strategy must target U1 (the populated ref).
        assert "U1" in strategy.affected_components

    def test_loop_emits_no_strategy_when_blocker_ref_is_none(self, capsys):
        """Pre-fix behavior: ref=None drops every candidate.

        This test pins the *current* fix: when the analyzer fails to
        populate the ref (e.g. on a grid with no pad data, like the chorus
        test pre-fix), the loop now emits a structured diagnostic instead
        of the silent "No suitable placement strategy found".
        """
        from kicad_tools.router.placement_feedback import PlacementFeedbackLoop

        pcb = self._make_pcb_with_footprint("U1", 50.0, 50.0)
        analysis = self._make_analysis_with_blocker(ref=None)
        router = self._make_router(analysis)

        loop = PlacementFeedbackLoop(router=router, pcb=pcb, verbose=True, max_movement=5.0)
        strategy = loop._find_best_placement_strategy(failed_nets=[1], min_confidence=0.5)
        assert strategy is None
        captured = capsys.readouterr()
        # Acceptance criterion #4: when no candidates are generated we must
        # log a diagnostic that distinguishes the population bug from the
        # filter rejections.
        assert "No suitable placement strategy" in captured.out
        # Either "0 movable blockers" or "0 MOVE_COMPONENT candidates" must
        # appear so an operator can tell ref-population is the failure mode.
        assert "0 movable blockers" in captured.out or "0 MOVE_COMPONENT candidates" in captured.out

    def test_loop_emits_summary_in_non_verbose_mode(self, capsys):
        """Issue #2604 follow-up: default (non-verbose) mode is not silent.

        Before this fix the structured rejection breakdown only printed
        ``if self.verbose`` and the prior ``"No suitable placement
        strategy found"`` line had been removed entirely, so default
        runs were *more* silent than before.  This test pins the
        single-line summary that must appear in non-verbose mode.
        """
        from kicad_tools.router.placement_feedback import PlacementFeedbackLoop

        pcb = self._make_pcb_with_footprint("U1", 50.0, 50.0)
        analysis = self._make_analysis_with_blocker(ref=None)
        router = self._make_router(analysis)

        loop = PlacementFeedbackLoop(router=router, pcb=pcb, verbose=False, max_movement=5.0)
        strategy = loop._find_best_placement_strategy(failed_nets=[1], min_confidence=0.5)
        assert strategy is None
        captured = capsys.readouterr()
        # The non-verbose summary must surface at least the headline
        # diagnostic so default users aren't left guessing why the loop
        # bailed out.
        assert "No suitable placement strategy" in captured.out

    def test_loop_logs_filter_breakdown_when_all_candidates_rejected(self, capsys):
        """Acceptance criterion #4: when all candidates are anchored, say so."""
        from kicad_tools.router.placement_feedback import PlacementFeedbackLoop

        pcb = self._make_pcb_with_footprint("U1", 50.0, 50.0)
        analysis = self._make_analysis_with_blocker(ref="U1")
        router = self._make_router(analysis)

        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=True,
            max_movement=5.0,
            fixed_refs={"U1"},  # Anchor the only candidate.
        )
        strategy = loop._find_best_placement_strategy(failed_nets=[1], min_confidence=0.5)
        assert strategy is None
        captured = capsys.readouterr()
        assert "anchored" in captured.out
        assert "rejected" in captured.out.lower()

    def test_loop_logs_over_budget_breakdown(self, capsys):
        """Acceptance criterion #4: when all candidates exceed the cap, say so.

        Force the over-budget filter by stubbing the strategy generator
        to return a strategy whose move action targets a position far
        outside the loop's movement budget.  The loop must report ``over
        budget`` in its rejection breakdown.
        """
        from kicad_tools.recovery import (
            Action,
            Difficulty,
            ResolutionStrategy,
            StrategyType,
        )
        from kicad_tools.router.placement_feedback import PlacementFeedbackLoop

        pcb = self._make_pcb_with_footprint("U1", 50.0, 50.0)
        analysis = self._make_analysis_with_blocker(ref="U1")
        router = self._make_router(analysis)

        loop = PlacementFeedbackLoop(router=router, pcb=pcb, verbose=True, max_movement=2.0)

        # Stub the strategy generator to return an over-budget move so we
        # can exercise the filter deterministically.
        class _StubGenerator:
            def generate_strategies(self, _pcb, _failure, max_movement=None):
                return [
                    ResolutionStrategy(
                        type=StrategyType.MOVE_COMPONENT,
                        difficulty=Difficulty.EASY,
                        confidence=0.85,
                        # 50,50 -> 100,100 = 70.7mm, way over the 2mm cap.
                        actions=[
                            Action(
                                type="move",
                                target="U1",
                                params={"x": 100.0, "y": 100.0},
                            )
                        ],
                        affected_components=["U1"],
                    )
                ]

        loop._strategy_generator = _StubGenerator()
        strategy = loop._find_best_placement_strategy(failed_nets=[1], min_confidence=0.5)
        assert strategy is None
        captured = capsys.readouterr()
        assert "over budget" in captured.out
        assert "rejected" in captured.out.lower()

    def test_loop_passes_max_movement_to_strategy_generator(self):
        """Issue #2604 secondary fix: budget is plumbed to the generator.

        Verify candidates respect the cap by ensuring the resulting
        strategy targets a position within ``max_movement`` of the
        original.  Pre-fix the candidate offsets were corridor-derived
        and routinely exceeded the cap.
        """
        from kicad_tools.router.placement_feedback import PlacementFeedbackLoop

        pcb = self._make_pcb_with_footprint("U1", 50.0, 50.0)
        analysis = self._make_analysis_with_blocker(ref="U1")
        router = self._make_router(analysis)

        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=False,
            max_movement=3.0,
        )
        strategy = loop._find_best_placement_strategy(failed_nets=[1], min_confidence=0.5)
        assert strategy is not None

        import math

        for action in strategy.actions:
            if action.type != "move":
                continue
            new_x = action.params["x"]
            new_y = action.params["y"]
            distance = math.hypot(new_x - 50.0, new_y - 50.0)
            assert distance <= 3.0 + 1e-9, (
                f"Move candidate of {distance:.3f}mm exceeds max_movement=3.0mm"
            )

    def test_strategy_generator_respects_max_movement_directly(self):
        """Direct test of the generator-level budget plumbing.

        Even outside the feedback loop, calling
        ``StrategyGenerator.generate_strategies(..., max_movement=...)``
        produces only candidates inside the cap.
        """
        import math

        from kicad_tools.recovery.strategy import StrategyGenerator
        from kicad_tools.recovery.types import (
            BlockingElement as RecoveryBlockingElement,
        )
        from kicad_tools.recovery.types import (
            FailureAnalysis as RecoveryFailureAnalysis,
        )
        from kicad_tools.recovery.types import (
            FailureCause as RecoveryFailureCause,
        )
        from kicad_tools.recovery.types import (
            Rectangle as RecoveryRectangle,
        )

        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("U1", 50.0, 50.0))

        analysis = RecoveryFailureAnalysis(
            root_cause=RecoveryFailureCause.BLOCKED_PATH,
            confidence=0.85,
            failure_location=(50.0, 50.0),
            failure_area=RecoveryRectangle(45.0, 45.0, 55.0, 55.0),
            blocking_elements=[
                RecoveryBlockingElement(
                    type="component",
                    ref="U1",
                    net=None,
                    bounds=RecoveryRectangle(48.0, 48.0, 52.0, 52.0),
                    movable=True,
                )
            ],
        )

        generator = StrategyGenerator()
        strategies = generator.generate_strategies(pcb, analysis, max_movement=2.0)
        move_strategies = [
            s for s in strategies if s.type.value in ("move_component", "move_multiple")
        ]
        assert move_strategies, (
            "Issue #2604 regression: generator dropped all move strategies "
            "even though blocker.ref is populated"
        )
        # Every candidate must respect the budget.
        for s in move_strategies:
            for action in s.actions:
                if action.type != "move":
                    continue
                d = math.hypot(action.params["x"] - 50.0, action.params["y"] - 50.0)
                assert d <= 2.0 + 1e-9, (
                    f"Generator emitted candidate at {d:.3f}mm, exceeds max_movement=2.0mm"
                )

    def test_move_candidate_sweep_spans_both_radii_and_diverse_directions(self):
        """Issue #2604 follow-up: cap-radius candidates must reach the strategy slice.

        Before the fix, the 16-candidate sweep (8 angles x 2 radii) was
        sorted purely by ``(-improvement, distance)`` -- with all 16
        candidates landing outside the failure area they shared the same
        ``improvement=0.9`` and the 8 small-radius (closer) candidates
        always sorted ahead of the 8 cap-radius ones.  The downstream
        ``_generate_move_strategies`` slice ``candidates[:3]`` then only
        ever surfaced 3 small-radius candidates at angles 0/45/90 deg
        (East / NE / North), so South / West / SE / SW directions and
        the cap-radius band were effectively dead code.

        This test pins the new "diversity" sort: across the strategies
        emitted for a single blocker we must see candidates spanning
        *both* radius bands AND covering at least 3 distinct quadrants.
        """
        import math

        from kicad_tools.recovery.strategy import StrategyGenerator
        from kicad_tools.recovery.types import (
            BlockingElement as RecoveryBlockingElement,
        )
        from kicad_tools.recovery.types import (
            FailureAnalysis as RecoveryFailureAnalysis,
        )
        from kicad_tools.recovery.types import (
            FailureCause as RecoveryFailureCause,
        )
        from kicad_tools.recovery.types import (
            Rectangle as RecoveryRectangle,
        )

        pcb = MockPCB()
        pcb.footprints.append(MockFootprint("U1", 50.0, 50.0))

        # Failure area is a tiny square at the component centre, so all
        # 16 sweep candidates land outside it and tie on ``improvement``.
        # max_movement = 4.0 produces radii [2.0, 4.0] (small + cap).
        analysis = RecoveryFailureAnalysis(
            root_cause=RecoveryFailureCause.BLOCKED_PATH,
            confidence=0.85,
            failure_location=(50.0, 50.0),
            failure_area=RecoveryRectangle(49.5, 49.5, 50.5, 50.5),
            blocking_elements=[
                RecoveryBlockingElement(
                    type="component",
                    ref="U1",
                    net=None,
                    bounds=RecoveryRectangle(49.0, 49.0, 51.0, 51.0),
                    movable=True,
                )
            ],
        )

        generator = StrategyGenerator()
        strategies = generator.generate_strategies(pcb, analysis, max_movement=4.0)
        move_strategies = [
            s for s in strategies if s.type.value in ("move_component", "move_multiple")
        ]
        assert move_strategies, "expected at least one MOVE_COMPONENT strategy"

        radii_seen: set[int] = set()
        quadrants_seen: set[str] = set()
        for s in move_strategies:
            for action in s.actions:
                if action.type != "move":
                    continue
                dx = action.params["x"] - 50.0
                dy = action.params["y"] - 50.0
                # Bucket distance into "small" (~2mm) vs "cap" (~4mm).
                d = math.hypot(dx, dy)
                if d < 3.0:
                    radii_seen.add(0)  # small
                else:
                    radii_seen.add(1)  # cap
                # Bucket into quadrants by sign of dx, dy.
                qx = "E" if dx > 1e-6 else ("W" if dx < -1e-6 else "0")
                qy = "N" if dy > 1e-6 else ("S" if dy < -1e-6 else "0")
                quadrants_seen.add(f"{qx}{qy}")

        assert len(radii_seen) >= 2, (
            f"Issue #2604 follow-up: cap-radius candidates dropped -- only saw radii {radii_seen}"
        )
        # With the [:3] slice and strict (radius, opposite-angle)
        # interleaving the first 3 picks land on East-small, East-cap,
        # West-small.  That covers two opposing quadrants -- the key
        # regression we are pinning is "both radius bands appear" rather
        # than the slice-degenerate single-band case.  We separately
        # verify the *underlying* candidate list (before the slice) is
        # diverse via a direct generator inspection below.
        assert len(quadrants_seen) >= 2, (
            f"Issue #2604 follow-up: direction sweep collapsed to "
            f"{quadrants_seen}; expected >=2 distinct quadrants"
        )

        # Direct inspection of the candidate list (pre-slice) confirms
        # the sweep is producing the full 8-direction x 2-radius set so
        # downstream consumers that look beyond the [:3] slice see all
        # of them.
        from kicad_tools.recovery.types import (
            BlockingElement as _BlockingElement,
        )
        from kicad_tools.recovery.types import (
            Rectangle as _Rect,
        )

        blocker = _BlockingElement(
            type="component",
            ref="U1",
            net=None,
            bounds=_Rect(49.0, 49.0, 51.0, 51.0),
            movable=True,
        )
        candidates = generator._find_move_candidates(
            pcb,
            blocker,
            _Rect(49.5, 49.5, 50.5, 50.5),
            max_movement=4.0,
        )
        # 8 angles x 2 radii = 16 candidates expected.
        assert len(candidates) == 16, (
            f"expected 16 sweep candidates (8 angles x 2 radii), got {len(candidates)}"
        )
        all_quadrants: set[str] = set()
        for c in candidates:
            cdx = c["position"][0] - 50.0
            cdy = c["position"][1] - 50.0
            qx = "E" if cdx > 1e-6 else ("W" if cdx < -1e-6 else "0")
            qy = "N" if cdy > 1e-6 else ("S" if cdy < -1e-6 else "0")
            all_quadrants.add(f"{qx}{qy}")
        # 8-direction sweep => 8 distinct (qx,qy) buckets including
        # the diagonals.  South / West / SE / SW must all appear.
        assert "W0" in all_quadrants and "0S" in all_quadrants
        assert "WS" in all_quadrants and "ES" in all_quadrants


class _RoutingFakeRouter:
    """Fake Autorouter that mutates ``self.routes`` per iteration.

    Used by Issue #2840 rollback tests.  Each call to
    ``route_all_negotiated`` consumes the next entry in ``routes_per_call``
    and populates ``self.routes`` accordingly so:

    - ``_reset_for_new_trial()`` clears ``self.routes`` (mirroring real
      :class:`Autorouter` behavior).
    - ``route_all_negotiated()`` appends the configured routes for the
      current call index.
    - ``get_failed_nets()`` derives strictly from ``self.routes`` membership
      (mirroring real :meth:`Autorouter.get_failed_nets` at
      ``core.py:8320``), so the loop's monotonicity logic operates against
      a state model identical to the real router.

    The ``Route`` objects are real ``router.primitives.Route`` instances so
    ``copy.deepcopy`` in the loop's snapshot path exercises the real
    ``Route`` deep-copy semantics.
    """

    def __init__(self, total_nets: int, routes_per_call: list[list]):
        # ``nets`` len = total_nets + 1 so the loop's ``-1 for net 0``
        # math yields ``total_nets``.
        self.nets: dict[int, list] = {i: [] for i in range(total_nets + 1)}
        self.routes: list = []
        self._routes_per_call = routes_per_call
        self._call_index = 0

    def route_all_negotiated(self, **_kwargs):
        idx = min(self._call_index, len(self._routes_per_call) - 1)
        # Caller expects route_all_negotiated to populate self.routes
        # with the iteration's results (real router does this via the
        # per-iteration append loop).
        for r in self._routes_per_call[idx]:
            self.routes.append(r)
        self._call_index += 1
        return list(self.routes)

    def route_all(self, **_kwargs):
        return self.route_all_negotiated()

    def get_failed_nets(self) -> list[int]:
        """Derive failed_nets from self.routes -- mirrors core.py:8320."""
        routed_nets = {r.net for r in self.routes}
        all_nets = {n for n in self.nets if n != 0}
        return list(all_nets - routed_nets)

    def _reset_for_new_trial(self) -> None:
        """Clear routes -- mirrors core.py:6683."""
        self.routes = []

    def analyze_routing_failure(self, _net_id):
        return None


def _make_test_route(net_id: int) -> "object":
    """Construct a minimal ``Route`` carrying just enough state for tests.

    The route's ``net`` field is what ``get_failed_nets`` keys on, so
    that's all that matters for rollback verification.  Segments and vias
    can be empty lists; deep-copy still exercises the dataclass clone path.
    """
    from kicad_tools.router.primitives import Route

    return Route(net=net_id, net_name=f"NET_{net_id}", segments=[], vias=[])


class TestPlacementFeedbackLoopRollback:
    """Issue #2840: PlacementFeedbackLoop must restore best-known state.

    The placement-feedback loop is non-monotonic by construction -- each
    iteration clears all routes and re-routes from scratch.  Without an
    explicit best-state snapshot, a later iteration that regresses
    leaves ``self.router.routes`` worse than a prior pass produced, and
    the final ``PlacementFeedbackResult`` reports the regression.

    These tests exercise the snapshot/restore path added in #2840 by
    driving a fake router through scripted sequences of routed-state
    transitions and asserting the loop returns the best-observed state.
    """

    def _strategy_always_applies(self, loop):
        """Patch the strategy pipeline so the loop iterates max_adjustments+1 times.

        Mirrors ``_AlwaysApplyLoop.patch`` above: a dummy strategy that
        always 'succeeds' (moves nothing) so the loop never short-circuits
        on "no suitable placement strategy found".
        """
        dummy_strategy = ResolutionStrategy(
            type=StrategyType.MOVE_COMPONENT,
            difficulty=Difficulty.EASY,
            confidence=0.99,
            actions=[Action(type="move", target="C1", params={"x": 0.0, "y": 0.0})],
        )

        def _dummy_find(_failed, _conf):
            return dummy_strategy

        class _DummyApplicator:
            def apply_strategy(self, _pcb, _strategy):
                return ApplicationResult(
                    success=True,
                    components_moved=[],
                    message="(dummy: no-op move)",
                )

            def is_safe_to_apply(self, _strategy, _pcb):
                return True

        loop._find_best_placement_strategy = _dummy_find  # type: ignore[method-assign]
        loop._strategy_applicator = _DummyApplicator()
        return loop

    def test_regression_after_best_iteration_restores_best_state(self):
        """Pass 1 fails connectivity -> post-rollback state == pre-pass-1 state.

        Three iterations with routed counts [3, 1, 2] across 4 total
        nets.  Without #2840 the loop returns iteration 2's state
        (2 routed, 2 failed); WITH #2840 the loop must restore to
        iteration 0's snapshot (3 routed, 1 failed).
        """
        from kicad_tools.router import PlacementFeedbackLoop

        # iteration-0 routes nets 1,2,3 (3 routed); iteration-1 routes
        # only net 1 (1 routed); iteration-2 routes nets 1,2 (2 routed).
        # total_nets = 4 so failed_nets = {4} / {2,3,4} / {3,4} respectively.
        iter0_routes = [_make_test_route(1), _make_test_route(2), _make_test_route(3)]
        iter1_routes = [_make_test_route(1)]
        iter2_routes = [_make_test_route(1), _make_test_route(2)]
        router = _RoutingFakeRouter(
            total_nets=4,
            routes_per_call=[iter0_routes, iter1_routes, iter2_routes],
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=False,
            stagnation_patience=0,  # disable stagnation so we run the full budget
        )
        self._strategy_always_applies(loop)
        result = loop.run(max_adjustments=2)  # 3 iterations total

        # Acceptance: the loop must NOT return the live last-iteration
        # state (iteration 2 = 2 routed).  It must restore iteration 0's
        # snapshot (3 routed) because that was the best observed.
        assert len(result.routes) == 3, (
            f"Issue #2840 rollback: expected 3 routes from iteration 0's "
            f"snapshot, got {len(result.routes)} (loop returned the live "
            f"final-iteration state instead of the best-known)"
        )
        # Failed nets must reflect the restored state too.
        assert sorted(result.failed_nets) == [4], (
            f"Issue #2840 rollback: failed_nets must match the restored "
            f"snapshot (iteration 0 -> net 4 failed), got {result.failed_nets}"
        )
        # The router's own state must also be restored so post-loop
        # callers reading self.router.routes see the best snapshot.
        assert len(router.routes) == 3
        assert sorted(r.net for r in router.routes) == [1, 2, 3]

    def test_route_by_route_equivalence_after_rollback(self):
        """Post-rollback routes are deep copies of the pre-pass-1 snapshot.

        Construct distinct ``Route`` objects per iteration so equivalence
        must hold on the routes' contents (net id + net_name), not on
        Python identity.  After rollback, the restored routes must:

        - Have the same ``(net, net_name)`` tuples as iter-0's routes.
        - NOT be the same Python objects (``id()``) as iter-0's originals
          -- the loop took a deep copy on iter-0 capture and a deep copy
          again on restoration.
        """
        from kicad_tools.router import PlacementFeedbackLoop

        # iter-0 = 3/4 (best); iter-1 regresses to 1/4; iter-2 stays low.
        # Avoid 4/4 on iter-0 so the success short-circuit does not fire.
        iter0_routes = [_make_test_route(i) for i in (10, 20, 30)]
        iter1_routes = [_make_test_route(10)]
        iter2_routes = [_make_test_route(10)]
        iter0_ids = {id(r) for r in iter0_routes}
        expected_signatures = sorted((r.net, r.net_name) for r in iter0_routes)

        router = _RoutingFakeRouter(
            total_nets=4,
            routes_per_call=[iter0_routes, iter1_routes, iter2_routes],
        )
        # Bind nets so get_failed_nets() returns the singleton {40} after
        # restoration (10/20/30 routed, 40 unrouted, plus net 0 ignored).
        # Total nets = 4 (excluding net 0) so len(self.nets) - 1 = 4
        # which matches the loop's ``total_nets`` math.
        router.nets = {0: [], 10: [], 20: [], 30: [], 40: []}
        assert len(router.nets) - 1 == 4

        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=False,
            stagnation_patience=0,
        )
        self._strategy_always_applies(loop)
        result = loop.run(max_adjustments=2)

        # Route-by-route equivalence on (net, net_name).
        restored_signatures = sorted((r.net, r.net_name) for r in result.routes)
        assert restored_signatures == expected_signatures, (
            f"Issue #2840 rollback: restored routes do not match iter-0 "
            f"snapshot.\n  expected: {expected_signatures}\n  got:      "
            f"{restored_signatures}"
        )
        # Deep-copy semantics: no shared object identities with iter-0.
        for r in result.routes:
            assert id(r) not in iter0_ids, (
                "Issue #2840: rollback returned routes by reference; "
                "expected deep copies so future mutation of "
                "self.router.routes does not corrupt the result"
            )

    def test_first_iteration_best_no_rollback_needed_success(self):
        """First iteration converges -> success short-circuit, no rollback log.

        Iteration 0 routes all nets (failed = []).  The loop returns via
        the ``pf_converged`` short-circuit at line 391-403, which predates
        #2840 and is unchanged.  No "Restoring best snapshot" log emitted.
        """
        from kicad_tools.router import PlacementFeedbackLoop

        # iter-0 routes all 4 nets -> failed_nets = [] -> short-circuit.
        iter0_routes = [_make_test_route(i) for i in (1, 2, 3, 4)]
        router = _RoutingFakeRouter(
            total_nets=4,
            routes_per_call=[iter0_routes],
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=False,
            stagnation_patience=0,
        )
        result = loop.run(max_adjustments=3)

        # Success short-circuit fires; no rollback path executed.
        assert result.exit_reason == "pf_converged"
        assert result.success is True
        assert result.iterations == 1
        assert len(result.routes) == 4
        assert result.failed_nets == []

    def test_no_regression_returns_last_iteration_state(self):
        """Monotonically improving routed_count -> no restore needed.

        All iterations strictly improve (3 -> 4 -> 4).  The "best" is the
        final iteration, so the restore branch is not taken.  Result must
        be the live final state.
        """
        from kicad_tools.router import PlacementFeedbackLoop

        # iter-0: 3 routed (net 4 failed); iter-1: 4 routed (success).
        iter0_routes = [_make_test_route(i) for i in (1, 2, 3)]
        iter1_routes = [_make_test_route(i) for i in (1, 2, 3, 4)]
        router = _RoutingFakeRouter(
            total_nets=4,
            routes_per_call=[iter0_routes, iter1_routes],
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=False,
            stagnation_patience=0,
        )
        self._strategy_always_applies(loop)
        result = loop.run(max_adjustments=2)

        # iter-1 short-circuits via pf_converged.
        assert result.exit_reason == "pf_converged"
        assert result.success is True
        assert len(result.routes) == 4

    def test_trajectory_log_emits_per_iteration_progress(self, capsys):
        """Verbose mode emits ``[iter N] ... improved|reverted|tied`` per pass.

        Iterates [3, 1, 2] across 3 iterations.  Captured stdout must
        contain iter-0 marked improved, iter-1 marked reverted, iter-2
        marked reverted (still below best), and a "Restoring best
        snapshot" line at the end.
        """
        from kicad_tools.router import PlacementFeedbackLoop

        iter0_routes = [_make_test_route(i) for i in (1, 2, 3)]  # 3 routed
        iter1_routes = [_make_test_route(1)]  # 1 routed
        iter2_routes = [_make_test_route(1), _make_test_route(2)]  # 2 routed
        router = _RoutingFakeRouter(
            total_nets=4,
            routes_per_call=[iter0_routes, iter1_routes, iter2_routes],
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=True,
            stagnation_patience=0,
        )
        self._strategy_always_applies(loop)
        loop.run(max_adjustments=2)
        captured = capsys.readouterr().out

        # Trajectory log lines.
        assert "[iter 0]" in captured
        assert "improved" in captured
        assert "[iter 1]" in captured
        assert "reverted" in captured
        # End-of-loop restore log line.
        assert "Restoring best snapshot" in captured
        # Restoration target points back at iteration 0.
        assert "iter 0" in captured

    def test_tied_iteration_does_not_replace_snapshot(self):
        """Strict-improvement semantics: a tied iteration keeps the earlier snapshot.

        iter-0 routes 3 nets, iter-1 routes the SAME 3 nets (same count,
        same identity).  ``best_iteration`` must stay at 0; the snapshot
        must not be replaced (so a later regression rolls back to iter 0,
        not iter 1).
        """
        from kicad_tools.router import PlacementFeedbackLoop

        iter0_routes = [_make_test_route(i) for i in (1, 2, 3)]
        iter1_routes = [_make_test_route(i) for i in (1, 2, 3)]
        iter2_routes = [_make_test_route(1)]  # regression
        router = _RoutingFakeRouter(
            total_nets=4,
            routes_per_call=[iter0_routes, iter1_routes, iter2_routes],
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router,
            pcb=pcb,
            verbose=False,
            stagnation_patience=0,
        )
        self._strategy_always_applies(loop)
        result = loop.run(max_adjustments=2)

        # 3 routes restored from the best-known snapshot.
        assert len(result.routes) == 3
        # The exit_reason is pf_max_iter (we ran all 3 iterations without
        # success), confirming no early break.
        assert result.exit_reason == "pf_max_iter"
