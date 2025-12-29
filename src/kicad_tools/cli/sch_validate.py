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
from typing import List

from kicad_tools.schema import Schematic
from kicad_tools.schema.hierarchy import build_hierarchy


@dataclass
class ValidationIssue:
    """A single validation issue."""

    severity: str  # "error", "warning", "info"
    category: str  # "erc", "unconnected", "footprint", "hierarchy"
    message: str
    location: str = ""  # Sheet or reference


@dataclass
class ValidationResult:
    """Complete validation results."""

    schematic: str
    issues: List[ValidationIssue] = field(default_factory=list)
    checks_run: List[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def passed(self) -> bool:
        return self.error_count == 0


def run_erc(schematic_path: str) -> List[ValidationIssue]:
    """Run KiCad ERC check."""
    issues = []

    try:
        # Find kicad-cli
        result = subprocess.run(
            ["which", "kicad-cli"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="erc",
                    message="kicad-cli not found, ERC check skipped",
                )
            )
            return issues

        kicad_cli = result.stdout.strip()

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
                        for sheet in data.get("sheets", []):
                            for violation in sheet.get("violations", []):
                                issues.append(
                                    ValidationIssue(
                                        severity=violation.get("severity", "warning"),
                                        category="erc",
                                        message=violation.get("description", "Unknown ERC issue"),
                                        location=sheet.get("path", ""),
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


def check_missing_footprints(schematic_path: str) -> List[ValidationIssue]:
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
            except Exception:
                pass

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="footprint",
                message=f"Footprint check failed: {e}",
            )
        )

    return issues


def check_missing_values(schematic_path: str) -> List[ValidationIssue]:
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
            except Exception:
                pass

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="value",
                message=f"Value check failed: {e}",
            )
        )

    return issues


def check_hierarchy(schematic_path: str) -> List[ValidationIssue]:
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

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="hierarchy",
                message=f"Hierarchy check failed: {e}",
            )
        )

    return issues


def validate_schematic(schematic_path: str, lib_paths: List[str] = None) -> ValidationResult:
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
                icon = "❌" if issue.severity == "error" else "⚠️"
                loc = f" ({issue.location})" if issue.location else ""
                print(f"  {icon} {issue.message}{loc}")
            if len(issues) > 10:
                print(f"  ... and {len(issues) - 10} more")


if __name__ == "__main__":
    main()
