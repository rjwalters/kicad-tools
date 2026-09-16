"""Explicit CLI bridge to the deferred automatic-pour access policy."""

from __future__ import annotations

import json
import math
from argparse import Namespace
from pathlib import Path

from shapely.geometry import Polygon  # type: ignore[import-untyped]

from kicad_tools.router.net_class import NetClass
from kicad_tools.router.plane_access import PlaneAccessPolicy, PlaneAccessTarget
from kicad_tools.zones.generator import (
    ZoneGenerator,
    _assign_layers_for_pour_nets,
    _compute_pour_outlines,
)
from kicad_tools.zones.pour_escape import EscapeRules


def policy_for_attempt(
    args: Namespace, pcb_path: str | Path, skip_nets: list[str]
) -> PlaneAccessPolicy | None:
    """Resolve real pour regions afresh in this attempt's coordinate frame.

    The plan declares the same inputs as ``auto_create_zones_for_pour_nets``;
    it cannot supply weaker access dimensions or arbitrary copper regions.
    """
    plan_path = getattr(args, "plane_access_plan", None)
    if plan_path is None:
        return None
    if any(
        getattr(args, name, None)
        for name in (
            "nets",
            "complete",
            "region",
            "_region_box",
            "allow_offboard",
            "preserve_existing",
            "auto_layers",
            "auto_mfr_tier",
            "adaptive_rules",
            "auto_pcb_size",
        )
    ):
        raise ValueError("Plane access requires a complete unrouted fixed-stack attempt")
    plan = json.loads(Path(plan_path).read_text())
    if (
        not isinstance(plan, dict)
        or set(plan) != {"version", "pour_nets", "edge_clearance", "source_project"}
        or type(plan["version"]) is not int
        or plan["version"] != 1
        or not isinstance(plan["source_project"], str)
    ):
        raise ValueError("Invalid plane access plan schema")
    inset = plan["edge_clearance"]
    if (
        isinstance(inset, bool)
        or not isinstance(inset, (int, float))
        or not math.isfinite(inset)
        or inset < 0
    ):
        raise ValueError("Invalid deferred pour edge clearance")
    declarations = plan["pour_nets"]
    if not isinstance(declarations, list) or not declarations:
        raise ValueError("Plane access requires deferred pour nets")
    pours = []
    for item in declarations:
        if (
            not isinstance(item, list)
            or len(item) != 2
            or not isinstance(item[0], str)
            or not item[0]
            or item[1] not in ("ground", "power")
        ):
            raise ValueError("Invalid deferred pour net declaration")
        pours.append((item[0], NetClass(item[1])))
    names = [name for name, _ in pours]
    if len(set(names)) != len(names) or set(names) != set(skip_nets):
        raise ValueError("Plane access declarations must match all explicitly skipped nets")
    source = Path(plan["source_project"])
    output = (
        Path(args.output)
        if args.output
        else Path(pcb_path).with_stem(Path(pcb_path).stem + "_routed")
    )
    output_project = output.with_suffix(".kicad_pro")
    if not source.is_file() or not output_project.is_file():
        raise ValueError("Plane access requires explicit source and output project contexts")
    rules = EscapeRules.from_projects(
        source, Path(pcb_path).with_suffix(".kicad_pro"), output_project
    )
    generator = ZoneGenerator.from_pcb(pcb_path, edge_clearance=inset)
    assignments = _assign_layers_for_pour_nets(len(generator.pcb.copper_layers), pours)
    outlines = _compute_pour_outlines(generator.pcb, assignments, generator.board_outline)
    targets = tuple(
        PlaneAccessTarget(name, layer, Polygon(outlines[name] or generator.board_outline))
        for name, layer, _ in assignments
    )
    return PlaneAccessPolicy(targets, rules)
