"""Resolve project netclass clearances without changing project data.

This is netclass resolution, not a custom .kicad_dru rule evaluator. Pattern
support is the native-verified subset in netclass_diagnostics; unsupported
input raises instead of silently routing with weaker constraints.
"""

from __future__ import annotations

import math
from copy import deepcopy
from dataclasses import dataclass
from typing import Any, Iterable

from .netclass_diagnostics import diagnose_netclasses


class ProjectClearanceError(ValueError):
    """The project's effective netclass clearance cannot be resolved safely."""


@dataclass(frozen=True)
class NetclassClearance:
    """An effective electrical requirement and the class supplying it."""

    clearance: float
    source_class: str


def resolve_project_clearances(
    project: dict[str, Any], net_names: Iterable[str]
) -> dict[str, NetclassClearance]:
    """Resolve schema 3–5 netclasses with native priority/inheritance semantics.

    Smaller priority numbers win; ties sort by class name. A class lacking a
    clearance inherits from the next matching class that supplies one, then
    Default. Default is not a minimum imposed on explicitly assigned classes.
    Schema 3 class order and string assignments are migrated in a private copy.
    No manufacturer choice or route relaxation changes these authored values.

    Missing net_settings returns an empty mapping. Malformed declarations,
    unsupported patterns/schemas, and unresolved references fail explicitly.
    Custom DRC rules, board minima and fabrication limits remain separate
    constraints for the routing caller to combine.
    """
    if not isinstance(project, dict):
        raise ProjectClearanceError("Expected a project object")
    if isinstance(net_names, (str, bytes)):
        raise ProjectClearanceError("Net names must be a collection of strings")
    names = list(net_names)
    if not all(isinstance(name, str) for name in names):
        raise ProjectClearanceError("Net names must be strings")
    if "net_settings" not in project:
        return {}
    data = deepcopy(project)
    settings = data["net_settings"]
    if not isinstance(settings, dict):
        raise ProjectClearanceError("net_settings must be an object")
    meta = settings.get("meta", {})
    version = meta.get("version") if isinstance(meta, dict) else None
    if type(version) is not int or version not in (3, 4, 5):
        raise ProjectClearanceError(f"Unsupported net_settings schema: {version!r}")
    classes = settings.get("classes", [])
    if not isinstance(classes, list):
        raise ProjectClearanceError("net_settings.classes must be an array")
    if version == 3:
        priority = 0
        for item in classes:
            if not isinstance(item, dict):
                continue
            if item.get("name") == "Default":
                item["priority"] = 2**31 - 1
            else:
                item["priority"] = priority
                priority += 1
        assignments = settings.get("netclass_assignments")
        if isinstance(assignments, dict):
            for net, value in assignments.items():
                if not isinstance(value, str):
                    raise ProjectClearanceError("Schema 3 assignments must be strings")
                assignments[net] = [value] if value else []
    if not any(isinstance(item, dict) and item.get("name") == "Default" for item in classes):
        classes.append({"name": "Default", "clearance": 0.2, "priority": 2**31 - 1})
        settings["classes"] = classes
    report = diagnose_netclasses(data, names)
    ignored = {"missing_default", "no_matches"}
    errors = [d for d in report["diagnostics"] if d["code"] not in ignored]
    if errors:
        raise ProjectClearanceError("; ".join(f"{d['path']}: {d['message']}" for d in errors))

    definitions = {item["name"]: item for item in classes}
    for name, item in definitions.items():
        priority = item.get("priority", -1)
        if type(priority) is not int or not -(2**31) <= priority < 2**31:
            raise ProjectClearanceError(f"Invalid priority for class {name!r}")
        value = item.get("clearance")
        if value is None and name != "Default":
            continue
        if (
            isinstance(value, bool)
            or not isinstance(value, (int, float))
            or value < 0
            or value > (2**31 - 1) / 1_000_000
            or not math.isfinite(value)
        ):
            raise ProjectClearanceError(f"Invalid clearance for class {name!r}")

    memberships: dict[str, set[str]] = {name: set() for name in names if name}
    for row in report["patterns"]:
        for net in row["matches"]:
            memberships[net].add(row["target"])
    for net, targets in (settings.get("netclass_assignments") or {}).items():
        if net in memberships:
            memberships[net].update(targets)

    result = {}
    for net, members in memberships.items():
        ordered = sorted(members, key=lambda name: (definitions[name].get("priority", -1), name))
        for name in [*ordered, "Default"]:
            value = definitions[name].get("clearance")
            if value is not None:
                result[net] = NetclassClearance(float(value), name)
                break
    return result
