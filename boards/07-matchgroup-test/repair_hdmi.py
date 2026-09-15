#!/usr/bin/env python3
"""Retarget the historical HDMI widths, measuring contact preservation (#5126).

Writes a byte-minimal candidate and separate native-refilled evidence copies.
Success means the existing defective witness is preserved, not that this board
is manufacturing-ready. No routing, tuning, fixture promotion or hash-pin edits.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any

from kicad_tools.sexp import parse_string
from kicad_tools.validate.connectivity import ConnectivityValidator

BOARD_DIR = Path(__file__).resolve().parent
TMDS_NETS = {f"TMDS_D{i}_{polarity}" for i in range(3) for polarity in ("P", "N")}
KNOWN_OPENS = {"DQ3", "DQ4", "TMDS_D0_N", "TMDS_D1_N"}

# Runs in KiCad's own Python (including the macOS Python 3.9 bundle).
NATIVE_PARTITIONS = r"""
import hashlib, json, pathlib, sys, pcbnew
path = pathlib.Path(sys.argv[1])
before = hashlib.sha256(path.read_bytes()).hexdigest()
version = pcbnew.GetBuildVersion()
if not version.startswith("10."):
    raise RuntimeError("KiCad 10 pcbnew required: " + version)
board = pcbnew.LoadBoard(str(path))
if board is None:
    raise RuntimeError("Native board load failed")
board.BuildConnectivity()
connection = board.GetConnectivity()
pads = {str(p.m_Uuid.AsString()): p for f in board.GetFootprints()
        for p in f.Pads() if f.GetReference() and not f.GetReference().startswith("#")
        and p.GetNumber()}
if not pads:
    raise RuntimeError("Native pad census is empty")
names = {u: p.GetParentFootprint().GetReference() + "." + p.GetNumber()
         for u, p in pads.items()}
if len(set(names.values())) != len(names):
    raise RuntimeError("Duplicate logical pad identity requires an occurrence-aware audit")
groups, seen = [], set()
for uuid, pad in pads.items():
    if uuid in seen:
        continue
    group = ({uuid} | {str(p.m_Uuid.AsString())
                      for p in connection.GetConnectedItems(pad, 0)}) & pads.keys()
    seen.update(group)
    groups.append(sorted(group))
if before != hashlib.sha256(path.read_bytes()).hexdigest():
    raise RuntimeError("Native connectivity modified the input")
print(json.dumps({"version": version, "sha256": before, "pad_names": names,
                  "components": sorted(groups)}))
"""


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def retarget_widths(text: str, sidecar: dict[str, Any]) -> tuple[str, dict[str, int]]:
    """Use parser positions to replace only the chosen width atoms, not a reserialization."""
    if {name for name in sidecar if name.startswith("TMDS_")} != TMDS_NETS:
        raise ValueError("Sidecar must identify exactly the six reviewed TMDS nets")
    for name in TMDS_NETS:
        rule = sidecar[name]
        if rule.get("name") != "HDMI_TMDS_LANES" or rule.get("trace_width") != 0.225:
            raise ValueError(f"Unexpected authored HDMI width/class for {name}")
    tree = parse_string(text, track_positions=True)
    if tree.name != "kicad_pcb":
        raise ValueError("Expected a KiCad PCB")
    nets = {node.get_int(0): node.get_string(1) for node in tree.find_children("net")}
    starts = [0]
    starts.extend(match.end() for match in re.finditer("\n", text))
    edits = []
    routed = set()
    counts = dict.fromkeys(sorted(TMDS_NETS), 0)
    for segment in tree.find_children("segment") + tree.find_children("arc"):
        net_nodes = segment.find_children("net")
        if len(net_nodes) != 1 or len(net_nodes[0].children) != 1:
            raise ValueError("Missing or ambiguous track net")
        atom = net_nodes[0].children[0].value
        name = nets.get(atom, "") if isinstance(atom, int) else str(atom)
        if not name:
            raise ValueError(f"Unresolved track net {atom!r}")
        if name not in TMDS_NETS:
            if name.startswith("TMDS_"):
                raise ValueError(f"Unreviewed TMDS net {name}")
            continue
        routed.add(name)
        if segment.name != "segment":
            raise ValueError("Historical TMDS arc is outside the reviewed straight-track scope")
        widths = segment.find_children("width")
        if len(widths) != 1 or len(widths[0].children) != 1:
            raise ValueError("Missing or ambiguous TMDS width")
        width = widths[0]
        if width.get_float(0) not in (0.375, 0.225):
            raise ValueError(f"Unexpected existing width on {name}")
        if width.get_float(0) == 0.225:
            continue
        offset = starts[width.line - 1] + width.column - 1
        match = re.match(r"\(width\s+([^\s()]+)\s*\)", text[offset:])
        if match is None or float(match[1]) != 0.375:
            raise ValueError("Width token does not match parser position")
        edits.append((offset + match.start(1), offset + match.end(1), "0.225"))
        counts[name] += 1
    if not routed:
        raise ValueError("No routed TMDS tracks found")
    for start, end, replacement in sorted(edits, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text, counts


def analyzer_components(path: Path) -> list[list[str]]:
    from kicad_tools._shapely import has_shapely

    if not has_shapely():
        raise RuntimeError("Shapely required for physical fill connectivity")
    groups = sorted(sorted(group) for group in ConnectivityValidator(path).extract_pad_partition())
    if not groups:
        raise RuntimeError("Analyzer pad census is empty")
    return groups


def require_same_components(before: Any, after: Any, label: str) -> None:
    if before != after:
        raise RuntimeError(f"{label}: physical pad components changed; do not promote")


def native_signatures(report: dict[str, Any]) -> dict[str, Any]:
    """Retain multiplicity and item UUID identities, not only error/open counts."""
    if not str(report.get("kicad_version", "")).startswith("10."):
        raise RuntimeError("Native DRC report is missing a KiCad 10 identity")

    def rows(key: str, errors_only: bool) -> list[Any]:
        values = report.get(key)
        if not isinstance(values, list):
            raise RuntimeError(f"Native DRC report lacks {key}")
        result = []
        for value in values:
            if value.get("severity") not in {"error", "warning", "info"}:
                raise RuntimeError("Native violation is missing severity")
            if errors_only and value["severity"] != "error":
                continue
            items = value.get("items")
            if not isinstance(items, list) or any(not item.get("uuid") for item in items):
                raise RuntimeError("Native violation is missing item identities")
            if not value.get("type"):
                raise RuntimeError("Native violation is missing its rule type")
            result.append([value["type"], sorted(item["uuid"] for item in items)])
        return sorted(result)

    return {"errors": rows("violations", True), "opens": rows("unconnected_items", False)}


def check_signatures(report: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(report.get("violations"), list) or not isinstance(
        report.get("summary"), dict
    ):
        raise RuntimeError("Missing kct check report fields")
    errors = []
    opens = set()
    for value in report["violations"]:
        if value.get("severity") == "error":
            if not value.get("rule_id") or not isinstance(value.get("nets"), list):
                raise RuntimeError("Malformed kct error signature")
            errors.append([value["rule_id"], sorted(value["nets"])])
            if value["rule_id"] == "connectivity":
                opens.update(value["nets"])
    if report["summary"].get("errors") != len(errors):
        raise RuntimeError("kct error count does not match its report")
    if opens != KNOWN_OPENS:
        raise RuntimeError(f"Unexpected historical open nets: {sorted(opens)}")
    return {"errors": sorted(errors), "open_nets": sorted(opens)}


def run_command(command: list[str], log: Path, allowed: tuple[int, ...] = (0,)) -> int:
    """Always retain command/output; never use an old report after a command failure."""
    with log.open("w") as stream:
        stream.write(json.dumps(command) + "\n")
        stream.flush()
        result = subprocess.run(command, stdout=stream, stderr=subprocess.STDOUT, timeout=180)
    if result.returncode not in allowed:
        raise RuntimeError(f"Command failed ({result.returncode}); see {log}")
    return result.returncode


def read_report(path: Path) -> dict[str, Any]:
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise RuntimeError(f"Expected a JSON object: {path}")
    return data


def native_components(path: Path, python: str, output: Path) -> dict[str, Any]:
    command = [python, "-c", NATIVE_PARTITIONS, str(path)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=180)
    output.with_suffix(".log").write_text(json.dumps(command) + "\n" + result.stderr)
    if result.returncode:
        raise RuntimeError(f"Native partition failed: {output.with_suffix('.log')}")
    data = json.loads(result.stdout)
    if not isinstance(data, dict) or data.get("sha256") != sha256(path):
        raise RuntimeError("Native component result does not bind the current bytes")
    output.write_text(json.dumps(data, indent=2) + "\n")
    return data


def validate_pair(before: Path, after: Path, cli: str, native_python: str) -> dict[str, Any]:
    evidence: dict[str, Any] = {}
    for label, path in (("before", before), ("after", after)):
        digest = sha256(path)
        native_report = path.parent / "native-drc.json"
        run_command(
            [cli, "pcb", "drc", "--format", "json", "-o", str(native_report), str(path)],
            path.parent / "native-drc.log",
        )
        native = native_components(path, native_python, path.parent / "native-components.json")
        kct_report = path.parent / "check.json"
        check_exit = run_command(
            [
                sys.executable,
                "-m",
                "kicad_tools.cli",
                "check",
                str(path),
                "--mfr",
                "jlcpcb",
                "--net-class-map",
                str(path.parent / "net_class_map.json"),
                "--format",
                "json",
                "--output",
                str(kct_report),
            ],
            path.parent / "check.log",
            (0, 2),
        )
        check = read_report(kct_report)
        signatures = check_signatures(check)
        if check_exit == 0 and signatures["errors"]:
            raise RuntimeError("kct exit status contradicts its error report")
        evidence[label] = {
            "sha256": digest,
            "analyzer_components": analyzer_components(path),
            "native_components": native["components"],
            "native_pad_names": native["pad_names"],
            "native_version": native["version"],
            "native_signatures": native_signatures(read_report(native_report)),
            "check_signatures": signatures,
        }
        if sha256(path) != digest:
            raise RuntimeError("Read-only checks modified the PCB")
    for key in evidence["before"]:
        if key != "sha256":
            require_same_components(evidence["before"][key], evidence["after"][key], key)
    return evidence


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output_dir", type=Path)
    parser.add_argument(
        "--input",
        type=Path,
        default=BOARD_DIR / "regression-fixture/matchgroup_test_routed.kicad_pcb",
    )
    parser.add_argument("--kicad-cli", default="kicad-cli")
    parser.add_argument(
        "--native-python",
        default="python3",
        help="Python interpreter with KiCad 10 pcbnew installed",
    )
    args = parser.parse_args()
    source = args.input.resolve()
    output = args.output_dir.resolve()
    if output.exists():
        raise ValueError("Use a new output directory; prior evidence must not be overwritten")
    sidecars = [source.with_suffix(suffix) for suffix in (".kicad_pro", ".kicad_dru")]
    sidecars.append(source.parent / "net_class_map.json")
    if any(not path.is_file() for path in [source, *sidecars]):
        raise ValueError("Input PCB, original project/rules and net_class_map.json are required")
    original_hashes = {str(path): sha256(path) for path in [source, *sidecars]}
    original = source.read_bytes()
    text, counts = retarget_widths(original.decode(), read_report(sidecars[-1]))
    output.mkdir(parents=True)
    run_command([args.kicad_cli, "version"], output / "native-version.log")
    if not (output / "native-version.log").read_text().splitlines()[-1].startswith("10."):
        raise RuntimeError("KiCad 10 CLI required")
    boards = {}
    for label in ("before", "after", "before-refilled", "after-refilled"):
        folder = output / label
        folder.mkdir()
        path = folder / source.name
        path.write_bytes(original if label.startswith("before") else text.encode())
        for sidecar in sidecars:
            shutil.copy2(sidecar, folder / sidecar.name)
        boards[label] = path
    evidence: dict[str, Any] = {"input_hashes": original_hashes, "width_edits": counts}
    try:
        evidence["saved"] = validate_pair(
            boards["before"], boards["after"], args.kicad_cli, args.native_python
        )
        for label in ("before-refilled", "after-refilled"):
            path = boards[label]
            run_command(
                [
                    args.kicad_cli,
                    "pcb",
                    "drc",
                    "--refill-zones",
                    "--save-board",
                    "--format",
                    "json",
                    "-o",
                    str(path.parent / "refill-drc.json"),
                    str(path),
                ],
                path.parent / "refill.log",
            )
            native_signatures(read_report(path.parent / "refill-drc.json"))
        evidence["refilled"] = validate_pair(
            boards["before-refilled"], boards["after-refilled"], args.kicad_cli, args.native_python
        )
        for key in (
            "analyzer_components",
            "native_components",
            "native_pad_names",
            "native_signatures",
            "check_signatures",
        ):
            require_same_components(
                evidence["saved"]["after"][key], evidence["refilled"]["after"][key], "refill " + key
            )
        if any(sha256(Path(path)) != digest for path, digest in original_hashes.items()):
            raise RuntimeError("Historical input or authored constraints changed during validation")
        evidence["status"] = "validated historical width correction; board remains partial"
    except Exception as error:
        evidence["status"] = "FAILED: " + str(error)
        raise
    finally:
        (output / "evidence.json").write_text(json.dumps(evidence, indent=2) + "\n")
    print(f"Verified byte-minimal candidate: {boards['after']}")
    print(f"Separate native refill evidence: {output / 'after-refilled'}")
    print("Historical error signatures preserved; board remains partial (exit 2)")
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
