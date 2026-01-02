"""Footprint command handlers."""

__all__ = ["run_footprint_command"]


def run_footprint_command(args) -> int:
    """Handle footprint subcommands."""
    if not args.footprint_command:
        print("Usage: kicad-tools footprint <command> [options]")
        print("Commands: generate")
        return 1

    if args.footprint_command == "generate":
        from ..footprint_generate import main as generate_main

        # Pass all remaining arguments to the generate subcommand
        sub_argv = args.fp_args if hasattr(args, "fp_args") and args.fp_args else []
        return generate_main(sub_argv) or 0

    return 1
