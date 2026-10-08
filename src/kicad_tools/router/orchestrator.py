"""
Routing orchestration layer for coordinating multi-strategy routing.

This module provides the RoutingOrchestrator class that intelligently
selects and sequences routing strategies based on net characteristics,
design intent, and board complexity. It coordinates the various routing
capabilities (global router, hierarchical router, sub-grid router, escape
router, via conflict manager, trace clearance repair) into a unified
workflow.

The orchestrator provides an agent-first API that hides routing complexity
behind a single route_net() call while returning rich feedback for
debugging and optimization.

Example::

    from kicad_tools.router.orchestrator import RoutingOrchestrator
    from kicad_tools.router.rules import DesignRules

    orchestrator = RoutingOrchestrator(
        pcb=pcb,
        rules=DesignRules(),
        backend="cuda"  # Optional GPU acceleration
    )

    result = orchestrator.route_net(
        net="USB_D+",
        intent=NetIntent(is_differential=True, impedance=90)
    )

    if result.success:
        print(f"Routed with {result.metrics.via_count} vias")
        print(f"Strategy: {result.strategy_used.name}")
    else:
        print(f"Failed: {result.error_message}")
        for alt in result.alternative_strategies:
            print(f"  Try: {alt.strategy.name} - {alt.reason}")
"""

from __future__ import annotations

import logging
import math
import sys
import time
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from ..design_intent import NetIntent
    from .layers import LayerStack
    from .primitives import PCB, Pad
    from .rule_area_resolve import RuleAreaSpec
    from .rules import DesignRules, NetClassRouting

from .adaptive import AdaptiveAutorouter
from .adaptive_grid import identify_fine_pitch_components
from .board_clearance_rules import BoardClearanceRules
from .escape import EscapeRouter, is_dense_package
from .foreign_copper import (
    ConflictReport,
    ForeignItem,
    board_clearance_rules,
    board_copper,
    board_holes,
    find_conflicts,
    summarize_conflicts,
)
from .global_router import GlobalRouter
from .region_graph import RegionGraph
from .strategies import (
    AlternativeStrategy,
    PerformanceStats,
    RepairAction,
    RoutingMetrics,
    RoutingResult,
    RoutingStrategy,
)
from .subgrid import SubGridRouter
from .via_conflict import ViaConflictManager, ViaConflictStrategy

logger = logging.getLogger(__name__)


class RoutingOrchestrator:
    """Intelligently coordinate routing strategies based on net characteristics.

    The orchestrator analyzes net properties (pin pitch, differential pairs,
    dense areas, via conflicts) and automatically selects the optimal routing
    strategy sequence. This provides a single unified API for routing while
    leveraging all available routing capabilities.

    The orchestrator is designed for AI agents and provides rich structured
    feedback rather than simple success/failure boolean returns.

    Usage:
        orchestrator = RoutingOrchestrator(pcb, rules, backend="cuda")
        result = orchestrator.route_net(
            net="USB_D+",
            intent=NetIntent(is_differential=True)
        )

    Args:
        pcb: The PCB object containing board geometry and components
        rules: Design rules for routing (clearances, widths, etc.)
        backend: GPU acceleration backend ("cuda", "metal", "cpu", or None for auto)
        corridor_width: Default corridor width for global router in mm
        density_threshold: Grid cell utilization above this triggers sub-grid routing
        enable_repair: Whether to enable automatic clearance repair
        enable_via_conflict_resolution: Whether to enable via conflict resolution
        net_class_map: Optional mapping of net names to NetClassRouting objects.
            When provided, the orchestrator uses this to auto-skip pour nets
            (nets with ``is_pour_net=True``) and return a success result with a
            warning directing users to zone fill instead of trace routing.
        max_strategy_retries: Maximum number of alternative strategies to try
            when the primary strategy fails.  When a strategy returns
            ``success=False`` with a populated ``alternative_strategies`` list,
            the orchestrator automatically iterates through alternatives (up to
            this limit) before returning failure.  Set to ``0`` to disable
            automatic retry and preserve the legacy behaviour of returning
            immediately on failure.  Default is ``2``.
        layer_stack: The board's copper stack-up.  Used to resolve keepout
            rule-area layer specs and to cap the hierarchical strategy's layer
            count at the layers the board actually has (Issue #6059).
            ``None`` derives it from ``pcb.layer_stack`` or the schema PCB's
            copper layers.
        rule_areas: Track/via-blocking keepout rule areas, in the same frame
            as the pads (Issue #6059).  ``None`` (the default) reads them from
            ``pcb.rule_areas`` when ``pcb`` is a schema
            :class:`~kicad_tools.schema.pcb.PCB`; pass ``[]`` to route as if
            the board declared none.
    """

    def __init__(
        self,
        pcb: PCB,
        rules: DesignRules,
        backend: str | None = None,
        corridor_width: float = 0.5,
        density_threshold: float = 0.7,
        enable_repair: bool = True,
        enable_via_conflict_resolution: bool = True,
        net_class_map: dict[str, NetClassRouting] | None = None,
        max_strategy_retries: int = 2,
        layer_stack: LayerStack | None = None,
        rule_areas: Sequence[RuleAreaSpec] | None = None,
    ):
        self.pcb = pcb
        self.rules = rules
        self.backend = backend
        self.corridor_width = corridor_width
        self.density_threshold = density_threshold
        self.enable_repair = enable_repair
        self.enable_via_conflict_resolution = enable_via_conflict_resolution
        self.net_class_map: dict[str, NetClassRouting] = net_class_map or {}
        self.max_strategy_retries = max_strategy_retries

        # Issue #3432 (mirrors ``Autorouter.paired_escape_coupling`` from
        # #3419/#3431): gate for the diff-pair paired-escape pre-pass.
        # The pre-pass emits two tightly-coupled escape endpoints (at the
        # intra-pair clearance) that are only routable by a COUPLED
        # consumer (CoupledPathfinder).  The orchestrator's escape
        # consumer is the per-net GlobalRouter (Phase 2 of
        # ``_route_escape_then_global``) and its diff-pair strategy
        # delegates to the per-net AdaptiveAutorouter -- there is no
        # coupled consumer on the ``kct route-auto`` path today, so this
        # flag defaults to False and is never flipped.  Both EscapeRouter
        # construction sites consult it before threading
        # ``_get_diff_pair_map()``; with the flag off they pass an empty
        # map, matching the Autorouter's default-off behaviour and
        # avoiding the per-net A* stranding documented in #3419 (board
        # 06: 41% -> 27% reach).  If a coupled consumer is ever added to
        # the route-auto path, flip this flag on BEFORE the escape phase
        # (escapes are generated lazily).
        self.paired_escape_coupling: bool = False

        # Lazy-initialized routers (created on first use)
        self._global_router: GlobalRouter | None = None
        self._hierarchical: AdaptiveAutorouter | None = None
        self._subgrid: SubGridRouter | None = None
        self._escape: EscapeRouter | None = None
        self._via_manager: ViaConflictManager | None = None
        self._region_graph: RegionGraph | None = None

        # Issue #6059: board keepout rule areas.  Resolved lazily (a mock PCB
        # in a unit test never pays for it) and cached.
        self._layer_stack_override = layer_stack
        self._rule_area_specs_override = list(rule_areas) if rule_areas is not None else None
        self._rule_area_specs_cache: list[RuleAreaSpec] | None = None
        self._keepout_mask_cache: Any = None
        self._keepout_mask_resolved = False
        self._keepout_warned = False

        # Issue #6001: every strategy ``_execute_strategy`` ran during the
        # latest ``route_net`` call, in order (retries included).  The
        # unrouted-cause diagnosis reads it to tell whether any of them
        # searched a fine routing grid.
        self.strategies_attempted: list[RoutingStrategy] = []
        # Issues #6001 / #6107: every pad, track, arc and via already on the
        # board, for the short/clearance gate (lazily parsed, cached), and the
        # copper clearance KiCad measures the board against.
        self._board_copper_cache: list[ForeignItem] | None = None
        self._authored_floors_cache: bool | None = None  # Issue #6254
        self._board_holes_cache: list[ForeignItem] | None = None
        self._required_clearance_cache: float | None = None
        # Issue #6122: the board's per-pair clearance rules (netclasses and
        # .kicad_dru rules); ``None`` without a board file.
        self._clearance_rules_cache: BoardClearanceRules | None = None
        self._clearance_rules_resolved = False
        self._copper_warned = False
        # Issue #6107: the board-loaded Autorouter the hierarchical strategy
        # last routed on (``None`` when it ran the legacy own-pads-only grid).
        self._hierarchical_board_router: Any = None

        logger.info(
            f"RoutingOrchestrator initialized: backend={backend}, "
            f"corridor_width={corridor_width}mm, density_threshold={density_threshold}, "
            f"max_strategy_retries={max_strategy_retries}"
        )

    # ------------------------------------------------------------------
    # Keepout rule areas (Issue #6059)
    #
    # ``kct route`` enforces board keepout rule areas on every engine (#4605
    # lattice, #6008 grid).  ``kct route-auto`` reaches none of that code: its
    # strategies plan on the coarse GlobalRouter / RegionGraph or build their
    # own AdaptiveAutorouter with no source board file.  The helpers below
    # give the orchestrator the SAME resolved areas (``rule_area_resolve``,
    # the shared #5575 parse) and use them three ways:
    #
    # * the hierarchical strategy hands them to its inner Autorouter, which
    #   enforces them on its grid exactly like ``kct route``;
    # * any routing grid the escape / sub-grid / via-conflict phases use gets
    #   them installed (``rule_area_grid``);
    # * every strategy's OUTPUT is checked against them with the lattice
    #   engine's own predicates, and copper inside an area is refused.  That
    #   is the guarantee for the coarse corridor strategies, which cannot
    #   route around an area -- they warn once, then fail rather than write.
    # ------------------------------------------------------------------

    # CLI spellings (``kct route-auto --strategy``) for user-facing messages.
    _STRATEGY_CLI_NAMES = {
        RoutingStrategy.GLOBAL_WITH_REPAIR: "global",
        RoutingStrategy.ESCAPE_THEN_GLOBAL: "escape",
        RoutingStrategy.HIERARCHICAL_DIFF_PAIR: "hierarchical",
        RoutingStrategy.SUBGRID_ADAPTIVE: "subgrid",
        RoutingStrategy.VIA_CONFLICT_RESOLUTION: "via_resolution",
        RoutingStrategy.MULTI_RESOLUTION: "multi_resolution",
        RoutingStrategy.FULL_PIPELINE: "full_pipeline",
    }

    # Strategies whose copper comes (at least partly) from the coarse corridor
    # planner, which has no obstacle model and cannot route around an area.
    _CORRIDOR_STRATEGIES = frozenset(
        {
            RoutingStrategy.GLOBAL_WITH_REPAIR,
            RoutingStrategy.ESCAPE_THEN_GLOBAL,
            RoutingStrategy.SUBGRID_ADAPTIVE,
            RoutingStrategy.VIA_CONFLICT_RESOLUTION,
            RoutingStrategy.MULTI_RESOLUTION,
            RoutingStrategy.FULL_PIPELINE,
        }
    )

    def _known_layer_stack(self) -> LayerStack | None:
        """The board's copper stack, or ``None`` when it cannot be told.

        Order: the ``layer_stack`` constructor argument, an Autorouter-like
        ``pcb.layer_stack``, then the schema PCB's copper layer names.
        """
        from .layers import LayerDefinition, LayerType
        from .layers import LayerStack as _LayerStack

        if self._layer_stack_override is not None:
            return self._layer_stack_override
        stack = getattr(self.pcb, "layer_stack", None)
        if isinstance(stack, _LayerStack):
            return stack
        copper = getattr(self.pcb, "copper_layers", None)
        if not isinstance(copper, list):
            return None
        names = [n for n in (getattr(layer, "name", "") for layer in copper) if n.endswith(".Cu")]
        if len(names) <= 2:
            return _LayerStack.two_layer() if names else None

        def _order(name: str) -> tuple[int, int]:
            if name == "F.Cu":
                return (0, 0)
            if name == "B.Cu":
                return (2, 0)
            digits = "".join(ch for ch in name if ch.isdigit())
            return (1, int(digits) if digits else 0)

        ordered = sorted(dict.fromkeys(names), key=_order)
        last = len(ordered) - 1
        return _LayerStack(
            layers=[
                LayerDefinition(name, i, LayerType.SIGNAL, is_outer=i in (0, last))
                for i, name in enumerate(ordered)
            ],
            name="Board copper",
        )

    def _board_layer_stack(self) -> LayerStack:
        """:meth:`_known_layer_stack`, defaulting to a 2-layer stack."""
        from .layers import LayerStack as _LayerStack

        return self._known_layer_stack() or _LayerStack.two_layer()

    def _rule_area_specs(self) -> list[RuleAreaSpec]:
        """Track/via-blocking rule areas in the pads' frame (cached).

        The constructor's ``rule_areas`` wins; otherwise they are read from a
        schema ``PCB``'s ``rule_areas``.  Its polygons are board-relative,
        which is the frame ``route_net_auto`` builds pads in, so no origin
        shift is applied.
        """
        if self._rule_area_specs_cache is not None:
            return self._rule_area_specs_cache
        specs: list[RuleAreaSpec] = []
        if self._rule_area_specs_override is not None:
            specs = list(self._rule_area_specs_override)
        elif isinstance(getattr(self.pcb, "rule_areas", None), list):
            from .rule_area_resolve import keepout_rule_area_specs

            try:
                specs = keepout_rule_area_specs(self.pcb)
            except Exception as exc:  # defensive: never crash routing on a bad board
                print(
                    "Warning: could not read keepout rule areas "
                    f"({type(exc).__name__}: {exc}); rule areas will not "
                    "constrain route-auto.",
                    file=sys.stderr,
                )
                specs = []
        self._rule_area_specs_cache = specs
        return specs

    def _keepout_mask(self) -> Any:
        """The areas as a :class:`~.lattice.obstacles.LatticeKeepoutMask`.

        ``None`` when the board declares no track/via-blocking area on any of
        its copper layers.  Cached.
        """
        if self._keepout_mask_resolved:
            return self._keepout_mask_cache
        self._keepout_mask_resolved = True
        specs = self._rule_area_specs()
        if not specs:
            return None
        from .lattice.obstacles import LatticeKeepoutMask
        from .rule_area_resolve import keepout_areas_for_stack

        mask = LatticeKeepoutMask(keepout_areas_for_stack(specs, self._board_layer_stack()))
        self._keepout_mask_cache = mask if mask else None
        return self._keepout_mask_cache

    def _install_grid_keepouts(self, grid: Any) -> None:
        """Enforce the rule areas on a routing grid a strategy is about to use.

        An Autorouter-like ``pcb`` installs its own resolution (which also
        carries any ``spatial_keepouts`` filter); otherwise the orchestrator's
        areas are rasterised onto ``grid``.  Idempotent per grid.
        """
        if grid is None:
            return
        installer = getattr(self.pcb, "_install_grid_rule_area_keepouts", None)
        if callable(installer) and not isinstance(getattr(self.pcb, "rule_areas", None), list):
            installer(grid)
            return
        mask = self._keepout_mask()
        if mask is None:
            return
        from .rule_area_grid import install_rule_area_keepouts

        install_rule_area_keepouts(grid, mask.areas)

    def _drop_keepout_escapes(self, grid: Any, escapes: list) -> list:
        """Escapes whose copper stays out of the grid's keepout rule areas.

        The same check (``rule_area_grid.escape_copper_rule_area_hit``) the
        #6061 commit gate in ``EscapeRouter.apply_escape_routes`` runs.
        """
        if not escapes or not getattr(grid, "_rule_area_keepouts", None):
            return escapes
        from .rule_area_grid import escape_copper_rule_area_hit

        kept = []
        for escape in escapes:
            hit = escape_copper_rule_area_hit(
                grid,
                escape.segments,
                [escape.via] if escape.via is not None else [],
                escape.pad.net,
                self.rules,
            )
            if hit is None:
                kept.append(escape)
            else:
                logger.info(
                    "route-auto escape: dropped %s pin %s (ref=%s) -- its %s "
                    "enters a keepout rule area (Issue #6059)",
                    escape.pad.net_name,
                    escape.pad.pin,
                    escape.pad.ref,
                    hit,
                )
        return kept

    def _warn_corridor_keepouts(self, strategy: RoutingStrategy) -> str | None:
        """Warn once that a corridor strategy cannot route around the areas.

        Mirrors ``kct route``'s #4605 warning for ``--route-engine mesh``.
        Returns the message (also printed to stderr) the first time, ``None``
        afterwards or when there is nothing to warn about.
        """
        if strategy not in self._CORRIDOR_STRATEGIES or self._keepout_warned:
            return None
        mask = self._keepout_mask()
        if mask is None:
            return None
        self._keepout_warned = True
        name = self._STRATEGY_CLI_NAMES.get(strategy, strategy.name)
        message = (
            f"board declares {len(mask.areas)} track/via-blocking keepout rule "
            f"area(s), but route-auto's '{name}' strategy plans coarse corridors "
            "that cannot route around them (only the 'hierarchical' strategy "
            "enforces them during the search; see issue #6059). A corridor "
            "that crosses an area is refused, not written."
        )
        print(f"Warning: {message}", file=sys.stderr)
        return message

    def _keepout_violations(self, result: RoutingResult) -> tuple[int, int, list[str]]:
        """Copper in ``result`` that enters a rule area.

        Uses the lattice engine's predicates (``segment_blocked`` /
        ``via_blocked``): a trace's copper edge, not its centreline, must stay
        out of a track-blocking area, and a via's pad out of a via-blocking
        one.

        Returns:
            ``(segments_inside, vias_inside, area_names)``.
        """
        mask = self._keepout_mask()
        if mask is None or not (result.segments or result.vias):
            return (0, 0, [])
        from .lattice.obstacles import LatticeKeepoutMask

        stack = self._board_layer_stack()
        per_area = [(area.name or "<unnamed>", LatticeKeepoutMask([area])) for area in mask.areas]
        names: dict[str, None] = {}
        seg_hits = 0
        for seg in result.segments:
            try:
                layer = stack.layer_enum_to_index(seg.layer)
            except Exception:
                continue  # a layer this board does not have: no area covers it
            a, b = (seg.x1, seg.y1), (seg.x2, seg.y2)
            net = int(getattr(seg, "net", 0) or 0)
            half = float(getattr(seg, "width", 0.0) or 0.0) / 2.0
            if mask.segment_blocked(a, b, layer, net, half):
                seg_hits += 1
                for label, single in per_area:
                    if single.segment_blocked(a, b, layer, net, half):
                        names[label] = None
        via_hits = 0
        for via in result.vias:
            point = (via.x, via.y)
            net = int(getattr(via, "net", 0) or 0)
            radius = float(getattr(via, "diameter", 0.0) or 0.0) / 2.0
            if mask.via_blocked(point, net, radius):
                via_hits += 1
                for label, single in per_area:
                    if single.via_blocked(point, net, radius):
                        names[label] = None
        return (seg_hits, via_hits, list(names))

    def _enforce_keepouts(self, result: RoutingResult, strategy: RoutingStrategy) -> None:
        """Refuse a result whose copper enters a keepout rule area.

        The result becomes a failure with no geometry, so nothing inside an
        area is ever persisted.  Unless the hierarchical strategy itself
        produced it, the hierarchical strategy -- which enforces the areas
        during the search -- is offered as the retry.
        """
        seg_hits, via_hits, names = self._keepout_violations(result)
        if not seg_hits and not via_hits:
            return
        name = self._STRATEGY_CLI_NAMES.get(strategy, strategy.name)
        result.success = False
        result.partial = False
        result.segments = []
        result.vias = []
        result.metrics = RoutingMetrics()
        result.error_message = (
            f"route-auto '{name}' strategy produced copper inside keepout rule "
            f"area(s) {', '.join(repr(n) for n in names)} ({seg_hits} segment(s), "
            f"{via_hits} via(s)); refusing it rather than writing it (issue #6059)."
        )
        if strategy != RoutingStrategy.HIERARCHICAL_DIFF_PAIR:
            result.alternative_strategies = [
                AlternativeStrategy(
                    strategy=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                    reason="Routes on a grid that enforces keepout rule areas",
                    estimated_cost=1.5,
                    success_probability=0.6,
                )
            ]

    # ------------------------------------------------------------------
    # Other nets' copper already on the board (Issues #6001, #6107)
    #
    # The corridor strategies plan on the coarse GlobalRouter / RegionGraph,
    # which has no obstacle model at all.  The hierarchical strategy now routes
    # on a grid loaded from the board file (``_route_hierarchical_on_board``),
    # with every other net's pads, tracks, vias and arcs on it, so it routes
    # around them.  The guarantee for every strategy is still an OUTPUT check:
    # copper that shorts another net's pad, track, arc or via, or comes closer
    # to it than the board's clearance, is refused and never written.
    # ------------------------------------------------------------------

    def _board_copper(self) -> list[ForeignItem]:
        """Every track, arc, via and pad on the board (cached).

        Same frame as the pads the orchestrator routes (a schema ``PCB`` is
        board-relative).  Empty for a PCB that exposes no copper lists (the
        lightweight test mocks).
        """
        if self._board_copper_cache is None:
            self._board_copper_cache = board_copper(self.pcb)
        return self._board_copper_cache

    def _board_holes(self) -> list[ForeignItem]:
        """Every drilled hole on the board -- pad and via drills (cached, #6139)."""
        if self._board_holes_cache is None:
            self._board_holes_cache = board_holes(self.pcb)
        return self._board_holes_cache

    def _clearance_rules(self) -> BoardClearanceRules | None:
        """The board's per-pair clearance rules (cached, #6122); ``None`` without a file.

        Warns once when the board's ``.kicad_dru`` is one ``kicad-cli`` would
        discard whole, so its rules are not applied (#6150).
        """
        if not self._clearance_rules_resolved:
            self._clearance_rules_resolved = True
            try:
                self._clearance_rules_cache = board_clearance_rules(self._board_path())
            except Exception:  # defensive: rule reading must never crash routing
                self._clearance_rules_cache = None
            error = getattr(self._clearance_rules_cache, "dru_error", None)
            if error:
                logger.warning(error)
                print(f"Warning: {error}", file=sys.stderr)
        return self._clearance_rules_cache

    def _net_required_clearance(self, net_name: str) -> float:
        """The largest copper clearance ``net_name`` needs from any net on the board.

        What a router must keep this net's new copper from every other net's
        copper so that no pair the output gate measures is too close: each
        pair is resolved per netclass and ``.kicad_dru`` rule (#6122), and
        the largest wins.  The board-wide clearance without a board file.
        """
        rules = self._clearance_rules()
        if rules is None:
            return self._required_clearance()
        counterparts = {
            item.net_name for item in self._board_copper() if item.net_name or item.net == 0
        }
        try:
            return rules.net_requirement(net_name or None, counterparts)
        except Exception:  # defensive
            return self._required_clearance()

    def _board_path(self) -> Path | None:
        """The ``.kicad_pcb`` the orchestrator's schema ``PCB`` was loaded from."""
        path = getattr(self.pcb, "path", None)
        if not isinstance(path, (str, Path)):
            return None
        path = Path(path)
        if path.suffix != ".kicad_pcb" or not path.is_file():
            return None
        return path

    def _required_clearance(self) -> float:
        """The copper clearance KiCad's DRC measures this board against (cached).

        See :func:`~kicad_tools.router.foreign_copper.board_required_clearance`.
        Without a board file it is the routing rules' own ``trace_clearance``.
        """
        if self._required_clearance_cache is None:
            rules = self._clearance_rules()
            try:
                self._required_clearance_cache = (
                    rules.board_clearance()
                    if rules is not None
                    else float(self.rules.trace_clearance)
                )
            except Exception:  # defensive: rule reading must never crash routing
                self._required_clearance_cache = float(self.rules.trace_clearance)
        return self._required_clearance_cache

    @staticmethod
    def _own_net_identity(result: RoutingResult) -> tuple[set[int], set[str]]:
        own_ids: set[int] = set()
        own_names: set[str] = set()
        for item in (*result.segments, *result.vias):
            item_net = int(getattr(item, "net", 0) or 0)
            item_name = str(getattr(item, "net_name", "") or "")
            if item_net:
                own_ids.add(item_net)
            if item_name:
                own_names.add(item_name)
        if isinstance(result.net, int) and result.net:
            own_ids.add(result.net)
        elif isinstance(result.net, str) and result.net:
            own_names.add(result.net)
        return own_ids, own_names

    def _foreign_copper_conflicts(self, result: RoutingResult) -> ConflictReport:
        """Copper in ``result`` that shorts, or violates clearance to, another
        net's existing pad, track, arc or via, or crowds a drilled hole.

        Pads are measured with their real shapes (the clearance kernel's exact
        pad model), arcs along their true circle, holes and slots along their
        drill outline (#6139).  Each pair's requirement is resolved from the
        board's netclasses and ``.kicad_dru`` rules (#6122).  Copper of the
        routed net itself never counts.  No-net pads and NPTH holes do count
        (they are foreign to every net); net-0 tracks, vias and arcs stay
        exempt.
        """
        required = self._required_clearance()
        if not (result.segments or result.vias):
            return ConflictReport(required_mm=required)
        items = [*self._board_copper(), *self._board_holes()]
        if not items:
            return ConflictReport(required_mm=required)
        own_ids, own_names = self._own_net_identity(result)
        net_name = next(iter(own_names)) if len(own_names) == 1 else None
        return find_conflicts(
            items,
            result.segments,
            result.vias,
            own_ids=own_ids,
            own_names=own_names,
            required_mm=required,
            rules=self._clearance_rules(),
            net_name=net_name,
            origin=tuple(getattr(self.pcb, "_board_origin", (0.0, 0.0)) or (0.0, 0.0)),
        )

    def _enforce_no_foreign_shorts(self, result: RoutingResult, strategy: RoutingStrategy) -> None:
        """Refuse a result that shorts, or violates clearance to, other nets' copper.

        The result becomes a failure with no geometry, so the conflict is never
        persisted.  The message names the conflicting nets and items.  A
        corridor strategy is offered the hierarchical strategy -- which routes
        around other nets' copper -- as its retry.
        """
        report = self._foreign_copper_conflicts(result)
        if not report:
            return
        name = self._STRATEGY_CLI_NAMES.get(strategy, strategy.name)
        result.success = False
        result.partial = False
        result.segments = []
        result.vias = []
        result.metrics = RoutingMetrics()
        result.error_message = summarize_conflicts(report, name)
        if strategy != RoutingStrategy.HIERARCHICAL_DIFF_PAIR:
            result.alternative_strategies = [
                AlternativeStrategy(
                    strategy=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                    reason="Routes on a grid that holds other nets' pads and copper",
                    estimated_cost=1.5,
                    success_probability=0.6,
                )
            ]
        else:
            result.alternative_strategies = []

    def _board_has_authored_floors(self) -> bool:
        """Whether the board's project declares a netclass minimum above the base (cached, #6254).

        Cheap gate for :meth:`_enforce_authored_floors`: a ``Default``-only
        project (the common case) never pays for the board load.  Reads the
        same project and applies the same "above the router's own base" cut
        :func:`~kicad_tools.router.io.load_pcb_for_routing` does.
        """
        if self._authored_floors_cache is None:
            self._authored_floors_cache = False
            path = self._board_path()
            nets = getattr(self.pcb, "nets", None)
            if path is not None and isinstance(nets, dict):
                from .io import _authored_net_clearances

                names = {str(getattr(n, "name", "") or "") for n in nets.values()} - {""}
                base = min(self.rules.trace_clearance, self.rules.via_clearance)
                try:
                    authored = _authored_net_clearances(path, None, dict.fromkeys(names, 0))
                except Exception as exc:  # unreadable/unsupported project: say so, don't crash
                    logger.warning("authored netclass minima not checked: %s", exc)
                    authored = {}
                self._authored_floors_cache = any(v > base + 1e-9 for v in authored.values())
        return self._authored_floors_cache

    def _enforce_authored_floors(self, result: RoutingResult, strategy: RoutingStrategy) -> None:
        """Refuse a result whose copper breaks an authored netclass minimum (#6254).

        The final-pass counterpart of ``kct route``'s
        ``revalidate_committed_copper_or_demote`` (#6243).  The foreign-copper
        gate resolves each pair with the #6122 model, which takes the largest
        class of a multi-class net; the authored census uses KiCad 10 priority
        and inheritance.  So the result is judged by the SAME helper ``kct
        route`` uses, :meth:`Autorouter.demote_authored_floor_violation_nets`,
        on the board loaded with the project's floors: a net it would demote
        is refused here instead of written.
        """
        if not (result.segments or result.vias) or not self._board_has_authored_floors():
            return
        path = self._board_path()
        _, own_names = self._own_net_identity(result)
        net_name = next(iter(own_names)) if len(own_names) == 1 else ""
        if not net_name and isinstance(result.net, str):
            net_name = result.net
        nets_raw = getattr(self.pcb, "nets", None)
        if path is None or not net_name or not isinstance(nets_raw, dict):
            return
        board_nets = {str(getattr(n, "name", "") or "") for n in nets_raw.values()} - {""}
        if net_name not in board_nets:
            return
        import contextlib
        from dataclasses import replace as _replace

        from .primitives import Route

        try:
            router, net_id = self._load_board_router(path, net_name, board_nets, raise_base=False)
            if net_id <= 0:
                return
            ox, oy = getattr(self.pcb, "_board_origin", (0.0, 0.0)) or (0.0, 0.0)
            route = Route(
                net=net_id,
                net_name=net_name,
                segments=[
                    _replace(s, x1=s.x1 + ox, y1=s.y1 + oy, x2=s.x2 + ox, y2=s.y2 + oy, net=net_id)
                    for s in result.segments
                ],
                vias=[_replace(v, x=v.x + ox, y=v.y + oy, net=net_id) for v in result.vias],
            )
            router.grid.mark_route(route)
            router.routes.append(route)
            with contextlib.redirect_stdout(sys.stderr):
                demoted = router.demote_authored_floor_violation_nets()
        except Exception as exc:  # defensive: the census must never crash routing
            logger.warning("authored netclass census skipped for '%s': %s", net_name, exc)
            return
        if net_id not in demoted:
            return
        name = self._STRATEGY_CLI_NAMES.get(strategy, strategy.name)
        result.success = False
        result.partial = False
        result.segments = []
        result.vias = []
        result.metrics = RoutingMetrics()
        result.error_message = (
            f"route-auto '{name}' strategy produced copper for '{net_name}' below its "
            "authored netclass minimum clearance; refusing it rather than writing it "
            "(issues #6243, #6254)."
        )
        result.alternative_strategies = (
            []
            if strategy == RoutingStrategy.HIERARCHICAL_DIFF_PAIR
            else [
                AlternativeStrategy(
                    strategy=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                    reason="Routes on a grid that holds the authored netclass floors",
                    estimated_cost=1.5,
                    success_probability=0.6,
                )
            ]
        )

    def _warn_corridor_copper(self, strategy: RoutingStrategy) -> str | None:
        """Warn once that a corridor strategy cannot route around other nets' copper.

        The copper counterpart of :meth:`_warn_corridor_keepouts`.  Returns the
        message (also printed to stderr) the first time, ``None`` afterwards or
        when the board holds no copper of another net.
        """
        if strategy not in self._CORRIDOR_STRATEGIES or self._copper_warned:
            return None
        nets = {item.net for item in self._board_copper() if item.net > 0}
        if len(nets) < 2:
            return None  # at most the routed net itself
        self._copper_warned = True
        name = self._STRATEGY_CLI_NAMES.get(strategy, strategy.name)
        message = (
            f"route-auto's '{name}' strategy plans coarse corridors that cannot "
            "route around other nets' pads, tracks, arcs and vias (only the "
            "'hierarchical' strategy routes around them; see issue #6107). A "
            "corridor that shorts another net or violates the board's "
            f"{self._required_clearance():.3f} mm clearance is refused, not written."
        )
        print(f"Warning: {message}", file=sys.stderr)
        return message

    def _get_net_class_routing(self, net: str | int) -> NetClassRouting | None:
        """Look up the NetClassRouting for a net by name.

        Args:
            net: Net name or ID. Only string names are looked up in the map.

        Returns:
            NetClassRouting if found, None otherwise.
        """
        if isinstance(net, str) and net in self.net_class_map:
            return self.net_class_map[net]
        return None

    def _get_diff_pair_map(self) -> dict[str, str]:
        """Build a bidirectional net-name to partner-net-name map.

        Issue #2639 / Epic #2556 Phase 2F: feeds the EscapeRouter's
        ``diff_pair_map`` parameter so paired pads on dense packages
        get coupled-at-launch escape routes.

        Prefers an existing autorouter-style hook
        (``self.pcb.get_diff_pair_map`` -- present on the
        :class:`Autorouter`) and falls back to layered detection on
        ``self.pcb.net_names`` when the PCB-like object exposes one.
        When neither is available the map is empty and the escape
        router falls back to pre-#2639 single-ended behavior.

        Returns:
            ``{p: n, n: p}`` for every detected diff pair.  Empty when
            no pairs are detected or the PCB-like object lacks the
            necessary attributes.
        """
        # Prefer a pre-built map on the PCB-like object (the Autorouter
        # exposes ``get_diff_pair_map`` from #2639).
        getter = getattr(self.pcb, "get_diff_pair_map", None)
        if callable(getter):
            try:
                result = getter()
                if isinstance(result, dict):
                    return dict(result)
            except Exception:
                pass

        # Fall back to direct layered detection on the PCB-like object.
        net_names = getattr(self.pcb, "net_names", None)
        if not net_names:
            return {}
        try:
            from .diffpair_detection import detect_diff_pairs
        except ImportError:
            return {}

        net_to_class: dict[str, str] = {}
        for net_name, nc in self.net_class_map.items():
            net_to_class[net_name] = nc.name

        try:
            detected = detect_diff_pairs(
                net_names,
                net_class_routing=self.net_class_map,
                net_to_class=net_to_class,
                kicad_groups=getattr(self.pcb, "kicad_diff_pair_groups", None),
            )
        except Exception:
            return {}

        out: dict[str, str] = {}
        for d in detected:
            p = d.pair.positive.net_name
            n = d.pair.negative.net_name
            if p and n:
                out[p] = n
                out[n] = p
        return out

    def _build_net_target_positions(self) -> dict[int, list[tuple[float, float, str]]] | None:
        """Build the board-wide net-id -> [(x, y, ref), ...] pad map.

        Issue #3428: feeds the EscapeRouter's ``net_target_positions``
        parameter so the fine-pitch QFP in-pad rescue can aim its inner
        stub at the net's actual routing target (see
        ``EscapeRouter._compute_target_direction``).  This mirrors the
        ``Autorouter._build_net_target_positions`` wiring on the ``kct
        route`` path -- the orchestrator (``kct route-auto``) is an
        independent code path and fixing only one is a known foot-gun.

        Two PCB-like shapes are supported, both defensively:

        1. Autorouter-style ``self.pcb.pads`` dict keyed by
           ``(ref, pin)`` with router ``Pad`` values.
        2. Document-model ``self.pcb.footprints`` with relative pad
           positions (rotated into world coordinates with KiCad's
           negated-angle convention -- see core.geometry.rotate_pad_offset,
           #3739 -- mirroring
           ``kicad_tools.mcp.tools.routing._build_pads_for_net``).

        Returns ``None`` when neither shape is usable (e.g. mock PCBs in
        unit tests) so the EscapeRouter falls back to legacy
        parity-derived stub directions.  Iteration is over sorted keys /
        document order to keep the per-net lists deterministic.
        """
        result: dict[int, list[tuple[float, float, str]]] = {}
        try:
            pads_attr = getattr(self.pcb, "pads", None)
            if isinstance(pads_attr, dict) and pads_attr:
                for key in sorted(pads_attr):
                    pad = pads_attr[key]
                    net = getattr(pad, "net", 0)
                    if not isinstance(net, int) or net == 0:
                        continue
                    result.setdefault(net, []).append((pad.x, pad.y, getattr(pad, "ref", "") or ""))
                return result or None

            footprints = getattr(self.pcb, "footprints", None)
            if footprints:
                for fp in footprints:
                    ref = getattr(fp, "reference", "") or ""
                    if not ref or ref.startswith("#"):
                        continue
                    fp_x, fp_y = fp.position
                    # KiCad applies the footprint orientation as a NEGATED angle
                    # vs standard CCW math (verified vs pcbnew 10.0.1, #3739).
                    rot_rad = math.radians(-(getattr(fp, "rotation", 0.0) or 0.0))
                    cos_r, sin_r = math.cos(rot_rad), math.sin(rot_rad)
                    for pad in fp.pads:
                        net = getattr(pad, "net_number", 0)
                        if not isinstance(net, int) or net == 0:
                            continue
                        px, py = pad.position
                        result.setdefault(net, []).append(
                            (
                                fp_x + px * cos_r - py * sin_r,
                                fp_y + px * sin_r + py * cos_r,
                                ref,
                            )
                        )
                return result or None
        except Exception:  # pragma: no cover - defensive against mock PCBs
            return None
        return None

    def _build_component_hole_census(self) -> list[Pad] | None:
        """Use the physical census before any routing-destination filtering."""
        from .via_in_pad_eligibility import (
            component_holes_for_router,
            component_holes_from_document,
        )

        if hasattr(self.pcb, "all_pads"):
            return component_holes_for_router(self.pcb)
        return component_holes_from_document(self.pcb)

    def route_net(
        self,
        net: str | int,
        intent: NetIntent | None = None,
        pads: list[Pad] | None = None,
    ) -> RoutingResult:
        """Route a net using optimal strategy selection.

        This is the main entry point for the orchestrator. It analyzes the net
        characteristics and design intent, selects the optimal routing strategy,
        executes the routing, and returns rich feedback.

        Args:
            net: Net name or ID to route
            intent: Optional design intent (differential pairs, impedance control, etc.)
            pads: Optional list of pads for this net (if None, extracted from PCB)

        Returns:
            RoutingResult with success status, metrics, and rich feedback
        """
        start_time = time.time()
        perf = PerformanceStats(backend_type=self.backend or "cpu")
        self.strategies_attempted = []  # Issue #6001

        # Check for pour nets (power/ground handled by zone fill, not traces)
        net_class_routing = self._get_net_class_routing(net)
        if net_class_routing is not None and net_class_routing.is_pour_net:
            perf.total_time_ms = (time.time() - start_time) * 1000
            warning_msg = (
                f"Net '{net}' is a {net_class_routing.name.lower()} pour net. "
                f"Skipping trace routing — use zone fill instead."
            )
            logger.warning("Skipping pour net %s: %s", net, warning_msg)
            return RoutingResult(
                success=True,
                net=net,
                strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
                warnings=[warning_msg],
                performance=perf,
            )

        # Phase 1: Strategy selection
        strategy_start = time.time()
        strategy = self._select_strategy(net, intent, pads)
        perf.strategy_selection_ms = (time.time() - strategy_start) * 1000

        logger.info(f"Routing net {net} with strategy {strategy.name}")

        # Phase 2: Execute routing with selected strategy
        routing_start = time.time()
        result = self._execute_strategy(net, strategy, intent, pads)

        # Phase 2a: Truth-in-exit-condition (Issue #4165).
        # The global-family strategies (global/escape/subgrid) route a single
        # two-terminal corridor and report unconditional success even when a
        # multi-pad net has stranded pads.  Verify actual per-pad copper
        # reachability; if incomplete, demote to a PARTIAL result (success=
        # False, partial=True) so the retry loop below can fall through to a
        # completing strategy (hierarchical) instead of blessing a half-routed
        # net.
        self._verify_completion(result, pads)

        # Phase 2b: Automatic strategy retry on failure OR partial completion.
        if not result.success and result.alternative_strategies and self.max_strategy_retries > 0:
            retries = result.alternative_strategies[: self.max_strategy_retries]
            strategies_attempted = [strategy.name]

            # Track the best partial result seen (initial or any retry) so that
            # if nothing fully completes the net we still surface the most-
            # connected honest partial rather than a bare failure (Issue #4165).
            best_partial = result if result.partial else None

            for alt in retries:
                logger.info(
                    "Retrying net %s with alternative strategy %s (reason: %s, p_success=%.2f)",
                    net,
                    alt.strategy.name,
                    alt.reason,
                    alt.success_probability,
                )
                retry_result = self._execute_strategy(net, alt.strategy, intent, pads)
                strategies_attempted.append(alt.strategy.name)

                # Re-verify per-pad completion on the retry as well: a global-
                # family retry can itself be partial and must not be blessed.
                self._verify_completion(retry_result, pads)

                if retry_result.success:
                    retry_result.warnings.append(
                        f"Succeeded on retry with {alt.strategy.name} "
                        f"after {strategies_attempted[0]} failed "
                        f"(strategies attempted: "
                        f"{', '.join(strategies_attempted)})"
                    )
                    result = retry_result
                    break

                # Keep the retry with the most pads connected as the fallback
                # partial if no later strategy fully completes.
                if retry_result.partial and (
                    best_partial is None
                    or (retry_result.pads_connected or 0) > (best_partial.pads_connected or 0)
                ):
                    best_partial = retry_result
            else:
                # All retries exhausted without full completion.  Prefer the
                # best honest partial over a bare no-copper failure so callers
                # get "k/n pads connected" instead of an opaque failure.
                if best_partial is not None:
                    result = best_partial
                result.warnings.append(
                    f"All strategy retries exhausted (attempted: {', '.join(strategies_attempted)})"
                )

        perf.routing_ms = (time.time() - routing_start) * 1000

        # Phase 3: Post-route repair (if enabled and needed)
        if self.enable_repair and result.success and len(result.violations) > 0:
            repair_start = time.time()
            repair_count = self._apply_clearance_repair(result)
            perf.repair_ms = (time.time() - repair_start) * 1000
            result.metrics.repair_actions = repair_count

        # Update performance stats
        perf.total_time_ms = (time.time() - start_time) * 1000
        result.performance = perf

        logger.info(
            f"Routing complete: net={net}, strategy={strategy.name}, "
            f"success={result.success}, time={perf.total_time_ms:.1f}ms"
        )

        return result

    # Strategies whose "success" is a single two-terminal corridor and thus may
    # leave a multi-pad net's intermediate pads stranded (Issue #4165).  The
    # negotiated/hierarchical family completes the net by construction and is
    # trusted without a post-route reachability check.
    _GLOBAL_FAMILY_STRATEGIES = frozenset(
        {
            RoutingStrategy.GLOBAL_WITH_REPAIR,
            RoutingStrategy.ESCAPE_THEN_GLOBAL,
            RoutingStrategy.SUBGRID_ADAPTIVE,
            RoutingStrategy.MULTI_RESOLUTION,
        }
    )

    def _verify_completion(self, result: RoutingResult, pads: list[Pad] | None) -> None:
        """Verify real per-pad copper reachability and demote partials.

        Issue #4165: the global-family strategies report ``success=True`` once
        they emit ANY corridor, with no check that every pad of a multi-pad net
        is actually reached.  This runs a real reachability walk over the
        produced copper (segments + vias) unioned with pre-existing same-net
        copper on the PCB.  When fewer than all pads connect, the result is
        demoted to ``success=False, partial=True`` with ``pads_connected`` /
        ``pads_total`` populated and ``hierarchical`` injected as an
        alternative so the retry loop can complete the net.

        The check is a genuine geometric-adjacency walk (see
        :mod:`kicad_tools.router.connectivity`) -- it does NOT reuse
        ``NetStatusAnalyzer``'s tolerance-based union (Issue #4176), and errs
        toward reporting incomplete rather than over-connecting.

        Mutates ``result`` in place; no-op for non-global strategies, failures,
        or nets with fewer than two pads.
        """
        if result.strategy_used not in self._GLOBAL_FAMILY_STRATEGIES:
            return
        if not result.success:
            return
        if not pads or len(pads) < 2:
            return
        # No produced geometry means there is no corridor to under-reach: a
        # segment-less "success" is the #2913 concern (persistence surfaces it),
        # not #4165's stranded-pad concern.  Only demote when copper was
        # actually emitted that fails to reach every pad.
        if not result.segments and not result.vias:
            return

        from .connectivity import check_net_pad_connectivity

        pad_positions = [(p.x, p.y) for p in pads]

        # Gather pre-existing same-net copper so pads already joined by earlier
        # routing (or partial prior calls) count toward completion.
        existing_segments, existing_vias = self._existing_net_copper(pads)

        try:
            pads_connected, pads_total = check_net_pad_connectivity(
                pad_positions=pad_positions,
                segments=list(result.segments),
                vias=list(result.vias),
                existing_segments=existing_segments,
                existing_vias=existing_vias,
            )
        except Exception:  # pragma: no cover - defensive; never mask a route
            logger.debug("Per-pad connectivity check failed; leaving result unchanged")
            return

        result.pads_connected = pads_connected
        result.pads_total = pads_total

        if pads_connected >= pads_total:
            return  # fully connected — genuine success

        # Incomplete: demote to a partial result.
        result.partial = True
        result.success = False
        result.warnings.append(
            f"Partial route: {pads_connected}/{pads_total} pads connected by the "
            f"{result.strategy_used.name} corridor; remaining pad(s) left "
            "unconnected. This strategy routes a single two-terminal corridor "
            "and does not complete multi-pad nets (Issue #4165)."
        )
        # Steer the retry loop toward a completing strategy.
        if not any(
            a.strategy == RoutingStrategy.HIERARCHICAL_DIFF_PAIR
            for a in result.alternative_strategies
        ):
            result.alternative_strategies.insert(
                0,
                AlternativeStrategy(
                    strategy=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                    reason=(
                        "Global-family corridor left pads unconnected; the "
                        "iterative negotiated router aims to complete multi-pad "
                        "nets (not guaranteed on congested/hard nets)"
                    ),
                    estimated_cost=1.5,
                    success_probability=0.7,
                ),
            )

    def _existing_net_copper(self, pads: list[Pad]) -> tuple[list, list]:
        """Collect pre-existing same-net segments + vias from the PCB.

        Returns ``([], [])`` when the attached PCB does not expose copper
        accessors (e.g. the lightweight mock PCBs used in some tests).
        """
        net_numbers = {p.net for p in pads if getattr(p, "net", 0)}
        net_names = {p.net_name for p in pads if getattr(p, "net_name", "")}
        pcb = self.pcb
        segments: list = []
        vias: list = []

        seg_in_net = getattr(pcb, "segments_in_net", None)
        via_in_net = getattr(pcb, "vias_in_net", None)
        if callable(seg_in_net) and callable(via_in_net):
            for num in net_numbers:
                try:
                    segments.extend(seg_in_net(num))
                    vias.extend(via_in_net(num))
                except Exception:  # pragma: no cover - defensive
                    continue
            return segments, vias

        # Fallback: filter the full segment/via lists by net name when the PCB
        # only exposes flat accessors.
        all_segs = getattr(pcb, "segments", None)
        all_vias = getattr(pcb, "vias", None)
        if isinstance(all_segs, list):
            for s in all_segs:
                if (
                    getattr(s, "net_name", "") in net_names
                    or getattr(s, "net_number", -1) in net_numbers
                ):
                    segments.append(s)
        if isinstance(all_vias, list):
            for v in all_vias:
                if (
                    getattr(v, "net_name", "") in net_names
                    or getattr(v, "net_number", -1) in net_numbers
                ):
                    vias.append(v)
        return segments, vias

    def _select_strategy(
        self,
        net: str | int,
        intent: NetIntent | None,
        pads: list[Pad] | None,
    ) -> RoutingStrategy:
        """Analyze net characteristics and select optimal routing strategy.

        Strategy selection heuristics (in priority order):
        1. Fine-pitch escape needed? -> ESCAPE_THEN_GLOBAL
        2. Differential pair? -> HIERARCHICAL_DIFF_PAIR
        3. Dense area (high grid utilization)? -> SUBGRID_ADAPTIVE
        4. Via conflicts detected? -> VIA_CONFLICT_RESOLUTION
        5. Default: GLOBAL_WITH_REPAIR

        Args:
            net: Net identifier
            intent: Optional design intent
            pads: Optional list of pads for this net

        Returns:
            Selected routing strategy
        """
        # Check 1: Fine-pitch escape routing needed?
        if pads and self._needs_escape_routing(pads):
            logger.debug(f"Net {net}: Fine-pitch pads detected, using escape routing")
            return RoutingStrategy.ESCAPE_THEN_GLOBAL

        # Check 2: Differential pair optimization?
        if intent and hasattr(intent, "is_differential") and intent.is_differential:
            logger.debug(f"Net {net}: Differential pair, using hierarchical routing")
            return RoutingStrategy.HIERARCHICAL_DIFF_PAIR

        # Check 3: Dense area requiring sub-grid routing?
        if pads and self._check_density(pads) > self.density_threshold:
            logger.debug(f"Net {net}: High density detected, using sub-grid adaptive routing")
            return RoutingStrategy.SUBGRID_ADAPTIVE

        # Check 4: Via conflicts present?
        if self.enable_via_conflict_resolution and self._has_via_conflicts(net, pads):
            logger.debug(f"Net {net}: Via conflicts detected, using conflict resolution")
            return RoutingStrategy.VIA_CONFLICT_RESOLUTION

        # Default: Global router with optional repair
        logger.debug(f"Net {net}: Standard routing with global router")
        return RoutingStrategy.GLOBAL_WITH_REPAIR

    def _execute_strategy(
        self,
        net: str | int,
        strategy: RoutingStrategy,
        intent: NetIntent | None,
        pads: list[Pad] | None,
    ) -> RoutingResult:
        """Execute the selected routing strategy.

        Args:
            net: Net identifier
            strategy: Selected routing strategy
            intent: Optional design intent
            pads: Optional list of pads

        Returns:
            RoutingResult from the strategy execution
        """
        self.strategies_attempted.append(strategy)  # Issue #6001
        # Issue #6059: a corridor strategy cannot route around keepout rule
        # areas -- say so once, before it runs.
        keepout_warning = self._warn_corridor_keepouts(strategy)
        # Issue #6107: nor around other nets' pads and copper.
        copper_warning = self._warn_corridor_copper(strategy)

        try:
            if strategy == RoutingStrategy.GLOBAL_WITH_REPAIR:
                result = self._route_global(net, pads)

            elif strategy == RoutingStrategy.ESCAPE_THEN_GLOBAL:
                result = self._route_escape_then_global(net, pads)

            elif strategy == RoutingStrategy.HIERARCHICAL_DIFF_PAIR:
                result = self._route_hierarchical(net, intent, pads)

            elif strategy == RoutingStrategy.SUBGRID_ADAPTIVE:
                result = self._route_subgrid_adaptive(net, pads)

            elif strategy == RoutingStrategy.VIA_CONFLICT_RESOLUTION:
                result = self._route_with_via_resolution(net, pads)

            elif strategy == RoutingStrategy.FULL_PIPELINE:
                result = self._route_full_pipeline(net, intent, pads)

            elif strategy == RoutingStrategy.MULTI_RESOLUTION:
                result = self._route_multi_resolution(net, pads)

            else:
                error_msg = f"Unknown strategy: {strategy}"
                logger.error(error_msg)
                return RoutingResult(
                    success=False,
                    net=net,
                    strategy_used=strategy,
                    error_message=error_msg,
                )

            # Issue #6059: no strategy may hand back copper inside a keepout
            # rule area.  The hierarchical strategy avoids them during the
            # search; this output check is the guarantee for the rest.
            if keepout_warning is not None:
                result.warnings.append(keepout_warning)
            if copper_warning is not None:
                result.warnings.append(copper_warning)
            self._enforce_keepouts(result, strategy)
            # Issues #6001 / #6107: nor copper that shorts another net's pad,
            # track, arc or via, or violates the board's clearance to it.
            self._enforce_no_foreign_shorts(result, strategy)
            # Issue #6254: and the final authored-netclass pass kct route runs.
            self._enforce_authored_floors(result, strategy)

            # Ensure failed results always carry alternative suggestions so
            # the retry loop in route_net() has candidates to try.
            if not result.success and not result.alternative_strategies:
                result.alternative_strategies = self._suggest_alternatives(strategy)

            return result

        except Exception as e:
            logger.exception(f"Strategy execution failed: {e}")
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=strategy,
                error_message=f"Routing failed: {str(e)}",
                alternative_strategies=self._suggest_alternatives(strategy),
            )

    def _needs_escape_routing(self, pads: list[Pad]) -> bool:
        """Check if any pads require escape routing (fine-pitch components).

        Evaluates density per component rather than across net endpoints.
        A net connecting an MCU pin (fine-pitch QFP) to a peripheral pad
        (far away) would have large inter-endpoint distance but the MCU
        component itself is dense and needs escape routing.

        Args:
            pads: List of pads to analyze

        Returns:
            True if any component owning these pads is dense
        """
        if len(pads) < 2:
            return False

        # Group pads by component reference
        by_ref: dict[str, list[Pad]] = {}
        for pad in pads:
            ref = pad.component_key
            if ref:
                by_ref.setdefault(ref, []).append(pad)

        # Check each component for density using escape.is_dense_package()
        # which accounts for pin count, pitch, and design rule thresholds
        trace_width = getattr(self.rules, "trace_width", None)
        clearance = getattr(self.rules, "trace_clearance", None)

        return any(
            is_dense_package(
                ref_pads,
                trace_width=trace_width,
                clearance=clearance,
            )
            for ref_pads in by_ref.values()
            if len(ref_pads) >= 2
        )

    def _check_density(self, pads: list[Pad]) -> float:
        """Calculate routing density around pads (0.0 to 1.0).

        This is a simplified heuristic. In production, this would analyze
        actual grid cell utilization.

        Args:
            pads: List of pads to analyze

        Returns:
            Density metric (0.0 = sparse, 1.0 = very dense)
        """
        if len(pads) < 2:
            return 0.0

        # Calculate bounding box area
        min_x = min(p.x for p in pads)
        max_x = max(p.x for p in pads)
        min_y = min(p.y for p in pads)
        max_y = max(p.y for p in pads)

        area = (max_x - min_x) * (max_y - min_y)
        if area == 0:
            return 0.0

        # Estimate density as pads per square mm
        # This is a placeholder - real implementation would check grid utilization
        density = len(pads) / area
        return min(density / 10.0, 1.0)  # Normalize to 0-1 range

    def _has_via_conflicts(self, net: str | int, pads: list[Pad] | None) -> bool:
        """Check if there are existing via conflicts for this net.

        Queries the ViaConflictManager to detect vias from other nets
        that block access to this net's pads.

        Args:
            net: Net identifier
            pads: Optional list of pads

        Returns:
            True if via conflicts detected
        """
        if pads is None or not pads:
            return False

        # Initialize via manager lazily if we have a grid
        if self._via_manager is None:
            grid = getattr(self.pcb, "grid", None)
            if grid is None:
                return False
            self._via_manager = ViaConflictManager(grid=grid, rules=self.rules)

        net_id = net if isinstance(net, int) else 0
        for pad in pads:
            conflicts = self._via_manager.find_blocking_vias(pad=pad, pad_net=net_id)
            if conflicts:
                return True
        return False

    def _route_global(self, net: str | int, pads: list[Pad] | None) -> RoutingResult:
        """Execute global routing strategy.

        Uses the GlobalRouter to find a corridor assignment through the
        region graph, then materialises the corridor centreline into
        :class:`Segment` objects so the caller (e.g. ``route_net_auto``) can
        persist them to the PCB.  Prior to issue #2913 this method returned
        ``success=True`` with an empty ``segments`` list, which silently
        produced PCBs with zero new tracks.

        The global router is a coarse planner; the segments produced here
        follow the tile-corridor centreline and are not guaranteed to be
        DRC-clean.  They give the user something concrete to inspect and
        repair instead of the previous silent data loss.

        Args:
            net: Net identifier
            pads: Optional list of pads

        Returns:
            RoutingResult from global routing with populated ``segments``
        """
        # Local import to avoid circular import at module load time
        from .primitives import Segment as RouterSegment

        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
                error_message="Insufficient pads for global routing",
            )

        pad_positions = [(p.x, p.y) for p in pads]
        # Resolve the integer net ID from the pads themselves when ``net``
        # is a string (the previous ``hash(str(net))`` trick produced bogus
        # net numbers that would not round-trip into the PCB).  Falling
        # back to 0 means the segment will land on the "unconnected" net,
        # which is at least visible to the user.
        if isinstance(net, int):
            net_id = net
        else:
            net_id = pads[0].net if pads[0].net else 0

        assignment = self.global_router.route_net(net_id, pad_positions)

        if assignment is None:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
                error_message="Global router failed to find corridor assignment",
                alternative_strategies=self._suggest_alternatives(
                    RoutingStrategy.GLOBAL_WITH_REPAIR
                ),
            )

        # Materialise corridor centreline into Segment objects so the
        # caller can persist them.  Each consecutive pair of waypoint
        # coordinates becomes a trace segment on the assignment's layer
        # (defaults to F.Cu when ``assignment.layer == 0``).
        waypoints = assignment.waypoint_coords
        trace_width = getattr(self.rules, "trace_width", 0.2)
        # Map the GlobalRouter's integer layer index to a router Layer enum.
        try:
            from .layers import Layer as RouterLayer

            seg_layer = (
                RouterLayer.B_CU
                if getattr(assignment, "layer", 0) and assignment.layer != 0
                else RouterLayer.F_CU
            )
        except Exception:  # pragma: no cover - import safety
            seg_layer = None  # type: ignore[assignment]

        net_name = ""
        if pads and pads[0].net_name:
            net_name = pads[0].net_name
        elif isinstance(net, str):
            net_name = net

        segments: list = []
        total_length = 0.0
        for i in range(len(waypoints) - 1):
            x1, y1 = waypoints[i]
            x2, y2 = waypoints[i + 1]
            dx = x2 - x1
            dy = y2 - y1
            length = math.sqrt(dx * dx + dy * dy)
            if length == 0.0:
                # Skip degenerate zero-length segments emitted by the
                # corridor builder at region transitions.
                continue
            total_length += length
            if seg_layer is not None:
                segments.append(
                    RouterSegment(
                        x1=x1,
                        y1=y1,
                        x2=x2,
                        y2=y2,
                        width=trace_width,
                        layer=seg_layer,
                        net=net_id,
                        net_name=net_name,
                    )
                )

        return RoutingResult(
            success=True,
            net=net,
            strategy_used=RoutingStrategy.GLOBAL_WITH_REPAIR,
            segments=segments,
            metrics=RoutingMetrics(
                total_length_mm=total_length,
                via_count=0,
                layer_changes=0,
            ),
        )

    def _route_escape_then_global(self, net: str | int, pads: list[Pad] | None) -> RoutingResult:
        """Execute escape routing followed by global routing.

        Phase 1: Uses EscapeRouter to generate escape routes for dense
        packages, freeing inner pins for routing.
        Phase 2: Uses GlobalRouter to route the remaining connections.

        Args:
            net: Net identifier
            pads: List of pads

        Returns:
            RoutingResult combining escape and global routing
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.ESCAPE_THEN_GLOBAL,
                error_message="Insufficient pads for escape routing",
            )

        escape_count = 0
        escape_vias = 0
        escape_segments_list: list = []
        escape_vias_list: list = []

        # Phase 1: Escape routing
        if self._escape is None:
            grid = getattr(self.pcb, "grid", None)
            if grid is not None:
                self._install_grid_keepouts(grid)  # Issue #6059
                self._escape = EscapeRouter(
                    grid=grid,
                    rules=self.rules,
                    net_class_map=self.net_class_map,
                    edge_clearance=getattr(self.pcb, "_edge_clearance", None),
                    board_bounds=getattr(self.pcb, "_board_bbox", None),
                    manufacturer=getattr(self.rules, "manufacturer", None),
                    # Issue #2639 / Epic #2556 Phase 2F: thread the
                    # diff-pair partner map into the escape router for
                    # coupled-at-launch escape routes.
                    #
                    # Issue #3432: gated on ``paired_escape_coupling``
                    # (mirrors core.py's ``_escape`` property, #3419).
                    # Phase 2 of this strategy is the per-net
                    # GlobalRouter -- no CoupledPathfinder exists on the
                    # route-auto path, so threading the map would emit
                    # tightly-coupled paired escape endpoints that
                    # strand the per-net search.
                    diff_pair_map=(
                        self._get_diff_pair_map() if self.paired_escape_coupling else {}
                    ),
                    # Issue #3428: net -> pad-position map for target-aware
                    # in-pad rescue stub directions.  Same wiring as the
                    # Autorouter (``kct route``) path -- the orchestrator
                    # (``kct route-auto``) is an independent code path and
                    # fixing only one is a known foot-gun in this codebase.
                    net_target_positions=self._build_net_target_positions(),
                    # Issue #5201 (reopened): the COMPLETE physical hole
                    # census -- see ``_build_component_hole_census``.
                    # ``None`` (neither PCB shape usable) fails closed:
                    # the in-pad rescue refuses eligibility rather than
                    # silently granting it, exactly like every other
                    # missing-context path in this module.
                    component_holes=self._build_component_hole_census(),
                )

        if self._escape is not None:
            # Issue #6059/#6061: the escape generator gates stubs and vias on
            # the keepout rule areas installed on its grid, so they must be
            # there before it runs (idempotent per grid).
            self._install_grid_keepouts(self._escape.grid)
            package_info = self._escape.analyze_package(pads)
            if package_info.is_dense:
                escape_routes = self._escape.generate_escapes(package_info)
                # Issue #6059: the #6061 commit-time keepout gate lives in
                # ``EscapeRouter.apply_escape_routes``, which this path never
                # calls (it hands the copper back instead of committing it).
                # Apply the same gate here: an escape whose stub or via enters
                # a keepout rule area is dropped, leaving its pad to the
                # corridor phase.
                escape_routes = self._drop_keepout_escapes(self._escape.grid, escape_routes)
                escape_count = len(escape_routes)
                for er in escape_routes:
                    escape_segments_list.extend(er.segments)
                    if er.via is not None:
                        escape_vias_list.append(er.via)
                        escape_vias += 1

                logger.info(
                    "Escape routing: %d escape routes generated (%d with vias)",
                    escape_count,
                    escape_vias,
                )

        # Phase 2: Global routing for remaining connections
        global_result = self._route_global(net, pads)

        # Merge escape + global results
        return RoutingResult(
            success=global_result.success,
            net=net,
            strategy_used=RoutingStrategy.ESCAPE_THEN_GLOBAL,
            segments=escape_segments_list + global_result.segments,
            vias=escape_vias_list + global_result.vias,
            metrics=RoutingMetrics(
                total_length_mm=global_result.metrics.total_length_mm,
                via_count=global_result.metrics.via_count + escape_vias,
                layer_changes=global_result.metrics.layer_changes + escape_vias,
                escape_segments=escape_count,
            ),
        )

    # ------------------------------------------------------------------
    # Hierarchical routing on the real board (Issue #6107)
    #
    # The legacy hierarchical strategy built an AdaptiveAutorouter from the
    # routed net's own pads only, so its grid held none of the other nets'
    # pads or copper: it drew straight through them, and the output gate then
    # refused the short (or, before #6107, missed it).  When the board file is
    # known, the strategy instead loads the board the way ``kct route --nets``
    # does -- every pad with its real shape (other nets' as obstacles), every
    # existing track and via, the edge keepout and the keepout rule areas --
    # adds the board's arcs, which that loader does not read, and routes the
    # one net on it.
    # ------------------------------------------------------------------

    #: Wall-clock cap on one net's board-grid route (the router's own
    #: ``per_net_timeout`` / ``timeout``; issue #2794).
    HIERARCHICAL_BOARD_NET_TIMEOUT_S = 120.0

    def _load_board_router(
        self, path: Path, net_name: str, board_nets: set[str], *, raise_base: bool = True
    ) -> tuple[Any, int]:
        """The board as ``kct route --nets <net>`` loads it, plus its arcs.

        Returns ``(router, net_id)``.  Every other net is skipped (its pads
        stay on the grid as obstacles) and every existing track, via and arc
        is marked on the grid -- on the paired C++ grid too (the loader mirrors
        its tracks and vias itself since #6103; the arcs are mirrored here).
        """
        import contextlib
        import copy
        from dataclasses import replace as _replace

        from .foreign_copper import ARC_FLATTEN_ERROR_MM, flatten_arc
        from .io import load_pcb_for_routing
        from .layers import Layer
        from .primitives import Route, Segment

        rules = copy.copy(self.rules)
        # Route at no less than the clearance KiCad will measure this net
        # against -- the largest per-pair requirement between it and any net
        # on the board (#6122: netclasses and .kicad_dru rules) -- so a clean
        # route is one the output gate (and kicad-cli) accepts.
        # ``raise_base=False`` keeps the orchestrator's own base, so the
        # project's authored floors survive as floors (#6254 final pass).
        required = self._net_required_clearance(net_name) if raise_base else 0.0
        if required > rules.trace_clearance:
            rules = _replace(rules, trace_clearance=required)
        if required > rules.via_clearance:
            rules = _replace(rules, via_clearance=required)

        with contextlib.redirect_stdout(sys.stderr):
            router, net_map = load_pcb_for_routing(
                str(path),
                skip_nets=sorted(board_nets - {net_name}),
                rules=rules,
                use_pcb_rules=False,
                validate_drc=False,
                layer_stack=self._known_layer_stack(),
                load_existing_routes=True,
            )
        net_id = int(net_map.get(net_name, 0) or 0)

        # Arcs: ``load_pcb_for_routing`` reads only straight tracks and vias.
        ox, oy = getattr(self.pcb, "_board_origin", (0.0, 0.0)) or (0.0, 0.0)
        for arc in getattr(self.pcb, "arcs", None) or []:
            try:
                points = flatten_arc(arc)
                layer = Layer.from_kicad_name(str(arc.layer))
            except (AttributeError, TypeError, ValueError):
                continue
            arc_net_name = str(getattr(arc, "net_name", "") or "")
            arc_net = int(net_map.get(arc_net_name, 0) or getattr(arc, "net_number", 0) or 0)
            width = float(arc.width) + 2 * ARC_FLATTEN_ERROR_MM
            segments = [
                Segment(
                    x1=p[0] + ox,
                    y1=p[1] + oy,
                    x2=q[0] + ox,
                    y2=q[1] + oy,
                    width=width,
                    layer=layer,
                    net=arc_net,
                    net_name=arc_net_name,
                )
                for p, q in zip(points, points[1:], strict=False)
            ]
            route = Route(net=arc_net, net_name=arc_net_name, segments=segments, vias=[])
            router.grid.mark_route(route)
            # The loader mirrors its own tracks and vias onto the paired C++
            # grid (#6103); these arcs it never saw, so mirror only them.
            router.grid._mark_route_on_cpp_cells(route)
            router.existing_routes.append(route)
        return router, net_id

    def _route_hierarchical_on_board(self, net: str | int, pads: list[Pad]) -> RoutingResult | None:
        """Route ``net`` on the board-loaded grid; ``None`` when not applicable.

        Not applicable (the legacy own-pads grid runs instead, with the output
        gate as the guarantee) when the orchestrator's PCB has no board file,
        or when it routes synthetic ``--region`` stub terminals, which are not
        pads of the board.
        """
        path = self._board_path()
        if path is None:
            return None
        if any(not getattr(p, "ref", "") or getattr(p, "pin", "") == "stub" for p in pads):
            return None
        net_name = net if isinstance(net, str) else ""
        if not net_name:
            net_name = next((p.net_name for p in pads if getattr(p, "net_name", "")), "")
        board_nets_raw = getattr(self.pcb, "nets", None)
        if not net_name or not isinstance(board_nets_raw, dict):
            return None
        board_nets = {str(getattr(n, "name", "") or "") for n in board_nets_raw.values()} - {""}
        if net_name not in board_nets:
            return None

        try:
            router, net_id = self._load_board_router(path, net_name, board_nets)
        except Exception as exc:
            message = (
                f"hierarchical: could not load the board for net '{net_name}' "
                f"({type(exc).__name__}: {exc}); routing on the net's own pads only, "
                "where other nets' copper is checked only after routing."
            )
            logger.warning(message)
            print(f"Warning: {message}", file=sys.stderr)
            return None
        self._hierarchical_board_router = router
        if net_id <= 0 or net_id not in router.nets:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                error_message=f"Net '{net_name}' has no routable pads on the board grid",
            )

        import contextlib
        from dataclasses import replace as _replace

        from .observability import validate_net_connectivity

        with contextlib.redirect_stdout(sys.stderr):
            router.route_all(
                per_net_timeout=self.HIERARCHICAL_BOARD_NET_TIMEOUT_S,
                timeout=self.HIERARCHICAL_BOARD_NET_TIMEOUT_S,
            )

        new_routes = [r for r in router.routes if r.net == net_id]
        own_existing = [r for r in router.existing_routes if r.net == net_id]
        net_pads = [router.pads[k] for k in router.nets.get(net_id, []) if k in router.pads]
        pads_total = len(net_pads)
        info = (
            validate_net_connectivity(new_routes + own_existing, {net_id: net_pads}).get(net_id)
            if pads_total >= 2
            else None
        )
        if pads_total < 2:
            connected, pads_connected = True, pads_total
        elif info is None:
            connected, pads_connected = False, 1 if pads_total else 0
        else:
            connected = bool(info.get("connected"))
            pads_connected = int(info.get("connected_pads", 0))

        # The loader routes in the sheet frame; the orchestrator's pads, and
        # ``route_net_auto``'s writer, use the board-relative one.
        ox, oy = getattr(self.pcb, "_board_origin", (0.0, 0.0)) or (0.0, 0.0)
        segments: list = []
        vias: list = []
        total_length = 0.0
        for route in new_routes:
            for seg in route.segments:
                segments.append(
                    _replace(seg, x1=seg.x1 - ox, y1=seg.y1 - oy, x2=seg.x2 - ox, y2=seg.y2 - oy)
                )
                total_length += math.hypot(seg.x2 - seg.x1, seg.y2 - seg.y1)
            for via in route.vias:
                vias.append(_replace(via, x=via.x - ox, y=via.y - oy))

        partial = not connected and bool(segments or vias)
        return RoutingResult(
            success=connected,
            partial=partial,
            pads_connected=pads_connected,
            pads_total=pads_total,
            net=net,
            strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
            error_message=(
                ""
                if connected
                else (
                    f"Hierarchical router connected {pads_connected}/{pads_total} pads of "
                    f"'{net_name}' on the board grid, where every other net's pads, "
                    "tracks, arcs and vias are obstacles"
                )
            ),
            segments=segments,
            vias=vias,
            metrics=RoutingMetrics(
                total_length_mm=total_length,
                via_count=len(vias),
                layer_changes=len(vias),
            ),
        )

    def _route_hierarchical(
        self, net: str | int, intent: NetIntent | None, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute hierarchical routing for differential pairs.

        Uses AdaptiveAutorouter which automatically discovers the optimal
        layer count and routes using negotiated congestion resolution.
        Differential pair intent is passed through to guide routing.

        Args:
            net: Net identifier
            intent: Design intent (should have is_differential=True)
            pads: List of pads

        Returns:
            RoutingResult from hierarchical routing
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                error_message="Insufficient pads for hierarchical routing",
            )

        # Issue #6107: with the board file known, route on the real board --
        # other nets' pads, tracks, arcs and vias included -- not on a grid
        # holding only this net's pads.
        board_result = self._route_hierarchical_on_board(net, pads)
        if board_result is not None:
            return board_result

        if self._hierarchical is None:
            width = getattr(self.pcb, "width", 65.0)
            height = getattr(self.pcb, "height", 56.0)

            # Build component data from available pads
            components_by_ref: dict[str, dict] = {}
            net_map: dict[str, int] = {}

            for pad in pads:
                ref = pad.ref or "U1"
                if ref not in components_by_ref:
                    components_by_ref[ref] = {
                        "ref": ref,
                        "x": pad.x,
                        "y": pad.y,
                        "rotation": 0,
                        "pads": [],
                    }
                comp = components_by_ref[ref]
                comp["pads"].append(
                    {
                        "number": pad.pin or str(len(comp["pads"]) + 1),
                        "x": pad.x - comp["x"],
                        "y": pad.y - comp["y"],
                        "net": pad.net_name or f"Net_{pad.net}",
                        "through_hole": pad.through_hole,
                    }
                )
                net_name = pad.net_name or f"Net_{pad.net}"
                if net_name not in net_map:
                    net_map[net_name] = pad.net

            # Skip power nets if differential pair intent
            skip_nets: list[str] = []
            if intent and hasattr(intent, "skip_nets"):
                skip_nets = intent.skip_nets

            # Issue #6059: this AdaptiveAutorouter has no source board file,
            # so hand it the board's keepout rule areas (resolved per tried
            # stack inside).  Cap its layer escalation at the board's copper
            # count: on a 2-layer board a 4-layer retry would otherwise route
            # straight past a full-height F.Cu/B.Cu keepout on In1.Cu/In2.Cu
            # -- layers the board does not have.
            known_stack = self._known_layer_stack()
            max_layers = max(2, known_stack.num_layers) if known_stack is not None else 6
            self._hierarchical = AdaptiveAutorouter(
                width=width,
                height=height,
                components=list(components_by_ref.values()),
                net_map=net_map,
                rules=self.rules,
                verbose=False,
                skip_nets=skip_nets,
                max_layers=max_layers,
                rule_area_specs=self._rule_area_specs() or None,
                # Issue #6099: an 8+-layer board has no preset rung; let the
                # ladder end on the board's own stack.
                board_layer_stack=known_stack,
            )

        adaptive_result = self._hierarchical.route()

        # Convert AdaptiveAutorouter result to orchestrator RoutingResult
        total_length = 0.0
        via_count = 0
        all_segments: list = []
        all_vias: list = []

        for route in adaptive_result.routes:
            for seg in route.segments:
                length = math.sqrt((seg.x2 - seg.x1) ** 2 + (seg.y2 - seg.y1) ** 2)
                total_length += length
                all_segments.append(seg)
            via_count += len(route.vias)
            all_vias.extend(route.vias)

        return RoutingResult(
            success=adaptive_result.converged,
            net=net,
            strategy_used=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
            error_message=(
                ""
                if adaptive_result.converged
                else (
                    "Hierarchical router did not converge "
                    f"({getattr(adaptive_result, 'nets_routed', '?')}/"
                    f"{getattr(adaptive_result, 'nets_requested', '?')} net(s) routed "
                    f"at up to {getattr(adaptive_result, 'layer_count', '?')} layers)"
                )
            ),
            segments=all_segments,
            vias=all_vias,
            metrics=RoutingMetrics(
                total_length_mm=total_length,
                via_count=via_count,
                layer_changes=via_count,
            ),
        )

    def _route_subgrid_adaptive(self, net: str | int, pads: list[Pad] | None) -> RoutingResult:
        """Execute sub-grid adaptive routing for dense areas.

        Uses AdaptiveGridRouter for two-phase routing:
        Phase 1: Fine-grid escape routing for off-grid pads
        Phase 2: Coarse-grid channel routing handled by ``_route_global``

        Issue #2913: previously this method returned ``success=True`` even
        when no escape segments were generated, then never produced
        coarse-grid segments either, leading to silent data loss in
        ``route_net_auto``.  The fix chains a ``_route_global`` call when
        no fine-pitch components are detected (or when the sub-grid grid
        is unavailable), and surfaces the escape segments in
        ``result.segments`` so the caller can persist them.

        Args:
            net: Net identifier
            pads: List of pads

        Returns:
            RoutingResult from adaptive grid routing.  ``segments`` carries
            any escape segments + the global-router corridor segments so
            the caller can persist them via ``add_trace``.
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.SUBGRID_ADAPTIVE,
                error_message="No pads provided for sub-grid adaptive routing",
            )

        # Check if any pads are from fine-pitch components
        fine_components = identify_fine_pitch_components(
            pads,
            coarse_resolution=getattr(self.rules, "grid_resolution", 0.1),
        )

        escape_count = 0
        escape_segments: list = []
        if fine_components:
            # Initialize sub-grid router for escape phase
            if self._subgrid is None:
                self._install_grid_keepouts(getattr(self.pcb, "grid", None))  # Issue #6059
                self._subgrid = SubGridRouter(
                    grid=self.pcb.grid if hasattr(self.pcb, "grid") else None,
                    rules=self.rules,
                )

            if self._subgrid.grid is not None:
                # Issue #6059/#6061: SubGridRouter gates each escape leg on the
                # grid's keepout rule areas -- install them first.
                self._install_grid_keepouts(self._subgrid.grid)
                fine_pads = [p for p in pads if p.ref in fine_components]
                if fine_pads:
                    subgrid_result = self._subgrid.route_with_subgrid(fine_pads)
                    escape_count = subgrid_result.success_count
                    # Surface escape segments so the caller can persist them.
                    for esc in subgrid_result.escapes:
                        if esc.segment is not None:
                            escape_segments.append(esc.segment)

            logger.info(
                "Sub-grid adaptive: %d fine-pitch components, %d escapes generated",
                len(fine_components),
                escape_count,
            )

        # Phase 2: global routing for the remaining inter-component path.
        global_result = self._route_global(net, pads)

        return RoutingResult(
            success=global_result.success,
            net=net,
            strategy_used=RoutingStrategy.SUBGRID_ADAPTIVE,
            segments=escape_segments + global_result.segments,
            vias=global_result.vias,
            metrics=RoutingMetrics(
                total_length_mm=global_result.metrics.total_length_mm,
                via_count=global_result.metrics.via_count,
                layer_changes=global_result.metrics.layer_changes,
                escape_segments=escape_count,
            ),
            error_message=global_result.error_message,
            alternative_strategies=global_result.alternative_strategies,
        )

    def _route_with_via_resolution(self, net: str | int, pads: list[Pad] | None) -> RoutingResult:
        """Execute routing with via conflict resolution.

        Detects vias from other nets that block access to this net's pads,
        resolves them using relocation (falling back to rip-reroute), then
        routes the net using the global router.

        Args:
            net: Net identifier
            pads: List of pads

        Returns:
            RoutingResult after via conflict resolution
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.VIA_CONFLICT_RESOLUTION,
                error_message="Insufficient pads for via conflict resolution routing",
            )

        manager = self.via_manager
        if manager is None:
            # No grid available, fall back to global routing
            logger.warning(
                "Via conflict resolution requested but no routing grid available, "
                "falling back to global routing"
            )
            result = self._route_global(net, pads)
            result.strategy_used = RoutingStrategy.VIA_CONFLICT_RESOLUTION
            result.warnings.append(
                "Via conflict resolution unavailable (no routing grid); "
                "used global routing as fallback"
            )
            return result

        # Find all blocking vias for this net's pads
        net_id = net if isinstance(net, int) else 0
        all_conflicts = []
        for pad in pads:
            conflicts = manager.find_blocking_vias(pad=pad, pad_net=net_id)
            all_conflicts.extend(conflicts)

        # Deduplicate conflicts by via position
        seen_positions: set[tuple[float, float]] = set()
        unique_conflicts = []
        for conflict in all_conflicts:
            key = (round(conflict.via.x, 4), round(conflict.via.y, 4))
            if key not in seen_positions:
                seen_positions.add(key)
                unique_conflicts.append(conflict)

        # Resolve conflicts using RELOCATE strategy (with fallback to rip-reroute)
        resolutions = []
        if unique_conflicts:
            resolutions = manager.resolve_conflicts(
                unique_conflicts,
                strategy=ViaConflictStrategy.RELOCATE,
            )
            logger.info(
                "Via conflict resolution: %d conflicts found, %d resolutions attempted",
                len(unique_conflicts),
                len(resolutions),
            )

        # Route the net after conflict resolution
        result = self._route_global(net, pads)
        result.strategy_used = RoutingStrategy.VIA_CONFLICT_RESOLUTION

        # Add via conflict resolution info to result
        successful_resolutions = sum(1 for r in resolutions if getattr(r, "success", False))
        if unique_conflicts:
            result.warnings.append(
                f"Via conflict resolution: {len(unique_conflicts)} conflicts found, "
                f"{successful_resolutions} resolved"
            )

        return result

    def _route_full_pipeline(
        self, net: str | int, intent: NetIntent | None, pads: list[Pad] | None
    ) -> RoutingResult:
        """Execute complete routing pipeline with all stages.

        Chains all routing strategies in sequence:
        1. Escape routing (if fine-pitch pads detected)
        2. Global routing for coarse path planning
        3. Sub-grid adaptive routing for dense areas
        4. Via conflict resolution (if conflicts detected)
        5. Clearance repair on final result

        Each phase handles failures gracefully — if an early phase fails,
        later phases still attempt routing. Metrics are aggregated across
        all phases.

        Args:
            net: Net identifier
            intent: Optional design intent
            pads: List of pads

        Returns:
            RoutingResult from full pipeline with aggregated metrics
        """
        if pads is None or len(pads) < 2:
            return RoutingResult(
                success=False,
                net=net,
                strategy_used=RoutingStrategy.FULL_PIPELINE,
                error_message="Insufficient pads for full pipeline routing",
            )

        strategies_used: list[str] = []
        all_segments: list = []
        all_vias: list = []
        all_warnings: list[str] = []
        total_length = 0.0
        total_via_count = 0
        total_layer_changes = 0
        total_escape_segments = 0
        total_repair_actions = 0
        pipeline_success = False

        # Phase 1: Escape routing (if fine-pitch pads detected)
        if self._needs_escape_routing(pads):
            try:
                escape_result = self._route_escape_then_global(net, pads)
                strategies_used.append("escape_then_global")
                if escape_result.success:
                    pipeline_success = True
                    all_segments.extend(escape_result.segments)
                    all_vias.extend(escape_result.vias)
                    total_length += escape_result.metrics.total_length_mm
                    total_via_count += escape_result.metrics.via_count
                    total_layer_changes += escape_result.metrics.layer_changes
                    total_escape_segments += escape_result.metrics.escape_segments
                else:
                    all_warnings.append(f"Escape routing failed: {escape_result.error_message}")
            except Exception as e:
                logger.warning("Full pipeline: escape routing phase failed: %s", e)
                all_warnings.append(f"Escape routing exception: {e}")
        else:
            # Phase 2: Global routing (when escape routing is not needed)
            try:
                global_result = self._route_global(net, pads)
                strategies_used.append("global")
                if global_result.success:
                    pipeline_success = True
                    all_segments.extend(global_result.segments)
                    all_vias.extend(global_result.vias)
                    total_length += global_result.metrics.total_length_mm
                    total_via_count += global_result.metrics.via_count
                    total_layer_changes += global_result.metrics.layer_changes
                else:
                    all_warnings.append(f"Global routing failed: {global_result.error_message}")
            except Exception as e:
                logger.warning("Full pipeline: global routing phase failed: %s", e)
                all_warnings.append(f"Global routing exception: {e}")

        # Phase 3: Sub-grid adaptive routing for dense areas
        if self._check_density(pads) > self.density_threshold:
            try:
                subgrid_result = self._route_subgrid_adaptive(net, pads)
                strategies_used.append("subgrid_adaptive")
                if subgrid_result.success:
                    total_escape_segments += subgrid_result.metrics.escape_segments
                else:
                    all_warnings.append(f"Sub-grid adaptive failed: {subgrid_result.error_message}")
            except Exception as e:
                logger.warning("Full pipeline: sub-grid adaptive phase failed: %s", e)
                all_warnings.append(f"Sub-grid adaptive exception: {e}")

        # Phase 4: Via conflict resolution (if conflicts detected)
        if self.enable_via_conflict_resolution and self._has_via_conflicts(net, pads):
            try:
                via_result = self._route_with_via_resolution(net, pads)
                strategies_used.append("via_conflict_resolution")
                if via_result.success:
                    # Via resolution re-routes, so use its metrics if prior routing
                    # failed or if it produced a better result
                    if not pipeline_success:
                        pipeline_success = True
                        all_segments = list(via_result.segments)
                        all_vias = list(via_result.vias)
                        total_length = via_result.metrics.total_length_mm
                        total_via_count = via_result.metrics.via_count
                        total_layer_changes = via_result.metrics.layer_changes
                all_warnings.extend(via_result.warnings)
            except Exception as e:
                logger.warning("Full pipeline: via conflict resolution phase failed: %s", e)
                all_warnings.append(f"Via conflict resolution exception: {e}")

        # Build the result before clearance repair
        result = RoutingResult(
            success=pipeline_success,
            net=net,
            strategy_used=RoutingStrategy.FULL_PIPELINE,
            segments=all_segments,
            vias=all_vias,
            metrics=RoutingMetrics(
                total_length_mm=total_length,
                via_count=total_via_count,
                layer_changes=total_layer_changes,
                escape_segments=total_escape_segments,
                repair_actions=total_repair_actions,
            ),
            warnings=all_warnings,
        )

        if not pipeline_success:
            result.error_message = "All routing phases failed"
            result.alternative_strategies = self._suggest_alternatives(
                RoutingStrategy.FULL_PIPELINE
            )

        # Phase 5: Clearance repair on final result
        if pipeline_success and self.enable_repair and result.violations:
            try:
                repair_count = self._apply_clearance_repair(result)
                if repair_count > 0:
                    strategies_used.append("clearance_repair")
                    total_repair_actions += repair_count
                    result.metrics.repair_actions = total_repair_actions
            except Exception as e:
                logger.warning("Full pipeline: clearance repair phase failed: %s", e)
                result.warnings.append(f"Clearance repair exception: {e}")

        # Record which strategies were used
        if strategies_used:
            result.warnings.insert(0, f"Full pipeline phases: {', '.join(strategies_used)}")

        logger.info(
            "Full pipeline complete: net=%s, success=%s, phases=%s",
            net,
            pipeline_success,
            strategies_used,
        )

        return result

    def _apply_clearance_repair(self, result: RoutingResult) -> int:
        """Apply automatic clearance repair to fix violations.

        Uses ClearanceRepairer to compute minimal displacements for traces
        and vias that violate clearance rules. Requires a PCB file path
        to be available on the PCB object.

        Args:
            result: RoutingResult to repair (modified in place)

        Returns:
            Number of repairs applied
        """
        if not result.violations:
            return 0

        pcb_path = getattr(self.pcb, "path", None)
        if pcb_path is None:
            logger.warning("Clearance repair requested but no PCB file path available")
            return 0

        from ..core.types import Severity
        from ..drc.repair_clearance import ClearanceRepairer
        from ..drc.report import DRCReport
        from ..drc.violation import DRCViolation as DrcViolation
        from ..drc.violation import Location, ViolationType

        # Convert orchestrator violations to DRC report format
        drc_violations = []
        for v in result.violations:
            drc_v = DrcViolation(
                type=ViolationType.from_string(v.violation_type),
                type_str=v.violation_type,
                severity=(Severity.ERROR if v.severity == "error" else Severity.WARNING),
                message=v.description,
                locations=[
                    Location(x_mm=v.location[0], y_mm=v.location[1]),
                ],
                nets=list(v.affected_nets),
            )
            drc_violations.append(drc_v)

        report = DRCReport(
            source_file=str(pcb_path),
            created_at=None,
            pcb_name="",
            violations=drc_violations,
        )

        try:
            repairer = ClearanceRepairer(pcb_path)
            repair_result = repairer.repair_from_report(report)

            # Convert repair nudges to RepairAction objects
            for nudge in repair_result.nudges:
                result.repair_actions.append(
                    RepairAction(
                        action_type="nudge",
                        target=(
                            f"{nudge.object_type} [{nudge.net_name}] "
                            f"at ({nudge.x:.4f}, {nudge.y:.4f})"
                        ),
                        displacement_mm=nudge.displacement_mm,
                        success=True,
                        notes=(
                            f"Clearance {nudge.old_clearance_mm:.4f} "
                            f"-> {nudge.new_clearance_mm:.4f}mm"
                        ),
                    )
                )

            if repair_result.repaired > 0:
                repairer.save()

            return repair_result.repaired

        except Exception as e:
            logger.warning("Clearance repair failed: %s", e)
            return 0

    def _route_multi_resolution(
        self,
        net: str | int,
        pads: list[Pad] | None,
    ) -> RoutingResult:
        """Route a net using multi-resolution strategy.

        This delegates to the global router first and, on failure, notes
        that multi-resolution retry is recommended. The actual fine-grid
        retry is best performed at the board level via
        ``Autorouter.route_all_multi_resolution()`` since it requires
        grid reconstruction.

        Args:
            net: Net identifier
            pads: Optional list of pads

        Returns:
            RoutingResult with multi-resolution metadata
        """
        # First attempt with global routing
        result = self._route_global(net, pads)
        result.strategy_used = RoutingStrategy.MULTI_RESOLUTION

        if not result.success:
            result.warnings.append(
                "Net failed on coarse grid. Use Autorouter.route_all_multi_resolution() "
                "for automatic fine-grid retry."
            )

        return result

    def _suggest_alternatives(self, failed_strategy: RoutingStrategy) -> list[AlternativeStrategy]:
        """Suggest alternative strategies when a strategy fails.

        Args:
            failed_strategy: The strategy that failed

        Returns:
            List of alternative strategies to try
        """
        alternatives = []

        # If global routing failed, try multi-resolution then hierarchical
        if failed_strategy == RoutingStrategy.GLOBAL_WITH_REPAIR:
            alternatives.append(
                AlternativeStrategy(
                    strategy=RoutingStrategy.MULTI_RESOLUTION,
                    reason="Fine-grid retry may resolve geometry-constrained failures",
                    estimated_cost=1.5,
                    success_probability=0.65,
                )
            )
            alternatives.append(
                AlternativeStrategy(
                    strategy=RoutingStrategy.HIERARCHICAL_DIFF_PAIR,
                    reason="Multi-resolution routing may find path where global failed",
                    estimated_cost=1.5,
                    success_probability=0.6,
                )
            )

        # Always suggest full pipeline as last resort
        if failed_strategy != RoutingStrategy.FULL_PIPELINE:
            alternatives.append(
                AlternativeStrategy(
                    strategy=RoutingStrategy.FULL_PIPELINE,
                    reason="Complete pipeline applies all routing techniques",
                    estimated_cost=2.0,
                    success_probability=0.7,
                )
            )

        return alternatives

    @property
    def global_router(self) -> GlobalRouter:
        """Lazy-initialized global router instance."""
        if self._global_router is None:
            if self._region_graph is None:
                board_width = getattr(self.pcb, "width", 65.0)
                board_height = getattr(self.pcb, "height", 56.0)
                self._region_graph = RegionGraph(board_width=board_width, board_height=board_height)
            self._global_router = GlobalRouter(
                region_graph=self._region_graph,
                corridor_width=self.corridor_width,
            )
        return self._global_router

    @property
    def escape_router(self) -> EscapeRouter | None:
        """Lazy-initialized escape router instance.

        Returns None if the PCB does not expose a routing grid.
        """
        if self._escape is None:
            grid = getattr(self.pcb, "grid", None)
            if grid is not None:
                self._install_grid_keepouts(grid)  # Issue #6059
                self._escape = EscapeRouter(
                    grid=grid,
                    rules=self.rules,
                    net_class_map=self.net_class_map,
                    edge_clearance=getattr(self.pcb, "_edge_clearance", None),
                    board_bounds=getattr(self.pcb, "_board_bbox", None),
                    manufacturer=getattr(self.rules, "manufacturer", None),
                    # Issue #2639 / Epic #2556 Phase 2F: same threading
                    # as the escape_then_global ctor site above.
                    #
                    # Issue #3432: same ``paired_escape_coupling`` gate
                    # as the escape_then_global ctor site above.
                    diff_pair_map=(
                        self._get_diff_pair_map() if self.paired_escape_coupling else {}
                    ),
                    # Issue #3428: same target-aware in-pad stub wiring
                    # as the escape_then_global ctor site above.
                    net_target_positions=self._build_net_target_positions(),
                    # Issue #5201 (reopened): same component-hole census
                    # wiring as the escape_then_global ctor site above.
                    component_holes=self._build_component_hole_census(),
                )
        return self._escape

    @property
    def via_manager(self) -> ViaConflictManager | None:
        """Lazy-initialized via conflict manager instance.

        Returns None if the PCB does not expose a routing grid.
        """
        if self._via_manager is None:
            grid = getattr(self.pcb, "grid", None)
            if grid is not None:
                self._install_grid_keepouts(grid)  # Issue #6059
                self._via_manager = ViaConflictManager(grid=grid, rules=self.rules)
        return self._via_manager
