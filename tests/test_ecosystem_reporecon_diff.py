"""Tests for scripts/ecosystem_reporecon_diff.py (Issue #6749). No network."""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
FIXTURE = ROOT / "tests" / "fixtures" / "reporecon_kicad_ai_sample.json"

_spec = importlib.util.spec_from_file_location(
    "ecosystem_reporecon_diff", ROOT / "scripts" / "ecosystem_reporecon_diff.py"
)
diff = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(diff)


def test_missing_repos_filters_tracked_and_threshold():
    index = json.loads(FIXTURE.read_text())
    tracked = {"mixelpixx/kicad-mcp-server"}
    got = [f"{r['owner']}/{r['repo']}" for r in diff.missing_repos(index, tracked, 5)]
    assert got == ["paul356/KiCad-AI-Assistant", "someone/edge"]


def test_main_reads_file_and_prints(capsys):
    assert diff.main(["--file", str(FIXTURE), "--min-stars", "100"]) == 0
    out = capsys.readouterr().out
    assert "paul356" not in out  # tracked by the registry as of #6749
    assert "someone" not in out
