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
    )
