"""
Progress callback infrastructure for long-running operations.

Provides a callback-based progress reporting API for routing, DRC, and export operations.
This enables progress reporting in agents and UIs.

Example::

    from kicad_tools.progress import ProgressCallback, ProgressContext

    # Callback function signature
    def on_progress(progress: float, message: str, cancelable: bool) -> bool:
        '''
        Args:
            progress: 0.0 to 1.0 (or -1 for indeterminate)
            message: Current operation description
            cancelable: Whether cancel is supported

        Returns:
            False to cancel operation, True to continue
        '''
        print(f"{progress*100:.0f}%: {message}")
        return True  # Continue

    # Usage with routing
    from kicad_tools.router import route_pcb
    route_pcb(
        "board.kicad_pcb",
        progress_callback=on_progress,
    )

    # Context manager for scoped progress
    with ProgressContext(callback=on_progress) as ctx:
        # Operations automatically use ctx callback
        ...

For CLI usage, see kicad_tools.cli.progress for Rich-based progress bars.
"""

from __future__ import annotations

import json
import sys
from collections.abc import Callable
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

# Type alias for progress callbacks
# Returns False to cancel, True to continue
ProgressCallback: TypeAlias = Callable[[float, str, bool], bool]


class ProgressReporter(Protocol):
    """Protocol for progress reporters."""

    def report(self, progress: float, message: str, cancelable: bool = True) -> bool:
        """Report progress.

        Args:
            progress: 0.0 to 1.0, or -1 for indeterminate
            message: Current operation description
            cancelable: Whether operation can be cancelled

        Returns:
            False to cancel, True to continue
        """
        ...


# Context variable for the current progress callback
_current_progress: ContextVar[ProgressCallback | None] = ContextVar(
    "current_progress", default=None
)


def get_current_callback() -> ProgressCallback | None:
    """Get the current progress callback from context.

    Returns:
        The current progress callback, or None if not in a progress context.
    """
    return _current_progress.get()


def report_progress(progress: float, message: str, cancelable: bool = True) -> bool:
    """Report progress using the current context callback.

    This is a convenience function for use within operations that support
    progress callbacks. It checks for a context callback and calls it if present.

    Args:
        progress: 0.0 to 1.0, or -1 for indeterminate
        message: Current operation description
        cancelable: Whether operation can be cancelled

    Returns:
        False if cancelled, True to continue (always True if no callback)
    """
    callback = get_current_callback()
    if callback is not None:
        return callback(progress, message, cancelable)
    return True


@dataclass
class ProgressEvent:
    """A progress event for JSON output mode."""

    progress: float
    message: str
    cancelable: bool

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for JSON serialization."""
        return {
            "progress": self.progress,
            "message": self.message,
            "cancelable": self.cancelable,
        }

    def to_json(self) -> str:
        """Convert to JSON string."""
        return json.dumps(self.to_dict())


class ProgressContext:
    """Context manager for scoped progress reporting.

    Allows setting a progress callback that applies to all operations
    within the context scope.

    Example::

        def on_progress(progress, message, cancelable):
            print(f"{progress*100:.0f}%: {message}")
            return True

        with ProgressContext(callback=on_progress) as ctx:
            # Operations within this context will use the callback
            router.route_all()  # Will report progress
            exporter.export()   # Will also report progress

        # Or access the context for manual reporting
        with ProgressContext(callback=on_progress) as ctx:
            ctx.report(0.5, "Halfway done")
    """

    def __init__(self, callback: ProgressCallback | None = None):
        """Initialize progress context.

        Args:
            callback: Progress callback function. If None, progress is not reported.
        """
        self._callback = callback
        self._token = None
        self._cancelled = False

    def __enter__(self) -> ProgressContext:
        """Enter the context, setting the callback as current."""
        self._token = _current_progress.set(self._callback)
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> None:
        """Exit the context, restoring the previous callback."""
        if self._token is not None:
            _current_progress.reset(self._token)

    def report(self, progress: float, message: str, cancelable: bool = True) -> bool:
        """Report progress through the callback.

        Args:
            progress: 0.0 to 1.0, or -1 for indeterminate
            message: Current operation description
            cancelable: Whether operation can be cancelled

        Returns:
            False if cancelled, True to continue
        """
        if self._cancelled:
            return False
        if self._callback is not None:
            result = self._callback(progress, message, cancelable)
            if not result:
                self._cancelled = True
            return result
        return True

    @property
    def cancelled(self) -> bool:
        """Check if operation has been cancelled."""
        return self._cancelled


def create_json_callback(file=None) -> ProgressCallback:
    """Create a callback that outputs JSON progress events.

    Useful for agent consumption where structured output is needed.

    Args:
        file: File to write to (default: sys.stderr)

    Returns:
        A progress callback that outputs JSON events.

    Example::

        callback = create_json_callback()
        route_pcb("board.kicad_pcb", progress_callback=callback)
        # Outputs: {"progress": 0.5, "message": "Routing net GND", "cancelable": true}
    """
    output = file or sys.stderr

    def json_callback(progress: float, message: str, cancelable: bool) -> bool:
        event = ProgressEvent(progress=progress, message=message, cancelable=cancelable)
        print(event.to_json(), file=output, flush=True)
        return True  # Never cancel from JSON callback

    return json_callback


def create_print_callback(file=None, show_percent: bool = True) -> ProgressCallback:
    """Create a simple callback that prints progress to a file.

    Args:
        file: File to write to (default: sys.stderr)
        show_percent: Whether to show percentage

    Returns:
        A progress callback that prints progress.
    """
    output = file or sys.stderr

    def print_callback(progress: float, message: str, cancelable: bool) -> bool:
        if show_percent and progress >= 0:
            print(f"{progress * 100:.0f}%: {message}", file=output, flush=True)
        else:
            print(message, file=output, flush=True)
        return True

    return print_callback


@contextmanager
def null_progress():
    """Context manager that provides no-op progress reporting.

    Useful for testing or when progress is not needed.
    """
    yield ProgressContext(callback=None)


class SubProgressCallback:
    """Wrapper that scales progress for sub-operations.

    When an operation has multiple phases, this allows each phase
    to report 0-100% while the parent sees the scaled range.

    Example::

        def on_progress(progress, message, cancelable):
            print(f"Overall: {progress*100:.0f}%")
            return True

        # Phase 1: 0-50%
        phase1 = SubProgressCallback(on_progress, start=0.0, end=0.5)
        phase1(0.5, "Phase 1 halfway")  # Reports 25% to parent

        # Phase 2: 50-100%
        phase2 = SubProgressCallback(on_progress, start=0.5, end=1.0)
        phase2(0.5, "Phase 2 halfway")  # Reports 75% to parent
    """

    def __init__(
        self,
        parent: ProgressCallback,
        start: float = 0.0,
        end: float = 1.0,
        prefix: str = "",
    ):
        """Initialize sub-progress wrapper.

        Args:
            parent: Parent callback to forward to
            start: Start of this phase in parent's progress (0.0-1.0)
            end: End of this phase in parent's progress (0.0-1.0)
            prefix: Optional prefix for messages
        """
        self._parent = parent
        self._start = start
        self._end = end
        self._prefix = prefix

    def __call__(self, progress: float, message: str, cancelable: bool = True) -> bool:
        """Report progress, scaling to parent range.

        Args:
            progress: 0.0 to 1.0 within this phase, or -1 for indeterminate
            message: Current operation description
            cancelable: Whether operation can be cancelled

        Returns:
            False to cancel, True to continue
        """
        if progress < 0:
            # Indeterminate - pass through
            scaled = -1
        else:
            # Scale to parent range
            scaled = self._start + (progress * (self._end - self._start))

        full_message = f"{self._prefix}{message}" if self._prefix else message
        return self._parent(scaled, full_message, cancelable)


def create_cli_adapter(quiet: bool = False):
    """Create an adapter that bridges progress callbacks to CLI progress bars.

    This integrates with the Rich-based progress bars in kicad_tools.cli.progress.

    Args:
        quiet: If True, creates a no-op adapter

    Returns:
        A context manager that provides both CLI progress and callback progress.

    Example::

        with create_cli_adapter(quiet=args.quiet) as (progress, callback):
            # progress is a Rich Progress instance
            # callback is a ProgressCallback for the operation
            route_pcb("board.kicad_pcb", progress_callback=callback)
    """
    from .cli.progress import create_progress

    @contextmanager
    def adapter():
        with create_progress(quiet=quiet) as progress:
            task_id = None

            def callback(prog: float, message: str, cancelable: bool) -> bool:
                nonlocal task_id
                if task_id is None:
                    task_id = progress.add_task(message, total=100)
                if prog >= 0:
                    progress.update(task_id, completed=int(prog * 100), description=message)
                else:
                    progress.update(task_id, description=message)
                return True

            yield progress, callback

    return adapter()
