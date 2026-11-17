"""PCB.save must not write a numbered net table into a name-only board (#8239)."""

import re

from kicad_tools.schema.pcb import PCB

NAME_ONLY = (
    '(kicad_pcb (version 20260101) (generator "pcbnew")\n'
    "  (general (thickness 1.6))\n"
    '  (layers (0 "F.Cu" signal) (2 "B.Cu" signal))\n'
    '  (footprint "R" (layer "F.Cu") (at 0 0)\n'
    '    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net "GND")))\n'
    '  (segment (start 0 0) (end 1 0) (width 0.2) (layer "F.Cu") (net "GND"))\n'
    ")\n"
)
NUMBERED = NAME_ONLY.replace(
    "  (footprint", '  (net 0 "")\n  (net 1 "GND")\n  (footprint', 1
).replace('(net "GND")', '(net 1 "GND")')
HEADER = re.compile(r'^\t\(net\s+\d+\s+"[^"]*"\)\s*$', re.MULTILINE)


def test_name_only_board_saves_without_numbered_header(tmp_path):
    src = tmp_path / "in.kicad_pcb"
    src.write_text(NAME_ONLY)
    pcb = PCB.load(src)
    assert any(n.name == "GND" for n in pcb.nets.values())
    out = tmp_path / "out.kicad_pcb"
    pcb.save(out)
    assert not HEADER.search(out.read_text())
    assert any(n.name == "GND" for n in pcb.nets.values())


def test_numbered_board_keeps_header(tmp_path):
    src = tmp_path / "in.kicad_pcb"
    src.write_text(NUMBERED)
    out = tmp_path / "out.kicad_pcb"
    PCB.load(src).save(out)
    assert len(HEADER.findall(out.read_text())) == 2
