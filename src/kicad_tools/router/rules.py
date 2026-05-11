"""
Design rules and net class routing parameters.

This module provides:
- DesignRules: Trace width, clearance, via parameters, and A* costs
- NetClassRouting: Per-net-class routing preferences
- Predefined net classes for common use cases
"""

from dataclasses import dataclass, field

from .layers import Layer


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

    def get_clearance_for_component(self, ref: str, pin_pitch: float | None = None) -> float:
        """Get the clearance to use for a specific component.

        Checks for per-component clearance overrides, then for automatic
        fine-pitch clearance based on pin pitch, then falls back to the
        default trace_clearance.

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
        # Check explicit per-component override first
        if ref in self.component_clearances:
            return self.component_clearances[ref]

        # Check for automatic fine-pitch clearance
        if (
            self.fine_pitch_clearance is not None
            and pin_pitch is not None
            and pin_pitch < self.fine_pitch_threshold
        ):
            return self.fine_pitch_clearance

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
