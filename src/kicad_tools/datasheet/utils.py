"""
Utility functions for datasheet processing.
"""

from __future__ import annotations


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
    query_lower = query.lower()
    part_lower = (part_number or "").lower()

    if query_lower == part_lower:
        return 1.0
    elif query_lower in part_lower or part_lower in query_lower:
        return 0.9
    else:
        return 0.7
