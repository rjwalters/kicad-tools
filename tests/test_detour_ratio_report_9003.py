"""Detour-ratio report: metric core + non-perturbation guards (issue #9003).

The report's load-bearing contract is that the ``--report-detour-ratio``
probe is READ-ONLY: it runs the same per-net search the real route runs,
against the pristine pre-routing grid, and must leave no trace on the live
run -- routed output byte-identical with and without the flag.  A probe that
perturbs the search (journal entries, failure records, a cached sub-problem
solution served to the real run) would silently change copper and make the
whole report untrustworthy, so that identity is pinned here at both the CLI
level (negotiated driver, the ``kct route`` default) and the plain
``route_all`` driver, alongside the metric-core unit tests.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.analysis.detour_ratio import (
    DETOUR_RATIO_THRESHOLD,
    DetourConnectionRecord,
    detour_ratio_payload,
    format_detour_report,
    percentile,
    summarize_detour_records,
)
from kicad_tools.cli import route_cmd

# --- synthetic board ---------------------------------------------------------


def _pad(number: str, x: float, y: float, net: int, net_name: str) -> str:
    return (
        f'(pad "{number}" smd rect (at {x} {y}) (size 0.4 0.4) '
        f'(layers "F.Cu" "F.Paste" "F.Mask") (net {net} "{net_name}"))'
    )


def _fp(ref: str, uid: int, x: float, y: float, pads: str) -> str:
    return f"""  (footprint "Resistor_SMD:R_0603_1608Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-0000000000{uid:02d}")
    (at {x} {y})
    (property "Reference" "{ref}" (at 0 -1.5 0) (layer "F.SilkS"))
    (property "Value" "10k" (at 0 1.5 0) (layer "F.Fab"))
    {pads}
  )
"""


def _board(nets: int = 4) -> str:
    """A few point-to-point nets packed side by side (congestion for rip-up).

    Corridor pitch is chosen tight enough that the negotiated driver has to
    negotiate, but loose enough that the board routes to completion -- the
    byte-identity test needs a routed board on both runs.
    """
    parts: list[str] = []
    nets_decl = ['(net 0 "")']
    edge_y0 = 100.0
    pitch = 2.0
    for i in range(1, nets + 1):
        y = edge_y0 + 4.0 + i * pitch
        nets_decl.append(f'(net {i} "/N{i}")')
        parts.append(_fp(f"RA{i}", 10 + 2 * i, 103.0, y, _pad("1", 0, 0, i, f"/N{i}")))
        parts.append(_fp(f"RB{i}", 11 + 2 * i, 127.0, y, _pad("1", 0, 0, i, f"/N{i}")))
    return f"""(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general
    (thickness 1.6)
  )
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup
    (pad_to_mask_clearance 0)
  )
  {chr(10).join(nets_decl)}
  (gr_rect (start 100 100) (end 130 {edge_y0 + 4.0 + (nets + 1) * pitch + 2})
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
{"".join(parts)})
"""


# --- metric core -------------------------------------------------------------


class TestMetricCore:
    def test_ratio_arithmetic(self) -> None:
        rec = DetourConnectionRecord(
            net_name="N1", connection_index=0, drawn_mm=25.0, alone_mm=10.0
        )
        assert rec.ratio == pytest.approx(2.5)
        assert rec.is_offender
        assert not DetourConnectionRecord(
            net_name="N1", connection_index=0, drawn_mm=10.0, alone_mm=10.0
        ).is_offender

    def test_zero_and_none_alone_cost_yield_no_ratio(self) -> None:
        zero = DetourConnectionRecord(net_name="N1", connection_index=0, drawn_mm=5.0, alone_mm=0.0)
        none = DetourConnectionRecord(
            net_name="N1", connection_index=0, drawn_mm=5.0, alone_mm=None
        )
        assert zero.ratio is None and not zero.is_offender
        assert none.ratio is None and not none.is_offender

    def test_percentile_edges_n1_n2(self) -> None:
        assert percentile([7.0], 0.99) == pytest.approx(7.0)
        assert percentile([], 0.5) is None
        assert percentile([1.0, 2.0], 0.99) == pytest.approx(2.0)
        assert percentile([1.0, 2.0], 0.5) == pytest.approx(1.0)
        assert percentile([3.0, 1.0, 2.0], 0.9) == pytest.approx(3.0)

    def test_worst_ordering_stable_and_deterministic(self) -> None:
        recs = [
            DetourConnectionRecord(net_name="B", connection_index=0, drawn_mm=30.0, alone_mm=10.0),
            DetourConnectionRecord(net_name="A", connection_index=1, drawn_mm=30.0, alone_mm=10.0),
            DetourConnectionRecord(net_name="A", connection_index=0, drawn_mm=25.0, alone_mm=10.0),
            DetourConnectionRecord(net_name="C", connection_index=0, drawn_mm=10.0, alone_mm=10.0),
        ]
        first = summarize_detour_records(recs)
        again = summarize_detour_records(list(reversed(recs)))
        keys1 = [(r.net_name, r.connection_index) for r in first.worst]
        keys2 = [(r.net_name, r.connection_index) for r in again.worst]
        # A#1 and B#0 tie at 3.0x; the tie-break is (net_name, index), then
        # A#0 at 2.5x trails.  Order is identical regardless of input order.
        assert keys1 == keys2 == [("A", 1), ("B", 0), ("A", 0)]
        assert first.max == pytest.approx(3.0)

    def test_worst_limit_caps_rows(self) -> None:
        recs = [
            DetourConnectionRecord(
                net_name=f"N{i}", connection_index=0, drawn_mm=20.0, alone_mm=10.0
            )
            for i in range(20)
        ]
        summary = summarize_detour_records(recs)
        assert summary.offender_count == 20
        assert len(summary.worst) == 10

    def test_probe_no_path_counted_not_dropped(self) -> None:
        recs = [
            DetourConnectionRecord(net_name="N1", connection_index=0, alone_mm=10.0, drawn_mm=10.0),
            DetourConnectionRecord(
                net_name="N2", connection_index=0, alone_mm=None, probe_no_path=True
            ),
        ]
        summary = summarize_detour_records(recs)
        assert summary.probed == 2
        assert summary.no_path == 1
        assert summary.matched == 1
        report = format_detour_report(summary)
        assert "probe-no-path" in report and "no path on the empty board" in report

    def test_unmatched_excluded_from_distribution(self) -> None:
        recs = [
            DetourConnectionRecord(net_name="N1", connection_index=0, alone_mm=10.0, drawn_mm=10.0),
            DetourConnectionRecord(net_name="N2", connection_index=1, alone_mm=5.0, unmatched=True),
        ]
        summary = summarize_detour_records(recs)
        assert summary.unmatched == 1
        assert summary.matched == 1
        assert summary.max == pytest.approx(1.0)

    def test_all_ratio_one_no_offenders_no_division_error(self) -> None:
        recs = [
            DetourConnectionRecord(net_name="N1", connection_index=0, alone_mm=10.0, drawn_mm=10.0)
        ]
        summary = summarize_detour_records(recs)
        assert summary.offender_count == 0
        assert summary.threshold == pytest.approx(DETOUR_RATIO_THRESHOLD)
        report = format_detour_report(summary)
        assert "offenders (> 1.25x alone-cost): 0" in report

    def test_payload_shape_round_trips_every_row(self) -> None:
        recs = [
            DetourConnectionRecord(net_name="N1", connection_index=0, alone_mm=10.0, drawn_mm=12.6),
            DetourConnectionRecord(
                net_name="N2", connection_index=0, alone_mm=None, probe_no_path=True
            ),
        ]
        payload = detour_ratio_payload(summarize_detour_records(recs), recs)
        assert payload["schema"] == "kct-detour-ratio/1"
        assert len(payload["connections"]) == 2
        assert payload["connections"][1]["probe_no_path"] is True
        json.dumps(payload)  # must be JSON-serializable

    def test_empty_report_does_not_crash(self) -> None:
        summary = summarize_detour_records([])
        text = format_detour_report(summary)
        assert "no measurable connections" in text


# --- probe integration -------------------------------------------------------


def _route_signature(router) -> list:
    """Engine-level route geometry, for with/without-flag equality."""

    def layer_of(layer):
        return getattr(layer, "value", str(layer))

    return [
        (
            route.net,
            tuple(
                (s.x1, s.y1, s.x2, s.y2, layer_of(s.layer), round(s.width, 6))
                for s in route.segments
            ),
            tuple((v.x, v.y, v.net) for v in route.vias),
        )
        for route in router.routes
    ]


@pytest.fixture
def board_path(tmp_path: Path) -> Path:
    pcb = tmp_path / "congested.kicad_pcb"
    pcb.write_text(_board(nets=4))
    return pcb


class TestNonPerturbation:
    def test_cli_negotiated_output_byte_identical_with_and_without_flag(
        self, board_path: Path, tmp_path: Path, capsys
    ) -> None:
        """The AC that makes the report trustworthy: identical routed bytes.

        Runs the default CLI path (grid engine, negotiated driver) twice on
        the same synthetic board with a fixed --seed (deterministic UUIDs);
        only one run passes --report-detour-ratio.  The written boards must
        be byte-identical and the sidecar must exist on the flagged run
        only.
        """
        out_on = tmp_path / "on.kicad_pcb"
        out_off = tmp_path / "off.kicad_pcb"
        base = [str(board_path), "-o", "--strategy", "basic", "--skip-drc", "--seed", "7"]
        argv_on = [base[0], "-o", str(out_on), *base[2:], "--report-detour-ratio"]
        argv_off = [base[0], "-o", str(out_off), *base[2:]]

        rc_on = route_cmd.main(argv_on)
        rc_off = route_cmd.main(argv_off)
        assert rc_on == rc_off

        assert out_on.exists() and out_off.exists()
        assert out_on.read_bytes() == out_off.read_bytes()
        assert (tmp_path / "on.detour_ratio.json").exists()
        assert not (tmp_path / "off.detour_ratio.json").exists()
        captured = capsys.readouterr().out
        assert "Detour ratio" in captured
        payload = json.loads((tmp_path / "on.detour_ratio.json").read_text())
        assert payload["schema"] == "kct-detour-ratio/1"
        assert payload["summary"]["probed"] >= 4

    def test_plain_route_all_identical_and_journal_untouched(self, board_path: Path) -> None:
        """Plain ``route_all`` driver: identical copper, identical journal."""
        from kicad_tools.router.io import load_pcb_for_routing

        router_on, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router_off, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router_on.report_detour_ratio = True

        routes_on = router_on.route_all(suppress_no_timeout_warning=True)
        routes_off = router_off.route_all(suppress_no_timeout_warning=True)

        assert _route_signature(router_on) == _route_signature(router_off)
        assert len(routes_on) == len(routes_off)
        # The probe must not add, remove or reorder journal records.
        assert len(router_on.commit_journal) == len(router_off.commit_journal)
        assert [(rec.kind, rec.net, rec.pass_name) for rec in router_on.commit_journal] == [
            (rec.kind, rec.net, rec.pass_name) for rec in router_off.commit_journal
        ]
        # The probe ran on the flagged router only.
        assert router_on.detour_probe_records
        assert router_off.detour_probe_records is None
        # Every probe row measured an alone cost (this board routes fully).
        assert all(
            rec.alone_mm is not None or rec.probe_no_path for rec in router_on.detour_probe_records
        )

    def test_negotiated_driver_identical_with_ripup_budget(self, board_path: Path) -> None:
        """Negotiated driver with targeted rip-up: probe leaves no trace.

        ``use_targeted_ripup`` activates the rip-up machinery; a probe
        record leaking into negotiated state (history, nogood cache,
        search failures) would show up as a copper difference here.
        """
        from kicad_tools.router.io import load_pcb_for_routing

        router_on, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router_off, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router_on.report_detour_ratio = True

        kwargs = {
            "max_iterations": 4,
            "per_net_timeout": 10.0,
            "timeout": 60.0,
            "use_targeted_ripup": True,
        }
        router_on.route_all_negotiated(**kwargs)
        router_off.route_all_negotiated(**kwargs)

        assert _route_signature(router_on) == _route_signature(router_off)
        assert len(router_on.commit_journal) == len(router_off.commit_journal)
        # No probe failure may surface in the search-failures sidecar store.
        store_on = getattr(router_on.router, "search_failures_by_net", None)
        store_off = getattr(router_off.router, "search_failures_by_net", None)
        if isinstance(store_on, dict) and isinstance(store_off, dict):
            assert dict(store_on) == dict(store_off)

    def test_finalize_pairs_drawn_lengths(self, board_path: Path) -> None:
        from kicad_tools.router.io import load_pcb_for_routing

        router, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router.report_detour_ratio = True
        router.route_all_negotiated(max_iterations=4, per_net_timeout=10.0, timeout=60.0)
        records = router.detour_probe_records
        assert records
        router.finalize_detour_probe_records()
        # Every row ends measured, explicitly unmatched, or explicitly no-path.
        for rec in records:
            if rec.probe_no_path:
                assert rec.alone_mm is None
            else:
                assert rec.drawn_mm is not None or rec.unmatched
        summary = summarize_detour_records(
            records, unattributed_drawn_mm=router._detour_unattributed_drawn_mm
        )
        assert summary.probed == len(records)

    def test_finalize_matches_by_path_ends_not_list_position(self, board_path: Path) -> None:
        """An untagged escape-stub hop beside a full connection must not
        steal the pairing (board 04's 0.8mm hops once paired against 25mm
        probe rows by list index and reported 0.03x ratios)."""
        from kicad_tools.router.io import load_pcb_for_routing
        from kicad_tools.router.primitives import Route, Segment

        router, _ = load_pcb_for_routing(str(board_path), strategy="grid")
        router.report_detour_ratio = True
        router.route_all_negotiated(max_iterations=4, per_net_timeout=10.0, timeout=60.0)
        router.finalize_detour_probe_records()
        records = router.detour_probe_records
        assert records

        # Synthesize the pathology: prepend, for every net, a 0.8mm hop
        # route whose ends sit 0.4mm from the net's first probe endpoint
        # (i.e. right at a pad).  Position-based pairing would hand these
        # hops the first probe row of each net; endpoint matching must
        # still pair each probe row to the real connection, pushing the
        # hop into the unattributed bucket.
        from kicad_tools.router.layers import Layer

        hops = []
        for rec in records:
            if rec.endpoints is None:
                continue
            (x, y), _ = rec.endpoints
            hop = Route(net=0, net_name=rec.net_name)
            hop.segments = [
                Segment(x1=x, y1=y, x2=x + 0.8, y2=y, layer=Layer.F_CU, width=0.2, net=0)
            ]
            hops.append(hop)
        router.routes = hops + list(router.routes)
        for rec in records:
            rec.drawn_mm = None
            rec.unmatched = False
        router.finalize_detour_probe_records()
        for rec in records:
            if rec.endpoints is not None and not rec.probe_no_path:
                assert rec.drawn_mm is not None, f"{rec.net_name} lost its pairing"
        assert router._detour_unattributed_drawn_mm > 0.0

    def test_route_path_ends_and_pair_distance(self) -> None:
        from kicad_tools.analysis.detour_ratio import (
            endpoint_pair_distance,
            route_path_ends,
        )
        from kicad_tools.router.layers import Layer
        from kicad_tools.router.primitives import Route, Segment

        route = Route(net=1, net_name="N")
        route.segments = [
            Segment(x1=0.0, y1=0.0, x2=10.0, y2=0.0, layer=Layer.F_CU, width=0.2, net=1)
        ]
        ends = route_path_ends(route)
        assert ends == ((0.0, 0.0), (10.0, 0.0))
        assert route_path_ends(Route(net=1, net_name="N")) is None
        reversed_ends = ((10.0, 0.0), (0.0, 0.0))
        assert endpoint_pair_distance(ends, reversed_ends) == pytest.approx(0.0)
        assert endpoint_pair_distance(ends, ((0.0, 3.0), (10.0, 4.0))) == pytest.approx(7.0)

    def test_off_by_default_writes_no_sidecar_and_prints_nothing(
        self, board_path: Path, tmp_path: Path, capsys
    ) -> None:
        out = tmp_path / "plain.kicad_pcb"
        rc = route_cmd.main(
            [str(board_path), "-o", str(out), "--strategy", "basic", "--skip-drc", "--seed", "7"]
        )
        assert rc == 0
        captured = capsys.readouterr().out
        assert "Detour ratio" not in captured
        assert not (tmp_path / "plain.detour_ratio.json").exists()


class TestSidecarContract:
    def test_blocked_sidecar_write_warns_and_does_not_raise(
        self, tmp_path: Path, monkeypatch, caplog
    ) -> None:
        stub = type(
            "StubRouter",
            (),
            {
                "detour_probe_records": [
                    DetourConnectionRecord(net_name="N1", connection_index=0, alone_mm=10.0)
                ],
                "finalize_detour_probe_records": lambda self: setattr(
                    self.detour_probe_records[0], "drawn_mm", 12.6
                ),
            },
        )()

        def boom(*args, **kwargs):
            raise OSError("disk full")

        monkeypatch.setattr(Path, "write_text", boom)

        # Must not raise: a blocked write is a warning, never a failed route.
        route_cmd._report_detour_ratio(tmp_path / "out.kicad_pcb", stub)
        assert any("detour-ratio sidecar" in rec.message for rec in caplog.records)
        # And the stdout report still printed.
        assert stub.detour_probe_records[0].drawn_mm == pytest.approx(12.6)

    def test_report_is_noop_without_records(self, tmp_path: Path) -> None:
        # No records -> returns before touching the filesystem.
        route_cmd._report_detour_ratio(tmp_path / "out.kicad_pcb", object())
        assert not (tmp_path / "out.detour_ratio.json").exists()
