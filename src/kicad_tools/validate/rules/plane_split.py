"""Plane-split advisory: foreign tracks that cut a zone into pieces (Issue #9591).

A track from another net that crosses a plane or pour on its own layer can
split the zone into several disconnected pieces.  It is DRC-legal, and nothing
else in ``kct check`` reports it: ``isolated_copper`` only flags fill islands
holding *no* pad, and ``pour_slot`` only flags slots from parallel tracks.  The
case here is a zone cut into pieces that **each still hold pads** -- every piece
is "connected", yet the plane no longer is one.

Rule id ``plane_split`` (always ``severity="warning"``, advisory category).

Detection is geometric, on the zone *outline*, never on the committed fill
(which may be stale or empty -- an unfilled zone is judged like a filled one):

1. ``A`` = outline minus *all* foreign copper on the zone layer (tracks, arcs,
   pads, vias), each grown by the zone clearance, minus copper-pour keepouts.
2. ``B`` = outline minus only the foreign *pads, vias and keepouts* -- the
   legitimate obstacles a plane has to route around anyway.
3. A *piece* is a connected region of the result, merged with any other piece
   it shares same-net copper with (a same-net track, pad or via that touches
   both pieces re-joins them).  A piece is *pad-bearing* when a pad or via of
   the zone's net touches it.

When ``A`` has more pad-bearing pieces than ``B``, the extra splitting is due
to foreign **tracks**, and one warning is raised for the zone/layer.  Splits
caused by foreign pads, vias or keepouts alone, and cuts whose pieces carry no
extra pad (a stub of pour with nothing on it), are not reported.

Each finding names the zone net and layer, the offending track nets, and every
pad-bearing piece with the pads (``C20.1``) and vias (``via@(x, y)``) in it.

This is a geometric advisory, not a native-DRC oracle.  It does not model the
zone ``min_thickness`` (a thin neck narrower than that is dropped by KiCad's
filler, so a real fill can split slightly more than reported), other zones on
the same layer, or netclass clearances above the zone's own clearance.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kicad_tools._shapely import require_shapely
from kicad_tools.core.layers import via_spans_layer

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB, Zone

RULE_ID = "plane_split"

#: Slack (mm) added when asking whether copper touches a clearance-carved piece.
_TOUCH_TOL = 1e-6
#: Most anchors spelled out per piece in a message before ``+N more``.
_MAX_LISTED = 12


@dataclass
class _Item:
    """One copper feature on a layer."""

    kind: str  # "track" | "pad" | "via"
    geom: Any
    net_number: int
    net_name: str
    label: str = ""


@dataclass
class _Group:
    """A set of pieces joined by same-net copper."""

    geom: Any
    anchors: list[str] = field(default_factory=list)


def _layers_of(zone: Zone) -> list[str]:
    """Concrete copper layers a pour zone occupies (wildcards are skipped)."""
    names = [zone.layer, *getattr(zone, "layers", [])]
    out: list[str] = []
    for name in names:
        if name and "*" not in name and "&" not in name and name not in out:
            out.append(name)
    return out


def _keepout_covers(zone: Zone, layer: str) -> bool:
    for name in [zone.layer, *getattr(zone, "layers", [])]:
        if name == layer or name == "*.Cu" or (name == "F&B.Cu" and layer in ("F.Cu", "B.Cu")):
            return True
    return False


class PlaneSplitRule(DRCRule):
    """Report zones cut into extra pad-bearing pieces by foreign tracks."""

    rule_id = RULE_ID
    name = "Plane Split"
    description = (
        "Reports a copper zone whose outline is split into more pad-bearing "
        "pieces because of another net's tracks (advisory)."
    )

    def check(self, pcb: PCB, design_rules: DesignRules) -> DRCResults:
        require_shapely("plane split detection")

        results = DRCResults()
        results.rules_checked = 1
        results.rules_checked_by_rule[RULE_ID] = 1

        pours = [z for z in pcb.zones if getattr(z, "keepout", None) is None and z.polygon]
        if not pours:
            return results

        name_to_number = {n.name: n.number for n in pcb.nets.values() if n.name}

        def resolve(number: int, name: str) -> int:
            if number == 0 and name:
                return name_to_number.get(name, 0)
            return number

        copper_layers = [layer.name for layer in pcb.copper_layers] or ["F.Cu", "B.Cu"]
        by_layer = self._collect_items(pcb, copper_layers, resolve)
        keepouts = [z for z in pcb.zones if getattr(z, "keepout", None) is not None]
        origin = getattr(pcb, "board_origin", (0.0, 0.0))

        for zone in pours:
            net = resolve(zone.net_number, zone.net_name)
            if net == 0:
                continue
            outline = self._outline(zone)
            if outline is None:
                continue
            for layer in _layers_of(zone):
                violation = self._check_zone_layer(
                    zone, net, outline, layer, by_layer.get(layer, []), keepouts, resolve, origin
                )
                if violation is not None:
                    results.add(violation)
        return results

    # ------------------------------------------------------------------

    @staticmethod
    def _outline(zone: Zone) -> Any | None:
        from shapely.geometry import Polygon  # type: ignore[import-untyped]
        from shapely.validation import make_valid  # type: ignore[import-untyped]

        if len(zone.polygon) < 3:
            return None
        poly = Polygon(zone.polygon)
        if not poly.is_valid:
            poly = make_valid(poly)
        return None if poly.is_empty else poly

    @staticmethod
    def _collect_items(pcb: PCB, copper_layers: list[str], resolve: Any) -> dict[str, list[_Item]]:
        from shapely.geometry import LineString, Point

        from .clearance import _pad_on_layer, _pad_polygon
        from .dangling_copper import _collect_track_arcs, _segment_copper

        items: dict[str, list[_Item]] = {}

        def add(layer: str, item: _Item) -> None:
            items.setdefault(layer, []).append(item)

        for seg in pcb.segments:
            geom = _segment_copper(seg)
            if geom is None or not seg.layer:
                continue
            add(
                seg.layer,
                _Item("track", geom, resolve(seg.net_number, seg.net_name), seg.net_name),
            )

        for arc in _collect_track_arcs(pcb):
            if arc.width <= 0 or len(arc.points) < 2:
                continue
            add(
                arc.layer,
                _Item(
                    "track",
                    LineString(arc.points).buffer(arc.width / 2.0),
                    resolve(arc.net_number, arc.net_name),
                    arc.net_name,
                ),
            )

        ox, oy = getattr(pcb, "board_origin", (0.0, 0.0))
        for via in pcb.vias:
            if via.size <= 0:
                continue
            disc = Point(via.position).buffer(via.size / 2.0)
            label = f"via@({via.position[0] + ox:.2f}, {via.position[1] + oy:.2f})"
            for layer in copper_layers:
                if via_spans_layer(via.layers, layer):
                    add(
                        layer,
                        _Item(
                            "via", disc, resolve(via.net_number, via.net_name), via.net_name, label
                        ),
                    )

        for fp in pcb.footprints:
            for pad in fp.pads:
                poly = _pad_polygon(pad, fp)
                if poly is None:
                    continue
                label = f"{fp.reference or '?'}.{pad.number}"
                pad_net = resolve(pad.net_number, pad.net_name)
                for layer in copper_layers:
                    if _pad_on_layer(pad, layer):
                        add(layer, _Item("pad", poly, pad_net, pad.net_name, label))
        return items

    # ------------------------------------------------------------------

    def _check_zone_layer(
        self,
        zone: Zone,
        net: int,
        outline: Any,
        layer: str,
        layer_items: list[_Item],
        keepouts: list[Zone],
        resolve: Any,
        origin: tuple[float, float],
    ) -> DRCViolation | None:
        from shapely.ops import unary_union  # type: ignore[import-untyped]

        near = [it for it in layer_items if it.geom.intersects(outline)]
        own = [it for it in near if it.net_number == net]
        foreign = [it for it in near if it.net_number != net]
        anchors = [it for it in own if it.kind in ("pad", "via")]
        if not anchors or not any(it.kind == "track" for it in foreign):
            return None

        clearance = max(float(zone.clearance), 0.0)
        obstacles = [self._keepout_geom(k) for k in keepouts if _keepout_covers(k, layer)]
        obstacles = [g for g in obstacles if g is not None]
        fixed = [it for it in foreign if it.kind != "track"]
        tracks = [it for it in foreign if it.kind == "track"]

        fixed_geom = unary_union([*(it.geom.buffer(clearance) for it in fixed), *obstacles])
        track_geom = unary_union([it.geom.buffer(clearance) for it in tracks])
        bridges = [it for it in own if it.kind == "track"]

        base = self._groups(outline, fixed_geom, anchors, bridges)
        cut = self._groups(outline, unary_union([fixed_geom, track_geom]), anchors, bridges)
        base_n = sum(1 for g in base if g.anchors)
        pieces = [g for g in cut if g.anchors]
        if len(pieces) <= base_n:
            return None

        pieces.sort(key=lambda g: (round(g.geom.bounds[0], 6), round(g.geom.bounds[1], 6)))
        culprits = self._culprits(tracks, pieces, clearance)
        if not culprits:
            culprits = tracks
        names = sorted({t.net_name or f"net {t.net_number}" for t in culprits})
        zone_net = zone.net_name or f"net {net}"

        listing = "; ".join(
            f"piece {i}: {self._fmt(g.anchors)}" for i, g in enumerate(pieces, start=1)
        )
        centre = unary_union([t.geom for t in culprits]).centroid
        return DRCViolation(
            rule_id=RULE_ID,
            severity="warning",
            message=(
                f"Plane split: zone [{zone_net}] on {layer} is cut into {len(pieces)} "
                f"pad-bearing pieces (would be {base_n} without foreign tracks) by "
                f"track(s) of [{', '.join(names)}]; {listing}"
            ),
            location=(centre.x, centre.y),
            layer=layer,
            actual_value=float(len(pieces)),
            required_value=float(max(base_n, 1)),
            items=tuple(f"net:{n}" for n in [zone_net, *names]),
            nets=(zone_net, *names),
        )

    @staticmethod
    def _keepout_geom(zone: Zone) -> Any | None:
        keepout = getattr(zone, "keepout", None)
        if keepout is None or keepout.copperpour_allowed or len(zone.polygon) < 3:
            return None
        from shapely.geometry import Polygon
        from shapely.validation import make_valid

        poly = Polygon(zone.polygon)
        return poly if poly.is_valid else make_valid(poly)

    @staticmethod
    def _fmt(labels: list[str]) -> str:
        ordered = sorted(labels)
        shown = ", ".join(ordered[:_MAX_LISTED])
        extra = len(ordered) - _MAX_LISTED
        return f"{shown}, +{extra} more" if extra > 0 else shown

    @staticmethod
    def _groups(
        outline: Any, obstacles: Any, anchors: list[_Item], bridges: list[_Item]
    ) -> list[_Group]:
        """Pieces of ``outline - obstacles`` merged through same-net copper."""
        from shapely.ops import unary_union

        remaining = outline.difference(obstacles)
        pieces = [g for g in getattr(remaining, "geoms", [remaining]) if g.geom_type == "Polygon"]
        pieces = [p for p in pieces if not p.is_empty]
        parent = list(range(len(pieces)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def touching(item: _Item) -> list[int]:
            probe = item.geom.buffer(_TOUCH_TOL)
            return [i for i, p in enumerate(pieces) if p.intersects(probe)]

        for item in [*anchors, *bridges]:
            hit = touching(item)
            for i in hit[1:]:
                parent[find(i)] = find(hit[0])

        members: dict[int, list[int]] = {}
        for i in range(len(pieces)):
            members.setdefault(find(i), []).append(i)
        groups = {
            root: _Group(unary_union([pieces[i] for i in idx])) for root, idx in members.items()
        }
        for item in anchors:
            hit = touching(item)
            if hit:
                groups[find(hit[0])].anchors.append(item.label)
        return list(groups.values())

    @staticmethod
    def _culprits(tracks: list[_Item], pieces: list[_Group], clearance: float) -> list[_Item]:
        """Foreign tracks that touch two or more pad-bearing pieces (they cut
        between them); empty when no single track does."""
        out = []
        for track in tracks:
            probe = track.geom.buffer(_TOUCH_TOL)
            hits = sum(1 for g in pieces if g.geom.buffer(clearance + _TOUCH_TOL).intersects(probe))
            if hits >= 2:
                out.append(track)
        return out
