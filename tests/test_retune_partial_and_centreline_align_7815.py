"""Issue #7815: retune keeps achieved targets; centreline meanders slide along a span.

Board 07 under ``--differential-pairs``: the DQS lane of ``DDR_DATA_BYTE_0``
has one coupled span, and the centred meander tall enough to close the group
clipped DQ4's via and DQ6's trace at the span's BGA end.  The same meander
against the span's other end is clear.  Separately, one unattainable target
made ``retune_pcb`` discard the MIPI/HDMI groups it had tuned in the same call.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from unittest.mock import patch

import pytest

from kicad_tools.router import match_group_tuning as mgt
from kicad_tools.router.core import Autorouter
from kicad_tools.router.pair_centerline_meander import centerline_meander, find_coupled_spans
from kicad_tools.router.pcb_retune import RetuneError, retune_pcb
from kicad_tools.router.primitives import Pad

sys.path.insert(0, str(Path(__file__).parent))
from test_match_group_pair_members_lattice import (  # noqa: E402
    _measure,
    _pairs_group,
    _reference_pair,
    _route,
    _tune,
)
from test_pcb_retune import ADDR, addr_map, board, digest  # noqa: E402

# ---------------------------------------------------------------------------
# Centreline meander placement along its span
# ---------------------------------------------------------------------------


def _straight_pair():
    p = _route(1, [(0.0, 0.0), (12.0, 0.0)])
    n = _route(2, [(0.0, 0.3266), (12.0, 0.3266)])
    return p, n


def _bump_x(segments) -> tuple[float, float]:
    xs = [s.x1 for s in segments if abs(s.y1) > 1e-6 or abs(s.y2) > 1e-6]
    return min(xs), max(xs)


class TestCentrelineAlign:
    def _meander(self, **kw):
        p, n = _straight_pair()
        span = find_coupled_spans(p, n, max_edge_gap_mm=0.5)[0]
        return centerline_meander(
            span,
            p.segments[span.p_index],
            n.segments[span.n_index],
            length_add_mm=2.0,
            amplitude_mm=1.0,
            leg_spacing_mm=0.4,
            side=-1,
            **kw,
        )

    def test_default_is_centred_and_reports_slack(self):
        centred = self._meander()
        explicit = self._meander(align=0.5)
        assert centred is not None and explicit is not None
        assert centred.p_segments == explicit.p_segments
        assert centred.slack_mm > 5.0
        lo, hi = _bump_x(centred.p_segments)
        assert lo - 0.0 == pytest.approx(12.0 - hi, abs=1e-6)

    def test_ends_place_the_same_meander_against_either_end(self):
        centred = self._meander()
        low, high = self._meander(align=0.0), self._meander(align=1.0)
        assert low is not None and high is not None and centred is not None
        for m in (low, high):
            assert m.added_length_mm == pytest.approx(centred.added_length_mm)
            assert m.loops == centred.loops
        lo_low, hi_low = _bump_x(low.p_segments)
        lo_high, hi_high = _bump_x(high.p_segments)
        assert lo_low < _bump_x(centred.p_segments)[0] < lo_high
        assert hi_high == pytest.approx(12.0 - lo_low, abs=1e-6)
        assert hi_low - lo_low == pytest.approx(hi_high - lo_high, abs=1e-6)


# ---------------------------------------------------------------------------
# The tuner tries the span ends only after every centred candidate failed
# ---------------------------------------------------------------------------


def _pads(dy: float) -> dict[int, list[Pad]]:
    """Two foreign pads at x=6, ``dy`` outside each rail of the pair."""
    return {
        9: [
            Pad(x=6.0, y=-dy, width=0.4, height=0.4, net=9, net_name="X", ref="J9", pin="1"),
            Pad(
                x=6.0, y=0.3266 + dy, width=0.4, height=0.4, net=9, net_name="X", ref="J9", pin="2"
            ),
        ]
    }


def _tune_lane(dy: float, alignments: tuple[float, ...] | None = None):
    p, n = _straight_pair()
    routes = {1: p, 2: n, **_reference_pair(6.0, 16.0)}
    target = _measure(routes[3])
    kw = {"pads_by_net": _pads(dy), "pad_clearance_mm": 0.2}
    if alignments is None:
        results = _tune(_pairs_group([(1, 2), (3, 4)]), routes, **kw)
    else:
        with patch.object(mgt, "PAIR_CENTERLINE_ALIGNMENTS", alignments):
            results = _tune(_pairs_group([(1, 2), (3, 4)]), routes, **kw)
    return results, target


class TestTunerSlidesMeanderAlongSpan:
    def test_centred_only_cannot_clear_the_mid_span_obstacle(self):
        results, _ = _tune_lane(0.5, alignments=(0.5,))
        assert results[1][1].reason != "tuned"

    def test_meander_against_a_span_end_reaches_the_target(self):
        results, target = _tune_lane(0.5)
        (p_route, p_res), (n_route, _) = results[1], results[2]
        assert p_res.reason == "tuned"
        assert (_measure(p_route) + _measure(n_route)) / 2.0 == pytest.approx(target, abs=0.05)
        lo, hi = _bump_x(p_route.segments)
        assert hi < 6.0 - 0.2 - 0.2  # wholly clear of the pads at x=6

    def test_a_lane_the_centred_meander_tunes_is_unchanged(self):
        """The ends are tried last, so existing outcomes keep their geometry."""
        centred, _ = _tune_lane(1.0, alignments=(0.5,))
        default, _ = _tune_lane(1.0)
        assert centred[1][1].reason == default[1][1].reason == "tuned"
        assert centred[1][0].segments == default[1][0].segments
        assert centred[2][0].segments == default[2][0].segments

    def test_failure_message_names_each_strategy(self):
        results, _ = _tune_lane(0.5, alignments=(0.5,))
        message = results[1][1].message
        assert "per-leg:" in message


# ---------------------------------------------------------------------------
# retune_pcb(keep_achieved=True)
# ---------------------------------------------------------------------------

DATA = {"D0": (30, 5, 45), "D1": (34, 5, 41)}


def _two_group_board(tmp_path: Path) -> Path:
    path = tmp_path / "in.kicad_pcb"
    path.write_text(board(ADDR | DATA))
    return path


def _two_group_map():
    from kicad_tools.router.rules import NetClassRouting

    nc = NetClassRouting(
        name="DATA",
        length_match_group="DATA_BUS",
        length_match_tolerance_mm=0.5,
        length_critical=True,
    )
    return {**addr_map(), **dict.fromkeys(DATA, nc)}


@pytest.fixture
def no_data_bus_tuning(monkeypatch):
    """The tuner leaves DATA_BUS alone, so it stays 4.0 mm over its limit."""
    real = Autorouter.apply_match_group_tuning

    def only_addr(self, groups, **kw):
        return real(self, [g for g in groups if g.name != "DATA_BUS"], **kw)

    monkeypatch.setattr(Autorouter, "apply_match_group_tuning", only_addr)


class TestKeepAchieved:
    def test_default_stays_all_or_nothing_and_carries_the_targets(
        self, tmp_path, no_data_bus_tuning
    ):
        src = _two_group_board(tmp_path)
        with pytest.raises(RetuneError) as err:
            retune_pcb(
                src,
                tmp_path / "out.kicad_pcb",
                groups=["ADDR_BUS", "DATA_BUS"],
                net_class_map=_two_group_map(),
            )
        assert err.value.code == "unattainable"
        assert not (tmp_path / "out.kicad_pcb").exists()
        by_name = {t.name: t for t in err.value.targets}
        assert by_name["ADDR_BUS"].achieved and not by_name["DATA_BUS"].achieved

    def test_achieved_target_is_written_and_failure_reported(self, tmp_path, no_data_bus_tuning):
        src = _two_group_board(tmp_path)
        before = digest(src)
        out = tmp_path / "out.kicad_pcb"
        report = retune_pcb(
            src,
            out,
            groups=["ADDR_BUS", "DATA_BUS"],
            net_class_map=_two_group_map(),
            keep_achieved=True,
        )
        assert digest(src) == before
        assert out.is_file() and digest(out) == report.output_sha256
        assert report.status == "partial"
        by_name = {t.name: t for t in report.targets}
        addr, data = by_name["ADDR_BUS"], by_name["DATA_BUS"]
        assert addr.achieved and addr.status == "tuned" and addr.after_skew_mm <= 0.5
        assert not data.achieved and data.status == "unattainable"
        # Measured on the written board: DATA_BUS is as routed.
        assert data.before_skew_mm == pytest.approx(4.0)
        assert data.after_skew_mm == pytest.approx(4.0)
        assert report.changed_nets == ["A1", "A2", "A3"]
        prov = json.loads(Path(report.provenance_path).read_text())
        assert prov["status"] == "partial"
        assert prov["settings"]["unattainable_targets"] == ["DATA_BUS"]

    def test_nothing_achievable_still_raises(self, tmp_path, monkeypatch):
        monkeypatch.setattr(Autorouter, "apply_match_group_tuning", lambda self, *a, **k: {})
        src = _two_group_board(tmp_path)
        with pytest.raises(RetuneError) as err:
            retune_pcb(
                src,
                tmp_path / "out.kicad_pcb",
                groups=["ADDR_BUS", "DATA_BUS"],
                net_class_map=_two_group_map(),
                keep_achieved=True,
            )
        assert err.value.code == "unattainable"
        assert "ADDR_BUS" in err.value.message and "DATA_BUS" in err.value.message
        assert not (tmp_path / "out.kicad_pcb").exists()


class TestLimitingMemberDetail:
    def test_unattainable_message_names_the_short_members(self, tmp_path, monkeypatch):
        """The failure says which members stopped short, and the tuner's reason."""
        from kicad_tools.router.match_group_tuning import TuneResult

        def stuck(self, groups, **kw):
            (group,) = groups
            per_net = {}
            for nid in group.net_ids:
                name = self.net_names[nid]
                y, x0, x1 = ADDR[name]
                per_net[nid] = (
                    next(r for r in self.routes if r.net == nid),
                    TuneResult(
                        success=name == "A0",
                        reason="reference" if name == "A0" else "no_suitable_segment",
                        length_before_mm=x1 - x0,
                        length_after_mm=x1 - x0,
                        message=f"no host on {name}",
                    ),
                )
            return {group.name: per_net}

        monkeypatch.setattr(Autorouter, "apply_match_group_tuning", stuck)
        src = tmp_path / "in.kicad_pcb"
        src.write_text(board(ADDR))
        with pytest.raises(RetuneError) as err:
            retune_pcb(
                src, tmp_path / "out.kicad_pcb", groups=["ADDR_BUS"], net_class_map=addr_map()
            )
        (target,) = err.value.targets
        assert "A0" not in target.tuner_detail.split("[")[0]  # the reference is not a limiter
        for name in ("A1", "A2", "A3"):
            assert f"no host on {name}" in target.tuner_detail
        assert "[no_suitable_segment]" in target.tuner_detail
        assert "limited by" in err.value.message and "no host on A3" in err.value.message
