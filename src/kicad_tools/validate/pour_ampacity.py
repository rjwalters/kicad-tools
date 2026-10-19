"""Conservative ampacity of copper pours (Issue #7206, follow-up F3).

``AmpacityRule`` rates track segments.  A net whose declared current flows
through a copper pour -- only a pour, or a pour plus tracks -- also needs
the pour rated, or a necked pour would read as a pass.  This module
estimates it from the committed fill geometry.

Method
======

The pour is sized by its **narrowest neck along the current path between
the net's pads**.  For each copper layer ``L`` the net's own copper there
(its zone fills plus its pad outlines) is eroded by ``w_L / 2``, where
``w_L`` is the IPC-2221 width the track check would require on ``L`` for
the declared current
(:func:`kicad_tools.physics.ampacity.width_for_current`, with the same
``outer_copper_oz`` / ``inner_copper_oz`` external/internal split that
:class:`~kicad_tools.validate.rules.ampacity.AmpacityRule` uses).  What
survives the erosion is copper through which a channel at least ``w_L`` wide
runs.  The net passes when that surviving copper still joins **every** pad
of the net.  The pad may be joined directly or through the net's tracks and
vias, which act as bridges.  Their own widths are ``AmpacityRule``'s job,
and via barrels are not rated, the same as for tracks.

The reported neck is found by bisecting the erosion scale: the widest
``s * w_L`` that still joins every pad.

This is conservative by construction:

* every pad must be reached at full width, including pads that carry little
  of the current, such as a decoupling capacitor on a thermal spoke;
* parallel paths are never added together.  Two 0.3 mm spokes in parallel
  count as 0.3 mm, not 0.6 mm;
* the IPC-2221 trace formula is applied to the neck as if it were a long
  trace.  A short neck between two wide pours runs cooler than that.

Fail closed
===========

When no defensible estimate exists the net is **unmeasured**, and
``kct check`` reports ``ampacity`` as a blocking ``unknown``.  A pour is
never assumed to be wide:

* shapely is unavailable;
* a copper zone of the net has no committed fill;
* the net has fewer than two copper pads;
* a layer of the pour has no copper weight (an internal layer on a
  stackup with ``inner_copper_oz <= 0``);
* the net's copper, as modelled here, does not join all its pads even
  before any erosion.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from kicad_tools.physics.ampacity import width_for_current

from .violations import DRCViolation

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.schema.pcb import PCB

# Copper that meets within this distance (mm) touches; matches the 2.5 um
# edge-contact tolerance of :mod:`kicad_tools.validate.copper_contact`.
_TOUCH = 0.0025
# Smallest erosion (mm): "is the copper connected at all" probe.
_R0 = 1e-4
# A channel exactly at the required width passes (``>=``, like tracks).
_PASS_SLACK = 1e-4
# Bisection steps for the reported neck (scale resolution ~ 2**-12).
_BISECT_STEPS = 12
_EXTERNAL_LAYERS = ("F.Cu", "B.Cu")


@dataclass
class PourAmpacityResult:
    """Outcome of rating every declared net that has pour copper."""

    violations: list[DRCViolation] = field(default_factory=list)
    unmeasured: dict[str, str] = field(default_factory=dict)
    necks_mm: dict[str, float] = field(default_factory=dict)


@dataclass
class _Node:
    kind: str  # "pad" | "track" | "via"
    label: str
    layers: frozenset[str]
    geom: dict[str, Any]  # layer -> shapely geometry (pad outline / centreline / via disk)
    reach: float = 0.0  # half copper width beyond ``geom`` (tracks)
    location: tuple[float, float] = (0.0, 0.0)


def _same_net(obj: Any, net_name: str, net_number: int) -> bool:
    name = getattr(obj, "net_name", "") or ""
    if name:
        return bool(name == net_name)
    number = getattr(obj, "net_number", 0) or 0
    return bool(net_number and number == net_number)


def _board_copper_layers(pcb: Any) -> list[str]:
    try:
        names = [layer.name for layer in pcb.copper_layers]
    except (AttributeError, TypeError):
        names = []
    return names or ["F.Cu", "B.Cu"]


def _span(layers: list[str], board_layers: list[str]) -> frozenset[str]:
    """Copper layers a via spans (through: all; blind/buried: the range)."""
    named = [layer for layer in layers if layer in board_layers]
    if any(layer in ("*.Cu", "F&B.Cu") for layer in layers) or len(named) < 2:
        return frozenset(named or board_layers)
    i, j = sorted(board_layers.index(n) for n in named[:2])
    return frozenset(board_layers[i : j + 1])


def _required_width(current_a: float, layer: str, rules: DesignRules) -> float | None:
    if layer in _EXTERNAL_LAYERS:
        oz = rules.outer_copper_oz
        kind = "external"
    else:
        oz = rules.inner_copper_oz
        kind = "internal"
    if oz is None or oz <= 0:
        return None
    return float(width_for_current(current_a, copper_weight_oz=oz, layer=kind))


class _NetModel:
    """One net's copper: per-layer pour+pad regions and bridge nodes."""

    def __init__(self, regions: dict[str, Any], nodes: list[_Node]) -> None:
        self.regions = regions
        self.nodes = nodes
        self.pads = [i for i, n in enumerate(nodes) if n.kind == "pad"]
        # Bridges touching each other or a pad directly (track-track,
        # track-pad, via-track, via-pad) on a shared layer.  Independent of
        # the erosion, so computed once.
        self.bridge_pairs: list[tuple[int, int]] = []
        for i, a in enumerate(nodes):
            for j in range(i + 1, len(nodes)):
                b = nodes[j]
                if a.kind == "pad" and b.kind == "pad":
                    continue  # pads join only through copper between them
                for layer in a.layers & b.layers:
                    if a.geom[layer].distance(b.geom[layer]) <= a.reach + b.reach + _TOUCH:
                        self.bridge_pairs.append((i, j))
                        break

    def groups(self, radii: dict[str, float]) -> list[set[int]]:
        """Pad-index groups joined by copper eroded by ``radii[layer]``."""
        parent = list(range(len(self.nodes)))

        def find(i: int) -> int:
            while parent[i] != i:
                parent[i] = parent[parent[i]]
                i = parent[i]
            return i

        def union(a: int, b: int) -> None:
            ra, rb = find(a), find(b)
            if ra != rb:
                parent[ra] = rb

        for layer, region in self.regions.items():
            r = radii[layer]
            eroded = region.buffer(-r) if r > 0 else region
            parts = list(getattr(eroded, "geoms", [eroded])) if not eroded.is_empty else []
            for part in parts:
                if part.is_empty:
                    continue
                touching = [
                    i
                    for i, node in enumerate(self.nodes)
                    if layer in node.layers
                    and part.distance(node.geom[layer]) <= r + node.reach + _TOUCH
                ]
                for a, b in zip(touching, touching[1:], strict=False):
                    union(a, b)

        for a, b in self.bridge_pairs:
            union(a, b)

        by_root: dict[int, set[int]] = {}
        for p in self.pads:
            by_root.setdefault(find(p), set()).add(p)
        return list(by_root.values())

    def joined(self, radii: dict[str, float]) -> bool:
        return len(self.groups(radii)) <= 1


def _build_model(
    pcb: Any, net_name: str, net_number: int, board_layers: list[str]
) -> tuple[_NetModel | None, str | None]:
    from shapely.geometry import LineString, Point  # type: ignore[import-untyped]
    from shapely.ops import unary_union  # type: ignore[import-untyped]

    from .rules.clearance import _collect_zone_fills, _pad_polygon
    from .rules.thermal_asymmetry import pad_copper_layers

    zones = list(getattr(pcb, "zones", []) or [])
    fills: dict[str, list[Any]] = {}
    for layer, layer_fills in _collect_zone_fills(pcb).items():
        for fill in layer_fills:
            zone = zones[fill.source_zone_index] if fill.source_zone_index >= 0 else None
            if zone is None or getattr(zone, "keepout", None) is not None:
                continue
            if not _same_net(fill, net_name, net_number):
                continue
            polygon: Any = fill.polygon
            inflation = zone.fill_inflation()
            if inflation > 0:
                polygon = polygon.buffer(inflation)
            fills.setdefault(layer, []).append(polygon)

    nodes: list[_Node] = []
    pad_shapes: dict[str, list[Any]] = {}
    for fp in getattr(pcb, "footprints", []) or []:
        for pad in getattr(fp, "pads", []) or []:
            if not _same_net(pad, net_name, net_number):
                continue
            layers = pad_copper_layers(pad, board_layers)
            if not layers:
                continue
            outline = _pad_polygon(pad, fp)
            if outline is None or outline.is_empty:
                return None, f"pad {fp.reference}.{pad.number} has no copper outline"
            for layer in layers:
                pad_shapes.setdefault(layer, []).append(outline)
            c = outline.centroid
            nodes.append(
                _Node(
                    kind="pad",
                    label=f"{fp.reference}.{pad.number}",
                    layers=frozenset(layers),
                    geom=dict.fromkeys(layers, outline),
                    location=(round(c.x, 3), round(c.y, 3)),
                )
            )
    if len(nodes) < 2:
        return None, f"{len(nodes)} copper pad(s); no current path to rate"

    tracks = [s for s in getattr(pcb, "segments", []) or [] if _same_net(s, net_name, net_number)]
    tracks += [a for a in getattr(pcb, "arcs", []) or [] if _same_net(a, net_name, net_number)]
    for t in tracks:
        layer = getattr(t, "layer", "")
        if layer not in board_layers:
            continue
        pts = [t.start, t.mid, t.end] if hasattr(t, "mid") else [t.start, t.end]
        line = LineString(pts) if pts[0] != pts[-1] else Point(pts[0])
        nodes.append(
            _Node(
                kind="track",
                label="track",
                layers=frozenset({layer}),
                geom={layer: line},
                reach=max(float(getattr(t, "width", 0.0) or 0.0), 0.0) / 2.0,
            )
        )
    for v in getattr(pcb, "vias", []) or []:
        if not _same_net(v, net_name, net_number):
            continue
        span = _span(list(getattr(v, "layers", []) or []), board_layers)
        disk = Point(v.position).buffer(max(float(v.size), 0.0) / 2.0)
        nodes.append(_Node(kind="via", label="via", layers=span, geom=dict.fromkeys(span, disk)))

    regions = {
        layer: unary_union(polys + pad_shapes.get(layer, [])) for layer, polys in fills.items()
    }
    return _NetModel(regions, nodes), None


def _unfilled_zone(pcb: Any, net_name: str, net_number: int) -> bool:
    for zone in getattr(pcb, "zones", []) or []:
        if getattr(zone, "keepout", None) is not None:
            continue
        if not _same_net(zone, net_name, net_number):
            continue
        if not getattr(zone, "filled_polygons", None):
            return True
    return False


def poured_nets(pcb: Any, nets: list[str]) -> list[str]:
    """The nets in ``nets`` with at least one copper (non-keepout) zone."""
    poured = {
        getattr(z, "net_name", "")
        for z in getattr(pcb, "zones", []) or []
        if getattr(z, "net_name", "") and getattr(z, "keepout", None) is None
    }
    return [n for n in nets if n in poured]


def measure_pour_ampacity(
    pcb: PCB, specs: dict[str, float], design_rules: DesignRules
) -> PourAmpacityResult:
    """Rate the pour copper of every declared net that has a copper zone."""
    result = PourAmpacityResult()
    nets = poured_nets(pcb, sorted(specs))
    if not nets:
        return result

    from kicad_tools._shapely import has_shapely

    if not has_shapely():
        for net in nets:
            result.unmeasured[net] = "shapely unavailable"
        return result

    board_layers = _board_copper_layers(pcb)
    name_to_number = {
        getattr(n, "name", ""): getattr(n, "number", 0)
        for n in (getattr(pcb, "nets", {}) or {}).values()
    }
    for net in nets:
        current = specs[net]
        number = int(name_to_number.get(net, 0) or 0)
        if _unfilled_zone(pcb, net, number):
            result.unmeasured[net] = "zone fill absent"
            continue
        model, reason = _build_model(pcb, net, number, board_layers)
        if model is None:
            result.unmeasured[net] = reason or "unmeasurable"
            continue
        if not model.regions:
            result.unmeasured[net] = "no fill copper on a board copper layer"
            continue
        required: dict[str, float] = {}
        for layer in model.regions:
            width = _required_width(current, layer, design_rules)
            if width is None:
                required = {}
                result.unmeasured[net] = f"no copper weight for {layer}"
                break
            required[layer] = width
        if not required:
            continue

        def radii(scale: float, _req: dict[str, float] = required) -> dict[str, float]:
            return {layer: max(scale * w / 2.0 - _PASS_SLACK, _R0) for layer, w in _req.items()}

        if not model.joined(dict.fromkeys(model.regions, _R0)):
            groups = model.groups(dict.fromkeys(model.regions, _R0))
            labels = sorted(model.nodes[i].label for i in min(groups, key=len))
            result.unmeasured[net] = (
                f"pour copper does not join pad(s) {', '.join(labels)} to the rest of the net"
            )
            continue

        # Widest scale that still joins every pad: the pour's neck.
        lo, hi = 0.0, 1.0
        while model.joined(radii(hi)) and hi < 1024:
            lo, hi = hi, hi * 2
        for _ in range(_BISECT_STEPS):
            mid = (lo + hi) / 2
            if model.joined(radii(mid)):
                lo = mid
            else:
                hi = mid
        # Report the neck on an outer layer when the pour has one.
        ref_layer = sorted(required, key=lambda name: (name not in _EXTERNAL_LAYERS, name))[0]
        neck = lo * required[ref_layer]
        result.necks_mm[net] = round(neck, 4)
        if model.joined(radii(1.0)):
            continue

        groups = model.groups(radii(1.0))
        cut = min(groups, key=len)
        labels = sorted(model.nodes[i].label for i in cut)
        where = model.nodes[min(cut)].location
        layer_class = "external" if ref_layer in _EXTERNAL_LAYERS else "internal"
        oz = (
            design_rules.outer_copper_oz
            if layer_class == "external"
            else design_rules.inner_copper_oz
        )
        result.violations.append(
            DRCViolation(
                rule_id="ampacity",
                severity="error",
                message=(
                    f"Pour on {ref_layer} too narrow for {current:.1f}A: narrowest neck "
                    f"{neck:.3f}mm cuts off pad(s) {', '.join(labels)}, requires "
                    f"{required[ref_layer]:.3f}mm (IPC-2221, {oz}oz {layer_class})"
                ),
                location=where,
                layer=ref_layer,
                actual_value=round(neck, 4),
                required_value=required[ref_layer],
                items=(net,),
                nets=(net,),
            )
        )
    return result
