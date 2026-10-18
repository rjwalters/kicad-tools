"""Transmission line impedance calculations.

Provides analytical microstrip/CPWG calculations and a quasi-static boundary-
element stripline calculation with finite conductor thickness.

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

    # Calculate CPWG impedance
    result = tl.cpwg(width_mm=0.25, gap_mm=0.15, layer="F.Cu")
    print(f"CPWG Z0 = {result.z0:.1f}Ω")

References:
    Hammerstad & Jensen, "Accurate Models for Microstrip Computer-Aided Design",
    MTT-S International Microwave Symposium Digest, 1980.
    Ghione & Naldi, "Analytical Formulas for Coplanar Lines in Hybrid and
    Monolithic MICs", Electronics Letters, 1984.
    IPC-2141A Section 4.2, 4.4
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import COPPER_CONDUCTIVITY, SPEED_OF_LIGHT
from .stackup import Stackup
from .stripline import stripline_impedance


def _elliptic_k(k: float, tolerance: float = 1e-12) -> float:
    """Compute complete elliptic integral of the first kind K(k).

    Uses the arithmetic-geometric mean (AGM) algorithm, which converges
    extremely fast (typically 4-5 iterations for machine precision).

    Args:
        k: Elliptic modulus (0 <= k < 1)
        tolerance: Convergence tolerance

    Returns:
        K(k) = integral from 0 to pi/2 of 1/sqrt(1 - k^2*sin^2(t)) dt

    Note:
        For k >= 1, returns inf. For k < 0, uses symmetry K(-k) = K(k).
    """
    k = abs(k)
    if k >= 1.0:
        return float("inf")
    if k == 0.0:
        return math.pi / 2

    # AGM algorithm: K(k) = pi / (2 * AGM(1, k'))
    # where k' = sqrt(1 - k^2) is the complementary modulus
    a = 1.0
    b = math.sqrt(1 - k * k)  # k' = sqrt(1 - k^2)

    while abs(a - b) > tolerance:
        a, b = (a + b) / 2, math.sqrt(a * b)

    return math.pi / (2 * a)


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
        including their finite-thickness terms; Z0 agrees with the published
        Hammerstad-Jensen (1980) model to within 0.2% for typical PCB geometries.

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

        def _z01(u_: float) -> float:
            """Air-filled microstrip impedance (Hammerstad-Jensen)."""
            f_u = 6 + (2 * math.pi - 6) * math.exp(-((30.666 / u_) ** 0.7528))
            return 60 * math.log(f_u / u_ + math.sqrt(1 + (2 / u_) ** 2))

        u = w / h

        # Finite copper thickness (Hammerstad-Jensen 1980): a width increment
        # du1 for the air-filled line and a smaller du_r in the dielectric.
        du1 = 0.0
        du_r = 0.0
        if t > 0 and h > 0:
            t_h = t / h
            coth = 1 / math.tanh(math.sqrt(6.517 * u))
            du1 = (t_h / math.pi) * math.log(1 + 4 * math.e / (t_h * coth**2))
            du_r = 0.5 * (1 + 1 / math.cosh(math.sqrt(max(er - 1, 0.0)))) * du1
        u_1 = u + du1
        u_r = u + du_r

        # Effective dielectric constant (Hammerstad-Jensen) at the dielectric width
        a = (
            1
            + (1 / 49) * math.log((u_r**4 + (u_r / 52) ** 2) / (u_r**4 + 0.432))
            + (1 / 18.7) * math.log(1 + (u_r / 18.1) ** 3)
        )

        b = 0.564 * ((er - 0.9) / (er + 3)) ** 0.053

        eps_eff = (er + 1) / 2 + ((er - 1) / 2) * (1 + 10 / u_r) ** (-a * b)

        # Characteristic impedance from the dielectric width
        z0 = _z01(u_r) / math.sqrt(eps_eff)

        # Thickness correction to eps_eff (Hammerstad-Jensen 1980)
        if du1:
            eps_eff *= (_z01(u_1) / _z01(u_r)) ** 2

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

        Solves the finite conductor cross-section between both reference planes.
        The homogeneous dielectric approximation uses the stackup's Er.

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

        z0 = stripline_impedance(w, h1, h2, t, er)

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
        # alpha_d = pi * f * er * q * tan_d / (sqrt(eps_eff) * c)  (Pozar eq. 3.198)
        # where q is the filling factor
        q = (eps_eff - 1) / (er - 1) if er > 1 else 0.5
        alpha_d = math.pi * freq_hz * er * q * tan_d / (math.sqrt(eps_eff) * SPEED_OF_LIGHT)
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

        For edge-coupled differential pairs on outer layers. The
        differential impedance is that of
        :meth:`CoupledLines.edge_coupled_microstrip`.

        Args:
            width_mm: Individual trace width in mm
            spacing_mm: Edge-to-edge spacing between traces in mm
            layer: Layer name (e.g., "F.Cu")
            frequency_ghz: Frequency for loss calculation

        Returns:
            Tuple of (single-ended result, differential impedance)
        """
        # Imported here because coupled_lines imports this module.
        from .coupled_lines import CoupledLines

        single = self.microstrip(width_mm, layer, frequency_ghz)
        # One coupled-microstrip model: delegate to the field solve (#7060).
        z_diff = (
            CoupledLines(self.stackup).edge_coupled_microstrip(width_mm, spacing_mm, layer).zdiff
        )

        return single, z_diff

    def cpwg(
        self,
        width_mm: float,
        gap_mm: float,
        layer: str,
        frequency_ghz: float = 1.0,
    ) -> ImpedanceResult:
        """Calculate coplanar waveguide with ground (CPWG) impedance.

        CPWG geometry::

              gap   width   gap
          ═══════╗ ═══════ ╔═══════  ← Signal trace with coplanar ground
                 ╚═════════╝
          ─────────────────────────  ← Ground plane below

        CPWG provides better isolation than microstrip and is common
        for RF designs. The coplanar ground conductors on either side
        of the signal trace improve shielding and reduce crosstalk.

        Uses Ghione-Naldi conformal mapping analysis for accurate
        impedance calculation.

        Args:
            width_mm: Center conductor width in millimeters
            gap_mm: Gap between conductor and coplanar ground in mm
            layer: Layer name (e.g., "F.Cu", "B.Cu")
            frequency_ghz: Frequency in GHz for loss calculation

        Returns:
            ImpedanceResult with Z0, effective epsilon, loss, and velocity

        Raises:
            ValueError: If width or gap is non-positive
        """
        if width_mm <= 0:
            raise ValueError(f"Trace width must be positive, got {width_mm}")
        if gap_mm <= 0:
            raise ValueError(f"Gap must be positive, got {gap_mm}")

        h = self.stackup.get_reference_plane_distance(layer)
        if h <= 0:
            raise ValueError(f"Could not determine dielectric height for layer {layer}")

        er = self.stackup.get_dielectric_constant(layer)
        t = self.stackup.get_copper_thickness(layer)
        tan_d = self.stackup.get_loss_tangent(layer)

        return self._cpwg_calc(width_mm, gap_mm, h, er, t, tan_d, frequency_ghz)

    def _cpwg_calc(
        self,
        w: float,
        g: float,
        h: float,
        er: float,
        t: float,
        tan_d: float,
        freq_ghz: float,
    ) -> ImpedanceResult:
        """CPWG impedance using Ghione-Naldi conformal mapping.

        Args:
            w: Center conductor width in mm
            g: Gap to coplanar ground in mm
            h: Height to bottom ground plane in mm
            er: Relative dielectric constant
            t: Copper thickness in mm
            tan_d: Dielectric loss tangent
            freq_ghz: Frequency in GHz

        Returns:
            ImpedanceResult
        """
        # Effective width correction for finite copper thickness
        # (similar to microstrip thickness correction)
        if t > 0:
            delta_w = (1.25 * t / math.pi) * (1 + math.log(4 * math.pi * w / t))
            w_eff = w + delta_w
            # Adjust gap accordingly
            g_eff = max(g - delta_w / 2, g * 0.5)  # Don't let gap go negative
        else:
            w_eff = w
            g_eff = g

        # Geometry parameters for elliptic integral arguments
        # a = w/2 (half-width of center conductor)
        # b = w/2 + g (half-distance to edge of coplanar ground)
        a = w_eff / 2
        b = w_eff / 2 + g_eff

        # k0: modulus for air-dielectric interface
        # k0 = a / b
        k0 = a / b
        k0_prime = math.sqrt(1 - k0 * k0)

        # k3: modulus accounting for the bottom ground plane (conductor-backed
        # CPW, Ghione & Naldi 1987). tanh, not sinh: the back conductor sits
        # below the dielectric, so the strip-to-ground mapping is a tanh.
        tanh_b = math.tanh(math.pi * b / (2 * h))
        k3 = math.tanh(math.pi * a / (2 * h)) / tanh_b if tanh_b > 0 else k0
        k3_prime = math.sqrt(1 - k3 * k3)

        # Complete elliptic integrals
        K_k0 = _elliptic_k(k0)
        K_k0_prime = _elliptic_k(k0_prime)
        K_k3 = _elliptic_k(k3)
        K_k3_prime = _elliptic_k(k3_prime)

        if not all(math.isfinite(v) and v > 0 for v in (K_k0, K_k0_prime, K_k3, K_k3_prime)):
            raise ValueError(
                f"CPWG geometry out of range for closed-form model "
                f"(w={w}, g={g}, h={h}): elliptic integrals are degenerate"
            )

        # Effective dielectric constant (conductor-backed CPW)
        # r = K(k0') K(k3) / (K(k0) K(k3')); eps_eff = (1 + er*r) / (1 + r)
        r = (K_k0_prime * K_k3) / (K_k0 * K_k3_prime)
        eps_eff = (1 + er * r) / (1 + r)

        # Characteristic impedance
        # Z0 = (60*pi / sqrt(eps_eff)) / (K(k0)/K(k0') + K(k3)/K(k3'))
        z0 = (60 * math.pi / math.sqrt(eps_eff)) / (K_k0 / K_k0_prime + K_k3 / K_k3_prime)

        # Phase velocity
        v_p = SPEED_OF_LIGHT / math.sqrt(eps_eff)

        # Loss estimation
        loss = self._estimate_cpwg_loss(w, g, h, er, t, eps_eff, z0, tan_d, freq_ghz)

        return ImpedanceResult(
            z0=z0,
            epsilon_eff=eps_eff,
            loss_db_per_m=loss,
            phase_velocity=v_p,
        )

    def _estimate_cpwg_loss(
        self,
        w: float,
        g: float,
        h: float,
        er: float,
        t: float,
        eps_eff: float,
        z0: float,
        tan_d: float,
        freq_ghz: float,
    ) -> float:
        """Estimate CPWG loss in dB/m.

        CPWG has similar loss mechanisms to microstrip, but the current
        distribution is different due to the coplanar grounds.

        Args:
            w: Trace width in mm
            g: Gap width in mm
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
        mu0 = 4 * math.pi * 1e-7
        rs = math.sqrt(math.pi * freq_hz * mu0 / COPPER_CONDUCTIVITY)

        # For CPWG, current flows on center conductor and coplanar grounds
        # Effective width is larger due to ground contributions
        w_eff_m = (w + 2 * g) / 1000  # Total CPW cross-section in meters
        if w_eff_m > 0 and z0 > 0:
            # Approximate formula - CPWG has higher conductor loss than microstrip
            # due to current crowding at edges
            alpha_c = 1.5 * rs / (z0 * w_eff_m)  # Np/m, factor 1.5 for edge effects
            alpha_c_db = alpha_c * 8.686  # dB/m
        else:
            alpha_c_db = 0

        # Dielectric loss
        # Similar to microstrip with filling factor
        q = (eps_eff - 1) / (er - 1) if er > 1 else 0.5
        alpha_d = math.pi * freq_hz * er * q * tan_d / (math.sqrt(eps_eff) * SPEED_OF_LIGHT)
        alpha_d_db = alpha_d * 8.686  # Np/m to dB/m

        return alpha_c_db + alpha_d_db

    def cpwg_geometry_for_impedance(
        self,
        z0_target: float,
        layer: str,
        width_mm: float | None = None,
        gap_mm: float | None = None,
        tolerance: float = 0.01,
        max_iterations: int = 50,
    ) -> tuple[float, float]:
        """Calculate CPWG geometry for target impedance.

        Given a target impedance, calculate the required width and gap.
        You can specify either width or gap, and the other will be calculated.
        If neither is specified, optimizes for a balanced geometry.

        Args:
            z0_target: Target impedance in ohms
            layer: Layer name (e.g., "F.Cu")
            width_mm: If specified, calculate gap for this width
            gap_mm: If specified, calculate width for this gap
            tolerance: Relative tolerance for convergence
            max_iterations: Maximum iterations for solver

        Returns:
            Tuple of (width_mm, gap_mm) that produces target impedance

        Raises:
            ValueError: If both width and gap are specified, or if target is invalid
        """
        if z0_target <= 0:
            raise ValueError(f"Target impedance must be positive, got {z0_target}")
        if width_mm is not None and gap_mm is not None:
            raise ValueError("Specify either width_mm or gap_mm, not both")

        h = self.stackup.get_reference_plane_distance(layer)

        if width_mm is not None:
            # Fixed width, solve for gap
            if width_mm <= 0:
                raise ValueError(f"Width must be positive, got {width_mm}")
            return self._solve_cpwg_gap(z0_target, width_mm, layer, h, tolerance, max_iterations)
        elif gap_mm is not None:
            # Fixed gap, solve for width
            if gap_mm <= 0:
                raise ValueError(f"Gap must be positive, got {gap_mm}")
            return self._solve_cpwg_width(z0_target, gap_mm, layer, h, tolerance, max_iterations)
        else:
            # Neither specified - use balanced geometry (gap ≈ width)
            # Start with typical 50Ω CPWG dimensions and iterate
            return self._solve_cpwg_balanced(z0_target, layer, h, tolerance, max_iterations)

    def _solve_cpwg_gap(
        self,
        z0_target: float,
        width_mm: float,
        layer: str,
        h: float,
        tolerance: float,
        max_iterations: int,
    ) -> tuple[float, float]:
        """Solve for CPWG gap given fixed width."""
        # Gap bounds - empirical range
        g_min = width_mm * 0.1
        g_max = width_mm * 5.0

        # Verify bounds (wider gap = higher impedance)
        z_at_min = self.cpwg(width_mm, g_min, layer).z0
        z_at_max = self.cpwg(width_mm, g_max, layer).z0

        # Adjust bounds if needed
        while z_at_min > z0_target and g_min > h * 0.01:
            g_min /= 2
            z_at_min = self.cpwg(width_mm, g_min, layer).z0

        while z_at_max < z0_target and g_max < h * 10:
            g_max *= 2
            z_at_max = self.cpwg(width_mm, g_max, layer).z0

        # Bisection
        for _ in range(max_iterations):
            g_mid = (g_min + g_max) / 2
            z_mid = self.cpwg(width_mm, g_mid, layer).z0

            if abs(z_mid - z0_target) / z0_target < tolerance:
                return (width_mm, g_mid)

            # Impedance increases with gap
            if z_mid < z0_target:
                g_min = g_mid
            else:
                g_max = g_mid

        return (width_mm, (g_min + g_max) / 2)

    def _solve_cpwg_width(
        self,
        z0_target: float,
        gap_mm: float,
        layer: str,
        h: float,
        tolerance: float,
        max_iterations: int,
    ) -> tuple[float, float]:
        """Solve for CPWG width given fixed gap."""
        # Width bounds - empirical range
        w_min = gap_mm * 0.2
        w_max = gap_mm * 10.0

        # Verify bounds (wider trace = lower impedance)
        z_at_min = self.cpwg(w_min, gap_mm, layer).z0
        z_at_max = self.cpwg(w_max, gap_mm, layer).z0

        # Adjust bounds if needed
        while z_at_min < z0_target and w_min > h * 0.01:
            w_min /= 2
            z_at_min = self.cpwg(w_min, gap_mm, layer).z0

        while z_at_max > z0_target and w_max < h * 10:
            w_max *= 2
            z_at_max = self.cpwg(w_max, gap_mm, layer).z0

        # Bisection
        for _ in range(max_iterations):
            w_mid = (w_min + w_max) / 2
            z_mid = self.cpwg(w_mid, gap_mm, layer).z0

            if abs(z_mid - z0_target) / z0_target < tolerance:
                return (w_mid, gap_mm)

            # Impedance decreases with width
            if z_mid > z0_target:
                w_min = w_mid
            else:
                w_max = w_mid

        return ((w_min + w_max) / 2, gap_mm)

    def _solve_cpwg_balanced(
        self,
        z0_target: float,
        layer: str,
        h: float,
        tolerance: float,
        max_iterations: int,
    ) -> tuple[float, float]:
        """Solve for balanced CPWG geometry (gap == width).

        Z0(w, w) is not monotonic for w well below h (it peaks near w ~ h/2
        and then falls off), so bisect only on the monotonically decreasing
        branch w >= h. Targets outside that branch raise ValueError rather
        than returning a degenerate micron-scale geometry.
        """
        w_lo = h
        w_hi = h * 20.0
        z_lo = self.cpwg(w_lo, w_lo, layer).z0
        z_hi = self.cpwg(w_hi, w_hi, layer).z0
        if not (z_hi <= z0_target <= z_lo):
            raise ValueError(
                f"No balanced CPWG geometry (gap == width) on {layer} gives "
                f"{z0_target} ohm; reachable range is {z_hi:.1f}-{z_lo:.1f} ohm "
                f"for w in [{w_lo:.3f}, {w_hi:.3f}] mm"
            )

        for _ in range(max_iterations):
            w_mid = (w_lo + w_hi) / 2
            z_mid = self.cpwg(w_mid, w_mid, layer).z0
            if abs(z_mid - z0_target) / z0_target < tolerance:
                return (w_mid, w_mid)
            # Impedance decreases with width on this branch
            if z_mid > z0_target:
                w_lo = w_mid
            else:
                w_hi = w_mid

        w_mid = (w_lo + w_hi) / 2
        return (w_mid, w_mid)
