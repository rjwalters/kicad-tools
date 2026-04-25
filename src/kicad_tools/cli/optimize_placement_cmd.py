"""optimize-placement CLI command: run CMA-ES placement optimization on a KiCad PCB.

This is the user-facing entry point that ties together the evaluation pipeline
and optimizer strategy. It reads component/net data, runs the optimization loop
with progress reporting, and writes the result back to a .kicad_pcb file.

Usage:
    kct optimize-placement board.kicad_pcb
    kct optimize-placement board.kicad_pcb --strategy cmaes --max-iterations 500
    kct optimize-placement board.kicad_pcb --dry-run
    kct optimize-placement board.kicad_pcb --checkpoint ./checkpoints
"""

from __future__ import annotations

import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Sequence

from kicad_tools.placement.cost import (
    BoardOutline,
    ComponentPlacement,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    PlacementScore,
    evaluate_placement,
)
from kicad_tools.placement.seed import force_directed_placement, random_placement
from kicad_tools.placement.strategy import PlacementStrategy, StrategyConfig
from kicad_tools.placement.vector import (
    ComponentDef,
    PlacementVector,
    bounds,
    decode,
)


# ---------------------------------------------------------------------------
# Interrupt handling (SIGINT / SIGTERM)
# ---------------------------------------------------------------------------

# Global state for interrupt handling -- mirrors the pattern in route_cmd.py
_interrupt_state: dict = {
    "interrupted": False,
    "best_vector": None,
    "components": None,
    "pcb_path": None,
    "output_path": None,
    "board_origin": (0.0, 0.0),
    "quiet": False,
}


def _handle_placement_interrupt(signum, frame):
    """Handle SIGINT/SIGTERM by saving the best-so-far placement and exiting."""
    _interrupt_state["interrupted"] = True
    quiet = _interrupt_state["quiet"]

    if not quiet:
        sig_name = "SIGTERM" if signum == signal.SIGTERM else "SIGINT"
        print(f"\n  {sig_name} received -- saving best placement so far...")

    saved = _save_best_placement_on_interrupt()

    # Exit code 2 signals "interrupted with partial results saved",
    # distinguishing from normal success (0) and failure (1).
    sys.exit(2 if saved else 130)


def _save_best_placement_on_interrupt() -> bool:
    """Write the best-so-far placement to the output PCB file.

    Uses atomic write (write-to-temp then rename) to prevent corruption if
    the process is killed during the write itself.

    Returns True if the placement was saved successfully.
    """
    best_vector = _interrupt_state["best_vector"]
    components = _interrupt_state["components"]
    pcb_path = _interrupt_state["pcb_path"]
    output_path = _interrupt_state["output_path"]
    quiet = _interrupt_state["quiet"]

    if best_vector is None or components is None or pcb_path is None or output_path is None:
        return False

    try:
        board_origin = _interrupt_state.get("board_origin", (0.0, 0.0))
        _write_placements_to_pcb_atomic(
            pcb_path, output_path, best_vector, components, board_origin,
        )
        if not quiet:
            print(f"  Best placement saved to: {output_path}")
        return True
    except Exception as e:
        if not quiet:
            print(f"  Error saving placement on interrupt: {e}", file=sys.stderr)
        return False


def _write_placements_to_pcb_atomic(
    pcb_path: str,
    output_path: str,
    vector,
    components: Sequence,
    board_origin: tuple[float, float] = (0.0, 0.0),
) -> None:
    """Write placements via atomic write (temp file + rename).

    This prevents corruption if the process is killed mid-write.
    """
    out = Path(output_path)
    # Write to a temp file in the same directory, then rename.
    fd, tmp_path = tempfile.mkstemp(
        dir=str(out.parent),
        prefix=".placement_",
        suffix=".tmp",
    )
    os.close(fd)
    try:
        _write_placements_to_pcb(pcb_path, tmp_path, vector, components, board_origin)
        Path(tmp_path).replace(out)
    except BaseException:
        # Clean up the temp file on failure
        try:
            Path(tmp_path).unlink(missing_ok=True)
        except OSError:
            pass
        raise


def _vector_to_placements(
    vector: PlacementVector,
    components: Sequence[ComponentDef],
) -> list[ComponentPlacement]:
    """Convert a PlacementVector to a list of ComponentPlacement for cost evaluation.

    The cost module uses ComponentPlacement (reference, x, y, rotation) while
    the vector module uses PlacedComponent. This bridges the two.
    """
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


def _evaluate(
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
    return evaluate_placement(placements, nets, rules, board, cost_config, footprint_sizes)


def _build_footprint_sizes(
    components: Sequence[ComponentDef],
) -> dict[str, tuple[float, float]]:
    """Build a footprint_sizes dict from component definitions."""
    return {c.reference: (c.width, c.height) for c in components}


def _parse_weights(weights_json: str | None) -> PlacementCostConfig:
    """Parse a JSON string into PlacementCostConfig, or return defaults."""
    if weights_json is None:
        return PlacementCostConfig()
    try:
        data = json.loads(weights_json)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON for --weights: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    return PlacementCostConfig(
        overlap_weight=data.get("overlap", 1e6),
        drc_weight=data.get("drc", 1e4),
        boundary_weight=data.get("boundary", 1e5),
        wirelength_weight=data.get("wirelength", 1.0),
        area_weight=data.get("area", 0.1),
    )


def _create_strategy(strategy_name: str) -> PlacementStrategy:
    """Create a placement strategy by name."""
    if strategy_name == "cmaes":
        from kicad_tools.placement.cmaes_strategy import CMAESStrategy

        return CMAESStrategy()
    else:
        raise ValueError(f"Unknown strategy: {strategy_name!r}. Available: cmaes")


def _generate_seed(
    seed_method: str,
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
    board: BoardOutline,
) -> PlacementVector:
    """Generate initial seed placement."""
    if seed_method == "force-directed":
        return force_directed_placement(components, nets, board)
    elif seed_method == "random":
        return random_placement(components, board)
    else:
        raise ValueError(f"Unknown seed method: {seed_method!r}. Available: force-directed, random")


def _print_score(label: str, score: PlacementScore) -> None:
    """Print a score summary line."""
    b = score.breakdown
    feasible = "feasible" if score.is_feasible else "INFEASIBLE"
    print(
        f"  {label}: {score.total:.4f} ({feasible}) "
        f"[wl={b.wirelength:.2f} ovl={b.overlap:.2f} bnd={b.boundary:.2f} "
        f"drc={b.drc:.0f} area={b.area:.2f}]"
    )


def _read_board_data(
    pcb_path: str,
) -> tuple[
    list[ComponentDef],
    list[Net],
    BoardOutline,
    DesignRuleSet,
    tuple[float, float],
]:
    """Read component, net, and board data from a .kicad_pcb file.

    Uses kicad_tools.schema.pcb.PCB to parse the file and extract
    components, nets, board outline, design rules, and board origin.

    The returned board origin is needed by the writer to convert
    board-relative optimizer output back to sheet-absolute coordinates.
    """
    from kicad_tools.placement.vector import PadDef
    from kicad_tools.schema.pcb import PCB as SchemaPCB

    pcb = SchemaPCB.load(pcb_path)

    # --- Board outline from Edge.Cuts graphic lines ---
    board_outline = _extract_board_outline(pcb)

    # --- Components from footprints ---
    components: list[ComponentDef] = []
    for fp in pcb.footprints:
        ref = fp.reference
        if not ref:
            continue

        # Compute footprint size from pad extents
        width, height = _footprint_size_from_pads(fp)

        # Build PadDef list (positions are local to footprint origin)
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

    # --- Nets from footprint pad net assignments ---
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

    # --- Design rules (use defaults; PCB setup has limited rule info) ---
    rules = DesignRuleSet()

    return components, nets, board_outline, rules, pcb.board_origin


def _extract_board_outline(pcb) -> BoardOutline:
    """Extract board outline from Edge.Cuts graphic lines.

    Edge.Cuts coordinates are in sheet-absolute space, but footprint
    positions on ``SchemaPCB`` are board-relative (the origin is already
    subtracted).  We must convert the outline to the same board-relative
    coordinate system so the optimizer bounds match component positions.
    """
    xs: list[float] = []
    ys: list[float] = []

    for line in pcb.graphic_lines:
        if line.layer == "Edge.Cuts":
            xs.extend([line.start[0], line.end[0]])
            ys.extend([line.start[1], line.end[1]])

    if xs and ys:
        # Convert from sheet-absolute to board-relative coordinates.
        ox, oy = pcb.board_origin
        xs = [x - ox for x in xs]
        ys = [y - oy for y in ys]
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

    # Default fallback
    return BoardOutline(min_x=0.0, min_y=0.0, max_x=100.0, max_y=100.0)


def _footprint_size_from_pads(fp) -> tuple[float, float]:
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


def _write_placements_to_pcb(
    pcb_path: str,
    output_path: str,
    vector: PlacementVector,
    components: Sequence[ComponentDef],
    board_origin: tuple[float, float] = (0.0, 0.0),
) -> None:
    """Write optimized placements back to a .kicad_pcb file.

    Reads the original file, updates footprint positions, and writes
    the result.  Positions from the optimizer are in board-relative
    coordinates; the board origin offset is added back to produce the
    sheet-absolute values expected in the ``.kicad_pcb`` file.
    """
    placed = decode(vector, components)
    ox, oy = board_origin
    ref_to_placement = {p.reference: p for p in placed}

    # Read the original PCB content
    pcb_content = Path(pcb_path).read_text()

    # Update footprint positions via text replacement.
    # This is a pragmatic approach: for each footprint, find its (at ...) and
    # replace with the new position. This preserves all other PCB structure.
    import re

    # Pattern matches (footprint ... (at x y [angle]) ...)
    # We need to find each footprint block and update its (at ...) line

    lines = pcb_content.split("\n")
    output_lines: list[str] = []
    current_ref: str | None = None
    in_footprint = False
    paren_depth = 0

    for line in lines:
        stripped = line.strip()

        # Track footprint blocks
        if stripped.startswith("(footprint "):
            in_footprint = True
            paren_depth = 0
            current_ref = None

        if in_footprint:
            paren_depth += stripped.count("(") - stripped.count(")")

            # Extract reference
            ref_match = re.match(r'\s*\(fp_text\s+reference\s+"?([^")\s]+)"?\s', stripped)
            if not ref_match:
                ref_match = re.match(r'\s*\(property\s+"Reference"\s+"([^"]+)"', stripped)
            if ref_match:
                current_ref = ref_match.group(1)

            # Update (at ...) inside footprint
            if current_ref and current_ref in ref_to_placement:
                at_match = re.match(
                    r"(\s*)\(at\s+[\d.eE+-]+\s+[\d.eE+-]+(?:\s+[\d.eE+-]+)?\)", stripped
                )
                if at_match:
                    p = ref_to_placement[current_ref]
                    indent = at_match.group(1)
                    # Convert board-relative back to sheet-absolute.
                    abs_x = p.x + ox
                    abs_y = p.y + oy
                    new_at = f"{indent}(at {abs_x:.6f} {abs_y:.6f} {p.rotation:.0f})"
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


def run_optimize_placement(
    pcb_path: str,
    *,
    strategy_name: str = "cmaes",
    max_iterations: int = 1000,
    output_path: str | None = None,
    seed_method: str = "force-directed",
    weights_json: str | None = None,
    dry_run: bool = False,
    progress_interval: int = 0,
    checkpoint_dir: str | None = None,
    verbose: bool = False,
    quiet: bool = False,
    no_slide_off: bool = False,
) -> int:
    """Run placement optimization.

    Args:
        pcb_path: Path to .kicad_pcb file.
        strategy_name: Optimization strategy name.
        max_iterations: Maximum number of optimization iterations.
        output_path: Output file path. Defaults to overwriting input.
        seed_method: Seed placement method (force-directed or random).
        weights_json: JSON string for custom cost weights.
        dry_run: If True, only evaluate current placement.
        progress_interval: Print progress every N iterations (0 = no progress).
        checkpoint_dir: Directory for checkpoint save/resume.
        verbose: Enable verbose output.
        quiet: Suppress non-essential output.
        no_slide_off: If True, skip slide-off overlap pre-processing.

    Returns:
        Exit code (0 for success, 1 for failure).
    """
    # Validate PCB file exists
    pcb_file = Path(pcb_path)
    if not pcb_file.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1
    if pcb_file.suffix != ".kicad_pcb":
        print(f"Error: expected .kicad_pcb file, got: {pcb_file.suffix}", file=sys.stderr)
        return 1

    if output_path is None:
        output_path = pcb_path

    # Install signal handlers for graceful interrupt
    _interrupt_state["pcb_path"] = pcb_path
    _interrupt_state["output_path"] = output_path
    _interrupt_state["quiet"] = quiet
    _interrupt_state["interrupted"] = False
    _interrupt_state["best_vector"] = None
    _interrupt_state["components"] = None

    prev_sigint = signal.signal(signal.SIGINT, _handle_placement_interrupt)
    prev_sigterm = signal.signal(signal.SIGTERM, _handle_placement_interrupt)

    # Parse cost weights
    cost_config = _parse_weights(weights_json)

    if not quiet:
        print(f"Reading board: {pcb_path}")

    # Read board data
    try:
        components, nets, board_outline, rules, board_origin = _read_board_data(pcb_path)
    except Exception as e:
        print(f"Error reading PCB: {e}", file=sys.stderr)
        if verbose:
            import traceback

            traceback.print_exc()
        return 1

    if not components:
        print("Error: no components found in PCB", file=sys.stderr)
        return 1

    # Update interrupt state so handler can save intermediate results
    _interrupt_state["components"] = components
    _interrupt_state["board_origin"] = board_origin

    if not quiet:
        print(f"  Components: {len(components)}")
        print(f"  Nets: {len(nets)}")
        print(f"  Board: {board_outline.width:.1f} x {board_outline.height:.1f} mm")

    footprint_sizes = _build_footprint_sizes(components)

    # Compute bounds
    placement_bounds = bounds(board_outline, components)

    # --dry-run: just evaluate the current placement
    if dry_run:
        if not quiet:
            print("\n[dry-run] Evaluating current placement...")

        # Generate a seed to evaluate (since we don't have current positions
        # encoded as a vector, use force-directed as a proxy)
        seed_vector = _generate_seed(seed_method, components, nets, board_outline)
        score = _evaluate(
            seed_vector,
            components,
            nets,
            rules,
            board_outline,
            cost_config,
            footprint_sizes,
        )
        if not quiet:
            _print_score("Current", score)
            print(f"\n  Feasible: {score.is_feasible}")
            print(f"  Total score: {score.total:.4f}")
        return 0

    # Create strategy
    try:
        strategy = _create_strategy(strategy_name)
    except (ValueError, ImportError) as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1

    # Check for checkpoint to resume from
    resumed = False
    if checkpoint_dir:
        checkpoint_path = Path(checkpoint_dir) / "optimizer_state.json"
        if checkpoint_path.exists():
            if not quiet:
                print(f"\nResuming from checkpoint: {checkpoint_path}")
            try:
                strategy = type(strategy).load_state(checkpoint_path)
                resumed = True
            except Exception as e:
                if not quiet:
                    print(f"  Warning: could not load checkpoint: {e}")
                    print("  Starting fresh optimization...")

    # Configure strategy
    config = StrategyConfig(
        max_iterations=max_iterations,
        seed=42,  # Deterministic by default
    )

    # Initialize or resume
    if not resumed:
        if not quiet:
            print(f"\nGenerating seed placement ({seed_method})...")

        # Generate initial seed
        seed_vector = _generate_seed(seed_method, components, nets, board_outline)

        # Apply slide-off pre-processing
        if not no_slide_off:
            from kicad_tools.placement.slide_off import slide_off_overlaps

            seed_vector, slide_result = slide_off_overlaps(
                seed_vector,
                components,
                board_outline,
            )
            if not quiet:
                print(
                    f"  Slide-off: resolved {slide_result.overlaps_resolved} overlaps "
                    f"({slide_result.overlaps_remaining} remaining, "
                    f"{slide_result.iterations_run} iterations)"
                )

        # Evaluate seed
        seed_score = _evaluate(
            seed_vector,
            components,
            nets,
            rules,
            board_outline,
            cost_config,
            footprint_sizes,
        )
        if not quiet:
            _print_score("Seed", seed_score)

        if not quiet:
            print(f"\nInitializing {strategy_name} optimizer...")

        # Initialize strategy - this generates an initial population
        initial_population = strategy.initialize(placement_bounds, config)

        # Evaluate initial population
        initial_scores = []
        for candidate in initial_population:
            score = _evaluate(
                candidate,
                components,
                nets,
                rules,
                board_outline,
                cost_config,
                footprint_sizes,
            )
            initial_scores.append(score.total)

        strategy.observe(initial_population, initial_scores)

        initial_best_vec, initial_best_score = strategy.best()
    else:
        initial_best_vec, initial_best_score = strategy.best()
        seed_score = _evaluate(
            initial_best_vec,
            components,
            nets,
            rules,
            board_outline,
            cost_config,
            footprint_sizes,
        )

    # Keep interrupt state up-to-date with best vector for graceful save
    _interrupt_state["best_vector"] = initial_best_vec

    if not quiet:
        print(f"  Population size: {strategy._population_size}")
        print(f"  Initial best score: {initial_best_score:.4f}")

    # Optimization loop
    start_time = time.monotonic()
    iteration = 0

    if not quiet:
        print(f"\nOptimizing (max {max_iterations} iterations)...")

    try:
        for iteration in range(1, max_iterations + 1):
            if strategy.converged:
                if not quiet:
                    print(f"  Converged at iteration {iteration}")
                break

            # Ask for new candidates
            pop_size = strategy._population_size
            candidates = strategy.suggest(pop_size)

            # Evaluate candidates
            scores = []
            for candidate in candidates:
                score = _evaluate(
                    candidate,
                    components,
                    nets,
                    rules,
                    board_outline,
                    cost_config,
                    footprint_sizes,
                )
                scores.append(score.total)

            # Feed results back
            strategy.observe(candidates, scores)

            # Update interrupt state with latest best vector
            best_vec_now, _ = strategy.best()
            _interrupt_state["best_vector"] = best_vec_now

            # Progress reporting
            if progress_interval > 0 and iteration % progress_interval == 0:
                best_vec, best_score = strategy.best()
                elapsed = time.monotonic() - start_time
                print(f"  [{iteration:>5d}] score={best_score:.4f} elapsed={elapsed:.1f}s")

            # Periodic checkpoint saving
            if checkpoint_dir and iteration % 100 == 0:
                cp_path = Path(checkpoint_dir) / "optimizer_state.json"
                cp_path.parent.mkdir(parents=True, exist_ok=True)
                strategy.save_state(cp_path)

    except KeyboardInterrupt:
        if not quiet:
            print("\n  Optimization interrupted by user")
        # The signal handler may not have fired if Python caught
        # KeyboardInterrupt before the C-level handler.  Write the
        # best placement inline as a fallback.
        _interrupt_state["interrupted"] = True

    elapsed = time.monotonic() - start_time

    # Get final result
    best_vector, best_score = strategy.best()

    # Evaluate final result for full breakdown
    final_score = _evaluate(
        best_vector,
        components,
        nets,
        rules,
        board_outline,
        cost_config,
        footprint_sizes,
    )

    # Save final checkpoint
    if checkpoint_dir:
        cp_path = Path(checkpoint_dir) / "optimizer_state.json"
        cp_path.parent.mkdir(parents=True, exist_ok=True)
        strategy.save_state(cp_path)
        if not quiet:
            print(f"\n  Checkpoint saved: {cp_path}")

    # Print summary
    if not quiet:
        print("\n--- Optimization Summary ---")
        _print_score("Initial", seed_score)
        _print_score("Final", final_score)

        if seed_score.total > 0:
            improvement = (seed_score.total - final_score.total) / seed_score.total * 100
        else:
            improvement = 0.0

        print(f"\n  Improvement: {improvement:.1f}%")
        print(f"  Iterations: {iteration}")
        print(f"  Wall time: {elapsed:.2f}s")
        print(f"  Feasible: {final_score.is_feasible}")

    # Restore original signal handlers
    signal.signal(signal.SIGINT, prev_sigint)
    signal.signal(signal.SIGTERM, prev_sigterm)

    # Write output (atomic to prevent corruption on hard kill)
    if not quiet:
        print(f"\nWriting result to: {output_path}")

    try:
        _write_placements_to_pcb_atomic(pcb_path, output_path, best_vector, components, board_origin)
    except Exception as e:
        print(f"Error writing output: {e}", file=sys.stderr)
        if verbose:
            import traceback

            traceback.print_exc()
        return 1

    if not quiet:
        print("Done.")

    # Exit code 2 when interrupted (partial result saved), 0 otherwise.
    if _interrupt_state["interrupted"]:
        return 2
    return 0
