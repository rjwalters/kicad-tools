"""Tests for buttress-wire reinforcement (Unit A of #4218; issue #4220).

Covers the physics wire-gauge table, the polyline chaining + arc-length
anchor walk, the clearance-refuse path, and the ``kct pcb reinforce`` CLI
integration on synthetic routed boards.
"""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path

import pytest

from kicad_tools.manufacturers.base import DesignRules
from kicad_tools.pcb.reinforce import (
    ReinforceError,
    _anchor_positions,
    _chain_polylines,
    reinforce_net,
)
from kicad_tools.physics.wire_gauge import (
    anchor_drill_for_awg,
    anchor_pad_for_drill,
    bare_copper_diameter_mm,
    supported_gauges,
    wire_ampacity,
)
from kicad_tools.schema.pcb import PCB, Segment

# --------------------------------------------------------------------------
# Physics: wire-gauge table
# --------------------------------------------------------------------------


class TestWireGauge:
    def test_bare_copper_diameters(self):
        assert bare_copper_diameter_mm(16) == pytest.approx(1.291, abs=1e-3)
        assert bare_copper_diameter_mm(14) == pytest.approx(1.628, abs=1e-3)
        assert bare_copper_diameter_mm(12) == pytest.approx(2.053, abs=1e-3)

    def test_supported_gauges(self):
        assert set(supported_gauges()) == {12, 14, 16}

    def test_unsupported_gauge_raises(self):
        with pytest.raises(ValueError, match="unsupported wire gauge"):
            bare_copper_diameter_mm(10)

    def test_anchor_drill_16awg(self):
        # 1.291 bare + 0.125 slip-fit => ~1.416 mm, within the issue's
        # ~1.40-1.45 mm band.
        drill = anchor_drill_for_awg(16)
        assert drill == pytest.approx(1.416, abs=1e-3)
        assert 1.40 <= drill <= 1.45

    def test_anchor_drill_14_12(self):
        assert 1.75 <= anchor_drill_for_awg(14) <= 1.80
        assert 2.15 <= anchor_drill_for_awg(12) <= 2.20

    def test_anchor_drill_custom_clearance(self):
        assert anchor_drill_for_awg(16, slip_fit_clearance_mm=0.0) == pytest.approx(1.291, abs=1e-3)

    def test_anchor_drill_negative_clearance_raises(self):
        with pytest.raises(ValueError, match="slip_fit_clearance_mm"):
            anchor_drill_for_awg(16, slip_fit_clearance_mm=-0.1)

    def test_anchor_pad_meets_annular_ring(self):
        drill = anchor_drill_for_awg(16)
        pad = anchor_pad_for_drill(drill, min_annular_ring_mm=0.25)
        # pad within the issue's ~1.90-1.95 mm band at 0.25 annular ring.
        assert pad == pytest.approx(1.916, abs=1e-3)
        assert 1.90 <= pad <= 1.95
        # Annular ring formula must hold.
        assert (pad - drill) / 2 == pytest.approx(0.25, abs=1e-6)

    def test_anchor_pad_sources_annular_from_rules(self):
        drill = anchor_drill_for_awg(16)
        # A larger annular-ring floor => larger pad (not hardcoded).
        pad_small = anchor_pad_for_drill(drill, 0.15)
        pad_large = anchor_pad_for_drill(drill, 0.30)
        assert pad_large > pad_small
        assert (pad_large - drill) / 2 == pytest.approx(0.30, abs=1e-6)

    def test_anchor_pad_invalid_args(self):
        with pytest.raises(ValueError):
            anchor_pad_for_drill(0.0, 0.25)
        with pytest.raises(ValueError):
            anchor_pad_for_drill(1.4, -0.1)

    def test_wire_ampacity_monotonic_and_positive(self):
        # Larger wire (lower AWG) carries more current.
        a16 = wire_ampacity(16)
        a14 = wire_ampacity(14)
        a12 = wire_ampacity(12)
        assert 0 < a16 < a14 < a12
        # Sanity magnitude for 16 AWG at a 10 C rise.
        assert 20 <= a16 <= 45

    def test_wire_ampacity_scales_with_temp_rise(self):
        assert wire_ampacity(16, temp_rise_c=20) > wire_ampacity(16, temp_rise_c=10)

    def test_wire_ampacity_invalid_temp(self):
        with pytest.raises(ValueError):
            wire_ampacity(16, temp_rise_c=0)


# --------------------------------------------------------------------------
# Fixtures: synthetic routed boards
# --------------------------------------------------------------------------


def _straight_run_pcb(net: str = "FUSED_LINE", length: float = 100.0) -> PCB:
    """A board with a single straight horizontal run on ``net``.

    Segments run left-to-right from (20, 50) to (20+length, 50) split into
    four equal pieces so chaining is exercised.
    """
    pcb = PCB.create(width=200, height=100, center=False)
    x0, y = 20.0, 50.0
    n = 4
    step = length / n
    for i in range(n):
        pcb.add_trace(
            (x0 + i * step, y),
            (x0 + (i + 1) * step, y),
            width=2.0,
            layer="F.Cu",
            net=net,
        )
    return pcb


def _branched_run_pcb(net: str = "FUSED_LINE") -> PCB:
    """A Y-junction net with a clearly-longest linear run.

    A degree-3 junction at (100, 50) fans into three linear runs; the
    junction-aware chainer splits them (it does NOT merge across the
    junction). Lengths are distinct so the longest run is unambiguous:
      - west arm:  (20,50)  -> (100,50)  = 80 mm  (longest)
      - east arm:  (100,50) -> (150,50)  = 50 mm
      - branch:    (100,50) -> (100,70)  = 20 mm
    """
    pcb = PCB.create(width=200, height=120, center=False)
    pcb.add_trace((20, 50), (100, 50), width=2.0, layer="F.Cu", net=net)
    pcb.add_trace((100, 50), (150, 50), width=2.0, layer="F.Cu", net=net)
    pcb.add_trace((100, 50), (100, 70), width=2.0, layer="F.Cu", net=net)
    return pcb


def _net_segments(pcb: PCB, net_name: str) -> list[Segment]:
    """Return segments for ``net_name`` (``add_trace`` leaves seg net_name empty)."""
    net_obj = pcb.get_net_by_name(net_name)
    assert net_obj is not None
    return [s for s in pcb.segments if s.net_number == net_obj.number]


# --------------------------------------------------------------------------
# Chaining + arc-length walk
# --------------------------------------------------------------------------


class TestChaining:
    def test_straight_run_chains_to_single_ordered_polyline(self):
        pcb = _straight_run_pcb(length=100.0)
        segs = _net_segments(pcb, "FUSED_LINE")
        runs = _chain_polylines(segs)
        assert len(runs) == 1
        run = runs[0]
        assert len(run) == 4
        # Ordered endpoint-to-endpoint.
        for a, b in zip(run, run[1:], strict=False):
            assert a.end == pytest.approx(b.start)
        total = sum(math.dist(s.start, s.end) for s in run)
        assert total == pytest.approx(100.0, abs=1e-6)

    def test_branched_net_splits_and_longest_selected(self):
        pcb = _branched_run_pcb()
        segs = _net_segments(pcb, "FUSED_LINE")
        runs = _chain_polylines(segs)
        # Junction split => the three arms are separate linear runs (the
        # chainer does NOT merge across the degree-3 junction).
        assert len(runs) == 3
        runs.sort(key=lambda r: sum(math.dist(s.start, s.end) for s in r), reverse=True)
        longest_len = sum(math.dist(s.start, s.end) for s in runs[0])
        assert longest_len == pytest.approx(80.0, abs=1e-6)

    def test_anchor_positions_endpoints_and_spacing(self):
        points = [(0.0, 0.0), (100.0, 0.0)]
        positions = _anchor_positions(points, spacing=15.0)
        xs = [p[0] for p in positions]
        # 0,15,...,90 then the final endpoint 100.
        assert xs[0] == pytest.approx(0.0)
        assert xs[-1] == pytest.approx(100.0)
        # Evenly spaced by arc length in the interior.
        interior = xs[:-1]
        for i in range(1, len(interior)):
            assert interior[i] - interior[i - 1] == pytest.approx(15.0)

    def test_anchor_positions_short_run_only_endpoints(self):
        points = [(0.0, 0.0), (5.0, 0.0)]
        positions = _anchor_positions(points, spacing=15.0)
        assert len(positions) == 2
        assert positions[0] == pytest.approx((0.0, 0.0))
        assert positions[-1] == pytest.approx((5.0, 0.0))


# --------------------------------------------------------------------------
# reinforce_net API
# --------------------------------------------------------------------------


class TestReinforceNet:
    def test_places_evenly_spaced_same_net_anchors(self):
        pcb = _straight_run_pcb(length=100.0)
        before = len(pcb.vias)
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0)

        assert result.refused_count == 0
        assert result.placed_count >= 2
        # A via was added per placed anchor.
        assert len(pcb.vias) == before + result.placed_count

        # All placed anchors carry the target net.
        net_obj = pcb.get_net_by_name("FUSED_LINE")
        assert net_obj is not None
        added = pcb.vias[before:]
        assert all(v.net_number == net_obj.number for v in added)

        # Positions evenly spaced along the run by arc length (x increases
        # monotonically; interior gaps == spacing).
        xs = sorted(a.x for a in result.placed)
        assert xs[0] == pytest.approx(20.0)
        assert xs[-1] == pytest.approx(120.0)
        interior = xs[:-1]
        for i in range(1, len(interior)):
            assert interior[i] - interior[i - 1] == pytest.approx(15.0)

    def test_anchor_dimensions_from_gauge_and_rules(self):
        pcb = _straight_run_pcb()
        rules = DesignRules(
            min_trace_width_mm=0.127,
            min_clearance_mm=0.2,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.25,
        )
        result = reinforce_net(pcb, "FUSED_LINE", wire_gauge_awg=16, design_rules=rules)
        assert result.anchor_drill_mm == pytest.approx(1.416, abs=1e-3)
        assert result.anchor_pad_mm == pytest.approx(1.916, abs=1e-3)

    def test_dry_run_places_nothing(self):
        pcb = _straight_run_pcb()
        before = len(pcb.vias)
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
        assert result.placed_count >= 2
        assert len(pcb.vias) == before  # no mutation

    def test_single_segment_net(self):
        pcb = PCB.create(width=100, height=100, center=False)
        pcb.add_trace((10, 50), (40, 50), width=2.0, layer="F.Cu", net="FUSED_LINE")
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0)
        assert result.run_segment_count == 1
        # Endpoints anchored at least.
        assert result.placed_count >= 2
        xs = sorted(a.x for a in result.placed)
        assert xs[0] == pytest.approx(10.0)
        assert xs[-1] == pytest.approx(40.0)

    def test_branched_net_anchors_longest_and_reports_branch(self):
        pcb = _branched_run_pcb()
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0)
        # Longest linear run (west arm, 80mm) anchored; the other two arms
        # reported unhandled (not silently dropped).
        assert result.run_length_mm == pytest.approx(80.0, abs=1e-6)
        assert result.unhandled_runs == 2

    def test_net_with_no_routed_copper_errors(self):
        pcb = PCB.create(width=100, height=100, center=False)
        pcb.add_net("FUSED_LINE")  # net exists but nothing routed
        with pytest.raises(ReinforceError, match="no routed copper"):
            reinforce_net(pcb, "FUSED_LINE")

    def test_unknown_net_errors(self):
        pcb = _straight_run_pcb()
        with pytest.raises(ReinforceError, match="not found"):
            reinforce_net(pcb, "DOES_NOT_EXIST")

    def test_nonpositive_spacing_errors(self):
        pcb = _straight_run_pcb()
        with pytest.raises(ReinforceError, match="spacing"):
            reinforce_net(pcb, "FUSED_LINE", spacing_mm=0.0)

    def test_idempotent_second_run_adds_no_new_anchors(self):
        pcb = _straight_run_pcb()
        reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0)
        count_after_first = len(pcb.vias)
        reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0)
        # add_via(dedupe=True) rejects the identical anchors.
        assert len(pcb.vias) == count_after_first


# --------------------------------------------------------------------------
# Clearance-refuse
# --------------------------------------------------------------------------


class TestClearanceRefuse:
    def test_refuses_anchor_on_foreign_net_drill(self):
        """A foreign-net through-hole pad where an anchor would land is refused."""
        pcb = _straight_run_pcb(length=100.0)
        # An anchor lands at x=35 (arc-length 15 from x0=20). Drop a foreign
        # through-hole pad right there so the hole-to-hole / clearance check
        # must refuse it.
        _add_th_pad_footprint(pcb, ref="TP1", x=35.0, y=50.0, net="OTHER", drill=1.0, size=2.0)

        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0)

        assert result.refused_count >= 1
        # The refused anchor is at/near the foreign pad, and NOT placed there.
        refused_pts = [(r.x, r.y) for r in result.refused]
        assert any(abs(rx - 35.0) < 0.5 and abs(ry - 50.0) < 0.5 for rx, ry in refused_pts)
        placed_pts = [(a.x, a.y) for a in result.placed]
        assert not any(abs(px - 35.0) < 0.5 and abs(py - 50.0) < 0.5 for px, py in placed_pts)

    def test_same_net_pad_does_not_refuse(self):
        """A pad on the SAME net does not trigger a refusal."""
        pcb = _straight_run_pcb(length=100.0)
        _add_th_pad_footprint(pcb, ref="TP2", x=35.0, y=50.0, net="FUSED_LINE", drill=1.0, size=2.0)
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0)
        # No refusals from same-net copper.
        assert result.refused_count == 0


def _add_th_pad_footprint(
    pcb: PCB, *, ref: str, x: float, y: float, net: str, drill: float, size: float
) -> None:
    """Append an in-memory footprint carrying one through-hole pad on ``net``.

    Built directly (no KiCad library lookup) so tests run in CI. The
    reinforce clearance check reads pads via ``pcb.footprints`` +
    ``pcb.get_pad_position`` + ``pad.net_number``/``pad.drill``, all of which
    resolve from the in-memory ``Footprint``/``Pad`` objects.
    """
    from kicad_tools.schema.pcb import Footprint, Pad

    net_obj = pcb.add_net(net)
    pad = Pad(
        number="1",
        type="thru_hole",
        shape="circle",
        position=(0.0, 0.0),  # footprint-local; the footprint sits at (x, y)
        size=(size, size),
        layers=["*.Cu"],
        net_number=net_obj.number,
        net_name=net,
        drill=drill,
    )
    fp = Footprint(
        name="TH:Pad",
        layer="F.Cu",
        position=(x, y),
        rotation=0.0,
        reference=ref,
        value="TP",
        pads=[pad],
    )
    pcb._footprints.append(fp)


# --------------------------------------------------------------------------
# CLI integration
# --------------------------------------------------------------------------


def _reinforce_args(pcb_path: Path, **overrides) -> argparse.Namespace:
    base = {
        "pcb_command": "reinforce",
        "pcb": str(pcb_path),
        "net": "FUSED_LINE",
        "wire_gauge": 16,
        "spacing": 15.0,
        "layer": None,
        "output": None,
        "format": "text",
        "dry_run": False,
    }
    base.update(overrides)
    return argparse.Namespace(**base)


class TestReinforceCLI:
    def test_cli_in_place_places_anchors(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb(length=100.0).save(pcb_path)
        before_vias = len(PCB.load(pcb_path).vias)

        rc = run_pcb_command(_reinforce_args(pcb_path))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Anchors placed:" in out
        assert "refill" in out.lower()

        after = PCB.load(pcb_path)
        assert len(after.vias) > before_vias
        # All added vias same-net.
        net_obj = after.get_net_by_name("FUSED_LINE")
        assert net_obj is not None
        assert any(v.net_number == net_obj.number for v in after.vias)

    def test_cli_output_file_leaves_input_untouched(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "in.kicad_pcb"
        out_path = tmp_path / "out.kicad_pcb"
        _straight_run_pcb(length=100.0).save(pcb_path)
        in_before = len(PCB.load(pcb_path).vias)

        rc = run_pcb_command(_reinforce_args(pcb_path, output=str(out_path)))
        assert rc == 0
        # Input unchanged; output has anchors.
        assert len(PCB.load(pcb_path).vias) == in_before
        assert out_path.exists()
        assert len(PCB.load(out_path).vias) > in_before

    def test_cli_dry_run_does_not_write(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb(length=100.0).save(pcb_path)
        before = len(PCB.load(pcb_path).vias)

        rc = run_pcb_command(_reinforce_args(pcb_path, dry_run=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert "dry run" in out.lower()
        assert len(PCB.load(pcb_path).vias) == before

    def test_cli_json_output(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb(length=100.0).save(pcb_path)

        rc = run_pcb_command(_reinforce_args(pcb_path, format="json"))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["net"] == "FUSED_LINE"
        assert data["wire_gauge_awg"] == 16
        assert data["anchors_placed"] >= 2
        assert data["anchor_drill_mm"] == pytest.approx(1.416, abs=1e-3)

    def test_cli_unknown_net_errors(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb(length=100.0).save(pcb_path)

        rc = run_pcb_command(_reinforce_args(pcb_path, net="NOPE"))
        assert rc == 1
        err = capsys.readouterr().err
        assert "not found" in err

    def test_drc_clean_after_reinforce(self, tmp_path):
        """Cross-gate: kicad-cli DRC finds no shorts on the reinforced board.

        Anchors are same-net vias, so they must not introduce shorts or
        clearance violations. Skipped when kicad-cli is unavailable.
        """
        from kicad_tools.export import find_kicad_cli

        if find_kicad_cli() is None:
            pytest.skip("kicad-cli not available")

        import subprocess

        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb(length=100.0).save(pcb_path)
        rc = run_pcb_command(_reinforce_args(pcb_path))
        assert rc == 0

        report = tmp_path / "drc.json"
        cli = find_kicad_cli()
        try:
            subprocess.run(
                [
                    cli,
                    "pcb",
                    "drc",
                    "--format",
                    "json",
                    "--output",
                    str(report),
                    str(pcb_path),
                ],
                check=False,
                capture_output=True,
                timeout=120,
            )
        except (subprocess.TimeoutExpired, OSError) as e:
            pytest.skip(f"kicad-cli drc failed to run: {e}")

        if not report.exists():
            pytest.skip("kicad-cli drc produced no report")

        data = json.loads(report.read_text())
        violations = data.get("violations", [])
        # No shorting / clearance / hole violations may be attributed to the
        # same-net anchors we placed.
        offending = [
            v
            for v in violations
            if v.get("type") in {"shorting_items", "clearance", "hole_clearance", "hole_to_hole"}
        ]
        assert not offending, f"reinforce introduced DRC violations: {offending}"

    def test_parser_registers_reinforce(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(["pcb", "reinforce", "board.kicad_pcb", "--net", "FUSED_LINE"])
        assert args.pcb_command == "reinforce"
        assert args.net == "FUSED_LINE"
        assert args.wire_gauge == 16
        assert args.spacing == 15.0
