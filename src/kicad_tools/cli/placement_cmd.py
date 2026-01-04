"""CLI commands for placement conflict detection and resolution.

Usage:
    kicad-tools placement check board.kicad_pcb
    kicad-tools placement fix board.kicad_pcb --strategy spread
    kicad-tools placement optimize board.kicad_pcb --strategy force-directed
    kicad-tools placement snap board.kicad_pcb --grid 0.5
    kicad-tools placement align board.kicad_pcb -c R1,R2,R3,R4 --axis row
    kicad-tools placement distribute board.kicad_pcb -c LED1,LED2,LED3 --spacing 5.0
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

    # Signal integrity analysis if requested
    if getattr(args, "signal_integrity", False):
        from kicad_tools.optim.signal_integrity import (
            analyze_placement_for_si,
            classify_nets,
            get_si_score,
        )
        from kicad_tools.schema.pcb import PCB

        try:
            with spinner("Analyzing signal integrity...", quiet=quiet):
                pcb = PCB.load(str(pcb_path))
                classifications = classify_nets(pcb)
                hints = analyze_placement_for_si(pcb, classifications)
                score = get_si_score(pcb, classifications)
        except Exception as e:
            print(f"Error analyzing signal integrity: {e}", file=sys.stderr)
            return 1

        output_si_analysis(classifications, hints, score, args.verbose)

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


def cmd_snap(args) -> int:
    """Snap components to grid."""
    from kicad_tools.cli.progress import spinner
    from kicad_tools.optim import PlacementConfig, PlacementOptimizer, snap_to_grid
    from kicad_tools.schema.pcb import PCB

    quiet = getattr(args, "quiet", False)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Load PCB
    try:
        with spinner("Loading PCB...", quiet=quiet):
            pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Create optimizer from PCB
    config = PlacementConfig()
    with spinner("Creating optimizer...", quiet=quiet):
        optimizer = PlacementOptimizer.from_pcb(pcb, config=config)

    if not quiet:
        print(f"Found {len(optimizer.components)} components")

    # Snap to grid
    rotation_snap = args.rotation if args.rotation > 0 else None
    with spinner(f"Snapping to {args.grid}mm grid...", quiet=quiet):
        count = snap_to_grid(optimizer, grid_mm=args.grid, rotation_snap=rotation_snap)

    if not quiet:
        print(f"Snapped {count} components")

    if args.dry_run:
        if not quiet:
            print("\n(Dry run - no changes made)")
            print(optimizer.report())
        return 0

    # Write results
    output_path = Path(args.output) if args.output else pcb_path

    try:
        with spinner("Writing snapped placement...", quiet=quiet):
            updated = optimizer.write_to_pcb(pcb)
            pcb.save(str(output_path))

        if not quiet:
            print(f"\nUpdated {updated} component positions")
            print(f"Saved to: {output_path}")

    except Exception as e:
        print(f"Error saving PCB: {e}", file=sys.stderr)
        return 1

    return 0


def cmd_refine(args) -> int:
    """Interactive placement refinement session."""
    from kicad_tools.cli.progress import spinner
    from kicad_tools.optim.query import process_json_request
    from kicad_tools.optim.session import PlacementSession
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

    # Load PCB
    try:
        with spinner("Loading PCB...", quiet=quiet):
            pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Create session
    try:
        with spinner("Creating placement session...", quiet=quiet):
            session = PlacementSession(pcb, fixed_refs=fixed_refs)
    except Exception as e:
        print(f"Error creating session: {e}", file=sys.stderr)
        return 1

    if not quiet:
        status = session.get_status()
        print("Placement session started")
        print(f"  Components: {status['components']}")
        print(f"  Initial score: {status['initial_score']:.4f}")
        print(f"  Violations: {status['violations']}")

    # JSON mode - read commands from stdin, write responses to stdout
    if args.json:
        if not quiet:
            print("\nJSON mode: reading commands from stdin...\n")

        for line in sys.stdin:
            line = line.strip()
            if not line:
                continue
            if line.lower() in ("quit", "exit"):
                break

            response = process_json_request(session, line)
            print(response)
            sys.stdout.flush()

        return 0

    # Interactive mode
    if not quiet:
        print("\nInteractive mode. Commands:")
        print("  query <ref> <x> <y> [rotation]  - Query move impact")
        print("  apply <ref> <x> <y> [rotation]  - Apply move")
        print("  suggest <ref>                   - Get placement suggestions")
        print("  undo                            - Undo last move")
        print("  status                          - Show session status")
        print("  list                            - List all components")
        print("  commit                          - Save changes to PCB")
        print("  rollback                        - Discard all changes")
        print("  quit                            - Exit (without saving)")
        print()

    output_path = Path(args.output) if args.output else pcb_path

    while True:
        try:
            cmd_line = input("> ").strip()
        except EOFError:
            break
        except KeyboardInterrupt:
            print("\nInterrupted")
            break

        if not cmd_line:
            continue

        parts = cmd_line.split()
        cmd = parts[0].lower()

        try:
            if cmd in ("quit", "exit", "q"):
                if session.pending_moves:
                    print(f"Warning: {len(session.pending_moves)} pending moves will be discarded")
                break

            elif cmd == "query":
                if len(parts) < 4:
                    print("Usage: query <ref> <x> <y> [rotation]")
                    continue
                ref = parts[1]
                x = float(parts[2])
                y = float(parts[3])
                rotation = float(parts[4]) if len(parts) > 4 else None

                result = session.query_move(ref, x, y, rotation)
                if result.success:
                    print(f"Score: {result.score_delta:+.4f}", end="")
                    if result.new_violations:
                        print(f", {len(result.new_violations)} new violation(s)", end="")
                    if result.resolved_violations:
                        print(f", {len(result.resolved_violations)} resolved", end="")
                    print()
                    if result.routing_impact.estimated_length_change_mm != 0:
                        print(
                            f"  Routing: {result.routing_impact.estimated_length_change_mm:+.2f}mm "
                            f"({', '.join(result.routing_impact.affected_nets[:3])}...)"
                        )
                    if result.warnings:
                        for w in result.warnings:
                            print(f"  Warning: {w}")
                else:
                    print(f"Error: {result.error_message}")

            elif cmd == "apply":
                if len(parts) < 4:
                    print("Usage: apply <ref> <x> <y> [rotation]")
                    continue
                ref = parts[1]
                x = float(parts[2])
                y = float(parts[3])
                rotation = float(parts[4]) if len(parts) > 4 else None

                result = session.apply_move(ref, x, y, rotation)
                if result.success:
                    print(f"Applied. Pending: {len(session.pending_moves)} move(s)")
                else:
                    print(f"Error: {result.error_message}")

            elif cmd == "suggest":
                if len(parts) < 2:
                    print("Usage: suggest <ref>")
                    continue
                ref = parts[1]
                suggestions = session.get_suggestions(ref)
                if suggestions:
                    print(f"Suggestions for {ref}:")
                    for i, s in enumerate(suggestions[:5], 1):
                        print(f"  {i}. ({s.x:.2f}, {s.y:.2f}) score: +{s.score:.4f}")
                else:
                    print("No improvements found nearby")

            elif cmd == "undo":
                if session.undo():
                    print(f"Undone. Pending: {len(session.pending_moves)} move(s)")
                else:
                    print("Nothing to undo")

            elif cmd == "status":
                status = session.get_status()
                print(f"Pending moves: {status['pending_moves']}")
                print(f"Current score: {status['current_score']:.4f}")
                print(f"Score change: {status['score_change']:+.4f}")
                print(f"Violations: {status['violations']}")

            elif cmd == "list":
                components = session.list_components()
                for c in components[:20]:  # Limit to first 20
                    fixed_str = " [FIXED]" if c["fixed"] else ""
                    print(
                        f"  {c['ref']:8s}: ({c['x']:7.2f}, {c['y']:7.2f}) @ {c['rotation']:5.1f}°{fixed_str}"
                    )
                if len(components) > 20:
                    print(f"  ... and {len(components) - 20} more")

            elif cmd == "commit":
                if not session.pending_moves:
                    print("No pending moves to commit")
                    continue
                session.commit()
                pcb.save(str(output_path))
                print(f"Committed {len(session.pending_moves)} move(s) to {output_path}")

            elif cmd == "rollback":
                if not session.pending_moves:
                    print("No pending moves to rollback")
                    continue
                count = len(session.pending_moves)
                session.rollback()
                print(f"Rolled back {count} move(s)")

            elif cmd == "help":
                print("Commands:")
                print("  query <ref> <x> <y> [rotation]  - Query move impact")
                print("  apply <ref> <x> <y> [rotation]  - Apply move")
                print("  suggest <ref>                   - Get placement suggestions")
                print("  undo                            - Undo last move")
                print("  status                          - Show session status")
                print("  list                            - List all components")
                print("  commit                          - Save changes to PCB")
                print("  rollback                        - Discard all changes")
                print("  quit                            - Exit (without saving)")

            else:
                print(f"Unknown command: {cmd}. Type 'help' for available commands.")

        except ValueError as e:
            print(f"Invalid input: {e}")
        except Exception as e:
            print(f"Error: {e}")

    return 0


def cmd_align(args) -> int:
    """Align components in row or column."""
    from kicad_tools.cli.progress import spinner
    from kicad_tools.optim import PlacementConfig, PlacementOptimizer, align_components
    from kicad_tools.schema.pcb import PCB

    quiet = getattr(args, "quiet", False)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if not args.components:
        print("Error: No components specified. Use --components R1,R2,R3", file=sys.stderr)
        return 1

    # Load PCB
    try:
        with spinner("Loading PCB...", quiet=quiet):
            pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Create optimizer from PCB
    config = PlacementConfig()
    with spinner("Creating optimizer...", quiet=quiet):
        optimizer = PlacementOptimizer.from_pcb(pcb, config=config)

    # Parse components
    refs = [r.strip() for r in args.components.split(",") if r.strip()]
    if not quiet:
        print(f"Aligning {len(refs)} components: {', '.join(refs)}")

    # Align components
    axis = "horizontal" if args.axis == "row" else "vertical"
    with spinner(f"Aligning {axis}ly...", quiet=quiet):
        count = align_components(
            optimizer,
            refs,
            axis=axis,
            reference=args.reference,
            tolerance_mm=args.tolerance,
        )

    if not quiet:
        print(f"Aligned {count} components")

    if args.dry_run:
        if not quiet:
            print("\n(Dry run - no changes made)")
        return 0

    # Write results
    output_path = Path(args.output) if args.output else pcb_path

    try:
        with spinner("Writing aligned placement...", quiet=quiet):
            updated = optimizer.write_to_pcb(pcb)
            pcb.save(str(output_path))

        if not quiet:
            print(f"\nUpdated {updated} component positions")
            print(f"Saved to: {output_path}")

    except Exception as e:
        print(f"Error saving PCB: {e}", file=sys.stderr)
        return 1

    return 0


def cmd_distribute(args) -> int:
    """Distribute components evenly."""
    from kicad_tools.cli.progress import spinner
    from kicad_tools.optim import PlacementConfig, PlacementOptimizer, distribute_components
    from kicad_tools.schema.pcb import PCB

    quiet = getattr(args, "quiet", False)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if not args.components:
        print(
            "Error: No components specified. Use --components LED1,LED2,LED3,LED4", file=sys.stderr
        )
        return 1

    # Load PCB
    try:
        with spinner("Loading PCB...", quiet=quiet):
            pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Create optimizer from PCB
    config = PlacementConfig()
    with spinner("Creating optimizer...", quiet=quiet):
        optimizer = PlacementOptimizer.from_pcb(pcb, config=config)

    # Parse components
    refs = [r.strip() for r in args.components.split(",") if r.strip()]
    if not quiet:
        print(f"Distributing {len(refs)} components: {', '.join(refs)}")

    # Distribute components
    spacing = args.spacing if args.spacing > 0 else None
    with spinner(f"Distributing {args.axis}...", quiet=quiet):
        count = distribute_components(
            optimizer,
            refs,
            axis=args.axis,
            spacing_mm=spacing,
        )

    if not quiet:
        if spacing:
            print(f"Distributed {count} components with {spacing}mm spacing")
        else:
            print(f"Distributed {count} components evenly")

    if args.dry_run:
        if not quiet:
            print("\n(Dry run - no changes made)")
        return 0

    # Write results
    output_path = Path(args.output) if args.output else pcb_path

    try:
        with spinner("Writing distributed placement...", quiet=quiet):
            updated = optimizer.write_to_pcb(pcb)
            pcb.save(str(output_path))

        if not quiet:
            print(f"\nUpdated {updated} component positions")
            print(f"Saved to: {output_path}")

    except Exception as e:
        print(f"Error saving PCB: {e}", file=sys.stderr)
        return 1

    return 0


def cmd_suggest(args) -> int:
    """Generate placement suggestions with rationale."""
    from kicad_tools.cli.progress import spinner
    from kicad_tools.optim import (
        PlacementOptimizer,
        explain_placement,
        generate_placement_suggestions,
    )
    from kicad_tools.schema.pcb import PCB

    quiet = getattr(args, "quiet", False)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Load PCB
    try:
        with spinner("Loading PCB...", quiet=quiet):
            pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Create optimizer
    try:
        with spinner("Creating optimizer...", quiet=quiet):
            optimizer = PlacementOptimizer.from_pcb(pcb)
    except Exception as e:
        print(f"Error creating optimizer: {e}", file=sys.stderr)
        return 1

    if not quiet:
        print(f"Analyzing {len(optimizer.components)} components...")

    # Generate suggestions
    if args.component:
        # Single component explanation
        with spinner(f"Analyzing {args.component}...", quiet=quiet):
            suggestion = explain_placement(optimizer=optimizer, reference=args.component)

        if not suggestion:
            print(f"Error: Component '{args.component}' not found", file=sys.stderr)
            return 1

        if args.format == "json":
            print(json.dumps(suggestion.to_dict(), indent=2))
        else:
            output_suggestion_text(suggestion, args.verbose)
    else:
        # All components
        with spinner("Generating suggestions...", quiet=quiet):
            suggestions = generate_placement_suggestions(optimizer=optimizer)

        if args.format == "json":
            output = {ref: s.to_dict() for ref, s in suggestions.items()}
            print(json.dumps(output, indent=2))
        else:
            output_suggestions_text(suggestions, args.verbose)

    return 0


def output_suggestion_text(suggestion, verbose: bool = False):
    """Output a single suggestion in text format."""
    print(f"\n{suggestion.reference}:")
    print(f"  Position: ({suggestion.suggested_x:.2f}, {suggestion.suggested_y:.2f})")
    print(f"  Rotation: {suggestion.suggested_rotation:.1f}°")
    print(f"  Confidence: {suggestion.confidence:.0%}")
    print("  Rationale:")
    for reason in suggestion.rationale:
        print(f"    - {reason}")

    if suggestion.constraints_satisfied:
        print("  Constraints Satisfied:")
        for c in suggestion.constraints_satisfied:
            print(f"    ✓ {c}")

    if suggestion.constraints_violated:
        print("  Constraints Violated:")
        for c in suggestion.constraints_violated:
            print(f"    ✗ {c}")

    if verbose and suggestion.alternatives:
        print("  Alternatives:")
        for alt in suggestion.alternatives:
            print(
                f"    - ({alt.x:.2f}, {alt.y:.2f}) @ {alt.rotation:.0f}° "
                f"(score: {alt.score:.2f}): {alt.tradeoff}"
            )


def output_suggestions_text(suggestions: dict, verbose: bool = False):
    """Output all suggestions in text format."""
    print(f"\nPlacement Suggestions ({len(suggestions)} components)")
    print("=" * 60)

    # Sort by confidence (lowest first to highlight issues)
    sorted_suggestions = sorted(suggestions.values(), key=lambda s: s.confidence)

    for suggestion in sorted_suggestions:
        confidence_indicator = (
            "✓" if suggestion.confidence >= 0.8 else "⚠" if suggestion.confidence >= 0.5 else "✗"
        )
        print(
            f"\n{confidence_indicator} {suggestion.reference}: "
            f"({suggestion.suggested_x:.2f}, {suggestion.suggested_y:.2f}) @ {suggestion.suggested_rotation:.0f}° "
            f"[{suggestion.confidence:.0%}]"
        )
        for reason in suggestion.rationale[:3]:  # Limit to 3 reasons in summary
            print(f"    - {reason}")

        if verbose:
            if suggestion.constraints_violated:
                print("    Violations:")
                for c in suggestion.constraints_violated:
                    print(f"      ✗ {c}")

    # Summary
    high_conf = sum(1 for s in suggestions.values() if s.confidence >= 0.8)
    medium_conf = sum(1 for s in suggestions.values() if 0.5 <= s.confidence < 0.8)
    low_conf = sum(1 for s in suggestions.values() if s.confidence < 0.5)

    print(f"\nSummary: {high_conf} high confidence, {medium_conf} medium, {low_conf} low")


def _estimate_routability(pcb_path: Path, quiet: bool = False) -> tuple[float, int, int]:
    """
    Estimate routability of a PCB by analyzing net routing difficulty.

    Returns:
        Tuple of (estimated_success_rate, total_nets, problem_nets_count)
    """
    from kicad_tools.cli.progress import spinner
    from kicad_tools.router.analysis import RoutabilityAnalyzer
    from kicad_tools.router.core import Autorouter
    from kicad_tools.schema.pcb import PCB

    try:
        with spinner("Analyzing routability...", quiet=quiet):
            pcb = PCB.load(str(pcb_path))

            # Detect board dimensions
            width, height = 100.0, 100.0
            if pcb.footprints:
                all_x = [fp.position[0] for fp in pcb.footprints]
                all_y = [fp.position[1] for fp in pcb.footprints]
                width = max(all_x) - min(all_x) + 20
                height = max(all_y) - min(all_y) + 20

            # Create router for analysis
            router = Autorouter(width=width, height=height)

            # Load components into router
            import math

            from kicad_tools.router.layers import Layer

            for fp in pcb.footprints:
                ref = fp.reference
                cx, cy = fp.position
                rotation = fp.rotation
                rot_rad = math.radians(-rotation)
                cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

                pads = []
                for pad in fp.pads:
                    px, py = pad.position
                    rx = px * cos_r - py * sin_r
                    ry = px * sin_r + py * cos_r
                    is_pth = pad.type == "thru_hole"

                    pads.append(
                        {
                            "number": pad.number,
                            "x": cx + rx,
                            "y": cy + ry,
                            "width": pad.size[0],
                            "height": pad.size[1],
                            "net": pad.net_number,
                            "net_name": pad.net_name,
                            "layer": Layer.F_CU,
                            "through_hole": is_pth,
                            "drill": pad.drill if is_pth else 0.0,
                        }
                    )

                if pads:
                    router.add_component(ref, pads)

            # Analyze routability
            analyzer = RoutabilityAnalyzer(router)
            report = analyzer.analyze()

            return (
                report.estimated_success_rate,
                report.total_nets,
                len(report.problem_nets),
            )

    except Exception:
        # If analysis fails, return neutral values
        return (1.0, 0, 0)


def _cmd_optimize_routing_aware(args, pcb_path: Path, quiet: bool) -> int:
    """
    Run routing-aware placement optimization.

    Uses PlaceRouteOptimizer to iterate between placement and routing
    for better overall results.
    """
    from kicad_tools.cli.progress import spinner
    from kicad_tools.optimize.place_route import PlaceRouteOptimizer
    from kicad_tools.schema.pcb import PCB

    if not quiet:
        print("Routing-aware placement optimization")
        print("=" * 50)
        print("This mode iterates between placement and routing")
        print("to find placements that are actually routable.")
        print()

    # Load PCB
    try:
        with spinner("Loading PCB...", quiet=quiet):
            pcb = PCB.load(str(pcb_path))
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    # Create optimizer
    try:
        with spinner("Creating routing-aware optimizer...", quiet=quiet):
            optimizer = PlaceRouteOptimizer.from_pcb(
                pcb,
                pcb_path=pcb_path,
                verbose=not quiet,
            )
    except Exception as e:
        print(f"Error creating optimizer: {e}", file=sys.stderr)
        return 1

    # Run optimization
    max_iterations = getattr(args, "iterations", 10)
    try:
        result = optimizer.optimize(
            max_iterations=max_iterations,
            allow_placement_changes=True,
            skip_drc=False,
        )
    except Exception as e:
        print(f"Error during optimization: {e}", file=sys.stderr)
        return 1

    # Report results
    if not quiet:
        print()
        if result.success:
            print(f"Optimization converged in {result.iterations} iterations")
            if result.routes:
                print(f"Successfully routed {len(result.routes)} nets")
        else:
            print(f"Optimization did not fully converge: {result.message}")

    # Save if not dry run
    if not args.dry_run:
        output_path = Path(args.output) if args.output else pcb_path
        try:
            with spinner("Saving optimized PCB...", quiet=quiet):
                pcb.save(str(output_path))
            if not quiet:
                print(f"Saved to: {output_path}")
        except Exception as e:
            print(f"Error saving PCB: {e}", file=sys.stderr)
            return 1

    return 0 if result.success else 1


def cmd_optimize(args) -> int:
    """Optimize component placement for routability."""
    from kicad_tools.cli.progress import spinner
    from kicad_tools.optim import (
        EvolutionaryPlacementOptimizer,
        PlacementConfig,
        PlacementOptimizer,
        add_keepout_zones,
        detect_keepout_zones,
        load_constraints_from_yaml,
        load_keepout_zones_from_yaml,
    )
    from kicad_tools.optim.evolutionary import EvolutionaryConfig
    from kicad_tools.schema.pcb import PCB

    quiet = getattr(args, "quiet", False)
    routing_aware = getattr(args, "routing_aware", False)
    check_routability = getattr(args, "check_routability", False)

    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    # Handle routing-aware optimization mode
    if routing_aware:
        return _cmd_optimize_routing_aware(args, pcb_path, quiet)

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

    # Load keepout zones
    keepout_zones = []
    if getattr(args, "keepout", None):
        keepout_path = Path(args.keepout)
        if not keepout_path.exists():
            print(f"Error: Keepout file not found: {keepout_path}", file=sys.stderr)
            return 1
        try:
            with spinner("Loading keepout zones...", quiet=quiet):
                keepout_zones = load_keepout_zones_from_yaml(str(keepout_path))
            if not quiet:
                print(f"Loaded {len(keepout_zones)} keepout zones from {keepout_path}")
        except Exception as e:
            print(f"Error loading keepout file: {e}", file=sys.stderr)
            return 1

    # Auto-detect keepout zones if requested
    if getattr(args, "auto_keepout", False):
        try:
            with spinner("Detecting keepout zones...", quiet=quiet):
                auto_zones = detect_keepout_zones(pcb)
            keepout_zones.extend(auto_zones)
            if not quiet:
                print(f"Auto-detected {len(auto_zones)} keepout zones")
        except Exception as e:
            print(f"Warning: Could not auto-detect keepout zones: {e}", file=sys.stderr)

    strategy = args.strategy
    enable_clustering = getattr(args, "cluster", False)
    edge_detect = getattr(args, "edge_detect", False)

    if not quiet:
        print(f"Optimization strategy: {strategy}")
        if fixed_refs:
            print(f"Fixed components: {', '.join(fixed_refs)}")
        if enable_clustering:
            print("Functional clustering: enabled")
        if edge_detect:
            print("Edge detection: enabled")
        if keepout_zones:
            print(f"Keepout zones: {len(keepout_zones)}")

    try:
        if strategy == "force-directed":
            # Physics-based optimization
            config = PlacementConfig(
                grid_size=args.grid if args.grid > 0 else 0.0,
                rotation_grid=90.0,
                thermal_enabled=getattr(args, "thermal", False),
            )

            with spinner("Creating optimizer from PCB...", quiet=quiet):
                optimizer = PlacementOptimizer.from_pcb(
                    pcb,
                    config=config,
                    fixed_refs=fixed_refs,
                    enable_clustering=enable_clustering,
                    edge_detect=edge_detect,
                )

            # Add constraints if loaded
            if constraints:
                optimizer.add_grouping_constraints(constraints)

            # Add keepout zones to optimizer
            if keepout_zones:
                zones_added = add_keepout_zones(optimizer, keepout_zones)
                if not quiet:
                    print(f"  - Added {zones_added} keepout zones")

            if not quiet:
                print(f"Optimizing {len(optimizer.components)} components...")
                print(f"  - {len(optimizer.springs)} net connections")
                if enable_clustering and optimizer.clusters:
                    print(f"  - {len(optimizer.clusters)} functional clusters detected")
                if constraints:
                    print(f"  - {len(constraints)} grouping constraints")
                print(f"  - {len(optimizer.keepouts)} keepout zones")
                print(f"  - Max iterations: {args.iterations}")
                if config.thermal_enabled:
                    heat_sources = optimizer.get_heat_sources()
                    heat_sensitive = optimizer.get_heat_sensitive()
                    print(
                        f"  - Thermal mode: {len(heat_sources)} heat sources, {len(heat_sensitive)} heat-sensitive"
                    )

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

            # Add keepout zones to optimizer
            if keepout_zones:
                zones_added = add_keepout_zones(optimizer, keepout_zones)
                if not quiet:
                    print(f"  - Added {zones_added} keepout zones")

            if not quiet:
                print(f"Optimizing {len(optimizer.components)} components...")
                if enable_clustering and optimizer.clusters:
                    print(f"  - {len(optimizer.clusters)} functional clusters detected")
                print(f"  - Generations: {args.generations}")
                print(f"  - Population: {args.population}")
                print(f"  - Keepout zones: {len(optimizer.keepouts)}")

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

            # Add keepout zones to optimizer
            if keepout_zones:
                zones_added = add_keepout_zones(evo_optimizer, keepout_zones)
                if not quiet:
                    print(f"  - Added {zones_added} keepout zones")

            if not quiet:
                print(f"Optimizing {len(evo_optimizer.components)} components...")
                if enable_clustering and evo_optimizer.clusters:
                    print(f"  - {len(evo_optimizer.clusters)} functional clusters detected")
                print(f"  - Phase 1: Evolutionary ({args.generations} generations)")
                print(f"  - Phase 2: Physics refinement ({args.iterations} iterations)")
                print(f"  - Keepout zones: {len(evo_optimizer.keepouts)}")

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

    # Check routability before saving (if requested)
    before_rate, before_nets, before_problems = 0.0, 0, 0
    if check_routability:
        if not quiet:
            print("\nChecking routability before optimization...")
        before_rate, before_nets, before_problems = _estimate_routability(pcb_path, quiet)
        if not quiet:
            print(
                f"  Before: {before_rate * 100:.0f}% estimated routability ({before_nets} nets, {before_problems} problem nets)"
            )

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

    # Check routability after saving (if requested)
    if check_routability:
        if not quiet:
            print("\nChecking routability after optimization...")
        after_rate, after_nets, after_problems = _estimate_routability(output_path, quiet)
        if not quiet:
            print(
                f"  After: {after_rate * 100:.0f}% estimated routability ({after_nets} nets, {after_problems} problem nets)"
            )

            # Compare and warn if worse
            if after_rate < before_rate - 0.05:  # More than 5% worse
                print()
                print("WARNING: Routability decreased after placement optimization!")
                print(f"  Change: {(after_rate - before_rate) * 100:+.0f}%")
                print("  Consider using --routing-aware mode for better results.")
            elif after_rate > before_rate + 0.05:  # More than 5% better
                print(f"\n  Improvement: {(after_rate - before_rate) * 100:+.0f}%")

    # Print routability warning (always, unless quiet)
    if not quiet and not check_routability:
        print()
        print("NOTE: Placement optimization does not verify routing.")
        print("      Run `route` after optimization to verify routability.")
        print("      Use --routing-aware for integrated place-route optimization.")
        print("      Use --check-routability to see routability impact.")

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


def output_si_analysis(
    classifications: dict,
    hints: list,
    score: float,
    verbose: bool = False,
):
    """Output signal integrity analysis results."""
    from kicad_tools.optim.signal_integrity import SignalClass

    print("\n" + "=" * 60)
    print("Signal Integrity Analysis")
    print("=" * 60)

    # Summary of net classifications
    class_counts: dict[str, int] = {}
    for classification in classifications.values():
        class_name = classification.signal_class.value
        class_counts[class_name] = class_counts.get(class_name, 0) + 1

    print("\nNet Classification Summary:")
    print("-" * 40)
    for signal_class in SignalClass:
        count = class_counts.get(signal_class.value, 0)
        if count > 0:
            print(f"  {signal_class.value:<20} {count:>4} nets")

    print(f"\n  Total: {len(classifications)} nets classified")

    # SI Score
    print(f"\nSignal Integrity Score: {score:.1f}/100")

    if score >= 80:
        print("  ✅ Good - placement supports signal integrity")
    elif score >= 60:
        print("  ⚠️  Fair - some improvements recommended")
    else:
        print("  ❌ Poor - significant improvements needed")

    # Hints
    if hints:
        print(f"\nPlacement Hints ({len(hints)} issues found):")
        print("-" * 60)

        for hint in hints:
            severity_icon = {"critical": "🔴", "warning": "🟡", "info": "🔵"}.get(
                hint.severity, "⚪"
            )
            print(f"\n{severity_icon} [{hint.hint_type}] {hint.description}")
            print(f"   Components: {', '.join(hint.affected_components[:5])}")
            print(f"   → {hint.suggestion}")
            if hint.estimated_improvement and verbose:
                print(f"   Potential improvement: {hint.estimated_improvement:.1f}mm")
    else:
        print("\n✅ No signal integrity issues detected!")

    print("")


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
    check_parser.add_argument(
        "--signal-integrity",
        action="store_true",
        help="Analyze signal integrity and show placement hints",
    )

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

    # Snap subcommand
    snap_parser = subparsers.add_parser("snap", help="Snap components to grid")
    snap_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    snap_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: modify in place)",
    )
    snap_parser.add_argument(
        "--grid",
        type=float,
        default=0.5,
        help="Grid size in mm (default: 0.5)",
    )
    snap_parser.add_argument(
        "--rotation",
        type=int,
        default=90,
        help="Rotation snap in degrees (0 to disable, default: 90)",
    )
    snap_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without saving",
    )
    snap_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    snap_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")

    # Align subcommand
    align_parser = subparsers.add_parser("align", help="Align components in row or column")
    align_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    align_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: modify in place)",
    )
    align_parser.add_argument(
        "--components",
        "-c",
        required=True,
        help="Comma-separated component refs to align (e.g., R1,R2,R3)",
    )
    align_parser.add_argument(
        "--axis",
        choices=["row", "column"],
        default="row",
        help="Alignment axis: row (horizontal) or column (vertical) (default: row)",
    )
    align_parser.add_argument(
        "--reference",
        choices=["center", "top", "bottom", "left", "right"],
        default="center",
        help="Alignment reference point (default: center)",
    )
    align_parser.add_argument(
        "--tolerance",
        type=float,
        default=0.1,
        help="Tolerance for already-aligned components in mm (default: 0.1)",
    )
    align_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without saving",
    )
    align_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    align_parser.add_argument("-q", "--quiet", action="store_true", help="Suppress progress output")

    # Distribute subcommand
    distribute_parser = subparsers.add_parser("distribute", help="Distribute components evenly")
    distribute_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    distribute_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: modify in place)",
    )
    distribute_parser.add_argument(
        "--components",
        "-c",
        required=True,
        help="Comma-separated component refs to distribute (e.g., LED1,LED2,LED3,LED4)",
    )
    distribute_parser.add_argument(
        "--axis",
        choices=["horizontal", "vertical"],
        default="horizontal",
        help="Distribution axis (default: horizontal)",
    )
    distribute_parser.add_argument(
        "--spacing",
        type=float,
        default=0.0,
        help="Fixed spacing in mm (0 for automatic even distribution, default: 0)",
    )
    distribute_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview without saving",
    )
    distribute_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    distribute_parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

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
        "--edge-detect",
        action="store_true",
        help="Auto-detect edge components (connectors, mounting holes, etc.)",
    )
    optimize_parser.add_argument(
        "--thermal",
        action="store_true",
        help="Enable thermal-aware placement (keeps heat sources away from sensitive components)",
    )
    optimize_parser.add_argument(
        "--keepout",
        metavar="FILE",
        help="YAML file defining keepout zones",
    )
    optimize_parser.add_argument(
        "--auto-keepout",
        action="store_true",
        dest="auto_keepout",
        help="Auto-detect keepout zones from mounting holes and connectors",
    )
    optimize_parser.add_argument(
        "--routing-aware",
        action="store_true",
        dest="routing_aware",
        help="Use integrated place-route optimization (iterates between placement and routing)",
    )
    optimize_parser.add_argument(
        "--check-routability",
        action="store_true",
        dest="check_routability",
        help="Check routability before and after optimization to show impact",
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

    # Suggest subcommand
    suggest_parser = subparsers.add_parser(
        "suggest", help="Generate placement suggestions with rationale"
    )
    suggest_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    suggest_parser.add_argument(
        "--component",
        "-c",
        help="Explain placement for specific component reference",
    )
    suggest_parser.add_argument(
        "--format",
        choices=["text", "json"],
        default="text",
        help="Output format (default: text)",
    )
    suggest_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    suggest_parser.add_argument(
        "-q", "--quiet", action="store_true", help="Suppress progress output"
    )

    # Refine subcommand (interactive placement refinement)
    refine_parser = subparsers.add_parser("refine", help="Interactive placement refinement session")
    refine_parser.add_argument("pcb", help="Path to .kicad_pcb file")
    refine_parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: modify in place)",
    )
    refine_parser.add_argument(
        "--fixed",
        help="Comma-separated component refs to keep fixed (e.g., J1,J2,H1)",
    )
    refine_parser.add_argument(
        "--json",
        action="store_true",
        help="JSON API mode (read commands from stdin, write responses to stdout)",
    )
    refine_parser.add_argument("-v", "--verbose", action="store_true", help="Verbose output")
    refine_parser.add_argument(
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
    elif args.command == "snap":
        return cmd_snap(args)
    elif args.command == "align":
        return cmd_align(args)
    elif args.command == "distribute":
        return cmd_distribute(args)
    elif args.command == "optimize":
        return cmd_optimize(args)
    elif args.command == "suggest":
        return cmd_suggest(args)
    elif args.command == "refine":
        return cmd_refine(args)

    return 0


if __name__ == "__main__":
    sys.exit(main())
