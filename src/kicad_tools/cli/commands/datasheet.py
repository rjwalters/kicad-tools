"""Datasheet command handlers."""

__all__ = ["run_datasheet_command"]


def run_datasheet_command(args) -> int:
    """Handle datasheet subcommands."""
    if not args.datasheet_command:
        print("Usage: kicad-tools datasheet <command> [options]")
        print(
            "Commands: search, download, list, cache, convert, extract-images, extract-tables, info"
        )
        return 1

    from ..datasheet_cmd import main as datasheet_main

    if args.datasheet_command == "search":
        sub_argv = ["search", args.part]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        if args.limit != 10:
            sub_argv.extend(["--limit", str(args.limit)])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "download":
        sub_argv = ["download", args.part]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.force:
            sub_argv.append("--force")
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "list":
        sub_argv = ["list"]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "cache":
        sub_argv = ["cache", args.cache_action]
        if getattr(args, "older_than", None):
            sub_argv.extend(["--older-than", str(args.older_than)])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "convert":
        sub_argv = ["convert", args.pdf]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.pages:
            sub_argv.extend(["--pages", args.pages])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "extract-images":
        sub_argv = ["extract-images", args.pdf, "-o", args.output]
        if args.pages:
            sub_argv.extend(["--pages", args.pages])
        if args.min_size != 100:
            sub_argv.extend(["--min-size", str(args.min_size)])
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "extract-tables":
        sub_argv = ["extract-tables", args.pdf]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.pages:
            sub_argv.extend(["--pages", args.pages])
        if args.format != "markdown":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    elif args.datasheet_command == "info":
        sub_argv = ["info", args.pdf]
        if args.format != "text":
            sub_argv.extend(["--format", args.format])
        return datasheet_main(sub_argv) or 0

    return 1
