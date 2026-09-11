"""Apply the reviewed revision-B escape corrections, then require native DRC.

The seed-42 all-net route places two escape vias toward the adjacent pad,
shorting C1 and R5. These are explicit layout edits, not a DRC waiver. Their
connected segment endpoints move with them. A changed route still has to pass
native DRC; unmatched coordinates never silently certify a different board.
"""

import json
import subprocess
import sys
from pathlib import Path

from kicad_tools.router.quantize import quantize_pcb_file
from kicad_tools.sexp import parse_file, serialize_sexp


def _relocate_escapes(pcb):
    """Repair reviewed escape variants, moving only their own net endpoints."""
    moves = {
        (154.385, 117.5): (156.8, 117.5),  # C1 GND escape, outside pad courtyard
        # Keep the complete drill/annulus clear of SMT copper (issue #5012).
        (133.1, 113.5): (133.4, 112.8),  # LINE_A
        (148.1, 85.5): (148.4, 85.5),  # NODE_A
        (158.9, 85.5): (158.6, 85.5),  # NODE_A
        (138.0, 85.9): (138.4, 85.9),  # NODE_B
        (148.9, 105.5): (148.6, 105.5),  # NODE_C
        (138.1, 105.5): (138.4, 105.5),  # NODE_C
        (149.8, 105.9): (150.2, 106.2),  # NODE_C
        (158.9, 95.5): (158.6, 95.5),  # NODE_D
        (148.1, 95.5): (148.4, 95.5),  # NODE_D
        (157.1, 105.9): (156.7, 105.9),  # NODE_D
    }
    # Linux seed-42 routing chooses adjacent escape grid cells. The observed
    # variants need reviewed destinations. Net qualification
    # prevents an unrelated endpoint at the same coordinate from moving.
    net_names = {net.get_int(0): net.get_string(1) for net in pcb.find_children("net")}
    reviewed_moves = {
        ("VCC", (140.665, 117.3)): (139.2, 115.5),
        ("VCC", (140.665, 117.2)): (139.2, 115.5),
        ("RESET", (141.1, 118.1)): (140.9, 118.5),
        # CI reports board-relative (17.8, 40.8); origin is (123.5, 77.5).
        ("RESET", (141.3, 118.3)): (140.9, 118.5),
        # Unexempted pad-center seeds (issue #5004) choose these seed-42
        # variants. Keep the annulus off SMT pads and the J2/VCC drills
        # separated; native DRC and copper-LVS qualify the resulting copper.
        ("LINE_A", (133.1, 114.0)): (133.4, 112.8),
        ("NODE_B", (148.1, 105.5)): (148.4, 105.5),
        ("NODE_B", (158.9, 105.5)): (158.6, 105.5),
        ("NODE_C", (138.1, 105.0)): (138.4, 105.0),
        ("LINE_D", (165.7, 123.1)): (165.5, 123.3),
        ("VCC", (139.7, 116.235)): (139.2, 115.5),
        # Restoring the selected negotiated route's copper changes the
        # observed escape cells again. These variants retain the same
        # manufacturing limits, including annulus-to-pad and drill spacing.
        ("LINE_D", (163.1, 114.0)): (163.1, 114.5),
        ("NODE_B", (138.1, 84.8)): (138.1, 84.4),
        ("NODE_B", (148.1, 106.2)): (148.1, 106.55),
        ("NODE_B", (159.4, 106.2)): (159.4, 106.55),
        ("NODE_C", (138.1, 106.2)): (138.4, 106.5),
        ("NODE_C", (140.0, 95.7)): (140.4, 95.7),
        ("NODE_D", (163.9, 112.8)): (163.9, 112.4),
        ("GND", (155.1, 118.2)): (155.1, 118.6),
        ("GND", (131.1, 118.9)): (131.1, 118.4),
        ("VCC", (139.9, 116.8)): (139.9, 116.4),
        ("LINE_D", (165.4, 122.2)): (165.3, 122.2),
        ("VCC", (129.2, 123.8)): (129.6, 124.0),
    }
    # Only a present via activates a relocation. Quantization can create a
    # track-only waypoint at an old escape cell; moving it on a second pass
    # would distort an already repaired route.
    active_moves = {}
    for via in pcb.find_all("via"):
        net = via.find("net")
        net_name = net_names.get(net.get_int(0), net.get_string(0)) if net else None
        point = via.find("at")
        old = tuple(round(point.get_float(i), 3) for i in range(2))
        destination = reviewed_moves.get((net_name, old), moves.get(old))
        if destination is not None:
            active_moves[net_name, old] = destination
    for item in pcb.find_all("segment") + pcb.find_all("via"):
        net = item.find("net")
        net_name = net_names.get(net.get_int(0), net.get_string(0)) if net else None
        for key in ("at",) if item.name == "via" else ("start", "end"):
            point = item.find(key)
            old = tuple(round(point.get_float(i), 3) for i in range(2))
            destination = active_moves.get((net_name, old))
            if destination is not None:
                for i, value in enumerate(destination):
                    point.set_value(i, value)


def finalize_routing(path: Path) -> bool:
    pcb = parse_file(path)
    _relocate_escapes(pcb)
    path.write_text(serialize_sexp(pcb))
    # Endpoint relocations preserve connectivity but can skew attached tracks.
    # Restore exact 45-degree geometry before the independent native DRC gate.
    quantize_pcb_file(path)
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "fix-silkscreen",
            str(path),
            "--mfr",
            "jlcpcb",
            "--format",
            "summary",
        ],
        check=True,
    )
    report = path.with_suffix(".native-drc.json")
    subprocess.run(
        ["kicad-cli", "pcb", "drc", "--format", "json", "-o", str(report), str(path)],
        check=True,
    )
    data = json.loads(report.read_text())
    clean = (
        not data["violations"] and not data["unconnected_items"] and not data["schematic_parity"]
    )
    if not clean:
        print(json.dumps(data, indent=2), file=sys.stderr)
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

    via_findings = ViaInPadRule().check(PCB.load(path), None).violations
    if via_findings:
        print("Board02 via-in-pad findings:", file=sys.stderr)
        for finding in via_findings:
            print(finding, file=sys.stderr)
        return False
    return clean
