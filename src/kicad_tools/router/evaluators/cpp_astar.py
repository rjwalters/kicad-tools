"""Concrete ``RoutingEvaluator`` backed by C++ A* + EvolutionaryRoutingOptimizer.

This is the production *inner loop* of the cascaded place-and-route epic
(spheresemi/sphere#7199, KiCad-1 / Issue #2719).  Until now the
:class:`kicad_tools.optim.evolutionary.RoutingEvaluator` Protocol only had a
``_MockRoutingEvaluator`` test double — the placement GA could *call* an
evaluator if one was injected, but no concrete implementation existed.
This module provides one.

Architecture
------------

The outer **placement GA** (``EvolutionaryPlacementOptimizer``, in
``kicad_tools.optim.evolutionary``) explores component positions and asks the
evaluator: *"if I placed components here, how routable is the result?"*

This evaluator answers that question concretely:

1. Build (or reuse) an :class:`~kicad_tools.router.core.Autorouter` whose pad
   coordinates reflect the candidate placement.
2. Run the **inner routing GA**
   (:func:`kicad_tools.router.algorithms.evolutionary.run_evolutionary`) which
   already exists and is GA-over-net-orderings + per-net A* weights, with
   per-trial fitness via the C++ A* pathfinder (Issues #2438, #2439).
3. Convert the resulting routes into a single **hybrid completion rate** in
   ``[0.0, 1.0]`` and return it.

The protocol contract is intentionally narrow: ``evaluate_routability`` takes
position/rotation dicts and returns a float.  All routing complexity lives
inside the evaluator.

Hybrid ``completion_rate`` semantic
-----------------------------------

Existing scoring sites (``router/tuning.py``,
``router/algorithms/evolutionary.py:_score_routes``,
``router/algorithms/monte_carlo.py``) all use **per-net binary completion**:
``routed_nets / total_nets`` where "routed" means *at least one segment was
emitted for that net id*.  That is fast but inflates the rate for nets where
only 2 of 5 pads are stitched together.

The Curator analysis for #2719 recommends a **hybrid** rule that is more
honest about partial connectivity:

* If a net is fully stitched (every required pad-pair is on a connected
  component when considering its routed segments + vias), it contributes
  ``1.0``.
* Otherwise it contributes ``connected_pad_pairs / required_pad_pairs``
  where ``required_pad_pairs = max(0, len(pads_on_net) - 1)`` (a tree with
  ``N`` leaves has ``N-1`` edges).

The two rules agree on the easy cases (0% and 100%) and diverge only when
partial routing exists.  Net 0 ("unconnected") is excluded from both numerator
and denominator.

This implementation reuses the connectivity logic already present in
``Route.segments`` + ``Route.vias`` — a simple union-find over the segment
endpoints suffices to determine which pads end up electrically connected.

Determinism
-----------

Following the precedent set by PR #2642 (``--seed`` for the route CLI), the
evaluator accepts a ``seed`` (via :class:`RoutingEvaluatorConfig`) and forwards
it into ``run_evolutionary(seed=...)``.  This is **required** for the outer
placement GA's fitness signal to be reproducible — without it the same
candidate placement would score differently across calls and the GA's
selection pressure would degrade to noise.

Parallelism
-----------

``num_workers`` **defaults to 1** because the outer placement GA already forks
worker processes (via ``placement_cpp``).  Nesting ``ProcessPoolExecutor``
inside a worker process tends to deadlock on macOS and is wasteful on Linux.
Callers who *know* their outer loop is sequential can opt into inner
parallelism by overriding the config.

Relationship to ``PlacementFeedbackLoop``
----------------------------------------

These are **complementary**, not subsuming:

* :class:`~kicad_tools.router.placement_feedback.PlacementFeedbackLoop` is
  **reactive** — it runs *after* placement is fixed, detects routing failures
  on the actual PCB, and *moves components* to resolve them.
* :class:`CppAstarRoutingEvaluator` is **evaluative** — it scores a *candidate*
  placement during the outer GA's exploration.  It does **not** move any
  components and operates on synthetic Autorouter state.

Recommended usage: run placement GA + ``CppAstarRoutingEvaluator`` to find a
good starting placement, then apply ``PlacementFeedbackLoop`` for final
reactive cleanup of any still-unrouted nets.  The evaluator must never invoke
``PlacementFeedbackLoop`` internally (that would be ~50× the cost of one
``route_all`` call and would violate the inner-loop speed budget).

Speed budget (AC #4)
--------------------

The epic's original speed bar is *"50-100 routing configs in <5s per outer
candidate"*.  In practice, each routing config is a full ``route_all`` over
the candidate placement, which costs roughly ``O(nets * pads_per_net)`` of C++
A* work — ~0.02-0.05s/net on a sparse 50-net board, scaling roughly linearly
with density.  With ``num_workers=1`` the inner GA's wall-clock cost is

    pop_size * generations * route_all_cost_per_eval

For the minimal fixtures (``boards/00-simple-led`` ~1 net,
``boards/01-voltage-divider`` ~3 nets) this is well under the budget at
``pop_size=5, generations=2`` (=10 configs).  For larger boards a stricter
``timeout_seconds`` should be set; the inner GA respects it (Issue #2467).

See ``tests/test_routing_evaluator_concrete.py`` for benchmarks and the
documented achievable budget on the included fixtures.

Example
-------

>>> # KiCad-2 wiring (placeholder — issue #2720 will land the real glue)
>>> from kicad_tools.router.evaluators import (
...     CppAstarRoutingEvaluator, RoutingEvaluatorConfig,
... )
>>> from kicad_tools.optim.evolutionary import EvolutionaryPlacementOptimizer
>>>
>>> def router_factory(positions, rotations):
...     # Caller-supplied: build an Autorouter for the candidate placement.
...     # In production this is the placement-to-router bridge from KiCad-2.
...     ...
>>>
>>> evaluator = CppAstarRoutingEvaluator(
...     router_factory=router_factory,
...     config=RoutingEvaluatorConfig(seed=42, timeout_seconds=5.0),
... )
>>> # placement_opt = EvolutionaryPlacementOptimizer(..., routing_evaluator=evaluator)
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from kicad_tools.router.core import Autorouter
    from kicad_tools.router.primitives import Route


__all__ = [
    "CppAstarRoutingEvaluator",
    "RoutingEvaluatorConfig",
    "compute_hybrid_completion_rate",
]


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


@dataclass
class RoutingEvaluatorConfig:
    """Tunable parameters for :class:`CppAstarRoutingEvaluator`.

    The defaults are chosen to fit the epic's <5s per-candidate speed budget
    on small-to-medium boards (≤ 50 nets, ≤ 100 pads) when called from inside
    a placement GA worker process.

    Attributes:
        pop_size: Inner-loop GA population size.  Total inner evaluations per
            outer candidate is ``pop_size * generations``.  Default 5 keeps
            wall-clock cost predictable for sub-5s budgets.
        generations: Inner-loop GA generations.  Default 2.
        seed: Random seed forwarded to the inner GA.  When set, the evaluator
            is deterministic: the same placement scored twice returns the
            same float.  Required for the outer placement GA's fitness
            signal to be reproducible (PR #2642 precedent).  Default
            ``None`` (non-deterministic, *not* recommended in production).
        timeout_seconds: Wall-clock budget for the inner GA.  If exceeded
            the GA returns the best partial result (Issue #2467).  Default
            5.0s matches Epic §1.
        num_workers: Inner GA worker count.  **Defaults to 1** to prevent
            nested ``ProcessPoolExecutor`` deadlock when the outer placement
            GA already forks workers.  Set to a higher value only when the
            outer loop is known to be sequential.
        w_completion: Weight on completion rate in the *internal* fitness
            score used by the inner GA.  Mirrors
            ``_score_routes(completion_rate * 1000)``.  Exposed for callers
            who want to bias the GA toward fewer-via or shorter-trace
            solutions; the *returned* completion rate is unaffected.
        w_vias: Weight on via count in the inner GA fitness.
        w_length: Weight on trace length in the inner GA fitness.
        verbose: When True, prints inner-GA generation progress.  Default
            False — outer-loop callers usually want a quiet inner loop.
    """

    pop_size: int = 5
    generations: int = 2
    seed: int | None = None
    timeout_seconds: float = 5.0
    num_workers: int | None = 1
    # Cost weights forwarded conceptually to the inner GA's _score_routes
    # (currently hard-coded there; exposed here for AC #3 / future plumbing).
    w_completion: float = 1000.0
    w_vias: float = 0.1
    w_length: float = 0.01
    verbose: bool = False


# ---------------------------------------------------------------------------
# Hybrid completion rate
# ---------------------------------------------------------------------------


def compute_hybrid_completion_rate(
    router: "Autorouter",
    routes: "list[Route]",
    *,
    coord_tol: float = 0.05,
) -> float:
    """Compute the hybrid completion rate for a routed Autorouter.

    Rule (per #2719 Curator recommendation):

    * Exclude net id 0 (the "unconnected" net) from numerator and denominator.
    * For each remaining net, count ``required_pad_pairs = max(0, P - 1)``
      where ``P`` is the number of pads on that net.  ``P < 2`` is treated as
      "no work needed" and contributes ``1.0`` (a single pad is trivially
      "connected to itself").
    * Run a union-find over the net's segments + vias to determine which
      pads end up in the same electrical component.  The number of
      ``connected_pad_pairs`` is ``P_largest_component - 1`` where
      ``P_largest_component`` is the count of *pads* in the largest
      union-find component.  This is the spanning-tree edge count of the
      pads that are actually electrically reachable.
    * If ``connected_pad_pairs >= required_pad_pairs`` the net contributes
      ``1.0`` (full stitch).  Otherwise the net contributes
      ``connected_pad_pairs / required_pad_pairs``.
    * Final rate is ``sum(contributions) / total_signal_nets``.

    Args:
        router: Autorouter providing ``pads`` and ``nets`` mappings.
        routes: Routes produced by ``run_evolutionary`` (or equivalent).
        coord_tol: Coordinate tolerance for endpoint snapping when building
            the union-find graph.  0.05 mm matches typical routing-grid
            resolution.

    Returns:
        Completion rate in ``[0.0, 1.0]``.
    """
    # ------ filter to signal nets ------
    signal_net_ids = [n for n in router.nets if n != 0]
    if not signal_net_ids:
        return 1.0

    # Group routes by net id (one net may have multiple Route objects in
    # some pipelines; defensively concatenate their segments + vias).
    routes_by_net: dict[int, list[Route]] = {}
    for r in routes:
        if r.net == 0:
            continue
        routes_by_net.setdefault(r.net, []).append(r)

    contributions: list[float] = []
    for net_id in signal_net_ids:
        pad_keys = router.nets.get(net_id, [])
        pad_objs = [router.pads.get(k) for k in pad_keys]
        pad_objs = [p for p in pad_objs if p is not None]
        n_pads = len(pad_objs)

        if n_pads < 2:
            # Single-pad / no-pad nets are trivially "done".
            contributions.append(1.0)
            continue

        required_pairs = n_pads - 1
        net_routes = routes_by_net.get(net_id, [])

        if not net_routes:
            # No segments emitted at all → 0 contribution.
            contributions.append(0.0)
            continue

        connected = _count_connected_pad_pairs(pad_objs, net_routes, coord_tol)
        # Cap at required_pairs in case overlapping routes inflate the count.
        connected = min(connected, required_pairs)

        if connected >= required_pairs:
            contributions.append(1.0)
        else:
            contributions.append(connected / required_pairs)

    return sum(contributions) / len(contributions)


def _count_connected_pad_pairs(
    pad_objs: list,
    routes: "list[Route]",
    coord_tol: float,
) -> int:
    """Count pad-pair connectivity for a single net via union-find.

    Endpoints are snapped to a grid of size ``coord_tol`` so that segments
    that meet within tolerance are treated as connected.  Pads are added to
    the graph at their (x, y) coordinates; vias are no-ops electrically but
    are recorded so that two segments on different layers that share a via
    location stay connected.

    Returns the count of "spanning tree edges" within the largest connected
    component that contains pads, i.e. ``max_pads_in_one_component - 1``.
    A return of ``0`` means no pads on this net share a component; a return
    of ``n_pads - 1`` means all pads are stitched.
    """
    parent: dict[tuple[int, int], tuple[int, int]] = {}

    def _key(x: float, y: float) -> tuple[int, int]:
        # Snap to a tolerance-sized grid.  Using integer keys keeps the dict
        # hash cheap and is robust to FP drift.
        return (round(x / coord_tol), round(y / coord_tol))

    def _find(k: tuple[int, int]) -> tuple[int, int]:
        # Path-compressed find.
        while parent.get(k, k) != k:
            parent[k] = parent.get(parent[k], parent[k])
            k = parent[k]
        return k

    def _union(a: tuple[int, int], b: tuple[int, int]) -> None:
        parent.setdefault(a, a)
        parent.setdefault(b, b)
        ra, rb = _find(a), _find(b)
        if ra != rb:
            parent[ra] = rb

    # Seed the graph with pad locations.
    pad_keys: list[tuple[int, int]] = []
    for p in pad_objs:
        k = _key(p.x, p.y)
        parent.setdefault(k, k)
        pad_keys.append(k)

    # Add segments.
    for r in routes:
        for s in r.segments:
            a = _key(s.x1, s.y1)
            b = _key(s.x2, s.y2)
            _union(a, b)
        # Vias connect their (x, y) position to itself across layers; since
        # our 2-D union-find ignores layer, a via at (x, y) already shares
        # a key with any segment endpoint at (x, y) — no extra union needed.

    # Count pads per component, return (largest_count - 1).
    comp_pad_counts: dict[tuple[int, int], int] = {}
    for k in pad_keys:
        root = _find(k)
        comp_pad_counts[root] = comp_pad_counts.get(root, 0) + 1

    if not comp_pad_counts:
        return 0
    largest = max(comp_pad_counts.values())
    return max(0, largest - 1)


# ---------------------------------------------------------------------------
# Concrete evaluator
# ---------------------------------------------------------------------------


# Type alias for the router factory: takes (positions, rotations) and returns
# a freshly-prepared Autorouter whose pad coordinates reflect the candidate
# placement.  The factory is responsible for:
#   * mapping component refs -> pads on the router
#   * applying the position/rotation transform to each pad
#   * setting up nets and design rules
#
# Two natural implementations exist:
#   1. "build fresh": clone a base Autorouter and rewrite pad coords.
#   2. "load PCB": call load_pcb_for_routing(...) then mutate pads.
# Either is fine — the evaluator does not care.
RouterFactory = Callable[
    [dict[str, tuple[float, float]], dict[str, float]],
    "Autorouter",
]


class CppAstarRoutingEvaluator:
    """Concrete :class:`~kicad_tools.optim.evolutionary.RoutingEvaluator`.

    Backed by the C++ A* pathfinder (via the existing
    :func:`kicad_tools.router.algorithms.evolutionary.run_evolutionary`
    orchestration).  See module docstring for design rationale.

    Conforms structurally to the
    :class:`kicad_tools.optim.evolutionary.RoutingEvaluator` Protocol; no
    nominal subclassing is required (and is deliberately avoided so the
    router layer does not depend on the optim layer).

    Args:
        router_factory: Callable that builds a fresh
            :class:`~kicad_tools.router.core.Autorouter` for a candidate
            placement.  See :data:`RouterFactory`.
        config: Optional :class:`RoutingEvaluatorConfig`.  Defaults to
            ``RoutingEvaluatorConfig()`` (seed unset; num_workers=1).

    Example
    -------

    >>> # Minimal factory that mutates pad coordinates on a base router:
    >>> def make_factory(base_router, pads_by_ref):
    ...     def factory(positions, rotations):
    ...         import copy
    ...         router = copy.deepcopy(base_router)
    ...         for ref, (x, y) in positions.items():
    ...             for pin in pads_by_ref.get(ref, []):
    ...                 pad = router.pads.get((ref, pin))
    ...                 if pad is not None:
    ...                     pad.x = x + pad.dx_local  # caller stores offset
    ...                     pad.y = y + pad.dy_local
    ...         return router
    ...     return factory
    """

    def __init__(
        self,
        router_factory: RouterFactory,
        config: RoutingEvaluatorConfig | None = None,
    ) -> None:
        self._router_factory = router_factory
        self._config = config or RoutingEvaluatorConfig()

        # Stats for callers who want to introspect.
        self.calls: int = 0
        self.last_completion_rate: float = 0.0
        self.last_elapsed_seconds: float = 0.0
        self.last_routes_count: int = 0

    @property
    def config(self) -> RoutingEvaluatorConfig:
        """Return the active configuration (read-only)."""
        return self._config

    def evaluate_routability(
        self,
        positions: dict[str, tuple[float, float]],
        rotations: dict[str, float],
    ) -> float:
        """Score a candidate placement by running the inner routing GA.

        Args:
            positions: Mapping ``ref -> (x, y)`` in mm.
            rotations: Mapping ``ref -> rotation_degrees``.

        Returns:
            Hybrid completion rate in ``[0.0, 1.0]``.  Returns ``0.0`` on
            any router-side exception (the outer placement GA's fitness
            path also has a fall-back, so this is belt-and-suspenders).
        """
        self.calls += 1
        start = time.monotonic()
        try:
            router = self._router_factory(dict(positions), dict(rotations))
        except Exception:
            # Factory failure → unroutable placement.  Don't propagate;
            # the placement GA's fitness path expects [0, 1] floats.
            self.last_completion_rate = 0.0
            self.last_elapsed_seconds = time.monotonic() - start
            self.last_routes_count = 0
            return 0.0

        # Defensive: if the factory returned a router with no signal nets,
        # there is nothing to route — treat as fully routable.
        signal_nets = [n for n in getattr(router, "nets", {}) if n != 0]
        if not signal_nets:
            self.last_completion_rate = 1.0
            self.last_elapsed_seconds = time.monotonic() - start
            self.last_routes_count = 0
            return 1.0

        cfg = self._config
        try:
            routes = self._run_inner_ga(router, cfg)
        except Exception:
            self.last_completion_rate = 0.0
            self.last_elapsed_seconds = time.monotonic() - start
            self.last_routes_count = 0
            return 0.0

        rate = compute_hybrid_completion_rate(router, routes)
        # Clamp defensively in case of numerical drift.
        if rate < 0.0:
            rate = 0.0
        elif rate > 1.0:
            rate = 1.0

        self.last_completion_rate = rate
        self.last_elapsed_seconds = time.monotonic() - start
        self.last_routes_count = len(routes)
        return rate

    # -----------------------------------------------------------------
    # Internals
    # -----------------------------------------------------------------

    def _run_inner_ga(
        self,
        router: "Autorouter",
        cfg: RoutingEvaluatorConfig,
    ) -> "list[Route]":
        """Invoke the inner evolutionary routing GA on a prepared router.

        Imports are local to avoid a hard dependency at module-load time
        (keeps unit tests light when ``run_evolutionary`` is monkeypatched).
        """
        from kicad_tools.router.algorithms.evolutionary import run_evolutionary

        return run_evolutionary(
            autorouter=router,
            pop_size=cfg.pop_size,
            generations=cfg.generations,
            seed=cfg.seed,
            verbose=cfg.verbose,
            num_workers=cfg.num_workers,
            timeout=cfg.timeout_seconds,
        )
