"""Shared severity parsing utilities for DRC/ERC violations."""

from __future__ import annotations

from typing import Any


class SeverityMixin:
    """Mixin providing severity parsing from strings.

    This mixin expects the enum to have ERROR and WARNING members.
    Additional members (INFO, EXCLUSION) are detected dynamically.
    """

    @classmethod
    def from_string(cls, s: str, default: Any = None) -> Any:
        """Parse severity from string.

        Args:
            s: String to parse (e.g., "error", "Warning", "EXCLUSION")
            default: Default value if no match found. If None, uses the
                     last enum member as default.

        Returns:
            Matching severity enum member.
        """
        s_lower = s.lower().strip()

        # Check for error
        if "error" in s_lower:
            return cls.ERROR  # type: ignore[attr-defined]

        # Check for warning
        if "warning" in s_lower:
            return cls.WARNING  # type: ignore[attr-defined]

        # Check for exclusion (ERC-specific)
        if hasattr(cls, "EXCLUSION") and "exclu" in s_lower:
            return cls.EXCLUSION  # type: ignore[attr-defined]

        # Check for info (DRC-specific)
        if hasattr(cls, "INFO") and "info" in s_lower:
            return cls.INFO  # type: ignore[attr-defined]

        # Return default or last member
        if default is not None:
            return default

        # Fall back to last enum member (INFO for DRC, WARNING for ERC)
        members = list(cls)  # type: ignore[call-overload]
        return members[-1] if members else cls.WARNING  # type: ignore[attr-defined]
