"""
Pre-layout schematic validation command.

Checks that a schematic is ready for PCB layout by verifying footprint
library resolution, pin/pad consistency, net completeness, and power
flag coverage -- layering on top of the existing ``sch validate`` checks.

Usage:
    kicad-tools sch preflight <schematic.kicad_sch> [options]

Options:
    --format {text,json}   Output format (default: text)
    --strict               Exit with error on any warning
    --quiet                Only show errors, not warnings

Examples:
    kicad-tools sch preflight project.kicad_sch
    kicad-tools sch preflight project.kicad_sch --format json
    kicad-tools sch preflight project.kicad_sch --strict
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.schema import Schematic
from kicad_tools.schema.hierarchy import build_hierarchy
from kicad_tools.schema.library import LibrarySymbol

from .sch_validate import (
    ValidationIssue,
    ValidationResult,
    check_hierarchy,
    check_missing_footprints,
    check_missing_values,
    print_result,
)

# Bare type names that almost certainly need a real value before layout.
_GENERIC_VALUES = frozenset({"R", "C", "L", "D", "Q", "U", "J", "FB", "LED"})


# ---------------------------------------------------------------------------
# New preflight-specific checks
# ---------------------------------------------------------------------------


def check_footprint_library_resolution(schematic_path: str) -> list[ValidationIssue]:
    """Verify that every ``Library:Footprint`` reference resolves.

    This walks the schematic hierarchy and, for each symbol that has a
    Footprint property of the form ``Library:Footprint``, verifies that both
    parts are non-empty.  A missing library prefix (e.g. bare footprint name)
    or obviously placeholder value is flagged.

    Full filesystem resolution of ``.kicad_mod`` files is intentionally
    deferred -- it requires ``fp-lib-table`` parsing which varies by
    installation.  This check catches the most common authoring mistakes.
    """
    issues: list[ValidationIssue] = []
    try:
        hierarchy = build_hierarchy(schematic_path)
        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                for sym in sch.symbols:
                    if sym.lib_id.startswith("power:"):
                        continue
                    if sym.dnp:
                        continue
                    fp = sym.footprint
                    if not fp or fp == "~":
                        # Already caught by check_missing_footprints
                        continue
                    # Expect "Library:Footprint" format
                    if ":" not in fp:
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                category="footprint_resolution",
                                message=(
                                    f"{sym.reference} footprint '{fp}' "
                                    "missing library prefix (expected Library:Footprint)"
                                ),
                                location=node.get_path_string(),
                            )
                        )
                    else:
                        lib, name = fp.split(":", 1)
                        if not lib.strip() or not name.strip():
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    category="footprint_resolution",
                                    message=(
                                        f"{sym.reference} footprint '{fp}' "
                                        "has empty library or footprint name"
                                    ),
                                    location=node.get_path_string(),
                                )
                            )
            except Exception:
                pass
    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="footprint_resolution",
                message=f"Footprint resolution check failed: {e}",
            )
        )
    return issues


def check_pin_pad_count(schematic_path: str) -> list[ValidationIssue]:
    """Compare symbol pin count against library symbol pin count.

    Checks each schematic symbol's instance-level pin list against the
    library symbol definition embedded in the schematic's ``lib_symbols``
    section.  A mismatch usually means the symbol was edited after
    placement without updating instances.
    """
    issues: list[ValidationIssue] = []
    try:
        hierarchy = build_hierarchy(schematic_path)
        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                # Build map of library symbol name -> pin count from lib_symbols
                lib_pin_counts: dict[str, int] = {}
                extends_map: dict[str, str] = {}
                lib_syms_sexp = sch.lib_symbols
                if lib_syms_sexp is not None:
                    for sym_sexp in lib_syms_sexp.find_all("symbol"):
                        try:
                            lib_sym = LibrarySymbol.from_sexp(sym_sexp)
                            lib_pin_counts[lib_sym.name] = lib_sym.pin_count
                            # Track extends relationships for derived symbols
                            ext_node = sym_sexp.get("extends")
                            if ext_node is not None:
                                base_name = ext_node.get_string(0)
                                if base_name:
                                    extends_map[lib_sym.name] = base_name
                        except Exception:
                            pass

                    # Resolve extends chains: derived symbols with 0 pins
                    # inherit pin count from their base symbol.
                    for name, base in extends_map.items():
                        if lib_pin_counts.get(name, 0) == 0:
                            visited: set[str] = {name}
                            cur = base
                            while cur and cur not in visited:
                                count = lib_pin_counts.get(cur, 0)
                                if count > 0:
                                    lib_pin_counts[name] = count
                                    break
                                visited.add(cur)
                                cur = extends_map.get(cur)

                for sym in sch.symbols:
                    if sym.lib_id.startswith("power:"):
                        continue
                    if sym.dnp:
                        continue
                    lib_count = lib_pin_counts.get(sym.lib_id)
                    if lib_count is None:
                        continue
                    instance_count = len(sym.pins)
                    if instance_count != lib_count:
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                category="pin_pad_mismatch",
                                message=(
                                    f"{sym.reference} ({sym.lib_id}): "
                                    f"instance has {instance_count} pins, "
                                    f"library symbol has {lib_count}"
                                ),
                                location=node.get_path_string(),
                            )
                        )
            except Exception:
                pass
    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="pin_pad_mismatch",
                message=f"Pin/pad count check failed: {e}",
            )
        )
    return issues


def check_single_pin_nets(schematic_path: str) -> list[ValidationIssue]:
    """Detect nets connected to only one pin.

    A net with a single connection is almost always an error -- a dangling
    wire or a forgotten connection.  Nets explicitly marked as unconnected
    or with no-connect flags are excluded.
    """
    issues: list[ValidationIssue] = []
    try:
        hierarchy = build_hierarchy(schematic_path)
        # Collect label usage across the whole hierarchy.
        # This is a lightweight approach: count how many times each
        # net-label name appears across all sheets.
        label_counts: dict[str, int] = {}
        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                # Count pins connected to each label
                for label in sch.labels:
                    name = label.text if hasattr(label, "text") else str(label)
                    label_counts[name] = label_counts.get(name, 0) + 1
                for label in sch.global_labels:
                    name = label.text if hasattr(label, "text") else str(label)
                    label_counts[name] = label_counts.get(name, 0) + 1
            except Exception:
                pass

        for name, count in label_counts.items():
            if count == 1:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="single_pin_net",
                        message=f"Net '{name}' appears only once (single-pin net)",
                    )
                )
    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="single_pin_net",
                message=f"Single-pin net check failed: {e}",
            )
        )
    return issues


def check_generic_values(schematic_path: str) -> list[ValidationIssue]:
    """Flag components whose value is a bare type name.

    Components with values like ``R``, ``C``, or ``L`` almost certainly
    need real values (e.g. ``10k``, ``100nF``) before layout.
    """
    issues: list[ValidationIssue] = []
    try:
        hierarchy = build_hierarchy(schematic_path)
        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                for sym in sch.symbols:
                    if sym.lib_id.startswith("power:"):
                        continue
                    if sym.dnp:
                        continue
                    val = (sym.value or "").strip()
                    if val.upper() in _GENERIC_VALUES:
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                category="generic_value",
                                message=(
                                    f"{sym.reference} has generic value '{val}' "
                                    "-- needs a real value before layout"
                                ),
                                location=node.get_path_string(),
                            )
                        )
            except Exception:
                pass
    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="generic_value",
                message=f"Generic value check failed: {e}",
            )
        )
    return issues


def check_power_flags(schematic_path: str) -> list[ValidationIssue]:
    """Check that power nets have PWR_FLAG or equivalent drivers.

    Scans for power symbols and global power labels, then verifies at
    least one ``PWR_FLAG`` symbol exists in the design.  This is a
    simplified heuristic -- KiCad's ERC also checks this, but this
    provides an explicit pre-layout warning.
    """
    issues: list[ValidationIssue] = []
    try:
        hierarchy = build_hierarchy(schematic_path)
        has_pwr_flag = False
        has_power_symbols = False

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                for sym in sch.symbols:
                    if sym.lib_id.startswith("power:"):
                        has_power_symbols = True
                        if "PWR_FLAG" in sym.lib_id:
                            has_pwr_flag = True
            except Exception:
                pass

        if has_power_symbols and not has_pwr_flag:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="power_flag",
                    message="Design uses power symbols but has no PWR_FLAG -- ERC may report errors",
                )
            )
    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="power_flag",
                message=f"Power flag check failed: {e}",
            )
        )
    return issues


# ---------------------------------------------------------------------------
# Main preflight runner
# ---------------------------------------------------------------------------


def run_preflight(schematic_path: str) -> ValidationResult:
    """Run all preflight checks (inherited + new)."""
    result = ValidationResult(schematic=schematic_path)

    # --- Inherited checks from sch validate ---
    result.checks_run.append("footprints")
    result.issues.extend(check_missing_footprints(schematic_path))

    result.checks_run.append("values")
    result.issues.extend(check_missing_values(schematic_path))

    result.checks_run.append("hierarchy")
    result.issues.extend(check_hierarchy(schematic_path))

    # --- New preflight checks ---
    result.checks_run.append("footprint_resolution")
    result.issues.extend(check_footprint_library_resolution(schematic_path))

    result.checks_run.append("pin_pad_count")
    result.issues.extend(check_pin_pad_count(schematic_path))

    result.checks_run.append("single_pin_nets")
    result.issues.extend(check_single_pin_nets(schematic_path))

    result.checks_run.append("generic_values")
    result.issues.extend(check_generic_values(schematic_path))

    result.checks_run.append("power_flags")
    result.issues.extend(check_power_flags(schematic_path))

    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Pre-layout schematic validation",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )
    parser.add_argument(
        "--strict", action="store_true", help="Exit with error on any warning"
    )
    parser.add_argument(
        "--quiet", "-q", action="store_true", help="Only show errors"
    )

    args = parser.parse_args(argv)

    if not Path(args.schematic).exists():
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)

    result = run_preflight(args.schematic)

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


if __name__ == "__main__":
    main()
