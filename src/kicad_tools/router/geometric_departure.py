"""Resolve conservative departure raster halos against actual copper geometry."""

from __future__ import annotations

import time
from dataclasses import replace

from .construction_validation import constructed_pair_geometry_issue
from .departure_planning import ValidatedDeparture, departure_proposals
from .layers import Layer
from .primitives import Route


def geometric_departures(router, finder, pads, *, deadline, reserved_routes=()):
    """Offer existing escape shapes only after the full physical geometry gate.

    Native occupancy includes conservative route halos. Those can reject a
    physically clear barrel even when its trace can leave the pad. This path
    reconstructs an uncommitted proposal and uses the existing exact validator:
    unknown occupancy still fails closed, and all pads, copper, barrels and
    drills remain obstacles. Assembled bodies must separately pass the normal
    authored skew/coupling and post-tuning geometry gates.
    """
    if time.monotonic() >= deadline:
        return
    nc = finder.net_class_map.get(pads[0].net_name)
    if nc is None:
        return
    grid = finder.grid
    for proposal in departure_proposals(finder, pads):
        if time.monotonic() >= deadline:
            return
        routes, endpoints = [], []
        for offset, start, goal in ((0, pads[0], pads[1]), (3, pads[2], pads[3])):
            last = proposal.prefix[-1]
            x, y = grid.grid_to_world(last[offset], last[offset + 1])
            endpoint = replace(goal, x=x, y=y, layer=Layer(grid.index_to_layer(last[offset + 2])))
            gx, gy = grid.world_to_grid(start.x, start.y)
            start_layer = grid.layer_to_index(start.layer.value)
            path = [(*grid.grid_to_world(gx, gy), start_layer, False)]
            previous_layer = start_layer
            for step in proposal.prefix:
                layer = step[offset + 2]
                path.append(
                    (
                        *grid.grid_to_world(step[offset], step[offset + 1]),
                        layer,
                        layer != previous_layer,
                    )
                )
                previous_layer = layer
            route = Route(net=start.net, net_name=start.net_name)
            finder._build_route_from_path(route, path, start, endpoint)
            routes.append(route)
            endpoints.append(endpoint)
        issue = constructed_pair_geometry_issue(
            router,
            finder,
            *routes,
            (pads[0], endpoints[0], pads[2], endpoints[1]),
            intra_pair_clearance=nc.effective_intra_pair_clearance(),
            deadline=deadline,
            reserved_routes=reserved_routes,
        )
        if issue is None:
            yield ValidatedDeparture(proposal, *routes, iterations=0, native_validated=False)
