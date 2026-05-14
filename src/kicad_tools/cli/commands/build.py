"""Build command handler for end-to-end workflow orchestration."""

__all__ = ["run_build_command"]


def run_build_command(args) -> int:
    """Handle build command for end-to-end workflow."""
    from ..build_cmd import main as build_main

    sub_argv = []

    # Positional spec argument
    if getattr(args, "build_spec", None):
        sub_argv.append(args.build_spec)

    # Step selection
    if getattr(args, "build_step", "all") != "all":
        sub_argv.extend(["--step", args.build_step])

    # Manufacturer
    if getattr(args, "build_mfr", "jlcpcb") != "jlcpcb":
        sub_argv.extend(["--mfr", args.build_mfr])

    # Flags
    if getattr(args, "build_dry_run", False):
        sub_argv.append("--dry-run")

    if getattr(args, "build_verbose", False):
        sub_argv.append("--verbose")

    if getattr(args, "build_force", False):
        sub_argv.append("--force")

    # Quiet may come from the command-level --quiet/-q flag or the global flag
    if getattr(args, "build_quiet", False) or getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    # Output directory
    build_output = getattr(args, "build_output", None)
    if build_output:
        sub_argv.extend(["--output", build_output])

    # Optimize placement (opt-in CMA-ES)
    if getattr(args, "build_optimize_placement", False):
        sub_argv.append("--optimize-placement")

    # Smoke-check opt-out
    if getattr(args, "build_no_smoke_check", False):
        sub_argv.append("--no-smoke-check")

    # Routing-completeness preflight escape hatch
    if getattr(args, "build_allow_incomplete", False):
        sub_argv.append("--allow-incomplete")

    return build_main(sub_argv)
