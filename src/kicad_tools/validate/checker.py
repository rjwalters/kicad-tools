"""Pure Python DRC checker.

This module provides the main DRCChecker class that performs Design Rule
Checks on PCB designs without requiring kicad-cli.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools.manufacturers import DesignRules, get_profile

from .rules.clearance import ClearanceRule, SegmentZoneClearanceRule
from .rules.connectivity import ConnectivityRule
from .rules.diffpair_clearance_intra import DiffPairClearanceIntraRule
from .rules.diffpair_length_skew import DiffPairLengthSkewRule
from .rules.diffpair_routing_continuity import DiffPairRoutingContinuityRule
from .rules.edge import EdgeClearanceRule
from .rules.impedance import ImpedanceRule
from .rules.match_group_length_skew import MatchGroupLengthSkewRule
from .rules.placement import FootprintOutsideBoardRule
from .rules.silkscreen import check_all_silkscreen
from .rules.via_in_pad import ViaInPadRule
from .rules.zone_fill import ZoneFillRule
from .violations import DRCResults, DRCViolation

if TYPE_CHECKING:
    from kicad_tools.router.rules import NetClassRouting
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.filters import ViolationFilter


class DRCChecker:
    """Pure Python DRC checker for PCB validation.

    Validates PCB designs against manufacturer design rules without
    requiring kicad-cli to be installed.

    Example:
        >>> from kicad_tools.schema.pcb import PCB
        >>> from kicad_tools.validate import DRCChecker
        >>>
        >>> pcb = PCB.load("board.kicad_pcb")
        >>> checker = DRCChecker(pcb, manufacturer="jlcpcb", layers=4)
        >>> results = checker.check_all()
        >>>
        >>> if results.passed:
        ...     print("DRC passed!")
        >>> else:
        ...     for violation in results.errors:
        ...         print(f"{violation.severity}: {violation.message}")

    Attributes:
        pcb: The PCB being checked
        design_rules: Design rules from the manufacturer profile
        manufacturer: Manufacturer ID string
        layers: Number of PCB layers
    """

    def __init__(
        self,
        pcb: PCB,
        manufacturer: str = "jlcpcb",
        layers: int = 4,
        copper_oz: float = 1.0,
        suppress_library: bool = False,
        net_class_map: dict[str, NetClassRouting] | None = None,
    ) -> None:
        """Initialize the DRC checker.

        Args:
            pcb: The PCB to check
            manufacturer: Manufacturer ID (e.g., "jlcpcb", "oshpark")
            layers: Number of PCB layers (2, 4, 6, etc.)
            copper_oz: Copper weight in oz
            suppress_library: If True, suppress silkscreen warnings for
                footprints originating from standard KiCad libraries
            net_class_map: Optional ``{net_name: NetClassRouting}`` map
                (the autorouter convention).  When provided, the
                differential-pair routing continuity rule (Phase 2.5b /
                Issue #2652) re-derives the engagement state from the
                PCB + map and runs the rule with the resulting
                ``engaged_pairs`` set + per-pair threshold map.  When
                omitted, the rule degrades to a no-op (graceful
                standalone-``kct check`` behaviour).

        Raises:
            ValueError: If manufacturer ID is not recognized
        """
        self.pcb = pcb
        self.manufacturer = manufacturer
        self.layers = layers
        self.copper_oz = copper_oz
        self.suppress_library = suppress_library
        self.net_class_map = net_class_map

        # Load manufacturer profile and design rules
        profile = get_profile(manufacturer)
        self.design_rules: DesignRules = profile.get_design_rules(layers, copper_oz)

    # The canonical ordered list of bound-method names that
    # :meth:`check_all` invokes.  Exposed as a class attribute so the CLI
    # dispatcher (``cli/check_cmd.py::run_selected_checks``) can assert
    # at test time that its category dict is a superset of every check
    # ``check_all`` runs (regression test for Issue #3046 -- the CLI used
    # to silently omit ``check_via_in_pad`` and ``check_all`` used to
    # omit ``check_pad_grid_alignment``).
    #
    # Adding a new ``check_X`` method to this class therefore requires
    # updating exactly two places: this tuple AND the ``check_methods``
    # dict in ``cli/check_cmd.py``.  The regression test in
    # ``tests/test_check_cmd_coverage.py`` enforces the second half.
    CHECK_ALL_METHODS: tuple[str, ...] = (
        "check_clearances",
        "check_connectivity",
        "check_segment_zone_clearances",
        "check_diffpair_clearance_intra",
        "check_diffpair_length_skew",
        "check_diffpair_routing_continuity",
        "check_dimensions",
        "check_edge_clearances",
        "check_impedance",
        "check_match_group_length_skew",
        "check_silkscreen",
        "check_solder_mask_pads",
        "check_footprint_placement",
        "check_netlist",
        "check_single_pad_nets",
        "check_pad_grid_alignment",
        "check_via_in_pad",
        "check_zones",
    )

    # Per-rule severity classification (Issue #3044).
    #
    # Rules listed here are *advisory* -- they run in every entry point
    # and their violations appear in JSON / table output, but downstream
    # gating consumers (``ManufacturingAudit._check_drc``, ``kct export``
    # preflight) MUST NOT treat them as blocking, because a sibling
    # status field already classifies the same defect with finer-grained
    # logic (e.g., ``ConnectivityStatus`` distinguishes zone-bridged
    # incomplete nets from genuinely-unrouted nets).  All other rules
    # default to ``blocking`` severity.
    #
    # This replaces the per-call-site hardcoded ``rule_id == "X"`` filter
    # that PR #3060 used as a one-off workaround in ``auditor.py:761``.
    # When adding a new rule whose severity differs from "blocking",
    # update this set in ONE place; ``ManufacturingAudit._check_drc``
    # (and any future advisory-aware consumers) will pick it up via the
    # :meth:`is_advisory_rule` classifier.
    #
    # The standalone ``kct check`` CLI does NOT consult this set --
    # advisory rules still surface their errors to CI consumers; only
    # gating verdicts (audit / export readiness) filter on it.
    ADVISORY_RULE_IDS: frozenset[str] = frozenset({"connectivity"})

    @classmethod
    def is_advisory_rule(cls, rule_id: str) -> bool:
        """Return True iff ``rule_id`` is classified as advisory.

        Advisory rules are reported by every DRC entry point but do not
        count toward manufacturability-blocking verdicts.  See
        :attr:`ADVISORY_RULE_IDS` for the rationale and the current set.
        """
        return rule_id in cls.ADVISORY_RULE_IDS

    def check_all(
        self,
        filters: list[ViolationFilter] | None = None,
        pad_grid_auto_derive: bool = False,
    ) -> DRCResults:
        """Run all DRC checks.

        Args:
            filters: Optional list of :class:`ViolationFilter` rules.  When
                provided, matching violations are suppressed or reclassified
                and the ``suppressed_count`` field is populated.
            pad_grid_auto_derive: When ``True``, the ``pad_grid`` check
                derives its tolerance from the board's pad-offset
                histogram instead of the fixed 0.05 mm default -- the
                same policy ``kct check`` applies by default (issue
                #3061).  Defaults to ``False`` to preserve the existing
                Python-API behaviour; gating consumers that must agree
                with ``kct check`` (e.g. ``ManufacturingAudit``) opt in
                so report and CLI verdicts cannot drift (issue #3497).

        Returns:
            DRCResults containing all violations found (after filtering,
            if filters are provided).
        """
        results = DRCResults()

        # Run each category of checks (order matches CHECK_ALL_METHODS).
        for method_name in self.CHECK_ALL_METHODS:
            method = getattr(self, method_name)
            if method_name == "check_pad_grid_alignment":
                results.merge(method(auto_derive_threshold=pad_grid_auto_derive))
            else:
                results.merge(method())

        # Apply filters if provided
        if filters:
            from kicad_tools.validate.filters import FilterEngine

            engine = FilterEngine(filters)
            filter_result = engine.apply(results.violations)
            results.suppressed_count += filter_result.ignored_count
            results.violations = filter_result.kept

        return results

    def check_clearances(self) -> DRCResults:
        """Check clearance rules (trace-to-trace, trace-to-pad, etc.).

        Validates spacing between copper elements on the same layer
        but different nets against the manufacturer's minimum clearance.

        Returns:
            DRCResults containing clearance violations
        """
        rule = ClearanceRule()
        return rule.check(self.pcb, self.design_rules)

    def check_segment_zone_clearances(self) -> DRCResults:
        """Check track segments against foreign-net zone fill copper.

        Validates each segment against the committed ``filled_polygon``
        geometry of zones on a *different* net and the same layer,
        flagging both copper overlap (a hard short) and sub-clearance
        gaps.  Closes the Issue #3527 gap: a trace routed straight
        through a stale foreign fill was invisible to every other rule
        (segment-vs-segment / segment-vs-pad / segment-vs-via spacing
        never consult zone fills), so ``kct check`` certified boards
        whose committed copper shorts two nets together (found by PR
        #3526's judge on board 05: PWR_LED through +3V3 fill).

        Returns:
            DRCResults containing ``clearance_segment_zone`` violations
            (severity error, blocking).
        """
        rule = SegmentZoneClearanceRule()
        return rule.check(self.pcb, self.design_rules)

    def check_connectivity(self) -> DRCResults:
        """Check that every multi-pad net is fully connected.

        Loads the netlist via
        :class:`~kicad_tools.analysis.net_status.NetStatusAnalyzer` and
        emits one error per net whose pads are not all connected through
        traces, vias, or filled copper zones.  Single-pad nets are out
        of scope (handled by :meth:`check_single_pad_nets`).

        See Issue #3041: previously ``kct check`` reported ``DRC PASS``
        on PCBs with unrouted nets because no rule cross-referenced the
        netlist against actual copper connectivity.  This rule closes
        that gap so partial-route boards fail DRC with a clear, per-net
        diagnostic.

        Returns:
            DRCResults containing one error per incomplete or unrouted
            multi-pad net (severity error).
        """
        rule = ConnectivityRule()
        return rule.check(self.pcb, self.design_rules)

    def check_diffpair_clearance_intra(self) -> DRCResults:
        """Check within-pair clearance for differential pairs.

        Validates that segments belonging to a detected differential pair
        maintain at least the per-class ``intra_pair_clearance`` edge-to-edge
        spacing.  Within-pair edges are *allowed* to be tighter than the
        inter-pair manufacturer minimum (that's the whole point of diff-pair
        coupling), but they must still respect the intra threshold.

        Diff-pair detection uses the suffix-inference matcher in
        ``router/diffpair`` (USB_D+/USB_D-, HDMI_D0_P/HDMI_D0_N, etc.).
        Single-ended refusal patterns (``USB_CC1``/``USB_CC2``,
        ``SBU1``/``SBU2``) are correctly excluded per #2558.

        See Issue #2560 / Epic #2556 Phase 1D.  When the upstream router
        gains the per-pair clearance map (#2559), this method may be
        extended to accept it via constructor injection on
        :class:`DRCChecker`; today the rule falls back to the manufacturer's
        ``min_clearance_mm``, which makes it a no-op duplicate of the
        generic clearance rule for any pair without an explicit threshold.

        Returns:
            DRCResults containing intra-pair clearance violations.
        """

        rule = DiffPairClearanceIntraRule()
        return rule.check(self.pcb, self.design_rules)

    def check_diffpair_length_skew(self) -> DRCResults:
        """Check routed-length skew for engaged differential pairs.

        Validates that engaged pairs (per Epic #2556 Phase 2E, #2638)
        have a length skew ``|L_p - L_n|`` within their per-class
        ``skew_tolerance_mm`` (default 0.5 mm).  An "engaged" pair is
        one whose net class has ``coupled_routing == True`` AND which
        passed the engagement-layer single-ended refusal check.

        Phase 2.5c (Issue #2675) wires the producer side: when this
        checker was constructed with a ``net_class_map``, the per-pair
        skew is re-derived from the routed PCB by
        :func:`~kicad_tools.validate.diffpair_skew.derive_skew_data`
        (sister of
        :func:`~kicad_tools.validate.diffpair_engagement.derive_engagement_state`)
        and threaded into the rule along with the engagement state.
        Re-running detection + length-from-PCB-segments on the routed
        PCB is idempotent given the same net classes and physical
        routing -- this avoids needing to persist length / skew
        metadata in the PCB schema.

        When invoked from the standalone ``kct check`` CLI (no
        ``net_class_map``), no skew data is available, so the rule is
        a conservative no-op (preserves the AC #4 graceful-degradation
        contract -- mirrors the
        :meth:`check_diffpair_routing_continuity` behaviour).

        Phase 3J is **independent of Phase 3I** (serpentine insertion):
        the rule fires on routed-as-found geometry regardless of
        whether the tuner ran.  This is the
        "validator-for-externally-routed-boards" use case (Freerouting,
        KiCad's own router, manual layout) where the kicad-tools tuner
        never runs but the board still needs its skew validated.

        See Issue #2649 / Epic #2556 Phase 3J (the rule itself); Issue
        #2675 / Epic #2556 Phase 2.5c (this producer wiring).

        Returns:
            :class:`DRCResults` containing length-skew violations.
            Empty on standalone ``kct check`` invocations (no router
            context to supply ``skew_data``).
        """
        from kicad_tools.validate.diffpair_engagement import derive_engagement_state
        from kicad_tools.validate.diffpair_skew import derive_skew_data

        skew_data, skew_threshold_map = derive_skew_data(self.pcb, self.net_class_map)
        engaged_pairs, _ = derive_engagement_state(self.pcb, self.net_class_map)
        rule = DiffPairLengthSkewRule(
            skew_data=skew_data,
            engaged_pairs=engaged_pairs,
            threshold_map=skew_threshold_map,
        )
        return rule.check(self.pcb, self.design_rules)

    def check_diffpair_routing_continuity(self) -> DRCResults:
        """Check routing continuity for engaged differential pairs.

        Validates that engaged pairs (per Epic #2556 Phase 2E, #2638)
        stay coupled (parallel and within the coupling window) for the
        per-class ``coupled_continuity_threshold`` fraction of their
        routed length.  An "engaged" pair is one whose net class has
        ``coupled_routing == True`` AND which passed the engagement-layer
        single-ended refusal check.

        Phase 2.5b (Issue #2652) wires the producer side: when this
        checker was constructed with a ``net_class_map``, the engagement
        state is re-derived from the routed PCB by
        :func:`~kicad_tools.validate.diffpair_engagement.derive_engagement_state`
        and threaded into the rule.  Re-running
        :func:`should_engage_coupled` on the routed PCB's detected pairs
        is idempotent given the same net classes -- this avoids needing
        to persist engagement metadata in the PCB schema.

        When invoked from the standalone ``kct check`` CLI (no
        ``net_class_map``), no engaged-pairs set is available, so the
        rule is a conservative no-op (preserves the AC #4 graceful-
        degradation contract).

        See Issue #2640 / Epic #2556 Phase 2G (the rule itself); Issue
        #2652 / Epic #2556 Phase 2.5b (this producer wiring).

        Returns:
            DRCResults containing routing-continuity violations.  Empty
            on standalone ``kct check`` invocations (no engaged set).
        """
        from kicad_tools.validate.diffpair_engagement import derive_engagement_state

        engaged_pairs, threshold_map = derive_engagement_state(self.pcb, self.net_class_map)
        rule = DiffPairRoutingContinuityRule(
            engaged_pairs=engaged_pairs,
            threshold_map=threshold_map,
        )
        return rule.check(self.pcb, self.design_rules)

    def check_dimensions(self) -> DRCResults:
        """Check dimension rules (trace width, via drill, annular ring).

        Validates:
        - Minimum trace width
        - Minimum via drill diameter
        - Minimum via outer diameter
        - Minimum annular ring
        - Drill-to-drill clearance

        Returns:
            DRCResults containing dimension violations
        """
        from .rules.dimensions import DimensionRules

        rule = DimensionRules()
        return rule.check(self.pcb, self.design_rules)

    def check_edge_clearances(self) -> DRCResults:
        """Check edge clearance rules (copper-to-board-edge).

        Validates that all copper elements (traces, pads, zones) and holes
        (vias, through-hole pads) maintain minimum clearance from the board
        edge as specified by manufacturer design rules.

        Returns:
            DRCResults containing edge clearance violations
        """
        rule = EdgeClearanceRule()
        return rule.check(self.pcb, self.design_rules)

    def check_impedance(self) -> DRCResults:
        """Check trace widths against target impedance specifications.

        Wires the dormant :class:`ImpedanceRule` (already implemented in
        ``validate/rules/impedance.py``) into the standalone DRC pipeline
        per Issue #2650 (Epic #2556 Phase 3K).  The rule was previously
        registered in :class:`~kicad_tools.drc.violation.ViolationType`
        (``IMPEDANCE = "impedance"``) and exported from
        ``validate.rules.__init__`` but was never reachable from
        ``kct check`` because no ``DRCChecker`` method invoked it.

        Single-ended impedance is **declarative / opt-in** (Issue #3157).
        When invoked from the standalone CLI without a ``net_class_map``,
        the rule's built-in single-ended *name-pattern heuristics*
        (``.*CLK$`` / ``.*MCLK$`` / ``.*ETH.*`` -> 50Ω) are suppressed:
        slow I2S / DAC clock nets on low-speed audio boards (``DAC_CLK``,
        ``BCLK``, ``MCLK``, ``I2S_LRCLK``) match those patterns but need
        no controlled impedance, and on a 4-layer board with an explicit
        stackup the heuristics produced 32 false-positive impedance
        errors on chorus-test.

        When the checker is constructed with a ``net_class_map`` (the
        ``kct check --net-class-map`` sidecar, Issue #2684), a single-ended
        :class:`~kicad_tools.validate.rules.impedance.NetImpedanceSpec` is
        derived from each net class's
        :attr:`~kicad_tools.router.rules.NetClassRouting.target_single_impedance`
        and passed as an **explicit** ``specs=`` list.  Explicit specs
        bypass the heuristic-suppression gate and always evaluate, so a
        net declared 50Ω single-ended still gets checked and fires when
        its width is wrong.  This mirrors the producer-side wiring already
        used by :meth:`check_match_group_length_skew` (Issue #2710).

        Diff-pair impedance (``target_diff_impedance`` / ``detected_pairs``,
        board 06) is **unaffected** -- this method only governs the
        single-ended path.

        Returns:
            DRCResults containing impedance violations.  Empty when no
            traces match any spec (the standalone-CLI common case for
            boards without declared controlled-impedance nets).
        """
        if self.net_class_map is None:
            # No router context -> no declared single-ended impedance.
            # The rule's SE name-pattern heuristics stay suppressed
            # (Issue #3157); diff-pair defaults still resolve internally.
            rule = ImpedanceRule()
        else:
            from kicad_tools.validate.impedance_specs import (
                derive_single_ended_impedance_specs,
            )

            specs = derive_single_ended_impedance_specs(self.net_class_map)
            # Pass explicit ``specs=`` so they always evaluate (explicit
            # specs set ``_using_default_specs=False`` and bypass the
            # heuristic-suppression gate).  When no class declared a
            # single-ended impedance, ``specs`` is empty -> conservative
            # no-op (no single-ended impedance errors).
            rule = ImpedanceRule(specs=specs)
        return rule.check(self.pcb, self.design_rules)

    def check_match_group_length_skew(self) -> DRCResults:
        """Check routed-length skew for declared N-trace match groups.

        Validates that declared / detected
        :class:`~kicad_tools.router.match_group_length.MatchGroup`
        instances have a per-group length skew (``max(L) - min(L)``
        across the group's members) within their per-class
        :attr:`~kicad_tools.router.rules.NetClassRouting.length_match_tolerance_mm`
        (default 0.5 mm).  N>=3 generalization of
        :meth:`check_diffpair_length_skew` for bus-style groups (DDR
        DQ-strobe, MIPI CSI lanes, TMDS).

        **Independent of Phase 2E** (the N-trace tuner, Issue #2700):
        the rule fires on routed-as-found geometry regardless of
        whether the v2 tuner ran.  This is the explicit "validator-
        for-externally-routed-boards" use case (Freerouting / KiCad's
        own router / manual layout) where ``kicad_tools.router`` never
        runs but the board still needs its match-group skew validated.

        Phase 2.5G (Issue #2710) wires the producer side: when this
        checker was constructed with a ``net_class_map``, the per-group
        skew is re-derived from the routed PCB by
        :func:`~kicad_tools.validate.match_group_skew.derive_group_skew_data`
        (sister of
        :func:`~kicad_tools.validate.diffpair_skew.derive_skew_data`)
        and threaded into the rule along with the detected groups list
        and the per-group threshold map.  Re-running detection + length-
        from-PCB-segments on the routed PCB is idempotent given the
        same net classes and physical routing -- this avoids needing to
        persist length / skew metadata in the PCB schema.

        When invoked from the standalone ``kct check`` CLI (no
        ``net_class_map``), no skew data is available, so the rule is
        a conservative no-op (preserves the AC #1 graceful-degradation
        contract -- mirrors the
        :meth:`check_diffpair_length_skew` behaviour).

        See Issue #2702 / Epic #2661 Phase 2G (the rule itself); Issue
        #2710 / Epic #2661 Phase 2.5G (this producer wiring).

        Returns:
            :class:`DRCResults` containing match-group length-skew
            violations.  Empty on standalone ``kct check`` invocations
            (no router context to supply ``group_skew_data``).
        """
        if self.net_class_map is None:
            # Graceful-no-op: no router context -> no skew data to
            # validate.  Matches the standalone-``kct check`` contract.
            rule = MatchGroupLengthSkewRule()
        else:
            from kicad_tools.validate.match_group_skew import derive_group_skew_data

            group_skew_data, tracker_match_groups, threshold_map = derive_group_skew_data(
                self.pcb, self.net_class_map
            )
            rule = MatchGroupLengthSkewRule(
                group_skew_data=group_skew_data,
                tracker_match_groups=tracker_match_groups,
                threshold_map=threshold_map,
            )
        return rule.check(self.pcb, self.design_rules)

    def check_silkscreen(self) -> DRCResults:
        """Check silkscreen rules (line width, text height, over-pad).

        Validates:
        - Minimum silkscreen line width
        - Minimum silkscreen text height
        - Silkscreen elements overlapping exposed pads

        Returns:
            DRCResults containing silkscreen violations
        """
        return check_all_silkscreen(
            self.pcb, self.design_rules, suppress_library=self.suppress_library
        )

    def check_solder_mask_pads(self) -> DRCResults:
        """Check solder mask and pad dimension rules.

        Validates:
        - Solder mask expansion meets manufacturer minimum clearance
        - Minimum pad size for manufacturability
        - PTH pad annular ring

        Returns:
            DRCResults containing solder mask and pad violations
        """
        from .rules.solder_mask import SolderMaskPadRules

        rule = SolderMaskPadRules()
        return rule.check(self.pcb, self.design_rules)

    def check_footprint_placement(self) -> DRCResults:
        """Check that footprints are placed inside the board outline.

        Uses a point-in-polygon test on each footprint's centroid to
        detect components placed entirely outside the Edge.Cuts boundary.

        Returns:
            DRCResults containing placement violations
        """
        rule = FootprintOutsideBoardRule()
        return rule.check(self.pcb, self.design_rules)

    def check_netlist(self) -> DRCResults:
        """Check for pads referencing undeclared nets.

        Validates that every pad net name appears in the board-level
        net declarations.  Pads whose net name was never declared
        indicate a stale or incomplete netlist import.

        Returns:
            DRCResults containing netlist integrity warnings
        """
        from .rules.netlist import NetlistRule

        rule = NetlistRule()
        return rule.check(self.pcb, self.design_rules)

    def check_single_pad_nets(self) -> DRCResults:
        """Check for signal nets that are connected to only one pad.

        A declared signal net with exactly one pad assignment is
        structurally unroutable and almost always indicates a missing
        footprint or schematic/PCB drift.  Pour nets (POWER/GROUND) are
        silently allowed because a single test point or pour-only net
        is a legitimate design pattern.

        Returns:
            DRCResults containing single-pad-net errors.
        """
        from .rules.single_pad_net import SinglePadNetRule

        rule = SinglePadNetRule()
        return rule.check(self.pcb, self.design_rules)

    def check_via_in_pad(self) -> DRCResults:
        """Check for vias placed inside SMD pads on unsupported profiles.

        Fires only when the active manufacturer profile has
        ``via_in_pad_supported=False`` (the default for ``jlcpcb``,
        ``oshpark``, ``seeed``, ``flashpcb``).  The router refuses to
        place in-pad vias for those profiles, but a hand-edited or
        third-party-routed board could still contain them -- this rule
        verifies the resulting board independently.

        Returns:
            DRCResults containing via_in_pad violations (one per
            offending via/pad pair).  Empty on profiles that support
            via-in-pad (e.g., jlcpcb-tier1, pcbway).
        """
        rule = ViaInPadRule()
        return rule.check(self.pcb, self.design_rules)

    def check_zones(self) -> DRCResults:
        """Check zone fill rules (unfilled zones, disabled fill, unassigned nets).

        Validates that all copper zones have been filled with polygon data
        and are assigned to a net. Unfilled zones break power/ground
        connectivity.

        Returns:
            DRCResults containing zone fill violations
        """
        rule = ZoneFillRule()
        return rule.check(self.pcb, self.design_rules)

    def check_pad_grid_alignment(
        self,
        grid_resolution: float = 0.1,
        threshold: float | None = None,
        auto_derive_threshold: bool = False,
    ) -> DRCResults:
        """Check that every pad aligns to the router grid.

        Off-grid pads cause routing failures (``PADS_OFF_GRID``) deep
        inside the autorouter; running this check at PCB-write time
        produces a much earlier and more actionable error.  See
        :func:`kicad_tools.router.preflight.check_pad_grid_alignment`
        for the underlying implementation.

        Args:
            grid_resolution: Router grid resolution in mm (default ``0.1``,
                matching the ``Autorouter`` and ``KCT_ROUTE_GRID`` default).
            threshold: Maximum L2 deviation considered "on-grid", in mm.
                When ``None`` and ``auto_derive_threshold`` is ``False``,
                defaults to
                :data:`kicad_tools.router.preflight.DEFAULT_PAD_GRID_TOLERANCE_MM`
                (``0.05`` mm), chosen to clear stock KiCad library footprints
                whose pads sit 0.03-0.05 mm off the 0.1 mm grid by design.
                Genuine placement errors at >= 0.06 mm still flag.  An
                explicit ``threshold`` always wins over
                ``auto_derive_threshold``.
            auto_derive_threshold: When ``True`` and ``threshold`` is
                ``None``, derive the threshold per-board from the
                pad-offset histogram (issue #3061).  Defaults to
                ``False`` so the Python API preserves PR #3057 behaviour;
                ``kct check`` opts in by default.

        Returns:
            :class:`DRCResults` with one ``pad_grid`` violation (severity
            error) per off-grid pad.  Empty when all pads align.
        """
        from kicad_tools.router.preflight import check_pad_grid_alignment

        results = DRCResults()

        # The preflight needs the PCB file path -- it re-parses the file
        # using load_pads_for_analysis() to get absolute pad coordinates
        # with footprint rotation handled correctly.
        if self.pcb.path is None:
            # Cannot run without a backing file (in-memory PCBs can't be
            # checked because the underlying parser operates on text).
            results.rules_checked += 1
            return results

        report = check_pad_grid_alignment(
            self.pcb.path,
            grid_resolution=grid_resolution,
            threshold=threshold,
            clearance=self.design_rules.min_clearance_mm,
            auto_derive_threshold=auto_derive_threshold,
        )

        results.rules_checked += 1

        for pad in report.off_grid_pads:
            message = pad.message(report.grid_resolution, report.suggested_grid)
            ref_label = pad.label
            results.add(
                DRCViolation(
                    rule_id="pad_grid",
                    severity="warning",
                    message=message,
                    location=(pad.x, pad.y),
                    actual_value=pad.offset_mm,
                    required_value=report.threshold,
                    items=(ref_label,) if ref_label else (),
                )
            )
        return results

    def __repr__(self) -> str:
        return (
            f"DRCChecker(manufacturer={self.manufacturer!r}, "
            f"layers={self.layers}, copper_oz={self.copper_oz})"
        )
