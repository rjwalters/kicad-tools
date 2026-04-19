"""
Build command implementation for end-to-end workflow from spec to manufacturable design.

Orchestrates the full build pipeline:
1. Load project spec (.kct file)
2. Run schematic generator (if exists)
3. Run PCB generator (if exists)
4. Run autorouter
5. Run verification (DRC, audit)
6. Export manufacturing package (Gerbers, BOM, CPL)
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
    OUTLINE = "outline"
    ZONES = "zones"
    PLACEMENT = "placement"
    SILKSCREEN = "silkscreen"
    ROUTE = "route"
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
    creates copper zone definitions on the PCB before routing.  GND gets
    a zone on B.Cu with priority 1; other power nets get zones on F.Cu
    with priority 0.

    Zones are *defined* here (unfilled polygons).  Filling happens later
    after routing, typically via kicad-cli.
    """
    if not ctx.pcb_file or not ctx.pcb_file.exists():
        return BuildResult(
            step="zones",
            success=True,
            message="No PCB file found, skipping zone creation",
        )

    try:
        from kicad_tools.router.net_class import NetClass, auto_classify_nets
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.zones.generator import ZoneGenerator, auto_create_zones_for_pour_nets

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

        # Classify nets
        classifications = auto_classify_nets(net_names)

        # Identify pour nets (POWER and GROUND)
        pour_nets: list[tuple[str, NetClass]] = []
        for net_id, classification in classifications.items():
            if classification.net_class in (NetClass.POWER, NetClass.GROUND):
                net_name = net_names[net_id]
                pour_nets.append((net_name, classification.net_class))

        if not pour_nets:
            return BuildResult(
                step="zones",
                success=True,
                message="No power/ground nets detected, skipping zone creation",
            )

        # Check for existing zones on these nets (idempotency)
        existing_zone_nets = {z.net_name for z in pcb.zones}
        new_pour_nets = [
            (name, cls) for name, cls in pour_nets if name not in existing_zone_nets
        ]

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

        count = auto_create_zones_for_pour_nets(ctx.pcb_file, new_pour_nets)

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
            route_script, ctx.project_dir, ctx.verbose, env_vars=route_env_vars,
            script_args=script_args,
        )

        # Find routed PCB (check output dir first, then project dir)
        output_file: Path | None = None
        for search_dir in ([ctx.output_dir, ctx.project_dir] if ctx.output_dir else [ctx.project_dir]):
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
            return BuildResult(
                step="route",
                success=True,
                message="Routing completed successfully",
                output_file=output_file if output_file.exists() else None,
            )
        elif result.returncode == 2:
            # Exit code 2 = partial routing (some nets routed, some skipped).
            # When pour nets (GND, +3.3V, etc.) are auto-skipped for zone fill,
            # the router reports partial completion even though all signal nets
            # routed successfully.  Treat this as success so the build pipeline
            # continues to verification.
            return BuildResult(
                step="route",
                success=True,
                message="Routing completed (some nets skipped for zone fill)",
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
                f"[dry-run] Would run: kct export {pcb_to_export.name} "
                f"--mfr {mfr} -o {mfr_dir}"
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
        choices=["schematic", "pcb", "outline", "zones", "placement", "silkscreen", "route", "verify", "export", "all"],
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
        steps = [BuildStep.SCHEMATIC, BuildStep.PCB, BuildStep.OUTLINE, BuildStep.ZONES, BuildStep.PLACEMENT, BuildStep.SILKSCREEN, BuildStep.ROUTE, BuildStep.VERIFY, BuildStep.EXPORT]
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

            elif step == BuildStep.OUTLINE:
                result = _run_step_outline(ctx, console)

            elif step == BuildStep.ZONES:
                result = _run_step_zones(ctx, console)

            elif step == BuildStep.PLACEMENT:
                result = _run_step_placement(ctx, console)

            elif step == BuildStep.SILKSCREEN:
                result = _run_step_silkscreen(ctx, console)

            elif step == BuildStep.ROUTE:
                result = _run_step_route(ctx, console)
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
            if not result.success and step not in (BuildStep.VERIFY, BuildStep.EXPORT):
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
