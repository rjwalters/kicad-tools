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
        assert (
            detect_pf_stagnation([40, 46, 46, 46, 46], patience=3) is True
        )

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
        assert (
            detect_pf_stagnation([46, 47, 46, 46, 46], patience=3) is False
        )

    def test_configurable_patience_short(self):
        """patience=2 fires on three flat entries."""
        assert detect_pf_stagnation([46, 46, 46], patience=2) is True

    def test_configurable_patience_long(self):
        """patience=5 needs six flat entries -- four is not enough."""
        assert detect_pf_stagnation([46, 46, 46, 46], patience=5) is False

    def test_configurable_patience_long_satisfied(self):
        """patience=5 satisfied by six flat entries."""
        assert (
            detect_pf_stagnation(
                [46, 46, 46, 46, 46, 46], patience=5
            )
            is True
        )

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
            actions=[
                Action(type="move", target="C1", params={"x": 0.0, "y": 0.0})
            ],
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
        loop = PlacementFeedbackLoop(
            router=router, pcb=None, verbose=False, stagnation_patience=3
        )
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
        router = _FakeAutorouter(
            total_nets=10, failed_nets_constant=[1, 2, 3, 4, 5]
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router, pcb=pcb, verbose=False, stagnation_patience=3
        )
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

        router = _FakeAutorouter(
            total_nets=10, failed_nets_constant=[1, 2, 3]
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router, pcb=pcb, verbose=False, stagnation_patience=0
        )
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
            [1, 2, 3, 4],   # routed=6
            [1, 2, 3],      # routed=7
            [1, 2],         # routed=8
            [1],            # routed=9 (still not converged)
        ]
        router = _FakeAutorouter(
            total_nets=10, failed_nets_sequence=failed_sequence
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router, pcb=pcb, verbose=False, stagnation_patience=3
        )
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

        router = _FakeAutorouter(
            total_nets=10, failed_nets_constant=[1, 2, 3]
        )
        loop = PlacementFeedbackLoop(
            router=router, pcb=None, verbose=False, stagnation_patience=10
        )
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

        router = _FakeAutorouter(
            total_nets=10, failed_nets_constant=[1, 2]
        )
        pcb = MockPCB()
        loop = PlacementFeedbackLoop(
            router=router, pcb=pcb, verbose=False, stagnation_patience=2
        )
        _AlwaysApplyLoop.patch(loop)
        result = loop.run(max_adjustments=10)
        assert result.exit_reason == "pf_stagnated"
        assert "Exit reason: pf_stagnated" in result.summary()
