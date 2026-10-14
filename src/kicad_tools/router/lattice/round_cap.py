"""Lattice netset negotiation round cap and stop-reason reporting (issue #6362).

``Autorouter._negotiate_lattice_netset`` runs the whole-board lattice
negotiation once, bounded by two independent limits: a round cap
(``max_iterations``) and an optional wall-clock deadline.  When the run ends
unconverged, the summary line alone (``iterations=N, converged=False``) does
not say which limit fired, so a reader cannot tell "give it more rounds"
(``--lattice-iterations``) from "give it more time" (``--timeout`` /
``--per-net-timeout``).  :func:`lattice_stop_reason` names it.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .pathfinder import LatticeNegotiationStats

# Issue #6381: pair-free boards get the same 12 rounds as boards with coupled
# diff pairs (#4270).  ``kct route --lattice-iterations N`` overrides it.
LATTICE_NEGOTIATION_DEFAULT_ROUNDS = 12


def lattice_stop_reason(stats: LatticeNegotiationStats, cap: int) -> str | None:
    """Return a log clause naming the limit that ended an unconverged run.

    Returns ``None`` for a converged run (its output stays unchanged) and for
    an unconverged run that hit neither limit (nothing honest to report).
    The deadline wins when both are true: a deadline cut is the binding
    constraint even if it happened to land on the final round.
    """
    if stats.converged:
        return None
    if stats.deadline_hit:
        return (
            f"stopped by: deadline after {stats.iterations}/{cap} round(s) "
            f"({stats.elapsed_s:.1f}s) -- raise --timeout / --per-net-timeout "
            "for more time"
        )
    if stats.iterations >= cap:
        return f"stopped by: round cap ({cap}) -- raise --lattice-iterations for more rounds"
    return None
