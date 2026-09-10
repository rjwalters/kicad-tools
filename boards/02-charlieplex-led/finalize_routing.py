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

from kicad_tools.sexp import parse_file, serialize_sexp


def finalize_routing(path: Path) -> bool:
    pcb = parse_file(path)
    moves = {
        (154.385, 117.5): (156.8, 117.5),  # C1 GND escape, outside pad courtyard
        (140.665, 117.3): (139.2, 115.5),  # R5 VCC escape, above the resistor
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
        (141.1, 118.1): (140.9, 118.5),  # RESET
    }
    for item in pcb.find_all("segment") + pcb.find_all("via"):
        for key in ("at",) if item.name == "via" else ("start", "end"):
            point = item.find(key)
            old = tuple(round(point.get_float(i), 3) for i in range(2))
            if old in moves:
                for i, value in enumerate(moves[old]):
                    point.set_value(i, value)
    path.write_text(serialize_sexp(pcb))
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
    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

    if ViaInPadRule().check(PCB.load(path), None).violations:
        return False
    return not data["violations"] and not data["unconnected_items"] and not data["schematic_parity"]
