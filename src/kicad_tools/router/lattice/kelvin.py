"""Physical isolation of same-net Kelvin branches during lattice search.

Epic #5509 Phase 3d, deliberately **not** moved onto the clearance kernel.
:meth:`KelvinBranchGuard.clear` is a same-net *contact* test, not a clearance
test: it asks whether the moving conductor would touch previously-committed
root-net metal at all (a ``1e-6`` mm buffer, i.e. zero clearance), and it
answers that against a **unioned region minus the root pad's contact area**.
The kernel answers pairwise edge-to-edge gaps; a region subtraction is not
expressible as any sequence of them -- a path point may sit within the
conductor radius of root copper and still be legal because it lies inside the
contact area -- so a per-object kernel loop would change verdicts rather than
preserve them.  There is no ``required_mm`` term in this module to unify.

The clearance predicates this guard gates -- ``CommittedCopper.seg_clear`` /
``node_clear`` / ``via_clear``, which consult it before measuring anything --
are kernel-backed as of that phase.
"""

from __future__ import annotations

import math
from collections.abc import Callable, Iterable
from typing import TYPE_CHECKING, Any

from shapely import affinity  # type: ignore[import-untyped]
from shapely.geometry import LineString, Point  # type: ignore[import-untyped]
from shapely.ops import unary_union  # type: ignore[import-untyped]
from shapely.prepared import prep  # type: ignore[import-untyped]

from ..kelvin_obstacles import _pad_outline
from ..primitives import Pad
from .geometry import Pt

if TYPE_CHECKING:
    from .obstacles import CommittedCopper


# Shapely approximates circular arcs with inscribed polygons. Circumscribe
# those arcs so facets cannot admit a physical short between sample vertices.
_ARC_SCALE = 1 / math.cos(math.pi / 64)


def _outer_buffer(shape: Any, radius: float) -> Any:
    return shape.buffer(radius * _ARC_SCALE, quad_segs=16)


class KelvinBranchGuard:
    """Snapshot previous branches, permitting a join only inside root metal.

    This model uses actual committed widths. Lattice vias are through-vias,
    so their bodies occupy every layer, just as in CommittedCopper.
    """

    def __init__(
        self,
        committed: CommittedCopper,
        pads: list[Pad],
        root: Pad,
        target: Pad,
        pad_layers: Callable[[Pad], Iterable[int]],
    ) -> None:
        objects: list[list[Any]] = [[] for _ in range(committed.num_layers)]
        for layer, copper in enumerate(committed.copper):
            segments = {item for bucket in copper.buckets.values() for item in bucket}
            for a, b, net, half, _clearance in segments:
                if net == root.net:
                    objects[layer].append(_outer_buffer(LineString([a, b]), half))
        for point, net, radius, layers in committed.via_copper:
            if net == root.net:
                body = _outer_buffer(Point(point), radius)
                for layer in layers:
                    objects[layer].append(body)
        for pad in pads:
            if pad.key in (root.key, target.key):
                continue
            shape = _pad_outline(pad)
            if pad.shape in ("circle", "oval"):
                shape = affinity.scale(shape, _ARC_SCALE, _ARC_SCALE, origin=(pad.x, pad.y))
            for layer in pad_layers(pad):
                objects[layer].append(shape)
        self.copper = [unary_union(shapes) for shapes in objects]
        self.contact = _pad_outline(root, contact=True)
        self.root_layers = set(pad_layers(root))
        self._exclusions: dict[tuple[int, float], Any] = {}

    def clear(self, a: Pt, b: Pt, layer: int, radius: float) -> bool:
        """Check the complete moving conductor, including its rounded caps."""
        key = (layer, radius)
        if key not in self._exclusions:
            exclusion = _outer_buffer(self.copper[layer], radius + 1e-6)
            if layer in self.root_layers:
                exclusion = exclusion.difference(self.contact.buffer(-radius))
            self._exclusions[key] = prep(exclusion)
        path = Point(a) if a == b else LineString([a, b])
        return not self._exclusions[key].intersects(path)
