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
from typing import TYPE_CHECKING, Any, Sequence

# Board reading, net anchor weighting, footprint sizing, the placement writer and
# the whole decoupling pipeline are the CLI's own (``kct optimize-placement``):
# one implementation, so the MCP tool and the CLI see the same board, score it
# the same way and write the same file (issue #6253).
from kicad_tools.cli.optimize_placement_cmd import (
    _board_pins,
    _build_decoupling_context,
    _compute_net_anchor_weight,  # noqa: F401  (re-exported)
    _decoupling_report,
    _evaluate,
    _footprint_size_from_pads,  # noqa: F401  (re-exported)
    _locked_refs,
    _read_board_data,
    _read_current_vector,
    _with_sides,
    _without_caps,
    _write_placements_to_pcb,
    snap_decoupling,
    weights_to_cost_config,
)
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
    FIELDS_PER_COMPONENT,
    ComponentDef,
    PlacementVector,
    bounds,
    decode,
)
from kicad_tools.placement.wirelength import (
    compare_wirelength_estimators,
    compute_per_footprint_ratsnest,
)

if TYPE_CHECKING:
    from kicad_tools.placement.bo_strategy import BayesianOptStrategy
    from kicad_tools.placement.cmaes_strategy import CMAESStrategy

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


def _build_placed_components(
    placements: Sequence[ComponentPlacement],
    components: Sequence[ComponentDef],
) -> list:
    """Build PlacedComponent objects from ComponentPlacement + ComponentDef.

    Creates a PlacementVector from the placement positions and decodes it
    with the component definitions to produce PlacedComponent objects that
    have fully transformed pad coordinates.

    Args:
        placements: Current component positions.
        components: Component definitions with pad geometry.

    Returns:
        List of PlacedComponent with transformed pad coordinates.
    """
    import numpy as np

    ref_to_placement = {p.reference: p for p in placements}

    n = len(components)
    data = np.zeros(n * FIELDS_PER_COMPONENT, dtype=np.float64)
    for i, comp_def in enumerate(components):
        cp = ref_to_placement.get(comp_def.reference)
        if cp is None:
            continue
        base = i * FIELDS_PER_COMPONENT
        data[base] = cp.x
        data[base + 1] = cp.y
        data[base + 2] = float(int(round(cp.rotation / 90.0)) % 4)
        data[base + 3] = 0.0  # side: assume front

    vector = PlacementVector(data=data)
    return decode(vector, components)


def _breakdown_to_dict(breakdown: CostBreakdown) -> dict[str, float]:
    """Convert a CostBreakdown to a serializable dict."""
    return {
        "wirelength": round(breakdown.wirelength, 4),
        "overlap": round(breakdown.overlap, 4),
        "boundary": round(breakdown.boundary, 4),
        "drc": round(breakdown.drc, 4),
        "area": round(breakdown.area, 4),
        "decoupling": round(breakdown.decoupling, 4),
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
    pre_slide_off: bool = True,
    anchor_weight: float = 0.0,
) -> dict[str, Any]:
    """Optimize component placement on a PCB board using CMA-ES.

    Runs the placement optimization loop: reads component/net data from the
    board file, generates a seed placement, runs CMA-ES optimization, and
    returns the optimized result with convergence data.

    Args:
        pcb_path: Absolute path to .kicad_pcb file.
        strategy: Optimization strategy name. "cmaes" (default) or "bayesian" (needs the optional
            ``bayesian`` extra).
        max_iterations: Maximum number of optimization iterations.
        weights: Optional cost function weight overrides. Keys:
            overlap, drc, boundary, wirelength, area, creepage, cohesion,
            decoupling, plus ``mode`` ("lexicographic" or "weighted_sum").
            Same keys and defaults as ``kct optimize-placement --weights``.
            The decoupling-capacitor affinity term (default weight 2.0)
            pulls each decoupling cap onto the IC supply pin it serves, and
            a post-optimize snap pass finishes the job; pass
            ``{"decoupling": 0}`` to turn both off.
        seed_method: Seed placement method ("force-directed" or "random").
        output_path: Path for output file. If None, does not write to disk.
        pre_slide_off: If True, run slide-off overlap resolution on the seed
            placement before passing it to the optimizer.
        anchor_weight: When > 0, every net touching a footprint with the
            KiCad ``(locked)`` attribute gets a wirelength multiplier of
            ``1 + anchor_weight * anchor_pad_fraction``. Default 0.0
            preserves uniform per-net weighting (regression-safe).
            Recommended starting range: 2.0..5.0 for boards with
            perimeter-anchored signals (connectors, edge sense FETs).

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
        - decoupling: Per-cap assignment of the final placement (net, cap,
          supply pin, distance in mm). Empty when the term is off or the
          board has no cap/supply-pin structure.
        - decoupling_snap_moves: Caps the snap pass moved (cap, pin, before_mm,
          after_mm).
        - convergence_data: List of (iteration, best_score) snapshots.
        - error_message: Error description if success is False.

    Raises:
        FileNotFoundError: If the PCB file does not exist.
        ParseError: If the PCB file cannot be parsed.
        ValueError: If anchor_weight is negative.
    """
    _validate_pcb_path(pcb_path)

    if anchor_weight < 0.0:
        raise ValueError(f"anchor_weight must be >= 0 (got {anchor_weight})")

    # Parse board data
    try:
        components, nets, board_outline, rules, board_origin = _read_board_data(
            pcb_path,
            anchor_weight=anchor_weight,
        )
    except Exception as e:
        raise ParseError(f"Failed to parse PCB file: {e}") from e

    if not components:
        return {
            "success": False,
            "error_message": "No components found in PCB file",
            "component_count": 0,
            "net_count": 0,
        }

    try:
        cost_config = weights_to_cost_config(weights)
    except ValueError as e:
        return {
            "success": False,
            "error_message": str(e),
            "component_count": len(components),
            "net_count": len(nets),
        }
    footprint_sizes = _build_footprint_sizes(components)
    placement_bounds = bounds(board_outline, components)

    # Same scoring context as ``kct optimize-placement``: side flags pinned to
    # the board's own (the writer cannot flip footprints) and the decoupling-cap
    # affinity term, on by default, off with ``weights={"decoupling": 0}``.
    # Locked footprints keep their board pose as well (issue #6262).
    locked_refs = _locked_refs(pcb_path)
    fixed_sides = _board_pins(_read_current_vector(pcb_path, components), components, locked_refs)
    locked_indices = sorted(fixed_sides.poses)
    decoupling_groups = _build_decoupling_context(pcb_path, nets, cost_config, quiet=True)

    def _score(vec: PlacementVector) -> PlacementScore:
        return _evaluate(
            vec,
            components,
            nets,
            rules,
            board_outline,
            cost_config,
            footprint_sizes,
            decoupling_groups=decoupling_groups,
            fixed_sides=fixed_sides,
        )

    # Create strategy.  Annotated with the concrete union (not the abstract
    # PlacementStrategy) because the loop below reads ``_population_size``,
    # which both concrete strategies expose but the base class does not.
    optimizer: CMAESStrategy | BayesianOptStrategy
    try:
        if strategy == "cmaes":
            from kicad_tools.placement.cmaes_strategy import CMAESStrategy

            optimizer = CMAESStrategy()
        elif strategy == "bayesian":
            # Raises ImportError if the optional 'bayesian' extra is missing.
            from kicad_tools.placement.bo_strategy import BayesianOptStrategy

            optimizer = BayesianOptStrategy()
        else:
            return {
                "success": False,
                "error_message": f"Unknown strategy: {strategy!r}. Available: cmaes, bayesian",
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

    seed_vector = _with_sides(seed_vector, fixed_sides)

    # Apply slide-off pre-processing to resolve seed overlaps
    if pre_slide_off:
        from kicad_tools.placement.slide_off import slide_off_overlaps

        seed_vector, _slide_result = slide_off_overlaps(
            seed_vector,
            components,
            board_outline,
            fixed=locked_indices,
        )

    # Evaluate seed
    seed_score = _score(seed_vector)

    # Initialize optimizer
    config = StrategyConfig(
        max_iterations=max_iterations,
        seed=42,
    )
    initial_population = optimizer.initialize(placement_bounds, config)

    # Evaluate initial population
    initial_scores = []
    for candidate in initial_population:
        score = _score(candidate)
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
                score = _score(candidate)
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
    best_vector = _with_sides(best_vector, fixed_sides)

    # Post-convergence slide-off pass to resolve residual overlaps
    post_slide_result = None
    if pre_slide_off:
        from kicad_tools.placement.slide_off import slide_off_overlaps as _post_slide_off

        best_vector, post_slide_result = _post_slide_off(
            best_vector,
            components,
            board_outline,
            max_iterations=50,
            max_displacement_mm=50.0,
            fixed=locked_indices,
        )

    # Decoupling-cap snap: the CLI's own post-optimize pass (issue #6020).
    best_vector, snap_moves = snap_decoupling(
        best_vector,
        pcb_path,
        components,
        nets,
        rules,
        board_outline,
        cost_config,
        footprint_sizes,
        _without_caps(decoupling_groups, locked_refs),
        fixed_sides=fixed_sides,
    )

    final_score = _score(best_vector)

    # Calculate improvement
    if seed_score.total > 0:
        improvement_pct = (seed_score.total - final_score.total) / seed_score.total * 100
    else:
        improvement_pct = 0.0

    # Compute per-footprint ratsnest on the best placement
    best_placed = decode(best_vector, components)
    ratsnest_list = compute_per_footprint_ratsnest(best_placed, nets)

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
        "decoupling": _decoupling_report(best_vector, components, decoupling_groups),
        "decoupling_snap_moves": [
            {
                "cap": m.cap,
                "pin": m.pin,
                "before_mm": round(m.before_mm, 3),
                "after_mm": round(m.after_mm, 3),
            }
            for m in snap_moves
        ],
        "per_component_ratsnest": [
            {"reference": fr.reference, "ratsnest_mm": fr.ratsnest_mm} for fr in ratsnest_list
        ],
    }

    # Include overlap details when post-pass found unresolvable overlaps
    if post_slide_result is not None and post_slide_result.overlaps_remaining > 0:
        result["unresolved_overlaps"] = [
            {
                "ref1": d.ref1,
                "ref2": d.ref2,
                "actual_clearance_mm": d.actual_clearance_mm,
                "required_clearance_mm": d.required_clearance_mm,
            }
            for d in post_slide_result.overlap_details
        ]

    # Write output if requested
    if output_path:
        try:
            _write_placements_to_pcb(pcb_path, output_path, best_vector, components, board_origin)
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
        - wirelength_estimators: The centre-anchored and pad-anchored
          wirelength of this same layout side by side, with their delta and
          which one ``score`` was computed from (issue #4831 M5). Report-only:
          it does not change ``score`` or ``breakdown``.
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
        components, nets, board_outline, rules, _origin = _read_board_data(pcb_path)
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

    # Compute per-footprint ratsnest distances
    placed_components = _build_placed_components(current_placements, components)
    ratsnest_list = compute_per_footprint_ratsnest(placed_components, nets)

    # Both wirelength estimators on the same layout (issue #4831 M5). The MCP
    # objective is centre-anchored, so "scored" is always "centre" here; the
    # pad-anchored number is report-only evidence for whether that should
    # change. See docs/placement-pad-anchoring-audit.md.
    estimators = compare_wirelength_estimators(placed_components, nets, scored="centre")

    return {
        "success": True,
        "score": round(score.total, 4),
        "feasible": score.is_feasible,
        "breakdown": _breakdown_to_dict(score.breakdown),
        "wirelength_estimators": estimators.as_dict(ndigits=4),
        "component_count": len(components),
        "net_count": len(nets),
        "board_dimensions": {
            "width_mm": round(board_outline.width, 2),
            "height_mm": round(board_outline.height, 2),
        },
        "per_component_ratsnest": [
            {"reference": fr.reference, "ratsnest_mm": fr.ratsnest_mm} for fr in ratsnest_list
        ],
    }


# ---------------------------------------------------------------------------
# PCB output writer
# ---------------------------------------------------------------------------


# ---------------------------------------------------------------------------
# Standalone overlap resolution tool
# ---------------------------------------------------------------------------


def resolve_placement_overlaps(
    pcb_path: str,
    output_path: str | None = None,
    margin_mm: float = 0.5,
    max_iterations: int = 5,
    max_displacement_mm: float = 20.0,
) -> dict[str, Any]:
    """Resolve component overlaps in a PCB using the slide-off algorithm.

    Reads component positions from the board file, runs iterative
    slide-off overlap resolution, and optionally writes the modified
    board to *output_path*.

    Args:
        pcb_path: Absolute path to .kicad_pcb file.
        output_path: Path for output file. If None, does not write to disk.
        margin_mm: Extra clearance margin beyond zero overlap (mm).
        max_iterations: Maximum settling iterations.
        max_displacement_mm: Maximum per-component displacement (mm).

    Returns:
        Dictionary with overlap resolution results:
        - success: Whether resolution completed successfully.
        - overlaps_resolved: Number of overlap pairs resolved.
        - overlaps_remaining: Number of overlap pairs still present.
        - iterations_run: Number of iterations executed.
        - max_displacement_applied: Maximum component displacement (mm).
        - component_count: Number of components processed.
        - output_path: Path to output file (if written).
        - error_message: Error description if success is False.
    """
    _validate_pcb_path(pcb_path)

    try:
        components, nets, board_outline, rules, board_origin = _read_board_data(pcb_path)
    except Exception as e:
        raise ParseError(f"Failed to parse PCB file: {e}") from e

    if not components:
        return {
            "success": False,
            "error_message": "No components found in PCB file",
            "component_count": 0,
        }

    # Build a PlacementVector from current positions
    from kicad_tools.schema.pcb import PCB as SchemaPCB

    pcb = SchemaPCB.load(pcb_path)

    from kicad_tools.placement.vector import (
        FIELDS_PER_COMPONENT as FPC,
    )
    from kicad_tools.placement.vector import (
        PlacementVector as PV,
    )

    # Map reference -> ComponentDef index
    ref_to_idx = {c.reference: i for i, c in enumerate(components)}

    import numpy as np

    data = np.zeros(len(components) * FPC, dtype=np.float64)
    for fp in pcb.footprints:
        ref = fp.reference
        if not ref or ref not in ref_to_idx:
            continue
        idx = ref_to_idx[ref]
        base = idx * FPC
        data[base] = fp.position[0]
        data[base + 1] = fp.position[1]
        rot_idx = int(round(fp.rotation / 90.0)) % 4
        data[base + 2] = float(rot_idx)
        # Determine side from layer
        layer = getattr(fp, "layer", "F.Cu")
        data[base + 3] = 1.0 if "B." in str(layer) else 0.0

    vector = PV(data=data)

    # Locked footprints are never moved by the writer (issue #6262), so the
    # slide-off must treat them as immovable: otherwise it reports an overlap
    # resolved by pushing a locked part whose move is then dropped on write.
    locked_refs = _locked_refs(pcb_path)
    locked_indices = sorted(i for i, c in enumerate(components) if c.reference in locked_refs)

    # Run slide-off
    from kicad_tools.placement.slide_off import slide_off_overlaps

    new_vector, slide_result = slide_off_overlaps(
        vector,
        components,
        board_outline,
        margin_mm=margin_mm,
        max_iterations=max_iterations,
        max_displacement_mm=max_displacement_mm,
        fixed=locked_indices,
    )

    result: dict[str, Any] = {
        "success": True,
        "overlaps_resolved": slide_result.overlaps_resolved,
        "overlaps_remaining": slide_result.overlaps_remaining,
        "iterations_run": slide_result.iterations_run,
        "max_displacement_applied": round(slide_result.max_displacement_applied, 4),
        "component_count": len(components),
    }

    if output_path:
        try:
            _write_placements_to_pcb(pcb_path, output_path, new_vector, components, board_origin)
            result["output_path"] = output_path
        except Exception as e:
            result["warnings"] = [f"Resolution succeeded but save failed: {e}"]

    return result
