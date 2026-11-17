"""Predictive warning types for MCP tools.

Provides dataclasses for anticipating potential future problems
from component moves and placements.
"""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class PredictiveWarningInfo:
    """A predictive warning about potential future problems.

    Predictive warnings anticipate issues that may arise from a component
    move, such as routing difficulties, congestion, or intent risks.

    Attributes:
        type: Warning type:
            - "routing_difficulty": Move makes routing harder
            - "congestion": Area becoming too dense
            - "thermal": Thermal management concerns
            - "intent_risk": May violate declared design intents
        message: Human-readable description of the warning
        confidence: Confidence level from 0.0 to 1.0
        suggestion: Optional suggestion to avoid the problem
        affected_nets: Net names affected by this warning
        location: (x, y) position where issue may occur
    """

    type: str
    message: str
    confidence: float
    suggestion: str | None = None
    affected_nets: list[str] = field(default_factory=list)
    location: tuple[float, float] | None = None

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        result: dict = {
            "type": self.type,
            "message": self.message,
            "confidence": round(self.confidence, 2),
            "affected_nets": self.affected_nets,
        }
        if self.suggestion:
            result["suggestion"] = self.suggestion
        if self.location:
            result["location"] = {"x": round(self.location[0], 3), "y": round(self.location[1], 3)}
        return result
