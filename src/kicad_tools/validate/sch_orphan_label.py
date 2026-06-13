"""Orphan-label ERC rule (schematic-level).

Detects named labels that connect to exactly one pin in the schematic.
A label with a meaningful name (e.g., ``UART_TX``) syntactically asserts
that the named signal exists and has at least two endpoints (one source,
one sink).  When the label only attaches to a single pin in the entire
design, the implied counterpart connection is missing -- a real design
defect that KiCad's built-in ERC does NOT catch because the label itself
syntactically resolves (it just has only one endpoint).

This is one of the four tooling improvements requested by issue #2613.
The chorus-test-revA schematic exhibits this defect: ``UART_TX``,
``UART_RX``, ``I2S_BCLK``, ``DBG_LED2`` etc. all have global labels
attached to a single MCU pin with no counterpart on the connector or
the rest of the design.

Algorithm
---------

Given a list of schematics (typically the root + every child sheet),
the rule:

1. Builds a mapping ``label_name -> [(x_mm, y_mm, sheet, kind)]`` over
   local labels, hierarchical labels, and global labels (global labels
   are cross-sheet; their occurrences are pooled across all sheets).
2. Builds a mapping ``label_name -> set of pins reached`` by walking
   each label's coordinate position and following any wire that
   touches it to the pin endpoints (using a simple flood-fill via
   `_collect_label_pin_endpoints`).
3. Reports any label whose name does not match a benign pattern
   (e.g., looks like a power net or a generic ``Net-...`` net), is
   attached to only one pin in the whole design, and is not silenced
   by an explicit ``no_connect`` flag at the lone pin.

The rule is conservative by design -- it only fires on **named** labels
(``UART_TX`` etc.).  Default-named nets (``Net-(...)``) are categorized
by the separate single_pad_net DRC rule on the PCB side.
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from dataclasses import dataclass

# Names that look like power/ground rails should not be considered
# "intended-signal" labels.  KiCad treats these specially (a single
# pad connected to GND or +3V3 is a legitimate test point or pour
# tap).  This list is intentionally short and conservative; the
# single_pad_net DRC rule uses a richer classifier on the PCB side.
_POWER_LIKE_PATTERN = re.compile(
    r"^(?:GND|VCC|VDD|VSS|VBAT|VBUS|VPLUS|VMINUS|"
    r"\+?[\d.]+V[\d.A-Z]*|-?[\d.]+V[\d.A-Z]*|"
    r"\+[\d.A-Z]+|-[\d.A-Z]+)$",
    re.IGNORECASE,
)

# Default-named nets (``Net-(...)``) are not "intended signal" labels.
_DEFAULT_NET_PATTERN = re.compile(r"^Net-\(.+\)$")

# Coordinates close enough to be considered identical.  Schematic
# coordinates are stored in mm with 4-5 decimal places; pin positions
# are rounded to the schematic grid (1.27 mm typically).
_COORD_EPS = 0.01  # 10 micrometers -- well below any schematic grid.


@dataclass(frozen=True)
class OrphanLabelFinding:
    """A single orphan-label finding.

    Attributes:
        label_name: The label's text (e.g., ``"UART_TX"``).
        sheet: Path to the schematic sheet containing the lone pin.
        pin_ref: Component reference + pin number (e.g., ``"U8.10"``).
        position_mm: (x, y) coordinates of the lone pin in mm.
        kind: Severity hint -- always ``"error"`` because an orphan
            label is a real schematic defect.
    """

    label_name: str
    sheet: str
    pin_ref: str
    position_mm: tuple[float, float]
    kind: str = "error"


def _approx_eq(a: float, b: float, eps: float = _COORD_EPS) -> bool:
    """True when two floats are within ``eps``."""
    return abs(a - b) <= eps


def _is_intended_signal_name(label_name: str) -> bool:
    """Filter labels that look like power/ground or default-named nets.

    Args:
        label_name: The label text.

    Returns:
        True when this looks like a real named signal (e.g.,
        ``UART_TX``).  False for power rails (``+3V3``, ``GND``) and
        default-named nets (``Net-(U1-Pad3)``).
    """
    name = label_name.strip()
    if not name:
        return False
    if _POWER_LIKE_PATTERN.match(name):
        return False
    if _DEFAULT_NET_PATTERN.match(name):
        return False
    return True


def _collect_label_positions(
    schematic, sheet_path: str
) -> dict[str, list[tuple[float, float, str]]]:
    """Collect named label positions in a single schematic sheet.

    Args:
        schematic: A :class:`kicad_tools.schema.Schematic` instance.
        sheet_path: Path string identifying the sheet (used in
            findings, not used for processing).

    Returns:
        Mapping ``label_name -> [(x_mm, y_mm, sheet_path)]`` for every
        label occurrence in this sheet.  Labels of all three kinds
        (local, hierarchical, global) are pooled into one dict.
    """
    positions: dict[str, list[tuple[float, float, str]]] = {}
    # Local labels (single-sheet scope, but still relevant -- a local
    # label that connects to only one pin within its sheet is an
    # orphan).
    for lbl in schematic.labels:
        text = getattr(lbl, "text", "") or ""
        pos = getattr(lbl, "position", (0.0, 0.0))
        if text:
            positions.setdefault(text, []).append((pos[0], pos[1], sheet_path))
    # Hierarchical labels -- always paired with a sheet-pin on the
    # parent sheet.  Listed here for completeness; a single
    # hierarchical label with a single pin counterpart is unusual but
    # still a valid orphan signal.
    for lbl in schematic.hierarchical_labels:
        text = getattr(lbl, "text", "") or ""
        pos = getattr(lbl, "position", (0.0, 0.0))
        if text:
            positions.setdefault(text, []).append((pos[0], pos[1], sheet_path))
    # Global labels (cross-sheet scope).
    for lbl in schematic.global_labels:
        text = getattr(lbl, "text", "") or ""
        pos = getattr(lbl, "position", (0.0, 0.0))
        if text:
            positions.setdefault(text, []).append((pos[0], pos[1], sheet_path))
    return positions


def _resolve_pin_positions(
    schematic, lib_symbols: dict[str, object]
) -> list[tuple[str, str, float, float]]:
    """Resolve every pin position on every placed symbol in a sheet.

    Args:
        schematic: A :class:`kicad_tools.schema.Schematic`.
        lib_symbols: Mapping ``lib_id -> LibrarySymbol`` (resolved).
            Provided externally so the caller can prebuild it once for
            a multi-sheet design.

    Returns:
        List of tuples ``(reference, pin_number, x_mm, y_mm)`` for
        every pin on every placed (non-DNP) symbol in the sheet.
    """
    results: list[tuple[str, str, float, float]] = []
    for sym in schematic.symbols:
        if sym.dnp:
            continue
        lib_sym = lib_symbols.get(sym.lib_id)
        if lib_sym is None:
            # No library definition resolved -- skip pin position
            # extraction for this symbol.  Tests must seed the
            # library map.
            continue
        ref = sym.reference or ""
        if not ref:
            continue
        try:
            pin_positions = lib_sym.get_all_pin_positions(
                instance_pos=sym.position,
                instance_rot=sym.rotation,
                mirror=sym.mirror,
            )
        except Exception:
            # Library symbol shape unknown -- safest to skip than to
            # blow up the whole validation pass.
            continue
        for pin_num, (x, y) in pin_positions.items():
            results.append((ref, pin_num, x, y))
    return results


def _wires_at_point(wires: Iterable, point: tuple[float, float]) -> list[object]:
    """Return the wires that have an endpoint at ``point``."""
    hits = []
    px, py = point
    for w in wires:
        if (_approx_eq(w.start[0], px) and _approx_eq(w.start[1], py)) or (
            _approx_eq(w.end[0], px) and _approx_eq(w.end[1], py)
        ):
            hits.append(w)
    return hits


def _collect_label_pin_endpoints(
    label_pos: tuple[float, float],
    wires: list,
    pin_positions: list[tuple[str, str, float, float]],
) -> list[tuple[str, str, float, float]]:
    """Walk wires from a label position, collecting reached pins.

    Performs a simple flood-fill: start at the label position, walk
    every wire that has an endpoint there, hop to the opposite
    endpoint, and continue until no new endpoints are reached.  Pins
    coincident with any visited endpoint are returned.

    Args:
        label_pos: (x, y) of the label.
        wires: All wires in the relevant sheet(s).
        pin_positions: All pin positions in the relevant sheet(s).

    Returns:
        List of (ref, pin_num, x, y) pins reached from the label.
    """
    visited_points: set[tuple[float, float]] = set()
    frontier: list[tuple[float, float]] = [label_pos]

    def _round(p: tuple[float, float]) -> tuple[float, float]:
        """Snap a point to micrometer precision for set hashing."""
        return (round(p[0], 4), round(p[1], 4))

    while frontier:
        pt = frontier.pop()
        key = _round(pt)
        if key in visited_points:
            continue
        visited_points.add(key)
        for w in _wires_at_point(wires, pt):
            # Walk to opposite endpoint.
            if _approx_eq(w.start[0], pt[0]) and _approx_eq(w.start[1], pt[1]):
                other = w.end
            else:
                other = w.start
            if _round(other) not in visited_points:
                frontier.append(other)

    reached: list[tuple[str, str, float, float]] = []
    for ref, pin_num, x, y in pin_positions:
        if _round((x, y)) in visited_points:
            reached.append((ref, pin_num, x, y))
    return reached


def find_orphan_labels(
    sheets: list[tuple[str, object]],
    lib_symbols: dict[str, object],
) -> list[OrphanLabelFinding]:
    """Find orphan labels across a multi-sheet schematic design.

    Args:
        sheets: List of ``(sheet_path, Schematic)`` tuples for every
            sheet in the design (root + children).
        lib_symbols: Mapping ``lib_id -> LibrarySymbol`` resolved for
            every symbol used in the design.  The caller typically
            builds this by walking each sheet's ``lib_symbols``
            section and resolving extends chains.

    Returns:
        List of :class:`OrphanLabelFinding`.  Empty when no orphan
        labels are detected.
    """
    # Pool global-label positions across all sheets; local and
    # hierarchical labels stay per-sheet (single-sheet scope).
    global_positions: dict[str, list[tuple[float, float, str]]] = {}
    per_sheet_label_positions: list[tuple[str, dict[str, list[tuple[float, float, str]]]]] = []
    per_sheet_pins: dict[str, list[tuple[str, str, float, float]]] = {}
    per_sheet_wires: dict[str, list] = {}
    per_sheet_no_connects: dict[str, list[tuple[float, float]]] = {}

    for sheet_path, sch in sheets:
        local_pos = _collect_label_positions(sch, sheet_path)
        per_sheet_label_positions.append((sheet_path, local_pos))
        for gl in sch.global_labels:
            text = getattr(gl, "text", "") or ""
            pos = getattr(gl, "position", (0.0, 0.0))
            if text:
                global_positions.setdefault(text, []).append((pos[0], pos[1], sheet_path))
        per_sheet_pins[sheet_path] = _resolve_pin_positions(sch, lib_symbols)
        per_sheet_wires[sheet_path] = list(sch.wires)
        per_sheet_no_connects[sheet_path] = [
            (nc.position[0], nc.position[1]) for nc in sch.no_connects
        ]

    findings: list[OrphanLabelFinding] = []

    # Tally pins reached per global label across the whole design.
    for label_name, occurrences in global_positions.items():
        if not _is_intended_signal_name(label_name):
            continue
        reached: list[tuple[str, str, float, float, str]] = []
        for x, y, sheet_path in occurrences:
            pins = _collect_label_pin_endpoints(
                (x, y),
                per_sheet_wires[sheet_path],
                per_sheet_pins[sheet_path],
            )
            for ref, pin_num, px, py in pins:
                reached.append((ref, pin_num, px, py, sheet_path))
        # Deduplicate by (ref, pin_num).
        seen_pins: set[tuple[str, str]] = set()
        unique = []
        for ref, pin_num, px, py, sheet_path in reached:
            key = (ref, pin_num)
            if key in seen_pins:
                continue
            seen_pins.add(key)
            unique.append((ref, pin_num, px, py, sheet_path))
        if len(unique) == 1:
            ref, pin_num, px, py, sheet_path = unique[0]
            # Skip if this pin has an explicit no_connect flag.
            no_connects = per_sheet_no_connects.get(sheet_path, [])
            if any(_approx_eq(nx, px) and _approx_eq(ny, py) for nx, ny in no_connects):
                continue
            findings.append(
                OrphanLabelFinding(
                    label_name=label_name,
                    sheet=sheet_path,
                    pin_ref=f"{ref}.{pin_num}",
                    position_mm=(px, py),
                )
            )

    # Per-sheet check for local + hierarchical labels.  We compare
    # within the sheet only -- a local label cannot reach pins on
    # other sheets.
    for sheet_path, local_pos in per_sheet_label_positions:
        for label_name, occurrences in local_pos.items():
            if label_name in global_positions:
                # Already handled by the global pass.
                continue
            if not _is_intended_signal_name(label_name):
                continue
            reached_pins: list[tuple[str, str, float, float]] = []
            for x, y, _ in occurrences:
                reached_pins.extend(
                    _collect_label_pin_endpoints(
                        (x, y),
                        per_sheet_wires[sheet_path],
                        per_sheet_pins[sheet_path],
                    )
                )
            # Deduplicate.
            seen_pins = set()
            unique_pins = []
            for ref, pin_num, px, py in reached_pins:
                key = (ref, pin_num)
                if key in seen_pins:
                    continue
                seen_pins.add(key)
                unique_pins.append((ref, pin_num, px, py))
            if len(unique_pins) == 1:
                ref, pin_num, px, py = unique_pins[0]
                no_connects = per_sheet_no_connects.get(sheet_path, [])
                if any(_approx_eq(nx, px) and _approx_eq(ny, py) for nx, ny in no_connects):
                    continue
                findings.append(
                    OrphanLabelFinding(
                        label_name=label_name,
                        sheet=sheet_path,
                        pin_ref=f"{ref}.{pin_num}",
                        position_mm=(px, py),
                    )
                )

    return findings
