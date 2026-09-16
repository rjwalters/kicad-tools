"""Bounded physical escape search for pads stranded after pour repair."""

from __future__ import annotations

import heapq
import json
import math
import re
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from shapely.prepared import prep

from kicad_tools.manufacturers.dru_generator import (
    DRU_FLOORS_BLOCK_BEGIN,
    DRU_FLOORS_BLOCK_END,
)

# Exact factory scopes, not a rule-name whitelist. A changed condition or
# constraint can alter which new copper is affected, so reject it explicitly.
# None fields concern immutable pads/noncopper, or the existing edge policy.
_FLOOR_SCOPES = {
    "Trace Width": ("track_width", "A.Type == 'track'", None, "width"),
    "Clearance": ("clearance", None, None, "clearance"),
    "Via Drill": ("hole_size", "A.Type == 'via' && A.Via_Type != 'Micro'", None, "drill"),
    "Via Diameter": ("via_diameter", "A.Type == 'via' && A.Via_Type != 'Micro'", None, "diameter"),
    "Annular Ring": ("annular_width", "A.Via_Type != 'Micro'", None, "annulus"),
    "PTH Annular Ring": ("annular_width", "A.Type == 'pad'", None, None),
    "Copper to Edge": ("edge_clearance", None, None, "edge_clearance"),
    "Hole to Edge": (
        "physical_hole_clearance",
        "(A.Type == 'via' || A.Type == 'pad') && B.Layer == 'Edge.Cuts'",
        None,
        "hole_edge_clearance",
    ),
    "Silkscreen Width": (
        "text_thickness",
        "A.Type == 'text' && A.Layer == 'F.Silkscreen'",
        None,
        None,
    ),
    "Silkscreen Height": (
        "text_height",
        "A.Type == 'text' && A.Layer == 'F.Silkscreen'",
        None,
        None,
    ),
    # The search creates neither silk nor pads; these exact pair scopes cannot
    # be changed by its emitted tracks/vias. They are not general clearances.
    "Silk to Pad": ("silk_clearance", "A.Type == 'Pad' || B.Type == 'Pad'", None, None),
    "SMD Pad Clearance": ("clearance", "A.Pad_Type == 'SMD' && B.Pad_Type == 'SMD'", None, None),
    "PTH Hole to Track": (
        "hole_clearance",
        "(A.Pad_Type == 'Through-hole' && B.Type == 'Track') || (B.Pad_Type == 'Through-hole' && A.Type == 'Track')",
        None,
        "pth_hole_track",
    ),
    "Inner PTH Hole to Copper": (
        "hole_clearance",
        "A.Pad_Type == 'Through-hole' || B.Pad_Type == 'Through-hole'",
        "inner",
        "inner_pth_hole_copper",
    ),
}
_DRU_RULE_RE = re.compile(
    r'\(rule "(?P<name>[^\"]+)"\n'
    r"(?:\s*\(layer (?P<layer>\w+)\)\n)?"
    r'(?:\s*\(condition "(?P<condition>[^\"]*)"\)\n)?'
    r"\s*\(constraint (?P<constraint>\w+) \(min (?P<value>[0-9.]+)mm\)\)\)"
)


def _kct_managed_floor_minima(dru_text: str) -> dict[str, float] | None:
    """Extract escape minima from a *pure* kct fab-floors ``.kicad_dru``.

    Returns ``None`` -- not safe to auto-extract -- unless the file is
    *exactly* the sentinel-delimited managed block
    :func:`kicad_tools.manufacturers.dru_generator.generate_dru` writes
    (Issue #4600), with nothing else present (hand-authored rules, the
    creepage exporter's separate block, ...). A file matching that shape is
    safe to fold in even though it "exists": every rule family it can
    contain is enumerated above, so nothing is silently ignored.
    """
    body_lines = [line for line in dru_text.splitlines() if line.strip()]
    if not body_lines or body_lines.pop(0).strip() != "(version 1)":
        return None
    body = "\n".join(body_lines).strip()
    if not (body.startswith(DRU_FLOORS_BLOCK_BEGIN) and body.endswith(DRU_FLOORS_BLOCK_END)):
        return None
    inner = body[len(DRU_FLOORS_BLOCK_BEGIN) : -len(DRU_FLOORS_BLOCK_END)].strip("\n")
    if not inner:
        return {}
    minima: dict[str, float] = {}
    for stanza in re.split(r"\n(?=\(rule )", inner):
        match = _DRU_RULE_RE.fullmatch(stanza.strip())
        if match is None:
            return None  # hand-edited or unrecognized stanza -- fail closed
        family = match.group("name").split(" - ")[0]
        scope = _FLOOR_SCOPES.get(family)
        if scope is None or (match["constraint"], match["condition"], match["layer"]) != scope[:3]:
            return None
        try:
            value = float(match["value"])
        except ValueError:
            return None
        if not math.isfinite(value) or value < 0:
            return None
        field = scope[3]
        if field is not None:
            minima[field] = max(minima.get(field, 0.0), value)
    return minima


@dataclass(frozen=True)
class EscapeRules:
    # KiCad 10 project defaults, independently checked with native DRC. These
    # are board-rule defaults, not assertions about a manufacturer's capability.
    clearance: float = 0.2
    width: float = 0.2
    diameter: float = 0.5
    drill: float = 0.3
    annulus: float = 0.1
    hole_gap: float = 0.5  # Preserve this recipe's stronger drill-spacing floor.
    hole_copper: float = 0.25
    pth_hole_track: float = 0.0
    inner_pth_hole_copper: float = 0.0
    edge_clearance: float = 0.0
    hole_edge_clearance: float = 0.0

    @classmethod
    def from_project(cls, path: Path) -> EscapeRules:
        """Strengthen defaults with project minima; reject unmodeled custom rules."""
        dru_path = path.with_suffix(".kicad_dru")
        dru_floor_minima: dict[str, float] = {}
        if dru_path.exists():
            parsed = _kct_managed_floor_minima(dru_path.read_text())
            if parsed is None:
                raise ValueError("Escape search cannot evaluate custom DRC rules")
            dru_floor_minima = parsed
        data = json.loads(path.read_text()) if path.exists() else {}
        if not isinstance(data, dict):
            raise ValueError("Project must be an object")

        def section(parent, key):
            value = parent.get(key, {})
            if not isinstance(value, dict):
                raise ValueError(f"Invalid project {key} section")
            return value

        rules = section(section(section(data, "board"), "design_settings"), "rules")

        def number(value):
            if isinstance(value, bool) or not isinstance(value, (float, int)):
                raise ValueError("Project dimensions must be finite nonnegative numbers")
            if not math.isfinite(value) or value < 0:
                raise ValueError("Project dimensions must be finite nonnegative numbers")
            return float(value)

        defaults = cls()
        mapping = {
            "clearance": ("min_clearance",),
            "width": ("min_track_width",),
            "diameter": ("min_via_diameter",),
            "drill": ("min_through_hole_diameter", "min_via_hole"),
            "annulus": ("min_via_annular_width",),
            "hole_gap": ("min_hole_to_hole",),
            "hole_copper": ("hole_clearance",),
            "pth_hole_track": (),
            "inner_pth_hole_copper": (),
            "edge_clearance": ("min_copper_edge_clearance",),
            "hole_edge_clearance": (),
        }
        values = {
            name: max(
                [getattr(defaults, name)]
                + [number(rules[k]) for k in keys if k in rules]
                + ([dru_floor_minima[name]] if name in dru_floor_minima else [])
            )
            for name, keys in mapping.items()
        }
        # Taking the strongest class is conservative even when a project uses
        # assignments/patterns this recipe does not resolve. Never silently
        # apply only Default while overlooking a stricter assigned class.
        classes = section(data, "net_settings").get("classes", [])
        if not isinstance(classes, list):
            raise ValueError("Project net classes must be a list")
        for net_class in classes:
            if not isinstance(net_class, dict):
                raise ValueError("Invalid project net class")
            if "clearance" in net_class:
                values["clearance"] = max(values["clearance"], number(net_class["clearance"]))
        values["diameter"] = max(values["diameter"], values["drill"] + 2 * values["annulus"])
        return cls(**values)


@dataclass(frozen=True)
class Escape:
    points: tuple[tuple[float, float], ...]
    via: bool
    rules: EscapeRules


def find_escape(
    start,
    net,
    layer,
    pads,
    segments,
    vias,
    primary,
    bounds,
    rules,
    *,
    step=0.05,
    node_budget=200_000,
    allowed_region=None,
):
    """Find a clear 45-degree path to primary copper or a legal through via.

    Geometry is immutable here. The caller commits only a complete result.
    Via entries carry their actual drill diameter, including earlier repairs.
    A budget exhaustion returns no repair, never a partial path.
    """
    if not math.isfinite(step) or step <= 0 or node_budget < 1:
        raise ValueError("Escape search requires a positive grid step and node budget")
    radius = rules.diameter / 2
    drill_radius = rules.drill / 2
    guard = 0.001
    trace_blocks, via_blocks = [], []
    for entry in pads:
        geom, pnet, layers, hole_radius, center = entry[:5]
        if pnet != net and layer in layers:
            trace_blocks.append(geom)
        via_blocks.append(geom.buffer(radius + (rules.clearance if pnet != net else 0) + guard))
        if hole_radius:
            # Legacy tuples have no pad type, and the recipe's is_th flag
            # denotes wildcard layers (also possible for NPTH). Apply PTH
            # floors conservatively to drilled pads: NPTH may be overblocked,
            # but ambiguous metadata never weakens a required hole clearance.
            via_blocks.append(
                Point(center).buffer(drill_radius + hole_radius + rules.hole_gap + guard)
            )
            if pnet != net:
                # A through drill traverses inner layers even when its pad
                # copper lists only F.Cu/B.Cu (native applicability probes,
                # review65). Candidate through-via copper sees that hole.
                inner_floor = rules.inner_pth_hole_copper
                via_blocks.append(
                    Point(center).buffer(
                        hole_radius + radius + max(rules.hole_copper, inner_floor) + guard
                    )
                )
                hole_floor = max(
                    rules.hole_copper,
                    rules.pth_hole_track,
                    inner_floor if layer not in {"F.Cu", "B.Cu"} else 0.0,
                )
                trace_blocks.append(
                    Point(center).buffer(hole_radius + max(0, hole_floor - rules.clearance))
                )
        if pnet != net:
            # The generated PTH OR condition also matches the reverse
            # measurement: candidate via hole to existing drilled-pad copper.
            # Native DRC applies it even with an outer-only PTH copper list.
            reciprocal_floor = max(
                rules.hole_copper, rules.inner_pth_hole_copper if hole_radius else 0.0
            )
            via_blocks.append(geom.buffer(drill_radius + reciprocal_floor + guard))
    for geom, snet, lay in segments:
        if snet != net:
            via_blocks.append(
                geom.buffer(max(radius + rules.clearance, drill_radius + rules.hole_copper) + guard)
            )
            if lay == layer:
                trace_blocks.append(geom)
    for pt, vnet, existing_radius, existing_drill in vias:
        via_blocks.append(pt.buffer(drill_radius + existing_drill / 2 + rules.hole_gap + guard))
        if vnet != net:
            via_blocks.append(
                pt.buffer(
                    max(
                        existing_radius + radius + rules.clearance,
                        existing_radius + drill_radius + rules.hole_copper,
                        existing_drill / 2 + radius + rules.hole_copper,
                    )
                    + guard
                )
            )
            trace_blocks.append(
                pt.buffer(
                    max(existing_radius, existing_drill / 2 + rules.hole_copper - rules.clearance)
                )
            )
    blocked_trace = prep(
        unary_union(trace_blocks).buffer(rules.clearance + rules.width / 2 + guard)
    )
    blocked_via = prep(unary_union(via_blocks))
    front = prep(
        unary_union([geom for geom, layers, _ in primary if layer in layers]).buffer(-0.025)
    )
    through = prep(
        unary_union([geom for geom, layers, _ in primary if layers - {layer}]).buffer(
            radius - 0.025
        )
    )
    min_x, min_y, max_x, max_y = bounds

    def point(node):
        return (round(start[0] + node[0] * step, 3), round(start[1] + node[1] * step, 3))

    origin = (0, 0)
    if blocked_trace.intersects(Point(point(origin))):
        return None
    # Euclidean distance to any accepted target region is a lower bound on
    # remaining physical path length. Keep through targets unclipped by via
    # obstacles/bounds: enlarging the goal set only lowers this heuristic.
    # The distinct-node cap still makes this a bounded search, not a promise
    # of completeness or shortest paths under that cap.
    targets = unary_union([front.context, through.context])

    def estimate(xy):
        return 0.0 if targets.is_empty else targets.distance(Point(xy))

    pending = [(estimate(point(origin)), 0, 0.0, origin)]
    sequence = 0
    previous = {origin: None}
    costs = {origin: 0.0}
    estimates = {origin: pending[0][0]}
    moves = ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))
    while pending:
        _, _, cost, current = heapq.heappop(pending)
        if cost != costs[current]:
            continue  # Superseded open entry; never expand an obsolete path.
        xy = point(current)
        pt = Point(xy)
        on_front = current != origin and front.intersects(pt)
        on_via = (
            min_x + radius <= xy[0] <= max_x - radius
            and min_y + radius <= xy[1] <= max_y - radius
            and not blocked_via.intersects(pt)
            and through.intersects(pt)
            and (allowed_region is None or allowed_region.covers(pt.buffer(radius)))
        )
        if on_front or on_via:
            nodes = []
            while current is not None:
                nodes.append(point(current))
                current = previous[current]
            nodes.reverse()
            corners = [nodes[0]]
            for a, b, c in zip(nodes, nodes[1:], nodes[2:], strict=False):
                if abs((b[0] - a[0]) * (c[1] - b[1]) - (b[1] - a[1]) * (c[0] - b[0])) > 1e-8:
                    corners.append(b)
            if len(nodes) > 1:
                corners.append(nodes[-1])
            return Escape(tuple(corners), not on_front, rules)
        for dx, dy in moves:
            following = (current[0] + dx, current[1] + dy)
            nx, ny = point(following)
            # Use the emitted (rounded) physical endpoints, including the
            # longer diagonal step, rather than grid-hop counts.
            following_cost = cost + math.hypot(nx - xy[0], ny - xy[1])
            if following_cost >= costs.get(following, math.inf):
                continue
            if not (min_x <= nx <= max_x and min_y <= ny <= max_y):
                continue
            step_line = LineString([xy, (nx, ny)])
            if allowed_region is not None and not allowed_region.covers(
                step_line.buffer(rules.width / 2)
            ):
                continue
            if blocked_trace.intersects(step_line):
                continue
            if following not in previous:
                if len(previous) >= node_budget:
                    return None
                estimates[following] = estimate((nx, ny))
            previous[following] = current
            costs[following] = following_cost
            sequence += 1
            heapq.heappush(
                pending,
                (following_cost + estimates[following], sequence, following_cost, following),
            )
    return None
