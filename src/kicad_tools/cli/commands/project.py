"""Project-level command handlers."""

__all__ = ["run_clean_command"]


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
