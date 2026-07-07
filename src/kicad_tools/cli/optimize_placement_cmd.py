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

import contextlib
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
    CostMode,
    DesignRuleSet,
    Net,
    PlacementCostConfig,
    PlacementScore,
    evaluate_placement,
)
from kicad_tools.placement.geometry import extract_board_outline as _extract_board_outline
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
            pcb_path,
            output_path,
            best_vector,
            components,
            board_origin,
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
        with contextlib.suppress(OSError):
            Path(tmp_path).unlink(missing_ok=True)
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
    """Parse a JSON string into PlacementCostConfig.

    Defaults to :class:`CostMode.LEXICOGRAPHIC` so that the optimizer's
    convergence check (issue #2821) can use the feasibility-sentinel
    score (>= 1e12) to refuse early convergence in the infeasible region.

    Callers can override the mode via the ``"mode"`` key in the JSON
    payload (``"lexicographic"`` or ``"weighted_sum"``).
    """
    defaults = {
        "overlap_weight": 1e6,
        "drc_weight": 1e4,
        "boundary_weight": 1e5,
        "wirelength_weight": 1.0,
        "area_weight": 0.1,
        "mode": CostMode.LEXICOGRAPHIC,
    }

    if weights_json is None:
        return PlacementCostConfig(**defaults)
    try:
        data = json.loads(weights_json)
    except json.JSONDecodeError as e:
        print(f"Error: invalid JSON for --weights: {e}", file=sys.stderr)
        raise SystemExit(1) from e

    mode = defaults["mode"]
    raw_mode = data.get("mode")
    if raw_mode is not None:
        try:
            mode = CostMode(raw_mode)
        except ValueError as e:
            valid = ", ".join(m.value for m in CostMode)
            print(
                f"Error: invalid 'mode' in --weights JSON (got {raw_mode!r}; valid: {valid})",
                file=sys.stderr,
            )
            raise SystemExit(1) from e

    return PlacementCostConfig(
        overlap_weight=data.get("overlap", defaults["overlap_weight"]),
        drc_weight=data.get("drc", defaults["drc_weight"]),
        boundary_weight=data.get("boundary", defaults["boundary_weight"]),
        wirelength_weight=data.get("wirelength", defaults["wirelength_weight"]),
        area_weight=data.get("area", defaults["area_weight"]),
        mode=mode,
    )


def _create_strategy(strategy_name: str) -> PlacementStrategy:
    """Create a placement strategy by name."""
    if strategy_name == "cmaes":
        from kicad_tools.placement.cmaes_strategy import CMAESStrategy

        return CMAESStrategy()
    else:
        raise ValueError(f"Unknown strategy: {strategy_name!r}. Available: cmaes")


def _read_current_vector(
    pcb_path: str,
    components: Sequence[ComponentDef],
) -> PlacementVector:
    """Encode the current on-disk footprint placement as a PlacementVector.

    Unlike :func:`_generate_seed`, this reads the actual footprint positions
    from the ``.kicad_pcb`` file so that ``--dry-run`` scores the *layout as
    placed* rather than a freshly generated seed (issue #3940).

    Positions are read via ``PCB.load``, which converts footprint coordinates
    to board-relative space (offset by the detected board origin) -- the same
    coordinate space :func:`evaluate_placement` and the writer operate in.
    Rotation is snapped to the nearest 90-degree step and ``side`` is derived
    from the footprint layer (``B.Cu`` -> back).

    Args:
        pcb_path: Path to the ``.kicad_pcb`` file.
        components: Component definitions in the order produced by
            :func:`_read_board_data`. The returned vector uses this same
            order so ``decode(vector, components)`` round-trips correctly.

    Returns:
        A :class:`PlacementVector` encoding the current placement, aligned to
        ``components`` order. Components absent from the PCB (should not happen
        for vectors derived from the same file) default to the origin.
    """
    from kicad_tools.placement.vector import PlacedComponent, encode
    from kicad_tools.schema.pcb import PCB as SchemaPCB

    pcb = SchemaPCB.load(pcb_path)

    # Map reference -> (x, y, rotation, side) from the current placement.
    current: dict[str, tuple[float, float, float, int]] = {}
    for fp in pcb.footprints:
        ref = fp.reference
        if not ref:
            continue
        x, y = fp.position
        side = 1 if fp.layer == "B.Cu" else 0
        current[ref] = (x, y, fp.rotation, side)

    placed: list[PlacedComponent] = []
    for comp in components:
        x, y, rot, side = current.get(comp.reference, (0.0, 0.0, 0.0, 0))
        placed.append(
            PlacedComponent(
                reference=comp.reference,
                x=x,
                y=y,
                rotation=rot,
                side=side,
            )
        )

    return encode(placed)


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
    *,
    anchor_weight: float = 0.0,
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

    Args:
        pcb_path: Path to .kicad_pcb file.
        anchor_weight: When > 0, every net touching at least one ``(locked)``
            footprint receives ``Net.weight = 1 + anchor_weight * f``, where
            ``f`` is the fraction of the net's pins that land on locked
            footprints (range 0..1). Default 0.0 preserves uniform weighting.
    """
    from kicad_tools.placement.vector import PadDef
    from kicad_tools.schema.pcb import PCB as SchemaPCB

    pcb = SchemaPCB.load(pcb_path)

    # --- Board outline -- try Shapely geometry, fall back to legacy AABB ---
    board_outline: BoardOutline | None = None
    try:
        from kicad_tools.pcb.board_geometry import BoardGeometry, has_shapely

        if has_shapely():
            try:
                board_geom = BoardGeometry.from_pcb(pcb)
                board_outline = board_geom.to_board_outline()
            except (ValueError, Exception):
                pass
    except ImportError:
        pass
    if board_outline is None:
        board_outline = _extract_board_outline(pcb)

    # --- Components from footprints ---
    # Track which footprints carry the (locked) attribute so we can later
    # compute per-net anchor fractions for weighted wirelength.
    locked_refs: set[str] = set()
    components: list[ComponentDef] = []
    for fp in pcb.footprints:
        ref = fp.reference
        if not ref:
            continue

        if getattr(fp, "locked", False):
            locked_refs.add(ref)

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
        if len(pins) < 2:
            continue
        weight = _compute_net_anchor_weight(pins, locked_refs, anchor_weight)
        nets.append(Net(name=net_name, pins=pins, weight=weight))

    # --- Design rules (use defaults; PCB setup has limited rule info) ---
    rules = DesignRuleSet()

    return components, nets, board_outline, rules, pcb.board_origin


def _compute_net_anchor_weight(
    pins: Sequence[tuple[str, str]],
    locked_refs: set[str],
    anchor_weight: float,
) -> float:
    """Compute the per-net wirelength weight from anchor pad fraction.

    A pin contributes to the "anchored" count when its component reference
    appears in ``locked_refs`` (set of footprints carrying the ``(locked)``
    attribute). The returned weight is::

        1.0 + anchor_weight * (anchored_pins / total_pins)

    For ``anchor_weight <= 0`` the weight collapses to 1.0 (regression-safe
    default). Nets with no anchored pins also collapse to 1.0.
    """
    if anchor_weight <= 0.0 or not pins or not locked_refs:
        return 1.0
    anchored = sum(1 for ref, _ in pins if ref in locked_refs)
    if anchored == 0:
        return 1.0
    fraction = anchored / len(pins)
    return 1.0 + anchor_weight * fraction


# _extract_board_outline is imported from kicad_tools.placement.geometry
# (consolidated in #2349).


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
    anchor_weight: float = 0.0,
    time_budget: float | None = None,
    allow_infeasible: bool = False,
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
        anchor_weight: When > 0, nets touching a ``(locked)`` footprint
            receive an inflated wirelength weight of
            ``1 + anchor_weight * anchor_pad_fraction``. Default 0.0
            preserves the historical uniform weighting (regression-safe).
        time_budget: Wall-clock budget in seconds. The main optimization
            loop exits as soon as the elapsed time exceeds this value
            (after completing the current generation). ``None`` means no
            wall-clock cap (issue #2821). Used to bound the new
            "keep going past plateau while infeasible" behaviour.
        allow_infeasible: When True, the command returns exit code 0 even
            if the final placement is infeasible (overlap/DRC/boundary
            violations remain). Default behaviour is to exit 1 with a
            ``FATAL:`` message on stderr in that case (issue #2821).

    Returns:
        Exit code:
            * 0 -- final placement is feasible (or ``allow_infeasible=True``)
            * 1 -- input error, write error, infeasible final placement, or
              unresolved pad-pad overlaps from the post-pass slide-off
            * 2 -- interrupted (SIGINT/SIGTERM); partial result saved
    """
    # Validate PCB file exists
    pcb_file = Path(pcb_path)
    if not pcb_file.exists():
        print(f"Error: PCB file not found: {pcb_path}", file=sys.stderr)
        return 1
    if pcb_file.suffix != ".kicad_pcb":
        print(f"Error: expected .kicad_pcb file, got: {pcb_file.suffix}", file=sys.stderr)
        return 1
    if anchor_weight < 0.0:
        print(
            f"Error: --anchor-weight must be >= 0 (got {anchor_weight})",
            file=sys.stderr,
        )
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
        components, nets, board_outline, rules, board_origin = _read_board_data(
            pcb_path,
            anchor_weight=anchor_weight,
        )
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

    # --dry-run: evaluate the CURRENT on-disk placement (issue #3940).
    # Previously this generated a fresh force-directed seed and scored that,
    # so the reported ovl/drc reflected a randomized layout rather than the
    # footprints as placed -- making the check meaningless. We now encode the
    # actual positions read from the .kicad_pcb file.
    if dry_run:
        if not quiet:
            print("\n[dry-run] Evaluating current placement...")

        current_vector = _read_current_vector(pcb_path, components)
        score = _evaluate(
            current_vector,
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
            # The optimizer objective (bounding-box overlap area in mm^2 and a
            # bbox-clearance DRC count) is a distinct metric from
            # `kct placement check`, which uses courtyard-expanded polygons and
            # real KiCad DRC. The two surfaces can disagree by the courtyard
            # margin for touching footprints; this is intentional. See
            # docs/placement-scoring.md for the full comparison.
            print(
                "\n  Note: this is the optimizer objective (bbox overlap area / "
                "bbox-clearance DRC), NOT the `kct placement check` metric "
                "(courtyard polygons / KiCad DRC). See docs/placement-scoring.md."
            )
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

            # Wall-clock budget check (issue #2821): exit gracefully if
            # the configured time budget has been exceeded. Checked
            # before each generation so the most recent best is preserved.
            if time_budget is not None and (time.monotonic() - start_time) >= time_budget:
                if not quiet:
                    elapsed_now = time.monotonic() - start_time
                    print(
                        f"  Time budget exhausted at iteration {iteration} "
                        f"(elapsed={elapsed_now:.1f}s, budget={time_budget:.1f}s)"
                    )
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

    # --- Post-convergence overlap resolution pass ---
    post_slide_result = None
    if not no_slide_off:
        from kicad_tools.placement.slide_off import slide_off_overlaps

        best_vector, post_slide_result = slide_off_overlaps(
            best_vector,
            components,
            board_outline,
            max_iterations=50,
            max_displacement_mm=50.0,
        )
        if not quiet and post_slide_result.overlaps_resolved > 0:
            print(
                f"\n  Post-pass slide-off: resolved {post_slide_result.overlaps_resolved} "
                f"overlaps ({post_slide_result.overlaps_remaining} remaining)"
            )

    # Evaluate final result for full breakdown (after post-pass)
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

        # Per-axis breakdown. We deliberately avoid a single "Improvement: X%"
        # line because, under LEXICOGRAPHIC mode, the absolute total is
        # dominated by the INFEASIBILITY_OFFSET (~1e12) and a real improvement
        # of ~1e9 inside the infeasible region rounds to "0.0%". See #2828.
        si, sf = seed_score.breakdown, final_score.breakdown

        def _delta(initial: float, final: float, *, fmt: str = ".2f") -> str:
            if initial == 0 and final == 0:
                return f"{initial:{fmt}} → {final:{fmt}} (no change)"
            if initial == 0:
                return f"{initial:{fmt}} → {final:{fmt}} (new)"
            pct = (final - initial) / initial * 100
            return f"{initial:{fmt}} → {final:{fmt}} ({pct:+.1f}%)"

        print("\n  Per-axis change:")
        print(f"    Wirelength:    {_delta(si.wirelength, sf.wirelength)}")
        print(f"    Overlap:       {_delta(si.overlap, sf.overlap)}")
        print(f"    Boundary:      {_delta(si.boundary, sf.boundary)}")
        print(f"    DRC:           {si.drc:.0f} → {sf.drc:.0f} ({sf.drc - si.drc:+.0f})")
        print(f"    Area:          {_delta(si.area, sf.area)}")

        # Feasibility transition (categorical, not percent)
        seed_feas = "feasible" if seed_score.is_feasible else "INFEASIBLE"
        final_feas = "feasible" if final_score.is_feasible else "INFEASIBLE"
        print(f"  Feasibility:   {seed_feas} → {final_feas}")

        print(f"  Iterations: {iteration}")
        print(f"  Wall time: {elapsed:.2f}s")
        print(f"  Feasible: {final_score.is_feasible}")

    # Report unresolvable overlaps
    has_unresolved_overlaps = False
    if post_slide_result is not None and post_slide_result.overlaps_remaining > 0:
        has_unresolved_overlaps = True
        if not quiet:
            print(
                f"\n  WARNING: {post_slide_result.overlaps_remaining} "
                f"unresolved overlap(s) after post-pass:"
            )
            for detail in post_slide_result.overlap_details:
                # Courtyard overlaps (actual_clearance >= 0 but within margin)
                # are warnings; pad-pad overlaps (actual_clearance < 0) are errors
                severity = "WARNING" if detail.actual_clearance_mm >= 0 else "ERROR"
                print(f"    {severity}: {detail}")
        else:
            # Even in quiet mode, print errors to stderr
            for detail in post_slide_result.overlap_details:
                if detail.actual_clearance_mm < 0:
                    print(f"ERROR: {detail}", file=sys.stderr)

    # Restore original signal handlers
    signal.signal(signal.SIGINT, prev_sigint)
    signal.signal(signal.SIGTERM, prev_sigterm)

    # Write output (atomic to prevent corruption on hard kill)
    if not quiet:
        print(f"\nWriting result to: {output_path}")

    try:
        _write_placements_to_pcb_atomic(
            pcb_path, output_path, best_vector, components, board_origin
        )
    except Exception as e:
        print(f"Error writing output: {e}", file=sys.stderr)
        if verbose:
            import traceback

            traceback.print_exc()
        return 1

    if not quiet:
        print("Done.")

    # Exit code 2 when interrupted (partial result saved).
    if _interrupt_state["interrupted"]:
        return 2

    # Issue #2821: gate exit code on full feasibility, not just pad-pad
    # slide-off failures. If the final placement has overlap > 0 OR
    # drc > 0 OR boundary > 0 OR block_boundary > 0, the optimizer has
    # produced an illegal placement and downstream consumers (router,
    # DRC) will inherit it. Print a FATAL line and exit non-zero so
    # pipelines like `place_route.py` and `BuildStep.PLACE` can detect
    # the failure. The legacy "exit 0 even when infeasible" behaviour
    # is available via ``--allow-infeasible`` for explicit opt-in
    # debugging / interactive workflows.
    if not final_score.is_feasible:
        b = final_score.breakdown
        components_failing = []
        if b.overlap > 0:
            components_failing.append(f"overlap={b.overlap:.2f}mm^2")
        if b.drc > 0:
            components_failing.append(f"drc={b.drc:.0f}")
        if b.boundary > 0:
            components_failing.append(f"boundary={b.boundary:.2f}")
        if b.block_boundary > 0:
            components_failing.append(f"block_boundary={b.block_boundary:.2f}")
        detail = ", ".join(components_failing) if components_failing else "unknown"

        if not allow_infeasible:
            print(
                f"FATAL: optimizer exited with infeasible placement ({detail}). "
                f"Downstream router/DRC will inherit illegal geometry. "
                f"Pass --allow-infeasible to suppress this error.",
                file=sys.stderr,
            )
            return 1
        elif not quiet:
            print(
                f"\n  WARNING: final placement is infeasible ({detail}); "
                f"--allow-infeasible suppresses non-zero exit."
            )

    # Exit code 1 when pad-pad overlaps remain (actual clearance < 0).
    # Note: with the feasibility gate above this is now mostly redundant
    # (any pad overlap implies overlap > 0 in the cost breakdown). It is
    # preserved as a fallback for callers that disable the feasibility
    # gate by writing custom weights with ``CostMode.WEIGHTED_SUM``,
    # where ``is_feasible`` is still computed but the feasibility gate
    # may behave differently. Skipped under ``--allow-infeasible``.
    if has_unresolved_overlaps and not allow_infeasible:
        pad_overlaps = [d for d in post_slide_result.overlap_details if d.actual_clearance_mm < 0]
        if pad_overlaps:
            return 1

    return 0
