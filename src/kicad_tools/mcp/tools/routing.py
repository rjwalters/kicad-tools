"""MCP tools for routing operations.

Provides tools for querying unrouted nets and routing individual nets.
"""

from __future__ import annotations

import logging
import math
from collections import defaultdict
from pathlib import Path
from typing import Literal

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.exceptions import FileNotFoundError as KiCadFileNotFoundError
from kicad_tools.exceptions import ParseError
from kicad_tools.mcp.types import (
    NetRoutingStatus,
    RouteNetResult,
    UnroutedNetsResult,
)
from kicad_tools.schema.pcb import PCB

logger = logging.getLogger(__name__)


def get_unrouted_nets(
    pcb_path: str,
    include_partial: bool = True,
) -> UnroutedNetsResult:
    """List nets that need routing.

    Analyzes a PCB file to identify nets that are unrouted or partially
    routed. Provides difficulty estimates and routing recommendations.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        include_partial: Include partially routed nets in the results.
                        If False, only completely unrouted nets are returned.

    Returns:
        UnroutedNetsResult with net details including routing status,
        difficulty estimates, and recommendations.

    Raises:
        FileNotFoundError: If the PCB file does not exist
        ParseError: If the PCB file cannot be parsed (invalid format)

    Example:
        >>> result = get_unrouted_nets("/path/to/board.kicad_pcb")
        >>> for net in result.nets:
        ...     print(f"{net.name}: {net.status} ({net.difficulty})")
    """
    path = Path(pcb_path)
    if not path.exists():
        raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")

    if path.suffix != ".kicad_pcb":
        raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        raise ParseError(f"Failed to parse PCB file: {e}") from e

    # Use NetStatusAnalyzer for accurate routing status
    analyzer = NetStatusAnalyzer(pcb)
    result = analyzer.analyze()

    # Build pad position map for distance calculations
    pad_positions = _build_pad_positions(pcb)

    # Collect nets needing routing
    nets: list[NetRoutingStatus] = []
    unrouted_count = 0
    partial_count = 0
    complete_count = 0

    for net_status in result.nets:
        if net_status.status == "complete":
            complete_count += 1
            continue

        if net_status.status == "unrouted":
            unrouted_count += 1
        elif net_status.status == "incomplete":
            partial_count += 1
            if not include_partial:
                continue

        # Calculate estimated length and difficulty
        net_pads = pad_positions.get(net_status.net_number, [])
        estimated_length = _estimate_routing_length(net_pads)
        difficulty, reason = _estimate_difficulty(net_status, net_pads, pcb)

        # Total connections needed = pins - 1 (minimum spanning tree)
        total_connections = max(0, net_status.total_pads - 1)
        routed_connections = net_status.connected_count - 1 if net_status.connected_count > 1 else 0

        nets.append(
            NetRoutingStatus(
                name=net_status.net_name,
                status=net_status.status,
                pins=net_status.total_pads,
                routed_connections=max(0, routed_connections),
                total_connections=total_connections,
                estimated_length_mm=estimated_length,
                difficulty=difficulty,
                reason=reason if difficulty != "easy" else None,
            )
        )

    # Sort by difficulty (hard first), then by name
    difficulty_order = {"hard": 0, "medium": 1, "easy": 2}
    nets.sort(key=lambda n: (difficulty_order.get(n.difficulty, 3), n.name))

    return UnroutedNetsResult(
        total_nets=result.total_nets,
        unrouted_count=unrouted_count,
        partial_count=partial_count,
        complete_count=complete_count,
        nets=nets,
    )


def route_net(
    pcb_path: str,
    net_name: str,
    output_path: str | None = None,
    strategy: Literal["auto", "shortest", "avoid_vias"] = "auto",
    layer_preference: str | None = None,
) -> RouteNetResult:
    """Route a specific net.

    Attempts to route all unconnected pads on the specified net using
    the autorouter. The result can be saved to a new file or overwrite
    the original.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        net_name: Name of the net to route (e.g., "GND", "SPI_CLK")
        output_path: Path for output file. If None, overwrites the original.
        strategy: Routing strategy to use:
                  - "auto": Automatically choose best strategy
                  - "shortest": Minimize trace length
                  - "avoid_vias": Prefer single-layer routing
        layer_preference: Preferred layer for routing (e.g., "F.Cu", "B.Cu").
                         If None, router chooses optimal layer.

    Returns:
        RouteNetResult with routing details including success status,
        trace length, vias used, and any suggestions if routing failed.

    Raises:
        FileNotFoundError: If the PCB file does not exist
        ParseError: If the PCB file cannot be parsed
        ValueError: If the net name is not found in the design

    Example:
        >>> result = route_net("/path/to/board.kicad_pcb", "SPI_CLK")
        >>> if result.success:
        ...     print(f"Routed {result.trace_length_mm}mm of trace")
        ... else:
        ...     print(f"Failed: {result.error_message}")
    """
    path = Path(pcb_path)
    if not path.exists():
        raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")

    if path.suffix != ".kicad_pcb":
        raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        raise ParseError(f"Failed to parse PCB file: {e}") from e

    # Find the net
    net_number = None
    for num, net in pcb.nets.items():
        if net.name == net_name:
            net_number = num
            break

    if net_number is None:
        raise ValueError(f"Net '{net_name}' not found in design")

    # Get current net status
    analyzer = NetStatusAnalyzer(pcb)
    status_result = analyzer.analyze()
    net_status = status_result.get_net(net_name)

    if net_status is None:
        raise ValueError(f"Net '{net_name}' not found in design")

    # Check if already fully routed
    if net_status.status == "complete":
        return RouteNetResult(
            success=True,
            net_name=net_name,
            routed_connections=max(0, net_status.connected_count - 1),
            total_connections=max(0, net_status.total_pads - 1),
            trace_length_mm=_measure_existing_trace_length(pcb, net_number),
            vias_used=_count_vias_on_net(pcb, net_number),
            layers_used=_get_layers_used(pcb, net_number),
            output_path=output_path or pcb_path,
            suggestions=["Net is already fully routed"],
        )

    # Import router components
    try:
        from kicad_tools.router import Autorouter
        from kicad_tools.router.io import (
            merge_routes_into_pcb,
            parse_pcb_design_rules,
        )
    except ImportError as e:
        return RouteNetResult(
            success=False,
            net_name=net_name,
            error_message=f"Router module not available: {e}",
            suggestions=["Ensure kicad_tools router module is installed"],
        )

    # Extract design rules from PCB
    pcb_text = path.read_text()
    pcb_rules = parse_pcb_design_rules(pcb_text)
    design_rules = pcb_rules.to_design_rules()

    # Get board dimensions
    outline = pcb.get_board_outline()
    if not outline:
        return RouteNetResult(
            success=False,
            net_name=net_name,
            error_message="Could not determine board outline",
            suggestions=["Add Edge.Cuts outline to the board"],
        )

    min_x = min(p[0] for p in outline)
    max_x = max(p[0] for p in outline)
    min_y = min(p[1] for p in outline)
    max_y = max(p[1] for p in outline)
    board_width = max_x - min_x
    board_height = max_y - min_y

    # Configure router based on strategy
    if strategy == "avoid_vias":
        design_rules.cost_via = 1000.0  # Heavy penalty for vias
    elif strategy == "shortest":
        design_rules.cost_via = 1.0  # Low via cost to prioritize shortest path

    # Create autorouter
    router = Autorouter(
        width=board_width,
        height=board_height,
        origin_x=min_x,
        origin_y=min_y,
        rules=design_rules,
    )

    # Collect pads for the specific net, grouped by component
    from kicad_tools.router.layers import Layer

    component_pads: dict[str, list[dict]] = defaultdict(list)
    net_pads: list[dict] = []

    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue

        fp_x, fp_y = fp.position
        rotation = fp.rotation
        # KiCad applies the footprint orientation as a NEGATED angle vs standard
        # CCW math (verified vs pcbnew 10.0.1, issue #3739); matches
        # PCB.get_pad_position / core.geometry.rotate_pad_offset.
        rot_rad = math.radians(-rotation)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        for pad in fp.pads:
            if pad.net_number == net_number:
                # Transform pad position to board coordinates
                px, py = pad.position
                rx = px * cos_r - py * sin_r
                ry = px * sin_r + py * cos_r

                # Determine layer from pad layers
                pad_layer = Layer.F_CU
                if (
                    layer_preference == "B.Cu"
                    or "B.Cu" in (pad.layers or [])
                    and "F.Cu" not in (pad.layers or [])
                ):
                    pad_layer = Layer.B_CU

                # Check if through-hole
                is_through_hole = "*.Cu" in (pad.layers or [])

                pad_info = {
                    "number": pad.number,
                    "x": fp_x + rx,
                    "y": fp_y + ry,
                    "width": pad.size[0] if pad.size else 0.5,
                    "height": pad.size[1] if pad.size else 0.5,
                    "net": net_number,
                    "net_name": net_name,
                    "layer": pad_layer,
                    "through_hole": is_through_hole,
                }
                component_pads[fp.reference].append(pad_info)
                net_pads.append(pad_info)

    if len(net_pads) < 2:
        return RouteNetResult(
            success=True,
            net_name=net_name,
            routed_connections=0,
            total_connections=0,
            output_path=output_path or pcb_path,
            suggestions=["Net has fewer than 2 pads, no routing needed"],
        )

    # Add components to router
    for ref, pads in component_pads.items():
        router.add_component(ref, pads)

    # Attempt to route the net
    try:
        routes = router.route_net(net_number)
    except Exception as e:
        return RouteNetResult(
            success=False,
            net_name=net_name,
            error_message=f"Routing failed: {e}",
            suggestions=_generate_suggestions(net_status, net_pads, pcb),
        )

    # Calculate results
    if routes:
        # Calculate trace length and count vias
        trace_length = 0.0
        vias_count = 0
        layers_used: set[str] = set()

        for route in routes:
            for seg in route.segments:
                trace_length += math.sqrt((seg.x2 - seg.x1) ** 2 + (seg.y2 - seg.y1) ** 2)

                layer_name = "F.Cu" if seg.layer == Layer.F_CU else "B.Cu"
                layers_used.add(layer_name)

            vias_count += len(route.vias)

        # Merge the routed traces into the PCB
        try:
            merge_routes_into_pcb(
                pcb,
                routes,
                net_map={net_name: net_number},
                trace_width=design_rules.trace_width,
                via_diameter=design_rules.via_diameter,
                via_drill=design_rules.via_drill,
            )

            # Save the result
            save_path = output_path or pcb_path
            pcb.save(save_path)

            return RouteNetResult(
                success=True,
                net_name=net_name,
                routed_connections=len(routes),
                total_connections=max(0, len(net_pads) - 1),
                trace_length_mm=trace_length,
                vias_used=vias_count,
                layers_used=sorted(layers_used),
                output_path=save_path,
            )
        except Exception as e:
            return RouteNetResult(
                success=False,
                net_name=net_name,
                routed_connections=len(routes),
                total_connections=max(0, len(net_pads) - 1),
                error_message=f"Failed to save routed PCB: {e}",
                suggestions=["Check file permissions", "Try specifying a different output_path"],
            )
    else:
        # Routing failed
        return RouteNetResult(
            success=False,
            net_name=net_name,
            total_connections=max(0, len(net_pads) - 1),
            error_message="Autorouter could not find a valid path",
            suggestions=_generate_suggestions(net_status, net_pads, pcb),
        )


def _detect_stub_terminals_for_pcb(pcb: PCB, region_box: tuple[float, float, float, float]) -> dict:
    """Detect boundary stub terminals for every straddling net on ``pcb``.

    Thin adapter over the shared #4172 producer
    :func:`kicad_tools.router.stub_terminals.detect_boundary_stub_terminals`:
    it reduces the PCB's loaded segments/pads into the detector's pure
    board-relative :class:`StubSegment` / :class:`PadLocation` inputs and
    returns its ``{net_id: [StubTerminal, ...]}`` mapping.  Stub geometry is
    NEVER re-derived here -- this is the single reuse point on the route-auto
    path (shared by both the reachability gate's has-stub check and the
    ``_build_pads_for_net`` target injection), mirroring the Autorouter side's
    single call from ``io.py::load_pcb_for_routing``.

    ``region_box`` is board-relative ``(x1, y1, x2, y2)`` mm (same convention as
    ``pcb strip --region``).  Returns an empty dict on any adaptation failure so
    callers can treat "no detectable stub" uniformly.

    Note: :class:`StubTerminal` objects are route-scoped and ephemeral -- they
    are never persisted as a ``Pad`` on the PCB.
    """
    try:
        from kicad_tools.core.types import CopperLayer
        from kicad_tools.router.stub_terminals import (
            PadLocation,
            RegionBox,
            StubSegment,
            detect_boundary_stub_terminals,
        )
    except Exception:  # pragma: no cover - defensive import guard
        return {}

    rx1, ry1, rx2, ry2 = region_box
    region = RegionBox(rx1, ry1, rx2, ry2)

    segments: list = []
    for seg in pcb._segments:
        try:
            layer = CopperLayer.from_kicad_name(seg.layer)
        except ValueError:
            continue
        segments.append(
            StubSegment(
                net_id=seg.net_number,
                net_name=seg.net_name,
                x1=seg.start[0],
                y1=seg.start[1],
                x2=seg.end[0],
                y2=seg.end[1],
                layer=layer,
                uuid=getattr(seg, "uuid", "") or None,
            )
        )

    pad_locs: list = []
    for fp in pcb.footprints:
        for pad in fp.pads:
            pos = pcb.get_pad_position(fp.reference, pad.number)
            if pos is not None:
                pad_locs.append(PadLocation(net_id=pad.net_number, x=pos[0], y=pos[1]))

    try:
        return detect_boundary_stub_terminals(segments, pad_locs, region)
    except Exception:  # pragma: no cover - defensive
        return {}


def route_net_auto(
    pcb_path: str,
    net_name: str,
    output_path: str | None = None,
    strategy: str = "auto",
    enable_repair: bool = True,
    enable_via_resolution: bool = True,
    region: str | tuple[float, float, float, float] | None = None,
    allow_partial: bool = False,
) -> dict:
    """Route a specific net using the RoutingOrchestrator.

    Uses smart strategy selection via RoutingOrchestrator rather than the
    simple Autorouter used by route_net(). The orchestrator analyzes net
    characteristics (pin pitch, differential pairs, density, via conflicts)
    and automatically selects the optimal routing strategy.

    Per-strategy completion semantics (Issue #4165)
    -----------------------------------------------
    The ``global``, ``escape``, and ``subgrid`` strategies route a **single
    two-terminal corridor** between the two most distant pads of a net.  For a
    multi-pad net this can leave intermediate pads unconnected even though the
    strategy's own internal status is "success".  ``route_net_auto`` now runs a
    real per-pad copper-reachability check after routing (independent of the
    strategy's self-reported success) and, when a net is only partially
    connected, reports ``success=False`` with ``partial=True`` and
    ``pads_connected``/``pads_total`` populated.  With ``strategy="auto"`` the
    orchestrator automatically falls back to the ``hierarchical`` (iterative
    negotiated) router, which completes multi-pad nets by construction.  When a
    partial route cannot be completed, no copper is saved unless
    ``allow_partial=True``.

    Args:
        pcb_path: Absolute path to .kicad_pcb file
        net_name: Name of the net to route (e.g., "GND", "SPI_CLK")
        output_path: Path for output file. If None, result is not saved.
        strategy: Strategy override ("auto", "global", "escape", "hierarchical",
                  "subgrid", "via_resolution", or "multi_resolution").
                  Use "auto" for smart selection.  Note: "global"/"escape"/
                  "subgrid" route a single two-terminal corridor and may leave
                  a multi-pad net partially connected; "hierarchical" iterates
                  to full net completion (Issue #4165).
        enable_repair: Whether to enable automatic clearance repair after routing
        enable_via_resolution: Whether to enable via conflict resolution
        allow_partial: When True, a partially-routed multi-pad net (some pads
                  still unconnected) is still saved to ``output_path`` instead
                  of refusing to write incomplete copper.  ``success`` remains
                  False and ``partial`` True either way; this only controls
                  whether the partial copper is persisted (Issue #4165).
        region: Optional spatial routing bound (Issue #4148, Phase 2a).  Either
                a ``"x1,y1,x2,y2"`` string or a ``(x1, y1, x2, y2)`` tuple in
                BOARD-RELATIVE mm (same convention as ``pcb strip --region``
                and ``route --region``).  When given, the net is only routed if
                ALL of its pads lie inside the box; a net with an endpoint
                outside fails with a clear message (bare-stub reconnection is
                deferred to Phase 2b).  Any produced segment/via that escapes
                the box fails the route rather than writing out-of-region copper.

    Returns:
        Dictionary with routing result including:
        - success: Whether routing succeeded
        - net_name: Name of the net routed
        - strategy_used: Name of the strategy that was applied
        - metrics: Quantitative metrics (length, vias, layer changes, repairs)
        - repair_actions: List of repairs applied
        - warnings: Non-fatal warnings
        - performance: Timing breakdown
        - error_message: Error description if success is False
        - alternative_strategies: Suggestions if routing failed

    Raises:
        FileNotFoundError: If the PCB file does not exist
        ParseError: If the PCB file cannot be parsed
        ValueError: If the net name is not found in the design

    Example:
        >>> result = route_net_auto("/path/to/board.kicad_pcb", "SPI_CLK")
        >>> if result["success"]:
        ...     print(f"Routed with strategy: {result['strategy_used']}")
        ...     print(f"Total length: {result['metrics']['total_length_mm']:.2f}mm")
        ... else:
        ...     print(f"Failed: {result['error_message']}")
    """
    path = Path(pcb_path)
    if not path.exists():
        raise KiCadFileNotFoundError(f"PCB file not found: {pcb_path}")

    if path.suffix != ".kicad_pcb":
        raise ParseError(f"Invalid file extension: {path.suffix} (expected .kicad_pcb)")

    try:
        pcb = PCB.load(pcb_path)
    except Exception as e:
        raise ParseError(f"Failed to parse PCB file: {e}") from e

    # Find the net
    net_number = None
    for num, net in pcb.nets.items():
        if net.name == net_name:
            net_number = num
            break

    if net_number is None:
        raise ValueError(f"Net '{net_name}' not found in design")

    # Issue #4148: parse + validate the spatial region bound (board-relative).
    region_box: tuple[float, float, float, float] | None = None
    if region is not None:
        if isinstance(region, str):
            parts = [p.strip() for p in region.split(",")]
            if len(parts) != 4:
                raise ValueError(
                    f"--region expects 'x1,y1,x2,y2' (four comma-separated numbers), got {region!r}"
                )
            try:
                rx1, ry1, rx2, ry2 = (float(p) for p in parts)
            except ValueError as _e:
                raise ValueError(f"--region values must be numeric, got {region!r}") from _e
        else:
            rx1, ry1, rx2, ry2 = (float(v) for v in region)
        if rx1 >= rx2 or ry1 >= ry2:
            raise ValueError(
                "--region must satisfy x1 < x2 and y1 < y2 "
                f"(got x1={rx1}, y1={ry1}, x2={rx2}, y2={ry2})"
            )
        region_box = (rx1, ry1, rx2, ry2)

        # Reachability gate: every pad of the routed net must lie inside the box
        # (board-relative).  A pad outside needs outside access.
        outside_pads = []
        for fp_ in pcb.footprints:
            for pad in fp_.pads:
                if (getattr(pad, "net_name", "") or "") != net_name:
                    continue
                pos = pcb.get_pad_position(fp_.reference, pad.number)
                if pos is None:
                    continue
                if not (rx1 <= pos[0] <= rx2 and ry1 <= pos[1] <= ry2):
                    outside_pads.append((fp_.reference, pad.number))
        if outside_pads:
            # Issue #4173 (Phase 2c): a net with pad(s) outside the box is only
            # reachable if it owns a same-net BOUNDARY STUB -- a bare clipped
            # copper endpoint on the region edge left by ``pcb strip --region``.
            # If such a stub exists, the outside pad is already electrically
            # connected via the preserved stub copper, so we reconnect the
            # in-region island to the boundary tip instead of failing (the
            # actual injection happens later, in ``_build_pads_for_net``, via
            # ``region_stub_terminals``).  A net with an outside pad and NO
            # reconnectable stub is genuinely unreachable and still fails fast.
            detected = _detect_stub_terminals_for_pcb(pcb, region_box)
            has_stub = any(t.net_name == net_name for terms in detected.values() for t in terms)
            if not has_stub:
                preview = ", ".join(f"{r}.{p}" for r, p in outside_pads[:6])
                msg = (
                    f"--region cannot route net '{net_name}': pad(s) {preview} "
                    "lie outside the region with no same-net boundary stub to "
                    "reconnect to."
                )
                return {
                    "success": False,
                    "net_name": net_name,
                    "strategy_used": "region-bounded",
                    "metrics": {},
                    "repair_actions": [],
                    "warnings": [],
                    "performance": {},
                    "error_message": msg,
                    "alternative_strategies": [],
                }

    # Import router components
    try:
        from kicad_tools.router.io import parse_pcb_design_rules
        from kicad_tools.router.orchestrator import RoutingOrchestrator
        from kicad_tools.router.strategies import RoutingStrategy
    except ImportError as e:
        return {
            "success": False,
            "net_name": net_name,
            "error_message": f"Router module not available: {e}",
            "strategy_used": "unknown",
            "metrics": {},
            "repair_actions": [],
            "warnings": ["Ensure kicad_tools router module is installed"],
            "performance": {},
            "alternative_strategies": [],
        }

    # Extract design rules from PCB
    pcb_text = path.read_text()
    pcb_rules = parse_pcb_design_rules(pcb_text)
    design_rules = pcb_rules.to_design_rules()

    # Build a lightweight PCB-like object for the orchestrator
    # The orchestrator needs pcb.width, pcb.height, and optionally pcb.grid
    outline = pcb.get_board_outline()
    if outline:
        min_x = min(p[0] for p in outline)
        max_x = max(p[0] for p in outline)
        min_y = min(p[1] for p in outline)
        max_y = max(p[1] for p in outline)
        board_width = max_x - min_x
        board_height = max_y - min_y
    else:
        board_width = 100.0
        board_height = 100.0

    # Attach dimensions to the pcb object for orchestrator use.
    # (width/height are not @property on PCB, so dynamic assignment is safe;
    # the orchestrator reads them via getattr with fallback defaults.)
    pcb.width = board_width  # type: ignore[attr-defined]
    pcb.height = board_height  # type: ignore[attr-defined]
    # Note: pcb.path is a read-only @property already set by PCB.load(),
    # so we must NOT assign to it here.

    # Resolve strategy override
    strategy_map = {
        "global": RoutingStrategy.GLOBAL_WITH_REPAIR,
        "escape": RoutingStrategy.ESCAPE_THEN_GLOBAL,
        "hierarchical": RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
        "subgrid": RoutingStrategy.SUBGRID_ADAPTIVE,
        "via_resolution": RoutingStrategy.VIA_CONFLICT_RESOLUTION,
        "multi_resolution": RoutingStrategy.MULTI_RESOLUTION,
    }

    # Build net class map for pour-net detection
    net_class_map = None
    try:
        from kicad_tools.router.net_class import classify_and_apply_rules

        net_names = {num: net.name for num, net in pcb.nets.items()}
        net_class_map = classify_and_apply_rules(net_names)
    except Exception:
        logger.debug("Net classification unavailable, skipping pour-net detection")

    # Create orchestrator
    orchestrator = RoutingOrchestrator(
        pcb=pcb,  # type: ignore[arg-type]
        rules=design_rules,
        enable_repair=enable_repair,
        enable_via_conflict_resolution=enable_via_resolution,
        net_class_map=net_class_map,
    )

    # If a strategy override is requested, patch the strategy selection
    if strategy != "auto" and strategy in strategy_map:
        forced_strategy = strategy_map[strategy]

        def _forced_select(net, intent, pads):
            return forced_strategy

        orchestrator._select_strategy = _forced_select  # type: ignore[method-assign]

        # Issue #4165: honor the user's explicit strategy choice.  When a
        # strategy is forced we DO NOT silently fall back to hierarchical on a
        # partial route -- the caller asked for exactly this strategy, so a
        # multi-pad net stranded by a single two-terminal corridor is reported
        # honestly as ``partial`` (k/n) and exits non-zero.  Disabling the
        # retry loop is what preserves that honest partial; the automatic
        # hierarchical fallback is reserved for ``strategy="auto"``.
        orchestrator.max_strategy_retries = 0

    # Build pad list from PCB footprints so the orchestrator can
    # perform strategy selection and routing (without pads every
    # strategy returns "Insufficient pads").
    #
    # Issue #4173 (Phase 2c): when a region box is set, augment the pad list
    # with boundary stub terminals so a straddling net reconnects its in-region
    # island to the preserved outside stub, at parity with ``kct route
    # --region``.  ``_build_pads_for_net`` is the SINGLE producer of the ``pads``
    # list read by every orchestrator strategy (``_route_global``,
    # ``_route_escape_then_global``, ...), so augmenting it here confines the
    # fix to one upstream site (mirroring the Autorouter's single ``self.nets``
    # prune at ``io.py::load_pcb_for_routing``).
    pads = _build_pads_for_net(pcb, net_number, net_name, region_box=region_box)

    # Route the net
    result = orchestrator.route_net(net=net_name, pads=pads)

    # Issue #4148 / #4173: confine the produced geometry to the region box.  The
    # orchestrator has no per-cell obstacle grid, so we enforce the bound on its
    # OUTPUT: any routed segment/via endpoint that escapes the box fails the
    # route rather than writing out-of-region copper.  This post-route
    # output-escape filter is the ONLY confinement guarantee on the route-auto
    # path (the coarse GlobalRouter/RegionGraph corridor planner is not
    # region-confined), so it does real work on #4173's stub-reconnection
    # geometry -- e.g. a coarse corridor that bulges through an out-of-box tile
    # center fails here honestly instead of emitting out-of-region copper.
    #
    # Frame: the orchestrator routes in the SAME frame as its input pads, and
    # ``_build_pads_for_net`` builds pads in the BOARD-RELATIVE frame (footprint
    # positions are stored origin-shifted; ``pcb.save`` re-adds the origin on
    # serialization).  ``region_box`` is board-relative too, so the box and the
    # ``result.segments`` are compared directly with no origin shift.
    if region_box is not None and result.success:
        bx1, by1, bx2, by2 = region_box
        rlo_x, rhi_x = min(bx1, bx2), max(bx1, bx2)
        rlo_y, rhi_y = min(by1, by2), max(by1, by2)
        tol = 1e-3

        def _in_box(px: float, py: float) -> bool:
            return rlo_x - tol <= px <= rhi_x + tol and rlo_y - tol <= py <= rhi_y + tol

        escaped = False
        for seg in getattr(result, "segments", []) or []:
            if not (_in_box(seg.x1, seg.y1) and _in_box(seg.x2, seg.y2)):
                escaped = True
                break
        if not escaped:
            for via in getattr(result, "vias", []) or []:
                if not _in_box(via.x, via.y):
                    escaped = True
                    break
        if escaped:
            return {
                "success": False,
                "net_name": net_name,
                "strategy_used": getattr(result.strategy_used, "name", str(result.strategy_used)),
                "metrics": {},
                "repair_actions": [],
                "warnings": [],
                "performance": {},
                "error_message": (
                    f"--region: routing net '{net_name}' produced geometry "
                    "outside the region box; refusing to write out-of-region "
                    "copper (Phase 2a confines route-auto output to the region)."
                ),
                "alternative_strategies": [],
            }

    # Convert to dict with net_name included
    result_dict = result.to_dict()
    result_dict["net_name"] = net_name

    # Issue #4165: decide whether the produced copper is persistable.  A fully
    # successful route always persists; a PARTIAL route (multi-pad net with
    # stranded pads) persists only when the caller opts in via ``allow_partial``
    # so incomplete copper is not written silently.
    should_persist = result.success or (getattr(result, "partial", False) and allow_partial)

    # Save output if requested and routing succeeded.
    #
    # Issue #2913: the orchestrator's RoutingResult carries the produced
    # segments + vias in ``result.segments`` / ``result.vias`` but the
    # PCB object is *not* mutated by the orchestrator.  Prior to this
    # fix we called ``pcb.save(output_path)`` directly on the un-mutated
    # PCB which silently produced a board with zero new tracks for the
    # net while reporting ``success=True``.  We now persist the segments
    # and vias via the PCB schema's ``add_trace`` / ``add_via`` helpers
    # before saving, and surface a clear failure when the orchestrator
    # reports success but produced no physical segments.
    if output_path and should_persist:
        segments_written, vias_written = _persist_routing_result_to_pcb(pcb, result, net_name)
        result_dict["segments_written"] = segments_written
        result_dict["vias_written"] = vias_written

        if segments_written == 0 and vias_written == 0:
            # Orchestrator claimed success but produced nothing physical.
            # Refuse to silently save an empty PCB -- mark the call as a
            # failure with a clear error message instead.
            result_dict["success"] = False
            result_dict["error_message"] = (
                "Routing reported success but no physical segments or "
                "vias were produced; refusing to save un-modified PCB "
                "(issue #2913)."
            )
            return result_dict

        try:
            pcb.save(output_path)
            result_dict["output_path"] = output_path
        except Exception as e:
            result_dict["warnings"] = result_dict.get("warnings", []) + [
                f"Routing succeeded but save failed: {e}"
            ]

    return result_dict


def _persist_routing_result_to_pcb(pcb: PCB, result, net_name: str) -> tuple[int, int]:
    """Persist orchestrator result segments + vias into the PCB object.

    Issue #2913: the orchestrator returns segments/vias on the
    :class:`RoutingResult` but does not mutate the PCB.  This helper
    materialises them via ``pcb.add_trace`` / ``pcb.add_via`` so a
    subsequent ``pcb.save`` writes the physical traces to disk.

    Args:
        pcb: Loaded PCB object (will be mutated).
        result: Orchestrator ``RoutingResult`` (carries ``segments``/``vias``).
        net_name: Net name to associate with the new traces.

    Returns:
        Tuple of ``(segments_written, vias_written)``.
    """
    segments_written = 0
    vias_written = 0

    # Persist segments.  The orchestrator's Segment uses router.layers.Layer
    # (KiCad name accessor: ``layer.kicad_name``).  ``add_trace`` accepts
    # the layer as a KiCad string (e.g. "F.Cu").
    for seg in getattr(result, "segments", []) or []:
        try:
            layer_name = (
                seg.layer.kicad_name if hasattr(seg.layer, "kicad_name") else str(seg.layer)
            )
            pcb.add_trace(
                start=(float(seg.x1), float(seg.y1)),
                end=(float(seg.x2), float(seg.y2)),
                width=float(getattr(seg, "width", 0.2)),
                layer=layer_name,
                net=net_name,
            )
            segments_written += 1
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to persist segment for net %s: %s", net_name, e)

    # Persist vias.
    for via in getattr(result, "vias", []) or []:
        try:
            layers = getattr(via, "layers", None)
            if layers and len(layers) >= 2:
                layer_pair = (
                    layers[0].kicad_name if hasattr(layers[0], "kicad_name") else str(layers[0]),
                    layers[1].kicad_name if hasattr(layers[1], "kicad_name") else str(layers[1]),
                )
            else:
                layer_pair = ("F.Cu", "B.Cu")
            pcb.add_via(
                x=float(via.x),
                y=float(via.y),
                size=float(getattr(via, "diameter", 0.6)),
                drill=float(getattr(via, "drill", 0.3)),
                layers=layer_pair,
                net=net_name,
            )
            vias_written += 1
        except Exception as e:  # pragma: no cover - defensive
            logger.warning("Failed to persist via for net %s: %s", net_name, e)

    return segments_written, vias_written


def _build_pad_positions(pcb: PCB) -> dict[int, list[tuple[float, float]]]:
    """Build a map of net numbers to pad positions.

    Args:
        pcb: Loaded PCB object

    Returns:
        Dict mapping net numbers to lists of (x, y) positions
    """
    positions: dict[int, list[tuple[float, float]]] = defaultdict(list)

    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue

        fp_x, fp_y = fp.position
        rotation = fp.rotation
        # KiCad applies the footprint orientation as a NEGATED angle vs standard
        # CCW math (verified vs pcbnew 10.0.1, issue #3739); matches
        # PCB.get_pad_position / core.geometry.rotate_pad_offset.
        rot_rad = math.radians(-rotation)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        for pad in fp.pads:
            if pad.net_number > 0:
                px, py = pad.position
                rx = px * cos_r - py * sin_r
                ry = px * sin_r + py * cos_r
                positions[pad.net_number].append((fp_x + rx, fp_y + ry))

    return positions


def _build_pads_for_net(
    pcb: PCB,
    net_number: int,
    net_name: str,
    region_box: tuple[float, float, float, float] | None = None,
) -> list:
    """Build a list of router Pad objects for a specific net.

    Extracts pad positions from the loaded PCB footprints and converts
    them to the router's Pad primitive type, which the RoutingOrchestrator
    needs for strategy selection and routing execution.

    Region-bounded reconnection (Issue #4173, Phase 2c)
    ---------------------------------------------------
    When ``region_box`` is given (board-relative ``(x1, y1, x2, y2)`` mm) and the
    net owns a same-net boundary stub, this augments the pad list with the
    "prune the outside pad, substitute the boundary stub tip" shape that ``kct
    route --region`` applies to ``Autorouter.nets`` (io.py:3889-3928):

    * Pads whose world position lies OUTSIDE the box are dropped.  Such a pad is
      the far end of a clipped boundary stub -- it is already electrically
      connected through the preserved stub copper and sits outside the routable
      region, so routing to it would both duplicate the connection and emit
      out-of-region copper.
    * Each detected :class:`StubTerminal` tip is added as a synthetic,
      route-scoped ``Pad`` at the tip's WORLD position (board-relative tip +
      ``pcb._board_origin``), giving the orchestrator an in-region reconnection
      target.  These synthetic pads are ephemeral: they are never persisted onto
      the PCB (matching the ``StubTerminal`` "never a Pad" contract).

    Confinement note (Issue #4173): unlike the Autorouter path, the
    orchestrator has NO per-cell obstacle grid -- its ``GlobalRouter`` /
    ``RegionGraph`` corridor planner routes through coarse board-tile centers
    that are not region-confined.  Region confinement on the route-auto path is
    therefore provided ENTIRELY by the post-route output-escape filter in
    ``route_net_auto`` (which fails any route whose geometry leaves the box),
    NOT by a pre-route cell-level bound.  A consequence: a coarse corridor
    between two in-region endpoints can bulge through an out-of-box tile center;
    that route then fails honestly via the output filter rather than writing
    out-of-region copper.  This is expected behavior, not a bug -- it never
    violates the zero-copper-outside-the-region contract.

    Args:
        pcb: Loaded PCB object
        net_number: Numeric net ID to filter pads
        net_name: Name of the net (stored on each Pad)
        region_box: Optional board-relative ``(x1, y1, x2, y2)`` routing box.
            When set, enables stub-terminal injection + outside-pad pruning.

    Returns:
        List of router Pad objects for the given net
    """
    from kicad_tools.router.layers import Layer
    from kicad_tools.router.primitives import Pad as RouterPad

    # The pad coordinates computed below (``fp.position`` + rotated offset) are
    # BOARD-RELATIVE -- ``PCB`` stores footprint positions already shifted by the
    # board origin (verified: ``pcb.get_pad_position`` == ``fp.position`` for a
    # centered pad).  ``region_box`` and the detected ``StubTerminal`` tips are
    # ALSO board-relative, so pruning + injection stay in the board-relative
    # frame with no origin shift here.  (The board-origin shift lives in
    # ``route_net_auto``'s output-escape filter, which operates on the
    # orchestrator's world-frame result geometry.)
    stub_terminals: list = []
    if region_box is not None:
        detected = _detect_stub_terminals_for_pcb(pcb, region_box)
        stub_terminals = [t for terms in detected.values() for t in terms if t.net_name == net_name]

    def _in_region(px: float, py: float) -> bool:
        assert region_box is not None
        lo_x, hi_x = min(region_box[0], region_box[2]), max(region_box[0], region_box[2])
        lo_y, hi_y = min(region_box[1], region_box[3]), max(region_box[1], region_box[3])
        return lo_x <= px <= hi_x and lo_y <= py <= hi_y

    pads: list[RouterPad] = []
    for fp in pcb.footprints:
        if not fp.reference or fp.reference.startswith("#"):
            continue

        fp_x, fp_y = fp.position
        # KiCad applies the footprint orientation as a NEGATED angle vs standard
        # CCW math (verified vs pcbnew 10.0.1, issue #3739).
        rot_rad = math.radians(-fp.rotation)
        cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)

        for pad in fp.pads:
            if pad.net_number != net_number:
                continue

            px, py = pad.position
            abs_x = fp_x + px * cos_r - py * sin_r
            abs_y = fp_y + px * sin_r + py * cos_r

            # Issue #4173: prune outside-region pads when reconnecting via a
            # boundary stub (only when a stub actually exists -- otherwise the
            # net's normal pad set is untouched and the reachability gate has
            # already handled genuinely-unreachable nets).
            if region_box is not None and stub_terminals and not _in_region(abs_x, abs_y):
                continue

            # Determine layer
            pad_layer = Layer.F_CU
            pad_layers = pad.layers or []
            if "B.Cu" in pad_layers and "F.Cu" not in pad_layers:
                pad_layer = Layer.B_CU

            is_through_hole = "*.Cu" in pad_layers

            pads.append(
                RouterPad(
                    x=abs_x,
                    y=abs_y,
                    width=pad.size[0] if pad.size else 1.0,
                    height=pad.size[1] if pad.size else 1.0,
                    net=net_number,
                    net_name=net_name,
                    layer=pad_layer,
                    ref=fp.reference,
                    pin=pad.number,
                    through_hole=is_through_hole,
                    drill=pad.drill,
                )
            )

    # Issue #4173: append synthetic in-region stub-tip targets.  ``StubTerminal``
    # coords are board-relative (same frame as the pads above), so no origin
    # shift is applied.  Route-scoped only -- never persisted onto the PCB.
    if stub_terminals:
        for term in stub_terminals:
            pads.append(
                RouterPad(
                    x=term.x,
                    y=term.y,
                    width=0.2,
                    height=0.2,
                    net=net_number,
                    net_name=net_name,
                    layer=term.layer,
                    ref="",
                    pin="stub",
                )
            )

    return pads


def _estimate_routing_length(pad_positions: list[tuple[float, float]]) -> float:
    """Estimate minimum routing length using minimum spanning tree approximation.

    Args:
        pad_positions: List of (x, y) pad positions

    Returns:
        Estimated routing length in millimeters
    """
    if len(pad_positions) < 2:
        return 0.0

    # Simple approximation: sum of distances in a chain
    # This is an upper bound for MST
    total = 0.0
    remaining = list(pad_positions[1:])
    current = pad_positions[0]

    while remaining:
        # Find closest remaining pad
        min_dist = float("inf")
        min_idx = 0
        for i, pos in enumerate(remaining):
            dist = math.sqrt((pos[0] - current[0]) ** 2 + (pos[1] - current[1]) ** 2)
            if dist < min_dist:
                min_dist = dist
                min_idx = i

        total += min_dist
        current = remaining.pop(min_idx)

    return total


def _estimate_difficulty(
    net_status,
    pad_positions: list[tuple[float, float]],
    pcb: PCB,
) -> tuple[str, str | None]:
    """Estimate routing difficulty for a net.

    Args:
        net_status: NetStatus object from analyzer
        pad_positions: List of (x, y) pad positions
        pcb: Loaded PCB object

    Returns:
        Tuple of (difficulty, reason) where difficulty is "easy", "medium", or "hard"
    """
    if len(pad_positions) < 2:
        return "easy", None

    # Calculate bounding box and distances
    min_x = min(p[0] for p in pad_positions)
    max_x = max(p[0] for p in pad_positions)
    min_y = min(p[1] for p in pad_positions)
    max_y = max(p[1] for p in pad_positions)

    span_x = max_x - min_x
    span_y = max_y - min_y
    max_span = max(span_x, span_y)

    # Check for power nets (often need planes, not traces)
    power_patterns = ["GND", "VCC", "VDD", "VSS", "+", "-", "VBUS", "PWR"]
    is_power = any(p in net_status.net_name.upper() for p in power_patterns)
    if is_power and len(pad_positions) > 4:
        return "hard", "Power net with many connections - consider using copper pour"

    # Check for long distances
    if max_span > 50:
        return "hard", "Long routing distance"
    elif max_span > 25:
        return "medium", "Moderate routing distance"

    # Check for high fanout
    if len(pad_positions) > 8:
        return "hard", f"High fanout net ({len(pad_positions)} pins)"
    elif len(pad_positions) > 5:
        return "medium", f"Multiple connections ({len(pad_positions)} pins)"

    # Check for differential pair patterns
    diff_patterns = ["_P", "_N", "+", "-", "DP", "DM", "D+", "D-"]
    if any(net_status.net_name.endswith(p) for p in diff_patterns):
        return "medium", "Differential pair - length matching may be needed"

    # Check for clock/high-speed patterns
    clock_patterns = ["CLK", "CLOCK", "SCK", "SCLK"]
    if any(p in net_status.net_name.upper() for p in clock_patterns):
        return "medium", "Clock signal - routing length may be important"

    return "easy", None


def _generate_suggestions(net_status, net_pads: list[dict], pcb: PCB) -> list[str]:
    """Generate suggestions for failed routing.

    Args:
        net_status: NetStatus object from analyzer
        net_pads: List of pad info dicts
        pcb: Loaded PCB object

    Returns:
        List of actionable suggestions
    """
    suggestions = []

    # Check for obstacles
    if len(net_pads) > 2:
        suggestions.append("Consider routing in segments (partial routing)")

    # Check for congested areas
    suggestions.append("Check for component placement conflicts")

    # Power net suggestions
    power_patterns = ["GND", "VCC", "VDD", "VSS"]
    if any(p in net_status.net_name.upper() for p in power_patterns):
        suggestions.append("Consider using copper pour for this power net")
        suggestions.append("Use vias to connect to internal power plane")

    # General suggestions
    suggestions.append("Try adjusting layer_preference parameter")
    suggestions.append("Manual routing may be required for complex paths")

    return suggestions


def _measure_existing_trace_length(pcb: PCB, net_number: int) -> float:
    """Measure total trace length for a net.

    Args:
        pcb: Loaded PCB object
        net_number: Net number to measure

    Returns:
        Total trace length in millimeters
    """
    total = 0.0
    for seg in pcb.segments_in_net(net_number):
        dx = seg.end[0] - seg.start[0]
        dy = seg.end[1] - seg.start[1]
        total += math.sqrt(dx * dx + dy * dy)
    return total


def _count_vias_on_net(pcb: PCB, net_number: int) -> int:
    """Count vias on a net.

    Args:
        pcb: Loaded PCB object
        net_number: Net number to count

    Returns:
        Number of vias on the net
    """
    return len(list(pcb.vias_in_net(net_number)))


def _get_layers_used(pcb: PCB, net_number: int) -> list[str]:
    """Get list of layers used by a net.

    Args:
        pcb: Loaded PCB object
        net_number: Net number to check

    Returns:
        List of layer names with traces or vias
    """
    layers: set[str] = set()

    for seg in pcb.segments_in_net(net_number):
        layers.add(seg.layer)

    for via in pcb.vias_in_net(net_number):
        layers.update(via.layers)

    return sorted(layers)
