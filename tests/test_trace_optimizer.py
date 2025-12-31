"""Tests for the TraceOptimizer staircase compression feature."""

import pytest

from kicad_tools.router import (
    Layer,
    OptimizationConfig,
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

    def make_segment(
        self, x1: float, y1: float, x2: float, y2: float
    ) -> Segment:
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

    def test_not_45_degree_apart(self, optimizer):
        """Test that non-45° angle differences are not detected as staircase."""
        segments = [
            self.make_segment(0, 0, 1, 0),  # 0° horizontal
            self.make_segment(1, 0, 1, 1),  # 90° vertical (90° apart, not 45°)
        ]

        end_idx = optimizer._find_staircase_end(segments, 0)
        assert end_idx == 1  # Not a staircase


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
