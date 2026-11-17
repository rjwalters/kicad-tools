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

        # Normally intercepted in cli.main() with its argv; reached only bare.
        return generate_main([]) or 0

    return 1
