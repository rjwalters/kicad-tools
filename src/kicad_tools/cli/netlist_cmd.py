"""
Netlist analysis and comparison CLI commands.

Provides CLI commands for analyzing netlist connectivity, finding issues,
and comparing netlists between schematic versions.

Usage:
    kct netlist analyze design.kicad_sch
    kct netlist list design.kicad_sch --format table
    kct netlist show design.kicad_sch --net VCC
    kct netlist check design.kicad_sch
    kct netlist compare old.kicad_sch new.kicad_sch
    kct netlist export design.kicad_sch -o output.net
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.operations.netlist import (
    Netlist,
    NetlistNet,
    export_netlist,
)


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct netlist command."""
    parser = argparse.ArgumentParser(
        prog="kct netlist",
        description="Netlist analysis and comparison tools",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    subparsers = parser.add_subparsers(dest="command", help="Netlist commands")

    # analyze subcommand
    analyze_parser = subparsers.add_parser("analyze", help="Show connectivity statistics")
    analyze_parser.add_argument("schematic", help="Path to .kicad_sch file")
    analyze_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )

    # list subcommand
    list_parser = subparsers.add_parser("list", help="List all nets with connection counts")
    list_parser.add_argument("schematic", help="Path to .kicad_sch file")
    list_parser.add_argument(
        "--format", choices=["table", "json"], default="table", help="Output format"
    )
    list_parser.add_argument(
        "--sort",
        choices=["name", "connections"],
        default="connections",
        help="Sort order (default: connections)",
    )

    # show subcommand
    show_parser = subparsers.add_parser("show", help="Show specific net details")
    show_parser.add_argument("schematic", help="Path to .kicad_sch file")
    show_parser.add_argument("--net", required=True, help="Net name to show")
    show_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )

    # check subcommand
    check_parser = subparsers.add_parser("check", help="Find connectivity issues")
    check_parser.add_argument("schematic", help="Path to .kicad_sch file")
    check_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )

    # compare subcommand
    compare_parser = subparsers.add_parser("compare", help="Compare two netlists")
    compare_parser.add_argument("old", help="Path to old .kicad_sch file")
    compare_parser.add_argument("new", help="Path to new .kicad_sch file")
    compare_parser.add_argument(
        "--format", choices=["text", "json"], default="text", help="Output format"
    )

    # export subcommand
    export_parser = subparsers.add_parser("export", help="Export netlist file")
    export_parser.add_argument("schematic", help="Path to .kicad_sch file")
    export_parser.add_argument("-o", "--output", help="Output file path")
    export_parser.add_argument(
        "--format",
        choices=["kicad", "json"],
        default="kicad",
        help="Output format (default: kicad)",
    )

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    try:
        if args.command == "analyze":
            return cmd_analyze(Path(args.schematic), args.format)
        elif args.command == "list":
            return cmd_list(Path(args.schematic), args.format, args.sort)
        elif args.command == "show":
            return cmd_show(Path(args.schematic), args.net, args.format)
        elif args.command == "check":
            return cmd_check(Path(args.schematic), args.format)
        elif args.command == "compare":
            return cmd_compare(Path(args.old), Path(args.new), args.format)
        elif args.command == "export":
            return cmd_export(
                Path(args.schematic),
                Path(args.output) if args.output else None,
                args.format,
            )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except RuntimeError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    return 0


def cmd_analyze(schematic_path: Path, format: str) -> int:
    """Show connectivity statistics."""
    netlist = export_netlist(schematic_path)
    stats = netlist.summary()

    # Additional analysis
    single_pin_nets = find_single_pin_nets(netlist)
    stats["single_pin_net_count"] = len(single_pin_nets)

    if format == "json":
        print(json.dumps(stats, indent=2))
    else:
        print_analyze_text(stats, netlist)

    return 0


def print_analyze_text(stats: dict, netlist: Netlist) -> None:
    """Print analyze output in text format."""
    print("=" * 60)
    print("NETLIST ANALYSIS")
    print("=" * 60)

    if stats.get("source_file"):
        print(f"Source: {Path(stats['source_file']).name}")
    if stats.get("tool"):
        print(f"Tool: {stats['tool']}")
    if stats.get("date"):
        print(f"Date: {stats['date']}")

    print(f"\nSheets: {stats['sheet_count']}")
    print(f"Components: {stats['component_count']}")

    if stats.get("components_by_type"):
        print("  By type:")
        for prefix, count in sorted(stats["components_by_type"].items()):
            print(f"    {prefix}: {count}")

    print(f"\nNets: {stats['net_count']}")
    print(f"  Power: {stats['power_net_count']}")
    print(f"  Signal: {stats['signal_net_count']}")

    single_pin_count = stats.get("single_pin_net_count", 0)
    if single_pin_count > 0:
        print(f"\n⚠ Single-pin nets: {single_pin_count} (potential issues)")
    else:
        print("\n✓ No single-pin nets (connectivity looks good)")

    print("=" * 60)


def cmd_list(schematic_path: Path, format: str, sort: str) -> int:
    """List all nets with connection counts."""
    netlist = export_netlist(schematic_path)

    if sort == "connections":
        sorted_nets = sorted(netlist.nets, key=lambda n: -n.connection_count)
    else:
        sorted_nets = sorted(netlist.nets, key=lambda n: n.name)

    if format == "json":
        print_list_json(sorted_nets, netlist)
    else:
        print_list_table(sorted_nets, netlist)

    return 0


def print_list_json(nets: list[NetlistNet], netlist: Netlist) -> None:
    """Print net list as JSON."""
    power_nets = {n.name for n in netlist.power_nets}
    data = []
    for net in nets:
        nodes_summary = [f"{node.reference}.{node.pin}" for node in net.nodes[:5]]
        if len(net.nodes) > 5:
            nodes_summary.append(f"... +{len(net.nodes) - 5} more")
        data.append(
            {
                "name": net.name,
                "connections": net.connection_count,
                "type": "power" if net.name in power_nets else "signal",
                "pins": nodes_summary,
            }
        )
    print(json.dumps(data, indent=2))


def print_list_table(nets: list[NetlistNet], netlist: Netlist) -> None:
    """Print net list as table."""
    if not nets:
        print("No nets found.")
        return

    power_nets = {n.name for n in netlist.power_nets}

    print(f"{'Name':<30} {'Conn':<6} {'Type':<8} Connected Pins")
    print("-" * 80)

    for net in nets:
        net_type = "power" if net.name in power_nets else "signal"
        pins = [f"{node.reference}.{node.pin}" for node in net.nodes[:4]]
        pins_str = ", ".join(pins)
        if len(net.nodes) > 4:
            pins_str += f" +{len(net.nodes) - 4} more"

        # Truncate name if too long
        name = net.name[:29] if len(net.name) > 29 else net.name
        print(f"{name:<30} {net.connection_count:<6} {net_type:<8} {pins_str}")

    print(f"\nTotal: {len(nets)} nets")


def cmd_show(schematic_path: Path, net_name: str, format: str) -> int:
    """Show specific net details."""
    netlist = export_netlist(schematic_path)
    net = netlist.get_net(net_name)

    if not net:
        # Try fuzzy match
        similar = find_similar_nets(netlist, net_name)
        print(f"Error: Net '{net_name}' not found", file=sys.stderr)
        if similar:
            print(f"Did you mean: {', '.join(similar[:5])}", file=sys.stderr)
        return 1

    if format == "json":
        print_show_json(net, netlist)
    else:
        print_show_text(net, netlist)

    return 0


def find_similar_nets(netlist: Netlist, name: str) -> list[str]:
    """Find net names similar to the given name."""
    name_lower = name.lower()
    matches = []
    for net in netlist.nets:
        if name_lower in net.name.lower():
            matches.append(net.name)
    return sorted(matches)[:10]


def print_show_json(net: NetlistNet, netlist: Netlist) -> None:
    """Print net details as JSON."""
    power_nets = {n.name for n in netlist.power_nets}
    data = {
        "name": net.name,
        "code": net.code,
        "connections": net.connection_count,
        "type": "power" if net.name in power_nets else "signal",
        "nodes": [
            {
                "reference": node.reference,
                "pin": node.pin,
                "pin_function": node.pin_function,
                "pin_type": node.pin_type,
            }
            for node in net.nodes
        ],
    }
    print(json.dumps(data, indent=2))


def print_show_text(net: NetlistNet, netlist: Netlist) -> None:
    """Print net details as text."""
    power_nets = {n.name for n in netlist.power_nets}
    net_type = "power" if net.name in power_nets else "signal"

    print(f"Net: {net.name}")
    print("=" * 50)
    print(f"Code: {net.code}")
    print(f"Type: {net_type}")
    print(f"Connections: {net.connection_count}")

    print("\nConnected Pins:")
    print("-" * 50)
    for node in net.nodes:
        func = f" ({node.pin_function})" if node.pin_function else ""
        ptype = f" [{node.pin_type}]" if node.pin_type else ""
        print(f"  {node.reference}.{node.pin}{func}{ptype}")


def cmd_check(schematic_path: Path, format: str) -> int:
    """Find connectivity issues."""
    netlist = export_netlist(schematic_path)

    # Find issues
    single_pin_nets = find_single_pin_nets(netlist)
    power_nets = netlist.power_nets

    issues: list[dict] = []
    for net in single_pin_nets:
        node = net.nodes[0]
        issues.append(
            {
                "type": "single_pin_net",
                "severity": "warning",
                "net": net.name,
                "reference": node.reference,
                "pin": node.pin,
                "message": f"Net '{net.name}' has only 1 connection ({node.reference}.{node.pin})",
            }
        )

    result = {
        "total_nets": len(netlist.nets),
        "power_nets": len(power_nets),
        "single_pin_nets": len(single_pin_nets),
        "issues": issues,
        "power_net_status": [
            {"name": n.name, "connections": n.connection_count, "status": "ok"} for n in power_nets
        ],
    }

    if format == "json":
        print(json.dumps(result, indent=2))
    else:
        print_check_text(result, power_nets)

    return 0


def print_check_text(result: dict, power_nets: list[NetlistNet]) -> None:
    """Print check results as text."""
    print("=" * 60)
    print("NETLIST CHECK RESULTS")
    print("=" * 60)

    issues = result.get("issues", [])
    if issues:
        print(f"\n⚠ Potential Issues ({len(issues)}):")
        print("-" * 60)
        for issue in issues:
            print(f"  {issue['severity'].upper()}: {issue['message']}")
    else:
        print("\n✓ No connectivity issues found")

    print(f"\nPower Net Status ({len(power_nets)}):")
    print("-" * 60)
    for net in sorted(power_nets, key=lambda n: n.name):
        print(f"  ✓ {net.name} ({net.connection_count} connections)")

    print("\nSummary:")
    print(f"  Total nets: {result['total_nets']}")
    print(f"  Power nets: {result['power_nets']}")
    print(f"  Single-pin nets: {result['single_pin_nets']}")
    print("=" * 60)


def cmd_compare(old_path: Path, new_path: Path, format: str) -> int:
    """Compare two netlists."""
    old_netlist = export_netlist(old_path)
    new_netlist = export_netlist(new_path)

    # Compare components
    old_refs = {c.reference for c in old_netlist.components}
    new_refs = {c.reference for c in new_netlist.components}
    added_components = sorted(new_refs - old_refs)
    removed_components = sorted(old_refs - new_refs)

    # Compare nets
    old_nets = {n.name: n for n in old_netlist.nets}
    new_nets = {n.name: n for n in new_netlist.nets}
    added_nets = sorted(set(new_nets.keys()) - set(old_nets.keys()))
    removed_nets = sorted(set(old_nets.keys()) - set(new_nets.keys()))

    # Find modified nets (same name but different connections)
    modified_nets = []
    for name in set(old_nets.keys()) & set(new_nets.keys()):
        old_count = old_nets[name].connection_count
        new_count = new_nets[name].connection_count
        if old_count != new_count:
            diff = new_count - old_count
            diff_str = f"+{diff}" if diff > 0 else str(diff)
            modified_nets.append(
                {"name": name, "old": old_count, "new": new_count, "diff": diff_str}
            )

    result = {
        "old_file": str(old_path),
        "new_file": str(new_path),
        "components": {
            "added": added_components,
            "removed": removed_components,
            "added_count": len(added_components),
            "removed_count": len(removed_components),
        },
        "nets": {
            "added": added_nets,
            "removed": removed_nets,
            "modified": modified_nets,
            "added_count": len(added_nets),
            "removed_count": len(removed_nets),
            "modified_count": len(modified_nets),
        },
    }

    if format == "json":
        print(json.dumps(result, indent=2))
    else:
        print_compare_text(result)

    return 0


def print_compare_text(result: dict) -> None:
    """Print comparison results as text."""
    print("=" * 60)
    print("NETLIST COMPARISON")
    print("=" * 60)
    print(f"Old: {Path(result['old_file']).name}")
    print(f"New: {Path(result['new_file']).name}")

    comps = result["components"]
    print("\nComponents:")
    print("-" * 40)
    if comps["added"]:
        print(f"  Added ({comps['added_count']}): {', '.join(comps['added'][:10])}")
        if comps["added_count"] > 10:
            print(f"         ... +{comps['added_count'] - 10} more")
    if comps["removed"]:
        print(f"  Removed ({comps['removed_count']}): {', '.join(comps['removed'][:10])}")
        if comps["removed_count"] > 10:
            print(f"           ... +{comps['removed_count'] - 10} more")
    if not comps["added"] and not comps["removed"]:
        print("  No component changes")

    nets = result["nets"]
    print("\nNets:")
    print("-" * 40)
    if nets["added"]:
        print(f"  Added ({nets['added_count']}): {', '.join(nets['added'][:10])}")
        if nets["added_count"] > 10:
            print(f"         ... +{nets['added_count'] - 10} more")
    if nets["removed"]:
        print(f"  Removed ({nets['removed_count']}): {', '.join(nets['removed'][:10])}")
        if nets["removed_count"] > 10:
            print(f"           ... +{nets['removed_count'] - 10} more")
    if nets["modified"]:
        print(f"  Modified ({nets['modified_count']}):")
        for mod in nets["modified"][:10]:
            print(f"    {mod['name']}: {mod['old']} → {mod['new']} ({mod['diff']})")
        if nets["modified_count"] > 10:
            print(f"    ... +{nets['modified_count'] - 10} more")
    if not nets["added"] and not nets["removed"] and not nets["modified"]:
        print("  No net changes")

    print("=" * 60)


def cmd_export(schematic_path: Path, output_path: Path | None, format: str) -> int:
    """Export netlist file."""
    netlist = export_netlist(schematic_path)

    if format == "json":
        output = netlist.to_json()
        if output_path:
            output_path.write_text(output)
            print(f"Exported JSON netlist to: {output_path}")
        else:
            print(output)
    else:
        # KiCad format - the netlist is already exported by export_netlist()
        # We just need to tell the user where it is
        default_output = schematic_path.parent / f"{schematic_path.stem}-netlist.kicad_net"
        if output_path and output_path != default_output:
            # Copy to requested location
            import shutil

            shutil.copy(default_output, output_path)
            print(f"Exported KiCad netlist to: {output_path}")
        else:
            print(f"Exported KiCad netlist to: {default_output}")

    return 0


def find_single_pin_nets(netlist: Netlist) -> list[NetlistNet]:
    """Find nets with only one connection (potential issues)."""
    return [net for net in netlist.nets if net.connection_count == 1]


if __name__ == "__main__":
    sys.exit(main())
