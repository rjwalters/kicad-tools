"""Validation command handlers (check, validate, validate-footprints, fix-footprints)."""

__all__ = [
    "run_check_command",
    "run_validate_command",
    "run_validate_connectivity_command",
    "run_validate_footprints_command",
    "run_fix_footprints_command",
]


def run_validate_footprints_command(args) -> int:
    """Handle validate-footprints command."""
    from ..footprint_cmd import main_validate

    sub_argv = [args.pcb]
    if args.min_pad_gap != 0.15:
        sub_argv.extend(["--min-pad-gap", str(args.min_pad_gap)])
    if args.format != "text":
        sub_argv.extend(["--format", args.format])
    if args.errors_only:
        sub_argv.append("--errors-only")
    # Standard comparison options
    if getattr(args, "compare_standard", False):
        sub_argv.append("--compare-standard")
    if getattr(args, "tolerance", 0.05) != 0.05:
        sub_argv.extend(["--tolerance", str(args.tolerance)])
    if getattr(args, "kicad_library_path", None):
        sub_argv.extend(["--kicad-library-path", args.kicad_library_path])
    # Use global quiet flag
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    return main_validate(sub_argv)


def run_fix_footprints_command(args) -> int:
    """Handle fix-footprints command."""
    from ..footprint_cmd import main_fix

    sub_argv = [args.pcb]
    if args.output:
        sub_argv.extend(["-o", args.output])
    if args.min_pad_gap != 0.2:
        sub_argv.extend(["--min-pad-gap", str(args.min_pad_gap)])
    if args.format != "text":
        sub_argv.extend(["--format", args.format])
    if args.dry_run:
        sub_argv.append("--dry-run")
    # Use global quiet flag
    if getattr(args, "global_quiet", False):
        sub_argv.append("--quiet")
    return main_fix(sub_argv)


def run_check_command(args) -> int:
    """Handle check command (pure Python DRC)."""
    from ..check_cmd import main as check_main

    sub_argv = [args.pcb]
    if args.format != "table":
        sub_argv.extend(["--format", args.format])
    if args.errors_only:
        sub_argv.append("--errors-only")
    if args.strict:
        sub_argv.append("--strict")
    if args.mfr != "jlcpcb":
        sub_argv.extend(["--mfr", args.mfr])
    if args.layers != 2:
        sub_argv.extend(["--layers", str(args.layers)])
    if args.copper != 1.0:
        sub_argv.extend(["--copper", str(args.copper)])
    if args.only_checks:
        sub_argv.extend(["--only", args.only_checks])
    if args.skip_checks:
        sub_argv.extend(["--skip", args.skip_checks])
    if args.verbose:
        sub_argv.append("--verbose")
    return check_main(sub_argv)


def run_validate_command(args) -> int:
    """Handle validate command."""
    # Route to connectivity validation if --connectivity flag is set
    if getattr(args, "connectivity", False):
        return run_validate_connectivity_command(args)

    # Default to sync validation
    if not args.sync:
        print("Usage: kicad-tools validate --sync [options] <project>")
        print("       kicad-tools validate --connectivity [options] <pcb>")
        print("\nOptions:")
        print("  --sync           Check schematic-to-PCB netlist synchronization")
        print("  --connectivity   Check net connectivity on PCB (detect unrouted nets)")
        return 1

    from ..validate_sync_cmd import main as validate_sync_main

    sub_argv = []
    if args.validate_project:
        sub_argv.append(args.validate_project)
    if args.validate_schematic:
        sub_argv.extend(["--schematic", args.validate_schematic])
    if args.validate_pcb:
        sub_argv.extend(["--pcb", args.validate_pcb])
    if args.validate_format != "table":
        sub_argv.extend(["--format", args.validate_format])
    if args.validate_errors_only:
        sub_argv.append("--errors-only")
    if args.validate_strict:
        sub_argv.append("--strict")
    if args.validate_verbose:
        sub_argv.append("--verbose")
    return validate_sync_main(sub_argv)


def run_validate_connectivity_command(args) -> int:
    """Handle validate --connectivity command."""
    from ..validate_connectivity_cmd import main as validate_connectivity_main

    sub_argv = []

    # Get PCB path from project or --pcb flag
    pcb_path = getattr(args, "validate_pcb", None) or getattr(args, "validate_project", None)
    if not pcb_path:
        print("Error: PCB file required. Use --pcb or provide a .kicad_pcb file.")
        return 1

    sub_argv.append(pcb_path)

    if getattr(args, "validate_format", "table") != "table":
        sub_argv.extend(["--format", args.validate_format])
    if getattr(args, "validate_errors_only", False):
        sub_argv.append("--errors-only")
    if getattr(args, "validate_strict", False):
        sub_argv.append("--strict")
    if getattr(args, "validate_verbose", False):
        sub_argv.append("--verbose")

    return validate_connectivity_main(sub_argv)
