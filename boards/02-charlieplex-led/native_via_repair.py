"""Bounded native-report repair; callers must independently qualify the result."""

import json
import math
import re

from shapely.geometry import LineString, Point

from kicad_tools.cli.relocate_in_pad_vias import _collect_smd_pads_by_net, _collect_tht_pads
from kicad_tools.drc.relocate_drill_clearance import _try_relocate
from kicad_tools.manufacturers import get_profile
from kicad_tools.sexp import parse_string
from kicad_tools.validate.rules.clearance import _pad_polygon


def repair_floors(path, pcb, report):
    """Use the strictest manufacturer, project, custom-rule and reported minimum."""
    project = json.loads(path.with_suffix(".kicad_pro").read_text())
    profile = get_profile(project.get("meta", {}).get("manufacturer", "jlcpcb"))
    rules = profile.get_design_rules(len(pcb.copper_layers))
    copper = [rules.min_clearance_mm]
    holes = [rules.min_hole_to_hole_mm]
    settings = project["board"]["design_settings"]
    copper.extend(
        [settings["rules"]["min_clearance"], settings.get("defaults", {}).get("clearance_min", 0)]
    )
    holes.append(settings["rules"]["min_hole_to_hole"])
    copper.extend(c["clearance"] for c in project.get("net_settings", {}).get("classes", []))
    custom = path.with_suffix(".kicad_dru")
    if custom.exists():
        for constraint in parse_string("(rules " + custom.read_text() + ")").find_all("constraint"):
            kind = constraint.get_string(0)
            if kind not in ("clearance", "hole_to_hole"):
                continue
            minimum = constraint.find("min")
            if minimum is None:
                continue
            match = re.fullmatch(r"([0-9.]+)(mm|mil)?", str(minimum.get_value(0)))
            if not match:
                raise ValueError("Cannot resolve custom clearance minimum")
            value = float(match[1]) * (0.0254 if match[2] == "mil" else 1)
            (copper if kind == "clearance" else holes).append(value)
    for finding in report["violations"]:
        kind = finding["type"]
        if kind in ("clearance", "hole_to_hole"):
            match = re.search(
                r"(?:min(?:imum)?|clearance)\s+([0-9.]+)\s*mm", finding["description"]
            )
            if not match:
                raise ValueError("Cannot resolve native clearance minimum")
            (copper if kind == "clearance" else holes).append(float(match[1]))
    if any(not math.isfinite(v) or v < 0 for v in copper + holes):
        raise ValueError("Invalid clearance minimum")
    return max(copper), max(holes)


def repair_report_vias(path, pcb, report):
    """Try each actual offending via once using the existing clearance engine."""
    copper, holes = repair_floors(path, pcb, report)
    ids = dict.fromkeys(
        item["uuid"]
        for finding in report["violations"]
        if finding["type"] in ("clearance", "hole_to_hole")
        for item in finding["items"]
    )
    vias = {v.uuid: v for v in pcb.vias}
    pads, tht = _collect_smd_pads_by_net(pcb), _collect_tht_pads(pcb)
    changed = False
    for identity in ids:
        if identity in vias:
            result = _try_relocate(pcb, vias[identity], pads, tht, copper, holes, False)
            if result is None:
                raise ValueError(f"No safe relocation for reported via {identity}")
            changed = True
    return changed


def redundant_leaf(pcb, track):
    """Prove a leaf serves no terminal beyond an already joined via and trunk.

    Reject zones and unsupported copper primitives rather than guessing their
    connectivity. All supported contacts use physical copper, not net labels.
    """
    if pcb.zones or pcb._sexp.find_all("arc"):
        return False
    for name in (
        "gr_line",
        "gr_arc",
        "gr_poly",
        "gr_circle",
        "gr_rect",
        "fp_line",
        "fp_arc",
        "fp_poly",
        "fp_circle",
        "fp_rect",
        "gr_text",
        "gr_text_box",
        "gr_curve",
        "fp_text",
        "fp_text_box",
        "fp_curve",
    ):
        for node in pcb._sexp.find_all(name):
            layer = node.find("layer")
            if layer and layer.get_string(0).endswith(".Cu"):
                return False
    copper = LineString([track.start, track.end]).buffer(track.width / 2, quad_segs=64)
    for fp in pcb.footprints:
        for pad in fp.pads:
            if track.layer not in pad.layers and "*.Cu" not in pad.layers:
                continue
            if pad.shape not in ("rect", "roundrect", "circle", "oval"):
                return False
            polygon = _pad_polygon(pad, fp)
            if polygon is None or copper.distance(polygon) < 1e-6:
                return False
    # Only two-layer through vias are supported by this deliberately narrow proof.
    if len(pcb.copper_layers) != 2:
        return False
    vias = [v for v in pcb.vias if copper.distance(Point(v.position).buffer(v.size / 2)) < 1e-6]
    tracks = [
        s
        for s in pcb.segments
        if s.uuid != track.uuid
        and s.layer == track.layer
        and copper.distance(LineString([s.start, s.end]).buffer(s.width / 2)) < 1e-6
    ]
    if len(vias) != 1 or len(tracks) != 1:
        return False
    via, trunk = vias[0], tracks[0]
    if via.net_number != track.net_number or trunk.net_number != track.net_number:
        return False

    def near(a, b):
        return math.dist(a, b) < 1e-6

    if not any(near(via.position, p) for p in (track.start, track.end)):
        return False
    if not any(near(via.position, p) for p in (trunk.start, trunk.end)):
        return False
    annulus = (
        Point(via.position)
        .buffer(via.size / 2, quad_segs=64)
        .difference(Point(via.position).buffer(via.drill / 2, quad_segs=64))
    )
    return (
        LineString([trunk.start, trunk.end]).buffer(trunk.width / 2).intersection(annulus).area
        > 1e-9
    )


def remove_reported_leaves(pcb, report):
    """Remove only independently proved native-reported leaves, rechecking each."""
    ids = dict.fromkeys(
        item["uuid"]
        for finding in report["violations"]
        if finding["type"] == "track_dangling"
        for item in finding["items"]
    )
    changed = False
    for identity in ids:
        track = next((s for s in pcb.segments if s.uuid == identity), None)
        if track is not None and redundant_leaf(pcb, track):
            if pcb.remove_segments([track]) != 1:
                raise ValueError("Failed to remove proved leaf")
            changed = True
    return changed
