"""Per-tier wall-clock budget slicing for ``--auto-mfr-tier`` escalation.

Issue #3463: the manufacturer-tier escalation ladder
(``route_with_mfr_tier_escalation`` in :mod:`kicad_tools.cli.route_cmd`)
walks a sequence of tiers (e.g. ``jlcpcb -> jlcpcb-tier1``).  For each
tier it re-enters the layer-escalation path, which divides the *remaining
routing budget* across its own layer attempts via
``_per_attempt_budgeted_timeout``.

The bug: that inner helper sees the **full** remaining routing budget at
base-tier time, so the base tier's layer escalation expands to fill the
entire routing deadline.  When control returns to the outer ladder, the
routing deadline is already exhausted, ``_deadline_expired(args)`` is
``True`` at the top of the next tier iteration, and the loop breaks
*before* attempting ``jlcpcb-tier1`` -- exactly the symptom reported in
#3463 (per-tier banner only ever shows ``['jlcpcb']``, escalation aborts
on the base-tier escape-infeasibility diagnosis instead of advancing to
the via-in-pad-capable tier).

The fix lives here, deliberately out of
:meth:`kicad_tools.router.core.Autorouter.route_all_negotiated` (a sibling
PR touches that method).  We expose a small context manager that
temporarily narrows ``args._routing_deadline`` to a fair per-tier slice of
the *remaining* routing budget, so each tier -- including the final one --
gets a real chance to run.  The original deadline is restored on exit so
auto-fix and any subsequent CLI reuse of ``args`` see the unmodified
total.

Design notes:

* The slice rolls forward naturally: a fast-finishing tier leaves more
  budget for the next, because the slice is recomputed from the *current*
  remaining budget divided by the *current* number of outstanding tiers.
* When no wall-clock deadline is configured (legacy unbounded runs, or
  ``--timeout`` unset) the context manager is a no-op and the inner path
  keeps its existing unbounded behaviour.
* The narrowing is intentionally a *floor-guarded* slice: we never narrow
  the per-tier deadline below a small floor, so a ladder with many tiers
  and a tight ``--timeout`` does not collapse the final tier to zero
  budget.  The floor matches the per-attempt floor philosophy used by the
  layer-escalation budget helper.
"""

from __future__ import annotations

import contextlib
import time
from collections.abc import Iterator

# Minimum per-tier routing budget (seconds).  Mirrors the spirit of the
# auto-fix reserve floor: a tier given less than this much time cannot
# make meaningful progress, so we never slice below it.  If the total
# remaining budget is itself below the floor, the slice collapses to the
# remaining budget (the tier gets whatever is left).
_PER_TIER_FLOOR_SEC = 30.0


def per_tier_routing_deadline(
    args,
    *,
    tier_index: int,
    tier_count: int,
    now: float | None = None,
) -> float | None:
    """Compute a fair per-tier routing deadline (monotonic clock value).

    Divides the *remaining* routing budget evenly across the *remaining*
    tiers (including the current one) and returns the monotonic deadline
    for the current tier: ``now + slice``.

    Args:
        args: Parsed CLI namespace.  Must carry ``_routing_deadline`` (set
            by ``_set_wall_clock_deadline``); when that is ``None`` this
            returns ``None`` (legacy unbounded behaviour).
        tier_index: 0-based index of the current tier in the ladder.
        tier_count: Total number of tiers the ladder intends to attempt.
        now: Override for the current monotonic time (testing seam).
            Defaults to ``time.monotonic()``.

    Returns:
        A monotonic deadline (float) for the current tier, or ``None`` when
        no routing deadline is configured.  When the current tier is the
        last outstanding one, the returned deadline equals the original
        routing deadline (no point reserving budget for a tier that does
        not exist).
    """
    routing_deadline = getattr(args, "_routing_deadline", None)
    if routing_deadline is None:
        return None

    if now is None:
        now = time.monotonic()

    remaining = routing_deadline - now
    if remaining <= 0.0:
        # Routing deadline already passed -- return the original so the
        # caller's existing ``_deadline_expired`` check fires as before.
        return routing_deadline

    # Outstanding tiers including the current one.
    outstanding = max(1, tier_count - tier_index)
    if outstanding <= 1:
        # Last tier: hand it the whole remaining budget.
        return routing_deadline

    per_tier_slice = remaining / outstanding
    # Floor-guard: never collapse a tier below the floor unless the total
    # remaining budget is itself below the floor (in which case the tier
    # simply gets whatever is left).
    per_tier_slice = max(per_tier_slice, min(_PER_TIER_FLOOR_SEC, remaining))
    # Never exceed the original routing deadline.
    return min(routing_deadline, now + per_tier_slice)


@contextlib.contextmanager
def per_tier_routing_budget(
    args,
    *,
    tier_index: int,
    tier_count: int,
) -> Iterator[None]:
    """Temporarily narrow ``args._routing_deadline`` to a per-tier slice.

    On ``__enter__`` the routing deadline is replaced with the value from
    :func:`per_tier_routing_deadline` (when a deadline is configured); on
    ``__exit__`` the original is restored unconditionally so auto-fix and
    any subsequent reuse of ``args`` see the unmodified total.

    This is the mechanism that lets the manufacturer-tier escalation ladder
    reserve budget for later tiers instead of letting the first tier's
    layer-escalation loop consume the entire routing deadline (Issue #3463).

    No-op (yields immediately, restores nothing meaningful) when no routing
    deadline is configured.
    """
    original = getattr(args, "_routing_deadline", None)
    narrowed = per_tier_routing_deadline(
        args,
        tier_index=tier_index,
        tier_count=tier_count,
    )
    if narrowed is not None:
        args._routing_deadline = narrowed
    try:
        yield
    finally:
        # Restore unconditionally.  ``original`` may be ``None`` (unbounded
        # run) -- restoring ``None`` is correct in that case.
        args._routing_deadline = original
