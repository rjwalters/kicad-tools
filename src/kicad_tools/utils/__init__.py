"""
Utility modules for kicad-tools.
"""

from __future__ import annotations

from pathlib import Path

from .scoring import (
    ConfidenceLevel,
    MatchResult,
    adjust_confidence,
    calculate_string_confidence,
    combine_confidences,
)


def ensure_parent_dir(path: Path) -> Path:
    """
    Ensure the parent directory of a path exists.

    Creates the parent directory (and any missing ancestors) if it doesn't exist.
    This is a convenience function to replace the common pattern:
        path.parent.mkdir(parents=True, exist_ok=True)

    Args:
        path: The file path whose parent directory should be ensured.

    Returns:
        The original path, unchanged. This allows chaining like:
            with ensure_parent_dir(output_path).open('w') as f:
                ...

    Example:
        >>> from pathlib import Path
        >>> output = Path("/tmp/test/subdir/file.txt")
        >>> ensure_parent_dir(output)
        PosixPath('/tmp/test/subdir/file.txt')
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


__all__ = [
    "ConfidenceLevel",
    "MatchResult",
    "calculate_string_confidence",
    "combine_confidences",
    "adjust_confidence",
    "ensure_parent_dir",
]
