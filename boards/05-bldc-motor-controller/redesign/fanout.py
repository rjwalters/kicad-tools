"""Native-pad escapes for revision B; no DRC exceptions or synthetic pins."""

from pathlib import Path

from kicad_tools.schema.pcb import PCB


def fanout(path):
    pcb = PCB.load(path)
    power = {"VM", "PHASE_A", "PHASE_B", "PHASE_C", "GND_SENSE"}
    count = 0
    for ref in ["U1", "U2", "U3"]:
        fp = next(f for f in pcb.footprints if f.reference == ref)
        for pad in fp.pads:
            if not pad.number or not pad.net_name or pad.type != "smd":
                continue
            number = int(pad.number)
            if (ref == "U2" and number == 29) or (ref == "U3" and number == 9):
                continue
            x, y = pad.position
            ax, ay = fp.position[0] + x, fp.position[1] + y
            offset = 1.2 if ref == "U1" or number % 2 else 2.0
            if abs(x) > abs(y) or ref in ["U2", "U3"]:
                via = (ax + offset * (1 if x > 0 else -1), ay)
            else:
                via = (ax, ay + offset * (1 if y > 0 else -1))
            pcb.add_trace(
                (ref, pad.number),
                via,
                width=0.4 if pad.net_name in power else 0.16,
                net=pad.net_name,
            )
            pcb.add_via(*via, size=0.6, drill=0.3, net=pad.net_name)
            count += 1
    pcb.save(path)
    return count


if __name__ == "__main__":
    import sys

    print("Escaped", fanout(Path(sys.argv[1])), "IC pads")
