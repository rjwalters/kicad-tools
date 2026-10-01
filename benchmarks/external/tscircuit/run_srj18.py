"""Sweep driver: route dataset-srj18 source boards with kct and tscircuit (Issue #5848).

For each board it runs (a) the tscircuit autorouter on the dataset's SRJ and
writes the result back with ``srj_to_kicad.py``, (b) ``kct route`` on the
normalized board, and judges every output with ``referee.py``. Each step has a
hard wall-clock cap; a timeout is recorded as such, never as a result.
Appends one JSON line per (board, router) to ``--results``.

Usage (see docs/research/tscircuit-evaluation.md "Reproducing"):
    uv run python benchmarks/external/tscircuit/run_srj18.py \
        --cache /tmp/srj18cache --dataset /tmp/ds18 --tsc-dir /tmp/tsc \
        --work /tmp/srj18work --results /tmp/srj18work/results.jsonl \
        --timeout 600 --jobs 6
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
SLUGS = [
    "arduino_leonardo", "arduino_mega_2560", "arduino_micro", "arduino_nano",
    "arduino_uno", "ddr5_testbed", "dual_gmsl_serializer_adapter", "ftdi_toolkit",
    "gmsl_serializer", "hdmi_edid_debug_board", "job_oculink_expansion",
    "oculink_pcie_adapter", "ov5640_dual_camera_board", "ov9281_camera_board",
    "sdi_fiber_adapter", "usb_c_power_adapter",
]  # fmt: skip


def run(cmd, cap, **kw):
    t0 = time.time()
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=cap, **kw)
        return r.returncode, time.time() - t0, r.stdout + r.stderr
    except subprocess.TimeoutExpired as e:
        return "timeout", time.time() - t0, str(e.stdout or "")[-500:]


def referee(board: Path) -> dict:
    rc, _, out = run([sys.executable, str(HERE / "referee.py"), str(board)], 900)
    try:
        d = json.loads(out.strip().splitlines()[-1])
        d.pop("board", None)
        return d
    except Exception:
        return {"referee_error": out[-300:]}


def one(args, idx: int, slug: str, router: str) -> dict:
    work = Path(args.work)
    norm = Path(args.cache) / "normalized" / f"srj18_{slug}.kicad_pcb"
    res = {"board": slug, "router": router, "sample": f"sample{idx:03d}"}
    if not norm.exists():
        return {**res, "status": "not_normalized"}
    out = work / f"{slug}.{router}.kicad_pcb"
    if router == "tsc":  # run_autorouter.mjs must sit next to node_modules
        shutil.copyfile(HERE / "run_autorouter.mjs", Path(args.tsc_dir) / "run_autorouter.mjs")
    if router == "kct":
        layers = json.loads((Path(args.dataset) / "samples" / f"sample{idx:03d}.json").read_text())[
            "layerCount"
        ]
        rc, secs, log = run(
            ["uv", "run", "kct", "route", str(norm), "-o", str(out), "--layers", str(layers),
             "--grid", "0.1", "--timeout", str(args.timeout), "--backend", "cpp", "--no-auto-pour"],
            args.timeout + 120,
        )  # fmt: skip
        (work / f"{slug}.kct.log").write_text(log)
    else:
        srj = Path(args.dataset) / "samples" / f"sample{idx:03d}.json"
        jout = work / f"{slug}.tsc.json"
        rc, secs, log = run(
            ["node", "--max-old-space-size=8192", "run_autorouter.mjs", str(srj), str(jout)],
            args.timeout, cwd=args.tsc_dir,
        )  # fmt: skip
        (work / f"{slug}.tsc.log").write_text(log)
        if rc == 0 and jout.exists():
            rc2, _, log2 = run(
                [sys.executable, str(HERE / "srj_to_kicad.py"), str(norm), str(srj), str(jout), str(out)], 600
            )  # fmt: skip
            if rc2 != 0:
                return {
                    **res,
                    "status": "writeback_failed",
                    "wall_s": round(secs),
                    "note": log2[-300:],
                }
            d = json.loads(jout.read_text())
            res["router_report"] = {
                "solved": d.get("solved"),
                "failed": d.get("failed"),
                "error": str(d.get("error"))[:120],
            }
    res["wall_s"] = round(secs)
    res["exit"] = rc
    if rc == "timeout":
        return {**res, "status": "timeout", "cap_s": args.timeout}
    if not out.exists():
        return {**res, "status": "no_output", "note": log[-300:]}
    return {**res, "status": "ok", **referee(out)}


def main() -> int:
    ap = argparse.ArgumentParser()
    for a in ("cache", "dataset", "tsc_dir", "work", "results"):
        ap.add_argument(f"--{a.replace('_', '-')}", required=True)
    ap.add_argument("--timeout", type=int, default=600)
    ap.add_argument("--jobs", type=int, default=6)
    ap.add_argument("--board", action="append")
    ap.add_argument(
        "--skip-done", action="store_true", help="skip (board, router) pairs already in --results"
    )
    args = ap.parse_args()
    Path(args.work).mkdir(parents=True, exist_ok=True)
    jobs = [
        (i + 1, s, r)
        for i, s in enumerate(SLUGS)
        if not args.board or s in args.board
        for r in ("tsc", "kct")
    ]

    if args.skip_done and Path(args.results).exists():
        done = {
            (r["board"], r["router"])
            for r in map(json.loads, Path(args.results).read_text().splitlines())
        }
        jobs = [j for j in jobs if (j[1], j[2]) not in done]

    def go(j):
        r = one(args, *j)
        with open(args.results, "a") as f:
            f.write(json.dumps(r) + "\n")
        print(r.get("board"), r.get("router"), r.get("status"), flush=True)

    with ThreadPoolExecutor(args.jobs) as ex:
        list(ex.map(go, jobs))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
