"""
Utility functions for datasheet processing.
"""

from __future__ import annotations

import re

from kicad_tools.utils.scoring import calculate_string_confidence

# Characters that are safe to keep verbatim in a single filename/directory
# component. Everything else (path separators, whitespace, shell/FS-hostile
# characters, unicode look-alikes, NTFS ADS ':', etc.) is replaced with '_'.
_UNSAFE_FILENAME_CHARS = re.compile(r"[^A-Za-z0-9._-]")


def sanitize_filename_component(name: str) -> str:
    """
    Sanitize a string for safe use as a single filename/directory component.

    Uses a character allowlist (``A-Za-z0-9._-``): path separators ('/', '\\\\',
    ``os.sep``) and every other filesystem-unsafe character are replaced with
    '_'. Leading dots are then stripped so the result can never resolve to '.'
    or '..' (preventing a path-traversal escape when this value is joined onto
    a base output directory). Falls back to ``"unnamed_part"`` if the result
    would otherwise be empty.

    Examples::

        sanitize_filename_component("MCP6001UT-I/OT")  # -> "MCP6001UT-I_OT"
        sanitize_filename_component("../../escape")     # -> "_.._escape"
        sanitize_filename_component("STM32F103C8T6")    # -> "STM32F103C8T6"

    Args:
        name: The raw string to sanitize (e.g. a source-resolved part number).

    Returns:
        A safe single filename component containing only allowlisted characters
        and never a leading dot.
    """
    # The allowlist handles '/', '\\', os.sep, and anything else unsafe.
    safe = _UNSAFE_FILENAME_CHARS.sub("_", name)
    # Strip leading '.' sequences so "..", ".", "...foo" cannot produce a
    # relative-path escape or a hidden file.
    safe = safe.lstrip(".")
    return safe or "unnamed_part"


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
