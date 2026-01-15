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
import subprocess
import sys
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

from rich.console import Console
from rich.panel import Panel
from rich.progress import Progress, SpinnerColumn, TextColumn

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
    schematic_file: Path | None = None
    pcb_file: Path | None = None
    routed_pcb_file: Path | None = None
    mfr: str = "jlcpcb"
    verbose: bool = False
    dry_run: bool = False
    quiet: bool = False


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
        except Exception:
            pass  # Fall back to directory search

    # Fall back to finding files in directory
    if not schematic:
        sch_files = list(directory.glob("*.kicad_sch"))
        # Filter out backup files
        sch_files = [f for f in sch_files if not f.name.endswith("-bak.kicad_sch")]
        if sch_files:
            schematic = sch_files[0]

    if not pcb:
        pcb_files = list(directory.glob("*.kicad_pcb"))
        # Filter out routed and backup files
        pcb_files = [
            f
            for f in pcb_files
            if not f.name.endswith("_routed.kicad_pcb") and not f.name.endswith("-bak.kicad_pcb")
        ]
        if pcb_files:
            pcb = pcb_files[0]

    return schematic, pcb


def _run_python_script(script_path: Path, cwd: Path, verbose: bool = False) -> tuple[bool, str]:
    """Run a Python generator script.

    Args:
        script_path: Path to the Python script
        cwd: Working directory for execution
        verbose: Whether to show script output

    Returns:
        Tuple of (success, output/error message)
    """
    try:
        result = subprocess.run(
            [sys.executable, str(script_path)],
            cwd=str(cwd),
            capture_output=not verbose,
            text=True,
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
        # Check if schematic already exists
        if ctx.schematic_file and ctx.schematic_file.exists():
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
        # Check if PCB already exists
        if ctx.pcb_file and ctx.pcb_file.exists():
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

    if ctx.dry_run:
        return BuildResult(
            step="pcb",
            success=True,
            message=f"[dry-run] Would run: {script.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running {script.name}...")

    success, message = _run_python_script(script, ctx.project_dir, ctx.verbose)

    # Re-scan for artifacts after generation
    _, pcb = _find_artifacts(ctx.project_dir, ctx.spec_file)

    return BuildResult(
        step="pcb",
        success=success,
        message=message,
        output_file=pcb,
    )


def _run_step_route(ctx: BuildContext, console: Console) -> BuildResult:
    """Run autorouting step."""
    # Check if a routed PCB already exists (e.g., from generate_design.py)
    # This prevents double-routing when a script already handled routing
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
        if ctx.dry_run:
            return BuildResult(
                step="route",
                success=True,
                message=f"[dry-run] Would run: {route_script.name}",
            )

        if not ctx.quiet:
            console.print(f"  Running {route_script.name}...")

        success, message = _run_python_script(route_script, ctx.project_dir, ctx.verbose)

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

    if ctx.dry_run:
        return BuildResult(
            step="route",
            success=True,
            message=f"[dry-run] Would run: kct route {ctx.pcb_file.name} -o {output_file.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running autorouter on {ctx.pcb_file.name}...")

    try:
        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "route",
            str(ctx.pcb_file),
            "-o",
            str(output_file),
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

    # Find existing artifacts
    schematic, pcb = _find_artifacts(project_dir, spec_file)

    # Create build context
    ctx = BuildContext(
        project_dir=project_dir,
        spec_file=spec_file,
        schematic_file=schematic,
        pcb_file=pcb,
        mfr=args.mfr,
        verbose=args.verbose,
        dry_run=args.dry_run,
        quiet=args.quiet,
    )

    # Print build header
    if not args.quiet:
        project_name = project_dir.name
        if spec_file:
            try:
                from kicad_tools.spec import load_spec

                spec = load_spec(spec_file)
                project_name = spec.project.name
            except Exception:
                pass

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
