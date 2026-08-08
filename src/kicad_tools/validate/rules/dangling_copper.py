"""Dangling copper detection (``track_dangling`` / ``via_dangling``).

``kicad-cli pcb drc`` reports two connectivity-shape rule classes that
``kct check`` historically had no detection for at all (Issue #4680, the
same gap shape as #4612's ``silk_overlap``):

* ``track_dangling`` -- a trace with a free end: nothing conductive
  (another track's copper, a via barrel, a pad, or a same-net zone fill)
  contains the endpoint.  Dangling stubs on clock/oscillator nets are
  unterminated antennas; on any net they indicate an incomplete or
  abandoned route.
* ``via_dangling`` -- a via that bonds copper on fewer than two layers
  (KiCad's "Via is not connected or connected on only one layer").  The
  0-layer case (a via touching nothing at all) is the degenerate form of
  the same predicate.

Both mirror KiCad's native semantics and default severity (**warning**,
not error) -- these are routing-quality advisories, not fab-blocking
copper defects, so they must not turn a previously-passing gate red.

Detection model
---------------

KiCad's connectivity engine is *geometric*: an endpoint anchor is
connected when some other item's copper contains the anchor point
(``BOARD_CONNECTED_ITEM::HitTest``).  This rule builds the same
predicate on the repo's committed-copper source of truth:

* **Track endpoints**: an endpoint is terminated when any other copper
  item on the segment's layer -- another segment's copper (endpoint cap
  or body), a track arc, a via barrel spanning the layer, a pad, or a
  **same-net** zone ``filled_polygon`` -- lies within
  :data:`~kicad_tools.validate.rules.base.DRC_TOLERANCE` of the point.
  One violation is emitted per dangling *track* (at the first dangling
  endpoint), matching kicad-cli's per-track reporting so counts agree.
* **Vias**: for every copper layer the barrel spans
  (:func:`~kicad_tools.core.layers.via_spans_layer` -- a through via is
  copper on ALL intermediate layers), the layer counts as bonded when
  any segment / arc / pad / other via / same-net fill copper comes
  within ``via radius`` of the via center.  Fewer than two bonded
  layers -> ``via_dangling``.

Zone fills participate with a **same-net** restriction (a track ending
inside a *foreign* pour is a short, not a termination -- the clearance
rules own that case).  Fill copper comes from
:func:`~kicad_tools.validate.rules.clearance._collect_zone_fills`, i.e.
the committed ``filled_polygon`` data (issues #3482/#3523/#3527): when a
zone was never filled there is no committed copper, so a stub "ending"
in an unfilled zone outline is correctly reported as dangling
(``ZoneFillRule`` separately warns about the unfilled zone itself).

Known divergences from kicad-cli (documented per the #4680 acceptance
criteria):

* Track arcs (``(arc ...)`` board nodes) are consumed as *termination
  candidates* -- approximated by their start-mid-end chords so the
  exact arc endpoints bond correctly -- but are not themselves tested
  for dangling ends (the schema does not model track arcs as
  first-class copper primitives).  A dangling arc kicad-cli flags is
  therefore not reported here.
* KiCad's "free via" / locked-item exemptions are not modeled.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.core.layers import via_spans_layer

from ..violations import DRCResults, DRCViolation
from .base import DRC_TOLERANCE, DRCRule
from .clearance import _collect_zone_fills, _pad_on_layer, _pad_polygon

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Segment

# An endpoint / barrel counts as bonded when copper lies within this
# distance (mm).  Same float-noise guard band as the clearance rules --
# see the DRC_TOLERANCE rationale in ``base.py``.
_TOUCH_TOL_MM = DRC_TOLERANCE

# Minimum bonded copper layers for a via to be considered connected
# (KiCad: "Via is not connected or connected on only one layer").
_VIA_MIN_BONDED_LAYERS = 2


@dataclass
class _CopperItem:
    """One candidate copper feature on a single layer.

    ``key`` carries ``id()`` of the source schema object so a segment /
    via never terminates itself; ``None`` for fills and arcs (which are
    candidates only, never subjects).
    """

    kind: str  # "segment" | "arc" | "pad" | "via" | "fill"
    geom: Any  # shapely copper outline in board-relative coordinates
    net_number: int
    net_name: str
    key: int | None = None


@dataclass
class _TrackArc:
    """A board-level ``(arc ...)`` track arc scraped from the raw tree."""

    points: list[tuple[float, float]]  # start, mid, end (board-relative)
    width: float
    layer: str
    net_number: int
    net_name: str


class _LayerIndex:
    """STRtree over one layer's copper items with a candidate query."""

    def __init__(self, items: list[_CopperItem]) -> None:
        from shapely.strtree import STRtree  # type: ignore[import-untyped]

        self.items = items
        self._tree = STRtree([item.geom for item in items]) if items else None

    def candidates(self, query_geom: Any) -> list[_CopperItem]:
        if self._tree is None:
            return []
        return [self.items[i] for i in self._tree.query(query_geom)]


@dataclass
class ClusterSeed:
    """A geometry whose transitive copper cluster is being interrogated.

    Used by :func:`cluster_copper_kinds`.  ``isolated_copper`` seeds one
    per committed zone-fill island; the seed itself participates in the
    graph as a ``"fill"`` node, so seed-seed contact (two touching
    islands) merges their clusters like any other copper contact.
    """

    layer: str
    geom: Any  # shapely geometry in board-relative coordinates
    net_number: int
    net_name: str


def copper_nets_match(a_number: int, a_name: str, b_number: int, b_name: str) -> bool:
    """Same-net test between two copper features.

    Prefer resolved net numbers; fall back to name equality for the
    KiCad-10 name-only dialect.  A feature carrying **no** net identity
    at all (number 0, empty name) never matches -- deliberately
    conservative: an over-eager union silently *suppresses*
    ``isolated_copper`` findings (see #4729), which is the failure mode
    that is invisible to a green test suite.
    """
    if a_number and b_number:
        return a_number == b_number
    if a_name and b_name:
        return a_name == b_name
    return False


def _tolerance_probe(geom: Any) -> Any:
    """Bounding box of ``geom`` expanded by :data:`DRC_TOLERANCE`.

    STRtree queries are pure bbox tests, so copper within tolerance of a
    geometry but with a disjoint bbox would otherwise be missed.
    """
    from shapely import box  # type: ignore[import-untyped]

    minx, miny, maxx, maxy = geom.bounds
    return box(
        minx - DRC_TOLERANCE,
        miny - DRC_TOLERANCE,
        maxx + DRC_TOLERANCE,
        maxy + DRC_TOLERANCE,
    )


def cluster_copper_kinds(
    indexes: dict[str, _LayerIndex],
    seeds: list[ClusterSeed],
) -> list[frozenset[str]]:
    """Transitive copper-connectivity clustering (Issue #4729).

    Builds KiCad's actual connectivity model over the committed-copper
    geometry this module already indexes: **every copper item is a
    conductor**, not a one-hop anchor.  Nodes are the ``seeds`` plus the
    same-net items of ``indexes``; edges are

    * **same-layer geometric touch** -- two same-net nodes whose copper
      lies within :data:`DRC_TOLERANCE` of each other (STRtree-accelerated
      with a tolerance-expanded bbox probe), and
    * **cross-layer identity** -- a via barrel (and a through-hole
      ``*.Cu`` pad) is indexed once per spanned layer with a shared
      ``key``; all instances of one ``key`` are unioned, which is what
      bridges a cluster between layers.

    Args:
        indexes: Per-layer copper indexes from
            :func:`build_copper_layer_indexes`.  For ``isolated_copper``
            these are built with ``include_fills=False``: fill copper
            enters the graph as *seeds*, so indexing it again would
            duplicate every island as a second node.
        seeds: The geometries to interrogate.

    Returns:
        One ``frozenset`` of ``_CopperItem.kind`` values per seed (in
        seed order) naming every kind present in that seed's cluster.
        The seed's own ``"fill"`` kind is always a member.  Callers ask
        set-membership questions of it -- ``isolated_copper``'s predicate
        is exactly ``"pad" in kinds``.
    """
    from shapely.strtree import STRtree

    if not seeds:
        return []

    # Only items sharing a seed's net identity can ever reach a seed
    # (every edge requires a same-net match), so filtering here is
    # semantics-preserving and keeps the graph small on real boards.
    seed_numbers = {seed.net_number for seed in seeds if seed.net_number}
    seed_names = {seed.net_name for seed in seeds if seed.net_name}

    layers: list[str] = []
    geoms: list[Any] = []
    numbers: list[int] = []
    names: list[str] = []
    kinds: list[str] = []
    keys: list[int | None] = []

    for seed in seeds:
        layers.append(seed.layer)
        geoms.append(seed.geom)
        numbers.append(seed.net_number)
        names.append(seed.net_name)
        kinds.append("fill")
        keys.append(None)

    for layer, index in indexes.items():
        for item in index.items:
            if not (
                (item.net_number and item.net_number in seed_numbers)
                or (item.net_name and item.net_name in seed_names)
            ):
                continue
            layers.append(layer)
            geoms.append(item.geom)
            numbers.append(item.net_number)
            names.append(item.net_name)
            kinds.append(item.kind)
            keys.append(item.key)

    parent = list(range(len(geoms)))

    def find(i: int) -> int:
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    def union(a: int, b: int) -> None:
        ra, rb = find(a), find(b)
        if ra != rb:
            parent[rb] = ra

    by_layer: dict[str, list[int]] = {}
    for node, layer_name in enumerate(layers):
        by_layer.setdefault(layer_name, []).append(node)

    for indices in by_layer.values():
        tree = STRtree([geoms[i] for i in indices])
        for i in indices:
            for other_pos in tree.query(_tolerance_probe(geoms[i])):
                j = indices[int(other_pos)]
                if j <= i:
                    continue
                if not copper_nets_match(numbers[i], names[i], numbers[j], names[j]):
                    continue
                if geoms[i].distance(geoms[j]) <= DRC_TOLERANCE:
                    union(i, j)

    # Cross-layer bridging: a via / through-hole pad appears once per
    # spanned layer under one shared ``key``.
    by_key: dict[int, list[int]] = {}
    for node, key in enumerate(keys):
        if key is None:
            continue
        by_key.setdefault(key, []).append(node)
    for group in by_key.values():
        for node in group[1:]:
            union(group[0], node)

    kinds_by_root: dict[int, set[str]] = {}
    for node in range(len(geoms)):
        kinds_by_root.setdefault(find(node), set()).add(kinds[node])

    return [frozenset(kinds_by_root[find(node)]) for node in range(len(seeds))]


def _fill_net_matches(net_number: int, net_name: str, fill_item: _CopperItem) -> bool:
    """Same-net test between a track/via and a zone-fill copper item.

    Prefer resolved net numbers; fall back to name equality for the
    KiCad-10 name-only dialect.  An item with no net identity at all
    (number 0, empty name) accepts any fill geometrically -- there is no
    net to mismatch, and KiCad's own connectivity is purely geometric.
    """
    if net_number and fill_item.net_number:
        return net_number == fill_item.net_number
    if net_name and fill_item.net_name:
        return net_name == fill_item.net_name
    return not net_number and not net_name


def _collect_track_arcs(pcb: PCB) -> list[_TrackArc]:
    """Scrape board-level ``(arc ...)`` track arcs from the raw tree.

    The schema does not model track arcs as first-class primitives (only
    ``gr_arc`` graphics), yet their copper terminates track ends on
    hand-routed boards.  The raw S-expression tree stores sheet-absolute
    coordinates, so ``board_origin`` is subtracted to land in the same
    board-relative frame as ``Segment.start`` / ``Via.position`` (see
    ``PCB._detect_board_origin``).  Any parse irregularity degrades to
    "no arcs" -- candidates only, never a false violation source.
    """
    sexp = getattr(pcb, "_sexp", None)
    if sexp is None:
        return []
    ox, oy = getattr(pcb, "board_origin", (0.0, 0.0))
    arcs: list[_TrackArc] = []
    try:
        children = list(sexp.iter_children())
    except Exception:  # pragma: no cover - defensive against test doubles
        return []
    for child in children:
        if getattr(child, "tag", None) != "arc":
            continue
        points: list[tuple[float, float]] = []
        for node_name in ("start", "mid", "end"):
            node = child.find(node_name)
            if node is None:
                break
            points.append(((node.get_float(0) or 0.0) - ox, (node.get_float(1) or 0.0) - oy))
        if len(points) != 3:
            continue
        width = 0.0
        if width_node := child.find("width"):
            width = width_node.get_float(0) or 0.0
        layer = ""
        if layer_node := child.find("layer"):
            layer = layer_node.get_string(0) or ""
        net_number = 0
        net_name = ""
        if net_node := child.find("net"):
            first_int = net_node.get_int(0)
            if first_int is not None:
                net_number = first_int
                net_name = net_node.get_string(1) or ""
            else:
                net_name = net_node.get_string(0) or ""
        if width <= 0 or not layer:
            continue
        arcs.append(
            _TrackArc(
                points=points,
                width=width,
                layer=layer,
                net_number=net_number,
                net_name=net_name,
            )
        )
    return arcs


def _segment_copper(segment: Segment) -> Any | None:
    """Shapely copper outline of a segment (round end caps)."""
    from shapely.geometry import LineString, Point  # type: ignore[import-untyped]

    if segment.width <= 0:
        return None
    radius = segment.width / 2.0
    if segment.start == segment.end:
        return Point(segment.start).buffer(radius)
    return LineString([segment.start, segment.end]).buffer(radius)


def build_copper_layer_indexes(
    pcb: PCB,
    copper_layers: list[str],
    resolve_net: Any,
    *,
    include_fills: bool = True,
) -> dict[str, _LayerIndex]:
    """Collect every copper feature per layer and index it in an STRtree.

    Shared by :class:`DanglingCopperRule` and the ``isolated_copper``
    detector (:class:`~kicad_tools.validate.rules.zone_fill.IsolatedCopperRule`)
    so both consult identical committed-copper geometry (Issue #4680).

    Args:
        pcb: The PCB whose copper to index.
        copper_layers: Names of the board's copper layers.
        resolve_net: ``(net_number, net_name) -> int`` resolver for the
            KiCad-10 name-only net dialect.
        include_fills: When False, zone ``filled_polygon`` copper is
            excluded -- the isolated-copper detector treats fills as
            *subjects*, not anchors (an isolated island must not anchor
            another isolated island; same-net island transitivity is
            handled by that rule's own union pass).

    Returns:
        Mapping of copper-layer name -> :class:`_LayerIndex`.
    """
    from shapely.geometry import LineString, Point

    items_by_layer: dict[str, list[_CopperItem]] = {}

    def add(layer: str, item: _CopperItem) -> None:
        items_by_layer.setdefault(layer, []).append(item)

    for segment in pcb.segments:
        geom = _segment_copper(segment)
        if geom is None or not segment.layer:
            continue
        add(
            segment.layer,
            _CopperItem(
                kind="segment",
                geom=geom,
                net_number=resolve_net(segment.net_number, segment.net_name),
                net_name=segment.net_name,
                key=id(segment),
            ),
        )

    for arc in _collect_track_arcs(pcb):
        add(
            arc.layer,
            _CopperItem(
                kind="arc",
                geom=LineString(arc.points).buffer(arc.width / 2.0),
                net_number=resolve_net(arc.net_number, arc.net_name),
                net_name=arc.net_name,
            ),
        )

    for via in pcb.vias:
        radius = via.size / 2.0
        if radius <= 0:
            continue
        disc = Point(via.position).buffer(radius)
        item_net = resolve_net(via.net_number, via.net_name)
        for layer in copper_layers:
            if via_spans_layer(via.layers, layer):
                add(
                    layer,
                    _CopperItem(
                        kind="via",
                        geom=disc,
                        net_number=item_net,
                        net_name=via.net_name,
                        key=id(via),
                    ),
                )

    for footprint in pcb.footprints:
        for pad in footprint.pads:
            polygon = _pad_polygon(pad, footprint)
            if polygon is None:
                continue
            pad_net = resolve_net(pad.net_number, pad.net_name)
            for layer in copper_layers:
                if _pad_on_layer(pad, layer):
                    add(
                        layer,
                        _CopperItem(
                            kind="pad",
                            geom=polygon,
                            net_number=pad_net,
                            net_name=pad.net_name,
                            key=id(pad),
                        ),
                    )

    if include_fills:
        for layer, fills in _collect_zone_fills(pcb).items():
            for fill in fills:
                add(
                    layer,
                    _CopperItem(
                        kind="fill",
                        geom=fill.polygon,
                        net_number=fill.net_number,
                        net_name=fill.net_name,
                    ),
                )

    return {layer: _LayerIndex(items) for layer, items in items_by_layer.items()}


class DanglingCopperRule(DRCRule):
    """Flag dangling track ends and under-bonded vias.

    Emits ``track_dangling`` (one warning per track with a free end, at
    the first dangling endpoint) and ``via_dangling`` (one warning per
    via bonding copper on fewer than two layers).  See the module
    docstring for the detection model and known divergences.
    """

    rule_id = "dangling_copper"
    name = "Dangling Copper"
    description = (
        "Detects track ends terminated by no other copper and vias "
        "bonding copper on fewer than two layers (KiCad track_dangling "
        "/ via_dangling parity)."
    )

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check every track endpoint and via for dangling copper.

        Args:
            pcb: The PCB to check.
            design_rules: Design rules from the manufacturer profile.
                Kept for the common rule interface; dangling detection is
                purely topological and profile-independent.

        Returns:
            DRCResults containing ``track_dangling`` and ``via_dangling``
            warnings.
        """
        # shapely is a core dependency (issue #3824); this is a
        # correctness path with no valid pure-Python fallback.
        require_shapely("dangling copper detection")

        results = DRCResults()
        results.rules_checked = 1
        results.rules_checked_by_rule["track_dangling"] = 1
        results.rules_checked_by_rule["via_dangling"] = 1

        copper_layers = self._copper_layer_names(pcb)
        name_to_number = {net.name: net.number for net in pcb.nets.values() if net.name}

        def resolve_net(number: int, name: str) -> int:
            if number == 0 and name:
                return name_to_number.get(name, 0)
            return number

        indexes = self._build_layer_indexes(pcb, copper_layers, resolve_net)

        self._check_tracks(pcb, indexes, resolve_net, results)
        self._check_vias(pcb, copper_layers, indexes, resolve_net, results)
        return results

    # ------------------------------------------------------------------
    # Index construction
    # ------------------------------------------------------------------

    @staticmethod
    def _copper_layer_names(pcb: PCB) -> list[str]:
        """Board copper layers, falling back to the two outer layers."""
        names = [layer.name for layer in pcb.copper_layers]
        if names:
            return names
        return ["F.Cu", "B.Cu"]

    def _build_layer_indexes(
        self,
        pcb: PCB,
        copper_layers: list[str],
        resolve_net: Any,
    ) -> dict[str, _LayerIndex]:
        """Collect every copper feature per layer and index it."""
        return build_copper_layer_indexes(pcb, copper_layers, resolve_net)

    # ------------------------------------------------------------------
    # Termination predicate
    # ------------------------------------------------------------------

    @staticmethod
    def _point_terminated(
        point_xy: tuple[float, float],
        self_key: int,
        net_number: int,
        net_name: str,
        index: _LayerIndex | None,
    ) -> bool:
        """True when some *other* copper item contains ``point_xy``."""
        from shapely import box
        from shapely.geometry import Point

        if index is None:
            return False
        px, py = point_xy
        probe = box(
            px - _TOUCH_TOL_MM,
            py - _TOUCH_TOL_MM,
            px + _TOUCH_TOL_MM,
            py + _TOUCH_TOL_MM,
        )
        point = Point(px, py)
        for item in index.candidates(probe):
            if item.key is not None and item.key == self_key:
                continue
            if item.kind == "fill" and not _fill_net_matches(net_number, net_name, item):
                continue
            if item.geom.distance(point) <= _TOUCH_TOL_MM:
                return True
        return False

    # ------------------------------------------------------------------
    # track_dangling
    # ------------------------------------------------------------------

    def _check_tracks(
        self,
        pcb: PCB,
        indexes: dict[str, _LayerIndex],
        resolve_net: Any,
        results: DRCResults,
    ) -> None:
        """One ``track_dangling`` warning per track with a free end."""
        for segment in pcb.segments:
            if segment.width <= 0 or not segment.layer:
                continue
            index = indexes.get(segment.layer)
            net_number = resolve_net(segment.net_number, segment.net_name)
            endpoints = (
                [segment.start] if segment.start == segment.end else [segment.start, segment.end]
            )
            dangling = [
                point
                for point in endpoints
                if not self._point_terminated(
                    point, id(segment), net_number, segment.net_name, index
                )
            ]
            if not dangling:
                continue
            length = math.dist(segment.start, segment.end)
            net_label = segment.net_name or (f"net {net_number}" if net_number else "unassigned")
            both_ends = len(endpoints) == 2 and len(dangling) == 2
            suffix = " (both ends dangling)" if both_ends else ""
            results.add(
                DRCViolation(
                    rule_id="track_dangling",
                    severity="warning",
                    message=(
                        f"Dangling track end: Track [{net_label}] on "
                        f"{segment.layer}, length {length:.4f} mm{suffix}"
                    ),
                    location=dangling[0],
                    layer=segment.layer,
                    actual_value=length,
                    items=(f"net:{net_label}",),
                    nets=(net_label,),
                )
            )

    # ------------------------------------------------------------------
    # via_dangling
    # ------------------------------------------------------------------

    def _check_vias(
        self,
        pcb: PCB,
        copper_layers: list[str],
        indexes: dict[str, _LayerIndex],
        resolve_net: Any,
        results: DRCResults,
    ) -> None:
        """One ``via_dangling`` warning per via bonded on < 2 layers."""
        from shapely import box
        from shapely.geometry import Point

        for via in pcb.vias:
            radius = via.size / 2.0
            if radius <= 0:
                continue
            vx, vy = via.position
            center = Point(vx, vy)
            probe = box(
                vx - radius - _TOUCH_TOL_MM,
                vy - radius - _TOUCH_TOL_MM,
                vx + radius + _TOUCH_TOL_MM,
                vy + radius + _TOUCH_TOL_MM,
            )
            net_number = resolve_net(via.net_number, via.net_name)
            bonded_layers = 0
            for layer in copper_layers:
                if not via_spans_layer(via.layers, layer):
                    continue
                index = indexes.get(layer)
                if index is None:
                    continue
                for item in index.candidates(probe):
                    if item.key is not None and item.key == id(via):
                        continue
                    if item.kind == "fill" and not _fill_net_matches(
                        net_number, via.net_name, item
                    ):
                        continue
                    if item.geom.distance(center) <= radius + _TOUCH_TOL_MM:
                        bonded_layers += 1
                        break
            if bonded_layers >= _VIA_MIN_BONDED_LAYERS:
                continue
            net_label = via.net_name or (f"net {net_number}" if net_number else "unassigned")
            top = via.layers[0] if via.layers else "F.Cu"
            bottom = via.layers[-1] if via.layers else "B.Cu"
            results.add(
                DRCViolation(
                    rule_id="via_dangling",
                    severity="warning",
                    message=(
                        f"Dangling via: Via [{net_label}] on {top} - {bottom} "
                        f"bonds copper on {bonded_layers} layer(s)"
                    ),
                    location=via.position,
                    layer=top,
                    actual_value=float(bonded_layers),
                    required_value=float(_VIA_MIN_BONDED_LAYERS),
                    items=(f"net:{net_label}",),
                    nets=(net_label,),
                )
            )
