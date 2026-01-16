"""
Protection circuit patterns.

This module provides PCB patterns for various protection circuits including
ESD protection, overcurrent protection, reverse polarity protection,
overvoltage protection, and thermal shutdown.

Example::

    from kicad_tools.patterns.protection import ESDProtection, ReversePolarityProtection

    # ESD protection for USB data lines
    esd = ESDProtection(lines=["USB_DP", "USB_DM"], protection_level="enhanced")
    rules = esd.get_placement_rules()

    # Reverse polarity protection using P-FET
    rprot = ReversePolarityProtection(topology="pfet", max_current=5.0)
    constraints = rprot.derive_constraints(["VIN", "VIN_PROT"])
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


class ESDProtection(IntentPattern):
    """ESD protection pattern for I/O lines.

    Provides placement and routing rules for ESD protection circuits using
    TVS diodes or dedicated ESD protection ICs.

    Attributes:
        lines: List of I/O lines to protect.
        protection_level: Level of protection ("basic" or "enhanced").
    """

    def __init__(
        self,
        lines: list[str],
        protection_level: Literal["basic", "enhanced"] = "basic",
    ) -> None:
        """Initialize ESD protection pattern.

        Args:
            lines: List of I/O line names to protect.
            protection_level: Level of protection:
                - "basic": Single TVS diode per line, 8kV contact discharge
                - "enhanced": Multi-stage protection, 15kV+ contact discharge

        Raises:
            ValueError: If lines is empty or protection_level is invalid.
        """
        if not lines:
            raise ValueError("lines cannot be empty")
        if protection_level not in ("basic", "enhanced"):
            raise ValueError(
                f"protection_level must be 'basic' or 'enhanced', got {protection_level}"
            )

        self._lines = lines
        self._protection_level = protection_level

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"esd_{self._protection_level}_{len(self._lines)}ch"

    @property
    def category(self) -> InterfaceCategory:
        """Return SINGLE_ENDED category for ESD protection."""
        return InterfaceCategory.SINGLE_ENDED

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return ESD protection placement rules.

        Returns:
            List of placement rules for ESD layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="tvs_at_connector",
                description="Place TVS diodes as close as possible to connector pins",
                priority=ConstraintPriority.CRITICAL,
                component_refs=["D_TVS", "U_ESD"],
                params={"max_distance_mm": 5.0},
            ),
            ConstraintPlacementRule(
                name="ground_via_nearby",
                description="Place ground via within 2mm of TVS cathode",
                priority=ConstraintPriority.CRITICAL,
                params={"max_distance_mm": 2.0, "via_to_ground": True},
            ),
            ConstraintPlacementRule(
                name="before_series_resistor",
                description="Place ESD protection before any series resistors in signal path",
                priority=ConstraintPriority.CRITICAL,
                params={"before_series_r": True},
            ),
        ]

        if self._protection_level == "enhanced":
            rules.extend(
                [
                    ConstraintPlacementRule(
                        name="multi_stage_placement",
                        description="For multi-stage protection, place primary TVS at connector, secondary near IC",
                        priority=ConstraintPriority.RECOMMENDED,
                        params={"multi_stage": True},
                    ),
                    ConstraintPlacementRule(
                        name="ferrite_between_stages",
                        description="Place ferrite bead between protection stages",
                        priority=ConstraintPriority.RECOMMENDED,
                        component_refs=["FB1"],
                        params={"between_stages": True},
                    ),
                ]
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return ESD protection routing rules.

        Returns:
            List of routing rules for ESD protection.
        """
        return [
            ConstraintRoutingRule(
                name="short_tvs_stub",
                description="Keep TVS connection stub as short as possible (<3mm)",
                net_pattern="*_ESD",
                params={"max_stub_mm": 3.0},
            ),
            ConstraintRoutingRule(
                name="wide_ground_trace",
                description="Use wide ground trace from TVS to ground plane",
                net_pattern="GND",
                params={"min_width_mm": 0.5},
            ),
            ConstraintRoutingRule(
                name="no_ground_splits",
                description="Do not route ESD path over ground plane splits",
                net_pattern="*",
                params={"avoid_ground_splits": True},
            ),
        ]

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate ESD protection configuration.

        Args:
            **kwargs: Validation parameters:
                - voltage_v: Signal voltage for clamping check

        Returns:
            List of validation error messages.
        """
        errors = []

        voltage = kwargs.get("voltage_v")
        if voltage is not None:
            # Basic check: TVS clamping voltage should be above signal voltage
            if voltage > 24:
                errors.append(
                    f"Signal voltage {voltage}V is high; ensure TVS breakdown "
                    "voltage is appropriate"
                )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from ESD protection pattern.

        Args:
            nets: List of protected net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        constraints.append(
            Constraint(
                type="esd_protection",
                params={
                    "nets": nets,
                    "level": self._protection_level,
                    "max_stub_mm": 3.0,
                },
                source=source,
                severity="warning",
            )
        )

        constraints.append(
            Constraint(
                type="ground_plane",
                params={"nets": nets, "continuous": True, "no_splits": True},
                source=source,
                severity="warning",
            )
        )

        return constraints


class OvercurrentProtection(IntentPattern):
    """Overcurrent protection pattern.

    Provides placement and routing rules for overcurrent protection using
    fuses, PTC resettable fuses, or electronic protection.

    Attributes:
        topology: Protection topology.
        max_current: Maximum continuous current in Amps.
        trip_current: Trip/blow current in Amps.
    """

    TOPOLOGIES = ("fuse", "ptc", "efuse")

    def __init__(
        self,
        topology: Literal["fuse", "ptc", "efuse"],
        max_current: float,
        trip_current: float | None = None,
    ) -> None:
        """Initialize overcurrent protection pattern.

        Args:
            topology: Protection topology:
                - "fuse": Traditional one-time fuse
                - "ptc": PTC resettable fuse (polyfuse)
                - "efuse": Electronic fuse IC
            max_current: Maximum continuous current in Amps.
            trip_current: Trip/blow current in Amps. If None, defaults to 2x max_current.

        Raises:
            ValueError: If topology is invalid or currents are non-positive.
        """
        if topology not in self.TOPOLOGIES:
            valid = ", ".join(self.TOPOLOGIES)
            raise ValueError(f"Invalid topology '{topology}'. Valid: {valid}")
        if max_current <= 0:
            raise ValueError(f"max_current must be positive, got {max_current}")

        self._topology = topology
        self._max_current = max_current
        self._trip_current = trip_current or (max_current * 2)

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"overcurrent_{self._topology}_{self._max_current}A"

    @property
    def category(self) -> InterfaceCategory:
        """Return POWER category for overcurrent protection."""
        return InterfaceCategory.POWER

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return overcurrent protection placement rules.

        Returns:
            List of placement rules for protection layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="protection_at_input",
                description="Place overcurrent protection at power input, before distribution",
                priority=ConstraintPriority.CRITICAL,
                params={"at_input": True},
            ),
        ]

        if self._topology == "fuse":
            rules.append(
                ConstraintPlacementRule(
                    name="fuse_accessible",
                    description="Ensure fuse is accessible for replacement",
                    priority=ConstraintPriority.RECOMMENDED,
                    component_refs=["F1"],
                    params={"accessible": True, "use_holder": True},
                )
            )

        if self._topology == "ptc":
            rules.extend(
                [
                    ConstraintPlacementRule(
                        name="ptc_thermal_isolation",
                        description="Keep PTC away from heat sources for proper trip behavior",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["PTC1"],
                        params={"thermal_isolation": True, "min_distance_mm": 10.0},
                    ),
                    ConstraintPlacementRule(
                        name="ptc_airflow",
                        description="Allow airflow around PTC for reset cooling",
                        priority=ConstraintPriority.RECOMMENDED,
                        params={"allow_airflow": True},
                    ),
                ]
            )

        if self._topology == "efuse":
            rules.extend(
                [
                    ConstraintPlacementRule(
                        name="sense_resistor_placement",
                        description="Place current sense resistor close to efuse IC",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["R_SENSE"],
                        params={"max_distance_mm": 5.0},
                    ),
                    ConstraintPlacementRule(
                        name="efuse_thermal_pad",
                        description="Ensure adequate thermal relief for efuse IC",
                        priority=ConstraintPriority.CRITICAL,
                        params={"thermal_vias": True, "via_count": 4},
                    ),
                ]
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return overcurrent protection routing rules.

        Returns:
            List of routing rules for protection traces.
        """
        # Calculate minimum trace width for current
        # Rule of thumb: 1 oz copper, ~0.3mm per Amp for 10C rise
        min_width_mm = max(0.3, self._max_current * 0.3)

        rules = [
            ConstraintRoutingRule(
                name="current_carrying_width",
                description=f"Use minimum {min_width_mm:.2f}mm trace width for {self._max_current}A",
                net_pattern="VIN*",
                params={"min_width_mm": min_width_mm, "current_a": self._max_current},
            ),
            ConstraintRoutingRule(
                name="kelvin_sense",
                description="Use Kelvin connection for current sense resistor",
                net_pattern="SENSE_*",
                params={"kelvin": True},
            ),
        ]

        if self._topology == "efuse":
            rules.append(
                ConstraintRoutingRule(
                    name="separate_sense_return",
                    description="Route sense ground separately from power ground",
                    net_pattern="GND_SENSE",
                    params={"separate_from": "GND_POWER"},
                )
            )

        return rules

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate overcurrent protection configuration.

        Args:
            **kwargs: Validation parameters:
                - load_current: Expected load current

        Returns:
            List of validation error messages.
        """
        errors = []

        load_current = kwargs.get("load_current")
        if load_current is not None:
            if load_current > self._max_current * 0.8:
                errors.append(
                    f"Load current {load_current}A is too close to protection "
                    f"rating {self._max_current}A; recommend 80% derating"
                )

        if self._trip_current < self._max_current * 1.5:
            errors.append(
                f"Trip current {self._trip_current}A may cause nuisance trips; "
                f"recommend >= 1.5x max current ({self._max_current * 1.5}A)"
            )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from overcurrent protection pattern.

        Args:
            nets: List of protected net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        min_width_mm = max(0.3, self._max_current * 0.3)

        constraints.append(
            Constraint(
                type="min_trace_width",
                params={"nets": nets, "min_mm": min_width_mm, "current_a": self._max_current},
                source=source,
                severity="error",
            )
        )

        constraints.append(
            Constraint(
                type="thermal_relief",
                params={"nets": nets, "required": True},
                source=source,
                severity="warning",
            )
        )

        return constraints


class ReversePolarityProtection(IntentPattern):
    """Reverse polarity protection pattern.

    Provides placement and routing rules for reverse polarity protection
    using diodes, P-FETs, or ideal diode controllers.

    Attributes:
        topology: Protection topology.
        max_current: Maximum current in Amps.
    """

    TOPOLOGIES = ("diode", "pfet", "ideal_diode")

    def __init__(
        self,
        topology: Literal["diode", "pfet", "ideal_diode"],
        max_current: float,
    ) -> None:
        """Initialize reverse polarity protection pattern.

        Args:
            topology: Protection topology:
                - "diode": Series Schottky diode (simple, ~0.3V drop)
                - "pfet": P-channel MOSFET (low drop, ~50mV)
                - "ideal_diode": Controller IC (lowest drop, ~20mV)
            max_current: Maximum current in Amps.

        Raises:
            ValueError: If topology is invalid or current is non-positive.
        """
        if topology not in self.TOPOLOGIES:
            valid = ", ".join(self.TOPOLOGIES)
            raise ValueError(f"Invalid topology '{topology}'. Valid: {valid}")
        if max_current <= 0:
            raise ValueError(f"max_current must be positive, got {max_current}")

        self._topology = topology
        self._max_current = max_current

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"reverse_polarity_{self._topology}"

    @property
    def category(self) -> InterfaceCategory:
        """Return POWER category for reverse polarity protection."""
        return InterfaceCategory.POWER

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return reverse polarity protection placement rules.

        Returns:
            List of placement rules for protection layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="at_power_input",
                description="Place reverse polarity protection at power input connector",
                priority=ConstraintPriority.CRITICAL,
                params={"at_input": True},
            ),
        ]

        if self._topology == "diode":
            rules.append(
                ConstraintPlacementRule(
                    name="diode_thermal",
                    description="Ensure adequate thermal relief for series diode",
                    priority=ConstraintPriority.CRITICAL,
                    component_refs=["D1"],
                    params={"thermal_vias": True},
                )
            )

        if self._topology == "pfet":
            rules.extend(
                [
                    ConstraintPlacementRule(
                        name="gate_resistor_close",
                        description="Place gate resistor close to MOSFET gate",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["R_GATE"],
                        params={"max_distance_mm": 5.0},
                    ),
                    ConstraintPlacementRule(
                        name="mosfet_thermal",
                        description="Provide thermal vias under MOSFET for heat dissipation",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["Q1"],
                        params={"thermal_vias": True, "via_count": 6},
                    ),
                ]
            )

        if self._topology == "ideal_diode":
            rules.extend(
                [
                    ConstraintPlacementRule(
                        name="controller_near_fet",
                        description="Place ideal diode controller close to MOSFET",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["U_CTRL", "Q1"],
                        params={"max_distance_mm": 10.0},
                    ),
                    ConstraintPlacementRule(
                        name="sense_resistor_kelvin",
                        description="Place sense resistor with Kelvin connection",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["R_SENSE"],
                        params={"kelvin": True},
                    ),
                ]
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return reverse polarity protection routing rules.

        Returns:
            List of routing rules for protection traces.
        """
        min_width_mm = max(0.3, self._max_current * 0.3)

        rules = [
            ConstraintRoutingRule(
                name="power_trace_width",
                description=f"Use minimum {min_width_mm:.2f}mm trace width",
                net_pattern="VIN*",
                params={"min_width_mm": min_width_mm},
            ),
        ]

        if self._topology in ("pfet", "ideal_diode"):
            rules.append(
                ConstraintRoutingRule(
                    name="gate_drive_short",
                    description="Keep MOSFET gate drive trace short",
                    net_pattern="GATE*",
                    params={"max_mm": 15.0},
                )
            )

        return rules

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate reverse polarity protection configuration.

        Args:
            **kwargs: Validation parameters:
                - input_voltage: Input voltage for power dissipation check

        Returns:
            List of validation error messages.
        """
        errors = []

        voltage = kwargs.get("input_voltage")
        if voltage is not None and self._topology == "diode":
            # Estimate power dissipation in series diode
            vf = 0.3  # Typical Schottky forward voltage
            power = vf * self._max_current
            if power > 1.0:
                errors.append(
                    f"Series diode dissipates {power:.1f}W at {self._max_current}A; "
                    "consider P-FET topology for lower loss"
                )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from reverse polarity protection pattern.

        Args:
            nets: List of protected net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        min_width_mm = max(0.3, self._max_current * 0.3)

        constraints.append(
            Constraint(
                type="min_trace_width",
                params={"nets": nets, "min_mm": min_width_mm},
                source=source,
                severity="error",
            )
        )

        if self._topology in ("pfet", "ideal_diode"):
            constraints.append(
                Constraint(
                    type="thermal_relief",
                    params={"nets": nets, "required": True, "via_count": 6},
                    source=source,
                    severity="warning",
                )
            )

        return constraints


class OvervoltageProtection(IntentPattern):
    """Overvoltage protection pattern.

    Provides placement and routing rules for overvoltage protection using
    zener diodes, TVS diodes, or crowbar circuits.

    Attributes:
        topology: Protection topology.
        clamp_voltage: Clamping voltage in Volts.
        max_current: Maximum surge current in Amps.
    """

    TOPOLOGIES = ("zener", "tvs", "crowbar")

    def __init__(
        self,
        topology: Literal["zener", "tvs", "crowbar"],
        clamp_voltage: float,
        max_current: float = 1.0,
    ) -> None:
        """Initialize overvoltage protection pattern.

        Args:
            topology: Protection topology:
                - "zener": Zener diode clamp (continuous power handling)
                - "tvs": TVS diode (high surge capability)
                - "crowbar": SCR crowbar (triggers fuse/circuit breaker)
            clamp_voltage: Clamping voltage in Volts.
            max_current: Maximum surge current in Amps.

        Raises:
            ValueError: If parameters are invalid.
        """
        if topology not in self.TOPOLOGIES:
            valid = ", ".join(self.TOPOLOGIES)
            raise ValueError(f"Invalid topology '{topology}'. Valid: {valid}")
        if clamp_voltage <= 0:
            raise ValueError(f"clamp_voltage must be positive, got {clamp_voltage}")
        if max_current <= 0:
            raise ValueError(f"max_current must be positive, got {max_current}")

        self._topology = topology
        self._clamp_voltage = clamp_voltage
        self._max_current = max_current

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"overvoltage_{self._topology}_{self._clamp_voltage}V"

    @property
    def category(self) -> InterfaceCategory:
        """Return POWER category for overvoltage protection."""
        return InterfaceCategory.POWER

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return overvoltage protection placement rules.

        Returns:
            List of placement rules for protection layout.
        """
        rules = [
            ConstraintPlacementRule(
                name="at_voltage_source",
                description="Place overvoltage protection near voltage source/input",
                priority=ConstraintPriority.CRITICAL,
                params={"at_input": True},
            ),
            ConstraintPlacementRule(
                name="ground_path",
                description="Ensure low-impedance path to ground",
                priority=ConstraintPriority.CRITICAL,
                params={"ground_via_count": 2},
            ),
        ]

        if self._topology == "crowbar":
            rules.extend(
                [
                    ConstraintPlacementRule(
                        name="fuse_upstream",
                        description="Place fuse upstream of crowbar circuit",
                        priority=ConstraintPriority.CRITICAL,
                        component_refs=["F1"],
                        params={"upstream": True},
                    ),
                    ConstraintPlacementRule(
                        name="scr_thermal",
                        description="Provide thermal relief for SCR during crowbar event",
                        priority=ConstraintPriority.RECOMMENDED,
                        component_refs=["SCR1"],
                        params={"thermal_vias": True},
                    ),
                ]
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return overvoltage protection routing rules.

        Returns:
            List of routing rules for protection traces.
        """
        return [
            ConstraintRoutingRule(
                name="short_clamp_path",
                description="Keep voltage clamp path as short as possible",
                net_pattern="*_CLAMP",
                params={"max_mm": 10.0},
            ),
            ConstraintRoutingRule(
                name="wide_ground",
                description="Use wide ground trace for surge current",
                net_pattern="GND",
                params={"min_width_mm": 0.5},
            ),
        ]

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate overvoltage protection configuration.

        Args:
            **kwargs: Validation parameters:
                - nominal_voltage: Nominal operating voltage

        Returns:
            List of validation error messages.
        """
        errors = []

        nominal = kwargs.get("nominal_voltage")
        if nominal is not None:
            if self._clamp_voltage < nominal * 1.1:
                errors.append(
                    f"Clamp voltage {self._clamp_voltage}V is too close to nominal "
                    f"{nominal}V; recommend >= 1.1x nominal"
                )
            if self._clamp_voltage > nominal * 1.5:
                errors.append(
                    f"Clamp voltage {self._clamp_voltage}V may not protect downstream "
                    f"components rated for {nominal}V"
                )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from overvoltage protection pattern.

        Args:
            nets: List of protected net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        constraints.append(
            Constraint(
                type="overvoltage_protection",
                params={
                    "nets": nets,
                    "clamp_v": self._clamp_voltage,
                    "topology": self._topology,
                },
                source=source,
                severity="warning",
            )
        )

        return constraints


class ThermalShutdown(IntentPattern):
    """Thermal shutdown protection pattern.

    Provides placement rules for thermal monitoring and shutdown circuits
    using temperature sensors and thermal cutoff devices.

    Attributes:
        sensor_type: Type of temperature sensor.
        shutdown_temp_c: Shutdown temperature in Celsius.
    """

    SENSOR_TYPES = ("ntc", "ptc", "ic", "thermostat")

    def __init__(
        self,
        sensor_type: Literal["ntc", "ptc", "ic", "thermostat"] = "ntc",
        shutdown_temp_c: float = 85.0,
    ) -> None:
        """Initialize thermal shutdown pattern.

        Args:
            sensor_type: Type of temperature sensor:
                - "ntc": NTC thermistor
                - "ptc": PTC thermistor (self-protecting)
                - "ic": Temperature sensor IC (LM35, TMP36, etc.)
                - "thermostat": Mechanical thermostat/thermal cutoff
            shutdown_temp_c: Shutdown temperature in Celsius.

        Raises:
            ValueError: If sensor_type is invalid.
        """
        if sensor_type not in self.SENSOR_TYPES:
            valid = ", ".join(self.SENSOR_TYPES)
            raise ValueError(f"Invalid sensor_type '{sensor_type}'. Valid: {valid}")

        self._sensor_type = sensor_type
        self._shutdown_temp_c = shutdown_temp_c

    @property
    def name(self) -> str:
        """Return pattern name."""
        return f"thermal_shutdown_{self._sensor_type}"

    @property
    def category(self) -> InterfaceCategory:
        """Return SINGLE_ENDED category for thermal monitoring."""
        return InterfaceCategory.SINGLE_ENDED

    def get_placement_rules(self) -> list[ConstraintPlacementRule]:
        """Return thermal shutdown placement rules.

        Returns:
            List of placement rules for thermal monitoring.
        """
        rules = [
            ConstraintPlacementRule(
                name="sensor_at_hotspot",
                description="Place temperature sensor at/near thermal hotspot",
                priority=ConstraintPriority.CRITICAL,
                component_refs=["TH1", "U_TEMP"],
                params={"at_hotspot": True},
            ),
            ConstraintPlacementRule(
                name="thermal_coupling",
                description="Ensure good thermal coupling between sensor and monitored component",
                priority=ConstraintPriority.CRITICAL,
                params={"thermal_via": True, "copper_pour": True},
            ),
        ]

        if self._sensor_type == "ntc":
            rules.append(
                ConstraintPlacementRule(
                    name="bias_resistor_away",
                    description="Place bias resistor away from heat source to avoid self-heating",
                    priority=ConstraintPriority.RECOMMENDED,
                    component_refs=["R_BIAS"],
                    params={"min_distance_mm": 5.0},
                )
            )

        if self._sensor_type == "ic":
            rules.append(
                ConstraintPlacementRule(
                    name="ic_decoupling",
                    description="Place decoupling capacitor close to sensor IC",
                    priority=ConstraintPriority.CRITICAL,
                    component_refs=["C_BYPASS"],
                    params={"max_distance_mm": 3.0},
                )
            )

        if self._sensor_type == "thermostat":
            rules.append(
                ConstraintPlacementRule(
                    name="thermostat_accessible",
                    description="Ensure thermal cutoff is accessible for replacement",
                    priority=ConstraintPriority.RECOMMENDED,
                    params={"accessible": True},
                )
            )

        return rules

    def get_routing_rules(self) -> list[ConstraintRoutingRule]:
        """Return thermal shutdown routing rules.

        Returns:
            List of routing rules for thermal monitoring.
        """
        rules = [
            ConstraintRoutingRule(
                name="sense_line_short",
                description="Keep temperature sense lines short",
                net_pattern="TEMP_*",
                params={"max_mm": 50.0},
            ),
        ]

        if self._sensor_type in ("ntc", "ic"):
            rules.append(
                ConstraintRoutingRule(
                    name="analog_separation",
                    description="Keep temperature sense away from noisy digital signals",
                    net_pattern="TEMP_*",
                    params={"min_spacing_mm": 2.0, "avoid_digital": True},
                )
            )

        return rules

    def validate(self, **kwargs: Any) -> list[str]:
        """Validate thermal shutdown configuration.

        Args:
            **kwargs: Validation parameters:
                - max_operating_temp_c: Maximum operating temperature

        Returns:
            List of validation error messages.
        """
        errors = []

        max_temp = kwargs.get("max_operating_temp_c")
        if max_temp is not None:
            if self._shutdown_temp_c < max_temp + 10:
                errors.append(
                    f"Shutdown temp {self._shutdown_temp_c}°C is too close to "
                    f"max operating temp {max_temp}°C; recommend >= 10°C margin"
                )

        return errors

    def derive_constraints(
        self, nets: list[str], params: dict[str, Any] | None = None
    ) -> list[Constraint]:
        """Derive intent constraints from thermal shutdown pattern.

        Args:
            nets: List of thermal monitoring net names.
            params: Optional parameters for constraint derivation.

        Returns:
            List of Constraint objects for the intent system.
        """
        constraints = []
        source = f"pattern:{self.name}"

        constraints.append(
            Constraint(
                type="thermal_monitoring",
                params={
                    "nets": nets,
                    "sensor_type": self._sensor_type,
                    "shutdown_temp_c": self._shutdown_temp_c,
                },
                source=source,
                severity="warning",
            )
        )

        return constraints


__all__ = [
    "ESDProtection",
    "OvercurrentProtection",
    "OvervoltageProtection",
    "ReversePolarityProtection",
    "ThermalShutdown",
]
