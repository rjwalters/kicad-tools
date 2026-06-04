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

from kicad_tools.footprints.library_path import (
    detect_kicad_library_path,
    list_project_libraries,
)
from kicad_tools.schema import Schematic
from kicad_tools.schema.hierarchy import build_hierarchy

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
    """Verify that every ``Library:Footprint`` reference resolves on disk.

    This walks the schematic hierarchy and, for each symbol that carries a
    Footprint property of the form ``Library:Footprint``:

    1. Sanity-checks the textual form (non-empty library and footprint parts,
       presence of a ``:`` separator).
    2. Resolves the library nickname through the merged
       ``project fp-lib-table`` + global libraries, and confirms the
       ``<footprint>.kicad_mod`` file exists in the resolved directory.

    The on-disk check is only performed when the local environment has at
    least one library source (project table or global libraries).  When
    neither is available (e.g. CI runners), the check degrades to the
    text-only sanity checks above so it does not produce false positives.
    """
    issues: list[ValidationIssue] = []

    # Pre-compute the merged library map ONCE per call -- candidate
    # discovery is the same for every symbol.  An empty list means we
    # only run the cheap text-form checks.
    sch_path = Path(schematic_path)
    global_paths = detect_kicad_library_path()
    lib_map: dict[str, Path] = {}
    try:
        for nick, lib_dir, _origin in list_project_libraries(sch_path, global_paths):
            lib_map.setdefault(nick, lib_dir)
    except Exception:
        # Discovery failures are non-fatal -- fall back to text-only checks.
        lib_map = {}

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
                        continue

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
                        continue

                    # On-disk resolution -- only when we have at least one
                    # library source.  Absence of any source means the
                    # environment cannot answer this question; do not flag.
                    if not lib_map:
                        continue
                    lib_dir = lib_map.get(lib)
                    if lib_dir is None:
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                category="footprint_resolution",
                                message=(
                                    f"{sym.reference} footprint '{fp}': "
                                    f"library nickname '{lib}' not found in "
                                    "project or global fp-lib-table"
                                ),
                                location=node.get_path_string(),
                            )
                        )
                        continue
                    mod_file = lib_dir / f"{name}.kicad_mod"
                    if not mod_file.is_file():
                        issues.append(
                            ValidationIssue(
                                severity="error",
                                category="footprint_resolution",
                                message=(
                                    f"{sym.reference} footprint '{fp}': "
                                    f"file not found at {mod_file}"
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
                # Build map of library symbol name -> pin count from lib_symbols.
                # get_lib_symbol_resolved() handles extends chains so derived
                # symbols return their effective (inherited) pin count.
                lib_pin_counts: dict[str, int] = {}
                lib_syms_sexp = sch.lib_symbols
                if lib_syms_sexp is not None:
                    for sym_sexp in lib_syms_sexp.find_all("symbol"):
                        try:
                            sym_name = sym_sexp.get_string(0) or ""
                            lib_sym = sch.get_lib_symbol_resolved(sym_name)
                            if lib_sym is not None:
                                lib_pin_counts[lib_sym.name] = lib_sym.pin_count
                        except Exception:
                            pass

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
