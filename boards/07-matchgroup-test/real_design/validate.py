#!/usr/bin/env python3
"""Check a staged SDRAM board against its original circuit and timing budgets.

Copies the candidate before native refill. Never edits published board artifacts.
"""

from __future__ import annotations

import argparse
import json
import math
import shutil
import subprocess
import sys
from pathlib import Path

from shapely.geometry import Point, Polygon  # type: ignore[import-untyped]
from shapely.ops import unary_union  # type: ignore[import-untyped]

from kicad_tools.analysis.net_status import NetStatusAnalyzer
from kicad_tools.analysis.trace_length import TraceLengthAnalyzer
from kicad_tools.lvs.recipe import write_lvs_report
from kicad_tools.manufacturers import get_profile
from kicad_tools.manufacturers.project_generator import write_drc_constraints
from kicad_tools.physics import Stackup, TransmissionLine
from kicad_tools.schema.pcb import PCB


def copper_elevations(stack: Stackup) -> dict[str, float]:
    """Copper midplanes, including every native dielectric sublayer."""
    elevations = {}
    depth = 0.0
    for layer in stack.layers:
        if layer.is_copper:
            elevations[layer.name] = depth + layer.thickness_mm / 2
        depth += layer.thickness_mm
    return elevations


def via_travel_mm(via, segments, elevations: dict[str, float]) -> float:
    """Conservative vertical travel between layers actually joined by copper.

    A standard through hole retains its complete physical barrel. Its unused
    barrel is an electrical stub, not extra series propagation distance. This
    includes all attached layers, so a branched net is never shortened to an
    arbitrary selected endpoint path. Stub capacitance needs separate review.
    """
    attached = set()
    px, py = via.position
    for segment in segments:
        ax, ay = segment.start
        bx, by = segment.end
        dx, dy = bx - ax, by - ay
        denominator = dx * dx + dy * dy
        fraction = (
            max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / denominator))
            if denominator
            else 0.0
        )
        distance = math.hypot(px - ax - fraction * dx, py - ay - fraction * dy)
        if distance <= (via.size + segment.width) / 2 + 1e-6:
            attached.add(segment.layer)
    if not attached:
        return 0.0
    depths = [elevations[layer] for layer in attached]
    return max(depths) - min(depths)


def via_barrel_capacitance_pf(
    pad_diameter_mm: float,
    antipad_diameter_mm: float,
    board_thickness_mm: float,
    epsilon_r: float,
) -> float | None:
    """Full-barrel via capacitance from measured pad/antipad/stackup geometry.

    TI SLYT335 p30: ``Cvia[pF] = 1.41 * er * T[inches] * Dpad / (Dantipad - Dpad)``.
    Returns ``None`` for degenerate geometry (antipad at or inside the pad, or
    any non-positive input) so callers fall back to the flat per-via allowance
    instead of dividing by zero or a negative gap.
    """
    values = (pad_diameter_mm, antipad_diameter_mm, board_thickness_mm, epsilon_r)
    if not all(math.isfinite(value) and value > 0 for value in values):
        return None
    gap_mm = antipad_diameter_mm - pad_diameter_mm
    if gap_mm <= 0:
        return None
    thickness_in = board_thickness_mm / 25.4
    return 1.41 * epsilon_r * thickness_in * pad_diameter_mm / gap_mm


def stackup_average_epsilon_r(stack: Stackup) -> float | None:
    """Use only complete explicit dielectric data, never synthesized defaults."""
    if not stack.has_explicit_data:
        return None
    copper_indices = [i for i, layer in enumerate(stack.layers) if layer.is_copper]
    if len(copper_indices) < 2:
        return None
    barrel_layers = stack.layers[copper_indices[0] : copper_indices[-1] + 1]
    # Surface paste/mask are outside the barrel and may carry zero defaults.
    # Every adjacent pair of copper layers must have explicit dielectric data.
    if any(
        not any(layer.is_dielectric for layer in stack.layers[a + 1 : b])
        for a, b in zip(copper_indices, copper_indices[1:], strict=False)
    ):
        return None
    dielectrics = [layer for layer in barrel_layers if layer.is_dielectric]
    if not dielectrics or not all(
        math.isfinite(layer.thickness_mm) and layer.thickness_mm > 0 for layer in barrel_layers
    ):
        return None
    if not all(math.isfinite(layer.epsilon_r) and layer.epsilon_r > 0 for layer in dielectrics):
        return None
    total_thickness = sum(layer.thickness_mm for layer in dielectrics)
    return sum(layer.thickness_mm * layer.epsilon_r for layer in dielectrics) / total_thickness


def board_reference_copper(board: PCB) -> dict:
    """Actual filled GND/+3V3 copper on Board07's designated reference layers.

    Native filled polygons can encode holes with doubled bridges; buffer(0)
    resolves those bridges into polygon interiors. Missing fills stay missing.
    Project clearance rules and unfilled zone outlines are never measurements.
    """
    planes = {}
    for layer, net_name in (("In1.Cu", "GND"), ("In4.Cu", "+3V3")):
        polygons = []
        for zone in board.zones:
            if zone.net_name != net_name:
                continue
            for index, points in enumerate(zone.filled_polygons):
                if zone.filled_polygon_layer(index) != layer:
                    continue
                if len(points) < 3 or not all(math.isfinite(v) for point in points for v in point):
                    return {}
                polygons.append(Polygon(points).buffer(0))
        if polygons:
            planes[layer] = unary_union(polygons)
    return planes


def measured_antipad_diameter_mm(via, planes: dict) -> float | None:
    """Recognize a circular, enclosed antipad in both Board07 reference planes.

    The 5 um circularity tolerance permits native polygon tessellation, not
    slots, plane edges, overlapping cutouts or thermal spokes. Use the smaller
    inscribed diameter across the two planes in the approximate full-barrel
    model. Unsupported topology returns None rather than a fictitious hole.
    """
    if set(via.layers) != {"F.Cu", "B.Cu"} or via.via_type is not None:
        return None
    point = Point(via.position)
    diameters = []
    for layer in ("In1.Cu", "In4.Cu"):
        copper = planes.get(layer)
        if copper is None or copper.is_empty or copper.covers(point):
            return None
        polygons = [copper] if copper.geom_type == "Polygon" else list(copper.geoms)
        holes = [
            Polygon(ring)
            for polygon in polygons
            if polygon.geom_type == "Polygon"
            for ring in polygon.interiors
            if Polygon(ring).contains(point)
        ]
        if len(holes) != 1:
            return None
        hole = holes[0]
        inner_radius = float(hole.boundary.distance(point))
        outer_radius = max(math.dist(via.position, xy) for xy in hole.exterior.coords)
        if inner_radius <= via.size / 2 or outer_radius - inner_radius > 0.005:
            return None
        # Islands/spokes inside the hole also disqualify the annular model.
        if copper.distance(point) < inner_radius - 1e-6:
            return None
        diameters.append(2 * inner_radius)
    return min(diameters)


def net_via_capacitance_pf(
    vias: list,
    reference_copper: dict,
    board_thickness_mm: float,
    epsilon_r: float | None,
) -> float | None:
    """Sum physical vias only when every barrel has measured usable geometry."""
    if epsilon_r is None or not vias:
        return None
    total_pf = 0.0
    for via in vias:
        antipad = measured_antipad_diameter_mm(via, reference_copper)
        if antipad is None:
            return None
        capacitance = via_barrel_capacitance_pf(
            pad_diameter_mm=via.size,
            antipad_diameter_mm=antipad,
            board_thickness_mm=board_thickness_mm,
            epsilon_r=epsilon_r,
        )
        if capacitance is None:
            return None
        total_pf += capacitance
    return total_pf


def external_load(
    name: str,
    trace_pf: float,
    physical_via_count: int,
    via_capacitance_pf: float | None = None,
) -> dict:
    """Per-driver load screen; unused through barrels still contribute load.

    ST DS9405 tables 103/105 specify 15 pF for SDCLK and 30 pF otherwise.
    ISSI specifies maximum 3.5 pF CLK and 3.8 pF other inputs. DQ is
    bidirectional and is now evaluated explicitly in both directions instead
    of one folded number: SDRAM-drives/MCU-receives uses a 10 pF engineering
    reserve (ST's DS9405 only publishes a 5 pF *typical* CIO with no maximum),
    while MCU-drives/SDRAM-receives uses ISSI's sourced 6.5 pF DQ maximum
    (RevG1 p14). The worse direction governs today only because both share
    the same 30 pF net-level limit -- a future asymmetric per-direction limit
    would now be caught instead of silently passing (#5134).

    ``via_capacitance_pf``, when provided, is the TI SLYT335 geometry-derived
    capacitance summed over this net's actual physical vias (see
    :func:`net_via_capacitance_pf`); ``None`` falls back to the conservative
    flat 1 pF/via allowance (which exceeds the ~0.68 pF nominal SLYT335
    estimate for board07's own .5/.8 mm pad/antipad, 1.6 mm, Er4.6
    construction -- see ``engineering/load-review.md``).
    """
    if name == "SDCLK":
        directions = {"MCU drives, SDRAM receives (clock)": (3.5, "ISSI maximum")}
    elif name.startswith("DQ"):
        directions = {
            "SDRAM drives, MCU receives": (10.0, "engineering reserve"),
            "MCU drives, SDRAM receives": (6.5, "ISSI maximum"),
        }
    else:
        directions = {"MCU drives, SDRAM receives": (3.8, "ISSI maximum")}

    if via_capacitance_pf is not None:
        via_pf = via_capacitance_pf
        via_basis = "TI SLYT335 geometry (measured pad/antipad/stackup)"
    else:
        via_pf = float(physical_via_count)
        via_basis = "1 pF/via allowance (geometry unavailable)"

    direction_entries = [
        (direction, float(receiver_pf), basis, trace_pf + via_pf + receiver_pf)
        for direction, (receiver_pf, basis) in directions.items()
    ]
    per_direction = {
        direction: {
            "receiver_allowance_pf": receiver_pf,
            "receiver_basis": basis,
            "estimated_external_load_pf": estimated_pf,
        }
        for direction, receiver_pf, basis, estimated_pf in direction_entries
    }
    worst_direction, worst_receiver_pf, worst_basis, worst_estimated_pf = max(
        direction_entries, key=lambda entry: entry[3]
    )
    return {
        "trace_pf": trace_pf,
        "full_through_via_count": physical_via_count,
        "via_allowance_pf": via_pf,
        "via_basis": via_basis,
        "directions": per_direction,
        "worst_direction": worst_direction,
        "receiver_allowance_pf": worst_receiver_pf,
        "receiver_basis": worst_basis,
        "estimated_external_load_pf": worst_estimated_pf,
        "external_limit_pf": 15.0 if name == "SDCLK" else 30.0,
    }


def kct_design_gate_passes(report: dict, returncode: int, manifest_present: bool) -> bool:
    """Allow only an explicitly absent export after every design gate passes."""
    meta = report.get("meta_checks", {})
    clean_design = (
        report["summary"]["errors"] == 0
        and report["summary"]["warnings"] == 0
        and all(meta.get(key, {}).get("status") == "PASSED" for key in ("drc", "erc", "lvs"))
    )
    if not clean_design:
        return False
    return (returncode == 0 and meta.get("manifest", {}).get("status") == "PASSED") or (
        returncode == 2
        and meta.get("manifest", {}).get("status") == "NOT RUN"
        and not manifest_present
    )


def physical_pad_geometry(board: PCB) -> dict:
    """Reject routing-only ports even when their logical pin/net map matches."""

    def rounded(values):
        return tuple(round(value, 6) for value in values)

    return {
        fp.reference: {
            "footprint": fp.name,
            "value": fp.value,
            "position": rounded(fp.position),
            "rotation": round(fp.rotation % 360, 6),
            "layer": fp.layer,
            "pads": {
                pad.number: {
                    "position": rounded(pad.position),
                    "size": rounded(pad.size),
                    "rotation": round(pad.rotation % 360, 6),
                    "layers": tuple(sorted(pad.layers)),
                    "drill": round(pad.drill, 6),
                    "type": pad._sexp_node.get_string(1),
                    "shape": pad._sexp_node.get_string(2),
                }
                for pad in fp.pads
            },
        }
        for fp in board.footprints
    }


def timing_errors(lengths: dict[str, float], constraints: dict) -> list[str]:
    """Check cross-group clock relationships, not just each group's spread."""
    errors = []
    required = {net for members in constraints["groups"].values() for net in members}
    missing = required - lengths.keys()
    if missing:
        errors.append(f"Missing routed length: {', '.join(sorted(missing))}")
    for group, members in constraints["groups"].items():
        if any(name not in lengths for name in members):
            continue
        values = [lengths[name] for name in members]
        spread = max(values) - min(values)
        if spread > constraints["group_skew_mm"] + 1e-6:
            errors.append(f"{group}: spread {spread:.3f} mm exceeds {constraints['group_skew_mm']}")
    reference = lengths.get(constraints["clock_reference"])
    for name in sorted(required & lengths.keys()):
        length = lengths[name]
        if length > constraints["max_trace_length_mm"] + 1e-6:
            errors.append(f"{name}: length {length:.3f} mm exceeds maximum")
        if reference is not None:
            skew = abs(length - reference)
            if skew > constraints["clock_relative_tolerance_mm"] + 1e-6:
                errors.append(f"{name}: clock-relative difference {skew:.3f} mm exceeds tolerance")
    return errors


def check(candidate: Path, source: Path, evidence: Path) -> bool:
    evidence.mkdir(parents=True, exist_ok=True)
    board_path = evidence / "checked.kicad_pcb"
    shutil.copy2(candidate, board_path)
    shutil.copy2(source / "sdram_demo.kicad_pro", board_path.with_suffix(".kicad_pro"))
    schematic = board_path.with_suffix(".kicad_sch")
    shutil.copy2(source / "sdram_demo.kicad_sch", schematic)
    # Fill with the final native project AND custom rules before any copper
    # measurements. A later .dru clearance can change plane fills even when
    # the project minima are preserved (#5023).
    write_drc_constraints(
        board_path,
        get_profile("jlcpcb").get_design_rules(layers=6),
        manufacturer_id="jlcpcb",
        layers=6,
    )
    subprocess.run(
        [
            "kicad-cli",
            "sch",
            "erc",
            "--format",
            "json",
            "-o",
            str(evidence / "erc.json"),
            str(schematic),
        ],
        check=True,
        timeout=120,
    )
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--refill-zones",
            "--save-board",
            "--format",
            "json",
            "-o",
            str(evidence / "native-drc.json"),
            str(board_path),
        ],
        check=True,
        timeout=120,
    )
    native = json.loads((evidence / "native-drc.json").read_text())
    erc = json.loads((evidence / "erc.json").read_text())
    errors = [
        f"Native DRC: {v['description']}" for v in native["violations"] if v["severity"] == "error"
    ]
    errors.extend(f"Native open: {v['description']}" for v in native["unconnected_items"])
    errors.extend(
        f"ERC: {v['description']}"
        for sheet in erc.get("sheets", [])
        for v in sheet.get("violations", [])
        if v["severity"] == "error"
    )
    board = PCB.load(board_path)
    expected = json.loads((source / "circuit.json").read_text())["pin_nets"]
    actual = {
        fp.reference: {p.number: p.net_name for p in fp.pads if p.net_name}
        for fp in board.footprints
    }
    if actual != expected:
        errors.append("Persisted candidate pad/net mapping differs from the authored circuit")
    authored = PCB.load(source / "sdram_demo.kicad_pcb")
    if physical_pad_geometry(board) != physical_pad_geometry(authored):
        errors.append("Candidate footprint/pad geometry differs from the physical authored board")

    def segment_identity(segment):
        return (
            segment.uuid,
            segment.net_name,
            segment.layer,
            tuple(round(x, 6) for x in segment.start + segment.end),
            round(segment.width, 6),
        )

    authored_escapes = {segment_identity(s) for s in authored.segments if s.layer == "F.Cu"}
    statuses = NetStatusAnalyzer(board, strict=True).analyze()
    # No name-based exception for power: every required net must be complete.
    incomplete = [n.net_name for n in statuses.nets if n.status != "complete"]
    if incomplete:
        errors.append(f"Strict copper connectivity incomplete: {', '.join(sorted(incomplete))}")
    constraints = json.loads((source / "sdram_constraints.json").read_text())
    stack = Stackup.from_pcb(board)
    line = TransmissionLine(stack)
    elevations = copper_elevations(stack)
    signal_layers = {layer.name for layer in stack.layers if layer.is_signal_layer}
    # Extract actual reference-plane fill and explicit dielectric inputs once.
    # Missing data or unsupported topology preserves the whole-net fallback.
    reference_copper = board_reference_copper(board)
    dielectric_epsilon_r = stackup_average_epsilon_r(stack)
    for via in board.vias:
        if set(via.layers) != {"F.Cu", "B.Cu"} or via.via_type is not None:
            errors.append(f"Nonstandard via span/type at {via.position}: {via.layers}")
    analyzer = TraceLengthAnalyzer()
    lengths = {}
    impedance = {}
    trace_capacitance = {}
    external_loads = {}
    for name in sorted({n for members in constraints["groups"].values() for n in members}):
        report = analyzer.analyze_net(board, name)
        if not report.segment_count:
            continue
        net = board.get_net_by_name(name)
        segments = list(board.segments_in_net(net.number))
        allowed = (
            {"F.Cu", "In2.Cu"}
            if name.startswith("DQ") or name in {"LDQM", "UDQM"}
            else {"In3.Cu", "B.Cu"}
        )
        for segment in segments:
            if segment.layer not in allowed and segment_identity(segment) not in authored_escapes:
                errors.append(
                    f"{name}: trunk on {segment.layer} violates data/control layer separation"
                )
        lengths[name] = report.total_length_mm + sum(
            via_travel_mm(via, segments, elevations) for via in board.vias_in_net(net.number)
        )
        dimensions = {(s.layer, s.width) for s in segments}
        measured = []
        capacitance_pf = 0.0
        for layer, width in sorted(dimensions):
            if layer not in signal_layers:
                errors.append(f"{name}: signal copper on reserved reference layer {layer}")
                continue
            try:
                calculate = line.microstrip if stack.is_outer_layer(layer) else line.stripline
                electrical = calculate(width_mm=width, layer=layer)
                z = electrical.z0
            except ValueError as error:
                errors.append(f"{name}: cannot evaluate {layer} impedance: {error}")
                continue
            measured.append({"layer": layer, "width_mm": width, "impedance_ohm": z})
            length_mm = sum(
                math.dist(s.start, s.end) for s in segments if (s.layer, s.width) == (layer, width)
            )
            # C'=1/(Z0*v), for the same quasi-TEM model used for impedance.
            capacitance_pf += length_mm * 1e9 / (z * electrical.phase_velocity)
            if abs(z - 50.0) > 5.0:
                errors.append(f"{name}: {layer} width {width} gives {z:.2f} ohm outside 45–55")
        impedance[name] = measured
        trace_capacitance[name] = capacitance_pf
        # Only trust the geometry path when the physical vias actually found
        # on this net match the count the trace-length analyzer used for
        # ``physical_via_count`` -- keeps the reported allowance and its
        # measured basis describing the same set of physical barrels.
        physical_vias = list(board.vias_in_net(net.number))
        via_pf_measured = (
            net_via_capacitance_pf(
                physical_vias, reference_copper, stack.board_thickness_mm, dielectric_epsilon_r
            )
            if len(physical_vias) == report.via_count
            else None
        )
        load = external_load(
            name, capacitance_pf, report.via_count, via_capacitance_pf=via_pf_measured
        )
        external_loads[name] = load
        if load["estimated_external_load_pf"] > load["external_limit_pf"]:
            errors.append(
                f"{name}: external load {load['estimated_external_load_pf']:.3f} pF "
                f"exceeds {load['external_limit_pf']} pF"
            )
    if board._sexp.find_children("arc"):
        errors.append(
            "Arc copper length is not supported by this checker; measure it before release"
        )
    errors.extend(timing_errors(lengths, constraints))
    copper_clean, label_clean = write_lvs_report(
        schematic, board_path, evidence, require_clean=False, run_copper=True, run_label=True
    )
    if not copper_clean or not label_clean:
        errors.append("Schematic/PCB copper or label LVS failed")
    filled_rule_sources = {
        suffix: board_path.with_suffix(suffix).read_bytes()
        for suffix in (".kicad_pro", ".kicad_dru")
    }
    with (evidence / "check.log").open("w") as log:
        result = subprocess.run(
            [
                "kct",
                "check",
                str(board_path),
                "--mfr",
                "jlcpcb",
                "--net-class-map",
                str(source / "net_class_map.json"),
                "--format",
                "json",
                "--output",
                str(evidence / "check.json"),
                "--emit-drc-constraints",
            ],
            stdout=log,
            stderr=subprocess.STDOUT,
            timeout=180,
        )
    tool_report = json.loads((evidence / "check.json").read_text())
    for suffix, filled_source in filled_rule_sources.items():
        if board_path.with_suffix(suffix).read_bytes() != filled_source:
            errors.append(f"Native rule source {suffix} changed after validated zone fill")
    meta = tool_report.get("meta_checks", {})
    manifest_status = meta.get("manifest", {}).get("status")
    manifest_absent = not any(
        (directory / "manufacturing/manifest.json").exists()
        for directory in (board_path.parent, board_path.parent.parent)
    )
    # A design must pass its physical/electrical gates before export exists.
    # Never treat an existing stale/failed manufacturing bundle as this stage.
    if not kct_design_gate_passes(tool_report, result.returncode, not manifest_absent):
        errors.append(f"Full kct design checks failed (exit {result.returncode})")
    # Recheck the already-filled physical board with the emitted native rules.
    # This invocation does not modify copper after LVS/length measurement.
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--format",
            "json",
            "-o",
            str(evidence / "native-drc-resolved.json"),
            str(board_path),
        ],
        check=True,
        timeout=120,
    )
    resolved_native = json.loads((evidence / "native-drc-resolved.json").read_text())
    errors.extend(
        f"Resolved native DRC: {finding['description']}"
        for finding in resolved_native["violations"]
        if finding["severity"] == "error"
    )
    errors.extend(
        f"Resolved native open: {finding['description']}"
        for finding in resolved_native["unconnected_items"]
    )
    with (evidence / "clock-spacing.log").open("w") as log:
        subprocess.run(
            [
                sys.executable,
                str(Path(__file__).parent / "engineering/clock_spacing.py"),
                str(board_path),
                str(evidence / "clock-spacing.json"),
            ],
            check=True,
            stdout=log,
            timeout=120,
        )
    clock_spacing = json.loads((evidence / "clock-spacing.json").read_text())
    errors.extend(
        f"Clock spacing: {finding['foreign_net']} at {finding['edge_gap_mm']} mm"
        for finding in clock_spacing["findings"]
        if finding["scope"] == "trace/via spacing"
    )
    report = {
        "status": "pass" if not errors else "blocked",
        "errors": errors,
        "lengths_including_vias_mm": lengths,
        "impedance": impedance,
        "trace_capacitance_pf": trace_capacitance,
        "external_load_estimates": external_loads,
        "validation_scope": "Physical/electrical design preflight; manufacturing bundle is a separate gate",
        "manufacturing_complete": manifest_status == "PASSED",
        "manufacturing_manifest_status": manifest_status,
        "capacitance_scope": (
            "trace_capacitance_pf models traces only; external_load_estimates includes "
            "receiver and full-via allowances, and DQ is evaluated in both directions "
            "(ISSI receiver maxima where specified; DQ MCU-receiver engineering reserve). "
            "The via allowance is now TI SLYT335 geometry-derived from the board's own "
            "measured pad size, enclosed circular filled-plane openings and explicit stackup dielectric "
            "when that geometry is available, else the conservative flat 1 pF/via "
            "allowance. MCU package/input capacitance corners remain an unconfirmed "
            "engineering reserve -- ST's IBIS archive was still unreachable during this "
            "pass, so the reserve is retained rather than invented; see #5134 and "
            "engineering/load-review.md. Slew/overshoot/undershoot/setup-hold/SSN "
            "modeling stays explicitly out of scope for this static lumped-capacitance "
            "screen and belongs to hardware bring-up or a dedicated SI simulation tool."
        ),
        "hardware_tested": False,
        "assembly_inventory_verified": False,
    }
    (evidence / "validation.json").write_text(json.dumps(report, indent=2))
    print(
        json.dumps({"status": report["status"], "errors": len(errors), "evidence": str(evidence)})
    )
    return not errors


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("candidate", type=Path)
    parser.add_argument("source", type=Path)
    parser.add_argument("evidence", type=Path)
    args = parser.parse_args()
    raise SystemExit(
        0 if check(args.candidate.resolve(), args.source.resolve(), args.evidence.resolve()) else 2
    )
