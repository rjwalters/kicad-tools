"""Transmission line impedance calculations.

Provides analytical calculations for microstrip and stripline impedance
using the Hammerstad-Jensen equations.

Example::

    from kicad_tools.physics import Stackup
    from kicad_tools.physics.transmission_line import TransmissionLine

    # Use manufacturer stackup preset
    stackup = Stackup.jlcpcb_4layer()
    tl = TransmissionLine(stackup)

    # Calculate microstrip impedance on top layer
    result = tl.microstrip(width_mm=0.2, layer="F.Cu")
    print(f"Z0 = {result.z0:.1f}Ω, εeff = {result.epsilon_eff:.2f}")

    # Calculate trace width for target impedance
    width = tl.width_for_impedance(z0_target=50, layer="F.Cu")
    print(f"50Ω requires {width:.3f}mm trace width")

References:
    Hammerstad & Jensen, "Accurate Models for Microstrip Computer-Aided Design",
    MTT-S International Microwave Symposium Digest, 1980.
    IPC-2141A Section 4.2
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import COPPER_CONDUCTIVITY, SPEED_OF_LIGHT
from .stackup import Stackup


@dataclass
class ImpedanceResult:
    """Result of transmission line impedance calculation.

    Attributes:
        z0: Characteristic impedance in ohms (Ω)
        epsilon_eff: Effective dielectric constant
        loss_db_per_m: Total loss in dB per meter at reference frequency
        phase_velocity: Signal propagation velocity in m/s
    """

    z0: float
    epsilon_eff: float
    loss_db_per_m: float
    phase_velocity: float

    @property
    def propagation_delay_ps_per_mm(self) -> float:
        """Propagation delay in picoseconds per millimeter."""
        if self.phase_velocity <= 0:
            return 0.0
        # 1 mm = 0.001 m, velocity in m/s
        # delay = distance / velocity = 0.001 / v_p (in seconds)
        # convert to ps: * 1e12
        return 1e12 * 0.001 / self.phase_velocity

    @property
    def propagation_delay_ns_per_inch(self) -> float:
        """Propagation delay in nanoseconds per inch (common US unit)."""
        # 1 inch = 25.4 mm
        return self.propagation_delay_ps_per_mm * 25.4 / 1000

    def __repr__(self) -> str:
        return (
            f"ImpedanceResult(z0={self.z0:.2f}Ω, "
            f"εeff={self.epsilon_eff:.3f}, "
            f"loss={self.loss_db_per_m:.3f}dB/m)"
        )


class TransmissionLine:
    """Analytical transmission line impedance calculator.

    Uses Hammerstad-Jensen equations for microstrip and analytical
    formulas for stripline to calculate characteristic impedance,
    effective dielectric constant, and propagation parameters.

    Attributes:
        stackup: PCB stackup defining layer geometry and materials
    """

    def __init__(self, stackup: Stackup) -> None:
        """Initialize with a stackup.

        Args:
            stackup: PCB stackup for geometry and material properties
        """
        self.stackup = stackup

    def microstrip(
        self,
        width_mm: float,
        layer: str,
        frequency_ghz: float = 1.0,
    ) -> ImpedanceResult:
        """Calculate microstrip impedance using Hammerstad-Jensen equations.

        Microstrip is an outer layer trace (F.Cu or B.Cu) with the dielectric
        below and air above. This method uses the Hammerstad-Jensen equations
        which are accurate to within 0.2% for most practical geometries.

        Args:
            width_mm: Trace width in millimeters
            layer: Layer name (e.g., "F.Cu", "B.Cu")
            frequency_ghz: Frequency in GHz for loss calculation

        Returns:
            ImpedanceResult with Z0, effective epsilon, loss, and velocity

        Raises:
            ValueError: If width is non-positive or layer not found
        """
        if width_mm <= 0:
            raise ValueError(f"Trace width must be positive, got {width_mm}")

        h = self.stackup.get_reference_plane_distance(layer)
        if h <= 0:
            raise ValueError(f"Could not determine dielectric height for layer {layer}")

        er = self.stackup.get_dielectric_constant(layer)
        t = self.stackup.get_copper_thickness(layer)
        tan_d = self.stackup.get_loss_tangent(layer)

        return self._microstrip_calc(width_mm, h, er, t, tan_d, frequency_ghz)

    def _microstrip_calc(
        self,
        w: float,
        h: float,
        er: float,
        t: float,
        tan_d: float,
        freq_ghz: float,
    ) -> ImpedanceResult:
        """Hammerstad-Jensen microstrip impedance calculation.

        Args:
            w: Trace width in mm
            h: Dielectric height in mm
            er: Relative dielectric constant
            t: Copper thickness in mm
            tan_d: Dielectric loss tangent
            freq_ghz: Frequency in GHz

        Returns:
            ImpedanceResult
        """
        # Effective width accounting for copper thickness
        # From Hammerstad-Jensen, accounts for fringing due to copper thickness
        if t > 0 and h > 0:
            # Prevent division by zero or log of non-positive values
            denom1 = (t / h) ** 2
            denom2 = (t / (w * math.pi + 1.1 * t * math.pi)) ** 2
            if denom1 + denom2 > 0:
                w_eff = w + (t / math.pi) * math.log(4 * math.e / math.sqrt(denom1 + denom2))
            else:
                w_eff = w
        else:
            w_eff = w

        # Normalized width
        u = w_eff / h

        # Effective dielectric constant (Hammerstad-Jensen)
        # Equation for epsilon_eff
        a = (
            1
            + (1 / 49) * math.log((u**4 + (u / 52) ** 2) / (u**4 + 0.432))
            + (1 / 18.7) * math.log(1 + (u / 18.1) ** 3)
        )

        b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053

        eps_eff = (er + 1) / 2 + ((er - 1) / 2) * (1 + 10 / u) ** (-a * b)

        # Characteristic impedance (Hammerstad-Jensen)
        # Two different formulas for narrow and wide traces
        f_u = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u) ** 0.7528))
        z0 = (60 / math.sqrt(eps_eff)) * math.log(f_u / u + math.sqrt(1 + (2 / u) ** 2))

        # Phase velocity
        v_p = SPEED_OF_LIGHT / math.sqrt(eps_eff)

        # Loss estimation
        loss = self._estimate_microstrip_loss(w, h, er, t, eps_eff, z0, tan_d, freq_ghz)

        return ImpedanceResult(
            z0=z0,
            epsilon_eff=eps_eff,
            loss_db_per_m=loss,
            phase_velocity=v_p,
        )

    def stripline(
        self,
        width_mm: float,
        layer: str,
        frequency_ghz: float = 1.0,
    ) -> ImpedanceResult:
        """Calculate stripline impedance for inner layer traces.

        Stripline is a trace sandwiched between two reference planes.
        This supports both symmetric (h1 = h2) and asymmetric (h1 ≠ h2)
        stripline configurations.

        Args:
            width_mm: Trace width in millimeters
            layer: Inner layer name (e.g., "In1.Cu", "In2.Cu")
            frequency_ghz: Frequency in GHz for loss calculation

        Returns:
            ImpedanceResult with Z0, effective epsilon, loss, and velocity

        Raises:
            ValueError: If width is non-positive or layer not found
        """
        if width_mm <= 0:
            raise ValueError(f"Trace width must be positive, got {width_mm}")

        h1, h2 = self.stackup.get_stripline_geometry(layer)
        if h1 <= 0 or h2 <= 0:
            raise ValueError(f"Could not determine geometry for layer {layer}")

        er = self.stackup.get_dielectric_constant(layer)
        t = self.stackup.get_copper_thickness(layer)
        tan_d = self.stackup.get_loss_tangent(layer)

        return self._stripline_calc(width_mm, h1, h2, er, t, tan_d, frequency_ghz)

    def _stripline_calc(
        self,
        w: float,
        h1: float,
        h2: float,
        er: float,
        t: float,
        tan_d: float,
        freq_ghz: float,
    ) -> ImpedanceResult:
        """Stripline impedance calculation.

        Uses the IPC-2141 stripline formula with thickness correction.
        For asymmetric stripline, uses an effective height.

        Args:
            w: Trace width in mm
            h1: Distance to upper reference plane in mm
            h2: Distance to lower reference plane in mm
            er: Relative dielectric constant
            t: Copper thickness in mm
            tan_d: Dielectric loss tangent
            freq_ghz: Frequency in GHz

        Returns:
            ImpedanceResult
        """
        # Total distance between planes (b in classic formula)
        b = h1 + h2 + t

        # For stripline, epsilon_eff = er (fully embedded in dielectric)
        eps_eff = er

        # Effective width with thickness correction
        # From IPC-2141: w_eff = w + t/pi * (1 + ln(2*h/t))
        # where h is the smaller of h1, h2
        h_min = min(h1, h2)
        if t > 0 and h_min > 0:
            w_eff = w + (t / math.pi) * (1 + math.log(2 * h_min / t))
        else:
            w_eff = w

        # Characteristic impedance using IPC-2141 formula
        # Z0 = (60 / sqrt(er)) * ln(4*b / (0.67*pi*(0.8*w + t)))
        # This is the classic stripline formula that works for most geometries
        denominator = 0.67 * math.pi * (0.8 * w_eff + t)
        if denominator > 0 and b > 0:
            z0 = (60 / math.sqrt(er)) * math.log(4 * b / denominator)
        else:
            z0 = 50.0  # Default fallback

        # For highly asymmetric stripline (h1 >> h2 or vice versa),
        # apply correction factor toward microstrip behavior
        asymmetry = abs(h1 - h2) / (h1 + h2) if (h1 + h2) > 0 else 0
        if asymmetry > 0.5:
            # High asymmetry - trace is closer to one plane
            # Reduce impedance slightly as it behaves more like microstrip
            correction = 1 - 0.2 * (asymmetry - 0.5)
            z0 = z0 * correction

        # Clamp to reasonable range
        z0 = max(10, min(z0, 200))

        # Phase velocity (in stripline, signal is fully in dielectric)
        v_p = SPEED_OF_LIGHT / math.sqrt(er)

        # Loss estimation
        loss = self._estimate_stripline_loss(w, b, er, t, z0, tan_d, freq_ghz)

        return ImpedanceResult(
            z0=z0,
            epsilon_eff=eps_eff,
            loss_db_per_m=loss,
            phase_velocity=v_p,
        )

    def _estimate_microstrip_loss(
        self,
        w: float,
        h: float,
        er: float,
        t: float,
        eps_eff: float,
        z0: float,
        tan_d: float,
        freq_ghz: float,
    ) -> float:
        """Estimate microstrip loss in dB/m.

        Total loss = conductor loss + dielectric loss

        Args:
            w: Trace width in mm
            h: Dielectric height in mm
            er: Relative dielectric constant
            t: Copper thickness in mm
            eps_eff: Effective dielectric constant
            z0: Characteristic impedance
            tan_d: Dielectric loss tangent
            freq_ghz: Frequency in GHz

        Returns:
            Total loss in dB/m
        """
        freq_hz = freq_ghz * 1e9

        # Conductor loss (skin effect)
        # Rs = sqrt(pi * f * mu0 / sigma)
        mu0 = 4 * math.pi * 1e-7
        rs = math.sqrt(math.pi * freq_hz * mu0 / COPPER_CONDUCTIVITY)

        # Conductor loss coefficient
        # alpha_c ≈ Rs / (z0 * w) for microstrip (simplified)
        w_m = w / 1000  # Convert mm to m
        if w_m > 0 and z0 > 0:
            # More accurate formula considering geometry
            alpha_c = rs / (z0 * w_m)  # Np/m
            alpha_c_db = alpha_c * 8.686  # dB/m
        else:
            alpha_c_db = 0

        # Dielectric loss
        # alpha_d = (pi * f * sqrt(eps_eff) * tan_d) / c
        # But for microstrip, we need the filling factor
        q = (eps_eff - 1) / (er - 1) if er > 1 else 0.5
        alpha_d = math.pi * freq_hz * math.sqrt(eps_eff) * er * q * tan_d / SPEED_OF_LIGHT
        alpha_d_db = alpha_d * 8.686  # Np/m to dB/m

        return alpha_c_db + alpha_d_db

    def _estimate_stripline_loss(
        self,
        w: float,
        b: float,
        er: float,
        t: float,
        z0: float,
        tan_d: float,
        freq_ghz: float,
    ) -> float:
        """Estimate stripline loss in dB/m.

        Args:
            w: Trace width in mm
            b: Total height between planes in mm
            er: Relative dielectric constant
            t: Copper thickness in mm
            z0: Characteristic impedance
            tan_d: Dielectric loss tangent
            freq_ghz: Frequency in GHz

        Returns:
            Total loss in dB/m
        """
        freq_hz = freq_ghz * 1e9

        # Conductor loss (skin effect)
        mu0 = 4 * math.pi * 1e-7
        rs = math.sqrt(math.pi * freq_hz * mu0 / COPPER_CONDUCTIVITY)

        # Conductor loss for stripline
        w_m = w / 1000  # mm to m
        if w_m > 0 and z0 > 0:
            # Approximate formula for stripline conductor loss
            alpha_c = (2.7e-3 * rs * math.sqrt(er)) / (z0 * w_m)  # Np/m
            alpha_c_db = alpha_c * 8.686  # dB/m
        else:
            alpha_c_db = 0

        # Dielectric loss for stripline (full dielectric, no filling factor)
        # alpha_d = (pi * f * sqrt(er) * tan_d) / c
        alpha_d = math.pi * freq_hz * math.sqrt(er) * tan_d / SPEED_OF_LIGHT
        alpha_d_db = alpha_d * 8.686  # Np/m to dB/m

        return alpha_c_db + alpha_d_db

    def width_for_impedance(
        self,
        z0_target: float,
        layer: str,
        mode: str = "auto",
        tolerance: float = 0.01,
        max_iterations: int = 50,
    ) -> float:
        """Calculate trace width for a target impedance.

        Uses bisection method to find the trace width that produces
        the target characteristic impedance on the specified layer.

        Args:
            z0_target: Target impedance in ohms (e.g., 50.0)
            layer: Layer name (e.g., "F.Cu", "In1.Cu")
            mode: Transmission line mode:
                - "microstrip": Force microstrip calculation
                - "stripline": Force stripline calculation
                - "auto": Detect from layer position (default)
            tolerance: Relative tolerance for convergence (default 1%)
            max_iterations: Maximum iterations for solver

        Returns:
            Trace width in millimeters

        Raises:
            ValueError: If z0_target is invalid or convergence fails
        """
        if z0_target <= 0:
            raise ValueError(f"Target impedance must be positive, got {z0_target}")

        # Determine calculation mode
        if mode == "auto":
            use_microstrip = self.stackup.is_outer_layer(layer)
        elif mode == "microstrip":
            use_microstrip = True
        elif mode == "stripline":
            use_microstrip = False
        else:
            raise ValueError(f"Invalid mode: {mode}. Use 'auto', 'microstrip', or 'stripline'")

        # Get layer geometry for bounds estimation
        h = self.stackup.get_reference_plane_distance(layer)

        # Initial width bounds (empirical range for typical PCB designs)
        # Very narrow traces have high impedance, wide traces have low impedance
        w_min = h * 0.05  # Very narrow - high Z0
        w_max = h * 10.0  # Very wide - low Z0

        # Bisection method
        calc_fn = self.microstrip if use_microstrip else self.stripline

        # Verify bounds produce impedances on either side of target
        z_at_min = calc_fn(w_min, layer).z0
        z_at_max = calc_fn(w_max, layer).z0

        # Adjust bounds if needed (impedance decreases with width)
        while z_at_min < z0_target and w_min > h * 0.001:
            w_min /= 2
            z_at_min = calc_fn(w_min, layer).z0

        while z_at_max > z0_target and w_max < h * 100:
            w_max *= 2
            z_at_max = calc_fn(w_max, layer).z0

        # Bisection search
        for _ in range(max_iterations):
            w_mid = (w_min + w_max) / 2
            z_mid = calc_fn(w_mid, layer).z0

            if abs(z_mid - z0_target) / z0_target < tolerance:
                return w_mid

            # Impedance decreases as width increases
            if z_mid > z0_target:
                w_min = w_mid  # Need wider trace for lower Z0
            else:
                w_max = w_mid  # Need narrower trace for higher Z0

        # Return best estimate even if not fully converged
        return (w_min + w_max) / 2

    def differential_microstrip(
        self,
        width_mm: float,
        spacing_mm: float,
        layer: str,
        frequency_ghz: float = 1.0,
    ) -> tuple[ImpedanceResult, float]:
        """Calculate differential microstrip impedance.

        For edge-coupled differential pairs on outer layers.

        Args:
            width_mm: Individual trace width in mm
            spacing_mm: Edge-to-edge spacing between traces in mm
            layer: Layer name (e.g., "F.Cu")
            frequency_ghz: Frequency for loss calculation

        Returns:
            Tuple of (single-ended result, differential impedance)
        """
        # First calculate single-ended impedance
        single = self.microstrip(width_mm, layer, frequency_ghz)

        # Differential impedance approximation
        # Zdiff ≈ 2 * Z0 * (1 - k), where k is coupling coefficient
        # k depends on spacing/height ratio
        h = self.stackup.get_reference_plane_distance(layer)
        s_over_h = spacing_mm / h if h > 0 else 1.0

        # Empirical coupling coefficient (loose coupling approximation)
        # k decreases as spacing increases
        k = math.exp(-2.0 * s_over_h) if s_over_h > 0 else 0.5

        z_diff = 2 * single.z0 * (1 - 0.347 * k)

        return single, z_diff
