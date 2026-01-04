"""Propagation delay and timing analysis for signal integrity.

This module provides timing analysis capabilities for:
- Propagation delay per unit length calculation
- Total net delay from trace analysis
- Length matching analysis for bus signals
- Differential pair skew analysis

Example::

    from kicad_tools.physics import Stackup, TimingAnalyzer

    # Create timing analyzer from stackup
    stackup = Stackup.jlcpcb_4layer()
    timing = TimingAnalyzer(stackup)

    # Get propagation delay characteristics
    result = timing.propagation_delay(width_mm=0.2, layer="F.Cu")
    print(f"Delay: {result.delay_ps_per_mm:.2f} ps/mm")

    # Analyze a specific trace length
    result = timing.analyze_trace(
        trace_length_mm=50.0,
        width_mm=0.2,
        layer="F.Cu"
    )
    print(f"Total delay: {result.total_delay_ns:.3f} ns")

    # Calculate trace length for target delay
    length = timing.length_for_delay(
        target_delay_ns=0.5,
        width_mm=0.2,
        layer="F.Cu"
    )
    print(f"Length for 0.5ns delay: {length:.2f} mm")

References:
    IPC-2141A Section 6: Signal Transmission Characteristics
    IEEE 802.3 length matching requirements
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

from .constants import SPEED_OF_LIGHT
from .stackup import Stackup
from .transmission_line import TransmissionLine

if TYPE_CHECKING:
    pass


@dataclass
class PropagationResult:
    """Propagation characteristics for a trace.

    Contains propagation delay and velocity information for a given
    trace geometry on a specific layer.

    Attributes:
        delay_ps_per_mm: Propagation delay in picoseconds per millimeter
        delay_ns_per_inch: Propagation delay in nanoseconds per inch
        velocity_m_per_s: Signal phase velocity in meters per second
        velocity_percent_c: Velocity as percentage of speed of light
        total_delay_ns: Total delay for the trace length (0 if not analyzed)
        trace_length_mm: Trace length used for total_delay calculation (0 if not set)
    """

    delay_ps_per_mm: float
    delay_ns_per_inch: float
    velocity_m_per_s: float
    velocity_percent_c: float
    total_delay_ns: float = 0.0
    trace_length_mm: float = 0.0

    def __repr__(self) -> str:
        if self.total_delay_ns > 0:
            return (
                f"PropagationResult(delay={self.delay_ps_per_mm:.2f}ps/mm, "
                f"total={self.total_delay_ns:.3f}ns, "
                f"v={self.velocity_percent_c:.1f}%c)"
            )
        return (
            f"PropagationResult(delay={self.delay_ps_per_mm:.2f}ps/mm, "
            f"v={self.velocity_percent_c:.1f}%c)"
        )


@dataclass
class TimingBudget:
    """Timing analysis for a net or group of nets.

    Used for length matching analysis where multiple nets need to have
    matched propagation delays (e.g., DDR data bus, differential pairs).

    Attributes:
        net_name: Name of the net being analyzed
        trace_length_mm: Total trace length in millimeters
        propagation_delay_ns: Total propagation delay in nanoseconds
        target_delay_ns: Target delay for matching (None if not specified)
        skew_ns: Difference from target delay (None if no target)
        within_budget: Whether the net is within acceptable skew tolerance
    """

    net_name: str
    trace_length_mm: float
    propagation_delay_ns: float
    target_delay_ns: float | None = None
    skew_ns: float | None = None
    within_budget: bool = True

    def __repr__(self) -> str:
        if self.skew_ns is not None:
            status = "OK" if self.within_budget else "FAIL"
            return (
                f"TimingBudget({self.net_name}: "
                f"{self.propagation_delay_ns:.3f}ns, "
                f"skew={self.skew_ns * 1000:.1f}ps [{status}])"
            )
        return f"TimingBudget({self.net_name}: {self.propagation_delay_ns:.3f}ns)"


@dataclass
class DifferentialPairSkew:
    """Skew analysis for a differential pair.

    Differential pairs require tight skew matching to maintain
    signal integrity. Different protocols have different requirements:
    - USB 2.0: max 10ps intra-pair skew
    - USB 3.0: max 15ps intra-pair skew
    - PCIe: max 5ps intra-pair skew

    Attributes:
        positive_net: Name of the positive (P) net
        negative_net: Name of the negative (N) net
        p_delay_ns: Propagation delay for positive net
        n_delay_ns: Propagation delay for negative net
        skew_ps: Absolute skew between P and N in picoseconds
        max_skew_ps: Maximum allowed skew in picoseconds
        within_spec: Whether skew is within specification
    """

    positive_net: str
    negative_net: str
    p_delay_ns: float
    n_delay_ns: float
    skew_ps: float
    max_skew_ps: float
    within_spec: bool

    @property
    def p_longer(self) -> bool:
        """Check if positive net is longer (has more delay)."""
        return self.p_delay_ns > self.n_delay_ns

    @property
    def recommendation(self) -> str | None:
        """Get recommendation if skew is out of spec."""
        if self.within_spec:
            return None
        longer = "P" if self.p_longer else "N"
        return f"Reduce {longer} net length by ~{self.skew_ps / 6:.1f}mm to meet spec"

    def __repr__(self) -> str:
        status = "OK" if self.within_spec else "FAIL"
        return (
            f"DifferentialPairSkew({self.positive_net}/{self.negative_net}: "
            f"skew={self.skew_ps:.1f}ps [{status}])"
        )


class TimingAnalyzer:
    """Propagation delay and timing analysis.

    Provides methods for calculating propagation delay, analyzing
    trace timing, and performing length matching analysis for
    high-speed signals.

    Attributes:
        stackup: PCB stackup for geometry and material properties
        tl: TransmissionLine calculator for impedance results
    """

    def __init__(self, stackup: Stackup) -> None:
        """Initialize with a stackup.

        Args:
            stackup: PCB stackup for geometry and material properties
        """
        self.stackup = stackup
        self.tl = TransmissionLine(stackup)

    def propagation_delay(
        self,
        width_mm: float,
        layer: str,
        mode: str = "auto",
    ) -> PropagationResult:
        """Calculate propagation delay characteristics.

        Computes the propagation delay per unit length for a trace
        with the given width on the specified layer.

        Args:
            width_mm: Trace width in millimeters
            layer: Layer name (e.g., "F.Cu", "In1.Cu")
            mode: Calculation mode:
                - "microstrip": Force microstrip calculation (outer layers)
                - "stripline": Force stripline calculation (inner layers)
                - "auto": Detect from layer position (default)

        Returns:
            PropagationResult with delay per unit length and velocity

        Raises:
            ValueError: If width is non-positive or layer not found
        """
        if width_mm <= 0:
            raise ValueError(f"Trace width must be positive, got {width_mm}")

        # Determine calculation mode
        if mode == "auto":
            use_microstrip = self.stackup.is_outer_layer(layer)
        elif mode == "microstrip":
            use_microstrip = True
        elif mode == "stripline":
            use_microstrip = False
        else:
            raise ValueError(f"Invalid mode: {mode}. Use 'auto', 'microstrip', or 'stripline'")

        # Get impedance result which includes phase velocity
        if use_microstrip:
            result = self.tl.microstrip(width_mm, layer)
        else:
            result = self.tl.stripline(width_mm, layer)

        # Calculate propagation delay
        # delay = distance / velocity
        # For 1mm: delay_s = 0.001 / v_p
        # Convert to ps: delay_ps = delay_s * 1e12
        delay_ps_per_mm = result.propagation_delay_ps_per_mm
        delay_ns_per_inch = result.propagation_delay_ns_per_inch

        # Velocity as percentage of speed of light
        velocity_percent_c = (result.phase_velocity / SPEED_OF_LIGHT) * 100

        return PropagationResult(
            delay_ps_per_mm=delay_ps_per_mm,
            delay_ns_per_inch=delay_ns_per_inch,
            velocity_m_per_s=result.phase_velocity,
            velocity_percent_c=velocity_percent_c,
            total_delay_ns=0.0,
            trace_length_mm=0.0,
        )

    def analyze_trace(
        self,
        trace_length_mm: float,
        width_mm: float,
        layer: str,
        mode: str = "auto",
    ) -> PropagationResult:
        """Calculate total propagation delay for a trace.

        Analyzes a trace of known length and returns the complete
        propagation characteristics including total delay.

        Args:
            trace_length_mm: Total trace length in millimeters
            width_mm: Trace width in millimeters
            layer: Layer name (e.g., "F.Cu", "In1.Cu")
            mode: Calculation mode ("auto", "microstrip", or "stripline")

        Returns:
            PropagationResult with total delay calculated

        Raises:
            ValueError: If length or width is non-positive
        """
        if trace_length_mm <= 0:
            raise ValueError(f"Trace length must be positive, got {trace_length_mm}")

        # Get base propagation characteristics
        base = self.propagation_delay(width_mm, layer, mode)

        # Calculate total delay
        # delay_ps = delay_ps_per_mm * length_mm
        # convert to ns: delay_ns = delay_ps / 1000
        total_delay_ns = base.delay_ps_per_mm * trace_length_mm / 1000

        return PropagationResult(
            delay_ps_per_mm=base.delay_ps_per_mm,
            delay_ns_per_inch=base.delay_ns_per_inch,
            velocity_m_per_s=base.velocity_m_per_s,
            velocity_percent_c=base.velocity_percent_c,
            total_delay_ns=total_delay_ns,
            trace_length_mm=trace_length_mm,
        )

    def length_for_delay(
        self,
        target_delay_ns: float,
        width_mm: float,
        layer: str,
        mode: str = "auto",
    ) -> float:
        """Calculate trace length required for a target delay.

        Given a target propagation delay, calculate the trace length
        needed to achieve it. Useful for length matching calculations.

        Args:
            target_delay_ns: Target propagation delay in nanoseconds
            width_mm: Trace width in millimeters
            layer: Layer name (e.g., "F.Cu", "In1.Cu")
            mode: Calculation mode ("auto", "microstrip", or "stripline")

        Returns:
            Trace length in millimeters

        Raises:
            ValueError: If target_delay is non-positive
        """
        if target_delay_ns <= 0:
            raise ValueError(f"Target delay must be positive, got {target_delay_ns}")

        # Get propagation characteristics
        prop = self.propagation_delay(width_mm, layer, mode)

        # Calculate length
        # delay_ps = delay_ps_per_mm * length_mm
        # delay_ns = delay_ps / 1000
        # length_mm = delay_ns * 1000 / delay_ps_per_mm
        return target_delay_ns * 1000 / prop.delay_ps_per_mm

    def analyze_length_matching(
        self,
        nets: list[dict[str, float]],
        width_mm: float,
        layer: str,
        max_skew_ns: float = 0.1,
        mode: str = "auto",
    ) -> list[TimingBudget]:
        """Analyze length matching for a group of nets.

        Given a list of nets with their lengths, calculate propagation
        delays and check if they are within the specified skew tolerance.

        Args:
            nets: List of dicts with 'name' and 'length_mm' keys
            width_mm: Common trace width in millimeters
            layer: Layer name (e.g., "F.Cu")
            max_skew_ns: Maximum allowed skew in nanoseconds (default 100ps)
            mode: Calculation mode ("auto", "microstrip", or "stripline")

        Returns:
            List of TimingBudget for each net with skew analysis

        Example::

            nets = [
                {"name": "DATA0", "length_mm": 45.2},
                {"name": "DATA1", "length_mm": 44.8},
                {"name": "DATA2", "length_mm": 46.1},
            ]
            results = timing.analyze_length_matching(
                nets, width_mm=0.2, layer="F.Cu", max_skew_ns=0.1
            )
        """
        if not nets:
            return []

        # Get propagation characteristics
        prop = self.propagation_delay(width_mm, layer, mode)

        # Calculate delay for each net
        budgets = []
        delays = []

        for net in nets:
            name = net["name"]
            length = net["length_mm"]
            delay_ns = prop.delay_ps_per_mm * length / 1000
            delays.append(delay_ns)

            budgets.append(
                TimingBudget(
                    net_name=name,
                    trace_length_mm=length,
                    propagation_delay_ns=delay_ns,
                )
            )

        # Find target delay (average or max, depending on matching strategy)
        # Using average as target provides balanced length matching
        target_delay = sum(delays) / len(delays)

        # Update budgets with skew analysis
        for budget in budgets:
            budget.target_delay_ns = target_delay
            budget.skew_ns = budget.propagation_delay_ns - target_delay
            budget.within_budget = abs(budget.skew_ns) <= max_skew_ns

        return budgets

    def analyze_differential_pair_skew(
        self,
        positive_length_mm: float,
        negative_length_mm: float,
        width_mm: float,
        layer: str,
        positive_net: str = "D+",
        negative_net: str = "D-",
        max_skew_ps: float = 10.0,
        mode: str = "auto",
    ) -> DifferentialPairSkew:
        """Analyze skew within a differential pair.

        Differential pairs require very tight length matching to
        minimize intra-pair skew. Different protocols have different
        requirements:
        - USB 2.0: max 10ps intra-pair skew
        - USB 3.0: max 15ps intra-pair skew
        - PCIe: max 5ps intra-pair skew
        - HDMI: max 15ps intra-pair skew

        Args:
            positive_length_mm: Length of positive (P) trace in mm
            negative_length_mm: Length of negative (N) trace in mm
            width_mm: Trace width in millimeters
            layer: Layer name (e.g., "F.Cu")
            positive_net: Name for positive net (default "D+")
            negative_net: Name for negative net (default "D-")
            max_skew_ps: Maximum allowed skew in picoseconds (default 10ps)
            mode: Calculation mode ("auto", "microstrip", or "stripline")

        Returns:
            DifferentialPairSkew with skew analysis

        Example::

            result = timing.analyze_differential_pair_skew(
                positive_length_mm=52.3,
                negative_length_mm=52.1,
                width_mm=0.15,
                layer="F.Cu",
                positive_net="USB_D+",
                negative_net="USB_D-",
                max_skew_ps=10.0,  # USB 2.0 spec
            )
            print(f"Skew: {result.skew_ps:.1f}ps - {'OK' if result.within_spec else 'FAIL'}")
        """
        if positive_length_mm <= 0 or negative_length_mm <= 0:
            raise ValueError("Trace lengths must be positive")

        # Get propagation characteristics
        prop = self.propagation_delay(width_mm, layer, mode)

        # Calculate delays
        p_delay_ns = prop.delay_ps_per_mm * positive_length_mm / 1000
        n_delay_ns = prop.delay_ps_per_mm * negative_length_mm / 1000

        # Calculate skew in picoseconds
        skew_ps = abs(p_delay_ns - n_delay_ns) * 1000

        return DifferentialPairSkew(
            positive_net=positive_net,
            negative_net=negative_net,
            p_delay_ns=p_delay_ns,
            n_delay_ns=n_delay_ns,
            skew_ps=skew_ps,
            max_skew_ps=max_skew_ps,
            within_spec=skew_ps <= max_skew_ps,
        )

    def length_difference_for_skew(
        self,
        max_skew_ps: float,
        width_mm: float,
        layer: str,
        mode: str = "auto",
    ) -> float:
        """Calculate maximum length difference for given skew budget.

        Given a maximum allowed skew, calculate the maximum length
        difference that stays within budget. Useful for setting
        design rules in EDA tools.

        Args:
            max_skew_ps: Maximum allowed skew in picoseconds
            width_mm: Trace width in millimeters
            layer: Layer name (e.g., "F.Cu")
            mode: Calculation mode ("auto", "microstrip", or "stripline")

        Returns:
            Maximum length difference in millimeters

        Example::

            # For USB 2.0 (10ps max skew)
            max_diff = timing.length_difference_for_skew(
                max_skew_ps=10.0,
                width_mm=0.15,
                layer="F.Cu"
            )
            print(f"Max length difference: {max_diff:.3f}mm ({max_diff*1000:.1f}um)")
        """
        if max_skew_ps <= 0:
            raise ValueError(f"Max skew must be positive, got {max_skew_ps}")

        # Get propagation characteristics
        prop = self.propagation_delay(width_mm, layer, mode)

        # Calculate length for the skew
        # skew_ps = delay_ps_per_mm * length_diff_mm
        # length_diff_mm = skew_ps / delay_ps_per_mm
        return max_skew_ps / prop.delay_ps_per_mm

    def serpentine_parameters(
        self,
        target_extra_delay_ns: float,
        width_mm: float,
        spacing_mm: float,
        layer: str,
        mode: str = "auto",
    ) -> dict[str, float]:
        """Calculate serpentine (meander) parameters for delay matching.

        When traces need to be lengthened for timing purposes, serpentine
        patterns are used. This calculates the additional length needed
        and suggests meander parameters.

        Args:
            target_extra_delay_ns: Additional delay needed in nanoseconds
            width_mm: Trace width in millimeters
            spacing_mm: Minimum spacing between meander traces
            layer: Layer name (e.g., "F.Cu")
            mode: Calculation mode ("auto", "microstrip", or "stripline")

        Returns:
            Dict with serpentine parameters:
            - extra_length_mm: Total extra length needed
            - meander_amplitude_mm: Suggested amplitude (height) of meanders
            - meander_pitch_mm: Suggested pitch (spacing between meanders)
            - num_meanders: Approximate number of meanders needed

        Example::

            params = timing.serpentine_parameters(
                target_extra_delay_ns=0.2,
                width_mm=0.2,
                spacing_mm=0.3,
                layer="F.Cu"
            )
            print(f"Extra length: {params['extra_length_mm']:.2f}mm")
            print(f"Meanders: {params['num_meanders']:.0f} at {params['meander_amplitude_mm']:.2f}mm height")
        """
        if target_extra_delay_ns <= 0:
            raise ValueError(f"Target extra delay must be positive, got {target_extra_delay_ns}")
        if spacing_mm <= 0:
            raise ValueError(f"Spacing must be positive, got {spacing_mm}")

        # Calculate extra length needed
        extra_length_mm = self.length_for_delay(target_extra_delay_ns, width_mm, layer, mode)

        # Design meander parameters
        # Rule of thumb: meander amplitude should be at least 3x trace width
        # to minimize coupling between parallel segments
        min_amplitude = max(3 * width_mm, spacing_mm + width_mm)
        meander_amplitude = min_amplitude * 1.5  # Add margin

        # Pitch should accommodate amplitude + spacing
        meander_pitch = 2 * spacing_mm + width_mm

        # Each meander adds approximately 2 * amplitude of extra length
        # (the vertical segments, minus the straight section it replaces)
        length_per_meander = 2 * meander_amplitude
        num_meanders = extra_length_mm / length_per_meander

        return {
            "extra_length_mm": extra_length_mm,
            "meander_amplitude_mm": meander_amplitude,
            "meander_pitch_mm": meander_pitch,
            "num_meanders": num_meanders,
        }
