"""optimize-placement command handler."""

__all__ = ["run_optimize_placement_command"]


def run_optimize_placement_command(args) -> int:
    """Handle optimize-placement command."""
    from ..optimize_placement_cmd import run_optimize_placement

    return run_optimize_placement(
        pcb_path=args.pcb,
        strategy_name=args.strategy,
        max_iterations=args.max_iterations,
        output_path=args.output,
        seed_method=args.seed_method,
        weights_json=args.weights,
        dry_run=args.dry_run,
        progress_interval=args.progress,
        checkpoint_dir=args.checkpoint,
        verbose=args.verbose,
        quiet=getattr(args, "quiet", False) or getattr(args, "global_quiet", False),
        no_slide_off=getattr(args, "no_slide_off", False),
        anchor_weight=getattr(args, "anchor_weight", 0.0),
        time_budget=getattr(args, "time_budget", None),
        allow_infeasible=getattr(args, "allow_infeasible", False),
        voltage_map_path=getattr(args, "voltage_map", None),
        hv_domains_path=getattr(args, "hv_domains", None),
        creepage_standard=getattr(args, "creepage_standard", "iec60664"),
        pollution_degree=getattr(args, "pollution_degree", 2),
        material_group=getattr(args, "material_group", "IIIa"),
        hv_threshold=getattr(args, "hv_threshold", 30.0),
    )
