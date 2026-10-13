"""Boards 05 and 04 lattice routes stay complete and manufacturable (#6449, #6451).

Board 05 was the first board to meet the full bar on a fresh route: every
signal net routed, 0 errors from both kicad-cli and ``kct check``, ``kct route``
exit 0.  #6427 then grew the lattice's static pad masks by the router's own
class clearances, which walled ``GND_SENSE`` out of ``U2.6`` (32/33, exit 3)
without any fast test noticing.  The structural cause is pinned by
``tests/router/test_lattice_pad_mask_designer_floors_6449.py``; this test is
the end-to-end assertion.  Board 04 (``--layers 2``, 9/9) regressed the same
way (Issue #6451: ``OSC_IN`` / ``OSC_OUT`` / ``SWDIO`` unrouted, exit 2).

It runs the issue's exact protocol through
``scripts/research/expand_route_experiment.py``.

Where it runs (Issue #6463):

* **Board 04** (~40 s) is ``slow`` only, so it runs in the twice-daily
  ``slow-tests.yml`` job (``-m slow``) and on every ``-m slow`` run locally.
* **Board 05** (15-25 min) is additionally opt-in: set
  ``KCT_LATTICE_BOARD_GUARD=1``.  ``slow-tests.yml`` sets it only on its
  weekly (Sunday) schedule.

Build the native backend first (``uv run kct build-native``), then::

    KCT_LATTICE_BOARD_GUARD=1 uv run pytest \\
        tests/test_lattice_boards_manufacturable_6449.py -m slow --no-cov -s

Run it before merging anything that touches lattice search geometry, pad
keep-outs, clearance floors or plane-via reservation.

Each case's harness ``--timeout`` (plus the subprocess grace) expires before
that case's pytest-timeout, so a slow route fails with its summary dict
instead of a bare pytest-timeout traceback.
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

#: ``subprocess.run`` timeout = harness ``--timeout`` + this grace (grading,
#: kicad-cli DRC, ``kct check`` run after the route budget).
SUBPROCESS_GRACE_S = 240

#: pytest-timeout for board 05; overrides the slow job's CLI ``--timeout=1200``.
BOARD05_PYTEST_TIMEOUT_S = 3000

#: The slow job's CLI ``--timeout`` (``.github/workflows/slow-tests.yml``).
SLOW_JOB_PYTEST_TIMEOUT_S = 1200

_BOARD05_OPT_IN = pytest.mark.skipif(
    os.environ.get("KCT_LATTICE_BOARD_GUARD") != "1",
    reason=(
        "board 05 lattice route takes 15-25 min; opt in with "
        "KCT_LATTICE_BOARD_GUARD=1 (runs weekly in slow-tests.yml)"
    ),
)

#: Each issue's protocol, verbatim:
#: (board, ``--extra`` flags, expected tally, harness route timeout in s).
CASES = [
    pytest.param(
        "05",
        "--route-engine lattice --strategy basic --layers 4 --no-auto-layers "
        "--deterministic-budget --per-net-iterations 1000000 "
        "--no-placement-delta-feedback --seed 42 --no-cache",
        "33/33",
        2700,
        id="board05-4L-6449",
        marks=[_BOARD05_OPT_IN, pytest.mark.timeout(BOARD05_PYTEST_TIMEOUT_S)],
    ),
    pytest.param(
        "04",
        "--route-engine lattice --strategy basic --layers 2 --no-auto-layers "
        "--deterministic-budget --per-net-iterations 1000000 "
        "--no-placement-delta-feedback --no-cache",
        "9/9",
        900,
        id="board04-2L-6451",
    ),
]

pytestmark = [pytest.mark.slow]

SUMMARY_KEYS = (
    "router_nets",
    "exit_code",
    "signal_nets_unfinished",
    "kct_check_errors",
    "kicad_drc_errors",
    "kicad_drc_unconnected",
    "artifact",
    "manufacturable",
)


def assert_row_manufacturable(row: dict, tally: str) -> None:
    """Assert every gate of the bar individually, so a failure names its gate.

    ``manufacturable`` folds in ``kicad_drc_unconnected`` and
    ``artifact == "final"``; they are asserted directly too (Issue #6463).
    A missing kicad-cli leaves the DRC fields ``None``, which fails here.
    """
    summary = {k: row.get(k) for k in SUMMARY_KEYS}
    gates = (
        ("router_nets", tally),
        ("exit_code", 0),
        ("signal_nets_unfinished", 0),
        ("kct_check_errors", 0),
        ("kicad_drc_errors", 0),
        ("kicad_drc_unconnected", 0),
        ("artifact", "final"),
        ("manufacturable", True),
    )
    for gate, want in gates:
        got = row.get(gate)
        ok = got is want if isinstance(want, bool) else got == want
        assert ok, f"gate {gate!r}: got {got!r}, want {want!r}; {summary}"


@pytest.mark.xdist_group("lattice_board_guard")
@pytest.mark.parametrize("board,extra,tally,route_timeout_s", CASES)
def test_lattice_route_is_complete_and_manufacturable(
    tmp_path, board, extra, tally, route_timeout_s
):
    rows_path = tmp_path / "rows.json"
    cmd = [
        sys.executable,
        str(HARNESS),
        "route",
        "--boards",
        board,
        "--grid-policy",
        "area",
        "--scales",
        "1.0",
        "--modes",
        "uniform",
        "--seed",
        "42",
        "--timeout",
        str(route_timeout_s),
        f"--extra={extra}",
        "--work-dir",
        str(tmp_path / "work"),
        "--json",
        str(rows_path),
    ]
    proc = subprocess.run(
        cmd,
        cwd=REPO,
        capture_output=True,
        text=True,
        timeout=route_timeout_s + SUBPROCESS_GRACE_S,
    )
    assert proc.returncode == 0, proc.stdout[-4000:] + proc.stderr[-4000:]
    rows = json.loads(rows_path.read_text())["rows"]
    assert len(rows) == 1, rows
    assert_row_manufacturable(rows[0], tally)
