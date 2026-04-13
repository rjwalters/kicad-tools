"""
Pipeline command for end-to-end repair workflow on existing PCBs.

Orchestrates the full repair pipeline:
1. Load board state (detect routing status)
2. Fix vias (manufacturer compliance)
3. [Optional] Route (if board is unrouted)
4. Fix DRC violations
5. Optimize traces
6. Zone fill (requires kicad-cli)
7. Audit / check

Usage:
    kct pipeline board.kicad_pcb --mfr jlcpcb
    kct pipeline board.kicad_pcb --dry-run
    kct pipeline board.kicad_pcb --step fix-vias
    kct pipeline project.kicad_pro --mfr jlcpcb --layers 4
"""

from __future__ import annotations

import argparse
import logging
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

logger = logging.getLogger(__name__)

__all__ = ["main"]


class PipelineStep(str, Enum):
    """Pipeline step identifiers."""

    ROUTE = "route"
    FIX_VIAS = "fix-vias"
    FIX_DRC = "fix-drc"
    OPTIMIZE = "optimize"
    ZONES = "zones"
    AUDIT = "audit"


# Ordered list of all pipeline steps
ALL_STEPS = [
    PipelineStep.FIX_VIAS,
    PipelineStep.ROUTE,
    PipelineStep.FIX_DRC,
    PipelineStep.OPTIMIZE,
    PipelineStep.ZONES,
    PipelineStep.AUDIT,
]


@dataclass
class PipelineResult:
    """Result of a single pipeline step."""

    step: str
    success: bool
    message: str
    skipped: bool = False


@dataclass
class PipelineContext:
    """Context for pipeline execution."""

    pcb_file: Path
    project_file: Path | None = None
    mfr: str = "jlcpcb"
    layers: int = 2
    dry_run: bool = False
    verbose: bool = False
    quiet: bool = False
    force: bool = False
    is_project: bool = False


def _detect_routing_status(pcb_file: Path) -> tuple[bool, int, int]:
    """Detect whether a PCB has been routed by counting segments and nets.

    Reads the PCB file and counts (segment ...) and (arc ...) nodes
    to determine if routing has been performed.

    Args:
        pcb_file: Path to .kicad_pcb file

    Returns:
        Tuple of (is_routed, segment_count, net_count) where is_routed
        is True if the board has routing segments.
    """
    try:
        content = pcb_file.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as e:
        logger.warning("Could not read PCB file %s: %s", pcb_file, e)
        return False, 0, 0

    # Count segment and arc nodes (routing traces)
    segment_count = content.count("(segment ")
    arc_count = content.count("(arc ")
    total_traces = segment_count + arc_count

    # Count nets (excluding net 0 which is the unconnected net)
    net_count = content.count("(net ") - content.count("(net 0 ")

    is_routed = total_traces > 0

    return is_routed, total_traces, net_count


def _resolve_pcb_from_project(project_file: Path) -> Path | None:
    """Resolve .kicad_pcb path from a .kicad_pro file.

    KiCad project files use the same stem as their PCB files.

    Args:
        project_file: Path to .kicad_pro file

    Returns:
        Path to the corresponding .kicad_pcb if it exists, None otherwise.
    """
    pcb_path = project_file.with_suffix(".kicad_pcb")
    if pcb_path.exists():
        return pcb_path
    return None


def _run_subprocess_step(
    cmd: list[str],
    cwd: Path,
    verbose: bool = False,
) -> tuple[bool, str]:
    """Run a subprocess command and return (success, message).

    Args:
        cmd: Command and arguments to execute
        cwd: Working directory
        verbose: Whether to show output

    Returns:
        Tuple of (success, output/error message)
    """
    try:
        result = subprocess.run(
            cmd,
            cwd=str(cwd),
            capture_output=not verbose,
            text=True,
        )

        if result.returncode == 0:
            return True, "completed successfully"
        elif result.returncode == 2:
            # Some commands use exit code 2 for partial success (e.g., DRC-only failures)
            return True, "completed with warnings"
        else:
            error_msg = result.stderr.strip() if result.stderr else f"exit code {result.returncode}"
            return False, f"failed: {error_msg}"

    except FileNotFoundError:
        return False, f"command not found: {cmd[0]}"
    except Exception as e:
        return False, f"failed: {e}"


def _run_step_route(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run routing step if board is unrouted."""
    is_routed, trace_count, net_count = _detect_routing_status(ctx.pcb_file)

    if is_routed and not ctx.force:
        return PipelineResult(
            step=PipelineStep.ROUTE,
            success=True,
            message=f"Board already routed ({trace_count} traces, {net_count} nets) - skipped",
            skipped=True,
        )

    if ctx.dry_run:
        if is_routed:
            return PipelineResult(
                step=PipelineStep.ROUTE,
                success=True,
                message=f"[dry-run] Would re-route (--force): {ctx.pcb_file.name}",
            )
        return PipelineResult(
            step=PipelineStep.ROUTE,
            success=True,
            message=f"[dry-run] Would run: kct route {ctx.pcb_file.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running autorouter on {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "route",
        str(ctx.pcb_file),
        "-o",
        str(ctx.pcb_file),  # Route in place for pipeline
    ]

    if ctx.quiet:
        cmd.append("--quiet")

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.ROUTE,
        success=success,
        message=f"route: {message}",
    )


def _run_step_fix_vias(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run via repair step."""
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.FIX_VIAS,
            success=True,
            message=f"[dry-run] Would run: kct fix-vias {ctx.pcb_file.name} --mfr {ctx.mfr} --layers {ctx.layers}",
        )

    if not ctx.quiet:
        console.print(f"  Fixing vias for {ctx.mfr} ({ctx.layers} layers)...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-vias",
        str(ctx.pcb_file),
        "--mfr",
        ctx.mfr,
        "--layers",
        str(ctx.layers),
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.FIX_VIAS,
        success=success,
        message=f"fix-vias: {message}",
    )


def _run_step_fix_drc(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run DRC repair step."""
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.FIX_DRC,
            success=True,
            message=f"[dry-run] Would run: kct fix-drc {ctx.pcb_file.name} --max-passes 3",
        )

    if not ctx.quiet:
        console.print(f"  Running DRC repair on {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-drc",
        str(ctx.pcb_file),
        "--max-passes",
        "3",
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.FIX_DRC,
        success=success,
        message=f"fix-drc: {message}",
    )


def _run_step_optimize(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run trace optimization step."""
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.OPTIMIZE,
            success=True,
            message=(
                f"[dry-run] Would run: kct optimize-traces {ctx.pcb_file.name} "
                f"--drc-aware --mfr {ctx.mfr} --layers {ctx.layers}"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Optimizing traces in {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "optimize-traces",
        str(ctx.pcb_file),
        "--drc-aware",
        "--mfr",
        ctx.mfr,
        "--layers",
        str(ctx.layers),
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.OPTIMIZE,
        success=success,
        message=f"optimize-traces: {message}",
    )


def _run_step_zones(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run zone fill step (requires kicad-cli)."""
    from .runner import find_kicad_cli

    kicad_cli = find_kicad_cli()

    if kicad_cli is None:
        return PipelineResult(
            step=PipelineStep.ZONES,
            success=True,
            message="zones fill: skipped (kicad-cli not installed)",
            skipped=True,
        )

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.ZONES,
            success=True,
            message=f"[dry-run] Would run: kct zones fill {ctx.pcb_file.name}",
        )

    if not ctx.quiet:
        console.print(f"  Filling zones in {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "zones",
        "fill",
        str(ctx.pcb_file),
    ]

    if ctx.quiet:
        cmd.append("--quiet")

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.ZONES,
        success=success,
        message=f"zones fill: {message}",
    )


def _run_step_audit(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run final audit/check step."""
    # Use project-level audit if we have a .kicad_pro, else PCB-level check
    if ctx.is_project and ctx.project_file:
        target = str(ctx.project_file)
        cmd_name = "audit"
    else:
        target = str(ctx.pcb_file)
        cmd_name = "check"

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.AUDIT,
            success=True,
            message=f"[dry-run] Would run: kct {cmd_name} {Path(target).name} --mfr {ctx.mfr}",
        )

    if not ctx.quiet:
        console.print(f"  Running {cmd_name} on {Path(target).name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        cmd_name,
        target,
        "--mfr",
        ctx.mfr,
    ]

    if cmd_name == "check":
        cmd.extend(["--layers", str(ctx.layers)])

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.AUDIT,
        success=success,
        message=f"{cmd_name}: {message}",
    )


# Map of step name to runner function
STEP_RUNNERS = {
    PipelineStep.ROUTE: _run_step_route,
    PipelineStep.FIX_VIAS: _run_step_fix_vias,
    PipelineStep.FIX_DRC: _run_step_fix_drc,
    PipelineStep.OPTIMIZE: _run_step_optimize,
    PipelineStep.ZONES: _run_step_zones,
    PipelineStep.AUDIT: _run_step_audit,
}


def run_pipeline(
    ctx: PipelineContext, steps: list[PipelineStep] | None = None
) -> list[PipelineResult]:
    """Run the pipeline with the given context.

    Args:
        ctx: Pipeline execution context
        steps: Steps to run, or None for all steps

    Returns:
        List of PipelineResult for each step executed.
    """
    if steps is None:
        steps = list(ALL_STEPS)

    console = Console(quiet=ctx.quiet)
    results: list[PipelineResult] = []

    # Print pipeline header
    if not ctx.quiet:
        mode = "[dry-run] " if ctx.dry_run else ""
        console.print(
            Panel.fit(
                f"[bold]{mode}Pipeline:[/bold] {ctx.pcb_file.name}\n"
                f"[dim]Directory:[/dim] {ctx.pcb_file.parent}\n"
                f"[dim]Manufacturer:[/dim] {ctx.mfr} ({ctx.layers} layers)",
                title="kct pipeline",
            )
        )

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        disable=ctx.quiet,
    ) as progress:
        for step in steps:
            runner = STEP_RUNNERS[step]
            task = progress.add_task(f"[cyan]{step.value}[/cyan]...", total=None)

            result = runner(ctx, console)
            results.append(result)

            progress.remove_task(task)

            # Print step result
            if not ctx.quiet:
                if result.skipped:
                    status = "[yellow]SKIP[/yellow]"
                elif result.success:
                    status = "[green]OK[/green]"
                else:
                    status = "[red]FAIL[/red]"
                console.print(f"  [{status}] {result.message}")

            # Stop on failure (unless it's the audit step -- always report)
            if not result.success and step != PipelineStep.AUDIT:
                break

    # Print summary
    if not ctx.quiet:
        console.print()
        success_count = sum(1 for r in results if r.success)
        total_count = len(results)
        skipped_count = sum(1 for r in results if r.skipped)

        if success_count == total_count:
            skip_note = f", {skipped_count} skipped" if skipped_count else ""
            console.print(
                f"[green]Pipeline completed successfully[/green] "
                f"({success_count}/{total_count} steps{skip_note})"
            )
        else:
            console.print(
                f"[red]Pipeline failed[/red] ({success_count}/{total_count} steps succeeded)"
            )

    return results


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct pipeline command."""
    parser = argparse.ArgumentParser(
        prog="kct pipeline",
        description="End-to-end repair pipeline for existing PCBs",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    kct pipeline board.kicad_pcb                      # Full pipeline with defaults
    kct pipeline board.kicad_pcb --mfr jlcpcb         # Target JLCPCB rules
    kct pipeline board.kicad_pcb --dry-run             # Preview steps
    kct pipeline board.kicad_pcb --step fix-vias       # Run single step
    kct pipeline project.kicad_pro --layers 4          # 4-layer project audit
    kct pipeline board.kicad_pcb --force               # Force re-route
        """,
    )

    parser.add_argument(
        "input",
        help="Path to .kicad_pcb or .kicad_pro file",
    )
    parser.add_argument(
        "--step",
        "-s",
        choices=[s.value for s in PipelineStep],
        default=None,
        help="Run only this step (default: run all steps in order)",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        default="jlcpcb",
        help="Target manufacturer (default: jlcpcb)",
    )
    parser.add_argument(
        "--layers",
        "-l",
        type=int,
        default=2,
        help="Number of PCB layers (default: 2)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview pipeline steps without modifying files",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output from each step",
    )
    parser.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress progress output",
    )
    parser.add_argument(
        "-f",
        "--force",
        action="store_true",
        help="Force all steps (e.g., re-route even if already routed)",
    )

    args = parser.parse_args(argv)

    input_path = Path(args.input).resolve()

    if not input_path.exists():
        print(f"Error: File not found: {input_path}", file=sys.stderr)
        return 1

    # Determine input type and resolve PCB file
    is_project = input_path.suffix == ".kicad_pro"
    project_file: Path | None = None

    if is_project:
        project_file = input_path
        pcb_file = _resolve_pcb_from_project(input_path)
        if pcb_file is None:
            print(
                f"Error: No .kicad_pcb file found for project {input_path.name}",
                file=sys.stderr,
            )
            return 1
    elif input_path.suffix == ".kicad_pcb":
        pcb_file = input_path
        # Check if a .kicad_pro exists alongside
        pro_file = input_path.with_suffix(".kicad_pro")
        if pro_file.exists():
            project_file = pro_file
            is_project = True
    else:
        print(
            f"Error: Unsupported file type: {input_path.suffix} "
            f"(expected .kicad_pcb or .kicad_pro)",
            file=sys.stderr,
        )
        return 1

    # Build context
    ctx = PipelineContext(
        pcb_file=pcb_file,
        project_file=project_file,
        mfr=args.mfr,
        layers=args.layers,
        dry_run=args.dry_run,
        verbose=args.verbose,
        quiet=args.quiet,
        force=args.force,
        is_project=is_project,
    )

    # Determine steps to run
    if args.step:
        steps = [PipelineStep(args.step)]
    else:
        steps = None  # All steps

    results = run_pipeline(ctx, steps)

    # Determine exit code: 0 if all succeeded, 1 if any failed
    if all(r.success for r in results):
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
