"""Engine-agnostic diff-pair via-count parity audit (issue #7093).

A differential pair whose two nets carry different numbers of vias has an
unmatched layer change: a mode-conversion and impedance discontinuity, and a
z-length skew.  ``kct check`` reports it as the ``diffpair_uncoupled_length``
"unequal via counts" finding, counting every via on each net of an *engaged*
pair (:mod:`kicad_tools.validate.rules.diffpair_uncoupled_length`).

The router tries to keep the counts equal on every path -- the coupled fat
agent is planar, a fan-via gateway gives each net one via per gatewayed end,
and the lattice uncoupled fallback ranks and mirrors its legs for parity.
When it still commits unequal copper (a lattice fallback or hybrid leg that
had no via-equal candidate, the grid engine's independent fallback, or a pair
deferred to the single-ended main loop), that is a pair-rule failure, and it
must never be silent.  This module measures the committed routes the same
way the checker measures the board, so the router's report and the
checker's finding name the same pairs with the same counts.

Nothing here imports an engine; :meth:`Autorouter._finalize_routing` feeds it.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class PairViaParityFailure:
    """One engaged pair committed with unequal P/N via counts."""

    p_name: str
    n_name: str
    vias_p: int
    vias_n: int

    def warning(self) -> str:
        """The router's ``WARNING`` line for this failure."""
        return (
            f"  WARNING: diff pair {self.p_name}/{self.n_name} committed with unequal "
            f"via counts ({self.p_name}: {self.vias_p}, {self.n_name}: {self.vias_n}); "
            "pair-rule failure: the router could not give both nets the same layer "
            "changes (issue #7093)"
        )


def net_via_counts(routes: Iterable[Any]) -> dict[int, int]:
    """``{net id: vias}`` over ``routes`` (each a :class:`~.primitives.Route`)."""
    counts: dict[int, int] = {}
    for route in routes:
        vias = getattr(route, "vias", None) or ()
        if vias:
            counts[route.net] = counts.get(route.net, 0) + len(vias)
    return counts


def audit_pair_via_parity(
    routes: Iterable[Any],
    pairs: Iterable[tuple[int, str, int, str]],
) -> list[PairViaParityFailure]:
    """Pairs whose committed nets carry unequal via counts.

    ``pairs`` is ``(p net id, p name, n net id, n name)`` per engaged pair.
    A pair with no copper on one net is not reported here: an unrouted net
    is already reported as an open, and its via count says nothing.
    """
    route_list = list(routes)
    routed = {r.net for r in route_list if getattr(r, "segments", None)}
    counts = net_via_counts(route_list)
    failures: list[PairViaParityFailure] = []
    for p_id, p_name, n_id, n_name in pairs:
        if p_id not in routed or n_id not in routed:
            continue
        vias_p, vias_n = counts.get(p_id, 0), counts.get(n_id, 0)
        if vias_p != vias_n:
            failures.append(PairViaParityFailure(p_name, n_name, vias_p, vias_n))
    return failures
