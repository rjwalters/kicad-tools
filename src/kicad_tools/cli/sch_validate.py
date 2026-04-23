#!/usr/bin/env python3
"""
Run all validation checks on a KiCad schematic.

Combines ERC, unconnected pin detection, and other checks into one command.

Usage:
    python3 sch-validate.py <schematic.kicad_sch> [options]

Options:
    --format {text,json}   Output format (default: text)
    --lib-path <path>      Path to symbol libraries (for pin checking)
    --strict               Exit with error on any warning
    --quiet                Only show errors, not warnings

Examples:
    python3 sch-validate.py project.kicad_sch
    python3 sch-validate.py project.kicad_sch --lib-path lib/symbols/
    python3 sch-validate.py project.kicad_sch --strict
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.erc.cross_sheet import filter_cross_sheet_global_labels
from kicad_tools.schema import Schematic
from kicad_tools.schema.hierarchy import build_hierarchy


@dataclass
class ValidationIssue:
    """A single validation issue."""

    severity: str  # "error", "warning", "info"
    category: str  # "erc", "unconnected", "footprint", "hierarchy"
    message: str
    location: str = ""  # Sheet or reference
    items: list[str] = field(default_factory=list)  # Contextual items (label/net names)


@dataclass
class ValidationResult:
    """Complete validation results."""

    schematic: str
    issues: list[ValidationIssue] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def passed(self) -> bool:
        return self.error_count == 0


# Violation types whose messages benefit from label/net name enrichment.
_LABEL_TYPES: frozenset[str] = frozenset({
    "isolated_pin_label",
    "single_global_label",
    "label_dangling",
    "global_label_dangling",
    "similar_labels",
    "multiple_net_names",
    "hier_label_mismatch",
})


def run_erc(schematic_path: str) -> list[ValidationIssue]:
    """Run KiCad ERC check."""
    issues = []

    try:
        # Find kicad-cli using the shared lookup that checks PATH
        # and platform-specific installation locations (e.g. macOS app bundle)
        kicad_cli_path = find_kicad_cli()
        if kicad_cli_path is None:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="erc",
                    message="kicad-cli not found, ERC check skipped",
                )
            )
            return issues

        kicad_cli = str(kicad_cli_path)

        # Run ERC
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                [
                    kicad_cli,
                    "sch",
                    "erc",
                    "--format",
                    "json",
                    "--severity-all",
                    "--output",
                    output_file,
                    schematic_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            # Parse results
            if Path(output_file).exists():
                import json as json_mod

                with open(output_file) as f:
                    content = f.read()
                    if content.strip():
                        data = json_mod.loads(content)

                        # Collect raw violation dicts so we can filter
                        # cross-sheet false positives before converting.
                        raw_violations: list[dict] = []
                        for sheet in data.get("sheets", []):
                            for violation in sheet.get("violations", []):
                                violation["_sheet_path"] = sheet.get("path", "")
                                raw_violations.append(violation)

                        # Filter false-positive single_global_label /
                        # isolated_pin_label violations for labels that
                        # actually appear on multiple sheets.
                        raw_violations = filter_cross_sheet_global_labels(
                            raw_violations, schematic_path
                        )

                        for violation in raw_violations:
                            item_descs = [
                                i.get("description", "")
                                for i in violation.get("items", [])
                                if i.get("description", "")
                            ]

                            desc = violation.get("description", "Unknown ERC issue")
                            vtype = violation.get("type", "")

                            # Enrich the message with item context for
                            # label-relevant violation types, but only if the
                            # description does not already contain the item
                            # text (some KiCad versions inline it).
                            if vtype in _LABEL_TYPES and item_descs:
                                new_parts = [
                                    d for d in item_descs if d not in desc
                                ]
                                if new_parts:
                                    desc = f"{desc} [{'; '.join(new_parts)}]"

                            issues.append(
                                ValidationIssue(
                                    severity=violation.get("severity", "warning"),
                                    category="erc",
                                    message=desc,
                                    location=violation.get("_sheet_path", ""),
                                    items=item_descs,
                                )
                            )
        finally:
            Path(output_file).unlink(missing_ok=True)

    except subprocess.TimeoutExpired:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="erc",
                message="ERC check timed out",
            )
        )
    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="erc",
                message=f"ERC check failed: {e}",
            )
        )

    return issues


def check_missing_footprints(schematic_path: str) -> list[ValidationIssue]:
    """Check for symbols missing footprints."""
    issues = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                for sym in sch.symbols:
                    # Skip power symbols
                    if sym.lib_id.startswith("power:"):
                        continue

                    # Skip DNP
                    if sym.dnp:
                        continue

                    # Check for missing footprint
                    if not sym.footprint or sym.footprint == "~":
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                category="footprint",
                                message=f"Missing footprint: {sym.reference} ({sym.value})",
                                location=node.get_path_string(),
                            )
                        )
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="footprint",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="footprint",
                message=f"Footprint check failed: {e}",
            )
        )

    return issues


def check_missing_values(schematic_path: str) -> list[ValidationIssue]:
    """Check for symbols missing values."""
    issues = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                for sym in sch.symbols:
                    # Skip power symbols
                    if sym.lib_id.startswith("power:"):
                        continue

                    # Check for missing value
                    if not sym.value or sym.value in ("~", "?"):
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                category="value",
                                message=f"Missing value: {sym.reference}",
                                location=node.get_path_string(),
                            )
                        )
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="value",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="value",
                message=f"Value check failed: {e}",
            )
        )

    return issues


def check_hierarchy(schematic_path: str) -> list[ValidationIssue]:
    """Check hierarchy for issues."""
    issues = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        # Check for unmatched hierarchical labels
        label_map = {}  # label_name -> list of locations

        for node in hierarchy.all_nodes():
            # Labels in this sheet
            for label in node.hierarchical_labels:
                if label not in label_map:
                    label_map[label] = []
                label_map[label].append(("label", node.name))

            # Pins on sheets
            for sheet in node.sheets:
                for pin in sheet.pins:
                    if pin.name not in label_map:
                        label_map[pin.name] = []
                    label_map[pin.name].append(("pin", sheet.name))

        # Check for labels without matching pins
        for name, locations in label_map.items():
            types = [loc[0] for loc in locations]
            if types.count("label") > 0 and types.count("pin") == 0:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="hierarchy",
                        message=f"Hierarchical label '{name}' has no matching sheet pin",
                        location=", ".join(loc[1] for loc in locations if loc[0] == "label"),
                    )
                )

            # Check for pins without matching labels
            if types.count("pin") > 0 and types.count("label") == 0:
                sheet_names = [loc[1] for loc in locations if loc[0] == "pin"]
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="hierarchy",
                        message=f"Sheet pin '{name}' has no matching hierarchical label in sub-schematic",
                        location=", ".join(sheet_names),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="hierarchy",
                message=f"Hierarchy check failed: {e}",
            )
        )

    return issues


def check_no_connect_on_input_pins(schematic_path: str) -> list[ValidationIssue]:
    """Flag no-connect markers placed on pins typed 'input' in the library.

    Input pins typically require a defined logic state.  Placing a no-connect
    flag on one silences the unconnected-pin check but may hide a real design
    issue (e.g. an active-low control left floating instead of being tied to a
    pull resistor).

    Only ``input`` pins are flagged -- ``passive``, ``no_connect``, and other
    types are intentionally excluded because no-connect markers on those are
    standard practice.
    """
    issues: list[ValidationIssue] = []

    try:
        from kicad_tools.schematic.models import Schematic as OpSchematic

        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = OpSchematic.load(node.path)

                nc_points = {(round(nc.x, 2), round(nc.y, 2)) for nc in sch.no_connects}
                if not nc_points:
                    continue

                for sym in sch.symbols:
                    # Skip power symbols -- their pins are always power_in/power_out
                    if sym.symbol_def.lib_id.startswith("power:"):
                        continue

                    for pin in sym.symbol_def.pins:
                        if pin.pin_type != "input":
                            continue

                        pos = sym.pin_position(pin.number)
                        pos_r = (round(pos[0], 2), round(pos[1], 2))

                        if pos_r in nc_points:
                            display = pin.name if pin.name and pin.name != "~" else pin.number
                            issues.append(
                                ValidationIssue(
                                    severity="info",
                                    category="no_connect",
                                    message=(
                                        f"No-connect on input pin {display} "
                                        f"(pin {pin.number}) of {sym.reference} "
                                        f"({sym.value}) -- verify this pin does "
                                        f"not need a defined state"
                                    ),
                                    location=node.get_path_string(),
                                )
                            )
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="no_connect",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="no_connect",
                message=f"No-connect input pin check failed: {e}",
            )
        )

    return issues


def check_global_label_directions(schematic_path: str) -> list[ValidationIssue]:
    """Check global label driver/receiver direction mismatches.

    Groups global labels by net name across all sheets and checks that each
    net has at least one driver and at least one receiver.

    Direction semantics:
      - ``output`` / ``tri_state``: driver only
      - ``input``: receiver only
      - ``bidirectional`` / ``passive``: counts as both driver and receiver
    """
    issues: list[ValidationIssue] = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        # Collect global labels across all sheets: net_name -> list of (shape, sheet_path)
        label_map: dict[str, list[tuple[str, str]]] = {}

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                sheet_path = node.get_path_string()
                for gl in sch.global_labels:
                    if gl.text not in label_map:
                        label_map[gl.text] = []
                    label_map[gl.text].append((gl.shape, sheet_path))
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="global_label",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

        # Shapes that count as driver (can source a signal)
        driver_shapes = {"output", "tri_state", "bidirectional", "passive"}
        # Shapes that count as receiver (can sink a signal)
        receiver_shapes = {"input", "bidirectional", "passive"}

        for net_name, entries in sorted(label_map.items()):
            shapes = {shape for shape, _ in entries}
            sheets = sorted({sheet for _, sheet in entries})
            has_driver = bool(shapes & driver_shapes)
            has_receiver = bool(shapes & receiver_shapes)

            if not has_driver:
                # All instances are input -- no driver exists
                shapes_str = ", ".join(sorted(shapes))
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="global_label",
                        message=(
                            f"Global label '{net_name}' has no driver "
                            f"(shapes: {shapes_str})"
                        ),
                        location=", ".join(sheets),
                    )
                )
            elif not has_receiver:
                # All instances are output/tri_state -- no receiver exists
                shapes_str = ", ".join(sorted(shapes))
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="global_label",
                        message=(
                            f"Global label '{net_name}' has no receiver "
                            f"(shapes: {shapes_str})"
                        ),
                        location=", ".join(sheets),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="global_label",
                message=f"Global label direction check failed: {e}",
            )
        )

    return issues


def check_missing_project_instances(schematic_path: str) -> list[ValidationIssue]:
    """Check for symbols missing the ``instances`` block.

    In KiCad 8+, every placed symbol must have an ``(instances ...)`` child
    node that registers it to a project path.  Without this block the
    component is invisible to the netlist exporter and BOM generator despite
    being visually present on the schematic.

    The check skips:
    - Power symbols (``lib_id`` starting with ``power:``)
    - Symbols with both ``in_bom=no`` and ``on_board=no`` (graphical-only)

    Multi-unit symbols are deduplicated by (reference, lib_id) so that a missing
    ``instances`` block on a two-unit IC produces a single warning, not one
    per unit.
    """
    issues: list[ValidationIssue] = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                # Track UUIDs already flagged to deduplicate multi-unit ICs.
                # Multi-unit symbols share the same base UUID (only the first
                # unit carries the instances block in the raw file, but after
                # parsing each unit becomes a separate SymbolInstance sharing
                # the same lib_id + reference).  Deduplicate by reference +
                # lib_id so we report once per logical component.
                seen: set[tuple[str, str]] = set()

                for sym in sch.symbols:
                    # Skip power symbols
                    if sym.lib_id.startswith("power:"):
                        continue

                    # Skip graphical-only symbols (not in BOM and not on board)
                    if not sym.in_bom and not sym.on_board:
                        continue

                    # Deduplicate multi-unit symbols
                    dedup_key = (sym.reference, sym.lib_id)
                    if dedup_key in seen:
                        continue

                    # Check for instances block in raw S-expression
                    has_instances = False
                    if sym._sexp is not None:
                        if sym._sexp.find("instances") is not None:
                            has_instances = True
                    else:
                        # Programmatically-created symbol without _sexp:
                        # skip with info if desired, but don't flag as missing
                        continue

                    if not has_instances:
                        seen.add(dedup_key)
                        ref = sym.reference or "?"
                        val = sym.value or "?"
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                category="project_instances",
                                message=(
                                    f"Missing project instances block: "
                                    f"{ref} ({val}) - will be absent from "
                                    f"netlist and BOM"
                                ),
                                location=node.get_path_string(),
                            )
                        )
                    else:
                        # Mark as seen even when instances are present, so
                        # other units of the same IC don't get flagged.
                        seen.add(dedup_key)
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="project_instances",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="project_instances",
                message=f"Project instances check failed: {e}",
            )
        )

    return issues


def validate_schematic(schematic_path: str, lib_paths: list[str] = None) -> ValidationResult:
    """Run all validation checks."""
    result = ValidationResult(schematic=schematic_path)

    # ERC
    result.checks_run.append("erc")
    result.issues.extend(run_erc(schematic_path))

    # Missing footprints
    result.checks_run.append("footprints")
    result.issues.extend(check_missing_footprints(schematic_path))

    # Missing values
    result.checks_run.append("values")
    result.issues.extend(check_missing_values(schematic_path))

    # Hierarchy
    result.checks_run.append("hierarchy")
    result.issues.extend(check_hierarchy(schematic_path))

    # No-connect on input pins
    result.checks_run.append("no_connect_input")
    result.issues.extend(check_no_connect_on_input_pins(schematic_path))

    # Global label directions
    result.checks_run.append("global_label_directions")
    result.issues.extend(check_global_label_directions(schematic_path))

    # Missing project instances
    result.checks_run.append("project_instances")
    result.issues.extend(check_missing_project_instances(schematic_path))

    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Path to symbol libraries"
    )
    parser.add_argument("--strict", action="store_true", help="Exit with error on any warning")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only show errors")

    args = parser.parse_args(argv)

    if not Path(args.schematic).exists():
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)

    result = validate_schematic(args.schematic, args.lib_paths)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "schematic": result.schematic,
                    "passed": result.passed,
                    "error_count": result.error_count,
                    "warning_count": result.warning_count,
                    "checks_run": result.checks_run,
                    "issues": [
                        {
                            "severity": i.severity,
                            "category": i.category,
                            "message": i.message,
                            "location": i.location,
                            "items": i.items,
                        }
                        for i in result.issues
                        if not args.quiet or i.severity == "error"
                    ],
                },
                indent=2,
            )
        )
    else:
        print_result(result, args.quiet)

    # Exit code
    if result.error_count > 0:
        sys.exit(1)
    if args.strict and result.warning_count > 0:
        sys.exit(1)


def print_result(result: ValidationResult, quiet: bool = False):
    """Print validation results."""
    print(f"Validation: {Path(result.schematic).name}")
    print("=" * 60)

    if result.passed and result.warning_count == 0:
        print("✅ All checks passed!")
    elif result.passed:
        print(f"⚠️  Passed with {result.warning_count} warnings")
    else:
        print(f"❌ Failed: {result.error_count} errors, {result.warning_count} warnings")

    print(f"\nChecks run: {', '.join(result.checks_run)}")

    # Group issues by category
    by_category = {}
    for issue in result.issues:
        if quiet and issue.severity != "error":
            continue
        if issue.category not in by_category:
            by_category[issue.category] = []
        by_category[issue.category].append(issue)

    if by_category:
        print("\nIssues:")
        for category, issues in sorted(by_category.items()):
            print(f"\n[{category.upper()}]")
            for issue in issues[:10]:  # Limit to 10 per category
                if issue.severity == "error":
                    icon = "❌"
                elif issue.severity == "info":
                    icon = "ℹ️"
                else:
                    icon = "⚠️"
                loc = f" ({issue.location})" if issue.location else ""
                print(f"  {icon} {issue.message}{loc}")
                for item in issue.items:
                    print(f"       {item}")
            if len(issues) > 10:
                print(f"  ... and {len(issues) - 10} more")


if __name__ == "__main__":
    main()
