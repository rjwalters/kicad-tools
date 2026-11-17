"""Preference learning from decision history.

Analyzes agent decision patterns to derive preferences and generate
suggestions based on learned behavior.

Example:
    >>> from kicad_tools.mcp.preference_learner import PreferenceLearner
    >>> from kicad_tools.mcp.context import Decision, AgentPreferences
    >>>
    >>> learner = PreferenceLearner()
    >>> decisions = [...]  # List of Decision objects
    >>> preferences = learner.analyze_decisions(decisions)
    >>> print(preferences.preferred_spacing)  # Derived from move patterns
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from kicad_tools.mcp.context import AgentPreferences, Decision, SessionContext


@dataclass
class PatternMatch:
    """A detected pattern in decision history."""

    name: str
    confidence: float
    occurrences: int
    examples: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        return {
            "name": self.name,
            "confidence": round(self.confidence, 2),
            "occurrences": self.occurrences,
            "examples": self.examples[:3],  # Limit to 3 examples
        }


class PreferenceLearner:
    """Learns agent preferences from decision history.

    Analyzes patterns in past decisions to derive:
    - Spacing preferences from move decisions
    - Via usage tolerance from routing decisions
    - Common design patterns
    - Trade-off preferences

    Example:
        >>> learner = PreferenceLearner()
        >>> preferences = learner.analyze_decisions(decisions)
        >>> suggestions = learner.suggest_based_on_preferences(session, preferences)
    """

    # Known pattern signatures
    PATTERN_SIGNATURES = {
        "bypass_cap_optimization": {
            "keywords": ["bypass", "decoupling", "VDD", "VCC", "power"],
            "action": "move",
        },
        "edge_connector_placement": {
            "keywords": ["connector", "header", "edge", "J"],
            "action": "move",
        },
        "thermal_spreading": {
            "keywords": ["thermal", "heat", "spreading", "power"],
            "action": "move",
        },
        "ground_plane_routing": {
            "keywords": ["GND", "ground", "plane", "pour"],
            "action": "route",
        },
        "differential_pair_routing": {
            "keywords": ["differential", "pair", "USB", "LVDS", "DP", "DM"],
            "action": "route",
        },
        "clock_tree_optimization": {
            "keywords": ["CLK", "clock", "timing", "sync"],
            "action": "route",
        },
    }

    def __init__(self) -> None:
        """Initialize the preference learner."""
        self._pattern_cache: dict[str, PatternMatch] = {}

    def analyze_decisions(
        self,
        decisions: list[Decision],
    ) -> AgentPreferences:
        """Infer preferences from decision patterns.

        Analyzes the decision history to extract:
        - Typical component spacing
        - Via usage patterns
        - Common design patterns

        Args:
            decisions: List of Decision objects to analyze.

        Returns:
            AgentPreferences derived from the patterns.
        """
        from kicad_tools.mcp.context import AgentPreferences

        prefs = AgentPreferences()

        if not decisions:
            return prefs

        # Analyze spacing from move decisions
        move_decisions = [d for d in decisions if d.action == "move"]
        if move_decisions:
            prefs.preferred_spacing = self._extract_spacing_preference(move_decisions)
            prefs.alignment_preference = self._infer_alignment_preference(move_decisions)

        # Analyze via usage from route decisions
        route_decisions = [d for d in decisions if d.action == "route"]
        if route_decisions:
            prefs.via_tolerance = self._infer_via_tolerance(route_decisions)
            prefs.layer_preference = self._extract_layer_preference(route_decisions)

        # Find common patterns
        prefs.common_patterns = self._find_common_patterns(decisions)
        prefs.avoided_patterns = self._find_avoided_patterns(decisions)

        # Infer trade-off preferences
        prefs.density_vs_routability = self._infer_density_preference(decisions)
        prefs.cost_vs_performance = self._infer_cost_performance_preference(decisions)

        return prefs

    def _extract_spacing_preference(self, move_decisions: list[Decision]) -> float:
        """Extract typical spacing from move decisions.

        Looks at the delta between component positions in move decisions
        to infer preferred spacing patterns.
        """
        spacings: list[float] = []

        for decision in move_decisions:
            params = decision.params
            if "spacing" in params:
                spacings.append(float(params["spacing"]))
            elif "x" in params and "y" in params:
                # Use position delta as proxy for spacing preference
                # This is a heuristic - real implementation would track
                # distances to nearest neighbors
                pass

        if spacings:
            # Use median for robustness against outliers
            sorted_spacings = sorted(spacings)
            mid = len(sorted_spacings) // 2
            if len(sorted_spacings) % 2 == 0:
                return (sorted_spacings[mid - 1] + sorted_spacings[mid]) / 2
            return sorted_spacings[mid]

        return 2.5  # Default spacing

    def _infer_alignment_preference(self, move_decisions: list[Decision]) -> str:
        """Infer alignment style from move patterns."""
        # Look for grid-aligned positions
        grid_aligned = 0
        functional_grouped = 0
        total = len(move_decisions)

        if total == 0:
            return "grid"

        for decision in move_decisions:
            params = decision.params
            x = params.get("x", 0)
            y = params.get("y", 0)

            # Check for grid alignment (positions on 0.5mm or 1mm grid)
            if isinstance(x, (int, float)) and isinstance(y, (int, float)):
                if (x * 2) % 1 < 0.01 and (y * 2) % 1 < 0.01:
                    grid_aligned += 1

            # Check rationale for functional grouping keywords
            rationale = decision.rationale or ""
            if any(
                kw in rationale.lower() for kw in ["bypass", "decoupling", "near", "close", "group"]
            ):
                functional_grouped += 1

        if functional_grouped > grid_aligned:
            return "functional"
        elif grid_aligned > total * 0.7:
            return "grid"
        else:
            return "aesthetic"

    def _infer_via_tolerance(self, route_decisions: list[Decision]) -> str:
        """Infer via usage preference from routing decisions."""
        via_counts: list[int] = []

        for decision in route_decisions:
            params = decision.params
            if "vias_used" in params:
                via_counts.append(int(params["vias_used"]))

        if not via_counts:
            return "moderate"

        avg_vias = sum(via_counts) / len(via_counts)

        if avg_vias < 1:
            return "minimal"
        elif avg_vias > 3:
            return "liberal"
        else:
            return "moderate"

    def _extract_layer_preference(self, route_decisions: list[Decision]) -> list[str]:
        """Extract preferred layer order from routing decisions."""
        layer_usage: Counter[str] = Counter()

        for decision in route_decisions:
            params = decision.params
            layers = params.get("layers_used", [])
            if isinstance(layers, list):
                for layer in layers:
                    layer_usage[layer] += 1

        if layer_usage:
            # Return layers sorted by usage frequency
            return [layer for layer, _ in layer_usage.most_common()]

        return ["F.Cu", "B.Cu"]  # Default

    def _find_common_patterns(self, decisions: list[Decision]) -> list[str]:
        """Find frequently occurring patterns in decisions."""
        patterns: list[str] = []

        for pattern_name, signature in self.PATTERN_SIGNATURES.items():
            matches = self._count_pattern_matches(decisions, pattern_name, signature)
            if matches >= 2:  # Pattern appears at least twice
                patterns.append(pattern_name)
                self._pattern_cache[pattern_name] = PatternMatch(
                    name=pattern_name,
                    confidence=min(1.0, matches / 5),  # Confidence scales with occurrences
                    occurrences=matches,
                    examples=[d.target for d in decisions if self._matches_pattern(d, signature)][
                        :3
                    ],
                )

        return patterns

    def _find_avoided_patterns(self, decisions: list[Decision]) -> list[str]:
        """Find patterns that appear to be avoided.

        Looks for decisions with low confidence or reverted outcomes
        that might indicate avoided patterns.
        """
        avoided: list[str] = []

        # Find patterns associated with reverted decisions
        reverted = [d for d in decisions if d.outcome == "reverted"]
        for decision in reverted:
            for pattern_name, signature in self.PATTERN_SIGNATURES.items():
                if self._matches_pattern(decision, signature):
                    if pattern_name not in avoided:
                        avoided.append(pattern_name)

        # Find patterns with consistently low confidence
        pattern_confidences: dict[str, list[float]] = {}
        for decision in decisions:
            for pattern_name, signature in self.PATTERN_SIGNATURES.items():
                if self._matches_pattern(decision, signature):
                    if pattern_name not in pattern_confidences:
                        pattern_confidences[pattern_name] = []
                    pattern_confidences[pattern_name].append(decision.confidence)

        for pattern_name, confidences in pattern_confidences.items():
            if confidences and sum(confidences) / len(confidences) < 0.5:
                if pattern_name not in avoided:
                    avoided.append(pattern_name)

        return avoided

    def _count_pattern_matches(
        self,
        decisions: list[Decision],
        pattern_name: str,
        signature: dict[str, Any],
    ) -> int:
        """Count how many decisions match a pattern signature."""
        return sum(1 for d in decisions if self._matches_pattern(d, signature))

    def _matches_pattern(
        self,
        decision: Decision,
        signature: dict[str, Any],
    ) -> bool:
        """Check if a decision matches a pattern signature."""
        # Check action type
        if "action" in signature and decision.action != signature["action"]:
            return False

        # Check for keyword matches in target or rationale
        keywords = signature.get("keywords", [])
        search_text = f"{decision.target} {decision.rationale or ''}".lower()

        return any(kw.lower() in search_text for kw in keywords)

    def _infer_density_preference(self, decisions: list[Decision]) -> float:
        """Infer density vs routability preference.

        Returns value from 0 (prefers sparse) to 1 (prefers dense).
        """
        # Look at move decisions for spacing hints
        move_decisions = [d for d in decisions if d.action == "move"]

        if not move_decisions:
            return 0.5

        # Count decisions that prioritize density vs routability
        dense_hints = 0
        sparse_hints = 0

        for decision in move_decisions:
            rationale = (decision.rationale or "").lower()
            if any(kw in rationale for kw in ["compact", "tight", "close", "dense"]):
                dense_hints += 1
            if any(kw in rationale for kw in ["space", "routing", "spread", "room"]):
                sparse_hints += 1

        total = dense_hints + sparse_hints
        if total == 0:
            return 0.5

        return dense_hints / total

    def _infer_cost_performance_preference(self, decisions: list[Decision]) -> float:
        """Infer cost vs performance preference.

        Returns value from 0 (prefers cheap) to 1 (prefers performant).
        """
        # Look at decisions for cost/performance hints
        perf_hints = 0
        cost_hints = 0

        for decision in decisions:
            rationale = (decision.rationale or "").lower()
            if any(
                kw in rationale for kw in ["performance", "signal integrity", "impedance", "speed"]
            ):
                perf_hints += 1
            if any(kw in rationale for kw in ["cost", "cheap", "budget", "simple"]):
                cost_hints += 1

        total = perf_hints + cost_hints
        if total == 0:
            return 0.5

        return perf_hints / total

    def suggest_based_on_preferences(
        self,
        context: SessionContext,
        preferences: AgentPreferences | None = None,
    ) -> list[str]:
        """Generate suggestions based on learned preferences.

        Args:
            context: Current session context.
            preferences: Preferences to use (defaults to context.preferences).

        Returns:
            List of actionable suggestions.
        """
        suggestions: list[str] = []
        prefs = preferences or context.preferences

        # Get latest snapshot if available for current state analysis
        latest_snapshot = context.snapshots[-1] if context.snapshots else None

        # Check for spacing suggestions
        if prefs.preferred_spacing < 2.0:
            suggestions.append(
                f"Agent prefers tight spacing ({prefs.preferred_spacing:.1f}mm). "
                "Consider compacting component groups."
            )
        elif prefs.preferred_spacing > 4.0:
            suggestions.append(
                f"Agent prefers generous spacing ({prefs.preferred_spacing:.1f}mm). "
                "Ensure adequate clearance between components."
            )

        # Check for pattern suggestions
        if "bypass_cap_optimization" in prefs.common_patterns:
            suggestions.append(
                "Agent commonly optimizes bypass caps. "
                "Prioritize decoupling capacitor placement near power pins."
            )

        if "differential_pair_routing" in prefs.common_patterns:
            suggestions.append(
                "Agent frequently works with differential pairs. "
                "Maintain pair symmetry and impedance matching."
            )

        # Check for DRC-based suggestions
        if latest_snapshot and latest_snapshot.drc_violation_count > 0:
            suggestions.append(
                f"Current state has {latest_snapshot.drc_violation_count} DRC violations. "
                "Address these before proceeding."
            )

        # Check for alignment suggestions
        if prefs.alignment_preference == "grid":
            suggestions.append(
                "Agent prefers grid-aligned placement. "
                "Use 0.5mm or 1mm grid for component positions."
            )
        elif prefs.alignment_preference == "functional":
            suggestions.append(
                "Agent prefers functional grouping. Group related components by circuit function."
            )

        return suggestions

    def get_detected_patterns(self) -> list[PatternMatch]:
        """Get all detected patterns from the cache."""
        return list(self._pattern_cache.values())

    def clear_cache(self) -> None:
        """Clear the pattern detection cache."""
        self._pattern_cache.clear()
