"""
Unified confidence scoring framework for kicad-tools.

This module provides standard methods for calculating confidence scores
across different matching contexts (part numbers, footprints, pins, etc.).

All confidence values are normalized to the range [0.0, 1.0]:
- 1.0: Perfect/exact match
- 0.8-0.99: High confidence match
- 0.5-0.79: Medium confidence match
- 0.3-0.49: Low confidence match
- 0.0-0.29: Very low confidence / no match
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ConfidenceLevel(Enum):
    """Standard confidence levels for categorizing match quality."""

    EXACT = 1.0
    HIGH = 0.9
    MEDIUM = 0.7
    LOW = 0.5
    VERY_LOW = 0.3
    NONE = 0.0

    @classmethod
    def from_score(cls, score: float) -> ConfidenceLevel:
        """
        Get the confidence level category for a given score.

        Args:
            score: Confidence score between 0.0 and 1.0

        Returns:
            The corresponding ConfidenceLevel
        """
        if score >= 0.95:
            return cls.EXACT
        elif score >= 0.8:
            return cls.HIGH
        elif score >= 0.6:
            return cls.MEDIUM
        elif score >= 0.4:
            return cls.LOW
        elif score >= 0.2:
            return cls.VERY_LOW
        else:
            return cls.NONE


@dataclass
class MatchResult:
    """
    Result of a matching operation with confidence score.

    Attributes:
        confidence: Confidence score between 0.0 and 1.0
        match_type: Description of how the match was made (e.g., "exact", "substring")
        matched_value: The value that was matched, if applicable
    """

    confidence: float
    match_type: str | None = None
    matched_value: str | None = None

    @property
    def level(self) -> ConfidenceLevel:
        """Get the confidence level category."""
        return ConfidenceLevel.from_score(self.confidence)

    def __post_init__(self) -> None:
        """Ensure confidence is clamped to valid range."""
        self.confidence = max(0.0, min(1.0, self.confidence))


def calculate_string_confidence(
    query: str,
    target: str,
    *,
    case_sensitive: bool = False,
    exact_score: float = 1.0,
    substring_score: float = 0.9,
    no_match_score: float = 0.7,
) -> MatchResult:
    """
    Calculate confidence score for string matching.

    This is a general-purpose string matching function that checks for:
    1. Exact match (highest confidence)
    2. Substring match (query in target or target in query)
    3. No direct match (lowest confidence)

    Args:
        query: The search string
        target: The string to match against
        case_sensitive: Whether to perform case-sensitive matching
        exact_score: Score for exact matches (default 1.0)
        substring_score: Score for substring matches (default 0.9)
        no_match_score: Score when there's no direct match (default 0.7)

    Returns:
        MatchResult with confidence score and match type

    Examples:
        >>> calculate_string_confidence("STM32F103", "STM32F103")
        MatchResult(confidence=1.0, match_type='exact', matched_value='STM32F103')

        >>> calculate_string_confidence("STM32", "STM32F103C8T6")
        MatchResult(confidence=0.9, match_type='substring', matched_value='STM32F103C8T6')
    """
    q = query if case_sensitive else query.lower()
    t = (target or "") if case_sensitive else (target or "").lower()

    # Handle empty strings - empty is substring of any string
    if not q and not t:
        # Both empty - exact match
        return MatchResult(
            confidence=exact_score,
            match_type="exact",
            matched_value=target or "",
        )
    elif not q or not t:
        # One is empty - empty is substring of non-empty
        return MatchResult(
            confidence=substring_score,
            match_type="substring",
            matched_value=target or "",
        )

    if q == t:
        return MatchResult(
            confidence=exact_score,
            match_type="exact",
            matched_value=target,
        )
    elif q in t or t in q:
        return MatchResult(
            confidence=substring_score,
            match_type="substring",
            matched_value=target,
        )
    else:
        return MatchResult(
            confidence=no_match_score,
            match_type="search",
            matched_value=target,
        )


def combine_confidences(
    *scores: float,
    method: str = "weighted_average",
    weights: list[float] | None = None,
) -> float:
    """
    Combine multiple confidence scores into a single score.

    Args:
        *scores: Variable number of confidence scores to combine
        method: Combination method:
            - "weighted_average": Weighted average of scores (default)
            - "min": Minimum of all scores (conservative)
            - "max": Maximum of all scores (optimistic)
            - "product": Product of all scores (very conservative)
        weights: Optional weights for weighted_average method

    Returns:
        Combined confidence score clamped to [0.0, 1.0]

    Examples:
        >>> combine_confidences(0.9, 0.8, 0.7)
        0.8

        >>> combine_confidences(0.9, 0.8, method="min")
        0.8

        >>> combine_confidences(0.9, 0.8, 0.7, weights=[0.5, 0.3, 0.2])
        0.85
    """
    if not scores:
        return 0.0

    score_list = list(scores)

    if method == "weighted_average":
        if weights is None:
            # Equal weights
            result = sum(score_list) / len(score_list)
        else:
            if len(weights) != len(score_list):
                raise ValueError("Number of weights must match number of scores")
            total_weight = sum(weights)
            if total_weight == 0:
                return 0.0
            result = sum(s * w for s, w in zip(score_list, weights, strict=False)) / total_weight
    elif method == "min":
        result = min(score_list)
    elif method == "max":
        result = max(score_list)
    elif method == "product":
        result = 1.0
        for s in score_list:
            result *= s
    else:
        raise ValueError(f"Unknown combination method: {method}")

    return max(0.0, min(1.0, result))


def adjust_confidence(
    base_confidence: float,
    *,
    multiplier: float = 1.0,
    bonus: float = 0.0,
    penalty: float = 0.0,
    minimum: float = 0.0,
    maximum: float = 1.0,
) -> float:
    """
    Adjust a confidence score with multipliers and bonuses.

    The adjustment is applied in order:
    1. Apply multiplier: base * multiplier
    2. Add bonus: result + bonus
    3. Subtract penalty: result - penalty
    4. Clamp to [minimum, maximum]

    Args:
        base_confidence: The starting confidence score
        multiplier: Multiplicative factor (default 1.0)
        bonus: Additive bonus (default 0.0)
        penalty: Subtractive penalty (default 0.0)
        minimum: Minimum result value (default 0.0)
        maximum: Maximum result value (default 1.0)

    Returns:
        Adjusted confidence score

    Examples:
        >>> adjust_confidence(0.5, bonus=0.2)
        0.7

        >>> adjust_confidence(0.8, multiplier=0.5, bonus=0.1)
        0.5

        >>> adjust_confidence(0.9, penalty=0.3, minimum=0.5)
        0.6
    """
    result = base_confidence * multiplier + bonus - penalty
    return max(minimum, min(maximum, result))
