#!/usr/bin/env python3
"""Board 05 routed from scratch, unattended, then gated (issue #7834, part of #4410).

The shipping recipe (``boards/05-bldc-motor-controller/design.py`` ->
``redesign/build.py``) replays reviewed copper from ``redesign/routing.json``,
so neither it nor the ``board-05-routing-regression`` CI job says anything
about whether ``kct route`` can produce this board on its own. This script is
that proof. It leaves the shipping path alone:

1. ``redesign/hardware.generate`` writes the schematic and the **unrouted**
   PCB. That is the step ``build.py`` runs before it applies ``routing.json``;
   nothing from ``routing.json`` is read here.
2. ``kct route`` routes that board under the #6508 protocol flags
   (:data:`PROTOCOL_FLAGS`) at each ``--seed``. Since #7831 it chains the
   ``--complete`` lattice pass itself; ``--reserve-pour-only-passives`` has
   been on by default since #7835, so it is not passed.
3. The routed board is graded with the pinned toolchain
   (:func:`kicad_tools.cli.kicad_toolchain.evidence_toolchain`, so
   ``KCT_KICAD_DOCKER=1`` works on a host):

   * kicad-cli ``pcb drc --refill-zones --all-track-errors``: 0 error-severity
     violations, 0 warnings and 0 unconnected items. A ``lib_footprint_issues`` finding
     refuses (exit 2): it describes the environment, not the board (#6962).
   * kicad-cli ``sch erc``: 0 violations (the same gate ``build.py`` applies).
   * ``kct check --mfr jlcpcb``: 0 errors and 0 warnings (the #7298
     no-warning gate ``build.py`` applies; #8073).
   * copper + label LVS (:func:`kicad_tools.lvs.write_lvs_report`): clean.
   * every net complete (``measure_completion``), and the route itself exited
     0 with a finished (not deadline best-so-far) output.

   A row whose stranded-pour oracle did not answer (kicad-cli OOM 137, docker
   unreachable; #7836) fails: it was never actually checked.

There is no allowlist of any kind: a single open net, DRC error or LVS
mismatch fails the run (:func:`gate_failures`).

Exit codes:
    0 -- every seed passed every gate.
    1 -- at least one seed failed a gate (or its route produced no board).
    2 -- environment refusal: kicad-cli is not the pinned release, or the
         DRC report carries ``lib_footprint_issues`` (#6962).

``--grade-only PCB`` skips generation and routing and grades an existing
routed board (its schematic is the sibling ``bldc_controller.kicad_sch``,
override with ``--sch``); that is how a negative run is made. Stage the
schematic's ``bldc_controller.kicad_pro``, ``sym-lib-table``,
``fp-lib-table`` and ``board05_revB.*`` beside it, as a seed run does, or
kicad-cli ERC reports ``lib_symbol_issues`` / ``footprint_link_issues`` and
the run fails.

Usage::

    uv run kct build-native --check
    KCT_KICAD_DOCKER=1 uv run python scripts/ci/board_05_unattended.py \\
        --seeds 42,7,1337 --work-dir /tmp/b05 --json /tmp/b05/results.json

Results across seeds: ``docs/research/board-05-unattended-route.md``.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib.util
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BOARD = REPO / "boards" / "05-bldc-motor-controller"
REDESIGN = BOARD / "redesign"
HARNESS = REPO / "scripts" / "research" / "expand_route_experiment.py"

PCB_NAME = "bldc_controller.kicad_pcb"
SCH_NAME = "bldc_controller.kicad_sch"
ROUTED_NAME = "bldc_controller_routed.kicad_pcb"

#: The #6508 protocol extras the #7831 evidence was measured under.
PROTOCOL_FLAGS: tuple[str, ...] = (
    "--no-auto-layers",
    "--deterministic-budget",
    "--per-net-iterations",
    "1000000",
    "--no-placement-delta-feedback",
    "--deterministic-rescue",
    "--no-fallback-bidirectional",
)

DEFAULT_SEEDS = (42, 7, 1337)
#: ``kct route --timeout`` (the router's own hard budget, s). The #7838 Judge
#: run finished seed 42 in 318 s on a host; the slack is for a CI runner.
DEFAULT_TIMEOUT_S = 4500
MANUFACTURER = "jlcpcb"

#: Files ``hardware.generate`` writes that kicad-cli and LVS need beside the
#: routed board (the board's own footprint library, else kicad-cli reports
#: ``lib_footprint_issues``).
GRADING_SIBLINGS = (
    SCH_NAME,
    "board05_revB.kicad_sym",
    "board05_revB.pretty",
    "fp-lib-table",
    "sym-lib-table",
)


def _load_harness():
    """``scripts/research/expand_route_experiment.py`` as a module.

    Its route-log parser carries the stranded-pour-oracle and wall-clock flags
    (#7836, #6420) and its artifact picker knows a deadline best-so-far from a
    finished route (#6308, #6475); reusing them keeps one definition.
    """
    spec = importlib.util.spec_from_file_location("expand_route_experiment", HARNESS)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules.setdefault("expand_route_experiment", module)
    spec.loader.exec_module(module)
    return module


# ---------------------------------------------------------------------------
# The gate (pure; unit-tested)
# ---------------------------------------------------------------------------


def gate_failures(row: dict) -> list[str]:
    """Why *row* is not a manufacturable unattended route; empty when it is.

    Every gate must have *run* and passed: a missing number is a failure, not a
    pass ("not checked" is not "clean"). No identity allowlist exists.
    """
    failures: list[str] = []
    if row.get("error"):
        failures.append(str(row["error"]))
    if "exit_code" in row and row.get("exit_code") != 0:
        failures.append(f"kct route exited {row.get('exit_code')}")
    if "artifact" in row and row.get("artifact") != "final":
        failures.append(f"graded board is a {row.get('artifact')!r} artifact, not the final output")
    if row.get("cache_hit"):
        failures.append("kct route replayed cached copper")
    if row.get("oracle_unverified"):
        reason = row.get("oracle_unverified_reason") or "unknown"
        failures.append(f"stranded-pour oracle did not answer ({reason})")

    complete, total = row.get("nets_complete"), row.get("nets_total")
    if complete is None or total is None:
        failures.append("net completion not measured")
    elif not total or complete != total:
        failures.append(f"nets {complete}/{total} complete")

    def count(key: str, label: str) -> None:
        value = row.get(key)
        if value is None:
            failures.append(f"{label} did not run")
        elif value:
            failures.append(f"{label}: {value}")

    count("kicad_drc_errors", "kicad-cli DRC errors")
    count("kicad_drc_unconnected", "kicad-cli unconnected items")
    count("kicad_erc_violations", "kicad-cli ERC violations")
    count("kct_check_errors", f"kct check --mfr {MANUFACTURER} errors")
    count("kicad_drc_warnings", "kicad-cli DRC warnings")
    count("kct_check_warnings", f"kct check --mfr {MANUFACTURER} warnings")

    for key, label in (("lvs_copper_clean", "copper LVS"), ("lvs_label_clean", "label LVS")):
        value = row.get(key)
        if value is None:
            failures.append(f"{label} did not run")
        elif value is not True:
            failures.append(f"{label} dirty")
    return failures


def exit_code(rows: list[dict]) -> int:
    """0 when every row passes :func:`gate_failures`, else 1 (also for no rows)."""
    if not rows:
        return 1
    return 0 if all(not gate_failures(row) for row in rows) else 1


# ---------------------------------------------------------------------------
# Generate / route / grade
# ---------------------------------------------------------------------------


def generate_unrouted(dest: Path) -> Path:
    """Write board 05's schematic + unrouted PCB into *dest* (no routing.json)."""
    sys.path.insert(0, str(REDESIGN))
    try:
        from hardware import generate  # boards/05-bldc-motor-controller/redesign
    finally:
        sys.path.remove(str(REDESIGN))
    generate(dest)
    return dest / PCB_NAME


def stage_seed(generated: Path, run_dir: Path) -> Path:
    """Copy the unrouted board and its grading siblings into *run_dir*."""
    if run_dir.exists():
        shutil.rmtree(run_dir)
    run_dir.mkdir(parents=True)
    for name in (PCB_NAME, "bldc_controller.kicad_pro", *GRADING_SIBLINGS):
        src = generated / name
        if src.is_dir():
            shutil.copytree(src, run_dir / name)
        elif src.exists():
            shutil.copy2(src, run_dir / name)
    return run_dir / PCB_NAME


def route_flags(seed: int) -> list[str]:
    return [*PROTOCOL_FLAGS, "--seed", str(seed)]


def run_kicad_drc(routed: Path, evidence: Path) -> dict:
    """kicad-cli DRC on *routed*; raises ``KiCadEnvironmentError`` on library findings."""
    from kicad_tools.cli.kicad_toolchain import require_library_environment

    report_path = evidence / "native-drc.json"
    proc = subprocess.run(
        [
            "kicad-cli",
            "pcb",
            "drc",
            str(routed),
            "--refill-zones",
            "--all-track-errors",
            "--format",
            "json",
            "--output",
            str(report_path),
        ],
        capture_output=True,
        text=True,
        timeout=1200,
        check=False,
    )
    if not report_path.exists():
        return {"kicad_drc_note": f"kicad-cli pcb drc wrote no report (rc={proc.returncode})"}
    report = json.loads(report_path.read_text())
    require_library_environment(report)
    errors = [v for v in report.get("violations", []) if v.get("severity") == "error"]
    by_type: dict[str, int] = {}
    for v in errors:
        by_type[v.get("type", "?")] = by_type.get(v.get("type", "?"), 0) + 1
    return {
        "kicad_drc_errors": len(errors),
        "kicad_drc_warnings": sum(
            1 for v in report.get("violations", []) if v.get("severity") == "warning"
        ),
        "kicad_drc_unconnected": len(report.get("unconnected_items", [])),
        "kicad_drc_by_type": dict(sorted(by_type.items())),
        "kicad_drc_report": str(report_path),
    }


def run_kicad_erc(sch: Path, evidence: Path) -> dict:
    report_path = evidence / "native-erc.json"
    proc = subprocess.run(
        ["kicad-cli", "sch", "erc", str(sch), "--format", "json", "--output", str(report_path)],
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    if not report_path.exists():
        return {"kicad_erc_note": f"kicad-cli sch erc wrote no report (rc={proc.returncode})"}
    report = json.loads(report_path.read_text())
    return {
        "kicad_erc_violations": sum(len(s.get("violations", [])) for s in report.get("sheets", []))
    }


def run_kct_check(routed: Path, evidence: Path) -> dict:
    report_path = evidence / "kct-check.json"
    subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "check",
            str(routed),
            "--mfr",
            MANUFACTURER,
            "--format",
            "json",
            "--output",
            str(report_path),
        ],
        stdout=subprocess.DEVNULL,
        timeout=1800,
        check=False,
    )
    if not report_path.exists():
        return {"kct_check_note": "kct check wrote no report"}
    report = json.loads(report_path.read_text())
    summary = report.get("summary", {})
    by_rule: dict[str, int] = {}
    for v in report.get("violations", []):
        if v.get("severity") == "error":
            rule = v.get("rule_id") or v.get("rule") or "?"
            by_rule[rule] = by_rule.get(rule, 0) + 1
    return {
        "kct_check_errors": summary.get("errors"),
        "kct_check_warnings": summary.get("warnings"),
        "kct_check_by_rule": dict(sorted(by_rule.items())),
        "kct_check_report": str(report_path),
    }


def run_lvs(sch: Path, routed: Path, evidence: Path) -> dict:
    from kicad_tools.lvs import write_lvs_report

    copper_clean, label_clean = write_lvs_report(sch, routed, evidence, require_clean=False)
    return {
        "lvs_copper_clean": bool(copper_clean),
        "lvs_label_clean": bool(label_clean),
        "lvs_report": str(evidence / "lvs.json"),
    }


def grade(routed: Path, sch: Path) -> dict:
    """Every gate on *routed*; each gate's failure to run is recorded, not raised."""
    from kicad_tools.benchmark.external.metrics import measure_completion

    evidence = routed.parent / "evidence"
    evidence.mkdir(exist_ok=True)
    comp = measure_completion(routed).to_dict()
    row: dict = {
        "nets_complete": comp["nets_complete"],
        "nets_total": comp["nets_total"],
        "connections_routed": comp["connections_routed"],
        "connections_total": comp["connections_total"],
    }
    row.update(run_kicad_drc(routed, evidence))  # environment refusal propagates
    row.update(run_kicad_erc(sch, evidence))
    row.update(run_kct_check(routed, evidence))
    try:
        row.update(run_lvs(sch, routed, evidence))
    except Exception as exc:  # recorded; gate_failures sees "did not run"
        row["lvs_error"] = f"{type(exc).__name__}: {exc}"
    with contextlib.suppress(Exception):
        from kicad_tools.schema.pcb import PCB

        row["via_count"] = len(PCB.load(str(routed)).vias)
    return row


def run_seed(harness, generated: Path, work_dir: Path, seed: int, timeout: float) -> dict:
    run_dir = work_dir / f"seed-{seed}"
    inp = stage_seed(generated, run_dir)
    routed = run_dir / ROUTED_NAME
    row: dict = {"seed": seed, "flags": route_flags(seed)}
    with contextlib.suppress(OSError):
        row["loadavg_start"] = [round(x, 2) for x in os.getloadavg()]
    row.update(harness.run_route(inp, routed, route_flags(seed), timeout, run_dir / "route.log"))
    log_facts = harness.parse_route_log((run_dir / "route.log").read_text(errors="replace"))
    row.update(
        {
            k: log_facts[k]
            for k in (
                "router_nets",
                "hit_timeout",
                "cache_hit",
                "oracle_unverified",
                "oracle_unverified_reason",
                "sweep_wall_clock",
                "rescue_wall_clock",
                "coarse_grid_mm",
            )
            if k in log_facts
        }
    )
    artifact, kind = harness.pick_artifact(routed, row.get("exit_code"))
    if artifact is None:
        row["error"] = f"no routed output (exit_code={row.get('exit_code')})"
    else:
        row["routed"] = str(artifact)
        row["artifact"] = kind
        row.update(grade(artifact, run_dir / SCH_NAME))
    with contextlib.suppress(OSError):
        row["loadavg_end"] = [round(x, 2) for x in os.getloadavg()]
    return row


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------


def render(rows: list[dict]) -> str:
    head = (
        "| Seed | Nets | kicad-cli DRC err | Unconnected | ERC | kct check err | LVS "
        "| Vias | Wall s | sweep wall clock | Oracle | Verdict |\n"
        "|---|---|---|---|---|---|---|---|---|---|---|---|"
    )
    lines = [head]
    for row in rows:
        failures = gate_failures(row)
        lvs = (
            "clean"
            if row.get("lvs_copper_clean") and row.get("lvs_label_clean")
            else ("dirty" if "lvs_copper_clean" in row else "--")
        )
        lines.append(
            "| {seed} | {nets} | {drc} | {unc} | {erc} | {kct} | {lvs} | {vias} | {wall} "
            "| {sweep} | {oracle} | {verdict} |".format(
                seed=row.get("seed", "--"),
                nets=f"{row.get('nets_complete', '--')}/{row.get('nets_total', '--')}",
                drc=row.get("kicad_drc_errors", "--"),
                unc=row.get("kicad_drc_unconnected", "--"),
                erc=row.get("kicad_erc_violations", "--"),
                kct=row.get("kct_check_errors", "--"),
                lvs=lvs,
                vias=row.get("via_count", "--"),
                wall=row.get("wall_s", "--"),
                sweep={True: "live", False: "no"}.get(row.get("sweep_wall_clock"), "--"),
                oracle="UNVERIFIED" if row.get("oracle_unverified") else "ok",
                verdict="PASS" if not failures else "FAIL: " + "; ".join(failures),
            )
        )
    return "\n".join(lines)


def _publish(rows: list[dict], json_path: Path | None) -> None:
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps({"rows": rows}, indent=2) + "\n")
    table = render(rows)
    print(table, flush=True)
    summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary:
        with open(summary, "a") as fh:
            fh.write("## Board 05 unattended route (issue #7834)\n\n" + table + "\n")
    for row in rows:
        for failure in gate_failures(row):
            print(f"::error::board 05 seed {row.get('seed', '?')}: {failure}")


def parse_seeds(text: str) -> list[int]:
    seeds = [int(s) for s in text.split(",") if s.strip()]
    if not seeds:
        raise argparse.ArgumentTypeError("at least one seed is required")
    return seeds


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument(
        "--seeds", type=parse_seeds, default=list(DEFAULT_SEEDS), help="comma list of seeds"
    )
    ap.add_argument("--work-dir", type=Path, required=True)
    ap.add_argument("--json", type=Path, help="write per-seed rows here")
    ap.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT_S, help="kct route --timeout")
    ap.add_argument("--grade-only", type=Path, help="grade this routed PCB; route nothing")
    ap.add_argument("--sch", type=Path, help="schematic for --grade-only (default: sibling)")
    args = ap.parse_args(argv)

    from kicad_tools.cli.kicad_toolchain import KiCadEnvironmentError, evidence_toolchain

    args.work_dir = args.work_dir.resolve()
    args.work_dir.mkdir(parents=True, exist_ok=True)
    mounts = [REPO, args.work_dir]
    if args.grade_only is not None:
        args.grade_only = args.grade_only.resolve()
        mounts.append(args.grade_only.parent)
    rows: list[dict] = []
    try:
        with evidence_toolchain(mounts):
            if args.grade_only is not None:
                sch = (args.sch or args.grade_only.parent / SCH_NAME).resolve()
                row = {"seed": "grade-only", "routed": str(args.grade_only)}
                row.update(grade(args.grade_only, sch))
                rows.append(row)
            else:
                harness = _load_harness()
                generated = args.work_dir / "generated"
                if generated.exists():
                    shutil.rmtree(generated)
                started = time.monotonic()
                generate_unrouted(generated)
                print(f"generated unrouted board in {time.monotonic() - started:.1f}s", flush=True)
                for seed in args.seeds:
                    print(f"==> board 05 unattended route, seed {seed}", flush=True)
                    row = run_seed(harness, generated, args.work_dir, seed, args.timeout)
                    rows.append(row)
                    print(json.dumps(row), flush=True)
                    _write_partial(rows, args.json)
    except KiCadEnvironmentError as exc:
        print(f"REFUSED (KiCad environment, Issue #6962): {exc}", file=sys.stderr)
        return 2
    _publish(rows, args.json)
    return exit_code(rows)


def _write_partial(rows: list[dict], json_path: Path | None) -> None:
    if json_path is not None:
        json_path.parent.mkdir(parents=True, exist_ok=True)
        json_path.write_text(json.dumps({"rows": rows}, indent=2) + "\n")


if __name__ == "__main__":
    raise SystemExit(main())
