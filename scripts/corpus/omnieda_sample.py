#!/usr/bin/env python3
"""Characterize an OmniEDA (OmniLayout / OmniRouting) sample drop (issue #4830, slice 3).

The OmniLayout benchmark (https://www.omnieda.com/) does **not** distribute
KiCad files: its records are JSON transcriptions of Eagle ``.brd`` boards, so
none of the slice-1/slice-2 corpus tooling (which drives
``kicad_tools.schema.PCB.load`` / ``Schematic.load``) can be pointed at them.
This module is the substitute measurement: it reads an already-downloaded
sample directory and reports, per board, what the records actually contain and
which fields have **no representation** in ``kicad_tools.schema.pcb`` today.

That second half is the point. A "can we convert this to ``.kicad_pcb``?"
question is only answerable as a list of concrete gaps, each of which maps to a
different piece of converter work (or to a decision not to do it). The gap
constants below are chosen on that basis -- see ``docs/research/omnilayout-recon.md``
for the resulting adopt / adapt / drop recommendation.

**Offline by construction.** The sample lives behind interactive Google Drive
links, so there is no fetch step here to get wrong: you download by hand (the
recon doc records the exact commands and the SHA-256 of what we measured) and
point ``--sample`` at the extracted directory. No network, no repo writes
outside the caller-supplied output path -- which is why ``tests/`` may import
this module while the slice-1/2 network scripts stay out of the test suite.

Usage::

    python scripts/corpus/omnieda_sample.py --sample /path/to/extracted --out /tmp/omni
"""

from __future__ import annotations

import argparse
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# ---------------------------------------------------------------------------
# Record flavors
# ---------------------------------------------------------------------------

FLAVOR_LAYOUT = "omnilayout-placement"  # placement-only record (OmniLayout drop)
FLAVOR_ROUTING = "omnirouting-graph"  # + reference routing (OmniRouting drop)
FLAVOR_UNKNOWN = "unknown"

_LAYOUT_KEYS = frozenset({"netlist", "board_boundary", "ic_library", "ic_position"})
_ROUTING_KEYS = frozenset({"routing_wires", "routing_vias"})

# Eagle numbers copper layers 1..16 (1 = Top, 16 = Bottom, 2..15 inner).
# Everything above 16 is documentation/system: 17 Pads, 18 Vias, 19 *Unrouted*,
# 20 Dimension, 21 tPlace.  A "wire" on 19 is a ratsnest airwire, not copper --
# counting it as routed copper would silently inflate any route-vs-human score.
EAGLE_COPPER_LAYERS = frozenset(str(n) for n in range(1, 17))
EAGLE_TOP = "1"
EAGLE_BOTTOM = "16"
EAGLE_UNROUTED = "19"

# ---------------------------------------------------------------------------
# Mappability gaps -- one constant per *distinct* piece of converter work.
# ---------------------------------------------------------------------------

GAP_PLACEMENT_WITHOUT_GEOMETRY = "placement-without-footprint-geometry"
GAP_MISSING_ROTATION = "component-missing-rotation"
GAP_TH_PAD_NO_DIAMETER = "through-hole-pad-without-diameter"
GAP_AIRWIRE_AS_ROUTING = "airwire-counted-as-routing"
GAP_CURVED_COPPER = "curved-copper-segment"
GAP_INNER_LAYER = "inner-copper-layer"
GAP_BLIND_BURIED_VIA = "blind-or-buried-via"
GAP_COPPER_POUR = "copper-pour-polygon"
GAP_NO_SCHEMATIC = "no-schematic-payload"

#: Every gap, with the converter work it implies.  Ordered from "blocks a
#: faithful conversion" to "cosmetic".
GAP_NOTES: dict[str, str] = {
    GAP_PLACEMENT_WITHOUT_GEOMETRY: (
        "element is placed but has no ic_library entry -- no pads to emit, so the "
        "footprint cannot be reconstructed from the record alone"
    ),
    GAP_MISSING_ROTATION: (
        "ic_library entry has no rotation key; the converter must fall back to the "
        "ic_position rotation or assume R0"
    ),
    GAP_TH_PAD_NO_DIAMETER: (
        "through-hole pad gives a drill but no diameter; annular ring has to be "
        "inferred from the Eagle design rules (rvPad*/rlMinPad*)"
    ),
    GAP_AIRWIRE_AS_ROUTING: (
        "wire sits on Eagle layer 19 (Unrouted) -- a ratsnest stub, not copper; it "
        "must be excluded from routed-length and completion metrics"
    ),
    GAP_CURVED_COPPER: (
        "wire has a non-zero Eagle curve (arc bulge); schema.pcb models copper as "
        "(segment ...) only, so an arc needs polyline approximation"
    ),
    GAP_INNER_LAYER: (
        "board uses Eagle inner copper (layers 2..15); the converter must synthesize "
        "a >2-layer KiCad stackup rather than the default two"
    ),
    GAP_BLIND_BURIED_VIA: (
        "via extent is not the full 1-16 stack; needs blind/buried via emission"
    ),
    GAP_COPPER_POUR: (
        "record carries Eagle polygon pours; these map to KiCad zones but the pour "
        "parameters (rank/isolate/thermals) do not translate one-to-one"
    ),
    GAP_NO_SCHEMATIC: (
        "record has no schematic payload despite the benchmark being billed as "
        "schematic-coupled; LVS/net-status validation cannot be exercised on it"
    ),
}


@dataclass
class BoardAudit:
    """Per-record characterization plus the KiCad-mappability gap census."""

    name: str
    flavor: str
    components: int = 0
    placements: int = 0
    nets: int = 0
    connections: int = 0
    smd_pads: int = 0
    th_pads: int = 0
    holes: int = 0
    boundary_segments: int = 0
    boundary_closed: bool = False
    copper_wires: int = 0
    vias: int = 0
    pours: int = 0
    copper_layers: list[str] = field(default_factory=list)
    clearance_mm: float | None = None
    eagle_version: str = ""
    designrule_set: str = ""
    gaps: dict[str, int] = field(default_factory=dict)

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "flavor": self.flavor,
            "components": self.components,
            "placements": self.placements,
            "nets": self.nets,
            "connections": self.connections,
            "smd_pads": self.smd_pads,
            "th_pads": self.th_pads,
            "holes": self.holes,
            "boundary_segments": self.boundary_segments,
            "boundary_closed": self.boundary_closed,
            "copper_wires": self.copper_wires,
            "vias": self.vias,
            "pours": self.pours,
            "copper_layers": self.copper_layers,
            "clearance_mm": self.clearance_mm,
            "eagle_version": self.eagle_version,
            "designrule_set": self.designrule_set,
            "gaps": dict(sorted(self.gaps.items())),
        }


def sniff_flavor(record: Any) -> str:
    """Classify a decoded JSON record as a layout / routing / unknown drop."""
    if not isinstance(record, dict):
        return FLAVOR_UNKNOWN
    keys = frozenset(record)
    if not keys >= _LAYOUT_KEYS:
        return FLAVOR_UNKNOWN
    if _ROUTING_KEYS & keys:
        return FLAVOR_ROUTING
    return FLAVOR_LAYOUT


def _as_list(value: Any) -> list[Any]:
    """Eagle-derived JSON collapses single-element lists to a bare object."""
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def _bump(gaps: dict[str, int], key: str, count: int = 1) -> None:
    if count:
        gaps[key] = gaps.get(key, 0) + count


def audit_record(name: str, record: Any) -> BoardAudit:
    """Characterize one decoded record and census its KiCad-mappability gaps."""
    flavor = sniff_flavor(record)
    audit = BoardAudit(name=name, flavor=flavor)
    if flavor == FLAVOR_UNKNOWN:
        return audit

    assert isinstance(record, dict)  # narrowed by sniff_flavor
    gaps = audit.gaps

    library = _as_list(record.get("ic_library"))
    placements = _as_list(record.get("ic_position"))
    audit.components = len(library)
    audit.placements = len(placements)

    known = {entry.get("element_name") for entry in library if isinstance(entry, dict)}
    orphan_placements = sum(
        1
        for entry in placements
        if isinstance(entry, dict) and entry.get("element_name") not in known
    )
    _bump(gaps, GAP_PLACEMENT_WITHOUT_GEOMETRY, orphan_placements)

    for entry in library:
        if not isinstance(entry, dict):
            continue
        smd = _as_list(entry.get("smd"))
        pads = _as_list(entry.get("pads"))
        audit.smd_pads += len(smd)
        audit.th_pads += len(pads)
        audit.holes += len(_as_list(entry.get("holes")))
        if entry.get("rotation") is None:
            _bump(gaps, GAP_MISSING_ROTATION)
        for pad in pads:
            if isinstance(pad, dict) and pad.get("diameter") is None:
                _bump(gaps, GAP_TH_PAD_NO_DIAMETER)

    nets = _as_list(record.get("netlist"))
    audit.nets = len(nets)
    audit.connections = sum(
        len(_as_list(net.get("contactref"))) for net in nets if isinstance(net, dict)
    )

    boundary = record.get("board_boundary")
    if isinstance(boundary, dict):
        audit.boundary_segments = len(_as_list(boundary.get("segment_details")))
        audit.boundary_closed = bool(boundary.get("closed"))

    layers: set[str] = set()
    for wire in _as_list(record.get("routing_wires")):
        if not isinstance(wire, dict):
            continue
        layer = str(wire.get("layer"))
        if layer in EAGLE_COPPER_LAYERS:
            audit.copper_wires += 1
            layers.add(layer)
            if wire.get("curve"):
                _bump(gaps, GAP_CURVED_COPPER)
        elif layer == EAGLE_UNROUTED:
            _bump(gaps, GAP_AIRWIRE_AS_ROUTING)
    inner = sorted(layers - {EAGLE_TOP, EAGLE_BOTTOM}, key=int)
    _bump(gaps, GAP_INNER_LAYER, len(inner))
    audit.copper_layers = sorted(layers, key=int)

    vias = _as_list(record.get("routing_vias"))
    audit.vias = len(vias)
    for via in vias:
        if isinstance(via, dict) and str(via.get("extent")) not in (
            f"{EAGLE_TOP}-{EAGLE_BOTTOM}",
            "None",
        ):
            _bump(gaps, GAP_BLIND_BURIED_VIA)

    pours = _as_list(record.get("copper_pours"))
    audit.pours = len(pours)
    _bump(gaps, GAP_COPPER_POUR, len(pours))

    clearance = record.get("Clearance")
    if isinstance(clearance, dict):
        rules = clearance.get("rules")
        if isinstance(rules, dict):
            widths = [v for v in rules.values() if isinstance(v, (int, float)) and v > 0]
            if widths:
                audit.clearance_mm = min(widths)

    semantics = record.get("board_semantics")
    if isinstance(semantics, dict):
        audit.eagle_version = str(semantics.get("eagle_version") or "")
        rules_block = semantics.get("designrules")
        if isinstance(rules_block, dict):
            audit.designrule_set = str(rules_block.get("name") or "")

    # No OmniEDA record shipped so far carries a schematic payload, despite the
    # "schematic-coupled" billing; assert it per record so a future drop that
    # *does* include one shows up as the gap disappearing.
    if not any(key in record for key in ("schematic", "sch", "schematic_graph")):
        _bump(gaps, GAP_NO_SCHEMATIC)

    return audit


def load_sample(sample_dir: Path) -> list[tuple[str, Any]]:
    """Load every ``*.json`` under ``sample_dir`` (recursively), sorted by path."""
    records: list[tuple[str, Any]] = []
    for path in sorted(sample_dir.rglob("*.json")):
        try:
            with path.open(encoding="utf-8") as handle:
                records.append((str(path.relative_to(sample_dir)), json.load(handle)))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
            records.append((str(path.relative_to(sample_dir)), {"__error__": str(exc)}))
    return records


def render_report(audits: list[BoardAudit]) -> str:
    """Human-readable summary: per-board table, then the pooled gap census."""
    lines: list[str] = []
    lines.append("OmniEDA sample audit")
    lines.append("=" * 72)
    scored = [a for a in audits if a.flavor != FLAVOR_UNKNOWN]
    lines.append(
        f"records: {len(audits)}  recognized: {len(scored)}  "
        f"unrecognized: {len(audits) - len(scored)}"
    )
    if not scored:
        lines.append("")
        lines.append("No OmniEDA records found -- is --sample pointing at the extract?")
        return "\n".join(lines) + "\n"

    lines.append("")
    header = (
        f"{'board':<44}{'flavor':<22}{'cmp':>5}{'nets':>6}"
        f"{'pads':>6}{'wires':>7}{'vias':>6}{'cu':>10}"
    )
    lines.append(header)
    lines.append("-" * len(header))
    for audit in scored:
        name = audit.name if len(audit.name) <= 43 else audit.name[:40] + "..."
        cu = ",".join(audit.copper_layers) or "-"
        lines.append(
            f"{name:<44}{audit.flavor:<22}{audit.components:>5}{audit.nets:>6}"
            f"{audit.smd_pads + audit.th_pads:>6}{audit.copper_wires:>7}"
            f"{audit.vias:>6}{cu:>10}"
        )

    pooled: dict[str, int] = {}
    affected: dict[str, int] = {}
    for audit in scored:
        for key, count in audit.gaps.items():
            pooled[key] = pooled.get(key, 0) + count
            affected[key] = affected.get(key, 0) + 1

    lines.append("")
    lines.append("KiCad-mappability gaps (occurrences / boards affected)")
    lines.append("-" * 72)
    if not pooled:
        lines.append("  none -- every field in this sample has a schema.pcb counterpart")
    for key, count in sorted(pooled.items(), key=lambda kv: (-kv[1], kv[0])):
        lines.append(f"  {key:<38} {count:>6}  on {affected[key]}/{len(scored)} boards")
        lines.append(f"      {GAP_NOTES.get(key, '(no note)')}")
    return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=(
            "Characterize a manually-downloaded OmniLayout/OmniRouting sample "
            "(offline; see docs/research/omnilayout-recon.md for the download steps)."
        )
    )
    parser.add_argument(
        "--sample",
        required=True,
        type=Path,
        help="directory holding the extracted sample JSON files",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=None,
        help="write the machine-readable audit here (default: stdout summary only)",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    sample_dir: Path = args.sample
    if not sample_dir.is_dir():
        print(f"error: --sample is not a directory: {sample_dir}", file=sys.stderr)
        return 2

    audits = [audit_record(name, record) for name, record in load_sample(sample_dir)]
    print(render_report(audits))

    if args.out is not None:
        out: Path = args.out
        if out.suffix != ".json":
            out = out / "omnieda-audit.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "sample_dir": str(sample_dir),
            "gap_notes": GAP_NOTES,
            "boards": [audit.to_dict() for audit in audits],
        }
        out.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
        print(f"wrote {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
