"""Reviewed two-layer fabrication: ordinary through vias, outside SMT lands.

JLC publishes 0.15 mm mechanical drills on two-layer boards as a paid option.
The seven fine-pitch escapes retain .30/.15 mm diameter/drill but must never
be described as laser microvias or use an unselected via-in-pad process.
"""

from __future__ import annotations

import hashlib
import json
import math
import uuid
from dataclasses import replace
from pathlib import Path

from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import parse_file, parse_string, serialize_sexp
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.rules.via_pad_geometry import is_smd_pad, pad_absolute_bbox

SOURCE = "https://jlcpcb.com/capabilities/pcb-capabilities/"
OPTIONS = {
    "layers": 2,
    "via_type": "through",
    "minimum_via_drill_mm": 0.15,
    "minimum_via_diameter_mm": 0.30,
    "minimum_via_annular_ring_mm": 0.075,
    "via_covering": "Tented",
    "paid_factory_option": "0.15 mm minimum via hole size",
    "via_in_pad": False,
    "source": SOURCE,
}
# Exact pad/net/placement contract; copper and UUIDs are checked separately.
PHYSICAL_SHA256 = "bde54c82117736336cbe72c2bb6329ca152626027ffff3ed81b0da98e9eb1288"
# ref.pad, net, reviewed original via center, reviewed off-pad center (board relative).
MOVES = [
    ("U1.2", "GND", (10.24, 11.61), (10.24, 12.55)),
    ("U2.35", "GND", (35.163, 19.75), (36.55, 19.75)),
    ("U2.23", "GND", (33.25, 25.838), (33.25, 24.95)),
    ("U2.47", "GND", (28.75, 17.838), (28.75, 19.0)),
    ("U2.8", "GND", (26.837, 22.75), (25.8, 22.75)),
    ("U2.6", "OSC_OUT", (26.8375, 21.75), (27.95, 21.75)),
    ("U2.39", "SWO", (32.75, 17.8375), (32.75, 19.0)),
    ("U2.7", "NRST", (27.3375, 22.25), (28.65, 22.25)),
]
INSET_ROUTE_MOVES = [
    # The inset pad seeds (#5004) route OSC_OUT beside C11.1: move its
    # ordinary 0.30 mm drill another 0.10 mm east to clear the SMT land.
    ("C11.1", "OSC_OUT", (24.7, 15.75), (24.8, 15.75)),
]


def physical_fingerprint(path):
    pcb = PCB.load(path)

    def rounded(x):
        if isinstance(x, float):
            return round(x, 6)
        if isinstance(x, (list, tuple)):
            return [rounded(v) for v in x]
        return x

    data = [
        pcb.board_origin,
        pcb.board_size,
        sorted(pcb.get_board_outline_segments()),
        parse_file(path).find_child("general").find_child("thickness").get_float(0),
        sorted(l.name for l in pcb.copper_layers),
        [
            (
                f.reference,
                f.name,
                f.value,
                f.layer,
                f.position,
                f.rotation,
                [
                    (
                        p.number,
                        p.position,
                        p.rotation,
                        p.size,
                        p.drill,
                        p.net_name,
                        p.type,
                        p.shape,
                        sorted(p.layers),
                    )
                    for p in sorted(f.pads, key=lambda p: (p.number, p.position))
                ],
            )
            for f in sorted(pcb.footprints, key=lambda f: f.reference)
        ],
    ]
    return hashlib.sha256(json.dumps(rounded(data), sort_keys=True).encode()).hexdigest()


def repair(pcb_path):
    """Move reviewed escapes/route vias and add a tail on every used layer."""
    pcb_path = Path(pcb_path)
    if physical_fingerprint(pcb_path) != PHYSICAL_SHA256:
        raise ValueError("Board04 physical design changed: review new via escapes")
    pcb, doc = PCB.load(pcb_path), parse_file(pcb_path)
    ox, oy = pcb.board_origin
    changed = 0
    for move in MOVES + INSET_ROUTE_MOVES:
        ref, net, old, new = move
        choices = [
            (i, v)
            for i, v in enumerate(pcb.vias)
            if pcb.nets[v.net_number].name == net and math.dist(v.position, old) < 0.001
        ]
        if not choices:
            if any(
                pcb.nets[v.net_number].name == net and math.dist(v.position, new) < 0.001
                for v in pcb.vias
            ):
                continue
            # The pinned historical route has no C11 transition at this
            # location. Its absence is allowed; validate_process below still
            # checks every drill against every SMT land on either variant.
            if move in INSET_ROUTE_MOVES:
                continue
            raise ValueError(f"Missing reviewed escape {ref}: cannot apply fixed repair")
        if len(choices) != 1:
            raise ValueError(f"Ambiguous escape {ref}")
        i, v = choices[0]
        node = doc.find_children("via")[i]
        at = node.find_child("at")
        at.set_atom(0, new[0] + ox)
        at.set_atom(1, new[1] + oy)
        node.children = [c for c in node.children if not (c.is_atom and c.value == "micro")]
        node.remove_child("tenting")
        node.add(parse_string("(tenting (front yes) (back yes))"))
        layers = {"F.Cu"}
        for segment in pcb.segments:
            if segment.net_number == v.net_number and any(
                math.dist(p, v.position) < 0.001 for p in [segment.start, segment.end]
            ):
                layers.add(segment.layer)
        for layer in sorted(layers):
            net_expr = serialize_sexp(node.find_child("net"))
            uid = uuid.uuid5(uuid.NAMESPACE_URL, f"board04-offpad:{ref}:{new}:{layer}")
            doc.add(
                parse_string(
                    f"(segment (start {v.position[0] + ox} {v.position[1] + oy}) "
                    f'(end {new[0] + ox} {new[1] + oy}) (width 0.15) (layer "{layer}") '
                    f'{net_expr} (uuid "{uid}"))'
                )
            )
        changed += 1
    for via in doc.find_children("via"):
        via.remove_child("tenting")
        via.add(parse_string("(tenting (front yes) (back yes))"))
    pcb_path.write_text(serialize_sexp(doc))
    trim_obsolete_nrst_tail(pcb_path)
    (pcb_path.parent / "manufacturing-requirements.json").write_text(
        json.dumps(OPTIONS, indent=2) + "\n"
    )
    validate_process(pcb_path, check_native=False)
    return changed


def trim_obsolete_nrst_tail(pcb_path):
    """Remove the unbonded back-layer remnant of U2.7's relocated escape.

    Some router builds leave the pad-to-old-via hop on both copper layers.
    The front hop still connects the SMT pad; its back-layer duplicate may
    terminate in empty space after the via moves. Only remove that exact hop
    when the shared topology checker confirms the old pad end is unbonded.
    """
    from kicad_tools.validate.rules.dangling_copper import DanglingCopperRule

    pcb_path = Path(pcb_path)
    pcb = PCB.load(pcb_path)
    old = next(old for ref, _, old, _ in MOVES if ref == "U2.7")
    pad_end = (old[0] - 0.5, old[1])
    candidates = [
        segment
        for segment in pcb.segments
        if segment.layer == "B.Cu"
        and pcb.nets[segment.net_number].name == "NRST"
        and any(
            math.dist(a, pad_end) < 0.001 and math.dist(b, old) < 0.001
            for a, b in [(segment.start, segment.end), (segment.end, segment.start)]
        )
    ]
    if not candidates:
        return 0
    findings = DanglingCopperRule().check(pcb, process_rules()).violations
    if not any(
        finding.rule_id == "track_dangling"
        and finding.layer == "B.Cu"
        and "NRST" in finding.nets
        and math.dist(finding.location, pad_end) < 0.001
        for finding in findings
    ):
        return 0
    remove = {segment.uuid for segment in candidates}
    doc = parse_file(pcb_path)
    doc.children = [
        node
        for node in doc.children
        if not (
            node.name == "segment"
            and node.find_child("uuid")
            and node.find_child("uuid").get_string(0) in remove
        )
    ]
    pcb_path.write_text(serialize_sexp(doc))
    return len(remove)


def process_rules():
    from kicad_tools.manufacturers import get_profile

    return replace(
        get_profile("jlcpcb-tier1").get_design_rules(2),
        min_via_drill_mm=0.15,
        min_via_diameter_mm=0.30,
        min_annular_ring_mm=0.075,
        via_in_pad_supported=False,
    )


def apply_native_floors(pcb_path):
    """Emit the exact same reviewed DesignRules used by the Python checker."""
    from kicad_tools.manufacturers import write_drc_constraints

    write_drc_constraints(
        Path(pcb_path),
        process_rules(),
        manufacturer_id="jlcpcb-tier1",
        layers=2,
        copper_oz=1.0,
        write_dru=True,
    )

    project = Path(pcb_path).with_suffix(".kicad_pro")
    data = json.loads(project.read_text())
    data["board"]["design_settings"]["rules"]["min_via_annular_width"] = 0.075
    project.write_text(json.dumps(data, indent=2) + "\n")


def validate_process(pcb_path, *, check_native=True):
    pcb_path = Path(pcb_path)
    if physical_fingerprint(pcb_path) != PHYSICAL_SHA256:
        raise ValueError("Board04 physical design changed")
    if json.loads((pcb_path.parent / "manufacturing-requirements.json").read_text()) != OPTIONS:
        raise ValueError("Select the reviewed paid 0.15 mm drilling option")
    pcb = PCB.load(pcb_path)
    for via in pcb.vias:
        if via.tenting_front != "yes" or via.tenting_back != "yes":
            raise ValueError("All through vias must have front/back tenting selected")
    pads = [
        pad_absolute_bbox(p, f)
        for f in pcb.footprints
        for p in sorted(f.pads, key=lambda p: (p.number, p.position))
        if is_smd_pad(p)
    ]
    for v in pcb.vias:
        if v.via_type not in (None, "through"):
            raise ValueError("Only mechanical through vias are supported")
        if v.drill < 0.15 - 1e-6 or v.size < 0.30 - 1e-6 or (v.size - v.drill) / 2 < 0.075 - 1e-6:
            raise ValueError("Via violates reviewed drilling dimensions")
        x, y = v.position
        for left, top, right, bottom in pads:
            distance = math.hypot(max(left - x, 0, x - right), max(top - y, 0, y - bottom))
            if distance < v.drill / 2 + 0.10 - 1e-6:
                raise ValueError("Via drill too close to an SMT soldering land")
    if check_native:
        rules = json.loads(pcb_path.with_suffix(".kicad_pro").read_text())["board"][
            "design_settings"
        ]["rules"]
        if any(
            abs(rules[k] - v) > 1e-9
            for k, v in {
                "min_via_hole": 0.15,
                "min_via_diameter": 0.30,
                "min_via_annular_width": 0.075,
            }.items()
        ):
            raise ValueError("Native via floors differ from reviewed drilling option")
    return pcb


def make_checker(pcb_path):
    pcb = validate_process(pcb_path)
    checker = DRCChecker(pcb, "jlcpcb-tier1", layers=2, warn_on_inactive_skew_rules=False)
    checker.design_rules = process_rules()
    return checker
