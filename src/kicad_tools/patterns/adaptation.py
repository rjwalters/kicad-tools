"""Pattern adaptation framework.

Adapts circuit patterns for specific components by loading component
requirements from the database and generating appropriate pattern parameters.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from kicad_tools.patterns.components import (
    ComponentRequirements,
    get_component_requirements,
)

if TYPE_CHECKING:
    from kicad_tools.schema.pcb import PCB


@dataclass
class AdaptedPatternParams:
    """Parameters for an adapted pattern.

    Contains all the parameters needed to instantiate a pattern
    adapted for a specific component.

    Attributes:
        pattern_type: Type of pattern (e.g., "LDO", "BuckConverter")
        component_mpn: Manufacturer part number of the main component
        parameters: Dictionary of adapted parameters
        notes: Any notes or warnings about the adaptation
    """

    pattern_type: str
    component_mpn: str
    parameters: dict[str, Any]
    notes: list[str]

    def to_dict(self) -> dict:
        """Convert to dictionary for serialization."""
        return {
            "pattern_type": self.pattern_type,
            "component_mpn": self.component_mpn,
            "parameters": self.parameters,
            "notes": self.notes,
        }


class PatternAdapter:
    """Adapts circuit patterns for specific components.

    The PatternAdapter loads component requirements from the database
    and generates appropriate pattern parameters for specific parts.

    Example:
        >>> from kicad_tools.patterns import PatternAdapter
        >>>
        >>> adapter = PatternAdapter()
        >>> params = adapter.adapt_ldo_pattern("AMS1117-3.3")
        >>> print(params.parameters)
        {'input_cap': '10uF', 'output_caps': ['22uF', '100nF'], ...}
    """

    def __init__(self) -> None:
        """Initialize the pattern adapter."""
        pass

    def adapt(
        self,
        pattern_type: str,
        component_mpn: str,
        **overrides: Any,
    ) -> AdaptedPatternParams:
        """Adapt a pattern for a specific component.

        Args:
            pattern_type: Type of pattern (e.g., "LDO", "BuckConverter")
            component_mpn: Manufacturer part number of the component
            **overrides: Override specific parameters

        Returns:
            AdaptedPatternParams with adapted parameters

        Example:
            >>> params = adapter.adapt("LDO", "AMS1117-3.3")
            >>> params = adapter.adapt("LDO", "AMS1117-3.3", input_cap="22uF")
        """
        if pattern_type.upper() == "LDO":
            return self.adapt_ldo_pattern(component_mpn, **overrides)
        elif pattern_type.upper() in ("BUCK", "BUCKCONVERTER", "BUCK_CONVERTER"):
            return self.adapt_buck_pattern(component_mpn, **overrides)
        elif pattern_type.upper() in ("DECOUPLING", "BYPASS"):
            return self.adapt_decoupling_pattern(component_mpn, **overrides)
        else:
            raise ValueError(f"Unknown pattern type: {pattern_type}")

    def adapt_ldo_pattern(
        self,
        regulator_mpn: str,
        **overrides: Any,
    ) -> AdaptedPatternParams:
        """Adapt an LDO pattern for a specific regulator.

        Loads the regulator's requirements from the component database
        and generates appropriate capacitor values, thermal requirements,
        and other parameters.

        Args:
            regulator_mpn: Manufacturer part number of the LDO
            **overrides: Override specific parameters

        Returns:
            AdaptedPatternParams with LDO-specific parameters

        Example:
            >>> params = adapter.adapt_ldo_pattern("AMS1117-3.3")
            >>> print(params.parameters["input_cap"])
            '10uF'
        """
        notes: list[str] = []

        # Get component requirements from database
        try:
            reqs = get_component_requirements(regulator_mpn)
        except KeyError:
            # Component not in database, use defaults
            notes.append(f"Component {regulator_mpn} not in database, using defaults")
            reqs = self._get_default_ldo_requirements()

        # Build parameters
        params: dict[str, Any] = {}

        # Input capacitor
        if reqs.input_cap_min_uf is not None:
            params["input_cap"] = self._format_capacitance(reqs.input_cap_min_uf)
            if reqs.input_cap_max_esr_mohm is not None:
                notes.append(f"Input cap max ESR: {reqs.input_cap_max_esr_mohm}mΩ")
        else:
            params["input_cap"] = "10uF"

        # Output capacitors
        if reqs.output_cap_min_uf is not None:
            # Add a bulk cap and a bypass cap
            bulk_cap = self._format_capacitance(reqs.output_cap_min_uf)
            params["output_caps"] = [bulk_cap, "100nF"]
            if reqs.output_cap_max_esr_mohm is not None:
                notes.append(f"Output cap max ESR: {reqs.output_cap_max_esr_mohm}mΩ")
        else:
            params["output_caps"] = ["10uF", "100nF"]

        # Thermal requirements
        if reqs.thermal_pad_required:
            params["thermal_pad_required"] = True
            notes.append("Thermal pad required for proper heat dissipation")
        else:
            params["thermal_pad_required"] = False

        # Dropout voltage (informational)
        if reqs.dropout_voltage is not None:
            params["dropout_voltage"] = reqs.dropout_voltage
            notes.append(f"Dropout voltage: {reqs.dropout_voltage}V")

        # Max output current
        if reqs.max_output_current_ma is not None:
            params["max_output_current_ma"] = reqs.max_output_current_ma

        # Apply overrides
        params.update(overrides)

        return AdaptedPatternParams(
            pattern_type="LDO",
            component_mpn=regulator_mpn,
            parameters=params,
            notes=notes,
        )

    def adapt_buck_pattern(
        self,
        regulator_mpn: str,
        **overrides: Any,
    ) -> AdaptedPatternParams:
        """Adapt a buck converter pattern for a specific regulator.

        Loads the regulator's requirements from the component database
        and generates appropriate inductor, capacitor, and diode values.

        Args:
            regulator_mpn: Manufacturer part number of the buck regulator
            **overrides: Override specific parameters

        Returns:
            AdaptedPatternParams with buck converter parameters

        Example:
            >>> params = adapter.adapt_buck_pattern("LM2596-5.0")
            >>> print(params.parameters["inductor"])
            '33uH'
        """
        notes: list[str] = []

        # Get component requirements from database
        try:
            reqs = get_component_requirements(regulator_mpn)
        except KeyError:
            notes.append(f"Component {regulator_mpn} not in database, using defaults")
            reqs = self._get_default_buck_requirements()

        params: dict[str, Any] = {}

        # Input capacitor
        if reqs.input_cap_min_uf is not None:
            params["input_cap"] = self._format_capacitance(reqs.input_cap_min_uf)
        else:
            params["input_cap"] = "100uF"

        # Output capacitor
        if reqs.output_cap_min_uf is not None:
            params["output_cap"] = self._format_capacitance(reqs.output_cap_min_uf)
        else:
            params["output_cap"] = "220uF"

        # Inductor
        if reqs.inductor_min_uh is not None:
            params["inductor"] = self._format_inductance(reqs.inductor_min_uh)
            if reqs.inductor_max_dcr_mohm is not None:
                notes.append(f"Inductor max DCR: {reqs.inductor_max_dcr_mohm}mΩ")
        else:
            params["inductor"] = "33uH"

        # Diode (for async topology)
        if reqs.diode_part_number is not None:
            params["diode"] = reqs.diode_part_number
        else:
            params["diode"] = "SS34"

        # Switching frequency
        if reqs.switching_freq_khz is not None:
            params["switching_freq_khz"] = reqs.switching_freq_khz
            notes.append(f"Switching frequency: {reqs.switching_freq_khz}kHz")

        # Apply overrides
        params.update(overrides)

        return AdaptedPatternParams(
            pattern_type="BuckConverter",
            component_mpn=regulator_mpn,
            parameters=params,
            notes=notes,
        )

    def adapt_decoupling_pattern(
        self,
        ic_mpn: str,
        **overrides: Any,
    ) -> AdaptedPatternParams:
        """Adapt a decoupling pattern for a specific IC.

        Loads the IC's requirements from the component database
        and generates appropriate decoupling capacitor values.

        Args:
            ic_mpn: Manufacturer part number of the IC
            **overrides: Override specific parameters

        Returns:
            AdaptedPatternParams with decoupling parameters

        Example:
            >>> params = adapter.adapt_decoupling_pattern("STM32F405RGT6")
            >>> print(params.parameters["capacitors"])
            ['4.7uF', '100nF', '100nF', '100nF', '100nF']
        """
        notes: list[str] = []

        # Get component requirements from database
        try:
            reqs = get_component_requirements(ic_mpn)
        except KeyError:
            notes.append(f"Component {ic_mpn} not in database, using defaults")
            reqs = self._get_default_ic_requirements()

        params: dict[str, Any] = {}

        # Decoupling capacitors
        if reqs.decoupling_caps is not None:
            params["capacitors"] = reqs.decoupling_caps
        else:
            # Default: one bulk + bypass caps per VDD pin
            num_vdd_pins = reqs.num_vdd_pins or 1
            params["capacitors"] = ["4.7uF"] + ["100nF"] * num_vdd_pins

        # Maximum distance requirement
        if reqs.max_decoupling_distance_mm is not None:
            params["max_distance_mm"] = reqs.max_decoupling_distance_mm
        else:
            params["max_distance_mm"] = 5.0

        # Apply overrides
        params.update(overrides)

        return AdaptedPatternParams(
            pattern_type="Decoupling",
            component_mpn=ic_mpn,
            parameters=params,
            notes=notes,
        )

    def adapt_from_pcb(
        self,
        pattern_type: str,
        pcb: PCB,
        main_component_ref: str,
    ) -> AdaptedPatternParams:
        """Adapt a pattern based on existing PCB component values.

        Reads the current component values from the PCB and generates
        parameters that match the existing design.

        Args:
            pattern_type: Type of pattern to adapt
            pcb: PCB containing the existing design
            main_component_ref: Reference of the main component (e.g., "U1")

        Returns:
            AdaptedPatternParams based on existing PCB values

        Example:
            >>> pcb = PCB.load("board.kicad_pcb")
            >>> params = adapter.adapt_from_pcb("LDO", pcb, "U1")
        """
        fp = pcb.get_footprint(main_component_ref)
        if not fp:
            raise ValueError(f"Component {main_component_ref} not found on PCB")

        # Try to get MPN from footprint value or properties
        mpn = fp.value or "Unknown"

        # Get adapted parameters for this component
        params = self.adapt(pattern_type, mpn)

        # Add note about source
        params.notes.append(f"Adapted from existing PCB component {main_component_ref}")

        return params

    @staticmethod
    def _format_capacitance(value_uf: float) -> str:
        """Format a capacitance value in appropriate units.

        Args:
            value_uf: Capacitance in microfarads

        Returns:
            Formatted string (e.g., "10uF", "100nF", "47pF")
        """
        if value_uf >= 1.0:
            if value_uf == int(value_uf):
                return f"{int(value_uf)}uF"
            return f"{value_uf}uF"
        elif value_uf >= 0.001:
            nf = value_uf * 1000
            if nf == int(nf):
                return f"{int(nf)}nF"
            return f"{nf}nF"
        else:
            pf = value_uf * 1_000_000
            if pf == int(pf):
                return f"{int(pf)}pF"
            return f"{pf}pF"

    @staticmethod
    def _format_inductance(value_uh: float) -> str:
        """Format an inductance value in appropriate units.

        Args:
            value_uh: Inductance in microhenries

        Returns:
            Formatted string (e.g., "33uH", "4.7uH", "100nH")
        """
        if value_uh >= 1.0:
            if value_uh == int(value_uh):
                return f"{int(value_uh)}uH"
            return f"{value_uh}uH"
        else:
            nh = value_uh * 1000
            if nh == int(nh):
                return f"{int(nh)}nH"
            return f"{nh}nH"

    @staticmethod
    def _get_default_ldo_requirements() -> ComponentRequirements:
        """Get default requirements for an unknown LDO."""
        return ComponentRequirements(
            mpn="Unknown",
            component_type="LDO",
            input_cap_min_uf=10.0,
            output_cap_min_uf=10.0,
            input_cap_max_esr_mohm=500,
            output_cap_max_esr_mohm=500,
            thermal_pad_required=False,
        )

    @staticmethod
    def _get_default_buck_requirements() -> ComponentRequirements:
        """Get default requirements for an unknown buck converter."""
        return ComponentRequirements(
            mpn="Unknown",
            component_type="BuckConverter",
            input_cap_min_uf=100.0,
            output_cap_min_uf=220.0,
            inductor_min_uh=33.0,
            switching_freq_khz=150,
        )

    @staticmethod
    def _get_default_ic_requirements() -> ComponentRequirements:
        """Get default requirements for an unknown IC."""
        return ComponentRequirements(
            mpn="Unknown",
            component_type="IC",
            num_vdd_pins=1,
            decoupling_caps=["4.7uF", "100nF"],
            max_decoupling_distance_mm=5.0,
        )
