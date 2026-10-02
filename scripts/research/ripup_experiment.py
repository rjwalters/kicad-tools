#!/usr/bin/env python3
"""Sequential N+1 rip-up A/B for ``kct route`` (Issue #5894, Epic #5784 Phase 3 step 3).

Step 1 (``docs/research/kct-route-phase-profile.md``) attributed where routing
time goes; step 2 (``docs/research/kct-route-net-order-experiment.md``)
measured net ordering and found it did not beat the negotiated default. This
script answers step 3's question: does the KRT-style sequential N+1 rip-up
prototype (``--ripup-strategy sequential-n1``,
``src/kicad_tools/router/sequential_ripup.py``) move completion, vias or
routing-loop time on boards 02/03, with no regression on 00/01/06a, relative
to the ``negotiated`` default?

Modeled directly on ``net_order_experiment.py``'s ``route`` subcommand (same
board keys/recipes as ``krt_compare.py``, same grading functions, same
subprocess-per-(board, variant) isolation via
``route_phase_profile.profile_route``) rather than ``krt_compare.py`` itself:
that script requires ``--krt-dir``, an out-of-tree KiCadRoutingTools checkout,
even for a kct-only comparison -- see ``net_order_experiment.py``'s own
docstring for why. This script is intentionally NOT a generalisation of
``net_order_experiment.py`` into a shared multi-flag harness: the two
experiments' per-run diagnostics differ (order-applied vs.
rip-up-pass/baseline-gate lines) enough that a shared harness would need as
much plumbing as the duplication costs, matching the
``krt_compare.py``/``net_order_experiment.py`` precedent's own choice to
duplicate ``Board``/``BOARDS`` rather than import across scripts.

Research-only: not wired into CI. Inputs are copied into ``--work-dir``; the
repo's ``boards/`` tree is never written.

Usage::

    uv run kct build-native --check
    uv run python scripts/research/ripup_experiment.py \\
        --boards 00,01,02,03,06a --variants negotiated,sequential-n1 \\
        --work-dir /tmp/ripup-experiment --json /tmp/ripup-experiment/summary.json
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


# Keys and recipes deliberately match ``scripts/research/krt_compare.py`` (and
# ``net_order_experiment.py``) so a row here reads against a row there. 00/01/06a
# are the no-regression set named in #5894's acceptance criteria; 02/03 are the
# subjects.
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

# Variant name -> extra ``kct route`` arguments. ``negotiated`` is the
# documented default recipe (no flag -- byte-identical to pre-#5894 main);
# ``sequential-n1`` is the new prototype. Every variant also gets ``--seed``
# so the only difference between two arms is the rip-up strategy.
VARIANTS: dict[str, tuple[str, ...]] = {
    "negotiated": (),
    "sequential-n1": ("--ripup-strategy", "sequential-n1"),
    # ``--allow-stranded-pour-pads`` variant of the prototype: #5894's first
    # fleet run found ``sequential-n1`` can leave the post-route pour-net
    # oracle unable to converge on board 02 even though the real signal nets
    # all completed (see the research doc's "pour-oracle regression"
    # section) -- this arm isolates the signal-routing comparison from that
    # separate, order/strategy-independent subsystem's pass/fail gate.
    "sequential-n1-allow-stranded-pour": (
        "--ripup-strategy",
        "sequential-n1",
        "--allow-stranded-pour-pads",
    ),
}

# Stage markers that constitute "routing proper" -- everything else recorded
# by ``route_deadline.record_stage`` (optimization, native-zone-fill,
# post-route-drc, serialization, ...) is strategy-independent post-processing.
# Matches ``net_order_experiment.py``'s own ``ROUTING_STAGES``.
ROUTING_STAGES = ("layer-escalation",)


def _routing_loop_seconds(profile: dict) -> float:
    return round(
        sum(r["seconds"] for r in profile.get("stages", []) if r["stage"] in ROUTING_STAGES), 3
    )


def _astar_calls(profile: dict) -> int:
    """Total A* invocations -- a load-independent proxy for search work.

    Seconds on a shared host are dominated by contention (see
    ``net_order_experiment.py``'s own measurement: 154s vs 63s on board 02
    for two runs with identical overflow trajectories). Call counts are not.
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


def _strategy_applied(profile: dict, variant: str) -> bool:
    """Did the run actually dispatch to the sequential-n1 prototype?

    Guards against a silent no-op the same way ``net_order_experiment.py``'s
    ``_order_applied`` does for ``--order-method`` (#5908's lesson): an A/B
    whose treatment arm never applied the treatment is worse than no
    measurement. ``negotiated`` has no marker to check (it IS the absence of
    the banner), so it is trivially "applied".
    """
    if not variant.startswith("sequential-n1"):
        return True
    return any("Sequential N+1 Rip-up Routing" in line for line in _log_lines(profile))


def _ripup_pass_lines(profile: dict, prefix: str) -> list[str]:
    """Every log line starting with ``prefix``, in order -- not just the first.

    A run can invoke the strategy more than once: board 03 enters the
    ``layer-escalation`` stage and routes twice, so each of these prefixes
    appears twice with different seconds and counters.  Returning only the
    first match silently dropped the later invocations, which is harmless when
    they agree (they did on this fleet) and a misreport when they do not.
    """
    return [
        stripped for line in _log_lines(profile) if (stripped := line.strip()).startswith(prefix)
    ]


def _improvement_gate_kept(profile: dict) -> bool | None:
    """Did the whole-pass improvement gate keep the rip-up pass, or revert?

    Returns ``None`` when the gate line is absent (``negotiated`` arm, or a
    ``sequential-n1`` run with ``enable_improvement_gate=False`` -- not used
    by this script's variants, but the gate is checked by substring, not
    position, so a future variant adding that flag stays correctly readable).
    """
    for line in _log_lines(profile):
        stripped = line.strip()
        if "Improvement gate: rip-up pass kept" in stripped:
            return True
        if "Improvement gate: REVERTED" in stripped:
            return False
    return None


def _negotiation_trajectory(profile: dict) -> dict:
    """Iteration count and the overflow sequence -- deterministic per run."""
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

    No DRC referee here -- a rip-up-strategy A/B compares kct against kct on
    the same input, so there is no cross-tool goalpost to normalise. Matches
    ``net_order_experiment.py``'s own ``grade``.
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
            "astar_calls": _astar_calls(profile),
            "strategy_applied": _strategy_applied(profile, variant),
            # Plural: one entry per invocation of the strategy in this run (a
            # board that escalates layers routes more than once).
            "rip_up_pass_lines": _ripup_pass_lines(profile, "Rip-up pass:"),
            "no_ripup_baseline_lines": _ripup_pass_lines(profile, "No-rip-up baseline:"),
            # Issue #5894: the gate can only say WHETHER two passes tied, not
            # why.  These two lines (``_PassStats.summary``) say how many
            # escalations fired and how many committed, which is what
            # distinguishes "no net ever failed its direct attempt" from
            # "every escalation was rolled back" when the metrics tie.
            "ripup_stats_lines": _ripup_pass_lines(profile, "[rip-up]"),
            "baseline_stats_lines": _ripup_pass_lines(profile, "[no-rip-up baseline]"),
            "improvement_gate_kept": _improvement_gate_kept(profile),
            "loadavg_start": profile["host"].get("loadavg"),
            "loadavg_end": profile["host"].get("loadavg_end"),
            **_negotiation_trajectory(profile),
        }
    )
    if variant.startswith("sequential-n1") and not row["strategy_applied"]:
        row["warning"] = (
            f"variant {variant!r} did not log the 'Sequential N+1 Rip-up Routing' "
            "banner -- the flag may be a no-op on this code path; do not read "
            "this row as an A/B"
        )
    routed = run_dir / f"{Path(board.src).stem}_routed.kicad_pcb"
    if routed.exists():
        row.update(grade(routed))
    else:
        row["error"] = f"no routed output produced (exit_code={profile.get('exit_code')})"
    return row


def render(rows: list[dict]) -> str:
    out = [
        "| Board | Variant | Applied | Gate kept | Nets complete | Connections | Vias "
        "| Wirelength mm | Iters | A* calls | Routing-loop s | Wall s |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        if "nets_complete" not in r:
            out.append(
                f"| {r['board']} | {r['variant']} | -- | -- | ERROR | -- | -- | -- | -- "
                f"| -- | -- | {r.get('subprocess_wall_s', '--')} |"
            )
            continue
        out.append(
            f"| {r['board']} | {r['variant']} | {r.get('strategy_applied')} "
            f"| {r.get('improvement_gate_kept', '--')} "
            f"| {r['nets_complete']}/{r['nets_total']} "
            f"| {r['connections_routed']}/{r['connections_total']} "
            f"| {r['via_count']} | {r['wirelength_mm']} "
            f"| {r.get('negotiated_iterations', '--')} | {r.get('astar_calls', '--')} "
            f"| {r['routing_loop_s']:.2f} | {r['wall_s']:.1f} |"
        )
    return "\n".join(out)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--boards", default="00,01,02,03,06a")
    ap.add_argument("--variants", default="negotiated,sequential-n1")
    ap.add_argument("--work-dir", type=Path, required=True)
    ap.add_argument("--json", type=Path)
    ap.add_argument("--seed", type=int, default=42, help="same seed in every arm")
    ap.add_argument("--timeout", type=float, default=1800.0, help="per board+variant cap, s")
    args = ap.parse_args()

    keys = [k.strip() for k in args.boards.split(",") if k.strip()]
    for key in keys:
        if key not in BOARDS:
            ap.error(f"unknown board {key!r}; known: {','.join(BOARDS)}")
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
