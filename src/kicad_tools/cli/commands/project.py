"""Project-level command handlers."""

__all__ = ["run_clean_command", "run_init_command"]


def run_init_command(args) -> int:
    """Handle init command."""
    from ..init_cmd import init_project

    return init_project(
        target=args.init_project,
        manufacturer=args.init_mfr,
        layers=args.init_layers,
        copper=args.init_copper,
        design_type=args.init_design_type,
        dry_run=args.init_dry_run,
        output_format=args.init_format,
    )


def run_clean_command(args) -> int:
    """Handle clean command."""
    from ..clean_cmd import main as clean_main

    sub_argv = [args.clean_project]
    if args.clean_dry_run:
        sub_argv.append("--dry-run")
    if args.clean_deep:
        sub_argv.append("--deep")
    if args.clean_force:
        sub_argv.append("--force")
    if args.clean_format != "text":
        sub_argv.extend(["--format", args.clean_format])
    if args.clean_verbose:
        sub_argv.append("--verbose")
    return clean_main(sub_argv)
