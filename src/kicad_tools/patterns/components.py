"""Component requirements database.

Provides access to component specifications and requirements for
pattern validation and adaptation. Data is loaded from YAML files
in the data/ directory.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Path to component data files
DATA_DIR = Path(__file__).parent / "data"


@dataclass
class ComponentRequirements:
    """Requirements for a specific component.

    Contains all specifications needed for pattern validation
    and adaptation. Fields are optional as different component
    types have different requirements.

    Attributes:
        mpn: Manufacturer part number
        component_type: Type of component (LDO, BuckConverter, IC, etc.)
        manufacturer: Component manufacturer name
        description: Human-readable description

        # Capacitor requirements
        input_cap_min_uf: Minimum input capacitance in microfarads
        input_cap_max_esr_mohm: Maximum input capacitor ESR in milliohms
        output_cap_min_uf: Minimum output capacitance in microfarads
        output_cap_max_esr_mohm: Maximum output capacitor ESR in milliohms

        # Inductor requirements (for switching regulators)
        inductor_min_uh: Minimum inductance in microhenries
        inductor_max_dcr_mohm: Maximum inductor DC resistance in milliohms

        # Thermal requirements
        thermal_pad_required: Whether thermal pad/via is required
        max_junction_temp_c: Maximum junction temperature in Celsius

        # Electrical specifications
        dropout_voltage: Dropout voltage in volts (for LDOs)
        max_output_current_ma: Maximum output current in milliamps
        switching_freq_khz: Switching frequency in kHz (for switchers)
        input_voltage_min: Minimum input voltage
        input_voltage_max: Maximum input voltage
        output_voltage: Regulated output voltage

        # Diode requirements (for async buck)
        diode_part_number: Recommended catch diode part number

        # Decoupling requirements (for ICs)
        num_vdd_pins: Number of VDD power pins
        decoupling_caps: List of recommended decoupling capacitor values
        max_decoupling_distance_mm: Maximum distance for decoupling caps

        # Additional notes
        notes: List of application notes or warnings
    """

    mpn: str
    component_type: str = ""
    manufacturer: str = ""
    description: str = ""

    # Capacitor requirements
    input_cap_min_uf: float | None = None
    input_cap_max_esr_mohm: float | None = None
    output_cap_min_uf: float | None = None
    output_cap_max_esr_mohm: float | None = None

    # Inductor requirements
    inductor_min_uh: float | None = None
    inductor_max_dcr_mohm: float | None = None

    # Thermal requirements
    thermal_pad_required: bool = False
    max_junction_temp_c: float | None = None

    # Electrical specifications
    dropout_voltage: float | None = None
    max_output_current_ma: float | None = None
    switching_freq_khz: float | None = None
    input_voltage_min: float | None = None
    input_voltage_max: float | None = None
    output_voltage: float | None = None

    # Diode requirements
    diode_part_number: str | None = None

    # Decoupling requirements
    num_vdd_pins: int | None = None
    decoupling_caps: list[str] | None = None
    max_decoupling_distance_mm: float | None = None

    # Additional notes
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for serialization."""
        result = {
            "mpn": self.mpn,
            "component_type": self.component_type,
            "manufacturer": self.manufacturer,
            "description": self.description,
        }

        # Add non-None optional fields
        if self.input_cap_min_uf is not None:
            result["input_cap"] = {
                "min_uf": self.input_cap_min_uf,
                "max_esr_mohm": self.input_cap_max_esr_mohm,
            }
        if self.output_cap_min_uf is not None:
            result["output_cap"] = {
                "min_uf": self.output_cap_min_uf,
                "max_esr_mohm": self.output_cap_max_esr_mohm,
            }
        if self.inductor_min_uh is not None:
            result["inductor"] = {
                "min_uh": self.inductor_min_uh,
                "max_dcr_mohm": self.inductor_max_dcr_mohm,
            }
        if self.thermal_pad_required:
            result["thermal_pad"] = {"required": True}
        if self.dropout_voltage is not None:
            result["dropout_voltage"] = self.dropout_voltage
        if self.max_output_current_ma is not None:
            result["max_output_current_ma"] = self.max_output_current_ma
        if self.switching_freq_khz is not None:
            result["switching_freq_khz"] = self.switching_freq_khz
        if self.decoupling_caps is not None:
            result["decoupling_caps"] = self.decoupling_caps
        if self.notes:
            result["notes"] = self.notes

        return result


# Built-in component database
# This provides data for common components without requiring YAML files
_BUILTIN_COMPONENTS: dict[str, ComponentRequirements] = {
    # LDO Regulators
    "AMS1117-3.3": ComponentRequirements(
        mpn="AMS1117-3.3",
        component_type="LDO",
        manufacturer="Advanced Monolithic Systems",
        description="3.3V 1A Low Dropout Regulator",
        input_cap_min_uf=10.0,
        input_cap_max_esr_mohm=500,
        output_cap_min_uf=10.0,
        output_cap_max_esr_mohm=500,
        dropout_voltage=1.0,
        max_output_current_ma=1000,
        input_voltage_min=4.75,
        input_voltage_max=15.0,
        output_voltage=3.3,
        thermal_pad_required=False,
        notes=["Requires low ESR capacitors for stability"],
    ),
    "AMS1117-5.0": ComponentRequirements(
        mpn="AMS1117-5.0",
        component_type="LDO",
        manufacturer="Advanced Monolithic Systems",
        description="5.0V 1A Low Dropout Regulator",
        input_cap_min_uf=10.0,
        input_cap_max_esr_mohm=500,
        output_cap_min_uf=10.0,
        output_cap_max_esr_mohm=500,
        dropout_voltage=1.0,
        max_output_current_ma=1000,
        input_voltage_min=6.5,
        input_voltage_max=15.0,
        output_voltage=5.0,
        thermal_pad_required=False,
        notes=["Requires low ESR capacitors for stability"],
    ),
    "XC6206P332MR": ComponentRequirements(
        mpn="XC6206P332MR",
        component_type="LDO",
        manufacturer="Torex",
        description="3.3V 200mA Low Dropout Regulator",
        input_cap_min_uf=1.0,
        output_cap_min_uf=1.0,
        dropout_voltage=0.25,
        max_output_current_ma=200,
        input_voltage_min=1.8,
        input_voltage_max=6.0,
        output_voltage=3.3,
        thermal_pad_required=False,
        notes=["Ceramic capacitor compatible", "Very low quiescent current"],
    ),
    "AP2204K-3.3": ComponentRequirements(
        mpn="AP2204K-3.3",
        component_type="LDO",
        manufacturer="Diodes Incorporated",
        description="3.3V 150mA Low Dropout Regulator",
        input_cap_min_uf=1.0,
        output_cap_min_uf=1.0,
        dropout_voltage=0.4,
        max_output_current_ma=150,
        input_voltage_min=2.5,
        input_voltage_max=24.0,
        output_voltage=3.3,
        thermal_pad_required=False,
        notes=["Wide input voltage range", "Thermal shutdown protection"],
    ),
    "TLV1117-33": ComponentRequirements(
        mpn="TLV1117-33",
        component_type="LDO",
        manufacturer="Texas Instruments",
        description="3.3V 800mA Low Dropout Regulator",
        input_cap_min_uf=10.0,
        output_cap_min_uf=10.0,
        output_cap_max_esr_mohm=400,
        dropout_voltage=1.1,
        max_output_current_ma=800,
        input_voltage_min=4.75,
        input_voltage_max=15.0,
        output_voltage=3.3,
        thermal_pad_required=False,
        notes=["SOT-223 package provides good thermal dissipation"],
    ),
    # Buck Converters
    "LM2596-5.0": ComponentRequirements(
        mpn="LM2596-5.0",
        component_type="BuckConverter",
        manufacturer="Texas Instruments",
        description="5V 3A Step-Down Switching Regulator",
        input_cap_min_uf=100.0,
        output_cap_min_uf=220.0,
        inductor_min_uh=33.0,
        switching_freq_khz=150,
        max_output_current_ma=3000,
        input_voltage_min=7.0,
        input_voltage_max=40.0,
        output_voltage=5.0,
        diode_part_number="SS34",
        thermal_pad_required=True,
        notes=["Use Schottky catch diode", "External compensation not required"],
    ),
    "LM2596-3.3": ComponentRequirements(
        mpn="LM2596-3.3",
        component_type="BuckConverter",
        manufacturer="Texas Instruments",
        description="3.3V 3A Step-Down Switching Regulator",
        input_cap_min_uf=100.0,
        output_cap_min_uf=220.0,
        inductor_min_uh=33.0,
        switching_freq_khz=150,
        max_output_current_ma=3000,
        input_voltage_min=4.75,
        input_voltage_max=40.0,
        output_voltage=3.3,
        diode_part_number="SS34",
        thermal_pad_required=True,
        notes=["Use Schottky catch diode", "External compensation not required"],
    ),
    "MP1584EN": ComponentRequirements(
        mpn="MP1584EN",
        component_type="BuckConverter",
        manufacturer="Monolithic Power Systems",
        description="3A Step-Down Converter",
        input_cap_min_uf=22.0,
        output_cap_min_uf=22.0,
        inductor_min_uh=4.7,
        switching_freq_khz=1500,
        max_output_current_ma=3000,
        input_voltage_min=4.5,
        input_voltage_max=28.0,
        diode_part_number=None,  # Synchronous, no external diode
        thermal_pad_required=True,
        notes=["High frequency allows smaller inductor", "Synchronous rectification"],
    ),
    "TPS62200": ComponentRequirements(
        mpn="TPS62200",
        component_type="BuckConverter",
        manufacturer="Texas Instruments",
        description="300mA Step-Down Converter",
        input_cap_min_uf=10.0,
        output_cap_min_uf=10.0,
        inductor_min_uh=4.7,
        switching_freq_khz=1000,
        max_output_current_ma=300,
        input_voltage_min=2.5,
        input_voltage_max=6.0,
        diode_part_number=None,  # Synchronous
        thermal_pad_required=False,
        notes=["Low quiescent current", "Good for battery-powered applications"],
    ),
    # ICs (for decoupling)
    "STM32F405RGT6": ComponentRequirements(
        mpn="STM32F405RGT6",
        component_type="IC",
        manufacturer="STMicroelectronics",
        description="ARM Cortex-M4 MCU",
        num_vdd_pins=4,
        decoupling_caps=["4.7uF", "100nF", "100nF", "100nF", "100nF"],
        max_decoupling_distance_mm=5.0,
        notes=["Place 100nF caps as close as possible to VDD pins"],
    ),
    "ATMEGA328P": ComponentRequirements(
        mpn="ATMEGA328P",
        component_type="IC",
        manufacturer="Microchip",
        description="8-bit AVR MCU",
        num_vdd_pins=2,
        decoupling_caps=["10uF", "100nF", "100nF"],
        max_decoupling_distance_mm=5.0,
        notes=["Place 100nF cap between VCC and GND, close to pins"],
    ),
    "ESP32-WROOM-32": ComponentRequirements(
        mpn="ESP32-WROOM-32",
        component_type="IC",
        manufacturer="Espressif",
        description="WiFi+BT Module",
        num_vdd_pins=1,
        decoupling_caps=["10uF", "100nF"],
        max_decoupling_distance_mm=3.0,
        notes=["RF-sensitive, place caps very close to 3V3 pin"],
    ),
    "RP2040": ComponentRequirements(
        mpn="RP2040",
        component_type="IC",
        manufacturer="Raspberry Pi",
        description="Dual-core ARM Cortex-M0+ MCU",
        num_vdd_pins=5,
        decoupling_caps=["10uF", "100nF", "100nF", "100nF", "100nF", "100nF"],
        max_decoupling_distance_mm=3.0,
        notes=["Multiple power domains require careful decoupling"],
    ),
}


def get_component_requirements(mpn: str) -> ComponentRequirements:
    """Get requirements for a component by manufacturer part number.

    First checks the built-in database, then looks for YAML data files.

    Args:
        mpn: Manufacturer part number (case-insensitive)

    Returns:
        ComponentRequirements for the specified component

    Raises:
        KeyError: If component not found in database

    Example:
        >>> reqs = get_component_requirements("AMS1117-3.3")
        >>> print(reqs.input_cap_min_uf)
        10.0
    """
    # Normalize MPN for lookup
    mpn_upper = mpn.upper()

    # Check built-in database first
    for key, reqs in _BUILTIN_COMPONENTS.items():
        if key.upper() == mpn_upper:
            return reqs

    # Try loading from YAML file
    yaml_reqs = _load_from_yaml(mpn)
    if yaml_reqs:
        return yaml_reqs

    raise KeyError(f"Component {mpn} not found in database")


def list_components(component_type: str | None = None) -> list[str]:
    """List all components in the database.

    Args:
        component_type: Filter by component type (e.g., "LDO", "BuckConverter")

    Returns:
        List of manufacturer part numbers

    Example:
        >>> ldos = list_components("LDO")
        >>> print(ldos)
        ['AMS1117-3.3', 'AMS1117-5.0', 'XC6206P332MR', ...]
    """
    result = []

    for mpn, reqs in _BUILTIN_COMPONENTS.items():
        if component_type is None or reqs.component_type.upper() == component_type.upper():
            result.append(mpn)

    # Also list components from YAML files
    if DATA_DIR.exists():
        for yaml_file in DATA_DIR.glob("*.yaml"):
            # Extract MPN from filename
            mpn = yaml_file.stem
            if mpn not in result:
                # Load to check component type if filtering
                if component_type is not None:
                    try:
                        reqs = _load_from_yaml(mpn)
                        if reqs and reqs.component_type.upper() == component_type.upper():
                            result.append(mpn)
                    except Exception:
                        pass
                else:
                    result.append(mpn)

    return sorted(result)


def _load_from_yaml(mpn: str) -> ComponentRequirements | None:
    """Load component requirements from a YAML file.

    Args:
        mpn: Manufacturer part number

    Returns:
        ComponentRequirements if file exists and is valid, None otherwise
    """
    try:
        import yaml
    except ImportError:
        logger.warning("PyYAML not installed, cannot load YAML component data")
        return None

    # Try different filename formats
    potential_files = [
        DATA_DIR / f"{mpn}.yaml",
        DATA_DIR / f"{mpn.lower()}.yaml",
        DATA_DIR / f"{mpn.upper()}.yaml",
        DATA_DIR / f"{mpn.replace('-', '_')}.yaml",
    ]

    for yaml_path in potential_files:
        if yaml_path.exists():
            try:
                with open(yaml_path) as f:
                    data = yaml.safe_load(f)
                return _parse_yaml_data(mpn, data)
            except Exception as e:
                logger.warning(f"Error loading {yaml_path}: {e}")

    return None


def _parse_yaml_data(mpn: str, data: dict) -> ComponentRequirements:
    """Parse YAML data into ComponentRequirements.

    Args:
        mpn: Manufacturer part number
        data: Parsed YAML data

    Returns:
        ComponentRequirements object
    """
    reqs = ComponentRequirements(
        mpn=mpn,
        component_type=data.get("type", ""),
        manufacturer=data.get("manufacturer", ""),
        description=data.get("description", ""),
    )

    # Parse capacitor requirements
    if "input_cap" in data:
        cap_data = data["input_cap"]
        reqs.input_cap_min_uf = cap_data.get("min_uf")
        reqs.input_cap_max_esr_mohm = cap_data.get("max_esr_mohm")

    if "output_cap" in data:
        cap_data = data["output_cap"]
        reqs.output_cap_min_uf = cap_data.get("min_uf")
        reqs.output_cap_max_esr_mohm = cap_data.get("max_esr_mohm")

    # Parse inductor requirements
    if "inductor" in data:
        ind_data = data["inductor"]
        reqs.inductor_min_uh = ind_data.get("min_uh")
        reqs.inductor_max_dcr_mohm = ind_data.get("max_dcr_mohm")

    # Parse thermal requirements
    if "thermal_pad" in data:
        thermal_data = data["thermal_pad"]
        reqs.thermal_pad_required = thermal_data.get("required", False)
        reqs.max_junction_temp_c = thermal_data.get("max_junction_temp_c")

    # Parse electrical specifications
    reqs.dropout_voltage = data.get("dropout_voltage")
    reqs.max_output_current_ma = data.get("max_output_current_ma")
    reqs.switching_freq_khz = data.get("switching_freq_khz")
    reqs.input_voltage_min = data.get("input_voltage_min")
    reqs.input_voltage_max = data.get("input_voltage_max")
    reqs.output_voltage = data.get("output_voltage")
    reqs.diode_part_number = data.get("diode_part_number")

    # Parse decoupling requirements
    reqs.num_vdd_pins = data.get("num_vdd_pins")
    reqs.decoupling_caps = data.get("decoupling_caps")
    reqs.max_decoupling_distance_mm = data.get("max_decoupling_distance_mm")

    # Parse notes
    reqs.notes = data.get("notes", [])

    return reqs
