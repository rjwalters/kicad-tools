"""optimize-placement CLI command: run CMA-ES placement optimization on a KiCad PCB.

This is the user-facing entry point that ties together the evaluation pipeline
and optimizer strategy. It reads component/net data, runs the optimization loop
with progress reporting, and writes the result back to a .kicad_pcb file.

Usage:
    kct optimize-placement board.kicad_pcb
    kct optimize-placement board.kicad_pcb --strategy cmaes --max-iterations 500
    kct optimize-placement board.kicad_pcb --dry-run
    kct optimize-placement board.kicad_pcb --checkpoint ./checkpoints

Machine output (``--format json``, issue #4674): one document carrying the
board summary, the ``initial``/``final`` score breakdowns, the iteration count
and whether the result was written -- or ``{"error": ..., "success": false}``
on failure, with every exit code (0 / 1 / 2-on-interrupt) unchanged.
``wall_time_s`` is the one deliberately volatile field.  See
``docs/reference/machine-output.md``.

Under ``--dry-run`` the document also carries ``wirelength_estimators``: the
centre-anchored and pad-anchored wirelength of the *same* layout, side by
side (issue #4831 M5). Only one of them is scored -- the other is report-only
evidence for whether pad anchoring should become the default. See
``docs/placement-pad-anchoring-audit.md``.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import os
import signal
import sys
import tempfile
import time
from pathlib import Path
from typing import Callable, ParamSpec, Sequence, TypeVar

import numpy as np

from kicad_tools.cli.format_options import emit_json
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
from kicad_tools.placement.decoupling import (
    DecouplingGroup,
    SnapMove,
    decoupling_pairs,
    identify_decoupling_groups,
    snap_decoupling_caps,
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
from kicad_tools.placement.wirelength import (
    build_pad_position_map,
    compare_wirelength_estimators,
)
from kicad_tools.placement.writeback import write_footprint_placements

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
    ref_domains: dict[str, str] | None = None,
    required_mm_by_domain_pair: dict[tuple[str, str], float] | None = None,
    exempt_pairs: set[frozenset[str]] | None = None,
    pad_anchored: bool = False,
    decoupling_groups: Sequence[DecouplingGroup] | None = None,
    fixed_sides: Sequence[int] | None = None,
) -> PlacementScore:
    """Evaluate a single placement vector and return its score.

    When ``ref_domains`` and ``required_mm_by_domain_pair`` are supplied the
    HV creepage-keepout term (issue #4373) is active; otherwise the objective
    is byte-identical to the historical voltage-blind score.

    When *pad_anchored* is True the wirelength term is measured between the
    transformed pad coordinates ``decode`` already produces, instead of
    between footprint centres (issue #4831 M1). ``False`` (the default)
    discards those pads exactly as before, keeping the score unchanged.

    *decoupling_groups* (issue #6020) enables the decoupling-cap affinity
    term. It always measures at pads, whatever *pad_anchored* says.

    *fixed_sides* overrides the vector's side flags before decoding (see
    :func:`_with_sides`), so pads are scored where the writer will put them.
    """
    if fixed_sides is not None:
        vector = _with_sides(vector, fixed_sides)
    placed = decode(vector, components)
    placements = [
        ComponentPlacement(reference=p.reference, x=p.x, y=p.y, rotation=p.rotation) for p in placed
    ]
    pad_map = build_pad_position_map(placed) if (pad_anchored or decoupling_groups) else None
    pad_positions = pad_map if pad_anchored else None
    return evaluate_placement(
        placements,
        nets,
        rules,
        board,
        cost_config,
        footprint_sizes,
        ref_domains=ref_domains,
        required_mm_by_domain_pair=required_mm_by_domain_pair,
        exempt_pairs=exempt_pairs,
        pad_positions=pad_positions,
        decoupling_groups=decoupling_groups,
        decoupling_pad_positions=pad_map,
    )


def _with_sides(vector: PlacementVector, sides: Sequence[int]) -> PlacementVector:
    """Return a copy of *vector* with every side flag set from *sides*.

    The writer (:func:`_write_placements_to_pcb`) moves and rotates
    footprints but never flips them, so a side the optimizer picks is
    discarded on write. Scoring a flipped candidate anyway mirrors its pads
    (pad-anchored wirelength, decoupling affinity) and lets the slide-off
    pass skip overlaps between parts that will in fact share a side. Pinning
    the sides to the board's own keeps the model and the written board in
    step (issue #6020).
    """
    data = vector.data.copy()
    data[3::4] = np.asarray(sides, dtype=np.float64)
    return PlacementVector(data=data)


def _read_pad_pin_types(pcb_path: str) -> dict[tuple[str, str], tuple[str, str]]:
    """Read the schematic pin annotations on the PCB's pads (issue #5985).

    Returns ``(reference, pad) -> (pintype, pinfunction)`` for every pad that
    carries a ``(pintype ...)``. Empty for boards never annotated.
    """
    from kicad_tools.schema.pcb import PCB as SchemaPCB

    pcb = SchemaPCB.load(pcb_path)
    out: dict[tuple[str, str], tuple[str, str]] = {}
    for fp in pcb.footprints:
        if not fp.reference:
            continue
        for pad in fp.pads:
            pintype = getattr(pad, "pintype", "") or ""
            if pintype:
                out[(fp.reference, pad.number)] = (pintype, getattr(pad, "pinfunction", "") or "")
    return out


def _read_local_courtyards(pcb_path: str) -> dict[str, tuple[float, float, float, float]]:
    """Each footprint's body box in its own frame, for the decoupling snap.

    The courtyard (``F.CrtYd``/``B.CrtYd`` on the footprint's side) when the
    footprint has one; otherwise the silkscreen and fab outlines on that side
    together with the pads, so a cap is not snapped onto another part's silk
    (``silk_pad_clearance``). Coordinates are the unrotated footprint frame
    the optimizer's pads use.
    """
    from kicad_tools.schema.pcb import PCB as SchemaPCB

    out: dict[str, tuple[float, float, float, float]] = {}
    for fp in SchemaPCB.load(pcb_path).footprints:
        if not fp.reference:
            continue
        side = "B" if str(fp.layer).startswith("B") else "F"

        def points(layers: tuple[str, ...]) -> list[tuple[float, float]]:
            # Each vertex is widened by half the stroke: DRC measures the
            # drawn line, not its centreline.
            pts: list[tuple[float, float]] = []
            for g in fp.graphics:
                if g.layer not in layers:
                    continue
                hw = (g.stroke_width or 0.0) / 2
                for x, y in list(g.points) if g.graphic_type == "poly" else [g.start, g.end]:
                    pts.extend([(x - hw, y - hw), (x + hw, y + hw)])
            return pts

        pts = points((f"{side}.CrtYd",))
        if not pts:
            pts = points((f"{side}.SilkS", f"{side}.Fab"))
            for pad in fp.pads:
                (px, py), (sx, sy) = pad.position, pad.size
                pts.extend([(px - sx / 2, py - sy / 2), (px + sx / 2, py + sy / 2)])
        if pts:
            xs = [x for x, _ in pts]
            ys = [y for _, y in pts]
            out[fp.reference] = (min(xs), min(ys), max(xs), max(ys))
    return out


def _build_decoupling_context(
    pcb_path: str,
    nets: Sequence[Net],
    cost_config: PlacementCostConfig,
    *,
    quiet: bool,
) -> list[DecouplingGroup] | None:
    """Find the decoupling caps and IC supply pins (issue #6020).

    Returns ``None`` when the term is disabled (``decoupling`` weight 0) or
    the board has no cap/supply-pin structure.
    """
    if not cost_config.decoupling_weight:
        return None
    groups = identify_decoupling_groups(nets, _read_pad_pin_types(pcb_path))
    if not groups:
        return None
    if not quiet:
        n_caps = sum(len(g.caps) for g in groups)
        n_pins = sum(len(g.pins) for g in groups)
        nets_str = ", ".join(g.net for g in groups)
        print(
            f"  Decoupling affinity: {n_caps} cap(s) -> {n_pins} IC supply pin(s) "
            f"on {len(groups)} rail(s) ({nets_str}); "
            f"weight={cost_config.decoupling_weight:g}"
        )
    return groups


def snap_decoupling(
    vector: PlacementVector,
    pcb_path: str,
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
    rules: DesignRuleSet,
    board: BoardOutline,
    cost_config: PlacementCostConfig,
    footprint_sizes: dict[str, tuple[float, float]],
    decoupling_groups: Sequence[DecouplingGroup] | None,
    *,
    fixed_sides: Sequence[int] | None = None,
    ref_domains: dict[str, str] | None = None,
    required_mm_by_domain_pair: dict[tuple[str, str], float] | None = None,
    exempt_pairs: set[frozenset[str]] | None = None,
    pad_anchored: bool = False,
) -> tuple[PlacementVector, list[SnapMove]]:
    """Run the post-optimize decoupling-cap snap (issue #6020).

    The global search rarely lands a 2 mm part within a millimetre of one
    pin. Finish the job one cap at a time: each cap moves to the nearest
    free spot beside its assigned supply pin (clear of other bodies and of
    IC signal-pin escape lanes), never adding an overlap/DRC/boundary
    violation. See :func:`~kicad_tools.placement.decoupling.snap_decoupling_caps`.

    Shared by ``kct optimize-placement`` and the MCP ``optimize_placement``
    tool (issue #6253). A no-op when *decoupling_groups* is empty/``None``.
    """
    if not decoupling_groups:
        return vector, []

    def _score_vector(vec: PlacementVector) -> PlacementScore:
        return _evaluate(
            vec,
            components,
            nets,
            rules,
            board,
            cost_config,
            footprint_sizes,
            ref_domains=ref_domains,
            required_mm_by_domain_pair=required_mm_by_domain_pair,
            exempt_pairs=exempt_pairs,
            pad_anchored=pad_anchored,
            decoupling_groups=decoupling_groups,
            fixed_sides=fixed_sides,
        )

    return snap_decoupling_caps(
        vector,
        components,
        decoupling_groups,
        board,
        _score_vector,
        cost_config,
        pad_nets={pin: net.name for net in nets for pin in net.pins},
        extents=_read_local_courtyards(pcb_path),
    )


def _decoupling_report(
    vector: PlacementVector,
    components: Sequence[ComponentDef],
    groups: Sequence[DecouplingGroup] | None,
) -> list[dict]:
    """Per-cap assignment for *vector*: net, cap, pin and distance (mm)."""
    if not groups:
        return []
    placed = decode(vector, components)
    placements = [
        ComponentPlacement(reference=p.reference, x=p.x, y=p.y, rotation=p.rotation) for p in placed
    ]
    return [
        {
            "net": net,
            "cap": cap.reference,
            "pin": f"{pin.reference}.{pin.pad}",
            "distance_mm": round(dist, 3),
        }
        for net, cap, pin, dist in decoupling_pairs(
            groups, placements, build_pad_position_map(placed)
        )
    ]


def _build_footprint_sizes(
    components: Sequence[ComponentDef],
) -> dict[str, tuple[float, float]]:
    """Build a footprint_sizes dict from component definitions."""
    return {c.reference: (c.width, c.height) for c in components}


def _build_hv_context(
    components: Sequence[ComponentDef],
    nets: Sequence[Net],
    *,
    voltage_map_path: str | None,
    hv_domains_path: str | None,
    creepage_standard: str,
    pollution_degree: int,
    material_group: str,
    hv_threshold: float,
    quiet: bool,
) -> tuple[dict[str, str] | None, dict[tuple[str, str], float] | None]:
    """Build the HV creepage-keepout context from the voltage/domain input.

    Returns ``(ref_domains, required_mm_by_domain_pair)``. Both are ``None``
    when no voltage/domain input is supplied (regression-safe no-op).

    Raises:
        ValueError: On mutually-exclusive inputs, a malformed input file, or a
            standard-table lookup that would require extrapolation
            (``StandardLookupError`` is re-raised as ``ValueError`` with an
            actionable message).
    """
    if voltage_map_path is None and hv_domains_path is None:
        return None, None
    if voltage_map_path is not None and hv_domains_path is not None:
        raise ValueError("--voltage-map and --hv-domains are mutually exclusive; supply only one")

    from kicad_tools.creepage.standards import StandardLookupError
    from kicad_tools.placement.hv_domains import (
        build_required_by_domain_pair,
        derive_ref_domains_from_declaration,
        derive_ref_domains_from_voltage_map,
        load_hv_domains,
        load_voltage_map,
    )

    if voltage_map_path is not None:
        voltage_map = load_voltage_map(voltage_map_path)
        ref_domains, domain_voltages = derive_ref_domains_from_voltage_map(nets, voltage_map)
        source = f"voltage map ({len(voltage_map)} nets)"
    else:
        assert hv_domains_path is not None  # guaranteed by the guards above
        declaration = load_hv_domains(hv_domains_path)
        refs = [c.reference for c in components]
        ref_domains, domain_voltages = derive_ref_domains_from_declaration(refs, declaration)
        source = f"hv-domains declaration ({len(declaration)} domains)"

    try:
        required = build_required_by_domain_pair(
            domain_voltages,
            standard_id=creepage_standard,
            pollution_degree=pollution_degree,
            material_group=material_group,
            hv_threshold=hv_threshold,
        )
    except StandardLookupError as e:
        raise ValueError(f"creepage lookup failed: {e}") from e

    if not quiet:
        print(
            f"  HV domains: {len(set(ref_domains.values()))} domains over "
            f"{len(ref_domains)} refs from {source}; "
            f"{len(required)} cross-domain keepout pair(s) "
            f"(standard={creepage_standard}, PD{pollution_degree}, "
            f"group={material_group}, threshold={hv_threshold:g}V)"
        )

    return ref_domains, required


def _parse_weights(weights_json: str | None) -> PlacementCostConfig:
    """Parse a JSON string into PlacementCostConfig.

    Thin wrapper over :func:`weights_to_cost_config` that turns bad input
    into the CLI's ``SystemExit(1)``.
    """
    data = None
    if weights_json is not None:
        try:
            data = json.loads(weights_json)
        except json.JSONDecodeError as e:
            print(f"Error: invalid JSON for --weights: {e}", file=sys.stderr)
            raise SystemExit(1) from e
    try:
        return weights_to_cost_config(data)
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        raise SystemExit(1) from e


def weights_to_cost_config(data: dict | None) -> PlacementCostConfig:
    """Build the optimizer's :class:`PlacementCostConfig` from a weights dict.

    Shared by ``kct optimize-placement --weights`` and the MCP
    ``optimize_placement`` tool (issue #6253) so both score identically.

    Defaults to :class:`CostMode.LEXICOGRAPHIC` so that the optimizer's
    convergence check (issue #2821) can use the feasibility-sentinel
    score (>= 1e12) to refuse early convergence in the infeasible region.
    The decoupling-cap affinity term (issue #6020) is on by default;
    ``{"decoupling": 0}`` turns it (and the post-optimize snap) off.

    Callers can override the mode via the ``"mode"`` key (``"lexicographic"``
    or ``"weighted_sum"``).

    Raises:
        ValueError: If ``mode`` is not a valid cost mode.
    """
    defaults = {
        "overlap_weight": 1e6,
        "drc_weight": 1e4,
        "boundary_weight": 1e5,
        "wirelength_weight": 1.0,
        "area_weight": 0.1,
        "creepage_weight": 1e5,
        "cohesion_weight": 1.0,
        "decoupling_weight": 2.0,
        "mode": CostMode.LEXICOGRAPHIC,
    }

    if data is None:
        return PlacementCostConfig(**defaults)

    mode = defaults["mode"]
    raw_mode = data.get("mode")
    if raw_mode is not None:
        try:
            mode = CostMode(raw_mode)
        except ValueError as e:
            valid = ", ".join(m.value for m in CostMode)
            raise ValueError(f"invalid 'mode' in weights (got {raw_mode!r}; valid: {valid})") from e

    return PlacementCostConfig(
        overlap_weight=data.get("overlap", defaults["overlap_weight"]),
        drc_weight=data.get("drc", defaults["drc_weight"]),
        boundary_weight=data.get("boundary", defaults["boundary_weight"]),
        wirelength_weight=data.get("wirelength", defaults["wirelength_weight"]),
        area_weight=data.get("area", defaults["area_weight"]),
        creepage_weight=data.get("creepage", defaults["creepage_weight"]),
        cohesion_weight=data.get("cohesion", defaults["cohesion_weight"]),
        decoupling_weight=data.get("decoupling", defaults["decoupling_weight"]),
        mode=mode,
    )


def _create_strategy(strategy_name: str) -> PlacementStrategy:
    """Create a placement strategy by name."""
    if strategy_name == "cmaes":
        from kicad_tools.placement.cmaes_strategy import CMAESStrategy

        return CMAESStrategy()
    elif strategy_name == "bayesian":
        # Raises ImportError if the optional 'bayesian' extra is missing.
        from kicad_tools.placement.bo_strategy import BayesianOptStrategy

        return BayesianOptStrategy()
    else:
        raise ValueError(f"Unknown strategy: {strategy_name!r}. Available: cmaes, bayesian")


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


def _score_document(score: PlacementScore) -> dict:
    """Render a :class:`PlacementScore` as the JSON score block (issue #4674).

    The breakdown is taken straight from the dataclass, so a new cost axis
    appears in the machine output without another edit here.
    """
    return {
        "total": score.total,
        "feasible": bool(score.is_feasible),
        "breakdown": dataclasses.asdict(score.breakdown),
    }


def _placement_error(pcb_path: str, message: str, *, as_json: bool, text: str | None = None) -> int:
    """Report an optimize-placement failure as a document (JSON) or prose."""
    if as_json:
        emit_json(
            {
                "command": "optimize-placement",
                "pcb": pcb_path,
                "error": message,
                "saved": False,
                "success": False,
            }
        )
    else:
        print(text if text is not None else f"Error: {message}", file=sys.stderr)
    return 1


def _print_score(label: str, score: PlacementScore) -> None:
    """Print a score summary line."""
    b = score.breakdown
    feasible = "feasible" if score.is_feasible else "INFEASIBLE"
    creepage_str = f" crp={b.creepage:.2f}" if b.creepage else ""
    decoupling_str = f" dcp={b.decoupling:.2f}" if b.decoupling else ""
    print(
        f"  {label}: {score.total:.4f} ({feasible}) "
        f"[wl={b.wirelength:.2f} ovl={b.overlap:.2f} bnd={b.boundary:.2f} "
        f"drc={b.drc:.0f} area={b.area:.2f}{creepage_str}{decoupling_str}]"
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
                # Pads are read as stored, i.e. already flipped for a
                # back-side footprint; decode must not mirror them again.
                side=1 if fp.layer == "B.Cu" else 0,
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
    # Through the PCB model, not text patching: it also moves the footprint's
    # board-absolute geometry -- embedded keepout zones and pad angles --
    # and edits the footprint's own ``(at ...)`` rather than the first
    # ``(at ...)`` line after the reference (Issue #6119).
    write_footprint_placements(
        pcb_path,
        output_path,
        ((p.reference, p.x + ox, p.y + oy, p.rotation) for p in placed),
    )


_P = ParamSpec("_P")
_R = TypeVar("_R")


def _restores_interrupt_handlers(func: Callable[_P, _R]) -> Callable[_P, _R]:
    """Restore SIGINT/SIGTERM when ``func`` returns or raises (Issue #6175).

    :func:`run_optimize_placement` installs :func:`_handle_placement_interrupt`
    for both signals, but several early ``return`` paths (no components, a
    bad ``--weights``, ``--dry-run``, ...) skipped the restore at the end.
    In-process callers -- the MCP server, ``kct`` called from Python, a pytest
    worker -- then kept a handler that answers a later SIGTERM with
    ``sys.exit(130)``.  A handler is put back only if it is still ours, so one
    installed by someone else mid-run is never clobbered.
    """

    @functools.wraps(func)
    def wrapper(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        saved = {}
        for signum in (signal.SIGINT, signal.SIGTERM):
            with contextlib.suppress(ValueError):  # not on the main thread
                saved[signum] = signal.getsignal(signum)
        try:
            return func(*args, **kwargs)
        finally:
            for signum, previous in saved.items():
                if previous is None:
                    continue  # installed from C; cannot be reinstalled
                with contextlib.suppress(ValueError):
                    if signal.getsignal(signum) is _handle_placement_interrupt:
                        signal.signal(signum, previous)

    return wrapper


@_restores_interrupt_handlers
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
    pad_anchored_wirelength: bool = False,
    time_budget: float | None = None,
    allow_infeasible: bool = False,
    voltage_map_path: str | None = None,
    hv_domains_path: str | None = None,
    creepage_standard: str = "iec60664",
    pollution_degree: int = 2,
    material_group: str = "IIIa",
    hv_threshold: float = 30.0,
    as_json: bool = False,
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
        pad_anchored_wirelength: When True, the wirelength term of the
            objective is measured between the transformed **pad**
            coordinates each candidate decode already produces, instead of
            between footprint centres (issue #4831 M1;
            ``docs/placement-pad-anchoring-audit.md``). This makes rotation
            visible to wirelength -- rotating a part cannot change a
            centre-anchored HPWL at all -- and stops a decap being scored as
            "at the chip" when it is a package-diagonal away from the pad it
            decouples. Default False keeps the objective byte-identical.
        time_budget: Wall-clock budget in seconds. The main optimization
            loop exits as soon as the elapsed time exceeds this value
            (after completing the current generation). ``None`` means no
            wall-clock cap (issue #2821). Used to bound the new
            "keep going past plateau while infeasible" behaviour.
        allow_infeasible: When True, the command returns exit code 0 even
            if the final placement is infeasible (overlap/DRC/boundary
            violations remain). Default behaviour is to exit 1 with a
            ``FATAL:`` message on stderr in that case (issue #2821).
        voltage_map_path: Optional path to a ``{net_name: volts}`` JSON voltage
            map (reusing the #4371 format). Enables the HV-aware creepage
            keepout: components are grouped into voltage domains and
            cross-domain footprints are pushed apart to their required creepage
            (issue #4373). Absent, the objective is byte-identical to today.
        hv_domains_path: Optional path to an ``--hv-domains`` declaration JSON
            (manual fallback when no voltage map is available). Mutually
            exclusive with ``voltage_map_path``.
        creepage_standard: Creepage standard id for the required-distance
            lookup (``iec60664`` / ``iec62368``).
        pollution_degree: IEC pollution degree (1, 2 or 3) for the lookup.
        material_group: Insulation material group (``I``/``II``/``IIIa``/``IIIb``).
        hv_threshold: Minimum cross-domain ``|ΔV|`` (volts) that triggers a
            creepage keepout; lower-difference domain pairs rely on normal DRC
            clearance (avoids over-segregating low-voltage nets).
        as_json: Emit one machine-readable document on stdout instead of the
            prose report (issue #4674). Progress/summary chatter is suppressed;
            exit codes and the ``FATAL:``/``ERROR:`` stderr diagnostics are
            unchanged.

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
        return _placement_error(pcb_path, f"PCB file not found: {pcb_path}", as_json=as_json)
    if pcb_file.suffix != ".kicad_pcb":
        return _placement_error(
            pcb_path, f"expected .kicad_pcb file, got: {pcb_file.suffix}", as_json=as_json
        )
    if anchor_weight < 0.0:
        return _placement_error(
            pcb_path, f"--anchor-weight must be >= 0 (got {anchor_weight})", as_json=as_json
        )

    # JSON mode owns stdout: every prose site below is already gated on
    # ``quiet``, so folding as_json into it suppresses the report while
    # leaving the stderr diagnostics untouched.
    quiet = quiet or as_json

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
        rc = _placement_error(
            pcb_path,
            f"reading PCB: {e}",
            as_json=as_json,
            text=f"Error reading PCB: {e}",
        )
        if verbose:
            import traceback

            traceback.print_exc()
        return rc

    if not components:
        return _placement_error(pcb_path, "no components found in PCB", as_json=as_json)

    # Update interrupt state so handler can save intermediate results
    _interrupt_state["components"] = components
    _interrupt_state["board_origin"] = board_origin

    if not quiet:
        print(f"  Components: {len(components)}")
        print(f"  Nets: {len(nets)}")
        print(f"  Board: {board_outline.width:.1f} x {board_outline.height:.1f} mm")

    # --- HV-aware placement context (issue #4373) ---
    # Build the per-ref domain map + per-domain-pair required-creepage table
    # from the supplied voltage/domain input. When neither is supplied the
    # structures stay None and the objective is byte-identical to today.
    try:
        hv_ref_domains, hv_required = _build_hv_context(
            components,
            nets,
            voltage_map_path=voltage_map_path,
            hv_domains_path=hv_domains_path,
            creepage_standard=creepage_standard,
            pollution_degree=pollution_degree,
            material_group=material_group,
            hv_threshold=hv_threshold,
            quiet=quiet,
        )
    except (ValueError, FileNotFoundError) as e:
        return _placement_error(pcb_path, str(e), as_json=as_json)

    # Derived-tap auto-exemption (issue #4373 Phase 3, auto). Only the
    # voltage-map path carries the per-net voltages the detector needs; the
    # --hv-domains declaration path has no per-net data, so hv_exempt stays
    # empty there. When no HV input is supplied hv_ref_domains is None and this
    # is skipped entirely (regression-safe no-op).
    hv_exempt: set[frozenset[str]] | None = None
    if voltage_map_path is not None and hv_ref_domains is not None:
        from kicad_tools.placement.hv_domains import (
            detect_derived_tap_exempt_pairs,
            load_voltage_map,
        )

        voltage_map = load_voltage_map(voltage_map_path)
        hv_exempt, hv_advisories = detect_derived_tap_exempt_pairs(
            nets, hv_ref_domains, voltage_map, hv_threshold=hv_threshold
        )
        if not quiet:
            for advisory in hv_advisories:
                print(f"  {advisory}")

    footprint_sizes = _build_footprint_sizes(components)

    # Side flags as on the board: the writer cannot flip footprints, so every
    # score and the final vector use these (see _with_sides).
    fixed_sides = [int(s) for s in _read_current_vector(pcb_path, components).data[3::4]]

    # Decoupling-cap affinity (issue #6020): pull each decoupling cap onto the
    # IC supply pin it serves. On by default; ``{"decoupling": 0}`` in
    # --weights turns it off. Boards without cap/supply-pin structure get None
    # and an unchanged objective.
    decoupling_groups = _build_decoupling_context(pcb_path, nets, cost_config, quiet=quiet)

    # Pad-anchored wirelength (issue #4831 M1). The pads are transformed on
    # every decode regardless; this only decides whether the objective reads
    # them or throws them away. Warn -- rather than silently no-op -- when the
    # board yielded no pads at all, since the per-pin fallback would then make
    # the flag a no-op.
    if pad_anchored_wirelength and not quiet:
        pad_count = sum(len(c.pads) for c in components)
        if pad_count == 0:
            print(
                "  WARNING: --pad-anchored-wirelength requested but no pads were "
                "extracted; wirelength falls back to footprint centres."
            )
        else:
            print(f"  Wirelength anchored to {pad_count} pads (not footprint centres)")

    # Compute bounds
    placement_bounds = bounds(board_outline, components)

    # The run-level fields every JSON document carries, whichever path exits.
    base_document = {
        "command": "optimize-placement",
        "pcb": pcb_path,
        "output": output_path,
        "strategy": strategy_name,
        "seed_method": seed_method,
        "max_iterations": max_iterations,
        "board": {
            "width_mm": board_outline.width,
            "height_mm": board_outline.height,
            "components": len(components),
            "nets": len(nets),
        },
        "dry_run": bool(dry_run),
    }

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
            ref_domains=hv_ref_domains,
            required_mm_by_domain_pair=hv_required,
            exempt_pairs=hv_exempt,
            pad_anchored=pad_anchored_wirelength,
            decoupling_groups=decoupling_groups,
            fixed_sides=fixed_sides,
        )
        # Report BOTH wirelength estimators for this layout (issue #4831 M5).
        # Report-only: `score` above is untouched, so --dry-run still reports
        # exactly the objective the optimizer would minimise. Decoding a
        # second time costs one pass over the components and happens once,
        # outside any search loop.
        estimators = compare_wirelength_estimators(
            decode(current_vector, components),
            nets,
            scored="pad" if pad_anchored_wirelength else "centre",
        )
        if not quiet:
            _print_score("Current", score)
            print(f"\n  Feasible: {score.is_feasible}")
            print(f"  Total score: {score.total:.4f}")
            print(f"  {estimators.summary_line()}")
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
            # Only one of the two estimator numbers above is scored; the other
            # is report-only, so the choice of anchoring can be argued from
            # measured boards. See docs/placement-pad-anchoring-audit.md.
            print(
                "  Note: only the "
                f"{estimators.scored}-anchored wirelength is scored above "
                "(--pad-anchored-wirelength switches it); the other estimator "
                "is report-only. See docs/placement-pad-anchoring-audit.md."
            )
        if as_json:
            emit_json(
                {
                    **base_document,
                    "mode": "evaluate",
                    "scores": {"current": _score_document(score)},
                    "wirelength_estimators": estimators.as_dict(),
                    "decoupling": _decoupling_report(current_vector, components, decoupling_groups),
                    "feasible": bool(score.is_feasible),
                    "saved": False,
                    "written_to": None,
                    "objective": (
                        "optimizer objective (bbox overlap area / bbox-clearance DRC), "
                        "NOT the `kct placement check` metric "
                        "(courtyard polygons / KiCad DRC)"
                    ),
                    "success": True,
                }
            )
        return 0

    # Create strategy
    try:
        strategy = _create_strategy(strategy_name)
    except (ValueError, ImportError) as e:
        return _placement_error(pcb_path, str(e), as_json=as_json)

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

        # Generate initial seed. The "current" method warm-starts from the
        # on-disk footprint positions (via _read_current_vector, the same
        # encoder used by --dry-run) so the optimizer refines the existing
        # layout; other methods synthesise a fresh seed.
        if seed_method == "current":
            seed_vector = _read_current_vector(pcb_path, components)
        else:
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

        # Warm-start CMA-ES from the (slid-off) current layout so the
        # optimizer refines it rather than re-imagining from the bounds
        # center. CMAESStrategy.initialize honours config.extra["mean"]
        # (shape-validated + bounds-clamped) and defaults to a tight sigma
        # when a mean override is present. Other seed methods keep their
        # historical center-mean behaviour -- their generated seed is only
        # scored for the "Seed" line below, never fed to the optimizer.
        if seed_method == "current":
            config.extra["mean"] = seed_vector.data

        # Evaluate seed
        seed_score = _evaluate(
            seed_vector,
            components,
            nets,
            rules,
            board_outline,
            cost_config,
            footprint_sizes,
            ref_domains=hv_ref_domains,
            required_mm_by_domain_pair=hv_required,
            exempt_pairs=hv_exempt,
            pad_anchored=pad_anchored_wirelength,
            decoupling_groups=decoupling_groups,
            fixed_sides=fixed_sides,
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
                ref_domains=hv_ref_domains,
                required_mm_by_domain_pair=hv_required,
                exempt_pairs=hv_exempt,
                pad_anchored=pad_anchored_wirelength,
                decoupling_groups=decoupling_groups,
                fixed_sides=fixed_sides,
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
            ref_domains=hv_ref_domains,
            required_mm_by_domain_pair=hv_required,
            exempt_pairs=hv_exempt,
            pad_anchored=pad_anchored_wirelength,
            decoupling_groups=decoupling_groups,
            fixed_sides=fixed_sides,
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
                    ref_domains=hv_ref_domains,
                    required_mm_by_domain_pair=hv_required,
                    exempt_pairs=hv_exempt,
                    pad_anchored=pad_anchored_wirelength,
                    decoupling_groups=decoupling_groups,
                    fixed_sides=fixed_sides,
                )
                scores.append(score.total)

            # Feed results back
            strategy.observe(candidates, scores)

            # Update interrupt state with latest best vector
            best_vec_now, _ = strategy.best()
            _interrupt_state["best_vector"] = best_vec_now

            # Progress reporting (suppressed in JSON mode: stdout carries
            # exactly one document)
            if progress_interval > 0 and iteration % progress_interval == 0 and not as_json:
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
    best_vector = _with_sides(best_vector, fixed_sides)

    # A warm start never hands back a worse placement than it started from.
    # CMA-ES only reports the best *sampled* candidate, so with a tight step
    # size it can return a jittered copy of the seed that scores worse than
    # the seed itself (issue #6020). ``--max-iterations 0`` with
    # ``--seed current`` keeps the seed outright, which makes the run a pure
    # refinement pass: slide-off plus the decoupling snap below.
    if (
        seed_method == "current"
        and not resumed
        and (max_iterations == 0 or seed_score.total <= best_score)
    ):
        best_vector = _with_sides(seed_vector, fixed_sides)
        if not quiet:
            print("  Keeping the warm-start placement: no candidate scored better")

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

    # --- Decoupling-cap snap (issue #6020) ---
    # The global search rarely lands a 2 mm part within a millimetre of one
    # pin. Finish the job one cap at a time: each cap moves to the nearest
    # free spot beside its assigned supply pin (clear of other bodies and of
    # IC signal-pin escape lanes), never adding an overlap/DRC/boundary
    # violation. See snap_decoupling_caps.
    best_vector, snap_moves = snap_decoupling(
        best_vector,
        pcb_path,
        components,
        nets,
        rules,
        board_outline,
        cost_config,
        footprint_sizes,
        decoupling_groups,
        fixed_sides=fixed_sides,
        ref_domains=hv_ref_domains,
        required_mm_by_domain_pair=hv_required,
        exempt_pairs=hv_exempt,
        pad_anchored=pad_anchored_wirelength,
    )
    if not quiet and snap_moves:
        print(f"\n  Decoupling snap: moved {len(snap_moves)} cap(s) onto their supply pins")
        for move in snap_moves:
            print(
                f"    {move.cap} -> {move.pin}: {move.before_mm:.2f} mm -> {move.after_mm:.2f} mm"
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
        ref_domains=hv_ref_domains,
        required_mm_by_domain_pair=hv_required,
        exempt_pairs=hv_exempt,
        pad_anchored=pad_anchored_wirelength,
        decoupling_groups=decoupling_groups,
        fixed_sides=fixed_sides,
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
        if si.creepage or sf.creepage:
            print(f"    Creepage:      {_delta(si.creepage, sf.creepage)}")
        if si.decoupling or sf.decoupling:
            print(f"    Decoupling:    {_delta(si.decoupling, sf.decoupling)}")

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
        rc = _placement_error(
            pcb_path,
            f"writing output: {e}",
            as_json=as_json,
            text=f"Error writing output: {e}",
        )
        if verbose:
            import traceback

            traceback.print_exc()
        return rc

    if not quiet:
        print("Done.")

    def _finish(rc: int, *, infeasible_detail: str | None = None) -> int:
        """Emit the single JSON document (if asked) and return *rc* unchanged."""
        if as_json:
            emit_json(
                {
                    **base_document,
                    "mode": "optimize",
                    "scores": {
                        "initial": _score_document(seed_score),
                        "final": _score_document(final_score),
                    },
                    "decoupling": _decoupling_report(best_vector, components, decoupling_groups),
                    "feasible": bool(final_score.is_feasible),
                    "infeasible_detail": infeasible_detail,
                    "iterations": iteration,
                    "wall_time_s": elapsed,
                    "interrupted": bool(_interrupt_state["interrupted"]),
                    "overlaps_remaining": (
                        post_slide_result.overlaps_remaining if post_slide_result is not None else 0
                    ),
                    "allow_infeasible": bool(allow_infeasible),
                    "saved": True,
                    "written_to": output_path,
                    "success": rc == 0,
                }
            )
        return rc

    # Exit code 2 when interrupted (partial result saved).
    if _interrupt_state["interrupted"]:
        return _finish(2)

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
        if b.creepage > 0:
            components_failing.append(f"creepage={b.creepage:.2f}mm")
        detail = ", ".join(components_failing) if components_failing else "unknown"

        if not allow_infeasible:
            print(
                f"FATAL: optimizer exited with infeasible placement ({detail}). "
                f"Downstream router/DRC will inherit illegal geometry. "
                f"Pass --allow-infeasible to suppress this error.",
                file=sys.stderr,
            )
            # ``detail`` is the joined string built above; ``str(...)`` only
            # satisfies the type checker, which still types this name from the
            # earlier ``for detail in ...overlap_details`` loop (the shadowing
            # is a pre-existing baseline entry, not something to fix here).
            return _finish(1, infeasible_detail=str(detail))
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
            return _finish(1, infeasible_detail=f"{len(pad_overlaps)} pad-pad overlap(s) remain")

    return _finish(0)
