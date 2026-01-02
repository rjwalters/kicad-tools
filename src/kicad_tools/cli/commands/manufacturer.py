"""Manufacturer (mfr) command handlers."""

import sys
from pathlib import Path

__all__ = ["run_mfr_command"]


def run_mfr_command(args) -> int:
    """Handle manufacturer subcommands."""
    if not args.mfr_command:
        print("Usage: kicad-tools mfr <command> [options]")
        print("Commands: list, info, rules, compare, apply-rules, validate, export-dru, import-dru")
        return 1

    from ..mfr import main as mfr_main

    if args.mfr_command == "list":
        return mfr_main(["list"]) or 0

    elif args.mfr_command == "info":
        return mfr_main(["info", args.manufacturer]) or 0

    elif args.mfr_command == "rules":
        sub_argv = ["rules", args.manufacturer]
        if args.layers != 4:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "compare":
        sub_argv = ["compare"]
        if args.layers != 4:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "apply-rules":
        sub_argv = ["apply-rules", args.file, args.manufacturer]
        if args.layers != 2:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        if args.output:
            sub_argv.extend(["--output", args.output])
        if args.dry_run:
            sub_argv.append("--dry-run")
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "validate":
        sub_argv = ["validate", args.file, args.manufacturer]
        if args.layers != 2:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "export-dru":
        sub_argv = ["export-dru", args.manufacturer]
        if args.layers != 4:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        if args.output:
            sub_argv.extend(["--output", args.output])
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "import-dru":
        from ..mfr_dru import import_dru

        file_path = Path(args.file)
        if not file_path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            return 1
        return import_dru(file_path, args.format)

    return 1
