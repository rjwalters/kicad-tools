#!/usr/bin/env python3
"""Rebuild board 07's MIPI copper while preserving the other routed nets.

Usage: uv run python boards/07-matchgroup-test/repair_mipi.py /tmp/board07-repair

The board remains partial: DDR and HDMI still have known opens and pair
quality failures. This entry point checks MIPI against the full authored
pair/group constraints and returns the overall check's nonzero status.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import generate_design

from kicad_tools.router.diffpair_detection import detect_diff_pairs
from kicad_tools.router.diffpair_length_tuning import tune_diff_pair_skew
from kicad_tools.router.match_group_length import MatchGroup
from kicad_tools.router.match_group_tuning import tune_match_group_v2
from kicad_tools.router.optimizer.pcb import (
    parse_net_names,
    parse_segments,
    parse_vias,
    replace_segments,
)
from kicad_tools.router.primitives import Route
from kicad_tools.schema.pcb import PCB
from kicad_tools.sexp import SExp, parse_string, serialize_sexp

BOARD_DIR = Path(__file__).resolve().parent
MIPI_NETS = tuple(
    name
    for name in generate_design.build_net_class_map(preserve_authored_gap=True)
    if name.startswith("MIPI_")
)


def logical_netlist(board: PCB) -> list[tuple[str, str, str]]:
    return sorted(
        (fp.reference, pad.number, pad.net_name) for fp in board.footprints for pad in fp.pads
    )


def restore_numeric_net_references(path: Path) -> None:
    """Retain legacy consumer compatibility after KiCad 10's name-only save.

    This changes only net-reference syntax. Pad mappings and all geometry
    are checked independently after the conversion.
    """
    tree = parse_string(path.read_text())
    refs = tree.find_all("net")
    if tree.find_children("net"):
        return
    names = sorted({ref.children[0].value for ref in refs if ref.children})
    ids = {name: index + 1 for index, name in enumerate(names)}

    def convert(parent: SExp) -> None:
        for node in parent.children:
            if node.name == "net" and node.children:
                name = node.children[0].value
                node.children = [SExp(value=ids[name])]
                if parent.name == "pad":
                    node.add(name)
                elif parent.name == "zone":
                    parent.add(SExp("net_name").add(name))
            else:
                convert(node)

    convert(tree)
    # KiCad resolves numeric references while reading, so declarations must
    # precede footprints and copper rather than being appended at EOF.
    first_geometry = next(
        index
        for index, node in enumerate(tree.children)
        if node.name in {"footprint", "segment", "via", "zone"}
    )
    tree.children[first_geometry:first_geometry] = [
        SExp("net").add(number).add(name) for name, number in ids.items()
    ]
    path.write_text(serialize_sexp(tree))


def tune_mipi(pcb_text: str) -> tuple[str, dict]:
    """Tune the six routes to a common target, retaining partner clearances.

    The symmetric group splice assumes identical P/N host segments, which
    these escapes do not have. Use the existing scalar length optimizer for
    the final common target; the published sidecar still declares all three
    pairs and their group, and the final independent checks remain mandatory.
    """
    segments, vias, names = (
        parse_segments(pcb_text),
        parse_vias(pcb_text),
        parse_net_names(pcb_text),
    )
    routes = {
        segs[0].net: Route(net=segs[0].net, net_name=name, segments=segs, vias=vias.get(name, []))
        for name, segs in segments.items()
    }
    mipis = {net: name for net, name in names.items() if name in MIPI_NETS}
    if len(mipis) != 6 or any(net not in routes for net in mipis):
        raise ValueError("All six MIPI nets must have routed copper before tuning")
    classes = generate_design.build_net_class_map(preserve_authored_gap=True)
    partners = {}
    results = {}
    for pair in detect_diff_pairs(mipis):
        p_id, n_id = pair.pair.positive.net_id, pair.pair.negative.net_id
        cls = classes[names[p_id]]
        p_route, n_route, result = tune_diff_pair_skew(
            pair,
            routes,
            tolerance_mm=cls.skew_tolerance_mm,
            intra_pair_clearance_mm=cls.intra_pair_clearance,
        )
        routes[p_id], routes[n_id] = p_route, n_route
        partners[p_id], partners[n_id] = n_id, p_id
        results[pair.pair.name] = {
            "reason": result.reason,
            "skew_before_mm": result.skew_before_mm,
            "skew_after_mm": result.skew_after_mm,
        }
    cls = classes[MIPI_NETS[0]]
    tuned = tune_match_group_v2(
        MatchGroup(
            name=cls.length_match_group,
            net_ids=list(mipis),
            tolerance=cls.length_match_tolerance_mm,
        ),
        routes,
        tolerance_mm=cls.length_match_tolerance_mm,
        intra_group_clearance_mm=0.15,
        intra_pair_clearance_mm=cls.intra_pair_clearance,
        diff_pair_partners=partners,
    )
    results["common_target"] = {
        names[net]: {"reason": result.reason, "length_after_mm": result.length_after_mm}
        for net, (_route, result) in tuned.items()
    }
    changed = {names[net]: route.segments for net, (route, _result) in tuned.items()}
    return replace_segments(pcb_text, segments, changed), results


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--input", type=Path, default=BOARD_DIR / "output/matchgroup_test_routed.kicad_pcb"
    )
    args = parser.parse_args()
    output = args.output_dir.resolve()
    output.mkdir(parents=True, exist_ok=True)
    board = PCB.load(args.input)
    original_netlist = logical_netlist(board)
    board.strip_traces(nets=list(MIPI_NETS))
    connector = board.get_footprint("J1")
    if connector is None:
        raise ValueError("Board 07 must contain MIPI connector J1")
    delta = -90.0 - connector.rotation
    connector.rotation = -90.0
    for pad in connector.pads:
        pad.rotation += delta
    route_input = output / "mipi-input.kicad_pcb"
    board.save(route_input)
    sidecar = generate_design.write_sidecar(
        generate_design.build_net_class_map(preserve_authored_gap=True), output
    )
    routed = output / "matchgroup_test_routed.kicad_pcb"
    cli = [sys.executable, "-m", "kicad_tools.cli"]
    command = cli + [
        "route",
        str(route_input),
        "--output",
        str(routed),
        "--nets",
        ",".join(MIPI_NETS),
        "--preserve-existing",
        "--no-cache",
        "--manufacturer",
        "jlcpcb",
        "--strategy",
        "negotiated",
        "--no-auto-layers",
        "--layers",
        "4",
        "--seed",
        "42",
        "--timeout",
        "360",
        "--deterministic-budget",
        "--net-class-map",
        str(sidecar),
        "--differential-pairs",
        "--diffpair-per-pair-timeout",
        "120",
    ]
    with (output / "route.log").open("w") as log:
        run = subprocess.run(
            command,
            stdout=log,
            stderr=subprocess.STDOUT,
            env={**os.environ, "PYTHONHASHSEED": "42"},
            timeout=720,
        )
    if run.returncode not in (0, 2, 3) or not routed.exists():
        raise RuntimeError(f"Routing failed ({run.returncode}); see {output / 'route.log'}")
    text, tuning = tune_mipi(routed.read_text())
    routed.write_text(text)
    (output / "mipi-tuning.json").write_text(json.dumps(tuning, indent=2))
    # The route CLI emits its own project/rules. Restore the input board's
    # original ones before comparing native DRC, exactly as in the A/B.
    for suffix in (".kicad_pro", ".kicad_dru"):
        source = args.input.with_suffix(suffix)
        if not source.exists():
            source = (BOARD_DIR / "output/matchgroup_test_routed.kicad_pcb").with_suffix(suffix)
        if source.resolve() != routed.with_suffix(suffix):
            shutil.copy2(source, routed.with_suffix(suffix))
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
            str(output / "native-drc.json"),
            str(routed),
        ],
        check=True,
        timeout=120,
    )
    restore_numeric_net_references(routed)
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            "--format",
            "json",
            "-o",
            str(output / "native-final.json"),
            str(routed),
        ],
        check=True,
        timeout=120,
    )
    if logical_netlist(PCB.load(routed)) != original_netlist:
        raise RuntimeError("Repair changed the logical pad/net mapping")
    native = json.loads((output / "native-final.json").read_text())
    if any(v["severity"] == "error" for v in native["violations"]):
        raise RuntimeError("Native DRC found errors; do not promote this repair")
    check = subprocess.run(
        cli
        + [
            "check",
            str(routed),
            "--mfr",
            "jlcpcb",
            "--net-class-map",
            str(sidecar),
            "--format",
            "json",
            "--output",
            str(output / "check.json"),
        ],
        stdout=subprocess.DEVNULL,
        timeout=120,
    )
    report = json.loads((output / "check.json").read_text())
    if any(v["severity"] == "error" and "MIPI" in v["message"] for v in report["violations"]):
        raise RuntimeError("MIPI still fails authored constraints; do not promote this repair")
    opens = {
        net
        for violation in report["violations"]
        if violation["rule_id"] == "connectivity" and violation["severity"] == "error"
        for net in violation["nets"]
    }
    if opens != {"DQ3", "DQ4", "TMDS_D0_N", "TMDS_D1_N"}:
        raise RuntimeError(f"Unexpected remaining open nets: {sorted(opens)}")
    print(f"MIPI checks pass; overall board errors: {report['summary']['errors']}")
    print(f"Verified candidate: {routed}")
    return check.returncode  # Honest overall partial/failed status.


if __name__ == "__main__":
    raise SystemExit(main())
