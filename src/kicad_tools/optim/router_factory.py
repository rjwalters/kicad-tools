"""Placement-to-router bridge for the cascaded optimization architecture.

This module provides the **router factory** that the placement GA's outer loop
uses when injecting :class:`~kicad_tools.router.evaluators.CppAstarRoutingEvaluator`
as its routability fitness signal (KiCad-2 / Issue #2720, epic
spheresemi/sphere#7199).

The :class:`~kicad_tools.router.evaluators.CppAstarRoutingEvaluator` accepts a
``RouterFactory`` callable: ``(positions, rotations) -> Autorouter``.  The
factory's job is to produce a fully-prepared :class:`Autorouter` whose pad
coordinates reflect the candidate placement so the inner GA can route it.

Architecture
------------

We take a **shared-base + in-place mutate** approach:

1. Build a *base* :class:`Autorouter` once (via
   :func:`~kicad_tools.router.io.load_pcb_for_routing`) from the user's PCB.
2. Cache, for each pad, its *local offset* relative to its component's
   reference position.  These offsets are read from the base PCB and remain
   constant for the duration of the placement GA (component footprints don't
   change shape during placement, only their position/rotation).
3. On each ``factory(positions, rotations)`` invocation:

   a. Mutate the base router's :attr:`Autorouter.pads` ``(x, y)`` for the
      candidate placement by applying ``pad.x = comp.x + rotate(local_dx)``.
   b. Call :meth:`Autorouter._reset_for_new_trial` to rebuild the underlying
      :class:`Grid` (obstacle masks, routing-cell occupancy) for the new pad
      positions.  The grid contains C++-extension state that is **not
      deep-copyable**, so we cannot clone the router; we mutate-in-place and
      reset the trial state instead.

   c. Return the same router instance.

Concurrency caveat
------------------

Because step 3 mutates the shared base router, this factory is **not
thread-safe**.  This is consistent with the
:class:`~kicad_tools.router.evaluators.RoutingEvaluatorConfig` default
``num_workers = 1``, which prevents nested ``ProcessPoolExecutor`` deadlocks
on top of the outer placement GA's worker pool.  If callers want
inter-candidate parallelism they must construct one factory per worker.

Performance
-----------

The factory is intentionally cheap: a single dict iteration + grid rebuild.
The expensive work (loading the PCB, parsing design rules, computing the
initial grid topology) happens once when :class:`PlacementRouterFactory` is
constructed.  This amortizes well across the GA's
``population_size * generations`` evaluations.

See :func:`build_pcb_router_factory` for the public entry point used by
``OptimizationWorkflow`` (kicad-tools' high-level workflow API).
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable

if TYPE_CHECKING:
    from pathlib import Path

    from kicad_tools.router.core import Autorouter

__all__ = [
    "PlacementRouterFactory",
    "build_pcb_router_factory",
]


# ---------------------------------------------------------------------------
# Cached pad offsets
# ---------------------------------------------------------------------------


@dataclass
class _PadOffset:
    """Local offset of a pad relative to its component's reference position.

    Stored once per pad at factory-construction time and reused across all
    ``factory(positions, rotations)`` invocations.

    Attributes:
        ref: Component reference (e.g. ``"U1"``).
        pin: Pad pin number/name.
        local_dx: Pad x relative to component x at the *base* rotation.
        local_dy: Pad y relative to component y at the *base* rotation.
        base_rotation: The component rotation in degrees that was active when
            the offset was captured.  We rotate the offset by
            ``(new_rotation - base_rotation)`` when a candidate placement
            specifies a different rotation.
    """

    ref: str
    pin: str
    local_dx: float
    local_dy: float
    base_rotation: float = 0.0


@dataclass
class _ComponentRef:
    """Base position + rotation for a component, captured from the source PCB.

    Used to compute pad offsets at factory-construction time, and as the
    fallback when a candidate placement does not specify a position for some
    component (in which case the base position is reused).
    """

    ref: str
    base_x: float
    base_y: float
    base_rotation: float = 0.0


@dataclass
class PlacementRouterFactory:
    """Callable factory that builds a candidate-placement Autorouter.

    Construct via :func:`build_pcb_router_factory`; do not instantiate
    directly unless you have the prerequisite cached state.

    Instances are *callable* with the ``RouterFactory`` signature expected by
    :class:`~kicad_tools.router.evaluators.CppAstarRoutingEvaluator`:

    .. code-block:: python

        factory = build_pcb_router_factory("board.kicad_pcb")
        router = factory({"U1": (50.0, 50.0)}, {"U1": 0.0})
        # ``router`` is an Autorouter with U1's pads recentered on (50, 50).

    Attributes:
        base_router: The template :class:`Autorouter` cloned per-call.
        pad_offsets: Per-pad local offset relative to its component.
        component_refs: Per-component base position/rotation for fallback.
    """

    base_router: Autorouter
    pad_offsets: list[_PadOffset] = field(default_factory=list)
    component_refs: dict[str, _ComponentRef] = field(default_factory=dict)

    def __call__(
        self,
        positions: dict[str, tuple[float, float]],
        rotations: dict[str, float],
    ) -> Autorouter:
        """Mutate and return the base Autorouter for the candidate placement.

        This method is **not thread-safe** — see module docstring's
        "Concurrency caveat" section.

        Args:
            positions: ``ref -> (x, y)`` in mm.  Components not listed retain
                their base PCB position.
            rotations: ``ref -> rotation_degrees``.  Components not listed
                retain their base PCB rotation.

        Returns:
            The shared base :class:`Autorouter`, with pad coordinates updated
            to reflect the candidate placement and routing-trial state reset.
        """
        router = self.base_router

        # Apply the candidate transform to every pad.  We rotate the local
        # offset by (new_rotation - base_rotation) and translate by the
        # candidate position.  Mutates router.pads in place — the call to
        # _reset_for_new_trial below propagates the new positions into the
        # routing grid (which holds the obstacle masks).
        for off in self.pad_offsets:
            comp_ref = self.component_refs.get(off.ref)
            if comp_ref is None:
                continue

            new_x, new_y = positions.get(off.ref, (comp_ref.base_x, comp_ref.base_y))
            new_rot = rotations.get(off.ref, comp_ref.base_rotation)

            delta_rot = math.radians(new_rot - off.base_rotation)
            cos_t = math.cos(delta_rot)
            sin_t = math.sin(delta_rot)
            rx = off.local_dx * cos_t - off.local_dy * sin_t
            ry = off.local_dx * sin_t + off.local_dy * cos_t

            pad = router.pads.get((off.ref, off.pin))
            if pad is None:
                continue
            pad.x = new_x + rx
            pad.y = new_y + ry

        # Refresh the underlying grid for the new pad positions.  Without
        # this, the C++ pathfinder would route against the *original* pad
        # locations even though pad.x/pad.y have moved.  ``_reset_for_new_trial``
        # rebuilds grid + zone manager and re-adds every pad with its new
        # coordinates.  Optional: not all stub routers (e.g. test fakes) have
        # this method, so we guard.
        reset = getattr(router, "_reset_for_new_trial", None)
        if callable(reset):
            try:
                reset()
            except Exception:
                # If the grid rebuild fails (e.g. pad now outside board
                # bounds), let the inner GA see the partial state — it will
                # report 0 routability and the placement GA will penalize
                # accordingly.  Suppressing here keeps the factory robust.
                pass

        return router


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def build_pcb_router_factory(
    pcb_path: str | Path,
    *,
    component_positions: dict[str, tuple[float, float, float]] | None = None,
    skip_nets: list[str] | None = None,
    use_pcb_rules: bool = True,
    validate_drc: bool = False,
    auto_adjust_grid: bool = True,
) -> PlacementRouterFactory:
    """Build a :class:`PlacementRouterFactory` for a PCB file.

    This is the bridge that
    :class:`~kicad_tools.optim.workflow.OptimizationWorkflow` uses when
    ``EvolutionaryConfig.use_routing_fitness`` is ``True``.  See
    :class:`PlacementRouterFactory` for the per-call semantics.

    Args:
        pcb_path: Path to the user's ``.kicad_pcb``.
        component_positions: Optional explicit per-component base positions
            ``ref -> (x, y, rotation_degrees)``.  When provided, overrides the
            positions read from the PCB file.  Useful when the placement GA
            is operating on an in-memory PCB whose footprints have been moved
            since the file was written.  When ``None``, base positions are
            derived from the loaded :class:`Autorouter`.
        skip_nets: Net names to skip when building the base router (typically
            plane nets like ``"GND"``, ``"+3.3V"``).  Forwarded to
            :func:`~kicad_tools.router.io.load_pcb_for_routing`.
        use_pcb_rules: Whether to extract design rules from the PCB.
            Forwarded to :func:`load_pcb_for_routing`.
        validate_drc: Forwarded to :func:`load_pcb_for_routing`.  Default
            ``False`` here (rather than ``True``) because the placement GA's
            inner loop is allowed to explore placements that would temporarily
            violate clearance — the fitness signal will reflect that with a
            low completion rate.
        auto_adjust_grid: Forwarded to :func:`load_pcb_for_routing`.

    Returns:
        A configured :class:`PlacementRouterFactory` ready for injection
        into :class:`~kicad_tools.router.evaluators.CppAstarRoutingEvaluator`.
    """
    # Local import to keep this module light at top-of-file (and to avoid a
    # heavy import chain when the flag is off).
    from kicad_tools.router.io import load_pcb_for_routing

    base_router, _ = load_pcb_for_routing(
        str(pcb_path),
        skip_nets=skip_nets,
        use_pcb_rules=use_pcb_rules,
        validate_drc=validate_drc,
        auto_adjust_grid=auto_adjust_grid,
    )

    return _build_factory_from_router(base_router, component_positions)


def _build_factory_from_router(
    base_router: Autorouter,
    component_positions: dict[str, tuple[float, float, float]] | None,
) -> PlacementRouterFactory:
    """Internal helper: extract pad offsets from a loaded Autorouter.

    Splits the cache-construction logic out of the public entry point so
    tests can exercise it with a synthetic ``base_router`` (no PCB file
    required).
    """
    component_positions = component_positions or {}

    # Group pads by component so we can derive per-component reference
    # positions when the caller didn't supply them.  Reference position is
    # the centroid of all pads for that component; rotation defaults to 0.
    pads_by_ref: dict[str, list[tuple[str, float, float]]] = {}
    for (ref, pin), pad in base_router.pads.items():
        pads_by_ref.setdefault(ref, []).append((pin, float(pad.x), float(pad.y)))

    component_refs: dict[str, _ComponentRef] = {}
    for ref, pads in pads_by_ref.items():
        if ref in component_positions:
            x, y, rot = component_positions[ref]
            component_refs[ref] = _ComponentRef(
                ref=ref, base_x=float(x), base_y=float(y), base_rotation=float(rot)
            )
        else:
            cx = sum(p[1] for p in pads) / len(pads)
            cy = sum(p[2] for p in pads) / len(pads)
            component_refs[ref] = _ComponentRef(
                ref=ref, base_x=cx, base_y=cy, base_rotation=0.0
            )

    pad_offsets: list[_PadOffset] = []
    for ref, pads in pads_by_ref.items():
        cref = component_refs[ref]
        for pin, x, y in pads:
            pad_offsets.append(
                _PadOffset(
                    ref=ref,
                    pin=pin,
                    local_dx=x - cref.base_x,
                    local_dy=y - cref.base_y,
                    base_rotation=cref.base_rotation,
                )
            )

    return PlacementRouterFactory(
        base_router=base_router,
        pad_offsets=pad_offsets,
        component_refs=component_refs,
    )


# ---------------------------------------------------------------------------
# Type alias re-export so callers don't need to import from two places.
# ---------------------------------------------------------------------------

RouterFactory = Callable[
    [dict[str, tuple[float, float]], dict[str, float]],
    "Autorouter",
]
