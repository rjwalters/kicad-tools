"""Routing command handlers (route, route-auto, zones, optimize-traces)."""

__all__ = [
    "run_route_command",
    "run_route_auto_command",
    "run_zones_command",
    "run_optimize_command",
]


def run_zones_command(args) -> int:
    """Handle zones command."""
    if not args.zones_command:
        print("Usage: kicad-tools zones <command> [options] <file>")
        print("Commands: add, list, batch, fill")
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

    elif args.zones_command == "fill":
        sub_argv = ["fill", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if getattr(args, "net", None):
            sub_argv.extend(["--net", args.net])
        if args.verbose:
            sub_argv.append("--verbose")
        if args.dry_run:
            sub_argv.append("--dry-run")
        # Use global quiet flag
        if getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return zones_main(sub_argv) or 0

    return 1


def run_route_auto_command(args) -> int:
    """Handle route-auto command using RoutingOrchestrator."""
    import sys

    # Dry-run: preview strategy selection without routing
    if args.dry_run:
        print(f"[dry-run] Would route net '{args.net}' on '{args.pcb}' using RoutingOrchestrator")
        print(f"  Strategy override: {args.strategy}")
        print(f"  Repair enabled: {not args.no_repair}")
        print(f"  Via resolution enabled: {not args.no_via_resolution}")
        if args.output:
            print(f"  Output: {args.output}")
        return 0

    from kicad_tools.mcp.tools.routing import route_net_auto

    try:
        result = route_net_auto(
            pcb_path=args.pcb,
            net_name=args.net,
            output_path=args.output,
            strategy=args.strategy,
            enable_repair=not args.no_repair,
            enable_via_resolution=not args.no_via_resolution,
        )
    except FileNotFoundError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:
        print(f"Error: {e}", file=sys.stderr)
        if getattr(args, "verbose", False):
            import traceback

            traceback.print_exc()
        return 1

    # Print result
    if result["success"]:
        metrics = result.get("metrics", {})
        print(f"Routed net '{result['net_name']}' successfully")
        print(f"  Strategy: {result.get('strategy_used', 'unknown')}")
        if metrics:
            length = metrics.get("total_length_mm", 0.0)
            vias = metrics.get("via_count", 0)
            repairs = metrics.get("repair_actions", 0)
            if length:
                print(f"  Total length: {length:.2f}mm")
            if vias:
                print(f"  Vias: {vias}")
            if repairs:
                print(f"  Repairs applied: {repairs}")
        for warning in result.get("warnings", []):
            print(f"  Warning: {warning}")
        if result.get("output_path"):
            print(f"  Saved to: {result['output_path']}")
        return 0
    else:
        print(f"Routing failed for net '{result['net_name']}'", file=sys.stderr)
        if result.get("error_message"):
            print(f"  Error: {result['error_message']}", file=sys.stderr)
        for alt in result.get("alternative_strategies", []):
            strategy_name = alt.get("strategy", "unknown")
            reason = alt.get("reason", "")
            print(f"  Try: {strategy_name} - {reason}", file=sys.stderr)
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
    grid_val = str(args.grid)
    if grid_val.lower() != "auto":
        sub_argv.extend(["--grid", grid_val])
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
    timeout_val = getattr(args, "timeout", None)
    if timeout_val is not None:
        sub_argv.extend(["--timeout", str(timeout_val)])
    per_net_timeout_val = getattr(args, "per_net_timeout", 30.0)
    if per_net_timeout_val != 30.0:
        sub_argv.extend(["--per-net-timeout", str(per_net_timeout_val)])
    if args.verbose:
        sub_argv.append("--verbose")
    if args.dry_run:
        sub_argv.append("--dry-run")
    # Use command-level quiet or global quiet
    if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    if getattr(args, "power_nets", None):
        sub_argv.extend(["--power-nets", args.power_nets])
    if getattr(args, "layers", "auto") != "auto":
        sub_argv.extend(["--layers", args.layers])
    if getattr(args, "force", False):
        sub_argv.append("--force")
    if getattr(args, "no_optimize", False):
        sub_argv.append("--no-optimize")
    # Issue #2388: --auto-layers is now enabled by default.  Forward only
    # the user's explicit choice (so the default takes effect when neither
    # is passed and --no-auto-layers is honored when disabled).
    auto_layers_attr = getattr(args, "auto_layers", True)
    if auto_layers_attr is False:
        sub_argv.append("--no-auto-layers")
    if getattr(args, "max_layers", 6) != 6:
        sub_argv.extend(["--max-layers", str(args.max_layers)])
    if getattr(args, "min_completion", 0.95) != 0.95:
        sub_argv.extend(["--min-completion", str(args.min_completion)])
    if getattr(args, "adaptive_rules", False):
        sub_argv.append("--adaptive-rules")
    if getattr(args, "min_trace", None) is not None:
        sub_argv.extend(["--min-trace", str(args.min_trace)])
    if getattr(args, "min_clearance_floor", None) is not None:
        sub_argv.extend(["--min-clearance-floor", str(args.min_clearance_floor)])
    if getattr(args, "manufacturer", "jlcpcb") != "jlcpcb":
        sub_argv.extend(["--manufacturer", args.manufacturer])
    if getattr(args, "high_performance", False):
        sub_argv.append("--high-performance")
    if getattr(args, "skip_drc", False):
        sub_argv.append("--skip-drc")
    if getattr(args, "auto_fix", False):
        sub_argv.append("--auto-fix")
    if getattr(args, "auto_fix_passes", None) is not None:
        sub_argv.extend(["--auto-fix-passes", str(args.auto_fix_passes)])
    # Issue #2595: forward placement-feedback flags.
    if getattr(args, "placement_feedback", False):
        sub_argv.append("--placement-feedback")
    if getattr(args, "placement_feedback_budget", 3) != 3:
        sub_argv.extend(
            ["--placement-feedback-budget", str(args.placement_feedback_budget)]
        )
    if getattr(args, "placement_feedback_max_movement", 5.0) != 5.0:
        sub_argv.extend(
            [
                "--placement-feedback-max-movement",
                str(args.placement_feedback_max_movement),
            ]
        )
    if getattr(args, "placement_feedback_anchor", None):
        sub_argv.extend(
            ["--placement-feedback-anchor", args.placement_feedback_anchor]
        )
    if getattr(args, "placement_feedback_no_anchor", None):
        sub_argv.extend(
            [
                "--placement-feedback-no-anchor",
                args.placement_feedback_no_anchor,
            ]
        )
    # Issue #2606: forward stagnation + outer-timeout flags only when
    # set to a non-default value so the "boards 01-05 produce identical
    # routes" invariant holds when --placement-feedback is off.
    if getattr(args, "placement_feedback_stagnation_patience", 3) != 3:
        sub_argv.extend(
            [
                "--placement-feedback-stagnation-patience",
                str(args.placement_feedback_stagnation_patience),
            ]
        )
    if getattr(args, "placement_feedback_outer_timeout", None) is not None:
        sub_argv.extend(
            [
                "--placement-feedback-outer-timeout",
                str(args.placement_feedback_outer_timeout),
            ]
        )
    if getattr(args, "export_failed_nets", None):
        sub_argv.extend(["--export-failed-nets", args.export_failed_nets])
    if getattr(args, "no_cache", False):
        sub_argv.append("--no-cache")
    if getattr(args, "backend", "auto") != "auto":
        sub_argv.extend(["--backend", args.backend])
    # Issue #2589: forward --seed for deterministic runs.  Default is None
    # (router uses os.urandom-derived state, existing behaviour).
    if getattr(args, "seed", None) is not None:
        sub_argv.extend(["--seed", str(args.seed)])
    if getattr(args, "strict", False):
        sub_argv.append("--strict")
    # Issue #2464: Forward differential pair routing flags
    if getattr(args, "differential_pairs", False):
        sub_argv.append("--differential-pairs")
    if getattr(args, "diffpair_spacing", None) is not None:
        sub_argv.extend(["--diffpair-spacing", str(args.diffpair_spacing)])
    if getattr(args, "diffpair_max_delta", None) is not None:
        sub_argv.extend(["--diffpair-max-delta", str(args.diffpair_max_delta)])
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
    # DRC-aware mode arguments
    if getattr(args, "drc_aware", False):
        sub_argv.append("--drc-aware")
    if getattr(args, "mfr", None):
        sub_argv.extend(["--mfr", args.mfr])
    if getattr(args, "layers", 2) != 2:
        sub_argv.extend(["--layers", str(args.layers)])
    if getattr(args, "copper", 1.0) != 1.0:
        sub_argv.extend(["--copper", str(args.copper)])
    return optimize_main(sub_argv)
