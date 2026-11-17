#!/usr/bin/env python3
"""Regenerate the ``net-status --why`` golden fixtures (Issue #5521).

``tests/test_net_status_routing_plan_5521.py`` asserts that ``kct
net-status --why`` output is **byte-identical** to these files when no
``<stem>.routing_plan.json`` sidecar is beside the board.  That is the
contract Epic #5510 Phase 1c signs: the plan consumer is additive, so a
board with no plan must look exactly as it did before the consumer
existed.

Run this ONLY when a deliberate, reviewed change to the ``--why`` output
lands -- never to make a red golden test go green.  A diff here is the
test doing its job.

    python scripts/regen_net_status_why_golden.py
    git diff tests/fixtures/net_status_why_golden/   # review EVERY line
"""

from __future__ import annotations

import contextlib
import io
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))

GOLDEN_DIR = REPO / "tests" / "fixtures" / "net_status_why_golden"

#: The token the board's tmp path is replaced with, so the fixtures are
#: machine-independent.  Mirrored by the test's own substitution.
PLACEHOLDER = "INCOMPLETE_PCB"


def main() -> int:
    from kicad_tools.cli.net_status_cmd import main as net_status_main
    from tests.test_net_status_cmd import INCOMPLETE_PCB

    GOLDEN_DIR.mkdir(parents=True, exist_ok=True)
    workdir = Path(tempfile.mkdtemp())
    pcb = workdir / "incomplete.kicad_pcb"
    pcb.write_text(INCOMPLETE_PCB)

    for argv, name in (
        (["--why"], "incomplete.txt"),
        (["--why", "--format", "json"], "incomplete.json"),
    ):
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            net_status_main([str(pcb), *argv])
        (GOLDEN_DIR / name).write_text(buf.getvalue().replace(str(pcb), PLACEHOLDER))
        print(f"wrote {GOLDEN_DIR / name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
