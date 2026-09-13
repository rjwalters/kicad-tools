"""Apply the reviewed revision-B escape corrections, then require native DRC.

The seed-42 all-net route places two escape vias toward the adjacent pad,
shorting C1 and R5. These are explicit layout edits, not a DRC waiver. Their
connected segment endpoints move with them. A changed route still has to pass
native DRC; unmatched coordinates never silently certify a different board.
"""

import json
import shutil
import subprocess
import sys
import tempfile
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
    # Linux seed-42 routing chooses adjacent escape grid cells. The
    # observed variants need reviewed destinations. Net qualification
    # prevents an unrelated endpoint at the same coordinate from moving.
    net_names = {net.get_int(0): net.get_string(1) for net in pcb.find_children("net")}
    net_qualified_moves = {
        # Authoritative-grid seed-42 capacitor escapes (#5270). Native DRC,
        # process eligibility and copper LVS validate these off-pad positions.
        ("GND", (155.1, 117.9)): (154.6, 118.4),  # C1-2; clear LINE_C and RESET
        ("VCC", (129.9, 119.5)): (128.6, 119.5),  # C2-1; outside the SMT drill area
        # Exact seed-42 variants exposed by the repaired edge parser (#5257).
        ("GND", (155.1, 117.5)): (156.8, 117.5),
        ("VCC", (139.7, 117.3)): (139.2, 115.5),
        # Directed Steiner searches produce these seed-42 escapes.
        ("NODE_A", (148.0, 84.9)): (148.0, 84.5),
        ("NODE_D", (157.9, 106.0)): (158.3, 106.0),
        ("GND", (155.2, 117.4)): (156.5, 117.4),
        ("VCC", (140.665, 117.3)): (139.2, 115.5),
        ("VCC", (140.665, 117.2)): (139.2, 115.5),
        ("RESET", (141.1, 118.1)): (140.9, 118.5),
        # CI reports board-relative (17.8, 40.8); origin is (123.5, 77.5).
        ("RESET", (141.3, 118.3)): (140.9, 118.5),
        # The authoritative four-line outline (#4978) produces alternate
        # bottom-row LED escape cells. Move their attached endpoints with
        # the via, using the same off-pad x offsets as the upper rows.
        ("NODE_B", (148.1, 105.5)): (148.4, 105.5),  # D8-1
        ("NODE_B", (158.9, 105.5)): (158.6, 105.5),  # D9-2
        ("NODE_C", (138.1, 106.0)): (138.4, 106.0),  # D7-1
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
        destination = net_qualified_moves.get((net_name, old), moves.get(old))
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


def _native_report(path):
    report = path.with_suffix(".native-drc.json")
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--refill-zones",
            "--format",
            "json",
            "--units",
            "mm",
            "-o",
            str(report),
            str(path),
        ],
        check=True,
        timeout=120,
    )
    return json.loads(report.read_text())


def _lvs_report(schematic, path):
    result = subprocess.run(
        [sys.executable, "-m", "kicad_tools.lvs.copper_lvs", str(schematic), str(path)],
        capture_output=True,
        text=True,
        timeout=120,
    )
    data = json.loads(result.stdout)
    if result.returncode not in (0, 1):
        raise ValueError(f"Copper LVS failed: {result.stderr}")
    return data


def _geometry(pcb):
    from kicad_tools.core.board_outline import outline_graphics

    return (
        pcb.board_origin,
        pcb.board_size,
        tuple(serialize_sexp(n) for n in outline_graphics(pcb._sexp)),
        tuple(
            (
                fp.reference,
                fp.position,
                fp.rotation,
                fp.layer,
                tuple(
                    (
                        p.number,
                        p.position,
                        p.size,
                        p.rotation,
                        p.shape,
                        p.drill,
                        tuple(p.layers),
                        p.net_number,
                        p.net_name,
                    )
                    for p in fp.pads
                ),
            )
            for fp in pcb.footprints
        ),
    )


def _finalize_candidate(path, schematic):
    from native_via_repair import remove_reported_leaves, repair_report_vias

    from kicad_tools.schema.pcb import PCB
    from kicad_tools.validate.rules.via_in_pad import ViaInPadRule

    before = _geometry(PCB.load(path))
    baseline = _lvs_report(schematic, path)
    pcb = parse_file(path)
    _relocate_escapes(pcb)
    path.write_text(serialize_sexp(pcb))
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
        timeout=120,
    )
    data = _native_report(path)
    if data["violations"]:
        pcb = PCB.load(path)
        if repair_report_vias(path, pcb, data):
            pcb.save(path)
            data = _native_report(path)
            pcb = PCB.load(path)
            if remove_reported_leaves(pcb, data):
                pcb.save(path)
                data = _native_report(path)
    if data["violations"] or data["unconnected_items"] or data["schematic_parity"]:
        print(json.dumps(data, indent=2), file=sys.stderr)
        return False
    pcb = PCB.load(path)
    if ViaInPadRule().check(pcb, None).violations:
        raise ValueError("Board02 via-in-pad gate failed")
    if _geometry(pcb) != before:
        raise ValueError("Board02 pad or outline geometry changed")
    lvs = _lvs_report(schematic, path)
    if (
        not lvs["clean"]
        or lvs["bound_pad_count"] <= 0
        or lvs["bound_pad_count"] != baseline["bound_pad_count"]
    ):
        raise ValueError(f"Board02 copper LVS gate failed: {lvs}")
    return True


def finalize_routing(path: Path, schematic_path: Path | None = None) -> bool:
    """Commit only a fully qualified scratch result; failures retain original bytes."""
    path = Path(path).resolve()
    schematic = schematic_path or path.with_name(path.stem.removesuffix("_routed") + ".kicad_sch")
    try:
        with tempfile.TemporaryDirectory(prefix="board02-finalize-", dir=path.parent) as scratch:
            candidate = Path(scratch) / path.name
            shutil.copy2(path, candidate)
            for suffix in (".kicad_pro", ".kicad_dru"):
                sidecar = path.with_suffix(suffix)
                if sidecar.exists():
                    shutil.copy2(sidecar, candidate.with_suffix(suffix))
            if not _finalize_candidate(candidate, schematic):
                return False
            # The scratch directory is on the same filesystem; replacement is atomic.
            candidate.replace(path)
            return True
    except Exception as exc:
        print(f"Board02 finalization failed; original PCB retained: {exc}", file=sys.stderr)
        return False
