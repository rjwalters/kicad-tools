"""Inline length-match gate uses the enforced skew limit (issue #7767)."""

from __future__ import annotations

from types import SimpleNamespace

from kicad_tools.router.diffpair_routing import DiffPairRouter
from kicad_tools.router.rules import NetClassRouting


def _pair(max_length_delta=0.5):
    return SimpleNamespace(
        rules=SimpleNamespace(max_length_delta=max_length_delta),
        positive=SimpleNamespace(net_name="D_P"),
        negative=SimpleNamespace(net_name="D_N"),
    )


def _tolerance(net_class_map, pair):
    router = DiffPairRouter.__new__(DiffPairRouter)
    router.autorouter = SimpleNamespace(net_class_map=net_class_map)
    return router._inline_match_tolerance(pair)


def _ncm(**kw):
    nc = NetClassRouting(name="HS", **kw)
    return {"D_P": nc, "D_N": nc}


def test_enforced_error_limit_tightens_the_gate():
    """A 0.235 mm delta must exceed a 0.05 mm enforced limit (old gate: 0.5)."""
    tol = _tolerance(_ncm(skew_tolerance_mm=0.05), _pair())
    assert tol == 0.05
    assert tol < 0.235


def test_looser_checker_limit_keeps_router_default():
    assert _tolerance(_ncm(skew_tolerance_mm=2.0), _pair(0.5)) == 0.5


def test_no_class_uses_generic_default_not_looser_than_router():
    assert _tolerance({}, _pair(0.1)) == 0.1


def test_route_gate_enters_length_match_above_the_enforced_limit(monkeypatch, capsys):
    """Through ``route_differential_pair_coupled``: a delta under ``max_length_delta``
    but above the enforced skew limit must reach the length-match block."""
    from tests.test_coupled_candidate_selection import _run, _tight

    p, n = _tight()
    n.segments[-1].x2 += 0.2  # N is 0.2 mm longer than P
    monkeypatch.setattr(DiffPairRouter, "_checker_skew_limit_mm", lambda self, pair: 0.05)
    *_, out = _run(monkeypatch, capsys, (p, n), None)
    assert "Length mismatch" in out or "above the enforced skew limit" in out
