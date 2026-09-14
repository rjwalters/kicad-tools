"""Hard layer-intent audit over the copper a run actually ships (#4979).

``NetClassRouting.avoid_layers`` is normally a SOFT cost bias.  Under
``kct route --strict-layers`` (or when the class declares a
``target_ampacity``) it becomes a HARD constraint --
:meth:`~kicad_tools.router.rules.NetClassRouting.hard_avoided_layer_indices`
is the single source of truth for that promotion, and both grid backends
(``router/pathfinder.py``, ``router/cpp_backend.py``) and -- since #4979 --
the lattice search (``router/lattice/pathfinder.py``) remove those layers
from the net's routable set during search.

This module is the independent **post-route** gate on the same promise:
given the copper the output board carries, it reports every segment or via
that sits on a layer its net declared off-limits.  It exists because a
search-time constraint is only as good as the engine that honours it --
issue #4979 was exactly the case where one engine (lattice, the
``--complete`` default) silently ignored the constraint and shipped 29
2.6 mm ``/PGND`` segments plus 8 thin ones onto a forbidden ``In2.Cu``
reference plane, with no hard failure reported anywhere in the run.

Two properties the CLI gate depends on:

- **Inherited vs newly created.**  Every violation records whether it came
  from copper this run routed (``inherited=False``) or from copper the run
  inherited from its input board and re-emitted verbatim under
  ``--preserve-existing`` / ``--complete`` (``inherited=True``).  Only NEW
  violations indict the router; inherited ones indict the input board (and
  are still reported, since the written board is no safer for having
  inherited its forbidden copper -- the same reasoning the #4699 pairwise
  gate applies).
- **Via semantics.**  A via is judged on the layers it *terminates* on
  (``Via.layers``), not on every layer its barrel physically crosses.  A
  standard through via on a 4-layer board spans F.Cu->B.Cu, so the
  barrel-crossing reading would make it impossible for an F/B-only net to
  change layers at all; keeping an inner plane clear of a via *barrel* is
  an antipad/clearance concern that DRC owns, not a layer-intent one.
  A blind/buried via that lands ON a forbidden layer IS a violation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from .layers import LayerStack
from .primitives import Route

__all__ = [
    "LayerIntentViolation",
    "find_layer_intent_violations",
    "forbidden_layer_names",
]


@dataclass(frozen=True)
class LayerIntentViolation:
    """One piece of copper on a layer its net declared hard-off-limits."""

    net_name: str
    """Board net name the copper belongs to (e.g. ``"/PGND"``)."""

    layer: str
    """KiCad layer name the copper sits on (e.g. ``"In2.Cu"``)."""

    kind: str
    """``"segment"`` or ``"via"``."""

    x: float
    y: float

    inherited: bool
    """True when this copper was re-emitted from the input board, not routed
    by this run (``--preserve-existing`` / ``--complete``)."""

    width: float = 0.0
    """Segment width (mm), or via diameter for ``kind == "via"``."""

    def describe(self) -> str:
        """One-line human-readable form used by the CLI gate's report."""
        origin = "inherited" if self.inherited else "new"
        return (
            f"[{origin}] {self.net_name} {self.kind} on {self.layer} "
            f"(w={self.width:.3f}mm) at ({self.x:.3f}, {self.y:.3f})"
        )


def forbidden_layer_names(
    net_class_map: Mapping[str, object] | None,
    *,
    strict_layers: bool,
    layer_stack: LayerStack | None,
) -> dict[str, frozenset[str]]:
    """Resolve each net's HARD-avoided layers to KiCad layer names.

    Keys are the ``net_class_map`` keys (board net names); values are the
    KiCad names (``"In1.Cu"``, ...) of the grid-layer indices
    :meth:`NetClassRouting.hard_avoided_layer_indices` hardens for that
    class.  Nets with no hard constraint are omitted entirely, so an empty
    result means "this run has no layer intent to enforce" and every caller
    can short-circuit to a strict no-op.

    Indices outside the board's stack are dropped: an ``In2.Cu`` exclusion
    on a 2-layer board cannot be violated because the layer does not exist
    (the vacuous-exclusion reporting question is issue #4685's, not this
    gate's).  A class whose ``avoid_layers`` cannot be resolved at all is
    skipped rather than raising -- the search path raises on the same class
    first (see ``hard_avoided_layer_indices``), so reaching this audit with
    an unresolvable entry means the class was never routed against.
    """
    if not net_class_map:
        return {}
    stack = layer_stack or LayerStack.two_layer()
    out: dict[str, frozenset[str]] = {}
    for name, net_class in net_class_map.items():
        resolver = getattr(net_class, "hard_avoided_layer_indices", None)
        if resolver is None:
            continue
        try:
            indices = resolver(strict_layers)
        except Exception:
            continue
        names = {
            layer_def.name
            for layer_def in (stack.get_layer(int(idx)) for idx in indices)
            if layer_def is not None
        }
        if names:
            out[name] = frozenset(names)
    return out


def find_layer_intent_violations(
    routed: Sequence[Route],
    preserved: Sequence[Route] = (),
    *,
    net_class_map: Mapping[str, object] | None,
    strict_layers: bool,
    layer_stack: LayerStack | None = None,
    id_to_name: Mapping[int, str] | None = None,
) -> list[LayerIntentViolation]:
    """Report every segment/via on a layer its net declared hard-off-limits.

    Args:
        routed: Copper this run routed (``Autorouter.routes``).  Findings
            here are ``inherited=False`` -- the router created them.
        preserved: Copper re-emitted verbatim from the input board under
            ``--preserve-existing`` / ``--complete``.  Findings here are
            ``inherited=True``.
        net_class_map: ``{net_name: NetClassRouting}`` as resolved by the
            router (classifier output merged with ``--net-class-map``).
        strict_layers: ``DesignRules.strict_layers`` (the
            ``--strict-layers`` opt-in).  ``avoid_layers`` on a class that
            also declares ``target_ampacity`` is hard regardless.
        layer_stack: The board's copper stack, used to turn hard-avoided
            grid indices into KiCad layer names.  Defaults to a 2-layer
            stack (matching ``Autorouter``'s own fallback).
        id_to_name: Optional ``{net_id: net_name}`` map, preferred over a
            route's own ``net_name`` so a preserved route whose stored id
            disagrees with this session's numbering is still audited under
            the right net's class (same preference order as
            ``find_pairwise_violations``).

    Returns:
        Deterministically ordered violations, NEW ones first (they indict
        this run), then by net name / layer / kind / position.  Empty when
        no net carries a hard layer constraint -- the strict no-op every
        pre-#4979 run takes.
    """
    forbidden = forbidden_layer_names(
        net_class_map, strict_layers=strict_layers, layer_stack=layer_stack
    )
    if not forbidden:
        return []

    violations: list[LayerIntentViolation] = []
    for routes, inherited in ((routed, False), (preserved, True)):
        for route in routes or ():
            name = None
            if id_to_name is not None:
                name = id_to_name.get(route.net)
            name = name or getattr(route, "net_name", "") or ""
            blocked = forbidden.get(name)
            if not blocked:
                continue
            for seg in route.segments:
                layer_name = _layer_name(seg.layer)
                if layer_name in blocked:
                    violations.append(
                        LayerIntentViolation(
                            net_name=name,
                            layer=layer_name,
                            kind="segment",
                            x=seg.x1,
                            y=seg.y1,
                            inherited=inherited,
                            width=seg.width,
                        )
                    )
            for via in route.vias:
                # Endpoint layers only -- see the module docstring on via
                # semantics (a through via's barrel is DRC's business).
                for layer in via.layers:
                    layer_name = _layer_name(layer)
                    if layer_name in blocked:
                        violations.append(
                            LayerIntentViolation(
                                net_name=name,
                                layer=layer_name,
                                kind="via",
                                x=via.x,
                                y=via.y,
                                inherited=inherited,
                                width=via.diameter,
                            )
                        )
    violations.sort(key=lambda v: (v.inherited, v.net_name, v.layer, v.kind, v.x, v.y))
    return violations


def _layer_name(layer: object) -> str:
    """KiCad name for a router ``Layer``/``CopperLayer`` (or a raw string)."""
    name = getattr(layer, "kicad_name", None)
    if isinstance(name, str):
        return name
    return str(layer)
