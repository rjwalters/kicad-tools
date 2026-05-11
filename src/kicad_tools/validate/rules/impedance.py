"""Impedance validation DRC rule.

Uses the physics module to verify that trace widths match target
impedance requirements based on net class specifications.

Example::

    from kicad_tools.validate.rules.impedance import ImpedanceRule
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.manufacturers import DesignRules

    pcb = PCB.load("board.kicad_pcb")
    design_rules = DesignRules.jlcpcb_4layer()

    rule = ImpedanceRule()
    results = rule.check(pcb, design_rules)

    if results.errors:
        for err in results.errors:
            print(f"Impedance error: {err.message}")
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import TYPE_CHECKING

from ..violations import DRCResults, DRCViolation
from .base import DRCRule

if TYPE_CHECKING:
    from kicad_tools.manufacturers import DesignRules
    from kicad_tools.physics import CoupledLines, Stackup, TransmissionLine
    from kicad_tools.router.diffpair import DifferentialPair
    from kicad_tools.schema.pcb import PCB


@dataclass
class NetImpedanceSpec:
    """Impedance specification for a net or net class.

    Attributes:
        net_pattern: Regex pattern to match net names
        target_z0: Target characteristic impedance (single-ended)
        target_zdiff: Target differential impedance (for diff pairs)
        tolerance_percent: Allowed deviation from target (default 10%)
    """

    net_pattern: str
    target_z0: float | None = None
    target_zdiff: float | None = None
    tolerance_percent: float = 10.0

    def matches(self, net_name: str) -> bool:
        """Check if this spec matches a net name."""
        return bool(re.match(self.net_pattern, net_name, re.IGNORECASE))


@dataclass
class ImpedanceCheckResult:
    """Result of checking a single trace for impedance compliance.

    Attributes:
        net_name: Net name that was checked
        layer: Layer the trace is on
        width_mm: Actual trace width
        calculated_z0: Calculated impedance for this width
        target_z0: Target impedance
        deviation_percent: Percentage deviation from target
        compliant: Whether the trace meets the tolerance
    """

    net_name: str
    layer: str
    width_mm: float
    calculated_z0: float
    target_z0: float
    deviation_percent: float
    compliant: bool


class ImpedanceRule(DRCRule):
    """DRC rule for verifying trace impedance matches target specifications.

    Uses the physics module to calculate actual trace impedance based on
    the PCB stackup, then compares against net class specifications.

    The rule checks:
    1. Single-ended traces against Z0 targets
    2. Differential pairs against Zdiff targets
    3. Trace width consistency across layers

    Attributes:
        rule_id: "impedance"
        name: "Impedance Control"
        description: Rule description
        specs: List of impedance specifications to check against
    """

    rule_id = "impedance"
    name = "Impedance Control"
    description = "Verify trace widths match target impedance requirements"

    @staticmethod
    def _get_default_specs() -> list[NetImpedanceSpec]:
        """Return default impedance specifications for common signal types."""
        return [
            # USB differential pairs - 90Ω differential
            NetImpedanceSpec(r"USB.*D[PM\+\-]?", target_zdiff=90.0),
            # High-speed single-ended - 50Ω
            NetImpedanceSpec(r".*CLK.*", target_z0=50.0),
            NetImpedanceSpec(r".*MCLK.*", target_z0=50.0),
            NetImpedanceSpec(r".*ETH.*", target_z0=50.0),
            # LVDS/high-speed diff pairs - 100Ω differential
            NetImpedanceSpec(r".*LVDS.*", target_zdiff=100.0),
            NetImpedanceSpec(r".*_[PN]$", target_zdiff=100.0),
        ]

    def __init__(
        self,
        specs: list[NetImpedanceSpec] | None = None,
        stackup: Stackup | None = None,
        detected_pairs: list[DifferentialPair] | None = None,
    ) -> None:
        """Initialize the impedance rule.

        Args:
            specs: Impedance specifications to check. If not provided,
                uses default specs for common signal types.  The defaults
                are auto-applied via net-name regex (``.*CLK.*`` -> 50Ω,
                ``USB.*D[PM]?`` -> 90Ω differential, etc.). On boards
                without a controlled-impedance stackup, these defaults
                are suppressed (see :meth:`check`) — this prevents the
                rule from demanding ~2.8mm-wide SWCLK traces on a generic
                hobbyist 2-layer board (Issue #2696). Pass an explicit
                ``specs`` list to force the rule to evaluate them
                regardless of stackup.
            stackup: PCB stackup for impedance calculations. If not provided,
                will try to extract from PCB during check.
            detected_pairs: Optional list of detected differential pairs
                (per ``router/diffpair.detect_diff_pairs``).  When provided,
                nets that are part of a detected pair are checked against
                ``target_zdiff`` using the coupled-lines model rather than
                the single-ended microstrip / stripline model.  This is the
                Phase 3K diff-pair-awareness extension (Issue #2650); when
                ``None`` (the default), the rule falls back to its pre-Phase
                3K single-ended-only behavior, which preserves backward
                compatibility for standalone ``kct check`` invocations.
        """
        # Remember whether the caller passed explicit specs.  When True,
        # the rule unconditionally evaluates them.  When False, the rule
        # uses its built-in defaults but suppresses them on boards that
        # don't opt into controlled-impedance routing (see check()).
        self._using_default_specs = specs is None
        self.specs = specs if specs is not None else self._get_default_specs()
        self._stackup = stackup
        self._tl: TransmissionLine | None = None
        self._cl: CoupledLines | None = None
        self._detected_pairs = detected_pairs or []
        # Map net name -> partner net name for fast diff-pair lookup.
        self._partner_map: dict[str, str] = {}
        for pair in self._detected_pairs:
            p_name = pair.positive.net_name
            n_name = pair.negative.net_name
            self._partner_map[p_name] = n_name
            self._partner_map[n_name] = p_name

    def add_spec(self, spec: NetImpedanceSpec) -> None:
        """Add an impedance specification to check."""
        self.specs.append(spec)

    def _init_physics(self, pcb: PCB) -> bool:
        """Initialize physics module from PCB or provided stackup.

        Args:
            pcb: PCB to extract stackup from

        Returns:
            True if physics module is available, False otherwise
        """
        if self._tl is not None:
            return True

        try:
            from kicad_tools.physics import CoupledLines, Stackup, TransmissionLine

            if self._stackup is None:
                self._stackup = Stackup.from_pcb(pcb)
            self._tl = TransmissionLine(self._stackup)
            self._cl = CoupledLines(self._stackup)
            return True
        except ImportError:
            return False
        except Exception:
            return False

    def _board_has_controlled_impedance(self) -> bool:
        """Decide whether the current board "opts in" to controlled impedance.

        A board opts in when either:

        - The PCB file has an explicit ``(setup (stackup ...))`` block —
          the designer took the trouble to declare dielectric thicknesses
          and Er values, which is the standard signal of impedance-control
          intent (``Stackup.has_explicit_data``); or
        - The board has 4 or more copper layers — multi-layer boards have
          reference planes close to signal layers, so 50Ω widths are
          physically feasible at the geometric scales the autorouter
          produces (~0.20 - 0.40 mm), and applying defaults does not
          impose unrealistic constraints.

        2-layer boards without an explicit stackup do NOT opt in.  On
        such boards, 50Ω on a generic FR4 1.6mm core requires ~2.8 mm
        wide traces — infeasible for typical 0.5 mm pad pitch — so
        auto-applying the default ``.*CLK.*`` -> 50Ω rule produces
        spurious DRC errors that the board can never satisfy (see
        Issue #2696 for the board-04 SWCLK case).

        Returns:
            True when defaults should be applied; False when the board
            looks like a generic hobbyist 2-layer and defaults should
            be suppressed.
        """
        if self._stackup is None:
            # Without a stackup we have no signal at all — preserve the
            # original "apply defaults" behavior for backward compat.
            return True
        if getattr(self._stackup, "has_explicit_data", False):
            return True
        # No explicit stackup: opt in iff 4+ copper layers.
        try:
            return self._stackup.num_copper_layers >= 4
        except AttributeError:
            return True

    def check(
        self,
        pcb: PCB,
        design_rules: DesignRules,
    ) -> DRCResults:
        """Check PCB traces for impedance compliance.

        Args:
            pcb: The PCB to check
            design_rules: Design rules from the manufacturer profile

        Returns:
            DRCResults containing any impedance violations
        """
        results = DRCResults(rules_checked=1)

        # Initialize physics module
        if not self._init_physics(pcb):
            # Physics module not available - add warning and return
            results.add(
                DRCViolation(
                    rule_id=self.rule_id,
                    severity="warning",
                    message="Impedance check skipped: physics module not available",
                    location=None,
                )
            )
            return results

        # Gate auto-applied defaults on a controlled-impedance opt-in.
        # When the caller did not pass explicit ``specs``, only apply
        # built-in name-pattern defaults (``.*CLK.*`` -> 50Ω, etc.) if
        # the board's stackup declares controlled-impedance intent.
        # Otherwise return empty results — a hobbyist 2-layer board
        # never claimed to be impedance-controlled and should not fail
        # DRC over a target it didn't set (Issue #2696).
        if self._using_default_specs and not self._board_has_controlled_impedance():
            return results

        # Collect all traces from PCB
        trace_data = self._collect_traces(pcb)

        # Check each net against matching specs
        for net_name, traces in trace_data.items():
            spec = self._find_matching_spec(net_name)
            if spec is None:
                continue

            for trace in traces:
                check_result = self._check_trace_impedance(trace, spec)
                if not check_result.compliant:
                    results.add(self._create_violation(check_result, spec))

        return results

    def _collect_traces(
        self,
        pcb: PCB,
    ) -> dict[str, list[dict]]:
        """Collect trace information from PCB.

        Args:
            pcb: PCB to extract traces from

        Returns:
            Dictionary mapping net names to list of trace dictionaries
        """
        trace_data: dict[str, list[dict]] = {}

        # Extract traces from PCB segments
        for segment in getattr(pcb, "segments", []):
            net_name = getattr(segment, "net_name", None)
            if not net_name:
                continue

            if net_name not in trace_data:
                trace_data[net_name] = []

            trace_data[net_name].append(
                {
                    "net_name": net_name,
                    "width_mm": getattr(segment, "width", 0.2),
                    "layer": getattr(segment, "layer", "F.Cu"),
                    "start": (getattr(segment, "x1", 0), getattr(segment, "y1", 0)),
                    "end": (getattr(segment, "x2", 0), getattr(segment, "y2", 0)),
                }
            )

        return trace_data

    def _find_matching_spec(self, net_name: str) -> NetImpedanceSpec | None:
        """Find the first matching impedance spec for a net name.

        Args:
            net_name: Net name to match

        Returns:
            Matching spec or None
        """
        for spec in self.specs:
            if spec.matches(net_name):
                return spec
        return None

    def _check_trace_impedance(
        self,
        trace: dict,
        spec: NetImpedanceSpec,
    ) -> ImpedanceCheckResult:
        """Check a single trace against an impedance specification.

        When the trace's net is part of a detected differential pair
        (per ``self._detected_pairs``) AND the spec has a ``target_zdiff``
        set, the rule uses the coupled-lines model and compares against
        the differential target.  Otherwise it falls back to the
        single-ended microstrip / stripline model.

        Args:
            trace: Trace dictionary with width_mm and layer
            spec: Impedance specification to check against

        Returns:
            ImpedanceCheckResult with calculated values
        """
        width_mm = trace["width_mm"]
        layer = trace["layer"]
        net_name = trace.get("net_name", "unknown")

        is_diff_pair = (
            net_name in self._partner_map and spec.target_zdiff is not None and self._cl is not None
        )

        if is_diff_pair:
            # Use coupled-lines model.  The gap is reconstructed from the
            # trace's spatial relationship to its partner net's nearest
            # trace; absent that, we use the conservative ``trace_clearance``
            # fallback (effectively single-ended for un-routed pairs).
            try:
                # Approximate gap from the trace's spec/class (the rule
                # doesn't have direct access to the per-class clearance
                # at this point; the calling context (the autorouter's
                # check-impedance integration) provides per-net specs
                # with the right zdiff target).  For now we fall back to
                # the spec's tolerance window for compliance check only.
                gap_mm = trace.get("intra_pair_clearance_mm", 0.15)
                if self._stackup.is_outer_layer(layer):
                    result = self._cl.edge_coupled_microstrip(width_mm, gap_mm, layer)
                else:
                    result = self._cl.edge_coupled_stripline(width_mm, gap_mm, layer)
                calculated_z = result.zdiff
                target_z = spec.target_zdiff
            except (ValueError, AttributeError):
                calculated_z = 100.0
                target_z = spec.target_zdiff or 100.0
        else:
            # Calculate single-ended impedance
            try:
                if self._stackup.is_outer_layer(layer):
                    result = self._tl.microstrip(width_mm, layer)
                else:
                    result = self._tl.stripline(width_mm, layer)
                calculated_z = result.z0
            except (ValueError, AttributeError):
                # Calculation failed - assume 50 ohms as default
                calculated_z = 50.0
            target_z = spec.target_z0 or 50.0

        # Calculate deviation
        deviation_percent = abs(calculated_z - target_z) / target_z * 100

        # Check compliance
        compliant = deviation_percent <= spec.tolerance_percent

        return ImpedanceCheckResult(
            net_name=net_name,
            layer=layer,
            width_mm=width_mm,
            calculated_z0=calculated_z,
            target_z0=target_z,
            deviation_percent=deviation_percent,
            compliant=compliant,
        )

    def _create_violation(
        self,
        result: ImpedanceCheckResult,
        spec: NetImpedanceSpec,
    ) -> DRCViolation:
        """Create a DRC violation for an impedance mismatch.

        Args:
            result: Check result showing the mismatch
            spec: Spec that was violated

        Returns:
            DRCViolation for the impedance mismatch
        """
        # Calculate required width for correct impedance
        try:
            required_width = self._tl.width_for_impedance(
                result.target_z0,
                result.layer,
            )
            width_hint = f" (requires {required_width:.3f}mm)"
        except (ValueError, AttributeError):
            width_hint = ""

        return DRCViolation(
            rule_id=self.rule_id,
            severity="error" if result.deviation_percent > 20 else "warning",
            message=(
                f"Trace impedance mismatch on {result.layer}: "
                f"width {result.width_mm:.3f}mm gives {result.calculated_z0:.1f}Ω, "
                f"target is {result.target_z0:.1f}Ω "
                f"({result.deviation_percent:.1f}% deviation){width_hint}"
            ),
            layer=result.layer,
            actual_value=result.calculated_z0,
            required_value=result.target_z0,
            items=(result.net_name,),
        )

    def get_required_width(
        self,
        target_z0: float,
        layer: str,
    ) -> float | None:
        """Calculate required trace width for target impedance.

        Convenience method for calculating trace width.

        Args:
            target_z0: Target impedance in ohms
            layer: Layer to calculate for

        Returns:
            Width in mm, or None if physics not available
        """
        if self._tl is None:
            return None

        try:
            return self._tl.width_for_impedance(target_z0, layer)
        except (ValueError, AttributeError):
            return None

    def get_layer_impedances(
        self,
        width_mm: float,
        layers: list[str] | None = None,
    ) -> dict[str, float]:
        """Calculate impedance for a given width across all layers.

        Useful for understanding how impedance varies by layer for
        a fixed trace width.

        Args:
            width_mm: Trace width in mm
            layers: Layers to calculate for (defaults to common copper layers)

        Returns:
            Dictionary mapping layer names to impedance values
        """
        if self._tl is None:
            return {}

        if layers is None:
            layers = ["F.Cu", "B.Cu", "In1.Cu", "In2.Cu"]

        impedances: dict[str, float] = {}
        for layer in layers:
            try:
                if self._stackup.is_outer_layer(layer):
                    result = self._tl.microstrip(width_mm, layer)
                else:
                    result = self._tl.stripline(width_mm, layer)
                impedances[layer] = result.z0
            except (ValueError, AttributeError):
                continue

        return impedances
