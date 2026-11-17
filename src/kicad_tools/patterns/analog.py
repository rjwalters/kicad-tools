"""
Analog circuit patterns.

This module provides PCB patterns for common analog circuits including
ADC input filtering, op-amp configurations, sensor interfaces, and
DAC output filtering.

Example::

    from kicad_tools.patterns.analog import ADCInputFilter, OpAmpCircuit

    # Create an anti-aliasing filter for 16-bit ADC at 100kHz
    adc_filter = ADCInputFilter(cutoff_hz=50000, order=2, topology="active")
    rules = adc_filter.get_placement_rules()

    # Non-inverting op-amp with gain of 10
    opamp = OpAmpCircuit(topology="non_inverting", gain=10.0)
    constraints = opamp.derive_constraints(["OPAMP_IN", "OPAMP_OUT"])
"""

from __future__ import annotations

from typing import Any, Literal

from kicad_tools.intent import Constraint, InterfaceCategory

from .constraints import (
    ConstraintPlacementRule,
    ConstraintPriority,
    ConstraintRoutingRule,
    IntentPattern,
)


class ADCInputFilter(IntentPattern):
    """Anti-aliasing filter pattern for ADC inputs.

    Provides placement and routing rules for ADC input filtering circuits.
    Supports both passive RC filters and active (op-amp based) filters.

    Attributes:
        cutoff_hz: Filter cutoff frequency in Hz.
        order: Filter order (1 for single-pole, 2 for two-pole).
        topology: Filter topology ("rc" or "active").
    """

    def __init__(
        self,
        cutoff_hz: float,
        order: int = 2,
        topology: Literal["rc", "active"] = "rc",
    ) -> None:
        """Initialize ADC input filter pattern.

        Args:
            cutoff_hz: Filter cutoff frequency in Hz (typically fs/2 for Nyquist).
            order: Filter order (1 or 2). Higher orders need multiple stages.
            topology: Filter topology:
                - "rc": Passive RC filter, simple but limited performance
                - "active": Active filter with op-amp, better performance

        Raises:
            ValueError: If parameters are out of valid range.
        """
        if cutoff_hz <= 0:
            raise ValueError(f"cutoff_hz must be positive, got {cutoff_hz}")
        if order not in (1, 2):
            raise ValueError(f"order must be 1 or 2, got {order}")
        if topology not in ("rc", "active"):
            raise ValueError(f"topology must be 'rc' or 'active', got {topology}")

        self._cutoff_hz = cutoff_hz
        self._order = order
        self._topology = topology

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"adc_filter_{self._topology}_order{self._order}"

    @property
    def category(self) -> InterfaceCategory:
        """Return SINGLE_ENDED category for analog signals."""
        return InterfaceCategory.SINGLE_ENDED

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return ADC filter placement rules.

        Returns:
            List of placement rules for filter layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="filter_near_adc",
                description="Place filter components close to ADC input pin",
                priority=ConstraintPriority.CRITICAL,
                params={"max_distance_mm": 10.0},
            ),
            ConstraintPlacementRule(
                name="caps_close_together",
                description="Keep filter capacitors close together for consistent ground reference",
                priority=ConstraintPriority.RECOMMENDED,
                params={"max_spacing_mm": 5.0},
            ),
        ]

        if self._topology == "active":
            rules.extend(
                [
                    ConstraintPlacementRule(
                        name="opamp_decoupling",
                        description="Place op-amp decoupling caps within 3mm of power pins",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["C_VCC", "C_VEE"],
                        params={"max_distance_mm": 3.0},
                    ),
                    ConstraintPlacementRule(
                        name="feedback_short",
                        description="Keep feedback resistor close to op-amp inverting input",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["R_FB"],
                        params={"max_distance_mm": 5.0},
                    ),
                ]
            )

        # High-impedance input handling
        if self._cutoff_hz < 1000:  # Low frequency = likely high impedance
            rules.append(
                ConstraintPlacementRule(
                    name="guard_ring",
                    description="Consider guard ring around high-impedance input traces",
                    priority=ConstraintPriority.RECOMMENDED,
                    params={"high_impedance": True},
                )
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return ADC filter routing rules.

        Returns:
            List of routing rules for filter traces.
        """
        rules = [
            ConstraintRoutingRule(
                name="short_input_trace",
                description="Keep input trace to filter as short as possible",
                net_pattern="ADC_IN*",
                params={"max_mm": 25.0},
            ),
            ConstraintRoutingRule(
                name="star_ground",
                description="Use star ground topology for filter ground connections",
                net_pattern="GND*",
                params={"star_ground": True, "avoid_ground_loops": True},
            ),
            ConstraintRoutingRule(
                name="separate_analog_digital",
                description="Keep analog filter traces away from digital signals",
                net_pattern="ADC_*",
                params={"min_spacing_mm": 2.0, "avoid_digital": True},
            ),
        ]

        if self._topology == "active":
            rules.append(
                ConstraintRoutingRule(
                    name="minimize_input_capacitance",
                    description="Route op-amp inputs with minimum trace length/width",
                    net_pattern="OPAMP_IN*",
                    params={"max_mm": 10.0, "min_width": True},
                )
            )

        return rules

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate ADC filter configuration.

        Args:
            **kwargs: Validation parameters:
                - sample_rate_hz: ADC sample rate for Nyquist check

        Returns:
            List of validation error messages.
        """
        errors = []

        sample_rate = kwargs.get("sample_rate_hz")
        if sample_rate is not None:
            nyquist = sample_rate / 2
            if self._cutoff_hz > nyquist:
                errors.append(
                    f"Filter cutoff {self._cutoff_hz}Hz exceeds Nyquist frequency "
                    f"{nyquist}Hz for sample rate {sample_rate}Hz"
                )
            # Recommended: cutoff at 0.4-0.45 * fs for good margin
            if self._cutoff_hz > 0.45 * sample_rate:
                errors.append(
                    f"Filter cutoff {self._cutoff_hz}Hz is close to Nyquist; "
                    f"recommend ≤{0.45 * sample_rate}Hz for adequate attenuation"
                )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from ADC filter pattern.

        Args:
            nets: List of filter net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        # Analog separation constraint
        constraints.append(
            Constraint(
                type="analog_separation",
                params={"nets": nets, "min_spacing_mm": 2.0},
                source=source,
                severity="warning",
            )
        )

        # Short trace length for analog
        constraints.append(
            Constraint(
                type="max_length",
                params={"nets": nets, "max_mm": 50.0},
                source=source,
                severity="warning",
            )
        )

        return constraints


class OpAmpCircuit(IntentPattern):
    """Op-amp circuit pattern.

    Provides placement and routing rules for common op-amp configurations
    including inverting, non-inverting, buffer, and differential.

    Attributes:
        topology: Op-amp topology.
        gain: Circuit gain (for inverting/non-inverting).
    """

    TOPOLOGIES = ("inverting", "non_inverting", "buffer", "differential")

    def __init__(
        self,
        topology: Literal["inverting", "non_inverting", "buffer", "differential"],
        gain: float = 1.0,
    ) -> None:
        """Initialize op-amp circuit pattern.

        Args:
            topology: Op-amp configuration:
                - "inverting": Inverting amplifier
                - "non_inverting": Non-inverting amplifier
                - "buffer": Unity-gain buffer (voltage follower)
                - "differential": Differential amplifier
            gain: Circuit gain. Ignored for buffer topology.

        Raises:
            ValueError: If topology is invalid or gain is non-positive.
        """
        if topology not in self.TOPOLOGIES:
            valid = ", ".join(self.TOPOLOGIES)
            raise ValueError(f"Invalid topology '{topology}'. Valid: {valid}")
        if gain <= 0 and topology != "buffer":
            raise ValueError(f"gain must be positive, got {gain}")

        self._topology = topology
        self._gain = 1.0 if topology == "buffer" else gain

    @property
    def name(self) -> str:
        """Return pattern name."""
        if self._topology == "buffer":
            return "opamp_buffer"
        return f"opamp_{self._topology}_gain{self._gain:.1f}"

    @property
    def category(self) -> InterfaceCategory:
        """Return SINGLE_ENDED category for op-amp circuits."""
        return InterfaceCategory.SINGLE_ENDED

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return op-amp placement rules.

        Returns:
            List of placement rules for op-amp layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="decoupling_at_pins",
                description="Place decoupling capacitors within 3mm of op-amp power pins",
                priority=ConstraintPriority.CRITICAL,
                component_refs=["C_VCC", "C_VEE", "C_BYPASS"],
                params={"max_distance_mm": 3.0},
            ),
            ConstraintPlacementRule(
                name="feedback_near_inverting",
                description="Place feedback components close to inverting input",
                priority=ConstraintPriority.CRITICAL,
                component_refs=["R_FB", "C_FB"],
                params={"max_distance_mm": 5.0},
            ),
        ]

        if self._topology in ("inverting", "non_inverting") and self._gain > 10:
            rules.append(
                ConstraintPlacementRule(
                    name="input_resistor_placement",
                    description="For high gain, keep input resistor close to op-amp",
                    priority=ConstraintPriority.RECOMMENDED,
                    component_refs=["R_IN"],
                    params={"max_distance_mm": 5.0},
                )
            )

        if self._topology == "differential":
            rules.append(
                ConstraintPlacementRule(
                    name="matched_resistors",
                    description="Place matched resistor pairs symmetrically",
                    priority=ConstraintPriority.CRITICAL,
                    params={"symmetric": True},
                )
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return op-amp routing rules.

        Returns:
            List of routing rules for op-amp traces.
        """
        rules = [
            ConstraintRoutingRule(
                name="minimize_input_capacitance",
                description="Keep op-amp input traces short to minimize parasitic capacitance",
                net_pattern="OPAMP_IN*",
                params={"max_mm": 10.0},
            ),
            ConstraintRoutingRule(
                name="ground_plane_reference",
                description="Route analog traces over continuous ground plane",
                net_pattern="OPAMP_*",
                params={"reference_plane": "GND", "avoid_splits": True},
            ),
            ConstraintRoutingRule(
                name="separate_power_return",
                description="Keep op-amp ground return separate from digital ground",
                net_pattern="AGND",
                params={"separate_from": "DGND"},
            ),
        ]

        if self._gain > 100:
            rules.append(
                ConstraintRoutingRule(
                    name="shield_high_gain_input",
                    description="Consider guard traces around high-gain input",
                    net_pattern="OPAMP_IN*",
                    params={"guard_trace": True},
                )
            )

        return rules

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate op-amp circuit configuration.

        Args:
            **kwargs: Validation parameters:
                - bandwidth_hz: Required bandwidth
                - gbw_hz: Op-amp gain-bandwidth product

        Returns:
            List of validation error messages.
        """
        errors = []

        bandwidth = kwargs.get("bandwidth_hz")
        gbw = kwargs.get("gbw_hz")

        if bandwidth is not None and gbw is not None:
            available_bw = gbw / self._gain
            if bandwidth > available_bw:
                errors.append(
                    f"Required bandwidth {bandwidth}Hz exceeds available "
                    f"{available_bw:.0f}Hz (GBW {gbw}Hz / gain {self._gain})"
                )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from op-amp pattern.

        Args:
            nets: List of op-amp circuit net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        constraints.append(
            Constraint(
                type="analog_separation",
                params={"nets": nets, "min_spacing_mm": 1.5},
                source=source,
                severity="warning",
            )
        )

        constraints.append(
            Constraint(
                type="max_length",
                params={"nets": nets, "max_mm": 30.0},
                source=source,
                severity="warning",
            )
        )

        return constraints


class SensorInterface(IntentPattern):
    """Sensor interface pattern.

    Provides placement and routing rules for various sensor types including
    thermistors, RTDs, strain gauges, and photodiodes.

    Attributes:
        sensor_type: Type of sensor.
        excitation: Excitation method ("voltage" or "current").
    """

    SENSOR_TYPES = ("thermistor", "rtd", "strain_gauge", "photodiode")

    def __init__(
        self,
        sensor_type: Literal["thermistor", "rtd", "strain_gauge", "photodiode"],
        excitation: Literal["voltage", "current"] = "voltage",
    ) -> None:
        """Initialize sensor interface pattern.

        Args:
            sensor_type: Type of sensor:
                - "thermistor": NTC/PTC thermistor
                - "rtd": Resistance temperature detector (PT100, PT1000)
                - "strain_gauge": Wheatstone bridge strain gauge
                - "photodiode": Photodiode with transimpedance amplifier
            excitation: Excitation method (voltage or current source).

        Raises:
            ValueError: If sensor_type or excitation is invalid.
        """
        if sensor_type not in self.SENSOR_TYPES:
            valid = ", ".join(self.SENSOR_TYPES)
            raise ValueError(f"Invalid sensor_type '{sensor_type}'. Valid: {valid}")
        if excitation not in ("voltage", "current"):
            raise ValueError(f"excitation must be 'voltage' or 'current', got {excitation}")

        self._sensor_type = sensor_type
        self._excitation = excitation

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"sensor_{self._sensor_type}"

    @property
    def category(self) -> InterfaceCategory:
        """Return SINGLE_ENDED category for sensor signals."""
        return InterfaceCategory.SINGLE_ENDED

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return sensor interface placement rules.

        Returns:
            List of placement rules for sensor layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="signal_conditioning_near_adc",
                description="Place signal conditioning circuitry close to ADC",
                priority=ConstraintPriority.CRITICAL,
                params={"max_distance_mm": 15.0},
            ),
        ]

        if self._sensor_type == "rtd":
            rules.extend(
                [
                    ConstraintPlacementRule(
                        name="reference_resistor",
                        description="Place precision reference resistor in thermally stable area",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["R_REF"],
                        params={"avoid_heat_sources": True},
                    ),
                    ConstraintPlacementRule(
                        name="four_wire_connection",
                        description="For 4-wire RTD, keep sense wires separate from drive",
                        priority=ConstraintPriority.RECOMMENDED,
                        params={"four_wire": True},
                    ),
                ]
            )

        if self._sensor_type == "strain_gauge":
            rules.append(
                ConstraintPlacementRule(
                    name="bridge_symmetry",
                    description="Place bridge resistors symmetrically for thermal matching",
                    priority=ConstraintPriority.CRITICAL,
                    params={"symmetric": True, "matched_thermal": True},
                )
            )

        if self._sensor_type == "photodiode":
            rules.append(
                ConstraintPlacementRule(
                    name="tia_near_photodiode",
                    description="Place transimpedance amplifier as close as possible to photodiode",
                    priority=ConstraintPriority.CRITICAL,
                    params={"max_distance_mm": 5.0, "minimize_capacitance": True},
                )
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return sensor interface routing rules.

        Returns:
            List of routing rules for sensor traces.
        """
        rules = [
            ConstraintRoutingRule(
                name="guard_traces",
                description="Use guard traces for high-impedance sensor signals",
                net_pattern="SENSOR_*",
                params={"guard_trace": True, "driven_guard": True},
            ),
            ConstraintRoutingRule(
                name="kelvin_connection",
                description="Use Kelvin (4-wire) connection for precision measurements",
                net_pattern="SENSE_*",
                params={"kelvin": True},
            ),
            ConstraintRoutingRule(
                name="avoid_digital_noise",
                description="Route sensor traces away from digital signals and switching supplies",
                net_pattern="SENSOR_*",
                params={"min_spacing_mm": 5.0, "avoid_switchers": True},
            ),
        ]

        if self._sensor_type == "strain_gauge":
            rules.append(
                ConstraintRoutingRule(
                    name="matched_trace_lengths",
                    description="Match trace lengths in bridge circuit for balance",
                    net_pattern="BRIDGE_*",
                    params={"tolerance_mm": 1.0},
                )
            )

        return rules

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate sensor interface configuration.

        Args:
            **kwargs: Validation parameters.

        Returns:
            List of validation error messages.
        """
        errors = []
        # No specific validation needed for basic configuration
        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from sensor pattern.

        Args:
            nets: List of sensor net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        constraints.append(
            Constraint(
                type="analog_separation",
                params={"nets": nets, "min_spacing_mm": 3.0},
                source=source,
                severity="warning",
            )
        )

        constraints.append(
            Constraint(
                type="guard_ring",
                params={"nets": nets, "recommended": True},
                source=source,
                severity="warning",
            )
        )

        return constraints


class DACOutputFilter(IntentPattern):
    """Output filter pattern for DAC outputs.

    Provides placement and routing rules for DAC output filtering circuits,
    including reconstruction filters for removing DAC quantization noise.

    Attributes:
        cutoff_hz: Filter cutoff frequency in Hz.
        order: Filter order.
        topology: Filter topology.
    """

    def __init__(
        self,
        cutoff_hz: float,
        order: int = 2,
        topology: Literal["rc", "active"] = "rc",
    ) -> None:
        """Initialize DAC output filter pattern.

        Args:
            cutoff_hz: Filter cutoff frequency in Hz.
            order: Filter order (1 or 2).
            topology: Filter topology ("rc" or "active").

        Raises:
            ValueError: If parameters are out of valid range.
        """
        if cutoff_hz <= 0:
            raise ValueError(f"cutoff_hz must be positive, got {cutoff_hz}")
        if order not in (1, 2):
            raise ValueError(f"order must be 1 or 2, got {order}")
        if topology not in ("rc", "active"):
            raise ValueError(f"topology must be 'rc' or 'active', got {topology}")

        self._cutoff_hz = cutoff_hz
        self._order = order
        self._topology = topology

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"dac_filter_{self._topology}_order{self._order}"

    @property
    def category(self) -> InterfaceCategory:
        """Return SINGLE_ENDED category for analog signals."""
        return InterfaceCategory.SINGLE_ENDED

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return DAC filter placement rules.

        Returns:
            List of placement rules for filter layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="filter_near_dac",
                description="Place filter components close to DAC output pin",
                priority=ConstraintPriority.CRITICAL,
                params={"max_distance_mm": 10.0},
            ),
            ConstraintPlacementRule(
                name="output_series_resistor",
                description="Include series resistor at filter output for stability",
                priority=ConstraintPriority.RECOMMENDED,
                component_refs=["R_OUT"],
                params={"resistance_ohms": 100},
            ),
        ]

        if self._topology == "active":
            rules.append(
                ConstraintPlacementRule(
                    name="opamp_near_dac",
                    description="Place output op-amp close to DAC for short signal path",
                    priority=ConstraintPriority.CRITICAL,
                    params={"max_distance_mm": 15.0},
                )
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return DAC filter routing rules.

        Returns:
            List of routing rules for filter traces.
        """
        return [
            ConstraintRoutingRule(
                name="ground_plane_reference",
                description="Route DAC output over continuous ground plane",
                net_pattern="DAC_OUT*",
                params={"reference_plane": "GND", "avoid_splits": True},
            ),
            ConstraintRoutingRule(
                name="separate_from_digital",
                description="Keep DAC output traces away from digital signals",
                net_pattern="DAC_*",
                params={"min_spacing_mm": 2.0, "avoid_digital": True},
            ),
        ]

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate DAC filter configuration.

        Args:
            **kwargs: Validation parameters:
                - update_rate_hz: DAC update rate

        Returns:
            List of validation error messages.
        """
        errors = []

        update_rate = kwargs.get("update_rate_hz")
        if update_rate is not None:
            nyquist = update_rate / 2
            if self._cutoff_hz > nyquist:
                errors.append(
                    f"Filter cutoff {self._cutoff_hz}Hz should be below Nyquist "
                    f"({nyquist}Hz) for update rate {update_rate}Hz"
                )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from DAC filter pattern.

        Args:
            nets: List of filter net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        constraints.append(
            Constraint(
                type="analog_separation",
                params={"nets": nets, "min_spacing_mm": 2.0},
                source=source,
                severity="warning",
            )
        )

        constraints.append(
            Constraint(
                type="max_length",
                params={"nets": nets, "max_mm": 50.0},
                source=source,
                severity="warning",
            )
        )

        return constraints


__all__ = [
    "ADCInputFilter",
    "DACOutputFilter",
    "OpAmpCircuit",
    "SensorInterface",
]
