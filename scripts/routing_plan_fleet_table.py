#!/usr/bin/env python3
"""Fleet precision/recall table for the routing plan (Issue #5521).

Epic #5510 Phase 1's honest instrument.  For each routed board it reads two
artifacts that already exist -- the ``<stem>.routing_plan.json`` sidecar
``kct route`` wrote and a ``kct net-status --strict --format json`` dump of
the same routed board -- and prints one markdown row saying how well the
plan's predicted congestion matched the board's actual opens:

``recall``
    Of the nets that really ended unrouted, the fraction crossing at least
    one overflowed corridor.  Low recall means the plan did not see the
    congestion that actually stopped the router.
``precision``
    Of the overflowed corridors, the fraction crossed by at least one
    unrouted net.  Low precision means the plan cried wolf.

**It does not route.**  Each row's ``kct route`` argv is documented in
``docs/reference/routing-plan.md`` so anyone can reproduce the inputs; this
script only measures what those runs left behind, which is what keeps it
cheap enough to re-run and impossible to accidentally turn into a board
regeneration.

Usage::

    python scripts/routing_plan_fleet_table.py DIR [DIR ...] [--sha SHA]

Each ``DIR`` is a routed-output directory holding exactly one
``*.routing_plan.json`` plus either a ``net_status.json`` dump or the routed
``.kicad_pcb`` itself (in which case ``kct net-status`` is invoked to
produce one).  The row label is the directory's name.
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import subprocess
import sys
from pathlib import Path

#: ``NetStatus`` values that count as "this net did not finish".
OPEN_STATUSES = frozenset({"incomplete", "unrouted"})

#: Plan net statuses excluded from the denominator: a pour net was never
#: offered to the trace router, so its open-ness is not the plan's business.
EXCLUDED_PLAN_STATUSES = frozenset({"pour_skipped"})

COLUMNS = (
    "board",
    "pcb",
    "kct SHA",
    "date",
    "plan_s",
    "relief_s",
    "total_overflow",
    "overflowed_edges",
    "feasible",
    "unrouted",
    "unrouted_crossing",
    "recall",
    "precision",
    "notes",
)


def _load_plan(directory: Path):
    from kicad_tools.router.routing_plan import RoutingPlan

    sidecars = sorted(directory.glob("*.routing_plan.json"))
    if not sidecars:
        raise SystemExit(f"{directory}: no *.routing_plan.json sidecar")
    return RoutingPlan.from_dict(json.loads(sidecars[0].read_text())), sidecars[0]


def _load_net_status(directory: Path, sidecar: Path) -> dict:
    cached = directory / "net_status.json"
    if cached.is_file():
        return json.loads(cached.read_text())
    pcb = sidecar.with_name(sidecar.name.replace(".routing_plan.json", ".kicad_pcb"))
    if not pcb.is_file():
        raise SystemExit(f"{directory}: neither net_status.json nor {pcb.name}")
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "kicad_tools.cli",
            "net-status",
            str(pcb),
            "--strict",
            "--format",
            "json",
        ],
        capture_output=True,
        text=True,
    )
    return json.loads(proc.stdout)


def _open_nets(net_status: dict, plan) -> list[str]:
    """Signal nets that did not finish AND that the plan actually planned.

    Two exclusions, both meaning "the plan makes no claim about this net, so
    it cannot be scored on it":

    - the plan filed the net as ``pour_skipped`` (Issue #5521's spec);
    - the plan has **no entry** for the net at all.  Board 04 is the case
      that forces this: it auto-pours ``+3.3V``/``GND`` instead of passing
      them to ``--skip-nets``, so they never enter the plan's net table --
      no ``pour_skipped`` row exists to exclude -- yet ``net-status``
      reports them ``incomplete`` (advisory plane residuals).  Counting
      them would put nets the global pass never saw in the recall
      denominator.
    """
    planned = {e.name: e.status for e in plan.nets.values()}
    return sorted(
        n["net_name"]
        for n in net_status.get("nets", [])
        if n.get("status") in OPEN_STATUSES
        and n["net_name"] in planned
        and planned[n["net_name"]] not in EXCLUDED_PLAN_STATUSES
    )


def _ratio(numerator: int, denominator: int) -> str:
    if denominator == 0:
        return "n/a (0/0)"
    return f"{numerator / denominator:.2f} ({numerator}/{denominator})"


def measure(directory: Path) -> dict[str, str]:
    plan, sidecar = _load_plan(directory)
    net_status = _load_net_status(directory, sidecar)
    report = plan.overflow_report
    crossings = plan.crossings_by_net_name()
    overflowed = plan.overflowed_edges()

    open_nets = _open_nets(net_status, plan)
    crossing_nets = [name for name in open_nets if crossings.get(name)]
    hit_edges = {(e.a, e.b) for name in crossing_nets for e in crossings.get(name, [])}

    return {
        "board": directory.name,
        "pcb": Path(str(plan.source.get("pcb") or sidecar)).name,
        "kct SHA": "",
        "date": "",
        "plan_s": f"{report.elapsed_s:.2f}" if report else "n/a",
        "relief_s": f"{float(plan.relief_meta.get('elapsed_s', 0.0)):.2f}",
        "total_overflow": str(report.total_overflow if report else 0),
        "overflowed_edges": str(report.overflowed_edges if report else 0),
        "feasible": str(bool(report.feasible)).lower() if report else "n/a",
        "unrouted": str(len(open_nets)),
        "unrouted_crossing": str(len(crossing_nets)),
        "recall": _ratio(len(crossing_nets), len(open_nets)),
        "precision": _ratio(len(hit_edges), len(overflowed)),
        "notes": ", ".join(open_nets) if open_nets else "",
    }


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("directories", nargs="+", type=Path)
    ap.add_argument("--sha", default="", help="kct SHA the rows were measured at")
    ap.add_argument("--date", default=_dt.date.today().isoformat())
    args = ap.parse_args(argv)

    rows = []
    for directory in args.directories:
        row = measure(directory)
        row["kct SHA"] = args.sha
        row["date"] = args.date
        rows.append(row)

    print("| " + " | ".join(COLUMNS) + " |")
    print("|" + "|".join("---" for _ in COLUMNS) + "|")
    for row in rows:
        print("| " + " | ".join(row[c] for c in COLUMNS) + " |")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
