"""
YAML parser for .kct project specification files.

Provides loading, saving, and validation of .kct files.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema import ProjectSpec

__all__ = ["load_spec", "save_spec", "validate_spec"]


# Custom YAML representers for clean output
def _str_representer(dumper: yaml.Dumper, data: str) -> yaml.Node:
    """Use literal block style for multiline strings."""
    if "\n" in data:
        return dumper.represent_scalar("tag:yaml.org,2002:str", data, style="|")
    return dumper.represent_scalar("tag:yaml.org,2002:str", data)


def _none_representer(dumper: yaml.Dumper, data: None) -> yaml.Node:
    """Represent None as empty string for cleaner YAML."""
    return dumper.represent_scalar("tag:yaml.org,2002:null", "")


class CleanDumper(yaml.SafeDumper):
    """Custom YAML dumper for clean output."""

    pass


CleanDumper.add_representer(str, _str_representer)
CleanDumper.add_representer(type(None), _none_representer)


def load_spec(path: Path | str) -> ProjectSpec:
    """Load a .kct specification file.

    Args:
        path: Path to the .kct file

    Returns:
        Parsed ProjectSpec instance

    Raises:
        FileNotFoundError: If the file doesn't exist
        ValueError: If the file format is invalid
        ValidationError: If the content doesn't match the schema
    """
    path = Path(path)

    if not path.exists():
        raise FileNotFoundError(f"Spec file not found: {path}")

    content = path.read_text(encoding="utf-8")

    try:
        data = yaml.safe_load(content)
    except yaml.YAMLError as e:
        raise ValueError(f"Invalid YAML in {path}: {e}") from e

    if data is None:
        raise ValueError(f"Empty spec file: {path}")

    if not isinstance(data, dict):
        raise ValueError(f"Spec file must contain a YAML mapping: {path}")

    return ProjectSpec.model_validate(data)


def save_spec(spec: ProjectSpec, path: Path | str, *, exclude_none: bool = True) -> None:
    """Save a ProjectSpec to a .kct file.

    Args:
        spec: The specification to save
        path: Output file path
        exclude_none: If True, omit None/null values from output
    """
    path = Path(path)

    # Convert to dict, optionally excluding None values
    data = spec.model_dump(
        exclude_none=exclude_none,
        mode="json",  # Use JSON-compatible serialization for dates
    )

    # Add header comment
    header = f"""\
# KiCad Tools Project Specification
# Format version: {spec.kct_version}
# Documentation: https://github.com/rjwalters/kicad-tools#spec-format
#
# This file captures design intent, requirements, and progress for the project.
# Edit manually or use: kct spec <command>
# =============================================================================

"""

    yaml_content = yaml.dump(
        data,
        Dumper=CleanDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )

    path.write_text(header + yaml_content, encoding="utf-8")


def validate_spec(path: Path | str) -> tuple[bool, list[str]]:
    """Validate a .kct specification file.

    Args:
        path: Path to the .kct file

    Returns:
        Tuple of (is_valid, list of error messages)
    """
    errors: list[str] = []

    try:
        spec = load_spec(path)
    except FileNotFoundError as e:
        return False, [str(e)]
    except ValueError as e:
        return False, [f"Parse error: {e}"]
    except Exception as e:
        return False, [f"Validation error: {e}"]

    # Additional semantic validation
    errors.extend(_validate_requirements(spec))
    errors.extend(_validate_progress(spec))
    errors.extend(_validate_decisions(spec))

    return len(errors) == 0, errors


def _validate_requirements(spec: ProjectSpec) -> list[str]:
    """Validate requirements section."""
    errors: list[str] = []

    if not spec.requirements:
        return errors

    req = spec.requirements

    # Check manufacturing requirements consistency
    if req.manufacturing:
        mfr = req.manufacturing
        if mfr.layers:
            preferred = mfr.layers.get("preferred", 0)
            max_layers = mfr.layers.get("max", preferred)
            if preferred > max_layers:
                errors.append(
                    f"Manufacturing: preferred layers ({preferred}) exceeds max ({max_layers})"
                )

    return errors


def _validate_progress(spec: ProjectSpec) -> list[str]:
    """Validate progress section."""
    errors: list[str] = []

    if not spec.progress:
        return errors

    progress = spec.progress

    # Validate current phase exists in phases dict
    if progress.phases:
        phase_key = progress.phase.value if hasattr(progress.phase, "value") else progress.phase
        if phase_key not in progress.phases:
            errors.append(f"Progress: current phase '{phase_key}' not found in phases")

    return errors


def _validate_decisions(spec: ProjectSpec) -> list[str]:
    """Validate decisions section."""
    errors: list[str] = []

    if not spec.decisions:
        return errors

    for i, decision in enumerate(spec.decisions):
        if not decision.topic:
            errors.append(f"Decision {i + 1}: missing topic")
        if not decision.choice:
            errors.append(f"Decision {i + 1}: missing choice")
        if not decision.rationale:
            errors.append(f"Decision {i + 1}: missing rationale")

    return errors


def create_minimal_spec(name: str, **kwargs: Any) -> ProjectSpec:
    """Create a minimal valid ProjectSpec.

    Args:
        name: Project name
        **kwargs: Additional fields to set

    Returns:
        New ProjectSpec instance
    """
    from datetime import date

    from .schema import DesignIntent, ProjectMetadata

    return ProjectSpec(
        project=ProjectMetadata(
            name=name,
            created=date.today(),
            **{k: v for k, v in kwargs.items() if k in ProjectMetadata.model_fields},
        ),
        intent=DesignIntent(
            summary=kwargs.get("summary", f"{name} project"),
        ),
    )


def get_template(template_name: str = "minimal") -> str:
    """Get a template .kct file content.

    Args:
        template_name: Template name (minimal, power_supply, sensor_board, mcu_breakout)

    Returns:
        Template YAML content

    Raises:
        ValueError: If template name is unknown
    """
    templates = {
        "minimal": _MINIMAL_TEMPLATE,
        "power_supply": _POWER_SUPPLY_TEMPLATE,
        "sensor_board": _SENSOR_BOARD_TEMPLATE,
        "mcu_breakout": _MCU_BREAKOUT_TEMPLATE,
    }

    if template_name not in templates:
        available = ", ".join(templates.keys())
        raise ValueError(f"Unknown template: {template_name}. Available: {available}")

    return templates[template_name]


# Template definitions
_MINIMAL_TEMPLATE = """\
# KiCad Tools Project Specification
# Format version: 1.0
# =============================================================================

kct_version: "1.0"

project:
  name: "My Project"
  revision: "A"
  created: {date}
  author: ""

intent:
  summary: |
    Brief description of what this board does and why.

requirements:
  manufacturing:
    target_fab: jlcpcb
    layers:
      preferred: 2

progress:
  phase: concept
"""

_POWER_SUPPLY_TEMPLATE = """\
# KiCad Tools Project Specification
# Format version: 1.0
# Power Supply Template
# =============================================================================

kct_version: "1.0"

project:
  name: "Power Supply"
  revision: "A"
  created: {date}
  author: ""

intent:
  summary: |
    Regulated power supply board providing stable DC outputs
    for embedded systems or test equipment.

  use_cases:
    - Bench power supply
    - Development board power input
    - Embedded system power

  interfaces:
    - name: AC_INPUT
      type: ac_mains
      voltage: "120-240VAC"

    - name: DC_OUTPUT_5V
      type: power_rail
      voltage: "5V"
      current_max: "3A"

requirements:
  electrical:
    input:
      voltage:
        min: "85VAC"
        max: "265VAC"
      frequency:
        min: "47Hz"
        max: "63Hz"
    outputs:
      - rail: "5V"
        tolerance: "±2%"
        current_max: "3A"
        ripple_max: "50mV_pp"

  mechanical:
    dimensions:
      width: "100mm"
      height: "60mm"

  environmental:
    temperature:
      operating: ["-20°C", "70°C"]

  manufacturing:
    target_fab: jlcpcb
    layers:
      preferred: 4
    min_trace: "0.15mm"
    min_space: "0.15mm"

  compliance:
    standards:
      - FCC_Part15B_ClassB
      - CE
    rohs: true

suggestions:
  components:
    regulator:
      preferred: ["LM7805", "TPS562201"]
      rationale: "Common, well-documented regulators"

  layout:
    - "Input filter near AC input connector"
    - "Thermal vias under power ICs"
    - "Wide traces for power paths"

progress:
  phase: concept
  phases:
    concept:
      status: in_progress
      checklist:
        - [ ] Define power requirements
        - [ ] Select topology
        - [ ] Initial component selection
    schematic:
      status: pending
      checklist:
        - [ ] Power input stage
        - [ ] Regulation stage
        - [ ] Output filtering
        - [ ] Protection circuits
    layout:
      status: pending
      checklist:
        - [ ] Component placement
        - [ ] Power routing
        - [ ] DRC clean
"""

_SENSOR_BOARD_TEMPLATE = """\
# KiCad Tools Project Specification
# Format version: 1.0
# Sensor Board Template
# =============================================================================

kct_version: "1.0"

project:
  name: "Sensor Board"
  revision: "A"
  created: {date}
  author: ""

intent:
  summary: |
    Multi-sensor acquisition board for environmental monitoring
    or data logging applications.

  use_cases:
    - Environmental monitoring
    - Data logging
    - IoT sensor node

  interfaces:
    - name: I2C_SENSORS
      type: i2c
      protocol: "I2C"
      pins: ["SDA", "SCL"]

    - name: POWER
      type: power_rail
      voltage: "3.3V"
      current_max: "100mA"

requirements:
  electrical:
    input:
      voltage:
        min: "3.0V"
        max: "3.6V"
      current:
        max: "100mA"

  mechanical:
    dimensions:
      width: "30mm"
      height: "30mm"

  manufacturing:
    target_fab: jlcpcb
    layers:
      preferred: 2
    min_trace: "0.15mm"

suggestions:
  components:
    temperature_sensor:
      preferred: ["BME280", "SHT40"]
      rationale: "I2C interface, good accuracy"

  layout:
    - "Place sensors away from heat sources"
    - "Short I2C traces"

progress:
  phase: concept
"""

_MCU_BREAKOUT_TEMPLATE = """\
# KiCad Tools Project Specification
# Format version: 1.0
# MCU Breakout Board Template
# =============================================================================

kct_version: "1.0"

project:
  name: "MCU Breakout"
  revision: "A"
  created: {date}
  author: ""

intent:
  summary: |
    Microcontroller breakout board providing easy access to GPIO,
    programming interface, and basic peripherals.

  use_cases:
    - Development and prototyping
    - Learning platform
    - Quick project integration

  interfaces:
    - name: USB
      type: usb_device
      protocol: "USB 2.0 Full Speed"

    - name: GPIO
      type: gpio
      pins: ["PA0-PA15", "PB0-PB15"]

    - name: SWD
      type: debug
      protocol: "ARM SWD"
      pins: ["SWCLK", "SWDIO", "RESET"]

requirements:
  electrical:
    input:
      voltage:
        nominal: "5V"
      current:
        max: "500mA"

  mechanical:
    dimensions:
      width: "50mm"
      height: "25mm"
    mounting_holes:
      - x: "2.5mm"
        y: "2.5mm"
        diameter: "2.2mm"
      - x: "47.5mm"
        y: "22.5mm"
        diameter: "2.2mm"

  manufacturing:
    target_fab: jlcpcb
    layers:
      preferred: 2
    min_trace: "0.2mm"
    min_space: "0.2mm"

suggestions:
  components:
    mcu:
      preferred: ["STM32F103C8T6", "RP2040"]
      rationale: "Popular, well-supported MCUs"
    usb_connector:
      preferred: ["USB-C", "Micro-USB"]

  layout:
    - "USB connector at board edge"
    - "Decoupling caps close to MCU"
    - "Crystal close to MCU with ground guard"
    - "SWD header accessible"

progress:
  phase: concept
  phases:
    concept:
      status: in_progress
      checklist:
        - [ ] Select MCU
        - [ ] Define pinout
        - [ ] Choose form factor
    schematic:
      status: pending
      checklist:
        - [ ] MCU and power
        - [ ] USB interface
        - [ ] Programming header
        - [ ] GPIO breakout
    layout:
      status: pending
      checklist:
        - [ ] Component placement
        - [ ] Routing
        - [ ] DRC clean
"""


def _format_template(template: str) -> str:
    """Format template with current date."""
    from datetime import date

    return template.format(date=date.today().isoformat())
