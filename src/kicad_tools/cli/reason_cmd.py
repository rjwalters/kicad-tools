"""
LLM-driven PCB reasoning CLI command.

Provides command-line access to the reasoning module for LLM-assisted layout:

    kct reason board.kicad_pcb --export-state
    kct reason board.kicad_pcb --interactive
    kct reason board.kicad_pcb --analyze

Machine output (``--format json``, issue #4674): one document carrying the
board summary, the DRC block and the selected ``mode``'s own payload
(``state`` / ``analysis`` / ``auto_route`` / ``prompt``), or
``{"error": ..., "success": false}`` on failure with the exit code unchanged.
``--interactive`` is a stdin/stdout dialogue with no single-document form, so
combining it with ``--format json`` is refused structurally rather than
half-emitted.  See ``docs/reference/machine-output.md``.
"""

import argparse
import contextlib
import io
import json
import sys
from pathlib import Path

from kicad_tools.cli.format_options import FORMAT_JSON, add_format_flag, emit_json
from kicad_tools.manufacturers import get_manufacturer_ids


@contextlib.contextmanager
def _stdout_to_stderr_when(active: bool):
    """Divert stdout to stderr while *active* (i.e. JSON mode owns stdout).

    ``--auto-route`` drives the router, which prints a multi-line progress log
    on **stdout** that this module does not own; under ``--format json`` that
    would corrupt the single-document contract, so it is captured and replayed
    on stderr.  Same helper as ``report_cmd._stdout_to_stderr_when`` (batch 5
    of #4674).
    """
    if not active:
        yield
        return
    buffer = io.StringIO()
    try:
        with contextlib.redirect_stdout(buffer):
            yield
    finally:
        if buffer.getvalue():
            print(buffer.getvalue(), end="", file=sys.stderr)


def _fail(
    as_json: bool,
    pcb: str,
    message: str,
    *,
    text: str | None = None,
    code: int = 1,
) -> int:
    """Report a reason failure as a document (JSON) or prose (text).

    ``text`` overrides the conventional ``Error: <message>`` prose for the
    paths that word themselves differently, so text mode stays byte-identical.
    """
    if as_json:
        emit_json(
            {
                "command": "reason",
                "pcb": pcb,
                "error": message,
                "success": False,
            }
        )
    else:
        print(text if text is not None else f"Error: {message}", file=sys.stderr)
    return code


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
        "--mfr",
        "-m",
        choices=get_manufacturer_ids(),
        default="jlcpcb",
        help="Target manufacturer for DRC rules (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=2,
        help="Number of copper layers for DRC (default: 2)",
    )
    parser.add_argument(
        "--no-drc",
        action="store_true",
        help="Skip automatic DRC checks (violations will be 0)",
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
    add_format_flag(parser)

    args = parser.parse_args(argv)
    as_json = args.format == FORMAT_JSON

    # Validate input
    pcb_path = Path(args.pcb)
    if not pcb_path.exists():
        return _fail(as_json, str(pcb_path), f"File not found: {pcb_path}")

    warnings: list[str] = []
    if pcb_path.suffix != ".kicad_pcb":
        message = f"Expected .kicad_pcb file, got {pcb_path.suffix}"
        warnings.append(message)
        if not as_json:
            print(f"Warning: {message}")

    # Determine output path
    if args.output:
        output_path = Path(args.output)
    else:
        output_path = pcb_path.with_stem(pcb_path.stem + "_reasoned")

    # --interactive is a stdin/stdout dialogue: there is no single document to
    # emit, so refuse the combination rather than printing a half-truth.
    if as_json and args.interactive:
        return _fail(
            as_json,
            str(pcb_path),
            "--interactive is a stdin/stdout dialogue and has no single-document "
            "form; use --export-state, --analyze or --auto-route with --format json",
            code=2,
        )

    # Import reasoning module
    from kicad_tools.reasoning import PCBReasoningAgent

    # Print header
    if not as_json:
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
        return _fail(
            as_json,
            str(pcb_path),
            f"loading PCB: {e}",
            text=f"Error loading PCB: {e}",
        )

    # Run automatic DRC checks if no external DRC report provided
    drc_block: dict = {"ran": False, "source": "report" if args.drc else "skipped"}
    if not args.drc and not args.no_drc:
        if not as_json:
            print("\n--- Running DRC Checks ---")
        try:
            from kicad_tools.schema.pcb import PCB
            from kicad_tools.validate import DRCChecker

            pcb = PCB.load(pcb_path)
            checker = DRCChecker(pcb, manufacturer=args.mfr, layers=args.layers)
            drc_results = checker.check_all()
            agent.update_violations_from_checker(drc_results)
            drc_block = {
                "ran": True,
                "source": "checker",
                "manufacturer": args.mfr,
                "layers": args.layers,
                "rules_checked": drc_results.rules_checked,
            }
            if not as_json:
                print(f"  Manufacturer: {args.mfr.upper()}")
                print(f"  Rules checked: {drc_results.rules_checked}")
        except Exception as e:
            drc_block = {"ran": False, "source": "checker", "error": str(e)}
            print(f"  Warning: DRC check failed: {e}", file=sys.stderr)
            if not as_json:
                print("  Use --no-drc to skip DRC checks")

    state = agent.get_state()
    if not as_json:
        print("\n--- Board Summary ---")
        print(f"  Board size: {state.outline.width:.1f}mm x {state.outline.height:.1f}mm")
        print(f"  Components: {len(state.components)}")
        print(f"  Nets total: {len(state.routed_nets) + len(state.unrouted_nets)}")
        print(f"  Nets routed: {len(state.routed_nets)}")
        print(f"  Nets unrouted: {len(state.unrouted_nets)}")
        print(f"  Violations: {len(state.violations)}")

    envelope = {
        "command": "reason",
        "pcb": str(pcb_path),
        "output": str(output_path),
        "dry_run": bool(args.dry_run),
        "warnings": warnings,
        "drc": drc_block,
        "board": {
            "width_mm": state.outline.width,
            "height_mm": state.outline.height,
            "components": len(state.components),
            "nets_total": len(state.routed_nets) + len(state.unrouted_nets),
            "nets_routed": len(state.routed_nets),
            "nets_unrouted": len(state.unrouted_nets),
            "violations": len(state.violations),
        },
    }

    # Handle different modes
    if args.export_state:
        rc, payload = _export_state(agent, args, as_json=as_json)
    elif args.analyze:
        rc, payload = _analyze(agent, args, as_json=as_json)
    elif args.interactive:
        # Unreachable under --format json (refused above).
        return _interactive_loop(agent, output_path, args)
    elif args.auto_route:
        rc, payload = _auto_route(agent, output_path, args, as_json=as_json)
    else:
        # Default: show the prompt and exit
        payload = {"mode": "prompt", "prompt": agent.get_prompt()}
        rc = 0
        if not as_json:
            print("\n--- Current State Prompt ---")
            print(agent.get_prompt())
            print("\n" + "=" * 60)
            print("Use --export-state, --interactive, --analyze, or --auto-route")

    if as_json:
        emit_json({**envelope, **payload, "success": rc == 0})
    return rc


def _export_state(agent, args, *, as_json: bool = False) -> tuple[int, dict]:
    """Export state as JSON for external LLM processing.

    Returns ``(exit_code, payload)``; the payload is merged into the
    ``--format json`` envelope by :func:`main` (issue #4674).
    """
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
        if not as_json:
            print(f"\n--- State exported to {args.state_output} ---")
    elif not as_json:
        print("\n--- State JSON ---")
        print(json_str)

    # The exported state is the artifact; the envelope carries it under
    # ``state`` (and names the file it was written to) rather than making the
    # caller parse a second document out of the stream.
    return 0, {
        "mode": "export-state",
        "state": state_dict,
        "state_output": args.state_output,
    }


def _analyze(agent, args, *, as_json: bool = False) -> tuple[int, dict]:
    """Print detailed analysis of PCB state (prose), or return it as data."""
    analysis = agent.analyze_current_state()
    if not as_json:
        print("\n" + analysis)
    return 0, {"mode": "analyze", "analysis": analysis}


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


def _auto_route(agent, output_path: Path, args, *, as_json: bool = False) -> tuple[int, dict]:
    """Auto-route priority nets without LLM.

    Returns ``(exit_code, payload)``; the payload names every attempted net and
    whether the result was written, so a machine caller never has to infer
    "did it save?" from the exit code alone (issue #4674).
    """
    if not as_json:
        print(f"\n--- Auto-routing up to {args.max_nets} priority nets ---")

    # ``route_priority_nets`` walks the unrouted nets in priority order and
    # returns one result per net in that same order, but ``CommandResult``
    # itself carries no net name -- so capture the target order up front to
    # label the per-net entries (missing labels degrade to ``null`` rather
    # than mis-attributing a result).
    targets = [
        net.name for net in sorted(agent.get_state().unrouted_nets, key=lambda n: n.priority)
    ][: args.max_nets]

    with _stdout_to_stderr_when(as_json):
        results = agent.route_priority_nets(max_nets=args.max_nets)

    successful = sum(1 for r in results if r.success)
    if not as_json:
        print(f"\nRouted {successful}/{len(results)} nets")

        # Show final progress
        progress = agent.get_progress()
        print(progress.to_prompt())

    # Save
    if args.dry_run:
        if not as_json:
            print("\n--- Dry run - not saving ---")
    else:
        if not as_json:
            print(f"\n--- Saving to {output_path} ---")
        agent.save(str(output_path))
        if not as_json:
            print(f"Saved to {output_path}")

    payload = {
        "mode": "auto-route",
        "auto_route": {
            "max_nets": args.max_nets,
            "attempted": len(results),
            "routed": successful,
            "nets": [
                {
                    "net": targets[i] if i < len(targets) else None,
                    "success": bool(r.success),
                    "message": r.message,
                    "vias_added": getattr(r, "vias_added", 0),
                    "trace_length_mm": getattr(r, "trace_length", 0.0),
                }
                for i, r in enumerate(results)
            ],
        },
        "saved": not args.dry_run,
        "written_to": None if args.dry_run else str(output_path),
    }
    return (0 if successful == len(results) else 1), payload


if __name__ == "__main__":
    sys.exit(main())
