"""Bounded relief search for an over-subscribed routing plan.

Issue #5521 (Epic #5510, Phase 1c).  The plan produced by
:mod:`kicad_tools.router.routing_plan` already says *where* a board is
over-subscribed (which tile boundary, by how much, for which nets).  This
module answers the next question -- *what would fix it* -- by **measuring**
candidates rather than guessing at them: each candidate is re-planned with
the identical global pass and keeps only if it lowers the board's total
overflow.

Three candidate kinds, per overflowed edge:

``move_component``
    Shift one adjacent component by a single :data:`RELIEF_STEP_MM` step in
    one of the four axis directions and re-plan.  The shift is applied to a
    **copy** of the pad dict (``dataclasses.replace``), never to the router,
    the grid or the PCB -- so "placement is restored exactly" is a property
    of the design, not of a restore path that could be skipped on an error.
``add_signal_layer``
    Re-plan with one PLANE index treated as a signal layer.  This is advice
    about *capacity* ("the board needs another routing layer's worth of
    room here"), never a claim that the plane can be deleted.
``swap_pins``
    A deferred stub only (``{"kind": "swap_pins", "deferred": "#5511"}``).
    Pin swapping is Issue #5511's; nothing here evaluates or implements it.

**The search is bounded on two axes** because every candidate costs a full
global pass (graph build + up to 15 negotiated iterations), i.e. about as
much as the plan stage itself:

1. A deterministic candidate cap -- :data:`RELIEF_MAX_EDGES` edges x
   :data:`RELIEF_MAX_REFS` refs x 4 unit steps, plus at most one
   ``add_signal_layer`` candidate per plane index.
2. A wall-clock budget (:data:`RELIEF_BUDGET_S`), checked **before** each
   re-plan.  Exhausting it stops the search and records
   ``relief_meta["truncated"] = True`` rather than silently reporting a
   partial search as a complete one.
"""

from __future__ import annotations

import time
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from .routing_plan import RoutingPlan, index_pad_refs, rank_refs_in_region, run_global_pass

if TYPE_CHECKING:
    from .primitives import Pad
    from .region_graph import RegionGraph

#: Overflowed edges examined, worst first.
RELIEF_MAX_EDGES = 3

#: Adjacent component refs examined per edge, highest-ranked first.
RELIEF_MAX_REFS = 3

#: Single translation step in mm.  Deliberately the same constant the
#: placement-feedback translation deltas use
#: (``placement_delta.MAX_TRANSLATE_MM``), so relief advice a human acts on
#: is expressed in the same unit the automated placement loop would move.
RELIEF_STEP_MM = 2.0

#: Wall-clock budget for the whole search, in seconds.  A module constant,
#: not a CLI flag: Issue #5521's scope guard adds exactly one flag
#: (``--plan-gate``) and this must not become a second tuning knob.
RELIEF_BUDGET_S = 5.0

#: The four unit steps, in evaluation order: +x, -x, +y, -y.
_STEPS: tuple[tuple[float, float], ...] = (
    (RELIEF_STEP_MM, 0.0),
    (-RELIEF_STEP_MM, 0.0),
    (0.0, RELIEF_STEP_MM),
    (0.0, -RELIEF_STEP_MM),
)


def shift_pads(pads: dict[Any, Pad], ref: str, dx: float, dy: float) -> dict[Any, Pad]:
    """A copy of *pads* with every pad of *ref* translated by ``(dx, dy)``.

    ``dataclasses.replace`` produces new :class:`~kicad_tools.router.
    primitives.Pad` objects, so the originals -- and therefore the router's
    own placement -- are untouched by construction.
    """
    out: dict[Any, Pad] = {}
    for key, pad in pads.items():
        if pad.ref == ref:
            out[key] = replace(pad, x=pad.x + dx, y=pad.y + dy)
        else:
            out[key] = pad
    return out


def _within_board(pads: dict[Any, Pad], ref: str, dx: float, dy: float, grid: Any) -> bool:
    """True when every pad of *ref* stays inside the board after the step."""
    min_x = float(getattr(grid, "origin_x", 0.0))
    min_y = float(getattr(grid, "origin_y", 0.0))
    max_x = min_x + float(getattr(grid, "width", 0.0))
    max_y = min_y + float(getattr(grid, "height", 0.0))
    for pad in pads.values():
        if pad.ref != ref:
            continue
        half_w = float(getattr(pad, "width", 0.0)) / 2.0
        half_h = float(getattr(pad, "height", 0.0)) / 2.0
        x = pad.x + dx
        y = pad.y + dy
        if x - half_w < min_x or x + half_w > max_x:
            return False
        if y - half_h < min_y or y + half_h > max_y:
            return False
    return True


def _candidate_refs(
    plan: RoutingPlan,
    graph: RegionGraph,
    pads: dict[Any, Pad],
    edge_key: tuple[int, int],
    edge_nets: list[int],
) -> list[str]:
    """Adjacent refs for one edge, highest-ranked first, capped.

    Prefers the ranking already serialized on the edge entry (so the report
    text and the search agree), and re-derives it only when the entry has
    none (a plan built without pads).
    """
    entry = next((e for e in plan.edges if (e.a, e.b) == edge_key), None)
    ranked: list[str] = []
    if entry is not None and (entry.refs_a or entry.refs_b):
        for a_ref, b_ref in zip(entry.refs_a, entry.refs_b, strict=False):
            ranked.extend([a_ref, b_ref])
        ranked.extend(entry.refs_a[len(entry.refs_b) :])
        ranked.extend(entry.refs_b[len(entry.refs_a) :])
    else:  # pragma: no cover - defensive; build_plan always passes pads
        index = index_pad_refs(graph, pads.values())
        for region_id in edge_key:
            ranked.extend(rank_refs_in_region(index.get(region_id, {}), edge_nets))

    seen: set[str] = set()
    out: list[str] = []
    for ref in ranked:
        if ref in seen:
            continue
        seen.add(ref)
        out.append(ref)
        if len(out) >= RELIEF_MAX_REFS:
            break
    return out


def compute_relief(
    plan: RoutingPlan,
    graph: RegionGraph,
    router: Any,
    *,
    net_order: list[int],
    corridor_width_factor: float = 2.0,
    budget_s: float = RELIEF_BUDGET_S,
    max_edges: int = RELIEF_MAX_EDGES,
) -> None:
    """Populate ``plan.relief`` / ``plan.relief_meta`` in place.

    Args:
        plan: The freshly built plan.  Must carry an ``overflow_report``
            with non-zero ``total_overflow`` -- a feasible board is not a
            candidate for relief and the caller should not enter here.
        graph: The ``RegionGraph`` the plan was computed on (read-only).
        router: The ``Autorouter`` / ``TwoPhaseRouter`` the plan came from.
            Never mutated.
        net_order: The exact net order the plan's global pass used, so a
            candidate differs from the baseline in the moved component
            alone.
        corridor_width_factor: Forwarded to the re-planned pass.
        budget_s: Wall-clock budget for the whole search.
        max_edges: Overflowed edges examined, worst first.
    """
    report = plan.overflow_report
    baseline = report.total_overflow if report is not None else 0
    started = time.time()
    evaluated = 0
    truncated = False
    pads: dict[Any, Pad] = dict(getattr(router, "pads", {}) or {})
    grid = getattr(router, "grid", None)
    plane_layers = list(plan.layers.get("plane", []))

    def _budget_left() -> bool:
        return (time.time() - started) < budget_s

    def _overflow_for(
        candidate_pads: dict[Any, Pad] | None = None,
        extra_signal_layers: tuple[int, ...] = (),
    ) -> int:
        nonlocal evaluated
        evaluated += 1
        result = run_global_pass(
            router,
            net_order=net_order,
            corridor_width_factor=corridor_width_factor,
            pads=candidate_pads,
            extra_signal_layers=extra_signal_layers,
        )
        return result.region_graph.get_total_overflow()

    edges = sorted(
        (e for e in plan.edges if e.overflow > 0),
        key=lambda e: (-e.overflow, e.a, e.b),
    )[:max_edges]

    # (b) add_signal_layer is a BOARD-level lever, not a per-edge one: the
    # re-planned pass gives one total-overflow number per plane index, so
    # evaluate each plane once up front and attach the (identical) result
    # to each edge block rather than paying for it per edge.
    plane_overflow: dict[int, int] = {}
    for plane in plane_layers:
        if not _budget_left():
            truncated = True
            break
        plane_overflow[plane] = _overflow_for(extra_signal_layers=(plane,))

    relief: list[dict[str, Any]] = []
    for edge in edges:
        edge_key = (edge.a, edge.b)

        # (a) move_component.
        found: list[dict[str, Any]] = []
        for ref in _candidate_refs(plan, graph, pads, edge_key, edge.nets):
            for dx, dy in _STEPS:
                if not _budget_left():
                    truncated = True
                    break
                if grid is not None and not _within_board(pads, ref, dx, dy, grid):
                    continue
                expected = _overflow_for(candidate_pads=shift_pads(pads, ref, dx, dy))
                if expected < baseline:
                    found.append(
                        {
                            "edge": [edge.a, edge.b],
                            "kind": "move_component",
                            "ref": ref,
                            "dx": dx,
                            "dy": dy,
                            "expected_overflow": expected,
                        }
                    )
            if truncated:
                break
        found.sort(key=lambda entry: int(entry["expected_overflow"]))
        relief.extend(found)

        # (b) add_signal_layer -- only when the stack actually has planes.
        layer_found: list[dict[str, Any]] = [
            {
                "edge": [edge.a, edge.b],
                "kind": "add_signal_layer",
                "layer_index": plane,
                "expected_overflow": expected,
            }
            for plane, expected in sorted(plane_overflow.items())
            if expected < baseline
        ]
        layer_found.sort(key=lambda entry: int(entry["expected_overflow"]))
        relief.extend(layer_found)

        # (c) swap_pins: a deferred stub, never evaluated (#5511).  Listed
        # last so a reader sees the measured candidates first.
        relief.append({"edge": [edge.a, edge.b], "kind": "swap_pins", "deferred": "#5511"})

        if truncated:
            break

    plan.relief = relief
    plan.relief_meta = {
        "candidates_evaluated": evaluated,
        "elapsed_s": time.time() - started,
        "truncated": truncated,
        "baseline_overflow": baseline,
        "budget_s": budget_s,
    }
