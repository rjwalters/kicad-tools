"""Replay the reviewed revision-B copper only on its exact physical circuit.

This is saved PCB design data, including manual repairs, not an autorouter
success claim. Every replay still needs native zone refill, DRC and copper LVS.
"""

from __future__ import annotations

import hashlib
import json
import math
from collections import defaultdict
from pathlib import Path

from kicad_tools.router.quantize import OffAngleSegmentError, verify_segment_45
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_file, parse_string, serialize_sexp
from kicad_tools.validate.rules.clearance import CopperElement
from kicad_tools.validate.rules.diffpair_routing_continuity import DiffPairRoutingContinuityRule

PLAN = Path(__file__).with_name("routing-plan.json")


def apply_native_fab_floor(pcb_path):
    """Match JLC's published 0.45 mm pad-hole limit after generic rule emission.

    The generic profile currently carries a conservative 0.50 mm hole floor.
    This explicit board fabrication setting is hash-bound by readiness evidence.
    It does not alter pad geometry or suppress any violation by UUID.
    """
    project = Path(pcb_path).with_suffix(".kicad_pro")
    data = json.loads(project.read_text())
    data.setdefault("board", {}).setdefault("design_settings", {}).setdefault("rules", {})[
        "min_hole_to_hole"
    ] = 0.45
    project.write_text(json.dumps(data, indent=2) + "\n")


def _rounded(value):
    if isinstance(value, float):
        return round(value, 6)
    if isinstance(value, (list, tuple)):
        return [_rounded(v) for v in value]
    if isinstance(value, dict):
        return {k: _rounded(v) for k, v in value.items()}
    return value


def _reference(fp):
    return next(
        p.get_string(1) for p in fp.find_children("property") if p.get_string(0) == "Reference"
    )


def _silk(node):
    layer = node.find_child("layer")
    return layer is not None and layer.get_string(0) in {"F.SilkS", "B.SilkS"}


def physical_contract(path):
    """Ignore UUID/format churn, bind placement, every land/net, stack and pours."""
    pcb, doc = PCB.load(path), parse_file(path)
    footprints = []
    for fp in sorted(pcb.footprints, key=lambda f: f.reference):
        pads = [
            {
                "number": p.number,
                "type": p.type,
                "shape": p.shape,
                "position": p.position,
                "rotation": p.rotation % 360,
                "size": p.size,
                "drill": p.drill,
                "layers": sorted(p.layers),
                "net": p.net_name,
                "roundrect_rratio": p.roundrect_rratio,
                "mask_margin": p.solder_mask_margin,
            }
            for p in fp.pads
        ]
        footprints.append(
            {
                "ref": fp.reference,
                "library": fp.name,
                "value": fp.value,
                "position": fp.position,
                "rotation": fp.rotation % 360,
                "layer": fp.layer,
                "pads": sorted(pads, key=lambda p: (p["number"], p["position"])),
            }
        )
    setup = doc.find_child("setup")
    stack = setup.find_child("stackup")
    layers = []
    if stack:
        for layer in stack.find_children("layer"):
            layers.append(
                [
                    layer.get_string(0),
                    *[
                        layer.find_child(k).get_atoms() if layer.find_child(k) else None
                        for k in ["type", "thickness", "material", "epsilon_r", "loss_tangent"]
                    ],
                ]
            )
    return _rounded(
        {
            "origin": pcb.board_origin,
            "size": pcb.board_size,
            "outline": pcb.get_board_outline_segments(),
            "copper_layers": sorted(l.name for l in pcb.copper_layers),
            "footprints": footprints,
            "stackup": layers,
            "via_filling": setup.find_child("filling").get_string(0)
            if setup.find_child("filling")
            else "no",
            "via_capping": setup.find_child("capping").get_string(0)
            if setup.find_child("capping")
            else "no",
            "zones": sorted(
                [
                    {
                        "net": z.net_name,
                        "layer": z.layer,
                        "polygon": z.polygon,
                        "clearance": z.clearance,
                        "thermal_gap": z.thermal_gap,
                        "thermal_bridge_width": z.thermal_bridge_width,
                        "min_thickness": z.min_thickness,
                    }
                    for z in pcb.zones
                ],
                key=lambda z: (z["layer"], z["net"]),
            ),
        }
    )


def fingerprint(path):
    return hashlib.sha256(json.dumps(physical_contract(path), sort_keys=True).encode()).hexdigest()


def usb_geometry(path):
    """Circuit-specific bounds complement the generic branched-net percentage.

    The common run must be physically connected and paired on In2.Cu. The
    surrounding USB-C orientation contacts and 1.9 mm ESD lands are launches.
    These engineering bounds are not limits from the USB specification.
    """
    pcb = PCB.load(path)
    tracks = defaultdict(list)
    for segment in pcb.segments:
        tracks[pcb.nets[segment.net_number].name].append(segment)
    lengths = {
        n: sum(math.dist(s.start, s.end) for s in tracks[n])
        for n in ["USB_D+", "USB_D-", "USB_MCU_D+", "USB_MCU_D-"]
    }
    common = {}
    for name in ["USB_D+", "USB_D-"]:
        # Board-relative rectangle around the common ESD-to-termination run.
        common[name] = [
            CopperElement.from_segment(s)
            for s in tracks[name]
            if s.layer == "In2.Cu"
            and all(37.5 <= x <= 40.3 and 13 <= y <= 16.8 for x, y in [s.start, s.end])
        ]
        segments = common[name]
        if not segments:
            raise ValueError(f"Missing USB common run: {name}")
        reached = {0}
        while True:
            added = {
                i
                for i, s in enumerate(segments)
                if any(
                    math.dist(a, b) < 1e-5
                    for j in reached
                    for a in [s.geometry[:2], s.geometry[2:4]]
                    for b in [segments[j].geometry[:2], segments[j].geometry[2:4]]
                )
            }
            if added <= reached:
                break
            reached |= added
        if len(reached) != len(segments):
            raise ValueError(f"Disconnected USB common run: {name}")
    rule = DiffPairRoutingContinuityRule(engaged_pairs=set())
    coupled = min(
        rule._coupled_length(common[a], common[b])
        for a, b in [("USB_D+", "USB_D-"), ("USB_D-", "USB_D+")]
    )
    skew = abs(lengths["USB_D+"] - lengths["USB_D-"])
    if coupled < 4.0 or max(lengths["USB_D+"], lengths["USB_D-"]) > 16.0 or skew > 0.5:
        raise ValueError(
            f"USB branch bounds failed: lengths={lengths}, common={coupled}, skew={skew}"
        )
    if max(lengths["USB_MCU_D+"], lengths["USB_MCU_D-"]) > 4.0:
        raise ValueError("MCU USB launch exceeds 4 mm")
    return {"lengths_mm": lengths, "common_coupled_mm": coupled, "branch_skew_mm": skew}


def _require_45_degree_copper(path):
    # Parse the nodes rather than relying on KiCad's pretty-print layout.
    for segment in parse_file(path).find_children("segment"):
        start, end = segment.find_child("start"), segment.find_child("end")
        try:
            verify_segment_45(
                start.get_float(0),
                start.get_float(1),
                end.get_float(0),
                end.get_float(1),
                strict=True,
            )
        except OffAngleSegmentError as exc:
            raise ValueError(
                "Routing plan contains off-angle copper; repair and revalidate before replay"
            ) from exc


def save_plan(reviewed_board, plan_path=PLAN):
    _require_45_degree_copper(reviewed_board)
    doc = parse_file(reviewed_board)
    data = {
        "schema_version": 1,
        "physical_sha256": fingerprint(reviewed_board),
        "description": "Revision B fixed-placement copper, manually completed, USB pair repaired, and 45-degree aligned",
        "required_factory_options": {
            "layers": 4,
            "stackup": "JLC7628",
            "finish": "ENIG",
            "via_covering": "Epoxy-filled & Capped",
            "secondary_soldering": "J1 USB4085 through-hole",
            "minimum_pad_hole_spacing_mm": 0.45,
        },
        "usb_geometry": usb_geometry(reviewed_board),
        "copper": [serialize_sexp(n) for n in doc.children if n.name in {"segment", "via", "arc"}],
        "silkscreen": {
            _reference(fp): [serialize_sexp(n) for n in fp.children if _silk(n)]
            for fp in doc.find_children("footprint")
        },
    }
    Path(plan_path).write_text(json.dumps(data, indent=2) + "\n")


def apply_plan(input_path, output_path, plan_path=PLAN):
    plan = json.loads(Path(plan_path).read_text())
    if plan.get("schema_version") != 1 or fingerprint(input_path) != plan["physical_sha256"]:
        raise ValueError(
            "Routing plan does not match physical circuit; reroute and review this revision"
        )
    doc = parse_file(input_path)
    nets = {n.get_string(1): n.get_int(0) for n in doc.find_children("net")}
    doc.children = [n for n in doc.children if n.name not in {"segment", "via", "arc"}]
    for source in plan["copper"]:
        node = parse_string(source)
        net = node.find_child("net")
        name = net.get_string(0) if len(net.children) == 1 else net.get_string(1)
        node.remove_child("net")
        node.add(parse_string(f"(net {nets[name]})"))
        doc.add(node)
    for fp in doc.find_children("footprint"):
        reference = _reference(fp)
        fp.children = [n for n in fp.children if not _silk(n)]
        for source in plan["silkscreen"][reference]:
            fp.add(parse_string(source))
    output_path = Path(output_path)
    candidate = output_path.with_name(f".{output_path.stem}.candidate.kicad_pcb")
    try:
        candidate.write_text(serialize_sexp(doc))
        _require_45_degree_copper(candidate)
        usb_geometry(candidate)
        candidate.replace(output_path)
    finally:
        candidate.unlink(missing_ok=True)
    return True
