#!/usr/bin/env python3
"""Build the real, four-channel LVDS demonstration circuit (revision C).

Run with ``uv run python boards/06-diffpair-test/assembled-demo/generate.py OUT``.
The historical synthetic protocol regression recipe remains separate.
Pin numbers below are from TI SLLS373M, tables 5-1 and 5-2 (SOIC D package).
"""

from __future__ import annotations

import argparse
import json
import math
import os
import uuid
from pathlib import Path

import yaml

from kicad_tools.core.project_file import create_minimal_project, save_project
from kicad_tools.router.rules import NetClassRouting, net_class_map_to_dict
from kicad_tools.schematic import PinDef, PinSide, PinType, SymbolDef, generate_symbol_sexp
from kicad_tools.schematic.models.schematic import Schematic
from kicad_tools.sexp import parse_file, parse_string

SOIC = "Package_SO:SOIC-8_3.9x4.9mm_P1.27mm"
RES = "Resistor_SMD:R_0603_1608Metric"
CAP = "Capacitor_SMD:C_0603_1608Metric"
HEADER = "Connector_PinHeader_2.54mm:PinHeader_1x{:02d}_P2.54mm_Vertical"
DATASHEET = "https://www.ti.com/lit/ds/symlink/sn65lvds1.pdf"


def components():
    result = []

    def add(ref, value, footprint, xy, pins, names=None, types=None, angle=0, lcsc="", mpn=""):
        result.append(
            {
                "ref": ref,
                "value": value,
                "footprint": footprint,
                "xy": xy,
                "pins": pins,
                "names": names or {},
                "types": types or {},
                "angle": angle,
                "lcsc": lcsc,
                "mpn": mpn or value,
            }
        )

    add(
        "J1",
        "3.3V regulated input",
        HEADER.format(2),
        (12, 12),
        {"1": "+3V3", "2": "GND"},
        mpn="Samtec TSW-102-07-G-S",
    )
    for ref, x, prefix in (("J2", 12, "IN"), ("J3", 88, "OUT")):
        add(
            ref,
            f"{prefix}1-4 / GND",
            HEADER.format(8),
            (x, 26),
            {str(i): f"{prefix}{(i + 1) // 2}" if i % 2 else "GND" for i in range(1, 9)},
            mpn="Samtec TSW-108-07-G-S",
        )
    add(
        "C17",
        "10uF",
        "Capacitor_SMD:C_0805_2012Metric",
        (19, 12),
        {"1": "+3V3", "2": "GND"},
        lcsc="C15850",
        mpn="CL21A106KAYNNNE",
    )
    for i in range(1, 5):
        y = 24 + 12 * (i - 1)
        p, n = f"LVDS{i}_P", f"LVDS{i}_N"
        driver = str(2 * i - 1)
        receiver = str(2 * i)
        add(
            "U" + driver,
            "SN65LVDS1DR",
            SOIC,
            (34, y),
            {
                "1": "+3V3",
                "2": f"IN{i}",
                "3": None,
                "4": "GND",
                "5": None,
                "6": None,
                "7": p,
                "8": n,
            },
            {"1": "VCC", "2": "D", "3": "NC", "4": "GND", "5": "NC", "6": "NC", "7": "Y", "8": "Z"},
            {
                "1": "power_in",
                "2": "input",
                "3": "no_connect",
                "4": "power_in",
                "5": "no_connect",
                "6": "no_connect",
                "7": "output",
                "8": "output",
            },
            lcsc="C2671256",
        )
        add(
            "U" + receiver,
            "SN65LVDS2DR",
            SOIC,
            (68, y),
            {
                "1": n,
                "2": p,
                "3": None,
                "4": None,
                "5": "GND",
                "6": None,
                "7": f"OUT{i}",
                "8": "+3V3",
            },
            {"1": "B", "2": "A", "3": "NC", "4": "NC", "5": "GND", "6": "NC", "7": "R", "8": "VCC"},
            {
                "1": "input",
                "2": "input",
                "3": "no_connect",
                "4": "no_connect",
                "5": "power_in",
                "6": "no_connect",
                "7": "output",
                "8": "power_in",
            },
            lcsc="C2671054",
        )
        # TI recommends both 1nF and 100nF locally. 1nF is nearest VCC.
        for j, x in enumerate((29, 73)):
            for k, (value, dy, lcsc, mpn) in enumerate(
                (
                    ("1nF", -3.5, "C1588", "CL10B102KB8NNNC"),
                    ("100nF", -6, "C14663", "CC0603KRX7R9BB104"),
                )
            ):
                add(
                    f"C{4 * (i - 1) + 2 * j + k + 1}",
                    value,
                    CAP,
                    (x, y + dy),
                    {"1": "+3V3", "2": "GND"},
                    lcsc=lcsc,
                    mpn=mpn,
                )
        add(
            f"R{i}",
            "100",
            RES,
            (63, y - 1.27),
            {"1": p, "2": n},
            angle=90,
            lcsc="C22775",
            mpn="0603WAF1000T5E",
        )
        add(
            f"R{i + 4}",
            "100k",
            RES,
            (23, y),
            {"1": f"IN{i}", "2": "GND"},
            lcsc="C25803",
            mpn="0603WAF1003T5E",
        )
    return result


def footprint_root():
    candidates = [
        os.environ.get("KICAD10_FOOTPRINT_DIR", ""),
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
        "/usr/share/kicad/footprints",
    ]
    for candidate in candidates:
        if candidate and Path(candidate).is_dir():
            return Path(candidate)
    raise RuntimeError("Set KICAD10_FOOTPRINT_DIR to the installed KiCad footprint library")


def make_pcb(out, parts):
    # 100 x 75 mm. Native KiCad library geometry is copied unchanged.
    pcb = parse_string("""(kicad_pcb (version 20260206) (generator "kicad-tools")
      (general (thickness 1.6)) (paper "A4")
      (layers (0 "F.Cu" signal) (1 "In1.Cu" power) (2 "In2.Cu" power)
       (31 "B.Cu" signal) (35 "F.Paste" user) (37 "F.SilkS" user)
       (39 "F.Mask" user) (38 "B.Mask" user) (44 "Edge.Cuts" user)
       (46 "B.CrtYd" user) (47 "F.CrtYd" user) (48 "B.Fab" user) (49 "F.Fab" user))
      (setup (pad_to_mask_clearance 0)
       (stackup (layer "F.Cu" (type "copper") (thickness 0.035))
        (layer "dielectric 1" (type "prepreg") (thickness 0.2104) (material "7628") (epsilon_r 4.6) (loss_tangent 0.02))
        (layer "In1.Cu" (type "copper") (thickness 0.0152))
        (layer "dielectric 2" (type "core") (thickness 1.065) (material "FR4") (epsilon_r 4.6) (loss_tangent 0.02))
        (layer "In2.Cu" (type "copper") (thickness 0.0152))
        (layer "dielectric 3" (type "prepreg") (thickness 0.2104) (material "7628") (epsilon_r 4.6) (loss_tangent 0.02))
        (layer "B.Cu" (type "copper") (thickness 0.035))))
      (net 0 "")
      (gr_rect (start 5 5) (end 95 75) (stroke (width 0.05) (type default)) (fill none) (layer "Edge.Cuts")))""")
    names = sorted({n for p in parts for n in p["pins"].values() if n})
    nets = {n: i + 1 for i, n in enumerate(names)}
    for n, code in nets.items():
        pcb.append(parse_string(f'(net {code} "{n}")'))
    positions = {}
    for part in parts:
        lib, name = part["footprint"].split(":")
        fp = parse_file(footprint_root() / f"{lib}.pretty" / f"{name}.kicad_mod")
        fp.set_atom(0, part["footprint"])
        for child in list(fp.children):
            if child.name in ("version", "generator", "generator_version"):
                fp.remove(child)
        x, y = part["xy"]
        angle = part["angle"]
        fp.insert_after("layer", parse_string(f"(at {x} {y} {angle})"))
        fp.append(parse_string(f'(uuid "{uuid.uuid4()}")'))
        for prop in fp.find_children("property"):
            key = prop.get_string(0)
            if key in ("Reference", "Value"):
                prop.set_atom(1, part["ref"] if key == "Reference" else part["value"])
                prop.children[1]._originally_quoted = True
            if key == "Reference" and part["ref"].startswith("C"):
                at = prop.find_child("at")
                at.set_atom(0, 4 if x > 50 else -4)
                at.set_atom(1, 0)
            # Keep visible reference labels, stock value belongs on Fab.
        for item in fp.children:
            if (
                item.name in ("fp_line", "fp_rect", "fp_poly", "fp_arc", "fp_circle")
                and item.find_child("layer").get_string(0) == "F.SilkS"
            ):
                item.find_child("stroke").find_child("width").set_atom(0, 0.15)
        for item in fp.children:
            if item.name not in ("property", "fp_text"):
                continue
            at = item.find_child("at")
            if at and len(at.get_atoms()) > 2:
                at.set_atom(2, at.get_float(2) + angle)
        for pad in fp.find_children("pad"):
            pin = pad.get_string(0)
            if pin not in part["pins"]:
                raise ValueError(f"Unreviewed pad {part['ref']}.{pin}")
            at = pad.find_child("at")
            px, py = at.get_float(0), at.get_float(1)
            a = math.radians(angle)
            positions[(part["ref"], pin)] = (
                x + px * math.cos(a) + py * math.sin(a),
                y - px * math.sin(a) + py * math.cos(a),
            )
            atoms = at.get_atoms()
            fp_angle = (atoms[2] if len(atoms) > 2 else 0) + angle
            if len(atoms) > 2:
                at.set_atom(2, fp_angle)
            else:
                at.append(parse_string(str(fp_angle)))
            net = part["pins"][pin]
            if net:
                pad.append(parse_string(f'(net {nets[net]} "{net}")'))
        pcb.append(fp)

    def trace(net, points, width=0.26, layer="F.Cu"):
        for start, end in zip(points, points[1:], strict=False):
            if start == end:
                continue
            pcb.append(
                parse_string(
                    f'(segment (start {start[0]} {start[1]}) (end {end[0]} {end[1]}) (width {width}) (layer "{layer}") (net {nets[net]}) (uuid "{uuid.uuid4()}"))'
                )
            )

    # Symmetric, 100-ohm nominal channels. Identical conductor lengths,
    # 0.26mm width / 0.15mm gap over 0.2mm FR4 to the GND reference plane.
    for i in range(1, 5):
        y = 24 + 12 * (i - 1)
        middle = y - 1.27
        for suffix, dpin, rpin, tpin, sign in [("P", "7", "2", "1", 1), ("N", "8", "1", "2", -1)]:
            net = f"LVDS{i}_{suffix}"
            start = positions[(f"U{2 * i - 1}", dpin)]
            end = positions[(f"U{2 * i}", rpin)]
            termination = positions[(f"R{i}", tpin)]
            trace(
                net,
                [
                    start,
                    (36.6, start[1]),
                    (37.03, middle + sign * 0.205),
                    (62.18, middle + sign * 0.205),
                    (62.8, termination[1]),
                    termination,
                    (64, termination[1]),
                    (64.19, end[1]),
                    end,
                ],
            )
    for net, layer in [("GND", "In1.Cu"), ("+3V3", "In2.Cu")]:
        pcb.append(
            parse_string(f'''(zone (net {nets[net]}) (net_name "{net}") (layer "{layer}") (uuid "{uuid.uuid4()}")
          (hatch edge 0.5) (connect_pads yes (clearance 0.2)) (min_thickness 0.2)
          (fill yes (thermal_gap 0.3) (thermal_bridge_width 0.3))
          (polygon (pts (xy 5.5 5.5) (xy 94.5 5.5) (xy 94.5 74.5) (xy 5.5 74.5))))''')
        )
    # Tangential through vias: all power pins receive a real plane connection.
    for part in parts:
        if part["ref"].startswith("J"):
            continue
        for pin, net in part["pins"].items():
            if net not in ("+3V3", "GND"):
                continue
            x, y = positions[(part["ref"], pin)]
            dx = -1.2 if x < part["xy"][0] else 1.2
            via = (x + dx, y)
            trace(net, [(x, y), via], 0.3)
            pcb.append(
                parse_string(
                    f'(via (at {via[0]} {via[1]}) (size 0.6) (drill 0.3) (layers "F.Cu" "B.Cu") (net {nets[net]}) (uuid "{uuid.uuid4()}"))'
                )
            )
    path = out / "diffpair_test.kicad_pcb"
    path.write_text(pcb.to_string() + "\n")
    return path


def make_schematic(out, parts):
    blocks = []
    for part in parts:
        pins = []
        for i, pin in enumerate(part["pins"]):
            pins.append(
                PinDef(
                    number=pin,
                    name=part["names"].get(pin, pin),
                    pin_type=PinType(part["types"].get(pin, "passive")),
                    side=PinSide.LEFT if i < len(part["pins"]) / 2 else PinSide.RIGHT,
                )
            )
        symbol = SymbolDef(
            name=part["ref"], pins=pins, reference=part["ref"][0], footprint=part["footprint"]
        )
        doc = parse_string(generate_symbol_sexp(symbol))
        blocks.extend(s.to_string() for s in doc.find_children("symbol"))
    lib = out / "lvds_demo.kicad_sym"
    (out / "sym-lib-table").write_text(
        '(sym_lib_table (version 7) (lib (name "lvds_demo") (type "KiCad") (uri "${KIPRJMOD}/lvds_demo.kicad_sym") (options "") (descr "TI SLLS373M reviewed pinouts")))\n'
    )
    lib.write_text(
        '(kicad_symbol_lib (version 20231120) (generator "kicad-tools")\n'
        + "\n".join(blocks)
        + ")\n"
    )
    sch = Schematic(
        title="Four-channel LVDS link demonstrator",
        revision="C",
        date="2026-09-10",
        company="kicad-tools",
        local_symbol_libs=[lib],
        grid=1.27,
    )
    # Generous A2 sheet; every functional pin has a typed datasheet symbol.
    for index, part in enumerate(parts):
        x = 40.64 + 76.2 * (index % 6)
        y = 30.48 + 40.64 * (index // 6)
        sym = sch.add_symbol(
            "lvds_demo:" + part["ref"],
            x=x,
            y=y,
            ref=part["ref"],
            value=part["value"],
            footprint=part["footprint"],
        )
        for pin, net in part["pins"].items():
            pos = sym.pin_position(pin)
            if not net:
                sch.add_no_connect(*pos)
                continue
            side = -1 if pos[0] < sym.x else 1
            end = (pos[0] + side * 5.08, pos[1])
            sch.add_wire(pos, end, snap=False)
            sch.add_global_label(net, *end, rotation=0 if side < 0 else 180, snap=False)
    for net, x in [("+3V3", 25.4), ("GND", 55.88)]:
        sch.add_global_label(net, x, 17.78, snap=False)
        sch.add_pwr_flag(x, 17.78)
    path = out / "diffpair_test.kicad_sch"
    sch.write(path)
    doc = parse_file(path)
    doc.find_child("paper").set_atom(0, "A2")
    path.write_text(doc.to_string() + "\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    out = args.output.resolve()
    out.mkdir(parents=True, exist_ok=True)
    parts = components()
    make_schematic(out, parts)
    make_pcb(out, parts)
    for stem in ("diffpair_test", "diffpair_test_routed"):
        project = create_minimal_project(stem + ".kicad_pro")
        project["board"]["design_settings"]["rules"].update(
            min_copper_edge_clearance=0.3, min_clearance=0.15, min_track_width=0.15
        )
        project["net_settings"]["classes"][0]["clearance"] = 0.15
        save_project(project, out / (stem + ".kicad_pro"))
    rules = {}
    for prefix in ("IN", "OUT"):
        for i in range(1, 5):
            rules[f"{prefix}{i}"] = NetClassRouting(
                name="LVTTL",
                trace_width=0.2,
                clearance=0.15,
                via_size=0.6,
                avoid_layers=[1, 2],
            )
    for i in range(1, 5):
        for suffix, other in [("P", "N"), ("N", "P")]:
            rules[f"LVDS{i}_{suffix}"] = NetClassRouting(
                name="LVDS",
                trace_width=0.26,
                clearance=0.15,
                intra_pair_clearance=0.15,
                coupled_routing=True,
                target_diff_impedance=100,
                impedance_tolerance_percent=15,
                skew_tolerance_mm=0.1,
                coupled_continuity_threshold=0.8,
                diffpair_partner=f"LVDS{i}_{other}",
            )
    (out / "net_class_map.json").write_text(
        json.dumps(net_class_map_to_dict(rules), indent=2) + "\n"
    )
    (out / "circuit.json").write_text(
        json.dumps({"datasheet": DATASHEET, "components": parts}, indent=2) + "\n"
    )
    spec = {
        "kct_version": "1.0",
        "project": {
            "name": "Four-channel LVDS link demonstrator",
            "revision": "C",
            "description": "Real SN65LVDS1DR drivers and SN65LVDS2DR receivers, four terminated differential links. External regulated 3.3V and LVTTL stimulus required. No firmware.",
            "artifacts": {
                "pcb": "diffpair_test.kicad_pcb",
                "pcb_routed": "diffpair_test_routed.kicad_pcb",
                "schematic": "diffpair_test.kicad_sch",
            },
        },
        "requirements": {
            "manufacturing": {
                "target_fab": "jlcpcb-tier1",
                "layers": {"preferred": 4},
                "assembly": "smt",
            }
        },
        "bom_entries": [
            {
                "ref": p["ref"],
                "part": p["mpn"],
                **(
                    {"lcsc": p["lcsc"]}
                    if p["lcsc"]
                    else {"source": "https://www.samtec.com/products/tsw"}
                ),
            }
            for p in parts
        ],
    }
    (out / "project.kct").write_text(yaml.safe_dump(spec, sort_keys=False))
    print(out)


if __name__ == "__main__":
    main()
