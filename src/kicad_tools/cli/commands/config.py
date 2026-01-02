"""Config and interactive command handlers."""

__all__ = ["run_config_command", "run_interactive_command"]


def run_config_command(args) -> int:
    """Handle config command."""
    from ..config_cmd import main as config_main

    sub_argv = []
    if args.show:
        sub_argv.append("--show")
    if args.init:
        sub_argv.append("--init")
    if args.paths:
        sub_argv.append("--paths")
    if args.user:
        sub_argv.append("--user")
    if args.config_action:
        sub_argv.append(args.config_action)
    if args.config_key:
        sub_argv.append(args.config_key)
    if args.config_value:
        sub_argv.append(args.config_value)
    return config_main(sub_argv) or 0


def run_interactive_command(args) -> int:
    """Handle interactive command."""
    from ..interactive import main as interactive_main

    sub_argv = []
    if args.project:
        sub_argv.extend(["--project", args.project])
    return interactive_main(sub_argv)
