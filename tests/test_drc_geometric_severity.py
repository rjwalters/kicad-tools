"""Severity-aware native geometric DRC parsing (issue #4497)."""

from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.drc.geometric import run_geometric_drc


@pytest.mark.parametrize("refill", [True, False])
def test_run_geometric_drc_preserves_error_gate_and_counts_warnings(monkeypatch, refill):
    """Warnings are exposed without changing the existing error-only fields."""
    import kicad_tools.drc as drc_package
    import kicad_tools.drc.geometric as geometric

    captured: dict[str, list[str]] = {}

    def fake_run(cmd, **_kwargs):
        captured["cmd"] = list(cmd)
        # Issue #6724: the report must be one kicad-cli actually wrote.
        Path(cmd[cmd.index("--output") + 1]).write_text('{"violations": []}')
        return SimpleNamespace(returncode=0)

    warning = SimpleNamespace(type_str="copper_sliver", is_error=False)
    error = SimpleNamespace(type_str="clearance", is_error=True)
    monkeypatch.setattr(geometric.subprocess, "run", fake_run)
    # Issue #6724: model a kicad-cli that supports --refill-zones (KiCad 10+).
    monkeypatch.setattr("kicad_tools.cli.runner._kicad_drc_supports_refill", lambda _cli: True)
    monkeypatch.setattr(
        drc_package.DRCReport,
        "load",
        lambda _path: SimpleNamespace(violations=[warning, error], unconnected_items=[]),
    )

    result = run_geometric_drc(
        Path("board.kicad_pcb"), kicad_cli=Path("/usr/bin/kicad-cli"), refill_zones=refill
    )

    assert ("--refill-zones" in captured["cmd"]) is refill
    assert "--severity-all" in captured["cmd"]
    assert "--severity-error" not in captured["cmd"]
    assert result.ran
    assert result.error_count == 1
    assert result.by_type == {"clearance": 1}
    assert result.all_by_type == {"copper_sliver": 1, "clearance": 1}
