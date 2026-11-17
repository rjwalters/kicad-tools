"""Issue #8720: a rising kicad-cli rule count (any type) rolls the net back."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli.optimize_cmd import _make_drc_verifier
from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer
from tests.test_optimize_traces_opens_8632 import PCB


def _run(tmp_path: Path, verifier) -> tuple[object, str]:
    src = tmp_path / "in.kicad_pcb"
    src.write_text(PCB)
    out = tmp_path / "out.kicad_pcb"
    cfg = OptimizationConfig()
    cfg.output_verifiers["drc"] = verifier
    stats = TraceOptimizer(cfg).optimize_pcb(str(src), output_path=str(out))
    return stats, out.read_text()


def test_non_clearance_rule_rise_rolls_net_back(tmp_path: Path) -> None:
    def verify(text: str) -> dict[str, int]:
        return {"silk_overlap": 1} if text.count("(net 2)") == 1 else {"silk_overlap": 0}

    stats, out = _run(tmp_path, verify)
    assert out.count("(net 2)") == 2  # BAD restored
    assert out.count("(net 1)") == 1  # GOOD still merged
    assert stats.output_verify_rolled_back["drc"] == ["BAD"]
    assert stats.output_checks["drc"] == "ran"
    assert stats.nets_rolled_back == 1


def test_drc_verifier_unavailable_without_kicad_cli(tmp_path: Path, monkeypatch) -> None:
    import kicad_tools.cli.optimize_cmd as mod

    monkeypatch.setattr(mod, "_kicad_cli_drc_counts", lambda *a, **k: None)
    assert _make_drc_verifier(tmp_path / "b.kicad_pcb")("(kicad_pcb)") is None
    stats, _ = _run(tmp_path, _make_drc_verifier(tmp_path / "in.kicad_pcb"))
    assert stats.output_checks["drc"] == "unavailable"
