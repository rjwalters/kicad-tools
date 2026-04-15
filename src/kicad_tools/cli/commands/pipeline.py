"""Pipeline command handler for end-to-end PCB repair workflow."""

__all__ = ["run_pipeline_command"]


def run_pipeline_command(args) -> int:
    """Handle pipeline command for existing PCB repair."""
    from ..pipeline_cmd import main as pipeline_main

    sub_argv = []

    # Positional input argument
    if getattr(args, "pipeline_input", None):
        sub_argv.append(args.pipeline_input)

    # Step selection
    if getattr(args, "pipeline_step", None):
        sub_argv.extend(["--step", args.pipeline_step])

    # Manufacturer
    if getattr(args, "pipeline_mfr", "jlcpcb") != "jlcpcb":
        sub_argv.extend(["--mfr", args.pipeline_mfr])

    # Layers
    if getattr(args, "pipeline_layers", None) is not None:
        sub_argv.extend(["--layers", str(args.pipeline_layers)])

    # Flags
    if getattr(args, "pipeline_dry_run", False):
        sub_argv.append("--dry-run")

    if getattr(args, "pipeline_verbose", False):
        sub_argv.append("--verbose")

    if getattr(args, "pipeline_force", False):
        sub_argv.append("--force")

    # Commit
    if getattr(args, "pipeline_commit", False):
        sub_argv.append("--commit")

    # Max displacement for fix-drc step
    max_disp = getattr(args, "pipeline_max_displacement", None)
    if max_disp is not None and max_disp != 2.0:
        sub_argv.extend(["--max-displacement", str(max_disp)])

    # Zones opt-in
    if getattr(args, "pipeline_zones", False):
        sub_argv.append("--zones")

    # Use global quiet or command-level quiet
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    return pipeline_main(sub_argv)
