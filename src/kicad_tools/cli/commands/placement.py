"""Placement command handlers."""

__all__ = ["run_placement_command"]


def run_placement_command(args) -> int:
    """Handle placement command."""
    if not args.placement_command:
        print("Usage: kicad-tools placement <command> [options] <file>")
        print(
            "Commands: check, fix, nudge, optimize, snap, align, distribute, "
            "suggest, place-unplaced, refine"
        )
        return 1

    from ..placement_cmd import main as placement_main

    if args.placement_command == "check":
        sub_argv = ["check", args.pcb]
        if args.format != "table":
            sub_argv.extend(["--format", args.format])
        if args.pad_clearance != 0.1:
            sub_argv.extend(["--pad-clearance", str(args.pad_clearance)])
        if args.hole_clearance != 0.5:
            sub_argv.extend(["--hole-clearance", str(args.hole_clearance)])
        if args.edge_clearance != 0.3:
            sub_argv.extend(["--edge-clearance", str(args.edge_clearance)])
        if args.verbose:
            sub_argv.append("--verbose")
        # Use command-level quiet or global quiet
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        if getattr(args, "signal_integrity", False):
            sub_argv.append("--signal-integrity")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "fix":
        sub_argv = ["fix", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.strategy != "spread":
            sub_argv.extend(["--strategy", args.strategy])
        if args.anchor:
            sub_argv.extend(["--anchor", args.anchor])
        if getattr(args, "only", None):
            sub_argv.extend(["--only", args.only])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if getattr(args, "timeout", None) is not None:
            sub_argv.extend(["--timeout", str(args.timeout)])
        if args.verbose:
            sub_argv.append("--verbose")
        # Use command-level quiet or global quiet
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "nudge":
        sub_argv = ["nudge", args.pcb]
        if getattr(args, "output", None):
            sub_argv.extend(["-o", args.output])
        if getattr(args, "anchor", None):
            sub_argv.extend(["--anchor", args.anchor])
        if getattr(args, "pad_clearance", 0.1) != 0.1:
            sub_argv.extend(["--pad-clearance", str(args.pad_clearance)])
        if getattr(args, "dry_run", False):
            sub_argv.append("--dry-run")
        if getattr(args, "verbose", False):
            sub_argv.append("--verbose")
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "optimize":
        sub_argv = ["optimize", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.strategy != "force-directed":
            sub_argv.extend(["--strategy", args.strategy])
        if args.iterations != 1000:
            sub_argv.extend(["--iterations", str(args.iterations)])
        if args.generations != 100:
            sub_argv.extend(["--generations", str(args.generations)])
        if args.population != 50:
            sub_argv.extend(["--population", str(args.population)])
        if args.grid != 0.0:
            sub_argv.extend(["--grid", str(args.grid)])
        if args.fixed:
            sub_argv.extend(["--fixed", args.fixed])
        if getattr(args, "cluster", False):
            sub_argv.append("--cluster")
        if getattr(args, "constraints", None):
            sub_argv.extend(["--constraints", args.constraints])
        if getattr(args, "edge_detect", False):
            sub_argv.append("--edge-detect")
        if getattr(args, "thermal", False):
            sub_argv.append("--thermal")
        if getattr(args, "keepout", None):
            sub_argv.extend(["--keepout", args.keepout])
        if getattr(args, "auto_keepout", False):
            sub_argv.append("--auto-keepout")
        if getattr(args, "routing_aware", False):
            sub_argv.append("--routing-aware")
        if getattr(args, "use_routing_fitness", False):
            sub_argv.append("--use-routing-fitness")
        if getattr(args, "check_routability", False):
            sub_argv.append("--check-routability")
        if getattr(args, "boundary_margin", None) is not None:
            sub_argv.extend(["--boundary-margin", str(args.boundary_margin)])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        if args.verbose:
            sub_argv.append("--verbose")
        # Use command-level quiet or global quiet
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "snap":
        sub_argv = ["snap", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        if args.grid != 0.5:
            sub_argv.extend(["--grid", str(args.grid)])
        if args.rotation != 90:
            sub_argv.extend(["--rotation", str(args.rotation)])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.verbose:
            sub_argv.append("--verbose")
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "align":
        sub_argv = ["align", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        sub_argv.extend(["--components", args.components])
        if args.axis != "row":
            sub_argv.extend(["--axis", args.axis])
        if args.reference != "center":
            sub_argv.extend(["--reference", args.reference])
        if args.tolerance != 0.1:
            sub_argv.extend(["--tolerance", str(args.tolerance)])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.verbose:
            sub_argv.append("--verbose")
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "distribute":
        sub_argv = ["distribute", args.pcb]
        if args.output:
            sub_argv.extend(["-o", args.output])
        sub_argv.extend(["--components", args.components])
        if args.axis != "horizontal":
            sub_argv.extend(["--axis", args.axis])
        if args.spacing != 0.0:
            sub_argv.extend(["--spacing", str(args.spacing)])
        if args.dry_run:
            sub_argv.append("--dry-run")
        if args.verbose:
            sub_argv.append("--verbose")
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "suggest":
        sub_argv = ["suggest", args.pcb]
        if getattr(args, "component", None):
            sub_argv.extend(["--component", args.component])
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        if args.verbose:
            sub_argv.append("--verbose")
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "place-unplaced":
        sub_argv = ["place-unplaced", args.pcb]
        if getattr(args, "output", None):
            sub_argv.extend(["-o", args.output])
        if getattr(args, "margin", 2.0) != 2.0:
            sub_argv.extend(["--margin", str(args.margin)])
        if getattr(args, "spacing", 2.0) != 2.0:
            sub_argv.extend(["--spacing", str(args.spacing)])
        if getattr(args, "cluster", False):
            sub_argv.append("--cluster")
        if getattr(args, "dry_run", False):
            sub_argv.append("--dry-run")
        if getattr(args, "format", "text") != "text":
            sub_argv.extend(["--format", args.format])
        if getattr(args, "verbose", False):
            sub_argv.append("--verbose")
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    elif args.placement_command == "refine":
        sub_argv = ["refine", args.pcb]
        if getattr(args, "output", None):
            sub_argv.extend(["-o", args.output])
        if getattr(args, "fixed", None):
            sub_argv.extend(["--fixed", args.fixed])
        if getattr(args, "json", False):
            sub_argv.append("--json")
        if getattr(args, "verbose", False):
            sub_argv.append("--verbose")
        if getattr(args, "quiet", False) or getattr(args, "global_quiet", False):
            sub_argv.append("--quiet")
        return placement_main(sub_argv) or 0

    return 1
