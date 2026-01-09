"""Tests for via optimization in post-routing cleanup."""


from kicad_tools.router.layers import Layer
from kicad_tools.router.optimizer import (
    OptimizationConfig,
    TraceOptimizer,
)
from kicad_tools.router.optimizer.via_optimizer import (
    ViaContext,
    ViaOptimizationConfig,
    ViaOptimizationStats,
    ViaOptimizer,
    optimize_route_vias,
)
from kicad_tools.router.primitives import Route, Segment, Via


class TestViaOptimizationConfig:
    """Test ViaOptimizationConfig defaults and options."""

    def test_default_config(self):
        config = ViaOptimizationConfig()
        assert config.enabled is True
        assert config.max_detour_factor == 1.5
        assert config.via_pair_threshold == 2.0

    def test_disabled_config(self):
        config = ViaOptimizationConfig(enabled=False)
        assert config.enabled is False


class TestViaOptimizationStats:
    """Test ViaOptimizationStats calculations."""

    def test_vias_removed(self):
        stats = ViaOptimizationStats(
            vias_before=10,
            vias_after=6,
            vias_removed_single=2,
            vias_removed_pairs=2,
        )
        assert stats.vias_removed == 4

    def test_via_reduction_percent(self):
        stats = ViaOptimizationStats(vias_before=10, vias_after=6)
        # Stats track before/after, but reduction is calculated from removed
        stats.vias_removed_single = 4
        assert stats.via_reduction_percent == 40.0

    def test_no_vias_reduction(self):
        stats = ViaOptimizationStats(vias_before=0, vias_after=0)
        assert stats.via_reduction_percent == 0.0


class TestViaContext:
    """Test ViaContext helper class."""

    def test_from_layer(self):
        via = Via(
            x=10.0,
            y=20.0,
            drill=0.3,
            diameter=0.6,
            layers=(Layer.F_CU, Layer.B_CU),
            net=1,
        )
        ctx = ViaContext(via=via, via_index=0, segments_before=[], segments_after=[])
        assert ctx.from_layer == Layer.F_CU
        assert ctx.to_layer == Layer.B_CU


class TestViaOptimizerBasic:
    """Basic via optimizer tests."""

    def test_empty_route(self):
        optimizer = ViaOptimizer()
        route = Route(net=1, net_name="Net1", segments=[], vias=[])
        result = optimizer.optimize_route(route)
        assert result.vias == []
        assert result.segments == []

    def test_route_with_no_vias(self):
        optimizer = ViaOptimizer()
        segments = [
            Segment(x1=0, y1=0, x2=10, y2=0, width=0.2, layer=Layer.F_CU, net=1),
            Segment(x1=10, y1=0, x2=10, y2=10, width=0.2, layer=Layer.F_CU, net=1),
        ]
        route = Route(net=1, net_name="Net1", segments=segments, vias=[])
        result = optimizer.optimize_route(route)
        assert len(result.vias) == 0
        assert len(result.segments) == 2

    def test_disabled_optimization(self):
        config = ViaOptimizationConfig(enabled=False)
        optimizer = ViaOptimizer(config=config)

        via = Via(x=5, y=5, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        route = Route(net=1, net_name="Net1", segments=[], vias=[via])

        result = optimizer.optimize_route(route)
        assert len(result.vias) == 1


class TestViaPairElimination:
    """Test via pair (down-up) elimination."""

    def test_identify_via_pair(self):
        optimizer = ViaOptimizer()

        # Via1: F.Cu -> B.Cu, Via2: B.Cu -> F.Cu (a pair)
        via1 = Via(x=0, y=0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        via2 = Via(x=1, y=0, drill=0.3, diameter=0.6, layers=(Layer.B_CU, Layer.F_CU), net=1)

        ctx1 = ViaContext(via=via1, via_index=0, segments_before=[], segments_after=[])
        ctx2 = ViaContext(via=via2, via_index=1, segments_before=[], segments_after=[])

        assert optimizer._is_via_pair(ctx1, ctx2) is True

    def test_not_a_via_pair(self):
        optimizer = ViaOptimizer()

        # Both vias go same direction - not a pair
        via1 = Via(x=0, y=0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        via2 = Via(x=1, y=0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)

        ctx1 = ViaContext(via=via1, via_index=0, segments_before=[], segments_after=[])
        ctx2 = ViaContext(via=via2, via_index=1, segments_before=[], segments_after=[])

        assert optimizer._is_via_pair(ctx1, ctx2) is False


class TestSameLayerPath:
    """Test same-layer path finding."""

    def test_direct_path_found(self):
        optimizer = ViaOptimizer()

        # No collision checker - direct path always works
        path = optimizer._find_same_layer_path(
            x1=0, y1=0, x2=5, y2=0, layer=Layer.F_CU, width=0.2, net=1, max_detour=10.0
        )

        assert path is not None
        assert len(path) == 1
        assert path[0].x1 == 0
        assert path[0].y1 == 0
        assert path[0].x2 == 5
        assert path[0].y2 == 0
        assert path[0].layer == Layer.F_CU

    def test_path_too_long(self):
        optimizer = ViaOptimizer()

        # Distance is 10, max_detour is 5 - should fail
        path = optimizer._find_same_layer_path(
            x1=0, y1=0, x2=10, y2=0, layer=Layer.F_CU, width=0.2, net=1, max_detour=5.0
        )

        assert path is None


class TestBuildViaContexts:
    """Test building via context from route."""

    def test_segments_connected_to_via(self):
        optimizer = ViaOptimizer()

        # Segment ends at via position on from_layer
        seg_before = Segment(x1=0, y1=0, x2=5, y2=5, width=0.2, layer=Layer.F_CU, net=1)
        # Segment starts at via position on to_layer
        seg_after = Segment(x1=5, y1=5, x2=10, y2=5, width=0.2, layer=Layer.B_CU, net=1)
        via = Via(x=5, y=5, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)

        route = Route(net=1, net_name="Net1", segments=[seg_before, seg_after], vias=[via])

        contexts = optimizer._build_via_contexts(route)

        assert len(contexts) == 1
        ctx = contexts[0]
        assert ctx.via == via
        assert len(ctx.segments_before) == 1
        assert len(ctx.segments_after) == 1
        assert ctx.segments_before[0] == seg_before
        assert ctx.segments_after[0] == seg_after


class TestIntegrationWithTraceOptimizer:
    """Test via optimization integration with TraceOptimizer."""

    def test_trace_optimizer_includes_via_optimization(self):
        config = OptimizationConfig(minimize_vias=True)
        optimizer = TraceOptimizer(config=config)

        # Create a simple route with a via
        seg1 = Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        seg2 = Segment(x1=5, y1=0, x2=10, y2=0, width=0.2, layer=Layer.B_CU, net=1)
        via = Via(x=5, y=0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        route = Route(net=1, net_name="Net1", segments=[seg1, seg2], vias=[via])

        result = optimizer.optimize_route(route)

        # Via optimizer ran (segments may have been modified)
        assert isinstance(result, Route)

    def test_via_optimization_disabled(self):
        config = OptimizationConfig(minimize_vias=False)
        optimizer = TraceOptimizer(config=config)

        via = Via(x=5, y=0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        route = Route(net=1, net_name="Net1", segments=[], vias=[via])

        result = optimizer.optimize_route(route)

        # Via should remain unchanged
        assert len(result.vias) == 1

    def test_get_via_stats(self):
        config = OptimizationConfig(minimize_vias=True)
        optimizer = TraceOptimizer(config=config)

        # Reset stats
        optimizer.reset_via_stats()

        # Optimize a route with vias
        seg1 = Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        via = Via(x=5, y=0, drill=0.3, diameter=0.6, layers=(Layer.F_CU, Layer.B_CU), net=1)
        route = Route(net=1, net_name="Net1", segments=[seg1], vias=[via])
        optimizer.optimize_route(route)

        stats = optimizer.get_via_stats()
        assert "vias_before" in stats
        assert "vias_after" in stats
        assert "vias_removed" in stats


class TestOptimizeRouteViasFunction:
    """Test the convenience function."""

    def test_optimize_route_vias(self):
        seg1 = Segment(x1=0, y1=0, x2=5, y2=0, width=0.2, layer=Layer.F_CU, net=1)
        route = Route(net=1, net_name="Net1", segments=[seg1], vias=[])

        optimized, stats = optimize_route_vias(route)

        assert isinstance(optimized, Route)
        assert isinstance(stats, ViaOptimizationStats)


class TestOptimizationStatsIntegration:
    """Test OptimizationStats via fields."""

    def test_stats_has_via_fields(self):
        from kicad_tools.router.optimizer.config import OptimizationStats

        stats = OptimizationStats(
            segments_before=10,
            segments_after=8,
            vias_before=5,
            vias_after=3,
        )

        assert stats.vias_before == 5
        assert stats.vias_after == 3
        assert stats.via_reduction == 40.0
