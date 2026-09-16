"""Commit physical off-pad access before routing signals over deferred planes.

This opt-in policy describes future plane geometry; it does not certify filled
plane connectivity. The saved/refilled board still needs its ordinary DRC and
physical connectivity gates.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from pathlib import Path

from shapely.affinity import translate
from shapely.geometry import LineString, Point

from kicad_tools.pcb.board_geometry import BoardGeometry
from kicad_tools.schema.pcb import PCB
from kicad_tools.zones.pour_escape import EscapeRules, find_escape

from .fixed_copper import FixedFill, FixedFillObstacles
from .layers import Layer
from .primitives import Route, Segment, Via


@dataclass(frozen=True)
class PlaneAccessTarget:
    """A plane's real net and copper layer, with optional world-coordinate region."""

    net_name: str
    layer: str
    region: object | None = None


@dataclass(frozen=True)
class PlaneAccessPolicy:
    """Protect SMD pads no larger than the deferred helper's via diameter.

    Dimensions come from the actual later stitch/repair policy, independently
    of signal routing widths. Existing copper and partial-board requests are
    rejected; reload an emitted board as existing copper instead of replanning.
    """

    targets: tuple[PlaneAccessTarget, ...]
    rules: EscapeRules
    step: float = 0.05
    node_budget: int = 200_000


def install_plane_access(router, pcb_path, net_map, policy: PlaneAccessPolicy) -> None:
    """Plan atomically, then install ordinary fixed copper on both routing grids."""
    board = PCB.load(Path(pcb_path))
    if board.segments or board.vias or board.zones or board.arcs:
        raise ValueError("Plane access planning requires an unrouted board without existing zones")
    if not policy.targets or len({t.net_name for t in policy.targets}) != len(policy.targets):
        raise ValueError("Plane access requires one unambiguous target per net")
    rules = policy.rules
    for field, value in vars(rules).items():
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"Invalid plane access rule: {field}")
    if (
        min(rules.width, rules.drill, rules.diameter) <= 0
        or rules.diameter < rules.drill + 2 * rules.annulus
    ):
        raise ValueError("Invalid plane access via/track dimensions")
    geometry = BoardGeometry.from_pcb(board)
    world = translate(geometry.polygon, *geometry.origin)
    # Conservatively impose hole-to-edge on the larger copper barrel too.
    edge = max(
        rules.edge_clearance, rules.hole_edge_clearance, getattr(router, "_edge_clearance", 0) or 0
    )
    allowed = world.buffer(-edge)
    if allowed.is_empty:
        raise ValueError("No board interior available for plane access")
    targets = {}
    for target in policy.targets:
        if net_map.get(target.net_name, 0) <= 0:
            raise ValueError(f"Unknown plane access net: {target.net_name}")
        target_layer = Layer.from_kicad_name(target.layer)
        if target_layer not in {layer.layer_enum for layer in router.grid.layer_stack.layers}:
            raise ValueError(f"Plane access layer absent from routing stack: {target.layer}")
        region = world if target.region is None else target.region.intersection(world)
        if region.is_empty:
            raise ValueError(f"Empty plane access region: {target.net_name}")
        targets[target.net_name] = (target, region)

    pads = []
    candidates = []
    ox, oy = board.board_origin
    copper_layers = frozenset(layer.kicad_name for layer in Layer)
    round_out = 1 / math.cos(math.pi / 256)
    for fp in board.footprints:
        angle = math.radians(fp.rotation or 0)
        for index, pad in enumerate(fp.pads):
            px, py = pad.position
            center = (
                fp.position[0] + ox + px * math.cos(angle) + py * math.sin(angle),
                fp.position[1] + oy - px * math.sin(angle) + py * math.cos(angle),
            )
            layers = (
                copper_layers
                if any("*" in layer for layer in pad.layers)
                else frozenset(layer for layer in pad.layers if layer.endswith(".Cu"))
            )
            if pad.shape not in {"rect", "roundrect", "circle", "oval"}:
                raise ValueError(f"Unsupported plane access obstacle shape: {pad.shape}")
            drill = max(float(pad.drill or 0), max(pad.drill_size or (0, 0)))
            hole_radius = drill / 2 + math.hypot(*pad.drill_offset) if drill else 0
            # Circular envelopes enclose rotated rectangles and rounded pads.
            geom = Point(center).buffer(math.hypot(*pad.size) / 2 * round_out, quad_segs=64)
            pads.append((geom, pad.net_name or "", layers, hole_radius, center))
            if (
                pad.net_name in targets
                and pad.type == "smd"
                and not drill
                and len(layers) == 1
                and max(pad.size) <= rules.diameter
            ):
                layer = next(iter(layers))
                target, region = targets[pad.net_name]
                if target.layer != layer:
                    candidates.append(
                        (fp.reference, index, center, pad.net_name, layer, region, target.layer)
                    )

    segments, vias, planned = [], [], []
    for ref, index, start, name, layer, region, target_layer in candidates:
        escape = find_escape(
            start,
            name,
            layer,
            pads,
            segments,
            vias,
            [(region, {target_layer}, name)],
            allowed.bounds,
            rules,
            step=policy.step,
            node_budget=policy.node_budget,
            allowed_region=allowed,
        )
        if escape is None or not escape.via:
            raise ValueError(
                f"No legal off-pad plane access for {ref} pad occurrence {index} ({name})"
            )
        number = net_map[name]
        route = Route(net=number, net_name=name, is_escape=True)
        for a, b in zip(escape.points, escape.points[1:], strict=False):
            route.segments.append(
                Segment(*a, *b, rules.width, Layer.from_kicad_name(layer), number, name)
            )
            segments.append((LineString([a, b]).buffer(rules.width / 2), name, layer))
        x, y = escape.points[-1]
        route.vias.append(
            Via(
                x=x,
                y=y,
                drill=rules.drill,
                diameter=rules.diameter,
                layers=(Layer.F_CU, Layer.B_CU),
                net=number,
                net_name=name,
            )
        )
        vias.append((Point(x, y), name, rules.diameter / 2, rules.drill))
        planned.append(route)
    # No geometry mutation occurs until every required access succeeds.
    fills = []
    round_out = 1 / math.cos(math.pi / 256)
    # Fixed-fill queries carry copper radii, not drill radii. This local,
    # conservative margin also enforces drill spacing for any nonnegative
    # candidate annulus without broadening unrelated pad/route halos.
    barrel_gap = max(
        rules.clearance, rules.hole_copper, rules.hole_gap - (rules.diameter - rules.drill) / 2
    )
    stub_gap = max(rules.clearance, rules.hole_copper)
    for route in planned:
        for segment in route.segments:
            fills.append(
                FixedFill(
                    route.net_name,
                    route.net,
                    router.grid.layer_to_index(segment.layer.value),
                    stub_gap,
                    LineString([(segment.x1, segment.y1), (segment.x2, segment.y2)]).buffer(
                        segment.width / 2 * round_out, quad_segs=64
                    ),
                    source_kind="plane_access_stub",
                )
            )
        for via in route.vias:
            copper = Point(via.x, via.y).buffer(via.diameter / 2 * round_out, quad_segs=64)
            for layer in router.grid.layer_stack.layers:
                fills.append(
                    FixedFill(
                        route.net_name,
                        route.net,
                        layer.index,
                        barrel_gap,
                        copper,
                        source_kind="plane_access_barrel",
                    )
                )
    router._plane_access_fills = tuple(fills)
    router.grid.install_fixed_fills(
        FixedFillObstacles(router.grid.fixed_fills.fills + tuple(fills))
    )
    router._plane_access_rules = rules
    router._plane_access_routes = tuple(planned)
    router._plane_access_expected = tuple(route.copy_geometry() for route in planned)
    for route in planned:
        router.existing_routes.append(route)
        router._mark_route(route)


def export_plane_access(router) -> tuple[Route, ...]:
    """Emit exactly the protected sites; never silently accept a lost fixed route."""
    routes = getattr(router, "_plane_access_routes", ())
    expected = getattr(router, "_plane_access_expected", ())
    for route, frozen in zip(routes, expected, strict=True):
        if (
            route != frozen
            or not any(item is route for item in router.existing_routes)
            or not any(item is route for item in router.grid.routes)
        ):
            raise ValueError("Fixed plane access changed or was lost during routing")
        if any(item is route for item in router.routes):
            raise ValueError("Fixed plane access duplicated in mutable routing results")
    if routes:
        if any(
            not any(item is fill for item in router.grid.fixed_fills.fills)
            for fill in router._plane_access_fills
        ):
            raise ValueError("Fixed plane access obstacles were lost during routing")
        _validate_access_clearances(routes, router.routes, router._plane_access_rules)
    return routes


def _validate_access_clearances(access, signal_routes, rules):
    """Fail closed if any later route consumes required physical access space.

    Through access barrels occupy every copper layer. This exact final guard
    is independent of raster occupancy and preserves the helper's stronger
    search margins even when signal rules use smaller clearances.
    """
    for fixed in access:
        for other in signal_routes:
            foreign = fixed.net != other.net
            for via in fixed.vias:
                point = Point(via.x, via.y)
                if foreign:
                    for seg in other.segments:
                        line = LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)])
                        required = (
                            max(
                                via.diameter / 2 + rules.clearance,
                                via.drill / 2 + rules.hole_copper,
                            )
                            + seg.width / 2
                        )
                        if point.distance(line) < required - 1e-4:
                            raise ValueError(
                                "Signal copper violates fixed plane access barrel clearance"
                            )
                for candidate in other.vias:
                    distance = point.distance(Point(candidate.x, candidate.y))
                    required = (via.drill + candidate.drill) / 2 + rules.hole_gap
                    if foreign:
                        required = max(
                            required,
                            (via.diameter + candidate.diameter) / 2 + rules.clearance,
                            (via.drill + candidate.diameter) / 2 + rules.hole_copper,
                            (via.diameter + candidate.drill) / 2 + rules.hole_copper,
                        )
                    if distance < required - 1e-4:
                        raise ValueError("Via violates fixed plane access clearance")
            if not foreign:
                continue
            for stub in fixed.segments:
                line = LineString([(stub.x1, stub.y1), (stub.x2, stub.y2)])
                for seg in other.segments:
                    if seg.layer != stub.layer:
                        continue
                    distance = line.distance(LineString([(seg.x1, seg.y1), (seg.x2, seg.y2)]))
                    if distance < (stub.width + seg.width) / 2 + rules.clearance - 1e-4:
                        raise ValueError("Signal copper violates fixed plane access stub clearance")
                for via in other.vias:
                    # Conservatively check every foreign via, including microvias.
                    required = stub.width / 2 + max(
                        via.diameter / 2 + rules.clearance, via.drill / 2 + rules.hole_copper
                    )
                    if line.distance(Point(via.x, via.y)) < required - 1e-4:
                        raise ValueError("Via violates fixed plane access stub clearance")


def serialize_plane_access(routes, *, name_only=False):
    """Tag access copper so reload cannot silently discard its stronger policy.

    KiCad preserves this ordinary named group across native save/refill. The
    loader refuses continuation until it can reconstruct the full policy; the
    original unrouted source remains the supported entry point for a reroute.
    """
    import re
    import uuid

    if not routes:
        return ""
    copper = "\n\t".join(route.to_sexp(name_only=name_only) for route in routes)
    members = " ".join(f'"{value}"' for value in re.findall(r'\(uuid "([^"]+)"\)', copper))
    group = f'(group "kct:fixed-plane-access:v1" (uuid "{uuid.uuid4()}") (members {members}))'
    return copper + "\n\t" + group
