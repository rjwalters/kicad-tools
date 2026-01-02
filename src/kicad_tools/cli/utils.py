"""Shared utilities for CLI commands."""

import traceback

from kicad_tools.exceptions import KiCadToolsError

__all__ = ["format_error"]


def format_error(e: Exception, verbose: bool = False) -> str:
    """
    Format an exception for user-friendly display.

    Args:
        e: The exception to format
        verbose: If True, include full stack trace

    Returns:
        Formatted error message string
    """
    if verbose:
        return traceback.format_exc()

    if isinstance(e, KiCadToolsError):
        return f"Error: {e}"

    # For other exceptions, show type and message
    return f"Error: {type(e).__name__}: {e}"
