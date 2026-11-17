"""Acid trap (acute angle) detection.

Acute angles in traces can trap etchant during manufacturing,
leading to over-etching and reliability issues.
"""

from __future__ import annotations

import math
from typing import TYPE_CHECKING

from ..mistakes import Mistake, MistakeCategory

if TYPE_CHECKING:
    from ...schema.pcb import PCB


# Minimum angle to avoid acid traps (degrees)
MIN_TRACE_ANGLE_DEG = 90

# Tolerance below the minimum before a joint is reported (degrees).  A joint
# at 89.7 deg is a rounded right angle, not an acid trap (issue #8663).
ANGLE_TOLERANCE_DEG = 1.0

# Angles below this are collinear overlaps (a segment doubling back on
# another), not corners; they are a separate defect class (issue #8663).
MIN_CORNER_ANGLE_DEG = 1.0


class AcidTrapCheck:
    """Check for acute angles in traces (acid traps).

    Acute angles (< 90°) in PCB traces can trap etchant during
    manufacturing, causing:
    - Over-etching at the angle vertex
    - Thin copper that may break
    - Reliability issues over time

    Also known as "acid traps" because etchant pools in the acute angle.
    """

    category = MistakeCategory.MANUFACTURABILITY

    def check(self, pcb: PCB) -> list[Mistake]:
        """Check for acid traps in traces.

        Args:
            pcb: The PCB to analyze

        Returns:
            List of Mistake objects for acid trap angles
        """
        mistakes: list[Mistake] = []

        # Group segments by net and layer to find connected segments
        segments_by_net_layer: dict[tuple[int, str], list] = {}
        for seg in pcb.segments:
            key = (seg.net_number, seg.layer)
            if key not in segments_by_net_layer:
                segments_by_net_layer[key] = []
            segments_by_net_layer[key].append(seg)

        # Check angles at segment junctions
        for (net_num, layer), segments in segments_by_net_layer.items():
            # Find connected segment pairs (sharing an endpoint)
            for i, seg1 in enumerate(segments):
                for seg2 in segments[i + 1 :]:
                    junction = self._find_junction(seg1, seg2)
                    if junction is None:
                        continue

                    angle = self._calculate_angle(seg1, seg2, junction)
                    if (
                        angle is not None
                        and MIN_CORNER_ANGLE_DEG
                        <= angle
                        < MIN_TRACE_ANGLE_DEG - ANGLE_TOLERANCE_DEG
                    ):
                        net = pcb.get_net(net_num)
                        net_name = net.name if net else f"Net {net_num}"

                        mistakes.append(
                            Mistake(
                                category=MistakeCategory.MANUFACTURABILITY,
                                severity="warning",
                                title="Acute angle in trace (acid trap)",
                                components=[net_name, layer],
                                location=junction,
                                explanation=(
                                    f"Trace on {net_name} ({layer}) has a {angle:.1f}° angle. "
                                    f"Angles less than 90° can trap etchant during PCB "
                                    f"manufacturing, causing over-etching at the vertex. "
                                    f"This can weaken the trace and cause reliability issues."
                                ),
                                fix_suggestion=(
                                    "Reroute the trace to use 45° or 90° angles instead. "
                                    "If the acute angle is unavoidable, add a teardrop "
                                    "at the junction to eliminate the sharp corner."
                                ),
                                learn_more_url="docs/mistakes/acid-traps.md",
                                measurements={"angle_deg": round(angle, 1)},
                            )
                        )

        return mistakes

    def _find_junction(self, seg1, seg2) -> tuple[float, float] | None:
        """Find the junction point between two segments if they connect."""
        tolerance = 0.01  # 0.01mm tolerance

        # Pick the CLOSEST endpoint pair within tolerance, not the first: a
        # near-zero-length segment makes start~start match before the true
        # shared point (end==start) and yields a fake angle (PR #8848 review).
        best: tuple[float, tuple[float, float]] | None = None
        for p1 in (seg1.start, seg1.end):
            for p2 in (seg2.start, seg2.end):
                dx, dy = abs(p1[0] - p2[0]), abs(p1[1] - p2[1])
                if dx < tolerance and dy < tolerance:
                    d = math.hypot(dx, dy)
                    if best is None or d < best[0]:
                        best = (d, p1)
        return best[1] if best else None

    def _calculate_angle(
        self,
        seg1,
        seg2,
        junction: tuple[float, float],
    ) -> float | None:
        """Calculate the angle between two segments at a junction.

        Returns angle in degrees, or None if calculation fails.
        """
        # Get the vectors from junction to the other endpoints
        tolerance = 0.01

        def other_end(seg, junction):
            if (
                abs(seg.start[0] - junction[0]) < tolerance
                and abs(seg.start[1] - junction[1]) < tolerance
            ):
                return seg.end
            return seg.start

        p1 = other_end(seg1, junction)
        p2 = other_end(seg2, junction)

        # Calculate vectors
        v1 = (p1[0] - junction[0], p1[1] - junction[1])
        v2 = (p2[0] - junction[0], p2[1] - junction[1])

        # Calculate magnitudes
        mag1 = math.sqrt(v1[0] ** 2 + v1[1] ** 2)
        mag2 = math.sqrt(v2[0] ** 2 + v2[1] ** 2)

        # Near-zero-length segments carry no direction (rounding artefacts).
        if mag1 < tolerance or mag2 < tolerance:
            return None

        # Calculate dot product and angle
        dot = v1[0] * v2[0] + v1[1] * v2[1]
        cos_angle = dot / (mag1 * mag2)

        # Clamp to valid range for acos
        cos_angle = max(-1, min(1, cos_angle))

        angle_rad = math.acos(cos_angle)
        return math.degrees(angle_rad)
