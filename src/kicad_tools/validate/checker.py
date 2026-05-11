"""Pure Python DRC checker.

This module provides the main DRCChecker class that performs Design Rule
Checks on PCB designs without requiring kicad-cli.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from kicad_tools.manufacturers import DesignRules, get_profile

from .rules.clearance import ClearanceRule
from .rules.diffpair_clearance_intra import DiffPairClearanceIntraRule
from .rules.edge import EdgeClearanceRule
from .rules.placement import FootprintOutsideBoardRule
from .rules.silkscreen import check_all_silkscreen
from .rules.via_in_pad import ViaInPadRule
from .rules.zone_fill import ZoneFillRule
from .violations import DRCResults, DRCViolation

if TYPE_CHECKING:
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
    ) -> None:
        """Initialize the DRC checker.

        Args:
            pcb: The PCB to check
            manufacturer: Manufacturer ID (e.g., "jlcpcb", "oshpark")
            layers: Number of PCB layers (2, 4, 6, etc.)
            copper_oz: Copper weight in oz
            suppress_library: If True, suppress silkscreen warnings for
                footprints originating from standard KiCad libraries

        Raises:
            ValueError: If manufacturer ID is not recognized
        """
        self.pcb = pcb
        self.manufacturer = manufacturer
        self.layers = layers
        self.copper_oz = copper_oz
        self.suppress_library = suppress_library

        # Load manufacturer profile and design rules
        profile = get_profile(manufacturer)
        self.design_rules: DesignRules = profile.get_design_rules(layers, copper_oz)

    def check_all(
        self,
        filters: list[ViolationFilter] | None = None,
    ) -> DRCResults:
        """Run all DRC checks.

        Args:
            filters: Optional list of :class:`ViolationFilter` rules.  When
                provided, matching violations are suppressed or reclassified
                and the ``suppressed_count`` field is populated.

        Returns:
            DRCResults containing all violations found (after filtering,
            if filters are provided).
        """
        results = DRCResults()

        # Run each category of checks
        results.merge(self.check_clearances())
        results.merge(self.check_diffpair_clearance_intra())
        results.merge(self.check_dimensions())
        results.merge(self.check_edge_clearances())
        results.merge(self.check_silkscreen())
        results.merge(self.check_solder_mask_pads())
        results.merge(self.check_footprint_placement())
        results.merge(self.check_netlist())
        results.merge(self.check_single_pad_nets())
        results.merge(self.check_via_in_pad())
        results.merge(self.check_zones())

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
                Defaults to ``grid_resolution / 10`` to match the router-side
                check.

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
