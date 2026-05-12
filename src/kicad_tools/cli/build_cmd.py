"""
Build command implementation for end-to-end workflow from spec to manufacturable design.

Orchestrates the full build pipeline:
1. Load project spec (.kct file)
2. Run schematic generator (if exists)
3. Run ERC on the schematic and persist ``erc_report.json``
4. Run PCB generator (if exists)
5. Run autorouter
6. Run verification (DRC, audit)
7. Export manufacturing package (Gerbers, BOM, CPL)
"""

from __future__ import annotations

import argparse
import logging
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
    ERC = "erc"
    PCB = "pcb"
    SYNC = "sync"
    OUTLINE = "outline"
    PLACEMENT = "placement"
    ZONES = "zones"
    SILKSCREEN = "silkscreen"
    ROUTE = "route"
    STITCH = "stitch"
    VERIFY = "verify"
    EXPORT = "export"
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
    output_dir: Path | None = None
    mfr: str = "jlcpcb"
    verbose: bool = False
    dry_run: bool = False
    quiet: bool = False
    force: bool = False
    optimize_placement: bool = False
    smoke_check: bool = True
    _executed_scripts: set[Path] | None = None
    _kicad_cli_warning_emitted: bool = False

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


def _generator_candidates(script_type: str) -> list[str]:
    """Return the list of candidate filenames for a generator script type.

    Args:
        script_type: Type of script ('schematic', 'pcb', 'design')

    Returns:
        List of candidate filenames in search order
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

    return candidates


def _find_generator_script(directory: Path, script_type: str) -> Path | None:
    """Find a generator script in the project directory.

    Args:
        directory: Project directory to search
        script_type: Type of script ('schematic', 'pcb', 'design')

    Returns:
        Path to the generator script if found
    """
    for candidate in _generator_candidates(script_type):
        script_path = directory / candidate
        if script_path.exists():
            return script_path

    return None


def _format_no_generator_message(script_type: str, directory: Path) -> str:
    """Format an informative error message when no generator script is found.

    Args:
        script_type: Type of script ('schematic' or 'pcb')
        directory: The project directory that was searched

    Returns:
        Multi-line error message with candidate list and guidance
    """
    candidates = _generator_candidates(script_type)
    candidate_list = "\n".join(f"  - {c}" for c in candidates)
    return (
        f"No {script_type} generator found in {directory}\n"
        f"Searched for:\n"
        f"{candidate_list}\n"
        f"Hint: create one of these files, or use a combined generator "
        f"(see boards/00-simple-led/generate_design.py for an example)"
    )


def _get_expected_pcb_artifact(ctx: BuildContext) -> str | None:
    """Get the expected PCB artifact path from the project spec.

    Returns:
        The expected PCB path string if defined in spec, None otherwise.
    """
    if ctx.spec and ctx.spec.project.artifacts and ctx.spec.project.artifacts.pcb:
        return ctx.spec.project.artifacts.pcb
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
    script_args: list[str] | None = None,
) -> tuple[bool, str]:
    """Run a Python generator script.

    Args:
        script_path: Path to the Python script
        cwd: Working directory for execution
        verbose: Whether to show script output
        env_vars: Optional additional environment variables to pass
        script_args: Optional positional arguments to pass to the script

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
            [sys.executable, str(script_path)] + (script_args or []),
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
            message=_format_no_generator_message("schematic", ctx.project_dir),
        )

    if ctx.dry_run:
        return BuildResult(
            step="schematic",
            success=True,
            message=f"[dry-run] Would run: {script.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running {script.name}...")

    script_args = [str(ctx.output_dir)] if ctx.output_dir else None
    success, message = _run_python_script(
        script, ctx.project_dir, ctx.verbose, script_args=script_args
    )

    # Mark this script as executed to avoid running it again in PCB step
    ctx.mark_script_executed(script)

    # Re-scan for artifacts after generation (check output dir first, then project dir)
    search_dir = ctx.output_dir if ctx.output_dir else ctx.project_dir
    schematic, _ = _find_artifacts(search_dir, ctx.spec_file)
    if not schematic and ctx.output_dir:
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
            message=_format_no_generator_message("pcb", ctx.project_dir),
        )

    # Skip if this script was already executed (e.g., generate_design.py ran in schematic step)
    if ctx.was_script_executed(script):
        # Re-scan for artifacts that may have been created by the earlier run
        search_dir = ctx.output_dir if ctx.output_dir else ctx.project_dir
        _, pcb = _find_artifacts(search_dir, ctx.spec_file)
        if not pcb and ctx.output_dir:
            _, pcb = _find_artifacts(ctx.project_dir, ctx.spec_file)

        # Verify that a PCB was actually created
        if pcb and pcb.exists():
            return BuildResult(
                step="pcb",
                success=True,
                message=f"Script {script.name} already ran (produces both schematic and PCB)",
                output_file=pcb,
            )
        else:
            # Script ran but didn't produce a PCB - report the failure clearly
            expected_pcb = _get_expected_pcb_artifact(ctx)
            if expected_pcb:
                return BuildResult(
                    step="pcb",
                    success=False,
                    message=f"Script {script.name} ran but expected artifact '{expected_pcb}' was not created. "
                    f"PCB generation may not be implemented.",
                )
            else:
                return BuildResult(
                    step="pcb",
                    success=False,
                    message=f"Script {script.name} ran but no PCB file was created. "
                    f"PCB generation may not be implemented.",
                )

    if ctx.dry_run:
        return BuildResult(
            step="pcb",
            success=True,
            message=f"[dry-run] Would run: {script.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running {script.name}...")

    script_args = [str(ctx.output_dir)] if ctx.output_dir else None
    success, message = _run_python_script(
        script, ctx.project_dir, ctx.verbose, script_args=script_args
    )

    # Mark this script as executed
    ctx.mark_script_executed(script)

    # Re-scan for artifacts after generation (check output dir first, then project dir)
    search_dir = ctx.output_dir if ctx.output_dir else ctx.project_dir
    _, pcb = _find_artifacts(search_dir, ctx.spec_file)
    if not pcb and ctx.output_dir:
        _, pcb = _find_artifacts(ctx.project_dir, ctx.spec_file)

    # Verify that a PCB was actually created (even if script reported success)
    if success and (not pcb or not pcb.exists()):
        expected_pcb = _get_expected_pcb_artifact(ctx)
        if expected_pcb:
            return BuildResult(
                step="pcb",
                success=False,
                message=f"Script {script.name} completed but expected artifact '{expected_pcb}' was not created. "
                f"PCB generation may not be implemented.",
            )
        else:
            return BuildResult(
                step="pcb",
                success=False,
                message=f"Script {script.name} completed but no PCB file was created. "
                f"PCB generation may not be implemented.",
            )

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
    Grid must be <= clearance / 2 to allow routing without DRC violations.

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

    # Auto-calculate grid: must be <= clearance / 2 so that worst-case
    # grid-quantisation error stays within DRC clearance margin.
    # Snap to a known-good grid value that divides evenly into common
    # imperial pitches (2.54mm, 5.08mm) to avoid off-grid THT pads.
    max_grid = clearance / 2
    # Candidate grids ordered coarsest-first; includes 0.127mm (5 mil)
    # which divides evenly into 2.54mm (20x) and 5.08mm (40x).
    _GRID_CANDIDATES = [0.25, 0.127, 0.1, 0.065, 0.05]
    grid = 0.05  # fallback minimum
    for candidate in _GRID_CANDIDATES:
        if candidate <= max_grid:
            grid = candidate
            break

    return grid, clearance, trace_width, via_drill, via_diameter


def _run_step_outline(ctx: BuildContext, console: Console) -> BuildResult:
    """Run board outline generation step.

    Reads mechanical dimensions from the project spec and writes a closed
    polygon on Edge.Cuts to the PCB file.  Skips if:
    - No PCB file exists yet
    - No mechanical dimensions in the spec
    - An outline already exists on Edge.Cuts
    """
    if not ctx.pcb_file or not ctx.pcb_file.exists():
        return BuildResult(
            step="outline",
            success=True,
            message="No PCB file found, skipping outline generation",
        )

    # Extract mechanical dimensions from spec
    width_mm = None
    height_mm = None

    if ctx.spec and ctx.spec.requirements and ctx.spec.requirements.mechanical:
        mech = ctx.spec.requirements.mechanical
        if mech.dimensions:
            width_mm = _parse_dimension_mm(mech.dimensions.get("width"))
            height_mm = _parse_dimension_mm(mech.dimensions.get("height"))

    if width_mm is None or height_mm is None:
        return BuildResult(
            step="outline",
            success=True,
            message="No mechanical dimensions in spec, skipping outline generation",
        )

    if ctx.dry_run:
        return BuildResult(
            step="outline",
            success=True,
            message=f"[dry-run] Would add {width_mm}x{height_mm}mm outline on Edge.Cuts",
        )

    if not ctx.quiet:
        console.print(f"  Adding board outline: {width_mm}x{height_mm}mm")

    try:
        from kicad_tools.pcb.editor import PCBEditor

        editor = PCBEditor(str(ctx.pcb_file))

        if editor.has_board_outline():
            if not ctx.quiet:
                console.print("  Outline already exists on Edge.Cuts, skipping")
            return BuildResult(
                step="outline",
                success=True,
                message="Board outline already exists, skipping",
                output_file=ctx.pcb_file,
            )

        nodes = editor.add_board_outline(width_mm, height_mm)

        if not nodes:
            return BuildResult(
                step="outline",
                success=True,
                message="Board outline already exists, skipping",
                output_file=ctx.pcb_file,
            )

        # Place mounting holes if specified
        mounting_msg = ""
        if (
            ctx.spec
            and ctx.spec.requirements
            and ctx.spec.requirements.mechanical
            and ctx.spec.requirements.mechanical.mounting_holes
        ):
            holes = ctx.spec.requirements.mechanical.mounting_holes
            hole_count = 0
            for hole in holes:
                hx = _parse_dimension_mm(hole.x)
                hy = _parse_dimension_mm(hole.y)
                diameter = _parse_dimension_mm(hole.diameter)
                if hx is not None and hy is not None and diameter is not None:
                    # Offset mounting hole positions relative to the board origin
                    abs_x = 100.0 + hx
                    abs_y = 100.0 + hy
                    editor.place_component(
                        f"H{hole_count + 1}",
                        abs_x,
                        abs_y,
                    )
                    hole_count += 1
            if hole_count > 0:
                mounting_msg = f" with {hole_count} mounting hole(s)"

        editor.save()

        return BuildResult(
            step="outline",
            success=True,
            message=f"Added {width_mm}x{height_mm}mm outline on Edge.Cuts{mounting_msg}",
            output_file=ctx.pcb_file,
        )

    except Exception as e:
        return BuildResult(
            step="outline",
            success=False,
            message=f"Outline generation failed: {e}",
        )


def _run_step_placement(ctx: BuildContext, console: Console) -> BuildResult:
    """Run placement optimization step.

    This step is opt-in: it only runs when ``--optimize-placement`` is passed.
    When enabled it invokes ``kct optimize-placement`` as a subprocess so that
    the existing PCB component positions are used as the starting layout and the
    optimizer refines them in-place.
    """
    if not ctx.optimize_placement:
        return BuildResult(
            step="placement",
            success=True,
            message="Placement optimization not requested, skipping",
        )

    if not ctx.pcb_file or not ctx.pcb_file.exists():
        return BuildResult(
            step="placement",
            success=True,
            message="No PCB file found, skipping placement optimization",
        )

    # Determine output path: optimise in-place (overwrite the PCB file) so the
    # subsequent routing step picks up the improved positions.
    output_path = ctx.pcb_file

    if ctx.dry_run:
        return BuildResult(
            step="placement",
            success=True,
            message=f"[dry-run] Would run: kct optimize-placement {ctx.pcb_file.name}",
        )

    if not ctx.quiet:
        console.print(f"  Running placement optimization on {ctx.pcb_file.name}...")

    try:
        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "optimize-placement",
            str(ctx.pcb_file),
            "--strategy",
            "cmaes",
            "--max-iterations",
            "300",
            "--seed",
            "force-directed",
            "--output",
            str(output_path),
        ]

        if ctx.quiet:
            cmd.append("--quiet")
        if ctx.verbose:
            cmd.append("--verbose")

        result = subprocess.run(
            cmd,
            cwd=str(ctx.project_dir),
            capture_output=not ctx.verbose,
            text=True,
        )

        if result.returncode == 0:
            return BuildResult(
                step="placement",
                success=True,
                message="Placement optimization completed",
                output_file=output_path,
            )
        else:
            error_msg = result.stderr if result.stderr else f"Exit code: {result.returncode}"
            return BuildResult(
                step="placement",
                success=False,
                message=f"Placement optimization failed: {error_msg}",
            )

    except Exception as e:
        return BuildResult(
            step="placement",
            success=False,
            message=f"Placement optimization failed: {e}",
        )


def _run_step_silkscreen(ctx: BuildContext, console: Console) -> BuildResult:
    """Run silkscreen generation step.

    Ensures footprint reference designators are visible on silkscreen layers
    and adds board-level markings (project name, revision, date) from the
    project spec metadata.
    """
    if not ctx.pcb_file or not ctx.pcb_file.exists():
        return BuildResult(
            step="silkscreen",
            success=True,
            message="No PCB file found, skipping silkscreen generation",
        )

    if ctx.dry_run:
        return BuildResult(
            step="silkscreen",
            success=True,
            message="[dry-run] Would generate silkscreen content",
        )

    try:
        from kicad_tools.silkscreen.generator import SilkscreenGenerator

        gen = SilkscreenGenerator(ctx.pcb_file)

        # Step 1: Ensure ref des are visible
        ref_result = gen.ensure_ref_des_visible()

        # Step 2: Add board markings from spec metadata
        mark_result = None
        name = None
        revision = None
        date_str = None

        if ctx.spec and ctx.spec.project:
            name = ctx.spec.project.name
            revision = ctx.spec.project.revision
            if ctx.spec.project.created:
                date_str = str(ctx.spec.project.created)

        if name:
            mark_result = gen.add_board_markings(
                name=name,
                revision=revision,
                date=date_str,
            )

        gen.save()

        # Build summary message
        parts = []
        if ref_result.refs_unhidden:
            parts.append(f"{ref_result.refs_unhidden} ref(s) unhidden")
        if mark_result and mark_result.markings_added:
            parts.append(f"{mark_result.markings_added} marking(s) added")
        if mark_result and mark_result.markings_skipped:
            parts.append(f"{mark_result.markings_skipped} marking(s) already present")

        if parts:
            message = "Silkscreen: " + ", ".join(parts)
        else:
            message = "Silkscreen: no changes needed"

        if not ctx.quiet:
            for msg in ref_result.messages:
                console.print(f"    {msg}")
            if mark_result:
                for msg in mark_result.messages:
                    console.print(f"    {msg}")

        return BuildResult(
            step="silkscreen",
            success=True,
            message=message,
            output_file=ctx.pcb_file,
        )

    except Exception as e:
        return BuildResult(
            step="silkscreen",
            success=False,
            message=f"Silkscreen generation failed: {e}",
        )


def _run_step_zones(ctx: BuildContext, console: Console) -> BuildResult:
    """Run automatic zone creation for power and ground nets.

    Identifies pour nets (POWER and GROUND) via net classification and
    creates copper zone definitions on the PCB before routing.  Layer
    assignment is stackup-aware: on 4-layer boards GND goes on In1.Cu
    and power nets are distributed across In2.Cu / F.Cu.

    Zones are *defined* here (unfilled polygons).  Filling happens later
    after routing -- ``kct route`` calls :func:`route_cmd._fill_zones_after_route`
    once routing succeeds, and :func:`pipeline_cmd._run_step_zones` performs
    the same fill via ``kct zones fill``.  ``kct export`` also runs a
    safety-net fill if the PCB still has unfilled zones at export time
    (see :func:`export.gerber.GerberExporter._export_gerbers`).

    All-power-board guard (issue #2740)
    -----------------------------------

    When *every* net on the board classifies as POWER or GROUND (e.g.
    board 01-voltage-divider with VIN/VOUT/GND only), zone creation is
    **skipped** entirely so the router can route those nets as ordinary
    signals.  If zones were created instead, ``kct route``'s
    ``_auto_skip_pour_nets`` would auto-skip every net (each has a zone),
    ``nets_to_route`` would drop to 0, the router would report 100%
    completion trivially, and the build would silently ship a PCB with
    zero copper segments.  This guard mirrors the one in
    :func:`kicad_tools.router.auto_pour.auto_pour_if_missing` and both
    call sites share :func:`classify_pour_candidates` so the two cannot
    drift again.
    """
    if not ctx.pcb_file or not ctx.pcb_file.exists():
        return BuildResult(
            step="zones",
            success=True,
            message="No PCB file found, skipping zone creation",
        )

    try:
        from kicad_tools.router.auto_pour import classify_pour_candidates
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.zones.generator import auto_create_zones_for_pour_nets

        pcb = PCB.load(str(ctx.pcb_file))

        # Build net_names dict {net_id: net_name} for classification
        net_names: dict[int, str] = {
            net_id: net.name for net_id, net in pcb.nets.items() if net.name
        }

        if not net_names:
            return BuildResult(
                step="zones",
                success=True,
                message="No nets found in PCB, skipping zone creation",
            )

        # Classify nets and check the all-power-board guard.  This MUST
        # match the guard in router.auto_pour.auto_pour_if_missing -- both
        # call sites use the same helper to prevent drift.  See the
        # function docstring for issue-#2740 context.
        pour_nets, _signal_net_count, is_all_power_board = classify_pour_candidates(net_names)

        if not pour_nets:
            return BuildResult(
                step="zones",
                success=True,
                message="No power/ground nets detected, skipping zone creation",
            )

        if is_all_power_board:
            if not ctx.quiet:
                console.print(
                    "  All nets are power/ground "
                    "(skipping zone creation — routing as signals instead)"
                )
            return BuildResult(
                step="zones",
                success=True,
                message=(
                    "All nets are power/ground; skipping zone creation so the "
                    "router routes them as signals (see issue #2740)"
                ),
                output_file=ctx.pcb_file,
            )

        # Check for existing zones on these nets (idempotency)
        existing_zone_nets = {z.net_name for z in pcb.zones}
        new_pour_nets = [(name, cls) for name, cls in pour_nets if name not in existing_zone_nets]

        if not new_pour_nets:
            return BuildResult(
                step="zones",
                success=True,
                message="Zones already exist for all power/ground nets, skipping",
                output_file=ctx.pcb_file,
            )

        if ctx.dry_run:
            net_list = ", ".join(f"{name} ({cls.value})" for name, cls in new_pour_nets)
            return BuildResult(
                step="zones",
                success=True,
                message=f"[dry-run] Would create zones for: {net_list}",
            )

        if not ctx.quiet:
            net_list = ", ".join(f"{name} ({cls.value})" for name, cls in new_pour_nets)
            console.print(f"  Creating zones for: {net_list}")

        # Look up edge_clearance from the manufacturer profile so that zone
        # copper does not extend to the board edge.  This mirrors the
        # auto-fill logic in route_cmd.py and prevents `edge_clearance_zone`
        # DRC violations when `kct build` is used (which calls this step
        # directly instead of going through `auto_pour_if_missing`).
        from kicad_tools.router.mfr_limits import get_mfr_limits

        edge_clearance: float | None = None
        try:
            _mfr_limits = get_mfr_limits(ctx.mfr)
            if _mfr_limits.min_edge_clearance > 0:
                edge_clearance = _mfr_limits.min_edge_clearance
        except ValueError:
            pass  # Unknown manufacturer -- edge_clearance stays None

        count = auto_create_zones_for_pour_nets(
            ctx.pcb_file, new_pour_nets, edge_clearance=edge_clearance
        )

        return BuildResult(
            step="zones",
            success=True,
            message=f"Created {count} zone(s) for power/ground nets",
            output_file=ctx.pcb_file,
        )

    except Exception as e:
        logger.exception("Zone creation failed")
        return BuildResult(
            step="zones",
            success=False,
            message=f"Zone creation failed: {e}",
        )


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
            search_dirs = [ctx.output_dir, ctx.project_dir] if ctx.output_dir else [ctx.project_dir]
            routed_files: list[Path] = []
            for sd in search_dirs:
                routed_files = list(sd.glob("**/*_routed.kicad_pcb"))
                if routed_files:
                    break
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

    # Pad-grid preflight: detect off-grid pads BEFORE invoking the router.
    # This surfaces routability problems at PCB-write time with an actionable
    # message instead of a deep PADS_OFF_GRID failure inside the router.
    # See issue #2497.
    grid_for_preflight, clearance_for_preflight, *_ = _get_routing_params(ctx.mfr, ctx.spec)
    preflight_target = ctx.pcb_file
    if preflight_target and preflight_target.exists() and not ctx.dry_run:
        try:
            from kicad_tools.router.preflight import check_pad_grid_alignment

            report = check_pad_grid_alignment(
                preflight_target,
                grid_resolution=float(grid_for_preflight),
                clearance=float(clearance_for_preflight),
            )
            if not report.passed and not ctx.quiet:
                # Off-grid pads are advisory: surface the report so the user can
                # round pad coords or pick a finer grid, but don't block routing.
                # The router's own off-grid check is the authoritative gate.
                console.print(
                    f"  [warning] Pad grid preflight: "
                    f"{len(report.off_grid_pads)} off-grid pad(s) at "
                    f"grid {report.grid_resolution}mm (continuing)"
                )
                if ctx.verbose:
                    console.print(report.summary())
        except Exception as e:
            # Preflight is advisory; never block routing on a bug in the
            # check itself.  Surface the error in verbose mode.
            if ctx.verbose:
                console.print(f"  [warning] Pad grid preflight skipped: {e}")

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

        script_args = [str(ctx.output_dir)] if ctx.output_dir else None
        success, message = _run_python_script(
            route_script,
            ctx.project_dir,
            ctx.verbose,
            env_vars=route_env_vars,
            script_args=script_args,
        )

        # Find routed PCB (check output dir first, then project dir)
        output_file: Path | None = None
        for search_dir in (
            [ctx.output_dir, ctx.project_dir] if ctx.output_dir else [ctx.project_dir]
        ):
            routed_files_found = list(search_dir.glob("*_routed.kicad_pcb"))
            if routed_files_found:
                output_file = routed_files_found[0]
                break

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

    # Place routed PCB in output dir if specified, otherwise alongside input
    if ctx.output_dir:
        output_file = ctx.output_dir / (ctx.pcb_file.stem + "_routed.kicad_pcb")
    else:
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
            "auto",  # Let route command auto-select grid from pad positions
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
            # Defense-in-depth postcondition (issue #2740): the router can
            # silently "succeed" with zero segments when every multi-pad net
            # is auto-skipped for zone fill but no traces are produced.
            # Verify that the routed PCB actually contains copper when the
            # input had routable signal nets.
            postcondition = _check_route_postcondition(
                input_pcb=ctx.pcb_file,
                routed_pcb=output_file,
            )
            if postcondition is not None:
                return postcondition
            return BuildResult(
                step="route",
                success=True,
                message="Routing completed successfully",
                output_file=output_file if output_file.exists() else None,
            )
        elif result.returncode in (2, 3, 4, 5):
            # Exit codes 2-5 all produce usable output files:
            #   2 = partial routing below --min-completion threshold
            #   3 = routing meets threshold but DRC violations remain
            #   4 = partial routing with segment-segment clearance violations
            #   5 = interrupted by SIGINT with partial results saved
            # Treat as non-fatal so the build pipeline continues to verification.
            _route_warning_messages = {
                2: "Routing completed (some nets skipped for zone fill)",
                3: "Routing complete but DRC violations remain",
                4: "Partial routing with clearance violations",
                5: "Routing interrupted by user, partial results saved",
            }
            return BuildResult(
                step="route",
                success=True,
                message=_route_warning_messages[result.returncode],
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


def _count_multi_pad_signal_nets(input_pcb: Path) -> int:
    """Return the count of nets that need routing (>=2 pads, not pour-only).

    Used by :func:`_check_route_postcondition` to decide whether a
    "success exit, zero segments" result from ``kct route`` is plausible
    (no signal nets to route) or pathological (silent empty output --
    issue #2740).

    A net counts as routable if:

    * It is referenced by at least two pads in the PCB, AND
    * It is *not* a pure pour net with an existing zone (those nets
      get connected via copper fill rather than traces, so zero
      segments on them is legitimate).

    A POWER/GROUND net **without** a zone is still counted -- such nets
    must be routed as signal traces (see :func:`route_cmd._auto_skip_pour_nets`
    and the ``_pour_nets_without_zones`` carve-out from issue #1841).

    Args:
        input_pcb: Path to the unrouted PCB the router consumed.

    Returns:
        Number of nets the router was expected to produce segments for.
        Returns 0 if the file cannot be parsed (postcondition then
        cannot fire -- best-effort).
    """
    try:
        from kicad_tools.schema.pcb import PCB

        pcb = PCB.load(str(input_pcb))

        # Count pads per net.
        pad_counts: dict[int, int] = {}
        for fp in pcb.footprints:
            for pad in fp.pads:
                if pad.net_number > 0:
                    pad_counts[pad.net_number] = pad_counts.get(pad.net_number, 0) + 1

        # Determine which nets have zones (pour-handled, segments not required).
        zone_net_names = {z.net_name for z in pcb.zones if z.net_name}

        # Build net_number -> net_name lookup.
        nets_with_zones: set[int] = set()
        for net_id, net in pcb.nets.items():
            if net.name and net.name in zone_net_names:
                nets_with_zones.add(net_id)

        # Count multi-pad nets that lack a zone (so must be routed as signals).
        return sum(
            1
            for net_id, count in pad_counts.items()
            if count >= 2 and net_id not in nets_with_zones
        )
    except Exception:
        return 0


def _check_route_postcondition(
    input_pcb: Path | None,
    routed_pcb: Path | None,
) -> BuildResult | None:
    """Validate that ``kct route``'s successful exit produced real copper.

    Defense-in-depth check for issue #2740.  The router can exit 0 with
    ``completion = 1.0`` when every multi-pad net is auto-skipped as a
    pour net (``nets_to_route`` becomes 0 -> the
    ``nets_routed / nets_to_route if nets_to_route > 0 else 1.0`` ternary
    in :mod:`kicad_tools.cli.route_cmd` returns 1.0).  Without this
    postcondition the build pipeline would proceed to verify/export with
    an electrically empty PCB and the only symptom would be
    ``zone_unfilled`` warnings (severity = warning, not error).

    Returns a *failure* :class:`BuildResult` when:

    * The input PCB had at least one multi-pad signal net (so routing
      should produce >=1 segment), AND
    * The routed PCB exists and contains zero segments AND zero vias.

    Returns ``None`` (i.e., "postcondition OK, continue") otherwise --
    including when the input PCB has no routable signal nets (small
    designs where every net is poured), when files are missing (handled
    by upstream callers), or when the PCB cannot be parsed.
    """
    if not input_pcb or not routed_pcb or not routed_pcb.exists():
        return None

    expected = _count_multi_pad_signal_nets(input_pcb)
    if expected == 0:
        return None  # No signal nets to route -- zero segments is legitimate

    try:
        from kicad_tools.schema.pcb import PCB

        routed = PCB.load(str(routed_pcb))
        segment_count = len(routed.segments)
        via_count = len(routed.vias)
    except Exception:
        return None  # Best-effort: parse failure should not break the build

    if segment_count == 0 and via_count == 0:
        return BuildResult(
            step="route",
            success=False,
            message=(
                f"Route step exited 0 but produced 0 segments and 0 vias "
                f"for {expected} routable signal net(s).  This indicates a "
                f"silent router failure (see issue #2740 -- the build will "
                f"not ship an electrically empty PCB)."
            ),
            output_file=routed_pcb,
        )
    return None


def _detect_layer_count(pcb_file: Path) -> int:
    """Return the number of copper layers defined in *pcb_file*.

    Probes the ``(layers ...)`` block via the same helper the stitch
    command uses (``get_copper_layers``) so the layer count matches what
    the stitcher sees.  Returns 2 on any error so the stitch step
    short-circuits to the 2-layer skip path rather than crashing.
    """
    try:
        from kicad_tools.cli.stitch_cmd import get_copper_layers
        from kicad_tools.core.sexp_file import load_pcb

        sexp = load_pcb(pcb_file)
        return len(get_copper_layers(sexp))
    except Exception as exc:
        logger.debug("Could not detect layer count for %s: %s", pcb_file, exc)
        return 2


def _run_step_stitch(ctx: BuildContext, console: Console) -> BuildResult:
    """Run stitching-via insertion on the routed PCB.

    Adds thermal-relief / connection vias from plane-net pads (e.g.
    GND, VCC, VBUS) down to the inner-layer zone copper so they end
    up electrically connected after zone fill.

    Skips gracefully when:
    - No PCB to stitch (no route output) — succeeds as a no-op.
    - The board has only 2 layers (no internal planes to stitch to).
    - The PCB has no plane-net zones to stitch onto.

    Operates on :attr:`BuildContext.routed_pcb_file` when present (the
    output of the route step), falling back to :attr:`BuildContext.pcb_file`
    so ``kct build --step stitch`` still works when invoked in isolation.

    Idempotent: ``run_stitch`` skips already-connected pads via
    ``is_pad_connected``, so a second invocation reports
    ``already_connected > 0`` and adds zero new vias.
    """
    # Locate the PCB to stitch.  Preference order:
    #   1. ctx.routed_pcb_file (populated by the preceding ROUTE step)
    #   2. An on-disk *_routed.kicad_pcb sibling of ctx.pcb_file (so
    #      `kct build --step stitch` finds the route output when
    #      invoked in isolation, mirroring _run_step_route's recovery
    #      path).
    #   3. ctx.pcb_file (unrouted PCB) -- the stitcher will still
    #      detect plane nets / pads correctly on an unrouted board.
    pcb_to_stitch = ctx.routed_pcb_file
    if (not pcb_to_stitch or not pcb_to_stitch.exists()) and ctx.pcb_file:
        expected_routed = ctx.pcb_file.with_stem(ctx.pcb_file.stem + "_routed")
        if expected_routed.exists():
            pcb_to_stitch = expected_routed

    if not pcb_to_stitch or not pcb_to_stitch.exists():
        pcb_to_stitch = ctx.pcb_file

    if not pcb_to_stitch or not pcb_to_stitch.exists():
        return BuildResult(
            step="stitch",
            success=True,
            message="stitch: no PCB file found — skipped",
        )

    # Layer-count check.  On 2-layer boards there are no internal planes
    # to stitch to, so the step is a no-op.
    layer_count = _detect_layer_count(pcb_to_stitch)
    if layer_count <= 2:
        return BuildResult(
            step="stitch",
            success=True,
            message="stitch: 2-layer board — skipped (no internal planes)",
            output_file=pcb_to_stitch,
        )

    # Probe the PCB for plane nets before invoking the stitcher.  This
    # avoids any work when there is nothing to stitch (e.g. a multi-layer
    # board whose zones step did not run, or a board with no power pours).
    try:
        from kicad_tools.cli.stitch_cmd import find_all_plane_nets
        from kicad_tools.core.sexp_file import load_pcb as _load_pcb

        sexp = _load_pcb(pcb_to_stitch)
        plane_nets = find_all_plane_nets(sexp)
    except Exception as exc:
        logger.debug("Could not probe plane nets: %s", exc)
        plane_nets = {}

    if not plane_nets:
        return BuildResult(
            step="stitch",
            success=True,
            message="stitch: no plane nets detected — skipped",
            output_file=pcb_to_stitch,
        )

    # Look up manufacturer-aware via dimensions so the stitching vias
    # satisfy DRC.  Fall back to conservative defaults if profile lookup
    # fails (e.g. unknown manufacturer string).
    try:
        from kicad_tools.manufacturers import get_profile

        profile = get_profile(ctx.mfr)
        rules = profile.get_design_rules(layers=layer_count)
        via_size = rules.min_via_diameter_mm
        via_drill = rules.min_via_drill_mm
    except Exception:
        via_size = 0.6
        via_drill = 0.3

    if ctx.dry_run:
        nets_str = ", ".join(sorted(plane_nets.keys()))
        return BuildResult(
            step="stitch",
            success=True,
            message=(
                f"[dry-run] Would run: kct stitch {pcb_to_stitch.name} "
                f"--via-size {via_size} --drill {via_drill} "
                f"(auto-detected nets: {nets_str})"
            ),
            output_file=pcb_to_stitch,
        )

    if not ctx.quiet:
        console.print(
            f"  Stitching vias on {pcb_to_stitch.name} "
            f"({len(plane_nets)} plane net(s), "
            f"via {via_size}mm/{via_drill}mm drill for {ctx.mfr})..."
        )

    # Invoke the in-process stitcher.  Preferred over the subprocess
    # entry point because it shares the loaded PCB representation and
    # surfaces structured StitchResult details for error messages.
    try:
        from kicad_tools.cli.stitch_cmd import run_stitch

        result = run_stitch(
            pcb_to_stitch,
            net_names=sorted(plane_nets.keys()),
            via_size=via_size,
            drill=via_drill,
        )
    except Exception as exc:
        return BuildResult(
            step="stitch",
            success=False,
            message=f"Stitching failed: {exc}",
            output_file=pcb_to_stitch,
        )

    added = len(result.vias_added)
    already = result.already_connected
    skipped = len(result.pads_skipped)

    if added == 0 and already > 0:
        message = (
            f"Stitching complete: {already} plane pad(s) already connected, no new vias needed"
        )
    elif added > 0:
        message = (
            f"Stitching complete: added {added} via(s) for "
            f"{len(plane_nets)} plane net(s)"
            + (f", {already} pad(s) already connected" if already else "")
            + (f", {skipped} pad(s) skipped" if skipped else "")
        )
    else:
        message = (
            f"Stitching complete: no via candidates found for "
            f"{len(plane_nets)} plane net(s)" + (f", {skipped} pad(s) skipped" if skipped else "")
        )

    return BuildResult(
        step="stitch",
        success=True,
        message=message,
        output_file=pcb_to_stitch,
    )


def _run_step_erc(ctx: BuildContext, console: Console) -> BuildResult:
    """Run ERC (Electrical Rules Check) on the schematic.

    Invokes ``kicad-cli sch erc`` to produce ``erc_report.json`` adjacent
    to the schematic (or in ``ctx.output_dir`` when set), so that the
    export-time preflight (``export/preflight.py:_check_erc``) finds the
    report instead of emitting a "No ERC report found" warning.

    Failures are surfaced as a non-success ``BuildResult`` but do not
    halt the pipeline unconditionally; the same "stop on failure unless
    VERIFY/EXPORT" rule as DRC applies, mirroring ``_run_step_verify``.
    """
    if not ctx.schematic_file or not ctx.schematic_file.exists():
        return BuildResult(
            step="erc",
            success=False,
            message="No schematic file found to run ERC against",
        )

    # Choose where to write the report. Mirror DRC: prefer ctx.output_dir,
    # otherwise drop the report next to the schematic so the export
    # preflight's auto-discovery picks it up.
    if ctx.output_dir:
        ctx.output_dir.mkdir(parents=True, exist_ok=True)
        erc_report_path = ctx.output_dir / "erc_report.json"
    else:
        erc_report_path = ctx.schematic_file.parent / "erc_report.json"

    if ctx.dry_run:
        return BuildResult(
            step="erc",
            success=True,
            message=f"[dry-run] Would run: kicad-cli sch erc {ctx.schematic_file.name}",
            output_file=erc_report_path,
        )

    if not ctx.quiet:
        console.print(f"  Running ERC on {ctx.schematic_file.name}...")

    try:
        from kicad_tools.cli.runner import find_kicad_cli, run_erc

        kicad_cli = find_kicad_cli()
        if kicad_cli is None:
            # ERC is best-effort: when kicad-cli is unavailable we
            # warn but do not block the pipeline. The export preflight
            # will subsequently see "no report" and emit its own warning.
            if not ctx.quiet:
                console.print(
                    "  [yellow]WARN[/yellow] kicad-cli not found; "
                    "skipping ERC (export preflight will warn)"
                )
            return BuildResult(
                step="erc",
                success=True,
                message="ERC skipped: kicad-cli not found",
            )

        result = run_erc(
            ctx.schematic_file,
            output_path=erc_report_path,
            format="json",
            severity_all=True,
            kicad_cli=kicad_cli,
        )

        if not result.success:
            return BuildResult(
                step="erc",
                success=False,
                message=f"ERC failed: {result.stderr or 'kicad-cli returned no output'}",
            )

        # Parse the report to surface error/warning counts.
        try:
            from kicad_tools.erc.report import ERCReport

            report = ERCReport.load(erc_report_path)
            error_count = report.error_count
            warning_count = report.warning_count
        except Exception as exc:
            return BuildResult(
                step="erc",
                success=False,
                message=f"Could not parse ERC report: {exc}",
                output_file=erc_report_path,
            )

        if error_count > 0:
            return BuildResult(
                step="erc",
                success=False,
                message=(f"ERC found {error_count} error(s), {warning_count} warning(s)"),
                output_file=erc_report_path,
            )

        return BuildResult(
            step="erc",
            success=True,
            message=f"ERC: 0 errors, {warning_count} warning(s)",
            output_file=erc_report_path,
        )

    except Exception as e:  # pragma: no cover - defensive
        return BuildResult(
            step="erc",
            success=False,
            message=f"ERC step failed: {e}",
        )


def _print_sync_analysis_detail(analysis, console: Console) -> None:
    """Print per-category sync analysis detail.

    Mirrors :func:`pipeline_cmd._print_sync_analysis_detail` so the build
    command surfaces the same actionable detail as ``kct pipeline``.
    """
    if analysis.value_mismatches:
        console.print(f"    Value mismatches ({len(analysis.value_mismatches)}):")
        for mm in analysis.value_mismatches:
            console.print(
                f"      {mm['reference']}: sch={mm['schematic_value']} pcb={mm['pcb_value']}"
            )

    if analysis.footprint_mismatches:
        console.print(f"    Footprint mismatches ({len(analysis.footprint_mismatches)}):")
        for mm in analysis.footprint_mismatches:
            console.print(
                f"      {mm['reference']}: sch={mm['schematic_footprint']}"
                f" pcb={mm['pcb_footprint']}"
            )

    if analysis.add_footprint_actions:
        console.print(f"    Add footprint ({len(analysis.add_footprint_actions)}):")
        for action in analysis.add_footprint_actions:
            ref = action["reference"]
            fp = action.get("footprint", "")
            val = action.get("value", "")
            console.print(f"      {ref}: {fp} ({val})")

    if analysis.schematic_orphans:
        console.print(f"    Schematic-only ({len(analysis.schematic_orphans)}):")
        for ref in analysis.schematic_orphans:
            console.print(f"      {ref} - missing from PCB")

    if analysis.pcb_orphans:
        console.print(f"    PCB-only ({len(analysis.pcb_orphans)}):")
        for ref in analysis.pcb_orphans:
            console.print(f"      {ref} - not in schematic")


def _run_step_sync(ctx: BuildContext, console: Console) -> BuildResult:
    """Reconcile schematic <-> PCB component sets in-process.

    Uses :class:`kicad_tools.sync.reconciler.Reconciler` to analyse drift
    between the schematic and PCB right after the PCB write — before any
    expensive downstream work (placement, zones, routing) is performed
    and well before manufacturing artefacts are produced.

    Mirrors :func:`pipeline_cmd._run_step_sync`'s blocking semantics:

    - In sync: success with a one-line "sync: in sync" message.
    - Schematic orphans (refs missing from PCB): blocking failure.
      ``ctx.force`` converts this into a non-blocking warning so the user
      can deliberately bypass the gate.
    - Value/footprint mismatches or PCB-only refs without schematic
      orphans: warning, build continues with ``success=True``.
    - No schematic available: skipped (``success=True``).

    Note: unlike :func:`pipeline_cmd._run_step_sync`, this implementation
    does NOT expose a ``--apply-sync`` flag.  ``kct build --force`` is the
    only escape hatch.  Auto-fixing drift belongs to ``kct pipeline`` /
    ``kct sync --apply``.
    """
    # Skip if no schematic file available
    if ctx.schematic_file is None or not ctx.schematic_file.exists():
        return BuildResult(
            step="sync",
            success=True,
            message=(
                "sync: no schematic available — skipped"
                " (use 'kct build --step schematic' or supply a schematic)"
            ),
        )

    # Find the PCB to reconcile (prefer routed version when available,
    # but typically SYNC runs right after PCB write so ctx.pcb_file is set
    # and ctx.routed_pcb_file is still None).
    pcb_to_check = ctx.routed_pcb_file or ctx.pcb_file
    if not pcb_to_check or not pcb_to_check.exists():
        return BuildResult(
            step="sync",
            success=False,
            message="sync: no PCB file found to reconcile",
        )

    # Dry-run mode: preview without instantiating Reconciler
    if ctx.dry_run:
        return BuildResult(
            step="sync",
            success=True,
            message=(
                f"[dry-run] Would reconcile {ctx.schematic_file.name} <-> {pcb_to_check.name}"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Reconciling {ctx.schematic_file.name} <-> {pcb_to_check.name}...")

    # Instantiate Reconciler in-process (no subprocess overhead)
    try:
        from kicad_tools.sync.reconciler import Reconciler

        reconciler = Reconciler(
            schematic=ctx.schematic_file,
            pcb=pcb_to_check,
        )
        analysis = reconciler.analyze()
    except Exception as e:
        logger.warning("sync: failed to analyze: %s", e)
        return BuildResult(
            step="sync",
            success=False,
            message=f"sync: failed to analyze — {e}",
        )

    # Clean pass
    if analysis.is_in_sync:
        return BuildResult(
            step="sync",
            success=True,
            message="sync: in sync",
        )

    # Print the summary plus per-category detail
    if not ctx.quiet:
        for line in analysis.summary().splitlines():
            console.print(f"    {line}")
        _print_sync_analysis_detail(analysis, console)

    schematic_orphans = list(analysis.schematic_orphans)
    value_mismatch_count = len(analysis.value_mismatches)
    footprint_mismatch_count = len(analysis.footprint_mismatches)
    pcb_orphan_count = len(analysis.pcb_orphans)

    # Schematic orphans are blocking (parallels ERC's blocking semantics).
    # --force lets the user bypass; otherwise the build halts here so no
    # downstream BOM/manufacturing artefacts are produced.
    if schematic_orphans:
        if ctx.force:
            if not ctx.quiet:
                console.print(
                    f"  [yellow]sync: {len(schematic_orphans)} schematic-only ref(s)"
                    " missing from PCB — continuing (--force)[/yellow]"
                )
            return BuildResult(
                step="sync",
                success=True,
                message=(
                    f"sync: {len(schematic_orphans)} schematic-only ref(s)"
                    " missing from PCB — continuing (--force)"
                ),
            )

        return BuildResult(
            step="sync",
            success=False,
            message=(
                f"sync: {len(schematic_orphans)} schematic-only ref(s) missing"
                " from PCB (use --force to continue, or 'kct sync --apply' to fix)"
            ),
        )

    # No blocking schematic orphans, but other drift exists -> warn, success=True
    parts = []
    if value_mismatch_count:
        parts.append(f"{value_mismatch_count} value mismatch(es)")
    if footprint_mismatch_count:
        parts.append(f"{footprint_mismatch_count} footprint mismatch(es)")
    if pcb_orphan_count:
        parts.append(f"{pcb_orphan_count} PCB-only ref(s)")

    return BuildResult(
        step="sync",
        success=True,
        message=(
            "sync: drift detected — " + (", ".join(parts) if parts else "non-blocking issues")
        ),
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
        # Write DRC report to output dir if specified, otherwise next to the PCB file
        if ctx.output_dir:
            drc_report_path = ctx.output_dir / "drc_report.json"
        else:
            drc_report_path = pcb_to_verify.parent / "drc_report.json"

        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(pcb_to_verify),
            "--mfr",
            ctx.mfr,
            "--output",
            str(drc_report_path),
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


def _run_step_export(ctx: BuildContext, console: Console) -> BuildResult:
    """Run manufacturing export step (Gerbers, BOM, CPL).

    Invokes ``kct export`` as a subprocess to generate a manufacturing
    package in a ``manufacturing/`` directory.
    """
    # Find the PCB to export (prefer routed version)
    pcb_to_export = ctx.routed_pcb_file or ctx.pcb_file

    if not pcb_to_export or not pcb_to_export.exists():
        return BuildResult(
            step="export",
            success=False,
            message="No PCB file found to export",
        )

    # Determine manufacturer: prefer target_fab from spec, fall back to ctx.mfr
    mfr = ctx.mfr
    if (
        ctx.spec
        and ctx.spec.requirements
        and ctx.spec.requirements.manufacturing
        and ctx.spec.requirements.manufacturing.target_fab
    ):
        mfr = ctx.spec.requirements.manufacturing.target_fab

    # Determine output directory
    if ctx.output_dir:
        mfr_dir = ctx.output_dir / "manufacturing"
    else:
        mfr_dir = pcb_to_export.parent / "manufacturing"

    if ctx.dry_run:
        return BuildResult(
            step="export",
            success=True,
            message=(
                f"[dry-run] Would run: kct export {pcb_to_export.name} --mfr {mfr} -o {mfr_dir}"
            ),
        )

    if not ctx.quiet:
        console.print(f"  Exporting manufacturing package for {pcb_to_export.name}...")

    try:
        cmd = [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "export",
            str(pcb_to_export),
            "--mfr",
            mfr,
            "-o",
            str(mfr_dir),
        ]

        # Pass schematic for BOM generation if available
        if ctx.schematic_file and ctx.schematic_file.exists():
            cmd.extend(["--sch", str(ctx.schematic_file)])

        # Skip BOM/CPL if no assembly specified in spec
        if not (
            ctx.spec
            and ctx.spec.requirements
            and ctx.spec.requirements.manufacturing
            and ctx.spec.requirements.manufacturing.assembly
        ):
            cmd.append("--no-bom")
            cmd.append("--no-cpl")

        result = subprocess.run(
            cmd,
            cwd=str(ctx.project_dir),
            capture_output=not ctx.verbose,
            text=True,
        )

        if result.returncode == 0:
            return BuildResult(
                step="export",
                success=True,
                message=f"Manufacturing package exported to {mfr_dir}",
                output_file=mfr_dir,
            )
        else:
            error_msg = result.stderr if result.stderr else f"Exit code: {result.returncode}"
            return BuildResult(
                step="export",
                success=False,
                message=f"Export failed: {error_msg}",
            )

    except Exception as e:
        return BuildResult(
            step="export",
            success=False,
            message=f"Export failed: {e}",
        )


# Build steps whose output is a *.kicad_pcb file that can be smoke-checked
# by kicad-cli.  Other steps (SCHEMATIC, VERIFY, EXPORT) are skipped:
# SCHEMATIC produces a schematic, VERIFY is read-only, and EXPORT is
# terminal (its own kicad-cli call surfaces load failures directly).
_PCB_WRITE_STEPS: set[BuildStep] = {
    BuildStep.PCB,
    BuildStep.OUTLINE,
    BuildStep.PLACEMENT,
    BuildStep.ZONES,
    BuildStep.SILKSCREEN,
    BuildStep.ROUTE,
    BuildStep.STITCH,
}


def _smoke_check_pcb(
    pcb_path: Path,
    producing_step: str,
    console: Console,
    ctx: BuildContext,
) -> BuildResult | None:
    """Verify that ``pcb_path`` can be loaded by kicad-cli.

    Runs ``kicad-cli pcb drc --schematic-parity off`` against the PCB and
    checks for a "Failed to load board" signal in stderr/stdout.  This
    catches PCB-write bugs (corrupt S-expressions, unrecognised tokens,
    bogus ``generator_version`` strings, etc.) at the writer that
    introduced them rather than at the export step many minutes later.

    Args:
        pcb_path: PCB file just written by ``producing_step``.
        producing_step: Name of the step that produced *pcb_path* (for
            attribution in the failure message).
        console: Rich console for warnings.
        ctx: Build context — used to track whether the
            "kicad-cli not installed" warning has already been emitted
            (so it prints at most once per build).

    Returns:
        ``None`` when the smoke check passes (kicad-cli loads the PCB
        without complaint, or kicad-cli is not installed).  A failed
        :class:`BuildResult` when kicad-cli rejects the file with
        "Failed to load board" in its output.

    Note:
        Real DRC rule violations cause kicad-cli to exit non-zero, but
        without the "Failed to load board" marker.  Those are *not*
        treated as smoke-check failures — only true parse rejections.
    """
    import contextlib
    import os
    import tempfile

    from kicad_tools.cli.runner import find_kicad_cli, run_drc

    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        if not ctx._kicad_cli_warning_emitted:
            console.print(
                "  [yellow]warning[/yellow] kicad-cli not installed; skipping PCB smoke checks"
            )
            ctx._kicad_cli_warning_emitted = True
        return None

    # Use a temp file for the DRC report — we don't care about its
    # contents, only that kicad-cli could parse the input.
    fd, report_path_str = tempfile.mkstemp(suffix=".json", prefix="smoke_drc_")
    os.close(fd)
    report_path = Path(report_path_str)

    try:
        result = run_drc(
            pcb_path,
            output_path=report_path,
            schematic_parity=False,
            kicad_cli=kicad_cli,
        )
    finally:
        # Clean up the report regardless of outcome.
        with contextlib.suppress(OSError):
            report_path.unlink(missing_ok=True)

    # Discriminate "load failure" from "DRC violations":
    # kicad-cli emits "Failed to load board" in stderr (and sometimes
    # stdout) when the .kicad_pcb cannot be parsed.  Real DRC violations
    # exit non-zero with no such marker.
    combined = (result.stderr or "") + "\n" + (result.stdout or "")
    if "Failed to load board" in combined:
        message = (
            f"Output of '{producing_step}' rejected by kicad-cli: "
            f"{result.stderr.strip() or result.stdout.strip()}\n"
            f"  Inspect with: head -10 {pcb_path}"
        )
        return BuildResult(
            step=producing_step,
            success=False,
            message=message,
            output_file=pcb_path,
        )

    return None


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
    kct build -o /tmp/output            # Write generated files to output dir
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
        choices=[
            "schematic",
            "erc",
            "pcb",
            "sync",
            "outline",
            "placement",
            "zones",
            "silkscreen",
            "route",
            "stitch",
            "verify",
            "export",
            "all",
        ],
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
    parser.add_argument(
        "-o",
        "--output",
        help="Output directory for generated files (default: project directory)",
    )
    parser.add_argument(
        "--optimize-placement",
        action="store_true",
        help="Run CMA-ES placement optimization before routing (opt-in)",
    )
    parser.add_argument(
        "--no-smoke-check",
        action="store_true",
        help=(
            "Disable the per-step kicad-cli load smoke check that runs "
            "after each PCB-write step.  Use to restore prior behaviour "
            "when kicad-cli is misbehaving or pipeline speed matters."
        ),
    )

    args = parser.parse_args(argv)
    console = Console(quiet=args.quiet)

    # Determine project directory and spec file
    if args.spec:
        spec_path = Path(args.spec).resolve()
        if spec_path.is_file():
            project_dir = spec_path.parent
            spec_file = spec_path
        elif spec_path.is_dir():
            project_dir = spec_path
            spec_file = _find_spec_file(project_dir)
        else:
            # Path doesn't exist -- if it looks like a file (.kct suffix), use parent
            if spec_path.suffix == ".kct":
                project_dir = spec_path.parent
                spec_file = None
            else:
                project_dir = spec_path
                spec_file = None
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

    # Resolve output directory if provided
    output_dir: Path | None = None
    if args.output:
        output_dir = Path(args.output).resolve()
        output_dir.mkdir(parents=True, exist_ok=True)

    # Find existing artifacts (search output dir first, then project dir)
    search_dir = output_dir if output_dir else project_dir
    schematic, pcb = _find_artifacts(search_dir, spec_file)
    # Fall back to project dir if nothing found in output dir
    if output_dir and not schematic and not pcb:
        schematic, pcb = _find_artifacts(project_dir, spec_file)

    # Create build context
    ctx = BuildContext(
        project_dir=project_dir,
        spec_file=spec_file,
        spec=spec,
        schematic_file=schematic,
        pcb_file=pcb,
        output_dir=output_dir,
        mfr=args.mfr,
        verbose=args.verbose,
        dry_run=args.dry_run,
        quiet=args.quiet,
        force=args.force,
        optimize_placement=args.optimize_placement,
        smoke_check=not args.no_smoke_check,
    )

    # Print build header
    if not args.quiet:
        project_name = spec.project.name if spec else project_dir.name

        header_lines = (
            f"[bold]Building:[/bold] {project_name}\n"
            f"[dim]Directory:[/dim] {project_dir}\n"
            f"[dim]Manufacturer:[/dim] {args.mfr}"
        )
        if output_dir:
            header_lines += f"\n[dim]Output:[/dim] {output_dir}"

        console.print(
            Panel.fit(
                header_lines,
                title="kct build",
            )
        )

    # Determine steps to run
    if args.step == "all":
        steps = [
            BuildStep.SCHEMATIC,
            BuildStep.ERC,
            BuildStep.PCB,
            BuildStep.SYNC,
            BuildStep.OUTLINE,
            BuildStep.PLACEMENT,
            BuildStep.ZONES,
            BuildStep.SILKSCREEN,
            BuildStep.ROUTE,
            BuildStep.STITCH,
            BuildStep.VERIFY,
            BuildStep.EXPORT,
        ]
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

            elif step == BuildStep.ERC:
                result = _run_step_erc(ctx, console)

            elif step == BuildStep.PCB:
                result = _run_step_pcb(ctx, console)
                if result.output_file:
                    ctx.pcb_file = result.output_file

            elif step == BuildStep.SYNC:
                result = _run_step_sync(ctx, console)

            elif step == BuildStep.OUTLINE:
                result = _run_step_outline(ctx, console)

            elif step == BuildStep.PLACEMENT:
                result = _run_step_placement(ctx, console)

            elif step == BuildStep.ZONES:
                result = _run_step_zones(ctx, console)

            elif step == BuildStep.SILKSCREEN:
                result = _run_step_silkscreen(ctx, console)

            elif step == BuildStep.ROUTE:
                result = _run_step_route(ctx, console)
                if result.output_file:
                    ctx.routed_pcb_file = result.output_file

            elif step == BuildStep.STITCH:
                result = _run_step_stitch(ctx, console)
                if result.output_file:
                    ctx.routed_pcb_file = result.output_file

            elif step == BuildStep.VERIFY:
                result = _run_step_verify(ctx, console)

            elif step == BuildStep.EXPORT:
                result = _run_step_export(ctx, console)

            else:
                result = BuildResult(step=step.value, success=False, message="Unknown step")

            results.append(result)
            progress.remove_task(task)

            # Print step result
            if not args.quiet:
                status = "[green]OK[/green]" if result.success else "[red]FAIL[/red]"
                console.print(f"  [{status}] {step.value}: {result.message}")

            # Stop on failure unless verifying or exporting
            if not result.success and step not in (
                BuildStep.ERC,
                BuildStep.VERIFY,
                BuildStep.EXPORT,
            ):
                break

            # Smoke-check: after every successful PCB-write step, ask
            # kicad-cli whether the PCB it produced is loadable.  This
            # attributes load-time rejections to the writer that just
            # ran rather than to the much-later EXPORT step.  Skipped
            # under --dry-run (no PCB was actually written) and when
            # the user opts out via --no-smoke-check.
            if result.success and ctx.smoke_check and not ctx.dry_run and step in _PCB_WRITE_STEPS:
                pcb_for_check = ctx.routed_pcb_file or ctx.pcb_file
                if pcb_for_check is not None and pcb_for_check.exists():
                    smoke = _smoke_check_pcb(pcb_for_check, step.value, console, ctx)
                    if smoke is not None:
                        results.append(smoke)
                        if not args.quiet:
                            console.print(
                                f"  [[red]FAIL[/red]] {step.value} (smoke-check): {smoke.message}"
                            )
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

            # Show manufacturing output if export step ran
            export_results = [r for r in results if r.step == "export" and r.success]
            if export_results and export_results[0].output_file:
                console.print(f"[dim]Manufacturing:[/dim] {export_results[0].output_file}")
        else:
            console.print(
                f"[red]Build failed[/red] ({success_count}/{total_count} steps succeeded)"
            )
            return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
