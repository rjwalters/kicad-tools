"""Issue #8632: whole-board output verification rolls back regressing nets."""

from __future__ import annotations

from pathlib import Path

from kicad_tools.router.optimizer import OptimizationConfig, TraceOptimizer

PCB = """\
(kicad_pcb
\t(version 20240108)
\t(generator "test")
\t(general (thickness 1.6))
\t(layers (0 "F.Cu" signal) (1 "In1.Cu" signal) (2 "In2.Cu" signal) (31 "B.Cu" signal) (44 "Edge.Cuts" user))
\t(setup (stackup (layer "F.Cu" (type "copper") (thickness 0.07)) (layer "dielectric 1" (type "core") (thickness 0.2))))
\t(net 0 "")
\t(net 1 "GOOD")
\t(net 2 "BAD")
\t(segment (start 10 10) (end 20 10) (width 0.25) (layer "F.Cu") (net 1) (uuid "g1"))
\t(segment (start 20 10) (end 30 10) (width 0.25) (layer "F.Cu") (net 1) (uuid "g2"))
\t(segment (start 10 20) (end 20 20) (width 0.25) (layer "F.Cu") (net 2) (uuid "b1"))
\t(segment (start 20 20) (end 30 20) (width 0.25) (layer "F.Cu") (net 2) (uuid "b2"))
)
"""


def _run(tmp_path: Path, verifier) -> tuple[object, str]:
    src = tmp_path / "in.kicad_pcb"
    src.write_text(PCB)
    out = tmp_path / "out.kicad_pcb"
    cfg = OptimizationConfig()
    if verifier is not None:
        cfg.output_verifiers["opens"] = verifier
    stats = TraceOptimizer(cfg).optimize_pcb(str(src), output_path=str(out))
    return stats, out.read_text()


def test_regressing_net_rolled_back_and_reported(tmp_path: Path) -> None:
    def verify(text: str) -> dict[str, int]:
        merged = text.count("(net 2)") == 1
        return {"BAD": 1} if merged else {}

    stats, out = _run(tmp_path, verify)
    assert out.count("(net 2)") == 2  # BAD restored
    assert out.count("(net 1)") == 1  # GOOD still merged
    assert stats.output_verify_rolled_back["opens"] == ["BAD"]
    assert stats.output_checks["opens"] == "ran"
    assert stats.nets_rolled_back == 1


def test_preexisting_problem_not_a_regression(tmp_path: Path) -> None:
    stats, out = _run(tmp_path, lambda text: {"BAD": 1})
    assert out.count("(net 2)") == 1  # merged: count unchanged vs input
    assert stats.nets_rolled_back == 0


def test_unavailable_verifier_is_reported(tmp_path: Path) -> None:
    stats, _ = _run(tmp_path, lambda text: None)
    assert stats.output_checks["opens"] == "unavailable"
