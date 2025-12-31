"""
CLI commands for KiCad design rules file (.kicad_dru) operations.

Provides the import-dru command to parse and display design rules files.
"""

import json
from pathlib import Path
from typing import Any

from kicad_tools.core.sexp_file import load_design_rules
from kicad_tools.sexp import SExp


def import_dru(file_path: Path, output_format: str = "text") -> int:
    """
    Parse and display a KiCad design rules file.

    Args:
        file_path: Path to .kicad_dru file
        output_format: "text" or "json"

    Returns:
        Exit code (0 for success)
    """
    try:
        sexp = load_design_rules(file_path)
    except Exception as e:
        print(f"Error loading design rules: {e}")
        return 1

    rules = _extract_design_rules(sexp)

    if output_format == "json":
        print(json.dumps(rules, indent=2))
    else:
        _print_design_rules(file_path, rules)

    return 0


def _extract_design_rules(sexp: SExp) -> dict[str, Any]:
    """Extract design rules from S-expression tree."""
    rules: dict[str, Any] = {
        "version": None,
        "rules": [],
    }

    for child in sexp.values:
        if not isinstance(child, SExp):
            continue

        if child.tag == "version":
            if child.values:
                rules["version"] = (
                    int(child.values[0])
                    if isinstance(child.values[0], (int, float))
                    else str(child.values[0])
                )

        elif child.tag == "rule":
            rule = _extract_rule(child)
            if rule:
                rules["rules"].append(rule)

    return rules


def _extract_rule(rule_sexp: SExp) -> dict[str, Any]:
    """Extract a single rule definition."""
    rule: dict[str, Any] = {}

    # Rule name is first value
    if rule_sexp.values and isinstance(rule_sexp.values[0], str):
        rule["name"] = rule_sexp.values[0]

    # Find constraint
    constraint = rule_sexp.find_child("constraint")
    if constraint:
        rule["constraint"] = _extract_constraint(constraint)

    # Find condition (optional)
    condition = rule_sexp.find_child("condition")
    if condition and condition.values:
        rule["condition"] = str(condition.values[0])

    # Find layer (optional)
    layer = rule_sexp.find_child("layer")
    if layer and layer.values:
        rule["layer"] = str(layer.values[0])

    return rule


def _extract_constraint(constraint_sexp: SExp) -> dict[str, Any]:
    """Extract constraint details."""
    constraint: dict[str, Any] = {}

    # Constraint type is first value
    if constraint_sexp.values and isinstance(constraint_sexp.values[0], str):
        constraint["type"] = constraint_sexp.values[0]

    # Find min/max/opt values
    for child in constraint_sexp.values:
        if isinstance(child, SExp):
            if child.tag == "min" and child.values:
                constraint["min"] = _parse_value_with_unit(child.values[0])
            elif child.tag == "max" and child.values:
                constraint["max"] = _parse_value_with_unit(child.values[0])
            elif child.tag == "opt" and child.values:
                constraint["opt"] = _parse_value_with_unit(child.values[0])

    return constraint


def _parse_value_with_unit(value: Any) -> dict[str, Any]:
    """Parse a value that may have a unit suffix."""
    if isinstance(value, (int, float)):
        return {"value": float(value), "unit": None}

    value_str = str(value)

    # Check for common units
    if value_str.endswith("mm"):
        try:
            return {"value": float(value_str[:-2]), "unit": "mm"}
        except ValueError:
            pass
    elif value_str.endswith("mil"):
        try:
            return {"value": float(value_str[:-3]), "unit": "mil"}
        except ValueError:
            pass
    elif value_str.endswith("in"):
        try:
            return {"value": float(value_str[:-2]), "unit": "in"}
        except ValueError:
            pass

    # Try to parse as plain number
    try:
        return {"value": float(value_str), "unit": None}
    except ValueError:
        return {"value": value_str, "unit": None}


def _print_design_rules(file_path: Path, rules: dict[str, Any]) -> None:
    """Print design rules in text format."""
    print(f"\nDesign Rules: {file_path.name}")
    print("=" * 60)

    if rules["version"]:
        print(f"Version: {rules['version']}")

    print(f"\nRules ({len(rules['rules'])} total):")
    print("-" * 60)

    for rule in rules["rules"]:
        name = rule.get("name", "(unnamed)")
        print(f"\n  {name}:")

        if "constraint" in rule:
            c = rule["constraint"]
            constraint_type = c.get("type", "unknown")
            print(f"    Type: {constraint_type}")

            if "min" in c:
                min_val = c["min"]
                unit = min_val.get("unit", "")
                print(f"    Min: {min_val['value']}{unit}")

            if "max" in c:
                max_val = c["max"]
                unit = max_val.get("unit", "")
                print(f"    Max: {max_val['value']}{unit}")

            if "opt" in c:
                opt_val = c["opt"]
                unit = opt_val.get("unit", "")
                print(f"    Opt: {opt_val['value']}{unit}")

        if "condition" in rule:
            print(f"    Condition: {rule['condition']}")

        if "layer" in rule:
            print(f"    Layer: {rule['layer']}")

    print("\n" + "=" * 60)
