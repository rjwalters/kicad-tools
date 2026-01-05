"""Validation command handlers (check, validate, validate-footprints, fix-footprints, constraints)."""

__all__ = [
    "run_check_command",
    "run_validate_command",
    "run_validate_connectivity_command",
    "run_validate_consistency_command",
    "run_validate_placement_command",
    "run_validate_footprints_command",
    "run_fix_footprints_command",
    "run_constraints_command",
    "run_audit_command",
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

    # Route to consistency validation if --consistency flag is set
    if getattr(args, "consistency", False):
        return run_validate_consistency_command(args)

    # Route to placement validation if --placement flag is set
    if getattr(args, "placement", False):
        return run_validate_placement_command(args)

    # Default to sync validation
    if not args.sync:
        print("Usage: kicad-tools validate --sync [options] <project>")
        print("       kicad-tools validate --sync [options] <schematic> <pcb>")
        print("       kicad-tools validate --connectivity [options] <pcb>")
        print("       kicad-tools validate --consistency [options] <project>")
        print("       kicad-tools validate --placement [options] <project>")
        print("\nOptions:")
        print("  --sync           Check schematic-to-PCB netlist synchronization")
        print("  --connectivity   Check net connectivity on PCB (detect unrouted nets)")
        print(
            "  --consistency    Check schematic-to-PCB consistency (components, nets, properties)"
        )
        print("  --placement      Check BOM components are placed on PCB")
        return 1

    from ..validate_sync_cmd import main as validate_sync_main

    sub_argv = []

    # Handle positional file arguments (validate_files is a list)
    validate_files = getattr(args, "validate_files", []) or []
    for f in validate_files:
        sub_argv.append(f)

    # Also pass explicit flags if provided
    if getattr(args, "validate_schematic", None):
        sub_argv.extend(["--schematic", args.validate_schematic])
    if getattr(args, "validate_pcb", None):
        sub_argv.extend(["--pcb", args.validate_pcb])
    if getattr(args, "validate_format", "table") != "table":
        sub_argv.extend(["--format", args.validate_format])
    if getattr(args, "validate_errors_only", False):
        sub_argv.append("--errors-only")
    if getattr(args, "validate_strict", False):
        sub_argv.append("--strict")
    if getattr(args, "validate_verbose", False):
        sub_argv.append("--verbose")
    return validate_sync_main(sub_argv)


def run_validate_connectivity_command(args) -> int:
    """Handle validate --connectivity command."""
    from ..validate_connectivity_cmd import main as validate_connectivity_main

    sub_argv = []

    # Get PCB path from validate_files or --pcb flag
    validate_files = getattr(args, "validate_files", []) or []
    pcb_path = getattr(args, "validate_pcb", None)

    # Try to find PCB from positional files
    if not pcb_path:
        for f in validate_files:
            if f.lower().endswith(".kicad_pcb"):
                pcb_path = f
                break
            elif f.lower().endswith(".kicad_pro"):
                # Project file - just pass it through
                pcb_path = f
                break

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


def run_validate_consistency_command(args) -> int:
    """Handle validate --consistency command."""
    from ..validate_consistency_cmd import main as validate_consistency_main

    sub_argv = []

    # Handle positional file arguments
    validate_files = getattr(args, "validate_files", []) or []
    for f in validate_files:
        sub_argv.append(f)

    if getattr(args, "validate_schematic", None):
        sub_argv.extend(["--schematic", args.validate_schematic])
    if getattr(args, "validate_pcb", None):
        sub_argv.extend(["--pcb", args.validate_pcb])
    if getattr(args, "validate_format", "table") != "table":
        sub_argv.extend(["--format", args.validate_format])
    if getattr(args, "validate_errors_only", False):
        sub_argv.append("--errors-only")
    if getattr(args, "validate_strict", False):
        sub_argv.append("--strict")
    if getattr(args, "validate_verbose", False):
        sub_argv.append("--verbose")

    return validate_consistency_main(sub_argv)


def run_validate_placement_command(args) -> int:
    """Handle validate --placement command."""
    from ..validate_placement_cmd import main as validate_placement_main

    sub_argv = []

    # Handle positional file arguments
    validate_files = getattr(args, "validate_files", []) or []
    for f in validate_files:
        sub_argv.append(f)

    if getattr(args, "validate_schematic", None):
        sub_argv.extend(["--schematic", args.validate_schematic])
    if getattr(args, "validate_pcb", None):
        sub_argv.extend(["--pcb", args.validate_pcb])
    if getattr(args, "validate_format", "table") != "table":
        sub_argv.extend(["--format", args.validate_format])
    if getattr(args, "validate_errors_only", False):
        sub_argv.append("--errors-only")
    if getattr(args, "validate_strict", False):
        sub_argv.append("--strict")
    if getattr(args, "validate_verbose", False):
        sub_argv.append("--verbose")

    return validate_placement_main(sub_argv)


def run_constraints_command(args) -> int:
    """Handle constraints command with its subcommands."""
    if not getattr(args, "constraints_command", None):
        print("Usage: kicad-tools constraints <command> [options]")
        print("\nCommands:")
        print("  check    Detect conflicts between constraints")
        print("\nUse 'kicad-tools constraints <command> --help' for more info.")
        return 1

    if args.constraints_command == "check":
        from ..constraints_cmd import main as constraints_main

        sub_argv = [args.pcb]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.keepout:
            sub_argv.extend(["--keepout", args.keepout])
        if getattr(args, "constraints_file", None):
            sub_argv.extend(["--constraints", args.constraints_file])
        if getattr(args, "auto_keepout", False):
            sub_argv.append("--auto-keepout")
        if args.verbose:
            sub_argv.append("--verbose")
        return constraints_main(sub_argv)

    return 0


def run_audit_command(args) -> int:
    """Handle audit command for manufacturing readiness."""
    from ..audit_cmd import main as audit_main

    sub_argv = [args.audit_project]

    if getattr(args, "audit_format", "table") != "table":
        sub_argv.extend(["--format", args.audit_format])
    if getattr(args, "audit_mfr", "jlcpcb") != "jlcpcb":
        sub_argv.extend(["--mfr", args.audit_mfr])
    if getattr(args, "audit_layers", None):
        sub_argv.extend(["--layers", str(args.audit_layers)])
    if getattr(args, "audit_copper", 1.0) != 1.0:
        sub_argv.extend(["--copper", str(args.audit_copper)])
    if getattr(args, "audit_quantity", 5) != 5:
        sub_argv.extend(["--quantity", str(args.audit_quantity)])
    if getattr(args, "audit_skip_erc", False):
        sub_argv.append("--skip-erc")
    if getattr(args, "audit_strict", False):
        sub_argv.append("--strict")
    if getattr(args, "audit_verbose", False):
        sub_argv.append("--verbose")

    return audit_main(sub_argv)
