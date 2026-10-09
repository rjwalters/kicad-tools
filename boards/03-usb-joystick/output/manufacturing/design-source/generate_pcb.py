#!/usr/bin/env python3
"""Generate revision-B USB joystick using real library and manufacturer-drawn land patterns."""

import os
import sys
from pathlib import Path

from joystick_hardware import COMPONENTS, NETS

from kicad_tools.core.schematic_uuids import UuidSequence
from kicad_tools.pcb.center_sheet import centered_origin
from kicad_tools.pcb.footprints import FootprintLibrary
from kicad_tools.sexp import parse_file, parse_string, serialize_sexp

BOARD_WIDTH, BOARD_HEIGHT = 80.0, 60.0
BOARD_ORIGIN_X, BOARD_ORIGIN_Y = centered_origin(BOARD_WIDTH, BOARD_HEIGHT)


# Deterministic PCB item UUIDs (Issue #6076): the n-th call in a build returns
# the same uuid5 every run, so a fresh build is byte-reproducible.
_PCB_UUIDS = UuidSequence("boards/03-usb-joystick/usb_joystick.kicad_pcb")


def generate_uuid() -> str:
    """Next deterministic KiCad-format UUID (Issue #6076)."""
    return _PCB_UUIDS()


def stage_local_footprints(output_dir):
    """Keep manufacturer-specific land patterns portable with the PCB project."""
    import shutil

    shutil.copytree(
        Path(__file__).parent / "footprints", output_dir / "footprints", dirs_exist_ok=True
    )
    (output_dir / "fp-lib-table").write_text(
        '(fp_lib_table (version 7) (lib (name "Joystick") (type "KiCad") '
        '(uri "${KIPRJMOD}/footprints/Joystick.pretty") (options "") '
        '(descr "Manufacturer land patterns for USB joystick")))\n'
    )


def footprint_path(lib_id):
    library, name = lib_id.split(":")
    roots = [
        Path(__file__).parent / "footprints",
        os.environ.get("KICAD10_FOOTPRINT_DIR", ""),
        *FootprintLibrary.KICAD_PATHS,
    ]
    for root in roots:
        candidate = Path(root) / f"{library}.pretty" / f"{name}.kicad_mod"
        if candidate.is_file():
            return candidate
    raise FileNotFoundError(f"Required real KiCad footprint unavailable: {lib_id}")


def component_footprint(comp):
    node = parse_file(footprint_path(comp.footprint))
    node.set_atom(0, comp.footprint)
    node.children = [
        n
        for n in node.children
        if n.name not in {"version", "generator", "generator_version", "at"}
    ]
    x, y, angle = comp.position
    node.children.insert(1, parse_string(f"(at {x + BOARD_ORIGIN_X} {y + BOARD_ORIGIN_Y} {angle})"))
    node.add(parse_string(f'(uuid "{generate_uuid()}")'))
    for prop in node.find_children("property"):
        if prop.get_string(0) == "Reference":
            prop.set_atom(1, comp.ref)
        elif prop.get_string(0) == "Value":
            prop.set_atom(1, comp.value)
            prop.children[1]._originally_quoted = True
    for pad in node.find_children("pad"):
        number = pad.get_string(0)
        if number in comp.pins:
            net = comp.pins[number]
            pad.add(parse_string(f'(net {NETS[net]} "{net}")'))
        at = pad.find_child("at")
        if at and angle:
            at.set_value(2, ((at.get_float(2) or 0) + angle) % 360)
    for item in node.iter_all():
        if item.name == "uuid":
            item.set_atom(0, generate_uuid())
    return serialize_sexp(node)


def generate_power_pours():
    x1, y1 = BOARD_ORIGIN_X + 0.5, BOARD_ORIGIN_Y + 0.5
    x2, y2 = x1 + BOARD_WIDTH - 1, y1 + BOARD_HEIGHT - 1
    return "\n".join(
        f'''(zone (net {NETS[net]}) (net_name "{net}") (layer "{layer}")
    (uuid "{generate_uuid()}") (hatch edge 0.5)
    (connect_pads (clearance 0.2)) (min_thickness 0.2) (filled_areas_thickness no)
    (fill yes (thermal_gap 0.2) (thermal_bridge_width 0.25))
    (polygon (pts (xy {x1} {y1}) (xy {x2} {y1}) (xy {x2} {y2}) (xy {x1} {y2}))))'''
        for layer, net in [("F.Cu", "GND"), ("In1.Cu", "GND"), ("In2.Cu", "VCC"), ("B.Cu", "GND")]
    )


def generate_pcb():
    _PCB_UUIDS.reset()  # Issue #6076: same sequence every build
    header = """(kicad_pcb (version 20260206) (generator "kicad-tools")
    (general (thickness 1.6)) (paper "A4")
    (layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal)
      (34 "B.Paste" user) (35 "F.Paste" user) (36 "B.SilkS" user) (37 "F.SilkS" user)
      (38 "B.Mask" user) (39 "F.Mask" user) (44 "Edge.Cuts" user)
      (46 "B.CrtYd" user) (47 "F.CrtYd" user) (48 "B.Fab" user) (49 "F.Fab" user))
    (setup (pad_to_mask_clearance 0) (capping yes) (filling yes)
      (stackup
        (layer "F.Cu" (type "copper") (thickness 0.035))
        (layer "dielectric 1" (type "prepreg") (thickness 0.2104) (material "7628") (epsilon_r 4.6) (loss_tangent 0.02))
        (layer "In1.Cu" (type "copper") (thickness 0.0152))
        (layer "dielectric 2" (type "core") (thickness 1.065) (material "FR4") (epsilon_r 4.6) (loss_tangent 0.02))
        (layer "In2.Cu" (type "copper") (thickness 0.0152))
        (layer "dielectric 3" (type "prepreg") (thickness 0.2104) (material "7628") (epsilon_r 4.6) (loss_tangent 0.02))
        (layer "B.Cu" (type "copper") (thickness 0.035))))"""
    nets = "\n".join(f'(net {number} "{name}")' for name, number in NETS.items())
    outline = f'''(gr_rect (start {BOARD_ORIGIN_X} {BOARD_ORIGIN_Y})
      (end {BOARD_ORIGIN_X + BOARD_WIDTH} {BOARD_ORIGIN_Y + BOARD_HEIGHT})
      (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts")
      (uuid "{generate_uuid()}"))'''
    return "\n".join(
        [
            header,
            nets,
            outline,
            *[component_footprint(c) for c in COMPONENTS],
            generate_power_pours(),
            ")",
        ]
    )


def write_pcb(target, *, verbose=True):
    """Write the generated board to *target* and repair its footprint silk.

    Issue #5744: three of this board's library land patterns (``J3``'s
    pin-header socket, the ``R3``/``R4`` 0402 pads) draw silkscreen closer to
    their own pads than the reviewed ``jlcpcb-tier1`` floor
    (``min_silk_to_pad_clearance_mm`` = 0.15mm) allows.  Those 21
    ``silk_pad_clearance`` errors were latent only because
    ``routing_plan.apply_plan()`` used to delete every footprint silk graphic
    on replay -- the routed artifact carried no silk to measure.  The replay
    is fixed, so the silk really is there and has to really be manufacturable.

    The repair belongs HERE, not in ``generate_design.py:main()``: every
    consumer of this generator (the full recipe, a bare
    ``python generate_pcb.py <dir>``, ``route_demo.py``,
    ``tests/test_board_03_regression.py``'s regeneration fixture) must get the
    same repaired placement, because each of them then feeds it to
    ``apply_plan()``.  Raises if any violation survives.
    """
    target = Path(target)
    if target.suffix != ".kicad_pcb":
        target = target / "usb_joystick.kicad_pcb"
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(generate_pcb())

    # Board-local sibling module: import by path-insert so this works whatever
    # the caller's cwd is and whether or not the caller already did so.
    sys.path.insert(0, str(Path(__file__).parent))
    from silk_repair import repair_board_silk_pad_clearance

    repair_board_silk_pad_clearance(target, verbose=verbose)
    return target


if __name__ == "__main__":
    target = write_pcb(
        Path(sys.argv[1])
        if len(sys.argv) > 1
        else Path(__file__).parent / "output/usb_joystick.kicad_pcb"
    )
    stage_local_footprints(target.parent)
    # KiCad loads project-local libraries only when the matching project exists.
    from kicad_tools.core.project_file import create_minimal_project, save_project

    project = target.with_suffix(".kicad_pro")
    if not project.exists():
        save_project(create_minimal_project(project.name), project)
    print(target)
