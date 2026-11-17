#!/usr/bin/env python3
"""Write the fleet 3D model coverage report (issue #7682).

Audits the routed PCB of every ``boards/NN-*`` board under both the ``host``
and ``pinned`` resolution environments and writes ``model-coverage.json``.
This is a report, not a gate: it exits 0 whatever the coverage, and nonzero
only when it cannot write its output.

Usage: model_coverage_report.py [--boards-dir boards] [-o model-coverage.json]
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from kicad_tools.pcb.model_audit import audit_pcb_models


def _board_pcb(board: Path) -> Path | None:
    out = board / "output"
    routed = sorted(out.glob("*_routed.kicad_pcb"))
    plain = sorted(p for p in out.glob("*.kicad_pcb") if not p.name.endswith("_routed.kicad_pcb"))
    return (routed or plain or [None])[0]


def build_report(boards_dir: Path) -> dict:
    boards: dict[str, dict] = {}
    for board in sorted(boards_dir.glob("[0-9][0-9]-*")):
        pcb = _board_pcb(board)
        if pcb is None:
            boards[board.name] = {"error": "no PCB found"}
            continue
        entry: dict = {"pcb": str(pcb)}
        for env in ("host", "pinned"):
            try:
                report = audit_pcb_models(pcb, env=env)
            except Exception as exc:  # one bad board must not hide the rest
                entry[env] = {"error": f"{type(exc).__name__}: {exc}"}
                continue
            entry[env] = report.summary()
            if env == "host":
                entry["gaps"] = [g.to_dict() for g in report.gaps]
                entry["suspects"] = [s.to_dict() for s in report.suspects]
        boards[board.name] = entry
    return {"schema_version": 1, "boards": boards}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--boards-dir", default="boards", type=Path)
    parser.add_argument("-o", "--output", default="model-coverage.json", type=Path)
    args = parser.parse_args(argv)
    try:
        args.output.write_text(json.dumps(build_report(args.boards_dir), indent=2) + "\n")
    except OSError as exc:
        print(f"error: cannot write {args.output}: {exc}", file=sys.stderr)
        return 1
    print(f"wrote {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
