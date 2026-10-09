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


#: The schema an unversioned ``net_settings`` block is read as: the newest one
#: modelled here (the KiCad 10.0.6 oracle captures it). Only schema 3 is
#: treated differently (string assignments, implicit priorities), so 4 vs 5
#: makes no difference to the result.
CURRENT_NET_SETTINGS_SCHEMA = 5


def _schema_version(settings: dict[str, Any]) -> object:
    """The ``net_settings`` schema version, reading an unversioned block as current.

    Issue #6262: KiCad's ``NESTED_SETTINGS::LoadFromFile`` treats a missing or
    unreadable ``meta.version`` as "no migration needed" and loads the block
    as its current schema -- kicad-cli 10 enforces the classes and patterns of
    a ``net_settings`` with no ``meta`` (pinned by the ``hv-class-*``
    scenarios of ``tests/test_route_auto_pair_and_hole_clearance_6122_6139``).
    Hand-written projects and ``merge_project_rules``'s own
    ``setdefault("net_settings", {})`` produce exactly that shape, so a
    missing ``meta``, a missing ``version`` or a JSON ``null`` one is read the
    way KiCad reads it.  A version that IS stated but is not one this module
    models (an older schema needing migration, or a newer one) still raises.
    """
    meta = settings.get("meta")
    if meta is None:
        return CURRENT_NET_SETTINGS_SCHEMA
    if not isinstance(meta, dict):
        raise ProjectClearanceError(f"net_settings.meta must be an object, not {meta!r}")
    version = meta.get("version")
    return CURRENT_NET_SETTINGS_SCHEMA if version is None else version


def resolve_project_clearances(
    project: dict[str, Any], net_names: Iterable[str]
) -> dict[str, NetclassClearance]:
    """Resolve schema 3–5 netclasses with native priority/inheritance semantics.

    Smaller priority numbers win; ties sort by class name. A class lacking a
    clearance inherits from the next matching class that supplies one, then
    Default. Default is not a minimum imposed on explicitly assigned classes.
    Schema 3 class order and string assignments are migrated in a private copy.
    No manufacturer choice or route relaxation changes these authored values.

    Missing or ``null`` net_settings returns an empty mapping (no authored
    netclasses); an unversioned block is read as the current schema, as KiCad
    reads it (#6262). Malformed declarations, unsupported patterns, a stated
    but unsupported schema, and unresolved references fail explicitly.
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
    if project.get("net_settings") is None:
        # Absent or JSON ``null``: the project declares no netclasses, so
        # there is nothing authored to resolve (#6262).
        return {}
    data = deepcopy(project)
    settings = data["net_settings"]
    if not isinstance(settings, dict):
        raise ProjectClearanceError("net_settings must be an object")
    version = _schema_version(settings)
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

    # Unconnected copper uses Default; wildcard assignments cannot give an
    # unnamed net an electrical identity.
    result = (
        {"": NetclassClearance(float(definitions["Default"]["clearance"]), "Default")}
        if "" in names
        else {}
    )
    for net, members in memberships.items():
        ordered = sorted(members, key=lambda name: (definitions[name].get("priority", -1), name))
        for name in [*ordered, "Default"]:
            value = definitions[name].get("clearance")
            if value is not None:
                result[net] = NetclassClearance(float(value), name)
                break
    return result


def authored_netclass_clearances(
    project: dict[str, Any], net_names: Iterable[str]
) -> dict[str, NetclassClearance]:
    """Nets whose effective clearance comes from a netclass other than ``Default``.

    Issue #6243: these are the designer-authored electrical minima the router
    must enforce per net.  ``Default`` itself is deliberately excluded -- it is
    the board-wide base that ``clearance_resolver.resolve_base_clearance``
    already folds into the router's scalar clearance alongside the fab tier and
    ``--clearance``, and which the manufacturer-profile project rewrite owns
    (``merge_project_rules`` relaxes it to the fab floor).  A named class's
    clearance is a statement the scalar path cannot express, so it is returned
    whatever its value; the caller drops the ones its own base already covers.

    A project that declares no clearance on any class other than ``Default``
    cannot give a net a non-``Default`` clearance, so it returns ``{}`` without
    consulting the full resolver -- which keeps a ``Default``-only project
    routable whatever its schema metadata says.  Anything else goes through
    :func:`resolve_project_clearances` and inherits its explicit failures for
    unsupported declarations.
    """
    if not isinstance(project, dict) or project.get("net_settings") is None:
        return {}
    settings = project["net_settings"]
    classes = settings.get("classes") if isinstance(settings, dict) else None
    if isinstance(classes, list) and not any(
        isinstance(item, dict) and item.get("name") != "Default" and "clearance" in item
        for item in classes
    ):
        return {}
    names = [name for name in net_names if isinstance(name, str) and name]
    resolved = resolve_project_clearances(project, names)
    return {
        name: value for name, value in resolved.items() if name and value.source_class != "Default"
    }
