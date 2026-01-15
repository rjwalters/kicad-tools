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

    # Use global quiet or command-level quiet
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")

    return build_main(sub_argv)
