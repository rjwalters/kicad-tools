#!/usr/bin/env python3
"""Reproduce the assembled LVDS demo, including independent routing gates."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("output", nargs="?", type=Path)
    parser.add_argument("--step", choices=("all", "route"), default="all")
    args = parser.parse_args()
    here = Path(__file__).resolve().parent
    out = (args.output or here.parent / "output").resolve()
    subprocess.run([sys.executable, str(here / "generate.py"), str(out)], check=True)
    routed = out / "diffpair_test_routed.kicad_pcb"
    route = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli.route_cmd",
            str(out / "diffpair_test.kicad_pcb"),
            "--output",
            str(routed),
            "--nets",
            "IN1,IN2,IN3,IN4,OUT1,OUT2,OUT3,OUT4",
            "--preserve-existing",
            "--layers",
            "4",
            "--no-auto-layers",
            "--no-auto-pour",
            "--strict-layers",
            "--no-cache",
            "--strategy",
            "negotiated",
            "--seed",
            "42",
            "--iterations",
            "20",
            "--timeout",
            "120",
            "--grid",
            "0.075",
            "--net-class-map",
            str(out / "net_class_map.json"),
        ]
    )
    if not routed.exists():
        return route.returncode or 1
    # Resolve the project's actual fabrication floors before filling copper.
    # This intermediate check only emits rules; the finished board is gated below.
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(routed),
            "--emit-dru",
            "--format",
            "json",
            "--output",
            str(out / "pre-finish-check.json"),
        ]
    )
    subprocess.run([sys.executable, str(here / "finish.py"), str(out), str(routed)], check=True)
    native = out / "native-drc.json"
    subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            str(routed),
            "--refill-zones",
            "--format",
            "json",
            "--output",
            str(native),
        ],
        check=True,
    )
    report = json.loads(native.read_text())
    if report.get("violations") or report.get("unconnected_items"):
        print("Native DRC has unresolved findings; no manufacturing release.")
        return 2
    check = out / "check.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(routed),
            "--format",
            "json",
            "--output",
            str(check),
        ]
    )
    data = json.loads(check.read_text())
    meta = data.get("meta_checks", {})
    passed = not data["summary"]["errors"] and not data["summary"]["warnings"]
    passed &= all(meta.get(k, {}).get("status") == "PASSED" for k in ("erc", "lvs"))
    if passed:
        print(
            "Circuit/routing gates pass. Regenerate and verify manufacturing exports before ordering."
        )
    return 0 if passed else 2


if __name__ == "__main__":
    raise SystemExit(main())
