"""Congested-vs-blocked diagnosis for ``kct route-auto`` (Issue #6001).

``kct route`` classifies each unrouted connection with
:func:`~kicad_tools.router.unrouted_cause.diagnose_unrouted` (Issue #5944).
``kct route-auto`` runs the RoutingOrchestrator instead, a separate code path
whose strategies route on different grids:

* ``global`` / ``escape`` / ``subgrid`` / ``via_resolution`` /
  ``multi_resolution`` / ``full_pipeline`` produce their copper from the
  coarse corridor planner (``GlobalRouter`` over a ``RegionGraph``).  That
  planner has no obstacle model and no per-cell search, so a failure there is
  not a failed search that can be re-run and explained.
* ``hierarchical`` runs the negotiated A* router on a fine grid at the board
  rules' ``grid_resolution``, with the board's keepout rule areas installed
  (#6059).

Which grid the diagnosis runs on
--------------------------------
The fine grid the hierarchical strategy searched is the right *resolution*.
Since #6107 it is also the right board when route-auto has the board file: the
strategy routes on the board loaded with every other net skipped (their pads
become obstacles) and only the routed net routable.  That grid still cannot
show a *contender* -- a skipped net's pads are anonymous obstacles there, and
the classifier needs every net's copper committed as liftable routes.  (Without
a board file the strategy falls back to a grid holding only the routed net's
own pads, which shows nothing at all.)  The diagnosis therefore rebuilds the
grid from the board file --
same design rules and resolution, same keepout rule areas, plus every pad and
every existing track and via -- with the loader ``kct route`` uses
(:func:`~kicad_tools.router.io.load_pcb_for_routing` with
``load_existing_routes=True``), and runs the unchanged #5944 classifier on it.
That makes a ``route-auto`` entry carry exactly the schema ``kct route``
emits: ``congested`` names the nets whose existing copper lies on the solo
path, ``blocked`` names the pads, keepout rule areas (by name, #6008) and
board edge closing an endpoint off.

When no attempted strategy searched a fine grid -- the user forced a corridor
strategy, or the retries never reached ``hierarchical`` -- every connection is
reported ``unclassified`` with a note that says so, rather than classified
against a search that never happened.

The connections diagnosed are the pad islands the strategy left apart: pads
already joined by this net's existing copper (and, for a partial result, its
new copper) form one island, and the Euclidean MST between islands gives one
connection per missing join.
"""

from __future__ import annotations

import contextlib
import copy
import math
import sys
import time
from collections.abc import Iterable, Sequence
from typing import TYPE_CHECKING, Any

from .strategies import RoutingStrategy
from .unrouted_cause import (
    CAUSE_UNCLASSIFIED,
    DEFAULT_PER_CONNECTION_S,
    UnroutedConnection,
    UnroutedDiagnosis,
    diagnose_unrouted,
)

if TYPE_CHECKING:
    from .core import Autorouter
    from .layers import LayerStack
    from .primitives import Pad
    from .rules import DesignRules
    from .strategies import RoutingResult

#: Strategies that run a per-cell A* search on a fine routing grid.  Every
#: other route-auto strategy takes its copper from the coarse corridor planner.
FINE_GRID_STRATEGIES = frozenset({RoutingStrategy.HIERARCHICAL_DIFF_PAIR})

_STRATEGY_CLI_NAMES = {
    RoutingStrategy.GLOBAL_WITH_REPAIR: "global",
    RoutingStrategy.ESCAPE_THEN_GLOBAL: "escape",
    RoutingStrategy.HIERARCHICAL_DIFF_PAIR: "hierarchical",
    RoutingStrategy.SUBGRID_ADAPTIVE: "subgrid",
    RoutingStrategy.VIA_CONFLICT_RESOLUTION: "via_resolution",
    RoutingStrategy.MULTI_RESOLUTION: "multi_resolution",
    RoutingStrategy.FULL_PIPELINE: "full_pipeline",
}


def _pad_key(pad: Any) -> tuple[str, str]:
    return (str(getattr(pad, "ref", "") or ""), str(getattr(pad, "pin", "") or ""))


def island_bridges(pads: Sequence[Pad], labels: Sequence[int]) -> list[tuple[Pad, Pad]]:
    """One pad pair per missing join between copper islands.

    A Euclidean minimum spanning tree over ``pads`` in which two pads of the
    same island (equal ``labels``) are free to join; the tree edges that cross
    islands are the connections still to route.  A net with no copper at all
    (every label distinct) yields its plain MST.
    """
    n = len(pads)
    if n < 2:
        return []
    in_tree = [False] * n
    best = [math.inf] * n
    parent = [-1] * n
    best[0] = 0.0
    bridges: list[tuple[Pad, Pad]] = []
    for _ in range(n):
        u = min((i for i in range(n) if not in_tree[i]), key=lambda i: best[i])
        in_tree[u] = True
        if parent[u] >= 0 and labels[parent[u]] != labels[u]:
            bridges.append((pads[parent[u]], pads[u]))
        for v in range(n):
            if in_tree[v]:
                continue
            d = (
                0.0
                if labels[u] == labels[v]
                else math.hypot(pads[u].x - pads[v].x, pads[u].y - pads[v].y)
            )
            if d < best[v]:
                best[v] = d
                parent[v] = u
    return bridges


def load_diagnosis_router(
    pcb_path: str,
    rules: DesignRules,
    layer_stack: LayerStack | None = None,
    *,
    force_python: bool = False,
) -> Autorouter:
    """The board as ``kct route`` would load it, ready for the classifier.

    Every pad, every existing track and via (committed as routes, so the
    classifier's lift takes them off), the board-edge keepout and the keepout
    rule areas (#6008) -- on a grid at ``rules.grid_resolution``.  The
    loader's progress lines go to stderr: ``route-auto --format json`` and the
    MCP server both own stdout.
    """
    from .io import load_pcb_for_routing

    with contextlib.redirect_stdout(sys.stderr):
        router, _net_map = load_pcb_for_routing(
            pcb_path,
            rules=copy.copy(rules),
            use_pcb_rules=False,
            validate_drc=False,
            layer_stack=layer_stack,
            load_existing_routes=True,
            force_python=force_python,
        )
        router._install_grid_rule_area_keepouts()
    return router


def _unclassified(
    net_id: int, net_name: str, pairs: Iterable[tuple[Any, Any]], note: str
) -> list[UnroutedConnection]:
    return [
        UnroutedConnection(
            net_id=net_id,
            net_name=net_name,
            source_pad=_pad_key(src),
            target_pad=_pad_key(dst),
            cause=CAUSE_UNCLASSIFIED,
            note=note,
        )
        for src, dst in pairs
    ]


def diagnose_route_auto_net(
    *,
    pcb_path: str,
    net_id: int,
    net_name: str,
    pads: Sequence[Pad],
    result: RoutingResult,
    strategies_attempted: Sequence[RoutingStrategy],
    rules: DesignRules,
    budget_s: float,
    layer_stack: LayerStack | None = None,
    existing_segments: Sequence[Any] = (),
    existing_vias: Sequence[Any] = (),
    per_connection_s: float = DEFAULT_PER_CONNECTION_S,
    router: Autorouter | None = None,
) -> UnroutedDiagnosis | None:
    """Classify the connections one ``route-auto`` net left unrouted.

    Args:
        pcb_path: The board the net was routed from (``route-auto``'s source,
            which holds every earlier ``--nets`` pass's copper).
        net_id / net_name: The routed net.
        pads: The pads the orchestrator routed (its frame).
        result: The orchestrator's final result for the net.
        strategies_attempted: Every strategy the orchestrator ran for it.
        rules: The design rules route-auto routed with (its grid resolution
            is the diagnosis grid's).
        budget_s: Total wall-clock budget, board load included.  ``<= 0``
            skips the pass.
        layer_stack: The board's copper stack.
        existing_segments / existing_vias: This net's copper already on the
            board (same frame as ``pads``); pads it joins are one island.
        per_connection_s: Cap on one connection's solo search.
        router: A board already loaded with :func:`load_diagnosis_router`
            (tests); loaded here when ``None``.

    Returns:
        The diagnosis, or ``None`` when the net routed, the budget is off, or
        no pad island is left to join.
    """
    if result.success or budget_s is None or budget_s <= 0:
        return None
    t0 = time.monotonic()
    from .connectivity import pad_copper_components

    pads = [p for p in pads if not getattr(p, "steiner_point", False)]
    new_segments = list(result.segments) if result.partial else []
    new_vias = list(result.vias) if result.partial else []
    labels = pad_copper_components(
        [(p.x, p.y) for p in pads],
        new_segments,
        new_vias,
        list(existing_segments),
        list(existing_vias),
    )
    pairs = island_bridges(pads, labels)
    if not pairs:
        return None

    tried = [s for s in strategies_attempted if s is not None]
    if not any(s in FINE_GRID_STRATEGIES for s in tried):
        tried_names = ", ".join(dict.fromkeys(_STRATEGY_CLI_NAMES.get(s, s.name) for s in tried))
        note = (
            f"route-auto searched no fine routing grid for this net (strategies tried: "
            f"{tried_names or 'none'}; each plans on the coarse corridor graph), so there is "
            "no failed search to classify; re-run with --strategy hierarchical, or "
            "diagnose the board with `kct route --format json`"
        )
        return UnroutedDiagnosis(
            connections=_unclassified(net_id, net_name, pairs, note),
            elapsed_s=time.monotonic() - t0,
            budget_s=budget_s,
            per_connection_s=per_connection_s,
            routes_lifted=0,
        )

    if router is None:
        router = load_diagnosis_router(pcb_path, rules, layer_stack)
    board_pads = dict(getattr(router, "pads", {}) or {})
    connections: list[tuple[int, Pad, Pad]] = []
    unmatched: list[tuple[Any, Any]] = []
    for src, dst in pairs:
        bsrc, bdst = board_pads.get(_pad_key(src)), board_pads.get(_pad_key(dst))
        if bsrc is None or bdst is None:
            unmatched.append((src, dst))
            continue
        connections.append((int(bsrc.net or net_id), bsrc, bdst))

    names = {net_id: net_name, **{c[0]: net_name for c in connections}}
    remaining = max(0.0, budget_s - (time.monotonic() - t0))
    diagnosis = None
    if connections:
        diagnosis = diagnose_unrouted(
            router,
            {c[0] for c in connections},
            net_names=names,
            budget_s=remaining,
            per_connection_s=per_connection_s,
            connections=connections,
        )
    if diagnosis is None:
        diagnosis = UnroutedDiagnosis(
            connections=[],
            elapsed_s=0.0,
            budget_s=budget_s,
            per_connection_s=per_connection_s,
            routes_lifted=0,
        )
    if unmatched:
        diagnosis.connections.extend(
            _unclassified(
                net_id,
                net_name,
                unmatched,
                "an endpoint is not a pad of the board (e.g. a --region boundary "
                "stub), so it has no cell on the diagnosis grid",
            )
        )
    # Report the whole pass -- board load included -- against the budget.
    diagnosis.elapsed_s = time.monotonic() - t0
    diagnosis.budget_s = budget_s
    return diagnosis if diagnosis.connections else None


__all__ = [
    "FINE_GRID_STRATEGIES",
    "diagnose_route_auto_net",
    "island_bridges",
    "load_diagnosis_router",
]
