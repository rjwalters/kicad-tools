"""
Trace optimizer for post-routing cleanup.

Provides algorithms to optimize routed traces:
- Collinear segment merging (combine same-direction segments)
- Zigzag elimination (remove unnecessary back-and-forth)
- 45-degree corner conversion (smooth 90-degree turns)

Example::

    from kicad_tools.router import TraceOptimizer, OptimizationConfig

    # Optimize a route in memory
    optimizer = TraceOptimizer()
    optimized_route = optimizer.optimize_route(route)

    # Optimize traces in a PCB file
    stats = optimizer.optimize_pcb("board.kicad_pcb", output="optimized.kicad_pcb")
    print(f"Reduced segments from {stats['before']} to {stats['after']}")
"""

from __future__ import annotations

import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .layers import Layer
from .primitives import Route, Segment, Via


@dataclass
class OptimizationConfig:
    """Configuration for trace optimization."""

    merge_collinear: bool = True
    """Merge adjacent segments with the same direction."""

    eliminate_zigzags: bool = True
    """Remove unnecessary back-and-forth patterns."""

    convert_45_corners: bool = True
    """Convert 90-degree corners to 45-degree chamfers."""

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


class TraceOptimizer:
    """Optimizer for PCB trace cleanup and simplification."""

    def __init__(self, config: Optional[OptimizationConfig] = None):
        """
        Initialize the trace optimizer.

        Args:
            config: Optimization configuration. Uses defaults if None.
        """
        self.config = config or OptimizationConfig()

    def optimize_segments(self, segments: List[Segment]) -> List[Segment]:
        """
        Optimize a list of segments for a single net/layer.

        Applies enabled optimizations in order:
        1. Collinear segment merging
        2. Zigzag elimination
        3. 45-degree corner conversion

        Args:
            segments: List of segments to optimize (should be connected path).

        Returns:
            Optimized list of segments.
        """
        if not segments:
            return []

        result = list(segments)

        # Apply optimizations in order
        if self.config.merge_collinear:
            result = self.merge_collinear(result)

        if self.config.eliminate_zigzags:
            result = self.eliminate_zigzags(result)

        if self.config.convert_45_corners:
            result = self.convert_corners_45(result)

        return result

    def merge_collinear(self, segments: List[Segment]) -> List[Segment]:
        """
        Merge adjacent collinear segments.

        Combines segments that:
        - Are connected (end of one matches start of next)
        - Have the same direction
        - Are on the same layer

        Args:
            segments: List of segments to merge.

        Returns:
            List with collinear segments merged.
        """
        if len(segments) < 2:
            return list(segments)

        result: List[Segment] = []
        current = segments[0]

        for next_seg in segments[1:]:
            # Check if segments can be merged
            if (self._is_connected(current, next_seg) and
                self._same_direction(current, next_seg) and
                current.layer == next_seg.layer and
                current.net == next_seg.net):
                # Extend current segment to include next
                current = Segment(
                    x1=current.x1,
                    y1=current.y1,
                    x2=next_seg.x2,
                    y2=next_seg.y2,
                    width=current.width,
                    layer=current.layer,
                    net=current.net,
                    net_name=current.net_name,
                )
            else:
                # Can't merge, save current and start new
                result.append(current)
                current = next_seg

        result.append(current)
        return result

    def eliminate_zigzags(self, segments: List[Segment]) -> List[Segment]:
        """
        Remove unnecessary zigzag patterns.

        Identifies segments where the path backtracks and removes
        the unnecessary detour.

        Args:
            segments: List of segments to process.

        Returns:
            List with zigzags eliminated.
        """
        if len(segments) < 3:
            return list(segments)

        result: List[Segment] = [segments[0]]
        i = 1

        while i < len(segments) - 1:
            prev = result[-1]
            curr = segments[i]
            next_seg = segments[i + 1]

            # Check if curr is a zigzag (backtrack)
            if self._is_zigzag(prev, curr, next_seg):
                # Skip curr, connect prev directly to next's start
                # Update the last segment in result
                result[-1] = Segment(
                    x1=prev.x1,
                    y1=prev.y1,
                    x2=curr.x2,  # Connect to where curr ends
                    y2=curr.y2,
                    width=prev.width,
                    layer=prev.layer,
                    net=prev.net,
                    net_name=prev.net_name,
                )
                i += 1  # Skip curr
            else:
                result.append(curr)
                i += 1

        # Add the last segment
        if segments:
            result.append(segments[-1])

        return result

    def convert_corners_45(self, segments: List[Segment]) -> List[Segment]:
        """
        Convert 90-degree corners to 45-degree chamfers.

        Replaces sharp 90-degree turns with smoother 45-degree entry/exit.

        Args:
            segments: List of segments to process.

        Returns:
            List with corners converted to 45 degrees.
        """
        if len(segments) < 2:
            return list(segments)

        result: List[Segment] = []
        chamfer = self.config.corner_chamfer_size

        for i, seg in enumerate(segments):
            if i == 0:
                # First segment - check if next segment forms 90-degree corner
                if i + 1 < len(segments):
                    next_seg = segments[i + 1]
                    if self._is_90_degree_corner(seg, next_seg):
                        # Shorten this segment to leave room for chamfer
                        shortened = self._shorten_segment_end(seg, chamfer)
                        if shortened:
                            result.append(shortened)
                        else:
                            result.append(seg)
                    else:
                        result.append(seg)
                else:
                    result.append(seg)

            elif i == len(segments) - 1:
                # Last segment - check if prev segment forms 90-degree corner
                prev_seg = segments[i - 1]
                if self._is_90_degree_corner(prev_seg, seg):
                    # Shorten start of this segment
                    shortened = self._shorten_segment_start(seg, chamfer)
                    if shortened:
                        # Add chamfer segment connecting prev end to this start
                        chamfer_seg = Segment(
                            x1=result[-1].x2,
                            y1=result[-1].y2,
                            x2=shortened.x1,
                            y2=shortened.y1,
                            width=seg.width,
                            layer=seg.layer,
                            net=seg.net,
                            net_name=seg.net_name,
                        )
                        result.append(chamfer_seg)
                        result.append(shortened)
                    else:
                        result.append(seg)
                else:
                    result.append(seg)

            else:
                # Middle segment - check both corners
                prev_seg = segments[i - 1]
                next_seg = segments[i + 1]

                modified_seg = seg

                # Handle corner with previous segment
                if self._is_90_degree_corner(prev_seg, seg):
                    shortened = self._shorten_segment_start(modified_seg, chamfer)
                    if shortened:
                        # Add chamfer
                        chamfer_seg = Segment(
                            x1=result[-1].x2,
                            y1=result[-1].y2,
                            x2=shortened.x1,
                            y2=shortened.y1,
                            width=seg.width,
                            layer=seg.layer,
                            net=seg.net,
                            net_name=seg.net_name,
                        )
                        result.append(chamfer_seg)
                        modified_seg = shortened

                # Handle corner with next segment
                if self._is_90_degree_corner(seg, next_seg):
                    shortened = self._shorten_segment_end(modified_seg, chamfer)
                    if shortened:
                        modified_seg = shortened

                result.append(modified_seg)

        return result

    def optimize_route(self, route: Route) -> Route:
        """
        Optimize a complete route.

        Args:
            route: Route to optimize.

        Returns:
            New Route with optimized segments.
        """
        # Group segments by layer for optimization
        segments_by_layer: Dict[Layer, List[Segment]] = {}
        for seg in route.segments:
            if seg.layer not in segments_by_layer:
                segments_by_layer[seg.layer] = []
            segments_by_layer[seg.layer].append(seg)

        # Optimize each layer's segments
        optimized_segments: List[Segment] = []
        for layer, segs in segments_by_layer.items():
            optimized = self.optimize_segments(segs)
            optimized_segments.extend(optimized)

        return Route(
            net=route.net,
            net_name=route.net_name,
            segments=optimized_segments,
            vias=list(route.vias),  # Vias unchanged
        )

    def optimize_pcb(
        self,
        pcb_path: str,
        output_path: Optional[str] = None,
        net_filter: Optional[str] = None,
        dry_run: bool = False,
    ) -> OptimizationStats:
        """
        Optimize traces in a PCB file.

        Args:
            pcb_path: Path to input .kicad_pcb file.
            output_path: Path for output file. If None, modifies in place.
            net_filter: Only optimize nets matching this pattern.
            dry_run: If True, calculate stats but don't write output.

        Returns:
            Statistics about the optimization.
        """
        pcb_text = Path(pcb_path).read_text()
        stats = OptimizationStats()

        # Parse existing segments
        segments_by_net = self._parse_segments(pcb_text)

        # Filter nets if requested
        if net_filter:
            segments_by_net = {
                net: segs for net, segs in segments_by_net.items()
                if net_filter.lower() in net.lower()
            }

        # Calculate before stats
        for net, segs in segments_by_net.items():
            stats.segments_before += len(segs)
            stats.corners_before += self._count_corners(segs)
            stats.length_before += self._total_length(segs)

        # Optimize each net
        optimized_segments: Dict[str, List[Segment]] = {}
        for net, segs in segments_by_net.items():
            optimized = self.optimize_segments(segs)
            optimized_segments[net] = optimized
            stats.nets_optimized += 1

        # Calculate after stats
        for net, segs in optimized_segments.items():
            stats.segments_after += len(segs)
            stats.corners_after += self._count_corners(segs)
            stats.length_after += self._total_length(segs)

        # Generate output (only if not dry run)
        if not dry_run:
            output_text = self._replace_segments(pcb_text, segments_by_net, optimized_segments)
            out_path = output_path or pcb_path
            Path(out_path).write_text(output_text)

        return stats

    # =========================================================================
    # Helper methods
    # =========================================================================

    def _is_connected(self, s1: Segment, s2: Segment) -> bool:
        """Check if end of s1 connects to start of s2."""
        tol = self.config.tolerance
        return (abs(s1.x2 - s2.x1) < tol and abs(s1.y2 - s2.y1) < tol)

    def _same_direction(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments have the same direction."""
        dx1, dy1 = s1.x2 - s1.x1, s1.y2 - s1.y1
        dx2, dy2 = s2.x2 - s2.x1, s2.y2 - s2.y1

        # Handle zero-length segments
        len1 = math.sqrt(dx1 * dx1 + dy1 * dy1)
        len2 = math.sqrt(dx2 * dx2 + dy2 * dy2)

        if len1 < self.config.tolerance or len2 < self.config.tolerance:
            return True  # Zero-length segments are "same direction"

        # Normalize
        dx1, dy1 = dx1 / len1, dy1 / len1
        dx2, dy2 = dx2 / len2, dy2 / len2

        # Cross product should be ~0 for parallel
        cross = abs(dx1 * dy2 - dy1 * dx2)
        # Dot product should be positive (same direction, not opposite)
        dot = dx1 * dx2 + dy1 * dy2

        return cross < 0.01 and dot > 0

    def _is_zigzag(self, s1: Segment, s2: Segment, s3: Segment) -> bool:
        """Check if s2 is a zigzag (backtrack) between s1 and s3."""
        # Calculate angles
        angle12 = self._angle_between(s1, s2)
        angle23 = self._angle_between(s2, s3)

        # Zigzag: s2 goes roughly opposite to s1, then s3 continues roughly same as s1
        # This means angle12 is close to 180 degrees
        if abs(angle12 - 180) < 30:
            return True

        return False

    def _angle_between(self, s1: Segment, s2: Segment) -> float:
        """Calculate angle between two segments in degrees (0-180)."""
        dx1, dy1 = s1.x2 - s1.x1, s1.y2 - s1.y1
        dx2, dy2 = s2.x2 - s2.x1, s2.y2 - s2.y1

        len1 = math.sqrt(dx1 * dx1 + dy1 * dy1)
        len2 = math.sqrt(dx2 * dx2 + dy2 * dy2)

        if len1 < self.config.tolerance or len2 < self.config.tolerance:
            return 0

        # Dot product
        dot = dx1 * dx2 + dy1 * dy2
        cos_angle = dot / (len1 * len2)
        cos_angle = max(-1, min(1, cos_angle))  # Clamp for numerical stability

        return math.degrees(math.acos(cos_angle))

    def _is_90_degree_corner(self, s1: Segment, s2: Segment) -> bool:
        """Check if two segments form a 90-degree corner."""
        angle = self._angle_between(s1, s2)
        return 80 < angle < 100  # Allow some tolerance

    def _shorten_segment_end(self, seg: Segment, amount: float) -> Optional[Segment]:
        """Shorten a segment from its end by the given amount."""
        dx = seg.x2 - seg.x1
        dy = seg.y2 - seg.y1
        length = math.sqrt(dx * dx + dy * dy)

        if length <= amount + self.config.min_segment_length:
            return None  # Can't shorten enough

        # New end point
        ratio = (length - amount) / length
        new_x2 = seg.x1 + dx * ratio
        new_y2 = seg.y1 + dy * ratio

        return Segment(
            x1=seg.x1, y1=seg.y1,
            x2=new_x2, y2=new_y2,
            width=seg.width,
            layer=seg.layer,
            net=seg.net,
            net_name=seg.net_name,
        )

    def _shorten_segment_start(self, seg: Segment, amount: float) -> Optional[Segment]:
        """Shorten a segment from its start by the given amount."""
        dx = seg.x2 - seg.x1
        dy = seg.y2 - seg.y1
        length = math.sqrt(dx * dx + dy * dy)

        if length <= amount + self.config.min_segment_length:
            return None  # Can't shorten enough

        # New start point
        ratio = amount / length
        new_x1 = seg.x1 + dx * ratio
        new_y1 = seg.y1 + dy * ratio

        return Segment(
            x1=new_x1, y1=new_y1,
            x2=seg.x2, y2=seg.y2,
            width=seg.width,
            layer=seg.layer,
            net=seg.net,
            net_name=seg.net_name,
        )

    def _count_corners(self, segments: List[Segment]) -> int:
        """Count number of corners (direction changes) in a segment list."""
        if len(segments) < 2:
            return 0

        corners = 0
        for i in range(len(segments) - 1):
            if not self._same_direction(segments[i], segments[i + 1]):
                corners += 1
        return corners

    def _total_length(self, segments: List[Segment]) -> float:
        """Calculate total length of segments."""
        total = 0.0
        for seg in segments:
            dx = seg.x2 - seg.x1
            dy = seg.y2 - seg.y1
            total += math.sqrt(dx * dx + dy * dy)
        return total

    def _parse_net_names(self, pcb_text: str) -> Dict[int, str]:
        """Parse net ID to name mapping from PCB file."""
        net_names: Dict[int, str] = {}

        # Match net declarations: (net N "name")
        pattern = re.compile(r'\(net\s+(\d+)\s+"([^"]*)"\)')
        for match in pattern.finditer(pcb_text):
            net_id = int(match.group(1))
            net_name = match.group(2)
            if net_name:  # Skip empty net names
                net_names[net_id] = net_name

        return net_names

    def _parse_segments(self, pcb_text: str) -> Dict[str, List[Segment]]:
        """Parse segments from PCB file text, grouped by net name."""
        segments_by_net: Dict[str, List[Segment]] = {}

        # First, build net ID to name mapping
        net_names = self._parse_net_names(pcb_text)

        # Match segment S-expressions (multiline format)
        # (segment
        #     (start X Y)
        #     (end X Y)
        #     (width W)
        #     (layer "L")
        #     (net N)
        #     ...
        # )
        pattern = re.compile(
            r'\(segment\s+'
            r'\(start\s+([\d.-]+)\s+([\d.-]+)\)\s*'
            r'\(end\s+([\d.-]+)\s+([\d.-]+)\)\s*'
            r'\(width\s+([\d.]+)\)\s*'
            r'\(layer\s+"([^"]+)"\)\s*'
            r'\(net\s+(\d+)\)',
            re.DOTALL
        )

        for match in pattern.finditer(pcb_text):
            x1 = float(match.group(1))
            y1 = float(match.group(2))
            x2 = float(match.group(3))
            y2 = float(match.group(4))
            width = float(match.group(5))
            layer_name = match.group(6)
            net = int(match.group(7))
            net_name = net_names.get(net, f"Net{net}")

            # Convert layer name to Layer enum
            layer = Layer.F_CU  # Default
            for l in Layer:
                if l.kicad_name == layer_name:
                    layer = l
                    break

            seg = Segment(
                x1=x1, y1=y1, x2=x2, y2=y2,
                width=width, layer=layer,
                net=net, net_name=net_name,
            )

            if net_name not in segments_by_net:
                segments_by_net[net_name] = []
            segments_by_net[net_name].append(seg)

        return segments_by_net

    def _replace_segments(
        self,
        pcb_text: str,
        original: Dict[str, List[Segment]],
        optimized: Dict[str, List[Segment]],
    ) -> str:
        """Replace original segments with optimized ones in PCB text."""
        result = pcb_text

        # Get net IDs for each net name
        net_ids_to_remove: set[int] = set()
        for net_name, segs in original.items():
            if net_name in optimized and segs:
                net_ids_to_remove.add(segs[0].net)

        # Remove existing segment blocks for nets we optimized
        # Match the multiline segment format:
        # (segment
        #     (start X Y)
        #     ...
        #     (net N)
        #     ...
        # )
        for net_id in net_ids_to_remove:
            pattern = re.compile(
                r'\(segment\s+[^)]*\(net\s+' + str(net_id) + r'\)[^)]*\)\s*',
                re.DOTALL
            )
            result = pattern.sub('', result)

        # Add optimized segments before the closing parenthesis
        new_segments_sexp = []
        for net_name, segs in optimized.items():
            for seg in segs:
                new_segments_sexp.append(seg.to_sexp())

        if new_segments_sexp:
            # Find the last ) and insert before it
            insert_pos = result.rfind(')')
            if insert_pos > 0:
                indent = "  "
                new_content = "\n" + indent + f"\n{indent}".join(new_segments_sexp) + "\n"
                result = result[:insert_pos] + new_content + result[insert_pos:]

        return result
