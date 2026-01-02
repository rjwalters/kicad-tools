"""Routing command handlers (route, zones, optimize-traces)."""

__all__ = ["run_route_command", "run_zones_command", "run_optimize_command"]


def run_zones_command(args) -> int:
    """Handle zones command."""
    if not args.zones_command:
        print("Usage: kicad-tools zones <command> [options] <file>")
        print("Commands: add, list, batch")
        return 1

    from ..zones_cmd import main as zones_main

    if args.zones_command == "add":
        sub_argv = ["add", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        sub_argv.extend(["--net", args.net])
        sub_argv.extend(["--layer", args.layer])
        if args.priority != 0:
            sub_argv.extend(["--priority", str(args.priority)])
        if args.clearance != 0.3:
            sub_argv.extend(["--clearance", str(args.clearance)])
        if getattr(args, "thermal_gap", 0.3) != 0.3:
            sub_argv.extend(["--thermal-gap", str(args.thermal_gap)])
        if getattr(args, "thermal_bridge", 0.4) != 0.4:
            sub_argv.extend(["--thermal-bridge", str(args.thermal_bridge)])
        if getattr(args, "min_thickness", 0.25) != 0.25:
            sub_argv.extend(["--min-thickness", str(args.min_thickness)])
        if args.verbose:
            sub_argv.append("--verbose")
        if args.dry_run:
            sub_argv.append("--dry-run")
        # Use global quiet flag
        if getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return zones_main(sub_argv) or 0

    elif args.zones_command == "list":
        sub_argv = ["list", args.pcb]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return zones_main(sub_argv) or 0

    elif args.zones_command == "batch":
        sub_argv = ["batch", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        sub_argv.extend(["--power-nets", args.power_nets])
        if args.clearance != 0.3:
            sub_argv.extend(["--clearance", str(args.clearance)])
        if args.verbose:
            sub_argv.append("--verbose")
        if args.dry_run:
            sub_argv.append("--dry-run")
        # Use global quiet flag
        if getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return zones_main(sub_argv) or 0

    return 1


def run_route_command(args) -> int:
    """Handle route command."""
    from ..route_cmd import main as route_main

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.strategy != "negotiated":
        sub_argv.extend(["--strategy", args.strategy])
    if args.skip_nets:
        sub_argv.extend(["--skip-nets", args.skip_nets])
    if args.grid != 0.25:
        sub_argv.extend(["--grid", str(args.grid)])
    if args.trace_width != 0.2:
        sub_argv.extend(["--trace-width", str(args.trace_width)])
    if args.clearance != 0.15:
        sub_argv.extend(["--clearance", str(args.clearance)])
    if args.via_drill != 0.3:
        sub_argv.extend(["--via-drill", str(args.via_drill)])
    if args.via_diameter != 0.6:
        sub_argv.extend(["--via-diameter", str(args.via_diameter)])
    if args.mc_trials != 10:
        sub_argv.extend(["--mc-trials", str(args.mc_trials)])
    if args.iterations != 15:
        sub_argv.extend(["--iterations", str(args.iterations)])
    if args.verbose:
        sub_argv.append("--verbose")
    if args.dry_run:
        sub_argv.append("--dry-run")
    # Use command-level quiet or global quiet
    if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    if getattr(args, "power_nets", None):
        sub_argv.extend(["--power-nets", args.power_nets])
    return route_main(sub_argv)


def run_optimize_command(args) -> int:
    """Handle optimize-traces command."""
    from ..optimize_cmd import main as optimize_main

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.net:
        sub_argv.extend(["--net", args.net])
    if args.no_merge:
        sub_argv.append("--no-merge")
    if args.no_zigzag:
        sub_argv.append("--no-zigzag")
    if args.no_45:
        sub_argv.append("--no-45")
    if args.chamfer_size != 0.5:
        sub_argv.extend(["--chamfer-size", str(args.chamfer_size)])
    if args.verbose:
        sub_argv.append("--verbose")
    if args.dry_run:
        sub_argv.append("--dry-run")
    # Use command-level quiet or global quiet
    if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    return optimize_main(sub_argv)
