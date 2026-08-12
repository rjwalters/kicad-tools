"""Net-id-space pairwise (HV) clearance projection for the lattice engine.

Issue #4602 (epic #4431): the lattice search consumes
:attr:`~kicad_tools.router.rules.DesignRules.pairwise_clearance` so HV<->LV
proximity is *avoided* during A*, not merely reported by the #4588
post-route gate.

The lattice pathfinder is deliberately geometry-only -- it works in integer
net ids and never sees net names or net classes (#4597 discipline).  The
name-keyed :class:`~kicad_tools.router.pairwise_clearance.PairwiseClearanceTable`
and the #4506 :class:`~kicad_tools.router.pairwise_clearance.AttachZone`
tuples are therefore projected ONCE by the caller (``Autorouter``, which owns
the ``net_names`` map) into this flat id-space carrier, mirroring the #4510
``build_cpp_domain_matrix`` / ``attach_zones_to_net_ids`` precedent for the
C++ grid.

Dormancy contract: when no ``--voltage-map`` was supplied the projection is
``None`` and every lattice predicate runs its pre-#4602 code byte-for-byte.
:func:`build_lattice_pairwise` also returns ``None`` when nothing resolves
to a widening pair on this board, keeping the fast path for such runs.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Mapping, Sequence

from ..pairwise_clearance import closest_gap_midpoint_xy
from .geometry import Pt

if TYPE_CHECKING:  # pragma: no cover - typing only
    from ..pairwise_clearance import AttachZone, PairwiseClearanceTable

# A projected attach zone: sheet-absolute bbox, the net ids it may exempt, and
# (issue #4699) the lattice layer indices each of those nets has PAD copper on.
# The trailing element is optional -- ``None`` (or a 5-tuple, as older callers
# and hand-built test fixtures produce) means the pre-#4699 layer-agnostic
# verdict, and a net absent from the map is unrestricted.
ZoneLayers = Mapping[int, frozenset[int]]
ZoneIds = tuple[float, float, float, float, frozenset[int], "ZoneLayers | None"]


@dataclass(frozen=True)
class LatticePairwise:
    """Order-independent net-id pair -> required edge-to-edge clearance (mm).

    Attributes:
        required_by_pair: ``{(lo_id, hi_id): required_mm}`` -- only pairs that
            genuinely need HV widening (values are the raw creepage
            requirement; the scalar path still governs via ``max``).
        zones: #4506 rated-footprint attach zones projected to net ids
            (sheet-absolute bboxes, same space as the lattice).
        max_by_net: per-net-id maximum requirement across every pair the net
            participates in -- the spatial-query inflation bound (the #4511
            "search-radius trap": a ~3 mm requirement vastly exceeds the DRU
            scalar, so un-inflated windows silently miss far-but-forbidden
            copper).  Nets absent from the map need no inflation at all.
    """

    required_by_pair: Mapping[tuple[int, int], float]
    zones: tuple[ZoneIds, ...]
    max_by_net: Mapping[int, float]

    def __post_init__(self) -> None:
        # Accept the pre-#4699 5-tuple zone shape (hand-built fixtures, older
        # callers) by padding it with the layer-agnostic ``None``.
        if any(len(zone) < 6 for zone in self.zones):
            object.__setattr__(
                self,
                "zones",
                tuple(zone if len(zone) >= 6 else (*zone, None) for zone in self.zones),
            )

    def required(self, net_a: int, net_b: int) -> float:
        """Raw pairwise requirement for an id pair; 0.0 when unmapped."""
        if net_a == net_b:
            return 0.0
        key = (net_a, net_b) if net_a < net_b else (net_b, net_a)
        return self.required_by_pair.get(key, 0.0)

    def max_required_for(self, net: int) -> float:
        """Widest requirement ``net`` participates in; 0.0 when unmapped."""
        return self.max_by_net.get(net, 0.0)

    def exempt(self, x: float, y: float, net_a: int, net_b: int, layer: int | None = None) -> bool:
        """True when ``(x, y)`` lies in a zone covering BOTH net ids (#4506).

        Mirrors :meth:`AttachZone.exempts` so the search-time verdict and the
        #4588 post-route gate agree by construction -- including the #4699
        layer scoping: a zone waives a pair on ``layer`` only when both nets
        have pad copper there.  ``layer is None`` keeps the layer-agnostic
        verdict (via-to-via, where no single layer applies).  Callers probe the
        closest-gap midpoint, exactly as ``segment_pair_violation`` does.
        """
        for x0, y0, x1, y1, ids, layers in self.zones:
            if not (x0 <= x <= x1 and y0 <= y <= y1 and net_a in ids and net_b in ids):
                continue
            if layer is None or not layers:
                return True
            a_layers = layers.get(net_a)
            b_layers = layers.get(net_b)
            if (a_layers is None or layer in a_layers) and (b_layers is None or layer in b_layers):
                return True
        return False

    # -- probe-point helpers (closest-gap midpoints, gate-compatible) -------

    def exempt_seg_seg(
        self, a: Pt, b: Pt, c: Pt, d: Pt, net_a: int, net_b: int, layer: int | None = None
    ) -> bool:
        """Zone exemption probed at the two segments' closest-gap midpoint."""
        if not self.zones:
            return False
        x, y = closest_gap_midpoint_xy(a[0], a[1], b[0], b[1], c[0], c[1], d[0], d[1])
        return self.exempt(x, y, net_a, net_b, layer)

    def exempt_seg_pt(
        self, a: Pt, b: Pt, p: Pt, net_a: int, net_b: int, layer: int | None = None
    ) -> bool:
        """Zone exemption probed midway between segment ``a-b`` and point ``p``."""
        if not self.zones:
            return False
        ax, ay = a
        dx, dy = b[0] - ax, b[1] - ay
        length2 = dx * dx + dy * dy
        if length2 <= 1e-18:
            cx, cy = ax, ay
        else:
            t = ((p[0] - ax) * dx + (p[1] - ay) * dy) / length2
            t = max(0.0, min(1.0, t))
            cx, cy = ax + t * dx, ay + t * dy
        return self.exempt((cx + p[0]) / 2.0, (cy + p[1]) / 2.0, net_a, net_b, layer)

    def exempt_pt_pt(self, p: Pt, q: Pt, net_a: int, net_b: int, layer: int | None = None) -> bool:
        """Zone exemption probed at the midpoint of two point sites."""
        if not self.zones:
            return False
        return self.exempt((p[0] + q[0]) / 2.0, (p[1] + q[1]) / 2.0, net_a, net_b, layer)


def build_lattice_pairwise(
    table: PairwiseClearanceTable | None,
    attach_zones: Sequence[AttachZone],
    net_name_to_id: Mapping[str, int],
    layer_indices: Mapping[str, int] | None = None,
) -> LatticePairwise | None:
    """Project a name-keyed pairwise table onto integer net ids (#4602).

    The projection shape follows :func:`~kicad_tools.router.pairwise_clearance.
    build_cpp_domain_matrix`: a board net name may carry a leading ``/`` while
    the table keys never do, and two distinct board nets can in principle
    normalise to the same key, so every matching id joins the pair.  Zones are
    translated through :func:`~kicad_tools.router.pairwise_clearance.
    attach_zones_to_net_ids` (they must already be sheet-absolute -- the
    ``_pairwise_attach_zones`` resolver's output, board-origin shift included
    per PR #4603).

    ``layer_indices`` (issue #4699) maps KiCad copper-layer names to lattice
    layer indices so each zone's per-net pad layers project into the lattice's
    integer layer space; the search's exemption then matches the #4588 gate's
    layer scoping by construction.  ``None`` (or an unresolvable layer) leaves
    that zone layer-agnostic, i.e. exactly the pre-#4699 verdict.

    Returns ``None`` when nothing needs widening on this board (no table, an
    empty matrix, or no pair whose BOTH nets resolve to board net ids) -- the
    dormant signal that keeps every lattice predicate on its scalar path.
    """
    if table is None:
        return None
    pairs = table.required_by_pair
    if not pairs:
        return None

    from ..pairwise_clearance import _norm_net_key

    ids_by_key: dict[str, list[int]] = {}
    for name, net_id in net_name_to_id.items():
        if int(net_id) >= 0:
            ids_by_key.setdefault(_norm_net_key(name), []).append(int(net_id))

    required_by_pair: dict[tuple[int, int], float] = {}
    max_by_net: dict[int, float] = {}
    for (name_a, name_b), required in pairs.items():
        if required <= 0.0:
            continue
        for id_a in ids_by_key.get(name_a, ()):
            for id_b in ids_by_key.get(name_b, ()):
                if id_a == id_b:
                    continue
                key = (id_a, id_b) if id_a < id_b else (id_b, id_a)
                value = max(required_by_pair.get(key, 0.0), float(required))
                required_by_pair[key] = value
                max_by_net[id_a] = max(max_by_net.get(id_a, 0.0), value)
                max_by_net[id_b] = max(max_by_net.get(id_b, 0.0), value)
    if not required_by_pair:
        return None

    zones: list[ZoneIds] = []
    for zone in attach_zones:
        net_ids = sorted({nid for key in zone.net_names for nid in ids_by_key.get(key, ())})
        if len(net_ids) < 2:
            # Cannot exempt any pair (mirrors ``attach_zones_to_net_ids``).
            continue
        zones.append(
            (
                zone.min_x,
                zone.min_y,
                zone.max_x,
                zone.max_y,
                frozenset(net_ids),
                _project_zone_layers(zone, ids_by_key, layer_indices),
            )
        )
    return LatticePairwise(
        required_by_pair=required_by_pair,
        zones=tuple(zones),
        max_by_net=max_by_net,
    )


def _project_zone_layers(
    zone: AttachZone,
    ids_by_key: Mapping[str, list[int]],
    layer_indices: Mapping[str, int] | None,
) -> ZoneLayers | None:
    """Project a zone's per-net pad layers into lattice layer indices (#4699).

    Thin delegation to the shared
    :func:`~kicad_tools.router.pairwise_clearance.project_zone_layers` helper
    (issue #4507 lifted the body there): the lattice, the C++ ``Grid3D`` and the
    #4588 gate must all read the SAME layer scoping, so there is exactly one
    implementation of it.  ``None`` is the layer-agnostic verdict.
    """
    from ..pairwise_clearance import project_zone_layers

    return project_zone_layers(zone, ids_by_key, layer_indices)
