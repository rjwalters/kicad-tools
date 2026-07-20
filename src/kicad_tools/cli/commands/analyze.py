"""Analyze command handlers (congestion analysis, trace-lengths, signal-integrity, thermal, etc.)."""

__all__ = ["run_analyze_command"]


def run_analyze_command(args) -> int:
    """Handle analyze command and its subcommands."""
    if not args.analyze_command:
        print("Usage: kicad-tools analyze <command> [options] <file>")
        print(
            "Commands: complexity, congestion, trace-lengths, signal-integrity, "
            "thermal, current-sense"
        )
        return 1

    if args.analyze_command == "complexity":
        return _run_complexity_command(args)

    if args.analyze_command == "congestion":
        return _run_congestion_command(args)

    if args.analyze_command == "trace-lengths":
        return _run_trace_lengths_command(args)

    if args.analyze_command == "signal-integrity":
        return _run_signal_integrity_command(args)

    if args.analyze_command == "thermal":
        return _run_thermal_command(args)

    if args.analyze_command == "current-sense":
        return _run_current_sense_command(args)

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


def _run_signal_integrity_command(args) -> int:
    """Handle analyze signal-integrity command."""
    from ..analyze_cmd import main as analyze_main

    sub_argv = ["signal-integrity", args.pcb]

    if getattr(args, "analyze_format", "text") != "text":
        sub_argv.extend(["--format", args.analyze_format])
    if getattr(args, "analyze_min_risk", "medium") != "medium":
        sub_argv.extend(["--min-risk", args.analyze_min_risk])
    if getattr(args, "analyze_crosstalk_only", False):
        sub_argv.append("--crosstalk-only")
    if getattr(args, "analyze_impedance_only", False):
        sub_argv.append("--impedance-only")
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    return analyze_main(sub_argv)


def _run_thermal_command(args) -> int:
    """Handle analyze thermal command."""
    from ..analyze_cmd import main as analyze_main

    sub_argv = ["thermal", args.pcb]

    if getattr(args, "analyze_format", "text") != "text":
        sub_argv.extend(["--format", args.analyze_format])
    if getattr(args, "analyze_cluster_radius", 10.0) != 10.0:
        sub_argv.extend(["--cluster-radius", str(args.analyze_cluster_radius)])
    if getattr(args, "analyze_min_power", 0.05) != 0.05:
        sub_argv.extend(["--min-power", str(args.analyze_min_power)])
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    return analyze_main(sub_argv)


def _run_current_sense_command(args) -> int:
    """Handle analyze current-sense command."""
    from ..analyze_cmd import main as analyze_main

    sub_argv = ["current-sense", args.pcb]

    if getattr(args, "analyze_format", "text") != "text":
        sub_argv.extend(["--format", args.analyze_format])
    for name in getattr(args, "analyze_sense_nets", None) or []:
        sub_argv.extend(["--sense-net", name])
    for name in getattr(args, "analyze_hicur_nets", None) or []:
        sub_argv.extend(["--hicur-net", name])
    if getattr(args, "analyze_max_parallel", 10.0) != 10.0:
        sub_argv.extend(["--max-parallel", str(args.analyze_max_parallel)])
    if getattr(args, "analyze_min_gap", 0.5) != 0.5:
        sub_argv.extend(["--min-gap", str(args.analyze_min_gap)])
    if getattr(args, "analyze_max_loop_area", 10.0) != 10.0:
        sub_argv.extend(["--max-loop-area", str(args.analyze_max_loop_area)])
    for pair in getattr(args, "analyze_sense_pairs", None) or []:
        sub_argv.extend(["--sense-pair", pair[0], pair[1]])
    if getattr(args, "analyze_sense_return", None):
        sub_argv.extend(["--sense-return", args.analyze_sense_return])
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    return analyze_main(sub_argv)


def _run_complexity_command(args) -> int:
    """Handle analyze complexity command."""
    from ..analyze_cmd import main as analyze_main

    sub_argv = ["complexity", args.pcb]

    if getattr(args, "analyze_format", "text") != "text":
        sub_argv.extend(["--format", args.analyze_format])
    if getattr(args, "analyze_grid_size", 5.0) != 5.0:
        sub_argv.extend(["--grid-size", str(args.analyze_grid_size)])
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    return analyze_main(sub_argv)
