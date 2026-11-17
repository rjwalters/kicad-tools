"""Placement DRC clearance checker for optimization-time constraint evaluation.

Checks component-to-component courtyard clearance and pad-to-pad clearance
against design rules during placement optimization.  Works with
:class:`PlacedComponent` and :class:`ComponentDef` types from the placement
vector module and the :class:`DesignRules` from the router rules module.

Two kinds of clearance check are performed:

* **Courtyard clearance** -- axis-aligned bounding box gap between components
  on the same board side must meet the minimum clearance from design rules.

* **Pad-to-pad clearance** -- pads belonging to *different* nets must have at
  least the required clearance between their bounding boxes.  Pads on the
  same net are excluded (they will eventually be connected by copper).

Usage::

    from kicad_tools.placement.drc import check_placement_drc, DrcResult
    from kicad_tools.router.rules import DesignRules

    result = check_placement_drc(placements, component_defs, rules)
    print(result.violation_count, result.total_violation_distance)
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence

from kicad_tools.placement.cost import Net
from kicad_tools.placement.geometry import _aabb
from kicad_tools.placement.vector import (
    ComponentDef,
    PlacedComponent,
    TransformedPad,
)
from kicad_tools.router.rules import DesignRules, NetClassRouting

# ---------------------------------------------------------------------------
# Result types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ClearanceViolation:
    """A single clearance violation between two objects.

    Attributes:
        ref_a: Reference designator of first component.
        ref_b: Reference designator of second component.
        kind: Violation kind -- ``"courtyard"`` or ``"pad"``.
        required_clearance: Minimum clearance required by design rules (mm).
        actual_clearance: Actual measured clearance (mm).  Negative values
            indicate overlap.
        violation_distance: How far below the minimum clearance (mm).
            Always ``>= 0`` for a true violation.
        pad_a: Pad name on first component (only for pad violations).
        pad_b: Pad name on second component (only for pad violations).
    """

    ref_a: str
    ref_b: str
    kind: str
    required_clearance: float
    actual_clearance: float
    violation_distance: float
    pad_a: str | None = None
    pad_b: str | None = None


@dataclass(frozen=True)
class DrcResult:
    """Result of a placement DRC clearance check.

    Attributes:
        violation_count: Total number of clearance violations.
        total_violation_distance: Sum of violation distances across all
            violations (mm).  This is the total amount by which components
            fall below the required clearance -- useful as a smooth penalty
            in an optimizer cost function.
        violations: Detailed list of every violating pair.
    """

    violation_count: int = 0
    total_violation_distance: float = 0.0
    violations: tuple[ClearanceViolation, ...] = ()


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _box_gap(
    box_a: tuple[float, float, float, float],
    box_b: tuple[float, float, float, float],
) -> float:
    """Compute the minimum edge-to-edge gap between two axis-aligned boxes.

    Returns a *negative* value when the boxes overlap.  For separated boxes
    the gap is the Euclidean distance between the closest edges/corners.

    Args:
        box_a: ``(min_x, min_y, max_x, max_y)``
        box_b: ``(min_x, min_y, max_x, max_y)``

    Returns:
        Minimum gap in mm (negative means overlap).
    """
    # Signed distances along each axis.  Positive = separated, negative = overlapping.
    gap_x = max(box_a[0], box_b[0]) - min(box_a[2], box_b[2])
    gap_y = max(box_a[1], box_b[1]) - min(box_a[3], box_b[3])

    if gap_x <= 0 and gap_y <= 0:
        # Overlapping on both axes -- gap is the larger (less-negative) axis distance.
        # e.g. gap_x=-2, gap_y=-1 => actual gap is -1 (just touching on y).
        return max(gap_x, gap_y)

    if gap_x > 0 and gap_y > 0:
        # Separated on both axes -- corner-to-corner Euclidean distance.
        return (gap_x**2 + gap_y**2) ** 0.5

    # Separated on exactly one axis -- edge-to-edge distance.
    return max(gap_x, gap_y)


def _pad_box(pad: TransformedPad) -> tuple[float, float, float, float]:
    """Compute the axis-aligned bounding box for a transformed pad.

    Args:
        pad: Transformed pad with absolute position and size.

    Returns:
        ``(min_x, min_y, max_x, max_y)``
    """
    half_x = pad.size_x / 2.0
    half_y = pad.size_y / 2.0
    return (
        pad.x - half_x,
        pad.y - half_y,
        pad.x + half_x,
        pad.y + half_y,
    )


def _build_pad_net_map(
    placements: Sequence[PlacedComponent],
    nets: Sequence[Net],
) -> dict[tuple[str, str], str]:
    """Build a mapping from (component_ref, pad_name) to net name.

    Args:
        placements: Decoded placements (used only for validation context).
        nets: Net definitions with pin lists.

    Returns:
        Dictionary mapping ``(reference, pad_name)`` to the net name.
    """
    pad_net: dict[tuple[str, str], str] = {}
    for net in nets:
        for ref, pin_name in net.pins:
            pad_net[(ref, pin_name)] = net.name
    return pad_net


def _clearance_for_pair(
    rules: DesignRules,
    net_a: str | None,
    net_b: str | None,
    net_class_map: dict[str, NetClassRouting] | None,
) -> float:
    """Determine the clearance requirement for a pair of nets.

    If both nets have net-class overrides, the *maximum* of the two
    net-class clearances is used (the stricter rule wins).  Otherwise
    falls back to ``rules.trace_clearance``.

    Args:
        rules: Global design rules.
        net_a: Net name of the first pad (or ``None``).
        net_b: Net name of the second pad (or ``None``).
        net_class_map: Optional mapping from net name to net-class routing.

    Returns:
        Required clearance in mm.
    """
    base = rules.trace_clearance

    if net_class_map is None:
        return base

    clearance_a = net_class_map[net_a].clearance if net_a and net_a in net_class_map else base
    clearance_b = net_class_map[net_b].clearance if net_b and net_b in net_class_map else base

    # The stricter (larger) clearance wins.
    return max(clearance_a, clearance_b)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def check_placement_drc(
    placements: Sequence[PlacedComponent],
    component_defs: Sequence[ComponentDef],
    rules: DesignRules,
    nets: Sequence[Net] | None = None,
    net_class_map: dict[str, NetClassRouting] | None = None,
) -> DrcResult:
    """Check all placement clearance constraints and return a DRC result.

    Performs two categories of check:

    1. **Courtyard clearance** -- for each pair of components on the same
       board side, verifies that the edge-to-edge gap between their
       axis-aligned bounding boxes meets ``rules.trace_clearance``.

    2. **Pad-to-pad clearance** -- for pads on *different* nets, verifies
       that the edge-to-edge gap between pad bounding boxes meets the
       clearance requirement (possibly per-net-class).  Pads on the same
       net are skipped.  Pad checks are only performed when *nets* is
       provided.

    Components on opposite board sides are not checked against each other.

    Complexity is O(N^2) for courtyard checks and O(P^2) for pad checks
    in the worst case, which is acceptable for boards with fewer than
    ~100 components.

    Args:
        placements: Placed components with positions, rotations, and sides.
        component_defs: Static component definitions (same order as
            *placements*).
        rules: Design rules providing default clearance values.
        nets: Optional net definitions for pad-to-pad checks.  When
            ``None``, only courtyard checks are performed.
        net_class_map: Optional mapping from net name to
            :class:`NetClassRouting` for per-net-class clearance overrides.

    Returns:
        :class:`DrcResult` summarizing all violations.

    Raises:
        ValueError: If *placements* and *component_defs* have different
            lengths.
    """
    n = len(placements)
    if n != len(component_defs):
        raise ValueError(f"placements has {n} items but component_defs has {len(component_defs)}")

    violations: list[ClearanceViolation] = []
    min_clearance = rules.trace_clearance

    # ------------------------------------------------------------------
    # 1. Courtyard (AABB) clearance checks
    # ------------------------------------------------------------------
    boxes = [
        _aabb(comp, comp_def) for comp, comp_def in zip(placements, component_defs, strict=True)
    ]

    for i in range(n):
        for j in range(i + 1, n):
            # Skip components on different board sides.
            if placements[i].side != placements[j].side:
                continue

            gap = _box_gap(boxes[i], boxes[j])
            if gap < min_clearance:
                violation_dist = min_clearance - gap
                violations.append(
                    ClearanceViolation(
                        ref_a=placements[i].reference,
                        ref_b=placements[j].reference,
                        kind="courtyard",
                        required_clearance=min_clearance,
                        actual_clearance=gap,
                        violation_distance=violation_dist,
                    )
                )

    # ------------------------------------------------------------------
    # 2. Pad-to-pad clearance checks (only if nets are provided)
    # ------------------------------------------------------------------
    if nets is not None:
        pad_net_map = _build_pad_net_map(placements, nets)

        # Collect all pads with their owning component info.
        all_pads: list[tuple[str, TransformedPad, str | None]] = []
        for comp in placements:
            for pad in comp.pads:
                net_name = pad_net_map.get((comp.reference, pad.name))
                all_pads.append((comp.reference, pad, net_name))

        p = len(all_pads)
        for i in range(p):
            ref_i, pad_i, net_i = all_pads[i]
            for j in range(i + 1, p):
                ref_j, pad_j, net_j = all_pads[j]

                # Skip pads on the same component.
                if ref_i == ref_j:
                    continue

                # Skip pads on the same net (they will be connected).
                if net_i is not None and net_i == net_j:
                    continue

                required = _clearance_for_pair(rules, net_i, net_j, net_class_map)
                gap = _box_gap(_pad_box(pad_i), _pad_box(pad_j))

                if gap < required:
                    violation_dist = required - gap
                    violations.append(
                        ClearanceViolation(
                            ref_a=ref_i,
                            ref_b=ref_j,
                            kind="pad",
                            required_clearance=required,
                            actual_clearance=gap,
                            violation_distance=violation_dist,
                            pad_a=pad_i.name,
                            pad_b=pad_j.name,
                        )
                    )

    # ------------------------------------------------------------------
    # Build result
    # ------------------------------------------------------------------
    total_dist = sum(v.violation_distance for v in violations)

    return DrcResult(
        violation_count=len(violations),
        total_violation_distance=total_dist,
        violations=tuple(violations),
    )
