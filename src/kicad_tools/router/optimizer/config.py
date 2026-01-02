"""Configuration and statistics for trace optimization."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class OptimizationConfig:
    """Configuration for trace optimization."""

    merge_collinear: bool = True
    """Merge adjacent segments with the same direction."""

    eliminate_zigzags: bool = True
    """Remove unnecessary back-and-forth patterns."""

    convert_45_corners: bool = True
    """Convert 90-degree corners to 45-degree chamfers."""

    compress_staircase: bool = True
    """Compress staircase patterns (alternating horizontal/diagonal) into optimal paths."""

    min_staircase_segments: int = 3
    """Minimum number of segments to consider as a staircase pattern."""

    min_segment_length: float = 0.05
    """Minimum segment length to keep (mm). Shorter segments may be merged."""

    corner_chamfer_size: float = 0.5
    """Size of 45-degree chamfer at corners (mm)."""

    tolerance: float = 1e-4
    """Tolerance for floating-point comparisons (mm)."""


@dataclass
class OptimizationStats:
    """Statistics from trace optimization."""

    segments_before: int = 0
    segments_after: int = 0
    corners_before: int = 0
    corners_after: int = 0
    length_before: float = 0.0
    length_after: float = 0.0
    nets_optimized: int = 0

    @property
    def segment_reduction(self) -> float:
        """Percentage reduction in segment count."""
        if self.segments_before == 0:
            return 0.0
        return (1 - self.segments_after / self.segments_before) * 100

    @property
    def length_reduction(self) -> float:
        """Percentage reduction in total length."""
        if self.length_before == 0:
            return 0.0
        return (1 - self.length_after / self.length_before) * 100
