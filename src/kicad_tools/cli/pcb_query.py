#!/usr/bin/env python3
"""
Query KiCad PCB files.

Provides commands to inspect PCB contents: footprints, traces, nets, stackup.

Usage:
    python3 pcb-query.py <pcb.kicad_pcb> <command> [options]

Commands:
    summary         Quick overview of the board
    footprints      List all footprints (components)
    footprint <ref> Show details for a specific footprint
    nets            List all nets
    net <name>      Show details for a specific net
    traces          Show trace statistics
    vias            List vias
    stackup         Show layer stackup

Examples:
    python3 pcb-query.py board.kicad_pcb summary
    python3 pcb-query.py board.kicad_pcb footprints --filter "U*"
    python3 pcb-query.py board.kicad_pcb footprint U1
    python3 pcb-query.py board.kicad_pcb nets --sorted
    python3 pcb-query.py board.kicad_pcb net GND
    python3 pcb-query.py board.kicad_pcb traces --layer F.Cu
    python3 pcb-query.py board.kicad_pcb stackup
"""

import argparse
import fnmatch
import json
import sys
from pathlib import Path

from kicad_tools.schema import PCB


def cmd_summary(pcb: PCB, args):
    """Show board summary."""
    summary = pcb.summary()

    if args.format == "json":
        print(json.dumps(summary, indent=2))
        return

    print(f"PCB: {summary['title'] or Path(args.pcb).stem}")
    print("=" * 60)

    if summary["revision"]:
        print(f"Revision: {summary['revision']}")

    print(f"\nLayers: {summary['copper_layers']} copper")
    print(f"Footprints: {summary['footprints']}")
    print(f"Nets: {summary['nets']}")
    print(f"Traces: {summary['segments']} segments ({summary['trace_length_mm']} mm)")
    print(f"Vias: {summary['vias']}")
    if summary["zones"]:
        print(f"Zones: {summary['zones']}")
    print()


def cmd_footprints(pcb: PCB, args):
    """List footprints."""
    footprints = pcb.footprints

    # Apply filter
    if args.filter:
        footprints = [fp for fp in footprints if fnmatch.fnmatch(fp.reference, args.filter)]

    # Sort
    if args.sorted:
        footprints = sorted(
            footprints,
            key=lambda fp: (
                fp.reference.rstrip("0123456789"),
                int("".join(c for c in fp.reference if c.isdigit()) or "0"),
            ),
        )

    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "reference": fp.reference,
                        "value": fp.value,
                        "footprint": fp.name,
                        "layer": fp.layer,
                        "position": {"x": fp.position[0], "y": fp.position[1]},
                        "rotation": fp.rotation,
                        "pads": len(fp.pads),
                    }
                    for fp in footprints
                ],
                indent=2,
            )
        )
        return

    print(f"{'Ref':<10} {'Value':<20} {'Footprint':<25} {'Layer':<8} {'Pads'}")
    print("-" * 80)

    for fp in footprints:
        print(f"{fp.reference:<10} {fp.value:<20} {fp.name[:25]:<25} {fp.layer:<8} {len(fp.pads)}")

    print(f"\nTotal: {len(footprints)} footprints")


def cmd_footprint(pcb: PCB, args):
    """Show footprint details."""
    fp = pcb.get_footprint(args.reference)

    if not fp:
        print(f"Error: Footprint '{args.reference}' not found", file=sys.stderr)
        available = sorted(set(f.reference for f in pcb.footprints))[:10]
        print(f"Available: {', '.join(available)}...", file=sys.stderr)
        sys.exit(1)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "reference": fp.reference,
                    "value": fp.value,
                    "footprint": fp.name,
                    "layer": fp.layer,
                    "position": {"x": fp.position[0], "y": fp.position[1]},
                    "rotation": fp.rotation,
                    "description": fp.description,
                    "tags": fp.tags,
                    "pads": [
                        {
                            "number": p.number,
                            "type": p.type,
                            "shape": p.shape,
                            "position": {"x": p.position[0], "y": p.position[1]},
                            "size": {"w": p.size[0], "h": p.size[1]},
                            "net": p.net_name,
                            "layers": p.layers,
                        }
                        for p in fp.pads
                    ],
                },
                indent=2,
            )
        )
        return

    print(f"Footprint: {fp.reference}")
    print("=" * 60)
    print(f"Value: {fp.value}")
    print(f"Package: {fp.name}")
    print(f"Layer: {fp.layer}")
    print(f"Position: ({fp.position[0]:.4f}, {fp.position[1]:.4f}) mm")
    print(f"Rotation: {fp.rotation}°")

    if fp.description:
        print(f"Description: {fp.description}")

    print(f"\nPads ({len(fp.pads)}):")
    print(f"  {'#':<6} {'Type':<10} {'Net':<20} {'Layers'}")
    print("  " + "-" * 55)

    for pad in sorted(
        fp.pads, key=lambda p: (int(p.number) if p.number.isdigit() else 999, p.number)
    ):
        layers = ", ".join(pad.layers[:2])
        if len(pad.layers) > 2:
            layers += "..."
        print(f"  {pad.number:<6} {pad.type:<10} {pad.net_name or '(none)':<20} {layers}")


def cmd_nets(pcb: PCB, args):
    """List nets."""
    nets = list(pcb.nets.values())

    # Filter out empty net (net 0)
    nets = [n for n in nets if n.name]

    if args.filter:
        nets = [n for n in nets if fnmatch.fnmatch(n.name, args.filter)]

    if args.sorted:
        nets = sorted(nets, key=lambda n: n.name)

    if args.format == "json":
        net_stats = []
        for net in nets:
            segments = list(pcb.segments_in_net(net.number))
            vias = list(pcb.vias_in_net(net.number))
            net_stats.append(
                {
                    "number": net.number,
                    "name": net.name,
                    "segments": len(segments),
                    "vias": len(vias),
                }
            )
        print(json.dumps(net_stats, indent=2))
        return

    print(f"{'Net':<25} {'#':<6} {'Segs':<8} {'Vias'}")
    print("-" * 50)

    for net in nets:
        segments = list(pcb.segments_in_net(net.number))
        vias = list(pcb.vias_in_net(net.number))
        print(f"{net.name:<25} {net.number:<6} {len(segments):<8} {len(vias)}")

    print(f"\nTotal: {len(nets)} nets")


def cmd_net(pcb: PCB, args):
    """Show net details."""
    net = pcb.get_net_by_name(args.name)

    if not net:
        # Try by number
        try:
            net = pcb.get_net(int(args.name))
        except ValueError:
            pass

    if not net:
        print(f"Error: Net '{args.name}' not found", file=sys.stderr)
        available = sorted(n.name for n in pcb.nets.values() if n.name)[:10]
        print(f"Available: {', '.join(available)}...", file=sys.stderr)
        sys.exit(1)

    segments = list(pcb.segments_in_net(net.number))
    vias = list(pcb.vias_in_net(net.number))

    # Find connected pads
    pads = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            if pad.net_number == net.number:
                pads.append((fp.reference, pad.number))

    # Calculate trace length
    import math

    trace_length = sum(
        math.sqrt((s.end[0] - s.start[0]) ** 2 + (s.end[1] - s.start[1]) ** 2) for s in segments
    )

    # Get layers used
    layers = sorted(set(s.layer for s in segments))

    if args.format == "json":
        print(
            json.dumps(
                {
                    "number": net.number,
                    "name": net.name,
                    "segments": len(segments),
                    "vias": len(vias),
                    "trace_length_mm": round(trace_length, 2),
                    "layers": layers,
                    "pads": [{"ref": ref, "pad": pad} for ref, pad in pads],
                },
                indent=2,
            )
        )
        return

    print(f"Net: {net.name} (#{net.number})")
    print("=" * 60)
    print(f"Trace segments: {len(segments)}")
    print(f"Total length: {trace_length:.2f} mm")
    print(f"Vias: {len(vias)}")
    print(f"Layers: {', '.join(layers)}")

    print(f"\nConnected pads ({len(pads)}):")
    for ref, pad_num in sorted(pads):
        print(f"  {ref}.{pad_num}")


def cmd_traces(pcb: PCB, args):
    """Show trace statistics."""
    segments = pcb.segments

    if args.layer:
        segments = list(pcb.segments_on_layer(args.layer))

    # Group by layer
    by_layer = {}
    for seg in segments:
        if seg.layer not in by_layer:
            by_layer[seg.layer] = []
        by_layer[seg.layer].append(seg)

    # Calculate stats
    import math

    stats = {}
    for layer, segs in by_layer.items():
        lengths = [
            math.sqrt((s.end[0] - s.start[0]) ** 2 + (s.end[1] - s.start[1]) ** 2) for s in segs
        ]
        widths = set(s.width for s in segs)
        stats[layer] = {
            "count": len(segs),
            "total_length": sum(lengths),
            "widths": sorted(widths),
        }

    if args.format == "json":
        print(
            json.dumps(
                {
                    layer: {
                        "segments": s["count"],
                        "total_length_mm": round(s["total_length"], 2),
                        "widths_mm": s["widths"],
                    }
                    for layer, s in stats.items()
                },
                indent=2,
            )
        )
        return

    print("Trace Statistics")
    print("=" * 60)

    for layer in sorted(stats.keys()):
        s = stats[layer]
        widths = ", ".join(f"{w:.3f}" for w in s["widths"][:3])
        if len(s["widths"]) > 3:
            widths += "..."
        print(f"\n{layer}:")
        print(f"  Segments: {s['count']}")
        print(f"  Total length: {s['total_length']:.2f} mm")
        print(f"  Widths: {widths} mm")

    total_length = sum(s["total_length"] for s in stats.values())
    print(f"\nTotal: {len(segments)} segments, {total_length:.2f} mm")


def cmd_vias(pcb: PCB, args):
    """List vias."""
    vias = pcb.vias

    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "position": {"x": v.position[0], "y": v.position[1]},
                        "size": v.size,
                        "drill": v.drill,
                        "layers": v.layers,
                        "net": pcb.get_net(v.net_number).name if pcb.get_net(v.net_number) else "",
                    }
                    for v in vias
                ],
                indent=2,
            )
        )
        return

    # Group by size
    by_size = {}
    for via in vias:
        key = (via.size, via.drill)
        if key not in by_size:
            by_size[key] = []
        by_size[key].append(via)

    print("Via Summary")
    print("=" * 60)

    for (size, drill), group in sorted(by_size.items()):
        print(f"\nSize {size:.2f}mm / Drill {drill:.2f}mm: {len(group)} vias")

        # Count by net
        net_counts = {}
        for v in group:
            net = pcb.get_net(v.net_number)
            name = net.name if net else "(none)"
            net_counts[name] = net_counts.get(name, 0) + 1

        top_nets = sorted(net_counts.items(), key=lambda x: -x[1])[:5]
        for net_name, count in top_nets:
            print(f"  {net_name}: {count}")

    print(f"\nTotal: {len(vias)} vias")


def cmd_stackup(pcb: PCB, args):
    """Show layer stackup."""
    if not pcb.setup or not pcb.setup.stackup:
        print("No stackup information available")
        return

    stackup = pcb.setup.stackup

    if args.format == "json":
        print(
            json.dumps(
                [
                    {
                        "name": layer.name,
                        "type": layer.type,
                        "thickness_mm": layer.thickness,
                        "material": layer.material,
                        "epsilon_r": layer.epsilon_r,
                    }
                    for layer in stackup
                ],
                indent=2,
            )
        )
        return

    print("Layer Stackup")
    print("=" * 60)

    total_thickness = 0.0
    for layer in stackup:
        total_thickness += layer.thickness

        thickness_str = f"{layer.thickness:.3f}mm" if layer.thickness else "-"
        material_str = layer.material if layer.material else ""

        if layer.type == "copper":
            print(f"  {layer.name:<12} {layer.type:<15} {thickness_str:<10} {material_str}")
        elif layer.type in ("prepreg", "core"):
            eps = f"Er={layer.epsilon_r}" if layer.epsilon_r else ""
            print(f"  {layer.name:<12} {layer.type:<15} {thickness_str:<10} {material_str} {eps}")
        else:
            print(f"  {layer.name:<12} {layer.type:<15} {thickness_str:<10}")

    print(f"\nTotal thickness: {total_thickness:.3f} mm")


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Query KiCad PCB files",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "command",
        nargs="?",
        default="summary",
        choices=["summary", "footprints", "footprint", "nets", "net", "traces", "vias", "stackup"],
        help="Command to run",
    )
    parser.add_argument("arg", nargs="?", help="Command argument (reference, net name)")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument("--filter", help="Filter pattern (e.g., 'U*', 'C*')")
    parser.add_argument("--sorted", action="store_true", help="Sort output")
    parser.add_argument("--layer", help="Filter by layer (for traces)")

    args = parser.parse_args(argv)

    if not Path(args.pcb).exists():
        print(f"Error: File not found: {args.pcb}", file=sys.stderr)
        sys.exit(1)

    try:
        pcb = PCB.load(args.pcb)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        sys.exit(1)

    # Route to command
    if args.command == "summary":
        cmd_summary(pcb, args)
    elif args.command == "footprints":
        cmd_footprints(pcb, args)
    elif args.command == "footprint":
        if not args.arg:
            print("Error: footprint command requires reference (e.g., U1)", file=sys.stderr)
            sys.exit(1)
        args.reference = args.arg
        cmd_footprint(pcb, args)
    elif args.command == "nets":
        cmd_nets(pcb, args)
    elif args.command == "net":
        if not args.arg:
            print("Error: net command requires net name", file=sys.stderr)
            sys.exit(1)
        args.name = args.arg
        cmd_net(pcb, args)
    elif args.command == "traces":
        cmd_traces(pcb, args)
    elif args.command == "vias":
        cmd_vias(pcb, args)
    elif args.command == "stackup":
        cmd_stackup(pcb, args)


if __name__ == "__main__":
    main()
