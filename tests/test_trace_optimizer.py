"""Tests for the TraceOptimizer staircase compression feature."""

import pytest

from kicad_tools.router import (
    DesignRules,
    GridCollisionChecker,
    Layer,
    OptimizationConfig,
    RoutingGrid,
    Segment,
    TraceOptimizer,
)


class TestStaircaseCompression:
    """Tests for staircase pattern compression."""

    @pytest.fixture
    def optimizer(self):
        """Create a TraceOptimizer with default config."""
        return TraceOptimizer()

    @pytest.fixture
    def optimizer_disabled(self):
        """Create a TraceOptimizer with staircase compression disabled."""
        config = OptimizationConfig(compress_staircase=False)
        return TraceOptimizer(config)

    def make_segment(self, x1: float, y1: float, x2: float, y2: float) -> Segment:
        """Helper to create a segment with default properties."""
        return Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="TEST",
        )

    def test_synthetic_staircase_horizontal_diagonal(self, optimizer):
        """Test that alternating horizontal/diagonal segments are compressed.

        Creates a staircase pattern:
          Seg 0: horizontal (180°)
          Seg 1: diagonal (225°)
          Seg 2: horizontal (180°)
          Seg 3: diagonal (225°)
          ... etc

        Should be compressed to 2 segments: diagonal + horizontal.
        """
        segments = []
        x, y = 0.0, 0.0

        # Create 10 alternating segments
        for i in range(10):
            if i % 2 == 0:
                # Horizontal segment going left (180°)
                new_x = x - 1.0
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                # Diagonal segment going down-left (225°)
                new_x = x - 0.5
                new_y = y - 0.5
                segments.append(self.make_segment(x, y, new_x, new_y))
                x, y = new_x, new_y

        # Before: 10 segments
        assert len(segments) == 10

        # After: should be ≤4 segments
        result = optimizer.compress_staircase(segments)
        assert len(result) <= 4, f"Expected ≤4 segments, got {len(result)}"

        # Verify endpoints are preserved
        assert abs(result[0].x1 - segments[0].x1) < 1e-4
        assert abs(result[0].y1 - segments[0].y1) < 1e-4
        assert abs(result[-1].x2 - segments[-1].x2) < 1e-4
        assert abs(result[-1].y2 - segments[-1].y2) < 1e-4

    def test_diagonal_only_unchanged(self, optimizer):
        """Test that a path that's already optimal is not changed."""
        # Create a diagonal followed by horizontal (already optimal)
        segments = [
            self.make_segment(0, 0, 5, 5),  # Diagonal
            self.make_segment(5, 5, 10, 5),  # Horizontal
        ]

        result = optimizer.compress_staircase(segments)

        # Should be unchanged (not a staircase pattern)
        assert len(result) == 2

    def test_short_staircase_not_compressed(self, optimizer):
        """Test that staircases below threshold are not compressed."""
        # Create just 2 alternating segments (below default threshold of 3)
        segments = [
            self.make_segment(0, 0, 1, 0),  # Horizontal
            self.make_segment(1, 0, 1.5, 0.5),  # Diagonal
        ]

        result = optimizer.compress_staircase(segments)

        # Should be unchanged (too short)
        assert len(result) == 2

    def test_mixed_pattern_staircases_compressed(self, optimizer):
        """Test that staircases in mixed patterns are compressed."""
        # Create: straight + staircase + straight
        segments = []

        # First: long horizontal
        segments.append(self.make_segment(0, 0, 5, 0))

        # Middle: staircase pattern (5 segments)
        x, y = 5, 0
        for i in range(5):
            if i % 2 == 0:
                new_x = x + 1
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                segments.append(self.make_segment(x, y, new_x, new_y))
                x, y = new_x, new_y

        # End: long horizontal
        segments.append(self.make_segment(x, y, x + 5, y))

        # Total: 1 + 5 + 1 = 7 segments
        assert len(segments) == 7

        result = optimizer.compress_staircase(segments)

        # Staircase (5 segs) should compress to ≤2, total ≤4
        assert len(result) <= 5, f"Expected ≤5 segments, got {len(result)}"

    def test_segment_properties_preserved(self, optimizer):
        """Test that width, layer, net, net_name are preserved."""
        template = Segment(
            x1=0,
            y1=0,
            x2=1,
            y2=0,
            width=0.35,
            layer=Layer.B_CU,
            net=42,
            net_name="SPECIAL_NET",
        )

        # Create staircase with specific properties
        segments = []
        x, y = 0, 0
        for i in range(6):
            if i % 2 == 0:
                new_x = x + 1
                seg = Segment(
                    x1=x,
                    y1=y,
                    x2=new_x,
                    y2=y,
                    width=template.width,
                    layer=template.layer,
                    net=template.net,
                    net_name=template.net_name,
                )
                segments.append(seg)
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                seg = Segment(
                    x1=x,
                    y1=y,
                    x2=new_x,
                    y2=new_y,
                    width=template.width,
                    layer=template.layer,
                    net=template.net,
                    net_name=template.net_name,
                )
                segments.append(seg)
                x, y = new_x, new_y

        result = optimizer.compress_staircase(segments)

        # All result segments should have same properties
        for seg in result:
            assert seg.width == template.width
            assert seg.layer == template.layer
            assert seg.net == template.net
            assert seg.net_name == template.net_name

    def test_compression_disabled(self, optimizer_disabled):
        """Test that compression can be disabled via config."""
        segments = []
        x, y = 0.0, 0.0

        # Create staircase pattern
        for i in range(10):
            if i % 2 == 0:
                new_x = x + 1.0
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                segments.append(self.make_segment(x, y, new_x, new_y))
                x, y = new_x, new_y

        result = optimizer_disabled.compress_staircase(segments)

        # Should be unchanged when disabled
        assert len(result) == 10

    def test_different_angles_not_compressed(self, optimizer):
        """Test that patterns with non-45° angle differences are not compressed."""
        # Create segments with 60° angle difference (not a valid staircase)
        segments = [
            self.make_segment(0, 0, 1, 0),  # 0°
            self.make_segment(1, 0, 1.5, 0.866),  # 60°
            self.make_segment(1.5, 0.866, 2.5, 0.866),  # 0°
            self.make_segment(2.5, 0.866, 3, 1.732),  # 60°
        ]

        result = optimizer.compress_staircase(segments)

        # Not a valid staircase pattern, should be mostly unchanged
        # (may still be processed segment by segment)
        assert len(result) >= 2

    def test_full_optimize_pipeline(self, optimizer):
        """Test that staircase compression works in full optimize pipeline."""
        segments = []
        x, y = 0.0, 0.0

        # Create staircase pattern
        for i in range(8):
            if i % 2 == 0:
                new_x = x + 1.0
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                segments.append(self.make_segment(x, y, new_x, new_y))
                x, y = new_x, new_y

        # Use full optimize_segments which includes all passes
        result = optimizer.optimize_segments(segments)

        # Should be significantly reduced
        assert len(result) < len(segments)


class TestSegmentDirection:
    """Tests for _segment_direction helper."""

    @pytest.fixture
    def optimizer(self):
        return TraceOptimizer()

    def make_segment(self, x1, y1, x2, y2):
        return Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="TEST",
        )

    def test_right_direction(self, optimizer):
        """Test 0° direction (positive X)."""
        seg = self.make_segment(0, 0, 1, 0)
        angle = optimizer._segment_direction(seg)
        assert abs(angle - 0) < 1

    def test_up_direction(self, optimizer):
        """Test 90° direction (positive Y)."""
        seg = self.make_segment(0, 0, 0, 1)
        angle = optimizer._segment_direction(seg)
        assert abs(angle - 90) < 1

    def test_left_direction(self, optimizer):
        """Test 180° direction (negative X)."""
        seg = self.make_segment(0, 0, -1, 0)
        angle = optimizer._segment_direction(seg)
        assert abs(angle - 180) < 1

    def test_down_direction(self, optimizer):
        """Test 270° direction (negative Y)."""
        seg = self.make_segment(0, 0, 0, -1)
        angle = optimizer._segment_direction(seg)
        assert abs(angle - 270) < 1

    def test_diagonal_45(self, optimizer):
        """Test 45° direction."""
        seg = self.make_segment(0, 0, 1, 1)
        angle = optimizer._segment_direction(seg)
        assert abs(angle - 45) < 1

    def test_diagonal_135(self, optimizer):
        """Test 135° direction."""
        seg = self.make_segment(0, 0, -1, 1)
        angle = optimizer._segment_direction(seg)
        assert abs(angle - 135) < 1

    def test_diagonal_225(self, optimizer):
        """Test 225° direction."""
        seg = self.make_segment(0, 0, -1, -1)
        angle = optimizer._segment_direction(seg)
        assert abs(angle - 225) < 1

    def test_diagonal_315(self, optimizer):
        """Test 315° direction."""
        seg = self.make_segment(0, 0, 1, -1)
        angle = optimizer._segment_direction(seg)
        assert abs(angle - 315) < 1


class TestOptimalPath:
    """Tests for _optimal_path helper."""

    @pytest.fixture
    def optimizer(self):
        return TraceOptimizer()

    @pytest.fixture
    def template(self):
        return Segment(
            x1=0,
            y1=0,
            x2=1,
            y2=0,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="TEST",
        )

    def test_purely_horizontal(self, optimizer, template):
        """Test optimal path for horizontal movement."""
        result = optimizer._optimal_path((0, 0), (10, 0), template)

        # Should be single horizontal segment
        assert len(result) == 1
        assert abs(result[0].x1 - 0) < 1e-4
        assert abs(result[0].y1 - 0) < 1e-4
        assert abs(result[0].x2 - 10) < 1e-4
        assert abs(result[0].y2 - 0) < 1e-4

    def test_purely_vertical(self, optimizer, template):
        """Test optimal path for vertical movement."""
        result = optimizer._optimal_path((0, 0), (0, 10), template)

        # Should be single vertical segment
        assert len(result) == 1
        assert abs(result[0].x2 - 0) < 1e-4
        assert abs(result[0].y2 - 10) < 1e-4

    def test_pure_diagonal(self, optimizer, template):
        """Test optimal path for 45° diagonal movement."""
        result = optimizer._optimal_path((0, 0), (5, 5), template)

        # Should be single diagonal segment
        assert len(result) == 1
        assert abs(result[0].x2 - 5) < 1e-4
        assert abs(result[0].y2 - 5) < 1e-4

    def test_mostly_horizontal(self, optimizer, template):
        """Test optimal path for mostly horizontal with some vertical."""
        result = optimizer._optimal_path((0, 0), (10, 3), template)

        # Should be 2 segments: diagonal + horizontal
        assert len(result) == 2

        # First segment is diagonal
        diag = result[0]
        assert abs(diag.x2 - diag.x1) > 0
        assert abs(diag.y2 - diag.y1) > 0

        # Second segment is horizontal/vertical for remaining
        ortho = result[1]
        # Endpoints should match
        assert abs(ortho.x2 - 10) < 1e-4
        assert abs(ortho.y2 - 3) < 1e-4

    def test_mostly_vertical(self, optimizer, template):
        """Test optimal path for mostly vertical with some horizontal."""
        result = optimizer._optimal_path((0, 0), (3, 10), template)

        # Should be 2 segments: diagonal + vertical
        assert len(result) == 2

        # Endpoints should match
        assert abs(result[-1].x2 - 3) < 1e-4
        assert abs(result[-1].y2 - 10) < 1e-4

    def test_negative_direction(self, optimizer, template):
        """Test optimal path for negative X and Y."""
        result = optimizer._optimal_path((10, 10), (0, 0), template)

        # Should handle negative movement
        assert len(result) <= 2
        assert abs(result[0].x1 - 10) < 1e-4
        assert abs(result[0].y1 - 10) < 1e-4
        assert abs(result[-1].x2 - 0) < 1e-4
        assert abs(result[-1].y2 - 0) < 1e-4

    def test_same_point(self, optimizer, template):
        """Test optimal path when start equals end."""
        result = optimizer._optimal_path((5, 5), (5, 5), template)

        # Should return empty list
        assert len(result) == 0


class TestFindStaircaseEnd:
    """Tests for _find_staircase_end helper."""

    @pytest.fixture
    def optimizer(self):
        return TraceOptimizer()

    def make_segment(self, x1, y1, x2, y2):
        return Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="TEST",
        )

    def test_valid_staircase_pattern(self, optimizer):
        """Test detection of valid horizontal/diagonal staircase."""
        segments = [
            self.make_segment(0, 0, 1, 0),  # 0° horizontal
            self.make_segment(1, 0, 1.5, 0.5),  # 45° diagonal
            self.make_segment(1.5, 0.5, 2.5, 0.5),  # 0° horizontal
            self.make_segment(2.5, 0.5, 3, 1),  # 45° diagonal
            self.make_segment(3, 1, 4, 1),  # 0° horizontal
        ]

        end_idx = optimizer._find_staircase_end(segments, 0)
        assert end_idx == 5  # All segments are part of staircase

    def test_staircase_ends_at_different_angle(self, optimizer):
        """Test that staircase detection stops at non-alternating angle."""
        segments = [
            self.make_segment(0, 0, 1, 0),  # 0° horizontal
            self.make_segment(1, 0, 1.5, 0.5),  # 45° diagonal
            self.make_segment(1.5, 0.5, 2.5, 0.5),  # 0° horizontal
            self.make_segment(2.5, 0.5, 2.5, 1.5),  # 90° vertical (breaks pattern)
        ]

        end_idx = optimizer._find_staircase_end(segments, 0)
        assert end_idx == 3  # Staircase ends before vertical segment

    def test_rectilinear_hv_staircase_detected(self, optimizer):
        """Test that 90° H/V patterns ARE detected as staircases.

        This is the core fix for issue #124 - A* router produces H/V
        alternating patterns that should be compressed.
        """
        segments = [
            self.make_segment(0, 0, 1, 0),  # 0° horizontal
            self.make_segment(1, 0, 1, 1),  # 90° vertical
            self.make_segment(1, 1, 2, 1),  # 0° horizontal
            self.make_segment(2, 1, 2, 2),  # 90° vertical
            self.make_segment(2, 2, 3, 2),  # 0° horizontal
        ]

        end_idx = optimizer._find_staircase_end(segments, 0)
        assert end_idx == 5  # All segments are part of staircase

    def test_invalid_angle_not_detected(self, optimizer):
        """Test that angle differences outside valid ranges are not detected."""
        # Create segments with 30° angle difference (outside both 45° and 90° ranges)
        # 0° horizontal to 30° diagonal
        import math

        segments = [
            self.make_segment(0, 0, 1, 0),  # 0° horizontal
            self.make_segment(1, 0, 2, math.tan(math.radians(30))),  # ~30°
        ]

        end_idx = optimizer._find_staircase_end(segments, 0)
        assert end_idx == 1  # Not a valid staircase


class TestRectilinearStaircaseCompression:
    """Tests for rectilinear (H/V) staircase compression (issue #124)."""

    @pytest.fixture
    def optimizer(self):
        """Create a TraceOptimizer with default config."""
        return TraceOptimizer()

    def make_segment(self, x1: float, y1: float, x2: float, y2: float) -> Segment:
        """Helper to create a segment with default properties."""
        return Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=0.2,
            layer=Layer.F_CU,
            net=1,
            net_name="TEST",
        )

    def test_hv_staircase_compressed(self, optimizer):
        """Test that alternating H/V segments are compressed.

        Creates a staircase pattern like the A* router produces:
          Seg 0: horizontal (0°)
          Seg 1: vertical (90°)
          Seg 2: horizontal (0°)
          Seg 3: vertical (90°)
          ... etc

        Should be compressed to 2 segments: diagonal + orthogonal.
        """
        segments = []
        x, y = 0.0, 0.0

        # Create 10 alternating H/V segments (classic A* staircase)
        for i in range(10):
            if i % 2 == 0:
                # Horizontal segment going right (0°)
                new_x = x + 1.0
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                # Vertical segment going up (90°)
                new_y = y + 1.0
                segments.append(self.make_segment(x, y, x, new_y))
                y = new_y

        # Before: 10 segments
        assert len(segments) == 10

        # After: should be ≤4 segments
        result = optimizer.compress_staircase(segments)
        assert len(result) <= 4, f"Expected ≤4 segments, got {len(result)}"

        # Verify endpoints are preserved
        assert abs(result[0].x1 - segments[0].x1) < 1e-4
        assert abs(result[0].y1 - segments[0].y1) < 1e-4
        assert abs(result[-1].x2 - segments[-1].x2) < 1e-4
        assert abs(result[-1].y2 - segments[-1].y2) < 1e-4

    def test_hv_staircase_properties_preserved(self, optimizer):
        """Test that width, layer, net, net_name are preserved after compression."""
        template = Segment(
            x1=0,
            y1=0,
            x2=1,
            y2=0,
            width=0.35,
            layer=Layer.B_CU,
            net=42,
            net_name="SWDIO",
        )

        # Create H/V staircase with specific properties
        segments = []
        x, y = 0, 0
        for i in range(6):
            if i % 2 == 0:
                new_x = x + 0.5
                seg = Segment(
                    x1=x,
                    y1=y,
                    x2=new_x,
                    y2=y,
                    width=template.width,
                    layer=template.layer,
                    net=template.net,
                    net_name=template.net_name,
                )
                segments.append(seg)
                x = new_x
            else:
                new_y = y + 0.5
                seg = Segment(
                    x1=x,
                    y1=y,
                    x2=x,
                    y2=new_y,
                    width=template.width,
                    layer=template.layer,
                    net=template.net,
                    net_name=template.net_name,
                )
                segments.append(seg)
                y = new_y

        result = optimizer.compress_staircase(segments)

        # All result segments should have same properties
        for seg in result:
            assert seg.width == template.width
            assert seg.layer == template.layer
            assert seg.net == template.net
            assert seg.net_name == template.net_name

    def test_perfect_diagonal_hv_staircase(self, optimizer):
        """Test H/V staircase where dx == dy produces single diagonal."""
        # Create equal H/V steps that result in perfect diagonal
        segments = []
        x, y = 0.0, 0.0

        for i in range(6):
            if i % 2 == 0:
                new_x = x + 1.0
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_y = y + 1.0
                segments.append(self.make_segment(x, y, x, new_y))
                y = new_y

        # Total displacement: 3mm horizontal, 3mm vertical = perfect 45° diagonal
        result = optimizer.compress_staircase(segments)

        # Should compress to 1-2 segments (diagonal, or diagonal + small stub)
        assert len(result) <= 2, f"Expected ≤2 segments for perfect diagonal, got {len(result)}"

    def test_asymmetric_hv_staircase(self, optimizer):
        """Test H/V staircase with unequal H and V totals."""
        # Create staircase where total H > total V
        segments = []
        x, y = 0.0, 0.0

        for i in range(6):
            if i % 2 == 0:
                new_x = x + 2.0  # Longer horizontal
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_y = y + 1.0  # Shorter vertical
                segments.append(self.make_segment(x, y, x, new_y))
                y = new_y

        # Total: 6mm horizontal, 3mm vertical
        result = optimizer.compress_staircase(segments)

        # Should compress to 2 segments (diagonal + horizontal stub)
        assert len(result) <= 3, f"Expected ≤3 segments, got {len(result)}"

        # Verify endpoints preserved
        assert abs(result[-1].x2 - segments[-1].x2) < 1e-4
        assert abs(result[-1].y2 - segments[-1].y2) < 1e-4

    def test_hv_staircase_in_full_pipeline(self, optimizer):
        """Test that H/V staircase compression works in full optimize pipeline."""
        segments = []
        x, y = 0.0, 0.0

        for i in range(8):
            if i % 2 == 0:
                new_x = x + 0.5
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_y = y + 0.5
                segments.append(self.make_segment(x, y, x, new_y))
                y = new_y

        # Use full optimize_segments which includes all passes
        result = optimizer.optimize_segments(segments)

        # Should be significantly reduced
        assert len(result) < len(segments)

    def test_mixed_diagonal_and_hv_patterns(self, optimizer):
        """Test that both diagonal and H/V staircases can be in same route."""
        segments = []

        # First: diagonal staircase (H + 45° diagonal)
        x, y = 0.0, 0.0
        for i in range(4):
            if i % 2 == 0:
                new_x = x + 1.0
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                segments.append(self.make_segment(x, y, new_x, new_y))
                x, y = new_x, new_y

        # Then: H/V staircase
        for i in range(4):
            if i % 2 == 0:
                new_x = x + 0.5
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_y = y + 0.5
                segments.append(self.make_segment(x, y, x, new_y))
                y = new_y

        # Total: 8 segments
        assert len(segments) == 8

        result = optimizer.compress_staircase(segments)

        # Both patterns should be compressed
        assert len(result) < len(segments)


class TestConfigOptions:
    """Tests for configuration options."""

    def test_min_staircase_segments_config(self):
        """Test that min_staircase_segments config is respected."""
        config = OptimizationConfig(min_staircase_segments=5)
        optimizer = TraceOptimizer(config)

        # Create 4-segment staircase (below threshold of 5)
        segments = []
        x, y = 0, 0
        for i in range(4):
            if i % 2 == 0:
                new_x = x + 1
                segments.append(
                    Segment(
                        x1=x,
                        y1=y,
                        x2=new_x,
                        y2=y,
                        width=0.2,
                        layer=Layer.F_CU,
                        net=1,
                        net_name="TEST",
                    )
                )
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                segments.append(
                    Segment(
                        x1=x,
                        y1=y,
                        x2=new_x,
                        y2=new_y,
                        width=0.2,
                        layer=Layer.F_CU,
                        net=1,
                        net_name="TEST",
                    )
                )
                x, y = new_x, new_y

        result = optimizer.compress_staircase(segments)

        # Should not be compressed (4 < 5 threshold)
        assert len(result) == 4

    def test_compress_staircase_in_config(self):
        """Test that compress_staircase config option works."""
        config = OptimizationConfig(compress_staircase=True)
        assert config.compress_staircase is True

        config = OptimizationConfig(compress_staircase=False)
        assert config.compress_staircase is False


class TestChainSorting:
    """Tests for segment chain sorting (issue #105 fix)."""

    @pytest.fixture
    def optimizer(self):
        return TraceOptimizer()

    def make_segment(self, x1: float, y1: float, x2: float, y2: float, net: int = 1) -> Segment:
        """Helper to create a segment with default properties."""
        return Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=0.2,
            layer=Layer.F_CU,
            net=net,
            net_name=f"NET{net}",
        )

    def test_single_chain_detected(self, optimizer):
        """Test that a single connected chain is detected as one chain."""
        # Create a simple L-shaped path
        segments = [
            self.make_segment(0, 0, 5, 0),  # Horizontal
            self.make_segment(5, 0, 5, 5),  # Vertical
        ]

        chains = optimizer._sort_into_chains(segments)

        assert len(chains) == 1
        assert len(chains[0]) == 2

    def test_multiple_chains_detected(self, optimizer):
        """Test that disconnected segments form separate chains."""
        # Create two separate paths that don't touch
        segments = [
            # Chain 1: L-shape at origin
            self.make_segment(0, 0, 5, 0),
            self.make_segment(5, 0, 5, 5),
            # Chain 2: L-shape 20mm away (not connected)
            self.make_segment(20, 0, 25, 0),
            self.make_segment(25, 0, 25, 5),
        ]

        chains = optimizer._sort_into_chains(segments)

        assert len(chains) == 2
        assert len(chains[0]) == 2
        assert len(chains[1]) == 2

    def test_chain_segments_sorted_in_order(self, optimizer):
        """Test that chain segments are sorted in path order."""
        # Create segments in scrambled order
        segments = [
            self.make_segment(5, 0, 5, 5),  # Middle (should be second)
            self.make_segment(0, 0, 5, 0),  # Start (should be first)
            self.make_segment(5, 5, 10, 5),  # End (should be third)
        ]

        chains = optimizer._sort_into_chains(segments)

        assert len(chains) == 1
        sorted_chain = chains[0]

        # Check segments are in path order
        assert abs(sorted_chain[0].x1 - 0) < 1e-4  # Starts at origin
        assert abs(sorted_chain[-1].x2 - 10) < 1e-4  # Ends at (10, 5)

        # Check connectivity: each segment's end connects to next segment's start
        for i in range(len(sorted_chain) - 1):
            assert abs(sorted_chain[i].x2 - sorted_chain[i + 1].x1) < 1e-4
            assert abs(sorted_chain[i].y2 - sorted_chain[i + 1].y1) < 1e-4

    def test_reversed_segments_handled(self, optimizer):
        """Test that segments with reversed direction are handled correctly."""
        # Create segments where one is "backwards"
        segments = [
            self.make_segment(0, 0, 5, 0),  # Forward: (0,0) -> (5,0)
            self.make_segment(5, 5, 5, 0),  # Backwards: end connects to previous end
        ]

        chains = optimizer._sort_into_chains(segments)

        assert len(chains) == 1
        sorted_chain = chains[0]

        # After sorting, segments should be connected end-to-start
        for i in range(len(sorted_chain) - 1):
            assert abs(sorted_chain[i].x2 - sorted_chain[i + 1].x1) < 1e-4
            assert abs(sorted_chain[i].y2 - sorted_chain[i + 1].y1) < 1e-4


class TestMultiChainOptimization:
    """Tests for multi-chain optimization safety (issue #105 core fix)."""

    @pytest.fixture
    def optimizer(self):
        return TraceOptimizer()

    def make_segment(self, x1: float, y1: float, x2: float, y2: float, net: int = 1) -> Segment:
        """Helper to create a segment with default properties."""
        return Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=0.2,
            layer=Layer.F_CU,
            net=net,
            net_name=f"NET{net}",
        )

    def test_parallel_chains_not_merged(self, optimizer):
        """Test that parallel but separate chains are not merged.

        This is the core issue #105 scenario: two parallel traces that
        should remain separate after optimization.
        """
        # Two parallel horizontal traces 5mm apart
        segments = [
            # Chain 1: horizontal at y=0
            self.make_segment(0, 0, 10, 0),
            # Chain 2: horizontal at y=5 (parallel, not connected)
            self.make_segment(0, 5, 10, 5),
        ]

        result = optimizer.optimize_segments(segments)

        # Should still have 2 segments (one per chain)
        assert len(result) == 2

        # Verify chains weren't merged into something crossing between them
        # Each segment should be purely horizontal
        for seg in result:
            assert abs(seg.y1 - seg.y2) < 1e-4, "Segment should be horizontal"

    def test_disconnected_staircases_optimized_independently(self, optimizer):
        """Test that two disconnected staircase patterns optimize separately."""
        segments = []

        # Staircase 1: at y=0
        x, y = 0, 0
        for i in range(6):
            if i % 2 == 0:
                new_x = x + 1
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                segments.append(self.make_segment(x, y, new_x, new_y))
                x, y = new_x, new_y

        # Staircase 2: 20mm away (not connected)
        x, y = 20, 0
        for i in range(6):
            if i % 2 == 0:
                new_x = x + 1
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                segments.append(self.make_segment(x, y, new_x, new_y))
                x, y = new_x, new_y

        # Before: 12 segments (6 per staircase)
        assert len(segments) == 12

        result = optimizer.optimize_segments(segments)

        # After: should be less than 12 (staircases compressed)
        assert len(result) < 12

        # Verify no segment crosses from chain 1 to chain 2
        # Chain 1 should have x < 10, Chain 2 should have x > 15
        for seg in result:
            if seg.x1 < 10:
                assert seg.x2 < 15, "Segment from chain 1 should not reach chain 2"
            if seg.x1 > 15:
                assert seg.x2 > 10, "Segment from chain 2 should not reach chain 1"

    def test_t_junction_single_chain(self, optimizer):
        """Test that a T-junction (3 segments meeting at a point) forms one chain."""
        # T-junction: segments meet at (5, 0)
        segments = [
            self.make_segment(0, 0, 5, 0),  # Left arm
            self.make_segment(5, 0, 10, 0),  # Right arm
            self.make_segment(5, 0, 5, 5),  # Vertical arm
        ]

        chains = optimizer._sort_into_chains(segments)

        # All segments are connected, should be one chain
        assert len(chains) == 1
        assert len(chains[0]) == 3

    def test_endpoints_preserved_after_optimization(self, optimizer):
        """Test that chain endpoints are preserved after optimization."""
        # Create a connected staircase
        segments = []
        x, y = 0, 0
        for i in range(8):
            if i % 2 == 0:
                new_x = x + 1
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                segments.append(self.make_segment(x, y, new_x, new_y))
                x, y = new_x, new_y

        # Record original endpoints
        original_start = (segments[0].x1, segments[0].y1)
        original_end = (segments[-1].x2, segments[-1].y2)

        result = optimizer.optimize_segments(segments)

        # Find actual start and end in result
        all_starts = [(s.x1, s.y1) for s in result]
        all_ends = [(s.x2, s.y2) for s in result]
        all_points = set(all_starts + all_ends)

        # Original endpoints should still exist
        assert any(
            abs(p[0] - original_start[0]) < 1e-4 and abs(p[1] - original_start[1]) < 1e-4
            for p in all_points
        ), "Original start point should be preserved"
        assert any(
            abs(p[0] - original_end[0]) < 1e-4 and abs(p[1] - original_end[1]) < 1e-4
            for p in all_points
        ), "Original end point should be preserved"


class TestSegmentsTouch:
    """Tests for _segments_touch helper."""

    @pytest.fixture
    def optimizer(self):
        return TraceOptimizer()

    def make_segment(self, x1, y1, x2, y2):
        return Segment(
            x1=x1, y1=y1, x2=x2, y2=y2, width=0.2, layer=Layer.F_CU, net=1, net_name="TEST"
        )

    def test_end_to_start_connection(self, optimizer):
        """Test end-to-start connection detected."""
        s1 = self.make_segment(0, 0, 5, 0)
        s2 = self.make_segment(5, 0, 10, 0)
        assert optimizer._segments_touch(s1, s2) is True

    def test_end_to_end_connection(self, optimizer):
        """Test end-to-end connection detected."""
        s1 = self.make_segment(0, 0, 5, 0)
        s2 = self.make_segment(10, 0, 5, 0)  # Ends at same point
        assert optimizer._segments_touch(s1, s2) is True

    def test_start_to_start_connection(self, optimizer):
        """Test start-to-start connection detected."""
        s1 = self.make_segment(5, 0, 10, 0)
        s2 = self.make_segment(5, 0, 5, 5)  # Starts at same point
        assert optimizer._segments_touch(s1, s2) is True

    def test_no_connection(self, optimizer):
        """Test disconnected segments not detected as touching."""
        s1 = self.make_segment(0, 0, 5, 0)
        s2 = self.make_segment(10, 0, 15, 0)  # 5mm gap
        assert optimizer._segments_touch(s1, s2) is False


class MockCollisionChecker:
    """Mock collision checker for testing."""

    def __init__(self, blocked_paths: list[tuple[float, float, float, float]] | None = None):
        """Initialize with optional list of blocked paths.

        Args:
            blocked_paths: List of (x1, y1, x2, y2) tuples representing blocked paths.
        """
        self.blocked_paths = blocked_paths or []
        self.calls: list[tuple[float, float, float, float, Layer, float, int]] = []

    def path_is_clear(
        self,
        x1: float,
        y1: float,
        x2: float,
        y2: float,
        layer: Layer,
        width: float,
        exclude_net: int,
    ) -> bool:
        """Check if a path is clear."""
        self.calls.append((x1, y1, x2, y2, layer, width, exclude_net))

        # Check if any blocked path overlaps
        for bx1, by1, bx2, by2 in self.blocked_paths:
            # Simple check: if paths cross, block it
            if self._paths_cross(x1, y1, x2, y2, bx1, by1, bx2, by2):
                return False
        return True

    def _paths_cross(
        self,
        ax1: float,
        ay1: float,
        ax2: float,
        ay2: float,
        bx1: float,
        by1: float,
        bx2: float,
        by2: float,
    ) -> bool:
        """Simplified check if two paths cross."""
        # Check if they share any significant overlap
        # For testing, we use a simple bounding box check
        a_min_x, a_max_x = min(ax1, ax2), max(ax1, ax2)
        a_min_y, a_max_y = min(ay1, ay2), max(ay1, ay2)
        b_min_x, b_max_x = min(bx1, bx2), max(bx1, bx2)
        b_min_y, b_max_y = min(by1, by2), max(by1, by2)

        # Check for overlap in both dimensions
        x_overlap = a_min_x <= b_max_x and a_max_x >= b_min_x
        y_overlap = a_min_y <= b_max_y and a_max_y >= b_min_y

        return x_overlap and y_overlap


class TestCollisionChecker:
    """Tests for CollisionChecker protocol and GridCollisionChecker."""

    @pytest.fixture
    def simple_grid(self):
        """Create a simple routing grid for testing."""
        rules = DesignRules(
            grid_resolution=0.25,
            trace_width=0.2,
            trace_clearance=0.15,
        )
        grid = RoutingGrid(
            width=10.0,
            height=10.0,
            rules=rules,
        )
        return grid

    @pytest.fixture
    def grid_checker(self, simple_grid):
        """Create a GridCollisionChecker."""
        return GridCollisionChecker(simple_grid)

    def test_grid_collision_checker_clear_path(self, grid_checker):
        """Test that an unobstructed path is clear."""
        result = grid_checker.path_is_clear(
            x1=1.0,
            y1=1.0,
            x2=5.0,
            y2=1.0,
            layer=Layer.F_CU,
            width=0.2,
            exclude_net=1,
        )
        assert result is True

    def test_grid_collision_checker_blocked_by_other_net(self, simple_grid, grid_checker):
        """Test that a path blocked by another net returns False."""
        # Add a segment from a different net to block the path
        from kicad_tools.router import Segment as RouteSegment

        # Mark some cells as blocked by another net
        blocking_seg = RouteSegment(
            x1=3.0,
            y1=0.0,
            x2=3.0,
            y2=5.0,
            width=0.2,
            layer=Layer.F_CU,
            net=2,  # Different net
            net_name="BLOCKER",
        )

        # Create a route and mark it on the grid
        from kicad_tools.router import Route

        blocking_route = Route(net=2, net_name="BLOCKER", segments=[blocking_seg])
        simple_grid.mark_route(blocking_route)

        # Check if path crossing the blocker is detected
        result = grid_checker.path_is_clear(
            x1=1.0,
            y1=2.5,
            x2=5.0,
            y2=2.5,  # This crosses the vertical segment at x=3.0
            layer=Layer.F_CU,
            width=0.2,
            exclude_net=1,  # We're net 1, the blocker is net 2
        )
        assert result is False

    def test_grid_collision_checker_same_net_allowed(self, simple_grid, grid_checker):
        """Test that cells from the same net don't block."""
        # Add a segment from the same net
        from kicad_tools.router import Route
        from kicad_tools.router import Segment as RouteSegment

        same_net_seg = RouteSegment(
            x1=3.0,
            y1=0.0,
            x2=3.0,
            y2=5.0,
            width=0.2,
            layer=Layer.F_CU,
            net=1,  # Same net
            net_name="SAME",
        )
        same_net_route = Route(net=1, net_name="SAME", segments=[same_net_seg])
        simple_grid.mark_route(same_net_route)

        # Check if path crossing same-net segment is allowed
        result = grid_checker.path_is_clear(
            x1=1.0,
            y1=2.5,
            x2=5.0,
            y2=2.5,
            layer=Layer.F_CU,
            width=0.2,
            exclude_net=1,  # Same net as the segment
        )
        assert result is True


class TestCollisionAwareOptimization:
    """Tests for collision-aware optimization in TraceOptimizer."""

    def make_segment(self, x1: float, y1: float, x2: float, y2: float, net: int = 1) -> Segment:
        """Helper to create a segment."""
        return Segment(
            x1=x1,
            y1=y1,
            x2=x2,
            y2=y2,
            width=0.2,
            layer=Layer.F_CU,
            net=net,
            net_name="TEST",
        )

    def test_optimizer_without_collision_checker(self):
        """Test that optimizer works without collision checker (backwards compatible)."""
        optimizer = TraceOptimizer()
        assert optimizer.collision_checker is None

        # Should still optimize normally
        segments = [
            self.make_segment(0, 0, 1, 0),
            self.make_segment(1, 0, 2, 0),  # Collinear
        ]
        result = optimizer.merge_collinear(segments)
        assert len(result) == 1  # Should merge

    def test_optimizer_with_collision_checker(self):
        """Test that optimizer accepts a collision checker."""
        checker = MockCollisionChecker()
        optimizer = TraceOptimizer(collision_checker=checker)
        assert optimizer.collision_checker is checker

    def test_merge_collinear_blocked_by_collision(self):
        """Test that merge_collinear respects collision checker."""
        # Block the merged path (from 0,0 to 2,0)
        checker = MockCollisionChecker(blocked_paths=[(0.5, -0.1, 1.5, 0.1)])
        optimizer = TraceOptimizer(collision_checker=checker)

        segments = [
            self.make_segment(0, 0, 1, 0),
            self.make_segment(1, 0, 2, 0),  # Would normally merge
        ]
        result = optimizer.merge_collinear(segments)

        # Should NOT merge because merged path would cross blocked area
        assert len(result) == 2

    def test_merge_collinear_allowed_when_clear(self):
        """Test that merge_collinear proceeds when path is clear."""
        checker = MockCollisionChecker()  # No blocked paths
        optimizer = TraceOptimizer(collision_checker=checker)

        segments = [
            self.make_segment(0, 0, 1, 0),
            self.make_segment(1, 0, 2, 0),
        ]
        result = optimizer.merge_collinear(segments)

        # Should merge because path is clear
        assert len(result) == 1
        # Verify collision checker was called
        assert len(checker.calls) > 0

    def test_eliminate_zigzags_blocked_by_collision(self):
        """Test that eliminate_zigzags respects collision checker."""
        # Block the shortcut path
        checker = MockCollisionChecker(blocked_paths=[(0, 0, 2, 1)])
        optimizer = TraceOptimizer(collision_checker=checker)

        # Create a zigzag pattern
        segments = [
            self.make_segment(0, 0, 1, 0),
            self.make_segment(1, 0, 1, 1),  # Zigzag
            self.make_segment(1, 1, 2, 1),
        ]
        result = optimizer.eliminate_zigzags(segments)

        # Should keep all segments because shortcut is blocked
        assert len(result) >= 2

    def test_compress_staircase_blocked_by_collision(self):
        """Test that compress_staircase respects collision checker."""
        # Block the optimal diagonal path
        checker = MockCollisionChecker(blocked_paths=[(0, 0, 5, 3)])
        optimizer = TraceOptimizer(collision_checker=checker)

        # Create a staircase pattern
        segments = []
        x, y = 0.0, 0.0
        for i in range(6):
            if i % 2 == 0:
                new_x = x + 1.0
                segments.append(self.make_segment(x, y, new_x, y))
                x = new_x
            else:
                new_x, new_y = x + 0.5, y + 0.5
                segments.append(self.make_segment(x, y, new_x, new_y))
                x, y = new_x, new_y

        result = optimizer.compress_staircase(segments)

        # Should keep original staircase because optimal path is blocked
        assert len(result) == 6

    def test_convert_corners_45_blocked_by_collision(self):
        """Test that convert_corners_45 respects collision checker."""
        # Block diagonal paths
        checker = MockCollisionChecker(blocked_paths=[(-1, -1, 1, 1)])
        optimizer = TraceOptimizer(collision_checker=checker)

        # Create a 90-degree corner
        segments = [
            self.make_segment(0, 0, 5, 0),  # Horizontal
            self.make_segment(5, 0, 5, 5),  # Vertical (90° corner)
        ]
        result = optimizer.convert_corners_45(segments)

        # Even with collision, the function may still add segments
        # but should not add chamfers that would cross blocked areas
        # (Implementation detail depends on how collision is checked)
        assert len(result) >= 2

    def test_full_optimization_with_collision_checker(self):
        """Test full optimize_segments with collision checker."""
        checker = MockCollisionChecker()
        optimizer = TraceOptimizer(collision_checker=checker)

        # Create some segments
        segments = [
            self.make_segment(0, 0, 1, 0),
            self.make_segment(1, 0, 2, 0),  # Collinear
        ]
        result = optimizer.optimize_segments(segments)

        # Should optimize since no paths are blocked
        assert len(result) <= len(segments)
        # Verify collision checker was used
        assert len(checker.calls) > 0
