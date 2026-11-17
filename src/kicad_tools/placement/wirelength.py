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

import math
from dataclasses import dataclass
from typing import Sequence

from .cost import ComponentPlacement, Net, compute_wirelength
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


def build_pad_position_map(
    placements: Sequence[PlacedComponent],
) -> dict[tuple[str, str], tuple[float, float]]:
    """Build a ``(reference, pad_name) -> (x, y)`` map of absolute pad positions.

    This is the bridge that lets the optimizer objective in
    :mod:`kicad_tools.placement.cost` measure wirelength against real pad
    coordinates without importing this module (``wirelength`` already imports
    ``cost``, so the dependency may only run one way). Pass the result as the
    ``pad_positions`` argument of
    :func:`kicad_tools.placement.cost.compute_wirelength` or
    :func:`kicad_tools.placement.cost.evaluate_placement` (issue #4831 M1;
    see ``docs/placement-pad-anchoring-audit.md``).

    Args:
        placements: Decoded placements with transformed pad coordinates
            (as returned by :func:`kicad_tools.placement.vector.decode`).

    Returns:
        Mapping from ``(component_reference, pad_name)`` to the pad's
        absolute ``(x, y)`` position in mm. Components without pads
        contribute no entries.
    """
    return {(comp.reference, pad.name): (pad.x, pad.y) for comp in placements for pad in comp.pads}


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


# ---------------------------------------------------------------------------
# Side-by-side estimator report (issue #4831 M5)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WirelengthEstimatorReport:
    """Both wirelength estimators measured on one layout (issue #4831 M5).

    The optimizer can score its wirelength term either between component
    centres (the historical default) or between real transformed pad
    coordinates (M1, ``--pad-anchored-wirelength``). This report evaluates
    *both* on the same placement so the choice can be argued from measured
    fleet evidence instead of from pcbplace's second-hand
    59.5 -> 12.7 mm number. It never changes what is optimized.

    Attributes:
        centre_anchored_mm: Weighted HPWL measured between component centres.
        pad_anchored_mm: Weighted HPWL measured between transformed pads,
            with a per-pin fallback to the component centre for pins that
            have no pad.
        delta_mm: ``pad_anchored_mm - centre_anchored_mm``. Negative means
            the pad-anchored estimator reads the layout as *shorter*.
        delta_pct: ``delta_mm`` as a percentage of ``centre_anchored_mm``, or
            ``None`` when the centre-anchored estimate is zero (no ratio is
            defined).
        scored: Which estimator the objective actually used for this run --
            ``"pad"`` or ``"centre"``. The other value is report-only.
        pads_available: True when at least one placement carries a pad. When
            False the two estimators are identical by construction and the
            comparison carries no information (a board whose components were
            decoded without pad geometry).
        pad_count: Number of transformed pads seen across all placements.
    """

    centre_anchored_mm: float
    pad_anchored_mm: float
    delta_mm: float
    delta_pct: float | None
    scored: str
    pads_available: bool
    pad_count: int

    def as_dict(self, ndigits: int | None = None) -> dict[str, object]:
        """Render as a JSON-serializable dict, optionally rounding the floats.

        Args:
            ndigits: When not ``None``, round every millimetre/percentage
                field to this many decimal places (the MCP surface rounds;
                the CLI ``--format json`` document does not).

        Returns:
            A dict with the same field names as this dataclass.
        """

        def _r(value: float | None) -> float | None:
            if value is None or ndigits is None:
                return value
            return round(value, ndigits)

        return {
            "centre_anchored_mm": _r(self.centre_anchored_mm),
            "pad_anchored_mm": _r(self.pad_anchored_mm),
            "delta_mm": _r(self.delta_mm),
            "delta_pct": _r(self.delta_pct),
            "scored": self.scored,
            "pads_available": self.pads_available,
            "pad_count": self.pad_count,
        }

    def summary_line(self) -> str:
        """One-line human summary for CLI prose output."""
        if not self.pads_available:
            return (
                f"Wirelength estimators: centre-anchored {self.centre_anchored_mm:.2f} mm; "
                "pad-anchored not measurable (no pad geometry decoded)"
            )
        pct = "n/a" if self.delta_pct is None else f"{self.delta_pct:+.1f}%"
        return (
            f"Wirelength estimators: centre-anchored {self.centre_anchored_mm:.2f} mm | "
            f"pad-anchored {self.pad_anchored_mm:.2f} mm ({pct}); "
            f"objective scored {self.scored}-anchored"
        )


def compare_wirelength_estimators(
    placements: Sequence[PlacedComponent],
    nets: Sequence[Net],
    *,
    scored: str = "centre",
) -> WirelengthEstimatorReport:
    """Measure the centre- and pad-anchored wirelength of one layout.

    Both legs go through :func:`kicad_tools.placement.cost.compute_wirelength`
    -- the *same* estimator with and without a pad map -- rather than pairing
    it against :func:`compute_hpwl`. That matters: ``compute_hpwl`` ignores
    ``Net.weight``, so pairing the two would confound the anchoring change
    with a silent loss of per-net weighting (the audit's M1 counter-note).
    Here the only difference between the two numbers is where each pin is
    measured.

    This is a pure measurement: it reads nothing global, mutates nothing, and
    returns the same values for the same inputs.

    Args:
        placements: Decoded placements with transformed pad coordinates (as
            returned by :func:`kicad_tools.placement.vector.decode`).
        nets: Net connectivity, including any ``Net.weight`` values.
        scored: Which estimator the caller's objective actually used --
            ``"centre"`` or ``"pad"``. Recorded in the report; it does not
            affect either measurement.

    Returns:
        A :class:`WirelengthEstimatorReport`.

    Raises:
        ValueError: If *scored* is neither ``"centre"`` nor ``"pad"``.
    """
    if scored not in ("centre", "pad"):
        raise ValueError(f"scored must be 'centre' or 'pad', got {scored!r}")

    centres = [
        ComponentPlacement(reference=p.reference, x=p.x, y=p.y, rotation=p.rotation)
        for p in placements
    ]
    pad_positions = build_pad_position_map(placements)

    centre_mm = compute_wirelength(centres, nets)
    pad_mm = compute_wirelength(centres, nets, pad_positions)
    delta = pad_mm - centre_mm

    return WirelengthEstimatorReport(
        centre_anchored_mm=centre_mm,
        pad_anchored_mm=pad_mm,
        delta_mm=delta,
        delta_pct=(delta / centre_mm * 100.0) if centre_mm else None,
        scored=scored,
        pads_available=bool(pad_positions),
        pad_count=len(pad_positions),
    )


# ---------------------------------------------------------------------------
# Per-footprint ratsnest distance
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class FootprintRatsnest:
    """Ratsnest distance for a single footprint.

    Attributes:
        reference: Component reference designator.
        ratsnest_mm: Sum of minimum nearest-pad distances to each connected
            net's other footprints, using actual pad coordinates.
    """

    reference: str
    ratsnest_mm: float


def compute_per_footprint_ratsnest(
    placements: Sequence[PlacedComponent],
    nets: Sequence[Net],
) -> list[FootprintRatsnest]:
    """Compute per-footprint ratsnest distance.

    For each footprint F, the ratsnest distance is the sum over all nets N
    containing F of the minimum Euclidean distance from any pad of F on net N
    to the nearest pad of any OTHER footprint on net N.  This is the sum of
    "nearest airwire" distances -- exactly what KiCad draws as ratsnest lines.

    The result list is sorted descending by ``ratsnest_mm`` so the worst-placed
    components appear first.  Footprints with no net connections have
    ``ratsnest_mm == 0.0``.

    Args:
        placements: Decoded placements with transformed pad coordinates.
        nets: Net connectivity information.

    Returns:
        List of :class:`FootprintRatsnest` sorted descending by ratsnest_mm.
    """
    # Build a lookup: (reference, pad_name) -> (x, y)
    pad_positions: dict[tuple[str, str], tuple[float, float]] = {}
    all_refs: list[str] = []
    for comp in placements:
        all_refs.append(comp.reference)
        for pad in comp.pads:
            pad_positions[(comp.reference, pad.name)] = (pad.x, pad.y)

    # Accumulate ratsnest distance per footprint
    ratsnest: dict[str, float] = dict.fromkeys(all_refs, 0.0)

    for net in nets:
        # Collect pads grouped by footprint for this net
        fp_pads: dict[str, list[tuple[float, float]]] = {}
        for ref, pad_name in net.pins:
            pos = pad_positions.get((ref, pad_name))
            if pos is not None:
                fp_pads.setdefault(ref, []).append(pos)

        refs = list(fp_pads.keys())
        if len(refs) < 2:
            continue

        # For each footprint in this net, find minimum distance to the
        # nearest pad on a different footprint in the same net
        for i, ref_a in enumerate(refs):
            min_dist = math.inf
            for j, ref_b in enumerate(refs):
                if i == j:
                    continue
                for pad_a in fp_pads[ref_a]:
                    for pad_b in fp_pads[ref_b]:
                        dist = math.hypot(pad_a[0] - pad_b[0], pad_a[1] - pad_b[1])
                        if dist < min_dist:
                            min_dist = dist
            if min_dist < math.inf:
                ratsnest[ref_a] += min_dist

    # Build result list sorted descending by ratsnest distance
    result = [
        FootprintRatsnest(reference=ref, ratsnest_mm=round(ratsnest[ref], 3)) for ref in all_refs
    ]
    result.sort(key=lambda fr: -fr.ratsnest_mm)
    return result
