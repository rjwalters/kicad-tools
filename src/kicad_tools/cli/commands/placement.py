"""Placement command handlers."""

__all__ = ["run_placement_command"]


def run_placement_command(args) -> int:
    """Handle placement command."""
    if not args.placement_command:
        print("Usage: kicad-tools placement <command> [options] <file>")
        print("Commands: check, fix, optimize")
        return 1

    from ..placement_cmd import main as placement_main

    if args.placement_command == "check":
        sub_argv = ["check", args.pcb]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.pad_clearance != 0.1:
            sub_argv.extend(["--pad-clearance", str(args.pad_clearance)])
        if args.hole_clearance != 0.5:
            sub_argv.extend(["--hole-clearance", str(args.hole_clearance)])
        if args.edge_clearance != 0.3:
            sub_argv.extend(["--edge-clearance", str(args.edge_clearance)])
        if args.verbose:
            sub_argv.append("--verbose")
        # Use command-level quiet or global quiet
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "fix":
        sub_argv = ["fix", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.strategy != "spread":
            sub_argv.extend(["--strategy", args.strategy])
        if args.anchor:
            sub_argv.extend(["--anchor", args.anchor])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.verbose:
            sub_argv.append("--verbose")
        # Use command-level quiet or global quiet
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "optimize":
        sub_argv = ["optimize", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.strategy != "force-directed":
            sub_argv.extend(["--strategy", args.strategy])
        if args.iterations != 1000:
            sub_argv.extend(["--iterations", str(args.iterations)])
        if args.generations != 100:
            sub_argv.extend(["--generations", str(args.generations)])
        if args.population != 50:
            sub_argv.extend(["--population", str(args.population)])
        if args.grid != 0.0:
            sub_argv.extend(["--grid", str(args.grid)])
        if args.fixed:
            sub_argv.extend(["--fixed", args.fixed])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.verbose:
            sub_argv.append("--verbose")
        # Use command-level quiet or global quiet
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    return 1
