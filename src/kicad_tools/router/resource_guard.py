"""Keep resource-exhaustion failures out of routing decisions (Issue #4724).

The router is full of deliberately-broad ``except Exception`` handlers whose
contract is *"a defect in an optional/defensive computation must never abort a
route"*.  That contract is right for a malformed board, a fixture grid without
an occupancy API, or a diagnostic that cannot run -- and wrong for
**resource exhaustion**.

A ``MemoryError`` (or a native allocation failure surfacing through the C++
grid mirror) raised inside one net's search is not a property of the board: it
is a property of the *host at that instant*.  When a broad handler absorbs it,
the router silently degrades a decision -- "this Steiner cell is free", "this
component has no pitch", "this reroute failed" -- and the run continues onto a
DIFFERENT trajectory.  That is exactly the #4724 signature: board 06's
stagnation recovery reported ``restored 5 / rerouted 6`` on a host at
load-average ~90 where five same-code runs on a quiet host all reported
``restored 4 / rerouted 7``, then diverged chaotically from there.  Both
artifacts were valid and passed every gate, so nothing failed loudly; the run
was simply not the run the same command produces on a quiet machine.

The rule this module enforces: **an exhausted host must abort the route, not
change it.**  Call :func:`reraise_if_resource_exhaustion` as the first
statement of a broad handler that sits on a routing-decision path.  Ordinary
exceptions fall through untouched (legacy behaviour byte-for-byte); an
exhaustion failure is announced on stderr with its type and the handler's
context, then re-raised with its original traceback.

Deliberately NOT applied to handlers whose fallback cannot change a search
outcome -- the GPU-backend init/sync handlers in ``grid.py``, for instance,
already log and fall back to the CPU backend, which is a legitimate
degradation on a machine whose *accelerator* memory is full.
"""

from __future__ import annotations

import errno
import sys

__all__ = ["is_resource_exhaustion", "reraise_if_resource_exhaustion"]

# Substrings a NATIVE allocation failure carries when the binding layer
# surfaces it as a plain ``RuntimeError`` rather than translating it to
# ``MemoryError`` (nanobind translates ``std::bad_alloc`` to ``MemoryError``,
# but a C++ layer that catches and rethrows with its own message does not).
_NATIVE_ALLOCATION_MARKERS = (
    "bad_alloc",
    "bad alloc",
    "cannot allocate memory",
    "out of memory",
)


def is_resource_exhaustion(exc: BaseException | None) -> bool:
    """Is ``exc`` a host-resource failure rather than a board/data failure?

    Recognised:

    * :class:`MemoryError` -- including the nanobind translation of a C++
      ``std::bad_alloc`` from the grid mirror / pathfinder.
    * :class:`RecursionError` -- stack exhaustion; load-correlated in the
      same way (a deeper interpreter stack under a loaded host is not the
      mechanism, but the failure is a host limit, not a board property).
    * :class:`OSError` with ``errno.ENOMEM`` -- e.g. an mmap/thread/alloc
      refusal from the OS.
    * :class:`RuntimeError` whose message names a native allocation failure
      (see :data:`_NATIVE_ALLOCATION_MARKERS`) -- the rethrow case above.

    Everything else -- ``ValueError``, ``AttributeError``, ``KeyError``, the
    fixture-grid ``TypeError``s the broad handlers exist for -- is NOT
    exhaustion and must keep its historical fallback.
    """
    if exc is None:
        return False
    if isinstance(exc, MemoryError | RecursionError):
        return True
    if isinstance(exc, OSError) and exc.errno == errno.ENOMEM:
        return True
    if isinstance(exc, RuntimeError):
        text = str(exc).lower()
        return any(marker in text for marker in _NATIVE_ALLOCATION_MARKERS)
    return False


def reraise_if_resource_exhaustion(context: str) -> None:
    """Re-raise the in-flight exception when it is resource exhaustion.

    Call this as the FIRST statement inside a broad ``except Exception``
    handler on a routing-decision path::

        try:
            ...
        except Exception:
            reraise_if_resource_exhaustion("steiner: blocked-cell probe")
            return None  # unchanged legacy fallback

    It reads the exception being handled from :func:`sys.exc_info`, so a bare
    ``except Exception:`` handler does not have to grow an ``as exc`` binding.
    A non-exhaustion exception (or no active exception at all) returns
    silently and the caller's fallback runs exactly as before.

    Args:
        context: Short human-readable identity of the guarded handler, e.g.
            ``"steiner: blocked-cell probe"``.  Named in the stderr line so a
            log that reaches a bug report says WHICH decision was at risk.

    Raises:
        BaseException: the in-flight exception, with its original traceback,
            when :func:`is_resource_exhaustion` accepts it.
    """
    exc = sys.exc_info()[1]
    if not is_resource_exhaustion(exc):
        return
    print(
        f"  [resource-exhaustion] {context}: {type(exc).__name__}: {exc} -- "
        "aborting instead of degrading a routing decision (issue #4724)",
        file=sys.stderr,
        flush=True,
    )
    raise
