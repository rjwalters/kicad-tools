"""Native build command handlers."""

__all__ = ["run_build_native_command"]


def run_build_native_command(args) -> int:
    """Handle build-native command."""
    from ..build_native_cmd import main as build_native_main

    sub_argv = []
    if getattr(args, "build_native_verbose", False):
        sub_argv.append("--verbose")
    if getattr(args, "build_native_force", False):
        sub_argv.append("--force")
    if getattr(args, "build_native_jobs", None):
        sub_argv.extend(["--jobs", str(args.build_native_jobs)])
    if getattr(args, "build_native_format", "text") != "text":
        sub_argv.extend(["--format", args.build_native_format])
    if getattr(args, "build_native_check", False):
        sub_argv.append("--check")
    return build_native_main(sub_argv)
