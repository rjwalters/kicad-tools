"""Coupled transmission line analysis for differential pairs.

Provides calculations for edge-coupled and broadside-coupled transmission
lines, including differential impedance, common-mode impedance, and
coupling coefficient extraction.

Example::

    from kicad_tools.physics import Stackup
    from kicad_tools.physics.coupled_lines import CoupledLines

    stackup = Stackup.jlcpcb_4layer()
    cl = CoupledLines(stackup)

    # Analyze differential pair on top layer
    result = cl.edge_coupled_microstrip(width_mm=0.127, gap_mm=0.127, layer="F.Cu")
    print(f"Zdiff = {result.zdiff:.1f}Ω, k = {result.coupling_coefficient:.3f}")

    # Calculate gap for target differential impedance
    gap = cl.gap_for_differential_impedance(zdiff_target=90, width_mm=0.127, layer="F.Cu")
    print(f"90Ω differential requires {gap:.3f}mm gap")

Edge-coupled geometries are solved as coupled-conductor quasi-static field
problems (boundary-element method, see ``stripline.py`` and
``coupled_microstrip.py``), including copper thickness and offset stripline.
The result is checked in the test suite against independent references:

    Kirschning & Jansen, "Accurate Wide-Range Design Equations for the
    Frequency-Dependent Characteristic of Parallel Coupled Microstrip
    Lines", IEEE Trans. MTT-32(1), 1984, pp. 83-90 (zero-thickness static
    microstrip oracle).
    Cohn, "Shielded Coupled-Strip Transmission Line", IRE Trans. MTT-3(5),
    1955, pp. 29-38 (zero-thickness centred stripline oracle).
"""

from __future__ import annotations

import math
from dataclasses import dataclass

from .constants import SPEED_OF_LIGHT
from .coupled_microstrip import coupled_microstrip_modes
from .stackup import Stackup
from .stripline import coupled_stripline_impedances
from .transmission_line import TransmissionLine


@dataclass
class DifferentialPairResult:
    """Result of differential pair analysis.

    Attributes:
        zdiff: Differential-mode impedance (Ω)
        zcommon: Common-mode impedance (Ω)
        z0_even: Even-mode characteristic impedance (Ω)
        z0_odd: Odd-mode characteristic impedance (Ω)
        coupling_coefficient: Coupling factor k = (Z0e - Z0o)/(Z0e + Z0o)
        epsilon_eff_even: Effective dielectric constant for even mode
        epsilon_eff_odd: Effective dielectric constant for odd mode
    """

    zdiff: float
    zcommon: float
    z0_even: float
    z0_odd: float
    coupling_coefficient: float
    epsilon_eff_even: float
    epsilon_eff_odd: float

    @property
    def phase_velocity_even(self) -> float:
        """Even-mode phase velocity in m/s."""
        if self.epsilon_eff_even <= 0:
            return SPEED_OF_LIGHT
        return SPEED_OF_LIGHT / math.sqrt(self.epsilon_eff_even)

    @property
    def phase_velocity_odd(self) -> float:
        """Odd-mode phase velocity in m/s."""
        if self.epsilon_eff_odd <= 0:
            return SPEED_OF_LIGHT
        return SPEED_OF_LIGHT / math.sqrt(self.epsilon_eff_odd)

    def __repr__(self) -> str:
        return (
            f"DifferentialPairResult(Zdiff={self.zdiff:.1f}Ω, "
            f"Zcommon={self.zcommon:.1f}Ω, k={self.coupling_coefficient:.3f})"
        )


class CoupledLines:
    """Coupled transmission line analysis for differential pairs.

    Provides calculations for edge-coupled microstrip, edge-coupled
    stripline, and broadside-coupled stripline geometries.

    Attributes:
        stackup: PCB stackup for geometry and material properties
    """

    def __init__(self, stackup: Stackup) -> None:
        """Initialize with a stackup.

        Args:
            stackup: PCB stackup for geometry and material properties
        """
        self.stackup = stackup
        self._tl = TransmissionLine(stackup)

    def edge_coupled_microstrip(
        self,
        width_mm: float,
        gap_mm: float,
        layer: str,
    ) -> DifferentialPairResult:
        """Analyze edge-coupled microstrip pair.

        Calculates even-mode and odd-mode impedances for two parallel
        traces on an outer layer with the dielectric below and air above.

        Geometry::

            ══════  ══════  ← Two parallel traces
               ↑      ↑
              gap   width
            ────────────────  ← Ground plane

        Solved as a two-conductor quasi-static field problem (boundary
        elements with a ground-plane image and a dielectric-interface
        polarisation layer), so finite copper thickness is part of the
        solution rather than a correction.

        Args:
            width_mm: Width of each trace in mm
            gap_mm: Edge-to-edge spacing between traces in mm
            layer: Outer layer name (e.g., "F.Cu", "B.Cu")

        Returns:
            DifferentialPairResult with Zdiff, Zcommon, coupling, etc.

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

        return self._edge_coupled_microstrip_calc(width_mm, gap_mm, h, er, t)

    def _edge_coupled_microstrip_calc(
        self,
        w: float,
        s: float,
        h: float,
        er: float,
        t: float,
    ) -> DifferentialPairResult:
        """Edge-coupled microstrip calculation.

        Even and odd mode impedances come from
        :func:`~kicad_tools.physics.coupled_microstrip.coupled_microstrip_modes`
        (#7060): ``Z0m = 1 / (c * sqrt(C_m * C_m,air))`` and
        ``eps_eff_m = C_m / C_m,air``. There is no empirical coupling factor
        and no clamping; callers' solvers bound their own search ranges.

        Args:
            w: Trace width in mm
            s: Gap (spacing) in mm
            h: Dielectric height in mm
            er: Relative dielectric constant
            t: Copper thickness in mm

        Returns:
            DifferentialPairResult
        """
        z0_even, z0_odd, eps_eff_even, eps_eff_odd = coupled_microstrip_modes(w, s, h, er, t)
        return self._pair_result(z0_even, z0_odd, eps_eff_even, eps_eff_odd)

    @staticmethod
    def _pair_result(
        z0_even: float, z0_odd: float, eps_eff_even: float, eps_eff_odd: float
    ) -> DifferentialPairResult:
        """Derive Zdiff, Zcommon and k from the even and odd mode impedances."""
        return DifferentialPairResult(
            zdiff=2 * z0_odd,
            zcommon=z0_even / 2,
            z0_even=z0_even,
            z0_odd=z0_odd,
            coupling_coefficient=(z0_even - z0_odd) / (z0_even + z0_odd),
            epsilon_eff_even=eps_eff_even,
            epsilon_eff_odd=eps_eff_odd,
        )

    def edge_coupled_stripline(
        self,
        width_mm: float,
        gap_mm: float,
        layer: str,
    ) -> DifferentialPairResult:
        """Analyze edge-coupled stripline pair.

        Calculates even-mode and odd-mode impedances for two parallel
        traces sandwiched between ground planes.

        Geometry::

            ────────────────  ← Upper ground plane
               ↑
              h1
               ↓
            ══════  ══════  ← Two parallel traces
               ↑
              h2
               ↓
            ────────────────  ← Lower ground plane

        Args:
            width_mm: Width of each trace in mm
            gap_mm: Edge-to-edge spacing between traces in mm
            layer: Inner layer name (e.g., "In1.Cu", "In2.Cu")

        Returns:
            DifferentialPairResult with Zdiff, Zcommon, coupling, etc.

        Raises:
            ValueError: If width or gap is non-positive
        """
        if width_mm <= 0:
            raise ValueError(f"Trace width must be positive, got {width_mm}")
        if gap_mm <= 0:
            raise ValueError(f"Gap must be positive, got {gap_mm}")

        h1, h2 = self.stackup.get_stripline_geometry(layer)
        if h1 <= 0 or h2 <= 0:
            raise ValueError(f"Could not determine geometry for layer {layer}")

        er = self.stackup.get_dielectric_constant(layer)
        t = self.stackup.get_copper_thickness(layer)

        return self._edge_coupled_stripline_calc(width_mm, gap_mm, h1, h2, er, t)

    def _edge_coupled_stripline_calc(
        self,
        w: float,
        s: float,
        h1: float,
        h2: float,
        er: float,
        t: float,
    ) -> DifferentialPairResult:
        """Edge-coupled stripline calculation.

        Even and odd mode impedances come from a two-strip boundary-element
        solve in a homogeneous dielectric (see
        :func:`~kicad_tools.physics.stripline.coupled_stripline_impedances`),
        so finite copper thickness and offset (``h1 != h2``) geometries are
        part of the solution. No empirical coupling factor or clamp (#7060).

        Args:
            w: Trace width in mm
            s: Gap (spacing) in mm
            h1: Distance to upper plane in mm
            h2: Distance to lower plane in mm
            er: Relative dielectric constant
            t: Copper thickness in mm

        Returns:
            DifferentialPairResult
        """
        z0_even, z0_odd = coupled_stripline_impedances(w, s, h1, h2, t, er)
        return self._pair_result(z0_even, z0_odd, er, er)

    def broadside_coupled_stripline(
        self,
        width_mm: float,
        layer1: str,
        layer2: str,
    ) -> DifferentialPairResult:
        """Analyze broadside-coupled stripline pair.

        Broadside coupling occurs when traces are on different layers,
        stacked vertically. The gap is determined by the dielectric
        thickness between the layers.

        Geometry::

            ────────────────  ← Upper ground plane
                 ↑
                h1
                 ↓
               ══════       ← Trace on layer1
                 ↑
                gap (dielectric between layers)
                 ↓
               ══════       ← Trace on layer2
                 ↑
                h2
                 ↓
            ────────────────  ← Lower ground plane

        Args:
            width_mm: Width of each trace in mm
            layer1: First copper layer name
            layer2: Second copper layer name

        Returns:
            DifferentialPairResult with Zdiff, Zcommon, coupling, etc.

        Raises:
            ValueError: If width is non-positive or layers invalid
        """
        if width_mm <= 0:
            raise ValueError(f"Trace width must be positive, got {width_mm}")

        # Get layer positions to calculate vertical gap
        idx1 = self.stackup.get_layer_index(layer1)
        idx2 = self.stackup.get_layer_index(layer2)

        if idx1 < 0 or idx2 < 0:
            raise ValueError(f"Could not find layers {layer1} and/or {layer2}")

        # Ensure idx1 < idx2 (layer1 is above layer2)
        if idx1 > idx2:
            idx1, idx2 = idx2, idx1
            layer1, layer2 = layer2, layer1

        # Calculate gap (dielectric thickness between layers)
        gap = 0.0
        for i in range(idx1 + 1, idx2):
            layer = self.stackup.layers[i]
            if layer.is_dielectric:
                gap += layer.thickness_mm

        if gap <= 0:
            raise ValueError(f"No dielectric found between {layer1} and {layer2}")

        # Get average dielectric constant between layers
        er_values = []
        for i in range(idx1 + 1, idx2):
            layer = self.stackup.layers[i]
            if layer.is_dielectric and layer.epsilon_r > 0:
                er_values.append(layer.epsilon_r)

        er = sum(er_values) / len(er_values) if er_values else 4.5

        # Get heights to reference planes
        h1_above = self.stackup.get_dielectric_height(layer1)
        h2_below = self.stackup.get_dielectric_height(layer2)

        t1 = self.stackup.get_copper_thickness(layer1)
        t2 = self.stackup.get_copper_thickness(layer2)
        t_avg = (t1 + t2) / 2

        return self._broadside_coupled_calc(width_mm, gap, h1_above, h2_below, er, t_avg)

    def _broadside_coupled_calc(
        self,
        w: float,
        gap: float,
        h1: float,
        h2: float,
        er: float,
        t: float,
    ) -> DifferentialPairResult:
        """Broadside-coupled stripline calculation.

        Args:
            w: Trace width in mm
            gap: Vertical gap between traces in mm
            h1: Distance from upper trace to upper ground in mm
            h2: Distance from lower trace to lower ground in mm
            er: Relative dielectric constant
            t: Average copper thickness in mm

        Returns:
            DifferentialPairResult
        """
        # Total height
        b = h1 + gap + h2 + 2 * t

        # For stripline, epsilon_eff = er
        eps_eff = er

        # Broadside coupling is typically stronger than edge coupling
        # at the same effective spacing
        gap_over_w = gap / w if w > 0 else 1.0

        # Effective single-ended impedance
        w_eff = w + (t / math.pi) * (1 + math.log(2 * h1 / t)) if t > 0 and h1 > 0 else w
        denom = 0.67 * math.pi * (0.8 * w_eff + t)
        if denom > 0 and b > 0:
            z0_single = (60 / math.sqrt(er)) * math.log(4 * b / denom)
        else:
            z0_single = 50.0

        # Coupling is stronger for broadside (same x position)
        # k ≈ exp(-pi * gap / w) for broadside coupling
        k_approx = math.exp(-math.pi * gap_over_w)

        # Even and odd mode impedances from coupling coefficient
        # Z0e = Z0 * sqrt((1+k)/(1-k)), Z0o = Z0 * sqrt((1-k)/(1+k))
        if k_approx < 0.999:
            z0_even = z0_single * math.sqrt((1 + k_approx) / (1 - k_approx))
            z0_odd = z0_single * math.sqrt((1 - k_approx) / (1 + k_approx))
        else:
            # Very tight coupling
            z0_even = z0_single * 2.0
            z0_odd = z0_single * 0.5

        # Derived quantities
        zdiff = 2 * z0_odd
        zcommon = z0_even / 2
        k = (z0_even - z0_odd) / (z0_even + z0_odd)

        return DifferentialPairResult(
            zdiff=zdiff,
            zcommon=zcommon,
            z0_even=z0_even,
            z0_odd=z0_odd,
            coupling_coefficient=k,
            epsilon_eff_even=eps_eff,
            epsilon_eff_odd=eps_eff,
        )

    def width_for_differential_impedance(
        self,
        zdiff_target: float,
        gap_mm: float,
        layer: str,
        tolerance: float = 0.001,
        max_iterations: int = 60,
    ) -> float:
        """Solve trace width at a fixed authored edge-to-edge pair gap.

        Unlike sizing a single-ended line at half the differential target,
        this preserves the specified coupling geometry. Layer position selects
        microstrip or stripline. Bounds scale with the reference-plane height.
        """
        if zdiff_target <= 0 or gap_mm <= 0:
            raise ValueError("Differential target and pair gap must be positive")
        calc = (
            self.edge_coupled_microstrip
            if self.stackup.is_outer_layer(layer)
            else self.edge_coupled_stripline
        )
        height = self.stackup.get_reference_plane_distance(layer)
        lo, hi = height * 0.001, height * 50
        if not calc(hi, gap_mm, layer).zdiff <= zdiff_target <= calc(lo, gap_mm, layer).zdiff:
            raise ValueError("Differential target is outside fixed-gap width bounds")
        for _ in range(max_iterations):
            mid = (lo + hi) / 2
            actual = calc(mid, gap_mm, layer).zdiff
            if abs(actual - zdiff_target) <= tolerance * zdiff_target:
                return mid
            if actual > zdiff_target:
                lo = mid
            else:
                hi = mid
        raise ValueError("Fixed-gap differential width solver did not converge")

    def gap_for_differential_impedance(
        self,
        zdiff_target: float,
        width_mm: float,
        layer: str,
        mode: str = "edge_microstrip",
        tolerance: float = 0.02,
        max_iterations: int = 50,
    ) -> float:
        """Calculate gap for target differential impedance.

        Uses bisection method to find the gap that produces the target
        differential impedance for the specified geometry.

        Args:
            zdiff_target: Target differential impedance (commonly 90Ω or 100Ω)
            width_mm: Fixed trace width in mm
            layer: Layer name
            mode: Coupling mode:
                - "edge_microstrip": Edge-coupled microstrip (outer layers)
                - "edge_stripline": Edge-coupled stripline (inner layers)
                - "auto": Detect from layer position
            tolerance: Relative tolerance for convergence (default 2%)
            max_iterations: Maximum iterations for solver

        Returns:
            Required gap in mm

        Raises:
            ValueError: If parameters are invalid or convergence fails
        """
        if zdiff_target <= 0:
            raise ValueError(f"Target Zdiff must be positive, got {zdiff_target}")
        if width_mm <= 0:
            raise ValueError(f"Width must be positive, got {width_mm}")

        # Determine calculation mode
        if mode == "auto":
            use_microstrip = self.stackup.is_outer_layer(layer)
        elif mode == "edge_microstrip":
            use_microstrip = True
        elif mode == "edge_stripline":
            use_microstrip = False
        else:
            raise ValueError(
                f"Invalid mode: {mode}. Use 'auto', 'edge_microstrip', or 'edge_stripline'"
            )

        # Select calculation function
        calc_fn = self.edge_coupled_microstrip if use_microstrip else self.edge_coupled_stripline

        # Get reference height for bounds
        h = self.stackup.get_reference_plane_distance(layer)

        # Initial gap bounds
        # Very tight gap (high coupling) → lower Zdiff
        # Very loose gap (low coupling) → higher Zdiff (approaches 2*Z0_single)
        gap_min = h * 0.05  # Very tight
        gap_max = h * 5.0  # Very loose

        # Verify bounds bracket the target
        zdiff_at_min = calc_fn(width_mm, gap_min, layer).zdiff
        zdiff_at_max = calc_fn(width_mm, gap_max, layer).zdiff

        # Adjust bounds if needed
        # Tighter gap → lower Zdiff (more coupling)
        # Looser gap → higher Zdiff (less coupling)
        while zdiff_at_min > zdiff_target and gap_min > h * 0.001:
            gap_min /= 2
            zdiff_at_min = calc_fn(width_mm, gap_min, layer).zdiff

        while zdiff_at_max < zdiff_target and gap_max < h * 50:
            gap_max *= 2
            zdiff_at_max = calc_fn(width_mm, gap_max, layer).zdiff

        # Bisection search
        for _ in range(max_iterations):
            gap_mid = (gap_min + gap_max) / 2
            zdiff_mid = calc_fn(width_mm, gap_mid, layer).zdiff

            if abs(zdiff_mid - zdiff_target) / zdiff_target < tolerance:
                return gap_mid

            # Zdiff increases as gap increases (less coupling)
            if zdiff_mid < zdiff_target:
                gap_min = gap_mid  # Need wider gap for higher Zdiff
            else:
                gap_max = gap_mid  # Need tighter gap for lower Zdiff

        # Return best estimate
        return (gap_min + gap_max) / 2
