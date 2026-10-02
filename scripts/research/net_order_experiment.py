#!/usr/bin/env python3
"""Net-ordering A/B for ``kct route`` (Issue #5787 step 2, Epic #5784 Phase 3).

Step 1 (``docs/research/kct-route-phase-profile.md``) attributed where the time
goes. This script answers step 2's question: **does changing the net order move
completion, vias or routing-loop time on boards 02 and 03?**

Two sub-commands:

``order-check``
    The cheap, deterministic half. Loads a board's router and asks whether the
    pre-existing ``--monotone-certificate-order`` / ``--bundle-river-planner``
    flags can change the net order *at all* on that board. Both act only
    through ``Autorouter._apply_byte_lane_inner_priority``, which needs a
    detected match group of at least 5 members projecting onto one co-located
    component row; without one it returns the identity order. This answers
    "are those two flags orderings on boards 02/03?" in seconds, instead of
    inferring it from two noisy full routes.

``route``
    The expensive half. For each (board, variant) pair, runs the full
    documented recipe through ``route_phase_profile.profile_route`` in a
    **subprocess** (so one variant's module-level instrumentation cannot leak
    into the next), then grades the routed output with the same referee
    functions ``krt_compare.py`` uses -- ``measure_completion`` /
    ``measure_copper`` from ``kicad_tools.benchmark.external.metrics`` -- and
    prints a comparison table.

Why not ``krt_compare.py`` directly: it requires ``--krt-dir``, an out-of-tree
KiCadRoutingTools checkout, even for ``--tools kct``. This harness reuses its
*measurement* path (identical functions on the same artifacts) so the kct rows
remain comparable, without needing KRT present for a kct-vs-kct A/B.

Read the **load-independent** columns first -- ``A* calls``, ``Iters``,
``overflow_trajectory``, and the quality metrics. Seconds (even routing-loop
seconds) are only meaningful on a quiet host: the first pass of this A/B
measured 154 s vs 63 s in the routing loop on board 02 for two runs with
*identical* overflow trajectories and byte-identical copper, i.e. pure
contention. Of the time columns, prefer **routing-loop seconds** (the
``layer-escalation`` stage) over wall: the step-1 profile showed zone fill, the
trace optimizer and post-route DRC dominate wall on several boards and are
order-independent.

Every ordering arm is checked for an ``Net order: --order-method`` log line and
flagged with ``warning`` when it is missing. An arm that silently discarded its
flag is how this experiment first "measured" a null result: ``kct route``
defaults to ``--auto-layers``, and that escalation path did not call
``_apply_order_method`` at all (fixed under #5787).

Research-only: not wired into CI. Inputs are copied into ``--work-dir``; the
repo's ``boards/`` tree is never written.

Usage::

    uv run kct build-native --check
    uv run python scripts/research/net_order_experiment.py order-check --boards 02,03
    uv run python scripts/research/net_order_experiment.py route \\
        --boards 00,01,02,03,06a --variants default,crossing \\
        --work-dir /tmp/net-order --json /tmp/net-order/summary.json
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
    note: str = ""


# Keys and recipes deliberately match ``scripts/research/krt_compare.py`` so a
# row here can be read against a row there. 00/01/06a are the no-regression
# set named in #5787's acceptance criteria; 02/03 are the subjects.
BOARDS: dict[str, Board] = {
    "00": Board("00", "boards/00-simple-led/output/simple_led.kicad_pcb"),
    "01": Board("01", "boards/01-voltage-divider/output/voltage_divider.kicad_pcb"),
    "02": Board("02", "boards/02-charlieplex-led/output/charlieplex_3x3.kicad_pcb"),
    "03": Board("03", "boards/03-usb-joystick/output/usb_joystick.kicad_pcb", layers=4),
    "06a": Board(
        "06a",
        "boards/06-diffpair-test/output/diffpair_test.kicad_pcb",
        kct_args=("--preserve-existing",),
        layers=4,
        note="LVDS pairs arrive pre-routed; routes the 8 LVTTL nets",
    ),
}

# Variant name -> extra ``kct route`` arguments. ``default`` is the documented
# recipe with no ordering flag; every variant also gets ``--seed`` so the only
# difference between two arms is the net order, not the RNG.
VARIANTS: dict[str, tuple[str, ...]] = {
    "default": (),
    "crossing": ("--order-method", "crossing"),
    "greedy": ("--order-method", "greedy"),
    "critical_first": ("--order-method", "critical_first"),
    "congestion": ("--order-method", "congestion"),
    "monotone": ("--monotone-certificate-order",),
    "bundle-river": ("--bundle-river-planner",),
}

# Stage markers that constitute "routing proper". Everything else recorded by
# ``route_deadline.record_stage`` (optimization, native-zone-fill,
# post-route-drc, serialization, ...) is order-independent post-processing.
ROUTING_STAGES = ("layer-escalation",)


# ---------------------------------------------------------------------------
# order-check: can the pre-existing flags reorder this board at all?
# ---------------------------------------------------------------------------


def order_check(board: Board) -> dict:
    """Report whether the byte-lane ordering flags are identity on ``board``.

    Loads the board's router once and compares the net order
    ``_apply_byte_lane_inner_priority`` returns with every byte-lane flag off
    against the order it returns with all three on. An identical order means
    ``--monotone-certificate-order`` and ``--bundle-river-planner`` cannot be
    net orderings on this board, whatever they do on board 07.

    Returns:
        A dict with the net count, both orders' equality, and the
        crossing-aware order's displacement from the default sort for context.
    """
    from kicad_tools.router.io import load_pcb_for_routing

    src = REPO / board.src
    router, _ = load_pcb_for_routing(str(src), validate_drc=False)

    base = sorted(router.nets.keys(), key=lambda n: router._get_net_priority(n))
    base = router._filter_pour_nets(base)
    base = router._interleave_match_groups(base)

    router.enable_byte_lane_reorder = False
    router.enable_bundle_river_planner = False
    router.enable_monotone_certificate_order = False
    flags_off = router._apply_byte_lane_inner_priority(list(base))

    router.enable_byte_lane_reorder = True
    router.enable_bundle_river_planner = True
    router.enable_monotone_certificate_order = True
    flags_on = router._apply_byte_lane_inner_priority(list(base))

    # Context: how far does the crossing-aware order move things, for a board
    # where the byte-lane flags provably cannot?
    sys.path.insert(0, str(REPO / "src"))
    from kicad_tools.cli.route_cmd import _crossing_aware_net_order

    crossing = [n for n in _crossing_aware_net_order(router) if n in set(base)]
    displaced = sum(1 for a, b in zip(base, crossing, strict=False) if a != b)

    return {
        "board": board.key,
        "nets": len(base),
        "byte_lane_flags_change_order": flags_off != flags_on,
        "byte_lane_order_is_identity": flags_on == list(base),
        "crossing_order_displaces_nets": displaced,
        "crossing_order_changes_order": crossing != list(base),
    }


# ---------------------------------------------------------------------------
# route: full-recipe A/B
# ---------------------------------------------------------------------------


def _routing_loop_seconds(profile: dict) -> float:
    return round(
        sum(r["seconds"] for r in profile.get("stages", []) if r["stage"] in ROUTING_STAGES), 3
    )


def _astar_seconds(profile: dict) -> float:
    for row in profile.get("phases", []):
        if row["phase"].startswith("A* search"):
            return round(row["inclusive_s"], 3)
    return 0.0


def _astar_calls(profile: dict) -> int:
    """Total A* invocations -- a **load-independent** proxy for search work.

    Seconds on a shared host are dominated by contention (a first pass of this
    A/B measured 154 s vs 63 s on board 02 for two runs with *identical*
    overflow trajectories and identical copper). Call counts are not, so they
    are the number to read when the host is busy.
    """
    return sum(
        int(row.get("calls", 0))
        for row in profile.get("phases", [])
        if row["phase"].startswith("A*")
    )


def _log_lines(profile: dict) -> list[str]:
    """Flatten ``profile['log']`` (``[elapsed_s, text]`` pairs) to text."""
    out = []
    for entry in profile.get("log", []):
        out.append(
            str(entry[1]) if isinstance(entry, (list, tuple)) and len(entry) > 1 else str(entry)
        )
    return out


def _order_applied(profile: dict) -> bool:
    """Did the run actually install a ``--order-method`` order?

    Guard against the silent no-op this experiment first hit: ``kct route``
    defaults to ``--auto-layers``, whose escalation path did not call
    ``_apply_order_method`` at all, so every ordering flag was discarded and
    both arms produced byte-identical copper. An A/B whose treatment arm never
    applied the treatment is worse than no measurement, so assert it per run.
    """
    return any("Net order: --order-method" in line for line in _log_lines(profile))


def _negotiation_trajectory(profile: dict) -> dict:
    """Iteration count and the overflow sequence -- deterministic per order."""
    overflow: list[int] = []
    for line in _log_lines(profile):
        match = re.search(r"overflow[:=] *(\d+)", line)
        if match and ("Rerouted" in line or "Routed" in line or "Iteration" in line):
            overflow.append(int(match.group(1)))
    return {
        "negotiated_iterations": len(profile.get("iterations", []) or []),
        "overflow_trajectory": overflow,
    }


def grade(routed: Path) -> dict:
    """Completion / via / wirelength metrics, via ``krt_compare``'s referee path.

    No DRC referee here: a net-order A/B compares kct against kct on the same
    input, so there is no cross-tool goalpost to normalise -- and the step-1
    profile showed the ``kicad-cli`` DRC pass is the single most load-sensitive
    phase on this host. DRC grading stays ``krt_compare.py``'s job.
    """
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


def run_one(board: Board, variant: str, work_dir: Path, seed: int, timeout: float) -> dict:
    """Profile one (board, variant) pair in a subprocess and grade the output."""
    run_dir = work_dir / f"{board.key}-{variant}"
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    profile_json = run_dir / "profile.json"
    extra = [*board.kct_args, "--seed", str(seed), *VARIANTS[variant]]

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
    started = time.time()
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)
    elapsed = round(time.time() - started, 1)

    row: dict = {
        "board": board.key,
        "variant": variant,
        "extra": extra,
        "subprocess_rc": proc.returncode,
        "subprocess_wall_s": elapsed,
    }
    if not profile_json.exists():
        row["error"] = (proc.stderr or proc.stdout)[-2000:]
        return row

    profile = json.loads(profile_json.read_text())
    row.update(
        {
            "exit_code": profile["exit_code"],
            "wall_s": profile["wall_s"],
            "routing_loop_s": _routing_loop_seconds(profile),
            "astar_s": _astar_seconds(profile),
            "astar_calls": _astar_calls(profile),
            "order_applied": _order_applied(profile),
            "loadavg_start": profile["host"].get("loadavg"),
            "loadavg_end": profile["host"].get("loadavg_end"),
            **_negotiation_trajectory(profile),
        }
    )
    # An ordering arm that did not install an order is not a measurement.
    if "--order-method" in VARIANTS[variant] and not row["order_applied"]:
        row["warning"] = (
            f"variant {variant!r} did not log a 'Net order:' line -- the ordering "
            "flag may be a no-op on this code path; do not read this row as an A/B"
        )
    routed = run_dir / f"{Path(board.src).stem}_routed.kicad_pcb"
    if routed.exists():
        row.update(grade(routed))
    else:
        row["error"] = "no routed output produced"
    return row


def render(rows: list[dict]) -> str:
    out = [
        "| Board | Variant | Order applied | Nets complete | Connections | Vias "
        "| Wirelength mm | Iters | A* calls | Routing-loop s | A* s | Wall s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "nets_complete" not in r:
            out.append(
                f"| {r['board']} | {r['variant']} | -- | ERROR | -- | -- | -- | -- | -- "
                f"| -- | -- | {r.get('subprocess_wall_s', '--')} |"
            )
            continue
        out.append(
            f"| {r['board']} | {r['variant']} | {r.get('order_applied')} "
            f"| {r['nets_complete']}/{r['nets_total']} "
            f"| {r['connections_routed']}/{r['connections_total']} "
            f"| {r['via_count']} | {r['wirelength_mm']} "
            f"| {r.get('negotiated_iterations', '--')} | {r.get('astar_calls', '--')} "
            f"| {r['routing_loop_s']:.2f} | {r['astar_s']:.2f} | {r['wall_s']:.1f} |"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)

    oc = sub.add_parser("order-check", help="are the byte-lane flags identity on this board?")
    oc.add_argument("--boards", default="02,03")
    oc.add_argument("--json", type=Path)

    rt = sub.add_parser("route", help="full-recipe net-ordering A/B")
    rt.add_argument("--boards", default="00,01,02,03,06a")
    rt.add_argument("--variants", default="default,crossing")
    rt.add_argument("--work-dir", type=Path, required=True)
    rt.add_argument("--json", type=Path)
    rt.add_argument("--seed", type=int, default=42, help="same seed in every arm")
    rt.add_argument("--timeout", type=float, default=1800.0, help="per board+variant cap, s")

    args = ap.parse_args()
    keys = [k.strip() for k in args.boards.split(",") if k.strip()]
    for key in keys:
        if key not in BOARDS:
            ap.error(f"unknown board {key!r}; known: {','.join(BOARDS)}")

    if args.cmd == "order-check":
        rows = [order_check(BOARDS[k]) for k in keys]
        print(
            "| Board | Nets | byte-lane flags change order? | byte-lane order identity? "
            "| crossing order changes it? | nets displaced |"
        )
        print("|---|---|---|---|---|---|")
        for r in rows:
            print(
                f"| {r['board']} | {r['nets']} | {r['byte_lane_flags_change_order']} "
                f"| {r['byte_lane_order_is_identity']} | {r['crossing_order_changes_order']} "
                f"| {r['crossing_order_displaces_nets']} |"
            )
        if args.json:
            args.json.parent.mkdir(parents=True, exist_ok=True)
            args.json.write_text(json.dumps(rows, indent=2) + "\n")
        return 0

    variants = [v.strip() for v in args.variants.split(",") if v.strip()]
    for variant in variants:
        if variant not in VARIANTS:
            ap.error(f"unknown variant {variant!r}; known: {','.join(VARIANTS)}")

    args.work_dir.mkdir(parents=True, exist_ok=True)
    rows: list[dict] = []
    for key in keys:
        for variant in variants:
            print(f"==> board {key}, variant {variant}", flush=True)
            try:
                row = run_one(BOARDS[key], variant, args.work_dir, args.seed, args.timeout)
            except subprocess.TimeoutExpired:
                row = {"board": key, "variant": variant, "error": "TIMEOUT"}
            rows.append(row)
            print(json.dumps(row, indent=2), flush=True)
            if args.json:
                args.json.parent.mkdir(parents=True, exist_ok=True)
                args.json.write_text(json.dumps(rows, indent=2) + "\n")
    print()
    print(render(rows))
    return 0


if __name__ == "__main__":
    sys.exit(main())
