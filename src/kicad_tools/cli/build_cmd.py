"""
Build command implementation for end-to-end workflow from spec to manufacturable design.

Orchestrates the full build pipeline:
1. Load project spec (.kct file)
2. Run schematic generator (if exists)
3. Run PCB generator (if exists)
4. Run autorouter
5. Run verification (DRC, audit)
"""

from __future__ import annotations

import argparse
import logging
import math
import re
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

from rich.console import Console

logger = logging.getLogger(__name__)
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

if TYPE_CHECKING:
    from kicad_tools.spec.schema import ProjectSpec

__all__ = ["main"]


class BuildStep(str, Enum):
    """Build pipeline steps."""

    SCHEMATIC = "schematic"
    PCB = "pcb"
    ROUTE = "route"
    VERIFY = "verify"
    ALL = "all"


@dataclass
class BuildResult:
    """Result of a build step."""

    step: str
    success: bool
    message: str
    output_file: Path | None = None


@dataclass
class BuildContext:
    """Context for a build operation."""

    project_dir: Path
    spec_file: Path | None
    spec: ProjectSpec | None = None
    schematic_file: Path | None = None
    pcb_file: Path | None = None
    routed_pcb_file: Path | None = None
    mfr: str = "jlcpcb"
    verbose: bool = False
    dry_run: bool = False
    quiet: bool = False
    force: bool = False
    _executed_scripts: set[Path] | None = None

    def mark_script_executed(self, script: Path) -> None:
        """Mark a script as having been executed."""
        if self._executed_scripts is None:
            self._executed_scripts = set()
        self._executed_scripts.add(script.resolve())

    def was_script_executed(self, script: Path) -> bool:
        """Check if a script has already been executed."""
        if self._executed_scripts is None:
            return False
        return script.resolve() in self._executed_scripts


def _find_spec_file(directory: Path) -> Path | None:
    """Find a .kct file in the given directory."""
    kct_files = list(directory.glob("*.kct"))
    if len(kct_files) == 1:
        return kct_files[0]
    elif len(kct_files) > 1:
        # Prefer 'project.kct' if it exists
        project_kct = directory / "project.kct"
        if project_kct.exists():
            return project_kct
        # Otherwise return the first one found
        return kct_files[0]
    return None


def _find_generator_script(directory: Path, script_type: str) -> Path | None:
    """Find a generator script in the project directory.

    Args:
        directory: Project directory to search
        script_type: Type of script ('schematic', 'pcb', 'design')

    Returns:
        Path to the generator script if found
    """
    candidates = [
        f"generate_{script_type}.py",
        f"gen_{script_type}.py",
        f"{script_type}_gen.py",
    ]

    # Also check for combined 'design' generator
    if script_type in ("schematic", "pcb"):
        candidates.append("generate_design.py")
        candidates.append("design.py")

    for candidate in candidates:
        script_path = directory / candidate
        if script_path.exists():
            return script_path

    return None


def _find_artifacts(directory: Path, spec_file: Path | None) -> tuple[Path | None, Path | None]:
    """Find schematic and PCB files in the project directory.

    Returns:
        Tuple of (schematic_path, pcb_path)
    """
    schematic = None
    pcb = None

    # Try to load from spec file first
    if spec_file and spec_file.exists():
        try:
            from kicad_tools.spec import load_spec

            spec = load_spec(spec_file)
            if spec.project.artifacts:
                if spec.project.artifacts.schematic:
                    sch_path = directory / spec.project.artifacts.schematic
                    if sch_path.exists():
                        schematic = sch_path
                if spec.project.artifacts.pcb:
                    pcb_path = directory / spec.project.artifacts.pcb
                    if pcb_path.exists():
                        pcb = pcb_path
        except Exception as e:
            logger.warning(
                "Failed to load artifacts from spec file %s: %s. Falling back to directory search.",
                spec_file,
                e,
            )

    # Fall back to finding files in directory (search recursively including subdirectories)
    if not schematic:
        sch_files = list(directory.glob("**/*.kicad_sch"))
        # Filter out backup files
        sch_files = [f for f in sch_files if not f.name.endswith("-bak.kicad_sch")]
        if sch_files:
            schematic = sch_files[0]

    if not pcb:
        pcb_files = list(directory.glob("**/*.kicad_pcb"))
        # Filter out routed and backup files
        pcb_files = [
            f
            for f in pcb_files
            if not f.name.endswith("_routed.kicad_pcb") and not f.name.endswith("-bak.kicad_pcb")
        ]
        if pcb_files:
            pcb = pcb_files[0]

    return schematic, pcb


def _run_python_script(
    script_path: Path,
    cwd: Path,
    verbose: bool = False,
    env_vars: dict[str, str] | None = None,
) -> tuple[bool, str]:
    """Run a Python generator script.

    Args:
        script_path: Path to the Python script
        cwd: Working directory for execution
        verbose: Whether to show script output
        env_vars: Optional additional environment variables to pass

    Returns:
        Tuple of (success, output/error message)
    """
    import os

    # Build environment with optional extra variables
    run_env = os.environ.copy()
    if env_vars:
        run_env.update(env_vars)

    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(cwd),
            capture_output=not verbose,
            text=True,
            env=run_env,
        )

        if result.returncode == 0:
            return True, f"Script {script_path.name} completed successfully"
        else:
            error_msg = result.stderr if result.stderr else f"Exit code: {result.returncode}"
            return False, f"Script {script_path.name} failed: {error_msg}"

    except Exception as e:
        return False, f"Failed to run {script_path.name}: {e}"


def _run_step_schematic(ctx: BuildContext, console: Console) -> BuildResult:
    """Run schematic generation step."""
    script = _find_generator_script(ctx.project_dir, "schematic")

    if not script:
        # Check if schematic already exists (unless force rebuild)
        if ctx.schematic_file and ctx.schematic_file.exists() and not ctx.force:
            return BuildResult(
                step="schematic",
                success=True,
                message="Schematic already exists, skipping generation",
                output_file=ctx.schematic_file,
            )
        return BuildResult(
            step="schematic",
            success=False,
            message="No schematic generator found (generate_schematic.py)",
        )

    if ctx.dry_run:
        return BuildResult(
            step="schematic",
            success=True,
            message=f"[dry-run] Would run: {script.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running {script.name}...")

    success, message = _run_python_script(script, ctx.project_dir, ctx.verbose)

    # Mark this script as executed to avoid running it again in PCB step
    ctx.mark_script_executed(script)

    # Re-scan for artifacts after generation
    schematic, _ = _find_artifacts(ctx.project_dir, ctx.spec_file)

    return BuildResult(
        step="schematic",
        success=success,
        message=message,
        output_file=schematic,
    )


def _run_step_pcb(ctx: BuildContext, console: Console) -> BuildResult:
    """Run PCB generation step."""
    script = _find_generator_script(ctx.project_dir, "pcb")

    if not script:
        # Check if PCB already exists (unless force rebuild)
        if ctx.pcb_file and ctx.pcb_file.exists() and not ctx.force:
            return BuildResult(
                step="pcb",
                success=True,
                message="PCB already exists, skipping generation",
                output_file=ctx.pcb_file,
            )
        return BuildResult(
            step="pcb",
            success=False,
            message="No PCB generator found (generate_pcb.py)",
        )

    # Skip if this script was already executed (e.g., generate_design.py ran in schematic step)
    if ctx.was_script_executed(script):
        # Re-scan for artifacts that may have been created by the earlier run
        _, pcb = _find_artifacts(ctx.project_dir, ctx.spec_file)
        return BuildResult(
            step="pcb",
            success=True,
            message=f"Script {script.name} already ran (produces both schematic and PCB)",
            output_file=pcb,
        )

    if ctx.dry_run:
        return BuildResult(
            step="pcb",
            success=True,
            message=f"[dry-run] Would run: {script.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running {script.name}...")

    success, message = _run_python_script(script, ctx.project_dir, ctx.verbose)

    # Mark this script as executed
    ctx.mark_script_executed(script)

    # Re-scan for artifacts after generation
    _, pcb = _find_artifacts(ctx.project_dir, ctx.spec_file)

    return BuildResult(
        step="pcb",
        success=success,
        message=message,
        output_file=pcb,
    )


def _parse_dimension_mm(value: str | None) -> float | None:
    """Parse a dimension string like '0.3mm' or '0.2 mm' into a float in mm.

    Args:
        value: String like "0.3mm", "0.2 mm", "0.15", or None

    Returns:
        Float value in mm, or None if parsing fails
    """
    if value is None:
        return None

    # Strip whitespace
    value = value.strip()
    if not value:
        return None

    # Try to parse numeric value with optional 'mm' suffix
    match = re.match(r"^([\d.]+)\s*(?:mm)?$", value, re.IGNORECASE)
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return None

    return None


def _get_routing_params(
    mfr: str, spec: ProjectSpec | None = None
) -> tuple[float, float, float, float, float]:
    """Get routing parameters from project spec or manufacturer rules.

    Priority:
    1. Project spec (min_trace, min_space from project.kct)
    2. Manufacturer profile defaults

    Auto-calculates grid to be compatible with clearance.
    Grid must be ≤ clearance / 2 to allow routing without DRC violations.

    Args:
        mfr: Manufacturer ID (e.g., "jlcpcb")
        spec: Optional ProjectSpec loaded from project.kct

    Returns:
        Tuple of (grid, clearance, trace_width, via_drill, via_diameter)
    """
    from kicad_tools.manufacturers import get_profile

    # Start with None values to track what we've found
    clearance: float | None = None
    trace_width: float | None = None
    via_drill: float | None = None
    via_diameter: float | None = None

    # First, try to get values from the project spec
    if spec is not None and spec.requirements is not None:
        mfr_reqs = spec.requirements.manufacturing
        if mfr_reqs is not None:
            # Parse min_space -> clearance
            if mfr_reqs.min_space is not None:
                parsed = _parse_dimension_mm(mfr_reqs.min_space)
                if parsed is not None:
                    clearance = parsed

            # Parse min_trace -> trace_width
            if mfr_reqs.min_trace is not None:
                parsed = _parse_dimension_mm(mfr_reqs.min_trace)
                if parsed is not None:
                    trace_width = parsed

            # Parse min_via -> via_diameter
            if mfr_reqs.min_via is not None:
                parsed = _parse_dimension_mm(mfr_reqs.min_via)
                if parsed is not None:
                    via_diameter = parsed

            # Parse min_drill -> via_drill
            if mfr_reqs.min_drill is not None:
                parsed = _parse_dimension_mm(mfr_reqs.min_drill)
                if parsed is not None:
                    via_drill = parsed

    # Fill in missing values from manufacturer profile
    try:
        profile = get_profile(mfr)
        rules = profile.get_design_rules(layers=2)  # Use 2-layer defaults

        if clearance is None:
            clearance = rules.min_clearance_mm
        if trace_width is None:
            trace_width = rules.min_trace_width_mm
        if via_drill is None:
            via_drill = rules.min_via_drill_mm
        if via_diameter is None:
            via_diameter = rules.min_via_diameter_mm

    except Exception:
        # Fall back to safe defaults if manufacturer lookup fails
        if clearance is None:
            clearance = 0.15
        if trace_width is None:
            trace_width = 0.15
        if via_drill is None:
            via_drill = 0.3
        if via_diameter is None:
            via_diameter = 0.6

    # Auto-calculate grid: must be ≤ clearance / 2 for DRC compliance
    # Round DOWN to a clean value (0.05mm increments) to ensure compliance
    grid = clearance / 2
    grid = max(0.05, math.floor(grid / 0.05) * 0.05)  # Round DOWN to 0.05mm, min 0.05mm

    return grid, clearance, trace_width, via_drill, via_diameter


def _run_step_route(ctx: BuildContext, console: Console) -> BuildResult:
    """Run autorouting step."""
    # Check if a routed PCB already exists (e.g., from generate_design.py)
    # This prevents double-routing when a script already handled routing
    # Skip these checks if force rebuild is requested
    if not ctx.force:
        # Search in the same directory as the unrouted PCB, or recursively in project
        if ctx.pcb_file and ctx.pcb_file.exists():
            # Look for routed file alongside the unrouted PCB
            expected_routed = ctx.pcb_file.with_stem(ctx.pcb_file.stem + "_routed")
            if expected_routed.exists():
                if expected_routed.stat().st_mtime >= ctx.pcb_file.stat().st_mtime:
                    if not ctx.quiet:
                        console.print(f"  Found existing routed PCB: {expected_routed.name}")
                    return BuildResult(
                        step="route",
                        success=True,
                        message="Using existing routed PCB (newer than unrouted)",
                        output_file=expected_routed,
                    )
        else:
            # No unrouted PCB, search recursively for any routed PCB
            routed_files = list(ctx.project_dir.glob("**/*_routed.kicad_pcb"))
            if routed_files:
                routed_file = routed_files[0]
                if not ctx.quiet:
                    console.print(f"  Found existing routed PCB: {routed_file.name}")
                return BuildResult(
                    step="route",
                    success=True,
                    message="Using existing routed PCB",
                    output_file=routed_file,
                )

    # First check for a route script
    route_script = ctx.project_dir / "route_demo.py"
    if not route_script.exists():
        route_script = ctx.project_dir / "route.py"

    if route_script.exists():
        # Get routing parameters from project.kct to pass as environment variables
        # This allows custom route scripts to optionally use project.kct settings
        grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params(
            ctx.mfr, ctx.spec
        )
        route_env_vars = {
            "KCT_ROUTE_GRID": str(grid),
            "KCT_ROUTE_CLEARANCE": str(clearance),
            "KCT_ROUTE_TRACE_WIDTH": str(trace_width),
            "KCT_ROUTE_VIA_DRILL": str(via_drill),
            "KCT_ROUTE_VIA_DIAMETER": str(via_diameter),
        }

        if ctx.dry_run:
            return BuildResult(
                step="route",
                success=True,
                message=f"[dry-run] Would run: {route_script.name}",
            )

        if not ctx.quiet:
            console.print(f"  Running {route_script.name}...")
            if ctx.verbose:
                console.print("    Routing params from project.kct:")
                console.print(f"      Grid: {grid}mm, Clearance: {clearance}mm")
                console.print(f"      Trace width: {trace_width}mm")
                console.print(f"      Via: {via_drill}mm drill, {via_diameter}mm diameter")

        success, message = _run_python_script(
            route_script, ctx.project_dir, ctx.verbose, env_vars=route_env_vars
        )

        # Find routed PCB
        routed_files = list(ctx.project_dir.glob("*_routed.kicad_pcb"))
        output_file = routed_files[0] if routed_files else None

        return BuildResult(
            step="route",
            success=success,
            message=message,
            output_file=output_file,
        )

    # Fall back to kct route command
    if not ctx.pcb_file:
        return BuildResult(
            step="route",
            success=False,
            message="No PCB file found to route",
        )

    output_file = ctx.pcb_file.with_stem(ctx.pcb_file.stem + "_routed")

    # Get routing parameters from project spec (preferred) or manufacturer rules (fallback)
    grid, clearance, trace_width, via_drill, via_diameter = _get_routing_params(ctx.mfr, ctx.spec)

    if ctx.dry_run:
        return BuildResult(
            step="route",
            success=True,
            message=(
                f"[dry-run] Would run: kct route {ctx.pcb_file.name} "
                f"--grid {grid} --clearance {clearance}"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Running autorouter on {ctx.pcb_file.name}...")
        if ctx.verbose:
            console.print(f"    Grid: {grid}mm, Clearance: {clearance}mm")

    try:
        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(ctx.pcb_file),
            "-o",
            str(output_file),
            "--grid",
            str(grid),
            "--clearance",
            str(clearance),
            "--trace-width",
            str(trace_width),
            "--via-drill",
            str(via_drill),
            "--via-diameter",
            str(via_diameter),
        ]

        if ctx.quiet:
            cmd.append("--quiet")

        result = subprocess.run(
            cmd,
            cwd=str(ctx.project_dir),
            capture_output=not ctx.verbose,
            text=True,
        )

        if result.returncode == 0:
            return BuildResult(
                step="route",
                success=True,
                message="Routing completed successfully",
                output_file=output_file if output_file.exists() else None,
            )
        else:
            error_msg = result.stderr if result.stderr else f"Exit code: {result.returncode}"
            return BuildResult(
                step="route",
                success=False,
                message=f"Routing failed: {error_msg}",
            )

    except Exception as e:
        return BuildResult(
            step="route",
            success=False,
            message=f"Routing failed: {e}",
        )


def _run_step_verify(ctx: BuildContext, console: Console) -> BuildResult:
    """Run verification step (DRC + audit)."""
    # Find the PCB to verify (prefer routed version)
    pcb_to_verify = ctx.routed_pcb_file or ctx.pcb_file

    if not pcb_to_verify or not pcb_to_verify.exists():
        return BuildResult(
            step="verify",
            success=False,
            message="No PCB file found to verify",
        )

    if ctx.dry_run:
        return BuildResult(
            step="verify",
            success=True,
            message=f"[dry-run] Would run: kct check {pcb_to_verify.name} --mfr {ctx.mfr}",
        )

    if not ctx.quiet:
        console.print(f"  Running DRC check on {pcb_to_verify.name}...")

    # Run DRC check
    try:
        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(pcb_to_verify),
            "--mfr",
            ctx.mfr,
        ]

        result = subprocess.run(
            cmd,
            cwd=str(ctx.project_dir),
            capture_output=True,
            text=True,
        )

        drc_success = result.returncode == 0
        drc_message = "DRC passed" if drc_success else "DRC found issues"

        if ctx.verbose and result.stdout:
            console.print(result.stdout)

        # Also run validate --sync if we have both schematic and PCB
        sync_message = ""
        if ctx.schematic_file and ctx.schematic_file.exists():
            if not ctx.quiet:
                console.print("  Running schematic-PCB sync check...")

            sync_cmd = [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "validate",
                "--sync",
                str(ctx.schematic_file),
                str(pcb_to_verify),
            ]

            sync_result = subprocess.run(
                sync_cmd,
                cwd=str(ctx.project_dir),
                capture_output=True,
                text=True,
            )

            if sync_result.returncode != 0:
                sync_message = " (sync check found mismatches)"
            else:
                sync_message = " (sync OK)"
        else:
            # Sync check was skipped - determine why and inform user
            spec_defines_schematic = (
                ctx.spec and ctx.spec.project.artifacts and ctx.spec.project.artifacts.schematic
            )

            if spec_defines_schematic:
                # Spec defines a schematic but it wasn't found - always warn
                expected_path = ctx.spec.project.artifacts.schematic
                if not ctx.quiet:
                    console.print(
                        f"  [yellow]⚠[/yellow] Skipping sync check: "
                        f"schematic '{expected_path}' not found"
                    )
                sync_message = " (sync skipped: schematic not found)"
            elif ctx.verbose:
                # No schematic defined in spec and verbose mode - inform user
                console.print("  [dim]Skipping sync check: no schematic available[/dim]")

        return BuildResult(
            step="verify",
            success=drc_success,
            message=f"{drc_message}{sync_message}",
            output_file=pcb_to_verify,
        )

    except Exception as e:
        return BuildResult(
            step="verify",
            success=False,
            message=f"Verification failed: {e}",
        )


def main(argv: list[str] | None = None) -> int:
    """Main entry point for kct build command."""
    parser = argparse.ArgumentParser(
        prog="kct build",
        description="Build from spec to manufacturable design",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    kct build                           # Build from .kct in current directory
    kct build project.kct               # Build from specific spec file
    kct build --step schematic          # Run only schematic generation
    kct build --step verify --mfr jlcpcb # Run only verification for JLCPCB
    kct build --dry-run                 # Preview what would be done
    kct build --force                   # Rebuild even if outputs exist
        """,
    )

    parser.add_argument(
        "spec",
        nargs="?",
        help="Path to .kct file or project directory (default: current directory)",
    )
    parser.add_argument(
        "--step",
        "-s",
        choices=["schematic", "pcb", "route", "verify", "all"],
        default="all",
        help="Run specific step or all (default: all)",
    )
    parser.add_argument(
        "--mfr",
        "-m",
        choices=["jlcpcb", "pcbway", "oshpark", "seeed"],
        default="jlcpcb",
        help="Target manufacturer for verification (default: jlcpcb)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Preview build steps without executing",
    )
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Show detailed output",
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
        help="Force rebuild, ignoring existing outputs and timestamp checks",
    )

    args = parser.parse_args(argv)
    console = Console(quiet=args.quiet)

    # Determine project directory and spec file
    if args.spec:
        spec_path = Path(args.spec).resolve()
        if spec_path.is_file():
            project_dir = spec_path.parent
            spec_file = spec_path
        else:
            project_dir = spec_path
            spec_file = _find_spec_file(project_dir)
    else:
        project_dir = Path.cwd()
        spec_file = _find_spec_file(project_dir)

    if not project_dir.exists():
        console.print(f"[red]Error:[/red] Directory not found: {project_dir}")
        return 1

    # Load the spec file if available
    spec = None
    if spec_file and spec_file.exists():
        try:
            from kicad_tools.spec import load_spec

            spec = load_spec(spec_file)
        except Exception as e:
            logger.warning(
                "Failed to load spec file %s: %s. Continuing without spec.",
                spec_file,
                e,
            )

    # Find existing artifacts
    schematic, pcb = _find_artifacts(project_dir, spec_file)

    # Create build context
    ctx = BuildContext(
        project_dir=project_dir,
        spec_file=spec_file,
        spec=spec,
        schematic_file=schematic,
        pcb_file=pcb,
        mfr=args.mfr,
        verbose=args.verbose,
        dry_run=args.dry_run,
        quiet=args.quiet,
        force=args.force,
    )

    # Print build header
    if not args.quiet:
        project_name = spec.project.name if spec else project_dir.name

        console.print(
            Panel.fit(
                f"[bold]Building:[/bold] {project_name}\n"
                f"[dim]Directory:[/dim] {project_dir}\n"
                f"[dim]Manufacturer:[/dim] {args.mfr}",
                title="kct build",
            )
        )

    # Determine steps to run
    if args.step == "all":
        steps = [BuildStep.SCHEMATIC, BuildStep.PCB, BuildStep.ROUTE, BuildStep.VERIFY]
    else:
        steps = [BuildStep(args.step)]

    # Run build steps
    results: list[BuildResult] = []

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        console=console,
        disable=args.quiet,
    ) as progress:
        for step in steps:
            task = progress.add_task(f"[cyan]{step.value}[/cyan]...", total=None)

            if step == BuildStep.SCHEMATIC:
                result = _run_step_schematic(ctx, console)
                if result.output_file:
                    ctx.schematic_file = result.output_file

            elif step == BuildStep.PCB:
                result = _run_step_pcb(ctx, console)
                if result.output_file:
                    ctx.pcb_file = result.output_file

            elif step == BuildStep.ROUTE:
                result = _run_step_route(ctx, console)
                if result.output_file:
                    ctx.routed_pcb_file = result.output_file

            elif step == BuildStep.VERIFY:
                result = _run_step_verify(ctx, console)

            else:
                result = BuildResult(step=step.value, success=False, message="Unknown step")

            results.append(result)
            progress.remove_task(task)

            # Print step result
            if not args.quiet:
                status = "[green]OK[/green]" if result.success else "[red]FAIL[/red]"
                console.print(f"  [{status}] {step.value}: {result.message}")

            # Stop on failure unless just verifying
            if not result.success and step != BuildStep.VERIFY:
                break

    # Print summary
    if not args.quiet:
        console.print()
        success_count = sum(1 for r in results if r.success)
        total_count = len(results)

        if success_count == total_count:
            console.print(
                f"[green]Build completed successfully[/green] ({success_count}/{total_count} steps)"
            )

            # Show output files
            if ctx.routed_pcb_file and ctx.routed_pcb_file.exists():
                console.print(f"\n[dim]Output:[/dim] {ctx.routed_pcb_file}")
            elif ctx.pcb_file and ctx.pcb_file.exists():
                console.print(f"\n[dim]Output:[/dim] {ctx.pcb_file}")
        else:
            console.print(
                f"[red]Build failed[/red] ({success_count}/{total_count} steps succeeded)"
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
