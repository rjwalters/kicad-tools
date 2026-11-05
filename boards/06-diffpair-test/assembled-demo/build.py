#!/usr/bin/env python3
"""Reproduce the assembled LVDS demo, including independent routing gates."""

import argparse
import json
import subprocess
import sys
from pathlib import Path


# Only IN/OUT are routed by ``main``, so ``kct route`` never engages the LVDS pairs
# and writes no native pair gap/skew block although the board carries four coupled
# pairs that #6443 / #7894 enforce natively.  ``write_pair_rules`` writes the block
# from the routed board's own pairs, through the resolvers ``kct route`` uses
# (``route_cmd._router_pair_gaps`` / ``_router_pair_limits``) (Issue #8404).  It runs
# before the export writer, which keeps an existing block.
def write_pair_rules(routed: Path, net_class_map_json: Path) -> None:
    from kicad_tools.cli import route_cmd
    from kicad_tools.manufacturers.netclass_dru import (
        merge_pair_gap_block,
        pair_gap_block_rules,
        pair_limit_rules,
    )
    from kicad_tools.manufacturers.project_generator import board_net_names
    from kicad_tools.router.rules import net_class_map_from_path

    routed = Path(routed)
    class_map = net_class_map_from_path(net_class_map_json, layer_stack=None)
    pairs = route_cmd._router_coupled_pairs(routed, class_map, None)
    gaps = route_cmd._router_pair_gaps(pairs, class_map, None)
    limits = route_cmd._router_pair_limits(pairs, class_map, routed)
    on_board = board_net_names(routed)
    dru = routed.with_suffix(".kicad_dru")
    existing = dru.read_text(encoding="utf-8") if dru.exists() else None
    merged = merge_pair_gap_block(
        existing,
        pair_gap_block_rules(gaps or [], on_board),
        pair_limit_rules(limits or [], on_board),
    )
    if merged is not None:
        dru.write_text(merged, encoding="utf-8")


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
    # Only IN/OUT are routed here, so kct route never engages the LVDS pairs and
    # writes no native pair gap/skew block; emit it from the board's own pairs
    # (Issue #8404, restoring what #6443 / #7894 enforce on this board).
    write_pair_rules(routed, out / "net_class_map.json")
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
