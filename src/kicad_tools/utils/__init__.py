"""
Utility modules for kicad-tools.
"""

from .scoring import (
    ConfidenceLevel,
    MatchResult,
    adjust_confidence,
    calculate_string_confidence,
    combine_confidences,
)

__all__ = [
    "ConfidenceLevel",
    "MatchResult",
    "calculate_string_confidence",
    "combine_confidences",
    "adjust_confidence",
]
