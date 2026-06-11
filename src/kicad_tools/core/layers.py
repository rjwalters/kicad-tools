"""Copper-layer stackup helpers shared across analysis and DRC code.

KiCad's layer *numbering* is not the physical stackup order (KiCad 9
numbers F.Cu=0, B.Cu=2, inner layers 4, 6, 8, ...), so code that needs
"which layers does this via barrel pass through?" must consult the
canonical front-to-back name ordering instead of layer indices.

The canonical ordering and the :func:`via_spans_layer` predicate were
originally private to ``analysis/net_status.py``; Issue #3487 promoted
them here so the DRC clearance rule can reuse the same semantics (a
through-via barrel is physical copper on EVERY layer it spans, not just
the two endpoint layers KiCad lists in ``(layers "F.Cu" "B.Cu")``).
"""

from __future__ import annotations

# Canonical copper layer ordering from top (front) to bottom (back).
# KiCad supports up to 30 inner layers named In1.Cu .. In30.Cu.
COPPER_LAYER_ORDER: tuple[str, ...] = (
    "F.Cu",
    *(f"In{i}.Cu" for i in range(1, 31)),
    "B.Cu",
)

_COPPER_LAYER_INDEX: dict[str, int] = {name: idx for idx, name in enumerate(COPPER_LAYER_ORDER)}


def via_spans_layer(via_layers: list[str] | tuple[str, ...], target_layer: str) -> bool:
    """Check whether a via barrel passes through a target copper layer.

    In KiCad, a via with layers ``["F.Cu", "B.Cu"]`` is a through-via
    whose barrel is physical copper on ALL intermediate copper layers
    (In1.Cu, In2.Cu, ...), not just the two listed endpoint layers.  A
    blind/buried/micro via such as ``["F.Cu", "In1.Cu"]`` spans only the
    layers between (and including) its declared endpoints.

    Args:
        via_layers: Layer names declared on the via
            (e.g., ``["F.Cu", "B.Cu"]``).
        target_layer: Copper layer name to test (e.g., ``"In1.Cu"``).

    Returns:
        True if the via barrel exists on (electrically connects to)
        the target layer.
    """
    # Direct match -- layer explicitly listed on the via.
    if target_layer in via_layers:
        return True

    # Determine the span of the via in the copper stack.
    indices = [_COPPER_LAYER_INDEX[layer] for layer in via_layers if layer in _COPPER_LAYER_INDEX]
    if len(indices) < 2:
        return False

    # Target must also be a recognised copper layer.
    target_idx = _COPPER_LAYER_INDEX.get(target_layer)
    if target_idx is None:
        return False

    # Via spans from min to max index; any layer in between is connected.
    return min(indices) <= target_idx <= max(indices)
