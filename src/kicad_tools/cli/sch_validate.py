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
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.erc.cross_sheet import (
    filter_cross_sheet_global_labels,
    filter_cross_sheet_power_violations,
)
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

                        # Filter false-positive power_pin_not_driven
                        # violations for power nets that have a
                        # power_out driver on another sheet.
                        raw_violations = filter_cross_sheet_power_violations(
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

            # Semantic shape-combination checks (only warn on real problems)
            if len(shapes) > 1:
                exclusive_drivers = shapes & {"output", "tri_state"}
                # Multiple exclusive drivers on the same net is a potential
                # driver conflict (e.g. output + tri_state, or the code could
                # be extended to count instances).  Complementary pairs like
                # output+input or tri_state+input are valid by design.
                if len(exclusive_drivers) > 1:
                    shape_locations: dict[str, list[str]] = {}
                    for shape, sheet in entries:
                        if shape not in shape_locations:
                            shape_locations[shape] = []
                        shape_locations[shape].append(sheet)
                    detail = "; ".join(
                        f"{s} on {', '.join(sorted(set(locs)))}"
                        for s, locs in sorted(shape_locations.items())
                    )
                    issues.append(
                        ValidationIssue(
                            severity="warning",
                            category="global_label",
                            message=(
                                f"Global label '{net_name}' has conflicting "
                                f"driver shapes: {detail}"
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


# ---------------------------------------------------------------------------
# Pin-net semantic mismatch detection
# ---------------------------------------------------------------------------

# Power-positive keywords (case-insensitive matching)
_POWER_POSITIVE_KEYWORDS: frozenset[str] = frozenset({
    "VCC", "VDD", "VBUS", "3V3", "5V", "AVCC", "DVCC", "AVDD", "DVDD",
    "PVDD", "IOVDD",
})

# Power-negative keywords
_POWER_NEGATIVE_KEYWORDS: frozenset[str] = frozenset({
    "GND", "VSS", "AGND", "DGND", "PGND", "GNDA", "GNDD",
})

# Bus protocol keyword groups: protocol -> set of signal keywords
_BUS_PROTOCOLS: dict[str, set[str]] = {
    "I2C": {"SDA", "SCL"},
    "SPI": {"MOSI", "MISO", "SCK", "SCLK", "CS", "SS", "NSS"},
    "I2S": {"BCLK", "LRCLK", "DIN", "DOUT", "SDIN", "SDOUT", "WS", "MCK"},
    "UART": {"TX", "RX", "TXD", "RXD"},
}

# All bus signal keywords (flattened)
_ALL_BUS_KEYWORDS: set[str] = set()
for _proto_signals in _BUS_PROTOCOLS.values():
    _ALL_BUS_KEYWORDS.update(_proto_signals)

# Map each keyword back to its protocol
_KEYWORD_TO_PROTOCOL: dict[str, str] = {}
for _proto, _signals in _BUS_PROTOCOLS.items():
    for _sig in _signals:
        _KEYWORD_TO_PROTOCOL[_sig] = _proto


def _tokenize_name(name: str) -> set[str]:
    """Tokenize a pin or net name into uppercase keyword tokens.

    Splits on ``_``, ``/``, ``-`` separators and camelCase boundaries
    (lowercase-to-uppercase).  Does NOT split within uppercase+digit
    sequences like ``I2S``, ``SPI0``, ``I2C1`` -- these are kept as
    single tokens since they represent common protocol abbreviations.

    Returns the set of uppercase tokens.
    """
    if not name or name == "~":
        return set()
    # Replace common separators with space
    s = name.replace("/", " ").replace("_", " ").replace("-", " ")
    # Insert spaces at camelCase boundaries (lowercase followed by uppercase)
    s = re.sub(r"([a-z])([A-Z])", r"\1 \2", s)
    return {t.upper() for t in s.split() if t}


def _find_protocol(tokens: set[str]) -> str | None:
    """Return the bus protocol name if any token matches a bus keyword."""
    for token in tokens:
        if token in _KEYWORD_TO_PROTOCOL:
            return _KEYWORD_TO_PROTOCOL[token]
    return None


def _is_generic_pin_name(name: str) -> bool:
    """Return True if a pin name is generic (numeric only, tilde, empty)."""
    if not name or name == "~":
        return True
    # Purely numeric pin names like "1", "2"
    stripped = name.strip()
    if stripped.isdigit():
        return True
    # Names like "P1", "P2" on generic connectors
    if re.match(r"^[Pp]\d+$", stripped):
        return True
    return False


def _is_passive_component(lib_id: str) -> bool:
    """Return True for passive component lib_ids (R, C, L, etc.)."""
    # Common passive library prefixes
    part = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    passive_prefixes = ("R", "C", "L", "D", "FB", "Ferrite")
    # Match R, R_Small, C, C_Polarized, L, etc.
    for prefix in passive_prefixes:
        if part == prefix or part.startswith(prefix + "_"):
            return True
    return False


def _is_resistor(lib_id: str) -> bool:
    """Return True if *lib_id* refers to a resistor symbol (R, R_Small, etc.)."""
    part = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    return part == "R" or part.startswith("R_")


def _is_capacitor(lib_id: str) -> bool:
    """Return True if *lib_id* refers to a capacitor symbol (C, C_Small, C_Polarized, etc.)."""
    part = lib_id.split(":")[-1] if ":" in lib_id else lib_id
    return part == "C" or part.startswith("C_")


def _is_stm32(lib_id: str) -> bool:
    """Return True if *lib_id* refers to an STM32 MCU symbol.

    Matches the ``MCU_ST_STM32`` prefix used by the standard KiCad
    symbol libraries for all STM32 families (F0/F1/F4/G0/H7/etc.).
    """
    return "MCU_ST_STM32" in lib_id


def _is_nrst_pin(pin_name: str) -> bool:
    """Return True if *pin_name* represents an NRST (reset) pin.

    Handles common notations: ``NRST``, ``~{NRST}``, ``nRST``.
    """
    # Strip KiCad active-low markup: ~{NRST} -> NRST
    cleaned = re.sub(r"~\{([^}]+)\}", r"\1", pin_name)
    return cleaned.upper() == "NRST"


def _parse_capacitance(value: str) -> float | None:
    """Parse a capacitor value string to farads.

    Handles common notations like ``100n``, ``100nF``, ``1u``, ``0.1uF``.
    Returns ``None`` if the value cannot be parsed.
    """
    multipliers = {
        "p": 1e-12,
        "n": 1e-9,
        "u": 1e-6,
        "\u00b5": 1e-6,  # µ
        "m": 1e-3,
    }

    value = value.strip()
    if not value:
        return None

    # Remove farad unit suffix
    for unit in ["F", "f"]:
        value = value.rstrip(unit)

    value = value.strip()
    if not value:
        return None

    # Check for multiplier suffix
    multiplier = 1.0
    if value and value[-1] in multipliers:
        multiplier = multipliers[value[-1]]
        value = value[:-1]

    value = value.strip()
    if not value:
        return None

    try:
        return float(value) * multiplier
    except ValueError:
        return None


def _is_i2c_net(net_name: str) -> bool:
    """Return True if *net_name* represents an I2C signal (SDA or SCL).

    Uses ``_tokenize_name`` to split the name into tokens and checks for
    I2C bus keywords.  This avoids false positives on names like ``PRESCALER``
    because the tokenizer splits on separators and camelCase boundaries, so
    ``PRESCALER`` tokenizes to ``{PRESCALER}`` which does not match ``SCL``.

    Trailing digits are stripped from tokens to handle common variants like
    ``SCL0``, ``SDA1`` where a bus instance number is appended directly.
    """
    tokens = _tokenize_name(net_name)
    i2c_keywords = _BUS_PROTOCOLS["I2C"]  # {"SDA", "SCL"}
    if tokens & i2c_keywords:
        return True
    # Also check tokens with trailing digits stripped (SCL0 -> SCL)
    stripped_tokens = {re.sub(r"\d+$", "", t) for t in tokens}
    return bool(stripped_tokens & i2c_keywords)


def check_i2c_pullups(schematic_path: str) -> list[ValidationIssue]:
    """Warn when I2C nets (SCL / SDA) lack pull-up resistors.

    I2C is an open-drain bus -- without pull-up resistors the bus cannot
    transition to logic high.  This check walks every sheet, identifies I2C
    nets, and verifies that at least one resistor connects each I2C net to a
    power-positive rail.

    A pull-up is considered present when a component whose ``lib_id``
    indicates a resistor (via ``_is_resistor``) has one pin on the I2C net
    and another pin on a net whose name tokenizes to a power-positive keyword
    (using ``_POWER_POSITIVE_KEYWORDS``).
    """
    issues: list[ValidationIssue] = []

    try:
        from kicad_tools.cli.sch_pin_map import resolve_pin_map

        hierarchy = build_hierarchy(schematic_path)

        # Global maps accumulated across all sheets:
        #   i2c_nets: set of net names identified as I2C
        #   net_to_sheets: net_name -> set of sheet paths where the net appears
        #   resistor_nets: ref -> list of net names the resistor's pins connect to
        i2c_nets: set[str] = set()
        net_to_sheets: dict[str, set[str]] = {}
        # ref -> [net_name, ...]  (one entry per pin)
        resistor_pin_nets: dict[str, list[str]] = {}

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                pin_map = resolve_pin_map(sch)
                sheet_path = node.get_path_string()

                for ref, entry in pin_map.items():
                    lib_id: str = entry.get("lib_id", "")
                    pins_data: dict[str, dict] = entry.get("pins", {})

                    for pin_num, pin_info in pins_data.items():
                        net_name = pin_info.get("net")
                        if net_name is None:
                            continue

                        # Track which sheets each net appears on
                        net_to_sheets.setdefault(net_name, set()).add(sheet_path)

                        # Identify I2C nets
                        if _is_i2c_net(net_name):
                            i2c_nets.add(net_name)

                    # Track resistor pin-to-net connections
                    if _is_resistor(lib_id):
                        nets_for_ref: list[str] = []
                        for pin_num, pin_info in pins_data.items():
                            n = pin_info.get("net")
                            if n is not None:
                                nets_for_ref.append(n)
                        # Accumulate across sheets (same ref may appear on
                        # different sheets in hierarchical designs, but
                        # typically a resistor only appears once).
                        if ref not in resistor_pin_nets:
                            resistor_pin_nets[ref] = nets_for_ref
                        else:
                            resistor_pin_nets[ref].extend(nets_for_ref)

            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="i2c_pullups",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

        # Determine which I2C nets have a valid pull-up resistor.
        # A valid pull-up: resistor has one pin on the I2C net AND another
        # pin on a power-positive net.
        def _is_power_positive_net(net_name: str) -> bool:
            # Tokenize as-is first
            tokens = _tokenize_name(net_name)
            if tokens & _POWER_POSITIVE_KEYWORDS:
                return True
            # Strip leading +/- (common in power net names like +3V3, +5V)
            stripped = net_name.lstrip("+-")
            if stripped != net_name:
                tokens2 = _tokenize_name(stripped)
                if tokens2 & _POWER_POSITIVE_KEYWORDS:
                    return True
            # Match common voltage patterns: +3.3V, +5V, +1.8V, etc.
            if re.match(r"^\+?\d+(\.\d+)?V$", net_name):
                return True
            return False

        i2c_nets_with_pullup: set[str] = set()
        for _ref, pin_net_list in resistor_pin_nets.items():
            net_set = set(pin_net_list)
            i2c_on_resistor = net_set & i2c_nets
            power_on_resistor = {n for n in net_set if _is_power_positive_net(n)}
            if i2c_on_resistor and power_on_resistor:
                i2c_nets_with_pullup.update(i2c_on_resistor)

        # Emit warnings for I2C nets missing pull-ups
        for net_name in sorted(i2c_nets - i2c_nets_with_pullup):
            sheets = sorted(net_to_sheets.get(net_name, set()))
            sheet_str = ", ".join(sheets) if sheets else "unknown"
            issues.append(
                ValidationIssue(
                    severity="warning",
                    category="i2c_pullups",
                    message=(
                        f"I2C net '{net_name}' has no pull-up resistor to a "
                        f"power rail"
                    ),
                    location=sheet_str,
                )
            )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="i2c_pullups",
                message=f"I2C pull-up check failed: {e}",
            )
        )

    return issues


def check_pin_net_semantic_mismatch(schematic_path: str) -> list[ValidationIssue]:
    """Flag pins where the connected net name has no semantic overlap with the pin name.

    For each non-power, non-passive symbol, resolves pin-to-net assignments
    and compares signal-type keywords between pin names and net names.
    Also detects systematic N-pin offset patterns within a single symbol.
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

                    # Skip power symbols
                    if lib_id.startswith("power:"):
                        continue

                    # Skip passive components
                    if _is_passive_component(lib_id):
                        continue

                    pins_data: dict[str, dict] = entry.get("pins", {})

                    # Collect mismatches for offset detection
                    # List of (pin_num_int, pin_name, net_name, pin_protocol, net_protocol)
                    mismatches: list[tuple[int, str, str, str | None, str | None]] = []

                    for pin_num, pin_info in pins_data.items():
                        pin_name = pin_info.get("name", "")
                        net_name = pin_info.get("net")
                        pin_type = pin_info.get("type", "")

                        # Skip unconnected pins
                        if net_name is None:
                            continue

                        # Skip generic pin names
                        if _is_generic_pin_name(pin_name):
                            continue

                        # Skip power pins (they are checked by power_short)
                        if pin_type in ("power_in", "power_out"):
                            continue

                        pin_tokens = _tokenize_name(pin_name)
                        net_tokens = _tokenize_name(net_name)

                        if not pin_tokens or not net_tokens:
                            continue

                        pin_protocol = _find_protocol(pin_tokens)
                        net_protocol = _find_protocol(net_tokens)

                        # Check for protocol mismatch
                        if pin_protocol and net_protocol and pin_protocol != net_protocol:
                            pin_num_int = int(pin_num) if pin_num.isdigit() else -1
                            mismatches.append(
                                (pin_num_int, pin_name, net_name, pin_protocol, net_protocol)
                            )
                            continue

                        # Check for bus keyword mismatch
                        pin_bus = pin_tokens & _ALL_BUS_KEYWORDS
                        net_bus = net_tokens & _ALL_BUS_KEYWORDS

                        if pin_bus and net_bus and not (pin_bus & net_bus):
                            # Both have bus keywords but no overlap
                            pin_num_int = int(pin_num) if pin_num.isdigit() else -1
                            mismatches.append(
                                (pin_num_int, pin_name, net_name, pin_protocol, net_protocol)
                            )
                        elif net_bus and not pin_bus and net_protocol:
                            # Net has bus keywords but pin does not -- the net
                            # is likely on the wrong pin (e.g., I2S_DIN on a
                            # MODE pin)
                            pin_num_int = int(pin_num) if pin_num.isdigit() else -1
                            mismatches.append(
                                (pin_num_int, pin_name, net_name, pin_protocol, net_protocol)
                            )
                        elif pin_bus and not net_bus and pin_protocol:
                            # Pin has bus keywords but net does not -- a bus
                            # pin connected to a non-bus net
                            pin_num_int = int(pin_num) if pin_num.isdigit() else -1
                            mismatches.append(
                                (pin_num_int, pin_name, net_name, pin_protocol, net_protocol)
                            )

                    if not mismatches:
                        continue

                    # Check for systematic offset pattern
                    offset_detected = _detect_systematic_offset(
                        mismatches, pins_data, ref, node.get_path_string(), issues
                    )

                    if not offset_detected:
                        # Emit individual warnings for each mismatch
                        for _, pin_name, net_name, pin_proto, net_proto in mismatches:
                            proto_info = ""
                            if pin_proto and net_proto and pin_proto != net_proto:
                                proto_info = (
                                    f" (pin expects {pin_proto}, "
                                    f"net is {net_proto})"
                                )
                            issues.append(
                                ValidationIssue(
                                    severity="warning",
                                    category="pin_assignment",
                                    message=(
                                        f"{ref}: pin '{pin_name}' connected to "
                                        f"net '{net_name}'{proto_info}"
                                    ),
                                    location=node.get_path_string(),
                                )
                            )

            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="pin_assignment",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="pin_assignment",
                message=f"Pin assignment check failed: {e}",
            )
        )

    return issues


def _detect_systematic_offset(
    mismatches: list[tuple[int, str, str, str | None, str | None]],
    pins_data: dict[str, dict],
    ref: str,
    location: str,
    issues: list[ValidationIssue],
) -> bool:
    """Detect if mismatches form a systematic offset pattern.

    If 3 or more mismatches all share the same offset between the pin
    they are on and the pin whose name matches the net, emit a single
    high-severity diagnostic.

    Returns True if a systematic offset was detected and reported.
    """
    if len(mismatches) < 3:
        return False

    # Build pin_name -> pin_number map for the symbol
    name_to_num: dict[str, int] = {}
    for pnum, pinfo in pins_data.items():
        pname = pinfo.get("name", "")
        if pname and pname != "~" and pnum.isdigit():
            name_to_num[pname.upper()] = int(pnum)

    # For each mismatch, figure out the offset: which pin number has a name
    # matching the net's bus keywords?
    offsets: list[int] = []
    for pin_num_int, pin_name, net_name, _, _ in mismatches:
        if pin_num_int < 0:
            continue

        net_tokens = _tokenize_name(net_name)
        net_bus = net_tokens & _ALL_BUS_KEYWORDS

        if not net_bus:
            continue

        # Find which pin on this symbol has a name matching the net's keyword
        for keyword in net_bus:
            if keyword in name_to_num:
                expected_pin = name_to_num[keyword]
                offset = pin_num_int - expected_pin
                if offset != 0:
                    offsets.append(offset)
                break

    if len(offsets) < 3:
        return False

    # Check if most offsets are the same
    from collections import Counter
    offset_counts = Counter(offsets)
    most_common_offset, count = offset_counts.most_common(1)[0]

    if count >= 3:
        issues.append(
            ValidationIssue(
                severity="error",
                category="pin_assignment",
                message=(
                    f"{ref}: systematic wiring offset detected -- "
                    f"{count} pins are shifted by {most_common_offset:+d} positions"
                ),
                location=location,
            )
        )
        return True

    return False


# ---------------------------------------------------------------------------
# Power net short detection (VCC-to-GND)
# ---------------------------------------------------------------------------


def check_power_net_shorts(schematic_path: str) -> list[ValidationIssue]:
    """Detect nets that connect both VCC-type and GND-type power pins.

    Builds a net-to-pin-types map from ``resolve_pin_map()`` across all
    sheets.  For each net, flags an error if it contains both power-positive
    and power-negative pins.
    """
    issues: list[ValidationIssue] = []

    try:
        from kicad_tools.cli.sch_pin_map import resolve_pin_map

        hierarchy = build_hierarchy(schematic_path)

        # net_name -> {"positive_pins": [...], "negative_pins": [...], "sheets": set}
        net_power_map: dict[str, dict] = {}

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                pin_map = resolve_pin_map(sch)
                sheet_path = node.get_path_string()

                for ref, entry in pin_map.items():
                    pins_data: dict[str, dict] = entry.get("pins", {})

                    for pin_num, pin_info in pins_data.items():
                        net_name = pin_info.get("net")
                        if net_name is None:
                            continue

                        pin_name = pin_info.get("name", "")
                        pin_type = pin_info.get("type", "")

                        # Only consider power pins
                        if pin_type not in ("power_in", "power_out"):
                            continue

                        pin_tokens = _tokenize_name(pin_name)
                        is_positive = bool(pin_tokens & _POWER_POSITIVE_KEYWORDS)
                        is_negative = bool(pin_tokens & _POWER_NEGATIVE_KEYWORDS)

                        if not is_positive and not is_negative:
                            continue

                        if net_name not in net_power_map:
                            net_power_map[net_name] = {
                                "positive_pins": [],
                                "negative_pins": [],
                                "sheets": set(),
                            }

                        entry_data = net_power_map[net_name]
                        pin_desc = f"{ref}.{pin_name} (pin {pin_num})"
                        if is_positive:
                            entry_data["positive_pins"].append(pin_desc)
                        if is_negative:
                            entry_data["negative_pins"].append(pin_desc)
                        entry_data["sheets"].add(sheet_path)

            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="power_short",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

        # Check for nets with both positive and negative power pins
        for net_name, data in sorted(net_power_map.items()):
            if data["positive_pins"] and data["negative_pins"]:
                pos_str = ", ".join(data["positive_pins"][:3])
                neg_str = ", ".join(data["negative_pins"][:3])
                sheets = ", ".join(sorted(data["sheets"]))
                issues.append(
                    ValidationIssue(
                        severity="error",
                        category="power_short",
                        message=(
                            f"Net '{net_name}' connects power and ground: "
                            f"positive [{pos_str}], negative [{neg_str}]"
                        ),
                        location=sheets,
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="power_short",
                message=f"Power short check failed: {e}",
            )
        )

    return issues


# ---------------------------------------------------------------------------
# Power pin polarity error detection (VDD on GND net, GND on VCC net)
# ---------------------------------------------------------------------------


def _is_power_positive_net(net_name: str) -> bool:
    """Return True if *net_name* represents a positive power rail.

    Checks tokenized keywords against ``_POWER_POSITIVE_KEYWORDS`` and
    common voltage patterns like ``+3.3V``, ``+5V``, ``3V3``, etc.
    """
    tokens = _tokenize_name(net_name)
    if tokens & _POWER_POSITIVE_KEYWORDS:
        return True
    # Strip leading +/- (common in power net names like +3V3, +5V)
    stripped = net_name.lstrip("+-")
    if stripped != net_name:
        tokens2 = _tokenize_name(stripped)
        if tokens2 & _POWER_POSITIVE_KEYWORDS:
            return True
    # Match common voltage patterns: +3.3V, +5V, +1.8V, etc.
    if re.match(r"^\+?\d+(\.\d+)?V$", net_name):
        return True
    return False


def _is_power_negative_net(net_name: str) -> bool:
    """Return True if *net_name* represents a ground/negative rail.

    Checks tokenized keywords against ``_POWER_NEGATIVE_KEYWORDS``.
    """
    tokens = _tokenize_name(net_name)
    if tokens & _POWER_NEGATIVE_KEYWORDS:
        return True
    # Strip leading +/- just in case
    stripped = net_name.lstrip("+-")
    if stripped != net_name:
        tokens2 = _tokenize_name(stripped)
        if tokens2 & _POWER_NEGATIVE_KEYWORDS:
            return True
    return False


def check_power_pin_polarity(schematic_path: str) -> list[ValidationIssue]:
    """Detect power pins connected to nets of opposite polarity.

    For example, a VDD pin connected to a GND net, or a GND pin connected
    to a +3.3V net.  This is different from the ``power_short`` check which
    detects VCC and GND pins on the *same* net -- polarity errors involve a
    single pin on a net whose polarity contradicts the pin name.

    Power symbols (``lib_id`` starting with ``power:``) are skipped since
    they define nets rather than consume them.
    """
    issues: list[ValidationIssue] = []

    try:
        from kicad_tools.cli.sch_pin_map import resolve_pin_map

        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                pin_map = resolve_pin_map(sch)
                sheet_path = node.get_path_string()

                for ref, entry in pin_map.items():
                    lib_id: str = entry.get("lib_id", "")

                    # Skip power symbols -- they define nets, not consume them
                    if lib_id.startswith("power:"):
                        continue

                    pins_data: dict[str, dict] = entry.get("pins", {})

                    for pin_num, pin_info in pins_data.items():
                        net_name = pin_info.get("net")
                        if net_name is None:
                            continue

                        pin_name = pin_info.get("name", "")
                        pin_type = pin_info.get("type", "")

                        # Only consider power pins
                        if pin_type not in ("power_in", "power_out"):
                            continue

                        pin_tokens = _tokenize_name(pin_name)
                        pin_is_positive = bool(pin_tokens & _POWER_POSITIVE_KEYWORDS)
                        pin_is_negative = bool(pin_tokens & _POWER_NEGATIVE_KEYWORDS)

                        # Skip ambiguous or unrecognized pin names
                        if not pin_is_positive and not pin_is_negative:
                            continue

                        net_is_positive = _is_power_positive_net(net_name)
                        net_is_negative = _is_power_negative_net(net_name)

                        # Skip nets that don't match any power classification
                        if not net_is_positive and not net_is_negative:
                            continue

                        # Detect polarity mismatch
                        if pin_is_positive and net_is_negative:
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    category="power_polarity",
                                    message=(
                                        f"Power pin polarity error: {ref}.{pin_name} "
                                        f"(pin {pin_num}) is a positive supply pin "
                                        f"but connected to negative net '{net_name}'"
                                    ),
                                    location=sheet_path,
                                )
                            )
                        elif pin_is_negative and net_is_positive:
                            issues.append(
                                ValidationIssue(
                                    severity="error",
                                    category="power_polarity",
                                    message=(
                                        f"Power pin polarity error: {ref}.{pin_name} "
                                        f"(pin {pin_num}) is a ground pin "
                                        f"but connected to positive net '{net_name}'"
                                    ),
                                    location=sheet_path,
                                )
                            )

            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="power_polarity",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="power_polarity",
                message=f"Power polarity check failed: {e}",
            )
        )

    return issues


def check_fully_unconnected_components(schematic_path: str) -> list[ValidationIssue]:
    """Detect components where every pin is floating (no wire, label, or no-connect).

    A fully unconnected component is one where ``resolve_pin_map()`` reports
    ``net=None`` for every pin **and** no no-connect marker is placed at any
    of its pin positions.  Such components are almost always placement errors.

    Exclusions (to avoid false positives):
    - Power symbols (``lib_id`` starts with ``power:``) -- already skipped by
      ``resolve_pin_map``
    - DNP symbols
    - Graphical-only symbols (``in_bom=no`` and ``on_board=no``)
    - Symbols whose library pins are all typed ``no_connect``
    """
    issues: list[ValidationIssue] = []

    try:
        from kicad_tools.cli.sch_pin_map import resolve_pin_map

        hierarchy = build_hierarchy(schematic_path)

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                pin_map = resolve_pin_map(sch)
                sheet_path = node.get_path_string()

                # Collect no-connect marker positions from the raw S-expression.
                nc_points: set[tuple[float, float]] = set()
                for nc_sexp in sch.sexp.find_children("no_connect"):
                    at = nc_sexp.find("at")
                    if at:
                        x = at.get_float(0)
                        y = at.get_float(1)
                        if x is not None and y is not None:
                            nc_points.add((round(x, 2), round(y, 2)))

                # Build a set of DNP / graphical-only references for exclusion.
                # Also collect per-reference metadata for the error message.
                skip_refs: set[str] = set()
                ref_values: dict[str, str] = {}
                for sym in sch.symbols:
                    ref = sym.reference
                    if not ref:
                        continue
                    ref_values[ref] = sym.value or "?"

                    if sym.lib_id.startswith("power:"):
                        skip_refs.add(ref)
                    if sym.dnp:
                        skip_refs.add(ref)
                    if not sym.in_bom and not sym.on_board:
                        skip_refs.add(ref)

                for ref, entry in pin_map.items():
                    if ref in skip_refs:
                        continue

                    pins_data: dict[str, dict] = entry.get("pins", {})
                    if not pins_data:
                        continue

                    # Check if all library pin types are "no_connect"
                    pin_types = {p.get("type", "") for p in pins_data.values()}
                    if pin_types and pin_types <= {"no_connect"}:
                        continue

                    # Check connectivity: any pin with a net means connected
                    has_any_connection = False
                    has_any_nc_marker = False

                    for pin_num, pin_info in pins_data.items():
                        if pin_info.get("net") is not None:
                            has_any_connection = True
                            break

                        # Check for no-connect marker at this pin position
                        pos = pin_info.get("position")
                        if pos:
                            pos_r = (round(pos[0], 2), round(pos[1], 2))
                            if pos_r in nc_points:
                                has_any_nc_marker = True

                    if has_any_connection:
                        continue
                    if has_any_nc_marker:
                        continue

                    # All pins are floating with no no-connect markers
                    value = ref_values.get(ref, entry.get("lib_id", "?"))
                    issues.append(
                        ValidationIssue(
                            severity="error",
                            category="unconnected_component",
                            message=(
                                f"Fully unconnected component: {ref} ({value}) "
                                f"-- all pins are floating"
                            ),
                            location=sheet_path,
                        )
                    )

            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="unconnected_component",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="unconnected_component",
                message=f"Unconnected component check failed: {e}",
            )
        )

    return issues


def check_nrst_filter_cap(schematic_path: str) -> list[ValidationIssue]:
    """Warn when an STM32 MCU's NRST pin lacks a filter capacitor to GND.

    ST hardware design guidelines (AN2586, AN4488) require a 100 nF capacitor
    from NRST to GND for noise filtering on all STM32 families.  Without it
    the MCU can suffer spurious resets from conducted or radiated noise.

    The check walks every sheet, identifies STM32 symbols, locates the NRST
    net, and verifies that at least one capacitor connects the NRST net to a
    ground rail.  If the capacitor value can be parsed, it also warns when the
    value falls outside the recommended 10 nF - 1 uF range.
    """
    issues: list[ValidationIssue] = []

    try:
        from kicad_tools.cli.sch_pin_map import resolve_pin_map

        hierarchy = build_hierarchy(schematic_path)

        # Accumulated across all sheets:
        #   nrst_nets: mapping from net_name -> (ref, sheet_path) of the STM32
        #   cap_pin_nets: ref -> list of (net_name, value) per pin
        nrst_nets: dict[str, tuple[str, str]] = {}
        # ref -> [(net_name, ...), ...]
        cap_pin_nets: dict[str, list[str]] = {}
        cap_values: dict[str, str] = {}  # ref -> value string

        for node in hierarchy.all_nodes():
            try:
                sch = Schematic.load(node.path)
                pin_map = resolve_pin_map(sch)
                sheet_path = node.get_path_string()

                # Build a map of ref -> value from schematic symbols for
                # capacitor value lookups.
                ref_to_value: dict[str, str] = {}
                for sym in sch.symbols:
                    r = sym.reference
                    if r:
                        ref_to_value[r] = sym.value or ""

                for ref, entry in pin_map.items():
                    lib_id: str = entry.get("lib_id", "")
                    pins_data: dict[str, dict] = entry.get("pins", {})

                    # Identify STM32 MCUs and find their NRST net
                    if _is_stm32(lib_id):
                        for pin_num, pin_info in pins_data.items():
                            pin_name = pin_info.get("name", "")
                            net_name = pin_info.get("net")
                            if net_name is not None and _is_nrst_pin(pin_name):
                                nrst_nets[net_name] = (ref, sheet_path)

                    # Track capacitor pin-to-net connections
                    if _is_capacitor(lib_id):
                        nets_for_ref: list[str] = []
                        for pin_num, pin_info in pins_data.items():
                            n = pin_info.get("net")
                            if n is not None:
                                nets_for_ref.append(n)
                        if ref not in cap_pin_nets:
                            cap_pin_nets[ref] = nets_for_ref
                        else:
                            cap_pin_nets[ref].extend(nets_for_ref)
                        # Store value for range checking
                        if ref not in cap_values:
                            cap_values[ref] = ref_to_value.get(ref, "")

            except Exception as e:
                issues.append(
                    ValidationIssue(
                        severity="info",
                        category="nrst_filter",
                        message=f"Skipped sheet {node.get_path_string()}: {e}",
                        location=node.get_path_string(),
                    )
                )

        # Check each NRST net for a capacitor to GND.
        for net_name, (mcu_ref, sheet_path) in nrst_nets.items():
            found_cap = False
            out_of_range_cap: str | None = None

            for cap_ref, pin_net_list in cap_pin_nets.items():
                net_set = set(pin_net_list)
                if net_name not in net_set:
                    continue
                # Check if the other pin(s) connect to a GND net
                other_nets = net_set - {net_name}
                gnd_on_cap = {n for n in other_nets if _is_power_negative_net(n)}
                if not gnd_on_cap:
                    continue

                # Capacitor connects NRST to GND -- check value range
                val_str = cap_values.get(cap_ref, "")
                if val_str:
                    farads = _parse_capacitance(val_str)
                    if farads is not None and (farads < 10e-9 or farads > 1e-6):
                        out_of_range_cap = (
                            f"NRST filter capacitor {cap_ref} ({val_str}) on "
                            f"net '{net_name}' is outside the recommended "
                            f"10nF-1uF range for {mcu_ref}"
                        )
                        # Still counts as present even if out of range
                    found_cap = True
                else:
                    # No parseable value but capacitor is present
                    found_cap = True

            if not found_cap:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="nrst_filter",
                        message=(
                            f"STM32 MCU {mcu_ref} NRST net '{net_name}' has no "
                            f"filter capacitor to GND (ST recommends 100nF)"
                        ),
                        location=sheet_path,
                    )
                )
            elif out_of_range_cap:
                issues.append(
                    ValidationIssue(
                        severity="warning",
                        category="nrst_filter",
                        message=out_of_range_cap,
                        location=sheet_path,
                    )
                )

    except Exception as e:
        issues.append(
            ValidationIssue(
                severity="warning",
                category="nrst_filter",
                message=f"NRST filter cap check failed: {e}",
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

    # Pin-net semantic mismatch (pin assignment audit)
    result.checks_run.append("pin_assignment")
    result.issues.extend(check_pin_net_semantic_mismatch(schematic_path))

    # Power net short detection (VCC-to-GND)
    result.checks_run.append("power_short")
    result.issues.extend(check_power_net_shorts(schematic_path))

    # Power pin polarity error detection (VDD on GND net, etc.)
    result.checks_run.append("power_polarity")
    result.issues.extend(check_power_pin_polarity(schematic_path))

    # I2C pull-up resistor detection
    result.checks_run.append("i2c_pullups")
    result.issues.extend(check_i2c_pullups(schematic_path))

    # Fully unconnected component detection
    result.checks_run.append("unconnected_component")
    result.issues.extend(check_fully_unconnected_components(schematic_path))

    # STM32 NRST filter capacitor detection
    result.checks_run.append("nrst_filter")
    result.issues.extend(check_nrst_filter_cap(schematic_path))

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
