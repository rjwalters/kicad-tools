"""Non-local under-body routing surcharge (Issue #9622, audit row 9).

Row 9 of the #9592 dense-board audit: a non-local net (no pad on the IC)
whose straight-line path crosses a fine-pitch IC's body routed a single
straight trunk through the body area at zero cost, splitting the free area
under the part and stranding whoever routed second.  Nothing in the grid
A* cost model distinguished "copper under a part's body" from "copper in
open board space" -- footprint bodies were neither obstacles nor cost
regions.

This module derives the surcharge REGIONS once per routing grid:

* the components ``identify_fine_pitch_components`` flags (pin pitch at or
  below :data:`UNDER_BODY_PITCH_THRESHOLD`, at least
  :data:`MIN_BODY_PAD_COUNT` pads so two-terminal chips are never
  surcharged),
* their body area -- the bounding box of the component's own pads (the
  pad ring hugs the body on QFN/QFP/SOIC-class packages, so the pad bbox
  is the body proxy the router's pad-only data model can compute),
* the outer copper layers only (inner layers are excluded; a trunk under
  the body on F.Cu/B.Cu is the failure mode the audit observed),
* the "local" nets -- every net with a pad on the component.  They are
  exempt: a part's own escape legs and fan-out must be able to move under
  its own body at normal cost.  A component whose pads are all net 0
  (no-connect pads, or real nets withheld as net-0 obstacles by the
  route-auto loader's ``skip_nets`` retry) keeps its region with an EMPTY
  local set, so every routed net pays the surcharge around it.

The A* cost terms in both backends charge ``rules.cost_under_body *
rules.cost_straight`` per step inside a region to a net that is NOT local
to it.  The surcharge is soft and additive: a detour-free board still
routes through the body (never blocked), while a board with free space
around the part steers the trunk around it -- which is the audit's wanted
behaviour with no separate keepout/relaxation machinery.

Backend wiring:

* C++ engine: :meth:`cpp_backend.CppPathfinder._under_body_budgets_for_net`
  turns the regions into per-net-filtered ``PadChannelBudget`` entries, so
  the existing #3143 per-cell cost lookup (``get_pad_channel_cost``)
  prices them on every cost site (trace steps and via drops) with no new
  C++ code.
* Pure-Python engine: :class:`pathfinder.Router` mirrors the identical
  per-cell additive term from a precomputed per-cell region bitmask (one
  bit per region), so a cell covered by two overlapping bodies is
  surcharged once per covering region -- the same summing semantics as
  the C++ budget lookup above.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .adaptive_grid import identify_fine_pitch_components

if TYPE_CHECKING:
    from .grid import RoutingGrid

# Pin pitch at or below this (mm) makes a component's body a surcharge
# region.  This is ``identify_fine_pitch_components``' own default, the
# threshold the adaptive-grid escape phase already uses for QFN/QFP/
# SSOP/TSSOP-class packages (0.5mm QFN, 0.65mm SSOP, 0.8mm TQFP).
UNDER_BODY_PITCH_THRESHOLD = 0.8

# A component needs at least this many pads before its body area is
# surcharged.  Two-terminal chips (R/C/L) and 3-pin SOT packages have
# pin-to-pin spacings inside the pitch threshold above while being exactly
# the parts dense boards route *between*; surcharging their bodies would
# distort ordinary boards without addressing the trunk-through-IC failure
# mode.  IC-class packages (QFN, QFP, SOIC/SSOP/TSSOP, BGA) all exceed it.
MIN_BODY_PAD_COUNT = 4

# The pad bbox is grown by this many grid cells on every side before it
# becomes a surcharge region, so the region covers the body sliver just
# outside the pad ring and the clearance halo a trunk would hug.
BODY_BBOX_MARGIN_CELLS = 1


@dataclass(frozen=True)
class BodySurchargeRegion:
    """One component's under-body surcharge area, in grid coordinates.

    ``gx1/gy1/gx2/gy2`` is the closed cell bbox (already clamped to the
    grid extent).  ``layers`` are grid layer indices -- outer copper
    layers only.  ``local_nets`` are the routing net ids with a pad on
    the component; the surcharge never applies to them.
    """

    ref: str
    gx1: int
    gy1: int
    gx2: int
    gy2: int
    layers: tuple[int, ...]
    local_nets: frozenset[int]


def compute_under_body_regions(grid: RoutingGrid) -> list[BodySurchargeRegion]:
    """Compute the per-component under-body surcharge regions for ``grid``.

    Called once per board (cached on the grid) after all pads are added;
    ``RoutingGrid.under_body_surcharges`` is the public accessor.

    Returns an empty list when the board has no qualifying fine-pitch
    component, so both engines' hot paths stay dormant (and byte-identical
    to pre-#9622 behaviour) on boards without one.

    A component whose pads are ALL net 0 (genuinely no-connect, or real
    nets withheld as net-0 obstacles by the route-auto loader's
    ``skip_nets`` retry) still gets a region -- with an empty
    ``local_nets`` set, so every routed net pays the surcharge around the
    body instead of splitting it (review of #9635).
    """
    pads = list(grid.pads)
    if not pads:
        return []

    fine_components = identify_fine_pitch_components(
        pads,
        grid.resolution,
        UNDER_BODY_PITCH_THRESHOLD,
    )
    if not fine_components:
        return []

    cols = grid.cols
    rows = grid.rows
    outer_layers = tuple(
        layer.index for layer in grid.layer_stack.outer_layers if 0 <= layer.index < grid.num_layers
    )
    if not outer_layers:
        return []

    # Group the pads per component once; skip no-connect / unconnected
    # nets (net id 0) so a NC pad never marks a real net "local".
    pads_by_ref: dict[str, list] = {}
    for pad in pads:
        key = pad.component_key
        if key in fine_components:
            pads_by_ref.setdefault(key, []).append(pad)

    regions: list[BodySurchargeRegion] = []
    for ref, comp_pads in pads_by_ref.items():
        if len(comp_pads) < MIN_BODY_PAD_COUNT:
            continue

        # Net 0 never marks a net local, but it does not void the body
        # either: a component whose pads are ALL net 0 (genuinely NC, or
        # real nets withheld as net-0 obstacles by the route-auto loader's
        # ``skip_nets`` retry) keeps its region with an EMPTY local set, so
        # every routed net pays the surcharge around it.  Dropping the
        # region instead let the hierarchical retry route the requested
        # trunk straight through the body (review of #9635).
        local_nets = frozenset(pad.net for pad in comp_pads if pad.net > 0)

        xs = [pad.x for pad in comp_pads]
        ys = [pad.y for pad in comp_pads]
        gx1 = min(grid.world_to_grid(x, y)[0] for x, y in zip(xs, ys, strict=True))
        gx2 = max(grid.world_to_grid(x, y)[0] for x, y in zip(xs, ys, strict=True))
        gy1 = min(grid.world_to_grid(x, y)[1] for x, y in zip(xs, ys, strict=True))
        gy2 = max(grid.world_to_grid(x, y)[1] for x, y in zip(xs, ys, strict=True))
        gx1 = max(0, gx1 - BODY_BBOX_MARGIN_CELLS)
        gy1 = max(0, gy1 - BODY_BBOX_MARGIN_CELLS)
        gx2 = min(cols - 1, gx2 + BODY_BBOX_MARGIN_CELLS)
        gy2 = min(rows - 1, gy2 + BODY_BBOX_MARGIN_CELLS)
        if gx1 > gx2 or gy1 > gy2:
            continue

        regions.append(
            BodySurchargeRegion(
                ref=ref,
                gx1=gx1,
                gy1=gy1,
                gx2=gx2,
                gy2=gy2,
                layers=outer_layers,
                local_nets=local_nets,
            )
        )

    return regions


def region_covers_cell(region: BodySurchargeRegion, gx: int, gy: int, layer: int) -> bool:
    """True when ``region`` covers grid cell ``(gx, gy)`` on ``layer``."""
    return (
        layer in region.layers and region.gx1 <= gx <= region.gx2 and region.gy1 <= gy <= region.gy2
    )
