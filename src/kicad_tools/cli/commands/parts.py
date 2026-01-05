"""Parts (LCSC) command handlers."""

__all__ = ["run_parts_command"]


def run_parts_command(args) -> int:
    """Handle parts subcommands."""
    if not args.parts_command:
        print("Usage: kicad-tools parts <command> [options]")
        print("Commands: lookup, search, availability, cache")
        return 1

    from ..parts_cmd import main as parts_main

    if args.parts_command == "lookup":
        sub_argv = ["lookup", args.part]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.no_cache:
            sub_argv.append("--no-cache")
        return parts_main(sub_argv) or 0

    elif args.parts_command == "search":
        sub_argv = ["search", args.query]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.limit != 20:
            sub_argv.extend(["--limit", str(args.limit)])
        if args.in_stock:
            sub_argv.append("--in-stock")
        if args.basic:
            sub_argv.append("--basic")
        return parts_main(sub_argv) or 0

    elif args.parts_command == "availability":
        sub_argv = ["availability", args.schematic]
        if args.quantity != 1:
            sub_argv.extend(["--quantity", str(args.quantity)])
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.no_alternatives:
            sub_argv.append("--no-alternatives")
        if args.issues_only:
            sub_argv.append("--issues-only")
        return parts_main(sub_argv) or 0

    elif args.parts_command == "cache":
        sub_argv = ["cache", args.cache_action]
        return parts_main(sub_argv) or 0

    return 1
