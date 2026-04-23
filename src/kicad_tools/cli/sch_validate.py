#!/usr/bin/env python3
"""
Run all validation checks on a KiCad schematic.

Combines ERC, unconnected pin detection, and other checks into one command.

Usage:
    python3 sch-validate.py <schematic.kicad_sch> [options]

Options:
    --format {text,json}   Output format (default: text)
    --lib-path <path>      Path to symbol libraries (for pin checking)
    --strict               Exit with error on any warning
    --quiet                Only show errors, not warnings

Examples:
    python3 sch-validate.py project.kicad_sch
    python3 sch-validate.py project.kicad_sch --lib-path lib/symbols/
    python3 sch-validate.py project.kicad_sch --strict
"""

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.erc.cross_sheet import filter_cross_sheet_global_labels
from kicad_tools.schema import Schematic
from kicad_tools.schema.hierarchy import build_hierarchy
from kicad_tools.schematic.blocks.interface.debug import DebugHeader


@dataclass
class ValidationIssue:
    """A single validation issue."""

    severity: str  # "error", "warning", "info"
    category: str  # "erc", "unconnected", "footprint", "hierarchy"
    message: str
    location: str = ""  # Sheet or reference
    items: list[str] = field(default_factory=list)  # Contextual items (label/net names)


@dataclass
class ValidationResult:
    """Complete validation results."""

    schematic: str
    issues: list[ValidationIssue] = field(default_factory=list)
    checks_run: list[str] = field(default_factory=list)

    @property
    def error_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for i in self.issues if i.severity == "warning")

    @property
    def passed(self) -> bool:
        return self.error_count == 0


# Violation types whose messages benefit from label/net name enrichment.
_LABEL_TYPES: frozenset[str] = frozenset({
    "isolated_pin_label",
    "single_global_label",
    "label_dangling",
    "global_label_dangling",
    "similar_labels",
    "multiple_net_names",
    "hier_label_mismatch",
})


def run_erc(schematic_path: str) -> list[ValidationIssue]:
    """Run KiCad ERC check."""
    issues = []

    try:
        # Find kicad-cli using the shared lookup that checks PATH
        # and platform-specific installation locations (e.g. macOS app bundle)
        kicad_cli_path = find_kicad_cli()
        if kicad_cli_path is None:
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="erc",
                    message="kicad-cli not found, ERC check skipped",
                )
            )
            return issues

        kicad_cli = str(kicad_cli_path)

        # Run ERC
        import tempfile

        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            output_file = f.name

        try:
            result = subprocess.run(
                [
                    kicad_cli,
                    "sch",
                    "erc",
                    "--format",
                    "json",
                    "--severity-all",
                    "--output",
                    output_file,
                    schematic_path,
                ],
                capture_output=True,
                text=True,
                timeout=60,
            )

            # Parse results
            if Path(output_file).exists():
                import json as json_mod

                with open(output_file) as f:
                    content = f.read()
                    if content.strip():
                        data = json_mod.loads(content)

                        # Collect raw violation dicts so we can filter
                        # cross-sheet false positives before converting.
                        raw_violations: list[dict] = []
                        for sheet in data.get("sheets", []):
                            for violation in sheet.get("violations", []):
                                violation["_sheet_path"] = sheet.get("path", "")
                                raw_violations.append(violation)

                        # Filter false-positive single_global_label /
                        # isolated_pin_label violations for labels that
                        # actually appear on multiple sheets.
                        raw_violations = filter_cross_sheet_global_labels(
                            raw_violations, schematic_path
                        )

                        for violation in raw_violations:
                            item_descs = [
                                i.get("description", "")
                                for i in violation.get("items", [])
                                if i.get("description", "")
                            ]

                            desc = violation.get("description", "Unknown ERC issue")
                            vtype = violation.get("type", "")

                            # Enrich the message with item context for
                            # label-relevant violation types, but only if the
                            # description does not already contain the item
                            # text (some KiCad versions inline it).
                            if vtype in _LABEL_TYPES and item_descs:
                                new_parts = [
                                    d for d in item_descs if d not in desc
                                ]
                                if new_parts:
                                    desc = f"{desc} [{'; '.join(new_parts)}]"

                            issues.append(
                                ValidationIssue(
                                    severity=violation.get("severity", "warning"),
                                    category="erc",
                                    message=desc,
                                    location=violation.get("_sheet_path", ""),
                                    items=item_descs,
                                )
                            )
        finally:
            Path(output_file).unlink(missing_ok=True)

    except subprocess.TimeoutExpired:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="erc",
                message="ERC check timed out",
            )
        )
    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="erc",
                message=f"ERC check failed: {e}",
            )
        )

    return issues


def check_missing_footprints(schematic_path: str) -> list[ValidationIssue]:
    """Check for symbols missing footprints."""
    issues = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                for sym in sch.symbols:
                    # Skip power symbols
                    if sym.lib_id.startswith("power:"):
                        continue

                    # Skip DNP
                    if sym.dnp:
                        continue

                    # Check for missing footprint
                    if not sym.footprint or sym.footprint == "~":
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                category="footprint",
                                message=f"Missing footprint: {sym.reference} ({sym.value})",
                                location=node.get_path_string(),
                            )
                        )
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="footprint",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="footprint",
                message=f"Footprint check failed: {e}",
            )
        )

    return issues


def check_missing_values(schematic_path: str) -> list[ValidationIssue]:
    """Check for symbols missing values."""
    issues = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                for sym in sch.symbols:
                    # Skip power symbols
                    if sym.lib_id.startswith("power:"):
                        continue

                    # Check for missing value
                    if not sym.value or sym.value in ("~", "?"):
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                category="value",
                                message=f"Missing value: {sym.reference}",
                                location=node.get_path_string(),
                            )
                        )
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="value",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="value",
                message=f"Value check failed: {e}",
            )
        )

    return issues


def check_hierarchy(schematic_path: str) -> list[ValidationIssue]:
    """Check hierarchy for issues."""
    issues = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        # Check for unmatched hierarchical labels
        label_map = {}  # label_name -> list of locations

        for node in hierarchy.all_nodes():
            # Labels in this sheet
            for label in node.hierarchical_labels:
                if label not in label_map:
                    label_map[label] = []
                label_map[label].append(("label", node.name))

            # Pins on sheets
            for sheet in node.sheets:
                for pin in sheet.pins:
                    if pin.name not in label_map:
                        label_map[pin.name] = []
                    label_map[pin.name].append(("pin", sheet.name))

        # Check for labels without matching pins
        for name, locations in label_map.items():
            types = [loc[0] for loc in locations]
            if types.count("label") > 0 and types.count("pin") == 0:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="hierarchy",
                        message=f"Hierarchical label '{name}' has no matching sheet pin",
                        location=", ".join(loc[1] for loc in locations if loc[0] == "label"),
                    )
                )

            # Check for pins without matching labels
            if types.count("pin") > 0 and types.count("label") == 0:
                sheet_names = [loc[1] for loc in locations if loc[0] == "pin"]
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="hierarchy",
                        message=f"Sheet pin '{name}' has no matching hierarchical label in sub-schematic",
                        location=", ".join(sheet_names),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="hierarchy",
                message=f"Hierarchy check failed: {e}",
            )
        )

    return issues


def check_no_connect_on_input_pins(schematic_path: str) -> list[ValidationIssue]:
    """Flag no-connect markers placed on pins typed 'input' in the library.

    Input pins typically require a defined logic state.  Placing a no-connect
    flag on one silences the unconnected-pin check but may hide a real design
    issue (e.g. an active-low control left floating instead of being tied to a
    pull resistor).

    Only ``input`` pins are flagged -- ``passive``, ``no_connect``, and other
    types are intentionally excluded because no-connect markers on those are
    standard practice.
    """
    issues: list[ValidationIssue] = []

    try:
        from kicad_tools.schematic.models import Schematic as OpSchematic

        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = OpSchematic.load(node.path)

                nc_points = {(round(nc.x, 2), round(nc.y, 2)) for nc in sch.no_connects}
                if not nc_points:
                    continue

                for sym in sch.symbols:
                    # Skip power symbols -- their pins are always power_in/power_out
                    if sym.symbol_def.lib_id.startswith("power:"):
                        continue

                    for pin in sym.symbol_def.pins:
                        if pin.pin_type != "input":
                            continue

                        pos = sym.pin_position(pin.number)
                        pos_r = (round(pos[0], 2), round(pos[1], 2))

                        if pos_r in nc_points:
                            display = pin.name if pin.name and pin.name != "~" else pin.number
                            issues.append(
                                ValidationIssue(
                                    severity="info",
                                    category="no_connect",
                                    message=(
                                        f"No-connect on input pin {display} "
                                        f"(pin {pin.number}) of {sym.reference} "
                                        f"({sym.value}) -- verify this pin does "
                                        f"not need a defined state"
                                    ),
                                    location=node.get_path_string(),
                                )
                            )
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="no_connect",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="no_connect",
                message=f"No-connect input pin check failed: {e}",
            )
        )

    return issues


def check_global_label_directions(schematic_path: str) -> list[ValidationIssue]:
    """Check global label driver/receiver direction mismatches.

    Groups global labels by net name across all sheets and checks that each
    net has at least one driver and at least one receiver.

    Direction semantics:
      - ``output`` / ``tri_state``: driver only
      - ``input``: receiver only
      - ``bidirectional`` / ``passive``: counts as both driver and receiver
    """
    issues: list[ValidationIssue] = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        # Collect global labels across all sheets: net_name -> list of (shape, sheet_path)
        label_map: dict[str, list[tuple[str, str]]] = {}

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                sheet_path = node.get_path_string()
                for gl in sch.global_labels:
                    if gl.text not in label_map:
                        label_map[gl.text] = []
                    label_map[gl.text].append((gl.shape, sheet_path))
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="global_label",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

        # Shapes that count as driver (can source a signal)
        driver_shapes = {"output", "tri_state", "bidirectional", "passive"}
        # Shapes that count as receiver (can sink a signal)
        receiver_shapes = {"input", "bidirectional", "passive"}

        for net_name, entries in sorted(label_map.items()):
            shapes = {shape for shape, _ in entries}
            sheets = sorted({sheet for _, sheet in entries})
            has_driver = bool(shapes & driver_shapes)
            has_receiver = bool(shapes & receiver_shapes)

            if not has_driver:
                # All instances are input -- no driver exists
                shapes_str = ", ".join(sorted(shapes))
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="global_label",
                        message=(
                            f"Global label '{net_name}' has no driver "
                            f"(shapes: {shapes_str})"
                        ),
                        location=", ".join(sheets),
                    )
                )
            elif not has_receiver:
                # All instances are output/tri_state -- no receiver exists
                shapes_str = ", ".join(sorted(shapes))
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="global_label",
                        message=(
                            f"Global label '{net_name}' has no receiver "
                            f"(shapes: {shapes_str})"
                        ),
                        location=", ".join(sheets),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="global_label",
                message=f"Global label direction check failed: {e}",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Connector pinout verification against known interface standards
# ---------------------------------------------------------------------------

# Interface standard catalog: each entry maps a standard name to its
# identifier signals (used for matching) and expected pin-to-signal map.
# The catalog is data-driven so new standards can be added without
# changing check logic.

_INTERFACE_CATALOG: list[dict] = [
    {
        "name": "ARM SWD 6-pin",
        "identifier_signals": {"SWDIO", "SWCLK"},
        "pin_count": 6,
        "pinout": DebugHeader.SWD_6PIN_PINOUT,
    },
    {
        "name": "ARM SWD 10-pin",
        "identifier_signals": {"SWDIO", "SWCLK"},
        "pin_count": 10,
        "pinout": DebugHeader.SWD_10PIN_PINOUT,
    },
    {
        "name": "ARM JTAG 20-pin",
        "identifier_signals": {"TDI", "TDO", "TMS", "TCK"},
        "pin_count": 20,
        "pinout": DebugHeader.JTAG_20PIN_PINOUT,
    },
]

# Power-rail net names that should be normalised to generic labels when
# comparing against the standard pinout.  For example, "+3.3V" on a VCC
# pin is correct, not a mismatch.
_POWER_ALIASES: dict[str, str] = {
    "+3.3V": "VCC",
    "+3V3": "VCC",
    "+5V": "VCC",
    "+1.8V": "VCC",
    "VBUS": "VCC",
    "VDD": "VCC",
    "VREF": "VCC",
}


def _normalise_signal(net_name: str | None) -> str | None:
    """Normalise a net name for comparison against standard pinouts.

    Strips common power-rail prefixes to their generic form so that
    ``+3.3V`` matches the ``VCC`` entry in the standard pinout.
    """
    if net_name is None:
        return None
    return _POWER_ALIASES.get(net_name, net_name)


def _match_interface(
    signal_set: set[str], pin_count: int
) -> dict | None:
    """Find the best matching interface standard for a set of signals.

    Returns the catalog entry or ``None`` if no standard matches.
    Matching requires all identifier signals to be present and the
    connector pin count to equal the standard's expected count.
    """
    for entry in _INTERFACE_CATALOG:
        if entry["pin_count"] != pin_count:
            continue
        if entry["identifier_signals"].issubset(signal_set):
            return entry
    return None


def check_connector_pinout(schematic_path: str) -> list[ValidationIssue]:
    """Verify connector pinouts against known interface standards.

    For each generic connector symbol, builds a pin-to-net map using
    the wire-graph flood-fill from ``sch_pin_map``, then checks whether
    the net assignment matches a known standard (SWD, JTAG, etc.).

    Only connectors whose signals match a known interface are checked;
    connectors with arbitrary nets are silently skipped.
    """
    issues: list[ValidationIssue] = []

    try:
        from kicad_tools.cli.sch_pin_map import resolve_pin_map

        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                pin_map = resolve_pin_map(sch)

                for ref, entry in pin_map.items():
                    lib_id: str = entry.get("lib_id", "")

                    # Only check generic connectors and pin headers
                    if not (
                        lib_id.startswith("Connector_Generic:")
                        or lib_id.startswith("Connector_PinHeader_")
                        or lib_id.startswith("Connector:")
                    ):
                        continue

                    pins_data: dict[str, dict] = entry.get("pins", {})
                    pin_count = len(pins_data)

                    # Build {pin_number: normalised_net_name}
                    actual_pinout: dict[str, str | None] = {}
                    signal_set: set[str] = set()
                    for pin_num, pin_info in pins_data.items():
                        raw_net = pin_info.get("net")
                        norm = _normalise_signal(raw_net)
                        actual_pinout[pin_num] = norm
                        if norm is not None:
                            signal_set.add(norm)

                    # Try to match against a known interface
                    matched = _match_interface(signal_set, pin_count)
                    if matched is None:
                        continue

                    standard_name = matched["name"]
                    expected_pinout = matched["pinout"]

                    # Compare each pin
                    for pin_num, expected_signal in expected_pinout.items():
                        # Skip non-functional pins (NC, KEY)
                        if expected_signal in ("NC", "KEY"):
                            continue

                        actual_signal = actual_pinout.get(pin_num)
                        expected_norm = _normalise_signal(expected_signal)

                        if actual_signal is None:
                            # Unconnected pin where a signal is expected
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    category="connector_pinout",
                                    message=(
                                        f"{ref} pin {pin_num}: expected "
                                        f"{expected_signal}, got unconnected "
                                        f"({standard_name})"
                                    ),
                                    location=node.get_path_string(),
                                )
                            )
                        elif actual_signal != expected_norm:
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    category="connector_pinout",
                                    message=(
                                        f"{ref} pin {pin_num}: expected "
                                        f"{expected_signal}, got {actual_signal} "
                                        f"({standard_name})"
                                    ),
                                    location=node.get_path_string(),
                                )
                            )

            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="connector_pinout",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="connector_pinout",
                message=f"Connector pinout check failed: {e}",
            )
        )

    return issues


def check_missing_project_instances(schematic_path: str) -> list[ValidationIssue]:
    """Check for symbols missing the ``instances`` block.

    In KiCad 8+, every placed symbol must have an ``(instances ...)`` child
    node that registers it to a project path.  Without this block the
    component is invisible to the netlist exporter and BOM generator despite
    being visually present on the schematic.

    The check skips:
    - Power symbols (``lib_id`` starting with ``power:``)
    - Symbols with both ``in_bom=no`` and ``on_board=no`` (graphical-only)

    Multi-unit symbols are deduplicated by (reference, lib_id) so that a missing
    ``instances`` block on a two-unit IC produces a single warning, not one
    per unit.
    """
    issues: list[ValidationIssue] = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                # Track UUIDs already flagged to deduplicate multi-unit ICs.
                # Multi-unit symbols share the same base UUID (only the first
                # unit carries the instances block in the raw file, but after
                # parsing each unit becomes a separate SymbolInstance sharing
                # the same lib_id + reference).  Deduplicate by reference +
                # lib_id so we report once per logical component.
                seen: set[tuple[str, str]] = set()

                for sym in sch.symbols:
                    # Skip power symbols
                    if sym.lib_id.startswith("power:"):
                        continue

                    # Skip graphical-only symbols (not in BOM and not on board)
                    if not sym.in_bom and not sym.on_board:
                        continue

                    # Deduplicate multi-unit symbols
                    dedup_key = (sym.reference, sym.lib_id)
                    if dedup_key in seen:
                        continue

                    # Check for instances block in raw S-expression
                    has_instances = False
                    if sym._sexp is not None:
                        if sym._sexp.find("instances") is not None:
                            has_instances = True
                    else:
                        # Programmatically-created symbol without _sexp:
                        # skip with info if desired, but don't flag as missing
                        continue

                    if not has_instances:
                        seen.add(dedup_key)
                        ref = sym.reference or "?"
                        val = sym.value or "?"
                        issues.append(
                            ValidationIssue(
                                severity="warning",
                                category="project_instances",
                                message=(
                                    f"Missing project instances block: "
                                    f"{ref} ({val}) - will be absent from "
                                    f"netlist and BOM"
                                ),
                                location=node.get_path_string(),
                            )
                        )
                    else:
                        # Mark as seen even when instances are present, so
                        # other units of the same IC don't get flagged.
                        seen.add(dedup_key)

            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="project_instances",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="project_instances",
                message=f"Project instances check failed: {e}",
            )
        )

    return issues


def check_duplicate_references(schematic_path: str) -> list[ValidationIssue]:
    """Detect duplicate reference designators across hierarchical sheets.

    Collects all component references across the full sheet hierarchy and
    flags any reference that appears on multiple sheets with different
    component UUIDs.  Multi-unit symbols (e.g. U1 unit 1 and U1 unit 2)
    are expected to share the same reference and ``lib_id`` but occupy
    different unit slots -- these are **not** flagged as duplicates.

    A conflict is reported when a reference appears more than once with
    either:
    - a different ``lib_id`` (definitively different components), or
    - the same ``lib_id`` but duplicate ``unit`` numbers (same unit placed
      on two sheets -- not a multi-unit split).
    """
    issues: list[ValidationIssue] = []

    try:
        hierarchy = build_hierarchy(schematic_path)

        # ref -> list of (uuid, lib_id, value, unit, sheet_path)
        ref_map: dict[str, list[tuple[str, str, str, int, str]]] = {}

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                sheet_path = node.get_path_string()
                for sym in sch.symbols:
                    # Skip power symbols -- they reuse refs like #PWR01
                    if sym.lib_id.startswith("power:"):
                        continue

                    ref = sym.reference
                    if not ref or ref == "?" or ref.startswith("#"):
                        continue

                    if ref not in ref_map:
                        ref_map[ref] = []
                    ref_map[ref].append(
                        (sym.uuid, sym.lib_id, sym.value, sym.unit, sheet_path)
                    )
            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="duplicate_reference",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

        for ref, entries in sorted(ref_map.items()):
            if len(entries) < 2:
                continue

            # Check whether all entries share the same lib_id
            lib_ids = {lib_id for _, lib_id, _, _, _ in entries}

            if len(lib_ids) > 1:
                # Different components with same reference -- always a conflict
                sheets = sorted({sp for _, _, _, _, sp in entries})
                values = sorted({v for _, _, v, _, _ in entries if v})
                val_str = f" (values: {', '.join(values)})" if values else ""
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="duplicate_reference",
                        message=(
                            f"Duplicate reference {ref} across sheets"
                            f"{val_str}"
                        ),
                        location=", ".join(sheets),
                    )
                )
            else:
                # Same lib_id -- check for duplicate unit numbers which
                # would indicate distinct components rather than a
                # multi-unit split.
                unit_counts: dict[int, list[str]] = {}
                for _, _, _, unit, sheet_path in entries:
                    if unit not in unit_counts:
                        unit_counts[unit] = []
                    unit_counts[unit].append(sheet_path)

                dup_units = {
                    u: sheets
                    for u, sheets in unit_counts.items()
                    if len(sheets) > 1
                }
                if dup_units:
                    all_sheets = sorted(
                        {s for sheets in dup_units.values() for s in sheets}
                    )
                    value = entries[0][2]
                    val_str = f" ({value})" if value else ""
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="duplicate_reference",
                            message=(
                                f"Duplicate reference {ref}{val_str} "
                                f"-- same unit on multiple sheets"
                            ),
                            location=", ".join(all_sheets),
                        )
                    )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="duplicate_reference",
                message=f"Duplicate reference check failed: {e}",
            )
        )

    return issues


def validate_schematic(schematic_path: str, lib_paths: list[str] = None) -> ValidationResult:
    """Run all validation checks."""
    result = ValidationResult(schematic=schematic_path)

    # ERC
    result.checks_run.append("erc")
    result.issues.extend(run_erc(schematic_path))

    # Missing footprints
    result.checks_run.append("footprints")
    result.issues.extend(check_missing_footprints(schematic_path))

    # Missing values
    result.checks_run.append("values")
    result.issues.extend(check_missing_values(schematic_path))

    # Hierarchy
    result.checks_run.append("hierarchy")
    result.issues.extend(check_hierarchy(schematic_path))

    # No-connect on input pins
    result.checks_run.append("no_connect_input")
    result.issues.extend(check_no_connect_on_input_pins(schematic_path))

    # Global label directions
    result.checks_run.append("global_label_directions")
    result.issues.extend(check_global_label_directions(schematic_path))

    # Connector pinout verification
    result.checks_run.append("connector_pinout")
    result.issues.extend(check_connector_pinout(schematic_path))

    # Missing project instances
    result.checks_run.append("project_instances")
    result.issues.extend(check_missing_project_instances(schematic_path))

    # Duplicate references across sheets
    result.checks_run.append("duplicate_references")
    result.issues.extend(check_duplicate_references(schematic_path))

    return result


def main(argv=None):
    parser = argparse.ArgumentParser(
        description="Validate a KiCad schematic",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("schematic", help="Path to .kicad_sch file")
    parser.add_argument("--format", choices=["text", "json"], default="text", help="Output format")
    parser.add_argument(
        "--lib-path", action="append", dest="lib_paths", help="Path to symbol libraries"
    )
    parser.add_argument("--strict", action="store_true", help="Exit with error on any warning")
    parser.add_argument("--quiet", "-q", action="store_true", help="Only show errors")

    args = parser.parse_args(argv)

    if not Path(args.schematic).exists():
        print(f"Error: File not found: {args.schematic}", file=sys.stderr)
        sys.exit(1)

    result = validate_schematic(args.schematic, args.lib_paths)

    if args.format == "json":
        print(
            json.dumps(
                {
                    "schematic": result.schematic,
                    "passed": result.passed,
                    "error_count": result.error_count,
                    "warning_count": result.warning_count,
                    "checks_run": result.checks_run,
                    "issues": [
                        {
                            "severity": i.severity,
                            "category": i.category,
                            "message": i.message,
                            "location": i.location,
                            "items": i.items,
                        }
                        for i in result.issues
                        if not args.quiet or i.severity == "error"
                    ],
                },
                indent=2,
            )
        )
    else:
        print_result(result, args.quiet)

    # Exit code
    if result.error_count > 0:
        sys.exit(1)
    if args.strict and result.warning_count > 0:
        sys.exit(1)


def print_result(result: ValidationResult, quiet: bool = False):
    """Print validation results."""
    print(f"Validation: {Path(result.schematic).name}")
    print("=" * 60)

    if result.passed and result.warning_count == 0:
        print("✅ All checks passed!")
    elif result.passed:
        print(f"⚠️  Passed with {result.warning_count} warnings")
    else:
        print(f"❌ Failed: {result.error_count} errors, {result.warning_count} warnings")

    print(f"\nChecks run: {', '.join(result.checks_run)}")

    # Group issues by category
    by_category = {}
    for issue in result.issues:
        if quiet and issue.severity != "error":
            continue
        if issue.category not in by_category:
            by_category[issue.category] = []
        by_category[issue.category].append(issue)

    if by_category:
        print("\nIssues:")
        for category, issues in sorted(by_category.items()):
            print(f"\n[{category.upper()}]")
            for issue in issues[:10]:  # Limit to 10 per category
                if issue.severity == "error":
                    icon = "❌"
                elif issue.severity == "info":
                    icon = "ℹ️"
                else:
                    icon = "⚠️"
                loc = f" ({issue.location})" if issue.location else ""
                print(f"  {icon} {issue.message}{loc}")
                for item in issue.items:
                    print(f"       {item}")
            if len(issues) > 10:
                print(f"  ... and {len(issues) - 10} more")


if __name__ == "__main__":
    main()
