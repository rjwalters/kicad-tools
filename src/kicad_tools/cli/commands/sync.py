"""Sync command handler for kicad-tools CLI."""

__all__ = ["run_sync_command"]


def run_sync_command(args) -> int:
    """Handle sync command by dispatching to sync_cmd.main()."""
    from ..sync_cmd import main as sync_main

    sub_argv = []

    # Mode flags
    if getattr(args, "sync_analyze", False):
        sub_argv.append("--analyze")
    elif getattr(args, "sync_apply", False):
        sub_argv.append("--apply")

    # Project file (positional)
    if getattr(args, "sync_project", None):
        sub_argv.append(args.sync_project)

    # Explicit file paths
    if getattr(args, "sync_schematic", None):
        sub_argv.extend(["--schematic", args.sync_schematic])
    if getattr(args, "sync_pcb", None):
        sub_argv.extend(["--pcb", args.sync_pcb])

    # Output options
    sync_format = getattr(args, "sync_format", "table")
    if sync_format != "table":
        sub_argv.extend(["--format", sync_format])
    if getattr(args, "sync_output_mapping", None):
        sub_argv.extend(["--output-mapping", args.sync_output_mapping])
    if getattr(args, "sync_output", None):
        sub_argv.extend(["--output", args.sync_output])

    # Apply options
    if getattr(args, "sync_dry_run", False):
        sub_argv.append("--dry-run")
    if getattr(args, "sync_confirm", False):
        sub_argv.append("--confirm")
    min_conf = getattr(args, "sync_min_confidence", "high")
    if min_conf != "high":
        sub_argv.extend(["--min-confidence", min_conf])

    return sync_main(sub_argv)
