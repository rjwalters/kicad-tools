"""Fail-closed native component proof for alternate airwire witnesses."""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from shapely.geometry import Polygon

from kicad_tools.zones.placement_fill import find_kicad_python


def _validate(data: dict[str, Any]) -> None:
    inventory, islands, groups = data["inventory"], data["islands"], data["groups"]
    if (
        not isinstance(inventory, dict)
        or not isinstance(islands, dict)
        or not isinstance(groups, list)
    ):
        raise ValueError("Malformed native component inventory")
    flat = [item for group in groups for item in group]
    if any(not isinstance(group, list) or not group for group in groups):
        raise ValueError("Malformed native component group")
    if len(flat) != len(set(flat)) or set(flat) != set(inventory):
        raise ValueError("Incomplete or overlapping native component membership")
    if not set(islands) <= set(inventory):
        raise ValueError("Unknown native island identity")
    for key, item in inventory.items():
        if not isinstance(key, str) or not key or not isinstance(item, dict):
            raise ValueError("Malformed native object identity")
        if item.get("kind") not in {"PAD", "PCB_TRACK", "PCB_ARC", "PCB_VIA", "ZONE"}:
            raise ValueError("Unsupported native copper object")
        if not isinstance(item.get("net"), str) or (item["kind"] == "ZONE") != (key in islands):
            raise ValueError("Malformed native copper kind or binding")


def compare_components(
    baseline: dict[str, Any], candidate: dict[str, Any], moved_vias: set[str]
) -> None:
    """Require every original copper component, including zone islands, to survive."""
    _validate(baseline)
    _validate(candidate)
    before, after = baseline["inventory"], candidate["inventory"]
    old_islands, new_islands = baseline["islands"], candidate["islands"]
    originals = set(before) - old_islands.keys()
    if not originals <= after.keys():
        raise ValueError("Relocation removed original copper")
    if any(before[key] != after[key] for key in originals):
        raise ValueError("Relocation changed original copper identity or net")
    if not moved_vias <= originals or any(before[key]["kind"] != "PCB_VIA" for key in moved_vias):
        raise ValueError("Unknown relocated via")

    def regions(islands):
        result = {}
        for key, island in islands.items():
            if not isinstance(island.get("zone"), str) or not isinstance(island.get("layer"), int):
                raise ValueError("Malformed native island provenance")
            shape = Polygon(island["outer"], island["holes"])
            if shape.is_empty or not shape.is_valid or shape.area <= 0:
                raise ValueError("Invalid native island geometry")
            result[key] = shape
        return result

    old_regions, new_regions = regions(old_islands), regions(new_islands)
    # Index order is not identity: require a one-to-one overlap relation within
    # each original zone/layer. Splits, merges and ambiguous matches refuse.
    matches = {}
    for key, island in new_islands.items():
        compatible = [
            old
            for old, previous in old_islands.items()
            if (island["zone"], island["layer"]) == (previous["zone"], previous["layer"])
            and new_regions[key].intersection(old_regions[old]).area > 0
        ]
        if len(compatible) != 1:
            raise ValueError("Native zone island split, merge or ambiguous correspondence")
        matches[key] = compatible[0]
        if before[compatible[0]] != after[key]:
            raise ValueError("Native island net changed")
    if len(set(matches.values())) != len(matches) or set(matches.values()) != set(old_islands):
        raise ValueError("Native zone island split, merge or disappearance")

    extras = set(after) - new_islands.keys() - originals
    if any(after[key]["kind"] != "PCB_TRACK" for key in extras):
        raise ValueError("Unexpected added copper object")
    projected = set()
    for group in candidate["groups"]:
        members = set(group)
        if members & extras and not members & moved_vias:
            raise ValueError("Added relocation stub is detached from relocated via")
        normalized = frozenset(matches.get(key, key) for key in members - extras)
        if not normalized:
            raise ValueError("Relocation introduced an isolated component")
        projected.add(normalized)
    expected = {frozenset(group) for group in baseline["groups"]}
    if projected != expected:
        raise ValueError("Relocation changed complete native copper components")


def prove_components(
    baseline: Path,
    candidate: Path,
    executable: Path,
    moved_vias: set[str],
    reports: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    python = find_kicad_python()
    if python is None:
        raise RuntimeError("Native KiCad Python is required to prove alternate airwire witnesses")
    version = subprocess.check_output([str(executable), "version"], text=True, timeout=10).strip()
    worker = Path(__file__).with_name("_native_components.py")
    results = []
    for board in (baseline, candidate):
        output = board.with_suffix(".components.json")
        completed = subprocess.run(
            [str(python), str(worker), str(board.resolve()), str(output.resolve())],
            capture_output=True,
            text=True,
            timeout=180,
        )
        if completed.returncode:
            raise RuntimeError("Native copper component extraction failed")
        result = json.loads(output.read_text())
        if result.get("version") != version:
            raise RuntimeError("Native CLI/Python version mismatch in connectivity proof")
        results.append(result)
    for result, report in zip(results, reports, strict=True):
        known = set(result["inventory"]) | {i["zone"] for i in result["islands"].values()}
        for finding in report["unconnected_items"]:
            if any(item["uuid"] not in known for item in finding["items"]):
                raise ValueError("Unknown native airwire witness identity")
    compare_components(results[0], results[1], moved_vias)
