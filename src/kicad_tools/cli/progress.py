"""Progress indicators for CLI operations.

Provides progress bars and spinners for long-running CLI operations.
All progress output goes to stderr to keep stdout clean for data.

Usage:
    from kicad_tools.cli.progress import create_progress, with_progress, spinner

    # Progress bar for iterable with known length
    for item in with_progress(items, desc="Processing", quiet=args.quiet):
        process(item)

    # Manual progress tracking
    with create_progress(quiet=args.quiet) as progress:
        task = progress.add_task("Routing nets...", total=len(nets))
        for net in nets:
            route_net(net)
            progress.update(task, advance=1)

    # Spinner for operations without measurable progress
    with spinner("Loading PCB...", quiet=args.quiet):
        pcb = load_pcb(path)
"""

import sys
from collections.abc import Iterator
from contextlib import contextmanager
from typing import TypeVar

T = TypeVar("T")


def is_terminal() -> bool:
    """Check if stderr is attached to a terminal."""
    return sys.stderr.isatty()


def create_progress(quiet: bool = False, **kwargs):
    """Create a Rich Progress instance configured for CLI use.

    Args:
        quiet: If True, returns a no-op progress context
        **kwargs: Additional arguments passed to Progress

    Returns:
        Progress context manager (or no-op if quiet or not a terminal)
    """
    if quiet or not is_terminal():
        return _NoOpProgress()

    from rich.progress import (
        BarColumn,
        MofNCompleteColumn,
        Progress,
        SpinnerColumn,
        TaskProgressColumn,
        TextColumn,
        TimeElapsedColumn,
    )

    return Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TaskProgressColumn(),
        MofNCompleteColumn(),
        TimeElapsedColumn(),
        console=_get_stderr_console(),
        **kwargs,
    )


def with_progress(
    items: Iterator[T],
    total: int | None = None,
    desc: str = "Processing...",
    quiet: bool = False,
) -> Iterator[T]:
    """Wrap an iterable with a progress bar.

    Args:
        items: Iterable to wrap
        total: Total number of items (auto-detected if list/tuple)
        desc: Description to show
        quiet: If True, yields items without progress display

    Yields:
        Items from the input iterable
    """
    if quiet or not is_terminal():
        yield from items
        return

    # Try to get total from the items if not provided
    if total is None and hasattr(items, "__len__"):
        total = len(items)  # type: ignore

    from rich.progress import track

    yield from track(
        items,
        total=total,
        description=desc,
        console=_get_stderr_console(),
    )


@contextmanager
def spinner(desc: str = "Processing...", quiet: bool = False):
    """Display a spinner for operations without measurable progress.

    Args:
        desc: Description to show
        quiet: If True, no spinner is shown

    Example:
        with spinner("Loading PCB...", quiet=args.quiet):
            pcb = load_pcb(path)
    """
    if quiet or not is_terminal():
        yield
        return

    from rich.live import Live
    from rich.spinner import Spinner

    console = _get_stderr_console()
    spin = Spinner("dots", text=desc)

    with Live(spin, console=console, refresh_per_second=10, transient=True):
        yield


def print_status(message: str, style: str = "bold", quiet: bool = False) -> None:
    """Print a styled status message to stderr.

    Args:
        message: Message to print
        style: Rich style to apply
        quiet: If True, nothing is printed
    """
    if quiet:
        return

    console = _get_stderr_console()
    console.print(message, style=style)


def _get_stderr_console():
    """Get a Rich Console that outputs to stderr."""
    from rich.console import Console

    return Console(stderr=True, force_terminal=None)


class _NoOpProgress:
    """No-op progress context manager for quiet mode."""

    def __enter__(self):
        return self

    def __exit__(self, *args):
        pass

    def add_task(self, description: str, total: float | None = None, **kwargs) -> int:
        return 0

    def update(self, task_id: int, **kwargs) -> None:
        pass

    def advance(self, task_id: int, advance: float = 1) -> None:
        pass

    def remove_task(self, task_id: int) -> None:
        pass

    def start_task(self, task_id: int) -> None:
        pass

    def stop_task(self, task_id: int) -> None:
        pass
