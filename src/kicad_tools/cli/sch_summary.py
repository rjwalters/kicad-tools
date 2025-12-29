#!/usr/bin/env python3
"""
Quick overview of a KiCad schematic.

Provides a single-command summary of schematic contents for rapid assessment.

Usage:
    python3 sch-summary.py <schematic.kicad_sch> [options]

Options:
    --format {text,json}   Output format (default: text)
    --verbose              Show more details

Examples:
    python3 sch-summary.py project.kicad_sch
    python3 sch-summary.py project.kicad_sch --verbose
    python3 sch-summary.py project.kicad_sch --format json
"""

import argparse
import json
import sys
from collections import Counter
from pathlib import Path

from kicad_tools.schema import Schematic
from kicad_tools.schema.bom import extract_bom
from kicad_tools.schema.hierarchy import build_hierarchy


def main():
    parser = argparse.ArgumentParser(
        description="Quick schematic overview",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument("--verbose", "-v", action="store_true", help="Show more details")

    args = parser.parse_args()

    try:
        summary = gather_summary(args.schematic, args.verbose)
    except FileNotFoundError:
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        print(json.dumps(summary, indent=2))
    else:
        print_summary(summary, args.verbose)


def gather_summary(schematic_path: str, verbose: bool = False) -> dict:
    """Gather all summary information."""
    path = Path(schematic_path)

    # Basic info
    summary = {
        "file": path.name,
        "path": str(path),
    }

    # Load hierarchy
    try:
        hierarchy = build_hierarchy(schematic_path)
        all_nodes = hierarchy.all_nodes()

        summary["hierarchy"] = {
            "total_sheets": len(all_nodes),
            "max_depth": max(n.depth for n in all_nodes),
            "sheets": [
                {"name": n.name, "path": n.get_path_string(), "labels": len(n.hierarchical_labels)}
                for n in all_nodes
            ]
            if verbose
            else [n.name for n in all_nodes if n.name != "Root"],
        }
    except Exception:
        summary["hierarchy"] = {"total_sheets": 1, "sheets": []}

    # Load BOM
    try:
        bom = extract_bom(schematic_path, hierarchical=True)
        bom_filtered = bom.filter(include_dnp=False)

        # Count by reference prefix
        ref_counts = Counter()
        for item in bom_filtered.items:
            if item.reference:
                prefix = "".join(c for c in item.reference if c.isalpha())
                ref_counts[prefix] += 1

        summary["components"] = {
            "total": bom.total_components,
            "unique_parts": bom.unique_parts,
            "dnp": bom.dnp_count,
            "by_type": dict(ref_counts.most_common()),
        }

        if verbose:
            groups = bom_filtered.grouped()
            summary["components"]["top_parts"] = [
                {"value": g.value, "qty": g.quantity, "refs": g.references}
                for g in sorted(groups, key=lambda x: -x.quantity)[:10]
            ]
    except Exception as e:
        summary["components"] = {"error": str(e)}

    # Load root schematic for connectivity info
    try:
        sch = Schematic.load(schematic_path)

        summary["connectivity"] = {
            "wires": len(list(sch.wires)),
            "junctions": len(list(sch.junctions)),
            "labels": len(list(sch.labels)),
            "global_labels": len(list(sch.global_labels)),
            "hierarchical_labels": len(list(sch.hierarchical_labels)),
        }

        if verbose:
            # Get unique label names
            label_names = set()
            for lbl in sch.labels:
                label_names.add(lbl.text)
            for lbl in sch.global_labels:
                label_names.add(lbl.text)

            summary["connectivity"]["unique_signals"] = sorted(label_names)[:20]

    except Exception:
        summary["connectivity"] = {}

    # Key signals (hierarchical labels across sheets)
    try:
        all_h_labels = set()
        for node in all_nodes:
            all_h_labels.update(node.hierarchical_labels)

        summary["key_signals"] = sorted(all_h_labels)[:15]
        summary["key_signals_count"] = len(all_h_labels)
    except Exception:
        summary["key_signals"] = []

    return summary


def print_summary(summary: dict, verbose: bool = False):
    """Print human-readable summary."""
    print(f"Schematic: {summary['file']}")
    print("=" * 60)

    # Hierarchy
    h = summary.get("hierarchy", {})
    sheets = h.get("total_sheets", 1)
    if sheets > 1:
        print(f"\n📁 Hierarchy: {sheets} sheets (depth {h.get('max_depth', 0)})")
        sheet_list = h.get("sheets", [])
        if isinstance(sheet_list, list) and sheet_list:
            if isinstance(sheet_list[0], dict):
                for s in sheet_list[:8]:
                    print(
                        f"   {'/' if s['name'] == 'Root' else '├─'} {s['name']} ({s['labels']} labels)"
                    )
            else:
                for name in sheet_list[:8]:
                    print(f"   ├─ {name}")
            if len(sheet_list) > 8:
                print(f"   └─ ... and {len(sheet_list) - 8} more")
    else:
        print("\n📄 Single-sheet schematic")

    # Components
    c = summary.get("components", {})
    if "error" not in c:
        print(f"\n🔧 Components: {c.get('total', 0)} total, {c.get('unique_parts', 0)} unique")
        if c.get("dnp", 0):
            print(f"   DNP: {c['dnp']}")

        by_type = c.get("by_type", {})
        if by_type:
            type_str = ", ".join(
                f"{k}:{v}" for k, v in sorted(by_type.items(), key=lambda x: -x[1])[:6]
            )
            print(f"   Types: {type_str}")

        if verbose and "top_parts" in c:
            print("\n   Top parts:")
            for p in c["top_parts"][:5]:
                print(f"   • {p['qty']}x {p['value']}")

    # Connectivity
    conn = summary.get("connectivity", {})
    if conn:
        wires = conn.get("wires", 0)
        junctions = conn.get("junctions", 0)
        if wires:
            print(f"\n🔌 Connectivity: {wires} wires, {junctions} junctions")

        label_count = conn.get("labels", 0) + conn.get("global_labels", 0)
        if label_count:
            print(f"   Labels: {label_count} local, {conn.get('global_labels', 0)} global")

    # Key signals
    signals = summary.get("key_signals", [])
    total_signals = summary.get("key_signals_count", len(signals))
    if signals:
        print(f"\n⚡ Key signals ({total_signals} total):")
        signal_str = ", ".join(signals[:10])
        if len(signals) > 10:
            signal_str += f" ... +{len(signals) - 10} more"
        print(f"   {signal_str}")

    print()


def run_summary(schematic_path: Path, format: str = "text", verbose: bool = False) -> int:
    """Run summary command programmatically.

    Args:
        schematic_path: Path to schematic file
        format: Output format ("text" or "json")
        verbose: Show detailed output

    Returns:
        Exit code (0 for success)
    """
    try:
        summary = gather_summary(str(schematic_path), verbose)
    except FileNotFoundError:
        print(f"Error: File not found: {schematic_path}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    if format == "json":
        print(json.dumps(summary, indent=2))
    else:
        print_summary(summary, verbose)

    return 0


if __name__ == "__main__":
    main()
