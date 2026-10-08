"""Process-wide wall-clock deadline for a bounded routing excursion (#5923).

The corridor-yield recovery (#4463) re-runs the caller's whole main routing
strategy once.  A stage ``timeout`` alone cannot bound that re-run's wall
time: ``route_all_negotiated`` checks its stage budget only BETWEEN nets, so
a search already in flight overruns it, and the post-loop clearance pass and
post-negotiation sweep run on allowances of their own after it.  Measured on
board 06 (#5923 A/B): a 223.5 s stage cap produced a 361.1 s re-run.

This module carries ONE absolute deadline that every stage of the excursion
sees:

* :func:`deadline` -- context manager that installs ``now + seconds`` for the
  duration of a block (nested blocks keep the tighter deadline) and always
  restores the previous value on exit, including on an exception.
* :func:`clamp_search_timeout` -- the per-search choke point.  Both A*
  entry points (``CppPathfinder.route`` and the pure-Python
  ``Router.route``) pass their ``per_net_timeout`` through it, so no single
  search can run past the deadline by more than the backend's own deadline
  check granularity (the C++ search samples its clock every 1024 node
  expansions).  Once the deadline is spent every search gets
  :data:`MIN_SEARCH_TIMEOUT_S` and gives up almost immediately.
* :func:`remaining` / :func:`clamp_epoch_deadline` -- for the stages that
  keep their own ``time.time()`` ceilings (clearance correction, rescue
  sweep) so they stop or skip at the deadline instead of starting a fresh
  allowance.

* :func:`abandon` -- a stage whose remaining work cannot be cut short
  safely and cannot be afforded in the time left (``route_all_negotiated``'s
  DRC safety-net demotes, ~12 s of geometry checks on board 06) skips that
  work and marks the excursion ABANDONED instead.  The copper it leaves
  behind has not been through the safety nets, so **the caller that
  installed the deadline must discard the excursion's result when**
  :attr:`Excursion.abandoned` **is set** -- installing a deadline is a
  promise to do so.  The corridor-yield re-run is transactional (it reverts
  to the first pass), which is what makes this safe there.

What it does NOT bound: pure-Python work already running when the deadline
passes (grid marking, rollback and best-snapshot restore, the clearance
pass's up-front validation, a safety-net pass that outran its cost
estimate, ``_finalize_routing``).  That is the residual overrun; callers must
state the figure they measured rather than claim an exact bound.

No deadline is installed outside an explicit :func:`deadline` block, so every
normal routing call is unaffected (all helpers are identity functions then).
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator
from dataclasses import dataclass

# Floor handed to a search once the deadline is spent.  It must stay > 0:
# both backends treat ``0`` / ``None`` as "no deadline" (unbounded).
MIN_SEARCH_TIMEOUT_S: float = 0.01

_deadline_monotonic: float | None = None


@dataclass
class Excursion:
    """State of one :func:`deadline` block, yielded to its installer."""

    seconds: float | None
    abandoned: bool = False
    abandon_reason: str | None = None


_excursion: Excursion | None = None


def active() -> bool:
    """True while a :func:`deadline` block is installed."""
    return _deadline_monotonic is not None


def remaining() -> float | None:
    """Seconds left before the installed deadline (may be negative).

    ``None`` when no deadline is installed.
    """
    if _deadline_monotonic is None:
        return None
    return _deadline_monotonic - time.monotonic()


def expired() -> bool:
    """True when a deadline is installed and has passed."""
    left = remaining()
    return left is not None and left <= 0.0


def clamp_search_timeout(per_net_timeout: float | None) -> float | None:
    """Clamp one A* search's wall budget to the installed deadline.

    Returns ``per_net_timeout`` unchanged when no deadline is installed.
    Otherwise returns ``min(per_net_timeout, remaining)`` -- an unbudgeted
    search (``None`` / ``<= 0``) gets ``remaining`` -- floored at
    :data:`MIN_SEARCH_TIMEOUT_S` so a spent deadline yields a near-instant
    search failure rather than an unbounded one.
    """
    left = remaining()
    if left is None:
        return per_net_timeout
    left = max(MIN_SEARCH_TIMEOUT_S, left)
    if per_net_timeout is None or per_net_timeout <= 0:
        return left
    return min(float(per_net_timeout), left)


def clamp_epoch_deadline(epoch_deadline: float | None) -> float | None:
    """Clamp an absolute ``time.time()`` deadline to the installed one.

    ``None`` in, no deadline installed -> ``None`` out (unbounded, as before).
    """
    left = remaining()
    if left is None:
        return epoch_deadline
    ours = time.time() + left
    return ours if epoch_deadline is None else min(epoch_deadline, ours)


def abandon(reason: str) -> bool:
    """Mark the innermost installed excursion abandoned (see module doc).

    Returns ``True`` when an excursion was marked -- the caller may then skip
    work whose result the installer is obliged to discard.  Returns
    ``False`` (and the caller must do the work) when no deadline is
    installed.
    """
    if _excursion is None or _deadline_monotonic is None:
        return False
    if not _excursion.abandoned:
        _excursion.abandoned = True
        _excursion.abandon_reason = reason
    return True


@contextlib.contextmanager
def deadline(seconds: float | None) -> Iterator[Excursion]:
    """Install ``now + seconds`` as the deadline for the enclosed block.

    ``None`` installs nothing.  A nested block never loosens an outer
    deadline.  The previous value is restored on exit, exception or not.
    Yields the block's :class:`Excursion`; its ``abandoned`` flag must be
    honoured by the installer.
    """
    global _deadline_monotonic, _excursion
    previous = _deadline_monotonic
    previous_excursion = _excursion
    state = Excursion(seconds=seconds)
    if seconds is not None:
        mine = time.monotonic() + max(0.0, float(seconds))
        _deadline_monotonic = mine if previous is None else min(previous, mine)
        _excursion = state
    try:
        yield state
    finally:
        _deadline_monotonic = previous
        _excursion = previous_excursion
