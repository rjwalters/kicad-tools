"""
LLM-driven PCB reasoning CLI command.

Provides command-line access to the reasoning module for LLM-assisted layout:

    kct reason board.kicad_pcb --export-state
    kct reason board.kicad_pcb --interactive
    kct reason board.kicad_pcb --analyze
"""

import argparse
import json
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> int:
    """Main entry point for reason command."""
    parser = argparse.ArgumentParser(
        prog="kicad-tools reason",
        description="LLM-driven PCB layout reasoning",
    )
    parser.add_argument("pcb", help="Path to .kicad_pcb file")
    parser.add_argument(
        "-o",
        "--output",
        help="Output file path (default: <input>_reasoned.kicad_pcb)",
    )
    parser.add_argument(
        "--export-state",
        action="store_true",
        help="Export current state as JSON for external LLM processing",
    )
    parser.add_argument(
        "--state-output",
        help="Output path for state JSON (default: stdout)",
    )
    parser.add_argument(
        "--interactive",
        action="store_true",
        help="Run interactive reasoning loop (stdin/stdout for LLM)",
    )
    parser.add_argument(
        "--analyze",
        action="store_true",
        help="Print detailed analysis of current PCB state",
    )
    parser.add_argument(
        "--auto-route",
        action="store_true",
        help="Auto-route priority nets without LLM (convenience mode)",
    )
    parser.add_argument(
        "--max-nets",
        type=int,
        default=10,
        help="Maximum nets to auto-route (default: 10)",
    )
    parser.add_argument(
        "--drc",
        help="Path to DRC report file for violation awareness",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Verbose output",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Show what would be done without writing output",
    )

    args = parser.parse_args(argv)

    # Validate input
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}", file=sys.stderr)
        return 1

    if pcb_path.suffix != ".kicad_pcb":
        print(f"Warning: Expected .kicad_pcb file, got {pcb_path.suffix}")

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_reasoned")

    # Import reasoning module
    from kicad_tools.reasoning import PCBReasoningAgent

    # Print header
    print("=" * 60)
    print("KiCad LLM-Driven PCB Reasoning")
    print("=" * 60)
    print(f"Input: {pcb_path}")

    # Create agent
    print("\n--- Loading PCB ---")
    try:
        agent = PCBReasoningAgent.from_pcb(
            str(pcb_path),
            drc_path=args.drc,
        )
    except Exception as e:
        print(f"Error loading PCB: {e}", file=sys.stderr)
        return 1

    state = agent.get_state()
    print(f"  Board size: {state.outline.width:.1f}mm x {state.outline.height:.1f}mm")
    print(f"  Components: {len(state.components)}")
    print(f"  Nets total: {len(state.routed_nets) + len(state.unrouted_nets)}")
    print(f"  Nets routed: {len(state.routed_nets)}")
    print(f"  Nets unrouted: {len(state.unrouted_nets)}")
    print(f"  Violations: {len(state.violations)}")

    # Handle different modes
    if args.export_state:
        return _export_state(agent, args)

    if args.analyze:
        return _analyze(agent, args)

    if args.interactive:
        return _interactive_loop(agent, output_path, args)

    if args.auto_route:
        return _auto_route(agent, output_path, args)

    # Default: show prompt and exit
    print("\n--- Current State Prompt ---")
    print(agent.get_prompt())
    print("\n" + "=" * 60)
    print("Use --export-state, --interactive, --analyze, or --auto-route")
    return 0


def _export_state(agent, args) -> int:
    """Export state as JSON for external LLM processing."""
    state = agent.get_state()

    # Build state dictionary
    state_dict = {
        "pcb_file": str(agent.pcb_path),
        "outline": {
            "width": state.outline.width,
            "height": state.outline.height,
        },
        "components": {
            ref: {
                "x": comp.x,
                "y": comp.y,
                "rotation": comp.rotation,
                "layer": comp.layer,
                "footprint": comp.footprint,
                "pads": [{"name": p.name, "x": p.x, "y": p.y, "net": p.net} for p in comp.pads],
            }
            for ref, comp in state.components.items()
        },
        "nets": {
            "routed": [{"name": n.name, "pad_count": n.pad_count} for n in state.routed_nets],
            "unrouted": [
                {"name": n.name, "pad_count": n.pad_count, "priority": n.priority}
                for n in state.unrouted_nets
            ],
        },
        "violations": [
            {
                "type": v.type,
                "severity": v.severity,
                "message": v.message,
                "x": v.x,
                "y": v.y,
                "nets": v.nets,
            }
            for v in state.violations
        ],
        "prompt": agent.get_prompt(),
    }

    # Output
    json_str = json.dumps(state_dict, indent=2)

    if args.state_output:
        Path(args.state_output).write_text(json_str)
        print(f"\n--- State exported to {args.state_output} ---")
    else:
        print("\n--- State JSON ---")
        print(json_str)

    return 0


def _analyze(agent, args) -> int:
    """Print detailed analysis of PCB state."""
    print("\n" + agent.analyze_current_state())
    return 0


def _interactive_loop(agent, output_path: Path, args) -> int:
    """Run interactive reasoning loop."""
    print("\n--- Interactive Mode ---")
    print("Enter commands as JSON. Type 'quit' to exit, 'save' to save.")
    print('Example: {"command": "route_net", "net": "SCL"}')
    print()

    while not agent.is_complete():
        # Show current state
        print(agent.get_prompt())
        print("\nCommand> ", end="", flush=True)

        try:
            line = input().strip()
        except EOFError:
            break

        if not line:
            continue

        if line.lower() == "quit":
            break

        if line.lower() == "save":
            if not args.dry_run:
                agent.save(str(output_path))
                print(f"Saved to {output_path}")
            else:
                print("Dry run - not saving")
            continue

        if line.lower() == "status":
            progress = agent.get_progress()
            print(progress.to_prompt())
            continue

        # Parse and execute command
        try:
            command_dict = json.loads(line)
            result, diagnosis = agent.execute_dict(command_dict)

            if result.success:
                print(f"✓ {result.message}")
            else:
                print(f"✗ {result.message}")
                if diagnosis:
                    print(f"  Diagnosis: {diagnosis[:200]}...")

        except json.JSONDecodeError as e:
            print(f"Invalid JSON: {e}")
        except Exception as e:
            print(f"Error: {e}")

        print()

    # Final save
    if not args.dry_run and not agent.is_complete():
        print("\n--- Session ended ---")
        save = input("Save current state? (y/n) ").strip().lower()
        if save == "y":
            agent.save(str(output_path))
            print(f"Saved to {output_path}")

    return 0


def _auto_route(agent, output_path: Path, args) -> int:
    """Auto-route priority nets without LLM."""
    print(f"\n--- Auto-routing up to {args.max_nets} priority nets ---")

    results = agent.route_priority_nets(max_nets=args.max_nets)

    successful = sum(1 for r in results if r.success)
    print(f"\nRouted {successful}/{len(results)} nets")

    # Show final progress
    progress = agent.get_progress()
    print(progress.to_prompt())

    # Save
    if args.dry_run:
        print("\n--- Dry run - not saving ---")
    else:
        print(f"\n--- Saving to {output_path} ---")
        agent.save(str(output_path))
        print(f"Saved to {output_path}")

    return 0 if successful == len(results) else 1


if __name__ == "__main__":
    sys.exit(main())
