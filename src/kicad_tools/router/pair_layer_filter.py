"""Layers a differential pair may put copper on (Issues #6857, #6866).

The single-ended router (Python ``Pathfinder._is_layer_allowed``, the C++
backend's ``_apply_allowed_layers_to_routable`` / ``_hard_avoided_layers`` and
the lattice ``LatticePathfinder._hard_avoided_layers``) removes two kinds of
layer from a net's routable set:

* layers whose KiCad name is not in ``rules.allowed_layers`` (set by
  ``--reserve-plane-layers``, on by default, #5014 / #5789);
* the net class's HARD ``avoid_layers``
  (``hard_avoided_layer_indices(rules.strict_layers)``: ampacity classes, or
  ``--strict-layers``).

Pair copper belongs to two nets, so the pair filter takes the union of both
nets' hard-avoided layers.  :func:`pair_permitted_layers` is the single source
of truth shared by the grid coupled search
(``CoupledPathfinder._coupled_routable_layers``, #6857) and the lattice
diff-pair tie step (``Autorouter._lattice_plan_pair_ties``, #6866), which
before #6866 placed tie copper on a reserved reference plane.

Issue #6876: a class may opt in to named reserved plane layers through
``NetClassRouting.allow_plane_layers``.  Each net's copper must be legal for
that net, so a pair layer outside ``rules.allowed_layers`` is permitted only
when :func:`~kicad_tools.router.rules.layer_permitted` holds for BOTH nets'
classes -- a pair where only one side opts in stays off the plane.
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

from .rules import layer_permitted

__all__ = ["pair_permitted_layers"]


def pair_permitted_layers(
    base: Iterable[int],
    layer_name: Callable[[int], str],
    rules: Any,
    net_classes: Iterable[Any],
) -> list[int]:
    """Return the indices in ``base`` a pair whose nets have ``net_classes`` may use.

    Args:
        base: Candidate layer indices (routable layers, or a pad-shared subset).
        layer_name: Maps an index in ``base`` to its KiCad layer name
            (``"F.Cu"``); only consulted when ``rules.allowed_layers`` is set.
        rules: Design rules; reads ``allowed_layers`` and ``strict_layers``.
        net_classes: The P and N nets' classes.  A ``None`` entry contributes
            no hard-avoid layers and no plane opt-in (#6876), so a pair with a
            class-less net never reaches a reserved plane.

    Order of ``base`` is preserved.  May return an empty list -- callers
    decline (the pair, or the tie) rather than fall back to a forbidden layer.
    """
    candidates = list(base)
    classes = list(net_classes)
    allowed = getattr(rules, "allowed_layers", None)
    strict = getattr(rules, "strict_layers", False)
    hard_avoided: set[int] = set()
    for net_class in classes:
        accessor = getattr(net_class, "hard_avoided_layer_indices", None)
        if accessor is not None:
            hard_avoided |= set(accessor(strict))
    if allowed is None and not hard_avoided:
        return candidates
    if not classes:
        classes = [None]

    def _permitted(idx: int) -> bool:
        if allowed is None:
            return True
        name = layer_name(idx)
        # Issue #6876: every net of the pair must be allowed on the layer.
        return all(layer_permitted(rules, nc, idx, name) for nc in classes)

    return [idx for idx in candidates if idx not in hard_avoided and _permitted(idx)]
