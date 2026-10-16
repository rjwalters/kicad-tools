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
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import Any

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
        net_classes: The P and N nets' classes (``None`` entries are skipped).

    Order of ``base`` is preserved.  May return an empty list -- callers
    decline (the pair, or the tie) rather than fall back to a forbidden layer.
    """
    candidates = list(base)
    allowed = getattr(rules, "allowed_layers", None)
    strict = getattr(rules, "strict_layers", False)
    hard_avoided: set[int] = set()
    for net_class in net_classes:
        accessor = getattr(net_class, "hard_avoided_layer_indices", None)
        if accessor is not None:
            hard_avoided |= set(accessor(strict))
    if allowed is None and not hard_avoided:
        return candidates
    allowed_set = None if allowed is None else set(allowed)
    return [
        idx
        for idx in candidates
        if idx not in hard_avoided and (allowed_set is None or layer_name(idx) in allowed_set)
    ]
