"""PCB (pcb) subcommand handlers."""

import json
import sys
from pathlib import Path

__all__ = ["run_pcb_command"]


def run_pcb_command(args) -> int:
    """Handle PCB subcommands."""
    if not args.pcb_command:
        print("Usage: kicad-tools pcb <command> [options] <file>")
        print("Commands: summary, footprints, nets, traces, stackup, strip")
        return 1

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Handle strip command separately (doesn't use pcb_query)
    if args.pcb_command == "strip":
        return _run_strip_command(args, pcb_path)

    from ..pcb_query import main as pcb_main

    if args.pcb_command == "summary":
        sub_argv = [str(pcb_path), "summary"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "footprints":
        sub_argv = [str(pcb_path), "footprints"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        if args.sorted:
            sub_argv.append("--sorted")
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "nets":
        sub_argv = [str(pcb_path), "nets"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.pattern:
            sub_argv.extend(["--filter", args.pattern])
        if args.sorted:
            sub_argv.append("--sorted")
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "traces":
        sub_argv = [str(pcb_path), "traces"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.layer:
            sub_argv.extend(["--layer", args.layer])
        return pcb_main(sub_argv) or 0

    elif args.pcb_command == "stackup":
        sub_argv = [str(pcb_path), "stackup"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return pcb_main(sub_argv) or 0

    return 1


def _run_strip_command(args, pcb_path: Path) -> int:
    """Handle the 'pcb strip' command."""
    from kicad_tools.schema.pcb import PCB

    # Parse net names if provided
    nets = None
    if args.nets:
        nets = [n.strip() for n in args.nets.split(",")]

    # Load PCB
    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Get initial counts for reporting
    initial_segments = len(pcb.segments)
    initial_vias = len(pcb.vias)
    initial_zones = len(pcb.zones)

    # Perform strip operation
    keep_zones = getattr(args, "keep_zones", True)
    stats = pcb.strip_traces(nets=nets, keep_zones=keep_zones)

    # Determine output path
    output_path = pcb_path
    if args.output:
        output_path = Path(args.output)
    elif not args.dry_run:
        # If no output specified and not dry-run, add -stripped suffix
        output_path = pcb_path.with_stem(f"{pcb_path.stem}-stripped")

    # Format output
    output_format = getattr(args, "format", "text")
    dry_run = getattr(args, "dry_run", False)

    result = {
        "input": str(pcb_path),
        "output": str(output_path) if not dry_run else None,
        "dry_run": dry_run,
        "nets_filtered": nets,
        "keep_zones": keep_zones,
        "before": {
            "segments": initial_segments,
            "vias": initial_vias,
            "zones": initial_zones,
        },
        "removed": stats,
        "after": {
            "segments": initial_segments - stats["segments"],
            "vias": initial_vias - stats["vias"],
            "zones": initial_zones - stats["zones"],
        },
    }

    if output_format == "json":
        print(json.dumps(result, indent=2))
    else:
        # Text format
        print(f"PCB Strip {'(dry run)' if dry_run else ''}")
        print(f"  Input:  {pcb_path}")
        if not dry_run:
            print(f"  Output: {output_path}")
        print()

        if nets:
            print(f"  Filtering nets: {', '.join(nets)}")
        else:
            print("  Stripping all nets")
        print(f"  Keep zones: {keep_zones}")
        print()

        print("  Removed:")
        print(f"    Segments: {stats['segments']:,}")
        print(f"    Vias:     {stats['vias']:,}")
        if not keep_zones:
            print(f"    Zones:    {stats['zones']:,}")
        print()

        print("  Remaining:")
        print(f"    Segments: {result['after']['segments']:,}")
        print(f"    Vias:     {result['after']['vias']:,}")
        print(f"    Zones:    {result['after']['zones']:,}")

    # Save unless dry-run
    if not dry_run:
        try:
            pcb.save(output_path)
            if output_format == "text":
                print()
                print(f"  Saved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB: {e}", file=sys.stderr)
            return 1

    return 0
