"""Router net-class clearances as per-net foreign-pair minima (Issue #6427).

KiCad resolves the clearance between two foreign nets as the larger of their
two netclass clearances.  Since #6390 the router declares its own effective
net classes (``Power`` / ``Audio`` / ... from ``net_class_map``) in the routed
``.kicad_pro`` / ``.kicad_dru`` sidecars, so both DRC engines hold every pair
to ``max(class(a), class(b))``.

The router, however, only applied a class clearance to the *routing* net's own
copper.  Foreign copper was checked at the board base, so an ``Audio`` (0.15)
trace could legally end up 0.165 mm from a ``Power`` (0.2) via -- the trace
optimizer's collinear merge did exactly that on board 02.

The #6243 authored-floor mechanism already enforces ``max(base, floor[a],
floor[b])`` everywhere it matters: the search's exact step and via gates, the
native ``Grid3D`` authored index (stored segments and vias included), the
optimizer narrow phase (``_authored_path_clear``) and the end-of-route census.
This module feeds the router's own class clearances into that same
``DesignRules.net_clearance_floors`` map, so router-declared classes and
designer-authored ones are judged by one predicate.

Only classes stricter than the router's base are kept -- a class at or below
the base adds nothing to any pair -- which is the same filter the authored
loader applies (``io.load_pcb_for_routing``) and the same "stricter than
``Default``" rule the #6390 sidecar writer uses to decide which classes to
declare.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import replace
from typing import Any

from .rules import DesignRules

_EPS = 1e-9


def clearance_base(rules: DesignRules) -> float:
    """The router's own foreign-pair base: a floor at or below it is a no-op."""
    return min(rules.trace_clearance, rules.via_clearance)


def class_clearances_by_name(
    net_class_map: Mapping[str, Any] | None, base: float
) -> dict[str, float]:
    """``{net_name: class clearance}`` for every class stricter than ``base``.

    ``net_class_map`` is the router's name-keyed ``{net: NetClassRouting}``
    map.  Entries without a positive finite ``clearance`` are ignored.
    """
    result: dict[str, float] = {}
    for name, net_class in (net_class_map or {}).items():
        value = getattr(net_class, "clearance", None)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            continue
        value = float(value)
        if value != value or value == float("inf"):  # NaN / inf
            continue
        if value > base + _EPS:
            result[str(name)] = value
    return result


def merge_named_floors(
    rules: DesignRules,
    by_name: Mapping[str, float],
    net_map: Mapping[str, int],
    components: Iterable[Mapping[str, Any]] = (),
) -> DesignRules:
    """Return ``rules`` with ``by_name`` minima merged into its net floors.

    Each name present on the board (``net_map``) raises its net id's floor to
    ``max(existing, value)``.  A pad whose net was neutralised to 0 (skipped,
    placement-excluded or unsupported nets) keeps the minimum on the pad
    itself as ``authored_clearance``, the same way the #6243 loader carries a
    neutralised pad's authored class.  The caller's ``rules`` object is never
    mutated: a fresh copy is returned when anything changed.
    """
    if not by_name:
        return rules
    floors = dict(rules.net_clearance_floors)
    for name, value in by_name.items():
        net_id = net_map.get(name, 0)
        if net_id:
            floors[net_id] = max(floors.get(net_id, 0.0), value)
    for comp in components:
        for pad in comp["pads"]:
            pad_floor = by_name.get(pad.get("net_name", ""))
            if pad_floor is not None and pad["net"] == 0:
                pad["authored_clearance"] = max(
                    float(pad.get("authored_clearance") or 0.0), pad_floor
                )
    if floors == rules.net_clearance_floors:
        return rules
    return replace(rules, net_clearance_floors=floors)


def remember_authored_floors(router: Any, floors: Mapping[int, float]) -> None:
    """Record the designer-authored floors alone on ``router``.

    :func:`refresh_router_class_floors` rebuilds ``authored + class`` from this
    snapshot, so a class lowered by a later ``--net-class-map`` drops its floor
    instead of keeping the loader's value forever.
    """
    router._authored_net_clearance_floors = dict(floors)


def refresh_router_class_floors(router: Any) -> bool:
    """Re-derive ``router.rules.net_clearance_floors`` after a class-map edit.

    The loader installs class floors from the map it built, but the CLI
    later merges a user ``--net-class-map`` and ``--analog-nets`` classes into
    ``router.net_class_map``.  Those classes are declared in the routed
    sidecars too, so they must bind foreign copper the same way.

    Recomputes ``authored floors + current class floors`` (the authored part
    is the snapshot the loader stored on ``router._authored_net_clearance_floors``)
    and installs it as a NEW dict on the router's shared ``DesignRules``
    object, so every holder -- grid, pathfinder, optimizer -- sees it and the
    identity-keyed authored index cache is invalidated.  The native grid
    re-syncs on its next ``sync_authored_floors`` call (keyed on contents).

    Issue #8577: names resolve through the loader's full ``board_net_ids``
    map, not only ``router.net_names``.  ``net_names`` is filled from routable
    pads, so a net the run SKIPS -- every net whose copper ``--preserve-existing``
    keeps -- is absent from it, and its kept copper got no class floor: new
    Signal copper was held to its own clearance against a preserved Analog
    track.  Those nets' pads are neutralised to net 0, so their item-level
    floor (``Pad.authored_clearance``) is re-derived here as well, as
    ``max(designer floor, current class floor)`` -- the value the loader
    gives a neutralised pad.

    Issue #8769: the kept copper of those nets (``placement_neutral_routes``,
    rekeyed to net 0 by the loader) carries the same item-level floor on
    ``Route.authored_clearance``; it is re-derived the same way.

    Returns ``True`` when the floors changed.
    """
    rules = getattr(router, "rules", None)
    if not isinstance(rules, DesignRules):
        return False
    net_names = getattr(router, "net_names", None) or {}
    ids_by_name = {
        str(name): int(net_id)
        for name, net_id in (getattr(router, "board_net_ids", None) or {}).items()
        if net_id
    }
    ids_by_name.update({name: net_id for net_id, name in net_names.items() if net_id})
    authored = getattr(router, "_authored_net_clearance_floors", None)
    floors: dict[int, float] = dict(
        authored if authored is not None else rules.net_clearance_floors
    )
    class_by_name = class_clearances_by_name(router.net_class_map, clearance_base(rules))
    for name, value in class_by_name.items():
        net_id = ids_by_name.get(name)
        if net_id:
            floors[net_id] = max(floors.get(net_id, 0.0), value)
    pads_changed = _refresh_neutral_pad_floors(router, rules, class_by_name)
    pads_changed = _refresh_neutral_route_floors(router, rules, class_by_name) or pads_changed
    if floors == rules.net_clearance_floors:
        return pads_changed
    rules.net_clearance_floors = floors
    grid_rules = getattr(getattr(router, "grid", None), "rules", None)
    if isinstance(grid_rules, DesignRules) and grid_rules is not rules:
        grid_rules.net_clearance_floors = dict(floors)
    return True


def _refresh_neutral_pad_floors(
    router: Any, rules: DesignRules, class_by_name: Mapping[str, float]
) -> bool:
    """Re-derive the item floor of every neutralised (net 0) pad (Issue #8577).

    A skipped net's pads are obstacles under net 0, so no net-keyed floor can
    reach them; the loader carries their class on the pad itself
    (:func:`merge_named_floors`).  A class map merged after loading must do
    the same.  The value is ``max(designer floor, class floor)`` by the pad's
    retained net name.  Without a designer snapshot (rules built outside the
    loader) a pad floor is only ever raised, never lowered.

    Bumps ``grid._authored_pad_floor_version`` and drops the Python index
    cache when a pad changed, so both the Python authored index and the native
    ``sync_authored_floors`` key pick the new floors up.

    Returns ``True`` when any pad floor changed.
    """
    grid = getattr(router, "grid", None)
    pads = list(getattr(grid, "_pads", None) or ())
    seen = {id(p) for p in pads}
    for pad in (getattr(router, "pads", None) or {}).values():
        if id(pad) not in seen:
            seen.add(id(pad))
            pads.append(pad)
    # ``designer_clearance_floors is None``: rules built outside the loader,
    # where every floor counts as designer-authored (#6449) -- raise only.
    designer = (
        rules.designer_clearance_floors_by_name
        if rules.designer_clearance_floors is not None
        else None
    )
    changed = False
    for pad in pads:
        if getattr(pad, "net", 0) != 0:
            continue
        name = getattr(pad, "net_name", "") or ""
        if not name:
            continue
        current = float(getattr(pad, "authored_clearance", 0.0) or 0.0)
        target = class_by_name.get(name, 0.0)
        if designer is not None:
            target = max(target, float(designer.get(name, 0.0) or 0.0))
        else:
            target = max(target, current)
        if abs(target - current) > _EPS:
            pad.authored_clearance = target
            changed = True
    if changed and grid is not None:
        if any(float(getattr(p, "authored_clearance", 0.0) or 0.0) > 0.0 for p in pads):
            grid._authored_item_floors = True
        grid._authored_index_cache = None
        grid._authored_pad_floor_version = getattr(grid, "_authored_pad_floor_version", 0) + 1
    return changed


def _refresh_neutral_route_floors(
    router: Any, rules: DesignRules, class_by_name: Mapping[str, float]
) -> bool:
    """Re-derive the item floor of every neutralised (net 0) kept route (#8769).

    The loader rekeys placement-preserved copper to net 0 and blanks its
    ``net_name`` (:attr:`Router.placement_neutral_routes`), so the name is read
    from its twin in ``placement_preserved_routes`` -- both are appended in the
    same loader branch, hence index-paired.  The value is ``max(designer floor,
    class floor)``, as for pads.

    Bumps ``grid._authored_route_floor_version`` and drops the Python index
    cache when a route changed; the native stored-route sync keys on the
    version, because it is otherwise incremental by route count.

    Returns ``True`` when any route floor changed.
    """
    neutral = tuple(getattr(router, "placement_neutral_routes", None) or ())
    preserved = tuple(getattr(router, "placement_preserved_routes", None) or ())
    if not neutral or len(neutral) != len(preserved):
        return False
    designer = (
        rules.designer_clearance_floors_by_name
        if rules.designer_clearance_floors is not None
        else None
    )
    changed = False
    for route, twin in zip(neutral, preserved, strict=True):
        name = getattr(twin, "net_name", "") or ""
        if not name:
            continue
        current = float(getattr(route, "authored_clearance", 0.0) or 0.0)
        target = class_by_name.get(name, 0.0)
        if designer is not None:
            target = max(target, float(designer.get(name, 0.0) or 0.0))
        else:
            target = max(target, current)
        if abs(target - current) > _EPS:
            route.authored_clearance = target
            changed = True
    grid = getattr(router, "grid", None)
    if changed and grid is not None:
        if any(float(getattr(r, "authored_clearance", 0.0) or 0.0) > 0.0 for r in neutral):
            grid._authored_item_floors = True
        grid._authored_index_cache = None
        grid._authored_route_floor_version = getattr(grid, "_authored_route_floor_version", 0) + 1
    return changed
