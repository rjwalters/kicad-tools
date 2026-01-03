#!/usr/bin/env python3
"""
Agent Refinement Session Demo

Demonstrates the interactive PlacementSession API designed for AI agents
to explore placement changes with query-before-commit semantics.
"""

from pathlib import Path

from kicad_tools.optim import PlacementSession
from kicad_tools.schema.pcb import PCB


def main():
    """Run the agent session demo."""
    # Load the example PCB
    pcb_path = Path(__file__).parent / "fixtures" / "mcu_board.kicad_pcb"
    print(f"Loading PCB: {pcb_path.name}")
    pcb = PCB.load(str(pcb_path))

    # Create placement session
    print("Creating placement session...")
    session = PlacementSession(pcb)

    # Show initial status
    status = session.get_status()
    print()
    print("=" * 60)
    print("Session Status")
    print("=" * 60)
    print()
    print(f"Components: {status['components']}")
    print(f"Initial score: {status['initial_score']:.4f}")
    print(f"Violations: {status['violations']}")
    print()

    # List some components
    print("=" * 60)
    print("Component Positions")
    print("=" * 60)
    print()
    components = session.list_components()
    print(f"{'Reference':<10} {'X':>8} {'Y':>8} {'Rotation':>8} {'Fixed'}")
    print("-" * 50)
    for c in components[:10]:  # Show first 10
        fixed_str = "Yes" if c["fixed"] else "No"
        print(f"{c['ref']:<10} {c['x']:>8.2f} {c['y']:>8.2f} {c['rotation']:>8.1f} {fixed_str}")
    if len(components) > 10:
        print(f"... and {len(components) - 10} more")
    print()

    # Demonstrate query_move: evaluate a hypothetical move without applying
    print("=" * 60)
    print("Query Move (What-If Analysis)")
    print("=" * 60)
    print()

    # Try moving C1 (bypass capacitor) closer to U1 (MCU)
    target_ref = "C1"
    target_pos = session.get_component_position(target_ref)
    if target_pos:
        print(f"Current position of {target_ref}:")
        print(f"  ({target_pos['x']}, {target_pos['y']}) @ {target_pos['rotation']}deg")
        print()

        # Query what would happen if we moved it
        new_x = target_pos["x"] - 5.0  # Move 5mm closer to MCU
        new_y = target_pos["y"]
        print(f"Querying move to ({new_x}, {new_y})...")
        result = session.query_move(target_ref, new_x, new_y)

        print()
        if result.success:
            print("Move evaluation:")
            print(f"  Score delta: {result.score_delta:+.4f} (negative = improvement)")
            print(
                f"  Routing length change: {result.routing_impact.estimated_length_change_mm:+.2f}mm"
            )
            print(
                f"  Affected nets: {', '.join(result.routing_impact.affected_nets[:3]) or 'none'}"
            )
            print(f"  New violations: {len(result.new_violations)}")
            print(f"  Resolved violations: {len(result.resolved_violations)}")
            if result.warnings:
                print("  Warnings:")
                for w in result.warnings:
                    print(f"    - {w}")
        else:
            print(f"Move not valid: {result.error_message}")
    print()

    # Demonstrate get_suggestions: get AI-friendly suggestions
    print("=" * 60)
    print("Get Suggestions")
    print("=" * 60)
    print()

    suggestions = session.get_suggestions(target_ref, num_suggestions=5, search_radius=15.0)

    if suggestions:
        print(f"Suggested positions for {target_ref}:")
        print()
        for i, s in enumerate(suggestions, 1):
            print(f"  {i}. ({s.x:.2f}, {s.y:.2f}) @ {s.rotation:.0f}deg")
            print(f"     Score improvement: {s.score:.4f}")
            print(f"     {s.rationale}")
            print()
    else:
        print(f"No improvement suggestions found for {target_ref} in search radius")
    print()

    # Demonstrate apply_move and undo
    print("=" * 60)
    print("Apply Move and Undo")
    print("=" * 60)
    print()

    if suggestions:
        best = suggestions[0]
        print(f"Applying best suggestion: ({best.x:.2f}, {best.y:.2f})")
        result = session.apply_move(target_ref, best.x, best.y)

        if result.success:
            print(f"Move applied! Pending moves: {len(session.pending_moves)}")
            status = session.get_status()
            print(
                f"Current score: {status['current_score']:.4f} (change: {status['score_change']:+.4f})"
            )

            # Undo
            print()
            print("Undoing move...")
            session.undo()
            status = session.get_status()
            print(
                f"After undo: score={status['current_score']:.4f}, pending={status['pending_moves']}"
            )
    print()

    # Show JSON output format for agent integration
    print("=" * 60)
    print("JSON Output Format (for agent integration)")
    print("=" * 60)
    print()

    # Query a move and show JSON output
    result = session.query_move("C2", 148.0, 130.0)
    print("Query result as JSON:")
    print(result.to_json())
    print()

    # Summary
    print("=" * 60)
    print("Session API Summary")
    print("=" * 60)
    print()
    print("Key methods for AI agent integration:")
    print()
    print("  session = PlacementSession(pcb)")
    print("  ")
    print("  # Query (what-if analysis)")
    print("  result = session.query_move(ref, x, y, rotation)")
    print("  ")
    print("  # Apply if good")
    print("  if result.score_delta < 0:")
    print("      session.apply_move(ref, x, y, rotation)")
    print("  ")
    print("  # Get suggestions")
    print("  suggestions = session.get_suggestions(ref)")
    print("  ")
    print("  # Commit or rollback")
    print("  session.commit()   # Apply to PCB")
    print("  session.rollback() # Discard changes")
    print()
    print("CLI JSON mode for agent integration:")
    print("  kicad-tools placement refine board.kicad_pcb --json")
    print("  # Reads JSON commands from stdin, writes JSON responses to stdout")


if __name__ == "__main__":
    main()
