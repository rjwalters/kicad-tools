"""Shared utilities for CLI commands."""

from __future__ import annotations

import sys
import traceback
from typing import TYPE_CHECKING

from kicad_tools.exceptions import KiCadToolsError

if TYPE_CHECKING:
    from rich.console import Console

__all__ = ["format_error", "print_error", "get_error_console"]

# Module-level console for error output, created lazily
_error_console: Console | None = None


def get_error_console() -> Console:
    """Get or create the Rich console for error output.

    Returns a console configured for stderr with appropriate settings.
    The console is created lazily and cached for reuse.
    """
    global _error_console
    if _error_console is None:
        from rich.console import Console

        _error_console = Console(stderr=True, force_terminal=None)
    return _error_console


def print_error(
    e: Exception,
    verbose: bool = False,
    use_rich: bool | None = None,
) -> None:
    """
    Print an exception with Rich formatting when available.

    Uses Rich console for beautiful error output on TTY terminals,
    falls back to plain text for non-TTY (pipes, JSON mode, etc.).

    Args:
        e: The exception to print
        verbose: If True, include full stack trace
        use_rich: Override automatic TTY detection (None = auto-detect)
    """
    console = get_error_console()

    # Determine whether to use Rich formatting
    if use_rich is None:
        use_rich = console.is_terminal

    if verbose:
        # Always use plain text for stack traces
        print(traceback.format_exc(), file=sys.stderr)
        return

    if use_rich and isinstance(e, KiCadToolsError):
        # Use Rich rendering via __rich_console__
        console.print(e)
    else:
        # Plain text fallback
        print(format_error(e, verbose=False), file=sys.stderr)


def format_error(e: Exception, verbose: bool = False) -> str:
    """
    Format an exception for user-friendly display (plain text).

    This is the plain-text fallback for non-TTY environments.
    For Rich formatted output, use print_error() instead.

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
