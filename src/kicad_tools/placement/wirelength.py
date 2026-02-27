"""Half-Perimeter Wirelength (HPWL) estimator for placement scoring.

Computes the standard HPWL wirelength proxy used in placement optimization.
For each net, HPWL is the half-perimeter of the bounding box enclosing all
pads belonging to that net:

    HPWL(net) = (max_x - min_x) + (max_y - min_y)

Unlike the simpler component-center wirelength in ``cost.py``, this module
operates on decoded placements with fully transformed pad coordinates from
:mod:`kicad_tools.placement.vector`.

Usage::

    from kicad_tools.placement.wirelength import compute_hpwl, compute_hpwl_breakdown
    from kicad_tools.placement.cost import Net

    total = compute_hpwl(placed_components, nets)
    breakdown = compute_hpwl_breakdown(placed_components, nets)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from .cost import Net
from .vector import PlacedComponent, TransformedPad


@dataclass(frozen=True)
class NetWirelength:
    """HPWL result for a single net.

    Attributes:
        name: Net name.
        hpwl: Half-perimeter wirelength in mm.
        pad_count: Number of pads resolved in this net.
    """

    name: str
    hpwl: float
    pad_count: int


@dataclass(frozen=True)
class HPWLResult:
    """Complete HPWL computation result.

    Attributes:
        total: Total HPWL across all nets (mm).
        per_net: Per-net breakdown with individual HPWL values.
    """

    total: float
    per_net: tuple[NetWirelength, ...]


def _build_pad_lookup(
    placements: Sequence[PlacedComponent],
) -> dict[tuple[str, str], TransformedPad]:
    """Build a lookup table mapping (reference, pad_name) to transformed pad.

    Args:
        placements: Decoded placements with transformed pad coordinates.

    Returns:
        Dictionary mapping (component_reference, pad_name) to the
        :class:`TransformedPad` instance.
    """
    lookup: dict[tuple[str, str], TransformedPad] = {}
    for comp in placements:
        for pad in comp.pads:
            lookup[(comp.reference, pad.name)] = pad
    return lookup


def _hpwl_for_net(
    net: Net,
    pad_lookup: dict[tuple[str, str], TransformedPad],
) -> NetWirelength:
    """Compute the HPWL for a single net.

    Args:
        net: Net with pin references.
        pad_lookup: Mapping from (reference, pad_name) to transformed pad.

    Returns:
        :class:`NetWirelength` with the half-perimeter wirelength.
    """
    xs: list[float] = []
    ys: list[float] = []

    for ref, pin_name in net.pins:
        pad = pad_lookup.get((ref, pin_name))
        if pad is not None:
            xs.append(pad.x)
            ys.append(pad.y)

    pad_count = len(xs)

    if pad_count < 2:
        # Single-pad or empty nets have zero wirelength.
        return NetWirelength(name=net.name, hpwl=0.0, pad_count=pad_count)

    hpwl = (max(xs) - min(xs)) + (max(ys) - min(ys))
    return NetWirelength(name=net.name, hpwl=hpwl, pad_count=pad_count)


def compute_hpwl(
    placements: Sequence[PlacedComponent],
    nets: Sequence[Net],
) -> float:
    """Compute total HPWL wirelength across all nets.

    For each net, HPWL = (max_x - min_x) + (max_y - min_y) across all pads
    in the net. Uses actual transformed pad coordinates from decoded
    placements, not component centers.

    Args:
        placements: Decoded placements with transformed pad coordinates.
        nets: Net connectivity information.

    Returns:
        Total HPWL in mm (sum over all nets).
    """
    if not nets:
        return 0.0

    pad_lookup = _build_pad_lookup(placements)

    total = 0.0
    for net in nets:
        result = _hpwl_for_net(net, pad_lookup)
        total += result.hpwl
    return total


def compute_hpwl_breakdown(
    placements: Sequence[PlacedComponent],
    nets: Sequence[Net],
) -> HPWLResult:
    """Compute HPWL with per-net breakdown.

    Same computation as :func:`compute_hpwl`, but also returns a per-net
    breakdown useful for debugging and analysis.

    Args:
        placements: Decoded placements with transformed pad coordinates.
        nets: Net connectivity information.

    Returns:
        :class:`HPWLResult` with total and per-net HPWL values.
    """
    if not nets:
        return HPWLResult(total=0.0, per_net=())

    pad_lookup = _build_pad_lookup(placements)

    per_net: list[NetWirelength] = []
    total = 0.0

    for net in nets:
        result = _hpwl_for_net(net, pad_lookup)
        per_net.append(result)
        total += result.hpwl

    return HPWLResult(total=total, per_net=tuple(per_net))
