"""
YAML parser for .kct project specification files.

Provides loading, saving, and validation of .kct files.
"""

from __future__ import annotations

import os
import re
import stat
import tempfile
from pathlib import Path
from typing import Any

import yaml

from .schema import Decision, ProjectSpec, SpecModel

__all__ = [
    "append_decision",
    "collect_unknown_keys",
    "load_spec",
    "save_spec",
    "validate_spec",
    "validate_spec_detailed",
]


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


def _dump_yaml(data: Any) -> str:
    """Serialize a plain data structure with the shared .kct dumper settings."""
    dumped: str = yaml.dump(
        data,
        Dumper=CleanDumper,
        default_flow_style=False,
        sort_keys=False,
        allow_unicode=True,
        width=100,
    )
    return dumped


def _spec_header(kct_version: str) -> str:
    """Build the standard .kct header comment block."""
    return f"""\
# KiCad Tools Project Specification
# Format version: {kct_version}
# Documentation: https://github.com/rjwalters/kicad-tools#spec-format
#
# This file captures design intent, requirements, and progress for the project.
# Edit manually or use: kct spec <command>
# =============================================================================

"""


def _apply_target_mode(tmp_path: Path, path: Path) -> None:
    """Give ``tmp_path`` the permissions ``path`` should end up with.

    ``tempfile.mkstemp`` deliberately creates its file ``0600``, and
    ``os.replace`` carries that mode onto the target -- so a naive atomic write
    silently narrows an existing ``0644`` ``.kct`` to ``0600`` on every save.
    That is exactly the class of unrequested side-effect this module exists to
    avoid, so the mode is set explicitly before the rename:

    * **existing target** -- copy its current permission bits verbatim, so a
      save changes the file's *content* and nothing else;
    * **new target** -- apply the process umask to ``0666``, matching what an
      ordinary ``open(path, "w")`` / :meth:`Path.write_text` would have
      produced (the behaviour of the repo's other atomic-write site).
    """
    try:
        mode = stat.S_IMODE(path.stat().st_mode)
    except OSError:
        # No existing target (or it is unreadable): fall back to umask, which
        # can only be read by temporarily setting it.
        umask = os.umask(0o022)
        os.umask(umask)
        mode = 0o666 & ~umask
    os.chmod(tmp_path, mode)


def _atomic_write_text(path: Path, content: str) -> None:
    """Write ``content`` to ``path`` atomically.

    A failure part-way through serialization must never leave a truncated
    ``.kct`` on disk: the file is written to a sibling temp file first and
    renamed into place only once it is complete. The target's existing
    permission bits are preserved across the rename -- see
    :func:`_apply_target_mode`.
    """
    path = Path(path)
    directory = path.parent if str(path.parent) else Path(".")
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(directory))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        _apply_target_mode(tmp_path, path)
        os.replace(tmp_path, path)
    except BaseException:
        tmp_path.unlink(missing_ok=True)
        raise


def _restore_null_extras(model: Any, dumped: Any) -> None:
    """Re-add unknown keys that ``exclude_none=True`` pruned from ``dumped``.

    ``exclude_none`` is meant to keep *schema* fields the author never set out
    of the output. Applied to an unknown key it is destructive instead: a
    hand-written ``some_key: null`` is content, and dropping it deletes the key
    from the user's file. Pydantic only prunes extras whose own value is
    ``None`` (values *nested* inside an extra are passed through untouched), so
    restoring those is sufficient.
    """
    if isinstance(model, SpecModel) and isinstance(dumped, dict):
        for key, value in (getattr(model, "__pydantic_extra__", None) or {}).items():
            if key not in dumped:
                dumped[key] = value
        for name in type(model).model_fields:
            if name in dumped:
                _restore_null_extras(getattr(model, name, None), dumped[name])
    elif isinstance(model, list) and isinstance(dumped, list) and len(model) == len(dumped):
        for child, child_dumped in zip(model, dumped, strict=True):
            _restore_null_extras(child, child_dumped)
    elif isinstance(model, dict) and isinstance(dumped, dict):
        for key, child in model.items():
            if key in dumped:
                _restore_null_extras(child, dumped[key])


def save_spec(spec: ProjectSpec, path: Path | str, *, exclude_none: bool = True) -> None:
    """Save a ProjectSpec to a .kct file.

    Unknown keys are preserved: every ``.kct`` model derives from
    :class:`~kicad_tools.spec.schema.SpecModel`, which sets ``extra="allow"``,
    so keys the schema does not define survive the load/dump round-trip instead
    of being silently discarded. Comments and quoting style are *not* preserved
    (pyyaml has no round-trip loader) -- see :func:`append_decision` for the
    non-destructive append path used by ``kct spec decide``.

    Args:
        spec: The specification to save
        path: Output file path
        exclude_none: If True, omit None/null *schema* fields from the output.
            Unknown keys are never omitted, even when their value is null --
            a hand-written ``some_key:`` is content, not an unset field.
    """
    path = Path(path)

    # Convert to dict, optionally excluding None values
    data = spec.model_dump(
        exclude_none=exclude_none,
        mode="json",  # Use JSON-compatible serialization for dates
    )

    if exclude_none:
        # ... but never let exclude_none delete an unknown key outright.
        _restore_null_extras(spec, data)

    _atomic_write_text(path, _spec_header(spec.kct_version) + _dump_yaml(data))


# ---------------------------------------------------------------------------
# Append-only decision log
# ---------------------------------------------------------------------------

# Matches a top-level (column-0) mapping key. Block-scalar bodies and nested
# mappings are always indented, so anchoring at column 0 cannot match inside
# them.
_TOP_LEVEL_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*)\s*:(.*)$")


def _render_sequence_item(entry: dict[str, Any], indent: str) -> list[str]:
    """Render ``entry`` as the lines of a YAML block-sequence item."""
    body = _dump_yaml(entry).splitlines()
    rendered: list[str] = []
    for i, line in enumerate(body):
        if not line:
            rendered.append("")
            continue
        prefix = f"{indent}- " if i == 0 else f"{indent}  "
        rendered.append((prefix + line).rstrip())
    return rendered


def _splice_decision(text: str, entry: dict[str, Any]) -> str | None:
    """Textually append ``entry`` to the top-level ``decisions:`` sequence.

    Returns the new file text, or ``None`` if the file's shape is one this
    splicer does not handle (the caller then falls back to a structural
    rewrite). The result is always re-parsed and compared by the caller, so a
    wrong splice can never be written.
    """
    lines = text.splitlines()

    dec_idx: int | None = None
    inline = ""
    for i, line in enumerate(lines):
        match = _TOP_LEVEL_KEY_RE.match(line)
        if match and match.group(1) == "decisions":
            dec_idx = i
            inline = match.group(2).strip()
            break

    # No decisions: key at all -- start the log at the end of the file.
    if dec_idx is None:
        body = list(lines)
        while body and not body[-1].strip():
            body.pop()
        body.append("")
        body.append("decisions:")
        body.extend(_render_sequence_item(entry, "  "))
        return "\n".join(body) + "\n"

    # `decisions: []` -- an explicitly empty log (the template shape).
    if inline == "[]":
        body = list(lines)
        body[dec_idx] = "decisions:"
        body[dec_idx + 1 : dec_idx + 1] = _render_sequence_item(entry, "  ")
        return "\n".join(body) + "\n"

    # Anything else on the same line (a flow sequence with content, an anchor,
    # an alias) is not a shape we splice into.
    if inline and not inline.startswith("#"):
        return None

    # Block sequence: the block runs until the next column-0 non-blank line.
    end = len(lines)
    for j in range(dec_idx + 1, len(lines)):
        stripped = lines[j].strip()
        if not stripped:
            continue
        if not lines[j][0].isspace():
            end = j
            break

    # Do not swallow the blank-line separator before the next section.
    while end > dec_idx + 1 and not lines[end - 1].strip():
        end -= 1

    indent = "  "
    for j in range(dec_idx + 1, end):
        stripped = lines[j].lstrip()
        if stripped.startswith("- "):
            indent = lines[j][: len(lines[j]) - len(stripped)]
            break

    body = list(lines)
    body[end:end] = _render_sequence_item(entry, indent)
    return "\n".join(body) + "\n"


def append_decision(path: Path | str, decision: Decision) -> None:
    """Append a design decision to a ``.kct`` file, append-only.

    This is the write path behind ``kct spec decide``. It is deliberately *not*
    a ``load_spec`` -> mutate -> :func:`save_spec` cycle: that rewrites the
    whole file, which normalizes quoting, drops every comment, and reorders and
    re-serializes decisions that were already there. "Append-only" is a
    durability claim, so the writer honours it literally -- the surrounding
    bytes are left alone and the new entry is spliced in after the last
    existing one.

    The file is still validated (via :func:`load_spec`) before anything is
    written, so a malformed ``.kct`` fails loudly instead of being partially
    rewritten, and the spliced result is re-parsed and compared against the
    expected data before it is committed to disk.

    Args:
        path: Path to the .kct file
        decision: The decision to append

    Raises:
        FileNotFoundError: If the file doesn't exist
        ValueError: If the file is not a valid .kct file
    """
    path = Path(path)

    # Validate before writing: a malformed spec must fail loudly.
    spec = load_spec(path)

    original_text = path.read_text(encoding="utf-8")
    original_data = yaml.safe_load(original_text)
    if not isinstance(original_data, dict):
        raise ValueError(f"Spec file must contain a YAML mapping: {path}")

    entry = decision.model_dump(exclude_none=True, mode="json")

    existing = original_data.get("decisions") or []
    if not isinstance(existing, list):
        raise ValueError(f"'decisions' must be a list in {path}, got {type(existing).__name__}")
    expected = dict(original_data)
    expected["decisions"] = [*existing, entry]

    new_text = _splice_decision(original_text, entry)
    if new_text is not None:
        try:
            reparsed = yaml.safe_load(new_text)
        except yaml.YAMLError:
            reparsed = None
        if reparsed != expected:
            new_text = None  # splice was not faithful -- fall back

    if new_text is None:
        # Structural fallback: rewrite from the *raw* mapping (not the model),
        # so unknown keys and key order survive even though comments do not.
        kct_version = str(expected.get("kct_version", spec.kct_version))
        new_text = _spec_header(kct_version) + _dump_yaml(expected)

    _atomic_write_text(path, new_text)


# ---------------------------------------------------------------------------
# Unknown-key reporting
# ---------------------------------------------------------------------------


def _collect_unknown(obj: Any, path: str) -> list[str]:
    """Recursively collect dotted paths of keys the schema does not define."""
    found: list[str] = []

    if isinstance(obj, SpecModel):
        extra = getattr(obj, "__pydantic_extra__", None) or {}
        for key in extra:
            found.append(f"{path}.{key}" if path else str(key))
        for name in type(obj).model_fields:
            child = getattr(obj, name, None)
            found.extend(_collect_unknown(child, f"{path}.{name}" if path else name))
    elif isinstance(obj, list):
        for i, item in enumerate(obj):
            found.extend(_collect_unknown(item, f"{path}[{i}]"))
    elif isinstance(obj, dict):
        for key, value in obj.items():
            found.extend(_collect_unknown(value, f"{path}.{key}" if path else str(key)))

    return found


def collect_unknown_keys(spec: ProjectSpec) -> list[str]:
    """List dotted paths of keys present in the spec but absent from the schema.

    ``.kct`` models keep unknown keys (``extra="allow"``) so a write can never
    destroy them. That safety would otherwise hide typos and schema drift, so
    this function surfaces them; ``kct spec validate`` reports them as
    warnings.

    Args:
        spec: A loaded ProjectSpec

    Returns:
        Sorted list of dotted key paths, e.g. ``requirements.components``
    """
    return sorted(_collect_unknown(spec, ""))


def validate_spec_detailed(path: Path | str) -> tuple[bool, list[str], list[str]]:
    """Validate a .kct specification file, reporting warnings separately.

    Warnings are non-fatal: today they name keys the schema does not define.
    Such keys are preserved on write (see
    :class:`~kicad_tools.spec.schema.SpecModel`), but they are almost always
    either a typo or schema drift, and surfacing them is what keeps
    ``extra="allow"`` from being a silent trap.

    Args:
        path: Path to the .kct file

    Returns:
        Tuple of (is_valid, list of error messages, list of warning messages)
    """
    errors: list[str] = []

    try:
        spec = load_spec(path)
    except FileNotFoundError as e:
        return False, [str(e)], []
    except ValueError as e:
        return False, [f"Parse error: {e}"], []
    except Exception as e:
        return False, [f"Validation error: {e}"], []

    # Additional semantic validation
    errors.extend(_validate_requirements(spec))
    errors.extend(_validate_progress(spec))
    errors.extend(_validate_decisions(spec))

    warnings = [
        f"Unrecognized key (preserved on write): {key}" for key in collect_unknown_keys(spec)
    ]

    return len(errors) == 0, errors, warnings


def validate_spec(path: Path | str) -> tuple[bool, list[str]]:
    """Validate a .kct specification file.

    Args:
        path: Path to the .kct file

    Returns:
        Tuple of (is_valid, list of error messages)
    """
    is_valid, errors, _warnings = validate_spec_detailed(path)
    return is_valid, errors


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

# Design memory -- an append-only log of material design choices.
# Record one with:
#   kct spec decide project.kct --topic "..." --choice "..." --rationale "..."
# Entries are only ever appended; existing entries are never rewritten.
# (Distinct from `kct decisions`, which queries machine-recorded
#  placement/routing rationale from the autorouter.)
decisions: []

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

# Design memory -- an append-only log of material design choices.
# Record one with:
#   kct spec decide project.kct --topic "..." --choice "..." --rationale "..."
# Entries are only ever appended; existing entries are never rewritten.
# (Distinct from `kct decisions`, which queries machine-recorded
#  placement/routing rationale from the autorouter.)
decisions: []

progress:
  phase: concept
  phases:
    concept:
      status: in_progress
      checklist:
        - "[ ] Define power requirements"
        - "[ ] Select topology"
        - "[ ] Initial component selection"
    schematic:
      status: pending
      checklist:
        - "[ ] Power input stage"
        - "[ ] Regulation stage"
        - "[ ] Output filtering"
        - "[ ] Protection circuits"
    layout:
      status: pending
      checklist:
        - "[ ] Component placement"
        - "[ ] Power routing"
        - "[ ] DRC clean"
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

# Design memory -- an append-only log of material design choices.
# Record one with:
#   kct spec decide project.kct --topic "..." --choice "..." --rationale "..."
# Entries are only ever appended; existing entries are never rewritten.
# (Distinct from `kct decisions`, which queries machine-recorded
#  placement/routing rationale from the autorouter.)
decisions: []

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

# Design memory -- an append-only log of material design choices.
# Record one with:
#   kct spec decide project.kct --topic "..." --choice "..." --rationale "..."
# Entries are only ever appended; existing entries are never rewritten.
# (Distinct from `kct decisions`, which queries machine-recorded
#  placement/routing rationale from the autorouter.)
decisions: []

progress:
  phase: concept
  phases:
    concept:
      status: in_progress
      checklist:
        - "[ ] Select MCU"
        - "[ ] Define pinout"
        - "[ ] Choose form factor"
    schematic:
      status: pending
      checklist:
        - "[ ] MCU and power"
        - "[ ] USB interface"
        - "[ ] Programming header"
        - "[ ] GPIO breakout"
    layout:
      status: pending
      checklist:
        - "[ ] Component placement"
        - "[ ] Routing"
        - "[ ] DRC clean"
"""
