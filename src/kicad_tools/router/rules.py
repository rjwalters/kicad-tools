"""
Design rules and net class routing parameters.

This module provides:
- DesignRules: Trace width, clearance, via parameters, and A* costs
- NetClassRouting: Per-net-class routing preferences
- Predefined net classes for common use cases
"""

from dataclasses import dataclass, field
from typing import Literal

from .layers import Layer

# Allowed values for :attr:`NetClassRouting.route_via`.
# - ``"pathfinder"`` (default) -- ordinary trace, standard pathfinder routing.
# - ``"pour"`` -- skip from pathfinder; expect a copper zone/pour to satisfy
#   the net (e.g. GND, VCC).  When no zone exists, callers fall through to
#   the existing ``no_zone_nets`` warning path rather than skipping silently.
# - ``"manual"`` -- skip from pathfinder; designer is responsible for routing
#   the net by hand (e.g. wide motor-phase traces).  Emits a distinct log
#   line so the user is not left wondering why the net is unconnected.
RouteVia = Literal["pathfinder", "pour", "manual"]


@dataclass
class ZoneRules:
    """Design rules specific to zone (copper pour) handling.

    These parameters control how zones interact with traces, pads, and vias
    during routing. They mirror KiCad's zone settings.

    Attributes:
        clearance: Zone-to-trace clearance in mm
        min_thickness: Minimum copper width within zone in mm
        thermal_gap: Gap between pad and zone copper for thermal relief in mm
        thermal_bridge_width: Width of thermal relief spokes in mm
        thermal_spoke_count: Number of thermal relief spokes (typically 2 or 4)
        thermal_spoke_angle: Rotation of spoke pattern in degrees (0 or 45)
        pth_connection: Connection type for PTH pads ("thermal", "solid", "none")
        smd_connection: Connection type for SMD pads ("thermal", "solid", "none")
        via_connection: Connection type for vias ("thermal", "solid", "none")
        remove_islands: Whether to remove isolated copper islands
        island_min_area: Minimum area for island removal in mm²
    """

    clearance: float = 0.2  # Zone-to-trace clearance (mm)
    min_thickness: float = 0.2  # Minimum copper width (mm)
    thermal_gap: float = 0.3  # Gap for thermal relief (mm)
    thermal_bridge_width: float = 0.3  # Spoke width (mm)
    thermal_spoke_count: int = 4  # Number of spokes
    thermal_spoke_angle: float = 45.0  # Spoke rotation (degrees)
    pth_connection: str = "thermal"  # PTH pad connection type
    smd_connection: str = "thermal"  # SMD pad connection type
    via_connection: str = "solid"  # Via connection type
    remove_islands: bool = True  # Remove isolated islands
    island_min_area: float = 0.5  # Minimum island area (mm²)


@dataclass
class DesignRules:
    """Design rules for routing."""

    trace_width: float = 0.2  # mm
    trace_clearance: float = 0.2  # mm
    via_drill: float = 0.35  # mm (JLCPCB min is 0.3, use 0.35 for margin)
    via_diameter: float = 0.7  # mm (0.35 drill + 0.35 annular ring)
    via_clearance: float = 0.2  # mm
    min_drill_clearance: float = 0.102  # mm (minimum drill-to-drill spacing, including same-net)
    grid_resolution: float = 0.1  # mm (routing grid)
    grid_origin_offset: tuple[float, float] = (
        0.0,
        0.0,
    )  # mm (grid origin shift for mixed-pitch alignment)

    # Manufacturer identifier (Issue #2605)
    # Used by the escape router (and potentially other consumers) to look up
    # manufacturer capability flags such as ``via_in_pad_supported`` via
    # ``mfr_limits.get_mfr_limits()``.  When ``None`` (default), the escape
    # router behaves as if the manufacturer does NOT support via-in-pad,
    # preserving pre-#2605 deferred-pin behavior on fine-pitch SSOP/TSSOP.
    manufacturer: str | None = None

    # Manufacturer-tier escalation in progress flag (Issue #2891)
    # When True, the escape router demotes the "Cannot escape ... does not
    # support via-in-pad" ERROR log (escape.py Issue #2880) to DEBUG level
    # because the outer ``route_with_mfr_tier_escalation`` wrapper
    # (route_cmd.py) will recover by switching to a manufacturer tier that
    # DOES support via-in-pad.  Must be cleared by the wrapper before the
    # FINAL tier attempt so that, on a fully-exhausted ladder, the ERROR
    # re-surfaces and the user sees the unfixable constraint.  Threaded
    # through DesignRules rather than the EscapeRouter constructor to keep
    # the change surface small (one new field, one read in escape.py,
    # writes localized to route_cmd.py's escalation wrapper).
    auto_mfr_tier_in_progress: bool = False

    # Per-component clearance overrides (Issue #1016)
    # Maps component reference (e.g., "U1") to clearance in mm
    # Use for fine-pitch ICs where tighter clearance is needed between pins
    component_clearances: dict[str, float] = field(default_factory=dict)

    # Fine-pitch automatic clearance (Issue #1016)
    # When set, components with pin pitch below fine_pitch_threshold automatically
    # use this clearance instead of trace_clearance
    fine_pitch_clearance: float | None = None
    fine_pitch_threshold: float = 0.8  # mm - components with pitch < this use fine_pitch_clearance

    # Trace neck-down configuration (Issue #1018)
    # When routing to fine-pitch pads, traces can be narrowed near the pad to fit
    # between adjacent clearance zones. This creates a smooth taper from normal
    # width to minimum width as the trace approaches the pad.
    min_trace_width: float | None = None  # Minimum width for neck-down (mm), None = disabled
    neck_down_distance: float = 1.0  # Distance from pad center where taper begins (mm)
    neck_down_threshold: float = 0.8  # Only neck-down for pads with pitch < this (mm)

    # Layer preferences
    preferred_layer: Layer = Layer.F_CU
    alternate_layer: Layer = Layer.B_CU

    # Costs for A* (tune these for routing style)
    cost_straight: float = 1.0
    cost_diagonal: float = 1.414
    cost_turn: float = 5.0  # Penalty for changing direction (bends)
    cost_via: float = 10.0  # Penalty for layer change
    cost_layer_inner: float = 2.0  # Penalty for using inner layers (applied by pathfinder)

    # Via cost cap (Issue #2325)
    # Caps the total incremental cost of a single via transition to prevent
    # accumulated additive penalties (inner-layer cost, layer utilization,
    # corridor deviation, congestion, via impact) from making vias
    # prohibitively expensive.  The effective cap is ``via_cost_cap_factor *
    # cost_via``.  Set to 0.0 to disable capping.
    via_cost_cap_factor: float = 2.0

    # Congestion-aware routing
    cost_congestion: float = 2.0  # Multiplier for congested regions
    congestion_threshold: float = 0.3  # Density above which region is congested
    congestion_grid_size: int = 10  # Cells per congestion region

    # Layer utilization balancing (Issue #2275)
    # Penalizes routing on heavily-utilized layers to encourage spreading traces
    # across all available layers.  The cost added per same-layer move or via
    # transition equals ``fill_ratio * cost_layer_utilization``, where fill_ratio
    # is the fraction of routable cells already occupied on the target layer.
    # A value of 5.0 (half of cost_via) makes the router prefer an empty layer
    # via when the current layer is mostly full.  Set to 0.0 to disable.
    cost_layer_utilization: float = 5.0

    # Global corridor deviation penalty (Issue #2288)
    # Penalizes cells outside the corridor assigned by global routing.  The penalty
    # is applied per-cell during A* expansion to steer the detailed router along
    # the corridor path established by the two-phase global router.  Set to 0.0
    # to disable corridor guidance.
    cost_corridor_deviation: float = 5.0

    # Diff-pair / match-group corridor attractor (Issue #2911)
    # Cells reserved by ``EscapeRouter._reserve_pair_continuation_corridor`` for
    # a paired set of nets receive a NEGATIVE cost bonus when the route's net is
    # in the reservation's owner set.  This complements the corridor reservation
    # primitive from #2677 (which protects reserved cells against partner-net
    # vias but does NOT tell the pathfinder to use the reserved layer).  Without
    # this attractor the main pathfinder treats the reserved channel as just
    # another empty layer and may not bother diving into the inner corridor at
    # all -- the board 06 USB3_TX1+/- pair stayed unrouted on top of an
    # otherwise-vacant reserved corridor.
    #
    # The bonus is applied as ``-cost_corridor_attractor`` per cell, capped at
    # the cell's positive cost so the total g_score never goes negative
    # (negative costs would corrupt A* admissibility).
    #
    # Default value 3.0 rationale (calibrated empirically against board 06):
    #
    #   * Comparable to ``cost_straight`` (1.0): one in-corridor step costs
    #     ``1.0 - min(1.0, 3.0) = 0.0`` (i.e., a clamp to free), so the
    #     pathfinder is incentivised to enter the corridor but the clamp
    #     prevents the cell cost from going negative.  The clamp also caps
    #     the "tar pit" risk -- once inside the corridor the attractor cannot
    #     compound across cells to produce a negative-cost cycle.
    #
    #   * ~30% of ``cost_via`` (10.0): big enough to outweigh a single
    #     same-layer detour but NOT a needless via.  The pathfinder will
    #     drop a via to dive into the reserved layer ONLY when the
    #     surface-layer alternative is genuinely costlier (blocked,
    #     congested, or a long detour) -- exactly the board 06 USB3_TX1+/-
    #     case where the surface lane is congested by the BGA escape fan.
    #
    #   * Smaller than ``cost_corridor_deviation`` (5.0): a route that
    #     ALREADY belongs to the global router's corridor should not be
    #     pulled out of it by the diff-pair attractor -- the two penalty
    #     systems compose so the global corridor stays the dominant
    #     directive and the diff-pair attractor refines the layer choice
    #     within it.
    #
    # If a future board surfaces a "tar pit" failure mode (route gets
    # trapped inside the corridor when it should exit early to reach a pad
    # outside), lowering to ~1.5 is the recommended first adjustment --
    # preserves the layer-preference signal without dominating in-corridor
    # detour costs.  Set to 0.0 to disable the attractor entirely
    # (reservations still protect against partner vias).
    cost_corridor_attractor: float = 3.0

    # Corridor penalty decay parameters (Issue #2308)
    # Controls how quickly the corridor penalty relaxes during negotiated
    # rip-up iterations, allowing the detailed router to escape suboptimal
    # global corridors over time.
    #   effective_penalty = corridor_penalty * max(floor, 1.0 - rate * iteration)
    # With defaults (rate=0.05, floor=0.3) the floor is reached at iteration 14.
    corridor_decay_rate: float = 0.05  # per-iteration linear decay
    corridor_decay_floor: float = 0.3  # minimum multiplier (never decays below this)

    # Rip-up cohort stagnation detection (Issue #2597)
    # Controls the heuristic that breaks out of the negotiated outer loop
    # when consecutive iterations rip up the same set of nets without
    # meaningful overflow progress (e.g. chorus-test-revA pattern
    # ``ripup=[{A..F}, {A..F}], overflow=[30, 12, 10]`` — strictly decreasing
    # but each iteration costs ~per-net-timeout × N seconds).
    #   - ``stagnation_overflow_delta_threshold``: minimum fractional
    #     overflow improvement required to *avoid* declaring stagnation.
    #     Default 0.20 (20 %).  Lower values declare stagnation sooner.
    #   - ``stagnation_jaccard_threshold``: minimum Jaccard similarity
    #     between consecutive rip-up cohorts to declare stagnation.  Default
    #     0.8.  A strict subset relationship between cohorts always
    #     satisfies this criterion regardless of the value.
    # See ``detect_ripup_stagnation()`` in
    # ``router.algorithms.negotiated`` for the full heuristic.
    stagnation_overflow_delta_threshold: float = 0.20
    stagnation_jaccard_threshold: float = 0.8

    # Crossing-aware routing (Issue #1250)
    # Penalizes candidate edges that cross already-routed segments on the same layer.
    # This steers A* toward non-crossing paths while still permitting crossings when
    # no alternative exists. Default 0.0 disables the feature for backward compatibility.
    crossing_penalty: float = 0.0  # Additive cost per crossing with a routed segment

    # Zone-specific rules
    zone_rules: ZoneRules = field(default_factory=ZoneRules)

    # Zone routing costs
    cost_zone_same_net: float = 0.1  # Low cost - encourage using zone copper
    cost_zone_clearance: float = 2.0  # Cost near zone boundaries

    # Hard layer constraints (Issue #715)
    # When set, only these layers are allowed for routing (blocks all others)
    # Use layer names like ["F.Cu"] for single-layer routing
    allowed_layers: list[str] | None = None

    # Bidirectional A* configuration (Issue #964)
    # Enable parallel frontier exploration for large paths
    bidirectional_search: bool = True  # Enable bidirectional A* by default
    bidirectional_threshold: int = 1000  # Min grid cells to enable bidirectional
    parallel_workers: int = 2  # Number of parallel workers (typically 2 for bidi)

    # Via placement optimization (Issue #1019)
    # Controls via placement to avoid blocking adjacent nets near fine-pitch ICs
    via_exclusion_from_fine_pitch: float = 0.0  # mm exclusion zone from fine-pitch pads
    via_impact_weight: float = (
        1.0  # Weight for via impact scoring (0=disabled, higher=stronger avoidance)
    )

    # Sub-grid routing for fine-pitch components (Issue #1109)
    # When enabled, generates escape segments from off-grid pad centers to the
    # nearest main-grid points before main routing begins. This allows fine-pitch
    # ICs (0.5-0.65mm pitch) to be routed without requiring a global fine grid.
    subgrid_routing: bool = False  # Enable sub-grid escape routing
    subgrid_escape_radius: int = 3  # Grid cells to search for escape endpoint
    subgrid_clearance_factor: float = (
        0.5  # Relaxed clearance multiplier for sub-grid escape Phase 3
    )

    # Constraint-aware net ordering (Issue #1020)
    # Routes highly-constrained nets first to give them access to routing resources
    # before less-constrained nets consume available channels.
    constraint_ordering_enabled: bool = True  # Enable constraint-aware ordering
    constraint_fine_pitch_weight: float = 10.0  # Weight for fine-pitch component connections
    constraint_pad_count_weight: float = 0.5  # Weight for number of pads in net
    constraint_congestion_weight: float = 5.0  # Weight for nets in congested areas

    # Stitch-via halo for plane-net pads (Issue #2842)
    # When True (default), the routing grid reserves a clearance halo of radius
    # ``stitch_via_halo_radius()`` around plane-net pads (``pad.net == 0``) so the
    # subsequent stitch step can land a via on the pad without colliding with
    # adjacent signal traces.  The halo only blocks *foreign* nets -- the plane
    # net itself (net id 0) is unaffected and the existing fine-pitch trace
    # clearance halo (``_clearance_for_pin_pitch``) is unchanged.  Set to False
    # for designs that intentionally never stitch (single-layer, no-plane, etc.)
    # to avoid the small extra clearance reservation.
    stitch_via_halo: bool = True

    @property
    def max_clearance(self) -> float:
        """Return the maximum clearance across all configured clearance values.

        This is used for conservative R-tree envelope inflation (Issue #2335).
        The inflated envelopes ensure that any segment within clearance distance
        of an indexed segment will be returned by an intersection query,
        eliminating per-query clearance arithmetic.

        The maximum is taken across:
        - Default trace_clearance
        - Per-component clearances (component_clearances dict)
        - Fine-pitch clearance (if configured)
        - Via clearance

        Returns:
            Maximum clearance value in mm.
        """
        clearances = [self.trace_clearance, self.via_clearance]
        if self.component_clearances:
            clearances.extend(self.component_clearances.values())
        if self.fine_pitch_clearance is not None:
            clearances.append(self.fine_pitch_clearance)
        return max(clearances)

    def stitch_via_halo_radius(self) -> float:
        """Return the foreign-net clearance halo to reserve around plane-net pads.

        Issue #2842 -- the stitch pass (``kct stitch``) drops one via per
        plane-net pad to bond the plane to the surface pin.  A via with
        diameter ``D`` and trace clearance ``C`` needs ``D/2 + C`` of clear
        space around the pad center for the via to land without violating
        clearance.  The router has historically sized its pad clearance halo
        for *traces* (``_clearance_for_pin_pitch``, ~0.05-0.3 mm depending on
        pitch), which is too small for a via drop and lets foreign-net
        traces crowd plane-net pads.  This method returns the larger
        via-aware radius so :meth:`RoutingGrid._add_pad_unsafe` can reserve
        the right amount of room for the deferred stitch step.

        The radius is derived from the configured manufacturer when
        available (``rules.manufacturer`` -> ``MfrLimits.min_via_diameter``
        + ``trace_clearance``).  When no manufacturer is configured the
        formula falls back to the stitcher's default 0.45 mm via plus the
        configured ``trace_clearance`` -- i.e. ``0.225 + trace_clearance``.

        TODO(#2848): once the router's ``--mfr`` flag plumbs the
        manufacturer profile through to stitch's via-dimension selection,
        the unmanufactured fallback can be tightened to match whatever the
        stitcher actually uses.  For now the 0.45 mm default mirrors
        ``stitch_cmd.py:2400, :2573`` byte-for-byte.

        Returns:
            Halo radius in mm.  Always at least as large as the standard
            ``trace_clearance + trace_width/2`` envelope so this never
            *shrinks* the existing clearance for callers that opt in.
        """
        # Default via diameter when no manufacturer profile is available.
        # Mirrors ``stitch_cmd.py:2400`` (the canonical stitcher default).
        # TODO(#2848): tighten once stitch consumes mfr-derived via dimensions
        # from the route step.
        default_via_diameter = 0.45

        via_diameter = default_via_diameter
        if self.manufacturer is not None:
            try:
                from .mfr_limits import get_mfr_limits

                mfr = get_mfr_limits(self.manufacturer)
                via_diameter = max(via_diameter, mfr.min_via_diameter)
            except (ValueError, ImportError):
                # Unknown manufacturer -> fall back to the conservative default.
                pass

        via_halo = via_diameter / 2.0 + self.trace_clearance

        # Never shrink below the standard pad-clearance envelope; that
        # envelope was tuned for trace routing and the via-aware envelope
        # is strictly a *minimum*.
        standard_envelope = self.trace_clearance + self.trace_width / 2.0
        return max(via_halo, standard_envelope)

    def get_clearance_for_component(self, ref: str, pin_pitch: float | None = None) -> float:
        """Get the clearance to use for a specific component.

        Checks for per-component clearance overrides, then for automatic
        fine-pitch clearance based on pin pitch, then falls back to the
        default trace_clearance.

        Issue #2867 -- narrow-channel guard (C++ validator path): this
        method is the C++ pad-vs-segment validator's clearance source
        (see ``cpp_backend.py:591`` -> ``clearance_override`` for
        ``add_pad``).  It is *parallel* to
        :meth:`RoutingGrid._clearance_for_pin_pitch` which sets the
        *grid halo* on the Python pathfinder side.  Both layers
        independently shrink for fine-pitch pads.  PR #2866 (issue
        #2865) added a narrow-channel guard to the grid-halo path
        only; this method continued to shrink unconditionally, so the
        C++ validator still accepted geometrically infeasible
        through-channel paths under congestion pressure (44
        ``clearance_pad_segment`` errors on routed board 04).

        Issue #2867 promotes the guard here as well.  When the
        fine-pitch shrink would produce an inter-pad channel that
        cannot host a trace at full manufacturer clearance, we decline
        the shrink and fall back to ``trace_clearance`` so the C++
        validator (and any other ``clearance_override`` consumer)
        rejects through-channel routes the same way the grid halo
        does.  Geometry (mirroring ``_clearance_for_pin_pitch``):

            effective_channel = pin_pitch - 2 * fine_pitch_clearance - trace_width
            required_channel  = 2 * trace_clearance + trace_width

        When ``effective_channel < required_channel`` the shrunk
        clearance is infeasible and the default ``trace_clearance`` is
        used instead.  Explicit per-component overrides
        (``component_clearances``) bypass the guard -- callers who set
        an override are asserting they know the geometry is feasible.

        Args:
            ref: Component reference (e.g., "U1")
            pin_pitch: Optional pin pitch in mm (for automatic fine-pitch detection)

        Returns:
            Clearance in mm to use for this component.

        Example:
            >>> rules = DesignRules(
            ...     trace_clearance=0.15,
            ...     component_clearances={"U1": 0.08},
            ...     fine_pitch_clearance=0.1,
            ...     fine_pitch_threshold=0.8,
            ... )
            >>> rules.get_clearance_for_component("U1")  # Explicit override
            0.08
            >>> rules.get_clearance_for_component("U2", pin_pitch=0.65)  # Auto fine-pitch
            0.1
            >>> rules.get_clearance_for_component("R1")  # Default
            0.15
        """
        # Check explicit per-component override first.  Explicit overrides
        # bypass the narrow-channel guard: the caller is asserting the
        # geometry is feasible for this specific component.
        if ref in self.component_clearances:
            return self.component_clearances[ref]

        # Check for automatic fine-pitch clearance
        if (
            self.fine_pitch_clearance is not None
            and pin_pitch is not None
            and pin_pitch < self.fine_pitch_threshold
        ):
            # Issue #2867 narrow-channel guard.  Mirror the grid-halo
            # check in :meth:`RoutingGrid._clearance_for_pin_pitch` so
            # the C++ validator (which consumes the value returned
            # here as ``clearance_override``) does not accept through-
            # channel routes that DRC will reject as
            # ``clearance_pad_segment``.  The shrunk fine-pitch
            # clearance is only sound when a trace centred between
            # two adjacent pads can satisfy full manufacturer
            # clearance against both.
            effective_channel = (
                pin_pitch - 2.0 * self.fine_pitch_clearance - self.trace_width
            )
            required_channel = 2.0 * self.trace_clearance + self.trace_width
            if effective_channel >= required_channel:
                return self.fine_pitch_clearance
            # Narrow channel -- fine-pitch shrink is geometrically
            # infeasible.  Fall through to the default clearance so
            # the validator rejects through-channel routes.

        # Fall back to default clearance
        return self.trace_clearance

    def should_apply_neck_down(self, ref: str | None, pin_pitch: float | None = None) -> bool:
        """Determine if neck-down should be applied for a component.

        Neck-down is applied when:
        1. min_trace_width is configured (feature enabled)
        2. The component has fine-pitch pins (below neck_down_threshold)

        Args:
            ref: Component reference (e.g., "U1"), or None for general check
            pin_pitch: Optional pin pitch in mm (for automatic detection)

        Returns:
            True if neck-down should be applied, False otherwise.

        Example:
            >>> rules = DesignRules(
            ...     trace_width=0.2,
            ...     min_trace_width=0.1,
            ...     neck_down_threshold=0.8,
            ... )
            >>> rules.should_apply_neck_down("U1", pin_pitch=0.65)  # Fine-pitch
            True
            >>> rules.should_apply_neck_down("R1", pin_pitch=1.27)  # Standard pitch
            False
            >>> rules.should_apply_neck_down("U2")  # No pitch info, use default
            False
        """
        # Feature must be enabled
        if self.min_trace_width is None:
            return False

        # If no pitch info, don't apply neck-down
        if pin_pitch is None:
            return False

        # Apply neck-down only for fine-pitch components
        return pin_pitch < self.neck_down_threshold

    def get_neck_down_width(self, distance_to_pad: float, pin_pitch: float | None = None) -> float:
        """Calculate trace width based on distance to pad center.

        Creates a smooth linear interpolation from trace_width to min_trace_width
        as the trace approaches a fine-pitch pad.

        Args:
            distance_to_pad: Distance from segment point to pad center (mm)
            pin_pitch: Optional pin pitch in mm (for determining if neck-down applies)

        Returns:
            Trace width in mm. Returns trace_width if:
            - Neck-down is disabled (min_trace_width is None)
            - Distance is beyond neck_down_distance
            - Pin pitch is above neck_down_threshold

        Example:
            >>> rules = DesignRules(
            ...     trace_width=0.2,
            ...     min_trace_width=0.1,
            ...     neck_down_distance=1.0,
            ... )
            >>> rules.get_neck_down_width(2.0)  # Far from pad
            0.2
            >>> rules.get_neck_down_width(0.5)  # In taper zone
            0.15
            >>> rules.get_neck_down_width(0.0)  # At pad
            0.1
        """
        # Feature disabled
        if self.min_trace_width is None:
            return self.trace_width

        # Check if this is a fine-pitch situation
        if pin_pitch is not None and pin_pitch >= self.neck_down_threshold:
            return self.trace_width

        # Beyond taper zone - use normal width
        if distance_to_pad >= self.neck_down_distance:
            return self.trace_width

        # Linear interpolation from trace_width to min_trace_width
        # At distance=0: min_trace_width
        # At distance=neck_down_distance: trace_width
        t = distance_to_pad / self.neck_down_distance
        return self.min_trace_width + t * (self.trace_width - self.min_trace_width)


@dataclass
class LengthConstraint:
    """Length constraint for timing-critical nets.

    Use cases:
    - DDR memory buses: Data lines must match clock ±50mil
    - Differential pairs: P/N must match within 5mil
    - Parallel buses: All bits should be similar length
    - Clock distribution: Equal path lengths to all loads

    Attributes:
        net_id: Net ID this constraint applies to
        min_length: Minimum required trace length in mm (optional)
        max_length: Maximum allowed trace length in mm (optional)
        match_group: Group name for nets that must match lengths (optional)
        match_tolerance: Tolerance for length matching in mm (default: 0.5mm)
    """

    net_id: int
    min_length: float | None = None
    max_length: float | None = None
    match_group: str | None = None
    match_tolerance: float = 0.5  # mm

    def __post_init__(self):
        """Validate constraint parameters."""
        if self.min_length is not None and self.max_length is not None:
            if self.min_length > self.max_length:
                raise ValueError(
                    f"min_length ({self.min_length}) cannot be greater than "
                    f"max_length ({self.max_length})"
                )
        if self.match_tolerance < 0:
            raise ValueError(f"match_tolerance must be non-negative, got {self.match_tolerance}")

    def to_dict(self) -> dict:
        """Serialize to a JSON-compatible dict.

        Round-trip companion to :meth:`from_dict` -- used by the
        ``kct check --net-class-map`` sidecar format (Issue #2684).
        """
        return {
            "net_id": self.net_id,
            "min_length": self.min_length,
            "max_length": self.max_length,
            "match_group": self.match_group,
            "match_tolerance": self.match_tolerance,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "LengthConstraint":
        """Deserialize from a dict produced by :meth:`to_dict`.

        Unknown keys are ignored (forward compatibility); missing keys
        fall back to dataclass defaults where applicable.
        """
        return cls(
            net_id=data["net_id"],
            min_length=data.get("min_length"),
            max_length=data.get("max_length"),
            match_group=data.get("match_group"),
            match_tolerance=data.get("match_tolerance", 0.5),
        )


@dataclass
class NetClassRouting:
    """Routing parameters for a net class."""

    name: str
    priority: int = 5  # 1=highest, 10=lowest
    trace_width: float = 0.2  # Override trace width
    clearance: float = 0.2  # Override clearance
    via_size: float = 0.6  # Override via diameter
    cost_multiplier: float = 1.0  # Cost multiplier (lower = prefer this net)
    length_critical: bool = False  # Must minimize length
    noise_sensitive: bool = False  # Avoid crossing other nets

    # Zone-related parameters
    zone_priority: int = 0  # Zone fill priority (higher = fills first)
    zone_connection: str = "thermal"  # Default connection type ("thermal", "solid", "none")
    is_pour_net: bool = False  # This net is used for copper pours (e.g., GND, VCC)

    # Routing-intent opt-out (Issue #2772)
    route_via: RouteVia = "pathfinder"
    """Declarative routing-intent selector for nets in this class.

    Lets designers opt OUT of the pathfinder declaratively rather than
    hand-rolling ``skip_nets`` in a custom ``design.py``:

    - ``"pathfinder"`` (default) -- standard pathfinder routing; preserves
      pre-#2772 behavior for every existing class.
    - ``"pour"`` -- ``_auto_skip_pour_nets`` skips this net when a zone for
      it exists in the PCB; otherwise falls through to the existing
      ``no_zone_nets`` warning path so the user is not left silently
      unconnected.  ``NET_CLASS_POWER`` flips to this value in #2772 to
      match the long-standing ``is_pour_net=True`` semantics.
    - ``"manual"`` -- always skip from the pathfinder; the designer is
      responsible for laying down the trace by hand (e.g. wide motor-phase
      traces routed in a custom script).  ``_auto_skip_pour_nets`` emits a
      distinct log line (``Manual: <names> ...``) so the user sees that the
      net was deliberately deferred to manual routing rather than dropped.

    Orthogonal to :attr:`is_pour_net`.  The legacy flag continues to drive
    zone-fill priority and zone-connection inference at the auto-pour layer
    (``router/auto_pour.py``); the new field drives the pathfinder skip
    predicate at the CLI layer (``cli/route_cmd.py``).  When both are set
    on a class, ``route_via`` takes precedence in the skip predicate -- a
    class with ``is_pour_net=True`` and an explicit ``route_via="pathfinder"``
    will NOT be auto-skipped, allowing a designer to override the legacy
    inference for a specific net class.

    Backward-compat: ``NET_CLASS_HIGH_CURRENT_SIGNAL`` deliberately stays
    at the ``"pathfinder"`` default; its phase outputs (PHASE_A/B/C) are
    point-to-point traces, not pours, and the per-net timeout pathology
    that motivated the original framing of this issue is fixed in sibling
    issues #2768 / #2769, not here.
    """

    # Layer preference parameters (Issue #625)
    preferred_layers: list[int] | None = None  # Layer indices to prefer (lower cost)
    avoid_layers: list[int] | None = None  # Layer indices to avoid (higher cost)
    layer_cost_multiplier: float = 2.0  # Cost penalty for non-preferred layers

    # Length constraint parameters (Issue #630)
    length_constraint: LengthConstraint | None = None  # Length constraint for this net class

    # Differential pair within-pair clearance (Issue #2557, Epic #2556 Phase 1A)
    intra_pair_clearance: float | None = None
    """Clearance applied to within-pair edges of a differential pair.

    When ``None`` (the default), the accessor :meth:`effective_intra_pair_clearance`
    falls back to :attr:`clearance`, preserving pre-#2557 single-clearance behavior.

    Callers (in Issue #2559 / Phase 1B) should read this via
    :meth:`effective_intra_pair_clearance` rather than touching the field
    directly, since the public ``None`` sentinel encodes "fall back to
    ``clearance``" rather than a literal zero clearance.

    Phase 1A scope (#2557) is the type-system foundation only; pathfinder /
    cpp_backend threading is explicitly out of scope and lands in #2559.
    """

    # Differential pair partner (Issue #2558, Epic #2556 Phase 1B)
    # When set, declares this net is the positive (or negative) half of a
    # differential pair whose partner is the named net.  This is the
    # AUTHORITATIVE source for diff-pair detection -- it overrides KiCad
    # group declarations and suffix inference.  A one-sided declaration
    # (only one of the two nets has ``diffpair_partner`` set) is sufficient
    # to form a pair.  Parallel addition to ``intra_pair_clearance`` from
    # Phase 1A (#2557).
    diffpair_partner: str | None = None

    # Differential pair coupled-routing engagement (Issue #2638, Epic #2556 Phase 2E)
    coupled_routing: bool = False
    """Opt-in flag for routing this net class's diff pairs via CoupledPathfinder.

    When ``False`` (default), differential pairs whose P or N net belongs to
    this class fall through to the main routing strategy even when
    ``--differential-pairs`` is enabled.  Phase 1's ``intra_pair_clearance``
    still applies to within-pair edges at the pathfinder layer, so a tight
    intra clearance can be honored without forcing coupled geometry.

    When ``True``, the diff-pair pre-pass / ``route_all_with_diffpairs``
    dispatch invokes :meth:`CoupledPathfinder` for pairs in this class,
    subject to the engagement-layer single-ended refusal in
    :func:`should_engage_coupled` (#2527 lesson -- pin pairs that look
    diff-pair-ish but are single-ended by spec, like USB-C CC1/CC2 and
    SBU1/SBU2, are refused at engagement time even when explicitly
    declared via :attr:`diffpair_partner`).

    Default is ``False`` for backward compatibility with all pre-#2636
    boards.  ``NET_CLASS_HIGH_SPEED`` was flipped to ``True`` in #2651
    (Epic #2556 Phase 2.5a) -- it is the canonical HSDI class that
    consumers opt into via ``high_speed_nets=`` and is the producer-side
    half of the Phase 2 coupled-routing pipeline.  Other predefined
    classes (``POWER``, ``HIGH_CURRENT_SIGNAL``, ``CLOCK``, ``AUDIO``,
    ``DIGITAL``, ``DEBUG``, ``DEFAULT``) keep ``coupled_routing=False``
    because they carry single-ended signals.

    NOTE -- name collision with the ``use_coupled_routing`` function
    parameter on :meth:`DiffPairRouter.route_differential_pair`.  The
    parameter is a runtime dispatch toggle ("call coupled vs independent
    for this single invocation"); this field is a class-level
    configuration flag ("nets in this class opt into the coupled
    engagement path").  Future refactors must not collapse the two.
    """

    # Target impedance for impedance-driven sizing (Issue #2650, Epic #2556 Phase 3K)
    target_diff_impedance: float | None = None
    """Target differential impedance in ohms (e.g. 90 for USB 2.0, 100 for USB
    3.0 / PCIe / MIPI).

    When set, the router consumes this field via
    :func:`kicad_tools.router.diffpair_impedance.apply_impedance_driven_sizing`
    to compute a ``(trace_width, intra_pair_clearance)`` pair from the PCB
    stackup using :class:`kicad_tools.physics.CoupledLines`.  When ``None``
    (the default), the per-class ``trace_width`` / ``intra_pair_clearance``
    literals are used unchanged, preserving pre-Phase-3K behavior.

    Independent of :attr:`target_single_impedance` -- a class may set one,
    both, or neither.  When both are set, diff-pair nets (those whose
    :attr:`diffpair_partner` is set OR whose name matches the suffix
    inference) consume :attr:`target_diff_impedance`; single-ended nets in
    the same class consume :attr:`target_single_impedance`.
    """

    target_single_impedance: float | None = None
    """Target single-ended (characteristic) impedance in ohms.

    Common values: 50 for clocks and most single-ended high-speed signals,
    75 for video / coaxial-style signals.  When set, the router computes
    the required ``trace_width`` from the stackup via
    :func:`kicad_tools.physics.TransmissionLine.width_for_impedance` and
    overrides :attr:`trace_width`.  When ``None`` (default), the per-class
    literal is used.
    """

    impedance_tolerance_percent: float = 10.0
    """Allowed deviation (in percent) from the target impedance that the
    DRC :class:`~kicad_tools.validate.rules.impedance.ImpedanceRule` fires
    on.

    Mirrors :attr:`kicad_tools.validate.rules.impedance.NetImpedanceSpec.tolerance_percent`
    (currently 10.0%).  Setting this leaves existing users at no-behavior-change
    because the rule's default tolerance is also 10.0%.
    """

    # Differential pair routing-continuity threshold (Issue #2640, Epic #2556 Phase 2G)
    coupled_continuity_threshold: float | None = None
    """Minimum coupled-fraction (0.0..1.0) required by the
    ``diffpair_routing_continuity`` DRC rule for engaged pairs in this class.

    The rule fires when a routed pair's coupled fraction (the share of P's
    routed length whose nearest point on N is within the coupling window
    AND parallel within +/-15 degrees) falls below this threshold.

    ``None`` (the default) means "use the rule's module-level default of
    0.7" -- empirically calibrated against board 03's USB pair, which
    couples ~60-80% in practice (curator note on #2640).  Setting
    ``0.9`` is appropriate for high-speed-differential-interface (HSDI)
    boards demanding tight coupling; setting ``0.5`` accommodates hobby
    boards with loose coupling expectations.

    Orthogonal to :attr:`diffpair_partner` and to the (Phase 2E)
    :attr:`coupled_routing` opt-in flag from #2638 -- this is a DRC-side
    knob that the autorouter consumer reads via
    :meth:`effective_coupled_continuity_threshold` and passes into
    :class:`~kicad_tools.validate.rules.diffpair_routing_continuity.DiffPairRoutingContinuityRule`
    via the ``threshold_map`` constructor argument.
    """

    # Differential pair length-match skew tolerance (Issue #2647, Epic #2556 Phase 3H)
    skew_tolerance_mm: float | None = None
    """Maximum allowed length skew (|L_p - L_n|, in mm) for differential pairs
    in this class.

    ``None`` (the default) means "use the rule's module-level default of
    0.5 mm" -- a conservative value covering USB 3.0 / PCIe Gen 2+ (~0.5-1 mm),
    MIPI D-PHY (~1 mm), and DDR4 DQ-strobe (~0.5 mm) headroom while still
    permitting the looser USB 2.0 HS budget (~3 mm) to be set explicitly.
    Setting ``0.3`` is appropriate for tight HSDI lanes; setting ``3.0``
    accommodates USB 2.0 full-/high-speed pairs.

    The :class:`~kicad_tools.router.diffpair_length.DiffPairLengthTracker`
    measures skew per pair unconditionally (no per-class gate); this field
    only controls the DRC rule's firing threshold (Phase 3J / Issue J).
    Phase 3I (serpentine insertion) consumes the accessor to choose which
    side to lengthen.

    Orthogonal to :attr:`length_critical`, which is a routing-priority hint
    rather than a skew gate (`length_critical=False` pairs still get
    measured).
    """

    # Match-group declaration (Issue #2687, Epic #2661 Phase 1A)
    length_match_group: str | None = None
    """Group name for nets in this class that must arrive length-matched.

    When set, declares this net class's nets are members of the named
    match group (e.g. ``"DDR_DATA"``, ``"MIPI_CSI_LANE0"``).  Multiple
    net classes may declare the same group name -- e.g. the lanes of a
    MIPI bus may live in different per-pair classes that all share a
    ``length_match_group="MIPI_CSI"``.

    AUTHORITATIVE.  Overrides suffix inference at Phase 1C (#2661).

    Cross-reference: a pre-existing field of the same name lives on a
    *different* dataclass at
    :attr:`kicad_tools.reasoning.vocabulary.RoutingPriority.length_match_group`
    (the reasoning-layer intermediate).  These are NOT in conflict -- they
    sit on different classes (router-layer vs reasoning-layer).  The
    reasoning-layer field, where used, maps INTO this router-layer field;
    it does not duplicate the semantic.  This router-layer field is the
    AUTHORITATIVE source for downstream routing/measurement consumers.

    Drift-prevention test asserts both fields share type annotation
    (``str | None``) and default (``None``).  See
    :class:`tests.test_match_group_declaration.TestDriftPrevention`.

    Phase 1A scope (#2687) is types only; accessor returns the field
    unchanged.  Phase 1B (#2688) consumes it via ``MatchGroupTracker``;
    Phase 1C (#2689) integrates with detection; Phase 1D (#2690) wires
    the producer side.
    """

    length_match_reference: str | None = None
    """Reference-selection policy for length-matching within the group.

    Semantics:

    - ``None`` (default) -- use the **longest trace in the group** as the
      reference (matches the legacy ``tune_match_group`` behavior at
      ``router/optimizer/serpentine.py:438``).
    - An **explicit net name** (e.g. ``"DQS_P"``) -- that net is the
      reference; every other group member is meandered to match its
      length.  Use this when one trace is hard to perturb (e.g. a DDR
      strobe whose timing budget can't absorb serpentines).
    - The **sentinel string** ``"clock"`` -- protocol-aware lookup,
      deferred to Phase 2/3 (MIPI/HDMI clock-relative case).  Phase 1A
      accepts the sentinel for forward-compat but does NOT implement
      protocol resolution.

    Phase 1A scope (#2687) is types only; accessor returns the field
    unchanged.  Phase 1B (#2688) consumes it via
    ``MatchGroupTracker.get_reference_length``.
    """

    length_match_tolerance_mm: float | None = None
    """Maximum allowed length skew (mm) across members of a match group.

    Mirrors the :attr:`skew_tolerance_mm` pattern (Issue #2647 / Phase 3H)
    but applies at the match-group level rather than the pair level.

    ``None`` (the default) means "use the rule's module-level default of
    0.5 mm" -- the same conservative default as :attr:`skew_tolerance_mm`.
    Setting ``0.1`` is appropriate for tight DDR data-byte groups; setting
    ``0.5`` accommodates MIPI lane-to-lane skew.

    Phase 1A (#2687) declares the field; Phase 1B (#2688) consumes it via
    :meth:`MatchGroupTracker.is_within_tolerance`; Phase 2G (#2661 issue G)
    consumes it via the future ``match_group_length_skew`` DRC rule.
    """

    def effective_intra_pair_clearance(self) -> float:
        """Return the clearance to apply to within-pair diff-pair edges.

        Backward-compatible accessor: returns :attr:`clearance` when
        :attr:`intra_pair_clearance` is unset (``None``), matching pre-#2557
        single-clearance behavior. Returns the explicit override otherwise.
        """
        if self.intra_pair_clearance is not None:
            return self.intra_pair_clearance
        return self.clearance

    def effective_coupled_continuity_threshold(self, default: float = 0.7) -> float:
        """Return the coupled-continuity threshold for the DRC rule.

        Backward-compatible accessor (Issue #2640 / Epic #2556 Phase 2G):
        returns ``default`` when :attr:`coupled_continuity_threshold` is
        unset (``None``).  ``default`` mirrors the rule's module-level
        ``DEFAULT_COUPLED_CONTINUITY_THRESHOLD`` so callers can override
        the floor consistently without re-importing it.

        Args:
            default: Fallback value when no per-class threshold is set.
                Defaults to ``0.7`` (the rule's empirically-calibrated
                default for the USB_D+/D- pair on board 03).

        Returns:
            The per-class threshold (in [0.0, 1.0]) if set, else ``default``.
        """
        if self.coupled_continuity_threshold is not None:
            return self.coupled_continuity_threshold
        return default

    def effective_skew_tolerance(self, default: float = 0.5) -> float:
        """Return the length-match skew tolerance for diff pairs in this class.

        Backward-compatible accessor (Issue #2647 / Epic #2556 Phase 3H):
        returns ``default`` when :attr:`skew_tolerance_mm` is unset
        (``None``).  ``default`` mirrors the (Phase 3J / Issue J) DRC rule's
        module-level ``DEFAULT_SKEW_TOLERANCE_MM`` so callers can override
        the floor consistently without re-importing it.

        Args:
            default: Fallback value (in mm) when no per-class skew
                tolerance is set.  Defaults to ``0.5`` -- a conservative
                value that covers USB 3.0 / PCIe Gen 2+ (~0.5-1 mm),
                MIPI D-PHY (~1 mm), and DDR4 DQ-strobe (~0.5 mm) while
                still permitting the looser USB 2.0 HS budget (~3 mm) to
                be set explicitly per class.

        Returns:
            The per-class skew tolerance in mm if set, else ``default``.
        """
        if self.skew_tolerance_mm is not None:
            return self.skew_tolerance_mm
        return default

    def effective_length_match_group(self) -> str | None:
        """Return the match-group name for this net class.

        Backward-compatible accessor (Issue #2687 / Epic #2661 Phase 1A):
        returns :attr:`length_match_group` directly.  Provided for API
        uniformity with :meth:`effective_intra_pair_clearance` /
        :meth:`effective_skew_tolerance`; no fallback chain is needed
        because ``None`` already means "no group".
        """
        return self.length_match_group

    def effective_length_match_reference(self) -> str | None:
        """Return the reference-selection policy for length-matching.

        Backward-compatible accessor (Issue #2687 / Epic #2661 Phase 1A):
        returns :attr:`length_match_reference` directly.  ``None`` means
        "use longest in group"; an explicit net name means "use this net
        as the reference"; the sentinel ``"clock"`` is reserved for
        Phase 2/3 protocol-aware resolution.
        """
        return self.length_match_reference

    def effective_length_match_tolerance(self, default: float = 0.5) -> float:
        """Return the match-group length-skew tolerance (mm).

        Backward-compatible accessor (Issue #2687 / Epic #2661 Phase 1A):
        returns ``default`` when :attr:`length_match_tolerance_mm` is
        unset (``None``).  Mirrors the :meth:`effective_skew_tolerance`
        pattern so the future ``match_group_length_skew`` DRC rule
        (Phase 2G) can override the floor consistently.

        Args:
            default: Fallback value (in mm) when no per-class tolerance
                is set.  Defaults to ``0.5`` to mirror the diff-pair
                :meth:`effective_skew_tolerance` default; the future
                Phase 2G rule's ``DEFAULT_MATCH_GROUP_TOLERANCE_MM``
                constant should equal this byte-for-byte.

        Returns:
            The per-class tolerance in mm if set, else ``default``.
        """
        if self.length_match_tolerance_mm is not None:
            return self.length_match_tolerance_mm
        return default

    def to_dict(self) -> dict:
        """Serialize this :class:`NetClassRouting` to a JSON-compatible dict.

        Issue #2684 / Epic #2556 Phase 2.5c-cli.  The round-trip wire
        format for the ``kct check --net-class-map <path>`` sidecar.  All
        fields the diff-pair validate rules consume are preserved:

        - ``coupled_routing`` (Phase 2E / #2638 -- gates engagement)
        - ``coupled_continuity_threshold`` (Phase 2G / #2640)
        - ``skew_tolerance_mm`` (Phase 3H / #2647)
        - ``diffpair_partner`` (Phase 1B / #2558)
        - ``target_diff_impedance`` (Phase 3K / #2650)
        - ``target_single_impedance`` (Phase 3K / #2650)
        - ``intra_pair_clearance`` (Phase 1A / #2557)
        - ``length_match_group`` / ``length_match_reference`` /
          ``length_match_tolerance_mm`` (Phase 1A / #2687, Epic #2661)

        Nested ``LengthConstraint`` is serialized via its own
        :meth:`LengthConstraint.to_dict` (``None`` is preserved as ``None``).

        Round-trip property (Issue #2684 AC: byte-equivalent round-trip):
        for any ``nc: NetClassRouting``,
        ``NetClassRouting.from_dict(nc.to_dict()) == nc`` holds.

        Drift-prevention: the
        :class:`tests.test_net_class_serialization.TestDriftPrevention`
        suite asserts ``{f.name for f in fields(NetClassRouting)}`` equals
        the literal key set returned here, so any future field addition
        to the dataclass is forced to update this method (and
        :meth:`from_dict`) in the same commit.
        """
        return {
            "name": self.name,
            "priority": self.priority,
            "trace_width": self.trace_width,
            "clearance": self.clearance,
            "via_size": self.via_size,
            "cost_multiplier": self.cost_multiplier,
            "length_critical": self.length_critical,
            "noise_sensitive": self.noise_sensitive,
            "zone_priority": self.zone_priority,
            "zone_connection": self.zone_connection,
            "is_pour_net": self.is_pour_net,
            "route_via": self.route_via,
            "preferred_layers": (
                list(self.preferred_layers) if self.preferred_layers is not None else None
            ),
            "avoid_layers": (list(self.avoid_layers) if self.avoid_layers is not None else None),
            "layer_cost_multiplier": self.layer_cost_multiplier,
            "length_constraint": (
                self.length_constraint.to_dict() if self.length_constraint is not None else None
            ),
            "intra_pair_clearance": self.intra_pair_clearance,
            "diffpair_partner": self.diffpair_partner,
            "coupled_routing": self.coupled_routing,
            "target_diff_impedance": self.target_diff_impedance,
            "target_single_impedance": self.target_single_impedance,
            "impedance_tolerance_percent": self.impedance_tolerance_percent,
            "coupled_continuity_threshold": self.coupled_continuity_threshold,
            "skew_tolerance_mm": self.skew_tolerance_mm,
            "length_match_group": self.length_match_group,
            "length_match_reference": self.length_match_reference,
            "length_match_tolerance_mm": self.length_match_tolerance_mm,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "NetClassRouting":
        """Deserialize from a dict produced by :meth:`to_dict`.

        Tolerates missing optional keys (falls back to dataclass defaults)
        and ignores unknown keys (forward compatibility).  The only
        required field is ``name``.

        Round-trip property: see :meth:`to_dict`.
        """
        if "name" not in data:
            raise ValueError(
                "NetClassRouting.from_dict requires a 'name' field; got keys: "
                f"{sorted(data.keys())}"
            )
        length_constraint_data = data.get("length_constraint")
        length_constraint = (
            LengthConstraint.from_dict(length_constraint_data)
            if length_constraint_data is not None
            else None
        )
        return cls(
            name=data["name"],
            priority=data.get("priority", 5),
            trace_width=data.get("trace_width", 0.2),
            clearance=data.get("clearance", 0.2),
            via_size=data.get("via_size", 0.6),
            cost_multiplier=data.get("cost_multiplier", 1.0),
            length_critical=data.get("length_critical", False),
            noise_sensitive=data.get("noise_sensitive", False),
            zone_priority=data.get("zone_priority", 0),
            zone_connection=data.get("zone_connection", "thermal"),
            is_pour_net=data.get("is_pour_net", False),
            route_via=data.get("route_via", "pathfinder"),
            preferred_layers=data.get("preferred_layers"),
            avoid_layers=data.get("avoid_layers"),
            layer_cost_multiplier=data.get("layer_cost_multiplier", 2.0),
            length_constraint=length_constraint,
            intra_pair_clearance=data.get("intra_pair_clearance"),
            diffpair_partner=data.get("diffpair_partner"),
            coupled_routing=data.get("coupled_routing", False),
            target_diff_impedance=data.get("target_diff_impedance"),
            target_single_impedance=data.get("target_single_impedance"),
            impedance_tolerance_percent=data.get("impedance_tolerance_percent", 10.0),
            coupled_continuity_threshold=data.get("coupled_continuity_threshold"),
            skew_tolerance_mm=data.get("skew_tolerance_mm"),
            length_match_group=data.get("length_match_group"),
            length_match_reference=data.get("length_match_reference"),
            length_match_tolerance_mm=data.get("length_match_tolerance_mm"),
        )


# =============================================================================
# PREDEFINED NET CLASSES
# =============================================================================

NET_CLASS_POWER = NetClassRouting(
    name="Power",
    priority=1,
    trace_width=0.5,
    clearance=0.2,
    via_size=0.8,
    cost_multiplier=0.8,
    zone_priority=10,  # Fill power zones first
    zone_connection="solid",  # Direct connection for power
    is_pour_net=True,  # Power nets often have pours
    # Issue #2772: declarative routing-intent matches the long-standing
    # ``is_pour_net=True`` semantics -- power nets should be satisfied by a
    # copper zone (or fall through to the no-zone warning path) rather than
    # consumed by the pathfinder.
    route_via="pour",
)

# High-current signal nets such as motor phase outputs (PHASE_A/B/C),
# coil drives, and stepper/solenoid returns.  These need POWER-tier
# routing priority so they get first pick of routing corridors before
# ordinary signals consume them, but they must NOT be poured: a phase
# output is point-to-point from the half-bridge FETs to the load and
# pouring it as a copper plane couples switching noise into nearby
# traces and breaks the per-trace current path.
#
# Trace width is wider than digital signals (default 0.4mm) to handle
# motor currents but narrower than full POWER (0.5mm) since these are
# typically routed individually per-phase rather than as bus rails.
NET_CLASS_HIGH_CURRENT_SIGNAL = NetClassRouting(
    name="HighCurrentSignal",
    priority=1,  # Same tier as POWER so motor phases route early
    trace_width=0.4,
    clearance=0.2,
    via_size=0.8,
    cost_multiplier=0.85,  # Prefer over normal signals, slightly less than power
    is_pour_net=False,  # Critical: phase outputs must NOT be poured
)

NET_CLASS_CLOCK = NetClassRouting(
    name="Clock",
    priority=2,
    trace_width=0.2,
    clearance=0.2,
    cost_multiplier=0.9,
    length_critical=True,
)

NET_CLASS_HIGH_SPEED = NetClassRouting(
    name="HighSpeed",
    priority=2,
    trace_width=0.2,
    clearance=0.15,
    intra_pair_clearance=0.075,  # Issue #2559 / Epic #2556 Phase 1C
    cost_multiplier=0.85,
    length_critical=True,
    coupled_routing=True,  # Issue #2651 / Epic #2556 Phase 2.5a: producer-side flip
)

NET_CLASS_AUDIO = NetClassRouting(
    name="Audio",
    priority=3,
    trace_width=0.2,
    clearance=0.15,
    cost_multiplier=1.0,
    noise_sensitive=True,
)

NET_CLASS_DIGITAL = NetClassRouting(
    name="Digital",
    priority=4,
    trace_width=0.2,
    clearance=0.15,
    cost_multiplier=1.0,
)

NET_CLASS_DEBUG = NetClassRouting(
    name="Debug",
    priority=5,
    trace_width=0.2,
    clearance=0.15,
    cost_multiplier=1.2,  # Route last, less important
)

NET_CLASS_DEFAULT = NetClassRouting(
    name="Default",
    priority=10,
    trace_width=0.2,
    clearance=0.2,
    cost_multiplier=1.0,
)


def create_net_class_map(
    power_nets: list[str] | None = None,
    clock_nets: list[str] | None = None,
    high_speed_nets: list[str] | None = None,
    audio_nets: list[str] | None = None,
    debug_nets: list[str] | None = None,
) -> dict[str, NetClassRouting]:
    """Create a net class mapping from net name lists.

    Args:
        power_nets: List of power net names (e.g., ["+5V", "+3.3V", "GND"])
        clock_nets: List of clock net names (e.g., ["MCLK", "BCLK"])
        high_speed_nets: List of high-speed signal nets (e.g., ["SPI_CLK"])
        audio_nets: List of audio signal nets (e.g., ["AUDIO_L", "AUDIO_R"])
        debug_nets: List of debug/low-priority nets (e.g., ["SWDIO", "NRST"])

    Returns:
        Dict mapping net names to NetClassRouting objects
    """
    net_class_map: dict[str, NetClassRouting] = {}

    if power_nets:
        for net in power_nets:
            net_class_map[net] = NET_CLASS_POWER

    if clock_nets:
        for net in clock_nets:
            net_class_map[net] = NET_CLASS_CLOCK

    if high_speed_nets:
        for net in high_speed_nets:
            net_class_map[net] = NET_CLASS_HIGH_SPEED

    if audio_nets:
        for net in audio_nets:
            net_class_map[net] = NET_CLASS_AUDIO

    if debug_nets:
        for net in debug_nets:
            net_class_map[net] = NET_CLASS_DEBUG

    return net_class_map


def net_class_map_to_dict(
    net_class_map: dict[str, NetClassRouting],
) -> dict[str, dict]:
    """Serialize a ``{net_name: NetClassRouting}`` map to a JSON-compatible dict.

    Issue #2684.  The wire format for the ``kct check --net-class-map``
    sidecar JSON.  Each entry is serialized via :meth:`NetClassRouting.to_dict`.
    """
    return {net: nc.to_dict() for net, nc in net_class_map.items()}


def net_class_map_from_dict(
    data: dict[str, dict],
) -> dict[str, NetClassRouting]:
    """Deserialize a ``{net_name: NetClassRouting}`` map from a JSON-shaped dict.

    Round-trip companion to :func:`net_class_map_to_dict`.  Each entry
    is deserialized via :meth:`NetClassRouting.from_dict`.

    Args:
        data: Mapping of ``net_name -> NetClassRouting-dict``.  Must be a
            dict-of-dicts.

    Raises:
        ValueError: If any entry is malformed (missing 'name' field).
        TypeError: If ``data`` is not a dict-of-dicts.
    """
    if not isinstance(data, dict):
        raise TypeError(f"net_class_map_from_dict expects a dict, got {type(data).__name__}")
    result: dict[str, NetClassRouting] = {}
    for net_name, entry in data.items():
        if not isinstance(entry, dict):
            raise TypeError(
                f"net_class_map entry for {net_name!r} must be a dict, got {type(entry).__name__}"
            )
        result[net_name] = NetClassRouting.from_dict(entry)
    return result


# Threshold for classifying a 2-pin signal net as "simple" (short) vs "complex" (long).
# Nets with a bounding-box diagonal below this value (in mm) are considered simple and
# are routed before longer/multi-pin nets within the same priority class.  This gives
# short connections first access to routing channels.
SIMPLE_NET_THRESHOLD_MM: float = 10.0

# Default net class map with common net names
DEFAULT_NET_CLASS_MAP: dict[str, NetClassRouting] = create_net_class_map(
    power_nets=["+5V", "+3.3V", "+3.3VA", "+1.8V", "VCC", "VDD", "GND", "GNDA", "PGND"],
    clock_nets=["CLK", "MCLK", "BCLK", "LRCLK", "SCK"],
    audio_nets=["AUDIO_L", "AUDIO_R", "I2S_DIN", "I2S_DOUT"],
    debug_nets=["SWDIO", "SWCLK", "NRST", "TDI", "TDO", "TCK", "TMS"],
)
