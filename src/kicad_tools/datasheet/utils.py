"""
Utility functions for datasheet processing.
"""

from __future__ import annotations

from kicad_tools.utils.scoring import calculate_string_confidence


def calculate_part_confidence(query: str, part_number: str) -> float:
    """
    Calculate confidence score for part number matching.

    The confidence score indicates how closely the query matches the part number:
    - 1.0: Exact match (case-insensitive)
    - 0.9: Substring match (query in part_number or vice versa)
    - 0.7: No direct match (result from search relevance)

    Args:
        query: The search query (e.g., user-provided part number)
        part_number: The manufacturer part number from the result

    Returns:
        Confidence score between 0.0 and 1.0
    """
    result = calculate_string_confidence(
        query,
        part_number or "",
        case_sensitive=False,
        exact_score=1.0,
        substring_score=0.9,
        no_match_score=0.7,
    )
    return result.confidence
