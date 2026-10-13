"""Board 05 lattice route stays 33/33 and manufacturable (Issue #6449 guard).

Board 05 was the first board to meet the full bar on a fresh route: every
signal net routed, 0 errors from both kicad-cli and ``kct check``, ``kct route``
exit 0.  #6427 then grew the lattice's static pad masks by the router's own
class clearances, which walled ``GND_SENSE`` out of ``U2.6`` (32/33, exit 3)
without any fast test noticing.  The structural cause is pinned by
``tests/router/test_lattice_pad_mask_designer_floors_6449.py``; this test is
the end-to-end assertion.

It runs the issue's exact protocol through
``scripts/research/expand_route_experiment.py`` and takes 15-25 minutes, so it
is opt-in: set ``KCT_BOARD05_LATTICE_GUARD=1`` (and build the native backend
first, ``uv run kct build-native``)::

    KCT_BOARD05_LATTICE_GUARD=1 uv run pytest \\
        tests/test_board05_lattice_manufacturable_6449.py --no-cov -s

Run it before merging anything that touches lattice search geometry, pad
keep-outs, clearance floors or plane-via reservation.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
HARNESS = REPO / "scripts" / "research" / "expand_route_experiment.py"

#: The issue's protocol, verbatim.
PROTOCOL = (
    "--route-engine lattice --strategy basic --layers 4 --no-auto-layers "
    "--deterministic-budget --per-net-iterations 1000000 "
    "--no-placement-delta-feedback --seed 42 --no-cache"
)

pytestmark = [
    pytest.mark.slow,
    pytest.mark.skipif(
        os.environ.get("KCT_BOARD05_LATTICE_GUARD") != "1",
        reason="opt-in 15-25 min board route; set KCT_BOARD05_LATTICE_GUARD=1",
    ),
]


def test_board05_lattice_route_is_complete_and_manufacturable(tmp_path):
    rows_path = tmp_path / "rows.json"
    cmd = [
        sys.executable,
        str(HARNESS),
        "route",
        "--boards",
        "05",
        "--grid-policy",
        "area",
        "--scales",
        "1.0",
        "--modes",
        "uniform",
        "--seed",
        "42",
        "--timeout",
        "7200",
        f"--extra={PROTOCOL}",
        "--work-dir",
        str(tmp_path / "work"),
        "--json",
        str(rows_path),
    ]
    proc = subprocess.run(cmd, cwd=REPO, capture_output=True, text=True, timeout=7700)
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    rows = json.loads(rows_path.read_text())["rows"]
    assert len(rows) == 1, rows
    row = rows[0]
    summary = {
        k: row.get(k)
        for k in (
            "router_nets",
            "exit_code",
            "signal_nets_unfinished",
            "kct_check_errors",
            "kicad_drc_errors",
            "kicad_drc_unconnected",
            "manufacturable",
        )
    }
    assert row["router_nets"] == "33/33", summary
    assert row["exit_code"] == 0, summary
    assert row["signal_nets_unfinished"] == 0, summary
    assert row["kct_check_errors"] == 0, summary
    assert row["kicad_drc_errors"] == 0, summary
    assert row["manufacturable"] is True, summary
