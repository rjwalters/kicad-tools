#!/usr/bin/env python3
"""Three-arm A/B for min-cost-flow escape-corridor assignment (Issue #6352).

Epic #5947 candidate 1.  Does solving the dense-package pin -> escape-exit
assignment *jointly* (min-cost max-flow, ``src/kicad_tools/router/escape_flow.py``)
beat the existing greedy face/octant + round-robin-layer assignment of
``EscapeCorridorPlanner`` (#4474/#4519)?

Arms (every arm also gets the same ``--seed``):

=========  ==============================================================
``A``      documented default recipe -- no corridor reservation (the
           pad-access invariant is ON, its default since #5891)
``B``      ``--escape-corridor-reservation`` (existing greedy planner)
``C``      ``B`` plus ``--escape-corridor-assignment flow`` (hidden,
           experiment-only selector)
=========  ==============================================================

``C`` vs ``B`` isolates the joint-assignment hypothesis; ``B`` vs ``A``
re-baselines the corridor flag itself, which bounds what ``C`` can show.

Modeled on ``ripup_experiment.py`` (same board keys/recipes as
``krt_compare.py``, same grading via ``kicad_tools.benchmark.external.metrics``,
same subprocess-per-(board, arm) isolation via ``route_phase_profile.py``).
Every row is recorded, including timeouts and errors.  Each row also says
whether its treatment actually fired (``treatment_applied`` plus the flow
planned/infeasible/fallback counters parsed from the router log), so an inert
treatment arm is reported as INVALID rather than read as a null result.

Research-only: not wired into CI.  Inputs are read from ``boards/`` and the
routed outputs land in ``--work-dir``; the repo's ``boards/`` tree is never
written (``route_phase_profile.py`` routes a copy).

Run sequentially on purpose: this is a shared host (no parallel fan-out).

Usage::

    uv run kct build-native --check
    uv run python scripts/research/escape_flow_experiment.py \\
        --boards 01,03,04,05,06a --arms A,B,C \\
        --work-dir /tmp/escape-flow --json /tmp/escape-flow/summary.json

    # plan-level comparison only (no routing; seconds):
    uv run python scripts/research/escape_flow_experiment.py \\
        --plans-only --boards 03,04,05,06a,09
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


@dataclass(frozen=True)
class Board:
    """One benchmark board, mirroring ``krt_compare.BOARDS`` keys and recipes."""

    key: str
    src: str  # repo-relative unrouted .kicad_pcb
    kct_args: tuple[str, ...] = ()
    layers: int = 2
    role: str = ""


# Dense-package census taken with ``Autorouter.detect_dense_packages`` on the
# measured revision (see the research note): 00/01/02 have none, so they can
# only show that B and C are inert; 03/04/05/06a are the subjects.
BOARDS: dict[str, Board] = {
    "01": Board(
        "01",
        "boards/01-voltage-divider/output/voltage_divider.kicad_pcb",
        role="control: no dense package (B/C must be inert)",
    ),
    "03": Board(
        "03",
        "boards/03-usb-joystick/output/usb_joystick.kicad_pcb",
        layers=4,
        role="subject: U1 QFP-44",
    ),
    "04": Board(
        "04",
        "boards/04-stm32-devboard/output/stm32_devboard.kicad_pcb",
        role="subject: U2 LQFP-48, 2 layers",
    ),
    "05": Board(
        "05",
        "boards/05-bldc-motor-controller/output/bldc_controller.kicad_pcb",
        layers=4,
        role="subject: U1 TQFP-32, U2 HTSSOP-28+EP, U3 HVSSOP-8+EP (4 layers, Kelvin)",
    ),
    "06a": Board(
        "06a",
        "boards/06-diffpair-test/output/diffpair_test.kicad_pcb",
        kct_args=("--preserve-existing",),
        layers=4,
        role="no-regression: 8x SOIC-8, LVDS pre-routed",
    ),
    # Plan-level census only by default: five dense parts (USB-C, SOP-8s,
    # TSSOP-10, a BGA-classified PD controller) widen the --plans-only sample.
    # Routable with --boards 09, but not in the default routed matrix.
    "09": Board(
        "09",
        "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb",
        layers=4,
        role="plans-only census: J1 USB-C, Q1/Q2 SOP-8, U3 TSSOP-10, U1",
    ),
}

# The routed matrix (``--boards`` default).  09 is deliberately absent: see above.
DEFAULT_ROUTED_BOARDS = "01,03,04,05,06a"

ARMS: dict[str, tuple[str, ...]] = {
    "A": (),
    "B": ("--escape-corridor-reservation",),
    "C": ("--escape-corridor-reservation", "--escape-corridor-assignment", "flow"),
}

ROUTING_STAGES = ("layer-escalation",)

_CORRIDOR_RE = re.compile(
    r"Escape corridors \((?P<ref>[^)]+)\): (?P<clusters>\d+) cluster\(s\), "
    r"(?P<cells>\d+) cells reserved"
)
_FLOW_SUMMARY_RE = re.compile(
    r"Escape-flow summary: packages=(?P<packages>\d+)"
    r"(?: planned=(?P<planned>\d+) infeasible=(?P<infeasible>\d+) "
    r"empty=(?P<empty>\d+) error=(?P<error>\d+))?"
)


def _log_lines(profile: dict) -> list[str]:
    out = []
    for entry in profile.get("log", []):
        out.append(
            str(entry[1]) if isinstance(entry, (list, tuple)) and len(entry) > 1 else str(entry)
        )
    return out


def _routing_loop_seconds(profile: dict) -> float:
    return round(
        sum(r["seconds"] for r in profile.get("stages", []) if r["stage"] in ROUTING_STAGES), 3
    )


def _astar_calls(profile: dict) -> int:
    return sum(
        int(row.get("calls", 0))
        for row in profile.get("phases", [])
        if row["phase"].startswith("A*")
    )


def corridor_diagnostics(lines: list[str]) -> dict:
    """Parse the corridor / flow lines the router prints (pure; unit-testable).

    A board routed more than once (layer escalation) prints each line per
    invocation; every occurrence is kept and the counters are summed.
    """
    corridors = []
    flow_lines = []
    summaries = []
    for raw in lines:
        line = raw.strip()
        m = _CORRIDOR_RE.search(line)
        if m:
            corridors.append(
                {"ref": m["ref"], "clusters": int(m["clusters"]), "cells": int(m["cells"])}
            )
        if line.startswith("Escape-flow ("):
            flow_lines.append(line)
        m = _FLOW_SUMMARY_RE.search(line)
        if m:
            summaries.append({k: int(v) for k, v in m.groupdict().items() if v is not None})
    totals = {"packages": 0, "planned": 0, "infeasible": 0, "empty": 0, "error": 0}
    for s in summaries:
        for k in totals:
            totals[k] += s.get(k, 0)
    return {
        "corridor_reservations": corridors,
        "corridor_cells": sum(c["cells"] for c in corridors),
        "flow_lines": flow_lines,
        "flow_summaries": len(summaries),
        "flow_counters": totals,
    }


def treatment_status(arm: str, diag: dict) -> tuple[bool, str]:
    """Did the arm's treatment fire?  ``(applied, note)``.

    * A: always applied (it IS the absence of a treatment).
    * B: applied when the planner reserved at least one corridor; a board
      with no dense package (or whose every cluster fell under the #4519
      gate) is an *inert* row, not a measurement of the flag.
    * C: applied only when the flow summary was printed AND at least one
      package received a flow plan that reserved cells.  A run where every
      package fell back to greedy measures greedy, not flow.
    """
    if arm == "A":
        return True, ""
    if arm == "B":
        if diag["corridor_cells"] > 0:
            return True, ""
        return False, "no corridor cells reserved (no dense package or all below the gate)"
    if diag["flow_summaries"] == 0:
        return False, "flow summary absent -- flow selector never reached the planner"
    c = diag["flow_counters"]
    if c["planned"] == 0:
        return False, f"no package got a flow plan (counters {c})"
    if diag["corridor_cells"] == 0:
        return False, "flow plans reserved no cells (all clusters below the #4519 gate)"
    return True, ""


def access_classes(sidecar: Path) -> dict:
    """Terminal access classes from the run's access-witness sidecar."""
    if not sidecar.exists():
        return {"witness": "absent"}
    try:
        payload = json.loads(sidecar.read_text())
    except (OSError, ValueError) as exc:
        return {"witness": f"unreadable ({exc})"}
    witness = payload.get("witness")
    if not witness:
        return {"witness": "none (nothing ended unrouted)", "stranded": 0}
    counts = {"stranded": 0, "closed_by_commit": 0, "reopened": 0, "stranded_pre_commit": 0}
    counts["refused_legal"] = 0
    for pad in witness.get("pads", []):
        empty = pad.get("final_access") == "empty"
        closed = pad.get("first_closed_at") is not None
        if empty:
            counts["stranded"] += 1
        if closed and pad.get("reopened"):
            counts["reopened"] += 1
        elif closed:
            counts["closed_by_commit"] += 1
        elif empty:
            counts["stranded_pre_commit"] += 1
        else:
            counts["refused_legal"] += 1
    counts["terminals"] = len(witness.get("pads", []))
    counts["witness"] = "present"
    return counts


def grade(routed: Path) -> dict:
    from kicad_tools.benchmark.external.metrics import measure_completion, measure_copper

    comp = measure_completion(routed).to_dict()
    cop = measure_copper(routed).to_dict()
    return {
        "nets_complete": comp["nets_complete"],
        "nets_total": comp["nets_total"],
        "connections_routed": comp["connections_routed"],
        "connections_total": comp["connections_total"],
        "completion_pct": comp["completion_pct"],
        "via_count": cop["via_count"],
        "wirelength_mm": round(cop["wirelength_mm"], 2),
        "segment_count": cop["segment_count"],
    }


def run_one(board: Board, arm: str, work_dir: Path, seed: int, timeout: float) -> dict:
    run_dir = work_dir / f"{board.key}-{arm}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    profile_json = run_dir / "profile.json"
    extra = [*board.kct_args, "--seed", str(seed), *ARMS[arm]]
    cmd = [
        sys.executable,
        str(REPO / "scripts" / "research" / "route_phase_profile.py"),
        str(REPO / board.src),
        "--work-dir",
        str(run_dir),
        "--json",
        str(profile_json),
        "--extra",
        " ".join(extra),
    ]
    row: dict = {"board": board.key, "arm": arm, "extra": extra}
    started = time.time()
    try:
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    except subprocess.TimeoutExpired:
        row.update({"error": "TIMEOUT", "subprocess_wall_s": round(time.time() - started, 1)})
        row["valid"] = False
        return row
    row["subprocess_rc"] = proc.returncode
    row["subprocess_wall_s"] = round(time.time() - started, 1)
    (run_dir / "stdout.txt").write_text(proc.stdout)
    (run_dir / "stderr.txt").write_text(proc.stderr)
    if not profile_json.exists():
        row["error"] = (proc.stderr or proc.stdout)[-2000:]
        row["valid"] = False
        return row

    profile = json.loads(profile_json.read_text())
    diag = corridor_diagnostics(_log_lines(profile))
    applied, note = treatment_status(arm, diag)
    row.update(
        {
            "exit_code": profile["exit_code"],
            "wall_s": profile["wall_s"],
            "routing_loop_s": _routing_loop_seconds(profile),
            "astar_calls": _astar_calls(profile),
            "treatment_applied": applied,
            "loadavg_start": profile.get("host", {}).get("loadavg_start"),
            **diag,
        }
    )
    if not applied:
        row["warning"] = note
    stem = Path(board.src).stem
    routed = run_dir / f"{stem}_routed.kicad_pcb"
    row["access"] = access_classes(run_dir / f"{stem}_routed.access_witness.json")
    if routed.exists():
        row.update(grade(routed))
        row["valid"] = True
    else:
        row["error"] = f"no routed output produced (exit_code={profile.get('exit_code')})"
        row["valid"] = False
    return row


def environment() -> dict:
    env: dict = {}
    try:
        env["git_sha"] = subprocess.run(
            ["git", "-C", str(REPO), "rev-parse", "HEAD"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.strip()
    except (OSError, subprocess.CalledProcessError):
        env["git_sha"] = "unknown"
    try:
        from kicad_tools.router import cpp_backend

        info = cpp_backend.get_backend_info()
        env["cpp_backend"] = {
            k: info.get(k) for k in ("available", "version", "build_version", "backend")
        }
    except Exception as exc:  # pragma: no cover - diagnostic only
        env["cpp_backend"] = f"unknown ({exc})"
    return env


def compare_plans(board: Board) -> list[dict]:
    """Offline greedy-vs-flow plan comparison for one board (no routing).

    Loads the board, detects its dense packages, and runs the corridor
    planner in both strategies on fresh routers, so the plan-level effect of
    the treatment (clusters, reserved cells, layers, pins moved off their
    nearest face, capacity headroom, corridor overlap) can be read without a
    multi-minute route.  ``kct route`` sets up its router with more options
    than ``load_pcb_for_routing`` does, so cell counts can differ slightly
    from the routed rows; the comparison between strategies is what matters.
    """
    import contextlib
    import io

    from kicad_tools.router.io import load_pcb_for_routing

    plans: dict[str, list] = {}
    for mode in ("greedy", "flow"):
        router, _ = load_pcb_for_routing(str(REPO / board.src))
        router.enable_escape_corridor_reservation = True
        router.escape_corridor_assignment = mode
        packages = router.detect_dense_packages()
        with contextlib.redirect_stdout(io.StringIO()):
            router._reserve_escape_corridors(packages)
        plans[mode] = list(router._escape_corridor_plans)

    def reserved(plan) -> set:
        return {
            (c.layer_idx, cell)
            for c in plan.clusters
            if c.reserved_cell_count
            for cell in c.corridor_cells
        }

    rows = []
    for g, f in zip(plans["greedy"], plans["flow"], strict=True):
        gc, fc = reserved(g), reserved(f)
        union = len(gc | fc)
        # Did the joint aspect ever bind?  An exit is saturated when the flow
        # filled it to capacity; with no saturated exit the min-cost flow
        # reduces to an independent per-pin cheapest-exit choice.
        caps = f.flow_stats.get("exit_capacity", {}) if f.assignment == "flow" else {}
        loads: dict[str, int] = {}
        for c in f.clusters if f.assignment == "flow" else []:
            key = f"{c.face}/L{c.layer_idx}"
            loads[key] = loads.get(key, 0) + c.pin_count
        utilisation = [loads.get(k, 0) / cap for k, cap in caps.items() if cap > 0]
        rows.append(
            {
                "board": board.key,
                "ref": g.ref,
                "greedy_clusters": g.cluster_count,
                "greedy_cells": g.total_reserved_cells,
                "greedy_layers": sorted(g.layers_used()),
                "flow_assignment": f.assignment,
                "flow_fallback": f.fallback_reason,
                "flow_clusters": f.cluster_count,
                "flow_cells": f.total_reserved_cells,
                "flow_layers": sorted(f.layers_used()),
                "pins": f.flow_stats.get("pins"),
                "capacity": f.flow_stats.get("total_capacity"),
                "face_moved": f.flow_stats.get("face_moved"),
                "jaccard": round(len(gc & fc) / union, 2) if union else 1.0,
                "saturated_exits": sum(
                    1 for k, cap in caps.items() if cap and loads.get(k, 0) >= cap
                ),
                "max_exit_utilisation": round(max(utilisation), 2) if utilisation else None,
                "zero_capacity_exits": sum(1 for cap in caps.values() if cap == 0),
            }
        )
    return rows


def render_plans(rows: list[dict]) -> str:
    out = [
        "| Board | Part | Pins | Capacity | Greedy clusters/cells/layers "
        "| Flow clusters/cells/layers | Pins moved off nearest face | Saturated exits "
        "| Max exit utilisation | Cell overlap (Jaccard) |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        flow = (
            f"{r['flow_clusters']}/{r['flow_cells']}/{r['flow_layers']}"
            if r["flow_assignment"] == "flow"
            else f"fallback: {r['flow_fallback']}"
        )
        out.append(
            f"| {r['board']} | {r['ref']} | {r['pins']} | {r['capacity']} "
            f"| {r['greedy_clusters']}/{r['greedy_cells']}/{r['greedy_layers']} | {flow} "
            f"| {r['face_moved']} | {r['saturated_exits']} | {r['max_exit_utilisation']} "
            f"| {r['jaccard']} |"
        )
    return "\n".join(out)


def render(rows: list[dict]) -> str:
    out = [
        "| Board | Arm | Applied | exit | Nets | Connections | Vias | Wirelength mm "
        "| Corridor cells | Flow planned/infeasible/empty | Stranded | A* calls "
        "| Routing-loop s | Wall s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "nets_complete" not in r:
            out.append(
                f"| {r['board']} | {r['arm']} | -- | -- | {r.get('error', 'ERROR')[:20]} "
                f"| -- | -- | -- | -- | -- | -- | -- | -- | {r.get('subprocess_wall_s', '--')} |"
            )
            continue
        c = r.get("flow_counters", {})
        flow = (
            f"{c.get('planned')}/{c.get('infeasible')}/{c.get('empty')}"
            if r["arm"] == "C"
            else "--"
        )
        out.append(
            f"| {r['board']} | {r['arm']} | {r.get('treatment_applied')} | {r.get('exit_code')} "
            f"| {r['nets_complete']}/{r['nets_total']} "
            f"| {r['connections_routed']}/{r['connections_total']} "
            f"| {r['via_count']} | {r['wirelength_mm']} | {r.get('corridor_cells', 0)} | {flow} "
            f"| {r.get('access', {}).get('stranded', '--')} | {r.get('astar_calls', '--')} "
            f"| {r['routing_loop_s']:.2f} | {r['wall_s']:.1f} |"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--boards", default=DEFAULT_ROUTED_BOARDS)
    ap.add_argument("--arms", default="A,B,C")
    ap.add_argument("--work-dir", type=Path)
    ap.add_argument(
        "--plans-only",
        action="store_true",
        help="compare greedy vs flow corridor plans offline (no routing, seconds per board)",
    )
    ap.add_argument("--json", type=Path)
    ap.add_argument("--seed", type=int, default=42, help="same seed in every arm")
    ap.add_argument("--timeout", type=float, default=1800.0, help="per board+arm cap, s")
    args = ap.parse_args()

    keys = [k.strip() for k in args.boards.split(",") if k.strip()]
    for key in keys:
        if key not in BOARDS:
            ap.error(f"unknown board {key!r}; known: {','.join(BOARDS)}")
    arms = [a.strip() for a in args.arms.split(",") if a.strip()]
    for arm in arms:
        if arm not in ARMS:
            ap.error(f"unknown arm {arm!r}; known: {','.join(ARMS)}")

    if args.plans_only:
        plan_rows: list[dict] = []
        for key in keys:
            plan_rows.extend(compare_plans(BOARDS[key]))
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(plan_rows, indent=2) + "\n")
        print(render_plans(plan_rows))
        return 0
    if args.work_dir is None:
        ap.error("--work-dir is required unless --plans-only is given")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    env = environment()
    print(json.dumps({"environment": env}, indent=2), flush=True)
    rows: list[dict] = []
    for key in keys:
        for arm in arms:
            print(f"==> board {key}, arm {arm}", flush=True)
            try:
                row = run_one(BOARDS[key], arm, args.work_dir, args.seed, args.timeout)
            except Exception as exc:  # record, never drop a row
                row = {"board": key, "arm": arm, "error": f"harness: {exc!r}", "valid": False}
            rows.append(row)
            print(json.dumps(row, indent=2), flush=True)
            if args.json:
                args.json.parent.mkdir(parents=True, exist_ok=True)
                args.json.write_text(
                    json.dumps({"environment": env, "rows": rows}, indent=2) + "\n"
                )
    print()
    print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
