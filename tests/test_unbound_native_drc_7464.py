"""Every tracked ``native-drc*.json`` under ``boards/*/output/`` has an owner (Issue #7464).

A DRC report that no producer regenerates and ``readiness.json`` does not bind
goes stale silently (board 04's top-level ``output/native-drc.json`` sat dated
2026-09-10 next to the live copies) and misleads anyone who reads it.  Each one
must be either bound by its board's ``readiness.json`` or a ``drc_targets``
entry that ``refresh_readiness.repin_staged`` keeps current.

``output/readiness-verification/`` is excluded: ``kct readiness --verify`` owns
and rewrites that tree wholesale.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REFRESH = REPO / "scripts" / "boards" / "refresh_readiness.py"


def _tracked_reports() -> list[Path]:
    out = subprocess.run(
        ["git", "ls-files", "-z", "--", "boards/*/output/*native-drc*.json"],
        cwd=REPO,
        check=True,
        capture_output=True,
        text=True,
    ).stdout
    paths = [Path(p) for p in out.split("\0") if p]
    return [p for p in paths if "readiness-verification" not in p.parts]


def _producer_targets() -> dict[str, set[str]]:
    spec = importlib.util.spec_from_file_location("_refresh_readiness_7464", REFRESH)
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    finally:
        sys.modules.pop(spec.name, None)
    return {name: set(cfg.drc_targets) for name, cfg in module.BOARDS.items()}


def _bound_by_readiness(board: str, rel: str) -> bool:
    readiness = REPO / "boards" / board / "output" / "readiness.json"
    if not readiness.exists():
        return False
    data = json.loads(readiness.read_text())
    bound: set[str] = set(data.get("inputs", {})) | set(data.get("report_sources", {}))
    bound |= {v for v in data.get("evidence", {}).values() if isinstance(v, str)}
    return rel in bound


def test_every_tracked_native_drc_report_is_bound_or_produced() -> None:
    reports = _tracked_reports()
    assert reports, "glob matched nothing; the check would pass vacuously"
    targets = _producer_targets()
    orphans = []
    for report in reports:
        board = report.parts[1]
        rel = Path(*report.parts[2:]).as_posix()
        if rel in targets.get(board, set()) or _bound_by_readiness(board, rel):
            continue
        orphans.append(report.as_posix())
    assert not orphans, (
        "tracked native DRC report(s) neither bound by readiness.json nor a "
        f"refresh_readiness drc_targets entry (delete or bind them): {orphans}"
    )
