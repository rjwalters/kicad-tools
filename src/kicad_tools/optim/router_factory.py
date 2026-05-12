"""Placement-to-router bridge for the cascaded optimization architecture.

This module provides the **router factory** that the placement GA's outer loop
uses when injecting :class:`~kicad_tools.router.evaluators.CppAstarRoutingEvaluator`
as its routability fitness signal (KiCad-2 / Issue #2720, epic
spheresemi/sphere#7199).

The :class:`~kicad_tools.router.evaluators.CppAstarRoutingEvaluator` accepts a
``RouterFactory`` callable: ``(positions, rotations) -> Autorouter``.  The
factory's job is to materialize a fully-prepared :class:`Autorouter` whose pad
coordinates reflect the candidate placement so the inner GA can route it.

Architecture
------------

We take a **template + transform** approach:

1. Build a *base* :class:`Autorouter` once (via
   :func:`~kicad_tools.router.io.load_pcb_for_routing`) from the user's PCB.
2. Cache, for each pad, its *local offset* relative to its component's
   reference position.  These offsets are read from the base PCB and remain
   constant for the duration of the placement GA (component footprints don't
   change shape during placement, only their position/rotation).
3. On each ``factory(positions, rotations)`` invocation, deep-copy the base
   router and rewrite each pad's ``(x, y)`` coordinates by applying the
   candidate transform: ``pad.x = comp.x + rotate(local_offset_x)``.
4. Return the mutated copy.

Two design decisions worth noting:

* **Deep-copy** is used per call (rather than mutating-in-place) so that the
  outer GA can evaluate multiple candidates without inter-call interference.
  ``copy.deepcopy`` of the small Autorouter pad/net dicts is well under 1 ms
  on the test boards; it is dwarfed by the inner-routing cost.
* The grid (``Grid``) on the cloned router is **not** rebuilt — the inner GA's
  ``run_evolutionary`` calls ``route_all`` which uses the C++ pathfinder
  configured against the existing grid topology.  This is consistent with how
  ``PlacementFeedbackLoop`` reuses the same router across iterations.

Performance
-----------

The factory is intentionally cheap: a single deep-copy + dict iteration.  The
expensive work (loading the PCB, parsing design rules, building the grid)
happens once when :class:`PlacementRouterFactory` is constructed.  This
amortizes well across the GA's ``population_size * generations`` evaluations.

See :func:`build_pcb_router_factory` for the public entry point used by
``OptimizationWorkflow`` (kicad-tools' high-level workflow API).
"""

from __future__ import annotations

import copy
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
        """Build an Autorouter for the candidate placement.

        Args:
            positions: ``ref -> (x, y)`` in mm.  Components not listed retain
                their base PCB position.
            rotations: ``ref -> rotation_degrees``.  Components not listed
                retain their base PCB rotation.

        Returns:
            A deep-copied :class:`Autorouter` whose pad coordinates reflect
            the candidate placement.
        """
        # Deep-copy the base router so the original stays pristine.  This is
        # the single largest cost in the factory (~0.1-1 ms on test boards),
        # but is still trivially small compared to the inner GA's runtime.
        router = copy.deepcopy(self.base_router)

        # Apply the candidate transform to every pad.  We rotate the local
        # offset by (new_rotation - base_rotation) and translate by the
        # candidate position.
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
