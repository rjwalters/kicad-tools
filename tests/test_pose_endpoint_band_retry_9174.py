"""Pose endpoint lead-out + finished-rail retry under the own-net slit band (#9174).

The C++ centerline search is blind to the own-net pad slit band (#9140), so a
pose whose first trunk run skirts its own pad inside ``(0, edge)`` was found,
refused by the finished-rail verification (``derived-rails-blocked``) and the
pair fell back to unguarded single-net A* (board 06 USB2_D, which walled in
``+3V3 J1.B8``).  Two Python-side guards:

* ``_scan_endpoints`` refuses a setback whose straight trunk lead-out fails the
  caller's exact pad gate;
* a finished rail refused next to one end drops that end's endpoint and
  searches again instead of declining the pair.

These tests drive ``route_centerline_pose`` on the synthetic open-field board of
``test_diffpair_pose_centerline_5786`` and need the C++ extension.
"""

from __future__ import annotations

import math

import pytest

from kicad_tools.router import diffpair_pose
from kicad_tools.router.cpp_backend import is_cpp_available

from .test_diffpair_pose_centerline_5786 import _setup

pytestmark = pytest.mark.skipif(not is_cpp_available(), reason="C++ router extension not built")


def _accept_all(*_args) -> bool:
    return True


def test_lead_out_is_gated_by_the_exact_pad_check():
    """Every candidate endpoint is held to a straight trunk lead-out."""
    pf, (ps, pe, ns, ne) = _setup()
    seen: list[tuple] = []

    def _record(*args):
        seen.append(args)
        return True

    assert diffpair_pose.route_centerline_pose(pf, ps, pe, ns, ne, span_check=_record) is not None
    lead_outs = [
        a
        for a in seen
        if math.isclose(
            math.hypot(a[2] - a[0], a[3] - a[1]), diffpair_pose._LEAD_OUT_MM, abs_tol=1e-6
        )
    ]
    assert lead_outs, "span_check never saw a lead-out span"


def test_lead_out_in_the_band_vetoes_the_endpoint():
    """A check that refuses every lead-out leaves no legal setback."""
    pf, (ps, pe, ns, ne) = _setup()

    def _refuse_lead_out(x1, y1, x2, y2, *_rest) -> bool:
        return not math.isclose(
            math.hypot(x2 - x1, y2 - y1), diffpair_pose._LEAD_OUT_MM, abs_tol=1e-6
        )

    assert (
        diffpair_pose.route_centerline_pose(pf, ps, pe, ns, ne, span_check=_refuse_lead_out) is None
    )
    assert pf.last_pose_report["reason"] == "no-legal-setback"


def _fake_blocked_once(monkeypatch, where: tuple[float, float], times: int = 1):
    """Make ``_Ctx.first_blocked`` refuse a FINISHED rail at ``where``.

    The endpoint scan also calls ``first_blocked`` (through ``poly_clear``) on
    short end legs; only a pad-to-pad polyline is a finished rail.
    """
    real = diffpair_pose._Ctx.first_blocked
    state = {"left": times}

    def _patched(self, pts, rail):
        span = max(p[0] for p in pts) - min(p[0] for p in pts)
        if span > 10.0 and state["left"] > 0:
            state["left"] -= 1
            return where, (where[0] + 0.1, where[1])
        return real(self, pts, rail)

    monkeypatch.setattr(diffpair_pose._Ctx, "first_blocked", _patched)


def test_rail_refused_beside_the_start_pad_retries_another_endpoint(monkeypatch):
    pf, (ps, pe, ns, ne) = _setup()
    baseline = diffpair_pose.route_centerline_pose(pf, ps, pe, ns, ne, span_check=_accept_all)
    assert baseline is not None
    assert pf.last_pose_report["endpoint_retries"] == 0
    base_cost = pf.last_pose_report["start_setback_cost"]

    _fake_blocked_once(monkeypatch, (ps.x + 0.5, ps.y))
    res = diffpair_pose.route_centerline_pose(pf, ps, pe, ns, ne, span_check=_accept_all)
    assert res is not None, pf.last_pose_report
    assert pf.last_pose_report["endpoint_retries"] == 1
    # The refused start endpoint was dropped, so the search used another one.
    assert pf.last_pose_report["start_setback_cost"] != base_cost


def test_rail_refused_beside_the_goal_pad_retries_another_endpoint(monkeypatch):
    pf, (ps, pe, ns, ne) = _setup()
    _fake_blocked_once(monkeypatch, (pe.x - 0.5, pe.y))
    res = diffpair_pose.route_centerline_pose(pf, ps, pe, ns, ne, span_check=_accept_all)
    assert res is not None, pf.last_pose_report
    assert pf.last_pose_report["endpoint_retries"] == 1


def test_mid_trunk_refusal_is_not_retried(monkeypatch):
    """A segment far from both pads cannot be fixed by another endpoint."""
    pf, (ps, pe, ns, ne) = _setup()
    _fake_blocked_once(monkeypatch, ((ps.x + pe.x) / 2.0, ps.y))
    assert diffpair_pose.route_centerline_pose(pf, ps, pe, ns, ne, span_check=_accept_all) is None
    assert pf.last_pose_report["reason"] == "derived-rails-blocked"
    assert "endpoint_retries" not in pf.last_pose_report


def test_retries_are_capped_and_decline_with_the_rails_reason(monkeypatch):
    pf, (ps, pe, ns, ne) = _setup()
    _fake_blocked_once(
        monkeypatch, (ps.x + 0.5, ps.y), times=diffpair_pose._MAX_ENDPOINT_RETRIES + 5
    )
    assert diffpair_pose.route_centerline_pose(pf, ps, pe, ns, ne, span_check=_accept_all) is None
    assert pf.last_pose_report["reason"] == "derived-rails-blocked"
