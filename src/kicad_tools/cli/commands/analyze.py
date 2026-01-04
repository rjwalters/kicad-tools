"""Analyze command handlers (congestion analysis, trace-lengths, etc.)."""

__all__ = ["run_analyze_command"]


def run_analyze_command(args) -> int:
    """Handle analyze command and its subcommands."""
    if not args.analyze_command:
        print("Usage: kicad-tools analyze <command> [options] <file>")
        print("Commands: congestion, trace-lengths")
        return 1

    if args.analyze_command == "congestion":
        return _run_congestion_command(args)

    if args.analyze_command == "trace-lengths":
        return _run_trace_lengths_command(args)

    return 1


def _run_congestion_command(args) -> int:
    """Handle analyze congestion command."""
    from ..analyze_cmd import main as analyze_main

    sub_argv = ["congestion", args.pcb]

    if getattr(args, "analyze_format", "text") != "text":
        sub_argv.extend(["--format", args.analyze_format])
    if getattr(args, "analyze_grid_size", 2.0) != 2.0:
        sub_argv.extend(["--grid-size", str(args.analyze_grid_size)])
    if getattr(args, "analyze_min_severity", "low") != "low":
        sub_argv.extend(["--min-severity", args.analyze_min_severity])
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    return analyze_main(sub_argv)


def _run_trace_lengths_command(args) -> int:
    """Handle analyze trace-lengths command."""
    from ..analyze_cmd import main as analyze_main

    sub_argv = ["trace-lengths", args.pcb]

    if getattr(args, "analyze_format", "text") != "text":
        sub_argv.extend(["--format", args.analyze_format])
    if getattr(args, "analyze_nets", None):
        for net in args.analyze_nets:
            sub_argv.extend(["--net", net])
    if getattr(args, "analyze_all", False):
        sub_argv.append("--all")
    if not getattr(args, "analyze_diff_pairs", True):
        sub_argv.append("--no-diff-pairs")
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    return analyze_main(sub_argv)
