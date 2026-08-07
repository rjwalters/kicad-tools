"""Manufacturer (mfr) command handlers."""

import sys
from pathlib import Path

__all__ = ["run_mfr_command"]


def _forward_format(args, sub_argv: list) -> None:
    """Forward the outer ``--format`` choice to the inner ``mfr`` parser."""
    if getattr(args, "format", "text") != "text":
        sub_argv.extend(["--format", args.format])


def run_mfr_command(args) -> int:
    """Handle manufacturer subcommands."""
    if not args.mfr_command:
        print("Usage: kicad-tools mfr <command> [options]")
        print("Commands: list, info, rules, compare, apply-rules, validate, export-dru, import-dru")
        return 1

    from ..mfr import main as mfr_main

    if args.mfr_command == "list":
        sub_argv = ["list"]
        _forward_format(args, sub_argv)
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "info":
        sub_argv = ["info", args.manufacturer]
        _forward_format(args, sub_argv)
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "rules":
        sub_argv = ["rules", args.manufacturer]
        # Always pass layers and copper to ensure inner command uses correct values
        sub_argv.extend(["--layers", str(args.layers)])
        sub_argv.extend(["--copper", str(args.copper)])
        _forward_format(args, sub_argv)
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "compare":
        sub_argv = ["compare"]
        # Always pass layers and copper to ensure inner command uses correct values
        sub_argv.extend(["--layers", str(args.layers)])
        sub_argv.extend(["--copper", str(args.copper)])
        _forward_format(args, sub_argv)
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
        _forward_format(args, sub_argv)
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "validate":
        sub_argv = ["validate", args.file, args.manufacturer]
        if args.layers != 2:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        _forward_format(args, sub_argv)
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "export-dru":
        sub_argv = ["export-dru", args.manufacturer]
        if args.layers != 4:
            sub_argv.extend(["--layers", str(args.layers)])
        if args.copper != 1.0:
            sub_argv.extend(["--copper", str(args.copper)])
        if args.output:
            sub_argv.extend(["--output", args.output])
        _forward_format(args, sub_argv)
        return mfr_main(sub_argv) or 0

    elif args.mfr_command == "import-dru":
        from ..mfr_dru import import_dru

        file_path = Path(args.file)
        if not file_path.exists():
            print(f"Error: File not found: {file_path}", file=sys.stderr)
            return 1
        return import_dru(file_path, args.format)

    return 1
