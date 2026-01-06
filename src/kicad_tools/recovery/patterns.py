"""
Pattern matching for common failure types.

This module provides the PatternMatcher class which identifies known failure
patterns and provides targeted suggestions based on best practices.
"""

from __future__ import annotations

from dataclasses import dataclass

from .types import BlockingElement, FailureAnalysis, FailureCause


@dataclass
class MatchedPattern:
    """A matched failure pattern with suggestions.

    Attributes:
        pattern: Name of the matched pattern.
        suggestion: Human-readable suggestion for resolving the issue.
        example: Concrete example of how to apply the suggestion.
        confidence: How confident we are that this pattern matches (0-1).
    """

    pattern: str
    suggestion: str
    example: str
    confidence: float = 1.0

    def to_dict(self) -> dict[str, str | float]:
        """Convert to dictionary for JSON serialization."""
        return {
            "pattern": self.pattern,
            "suggestion": self.suggestion,
            "example": self.example,
            "confidence": self.confidence,
        }


@dataclass
class PatternDefinition:
    """Definition of a failure pattern.

    Attributes:
        name: Pattern name identifier.
        description: Human-readable description of the pattern.
        suggestion: Suggested fix for this pattern.
        example: Concrete example of the suggestion.
    """

    name: str
    description: str
    suggestion: str
    example: str


class PatternMatcher:
    """Match failures to known patterns for better suggestions.

    Identifies common failure patterns in PCB routing and placement,
    providing targeted suggestions based on best practices and
    common solutions.

    Example::

        matcher = PatternMatcher()
        patterns = matcher.match_patterns(failure_analysis)

        for match in patterns:
            print(f"Pattern: {match.pattern}")
            print(f"Suggestion: {match.suggestion}")
            print(f"Example: {match.example}")
    """

    # Known failure patterns with signatures, suggestions, and examples
    PATTERNS: list[PatternDefinition] = [
        PatternDefinition(
            name="bypass_cap_blocking",
            description="Bypass capacitor blocking routing path",
            suggestion="Use radial bypass cap placement around IC",
            example="Place C1-C4 in a ring 2-3mm from U1 VDD pins",
        ),
        PatternDefinition(
            name="connector_bottleneck",
            description="Routing congestion near connector",
            suggestion="Fan out traces immediately after connector",
            example="Route USB signals away from connector before turning",
        ),
        PatternDefinition(
            name="power_plane_crossing",
            description="Signal crossing split power plane",
            suggestion="Route signal on single reference plane or add stitching vias",
            example="Keep CLK trace on layer 2 above continuous GND plane",
        ),
        PatternDefinition(
            name="pin_escape_congestion",
            description="Too many pins escaping in same direction",
            suggestion="Use alternating escape directions or add routing layers",
            example="Route odd pins left, even pins right from BGA",
        ),
        PatternDefinition(
            name="via_farm_blocking",
            description="Dense via array blocking routing",
            suggestion="Stagger vias or route through via farm gaps",
            example="Offset vias by 0.5mm to create routing channels",
        ),
        PatternDefinition(
            name="differential_pair_obstacle",
            description="Obstacle splitting differential pair",
            suggestion="Route both traces on same side of obstacle",
            example="Keep USB_DP and USB_DM together around C5",
        ),
        PatternDefinition(
            name="thermal_via_interference",
            description="Thermal vias interfering with routing",
            suggestion="Reduce thermal via density or route around thermal pad",
            example="Use 4 thermal vias instead of 9 under QFN thermal pad",
        ),
        PatternDefinition(
            name="crystal_isolation",
            description="Crystal circuit too close to noisy signals",
            suggestion="Isolate crystal with ground ring and keep signals away",
            example="Add ground traces around Y1, route digital signals 2mm away",
        ),
    ]

    def match_patterns(self, failure: FailureAnalysis) -> list[MatchedPattern]:
        """Find patterns matching this failure.

        Args:
            failure: The failure analysis to match against patterns.

        Returns:
            List of matched patterns with suggestions, sorted by confidence.
        """
        matches: list[MatchedPattern] = []

        # Check each pattern
        if self._matches_bypass_cap_blocking(failure):
            matches.append(self._create_match("bypass_cap_blocking"))

        if self._matches_connector_bottleneck(failure):
            matches.append(self._create_match("connector_bottleneck"))

        if self._matches_power_plane_crossing(failure):
            matches.append(self._create_match("power_plane_crossing"))

        if self._matches_pin_escape_congestion(failure):
            matches.append(self._create_match("pin_escape_congestion"))

        if self._matches_via_farm_blocking(failure):
            matches.append(self._create_match("via_farm_blocking"))

        if self._matches_differential_pair_obstacle(failure):
            matches.append(self._create_match("differential_pair_obstacle"))

        if self._matches_thermal_via_interference(failure):
            matches.append(self._create_match("thermal_via_interference"))

        if self._matches_crystal_isolation(failure):
            matches.append(self._create_match("crystal_isolation"))

        # Sort by confidence
        matches.sort(key=lambda m: m.confidence, reverse=True)
        return matches

    def get_all_patterns(self) -> list[PatternDefinition]:
        """Get all defined patterns.

        Returns:
            List of all pattern definitions.
        """
        return self.PATTERNS.copy()

    def _create_match(self, pattern_name: str, confidence: float = 1.0) -> MatchedPattern:
        """Create a matched pattern from a pattern name."""
        for pattern in self.PATTERNS:
            if pattern.name == pattern_name:
                return MatchedPattern(
                    pattern=pattern.name,
                    suggestion=pattern.suggestion,
                    example=pattern.example,
                    confidence=confidence,
                )
        # Fallback (should never happen)
        return MatchedPattern(
            pattern=pattern_name,
            suggestion="Unknown pattern",
            example="",
            confidence=0.0,
        )

    def _matches_bypass_cap_blocking(self, failure: FailureAnalysis) -> bool:
        """Check if failure matches bypass cap blocking pattern.

        Pattern: Blocked path with bypass capacitor(s) in the way.
        """
        if failure.root_cause != FailureCause.BLOCKED_PATH:
            return False

        # Check if any blocker is a capacitor
        return any(self._is_bypass_cap(blocker) for blocker in failure.blocking_elements)

    def _matches_connector_bottleneck(self, failure: FailureAnalysis) -> bool:
        """Check if failure matches connector bottleneck pattern.

        Pattern: Congestion near a connector component.
        """
        if failure.root_cause != FailureCause.CONGESTION:
            return False

        return failure.near_connector

    def _matches_power_plane_crossing(self, failure: FailureAnalysis) -> bool:
        """Check if failure matches power plane crossing pattern.

        Pattern: Layer conflict involving power/ground planes.
        """
        if failure.root_cause != FailureCause.LAYER_CONFLICT:
            return False

        # Check if blocking elements include zones
        return any(blocker.type == "zone" for blocker in failure.blocking_elements)

    def _matches_pin_escape_congestion(self, failure: FailureAnalysis) -> bool:
        """Check if failure matches pin escape congestion pattern.

        Pattern: High congestion near IC with many pins.
        """
        if failure.root_cause != FailureCause.CONGESTION:
            return False

        # High congestion score indicates this pattern
        if failure.congestion_score < 0.8:
            return False

        # Check if near an IC
        return any(
            blocker.ref and blocker.ref.upper().startswith("U")
            for blocker in failure.blocking_elements
        )

    def _matches_via_farm_blocking(self, failure: FailureAnalysis) -> bool:
        """Check if failure matches via farm blocking pattern.

        Pattern: Multiple vias blocking the routing path.
        """
        if failure.root_cause != FailureCause.BLOCKED_PATH:
            return False

        # Count via blockers
        via_count = sum(1 for blocker in failure.blocking_elements if blocker.type == "via")
        return via_count >= 3

    def _matches_differential_pair_obstacle(self, failure: FailureAnalysis) -> bool:
        """Check if failure matches differential pair obstacle pattern.

        Pattern: Differential pair routing failure.
        """
        return failure.root_cause == FailureCause.DIFFERENTIAL_PAIR

    def _matches_thermal_via_interference(self, failure: FailureAnalysis) -> bool:
        """Check if failure matches thermal via interference pattern.

        Pattern: Vias near QFN/thermal pad blocking routing.
        """
        if failure.root_cause not in [
            FailureCause.BLOCKED_PATH,
            FailureCause.CONGESTION,
        ]:
            return False

        # Check for via blockers near thermal pads
        has_vias = False
        has_thermal_component = False

        for blocker in failure.blocking_elements:
            if blocker.type == "via":
                has_vias = True
            if blocker.ref:
                # QFN/QFP typically have thermal pads
                ref_upper = blocker.ref.upper()
                if ref_upper.startswith("U") or ref_upper.startswith("IC"):
                    has_thermal_component = True

        return has_vias and has_thermal_component

    def _matches_crystal_isolation(self, failure: FailureAnalysis) -> bool:
        """Check if failure matches crystal isolation pattern.

        Pattern: Routing near crystal oscillator components.
        """
        if failure.root_cause != FailureCause.CLEARANCE:
            return False

        # Check if near a crystal (Y prefix)
        return any(
            blocker.ref and blocker.ref.upper().startswith("Y")
            for blocker in failure.blocking_elements
        )

    def _is_bypass_cap(self, blocker: BlockingElement) -> bool:
        """Check if a blocking element is a bypass capacitor."""
        if blocker.type != "component":
            return False
        if blocker.ref is None:
            return False
        return blocker.ref.upper().startswith("C")
