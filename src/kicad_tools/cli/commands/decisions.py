"""Decisions command handler (design decision tracking)."""

__all__ = ["run_decisions_command"]


def run_decisions_command(args) -> int:
    """Handle decisions command."""
    from ..decisions_cmd import main as decisions_main

    # Build argument list for decisions_cmd
    sub_argv = []

    # Add subcommand
    if hasattr(args, "decisions_command") and args.decisions_command:
        sub_argv.append(args.decisions_command)

        # Add PCB path if present
        if hasattr(args, "pcb") and args.pcb:
            sub_argv.append(args.pcb)

        # Add component filter if present
        if hasattr(args, "component") and args.component:
            sub_argv.extend(["--component", args.component])

        # Add net filter if present
        if hasattr(args, "net") and args.net:
            sub_argv.extend(["--net", args.net])

        # Add action filter if present
        if hasattr(args, "action") and args.action:
            sub_argv.extend(["--action", args.action])

        # Add format if present
        if hasattr(args, "format") and args.format:
            sub_argv.extend(["--format", args.format])

        # Add limit if present and not default
        if hasattr(args, "limit") and args.limit != 20:
            sub_argv.extend(["--limit", str(args.limit)])

    return decisions_main(sub_argv)
