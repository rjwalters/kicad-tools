"""Spec command handlers for .kct project specification format."""

from __future__ import annotations

import sys
from pathlib import Path

from ..format_options import emit_json

__all__ = ["run_spec_command"]


def _wants_json(args) -> bool:
    """True when the canonical ``--format json`` spelling was requested."""
    return getattr(args, "spec_format", "text") == "json"


def run_spec_command(args) -> int:
    """Handle spec subcommands."""
    spec_command = getattr(args, "spec_command", None)

    if spec_command == "init":
        return _run_spec_init(args)
    elif spec_command == "validate":
        return _run_spec_validate(args)
    elif spec_command == "status":
        return _run_spec_status(args)
    elif spec_command == "decide":
        return _run_spec_decide(args)
    elif spec_command == "check":
        return _run_spec_check(args)
    else:
        print("Usage: kct spec <command>", file=sys.stderr)
        print("Commands: init, validate, status, decide, check", file=sys.stderr)
        return 1


def _run_spec_init(args) -> int:
    """Initialize a new .kct specification file."""
    from datetime import date

    from rich.console import Console

    from kicad_tools.spec import get_template

    console = Console()

    name = args.spec_name
    template = getattr(args, "spec_template", "minimal")
    output = getattr(args, "spec_output", None)

    # Determine output path
    if output:
        output_path = Path(output)
    else:
        # Default to project.kct in current directory
        output_path = Path("project.kct")

    json_mode = _wants_json(args)

    # Check if file exists
    if output_path.exists() and not getattr(args, "spec_force", False):
        if json_mode:
            emit_json({"error": f"File already exists: {output_path} (use --force to overwrite)"})
            return 1
        console.print(f"[red]Error:[/red] File already exists: {output_path}")
        console.print("Use --force to overwrite")
        return 1

    try:
        # Get template and format with current date
        content = get_template(template)
        content = content.format(date=date.today().isoformat())

        # Replace placeholder name
        content = content.replace('name: "My Project"', f'name: "{name}"')
        content = content.replace('name: "Power Supply"', f'name: "{name}"')
        content = content.replace('name: "Sensor Board"', f'name: "{name}"')
        content = content.replace('name: "MCU Breakout"', f'name: "{name}"')

        # Write file
        output_path.write_text(content, encoding="utf-8")

        if json_mode:
            emit_json(
                {
                    "created": str(output_path),
                    "name": name,
                    "template": template,
                }
            )
            return 0

        console.print(f"[green]Created:[/green] {output_path}")
        console.print(f"Template: {template}")
        console.print("\nNext steps:")
        console.print(f"  1. Edit {output_path} to add your requirements")
        console.print(f"  2. Run 'kct spec validate {output_path}' to check the spec")
        console.print(f"  3. Run 'kct spec status {output_path}' to view progress")

        return 0

    except ValueError as e:
        if json_mode:
            emit_json({"error": str(e)})
            return 1
        console.print(f"[red]Error:[/red] {e}")
        return 1
    except OSError as e:
        if json_mode:
            emit_json({"error": f"Error writing file: {e}"})
            return 1
        console.print(f"[red]Error writing file:[/red] {e}")
        return 1


def _run_spec_validate(args) -> int:
    """Validate a .kct specification file."""
    from rich.console import Console

    from kicad_tools.spec import validate_spec_detailed

    console = Console()
    spec_file = Path(args.spec_file)

    is_valid, errors, warnings = validate_spec_detailed(spec_file)

    if _wants_json(args):
        emit_json(
            {
                "file": str(spec_file),
                "valid": is_valid,
                "errors": list(errors),
                "warnings": list(warnings),
            }
        )
        return 0 if is_valid else 1

    if is_valid:
        console.print(f"[green]Valid:[/green] {spec_file}")
    else:
        console.print(f"[red]Invalid:[/red] {spec_file}")
        for error in errors:
            console.print(f"  - {error}")

    # Unrecognized keys are preserved on write, but they are almost always a
    # typo or schema drift -- report them so drift is visible, never silent.
    if warnings:
        console.print(f"[yellow]Warnings:[/yellow] {len(warnings)} unrecognized key(s)")
        for warning in warnings:
            console.print(f"  - {warning}")
        console.print(
            "  These keys are not part of the .kct schema. They are preserved "
            "when kicad-tools rewrites the file, but check them for typos."
        )

    return 0 if is_valid else 1


def _run_spec_status(args) -> int:
    """Show status and progress from a .kct specification file."""
    from rich.console import Console
    from rich.panel import Panel
    from rich.progress import BarColumn, Progress, TextColumn
    from rich.table import Table

    from kicad_tools.spec import load_spec
    from kicad_tools.spec.schema import PhaseStatus

    console = Console()
    spec_file = Path(args.spec_file)

    try:
        spec = load_spec(spec_file)
    except Exception as e:
        if _wants_json(args):
            emit_json({"error": f"Error loading spec: {e}"})
            return 1
        console.print(f"[red]Error loading spec:[/red] {e}")
        return 1

    if _wants_json(args):
        emit_json(_status_payload(spec_file, spec))
        return 0

    # Project info
    console.print(
        Panel(
            f"[bold]{spec.project.name}[/bold] v{spec.project.revision}\n"
            f"{spec.intent.summary if spec.intent else 'No description'}",
            title="Project Specification",
        )
    )

    # Progress overview
    if spec.progress:
        phase_name = (
            spec.progress.phase.value
            if hasattr(spec.progress.phase, "value")
            else spec.progress.phase
        )
        console.print(f"\n[bold]Current Phase:[/bold] {phase_name}")

        # Show completion percentage
        completion = spec.get_completion_percentage()
        console.print(f"[bold]Overall Progress:[/bold] {completion:.0f}%")

        # Progress bar
        with Progress(
            TextColumn("[progress.description]{task.description}"),
            BarColumn(),
            TextColumn("[progress.percentage]{task.percentage:>3.0f}%"),
            console=console,
            transient=True,
        ) as progress:
            task = progress.add_task("Completion", total=100, completed=completion)
            progress.update(task)

        # Phase details table
        if spec.progress.phases:
            table = Table(title="Phase Progress")
            table.add_column("Phase", style="cyan")
            table.add_column("Status")
            table.add_column("Checklist")

            for phase_key, phase in spec.progress.phases.items():
                # Status with color
                if phase.status == PhaseStatus.COMPLETED:
                    status = "[green]completed[/green]"
                elif phase.status == PhaseStatus.IN_PROGRESS:
                    status = "[yellow]in_progress[/yellow]"
                elif phase.status == PhaseStatus.BLOCKED:
                    status = "[red]blocked[/red]"
                else:
                    status = "[dim]pending[/dim]"

                # Checklist summary
                if phase.checklist:
                    done = sum(
                        1
                        for item in phase.checklist
                        if item.startswith("[x]") or item.startswith("[X]")
                    )
                    total = len(phase.checklist)
                    checklist = f"{done}/{total}"
                else:
                    checklist = "-"

                # Highlight current phase
                phase_display = (
                    f"[bold]{phase_key}[/bold]" if phase_key == phase_name else phase_key
                )

                table.add_row(phase_display, status, checklist)

            console.print(table)

        # Show blockers
        if spec.progress.blockers:
            console.print("\n[bold red]Blockers:[/bold red]")
            for blocker in spec.progress.blockers:
                console.print(f"  - {blocker}")

    # Show recent decisions
    if spec.decisions:
        console.print("\n[bold]Recent Decisions:[/bold]")
        for decision in spec.decisions[-3:]:  # Last 3 decisions
            console.print(f"  [{decision.date}] {decision.topic}: {decision.choice}")

    # Validation summary
    if spec.validation and spec.validation.last_run:
        console.print(f"\n[bold]Last Validation:[/bold] {spec.validation.last_run}")
        if spec.validation.schematic:
            for check, result in spec.validation.schematic.items():
                status_icon = "[green]✓[/green]" if result.status == "pass" else "[red]✗[/red]"
                console.print(
                    f"  {status_icon} {check}: {result.errors} errors, {result.warnings} warnings"
                )

    return 0


def _status_payload(spec_file: Path, spec) -> dict:
    """Machine rendering of ``kct spec status`` (mirrors the prose report)."""

    def _enum_value(value):
        return value.value if hasattr(value, "value") else value

    payload: dict = {
        "file": str(spec_file),
        "project": {
            "name": spec.project.name,
            "revision": spec.project.revision,
        },
        "summary": spec.intent.summary if spec.intent else None,
        "phase": None,
        "completion_percent": None,
        "phases": {},
        "blockers": [],
        "decisions": [],
        "validation": None,
    }

    if spec.progress:
        payload["phase"] = _enum_value(spec.progress.phase)
        payload["completion_percent"] = spec.get_completion_percentage()
        for phase_key, phase in (spec.progress.phases or {}).items():
            checklist = phase.checklist or []
            done = sum(1 for item in checklist if item.startswith(("[x]", "[X]")))
            payload["phases"][phase_key] = {
                "status": _enum_value(phase.status),
                "checklist_done": done,
                "checklist_total": len(checklist),
            }
        payload["blockers"] = list(spec.progress.blockers or [])

    if spec.decisions:
        payload["decisions"] = [
            {
                "date": str(decision.date),
                "topic": decision.topic,
                "choice": decision.choice,
            }
            for decision in spec.decisions
        ]

    if spec.validation and spec.validation.last_run:
        validation: dict = {"last_run": str(spec.validation.last_run), "schematic": {}}
        if spec.validation.schematic:
            for check, result in spec.validation.schematic.items():
                validation["schematic"][check] = {
                    "status": result.status,
                    "errors": result.errors,
                    "warnings": result.warnings,
                }
        payload["validation"] = validation

    return payload


def _run_spec_decide(args) -> int:
    """Record a design decision in the .kct specification file."""
    from datetime import date

    from rich.console import Console

    from kicad_tools.spec import append_decision, load_spec
    from kicad_tools.spec.schema import Decision, DesignPhase

    console = Console()
    spec_file = Path(args.spec_file)
    json_mode = _wants_json(args)

    try:
        spec = load_spec(spec_file)
    except Exception as e:
        if json_mode:
            emit_json({"error": f"Error loading spec: {e}"})
            return 1
        console.print(f"[red]Error loading spec:[/red] {e}")
        return 1

    # Get current phase
    if spec.progress:
        phase = spec.progress.phase
    else:
        phase = DesignPhase.CONCEPT

    # Create decision
    decision = Decision(
        date=date.today(),
        phase=phase,
        topic=args.decide_topic,
        choice=args.decide_choice,
        rationale=args.decide_rationale,
        alternatives=args.decide_alternatives.split(",") if args.decide_alternatives else None,
    )

    # Append-only: splice the new entry into the existing file rather than
    # rewriting it from the model. Everything else in the file -- including
    # keys the schema does not define, and the decisions already recorded --
    # is left untouched.
    try:
        append_decision(spec_file, decision)
        if json_mode:
            emit_json(
                {
                    "file": str(spec_file),
                    "recorded": True,
                    "date": str(decision.date),
                    "phase": phase.value if hasattr(phase, "value") else phase,
                    "topic": decision.topic,
                    "choice": decision.choice,
                    "rationale": decision.rationale,
                    "alternatives": decision.alternatives,
                }
            )
            return 0
        console.print(f"[green]Decision recorded:[/green] {decision.topic}")
        console.print(f"  Choice: {decision.choice}")
        console.print(f"  Rationale: {decision.rationale}")
        return 0
    except Exception as e:
        if json_mode:
            emit_json({"error": f"Error saving spec: {e}"})
            return 1
        console.print(f"[red]Error saving spec:[/red] {e}")
        return 1


def _run_spec_check(args) -> int:
    """Mark a checklist item as complete in the .kct specification file."""
    from rich.console import Console

    from kicad_tools.spec import load_spec, save_spec

    console = Console()
    spec_file = Path(args.spec_file)
    json_mode = _wants_json(args)
    item_path = args.check_item  # Format: "phase.item text" or "item text"

    try:
        spec = load_spec(spec_file)
    except Exception as e:
        if json_mode:
            emit_json({"error": f"Error loading spec: {e}"})
            return 1
        console.print(f"[red]Error loading spec:[/red] {e}")
        return 1

    if not spec.progress or not spec.progress.phases:
        if json_mode:
            emit_json({"error": "No progress phases defined in spec"})
            return 1
        console.print("[red]Error:[/red] No progress phases defined in spec")
        return 1

    # Parse item path
    if "." in item_path:
        phase_name, item_text = item_path.split(".", 1)
    else:
        # Use current phase
        phase_name = (
            spec.progress.phase.value
            if hasattr(spec.progress.phase, "value")
            else spec.progress.phase
        )
        item_text = item_path

    phase_name = phase_name.strip()
    item_text = item_text.strip()

    # Find phase
    if phase_name not in spec.progress.phases:
        if json_mode:
            emit_json(
                {
                    "error": f"Phase not found: {phase_name}",
                    "available_phases": list(spec.progress.phases.keys()),
                }
            )
            return 1
        console.print(f"[red]Error:[/red] Phase not found: {phase_name}")
        console.print(f"Available phases: {', '.join(spec.progress.phases.keys())}")
        return 1

    phase = spec.progress.phases[phase_name]
    if not phase.checklist:
        if json_mode:
            emit_json({"error": f"No checklist in phase: {phase_name}"})
            return 1
        console.print(f"[red]Error:[/red] No checklist in phase: {phase_name}")
        return 1

    # Find and update item
    found = False
    checked_item = None
    for i, item in enumerate(phase.checklist):
        # Strip checkbox prefix for comparison
        item_content = item.lstrip("[ ]xX").strip()
        if item_content.lower() == item_text.lower() or item_text.lower() in item_content.lower():
            # Mark as complete
            phase.checklist[i] = f"[x] {item_content}"
            found = True
            checked_item = item_content
            if not json_mode:
                console.print(f"[green]Checked:[/green] {item_content}")
            break

    if not found:
        if json_mode:
            emit_json(
                {
                    "error": f"Item not found: {item_text}",
                    "available_items": list(phase.checklist),
                }
            )
            return 1
        console.print(f"[red]Error:[/red] Item not found: {item_text}")
        console.print("Available items:")
        for item in phase.checklist:
            console.print(f"  - {item}")
        return 1

    # Save
    try:
        save_spec(spec, spec_file)
        if json_mode:
            emit_json(
                {
                    "file": str(spec_file),
                    "phase": phase_name,
                    "checked": checked_item,
                }
            )
        return 0
    except Exception as e:
        if json_mode:
            emit_json({"error": f"Error saving spec: {e}"})
            return 1
        console.print(f"[red]Error saving spec:[/red] {e}")
        return 1
