"""Issue #4799: the placement-only pad-field escape-capacity forecast.

The earlier slices of #4799 (#4852 report, #4862 replay, #4865 gate) all rest on
a measurement taken *during* a previous route.  This module pins the first
predictor on this issue that needs no prior run at all: ring depth, inter-pad
channels and the pin count that must leave a footprint are properties of the
placement, so the forecast is exact on the first route of a brand-new board.

Sections:

1. The primitives -- ``channels_in_gap`` boundaries, onion-peel ``ring_depths``
   (a two-row connector must NOT be modelled as an array), and the interstitial
   ``via_site_clearance`` (a dogbone goes diagonally between four pads, not
   through the orthogonal gap -- getting this wrong flips a healthy BGA into a
   false "cannot reach an inner layer").
2. The ring-cut model -- demand/supply per cut, and the three verdicts that
   matter: ``infeasible`` (no channel at all), ``over-capacity`` (more pins than
   channels) and ``ample``.
3. What is *not* demand -- internal-only nets, and poured pins, which are
   deferred **only** where a via drop actually exists.
4. The document + human block, including "not applicable" staying distinct from
   a clean bill of health.
5. ``emit_forecast`` never raising, and the ``kct route`` wiring: both parsers
   carry the flags, the shim forwards them only when set, and the preflight
   cannot change an exit code.

Everything but the two file-based tests runs on fabricated :class:`FieldPad`
arrays, so this file costs milliseconds -- the point of a leading indicator.
"""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from kicad_tools.router.capacity_forecast import (
    MIN_PADS_FOR_FIELD,
    OVER_CAPACITY_RATIO,
    REPORT_KIND,
    SCHEMA_VERSION,
    TIGHT_RATIO,
    VERDICT_AMPLE,
    VERDICT_INFEASIBLE,
    VERDICT_NOT_APPLICABLE,
    VERDICT_OVER_CAPACITY,
    CapacityForecastError,
    FieldPad,
    build_forecast,
    channels_in_gap,
    emit_forecast,
    forecast_from_board,
    ring_depths,
    via_site_clearance,
    write_report,
)

# ---------------------------------------------------------------------------
# Fixtures: fabricated pad fields
# ---------------------------------------------------------------------------


def grid_field(
    rows: int,
    cols: int,
    pitch: float,
    pad: float,
    *,
    ref: str = "U1",
    net_of=None,
) -> list[FieldPad]:
    """A ``rows x cols`` grid array centred on the origin.

    ``net_of(row, col)`` names each pad's net; by default every pad gets its
    own net so demand is maximal.
    """
    pads: list[FieldPad] = []
    for row in range(rows):
        for col in range(cols):
            name = net_of(row, col) if net_of else f"N_{row}_{col}"
            pads.append(
                FieldPad(
                    x=col * pitch,
                    y=row * pitch,
                    width=pad,
                    height=pad,
                    ref=ref,
                    net_name=name,
                )
            )
    return pads


def sinks(nets, ref: str = "J9") -> list[FieldPad]:
    """One off-field pad per net, so every net counts as *leaving* the field."""
    return [
        FieldPad(x=100.0 + i, y=100.0, width=1.0, height=1.0, ref=ref, net_name=net)
        for i, net in enumerate(sorted(set(nets)))
    ]


def field_and_sinks(rows, cols, pitch, pad, **kwargs):
    field = grid_field(rows, cols, pitch, pad, **kwargs)
    return field + sinks(p.net_name for p in field)


# ---------------------------------------------------------------------------
# 1. Primitives
# ---------------------------------------------------------------------------


class TestChannelsInGap:
    def test_one_track_needs_clearance_both_sides(self):
        # 0.2 track + 0.2 clearance either side = 0.6mm of clear channel.
        assert channels_in_gap(0.59, 0.2, 0.2) == 0
        assert channels_in_gap(0.60, 0.2, 0.2) == 1

    def test_second_track_needs_a_further_track_plus_clearance(self):
        assert channels_in_gap(0.99, 0.2, 0.2) == 1
        assert channels_in_gap(1.00, 0.2, 0.2) == 2

    def test_gap_at_or_below_clearance_carries_nothing(self):
        assert channels_in_gap(0.2, 0.2, 0.2) == 0
        assert channels_in_gap(0.0, 0.2, 0.2) == 0
        assert channels_in_gap(-1.0, 0.2, 0.2) == 0

    def test_degenerate_rules_do_not_raise(self):
        assert channels_in_gap(1.0, 0.0, 0.0) == 0


class TestRingDepths:
    def test_square_grid_peels_into_rings(self):
        pads = grid_field(7, 7, 1.27, 0.45)
        depths = ring_depths([(p.x, p.y) for p in pads], 1.27)
        assert max(depths) == 3  # 7x7 -> 4 rings
        assert sum(1 for d in depths if d == 0) == 24  # perimeter
        assert sum(1 for d in depths if d >= 1) == 25  # interior
        centre = depths[3 * 7 + 3]
        assert centre == 3

    def test_two_row_connector_is_all_boundary(self):
        # Every pad of a dual-row header has open sky on one side, so the
        # model must NOT treat it as an array with an interior (a bbox-inset
        # rule gets this wrong and invents interior pins).
        pads = grid_field(2, 12, 0.5, 0.3)
        depths = ring_depths([(p.x, p.y) for p in pads], 0.5)
        assert set(depths) == {0}

    def test_single_row_is_all_boundary(self):
        pads = grid_field(1, 8, 0.8, 0.4)
        assert set(ring_depths([(p.x, p.y) for p in pads], 0.8)) == {0}

    def test_empty_and_degenerate_inputs(self):
        assert ring_depths([], 1.0) == []
        assert ring_depths([(0.0, 0.0)], 1.0) == [0]
        assert ring_depths([(0.0, 0.0), (1.0, 0.0)], 0.0) == [0, 0]


class TestViaSiteClearance:
    def test_dogbone_site_is_diagonal_not_orthogonal(self):
        # 1.27mm grid, 0.45mm square pads: the orthogonal gap leaves only
        # 0.41mm of half-clearance, but the diagonal interstice leaves ~0.58mm
        # -- which is the difference between "no via can be dropped here" and
        # the dogbone every BGA in the world uses.
        pads = grid_field(5, 5, 1.27, 0.45)
        clear = via_site_clearance(pads, 1.27)
        assert clear == pytest.approx(0.58, abs=0.02)
        assert clear > 0.5 * (1.27 - 0.45)

    def test_fine_pitch_array_has_no_room(self):
        pads = grid_field(5, 5, 0.5, 0.25)
        assert via_site_clearance(pads, 0.5) < 0.25

    def test_degenerate_inputs(self):
        assert via_site_clearance([], 1.0) == 0.0
        assert via_site_clearance(grid_field(2, 2, 1.0, 0.4), 0.0) == 0.0


# ---------------------------------------------------------------------------
# 2. The ring-cut model
# ---------------------------------------------------------------------------


class TestRingCutModel:
    def test_coarse_bga_with_a_dogbone_site_is_ample(self):
        # 7x7 @ 1.27mm, 0.6mm vias: a via fits in the diagonal interstice, so
        # the interior reaches the inner layers and the ring is not binding.
        forecast = build_forecast(
            field_and_sinks(7, 7, 1.27, 0.45),
            signal_layers=4,
            track_width_mm=0.2,
            clearance_mm=0.2,
            via_diameter_mm=0.6,
        )
        assert forecast.applicable
        field = forecast.worst_field
        assert field is not None
        assert field.rings == 4
        assert field.via_drop_available is True
        assert forecast.verdict == VERDICT_AMPLE
        assert forecast.worst_ratio < 1.0

    def test_same_array_without_a_via_drop_is_over_capacity(self):
        # Two layers and a via too big for the interstice: every interior pin
        # has to squeeze through the outer ring's 24 single-track gaps.
        forecast = build_forecast(
            field_and_sinks(7, 7, 1.27, 0.45),
            signal_layers=2,
            track_width_mm=0.2,
            clearance_mm=0.2,
            via_diameter_mm=1.0,
        )
        field = forecast.worst_field
        assert field is not None
        assert field.via_drop_available is False
        cut = field.worst_cut
        assert cut is not None
        assert cut.depth == 1
        assert cut.demand == 25
        assert cut.gaps == 24
        assert cut.channels_per_gap == 1
        assert cut.supply == 24
        assert cut.ratio == pytest.approx(25 / 24)
        assert forecast.verdict == VERDICT_OVER_CAPACITY
        assert cut.ratio >= OVER_CAPACITY_RATIO

    def test_via_in_pad_capability_reopens_the_inner_layers(self):
        # Oversized pads (0.9mm on a 1.27mm pitch) leave neither a surface
        # channel nor an interstice big enough for a 0.6mm via, so the field is
        # sealed -- until the fab tier allows the via to sit *in* the pad.
        sealed = build_forecast(
            field_and_sinks(7, 7, 1.27, 0.9),
            signal_layers=4,
            via_diameter_mm=0.6,
        )
        assert sealed.worst_field is not None
        assert sealed.worst_field.via_drop_available is False
        assert sealed.verdict == VERDICT_INFEASIBLE

        rescued = build_forecast(
            field_and_sinks(7, 7, 1.27, 0.9),
            signal_layers=4,
            via_diameter_mm=0.6,
            via_in_pad=True,
        )
        assert rescued.worst_field is not None
        assert rescued.worst_field.via_drop_available is True
        assert rescued.worst_field.drop_channels_per_gap == 1
        assert rescued.verdict == VERDICT_AMPLE

    def test_fine_pitch_bga_is_infeasible_not_merely_tight(self):
        # 0.5mm pitch, 0.25mm pads: no track fits between pads and no via fits
        # in the interstice, so interior pins have nowhere to go on any layer.
        forecast = build_forecast(
            field_and_sinks(8, 8, 0.5, 0.25),
            signal_layers=4,
            track_width_mm=0.1,
            clearance_mm=0.1,
            via_diameter_mm=0.45,
        )
        field = forecast.worst_field
        assert field is not None
        assert field.surface_channels_per_gap == 0
        assert field.via_drop_available is False
        cut = field.worst_cut
        assert cut is not None and cut.supply == 0 and cut.demand > 0
        assert forecast.verdict == VERDICT_INFEASIBLE

    def test_worst_cut_is_the_binding_one(self):
        forecast = build_forecast(
            field_and_sinks(7, 7, 1.27, 0.45),
            signal_layers=2,
            via_diameter_mm=1.0,
        )
        field = forecast.worst_field
        assert field is not None
        ratios = [cut.ratio for cut in field.cuts]
        assert field.worst_ratio == max(ratios)
        # Cuts are emitted outermost-first and demand falls monotonically.
        assert [cut.depth for cut in field.cuts] == sorted(cut.depth for cut in field.cuts)
        demands = [cut.demand for cut in field.cuts]
        assert demands == sorted(demands, reverse=True)

    def test_thresholds_bracket_the_tight_band(self):
        assert 0.0 < TIGHT_RATIO < OVER_CAPACITY_RATIO


# ---------------------------------------------------------------------------
# 3. What is not demand
# ---------------------------------------------------------------------------


class TestDemandDefinition:
    def test_nets_that_never_leave_the_footprint_are_not_demand(self):
        # Same pads, but every net is internal to the field: nothing has to
        # cross a ring, so the field is not modelled at all.
        field = grid_field(7, 7, 1.27, 0.45)
        forecast = build_forecast(field, signal_layers=2, via_diameter_mm=1.0)
        assert forecast.fields_modelled == 0
        assert forecast.verdict == VERDICT_NOT_APPLICABLE

    def test_poured_pins_are_deferred_when_a_via_drop_exists(self):
        pads = field_and_sinks(
            7, 7, 1.27, 0.45, net_of=lambda r, c: "GND" if (r + c) % 2 else f"S_{r}_{c}"
        )
        forecast = build_forecast(pads, signal_layers=4, via_diameter_mm=0.6, pour_nets={"GND"})
        field = forecast.worst_field
        assert field is not None
        assert field.via_drop_available is True
        assert field.pour_pads_deferred > 0
        cut = field.worst_cut
        assert cut is not None
        assert cut.demand < 25  # the GND interior pins dropped straight down

    def test_poured_pins_still_count_when_no_via_can_be_placed(self):
        # A pour net is only "free" if the pad can actually reach the plane.
        pads = field_and_sinks(
            7, 7, 1.27, 0.45, net_of=lambda r, c: "GND" if (r + c) % 2 else f"S_{r}_{c}"
        )
        forecast = build_forecast(pads, signal_layers=4, via_diameter_mm=1.0, pour_nets={"GND"})
        field = forecast.worst_field
        assert field is not None
        assert field.via_drop_available is False
        assert field.pour_pads_deferred == 0
        assert field.worst_cut is not None and field.worst_cut.demand == 25

    def test_small_footprints_are_never_modelled(self):
        pads = [
            FieldPad(x=i * 1.0, y=0.0, width=0.4, height=0.4, ref="R1", net_name=f"N{i}")
            for i in range(MIN_PADS_FOR_FIELD - 1)
        ]
        forecast = build_forecast(pads + sinks(p.net_name for p in pads))
        assert forecast.fields_modelled == 0

    def test_perimeter_only_package_is_not_applicable(self):
        # A QFN escapes outward from every pad; there is no ring to cross.
        pads = []
        for i in range(12):
            pads.append(
                FieldPad(x=i * 0.5, y=0.0, width=0.3, height=0.3, ref="U9", net_name=f"A{i}")
            )
            pads.append(
                FieldPad(x=i * 0.5, y=6.0, width=0.3, height=0.3, ref="U9", net_name=f"B{i}")
            )
        forecast = build_forecast(pads + sinks(p.net_name for p in pads))
        assert forecast.applicable is False
        assert forecast.verdict == VERDICT_NOT_APPLICABLE


# ---------------------------------------------------------------------------
# 4. Document + human block
# ---------------------------------------------------------------------------


class TestDocument:
    def _forecast(self):
        return build_forecast(
            field_and_sinks(7, 7, 1.27, 0.45),
            source="board.kicad_pcb",
            signal_layers=2,
            via_diameter_mm=1.0,
        )

    def test_envelope_matches_the_machine_output_convention(self):
        doc = self._forecast().to_dict()
        assert doc["schema_version"] == SCHEMA_VERSION
        assert doc["report"] == REPORT_KIND
        assert doc["source"] == "board.kicad_pcb"
        assert "generated_at" in doc
        assert doc["rules"]["signal_layers"] == 2
        assert doc["summary"]["verdict"] == VERDICT_OVER_CAPACITY
        assert doc["summary"]["worst_ref"] == "U1"
        assert doc["summary"]["worst_ratio"] == pytest.approx(1.042, abs=0.001)
        assert doc["fields"][0]["cuts"][0]["depth"] == 1

    def test_infinite_ratio_serialises_as_null(self):
        doc = build_forecast(
            field_and_sinks(8, 8, 0.5, 0.25),
            signal_layers=2,
            track_width_mm=0.1,
            clearance_mm=0.1,
            via_diameter_mm=0.45,
        ).to_dict()
        assert doc["summary"]["verdict"] == VERDICT_INFEASIBLE
        assert doc["summary"]["worst_ratio"] is None
        assert doc["fields"][0]["cuts"][0]["ratio"] is None
        json.dumps(doc)  # must stay serialisable

    def test_human_block_is_tagged_and_advisory(self):
        text = self._forecast().format_human()
        assert all(line.startswith("[capacity-forecast]") for line in text.splitlines())
        assert "ADVISORY ONLY" in text
        assert "U1" in text
        assert "FIX LAYER" in text

    def test_not_applicable_is_not_a_clean_bill_of_health(self):
        text = build_forecast([], source="empty.kicad_pcb").format_human()
        assert "NOT APPLICABLE" in text
        assert "not an 'ample' result" in text
        assert "ADVISORY ONLY" in text

    def test_write_report_creates_parents_and_sorts_keys(self, tmp_path):
        target = tmp_path / "nested" / "forecast.json"
        written = write_report(target, self._forecast())
        assert written == target
        payload = target.read_text()
        doc = json.loads(payload)
        assert doc["report"] == REPORT_KIND
        assert list(doc) == sorted(doc)


# ---------------------------------------------------------------------------
# 5. Board loading, emit, and the ``kct route`` wiring
# ---------------------------------------------------------------------------


def _synthetic_board(tmp_path, rows=7, cols=7, pitch=1.27, pad=0.45):
    """A minimal but real ``.kicad_pcb`` holding one grid array."""
    pads = []
    nets = []
    n = 0
    for row in range(rows):
        for col in range(cols):
            n += 1
            nets.append(f"N{n}")
            pads.append(
                f'    (pad "{n}" smd rect (at {col * pitch:.3f} {row * pitch:.3f}) '
                f'(size {pad} {pad}) (layers "F.Cu") (net {n} "N{n}"))'
            )
    # A second footprint gives every net a pad outside the array, so all of
    # them count as leaving it.
    sink_pads = [
        f'    (pad "{i + 1}" smd rect (at {i * 1.0:.3f} 0) (size 0.6 0.6) '
        f'(layers "F.Cu") (net {i + 1} "N{i + 1}"))'
        for i in range(len(nets))
    ]
    net_decls = "\n".join(f'  (net {i + 1} "N{i + 1}")' for i in range(len(nets)))
    text = f"""(kicad_pcb (version 20221018) (generator test)
  (general (thickness 1.6))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup)
  (net 0 "")
{net_decls}
  (footprint "Test:BGA" (layer "F.Cu") (at 50 50)
    (fp_text reference "U1" (at 0 -6) (layer "F.SilkS"))
{chr(10).join(pads)}
  )
  (footprint "Test:SINK" (layer "F.Cu") (at 120 50)
    (fp_text reference "J9" (at 0 -6) (layer "F.SilkS"))
{chr(10).join(sink_pads)}
  )
)
"""
    board = tmp_path / "synthetic.kicad_pcb"
    board.write_text(text)
    return board


class TestBoardLoading:
    def test_forecast_from_board_models_the_array(self, tmp_path):
        board = _synthetic_board(tmp_path)
        forecast = forecast_from_board(board, signal_layers=2, via_diameter_mm=1.0)
        assert forecast.footprints_seen == 2
        assert forecast.fields_modelled == 1
        field = forecast.worst_field
        assert field is not None and field.ref == "U1" and field.rings == 4
        assert forecast.verdict == VERDICT_OVER_CAPACITY

    def test_missing_board_raises_the_module_error(self, tmp_path):
        with pytest.raises(CapacityForecastError):
            forecast_from_board(tmp_path / "nope.kicad_pcb")

    def test_emit_forecast_prints_and_writes(self, tmp_path, capsys):
        board = _synthetic_board(tmp_path)
        report = tmp_path / "out" / "forecast.json"
        forecast = emit_forecast(board, report_path=report, signal_layers=2, via_diameter_mm=1.0)
        assert forecast is not None
        err = capsys.readouterr().err
        assert "[capacity-forecast]" in err
        assert "report written to" in err
        assert json.loads(report.read_text())["report"] == REPORT_KIND

    def test_emit_forecast_never_raises_on_a_bad_board(self, tmp_path, capsys):
        assert emit_forecast(tmp_path / "missing.kicad_pcb") is None
        assert "no forecast" in capsys.readouterr().err

    def test_emit_forecast_survives_an_unwritable_report_path(self, tmp_path, capsys):
        board = _synthetic_board(tmp_path)
        blocked = tmp_path / "file.txt"
        blocked.write_text("not a directory")
        forecast = emit_forecast(board, report_path=blocked / "forecast.json")
        assert forecast is not None  # the forecast itself still succeeded
        assert "report NOT written" in capsys.readouterr().err


class TestRouteWiring:
    def test_outer_parser_accepts_and_forwards_the_flags(self):
        """`kct route` parses with the OUTER parser and shells the inner one.

        Adding the flags to ``route_cmd.py`` alone leaves ``kct route
        --capacity-forecast`` dying with "unrecognized arguments" -- the #4862
        lesson, restated for this pair.
        """
        from kicad_tools.cli import route_cmd
        from kicad_tools.cli.commands import routing
        from kicad_tools.cli.parser import create_parser

        captured: list[list[str]] = []

        def fake_main(argv):
            captured.append(list(argv))
            return 0

        original = route_cmd.main
        route_cmd.main = fake_main  # imported inside run_route_command
        try:
            args = create_parser().parse_args(
                [
                    "route",
                    "board.kicad_pcb",
                    "--capacity-forecast",
                    "--capacity-forecast-json",
                    "f.json",
                ]
            )
            assert args.capacity_forecast is True
            assert args.capacity_forecast_json == "f.json"
            assert routing.run_route_command(args) == 0

            plain = create_parser().parse_args(["route", "board.kicad_pcb"])
            assert plain.capacity_forecast is False
            assert plain.capacity_forecast_json is None
            assert routing.run_route_command(plain) == 0
        finally:
            route_cmd.main = original

        with_flags, without_flags = captured
        assert "--capacity-forecast" in with_flags
        assert with_flags[with_flags.index("--capacity-forecast-json") + 1] == "f.json"
        assert "--capacity-forecast" not in without_flags
        assert "--capacity-forecast-json" not in without_flags

    def test_inner_parser_accepts_the_flags(self):
        import argparse
        from unittest.mock import patch

        from kicad_tools.cli.route_cmd import main as route_main

        captured: dict[str, argparse.Namespace] = {}
        real_parse_args = argparse.ArgumentParser.parse_args

        def fake_parse_args(self, *args, **kwargs):
            if getattr(self, "prog", "") == "kicad-tools route":
                captured["args"] = real_parse_args(self, *args, **kwargs)
                raise SystemExit(0)
            return real_parse_args(self, *args, **kwargs)

        with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
            with pytest.raises(SystemExit):
                route_main(
                    [
                        "board.kicad_pcb",
                        "--capacity-forecast",
                        "--capacity-forecast-json",
                        "f.json",
                    ]
                )
        assert captured["args"].capacity_forecast is True
        assert captured["args"].capacity_forecast_json == "f.json"

    def test_preflight_is_silent_when_unarmed(self, tmp_path, capsys):
        from kicad_tools.cli.route_cmd import _capacity_forecast_preflight

        board = _synthetic_board(tmp_path)
        args = SimpleNamespace(capacity_forecast=False, capacity_forecast_json=None)
        assert _capacity_forecast_preflight(board, args) is None
        assert capsys.readouterr().err == ""

    def test_preflight_prints_but_cannot_fail_a_route(self, tmp_path, capsys):
        from kicad_tools.cli.route_cmd import _capacity_forecast_preflight

        board = _synthetic_board(tmp_path)
        args = SimpleNamespace(
            capacity_forecast=True,
            capacity_forecast_json=None,
            layers="2",
            trace_width=0.2,
            clearance=0.2,
            via_diameter=1.0,
            manufacturer=None,
        )
        # No return value at all: the caller has nothing to turn into an exit
        # code, which is what "advisory in every case" means here.
        assert _capacity_forecast_preflight(board, args) is None
        err = capsys.readouterr().err
        assert "[capacity-forecast]" in err
        assert "over-capacity" in err

    def test_preflight_swallows_a_broken_board(self, tmp_path, capsys):
        from kicad_tools.cli.route_cmd import _capacity_forecast_preflight

        broken = tmp_path / "broken.kicad_pcb"
        broken.write_text("(this is not a board")
        args = SimpleNamespace(
            capacity_forecast=True,
            capacity_forecast_json=None,
            layers="auto",
            trace_width=None,
            clearance=0.15,
            via_diameter=0.6,
            manufacturer=None,
        )
        assert _capacity_forecast_preflight(broken, args) is None
        assert "[capacity-forecast]" in capsys.readouterr().err
