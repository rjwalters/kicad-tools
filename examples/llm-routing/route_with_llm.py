#!/usr/bin/env python3
"""
Example: LLM-Driven PCB Routing with kicad-tools

This example demonstrates how to integrate an LLM (like GPT-4 or Claude) with
kicad-tools to perform semantic PCB routing decisions.

The key insight: Traditional autorouters are semantically blind - they connect
pads without understanding design intent. An LLM can reason about WHY decisions
matter ("keep clocks short", "isolate analog section").

Usage:
    # Set your LLM API key
    export OPENAI_API_KEY="your-key-here"

    # Run the example
    python route_with_llm.py board.kicad_pcb

This script demonstrates the integration pattern. For production use, you'd
adapt the call_llm() function to your preferred LLM provider.
"""

import json
import os
import sys
from pathlib import Path


def call_llm(prompt: str) -> dict:
    """Call your LLM to get routing decisions.

    This is a placeholder that demonstrates the integration pattern.
    Replace with your actual LLM API call (OpenAI, Anthropic, local, etc.)

    The LLM should return a JSON command like:
    {
        "command": "route_net",
        "net": "SCL",
        "minimize_vias": true,
        "avoid_regions": ["analog_corner"]
    }

    Or:
    {
        "command": "place_component",
        "ref": "U2",
        "at": [50.0, 30.0],
        "rotation": 90
    }
    """
    # Check for OpenAI
    api_key = os.environ.get("OPENAI_API_KEY")
    if api_key:
        try:
            import openai

            client = openai.OpenAI(api_key=api_key)
            response = client.chat.completions.create(
                model="gpt-4",
                messages=[
                    {
                        "role": "system",
                        "content": """You are a PCB layout assistant. Analyze the board state
and suggest the next routing action as JSON. Available commands:

- route_net: Route a net
  {"command": "route_net", "net": "NET_NAME", "minimize_vias": true}

- delete_trace: Remove traces (e.g., to fix shorts)
  {"command": "delete_trace", "net": "NET_NAME", "reason": "fixing short"}

- place_component: Move a component
  {"command": "place_component", "ref": "U1", "at": [x, y], "rotation": 0}

Respond with ONLY valid JSON, no explanation.""",
                    },
                    {"role": "user", "content": prompt},
                ],
                max_tokens=200,
            )
            result_text = response.choices[0].message.content.strip()
            return json.loads(result_text)
        except ImportError:
            print("OpenAI library not installed. Install with: pip install openai")
        except Exception as e:
            print(f"LLM call failed: {e}")

    # Fallback: Simple heuristic-based routing
    # This demonstrates the command format without requiring an actual LLM
    print("  (Using fallback heuristic - no LLM API configured)")
    return {"command": "route_net", "net": "auto", "minimize_vias": True}


def main():
    """Main example demonstrating LLM-driven PCB routing."""
    if len(sys.argv) < 2:
        print("Usage: python route_with_llm.py <board.kicad_pcb>")
        print("\nThis example demonstrates LLM integration for PCB layout.")
        print("Set OPENAI_API_KEY environment variable for actual LLM calls.")
        sys.exit(1)

    pcb_path = Path(sys.argv[1])
    if not pcb_path.exists():
        print(f"Error: File not found: {pcb_path}")
        sys.exit(1)

    # Import reasoning module
    from kicad_tools.reasoning import PCBReasoningAgent

    print("=" * 60)
    print("LLM-Driven PCB Routing Example")
    print("=" * 60)

    # Create reasoning agent
    print(f"\nLoading: {pcb_path}")
    agent = PCBReasoningAgent.from_pcb(str(pcb_path))

    # Show initial state
    state = agent.get_state()
    print(f"Board: {state.outline.width:.1f}mm x {state.outline.height:.1f}mm")
    print(f"Components: {len(state.components)}")
    print(f"Unrouted nets: {len(state.unrouted_nets)}")
    print(f"Violations: {len(state.violations)}")

    # Reasoning loop
    print("\n--- Starting LLM Reasoning Loop ---")
    max_iterations = 20
    iteration = 0

    while not agent.is_complete() and iteration < max_iterations:
        iteration += 1
        print(f"\n[Iteration {iteration}]")

        # Get current state as prompt for LLM
        prompt = agent.get_prompt()

        # Show condensed state
        progress = agent.get_progress()
        print(f"  Progress: {progress.nets_routed}/{progress.nets_total} nets")
        print(f"  Violations: {progress.violations_current}")

        # Get LLM decision
        print("  Calling LLM...")
        try:
            command_dict = call_llm(prompt)
            print(f"  LLM suggests: {command_dict}")
        except Exception as e:
            print(f"  LLM error: {e}")
            break

        # Handle special "auto" net - route next priority net
        if command_dict.get("net") == "auto" and state.unrouted_nets:
            command_dict["net"] = sorted(state.unrouted_nets, key=lambda n: n.priority)[0].name
            print(f"  Auto-selected net: {command_dict['net']}")

        # Execute command
        result, diagnosis = agent.execute_dict(command_dict)

        if result.success:
            print(f"  Result: SUCCESS - {result.message}")
        else:
            print(f"  Result: FAILED - {result.message}")
            if diagnosis:
                # Show first 200 chars of diagnosis
                print(f"  Diagnosis: {diagnosis[:200]}...")

    # Final status
    print("\n" + "=" * 60)
    if agent.is_complete():
        print("SUCCESS: All nets routed!")
    else:
        progress = agent.get_progress()
        print(f"INCOMPLETE: {progress.nets_routed}/{progress.nets_total} nets routed")
        print(f"Violations remaining: {progress.violations_current}")

    # Save result
    output_path = pcb_path.with_stem(pcb_path.stem + "_llm_routed")
    print(f"\nSaving to: {output_path}")
    agent.save(str(output_path))

    # Export history for analysis
    history_path = pcb_path.with_stem(pcb_path.stem + "_routing_history").with_suffix(".json")
    agent.export_history(str(history_path))
    print(f"History exported to: {history_path}")


if __name__ == "__main__":
    main()
