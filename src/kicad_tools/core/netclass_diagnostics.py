"""Read-only inspection of project netclasses, separate from writer-oriented APIs.

See ``docs/guides/netclass-diagnostics.md`` for the KiCad 10.0.5 matching
contract and native oracle. Results describe independent memberships, not
assignment precedence, composite classes, or effective electrical constraints.
"""

from copy import deepcopy
from string import ascii_letters, digits
from typing import Any

_LITERAL = frozenset(ascii_letters + digits + "_ -:")


def _supported(pattern: str) -> bool:
    return all(c in _LITERAL or c in "*?" for c in pattern) and not any(
        a in "*?" and b in "*?" for a, b in zip(pattern, pattern[1:], strict=False)
    )


def _matches(pattern: str, net: str) -> bool:
    """Union of anchored wildcard and restricted anchored regex, without backtracking."""
    # NET_SETTINGS does not assign classes to the unnamed net.
    if not net:
        return False
    ends = {len(net)}
    if net.endswith("\n"):
        ends.add(len(net) - 1)
    positions = {0}
    for char in pattern:
        if char == "*":
            positions = set(range(min(positions), len(net) + 1)) if positions else set()
        else:
            positions = {
                i + 1 for i in positions if i < len(net) and (char == "?" or net[i] == char)
            }
    if ends & positions:
        return True
    # A leading quantifier is invalid regex; the wildcard branch still applies.
    if pattern.startswith(("*", "?")):
        return False
    positions = {0}
    index = 0
    while index < len(pattern):
        char = pattern[index]
        quantifier = pattern[index + 1 : index + 2]
        if quantifier in ("*", "?"):
            next_positions = set(positions)
            for start in positions:
                end = start
                while end < len(net) and net[end] == char:
                    end += 1
                    next_positions.add(end)
                    if quantifier == "?":
                        break
            positions = next_positions
            index += 2
        else:
            positions = {i + 1 for i in positions if i < len(net) and net[i] == char}
            index += 1
    return bool(ends & positions)


def diagnose_netclasses(data: Any, net_names: Any = None) -> dict[str, Any]:
    """Inspect loaded project JSON without modifying it or reading/writing disk.

    ``net_names`` is an optional list, tuple, set or frozenset of strings. None
    means not evaluated; an empty collection means evaluated with no nets.
    Invalid inventories are diagnosed and not partially evaluated. Matches are
    unique and sorted. Original entries are deep-copied, retaining duplicates,
    order, unknown fields, and malformed values for inspection.

    This validates assignment structure and class names, not electrical values
    in definitions. Unsupported patterns have no inferred matches. Legacy
    explicit assignment forms are reported unsupported, never migrated here.
    """
    report: dict[str, Any] = {
        "settings_status": "missing",
        "default_status": "absent",
        "inventory_status": "not_evaluated",
        "classes": [],
        "patterns": [],
        "assignments": [],
        "diagnostics": [],
    }

    def diagnostic(code: str, path: str, message: str) -> None:
        report["diagnostics"].append({"code": code, "path": path, "message": message})

    inventory = None
    if net_names is not None:
        if not isinstance(net_names, (list, tuple, set, frozenset)) or not all(
            isinstance(net, str) for net in net_names
        ):
            report["inventory_status"] = "invalid"
            diagnostic(
                "invalid_inventory", "net_names", "Expected a collection of net-name strings."
            )
        else:
            inventory = sorted(set(net_names))
            report["inventory_status"] = "evaluated"

    if not isinstance(data, dict):
        report["settings_status"] = "invalid"
        diagnostic("invalid_project", "$", "Expected a project JSON object.")
        return report
    if "net_settings" not in data:
        diagnostic("missing_settings", "net_settings", "No net settings are declared.")
        diagnostic("missing_default", "net_settings.classes", "Default is not declared.")
        return report
    settings = data["net_settings"]
    if not isinstance(settings, dict):
        report["settings_status"] = "invalid"
        diagnostic("invalid_settings", "net_settings", "Expected an object.")
        return report
    report["settings_status"] = "present"

    def entries(field: str) -> list[Any]:
        value = settings.get(field, [])
        if not isinstance(value, list):
            diagnostic("invalid_container", f"net_settings.{field}", "Expected an array.")
            return []
        return value

    names: set[str] = set()
    for index, entry in enumerate(entries("classes")):
        path = f"net_settings.classes[{index}]"
        name = entry.get("name") if isinstance(entry, dict) else None
        report["classes"].append({"index": index, "name": deepcopy(name), "entry": deepcopy(entry)})
        if not isinstance(name, str) or not name:
            diagnostic("invalid_class", path, "Expected an object with a nonempty class name.")
        elif name in names:
            diagnostic("duplicate_class", path, f"Class {name!r} is declared more than once.")
        else:
            names.add(name)
        if isinstance(entry, dict) and "nets" in entry:
            diagnostic(
                "unsupported_assignment",
                path + ".nets",
                "Legacy class-local assignments are not evaluated.",
            )
    report["default_status"] = "declared" if "Default" in names else "absent"
    if "Default" not in names:
        diagnostic("missing_default", "net_settings.classes", "Default is not declared.")

    def target_defined(target: Any, path: str) -> bool | None:
        if not isinstance(target, str) or not target:
            diagnostic("invalid_target", path, "Expected a nonempty class name.")
            return None
        if target not in names:
            diagnostic("undefined_class", path, f"Class {target!r} is not declared.")
            return False
        return True

    for index, entry in enumerate(entries("netclass_patterns")):
        path = f"net_settings.netclass_patterns[{index}]"
        pattern = entry.get("pattern") if isinstance(entry, dict) else None
        target = entry.get("netclass") if isinstance(entry, dict) else None
        row = {
            "index": index,
            "entry": deepcopy(entry),
            "pattern": deepcopy(pattern),
            "target": deepcopy(target),
            "target_defined": target_defined(target, path + ".netclass"),
            "status": "invalid",
            "matches": None,
        }
        report["patterns"].append(row)
        if not isinstance(pattern, str):
            diagnostic("invalid_pattern", path + ".pattern", "Expected a pattern string.")
            continue
        if not _supported(pattern):
            row["status"] = "unsupported"
            diagnostic(
                "unsupported_pattern", path + ".pattern", "Outside the verified literal/*/? subset."
            )
            continue
        row["status"] = "supported"
        if inventory is not None:
            row["matches"] = [net for net in inventory if _matches(pattern, net)]
            if not row["matches"]:
                diagnostic("no_matches", path + ".pattern", "No supplied board nets match.")

    assignments = settings.get("netclass_assignments")
    if assignments is not None:
        if not isinstance(assignments, dict):
            diagnostic(
                "unsupported_assignment",
                "net_settings.netclass_assignments",
                "Expected a net-name object of class-name arrays.",
            )
        else:
            for index, (net, targets) in enumerate(assignments.items()):
                path = f"net_settings.netclass_assignments[{index}]"
                if not isinstance(net, str) or not isinstance(targets, list):
                    report["assignments"].append(
                        {
                            "index": index,
                            "net": deepcopy(net),
                            "entry": deepcopy(targets),
                            "status": "unsupported",
                        }
                    )
                    diagnostic(
                        "unsupported_assignment",
                        path,
                        "Expected a string net name and an array of class names; legacy strings are not migrated.",
                    )
                    continue
                if not targets:
                    report["assignments"].append(
                        {
                            "index": index,
                            "net": net,
                            "entry": [],
                            "status": "supported",
                            "net_present": net in inventory if inventory is not None else None,
                        }
                    )
                for target_index, target in enumerate(targets):
                    defined = target_defined(target, f"{path}[{target_index}]")
                    report["assignments"].append(
                        {
                            "index": index,
                            "target_index": target_index,
                            "net": net,
                            "target": deepcopy(target),
                            "target_defined": defined,
                            "status": "supported" if defined is not None else "invalid",
                            "net_present": net in inventory if inventory is not None else None,
                        }
                    )
    return report
