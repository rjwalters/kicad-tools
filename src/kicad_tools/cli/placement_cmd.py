"""CLI commands for placement conflict detection and resolution.

Usage:
    kicad-tools placement check board.kicad_pcb
    kicad-tools placement fix board.kicad_pcb --strategy spread
    kicad-tools placement optimize board.kicad_pcb --strategy force-directed
"""

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.placement import (
    Conflict,
    PlacementAnalyzer,
    PlacementFixer,
)
from kicad_tools.placement.analyzer import DesignRules
from kicad_tools.placement.fixer import FixStrategy


def cmd_check(args) -> int:
    """Check PCB for placement conflicts."""
    from kicad_tools.cli.progress import spinner

    quiet = getattr(args, "quiet", False)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Build design rules from arguments
    rules = DesignRules(
        min_pad_clearance=args.pad_clearance,
        min_hole_to_hole=args.hole_clearance,
        min_edge_clearance=args.edge_clearance,
        courtyard_margin=args.courtyard_margin,
    )

    # Analyze
    analyzer = PlacementAnalyzer(verbose=args.verbose and not quiet)

    try:
        with spinner("Analyzing placement...", quiet=quiet):
            conflicts = analyzer.find_conflicts(pcb_path, rules)
    except Exception as e:
        print(f"Error analyzing PCB: {e}", file=sys.stderr)
        return 1

    # Output results
    if args.format == "json":
        output_json(conflicts)
    elif args.format == "summary":
        output_summary(conflicts)
    else:
        output_table(conflicts, args.verbose)

    # Return code based on conflicts
    errors = [c for c in conflicts if c.severity.value == "error"]
    return 1 if errors else 0


def cmd_fix(args) -> int:
    """Suggest and apply fixes for placement conflicts."""
    from kicad_tools.cli.progress import spinner

    quiet = getattr(args, "quiet", False)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Build design rules
    rules = DesignRules(
        min_pad_clearance=args.pad_clearance,
        min_hole_to_hole=args.hole_clearance,
        min_edge_clearance=args.edge_clearance,
        courtyard_margin=args.courtyard_margin,
    )

    # Analyze first
    analyzer = PlacementAnalyzer(verbose=args.verbose and not quiet)

    with spinner("Analyzing placement...", quiet=quiet):
        conflicts = analyzer.find_conflicts(pcb_path, rules)

    if not conflicts:
        if not quiet:
            print("No placement conflicts found!")
        return 0

    if not quiet:
        print(f"Found {len(conflicts)} conflicts")

    # Parse strategy
    strategy = FixStrategy(args.strategy)

    # Parse anchored components
    anchored = set()
    if args.anchor:
        anchored = set(args.anchor.split(","))
        if not quiet:
            print(f"Anchored components: {anchored}")

    # Create fixer and suggest fixes
    fixer = PlacementFixer(
        strategy=strategy,
        anchored=anchored,
        verbose=args.verbose and not quiet,
    )

    with spinner("Generating fix suggestions...", quiet=quiet):
        fixes = fixer.suggest_fixes(conflicts, analyzer)

    if not fixes:
        if not quiet:
            print("No fixes could be suggested")
        return 0

    if not quiet:
        print(f"\nSuggested {len(fixes)} fixes:")
        print(fixer.preview_fixes(fixes))

    if args.dry_run:
        if not quiet:
            print("\n(Dry run - no changes made)")
        return 0

    # Apply fixes
    output_path = args.output or pcb_path

    with spinner("Applying fixes...", quiet=quiet):
        result = fixer.apply_fixes(pcb_path, fixes, output_path)

    if not quiet:
        print(f"\n{result.message}")

        if result.new_conflicts > 0:
            print(f"Warning: {result.new_conflicts} conflicts remain after fixes")

    return 0 if result.success else 1


def cmd_optimize(args) -> int:
    """Optimize component placement for routability."""
    from kicad_tools.cli.progress import spinner
    from kicad_tools.optim import (
        EvolutionaryPlacementOptimizer,
        PlacementConfig,
        PlacementOptimizer,
        load_constraints_from_yaml,
    )
    from kicad_tools.optim.evolutionary import EvolutionaryConfig
    from kicad_tools.schema.pcb import PCB

    quiet = getattr(args, "quiet", False)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Parse fixed components
    fixed_refs = []
    if args.fixed:
        fixed_refs = [r.strip() for r in args.fixed.split(",") if r.strip()]

    # Load constraints if specified
    constraints = []
    if args.constraints:
        constraints_path = Path(args.constraints)
        if not constraints_path.exists():
            print(f"Error: Constraint file not found: {constraints_path}", file=sys.stderr)
            return 1
        try:
            with spinner("Loading constraints...", quiet=quiet):
                constraints = load_constraints_from_yaml(constraints_path)
            if not quiet:
                print(f"Loaded {len(constraints)} grouping constraints")
        except Exception as e:
            print(f"Error loading constraints: {e}", file=sys.stderr)
            return 1

    # Load PCB
    try:
        with spinner("Loading PCB...", quiet=quiet):
            pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    strategy = args.strategy
    enable_clustering = getattr(args, "cluster", False)

    if not quiet:
        print(f"Optimization strategy: {strategy}")
        if fixed_refs:
            print(f"Fixed components: {', '.join(fixed_refs)}")
        if enable_clustering:
            print("Functional clustering: enabled")

    try:
        if strategy == "force-directed":
            # Physics-based optimization
            config = PlacementConfig(
                grid_size=args.grid if args.grid > 0 else 0.0,
                rotation_grid=90.0,
            )

            with spinner("Creating optimizer from PCB...", quiet=quiet):
                optimizer = PlacementOptimizer.from_pcb(
                    pcb, config=config, fixed_refs=fixed_refs, enable_clustering=enable_clustering
                )

            # Add constraints if loaded
            if constraints:
                optimizer.add_grouping_constraints(constraints)

            if not quiet:
                print(f"Optimizing {len(optimizer.components)} components...")
                print(f"  - {len(optimizer.springs)} net connections")
                if enable_clustering and optimizer.clusters:
                    print(f"  - {len(optimizer.clusters)} functional clusters detected")
                if constraints:
                    print(f"  - {len(constraints)} grouping constraints")
                print(f"  - Max iterations: {args.iterations}")

            # Run simulation with progress
            def callback(iteration: int, energy: float):
                if args.verbose and iteration % 100 == 0:
                    print(f"  Iteration {iteration}: energy={energy:.4f}")

            with spinner(
                f"Running force-directed optimization ({args.iterations} iterations)...",
                quiet=quiet,
            ):
                iterations_run = optimizer.run(
                    iterations=args.iterations, callback=callback if args.verbose else None
                )

            # Snap to grid
            if args.grid > 0:
                optimizer.snap_to_grid(args.grid, 90.0)

            if not quiet:
                print(f"\nConverged after {iterations_run} iterations")
                print(f"Total wire length: {optimizer.total_wire_length():.2f} mm")
                print(f"System energy: {optimizer.compute_energy():.4f}")

                # Report constraint violations if any
                if constraints:
                    violations = optimizer.validate_constraints()
                    if violations:
                        print(f"\nConstraint violations ({len(violations)}):")
                        for v in violations:
                            print(f"  - {v}")
                    else:
                        print("\nAll grouping constraints satisfied!")

        elif strategy == "evolutionary":
            # Genetic algorithm optimization
            config = EvolutionaryConfig(
                generations=args.generations,
                population_size=args.population,
                grid_snap=args.grid if args.grid > 0 else 0.127,
            )

            with spinner("Creating evolutionary optimizer from PCB...", quiet=quiet):
                optimizer = EvolutionaryPlacementOptimizer.from_pcb(
                    pcb, config=config, fixed_refs=fixed_refs, enable_clustering=enable_clustering
                )

            if not quiet:
                print(f"Optimizing {len(optimizer.components)} components...")
                if enable_clustering and optimizer.clusters:
                    print(f"  - {len(optimizer.clusters)} functional clusters detected")
                print(f"  - Generations: {args.generations}")
                print(f"  - Population: {args.population}")

            def callback(gen: int, best):
                if args.verbose:
                    print(f"  Generation {gen}: fitness={best.fitness:.2f}")

            with spinner(
                f"Running evolutionary optimization ({args.generations} generations)...",
                quiet=quiet,
            ):
                best = optimizer.optimize(
                    generations=args.generations,
                    population_size=args.population,
                    callback=callback if args.verbose else None,
                )

            if not quiet:
                print(f"\nBest fitness: {best.fitness:.2f}")
                print(optimizer.report())

        elif strategy == "hybrid":
            # Evolutionary + physics refinement
            config = EvolutionaryConfig(
                generations=args.generations,
                population_size=args.population,
                grid_snap=args.grid if args.grid > 0 else 0.127,
            )

            physics_config = PlacementConfig(
                grid_size=args.grid if args.grid > 0 else 0.0,
                rotation_grid=90.0,
            )

            with spinner("Creating hybrid optimizer from PCB...", quiet=quiet):
                evo_optimizer = EvolutionaryPlacementOptimizer.from_pcb(
                    pcb, config=config, fixed_refs=fixed_refs, enable_clustering=enable_clustering
                )

            if not quiet:
                print(f"Optimizing {len(evo_optimizer.components)} components...")
                if enable_clustering and evo_optimizer.clusters:
                    print(f"  - {len(evo_optimizer.clusters)} functional clusters detected")
                print(f"  - Phase 1: Evolutionary ({args.generations} generations)")
                print(f"  - Phase 2: Physics refinement ({args.iterations} iterations)")

            def callback(gen: int, best):
                if args.verbose:
                    print(f"  Generation {gen}: fitness={best.fitness:.2f}")

            with spinner("Running hybrid optimization...", quiet=quiet):
                optimizer = evo_optimizer.optimize_hybrid(
                    evolutionary_generations=args.generations,
                    population_size=args.population,
                    physics_iterations=args.iterations,
                    physics_config=physics_config,
                    callback=callback if args.verbose else None,
                )

            if not quiet:
                print(f"\nTotal wire length: {optimizer.total_wire_length():.2f} mm")
                print(f"System energy: {optimizer.compute_energy():.4f}")

        else:
            print(f"Error: Unknown strategy '{strategy}'", file=sys.stderr)
            return 1

    except Exception as e:
        print(f"Error during optimization: {e}", file=sys.stderr)
        if args.verbose:
            import traceback

            traceback.print_exc()
        return 1

    # Dry run - just report
    if args.dry_run:
        if not quiet:
            print("\n(Dry run - no changes made)")
            print(optimizer.report())
        return 0

    # Write results
    output_path = Path(args.output) if args.output else pcb_path

    try:
        with spinner("Writing optimized placement...", quiet=quiet):
            updated = optimizer.write_to_pcb(pcb)
            pcb.save(str(output_path))

        if not quiet:
            print(f"\nUpdated {updated} component positions")
            print(f"Saved to: {output_path}")

    except Exception as e:
        print(f"Error saving PCB: {e}", file=sys.stderr)
        return 1

    return 0


def output_table(conflicts: list[Conflict], verbose: bool = False):
    """Output conflicts in table format."""
    if not conflicts:
        print("No placement conflicts found!")
        return

    print(f"\n{'Type':<18} {'Severity':<10} {'Components':<20} {'Message'}")
    print("-" * 80)

    for conflict in conflicts:
        comp_str = f"{conflict.component1} / {conflict.component2}"
        if len(comp_str) > 18:
            comp_str = comp_str[:17] + "..."

        print(
            f"{conflict.type.value:<18} "
            f"{conflict.severity.value:<10} "
            f"{comp_str:<20} "
            f"{conflict.message}"
        )

        if verbose and conflict.location:
            print(f"  Location: ({conflict.location.x:.3f}, {conflict.location.y:.3f}) mm")

    # Summary
    errors = sum(1 for c in conflicts if c.severity.value == "error")
    warnings = sum(1 for c in conflicts if c.severity.value == "warning")

    print(f"\nTotal: {len(conflicts)} conflicts ({errors} errors, {warnings} warnings)")


def output_summary(conflicts: list[Conflict]):
    """Output conflict summary."""
    if not conflicts:
        print("No placement conflicts found!")
        return

    # Count by type
    by_type: dict = {}
    for c in conflicts:
        t = c.type.value
        if t not in by_type:
            by_type[t] = {"error": 0, "warning": 0}
        by_type[t][c.severity.value] += 1

    print("\nConflict Summary")
    print("=" * 50)

    for ctype, counts in sorted(by_type.items()):
        total = counts["error"] + counts["warning"]
        print(f"  {ctype}: {total} ({counts['error']} errors, {counts['warning']} warnings)")

    errors = sum(1 for c in conflicts if c.severity.value == "error")
    warnings = sum(1 for c in conflicts if c.severity.value == "warning")
    print(f"\nTotal: {len(conflicts)} conflicts ({errors} errors, {warnings} warnings)")


def output_json(conflicts: list[Conflict]):
    """Output conflicts as JSON."""
    print(json.dumps([c.to_dict() for c in conflicts], indent=2))


def main(argv: list[str] | None = None) -> int:
    """Main entry point for placement commands."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools placement",
        description="Detect and fix placement conflicts in KiCad PCBs",
    )

    subparsers = parser.add_subparsers(dest="command", help="Commands")

    # Check subcommand
    check_parser = subparsers.add_parser("check", help="Check PCB for placement conflicts")
    check_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    check_parser.add_argument(
        "--format",
        choices=["table", "json", "summary"],
        default="table",
        help="Output format",
    )
    check_parser.add_argument(
        "--pad-clearance",
        type=float,
        default=0.1,
        help="Minimum pad-to-pad clearance in mm (default: 0.1)",
    )
    check_parser.add_argument(
        "--hole-clearance",
        type=float,
        default=0.5,
        help="Minimum hole-to-hole clearance in mm (default: 0.5)",
    )
    check_parser.add_argument(
        "--edge-clearance",
        type=float,
        default=0.3,
        help="Minimum edge clearance in mm (default: 0.3)",
    )
    check_parser.add_argument(
        "--courtyard-margin",
        type=float,
        default=0.25,
        help="Courtyard margin around pads in mm (default: 0.25)",
    )
    check_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    check_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")

    # Fix subcommand
    fix_parser = subparsers.add_parser("fix", help="Suggest and apply placement fixes")
    fix_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    fix_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: modify in place)",
    )
    fix_parser.add_argument(
        "--strategy",
        choices=["spread", "compact", "anchor"],
        default="spread",
        help="Fix strategy (default: spread)",
    )
    fix_parser.add_argument(
        "--anchor",
        help="Comma-separated list of component references to keep fixed",
    )
    fix_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show suggested fixes without applying",
    )
    fix_parser.add_argument(
        "--pad-clearance",
        type=float,
        default=0.1,
        help="Minimum pad-to-pad clearance in mm",
    )
    fix_parser.add_argument(
        "--hole-clearance",
        type=float,
        default=0.5,
        help="Minimum hole-to-hole clearance in mm",
    )
    fix_parser.add_argument(
        "--edge-clearance",
        type=float,
        default=0.3,
        help="Minimum edge clearance in mm",
    )
    fix_parser.add_argument(
        "--courtyard-margin",
        type=float,
        default=0.25,
        help="Courtyard margin around pads in mm",
    )
    fix_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    fix_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")

    # Optimize subcommand
    optimize_parser = subparsers.add_parser(
        "optimize", help="Optimize component placement for routability"
    )
    optimize_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    optimize_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: modify in place)",
    )
    optimize_parser.add_argument(
        "--strategy",
        choices=["force-directed", "evolutionary", "hybrid"],
        default="force-directed",
        help="Optimization strategy (default: force-directed)",
    )
    optimize_parser.add_argument(
        "--iterations",
        type=int,
        default=1000,
        help="Max iterations for physics simulation (default: 1000)",
    )
    optimize_parser.add_argument(
        "--generations",
        type=int,
        default=100,
        help="Generations for evolutionary/hybrid mode (default: 100)",
    )
    optimize_parser.add_argument(
        "--population",
        type=int,
        default=50,
        help="Population size for evolutionary/hybrid mode (default: 50)",
    )
    optimize_parser.add_argument(
        "--grid",
        type=float,
        default=0.0,
        help="Position grid snap in mm (0 to disable, default: 0)",
    )
    optimize_parser.add_argument(
        "--fixed",
        help="Comma-separated component refs to keep fixed (e.g., J1,J2,H1)",
    )
    optimize_parser.add_argument(
        "--cluster",
        action="store_true",
        help="Enable functional clustering (groups bypass caps near ICs, etc.)",
    )
    optimize_parser.add_argument(
        "--constraints",
        help="Path to YAML file with grouping constraints",
    )
    optimize_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview optimization without saving",
    )
    optimize_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    optimize_parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

    args = parser.parse_args(argv)

    if not args.command:
        parser.print_help()
        return 0

    if args.command == "check":
        return cmd_check(args)
    elif args.command == "fix":
        return cmd_fix(args)
    elif args.command == "optimize":
        return cmd_optimize(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
