"""Temporary physical isolation between branches of a Kelvin star."""

from __future__ import annotations

import math
from contextlib import contextmanager
from typing import TYPE_CHECKING

import numpy as np
from shapely import affinity, intersects_xy  # type: ignore[import-untyped]
from shapely.geometry import LineString, Point, box  # type: ignore[import-untyped]

if TYPE_CHECKING:
    from .grid import RoutingGrid
    from .primitives import Pad


def _pad_outline(pad: Pad, *, contact: bool = False):
    """Use an inner contact area when rounded-corner metadata is unavailable."""
    w, h = pad.width, pad.height
    if pad.shape == "circle" or (contact and pad.shape == "roundrect"):
        shape = Point(0, 0).buffer(min(w, h) / 2)
    elif pad.shape == "oval":
        radius = min(w, h) / 2
        delta = abs(w - h) / 2
        ends = [(-delta, 0), (delta, 0)] if w >= h else [(0, -delta), (0, delta)]
        shape = LineString(ends).buffer(radius)
    else:
        shape = box(-w / 2, -h / 2, w / 2, h / 2)
    # KiCad pad angles rotate clockwise in board coordinates; Shapely
    # positive angles rotate counterclockwise in this coordinate plane.
    return affinity.translate(affinity.rotate(shape, -pad.rotation), pad.x, pad.y)


@contextmanager
def isolate_kelvin_branch(
    grid: RoutingGrid, pads: list[Pad], root: Pad, target: Pad, *, trace_width: float | None = None
):
    """Block previous branches and nonterminal pads only during one search.

    Work is bounded by individual conductor bounding boxes. Existing grid state
    is restored before the caller commits the newly found route, including when
    search raises. The electrical net identifiers of emitted copper never change.
    """
    width = grid.rules.trace_width if trace_width is None else trace_width
    # A centerline on the pad boundary can put overlapping trace caps outside
    # the pad. Restrict shared centerline access to the inset metal instead.
    contact = _pad_outline(root, contact=True).buffer(-width / 2)
    objects = []
    for route in grid.routes:
        if route.net != root.net:
            continue
        for segment in route.segments:
            objects.append(
                (
                    grid.layer_to_index(segment.layer.value),
                    LineString([segment.start, segment.end]).buffer(segment.width / 2),
                )
            )
        for via in route.vias:
            first, last = sorted(grid.layer_to_index(layer.value) for layer in via.layers)
            for layer in range(first, last + 1):
                objects.append((layer, Point(via.x, via.y).buffer(via.diameter / 2)))
    # Escape endpoints carry terminal identities but are not the physical
    # pads. Other branches must avoid the original metal as well as the
    # escape trace already collected above.
    physical_pads = {(pad.ref, pad.pin): pad for pad in grid._pads if pad.ref and pad.pin}
    for pad in pads:
        if pad is root or pad is target:
            continue
        pad = physical_pads.get((pad.ref, pad.pin), pad)
        layers = (
            range(grid.num_layers) if pad.through_hole else [grid.layer_to_index(pad.layer.value)]
        )
        objects.extend((layer, _pad_outline(pad)) for layer in layers)

    cells: set[tuple[int, int, int]] = set()
    for layer, shape in objects:
        # Keep the moving trace centerline outside existing copper expanded
        # by its own radius, then cover cells touching that exclusion area.
        # The inset shunt contact is the only allowed shared-copper region.
        shape = shape.buffer(width / 2 + grid.resolution / math.sqrt(2)).difference(contact)
        if shape.is_empty:
            continue
        xmin, ymin, xmax, ymax = shape.bounds
        x0 = max(0, math.floor((xmin - grid.origin_x) / grid.resolution))
        x1 = min(grid.cols - 1, math.ceil((xmax - grid.origin_x) / grid.resolution))
        y0 = max(0, math.floor((ymin - grid.origin_y) / grid.resolution))
        y1 = min(grid.rows - 1, math.ceil((ymax - grid.origin_y) / grid.resolution))
        if x0 > x1 or y0 > y1:
            continue
        xs = np.arange(x0, x1 + 1)
        for row in range(y0, y1 + 1, 64):
            ys = np.arange(row, min(row + 64, y1 + 1))
            mask = intersects_xy(
                shape,
                grid.origin_x + xs[None, :] * grid.resolution,
                grid.origin_y + ys[:, None] * grid.resolution,
            )
            iy, ix = np.nonzero(mask)
            cells.update((layer, int(ys[y]), int(xs[x])) for y, x in zip(iy, ix, strict=True))
    if not cells:
        yield
        return

    indices = tuple(np.asarray(axis) for axis in zip(*sorted(cells), strict=True))
    fields = {
        "_net": 0,
        "_blocked": True,
        "_is_obstacle": True,
        "_pad_blocked": True,
        "_usage_count": 0,
    }
    cpp = getattr(grid, "_cpp_grid", None)
    cpp_fields = {
        "net": 0,
        "blocked": True,
        "is_obstacle": True,
        "pad_blocked": True,
        "static_blocked": True,
        "usage_count": 0,
    }
    cpp_saved = []
    with grid.locked():
        saved = {field: getattr(grid, field)[indices].copy() for field in fields}
        try:
            for field, value in fields.items():
                getattr(grid, field)[indices] = value
            if cpp is not None:
                for layer, y, x in sorted(cells):
                    cell = cpp._impl.at(x, y, layer)
                    cpp_saved.append((cell, {field: getattr(cell, field) for field in cpp_fields}))
                    for field, value in cpp_fields.items():
                        setattr(cell, field, value)
            grid.bump_occupancy_generation()
            yield
        finally:
            for field, values in saved.items():
                getattr(grid, field)[indices] = values
            for cell, values in cpp_saved:
                for field, value in values.items():
                    setattr(cell, field, value)
            grid.bump_occupancy_generation()
