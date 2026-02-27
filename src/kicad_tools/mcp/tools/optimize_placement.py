"""MCP tools for placement optimization and evaluation.

Provides two tools for AI agents to optimize and evaluate PCB component placement:
- optimize_placement: Run CMA-ES placement optimization on a board
- evaluate_placement: Evaluate current placement quality with score breakdown

These tools wrap the placement optimization pipeline (cost function, CMA-ES
strategy, seed generation) and expose them as MCP-callable functions.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Any, Sequence

from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    CostBreakdown,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    PlacementScore,
)
from kicad_tools.placement.cost import (
    evaluate_placement as cost_evaluate_placement,
)
from kicad_tools.placement.strategy import StrategyConfig
from kicad_tools.placement.vector import (
    ComponentDef,
    PadDef,
    PlacementVector,
    bounds,
    decode,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Internal helpers (shared between optimize_placement and evaluate_placement)
# ---------------------------------------------------------------------------


def _validate_pcb_path(pcb_path: str) -> Path:
    """Validate PCB file path and return a Path object.

    Args:
        pcb_path: Absolute path to .kicad_pcb file.

    Returns:
        Validated Path object.

    Raises:
        FileNotFoundError: If file does not exist.
        ParseError: If file extension is wrong.
    """
    path = Path(pcb_path)
    if not path.exists():
        raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")
    if path.suffix != ".kicad_pcb":
        raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")
    return path


def _read_board_data(
    pcb_path: str,
) -> tuple[list[ComponentDef], list[Net], BoardOutline, DesignRuleSet]:
    """Read component, net, and board data from a .kicad_pcb file.

    Uses kicad_tools.schema.pcb.PCB to parse the file and extract
    components, nets, board outline, and design rules.

    Args:
        pcb_path: Path to .kicad_pcb file.

    Returns:
        Tuple of (components, nets, board_outline, rules).
    """
    from kicad_tools.schema.pcb import PCB as SchemaPCB

    pcb = SchemaPCB.load(pcb_path)

    # Board outline from Edge.Cuts graphic lines
    board_outline = _extract_board_outline(pcb)

    # Components from footprints
    components: list[ComponentDef] = []
    for fp in pcb.footprints:
        ref = fp.reference
        if not ref:
            continue

        width, height = _footprint_size_from_pads(fp)

        pad_defs: list[PadDef] = []
        for pad in fp.pads:
            pad_defs.append(
                PadDef(
                    name=pad.number,
                    local_x=pad.position[0],
                    local_y=pad.position[1],
                    size_x=pad.size[0],
                    size_y=pad.size[1],
                )
            )

        components.append(
            ComponentDef(
                reference=ref,
                pads=tuple(pad_defs),
                width=width,
                height=height,
            )
        )

    # Nets from footprint pad net assignments
    component_refs = {c.reference for c in components}
    net_map: dict[str, list[tuple[str, str]]] = {}
    for fp in pcb.footprints:
        ref = fp.reference
        if not ref or ref not in component_refs:
            continue
        for pad in fp.pads:
            net_name = pad.net_name
            if net_name and net_name not in ("", "unconnected"):
                net_map.setdefault(net_name, []).append((ref, pad.number))

    nets: list[Net] = []
    for net_name, pins in net_map.items():
        if len(pins) >= 2:
            nets.append(Net(name=net_name, pins=pins))

    rules = DesignRuleSet()
    return components, nets, board_outline, rules


def _extract_board_outline(pcb: Any) -> BoardOutline:
    """Extract board outline from Edge.Cuts graphic lines."""
    xs: list[float] = []
    ys: list[float] = []

    for line in pcb.graphic_lines:
        if line.layer == "Edge.Cuts":
            xs.extend([line.start[0], line.end[0]])
            ys.extend([line.start[1], line.end[1]])

    if xs and ys:
        return BoardOutline(min_x=min(xs), min_y=min(ys), max_x=max(xs), max_y=max(ys))

    # Fallback: use footprint bounding box with margin
    for fp in pcb.footprints:
        xs.append(fp.position[0])
        ys.append(fp.position[1])

    if xs and ys:
        margin = 10.0
        return BoardOutline(
            min_x=min(xs) - margin,
            min_y=min(ys) - margin,
            max_x=max(xs) + margin,
            max_y=max(ys) + margin,
        )

    return BoardOutline(min_x=0.0, min_y=0.0, max_x=100.0, max_y=100.0)


def _footprint_size_from_pads(fp: Any) -> tuple[float, float]:
    """Estimate footprint bounding box from pad positions and sizes."""
    if not fp.pads:
        return (2.0, 2.0)

    xs: list[float] = []
    ys: list[float] = []
    for pad in fp.pads:
        px, py = pad.position
        sx, sy = pad.size
        xs.extend([px - sx / 2, px + sx / 2])
        ys.extend([py - sy / 2, py + sy / 2])

    if xs and ys:
        w = max(xs) - min(xs)
        h = max(ys) - min(ys)
        return (max(w, 1.0), max(h, 1.0))

    return (2.0, 2.0)


def _vector_to_placements(
    vector: PlacementVector,
    components: Sequence[ComponentDef],
) -> list[ComponentPlacement]:
    """Convert a PlacementVector to a list of ComponentPlacement for cost evaluation."""
    placed = decode(vector, components)
    return [
        ComponentPlacement(
            reference=p.reference,
            x=p.x,
            y=p.y,
            rotation=p.rotation,
        )
        for p in placed
    ]


def _evaluate_vector(
    vector: PlacementVector,
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
    rules: DesignRuleSet,
    board: BoardOutline,
    cost_config: PlacementCostConfig,
    footprint_sizes: dict[str, tuple[float, float]],
) -> PlacementScore:
    """Evaluate a single placement vector and return its score."""
    placements = _vector_to_placements(vector, components)
    return cost_evaluate_placement(placements, nets, rules, board, cost_config, footprint_sizes)


def _build_footprint_sizes(
    components: Sequence[ComponentDef],
) -> dict[str, tuple[float, float]]:
    """Build a footprint_sizes dict from component definitions."""
    return {c.reference: (c.width, c.height) for c in components}


def _breakdown_to_dict(breakdown: CostBreakdown) -> dict[str, float]:
    """Convert a CostBreakdown to a serializable dict."""
    return {
        "wirelength": round(breakdown.wirelength, 4),
        "overlap": round(breakdown.overlap, 4),
        "boundary": round(breakdown.boundary, 4),
        "drc": round(breakdown.drc, 4),
        "area": round(breakdown.area, 4),
    }


def _parse_weights(weights: dict[str, float] | None) -> PlacementCostConfig:
    """Parse a weights dict into PlacementCostConfig, or return defaults."""
    if weights is None:
        return PlacementCostConfig()
    return PlacementCostConfig(
        overlap_weight=weights.get("overlap", 1e6),
        drc_weight=weights.get("drc", 1e4),
        boundary_weight=weights.get("boundary", 1e5),
        wirelength_weight=weights.get("wirelength", 1.0),
        area_weight=weights.get("area", 0.1),
    )


# ---------------------------------------------------------------------------
# Public MCP tool functions
# ---------------------------------------------------------------------------


def optimize_placement(
    pcb_path: str,
    strategy: str = "cmaes",
    max_iterations: int = 200,
    weights: dict[str, float] | None = None,
    seed_method: str = "force-directed",
    output_path: str | None = None,
) -> dict[str, Any]:
    """Optimize component placement on a PCB board using CMA-ES.

    Runs the placement optimization loop: reads component/net data from the
    board file, generates a seed placement, runs CMA-ES optimization, and
    returns the optimized result with convergence data.

    Args:
        pcb_path: Absolute path to .kicad_pcb file.
        strategy: Optimization strategy name. Currently only "cmaes" is supported.
        max_iterations: Maximum number of optimization iterations.
        weights: Optional cost function weight overrides. Keys:
            overlap, drc, boundary, wirelength, area.
        seed_method: Seed placement method ("force-directed" or "random").
        output_path: Path for output file. If None, does not write to disk.

    Returns:
        Dictionary with optimization results:
        - success: Whether optimization completed successfully.
        - initial_score: Score before optimization (with breakdown).
        - final_score: Score after optimization (with breakdown).
        - improvement_pct: Percentage improvement in score.
        - iterations: Number of iterations completed.
        - converged: Whether the optimizer detected convergence.
        - wall_time_s: Wall clock time in seconds.
        - feasible: Whether the final placement is feasible.
        - component_count: Number of components optimized.
        - net_count: Number of nets considered.
        - output_path: Path to the output file (if written).
        - convergence_data: List of (iteration, best_score) snapshots.
        - error_message: Error description if success is False.

    Raises:
        FileNotFoundError: If the PCB file does not exist.
        ParseError: If the PCB file cannot be parsed.
    """
    _validate_pcb_path(pcb_path)

    # Parse board data
    try:
        components, nets, board_outline, rules = _read_board_data(pcb_path)
    except Exception as e:
        raise ParseError(f"Failed to parse PCB file: {e}") from e

    if not components:
        return {
            "success": False,
            "error_message": "No components found in PCB file",
            "component_count": 0,
            "net_count": 0,
        }

    cost_config = _parse_weights(weights)
    footprint_sizes = _build_footprint_sizes(components)
    placement_bounds = bounds(board_outline, components)

    # Create strategy
    try:
        if strategy == "cmaes":
            from kicad_tools.placement.cmaes_strategy import CMAESStrategy

            optimizer = CMAESStrategy()
        else:
            return {
                "success": False,
                "error_message": f"Unknown strategy: {strategy!r}. Available: cmaes",
                "component_count": len(components),
                "net_count": len(nets),
            }
    except ImportError as e:
        return {
            "success": False,
            "error_message": f"Strategy module not available: {e}",
            "component_count": len(components),
            "net_count": len(nets),
        }

    # Generate seed placement
    try:
        from kicad_tools.placement.seed import force_directed_placement, random_placement

        if seed_method == "force-directed":
            seed_vector = force_directed_placement(components, nets, board_outline)
        elif seed_method == "random":
            seed_vector = random_placement(components, board_outline)
        else:
            return {
                "success": False,
                "error_message": (
                    f"Unknown seed method: {seed_method!r}. Available: force-directed, random"
                ),
                "component_count": len(components),
                "net_count": len(nets),
            }
    except Exception as e:
        return {
            "success": False,
            "error_message": f"Failed to generate seed placement: {e}",
            "component_count": len(components),
            "net_count": len(nets),
        }

    # Evaluate seed
    seed_score = _evaluate_vector(
        seed_vector, components, nets, rules, board_outline, cost_config, footprint_sizes
    )

    # Initialize optimizer
    config = StrategyConfig(
        max_iterations=max_iterations,
        seed=42,
    )
    initial_population = optimizer.initialize(placement_bounds, config)

    # Evaluate initial population
    initial_scores = []
    for candidate in initial_population:
        score = _evaluate_vector(
            candidate, components, nets, rules, board_outline, cost_config, footprint_sizes
        )
        initial_scores.append(score.total)
    optimizer.observe(initial_population, initial_scores)

    # Optimization loop
    start_time = time.monotonic()
    convergence_data: list[dict[str, Any]] = []
    iteration = 0

    try:
        for iteration in range(1, max_iterations + 1):
            if optimizer.converged:
                break

            pop_size = optimizer._population_size
            candidates = optimizer.suggest(pop_size)

            scores = []
            for candidate in candidates:
                score = _evaluate_vector(
                    candidate,
                    components,
                    nets,
                    rules,
                    board_outline,
                    cost_config,
                    footprint_sizes,
                )
                scores.append(score.total)

            optimizer.observe(candidates, scores)

            # Record convergence snapshot every 10 iterations
            if iteration % 10 == 0 or iteration == 1:
                best_vec, best_score = optimizer.best()
                convergence_data.append(
                    {
                        "iteration": iteration,
                        "best_score": round(best_score, 6),
                    }
                )

    except Exception as e:
        logger.warning("Optimization interrupted: %s", e)

    elapsed = time.monotonic() - start_time

    # Get final result
    best_vector, best_score_value = optimizer.best()
    final_score = _evaluate_vector(
        best_vector, components, nets, rules, board_outline, cost_config, footprint_sizes
    )

    # Calculate improvement
    if seed_score.total > 0:
        improvement_pct = (seed_score.total - final_score.total) / seed_score.total * 100
    else:
        improvement_pct = 0.0

    result: dict[str, Any] = {
        "success": True,
        "initial_score": {
            "total": round(seed_score.total, 4),
            "feasible": seed_score.is_feasible,
            "breakdown": _breakdown_to_dict(seed_score.breakdown),
        },
        "final_score": {
            "total": round(final_score.total, 4),
            "feasible": final_score.is_feasible,
            "breakdown": _breakdown_to_dict(final_score.breakdown),
        },
        "improvement_pct": round(improvement_pct, 2),
        "iterations": iteration,
        "converged": optimizer.converged,
        "wall_time_s": round(elapsed, 3),
        "feasible": final_score.is_feasible,
        "component_count": len(components),
        "net_count": len(nets),
        "convergence_data": convergence_data,
    }

    # Write output if requested
    if output_path:
        try:
            _write_placements_to_pcb(pcb_path, output_path, best_vector, components)
            result["output_path"] = output_path
        except Exception as e:
            result["warnings"] = [f"Optimization succeeded but save failed: {e}"]

    return result


def evaluate_placement(
    pcb_path: str,
    weights: dict[str, float] | None = None,
) -> dict[str, Any]:
    """Evaluate current placement quality of a PCB without optimizing.

    Reads the board file, extracts component positions and net connectivity,
    and computes a placement quality score using the composite cost function.
    Useful for agents to assess board quality before deciding whether to
    optimize.

    Args:
        pcb_path: Absolute path to .kicad_pcb file.
        weights: Optional cost function weight overrides. Keys:
            overlap, drc, boundary, wirelength, area.

    Returns:
        Dictionary with placement evaluation results:
        - success: Whether evaluation completed.
        - score: Total placement score (lower is better).
        - feasible: Whether placement is feasible (no overlaps/violations).
        - breakdown: Per-component score breakdown (wirelength, overlap, DRC, area).
        - component_count: Number of components.
        - net_count: Number of nets.
        - board_dimensions: Board width and height in mm.
        - error_message: Error description if success is False.

    Raises:
        FileNotFoundError: If the PCB file does not exist.
        ParseError: If the PCB file cannot be parsed.
    """
    _validate_pcb_path(pcb_path)

    # Parse board data
    try:
        components, nets, board_outline, rules = _read_board_data(pcb_path)
    except Exception as e:
        raise ParseError(f"Failed to parse PCB file: {e}") from e

    if not components:
        return {
            "success": False,
            "error_message": "No components found in PCB file",
            "component_count": 0,
            "net_count": 0,
        }

    cost_config = _parse_weights(weights)
    footprint_sizes = _build_footprint_sizes(components)

    # Build placements from current footprint positions
    # We read positions directly from the PCB object
    from kicad_tools.schema.pcb import PCB as SchemaPCB

    pcb = SchemaPCB.load(pcb_path)
    current_placements: list[ComponentPlacement] = []
    for fp in pcb.footprints:
        if not fp.reference:
            continue
        current_placements.append(
            ComponentPlacement(
                reference=fp.reference,
                x=fp.position[0],
                y=fp.position[1],
                rotation=fp.rotation,
            )
        )

    if not current_placements:
        return {
            "success": False,
            "error_message": "No components with positions found in PCB file",
            "component_count": 0,
            "net_count": 0,
        }

    # Evaluate placement
    score = cost_evaluate_placement(
        current_placements, nets, rules, board_outline, cost_config, footprint_sizes
    )

    return {
        "success": True,
        "score": round(score.total, 4),
        "feasible": score.is_feasible,
        "breakdown": _breakdown_to_dict(score.breakdown),
        "component_count": len(components),
        "net_count": len(nets),
        "board_dimensions": {
            "width_mm": round(board_outline.width, 2),
            "height_mm": round(board_outline.height, 2),
        },
    }


# ---------------------------------------------------------------------------
# PCB output writer
# ---------------------------------------------------------------------------


def _write_placements_to_pcb(
    pcb_path: str,
    output_path: str,
    vector: PlacementVector,
    components: Sequence[ComponentDef],
) -> None:
    """Write optimized placements back to a .kicad_pcb file.

    Reads the original file, updates footprint positions, and writes
    the result.
    """
    import re

    placed = decode(vector, components)
    ref_to_placement = {p.reference: p for p in placed}

    pcb_content = Path(pcb_path).read_text()

    lines = pcb_content.split("\n")
    output_lines: list[str] = []
    current_ref: str | None = None
    in_footprint = False
    paren_depth = 0

    for line in lines:
        stripped = line.strip()

        if stripped.startswith("(footprint "):
            in_footprint = True
            paren_depth = 0
            current_ref = None

        if in_footprint:
            paren_depth += stripped.count("(") - stripped.count(")")

            ref_match = re.match(r'\s*\(fp_text\s+reference\s+"?([^")\s]+)"?\s', stripped)
            if not ref_match:
                ref_match = re.match(r'\s*\(property\s+"Reference"\s+"([^"]+)"', stripped)
            if ref_match:
                current_ref = ref_match.group(1)

            if current_ref and current_ref in ref_to_placement:
                at_match = re.match(
                    r"(\s*)\(at\s+[\d.eE+-]+\s+[\d.eE+-]+(?:\s+[\d.eE+-]+)?\)", stripped
                )
                if at_match:
                    p = ref_to_placement[current_ref]
                    indent = at_match.group(1)
                    new_at = f"{indent}(at {p.x:.6f} {p.y:.6f} {p.rotation:.0f})"
                    output_lines.append(new_at)
                    if paren_depth <= 0:
                        in_footprint = False
                        current_ref = None
                    continue

            if paren_depth <= 0:
                in_footprint = False
                current_ref = None

        output_lines.append(line)

    Path(output_path).write_text("\n".join(output_lines))
