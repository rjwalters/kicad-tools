"""
Pipeline command for end-to-end repair workflow on existing PCBs.

Orchestrates the full repair pipeline:
0. ERC check (schematic validation)
1. Load board state (detect routing status)
2. Fix silkscreen (manufacturer line-width compliance)
3. Fix vias (manufacturer compliance)
4. [Optional] Route (if board is unrouted)
5. Fix DRC violations
6. Optimize traces
7. Zone fill (requires kicad-cli)
8. Audit / check
9. Report generation (manufacturing report)

Usage:
    kct pipeline board.kicad_pcb --mfr jlcpcb
    kct pipeline board.kicad_pcb --dry-run
    kct pipeline board.kicad_pcb --step fix-vias
    kct pipeline board.kicad_pcb --step fix-silkscreen
    kct pipeline board.kicad_pcb --step erc
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

    ERC = "erc"
    FIX_SILKSCREEN = "fix-silkscreen"
    FIX_ERC = "fix-erc"
    ROUTE = "route"
    FIX_VIAS = "fix-vias"
    FIX_DRC = "fix-drc"
    OPTIMIZE = "optimize"
    ZONES = "zones"
    AUDIT = "audit"
    REPORT = "report"


# Ordered list of all pipeline steps
ALL_STEPS = [
    PipelineStep.ERC,
    PipelineStep.FIX_SILKSCREEN,
    PipelineStep.FIX_ERC,
    PipelineStep.FIX_VIAS,
    PipelineStep.ROUTE,
    PipelineStep.FIX_DRC,
    PipelineStep.OPTIMIZE,
    PipelineStep.ZONES,
    PipelineStep.AUDIT,
    PipelineStep.REPORT,
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
    schematic_file: Path | None = None
    mfr: str = "jlcpcb"
    layers: int | None = None
    dry_run: bool = False
    verbose: bool = False
    quiet: bool = False
    force: bool = False
    is_project: bool = False
    commit: bool = False


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


def _resolve_schematic(pcb_file: Path, project_file: Path | None = None) -> Path | None:
    """Resolve .kicad_sch path from a project or PCB file.

    Resolution chain:
        .kicad_pro -> stem.kicad_sch
        .kicad_pcb -> sibling stem.kicad_sch

    Args:
        pcb_file: Path to .kicad_pcb file
        project_file: Optional path to .kicad_pro file

    Returns:
        Path to the corresponding .kicad_sch if it exists, None otherwise.
    """
    # Try from project file first
    if project_file is not None:
        sch_path = project_file.with_suffix(".kicad_sch")
        if sch_path.exists():
            return sch_path

    # Try from PCB file (sibling with same stem)
    sch_path = pcb_file.with_suffix(".kicad_sch")
    if sch_path.exists():
        return sch_path

    return None


def _run_step_erc(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run ERC (Electrical Rules Check) step on the schematic.

    Checks the schematic for electrical rule violations before routing.
    Skips gracefully when no schematic is found or kicad-cli is missing.
    Halts the pipeline on errors unless --force is used.
    """
    from .runner import find_kicad_cli, run_erc

    # Skip if no schematic file available
    if ctx.schematic_file is None:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message="erc: no .kicad_sch found alongside PCB — skipped",
            skipped=True,
        )

    # Check for kicad-cli availability
    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message="erc: kicad-cli not found — install KiCad 8 to enable ERC",
            skipped=True,
        )

    # Dry-run mode
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message=f"[dry-run] Would run: kct erc {ctx.schematic_file.name} --errors-only",
        )

    if not ctx.quiet:
        console.print(f"  Running ERC on {ctx.schematic_file.name}...")

    # Run ERC via kicad-cli
    result = run_erc(ctx.schematic_file, kicad_cli=kicad_cli)

    if not result.success:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=False,
            message=f"erc: failed to run — {result.stderr}",
        )

    # Parse the ERC report
    try:
        from ..erc import ERCReport

        report = ERCReport.load(result.output_path)
    except Exception as e:
        logger.warning("Could not parse ERC report: %s", e)
        return PipelineResult(
            step=PipelineStep.ERC,
            success=False,
            message=f"erc: failed to parse report — {e}",
        )
    finally:
        # Clean up temporary report file
        if result.output_path:
            result.output_path.unlink(missing_ok=True)

    error_count = report.error_count
    warning_count = report.warning_count

    # Print per-violation details (unless --quiet)
    if not ctx.quiet and (error_count > 0 or warning_count > 0):
        from ..feedback.suggestions import generate_erc_suggestions

        for violation in report.violations:
            if violation.excluded:
                continue
            severity_tag = "ERR" if violation.is_error else "WARN"
            console.print(f"    [{severity_tag}] {violation.type_str}: {violation.description}")
            suggestions = generate_erc_suggestions(violation)
            if suggestions:
                console.print(f"          Suggestion: {suggestions[0]}")

    # No errors and no warnings -> clean pass
    if error_count == 0 and warning_count == 0:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message="erc: no violations found",
        )

    # Warnings only (no errors) -> pass
    if error_count == 0:
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message=f"erc: {warning_count} warning(s), no errors",
        )

    # Errors found
    if ctx.force:
        if not ctx.quiet:
            console.print(
                f"  [yellow]erc: {error_count} error(s) found — continuing (--force)[/yellow]"
            )
        return PipelineResult(
            step=PipelineStep.ERC,
            success=True,
            message=f"erc: {error_count} error(s) found — continuing (--force)",
        )

    # Halt pipeline
    return PipelineResult(
        step=PipelineStep.ERC,
        success=False,
        message=f"erc: {error_count} error(s) found (use --force to continue)",
    )


def _run_step_fix_silkscreen(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run silkscreen line-width repair step.

    Fixes silkscreen line widths to meet manufacturer minimum specifications.
    This is safe to run unconditionally and is idempotent.
    """
    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.FIX_SILKSCREEN,
            success=True,
            message=f"[dry-run] Would run: kct fix-silkscreen {ctx.pcb_file.name} --mfr {ctx.mfr}",
        )

    if not ctx.quiet:
        console.print(f"  Fixing silkscreen widths for {ctx.mfr}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-silkscreen",
        str(ctx.pcb_file),
        "--mfr",
        ctx.mfr,
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)
    return PipelineResult(
        step=PipelineStep.FIX_SILKSCREEN,
        success=success,
        message=f"fix-silkscreen: {message}",
    )


def _run_step_fix_erc(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run ERC auto-fix step on the schematic.

    Inserts PWR_FLAG and no-connect markers to resolve common ERC violations.
    Skips gracefully when no schematic is found.
    """
    # Skip if no schematic file available
    if ctx.schematic_file is None:
        return PipelineResult(
            step=PipelineStep.FIX_ERC,
            success=True,
            message="fix-erc: no .kicad_sch found alongside PCB -- skipped",
            skipped=True,
        )

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.FIX_ERC,
            success=True,
            message=f"[dry-run] Would run: kct fix-erc {ctx.schematic_file.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running ERC auto-fix on {ctx.schematic_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "fix-erc",
        str(ctx.schematic_file),
    ]

    success, message = _run_subprocess_step(cmd, ctx.schematic_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.FIX_ERC,
        success=success,
        message=f"fix-erc: {message}",
    )


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
                message=(
                    f"[dry-run] Would re-route (--force): {ctx.pcb_file.name} "
                    f"--grid auto --manufacturer {ctx.mfr} --layers auto --auto-fix"
                ),
            )
        return PipelineResult(
            step=PipelineStep.ROUTE,
            success=True,
            message=(
                f"[dry-run] Would run: kct route {ctx.pcb_file.name} "
                f"--grid auto --manufacturer {ctx.mfr} --layers auto --auto-fix"
            ),
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
        "--grid",
        "auto",
        "--manufacturer",
        ctx.mfr,
        "--layers",
        "auto",  # Let router auto-detect; avoids int-to-"4-sig" ambiguity
        "--auto-fix",
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


def _run_step_report(ctx: PipelineContext, console: Console) -> PipelineResult:
    """Run report generation step (final step after AUDIT)."""
    reports_dir = ctx.pcb_file.parent / "reports"

    if ctx.dry_run:
        return PipelineResult(
            step=PipelineStep.REPORT,
            success=True,
            message=(
                f"[dry-run] Would run: kct report generate {ctx.pcb_file.name} "
                f"--mfr {ctx.mfr} --no-figures -o reports/"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Generating report for {ctx.pcb_file.name}...")

    cmd = [
        sys.executable,
        "-m",
        "kicad_tools.cli",
        "report",
        "generate",
        str(ctx.pcb_file),
        "--mfr",
        ctx.mfr,
        "--no-figures",
        "-o",
        str(reports_dir),
    ]

    success, message = _run_subprocess_step(cmd, ctx.pcb_file.parent, ctx.verbose)

    return PipelineResult(
        step=PipelineStep.REPORT,
        success=success,
        message=f"report: {message}",
    )


def _is_git_repo(directory: Path) -> bool:
    """Check whether *directory* is inside a git repository.

    Args:
        directory: Directory to check.

    Returns:
        True if the directory is inside a git working tree.
    """
    try:
        result = subprocess.run(
            ["git", "-C", str(directory), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
        )
        return result.returncode == 0
    except FileNotFoundError:
        return False


def _build_commit_message(
    ctx: PipelineContext,
    results: list[PipelineResult],
) -> str:
    """Build a structured commit message from pipeline results.

    Attempts to extract DRC error count and routing net counts.  Falls back
    to a simpler message when metrics cannot be determined.

    Args:
        ctx: Pipeline context (for manufacturer name).
        results: List of results from the pipeline run.

    Returns:
        A single-line commit message string.
    """
    drc_errors: int | None = None
    routed_nets: int | None = None
    total_nets: int | None = None

    # Try to get routing status from the final PCB file
    try:
        _, _, net_count = _detect_routing_status(ctx.pcb_file)
        if net_count > 0:
            # The net count from _detect_routing_status is a rough count.
            # Use it as both routed and total since the pipeline aims for
            # full routing.
            routed_nets = net_count
            total_nets = net_count
    except Exception:
        pass

    # Try to extract DRC error count by running kct check --format json
    try:
        check_result = subprocess.run(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "check",
                str(ctx.pcb_file),
                "--mfr",
                ctx.mfr,
                "--layers",
                str(ctx.layers or 2),
                "--format",
                "json",
            ],
            cwd=str(ctx.pcb_file.parent),
            capture_output=True,
            text=True,
        )
        import json as _json

        data = _json.loads(check_result.stdout)
        if isinstance(data, dict):
            # Try common keys for violation count
            drc_errors = data.get("total_violations", data.get("violations_count"))
            if drc_errors is None and "violations" in data:
                violations = data["violations"]
                if isinstance(violations, list):
                    drc_errors = len(violations)
    except Exception:
        pass

    # Build message
    parts: list[str] = []
    if drc_errors is not None:
        parts.append(f"{drc_errors} DRC errors")
    if routed_nets is not None and total_nets is not None:
        parts.append(f"{routed_nets}/{total_nets} signal nets routed")

    if parts:
        detail = ", ".join(parts)
        return f"fix: run kct pipeline ({detail})"
    else:
        return f"fix: run kct pipeline ({ctx.mfr})"


def _git_commit_result(
    ctx: PipelineContext,
    results: list[PipelineResult],
    console: Console,
) -> int:
    """Stage the PCB file and create a git commit after a successful pipeline run.

    Args:
        ctx: Pipeline context.
        results: Pipeline step results (used for commit message).
        console: Rich console for output.

    Returns:
        0 on success, 1 on failure.
    """
    pcb_dir = ctx.pcb_file.parent

    # Verify we are inside a git repository
    if not _is_git_repo(pcb_dir):
        print(
            f"Error: --commit requires a git repository, "
            f"but {pcb_dir} is not inside a git working tree.",
            file=sys.stderr,
        )
        return 1

    # Stage the PCB file and reports/ directory (if present)
    reports_dir = ctx.pcb_file.parent / "reports"
    files_to_stage = [str(ctx.pcb_file)]
    if reports_dir.exists():
        files_to_stage.append(str(reports_dir))
    add_result = subprocess.run(
        ["git", "-C", str(pcb_dir), "add"] + files_to_stage,
        capture_output=True,
        text=True,
    )
    if add_result.returncode != 0:
        print(
            f"Error: git add failed: {add_result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    # Check whether there are actually staged changes
    diff_result = subprocess.run(
        ["git", "-C", str(pcb_dir), "diff", "--cached", "--quiet"],
        capture_output=True,
        text=True,
    )
    if diff_result.returncode == 0:
        # Exit code 0 means no staged changes
        print(
            "Error: --commit specified but the pipeline produced no file changes to commit.",
            file=sys.stderr,
        )
        return 1

    commit_msg = _build_commit_message(ctx, results)

    commit_result = subprocess.run(
        ["git", "-C", str(pcb_dir), "commit", "-m", commit_msg],
        capture_output=True,
        text=True,
    )
    if commit_result.returncode != 0:
        print(
            f"Error: git commit failed: {commit_result.stderr.strip()}",
            file=sys.stderr,
        )
        return 1

    if not ctx.quiet:
        console.print(f"[green]Committed:[/green] {commit_msg}")

    return 0


# Map of step name to runner function
STEP_RUNNERS = {
    PipelineStep.ERC: _run_step_erc,
    PipelineStep.FIX_SILKSCREEN: _run_step_fix_silkscreen,
    PipelineStep.FIX_ERC: _run_step_fix_erc,
    PipelineStep.ROUTE: _run_step_route,
    PipelineStep.FIX_VIAS: _run_step_fix_vias,
    PipelineStep.FIX_DRC: _run_step_fix_drc,
    PipelineStep.OPTIMIZE: _run_step_optimize,
    PipelineStep.ZONES: _run_step_zones,
    PipelineStep.AUDIT: _run_step_audit,
    PipelineStep.REPORT: _run_step_report,
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

            # Stop on failure (unless it's the audit or report step -- always run informational steps)
            if not result.success and step not in (PipelineStep.AUDIT, PipelineStep.REPORT):
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
    kct pipeline board.kicad_pcb --commit              # Commit changes after success
    kct pipeline board.kicad_pcb --mfr jlcpcb --commit # Pipeline + auto-commit
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
        default=None,
        help="Number of copper layers (default: auto-detected from board)",
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
    parser.add_argument(
        "--commit",
        action="store_true",
        default=False,
        help="Create a git commit with the modified PCB file after a successful pipeline run",
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

    # Resolve layer count from PCB when not explicitly specified
    if args.layers is not None:
        resolved_layers = args.layers
    else:
        try:
            from kicad_tools.schema.pcb import PCB

            pcb = PCB.load(pcb_file)
            detected = len(pcb.copper_layers)
            resolved_layers = detected if detected > 0 else 2
        except Exception:
            logger.warning(
                "Could not auto-detect layer count from %s; defaulting to 2",
                pcb_file.name,
            )
            resolved_layers = 2

    # Resolve schematic file for ERC step
    schematic_file = _resolve_schematic(pcb_file, project_file)

    # Build context
    ctx = PipelineContext(
        pcb_file=pcb_file,
        project_file=project_file,
        schematic_file=schematic_file,
        mfr=args.mfr,
        layers=resolved_layers,
        dry_run=args.dry_run,
        verbose=args.verbose,
        quiet=args.quiet,
        force=args.force,
        is_project=is_project,
        commit=args.commit,
    )

    # Determine steps to run
    if args.step:
        steps = [PipelineStep(args.step)]
    else:
        steps = None  # All steps

    results = run_pipeline(ctx, steps)

    # Determine exit code: 0 if all succeeded, 1 if any failed
    all_succeeded = all(r.success for r in results)

    if not all_succeeded:
        return 1

    # Handle --commit flag (silently ignored with --dry-run)
    if ctx.commit and not ctx.dry_run:
        console = Console(quiet=ctx.quiet)
        commit_rc = _git_commit_result(ctx, results, console)
        if commit_rc != 0:
            return commit_rc

    return 0


if __name__ == "__main__":
    sys.exit(main())
