"""Tests for buttress-wire reinforcement (Unit A of #4218; issue #4220).

Covers the physics wire-gauge table, the polyline chaining + arc-length
anchor walk, the clearance-refuse path, and the ``kct pcb reinforce`` CLI
integration on synthetic routed boards.
"""

from __future__ import annotations

import argparse
import json
import math
import re
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
from kicad_tools.router.current_paths import CurrentPathSpec, PathEndpoint
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


def _fragmented_straight_pcb(net: str = "FUSED_LINE", pieces: int = 12) -> PCB:
    """A single straight run expressed as many short collinear fragments.

    Emulates lattice/octilinear output where a straight branch is a chain of
    many short degree-2 collinear segments. The junction-aware chainer walks
    them into ONE run but leaves them as ``pieces`` separate segments; the
    tier-3 collinear merge coalesces them for reporting.
    """
    pcb = PCB.create(width=200, height=100, center=False)
    x0, y = 20.0, 50.0
    length = 96.0
    step = length / pieces
    for i in range(pieces):
        pcb.add_trace(
            (x0 + i * step, y),
            (x0 + (i + 1) * step, y),
            width=2.0,
            layer="F.Cu",
            net=net,
        )
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
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")

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
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
        assert result.run_segment_count == 1
        # Endpoints anchored at least.
        assert result.placed_count >= 2
        xs = sorted(a.x for a in result.placed)
        assert xs[0] == pytest.approx(10.0)
        assert xs[-1] == pytest.approx(40.0)

    def test_branched_net_anchors_longest_and_reports_branch(self):
        pcb = _branched_run_pcb()
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
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
            reinforce_net(pcb, "FUSED_LINE", spacing_mm=0.0, anchor_mode="spaced")

    def test_idempotent_second_run_adds_no_new_anchors(self):
        pcb = _straight_run_pcb()
        reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
        count_after_first = len(pcb.vias)
        reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
        # add_via(dedupe=True) rejects the identical anchors.
        assert len(pcb.vias) == count_after_first


# --------------------------------------------------------------------------
# Clearance-refuse
# --------------------------------------------------------------------------


class TestClearanceRefuse:
    def test_nudges_off_foreign_net_drill_then_places(self):
        """A foreign-net pad where an anchor would land triggers a bounded nudge.

        Tier-2 (#4319): rather than immediately refusing, the pass searches a
        short distance along the run's arc-length axis and places the first
        clear position. With ample clear copper on either side of the lone
        foreign pad, the anchor is placed at a nudged spot -- NOT on the pad,
        and NOT refused.
        """
        pcb = _straight_run_pcb(length=100.0)
        # An anchor lands at x=35 (arc-length 15 from x0=20). Drop a lone
        # foreign through-hole pad right there.
        _add_th_pad_footprint(pcb, ref="TP1", x=35.0, y=50.0, net="OTHER", drill=1.0, size=2.0)

        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")

        # Nudge found clear space, so nothing was refused ...
        assert result.refused_count == 0
        placed_pts = [(a.x, a.y) for a in result.placed]
        # ... no anchor sits on/near the foreign pad ...
        assert not any(abs(px - 35.0) < 2.0 and abs(py - 50.0) < 2.0 for px, py in placed_pts)
        # ... but an anchor was placed at a nudged position near it (within the
        # +/- spacing/2 search window).
        assert any(35.0 < px <= 42.5 and abs(py - 50.0) < 1e-6 for px, py in placed_pts)

    def test_hard_blocked_position_still_refuses(self):
        """A position blocked across the entire nudge window is still refused."""
        pcb = _straight_run_pcb(length=100.0)
        # A wide foreign track spanning the whole +/- spacing/2 window around
        # the x=35 anchor: every nudge candidate collides -> hard refusal.
        pcb.add_trace((27, 50), (43, 50), width=3.0, layer="F.Cu", net="OTHER")

        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")

        assert result.refused_count >= 1
        refused_pts = [(r.x, r.y) for r in result.refused]
        # The refusal is recorded at the blocked base position (x=35).
        assert any(abs(rx - 35.0) < 0.5 and abs(ry - 50.0) < 0.5 for rx, ry in refused_pts)
        # And no anchor was placed inside the blocked span.
        placed_pts = [(a.x, a.y) for a in result.placed]
        assert not any(27.0 <= px <= 43.0 and abs(py - 50.0) < 1e-6 for px, py in placed_pts)

    def test_same_net_pad_does_not_refuse(self):
        """A pad on the SAME net does not trigger a refusal."""
        pcb = _straight_run_pcb(length=100.0)
        _add_th_pad_footprint(pcb, ref="TP2", x=35.0, y=50.0, net="FUSED_LINE", drill=1.0, size=2.0)
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
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
        # --spacing defaults to None so an explicit value in ends mode can be
        # warned about; the handler applies 15.0 in spaced mode (#8742).
        assert args.spacing is None
        assert args.anchor_mode == "ends"
        assert args.end_offset == pytest.approx(1.0)
        # New flags default to backward-compatible values (#4319).
        assert args.all_runs is False
        assert args.min_run_length is None

    def test_parser_accepts_all_runs_and_min_run_length(self):
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "pcb",
                "reinforce",
                "board.kicad_pcb",
                "--net",
                "FUSED_LINE",
                "--all-runs",
                "--min-run-length",
                "25.5",
            ]
        )
        assert args.all_runs is True
        assert args.min_run_length == pytest.approx(25.5)

    def test_cli_all_runs_json_is_additive_superset(self, tmp_path, capsys):
        """`--all-runs --format json` keeps legacy keys and adds per-run detail."""
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _branched_run_pcb().save(pcb_path)

        rc = run_pcb_command(
            _reinforce_args(pcb_path, format="json", all_runs=True, anchor_mode="spaced")
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)

        # Legacy keys still present (AC-12: additive only).
        for key in (
            "run_segment_count",
            "run_length_mm",
            "unhandled_runs",
            "anchors_placed",
            "anchors_refused",
            "refused",
        ):
            assert key in data
        # New additive per-run keys.
        assert data["all_runs"] is True
        assert data["runs_total"] == 3
        assert isinstance(data["runs"], list) and len(data["runs"]) == 3
        for rs in data["runs"]:
            assert {
                "run_index",
                "length_mm",
                "segment_count",
                "anchors_needed",
                "anchors_placed",
                "anchors_refused",
            } <= set(rs)
        # An agent can read "N of M runs reinforced" programmatically.
        assert data["runs_fully_reinforced"] >= 2


def _write_single_pad_footprint_lib(tmp_path: Path) -> Path:
    """A minimal 1-pad .kicad_mod library file (issue #4980 CLI fixtures).

    Manually-appended in-memory footprints (``pcb._footprints.append``)
    never get an S-expression node, so they do NOT survive ``pcb.save()`` --
    a real library-backed footprint is required for CLI tests that load the
    saved file back (the CLI handler itself calls ``PCB.load``).
    """
    mod_path = tmp_path / "TestPad.kicad_mod"
    mod_path.write_text(
        '(footprint "TestPad" (layer "F.Cu")\n'
        '  (pad "1" thru_hole circle (at 0 0) (size 1 1) (drill 0.5) (layers "*.Cu")))\n'
    )
    return mod_path


class TestCurrentPathsCLI:
    """CLI coverage for ``--current-paths`` (reinforce) and
    ``current-paths-audit`` (issue #4980)."""

    def _build_kelvin_board(self, tmp_path: Path) -> Path:
        mod_path = _write_single_pad_footprint_lib(tmp_path)
        pcb = PCB.create(width=200, height=120, center=False)
        pcb.add_footprint_from_file(mod_path, "RSH1", 20, 50)
        pcb.add_footprint_from_file(mod_path, "J2", 120, 50)
        pcb.add_footprint_from_file(mod_path, "U3", 60, 80)
        # add_trace() creates copper but never assigns a pad to a net --
        # that has to be done explicitly for the pad's own net_name/
        # net_number to persist through save/load (issue #4980 endpoint
        # resolution reads the PAD's net, not just the routed copper's).
        for ref in ("RSH1", "J2", "U3"):
            pcb.assign_net_to_footprint_pad(ref, "1", "PGND")
        pcb.add_trace(("RSH1", "1"), (60, 50), width=3.0, layer="F.Cu", net="PGND")
        pcb.add_trace((60, 50), ("J2", "1"), width=3.0, layer="F.Cu", net="PGND")
        pcb.add_trace((60, 50), ("U3", "1"), width=0.2, layer="F.Cu", net="PGND")
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb.save(pcb_path)
        return pcb_path

    def _write_current_paths(self, tmp_path: Path) -> Path:
        sidecar = tmp_path / "current_paths.json"
        sidecar.write_text(
            json.dumps(
                {
                    "paths": [
                        {
                            "name": "FORCE",
                            "net": "PGND",
                            "source": {"ref": "RSH1", "pad": "1"},
                            "sink": {"ref": "J2", "pad": "1"},
                            "continuous_a": 15.0,
                            "reinforcement_eligible": True,
                        },
                        {
                            "name": "KELVIN_SENSE",
                            "net": "PGND",
                            "source": {"ref": "RSH1", "pad": "1"},
                            "sink": {"ref": "U3", "pad": "1"},
                            "continuous_a": 0.001,
                            "reinforcement_eligible": False,
                        },
                    ]
                }
            )
        )
        return sidecar

    def test_reinforce_gates_via_cli(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = self._build_kelvin_board(tmp_path)
        sidecar = self._write_current_paths(tmp_path)

        rc = run_pcb_command(
            _reinforce_args(
                pcb_path,
                net="PGND",
                spacing=10.0,
                all_runs=True,
                current_paths=str(sidecar),
                format="json",
            )
        )
        assert rc == 0

        after = PCB.load(pcb_path)
        # No via anchored near U3's sense pad.
        u3_pos = after.get_pad_position("U3", "1")
        assert u3_pos is not None
        assert all(math.dist(v.position, u3_pos) > 1.0 for v in after.vias)
        # At least one anchor landed on the force path.
        assert len(after.vias) > 0

    def test_reinforce_cli_bad_current_paths_file_errors(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = self._build_kelvin_board(tmp_path)
        rc = run_pcb_command(
            _reinforce_args(pcb_path, net="PGND", current_paths=str(tmp_path / "missing.json"))
        )
        assert rc == 1

    def test_current_paths_audit_cli_json(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = self._build_kelvin_board(tmp_path)
        sidecar = self._write_current_paths(tmp_path)

        args = argparse.Namespace(
            pcb_command="current-paths-audit",
            pcb=str(pcb_path),
            current_paths=str(sidecar),
            format="json",
        )
        rc = run_pcb_command(args)
        assert rc == 0
        out = capsys.readouterr().out
        data = json.loads(out)
        assert data["paths_declared"] == 2
        assert data["paths_resolved"] == 2
        assert data["all_resolved"] is True
        assert data["fully_covered"] is True
        # Board never mutated by the audit.
        assert len(PCB.load(pcb_path).vias) == 0

    def test_current_paths_audit_cli_reports_waveform_assumptions(self, tmp_path, capsys):
        """A pulsed declaration's thermal basis and unchecked fusing mode are
        both visible in the audit's JSON and text output (issue #4980)."""
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = self._build_kelvin_board(tmp_path)
        sidecar = tmp_path / "current_paths.json"
        sidecar.write_text(
            json.dumps(
                {
                    "paths": [
                        {
                            "name": "FORCE",
                            "net": "PGND",
                            "source": {"ref": "RSH1", "pad": "1"},
                            "sink": {"ref": "J2", "pad": "1"},
                            "continuous_a": 3.0,
                            "pulsed_a": 18.0,
                            "reinforcement_eligible": True,
                        }
                    ]
                }
            )
        )

        args = argparse.Namespace(
            pcb_command="current-paths-audit",
            pcb=str(pcb_path),
            current_paths=str(sidecar),
            format="json",
        )
        assert run_pcb_command(args) == 0
        data = json.loads(capsys.readouterr().out)
        resolution = data["resolutions"][0]
        assert resolution["pulsed_a"] == 18.0
        assert resolution["duty_cycle"] is None
        # No duty cycle -> sized at the peak, and the audit says which.
        assert resolution["thermal_basis"] == "peak-as-continuous"
        assert resolution["thermal_design_a"] == 18.0
        assert "duty_cycle" in resolution["thermal_assumption"]
        # No pulse duration -> fusing could not be evaluated.
        assert resolution["fusing_checked"] is False

        args.format = "text"
        assert run_pcb_command(args) == 0
        text = capsys.readouterr().out
        assert "peak treated as continuous" in text
        assert "fusing survivability NOT checked" in text

    def test_current_paths_audit_cli_text_reports_unresolved(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = self._build_kelvin_board(tmp_path)
        sidecar = tmp_path / "current_paths.json"
        sidecar.write_text(
            json.dumps(
                {
                    "paths": [
                        {
                            "name": "BROKEN",
                            "net": "PGND",
                            "source": {"ref": "RSH1", "pad": "1"},
                            "sink": {"ref": "J404", "pad": "1"},
                            "continuous_a": 15.0,
                        }
                    ]
                }
            )
        )

        args = argparse.Namespace(
            pcb_command="current-paths-audit",
            pcb=str(pcb_path),
            current_paths=str(sidecar),
            format="text",
        )
        rc = run_pcb_command(args)
        assert rc == 0
        out = capsys.readouterr().out
        assert "unresolved" in out
        assert "BROKEN" in out

    def test_current_paths_audit_cli_reports_unmodeled_copper(self, tmp_path, capsys):
        """A same-net pour (#5273) makes the force path ambiguous and is
        surfaced by name in both JSON and text audit output."""
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.schema.pcb import PCB
        from kicad_tools.sexp import parse_string

        pcb_path = self._build_kelvin_board(tmp_path)
        pcb = PCB.load(pcb_path)
        net = pcb.get_net_by_name("PGND")
        assert net is not None
        # A real ``(zone ...)`` sexp node, not just a ``Zone`` dataclass
        # appended to ``pcb._zones`` -- ``save()`` serializes ``pcb._sexp``
        # directly, and re-parsing on the next ``PCB.load()`` is what
        # populates ``pcb._zones``, so the node has to round-trip for real.
        pcb._sexp.append(
            parse_string(
                f'(zone (net {net.number} "PGND") (layer "F.Cu") '
                "(min_thickness 0.2) "
                "(polygon (pts (xy 0 0) (xy 200 0) (xy 200 120) (xy 0 120))))"
            )
        )
        pcb.save(pcb_path)
        assert len(PCB.load(pcb_path).zones) == 1
        sidecar = self._write_current_paths(tmp_path)

        args = argparse.Namespace(
            pcb_command="current-paths-audit",
            pcb=str(pcb_path),
            current_paths=str(sidecar),
            format="json",
        )
        assert run_pcb_command(args) == 0
        data = json.loads(capsys.readouterr().out)
        assert data["all_resolved"] is False
        assert data["resolutions"][0]["status"] == "ambiguous"
        assert "PGND" in data["unmodeled"]
        assert data["unmodeled"]["PGND"][0]["kind"] == "zone"

        args.format = "text"
        assert run_pcb_command(args) == 0
        text = capsys.readouterr().out
        assert "Unmodeled copper" in text
        assert "zone on F.Cu" in text


class TestAllRunsAnchoring:
    def test_default_mode_anchors_only_longest_run(self):
        pcb = _branched_run_pcb()
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
        # Three runs reported; only the longest (west, 80mm) is anchored.
        assert len(result.runs) == 3
        anchored = [r for r in result.runs if r.anchors_placed > 0]
        assert len(anchored) == 1
        # Every placed anchor lies on the west arm (x<=100, y==50).
        assert all(a.x <= 100.0 + 1e-6 and abs(a.y - 50.0) < 1e-6 for a in result.placed)
        assert result.unhandled_runs == 2

    def test_all_runs_anchors_every_branch(self):
        pcb = _branched_run_pcb()
        default = reinforce_net(
            _branched_run_pcb(), "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced"
        )
        result = reinforce_net(
            pcb, "FUSED_LINE", spacing_mm=15.0, all_runs=True, anchor_mode="spaced"
        )

        # Anchors now land on >=2 distinct runs (the pre-fix code placed on
        # exactly one). East arm (x>100) and branch (y>50) both anchored.
        assert any(a.x > 100.0 + 1e-6 for a in result.placed)
        assert any(a.y > 50.0 + 1e-6 for a in result.placed)
        anchored_runs = [r for r in result.runs if r.anchors_placed > 0]
        assert len(anchored_runs) >= 2
        # Strictly more coverage than default single-longest-run mode.
        assert result.placed_count > default.placed_count
        assert result.unhandled_runs == 0
        assert result.runs_fully_reinforced == 3

    def test_all_runs_dedupes_shared_junction_vertex(self):
        """The junction vertex shared by all three arms is anchored exactly once."""
        pcb = _branched_run_pcb()
        result = reinforce_net(
            pcb, "FUSED_LINE", spacing_mm=15.0, all_runs=True, anchor_mode="spaced"
        )
        junction_hits = [
            a for a in result.placed if abs(a.x - 100.0) < 1e-6 and abs(a.y - 50.0) < 1e-6
        ]
        assert len(junction_hits) == 1

    def test_all_runs_idempotent_second_run_adds_no_new_vias(self):
        pcb = _branched_run_pcb()
        reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, all_runs=True, anchor_mode="spaced")
        after_first = len(pcb.vias)
        reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, all_runs=True, anchor_mode="spaced")
        assert len(pcb.vias) == after_first

    def test_min_run_length_filters_short_runs_but_reports_them(self):
        pcb = _branched_run_pcb()
        # Threshold 30mm: west (80) and east (50) anchored; branch (20) filtered.
        result = reinforce_net(
            pcb,
            "FUSED_LINE",
            spacing_mm=15.0,
            all_runs=True,
            min_run_length_mm=30.0,
            anchor_mode="spaced",
        )
        # All three runs are still reported (never silently dropped).
        assert len(result.runs) == 3
        # The 20mm branch (goes up to y=70) is NOT anchored.
        assert not any(a.y > 50.0 + 1e-6 for a in result.placed)
        short = [r for r in result.runs if r.length_mm < 30.0]
        assert len(short) == 1
        assert short[0].anchors_placed == 0
        assert short[0].anchors_needed > 0
        assert result.unhandled_runs == 1

    def test_all_runs_below_threshold_places_nothing_without_error(self):
        pcb = _branched_run_pcb()
        result = reinforce_net(
            pcb,
            "FUSED_LINE",
            spacing_mm=15.0,
            all_runs=True,
            min_run_length_mm=500.0,
            anchor_mode="spaced",
        )
        assert result.placed_count == 0
        assert len(result.runs) == 3
        assert result.run_segment_count == 0
        assert result.unhandled_runs == 3


class TestCollinearMergeReporting:
    def test_raw_chain_keeps_fragments_but_report_merges(self):
        pcb = _fragmented_straight_pcb(pieces=12)
        segs = _net_segments(pcb, "FUSED_LINE")
        raw_run = _chain_polylines(segs)[0]
        # sort_into_chains walks the fragments into ONE run but keeps them as
        # 12 separate segments ...
        assert len(raw_run) == 12

        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
        # ... while the reinforce report coalesces them to a single geometric
        # segment (materially lower count).
        assert result.run_segment_count == 1
        assert result.runs[0].segment_count == 1

    def test_anchor_positions_unchanged_by_merge(self):
        pcb = _fragmented_straight_pcb(pieces=12)
        segs = _net_segments(pcb, "FUSED_LINE")
        raw_run = _chain_polylines(segs)[0]
        raw_points = [raw_run[0].start] + [s.end for s in raw_run]
        expected = _anchor_positions(raw_points, 15.0)

        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
        placed = sorted((round(a.x, 6), round(a.y, 6)) for a in result.placed)
        exp = sorted((round(x, 6), round(y, 6)) for x, y in expected)
        # Merge changes reporting, not anchor coverage.
        assert placed == exp

    def test_merge_does_not_bridge_junctions(self):
        """Collinear merge must not merge across a degree-3 junction (#2389)."""
        pcb = _branched_run_pcb()
        result = reinforce_net(
            pcb, "FUSED_LINE", spacing_mm=15.0, all_runs=True, anchor_mode="spaced"
        )
        # Still three distinct runs after merge -- the junction is never bridged.
        assert len(result.runs) == 3


# --------------------------------------------------------------------------
# Declared current-path gating (Issue #4980)
# --------------------------------------------------------------------------


def _add_pad_footprint(pcb: PCB, *, ref: str, x: float, y: float, net: str) -> None:
    """Same helper as ``_add_th_pad_footprint`` but a plain SMD-shaped pad."""
    from kicad_tools.schema.pcb import Footprint, Pad

    net_obj = pcb.add_net(net)
    pad = Pad(
        number="1",
        type="thru_hole",
        shape="circle",
        position=(0.0, 0.0),
        size=(1.0, 1.0),
        layers=["*.Cu"],
        net_number=net_obj.number,
        net_name=net,
        drill=0.5,
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


def _kelvin_shunt_pcb() -> PCB:
    """A four-terminal-shunt-style fixture on one net.

    RSH1 (force-) --force path--> J2 (force+)
         |
      Kelvin sense spur
         |
         v
        U3 (INA181-style sense pad)
    """
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="RSH1", x=20, y=50, net="PGND")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="PGND")
    _add_pad_footprint(pcb, ref="U3", x=60, y=80, net="PGND")
    pcb.add_trace(("RSH1", "1"), (60, 50), width=3.0, layer="F.Cu", net="PGND")
    pcb.add_trace((60, 50), ("J2", "1"), width=3.0, layer="F.Cu", net="PGND")
    pcb.add_trace((60, 50), ("U3", "1"), width=0.2, layer="F.Cu", net="PGND")
    return pcb


def _force_spec(current_a: float = 15.0) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="FORCE",
        net_name="PGND",
        source=PathEndpoint("RSH1", "1"),
        sink=PathEndpoint("J2", "1"),
        continuous_a=current_a,
        reinforcement_eligible=True,
    )


def _kelvin_sense_spec(current_a: float = 0.001) -> CurrentPathSpec:
    return CurrentPathSpec(
        name="KELVIN_SENSE",
        net_name="PGND",
        source=PathEndpoint("RSH1", "1"),
        sink=PathEndpoint("U3", "1"),
        continuous_a=current_a,
        reinforcement_eligible=False,
    )


class TestCurrentPathGating:
    def test_ungated_when_no_current_paths_supplied(self):
        """Legacy behavior is unchanged when ``current_paths`` is omitted."""
        pcb = _kelvin_shunt_pcb()
        result = reinforce_net(pcb, "PGND", spacing_mm=10.0, all_runs=True, anchor_mode="spaced")
        assert all(rs.path_excluded_reason is None for rs in result.runs)
        assert result.placed_count > 0

    def test_kelvin_sense_never_reinforced(self):
        """Force path anchored; the Kelvin sense spur is excluded even with
        --all-runs (it must never be bridged by a shared reinforcement
        anchor)."""
        pcb = _kelvin_shunt_pcb()
        result = reinforce_net(
            pcb,
            "PGND",
            spacing_mm=10.0,
            all_runs=True,
            current_paths=[_force_spec(), _kelvin_sense_spec()],
            anchor_mode="spaced",
        )
        # Force path (both trunk legs) anchored.
        force_runs = [rs for rs in result.runs if rs.length_mm in (40.0, 60.0)]
        assert all(rs.path_excluded_reason is None for rs in force_runs)
        assert all(rs.anchors_placed > 0 for rs in force_runs)

        # The sense-only leg (RSH1 junction -> U3, 30mm) is excluded.
        sense_run = next(rs for rs in result.runs if rs.length_mm == pytest.approx(30.0))
        assert sense_run.path_excluded_reason is not None
        assert sense_run.anchors_placed == 0

    def test_default_longest_run_picks_force_not_sense(self):
        """Even in default mode (no --all-runs), the gate must not let a
        longer sense spur get picked over the force path.

        Uses an explicit degree-3 junction (like ``_kelvin_shunt_pcb``) so
        the chainer splits the force legs and the sense spur into separate
        runs, with the sense spur made deliberately the LONGEST run --
        proving the gate, not run length, decides eligibility.
        """
        pcb = PCB.create(width=300, height=300, center=False)
        _add_pad_footprint(pcb, ref="RSH1", x=20, y=50, net="PGND")
        _add_pad_footprint(pcb, ref="J2", x=55, y=50, net="PGND")
        _add_pad_footprint(pcb, ref="U3", x=50, y=250, net="PGND")
        pcb.add_trace(("RSH1", "1"), (50, 50), width=3.0, layer="F.Cu", net="PGND")
        pcb.add_trace((50, 50), ("J2", "1"), width=3.0, layer="F.Cu", net="PGND")
        pcb.add_trace((50, 50), ("U3", "1"), width=0.2, layer="F.Cu", net="PGND")

        force_spec = CurrentPathSpec(
            name="FORCE",
            net_name="PGND",
            source=PathEndpoint("RSH1", "1"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=15.0,
            reinforcement_eligible=True,
        )
        sense_spec = CurrentPathSpec(
            name="KELVIN_SENSE",
            net_name="PGND",
            source=PathEndpoint("RSH1", "1"),
            sink=PathEndpoint("U3", "1"),
            continuous_a=0.001,
            reinforcement_eligible=False,
        )

        # Sanity: WITHOUT gating, the longest run (the 200mm sense spur)
        # would be the default anchor target.
        ungated = reinforce_net(pcb, "PGND", spacing_mm=10.0, anchor_mode="spaced")
        assert ungated.run_length_mm == pytest.approx(200.0, abs=1e-6)

        gated = reinforce_net(
            pcb,
            "PGND",
            spacing_mm=10.0,
            current_paths=[force_spec, sense_spec],
            anchor_mode="spaced",
        )
        assert gated.placed_count > 0
        # The anchored primary run is the (short) force leg RSH1->junction,
        # not the (long) sense spur.
        assert gated.run_length_mm == pytest.approx(30.0, abs=1e-6)

    def test_broken_force_endpoint_reinforces_nothing(self):
        """A trunk endpoint that no longer resolves fails closed: nothing
        is reinforced rather than silently continuing against a
        declaration nobody can verify."""
        pcb = _kelvin_shunt_pcb()
        broken_force = CurrentPathSpec(
            name="FORCE",
            net_name="PGND",
            source=PathEndpoint("RSH1", "1"),
            sink=PathEndpoint("J404", "1"),  # moved/removed component
            continuous_a=15.0,
            reinforcement_eligible=True,
        )
        result = reinforce_net(
            pcb,
            "PGND",
            spacing_mm=10.0,
            all_runs=True,
            current_paths=[broken_force],
            anchor_mode="spaced",
        )
        assert result.placed_count == 0
        assert all(rs.path_excluded_reason is not None for rs in result.runs)

    def test_same_net_pour_excludes_force_path_from_reinforcement(self):
        """A same-net copper pour (#5273) makes ``resolve_current_path``
        report the force path as ``"ambiguous"`` rather than ``"resolved"``.
        No code change is needed in ``pcb/reinforce.py`` itself --
        ``reinforcement_eligible_segment_ids()`` is an allow-list gated on
        ``resolution.ok``, so a net whose declared force path can no longer
        resolve cleanly drops out of reinforcement eligibility for free."""
        from kicad_tools.schema.pcb import Zone

        pcb = _kelvin_shunt_pcb()
        net = pcb.get_net_by_name("PGND")
        assert net is not None
        pcb._zones.append(
            Zone(
                net.number,
                "PGND",
                "F.Cu",
                polygon=[(0, 0), (200, 0), (200, 120), (0, 120)],
            )
        )
        result = reinforce_net(
            pcb,
            "PGND",
            spacing_mm=10.0,
            all_runs=True,
            current_paths=[_force_spec(), _kelvin_sense_spec()],
            anchor_mode="spaced",
        )
        # Every run is excluded -- the force path that used to anchor now
        # resolves ambiguous, and the sense spur was already ineligible.
        assert result.placed_count == 0
        assert all(rs.path_excluded_reason is not None for rs in result.runs)

    def test_current_paths_for_other_net_do_not_gate(self):
        """Specs targeting a different net are ignored -- gating is
        per-net, opt-in only where declared."""
        pcb = _kelvin_shunt_pcb()
        other_net_spec = CurrentPathSpec(
            name="UNRELATED",
            net_name="SOME_OTHER_NET",
            source=PathEndpoint("RSH1", "1"),
            sink=PathEndpoint("J2", "1"),
            continuous_a=1.0,
            reinforcement_eligible=True,
        )
        result = reinforce_net(
            pcb,
            "PGND",
            spacing_mm=10.0,
            all_runs=True,
            current_paths=[other_net_spec],
            anchor_mode="spaced",
        )
        assert all(rs.path_excluded_reason is None for rs in result.runs)
        assert result.placed_count > 0


@pytest.mark.parametrize("reverse_mask", range(8))
def test_current_path_anchors_ignore_segment_orientation(reverse_mask, tmp_path):
    """A serialized direction change cannot remove physical force eligibility."""

    def anchors(pcb):
        result = reinforce_net(
            pcb,
            "PGND",
            spacing_mm=10.0,
            all_runs=True,
            dry_run=True,
            current_paths=[_force_spec(), _kelvin_sense_spec()],
            anchor_mode="spaced",
        )
        return sorted((round(a.x, 6), round(a.y, 6)) for a in result.placed)

    import re

    from tests.test_cli_route_current_paths import _T_NETWORK_PCB_TEMPLATE

    text = (
        _T_NETWORK_PCB_TEMPLATE.format(trunk_width=3.0, sense_width=0.2)
        .replace("J1", "RSH1")
        .replace("NET1", "PGND")
    )
    path = tmp_path / "reversed.kicad_pcb"
    path.write_text(text)
    expected = anchors(PCB.load(path))
    assert expected
    index = 0

    def reverse(match):
        nonlocal index
        swapped = bool(reverse_mask & (1 << index))
        index += 1
        start, end = match.groups()
        if swapped:
            start, end = end, start
        return f"(segment (start {start}) (end {end})"

    path.write_text(re.sub(r"\(segment \(start ([^)]*)\) \(end ([^)]*)\)", reverse, text))
    assert index == 3
    assert anchors(PCB.load(path)) == expected


def test_current_path_boundary_splits_force_from_continuing_sense():
    """A degree-two force pad must end the eligible run before the sense tap."""
    pcb = PCB.create(width=160, height=100, center=False)
    for ref, x in [("RSH1", 20), ("J2", 80), ("U3", 120)]:
        _add_pad_footprint(pcb, ref=ref, x=x, y=50, net="PGND")
    pcb.add_trace((20, 50), (60, 50), width=3, layer="F.Cu", net="PGND")
    pcb.add_trace((60, 50), (80, 50), width=3, layer="F.Cu", net="PGND")
    pcb.add_trace((80, 50), (120, 50), width=0.2, layer="F.Cu", net="PGND")
    result = reinforce_net(
        pcb,
        "PGND",
        spacing_mm=10,
        all_runs=True,
        dry_run=True,
        current_paths=[_force_spec(), _kelvin_sense_spec()],
        anchor_mode="spaced",
    )
    assert result.placed
    assert all(a.x <= 80 for a in result.placed)
    assert any(r.length_mm == 60 and r.path_excluded_reason is None for r in result.runs)
    assert any(r.length_mm == 40 and r.path_excluded_reason for r in result.runs)


def test_board09_force_runs_survive_current_path_gate():
    """Separate eligibility from clearance refusals on the real shunt fixture."""
    board = (
        Path(__file__).resolve().parents[1]
        / "boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb"
    )
    before = board.read_bytes()
    pcb = PCB.load(board)
    specs = [
        CurrentPathSpec(
            name="force",
            net_name="VOUT_PRE",
            source=PathEndpoint("L1", "2"),
            sink=PathEndpoint("RSH1", "1"),
            continuous_a=3,
            reinforcement_eligible=True,
        ),
        CurrentPathSpec(
            name="feedback",
            net_name="VOUT_PRE",
            source=PathEndpoint("L1", "2"),
            sink=PathEndpoint("R7", "1"),
            continuous_a=0.001,
            reinforcement_eligible=False,
        ),
    ]
    result = reinforce_net(
        pcb, "VOUT_PRE", layer="F.Cu", all_runs=True, dry_run=True, current_paths=specs
    )
    eligible = [r for r in result.runs if r.path_excluded_reason is None]
    assert eligible
    assert sum(r.length_mm for r in eligible) > 20
    assert any(r.path_excluded_reason for r in result.runs)
    assert board.read_bytes() == before


# --------------------------------------------------------------------------
# Board rules: class floors, pairwise voltage map, same-net holes (#8085)
# --------------------------------------------------------------------------


def _blocked_by_foreign_track(gap_mm: float) -> PCB:
    """Straight run whose x=35 anchor window is walled by a thin foreign track.

    The 0.2 mm foreign track spans the whole nudge window and sits so its edge
    is ``gap_mm`` from the (1.916 mm) anchor pad edge.
    """
    pcb = _straight_run_pcb(length=100.0)
    pad_radius = anchor_pad_for_drill(anchor_drill_for_awg(16), 0.25) / 2
    dy = gap_mm + pad_radius + 0.1
    pcb.add_trace((27, 50 + dy), (43, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
    return pcb


class TestBoardRulesClearance:
    def test_class_floor_refuses_where_fallback_would_place(self):
        # 0.3 mm gap clears the 0.2 mm fallback ...
        baseline = reinforce_net(
            _blocked_by_foreign_track(0.3), "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced"
        )
        assert baseline.refused_count == 0
        # ... but not a 0.4 mm net-class floor on the obstacle's class.
        result = reinforce_net(
            _blocked_by_foreign_track(0.3),
            "FUSED_LINE",
            spacing_mm=15.0,
            net_class_clearances={"OTHER": 0.4},
            anchor_mode="spaced",
        )
        assert result.refused_count >= 1
        r = result.refused[0]
        assert r.rule == "dru_class_floor"
        assert r.required_mm == pytest.approx(0.4)
        assert r.actual_mm == pytest.approx(0.3, abs=0.01)
        assert r.obstacle_net == "OTHER"

    def test_pairwise_voltage_map_refuses_hv_pair(self):
        from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table

        table = build_pairwise_clearance_table(
            {"FUSED_LINE": 150.0, "OTHER": 3.3}, dru=0.0, hv_threshold=30.0
        )
        required = table.required_clearance("FUSED_LINE", "OTHER")
        assert required > 0.44
        # 0.44 mm passes the scalar floors ...
        scalar = reinforce_net(
            _blocked_by_foreign_track(0.44), "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced"
        )
        assert scalar.refused_count == 0
        # ... and is refused, naming the pairwise rule, once the map is given.
        result = reinforce_net(
            _blocked_by_foreign_track(0.44),
            "FUSED_LINE",
            spacing_mm=15.0,
            pairwise_clearance=table,
            anchor_mode="spaced",
        )
        assert result.refused_count >= 1
        r = result.refused[0]
        assert r.rule == "voltage_map_pairwise"
        assert r.required_mm == pytest.approx(required)
        assert r.actual_mm == pytest.approx(0.44, abs=0.01)

    def test_endpoint_on_same_net_pth_drill_is_nudged_off(self):
        pcb = _straight_run_pcb(length=100.0)
        _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net="FUSED_LINE", drill=1.0, size=2.0)
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
        drill = anchor_drill_for_awg(16)
        min_center = 0.5 + drill / 2 + 1.0 / 2  # hole-to-hole floor, edge to edge
        for a in result.placed:
            assert math.hypot(a.x - 20.0, a.y - 50.0) >= min_center - 1e-6

    def test_candidate_on_existing_same_net_via_is_refused(self):
        pcb = _straight_run_pcb(length=100.0)
        pcb.add_via(35.0, 50.0, size=0.6, drill=0.3, layers=("F.Cu", "B.Cu"), net="FUSED_LINE")
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
        drill = anchor_drill_for_awg(16)
        min_center = 0.5 + drill / 2 + 0.3 / 2
        for a in result.placed:
            assert math.hypot(a.x - 35.0, a.y - 50.0) >= min_center - 1e-6

    def test_hole_to_hole_refusal_names_rule(self):
        pcb = _straight_run_pcb(length=100.0)
        # Same-net vias walling the whole nudge window around x=35.
        for x in (28.0, 30.5, 33.0, 35.0, 37.0, 39.5, 42.0):
            pcb.add_via(x, 50.0, size=0.6, drill=0.3, layers=("F.Cu", "B.Cu"), net="FUSED_LINE")
        result = reinforce_net(pcb, "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced")
        mid = [r for r in result.refused if abs(r.x - 35.0) < 0.5]
        assert mid and mid[0].rule == "hole_to_hole"
        assert mid[0].required_mm == pytest.approx(0.5)

    def test_conditional_dru_rule_refuses_hv_pair(self, tmp_path):
        """A `.kicad_dru` NetClass-conditional clearance is honoured (#8494)."""
        from kicad_tools.router.board_clearance_rules import BoardClearanceRules, parse_dru

        dru = tmp_path / "b.kicad_dru"
        dru.write_text(
            "(version 1)\n"
            '(rule "Reviewed clearance - HV"\n'
            "  (condition \"A.NetClass == 'HV' || B.NetClass == 'HV'\")\n"
            "  (constraint clearance (min 0.4mm)))\n"
        )
        rules_list, err = parse_dru(dru)
        assert err is None
        board_rules = BoardClearanceRules(
            dru_rules=rules_list, netclass_assignments={"FUSED_LINE": "HV"}
        )
        # 0.3 mm clears the 0.2 mm fallback ...
        baseline = reinforce_net(
            _blocked_by_foreign_track(0.3), "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced"
        )
        assert baseline.refused_count == 0
        # ... but not the conditional 0.4 mm rule.
        result = reinforce_net(
            _blocked_by_foreign_track(0.3),
            "FUSED_LINE",
            spacing_mm=15.0,
            board_rules=board_rules,
            anchor_mode="spaced",
        )
        assert result.refused_count >= 1
        r = result.refused[0]
        assert r.rule == "dru_conditional"
        assert r.required_mm == pytest.approx(0.4)
        assert r.actual_mm == pytest.approx(0.3, abs=0.01)

    def test_board_rules_without_dru_leave_result_unchanged(self):
        from kicad_tools.router.board_clearance_rules import BoardClearanceRules

        plain = reinforce_net(
            _blocked_by_foreign_track(0.3), "FUSED_LINE", spacing_mm=15.0, anchor_mode="spaced"
        )
        with_rules = reinforce_net(
            _blocked_by_foreign_track(0.3),
            "FUSED_LINE",
            spacing_mm=15.0,
            board_rules=BoardClearanceRules(),
            anchor_mode="spaced",
        )
        assert with_rules.refused_count == plain.refused_count
        assert len(with_rules.placed) == len(plain.placed)


class TestReinforceCLIBoardRules:
    def test_cli_voltage_map_reports_rule_in_json(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _blocked_by_foreign_track(0.44).save(pcb_path)
        vm = tmp_path / "vm.json"
        vm.write_text(json.dumps({"FUSED_LINE": 150.0, "OTHER": 3.3}))

        rc = run_pcb_command(
            _reinforce_args(
                pcb_path, format="json", dry_run=True, voltage_map=str(vm), anchor_mode="spaced"
            )
        )
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        assert data["refused"]
        assert data["refused"][0]["rule"] == "voltage_map_pairwise"
        assert data["refused"][0]["required_mm"] > 0.44

    def test_cli_refusal_coordinates_are_sheet_absolute(self, tmp_path, capsys):
        """Refusal x/y (JSON and text) are sheet-absolute on a shifted board (#8511).

        Guards the ``board_origin`` re-add in ``_run_reinforce_command`` (#8493).
        """
        from kicad_tools.cli.commands.pcb import run_pcb_command

        # A centred board has a non-zero origin; traces use board-relative coords.
        pcb = PCB.create(width=200, height=100, center=True)
        pad_radius = anchor_pad_for_drill(anchor_drill_for_awg(16), 0.25) / 2
        for i in range(4):
            pcb.add_trace(
                (20 + 25 * i, 50), (45 + 25 * i, 50), width=2.0, layer="F.Cu", net="FUSED_LINE"
            )
        dy = 0.44 + pad_radius + 0.1
        pcb.add_trace((27, 50 + dy), (43, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb.save(pcb_path)
        vm = tmp_path / "vm.json"
        vm.write_text(json.dumps({"FUSED_LINE": 150.0, "OTHER": 3.3}))

        loaded = PCB.load(pcb_path)
        ox, oy = loaded.board_origin
        assert ox > 0 and oy > 0
        # Absolute Edge.Cuts extent of the board.
        x0, y0, x1, y1 = ox, oy, ox + 200, oy + 100

        args = {"dry_run": True, "voltage_map": str(vm), "anchor_mode": "spaced"}
        assert run_pcb_command(_reinforce_args(pcb_path, format="json", **args)) == 0
        refused = json.loads(capsys.readouterr().out)["refused"]
        assert refused
        for r in refused:
            assert x0 <= r["x"] <= x1 and y0 <= r["y"] <= y1
        # The refused anchor sits at board-relative (35, 50).
        assert (refused[0]["x"], refused[0]["y"]) == pytest.approx((35 + ox, 50 + oy), abs=0.5)

        assert run_pcb_command(_reinforce_args(pcb_path, format="text", **args)) == 0
        text = re.findall(r"refused at \(([-\d.]+), ([-\d.]+)\)", capsys.readouterr().out)
        assert len(text) == len(refused)
        for (tx, ty), r in zip(text, refused, strict=True):
            assert float(tx) == pytest.approx(r["x"], abs=0.01)
            assert float(ty) == pytest.approx(r["y"], abs=0.01)

    def test_cli_missing_voltage_map_errors(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb(length=100.0).save(pcb_path)
        rc = run_pcb_command(_reinforce_args(pcb_path, voltage_map=str(tmp_path / "nope.json")))
        assert rc == 1


# --------------------------------------------------------------------------
# End-anchor mode: two anchors per single-layer leg (Issue #8742)
# --------------------------------------------------------------------------

_ANCHOR_PAD = anchor_pad_for_drill(anchor_drill_for_awg(16), 0.25)


def _add_shaped_pad_footprint(
    pcb: PCB,
    *,
    ref: str,
    x: float,
    y: float,
    net: str,
    shape: str,
    size: tuple[float, float],
    rotation: float = 0.0,
    fp_rotation: float = 0.0,
    local: tuple[float, float] = (0.0, 0.0),
) -> None:
    """One SMD pad of any shape; ``rotation`` is the pad's ABSOLUTE angle."""
    from kicad_tools.schema.pcb import Footprint, Pad

    net_obj = pcb.add_net(net)
    pad = Pad(
        number="1",
        type="smd",
        shape=shape,
        position=local,
        size=size,
        layers=["F.Cu", "F.Paste", "F.Mask"],
        net_number=net_obj.number,
        net_name=net,
        rotation=rotation,
    )
    pcb._footprints.append(
        Footprint(
            name="SMD:Pad",
            layer="F.Cu",
            position=(x, y),
            rotation=fp_rotation,
            reference=ref,
            value="P",
            pads=[pad],
        )
    )


def _padded_straight_pcb(length: float = 100.0, pad_size: float = 2.0) -> PCB:
    pcb = _straight_run_pcb(length=length)
    _add_th_pad_footprint(pcb, ref="J1", x=20.0, y=50.0, net="FUSED_LINE", drill=1.0, size=pad_size)
    _add_th_pad_footprint(
        pcb, ref="J2", x=20.0 + length, y=50.0, net="FUSED_LINE", drill=1.0, size=pad_size
    )
    return pcb


def _two_layer_pcb() -> PCB:
    """RSH1 -> F.Cu -> via (70,50) -> B.Cu -> J2: one path, two layers."""
    pcb = PCB.create(width=200, height=120, center=False)
    _add_pad_footprint(pcb, ref="RSH1", x=20, y=50, net="PGND")
    _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="PGND")
    pcb.add_trace((20, 50), (70, 50), width=3.0, layer="F.Cu", net="PGND")
    pcb.add_via(70.0, 50.0, size=0.8, drill=0.4, layers=("F.Cu", "B.Cu"), net="PGND")
    pcb.add_trace((70, 50), (120, 50), width=3.0, layer="B.Cu", net="PGND")
    return pcb


class TestEndAnchorMode:
    def test_end_anchor_default_places_two_offset_from_pads(self):
        pcb = _padded_straight_pcb()
        before = len(pcb.vias)
        result = reinforce_net(pcb, "FUSED_LINE")

        assert result.anchor_mode == "ends"
        assert result.end_offset_mm == pytest.approx(1.0)
        assert result.placed_count == 2
        assert len(pcb.vias) == before + 2
        (leg,) = result.legs
        assert leg.status == "full" and leg.kind == "run" and leg.layer == "F.Cu"
        assert leg.polyline == [(20.0, 50.0), (120.0, 50.0)]
        inset = 1.0 + 1.0 + _ANCHOR_PAD / 2  # pad radius + end offset + anchor pad/2
        xs = sorted(a.x for a in result.placed)
        assert xs == pytest.approx([20.0 + inset, 120.0 - inset], abs=1e-3)
        assert [(a.x, a.y) for a in leg.anchors] == [(a.x, a.y) for a in result.placed]
        # Pad copper edge to anchor pad edge is exactly the end offset.
        for a, pad_x in zip(sorted(result.placed, key=lambda a: a.x), (20.0, 120.0), strict=True):
            gap = abs(a.x - pad_x) - 1.0 - _ANCHOR_PAD / 2
            assert gap == pytest.approx(1.0, abs=1e-3)
        # Nothing mid-run.
        assert not any(30.0 < a.x < 110.0 for a in result.placed)

    def test_end_anchor_offset_flag_moves_both(self):
        result = reinforce_net(_padded_straight_pcb(), "FUSED_LINE", end_offset_mm=2.5)
        inset = 1.0 + 2.5 + _ANCHOR_PAD / 2
        xs = sorted(a.x for a in result.placed)
        assert xs == pytest.approx([20.0 + inset, 120.0 - inset], abs=1e-3)

    def test_end_anchor_spaced_mode_keeps_old_count(self):
        # 0, 15, ..., 90 and the end: 8 anchors, as before #8742.
        spaced = reinforce_net(
            _straight_run_pcb(), "FUSED_LINE", anchor_mode="spaced", spacing_mm=15.0
        )
        assert spaced.placed_count == 8
        assert spaced.legs == []
        ends = reinforce_net(_straight_run_pcb(), "FUSED_LINE", spacing_mm=15.0)
        assert ends.placed_count == 2

    def test_end_anchor_free_end_measures_from_track_end(self):
        result = reinforce_net(_straight_run_pcb(), "FUSED_LINE")
        inset = 1.0 + _ANCHOR_PAD / 2
        assert sorted(a.x for a in result.placed) == pytest.approx(
            [20.0 + inset, 120.0 - inset], abs=1e-3
        )

    @pytest.mark.parametrize(
        ("shape", "size", "rotation", "exit_mm"),
        [
            ("rect", (6.0, 1.0), 0.0, 3.0),
            ("rect", (6.0, 1.0), 90.0, 0.5),
            ("rect", (6.0, 1.0), 180.0, 3.0),
            ("rect", (6.0, 1.0), 270.0, 0.5),
            ("oval", (4.0, 2.0), 90.0, 1.0),
            ("roundrect", (4.0, 2.0), 0.0, 2.0),
            ("circle", (3.0, 3.0), 0.0, 1.5),
        ],
    )
    def test_end_anchor_pad_edge_follows_shape_and_rotation(self, shape, size, rotation, exit_mm):
        pcb = _straight_run_pcb()
        _add_shaped_pad_footprint(
            pcb,
            ref="U1",
            x=20.0,
            y=50.0,
            net="FUSED_LINE",
            shape=shape,
            size=size,
            rotation=rotation,
        )
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
        first = min(a.x for a in result.placed)
        assert first - 20.0 == pytest.approx(exit_mm + 1.0 + _ANCHOR_PAD / 2, abs=1e-3)

    def test_end_anchor_rotated_footprint_pad_offset(self):
        """Footprint rotation moves the pad centre (KiCad negated-angle rule)."""
        pcb = _straight_run_pcb()
        # Local (2, 0) at 90 deg lands at (0, -2) from the footprint origin.
        _add_shaped_pad_footprint(
            pcb,
            ref="U1",
            x=20.0,
            y=52.0,
            net="FUSED_LINE",
            shape="rect",
            size=(6.0, 1.0),
            rotation=90.0,
            fp_rotation=90.0,
            local=(2.0, 0.0),
        )
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
        first = min(a.x for a in result.placed)
        assert first - 20.0 == pytest.approx(0.5 + 1.0 + _ANCHOR_PAD / 2, abs=1e-3)

    @pytest.mark.parametrize(("rotation", "exit_mm"), [(45.0, 3.0), (-45.0, 0.5), (315.0, 0.5)])
    def test_end_anchor_diagonal_pad_rotation_sign(self, rotation, exit_mm):
        """A +45 deg pad lies along the up-right diagonal (y down), so a leg
        leaving up-right runs its full length; a -45 deg one only its width."""
        pcb = PCB.create(width=200, height=200, center=False)
        c = math.sqrt(0.5)
        pcb.add_trace((20, 100), (20 + 60 * c, 100 - 60 * c), width=1.0, layer="F.Cu", net="N")
        _add_shaped_pad_footprint(
            pcb,
            ref="U1",
            x=20.0,
            y=100.0,
            net="N",
            shape="rect",
            size=(6.0, 1.0),
            rotation=rotation,
        )
        result = reinforce_net(pcb, "N", dry_run=True)
        first = min(result.placed, key=lambda a: math.dist((a.x, a.y), (20.0, 100.0)))
        dist = math.dist((first.x, first.y), (20.0, 100.0))
        assert dist == pytest.approx(exit_mm + 1.0 + _ANCHOR_PAD / 2, abs=1e-3)

    def test_end_anchor_idempotent_second_run(self):
        pcb = _padded_straight_pcb()
        reinforce_net(pcb, "FUSED_LINE")
        after_first = len(pcb.vias)
        again = reinforce_net(pcb, "FUSED_LINE")
        assert len(pcb.vias) == after_first
        assert again.legs[0].status == "full"

    def test_end_anchor_rejects_bad_mode_and_offset(self):
        with pytest.raises(ReinforceError, match="anchor_mode"):
            reinforce_net(_straight_run_pcb(), "FUSED_LINE", anchor_mode="middle")
        with pytest.raises(ReinforceError, match="end_offset_mm"):
            reinforce_net(_straight_run_pcb(), "FUSED_LINE", end_offset_mm=-1.0)


class TestEndAnchorTwoLayer:
    def test_two_layer_path_gets_two_anchors_per_leg(self):
        pcb = _two_layer_pcb()
        result = reinforce_net(pcb, "PGND", current_paths=[_force_spec()], dry_run=True)
        assert result.placed_count == 4
        assert sorted(leg.layer for leg in result.legs) == ["B.Cu", "F.Cu"]
        assert all(leg.status == "full" and len(leg.anchors) == 2 for leg in result.legs)
        assert all(leg.paths == ["FORCE"] for leg in result.legs)
        # Each leg's via end is measured from the 0.8 mm via copper edge.
        inset_via = 0.4 + 1.0 + _ANCHOR_PAD / 2
        xs = sorted(a.x for a in result.placed)
        assert xs[1] == pytest.approx(70.0 - inset_via, abs=1e-3)
        assert xs[2] == pytest.approx(70.0 + inset_via, abs=1e-3)

    def test_two_layer_without_current_paths_reports_other_layer(self):
        result = reinforce_net(_two_layer_pcb(), "PGND", layer="F.Cu", dry_run=True)
        assert result.placed_count == 2
        other = [leg for leg in result.legs if leg.layer == "B.Cu"]
        assert len(other) == 1
        assert other[0].status == "unhandled"
        assert "--layer B.Cu" in other[0].reason
        assert result.unhandled_runs == 1


class TestEndAnchorBranch:
    def test_branch_trunk_and_branch_anchors(self):
        result = reinforce_net(_branched_run_pcb(), "FUSED_LINE", all_runs=True, dry_run=True)
        trunk = next(leg for leg in result.legs if leg.kind == "trunk")
        branch = next(leg for leg in result.legs if leg.kind == "branch")
        assert len(result.legs) == 2
        assert trunk.polyline[0] == (20.0, 50.0) and trunk.polyline[-1] == (150.0, 50.0)
        assert trunk.length_mm == pytest.approx(130.0)
        inset = 1.0 + _ANCHOR_PAD / 2
        assert [(a.x, a.y) for a in trunk.anchors] == [
            pytest.approx((20.0 + inset, 50.0)),
            pytest.approx((150.0 - inset, 50.0)),
        ]
        # Branch: one beside the trunk (trunk half-width + offset + pad/2 from
        # the junction), one at its free end.
        assert branch.polyline[0] == (100.0, 50.0)
        assert [(a.x, a.y) for a in branch.anchors] == [
            pytest.approx((100.0, 50.0 + 1.0 + inset)),
            pytest.approx((100.0, 70.0 - inset)),
        ]
        trunk_pts = [(a.x, a.y) for a in trunk.anchors]
        for a in branch.anchors:
            assert all(math.dist((a.x, a.y), p) > _ANCHOR_PAD for p in trunk_pts)
        assert result.placed_count == 4

    def test_branch_without_all_runs_anchors_only_trunk(self):
        result = reinforce_net(_branched_run_pcb(), "FUSED_LINE", dry_run=True)
        branch = next(leg for leg in result.legs if leg.kind == "branch")
        assert branch.status == "unanchored" and "--all-runs" in branch.reason
        assert result.placed_count == 2
        assert result.run_length_mm == pytest.approx(130.0)

    def test_branch_from_two_overlapping_current_paths(self):
        """A trunk spec and a branch spec sharing copper: union, then Y split."""
        pcb = _kelvin_shunt_pcb()
        sense_as_branch = CurrentPathSpec(
            name="BRANCH",
            net_name="PGND",
            source=PathEndpoint("RSH1", "1"),
            sink=PathEndpoint("U3", "1"),
            continuous_a=5.0,
            reinforcement_eligible=True,
        )
        result = reinforce_net(
            pcb, "PGND", current_paths=[_force_spec(), sense_as_branch], dry_run=True
        )
        kinds = sorted(leg.kind for leg in result.legs)
        assert kinds == ["branch", "trunk"]
        trunk = next(leg for leg in result.legs if leg.kind == "trunk")
        branch = next(leg for leg in result.legs if leg.kind == "branch")
        assert trunk.length_mm == pytest.approx(100.0)
        assert set(trunk.paths) == {"FORCE", "BRANCH"}
        assert branch.paths == ["BRANCH"]
        assert all(leg.status == "full" for leg in result.legs)

    def test_branch_kelvin_sense_is_excluded_copper(self):
        result = reinforce_net(
            _kelvin_shunt_pcb(),
            "PGND",
            current_paths=[_force_spec(), _kelvin_sense_spec()],
            dry_run=True,
        )
        (leg,) = result.legs
        assert leg.kind == "run" and leg.length_mm == pytest.approx(100.0)
        assert leg.status == "full"
        excluded = [r for r in result.runs if r.path_excluded_reason]
        assert len(excluded) == 1 and excluded[0].length_mm == pytest.approx(30.0)
        assert all(a.y == pytest.approx(50.0) for a in result.placed)

    def test_branch_loop_and_cross_are_unhandled(self):
        ring = PCB.create(width=200, height=200, center=False)
        for a, b in [
            ((20, 20), (80, 20)),
            ((80, 20), (80, 80)),
            ((80, 80), (20, 80)),
            ((20, 80), (20, 20)),
        ]:
            ring.add_trace(a, b, width=1.0, layer="F.Cu", net="N")
        result = reinforce_net(ring, "N", all_runs=True, dry_run=True)
        assert result.legs and all(leg.status == "unhandled" for leg in result.legs)
        assert "loop" in result.legs[0].reason
        assert result.placed_count == 0

        cross = PCB.create(width=200, height=200, center=False)
        for end in [(20, 50), (80, 50), (50, 20), (50, 80)]:
            cross.add_trace((50, 50), end, width=1.0, layer="F.Cu", net="N")
        result = reinforce_net(cross, "N", all_runs=True, dry_run=True)
        assert len(result.legs) == 4
        assert all(leg.status == "unhandled" for leg in result.legs)
        assert "degree 4" in result.legs[0].reason


class TestEndAnchorTooShortAndRefusal:
    def test_too_short_leg_gets_no_anchors(self):
        pcb = _padded_straight_pcb(length=6.0)
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
        (leg,) = result.legs
        assert leg.status == "too_short"
        assert leg.anchors == [] and result.placed_count == 0
        assert result.refused_count == 0

    def test_too_short_foreign_track_at_end_refuses_partial(self):
        pcb = _straight_run_pcb(length=100.0)
        dy = 0.3 + _ANCHOR_PAD / 2 + 0.1
        # Walls the whole inward nudge window of the start anchor only.
        pcb.add_trace((15, 50 + dy), (30, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
        result = reinforce_net(pcb, "FUSED_LINE", net_class_clearances={"OTHER": 0.4}, dry_run=True)
        (leg,) = result.legs
        assert leg.status == "partial"
        assert len(leg.anchors) == 1 and leg.anchors[0].x > 100.0
        assert result.refused_count == 1
        r = result.refused[0]
        assert r.rule == "dru_class_floor" and r.obstacle_net == "OTHER"
        assert r.x == pytest.approx(20.0 + 1.0 + _ANCHOR_PAD / 2, abs=1e-3)

    def test_too_short_nudge_is_inward_only(self):
        """A foreign obstacle on the base spot pushes the anchor inward only."""
        pcb = _straight_run_pcb(length=100.0)
        base = 20.0 + 1.0 + _ANCHOR_PAD / 2
        dy = 0.3 + _ANCHOR_PAD / 2 + 0.1
        pcb.add_trace((10, 50 + dy), (base + 0.4, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
        result = reinforce_net(pcb, "FUSED_LINE", net_class_clearances={"OTHER": 0.4}, dry_run=True)
        leg = result.legs[0]
        assert leg.status == "full"
        start = min(a.x for a in leg.anchors)
        assert base < start <= base + 2.0 + 1e-9


class TestEndAnchorUnresolvedPath:
    def test_unresolved_path_moved_pad_reported(self):
        pcb = _kelvin_shunt_pcb()
        j2 = next(fp for fp in pcb.footprints if fp.reference == "J2")
        j2.position = (150.0, 100.0)  # pad moved off the routed copper
        result = reinforce_net(pcb, "PGND", current_paths=[_force_spec()], dry_run=True)
        assert result.placed_count == 0
        assert [u.path for u in result.unresolved_paths] == ["FORCE"]
        assert result.unresolved_paths[0].status == "unresolved"
        assert result.unresolved_paths[0].reason

    def test_unresolved_path_cli_json_and_warning(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        helper = TestCurrentPathsCLI()
        pcb_path = helper._build_kelvin_board(tmp_path)
        sidecar = tmp_path / "current_paths.json"
        sidecar.write_text(
            json.dumps(
                {
                    "paths": [
                        {
                            "name": "BROKEN",
                            "net": "PGND",
                            "source": {"ref": "RSH1", "pad": "1"},
                            "sink": {"ref": "J404", "pad": "1"},
                            "continuous_a": 15.0,
                            "reinforcement_eligible": True,
                        }
                    ]
                }
            )
        )
        rc = run_pcb_command(
            _reinforce_args(pcb_path, net="PGND", current_paths=str(sidecar), format="json")
        )
        assert rc == 0
        captured = capsys.readouterr()
        data = json.loads(captured.out)
        assert data["anchors_placed"] == 0
        assert data["unresolved_paths"][0]["path"] == "BROKEN"
        assert data["unresolved_paths"][0]["status"] == "unresolved"
        assert "BROKEN" in captured.err and "Warning" in captured.err


class TestEndAnchorCLI:
    def test_cli_json_legs_are_additive_and_sheet_absolute(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb = PCB.create(width=200, height=100, center=True)
        for i in range(4):
            pcb.add_trace(
                (20 + 25 * i, 50), (45 + 25 * i, 50), width=2.0, layer="F.Cu", net="FUSED_LINE"
            )
        pcb_path = tmp_path / "board.kicad_pcb"
        pcb.save(pcb_path)
        ox, oy = PCB.load(pcb_path).board_origin
        assert ox > 0 and oy > 0

        rc = run_pcb_command(_reinforce_args(pcb_path, format="json", dry_run=True))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        for key in (
            "net",
            "layer",
            "wire_gauge_awg",
            "anchor_drill_mm",
            "anchor_pad_mm",
            "spacing_mm",
            "run_segment_count",
            "run_length_mm",
            "unhandled_runs",
            "anchors_placed",
            "anchors_refused",
            "refused",
            "voltage_map",
            "all_runs",
            "min_run_length_mm",
            "runs_total",
            "runs_fully_reinforced",
            "runs_partial",
            "runs_unanchored",
            "runs",
            "current_paths",
        ):
            assert key in data
        assert data["anchor_mode"] == "ends"
        assert data["end_offset_mm"] == pytest.approx(1.0)
        assert data["layer"] == "F.Cu"
        (leg,) = data["legs"]
        assert {"net", "layer", "kind", "length_mm", "anchors", "status"} <= set(leg)
        assert leg["status"] == "full" and len(leg["anchors"]) == 2
        inset = 1.0 + _ANCHOR_PAD / 2
        xs = sorted(a["x"] for a in leg["anchors"])
        assert xs == pytest.approx([20 + inset + ox, 120 - inset + ox], abs=1e-3)
        assert all(a["y"] == pytest.approx(50 + oy) for a in leg["anchors"])
        assert leg["polyline"][0] == pytest.approx([20 + ox, 50 + oy])
        assert data["unresolved_paths"] == []

    def test_cli_spacing_with_ends_warns(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb(length=100.0).save(pcb_path)
        rc = run_pcb_command(_reinforce_args(pcb_path, spacing=5.0, dry_run=True))
        assert rc == 0
        captured = capsys.readouterr()
        assert "--spacing only applies to --anchor-mode spaced" in captured.err
        assert "Anchor mode:    ends" in captured.out
        assert "[full]" in captured.out

    def test_cli_spaced_mode_text_unchanged(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb(length=100.0).save(pcb_path)
        rc = run_pcb_command(
            _reinforce_args(pcb_path, anchor_mode="spaced", spacing=15.0, dry_run=True)
        )
        assert rc == 0
        captured = capsys.readouterr()
        assert captured.err == ""
        assert "Spacing:        15.0 mm" in captured.out
        assert "Anchors placed:  8" in captured.out

    def test_cli_help_describes_end_anchor_build(self, capsys):
        from kicad_tools.cli.parser import create_parser

        with pytest.raises(SystemExit):
            create_parser().parse_args(["pcb", "reinforce", "--help"])
        text = " ".join(capsys.readouterr().out.split())
        assert "--anchor-mode" in text and "--end-offset" in text
        assert "exactly two anchor holes" in text
        assert "#8743" in text
        assert "soldered through the anchor row" not in text

    def test_cli_parser_accepts_mode_and_offset(self):
        from kicad_tools.cli.parser import create_parser

        args = create_parser().parse_args(
            [
                "pcb",
                "reinforce",
                "b.kicad_pcb",
                "--net",
                "N",
                "--anchor-mode",
                "spaced",
                "--end-offset",
                "2.5",
            ]
        )
        assert args.anchor_mode == "spaced"
        assert args.end_offset == pytest.approx(2.5)
