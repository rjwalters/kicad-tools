"""
Manufacturing readiness audit command for KiCad designs.

Runs comprehensive pre-manufacturing checks:
- ERC (Electrical Rules Check) on schematic
- DRC (Design Rules Check) on PCB
- Net connectivity (all nets routed)
- Manufacturer compatibility (design rules meet fab specs)
- Layer utilization statistics
- Cost estimation

Usage:
    kct audit project.kicad_pro --mfr jlcpcb
    kct audit board.kicad_pcb --mfr jlcpcb --skip-erc
    kct audit project.kicad_pro --format json --strict

Exit Codes:
    0 - Ready for manufacturing (no errors)
    1 - Not ready (errors found) or command failure
    2 - Warnings found (only with --strict)
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.audit import AuditResult, AuditVerdict, ManufacturingAudit
from kicad_tools.manufacturers import get_manufacturer_ids


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct audit command."""
    parser = argparse.ArgumentParser(
        prog="kct audit",
        description="Manufacturing readiness audit for KiCad designs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument(
        "project_or_pcb",
        help="Path to .kicad_pro or .kicad_pcb file",
    )
    parser.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format (default: table)",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=get_manufacturer_ids(),
        default="jlcpcb",
        help="Target manufacturer (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        help="Layer count (auto-detected if not specified)",
    )
    parser.add_argument(
        "--copper",
        "-c",
        type=float,
        default=1.0,
        help="Copper weight in oz (default: 1.0)",
    )
    parser.add_argument(
        "--quantity",
        "-q",
        type=int,
        default=5,
        help="Quantity for cost estimate (default: 5)",
    )
    parser.add_argument(
        "--skip-erc",
        action="store_true",
        help="Skip ERC check (for PCB-only audits)",
    )
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with code 2 on warnings",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed information",
    )

    args = parser.parse_args(argv)

    # Validate input file
    path = Path(args.project_or_pcb)
    if not path.exists():
        print(f"Error: File not found: {path}", file=sys.stderr)
        return 1

    if path.suffix not in (".kicad_pro", ".kicad_pcb"):
        print(
            f"Error: Expected .kicad_pro or .kicad_pcb file, got: {path.suffix}",
            file=sys.stderr,
        )
        return 1

    # Run audit
    try:
        audit = ManufacturingAudit(
            path,
            manufacturer=args.mfr,
            layers=args.layers,
            copper_oz=args.copper,
            quantity=args.quantity,
            skip_erc=args.skip_erc,
        )
        result = audit.run()
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error running audit: {e}", file=sys.stderr)
        return 1

    # Output results
    if args.format == "json":
        output_json(result)
    elif args.format == "summary":
        output_summary(result)
    else:
        output_table(result, args.verbose)

    # Determine exit code
    if result.verdict == AuditVerdict.NOT_READY:
        return 1
    elif result.verdict == AuditVerdict.WARNING and args.strict:
        return 2
    return 0


def output_table(result: AuditResult, verbose: bool = False) -> None:
    """Output audit results as formatted table."""
    # Header
    print(f"\n{'=' * 70}")
    print("MANUFACTURING READINESS AUDIT")
    print(f"{'=' * 70}")
    print(f"Project: {result.project_name}")
    print(f"Timestamp: {result.timestamp.strftime('%Y-%m-%d %H:%M:%S')}")

    # Verdict banner
    verdict = result.verdict
    if verdict == AuditVerdict.READY:
        verdict_text = "READY FOR MANUFACTURING"
        verdict_symbol = "[OK]"
    elif verdict == AuditVerdict.WARNING:
        verdict_text = "REVIEW WARNINGS"
        verdict_symbol = "[!!]"
    else:
        verdict_text = "NOT READY - FIX ISSUES"
        verdict_symbol = "[XX]"

    print(f"\n{verdict_symbol} {verdict_text}")

    # Check results
    print(f"\n{'-' * 70}")
    print("CHECK RESULTS")
    print(f"{'-' * 70}")

    # ERC
    erc = result.erc
    erc_status = "PASS" if erc.passed else "FAIL"
    erc_icon = "OK" if erc.passed else "X"
    print(f"\n[{erc_icon}] ERC (Electrical Rules Check): {erc_status}")
    if erc.error_count > 0 or erc.warning_count > 0:
        print(f"    Errors: {erc.error_count}, Warnings: {erc.warning_count}")
        if erc.details:
            print(f"    Details: {erc.details}")
    elif erc.details:
        print(f"    Note: {erc.details}")

    # DRC
    drc = result.drc
    drc_status = "PASS" if drc.passed else "FAIL"
    drc_icon = "OK" if drc.passed else "X"
    print(f"\n[{drc_icon}] DRC (Design Rules Check): {drc_status}")
    if drc.error_count > 0 or drc.warning_count > 0:
        print(f"    Errors: {drc.error_count}, Warnings: {drc.warning_count}")
        if drc.blocking_count > 0:
            print(f"    Blocking: {drc.blocking_count} (must fix)")
        if drc.details:
            print(f"    Details: {drc.details}")

    # Connectivity
    conn = result.connectivity
    conn_status = "PASS" if conn.passed else "FAIL"
    conn_icon = "OK" if conn.passed else "X"
    print(f"\n[{conn_icon}] Net Connectivity: {conn_status}")
    print(
        f"    {conn.connected_nets}/{conn.total_nets} nets fully routed ({conn.completion_percent:.0f}%)"
    )
    if conn.unconnected_pads > 0:
        print(f"    Unconnected pads: {conn.unconnected_pads}")
    if conn.details:
        print(f"    Details: {conn.details}")

    # Manufacturer compatibility
    compat = result.compatibility
    compat_status = "PASS" if compat.passed else "FAIL"
    compat_icon = "OK" if compat.passed else "X"
    print(f"\n[{compat_icon}] Manufacturer Compatibility ({compat.manufacturer}): {compat_status}")

    if verbose or not compat.passed:
        # Show details
        trace = compat.min_trace_width
        trace_icon = "OK" if trace[2] else "X"
        print(f"    [{trace_icon}] Min trace: {trace[0]:.3f}mm (limit: {trace[1]:.3f}mm)")

        clearance = compat.min_clearance
        clearance_icon = "OK" if clearance[2] else "X"
        print(
            f"    [{clearance_icon}] Min clearance: {clearance[0]:.3f}mm (limit: {clearance[1]:.3f}mm)"
        )

        drill = compat.min_via_drill
        drill_icon = "OK" if drill[2] else "X"
        print(f"    [{drill_icon}] Min via drill: {drill[0]:.3f}mm (limit: {drill[1]:.3f}mm)")

        annular = compat.min_annular_ring
        annular_icon = "OK" if annular[2] else "X"
        print(
            f"    [{annular_icon}] Min annular ring: {annular[0]:.3f}mm (limit: {annular[1]:.3f}mm)"
        )

        size = compat.board_size
        size_icon = "OK" if size[2] else "X"
        print(
            f"    [{size_icon}] Board size: {size[0][0]:.1f}x{size[0][1]:.1f}mm (max: {size[1][0]:.0f}x{size[1][1]:.0f}mm)"
        )

        layers = compat.layer_count
        layers_icon = "OK" if layers[2] else "X"
        print(f"    [{layers_icon}] Layers: {layers[0]} (supported: {layers[1]})")

    # Layer utilization
    if verbose and result.layers.utilization:
        print(f"\n{'-' * 70}")
        print("LAYER UTILIZATION")
        print(f"{'-' * 70}")
        for layer, pct in result.layers.utilization.items():
            bar_len = int(pct / 5)  # Scale to 20 chars max
            bar = "#" * bar_len + "-" * (20 - bar_len)
            print(f"    {layer:12} [{bar}] {pct:.1f}%")

    # Cost estimate
    if result.cost.total_cost > 0:
        print(f"\n{'-' * 70}")
        print("COST ESTIMATE")
        print(f"{'-' * 70}")
        print(f"    Quantity: {result.cost.quantity} boards")
        print(f"    PCB cost: ${result.cost.pcb_cost:.2f}")
        if result.cost.component_cost:
            print(f"    Components: ${result.cost.component_cost:.2f}")
        if result.cost.assembly_cost:
            print(f"    Assembly: ${result.cost.assembly_cost:.2f}")
        print(f"    Total: ${result.cost.total_cost:.2f} {result.cost.currency}")

    # Action items
    if result.action_items:
        print(f"\n{'-' * 70}")
        print("ACTION ITEMS")
        print(f"{'-' * 70}")
        priority_labels = {1: "CRITICAL", 2: "IMPORTANT", 3: "OPTIONAL"}
        for item in result.action_items:
            label = priority_labels.get(item.priority, "")
            print(f"\n    [{label}] {item.description}")
            if item.command and verbose:
                print(f"        Command: {item.command}")

    print(f"\n{'=' * 70}")


def output_json(result: AuditResult) -> None:
    """Output audit results as JSON."""
    print(json.dumps(result.to_dict(), indent=2, default=str))


def output_summary(result: AuditResult) -> None:
    """Output concise audit summary."""
    summary = result.summary()

    # One-line verdict
    if summary["is_ready"]:
        print(f"READY: {result.project_name}")
    else:
        print(f"NOT READY: {result.project_name}")

    # Key metrics
    print(f"  Verdict: {summary['verdict'].upper()}")
    print(f"  ERC errors: {summary['erc_errors']}")
    print(f"  DRC violations: {summary['drc_violations']} ({summary['drc_blocking']} blocking)")
    print(f"  Net completion: {summary['net_completion']:.0f}%")
    print(f"  Manufacturer compatible: {'Yes' if summary['manufacturer_compatible'] else 'No'}")
    if summary["estimated_cost"] > 0:
        print(f"  Estimated cost: ${summary['estimated_cost']:.2f}")
    if summary["action_items"] > 0:
        print(f"  Action items: {summary['action_items']}")


if __name__ == "__main__":
    sys.exit(main())
