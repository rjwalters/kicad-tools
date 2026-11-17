"""Shared referee for the tscircuit-vs-kct comparison (Issue #5848).

Runs ``kicad-cli pcb drc --refill-zones`` and ``kct check`` on a board and
prints a one-line JSON summary: routing-relevant DRC counts (clearance,
shorts, hole/edge clearance, dangling copper), unconnected items, via/segment
counts. Run it on the human original, the normalized (copper-stripped) board
and each router's output so pre-existing violations (silk, courtyards,
footprint-library mismatches) can be subtracted.
"""

from __future__ import annotations

import collections
import json
import subprocess
import sys
import tempfile
from pathlib import Path

ROUTING_TYPES = (
    "clearance",
    "shorting_items",
    "hole_clearance",
    "copper_edge_clearance",
    "track_dangling",
    "via_dangling",
    "track_width",
    "via_diameter",
    "annular_width",
    "holes_co_located",
)


def drc(board: Path) -> dict:
    with tempfile.TemporaryDirectory() as td:
        out = Path(td) / "drc.json"
        subprocess.run(
            [
                "kicad-cli",
                "pcb",
                "drc",
                "--refill-zones",
                "--format",
                "json",
                "--severity-all",
                "-o",
                str(out),
                str(board),
            ],
            check=True,
            capture_output=True,
        )
        d = json.loads(out.read_text())
    c = collections.Counter(v["type"] for v in d["violations"])
    return {
        "drc_routing": {t: c[t] for t in ROUTING_TYPES if c[t]},
        "drc_routing_total": sum(c[t] for t in ROUTING_TYPES),
        "drc_other_total": sum(c.values()) - sum(c[t] for t in ROUTING_TYPES),
        "unconnected": len(d["unconnected_items"]),
    }


def kct_check(board: Path) -> dict:
    r = subprocess.run(
        ["kct", "check", str(board), "--format", "summary", "--drc-only", "--allow-incomplete"],
        capture_output=True,
        text=True,
    )
    return {
        "kct_check_exit": r.returncode,
        "kct_check_tail": (r.stdout + r.stderr).strip().splitlines()[-3:],
    }


def main(board: str) -> None:
    b = Path(board)
    text = b.read_text()
    res = {
        "board": b.name,
        "segments": text.count("(segment"),
        "vias": text.count("(via"),
        **drc(b),
        **kct_check(b),
    }
    print(json.dumps(res))


if __name__ == "__main__":
    main(sys.argv[1])
