"""PCB (pcb) subcommand handlers."""

import sys
from pathlib import Path

__all__ = ["run_pcb_command"]


def run_pcb_command(args) -> int:
    """Handle PCB subcommands."""
    if not args.pcb_command:
        print("Usage: kicad-tools pcb <command> [options] <file>")
        print("Commands: summary, footprints, nets, traces, stackup")
        return 1

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

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
