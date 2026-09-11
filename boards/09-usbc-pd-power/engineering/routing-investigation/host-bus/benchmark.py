#!/usr/bin/env python3
"""Measure the fixed four-net Board09 fixture without changing canonical artifacts."""

import argparse
import json
import platform
import shutil
import subprocess
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

from kicad_tools.schema.pcb import PCB
from kicad_tools.validate.connectivity import ConnectivityValidator

ROOT = Path(__file__).resolve().parent
OUTPUT = ROOT.parents[2] / "output"
TARGETS = ("SCL", "SDA", "MON_ALERT", "PD_ALERT")


def classify(pads, partition):
    """Require every expected terminal in one physical component for completion."""
    result = {}
    for name in TARGETS:
        expected = pads.get(name, set())
        sizes = [len(expected & group) for group in partition]
        largest = max(sizes, default=0)
        # A logical pad can occur on several disconnected physical islands.
        split_land = any(sum(pad in group for group in partition) > 1 for pad in expected)
        status = (
            "missing"
            if len(expected) < 2
            else (
                "complete"
                if largest == len(expected) and not split_land
                else "partial"
                if largest > 1
                else "unrouted"
            )
        )
        result[name] = {
            "status": status,
            "total_pads": len(expected),
            "largest_component_pads": largest,
        }
    return result


def prepare_case(directory, command_file):
    """Copy project and libraries; alter only paths in the committed command."""
    shutil.copytree(OUTPUT, directory)
    shutil.copy2(ROOT / "numbered-source.kicad_pcb", directory / "usbc_pd_power.kicad_pcb")
    shutil.copy2(directory / "usbc_pd_power.kicad_pro", directory / "support.kicad_pro")
    command = json.loads((ROOT / command_file).read_text())
    command[4] = str(directory / "usbc_pd_power.kicad_pcb")
    for flag in ("-o", "--net-class-map"):
        index = command.index(flag) + 1
        command[index] = str(directory / Path(command[index]).name)
    mapping = json.loads((directory / "net_class_map.json").read_text())
    if any(not {1, 2} <= set(mapping[name]["avoid_layers"]) for name in TARGETS):
        raise ValueError("Target net classes must avoid both inner layers")
    return command


def native_status(returncode, report):
    valid = isinstance(report, dict) and all(
        isinstance(report.get(key), list) for key in ("violations", "unconnected_items")
    )
    return {
        "status": "passed"
        if returncode == 0
        and valid
        and not report["violations"]
        and not report["unconnected_items"]
        else "failed",
        "returncode": returncode,
        "violations": len(report["violations"]) if valid else None,
        "opens": len(report["unconnected_items"]) if valid else None,
    }


def validate_native(pcb):
    executable = shutil.which("kicad-cli")
    if executable is None:
        return {"status": "unavailable", "reason": "kicad-cli not found"}
    report_path = pcb.with_suffix(".drc.json")
    report_path.unlink(missing_ok=True)
    command = [
        executable,
        "pcb",
        "drc",
        str(pcb),
        "--refill-zones",
        "--save-board",
        "--format",
        "json",
        "--output",
        str(report_path),
    ]
    try:
        process = subprocess.run(command, capture_output=True, text=True, timeout=180, check=False)
        pcb.with_suffix(".drc.log").write_text(process.stdout + process.stderr)
        report = json.loads(report_path.read_text()) if report_path.exists() else None
        return {**native_status(process.returncode, report), "command": command}
    except (OSError, ValueError, subprocess.TimeoutExpired) as error:
        return {"status": "failed", "reason": str(error), "command": command}


def target_pads(board):
    return {
        name: {
            f"{fp.reference}.{pad.number}"
            for fp in board.footprints
            for pad in fp.pads
            if pad.net_name == name
        }
        for name in TARGETS
    }


def copper(board):
    """Geometry multiset ignores numeric net IDs and UUIDs rewritten by native save."""
    segments = [("segment", s.start, s.end, s.width, s.layer, s.net_name) for s in board.segments]
    vias = [("via", v.position, v.size, v.drill, tuple(v.layers), v.net_name) for v in board.vias]
    # Filled polygons legitimately change during refill. Preserve zone boundaries,
    # clearances, layer restrictions and thermal settings instead.
    zones = []
    for zone in board.zones:
        fields = asdict(zone)
        for key in ("net_number", "uuid", "filled_polygons", "filled_polygon_layers", "is_filled"):
            fields.pop(key, None)
        zones.append(("zone", json.dumps(fields, sort_keys=True)))
    return Counter(segments + vias + zones)


def placement(board):
    return sorted(
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
                    tuple(sorted(p.layers)),
                    p.net_name,
                )
                for p in fp.pads
            ),
        )
        for fp in board.footprints
    )


def inspect_output(source, output):
    before, after = PCB.load(source), PCB.load(output)
    pads = target_pads(before)
    after_pads = target_pads(after)
    partition = ConnectivityValidator(after).extract_pad_partition()
    nets = classify(pads, partition)
    outer = all(s.layer in {"F.Cu", "B.Cu"} for s in after.segments if s.net_name in TARGETS)
    outer = outer and all(
        z.layer in {"F.Cu", "B.Cu"}
        for z in after.zones
        if z.net_name in TARGETS and z.keepout is None
    )
    outer = outer and all(
        set(v.layers) <= {"F.Cu", "B.Cu"} for v in after.vias if v.net_name in TARGETS
    )
    return {
        "nets": nets,
        "targets_preserved": pads == after_pads and all(len(pads[n]) >= 2 for n in TARGETS),
        "existing_copper_preserved": not (copper(before) - copper(after)),
        "placement_preserved": placement(before) == placement(after),
        "target_copper_outer_only": outer,
    }


def run_case(directory, command_file):
    command = prepare_case(directory, command_file)
    source, output = directory / "usbc_pd_power.kicad_pcb", directory / "support.kicad_pcb"
    output.unlink(missing_ok=True)
    started = time.perf_counter()
    result = {
        "command": command,
        "grid_mm": float(command[command.index("--grid") + 1]),
        "nets": {
            name: {"status": "unknown", "total_pads": None, "largest_component_pads": None}
            for name in TARGETS
        },
    }
    with (directory / "router.log").open("w") as log:
        try:
            process = subprocess.run(
                command, stdout=log, stderr=subprocess.STDOUT, timeout=300, check=False
            )
            result["router_returncode"] = process.returncode
        except (OSError, subprocess.TimeoutExpired) as error:
            result.update(router_returncode=None, error=str(error))
    result["elapsed_seconds"] = time.perf_counter() - started
    result["native_validation"] = (
        validate_native(output)
        if output.exists()
        else {"status": "unavailable", "reason": "router produced no PCB"}
    )
    if output.exists():
        try:
            result.update(inspect_output(source, output))
        except (OSError, ValueError) as error:
            result["inspection_error"] = str(error)
    result["success"] = (
        result["router_returncode"] == 0
        and result["native_validation"]["status"] == "passed"
        and all(
            result.get(key, False)
            for key in (
                "targets_preserved",
                "existing_copper_preserved",
                "placement_preserved",
                "target_copper_outer_only",
            )
        )
        and len(result.get("nets", {})) == 4
        and all(n["status"] == "complete" for n in result.get("nets", {}).values())
    )
    (directory / "result.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--results",
        type=Path,
        required=True,
        help="New directory for retained scratch boards, logs and JSON",
    )
    args = parser.parse_args()
    directory = args.results.resolve()
    directory.mkdir(parents=True, exist_ok=False)
    results = {
        "environment": {"platform": platform.platform(), "python": platform.python_version()},
        "cases": [],
    }
    if shutil.which("kicad-cli"):
        results["environment"]["kicad"] = subprocess.run(
            ["kicad-cli", "--version"], capture_output=True, text=True, check=False
        ).stdout.strip()
    for command_file in ("numbered-command.json", "fine-command.json"):
        result = run_case(directory / command_file.removesuffix(".json"), command_file)
        results["cases"].append(result)
        (directory / "results.json").write_text(json.dumps(results, indent=2) + "\n")
        print(
            f"grid={result['grid_mm']} elapsed={result['elapsed_seconds']:.1f}s success={result['success']}",
            flush=True,
        )
    return 0  # A measured routing failure is a benchmark result, not a harness crash.


if __name__ == "__main__":
    raise SystemExit(main())
