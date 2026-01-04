"""Crosstalk estimation between parallel traces.

Provides NEXT (near-end crosstalk) and FEXT (far-end crosstalk) calculations
based on coupling coefficients and signal characteristics.

Example::

    from kicad_tools.physics import Stackup
    from kicad_tools.physics.crosstalk import CrosstalkAnalyzer

    stackup = Stackup.jlcpcb_4layer()
    xt = CrosstalkAnalyzer(stackup)

    # Analyze crosstalk between parallel traces
    result = xt.analyze(
        aggressor_width_mm=0.2,
        victim_width_mm=0.2,
        spacing_mm=0.2,
        parallel_length_mm=20,
        layer="F.Cu",
        rise_time_ns=1.0,
    )
    print(f"NEXT: {result.next_percent:.1f}%, FEXT: {result.fext_percent:.1f}%")
    print(f"Severity: {result.severity}")

    # Calculate spacing for crosstalk budget
    spacing = xt.spacing_for_crosstalk_budget(
        max_crosstalk_percent=5.0,
        width_mm=0.2,
        parallel_length_mm=20,
        layer="F.Cu",
    )
    print(f"Need {spacing:.3f}mm spacing for <5% crosstalk")

References:
    IPC-2141A Section 5: Crosstalk and Near-End/Far-End Considerations
    Howard Johnson, "High-Speed Digital Design"
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import SPEED_OF_LIGHT
from .coupled_lines import CoupledLines
from .stackup import Stackup


@dataclass
class CrosstalkResult:
    """Result of crosstalk analysis.

    Attributes:
        next_coefficient: Near-end crosstalk coefficient (0-1)
        fext_coefficient: Far-end crosstalk coefficient (0-1)
        next_db: NEXT in dB (negative value, more negative = less crosstalk)
        fext_db: FEXT in dB (negative value, more negative = less crosstalk)
        next_percent: NEXT as percentage
        fext_percent: FEXT as percentage
        coupled_length_mm: Length of parallel run
        saturation_length_mm: Length where NEXT saturates
        severity: "acceptable", "marginal", or "excessive"
        recommendation: Actionable suggestion (or None if acceptable)
    """

    next_coefficient: float
    fext_coefficient: float
    next_db: float
    fext_db: float
    next_percent: float
    fext_percent: float
    coupled_length_mm: float
    saturation_length_mm: float
    severity: str
    recommendation: str | None

    def __repr__(self) -> str:
        return (
            f"CrosstalkResult(NEXT={self.next_percent:.1f}%, "
            f"FEXT={self.fext_percent:.1f}%, severity={self.severity})"
        )


class CrosstalkAnalyzer:
    """Crosstalk estimation for parallel traces.

    Uses coupled line theory to estimate near-end and far-end crosstalk
    between parallel traces based on geometry and signal characteristics.

    Attributes:
        stackup: PCB stackup for geometry and material properties
    """

    def __init__(self, stackup: Stackup) -> None:
        """Initialize with a stackup.

        Args:
            stackup: PCB stackup for geometry and material properties
        """
        self.stackup = stackup
        self._coupled_lines = CoupledLines(stackup)

    def analyze(
        self,
        aggressor_width_mm: float,
        victim_width_mm: float,
        spacing_mm: float,
        parallel_length_mm: float,
        layer: str,
        rise_time_ns: float = 1.0,
        aggressor_amplitude_v: float = 3.3,
    ) -> CrosstalkResult:
        """Estimate crosstalk between parallel traces.

        Calculates near-end crosstalk (NEXT) and far-end crosstalk (FEXT)
        based on geometry and signal characteristics.

        NEXT (backward crosstalk):
        - Travels backward from coupling region
        - Saturates after saturation length
        - Coefficient: Kb = k/2 (for saturated case)

        FEXT (forward crosstalk):
        - Travels forward with the signal
        - Proportional to coupled length
        - Depends on rise time

        Args:
            aggressor_width_mm: Aggressor trace width in mm
            victim_width_mm: Victim trace width in mm
            spacing_mm: Edge-to-edge spacing in mm
            parallel_length_mm: Length of parallel run in mm
            layer: Layer name (e.g., "F.Cu")
            rise_time_ns: Signal rise time in nanoseconds (default 1ns)
            aggressor_amplitude_v: Signal amplitude in volts (for reference)

        Returns:
            CrosstalkResult with NEXT, FEXT, and recommendations

        Raises:
            ValueError: If any dimension is non-positive
        """
        if aggressor_width_mm <= 0:
            raise ValueError(f"Aggressor width must be positive, got {aggressor_width_mm}")
        if victim_width_mm <= 0:
            raise ValueError(f"Victim width must be positive, got {victim_width_mm}")
        if spacing_mm <= 0:
            raise ValueError(f"Spacing must be positive, got {spacing_mm}")
        if parallel_length_mm <= 0:
            raise ValueError(f"Parallel length must be positive, got {parallel_length_mm}")
        if rise_time_ns <= 0:
            raise ValueError(f"Rise time must be positive, got {rise_time_ns}")

        # Use average width for coupling calculation
        avg_width = (aggressor_width_mm + victim_width_mm) / 2

        # Get coupling coefficient from coupled lines analysis
        # Use microstrip for outer layers, stripline for inner
        if self.stackup.is_outer_layer(layer):
            coupled_result = self._coupled_lines.edge_coupled_microstrip(
                width_mm=avg_width, gap_mm=spacing_mm, layer=layer
            )
        else:
            coupled_result = self._coupled_lines.edge_coupled_stripline(
                width_mm=avg_width, gap_mm=spacing_mm, layer=layer
            )

        k = coupled_result.coupling_coefficient
        eps_eff = (coupled_result.epsilon_eff_even + coupled_result.epsilon_eff_odd) / 2

        # Calculate NEXT and FEXT
        next_coeff, fext_coeff, lsat = self._calculate_crosstalk(
            k=k,
            length_mm=parallel_length_mm,
            rise_time_ns=rise_time_ns,
            eps_eff=eps_eff,
        )

        # Convert to percentages and dB
        next_pct = next_coeff * 100
        fext_pct = fext_coeff * 100

        # dB = 20 * log10(coefficient), clamp to avoid log(0)
        next_db = 20 * math.log10(max(next_coeff, 1e-6))
        fext_db = 20 * math.log10(max(fext_coeff, 1e-6))

        # Determine severity
        severity = self._severity(next_pct, fext_pct)

        # Generate recommendation if needed
        recommendation = self._recommendation(
            severity=severity,
            next_pct=next_pct,
            fext_pct=fext_pct,
            spacing_mm=spacing_mm,
            parallel_length_mm=parallel_length_mm,
            lsat=lsat,
        )

        return CrosstalkResult(
            next_coefficient=next_coeff,
            fext_coefficient=fext_coeff,
            next_db=next_db,
            fext_db=fext_db,
            next_percent=next_pct,
            fext_percent=fext_pct,
            coupled_length_mm=parallel_length_mm,
            saturation_length_mm=lsat,
            severity=severity,
            recommendation=recommendation,
        )

    def _calculate_crosstalk(
        self,
        k: float,
        length_mm: float,
        rise_time_ns: float,
        eps_eff: float,
    ) -> tuple[float, float, float]:
        """Calculate NEXT and FEXT coefficients.

        NEXT (backward crosstalk):
        - Saturates at saturation length
        - Kb = k/2 for L > Lsat

        FEXT (forward crosstalk):
        - Proportional to length
        - Depends on rise time
        - Kf = 2 * k * L / rise_distance

        Args:
            k: Coupling coefficient from CoupledLines (0-1)
            length_mm: Coupled length in mm
            rise_time_ns: Signal rise time in ns
            eps_eff: Effective dielectric constant

        Returns:
            Tuple of (next_coefficient, fext_coefficient, saturation_length_mm)
        """
        # Phase velocity
        v_p = SPEED_OF_LIGHT / math.sqrt(eps_eff) if eps_eff > 0 else SPEED_OF_LIGHT

        # Rise distance in mm
        # rise_time (ns) * velocity (m/s) * 1e-6 = mm
        rise_distance_mm = rise_time_ns * v_p * 1e-6

        # Saturation length (where NEXT reaches maximum)
        # NEXT saturates when coupled length exceeds rise_distance/2
        lsat = rise_distance_mm / 2

        # NEXT coefficient (backward crosstalk)
        # Kb = k/2 is the maximum (saturated) value
        kb = k / 2
        if length_mm < lsat:
            # Linear rise before saturation
            next_coeff = kb * (length_mm / lsat)
        else:
            # Saturated
            next_coeff = kb

        # FEXT coefficient (forward crosstalk)
        # FEXT = 2 * k * L / rise_distance for weak coupling
        # This is an approximation; actual FEXT depends on mode velocity difference
        kf = 2 * k * (length_mm / rise_distance_mm) if rise_distance_mm > 0 else 0

        # Clamp to [0, 1]
        next_coeff = max(0, min(next_coeff, 1.0))
        fext_coeff = max(0, min(kf, 1.0))

        return next_coeff, fext_coeff, lsat

    def _severity(self, next_pct: float, fext_pct: float) -> str:
        """Classify crosstalk severity.

        Thresholds based on typical digital signal integrity budgets:
        - <3%: Acceptable for most applications
        - 3-10%: Marginal, may cause issues with sensitive signals
        - >10%: Excessive, likely to cause signal integrity problems

        Args:
            next_pct: NEXT as percentage
            fext_pct: FEXT as percentage

        Returns:
            Severity classification string
        """
        max_xt = max(next_pct, fext_pct)
        if max_xt < 3:
            return "acceptable"
        elif max_xt < 10:
            return "marginal"
        else:
            return "excessive"

    def _recommendation(
        self,
        severity: str,
        next_pct: float,
        fext_pct: float,
        spacing_mm: float,
        parallel_length_mm: float,
        lsat: float,
    ) -> str | None:
        """Generate actionable recommendation.

        Args:
            severity: Current severity level
            next_pct: NEXT as percentage
            fext_pct: FEXT as percentage
            spacing_mm: Current spacing in mm
            parallel_length_mm: Current parallel length in mm
            lsat: Saturation length in mm

        Returns:
            Recommendation string or None if acceptable
        """
        if severity == "acceptable":
            return None

        recommendations = []

        # Primary mitigation: increase spacing
        # Rule of thumb: doubling spacing reduces coupling by ~75%
        if spacing_mm < 0.5:
            target_spacing = spacing_mm * 2
            recommendations.append(f"Increase spacing to {target_spacing:.2f}mm or more")

        # Secondary mitigation: reduce parallel length
        if fext_pct > next_pct and parallel_length_mm > lsat:
            # FEXT is proportional to length, so reducing length helps
            target_length = parallel_length_mm * 0.5
            recommendations.append(f"Reduce parallel run to {target_length:.1f}mm")

        # Tertiary mitigation: route on different layers
        if severity == "excessive":
            recommendations.append("Consider routing on different layers with ground between")

        if recommendations:
            return "; ".join(recommendations)
        return "Increase trace spacing or reduce parallel coupling length"

    def spacing_for_crosstalk_budget(
        self,
        max_crosstalk_percent: float,
        width_mm: float,
        parallel_length_mm: float,
        layer: str,
        rise_time_ns: float = 1.0,
        tolerance: float = 0.1,
        max_iterations: int = 50,
    ) -> float:
        """Calculate minimum spacing for crosstalk budget.

        Uses bisection to find the minimum edge-to-edge spacing that
        keeps both NEXT and FEXT below the specified percentage.

        Args:
            max_crosstalk_percent: Maximum acceptable crosstalk (e.g., 5%)
            width_mm: Trace width in mm
            parallel_length_mm: Expected parallel run length in mm
            layer: Layer name
            rise_time_ns: Signal rise time in ns
            tolerance: Relative tolerance for convergence (default 10%)
            max_iterations: Maximum iterations for solver

        Returns:
            Minimum edge-to-edge spacing in mm

        Raises:
            ValueError: If parameters are invalid or convergence fails
        """
        if max_crosstalk_percent <= 0:
            raise ValueError(f"Max crosstalk must be positive, got {max_crosstalk_percent}")
        if width_mm <= 0:
            raise ValueError(f"Width must be positive, got {width_mm}")
        if parallel_length_mm <= 0:
            raise ValueError(f"Parallel length must be positive, got {parallel_length_mm}")

        # Get reference height for initial bounds
        h = self.stackup.get_reference_plane_distance(layer)

        # Initial spacing bounds
        # Very tight spacing gives high crosstalk
        # Very loose spacing gives low crosstalk
        spacing_min = h * 0.1  # 10% of dielectric height
        spacing_max = h * 10.0  # 10x dielectric height

        def get_max_crosstalk(spacing: float) -> float:
            """Get maximum of NEXT and FEXT for given spacing."""
            result = self.analyze(
                aggressor_width_mm=width_mm,
                victim_width_mm=width_mm,
                spacing_mm=spacing,
                parallel_length_mm=parallel_length_mm,
                layer=layer,
                rise_time_ns=rise_time_ns,
            )
            return max(result.next_percent, result.fext_percent)

        # Check max bound
        xt_at_max = get_max_crosstalk(spacing_max)

        # Adjust bounds if needed
        while xt_at_max > max_crosstalk_percent and spacing_max < h * 100:
            spacing_max *= 2
            xt_at_max = get_max_crosstalk(spacing_max)

        # If max spacing still gives too much crosstalk, return it anyway
        # (caller should check the result)
        if xt_at_max > max_crosstalk_percent:
            return spacing_max

        # Bisection search
        for _ in range(max_iterations):
            spacing_mid = (spacing_min + spacing_max) / 2
            xt_mid = get_max_crosstalk(spacing_mid)

            if abs(xt_mid - max_crosstalk_percent) / max_crosstalk_percent < tolerance:
                return spacing_mid

            # Higher spacing = lower crosstalk
            if xt_mid > max_crosstalk_percent:
                spacing_min = spacing_mid  # Need wider spacing
            else:
                spacing_max = spacing_mid  # Can use tighter spacing

        # Return best estimate
        return (spacing_min + spacing_max) / 2
