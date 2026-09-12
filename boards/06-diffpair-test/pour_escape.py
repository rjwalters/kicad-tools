"""Bounded physical escape search for pads stranded after pour repair."""

from __future__ import annotations

import json
import math
import re
from collections import deque
from dataclasses import dataclass
from pathlib import Path

from shapely.geometry import LineString, Point
from shapely.ops import unary_union
from shapely.prepared import prep

from kicad_tools.manufacturers.dru_generator import (
    DRU_FLOORS_BLOCK_BEGIN,
    DRU_FLOORS_BLOCK_END,
)

# Rule-name families the fab-floors generator (Issue #4600) can emit that
# this escape search knows how to fold into its physical minima.
_KNOWN_FLOOR_FAMILY_FIELDS = {
    "Trace Width": "width",
    "Clearance": "clearance",
    "Via Drill": "drill",
    "Via Diameter": "diameter",
    "Annular Ring": "annulus",
}
# Families the generator can also emit that this recipe never routes near
# (board edge, silkscreen, solder mask, ampacity, pad-only annular ring) --
# recognized and safely ignored rather than folded in.
_IGNORED_FLOOR_FAMILIES = {
    "PTH Annular Ring",
    "Copper to Edge",
    "Hole to Edge",
    "Silkscreen Width",
    "Silkscreen Height",
    "Solder Mask Clearance",
    "Solder Mask Dam",
}
_DRU_RULE_RE = re.compile(
    r'\(rule "(?P<name>[^"]+)"\n'
    r'(?:\s*\(condition "[^"]*"\)\n)?'
    r"\s*\(constraint \w+ \(min (?P<value>[0-9.]+)mm\)\)\)"
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
    body_lines = [
        line
        for line in dru_text.splitlines()
        if line.strip() and not line.strip().startswith("(version")
    ]
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
        field = _KNOWN_FLOOR_FAMILY_FIELDS.get(family)
        if field is not None:
            minima[field] = max(minima.get(field, 0.0), float(match.group("value")))
        elif family not in _IGNORED_FLOOR_FAMILIES:
            return None  # unrecognized rule family -- fail closed
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
            via_blocks.append(
                Point(center).buffer(drill_radius + hole_radius + rules.hole_gap + guard)
            )
            if pnet != net:
                via_blocks.append(
                    Point(center).buffer(hole_radius + radius + rules.hole_copper + guard)
                )
                if layer in layers:
                    trace_blocks.append(
                        Point(center).buffer(
                            hole_radius + max(0, rules.hole_copper - rules.clearance)
                        )
                    )
        if pnet != net:
            via_blocks.append(geom.buffer(drill_radius + rules.hole_copper + guard))
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
    pending = deque([origin])
    previous = {origin: None}
    moves = ((1, 0), (0, 1), (-1, 0), (0, -1), (1, 1), (-1, 1), (-1, -1), (1, -1))
    while pending:
        current = pending.popleft()
        xy = point(current)
        pt = Point(xy)
        on_front = current != origin and front.intersects(pt)
        on_via = (
            min_x + radius <= xy[0] <= max_x - radius
            and min_y + radius <= xy[1] <= max_y - radius
            and not blocked_via.intersects(pt)
            and through.intersects(pt)
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
            if following in previous:
                continue
            nx, ny = point(following)
            if not (min_x <= nx <= max_x and min_y <= ny <= max_y):
                continue
            if blocked_trace.intersects(LineString([xy, (nx, ny)])):
                continue
            if len(previous) >= node_budget:
                return None
            previous[following] = current
            pending.append(following)
    return None
