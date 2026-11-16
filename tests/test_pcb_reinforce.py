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
    DEFAULT_MIN_WIRE_LENGTH_MM,
    ReinforceError,
    _anchor_positions,
    _chain_polylines,
    reinforce_net,
    resolve_min_wire_length_mm,
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
    layers: tuple[str, ...] = ("F.Cu", "F.Paste", "F.Mask"),
    pad_type: str = "smd",
    drill: float = 0.0,
) -> None:
    """One pad of any shape; ``rotation`` is the pad's ABSOLUTE angle."""
    from kicad_tools.schema.pcb import Footprint, Pad

    net_obj = pcb.add_net(net)
    pad = Pad(
        number="1",
        type=pad_type,
        shape=shape,
        position=local,
        size=size,
        layers=list(layers),
        drill=drill,
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
        # min_wire_length_mm=0: this asserts the too_short *geometry* gate
        # (no room for two anchors at the end offset), which the #8915
        # default minimum would pre-empt with ``unanchored``.
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True, min_wire_length_mm=0.0)
        (leg,) = result.legs
        assert leg.status == "too_short"
        assert leg.anchors == [] and result.placed_count == 0
        assert result.refused_count == 0

    def test_too_short_foreign_track_at_end_refuses_partial(self):
        pcb = _straight_run_pcb(length=100.0)
        dy = 0.3 + _ANCHOR_PAD / 2 + 0.1
        # Walls the whole inward nudge window of the start anchor only.
        pcb.add_trace((15, 50 + dy), (30, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
        result = reinforce_net(
            pcb,
            "FUSED_LINE",
            net_class_clearances={"OTHER": 0.4},
            max_end_shift_mm=2.0,
            dry_run=True,
        )
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


# --------------------------------------------------------------------------
# ends mode: straight-line end clearance and invariant coverage (PR #8751
# review). Each test below names the defect or mutation it guards against.
# --------------------------------------------------------------------------


def _rect_gap(anchor, center: tuple[float, float], half: tuple[float, float]) -> float:
    """Anchor pad edge to an axis-aligned rect pad's copper edge (mm)."""
    dx = max(abs(anchor.x - center[0]) - half[0], 0.0)
    dy = max(abs(anchor.y - center[1]) - half[1], 0.0)
    return math.hypot(dx, dy) - _ANCHOR_PAD / 2


def _bent_exit_pcb(exit_mm: float, angle_deg: float) -> PCB:
    """Two 2x6 mm rect pads; the track leaves each pad by ``exit_mm``, then
    turns by ``angle_deg`` (90 = runs parallel to the pad edge)."""
    pcb = PCB.create(width=200, height=120, center=False)
    for ref, x in (("U1", 20.0), ("U2", 120.0)):
        _add_shaped_pad_footprint(pcb, ref=ref, x=x, y=50.0, net="N", shape="rect", size=(2.0, 6.0))
    c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
    p1 = (21.0 + exit_mm, 50.0)
    p2 = (p1[0] + 20.0 * c, 50.0 + 20.0 * s)
    p4 = (119.0 - exit_mm, 50.0)
    p3 = (p4[0] - 20.0 * c, 50.0 + 20.0 * s)
    pts = [(20.0, 50.0), p1, p2, p3, p4, (120.0, 50.0)]
    for a, b in zip(pts[:-1], pts[1:], strict=True):
        pcb.add_trace(a, b, width=1.0, layer="F.Cu", net="N")
    return pcb


class TestEndAnchorStraightLineClearance:
    @pytest.mark.parametrize(
        ("exit_mm", "angle_deg"), [(0.3, 90.0), (1.0, 90.0), (0.3, 45.0), (1.0, 45.0)]
    )
    def test_end_anchor_bent_exit_keeps_euclidean_gap_at_both_ends(self, exit_mm, angle_deg):
        """Judge fixture: a bend soon after the pad used to put the anchor back
        on the pad (0.3 mm / 90 deg overlapped it by 0.658 mm, drill included)."""
        result = reinforce_net(_bent_exit_pcb(exit_mm, angle_deg), "N", dry_run=True)
        (leg,) = result.legs
        assert leg.status == "full" and len(leg.anchors) == 2
        for a in leg.anchors:
            pad_x = 20.0 if a.x < 70.0 else 120.0
            gap = _rect_gap(a, (pad_x, 50.0), (1.0, 3.0))
            # The clearance binds here, so the anchor sits exactly on it.
            assert gap == pytest.approx(1.0, abs=1e-3)
        # One anchor per end, not both at one.
        assert min(a.x for a in leg.anchors) < 70.0 < max(a.x for a in leg.anchors)

    def test_end_anchor_long_exit_unchanged_by_bend(self):
        """Past the offset the along-leg target already clears the pad."""
        result = reinforce_net(_bent_exit_pcb(3.0, 90.0), "N", dry_run=True)
        first = min(result.placed, key=lambda a: a.x)
        assert (first.x, first.y) == pytest.approx((21.0 + 1.0 + _ANCHOR_PAD / 2, 50.0))

    def test_end_anchor_unreachable_clearance_is_too_short(self):
        """A leg that hugs its pad everywhere has no clear spot: too_short."""
        pcb = PCB.create(width=200, height=120, center=False)
        _add_shaped_pad_footprint(
            pcb, ref="U1", x=20.0, y=50.0, net="N", shape="rect", size=(2.0, 40.0)
        )
        pts = [(20.0, 50.0), (21.3, 50.0), (21.3, 65.0), (20.0, 65.0)]
        for a, b in zip(pts[:-1], pts[1:], strict=True):
            pcb.add_trace(a, b, width=0.5, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", dry_run=True)
        (leg,) = result.legs
        assert leg.status == "too_short"
        assert leg.anchors == [] and result.placed_count == 0
        assert result.refused_count == 0

    @pytest.mark.parametrize("angle_deg", [30.0, 60.0])
    def test_end_anchor_oblique_branch_clears_trunk(self, angle_deg):
        """Judge fixture: a 30 deg branch off a 2 mm trunk put its junction
        anchor's pad 0.479 mm inside the trunk copper."""
        pcb = PCB.create(width=200, height=120, center=False)
        pcb.add_trace((20, 50), (100, 50), width=2.0, layer="F.Cu", net="N")
        pcb.add_trace((100, 50), (180, 50), width=2.0, layer="F.Cu", net="N")
        c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
        pcb.add_trace((100, 50), (100 + 30 * c, 50 + 30 * s), width=2.0, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", all_runs=True, dry_run=True)
        branch = next(leg for leg in result.legs if leg.kind == "branch")
        assert branch.status == "full"
        junction = min(branch.anchors, key=lambda a: math.dist((a.x, a.y), (100.0, 50.0)))
        trunk_gap = abs(junction.y - 50.0) - 1.0 - _ANCHOR_PAD / 2
        assert trunk_gap == pytest.approx(1.0, abs=1e-3)
        # Still on the branch centreline.
        assert math.atan2(junction.y - 50.0, junction.x - 100.0) == pytest.approx(
            math.radians(angle_deg), abs=1e-6
        )


class TestEndAnchorInvariants:
    def test_far_anchor_nudge_is_inward_only(self):
        """An obstacle at the FAR end only: the far anchor moves toward the
        start, never outward (mutation: ``t1 + d``)."""
        pcb = _straight_run_pcb(length=100.0)
        base = 120.0 - (1.0 + _ANCHOR_PAD / 2)
        dy = 0.3 + _ANCHOR_PAD / 2 + 0.1
        pcb.add_trace((base - 0.4, 50 + dy), (130, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
        result = reinforce_net(pcb, "FUSED_LINE", net_class_clearances={"OTHER": 0.4}, dry_run=True)
        (leg,) = result.legs
        assert leg.status == "full"
        far = max(a.x for a in leg.anchors)
        assert base - 2.0 - 1e-9 <= far < base
        assert min(a.x for a in leg.anchors) == pytest.approx(20.0 + 1.0 + _ANCHOR_PAD / 2)

    def test_far_anchor_never_passes_start_anchor(self, monkeypatch):
        """With a nudge window wider than the leg, the far anchor must still
        stop at the start anchor rather than jump past it (mutation: drop the
        ``t1 - d >= start_t + dedup_floor`` bound). The default 2 mm window
        cannot reach past, so the window is widened for the test."""
        from kicad_tools.pcb import reinforce as reinforce_mod

        monkeypatch.setattr(reinforce_mod, "END_NUDGE_MAX_MM", 6.0)
        monkeypatch.setattr(reinforce_mod, "END_NUDGE_STEPS", 24)
        inset = 1.0 + _ANCHOR_PAD / 2
        length = 2 * inset + _ANCHOR_PAD + 0.5  # room for two, plus 0.5 mm
        pcb = _straight_run_pcb(length=length)
        dy = 0.3 + _ANCHOR_PAD / 2 + 0.1
        # Blocks every far-anchor candidate up to the start anchor's floor.
        pcb.add_trace((23.5, 50 + dy), (40, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
        # Leg is deliberately just long enough for two anchors, below the
        # #8915 default minimum: pin it off to reach the nudge bound.
        result = reinforce_net(
            pcb,
            "FUSED_LINE",
            net_class_clearances={"OTHER": 0.4},
            dry_run=True,
            min_wire_length_mm=0.0,
        )
        (leg,) = result.legs
        assert leg.status == "partial"
        assert len(leg.anchors) == 1
        assert leg.anchors[0].x == pytest.approx(20.0 + inset)
        assert result.refused_count == 1

    def test_two_parallel_chains_are_a_loop(self):
        """Two chains between the same two junctions form a loop even though
        no single chain is closed (mutation: ``>=`` -> ``>``)."""
        pcb = PCB.create(width=200, height=200, center=False)
        for a, b in [
            ((20, 50), (40, 50)),
            ((40, 50), (80, 50)),
            ((40, 50), (60, 70)),
            ((60, 70), (80, 50)),
            ((80, 50), (100, 50)),
        ]:
            pcb.add_trace(a, b, width=1.0, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", all_runs=True, dry_run=True)
        assert len(result.legs) == 4
        assert all(leg.status == "unhandled" for leg in result.legs)
        assert all("loop" in leg.reason for leg in result.legs)
        assert result.placed_count == 0

    def test_back_side_smd_pad_ignored_by_front_leg(self):
        """An SMD pad on B.Cu is not the copper an F.Cu leg ends on
        (mutation: ``on_layer`` always True)."""
        pcb = _straight_run_pcb()
        _add_shaped_pad_footprint(
            pcb,
            ref="U1",
            x=20.0,
            y=50.0,
            net="FUSED_LINE",
            shape="rect",
            size=(6.0, 1.0),
            layers=("B.Cu", "B.Paste", "B.Mask"),
        )
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
        first = min(a.x for a in result.placed)
        assert first - 20.0 == pytest.approx(1.0 + _ANCHOR_PAD / 2, abs=1e-3)

    def test_back_side_smd_pad_measured_by_back_leg(self):
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20, 50), (120, 50), width=2.0, layer="B.Cu", net="N")
        _add_shaped_pad_footprint(
            pcb,
            ref="U1",
            x=20.0,
            y=50.0,
            net="N",
            shape="rect",
            size=(6.0, 1.0),
            layers=("B.Cu", "B.Paste", "B.Mask"),
        )
        result = reinforce_net(pcb, "N", dry_run=True)
        first = min(a.x for a in result.placed)
        assert first - 20.0 == pytest.approx(3.0 + 1.0 + _ANCHOR_PAD / 2, abs=1e-3)

    def test_through_hole_pad_counts_on_every_layer(self):
        """A plated through-hole pad is copper on B.Cu even when its layer
        list names only the front (mutation: THT ``layers=()`` -> ``pad.layers``)."""
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20, 50), (120, 50), width=2.0, layer="B.Cu", net="N")
        _add_shaped_pad_footprint(
            pcb,
            ref="J1",
            x=20.0,
            y=50.0,
            net="N",
            shape="circle",
            size=(3.0, 3.0),
            layers=("F.Cu", "F.Mask"),
            pad_type="thru_hole",
            drill=1.0,
        )
        result = reinforce_net(pcb, "N", dry_run=True)
        first = min(a.x for a in result.placed)
        assert first - 20.0 == pytest.approx(1.5 + 1.0 + _ANCHOR_PAD / 2, abs=1e-3)

    def test_oval_exit_through_rounded_end(self):
        """Leaving an oval through its rounded end: radius is the SHORT half
        side (mutation: ``min`` -> ``max``)."""
        pcb = _straight_run_pcb()
        _add_shaped_pad_footprint(
            pcb, ref="U1", x=20.0, y=50.0, net="FUSED_LINE", shape="oval", size=(4.0, 2.0)
        )
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
        first = min(a.x for a in result.placed)
        assert first - 20.0 == pytest.approx(2.0 + 1.0 + _ANCHOR_PAD / 2, abs=1e-3)

    def test_roundrect_exit_through_rounded_corner(self):
        """Leaving a 2x2 roundrect (rratio 0.25, r = 0.5) through its corner:
        exit at 0.5*sqrt(2) + 0.5, not the sharp corner's sqrt(2) (mutation:
        corner radius 0)."""
        pcb = PCB.create(width=200, height=200, center=False)
        c = math.sqrt(0.5)
        pcb.add_trace((20, 100), (20 + 60 * c, 100 - 60 * c), width=1.0, layer="F.Cu", net="N")
        _add_shaped_pad_footprint(
            pcb, ref="U1", x=20.0, y=100.0, net="N", shape="roundrect", size=(2.0, 2.0)
        )
        result = reinforce_net(pcb, "N", dry_run=True)
        first = min(result.placed, key=lambda a: math.dist((a.x, a.y), (20.0, 100.0)))
        exit_mm = 0.5 * math.sqrt(2.0) + 0.5
        assert math.dist((first.x, first.y), (20.0, 100.0)) == pytest.approx(
            exit_mm + 1.0 + _ANCHOR_PAD / 2, abs=1e-3
        )


class TestEndAnchorCLINoAnchors:
    def test_cli_text_does_not_claim_anchored_run_when_all_refused(self, tmp_path, capsys):
        """Both end anchors refused: the text output must not report an
        anchored run (it used to print ``Anchored run: 4 segment(s), 100.0 mm``)."""
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb = _straight_run_pcb(length=100.0)
        # 0.1 mm from every anchor position: below the 0.2 mm fab floor.
        dy = 0.1 + _ANCHOR_PAD / 2 + 0.1
        pcb.add_trace((10, 50 + dy), (130, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
        pcb_path = tmp_path / "walled.kicad_pcb"
        pcb.save(pcb_path)
        rc = run_pcb_command(_reinforce_args(pcb_path, spacing=None, dry_run=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Anchors placed:  0" in out
        assert "Anchors refused: 2" in out
        assert "Anchored run:   none" in out
        assert "segment(s)" not in out.split("Anchored run:", 1)[1].splitlines()[0]


# --------------------------------------------------------------------------
# ends mode, second review of PR #8751: end copper by overlap (whole trunk,
# track ending just outside a pad), both anchors decided together (never one
# for geometry), zero-length segments. Distances here are computed with
# shapely, independently of the production signed-distance helpers.
# --------------------------------------------------------------------------

_ANCHOR_DRILL = anchor_drill_for_awg(16)


def _pad_poly(shape: str, size, rotation: float, center, rratio: float = 0.25):
    """Pad copper as a shapely polygon: built pad-local, then rotated with
    KiCad's negated angle (``rotate(..., -rotation)`` in shapely's CCW sense)."""
    from shapely import affinity
    from shapely.geometry import LineString, Point, box

    hw, hh = size[0] / 2.0, size[1] / 2.0
    if shape == "circle":
        geom = Point(0, 0).buffer(hw, quad_segs=64)
    elif shape == "oval":
        r = min(hw, hh)
        geom = LineString([(-(hw - r), -(hh - r)), (hw - r, hh - r)]).buffer(r, quad_segs=64)
        if hw == hh:
            geom = Point(0, 0).buffer(r, quad_segs=64)
    elif shape == "roundrect":
        r = 2.0 * min(hw, hh) * min(rratio, 0.5)
        geom = box(-(hw - r), -(hh - r), hw - r, hh - r).buffer(r, quad_segs=64)
    else:
        geom = box(-hw, -hh, hw, hh)
    geom = affinity.rotate(geom, -rotation, origin=(0, 0))
    return affinity.translate(geom, center[0], center[1])


def _track_poly(points, width: float):
    from shapely.geometry import LineString

    return LineString(points).buffer(width / 2.0, quad_segs=64)


def _anchor_gaps(anchor, copper) -> tuple[float, float]:
    """(pad-edge gap, drill-edge gap) from an anchor to ``copper``."""
    from shapely.geometry import Point

    d = copper.distance(Point(anchor.x, anchor.y))
    if copper.contains(Point(anchor.x, anchor.y)):
        d = -d
    return d - _ANCHOR_PAD / 2, d - _ANCHOR_DRILL / 2


def _y_pcb(trunk_pts, branch_pts, width: float = 2.0) -> PCB:
    pcb = PCB.create(width=300, height=300, center=False)
    for pts in (trunk_pts, branch_pts):
        for a, b in zip(pts[:-1], pts[1:], strict=True):
            pcb.add_trace(a, b, width=width, layer="F.Cu", net="N")
    return pcb


def _fragmented(a, b, piece: float) -> list[tuple[float, float]]:
    n = max(1, round(math.dist(a, b) / piece))
    return [(a[0] + (b[0] - a[0]) * k / n, a[1] + (b[1] - a[1]) * k / n) for k in range(n + 1)]


def _junction_anchor(branch, junction):
    return min(branch.anchors, key=lambda a: math.dist((a.x, a.y), junction))


class TestEndCopperWholeTrunk:
    """Finding 1: a branch's junction anchor cleared only the trunk pieces
    that contained the junction, so a fragmented or bent trunk let it sit on
    trunk copper (1 mm fragments at 15 deg: pad 0.704 mm inside the trunk)."""

    @pytest.mark.parametrize(
        ("piece", "angle_deg"),
        [(1.0, 15.0), (1.0, 30.0), (1.0, 150.0), (0.5, 30.0), (10.0, 15.0), (1.0, 60.0)],
    )
    def test_branch_clears_fragmented_trunk(self, piece, angle_deg):
        trunk = _fragmented((20.0, 100.0), (100.0, 100.0), piece)[:-1] + _fragmented(
            (100.0, 100.0), (180.0, 100.0), piece
        )
        c, s = math.cos(math.radians(angle_deg)), math.sin(math.radians(angle_deg))
        branch_pts = [(100.0, 100.0), (100.0 + 30 * c, 100.0 + 30 * s)]
        result = reinforce_net(_y_pcb(trunk, branch_pts), "N", all_runs=True, dry_run=True)
        branch = next(leg for leg in result.legs if leg.kind == "branch")
        assert branch.status == "full" and len(branch.anchors) == 2
        trunk_copper = _track_poly(trunk, 2.0)
        gaps = [_anchor_gaps(a, trunk_copper)[0] for a in branch.anchors]
        assert min(gaps) >= 1.0 - 1e-3
        # The junction anchor sits exactly on the clearance, not further in.
        assert _anchor_gaps(_junction_anchor(branch, (100.0, 100.0)), trunk_copper)[
            0
        ] == pytest.approx(1.0, abs=1e-3)

    @pytest.mark.parametrize("side", [1.0, -1.0])
    def test_branch_clears_trunk_bend_past_junction(self, side):
        """Judge fixture: trunk arm bends 45 deg 1.5 mm past the junction;
        a 60 deg branch toward the bend put its pad 0.015 mm onto the trunk.
        Also kills the ``_segment_distance`` clamp mutation: an unclamped
        (infinite-line) distance pushes the anchor off the exact clearance."""
        trunk = [(20.0, 100.0), (100.0, 100.0), (101.5, 100.0), (141.5, 100.0 - side * 40.0)]
        c, s = math.cos(math.radians(60.0)), -side * math.sin(math.radians(60.0))
        branch_pts = [(100.0, 100.0), (100.0 + 30 * c, 100.0 + 30 * s)]
        result = reinforce_net(_y_pcb(trunk, branch_pts), "N", all_runs=True, dry_run=True)
        branch = next(leg for leg in result.legs if leg.kind == "branch")
        assert branch.status == "full" and len(branch.anchors) == 2
        trunk_copper = _track_poly(trunk, 2.0)
        junction = _junction_anchor(branch, (100.0, 100.0))
        assert _anchor_gaps(junction, trunk_copper)[0] == pytest.approx(1.0, abs=1e-3)
        far = next(a for a in branch.anchors if a is not junction)
        assert _anchor_gaps(far, trunk_copper)[0] >= 1.0 - 1e-3


def _folded_leg_pcb(jog: float, rotation: float, shape: str = "oval") -> PCB:
    """Judge fixture: two 2x6 pads 6 mm apart joined by a track with a
    ``jog`` mm detour, so the leg folds back on itself."""
    pcb = PCB.create(width=200, height=200, center=False)
    for ref, x in (("U1", 40.0), ("U2", 46.0)):
        _add_shaped_pad_footprint(
            pcb, ref=ref, x=x, y=100.0, net="N", shape=shape, size=(2.0, 6.0), rotation=rotation
        )
    pts = [
        (40.0, 100.0),
        (41.8, 100.0),
        (41.8, 100.0 - jog),
        (44.2, 100.0 - jog),
        (44.2, 100.0),
        (46.0, 100.0),
    ]
    for a, b in zip(pts[:-1], pts[1:], strict=True):
        pcb.add_trace(a, b, width=1.0, layer="F.Cu", net="N")
    return pcb


class TestEndAnchorPairNeverOne:
    """Finding 2: the too_short gate measured the anchor gap ALONG the leg,
    placement STRAIGHT-LINE; a folded leg passed the first, failed the
    second, and wrote ONE via with no rule behind the refusal."""

    def test_folded_leg_places_none_not_one(self):
        pcb = _folded_leg_pcb(3.0, 45.0)
        before = len(pcb.vias)
        result = reinforce_net(pcb, "N", min_wire_length_mm=0.0)
        (leg,) = result.legs
        assert leg.status == "too_short"
        assert leg.anchors == [] and result.placed_count == 0
        assert result.refused_count == 0
        assert len(pcb.vias) == before

    def test_folded_leg_straight_line_gate(self):
        """A U-shaped leg whose two anchor targets are far apart along the
        track but only 1.0 mm apart straight-line: the straight-line gate,
        not the along-leg one, makes it ``too_short``."""
        pcb = PCB.create(width=200, height=200, center=False)
        for ref, x in (("U1", 40.0), ("U2", 44.0)):
            _add_shaped_pad_footprint(
                pcb, ref=ref, x=x, y=100.0, net="N", shape="rect", size=(1.0, 1.0)
            )
        pts = [
            (40.0, 100.0),
            (41.5, 100.0),
            (41.5, 97.0),
            (42.5, 97.0),
            (42.5, 100.0),
            (44.0, 100.0),
        ]
        for a, b in zip(pts[:-1], pts[1:], strict=True):
            pcb.add_trace(a, b, width=0.5, layer="F.Cu", net="N")
        before = len(pcb.vias)
        result = reinforce_net(pcb, "N", min_wire_length_mm=0.0)
        (leg,) = result.legs
        assert leg.status == "too_short"
        assert leg.anchors == [] and result.refused_count == 0
        assert len(pcb.vias) == before
        assert "folds back" in leg.reason

    @pytest.mark.parametrize("shape", ["oval", "rect"])
    @pytest.mark.parametrize("rotation", [0.0, 30.0, 45.0, 90.0, 135.0])
    @pytest.mark.parametrize("jog", [1.5, 2.0, 2.5, 3.0, 3.5, 4.0, 5.0, 6.0])
    def test_folded_leg_sweep_zero_or_two(self, jog, rotation, shape):
        pcb = _folded_leg_pcb(jog, rotation, shape)
        before = len(pcb.vias)
        # The whole sweep is sub-15 mm folded legs: the #8915 minimum is
        # pinned off so the straight-line pairing gate is what decides.
        result = reinforce_net(pcb, "N", min_wire_length_mm=0.0)
        (leg,) = result.legs
        assert len(leg.anchors) in (0, 2)
        assert leg.status in ("full", "too_short")
        assert len(pcb.vias) - before == len(leg.anchors)
        if len(leg.anchors) == 2:
            a, b = leg.anchors
            assert math.dist((a.x, a.y), (b.x, b.y)) >= _ANCHOR_PAD - 1e-9

    def test_rule_refusal_partial_keeps_its_rule(self):
        """``partial`` remains only for a real rule refusal: the one
        refused anchor carries the rule that refused it."""
        result = reinforce_net(_blocked_by_foreign_track(0.05), "FUSED_LINE", dry_run=True)
        for leg in result.legs:
            assert len(leg.anchors) != 1 or leg.status == "partial"
        assert all(r.rule for r in result.refused)


class TestEndCopperOverlap:
    """Finding 3: a track ending just OUTSIDE the pad edge, its copper still
    on the pad, was not treated as landing on it; the drill went 0.108 mm
    into the pad."""

    @pytest.mark.parametrize("start_x", [41.05, 41.2, 41.45])
    def test_track_end_just_outside_pad_edge(self, start_x):
        pcb = PCB.create(width=200, height=200, center=False)
        _add_shaped_pad_footprint(
            pcb, ref="U1", x=40.0, y=100.0, net="N", shape="rect", size=(2.0, 6.0)
        )
        pts = [(start_x, 100.0), (41.6, 100.0), (41.6, 60.0), (80.0, 60.0)]
        for a, b in zip(pts[:-1], pts[1:], strict=True):
            pcb.add_trace(a, b, width=1.0, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", dry_run=True)
        (leg,) = result.legs
        assert leg.status == "full" and len(leg.anchors) == 2
        pad = _pad_poly("rect", (2.0, 6.0), 0.0, (40.0, 100.0))
        near = min(leg.anchors, key=lambda a: math.dist((a.x, a.y), (40.0, 100.0)))
        pad_gap, drill_gap = _anchor_gaps(near, pad)
        assert pad_gap == pytest.approx(1.0, abs=1e-3)
        assert drill_gap > 1.0

    def test_track_end_clear_of_pad_is_free_end(self):
        """A track whose copper does not reach the pad is a free end: the
        anchor is measured from the track end, not kept off the pad."""
        pcb = PCB.create(width=200, height=200, center=False)
        _add_shaped_pad_footprint(
            pcb, ref="U1", x=40.0, y=100.0, net="N", shape="rect", size=(2.0, 6.0)
        )
        pcb.add_trace((41.6, 100.0), (90.0, 100.0), width=1.0, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", dry_run=True)
        near = min(result.placed, key=lambda a: a.x)
        assert near.x == pytest.approx(41.6 + 1.0 + _ANCHOR_PAD / 2)


class TestEndAnchorReviewMutations:
    def test_zero_length_segment_does_not_make_a_loop(self):
        """A zero-length segment used to mark the leg ``unhandled`` as a
        'copper loop'; it carries no copper of its own and is dropped."""
        pcb = _padded_straight_pcb()
        pcb.add_trace((70.0, 50.0), (70.0, 50.0), width=2.0, layer="F.Cu", net="FUSED_LINE")
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
        (leg,) = result.legs
        assert leg.status == "full" and len(leg.anchors) == 2
        assert "loop" not in leg.reason

    def test_nudged_start_candidates_respect_end_clearance(self):
        """The leg turns back toward its pad right after the start target,
        and a foreign track refuses the target. Every inward nudge is inside
        the end clearance, so the start anchor is refused (a rule refusal)
        rather than nudged onto a spot within 1 mm of the pad (mutation:
        nudged positions not checked against the end clearance)."""
        pcb = PCB.create(width=200, height=200, center=False)
        _add_shaped_pad_footprint(
            pcb, ref="U1", x=20.0, y=50.0, net="N", shape="rect", size=(2.0, 2.0)
        )
        pts = [(20.0, 50.0), (23.0, 50.0), (21.5, 51.8), (21.5, 90.0)]
        for a, b in zip(pts[:-1], pts[1:], strict=True):
            pcb.add_trace(a, b, width=0.5, layer="F.Cu", net="N")
        dy = 0.3 + _ANCHOR_PAD / 2 + 0.1
        pcb.add_trace((22.0, 50 - dy), (24.5, 50 - dy), width=0.2, layer="F.Cu", net="OTHER")
        result = reinforce_net(
            pcb, "N", net_class_clearances={"OTHER": 0.4}, max_end_shift_mm=2.0, dry_run=True
        )
        pad = _pad_poly("rect", (2.0, 2.0), 0.0, (20.0, 50.0))
        for a in result.placed:
            assert _anchor_gaps(a, pad)[0] >= 1.0 - 1e-3
        (leg,) = result.legs
        assert leg.status == "partial" and len(leg.anchors) == 1
        assert [r.rule for r in result.refused] != [""] and all(r.rule for r in result.refused)

    def test_leg_wholly_inside_its_pad_is_reported_as_covered(self):
        """Kills ``whole leg covered -> None`` turned into ``continue``: the
        leg is too short for the right reason."""
        pcb = PCB.create(width=200, height=200, center=False)
        _add_shaped_pad_footprint(
            pcb, ref="U1", x=20.0, y=50.0, net="N", shape="rect", size=(40.0, 10.0)
        )
        pcb.add_trace((10.0, 50.0), (30.0, 50.0), width=1.0, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", dry_run=True)
        (leg,) = result.legs
        assert leg.status == "too_short"
        assert leg.reason == "same-net pad/via copper covers the whole leg"
        assert result.placed_count == 0

    def test_roundrect_ratio_above_half_is_clamped(self):
        """KiCad caps the corner ratio at 0.5 (a fully rounded end); a
        larger stored ratio must measure like the 0.5 shape, i.e. an oval.
        The leg leaves through the rounded short end (mutation: ratio clamp
        removed, which shrinks that end inward)."""
        anchors = []
        for shape, rratio in (("oval", 0.25), ("roundrect", 0.5), ("roundrect", 0.9)):
            pcb = _straight_run_pcb()
            _add_shaped_pad_footprint(
                pcb, ref="U1", x=20.0, y=50.0, net="FUSED_LINE", shape=shape, size=(4.0, 2.0)
            )
            pcb.footprints[-1].pads[0].roundrect_rratio = rratio
            result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
            anchors.append(min(a.x for a in result.placed))
        assert anchors[0] == pytest.approx(20.0 + 2.0 + 1.0 + _ANCHOR_PAD / 2, abs=1e-3)
        assert anchors[1] == pytest.approx(anchors[0], abs=1e-6)
        assert anchors[2] == pytest.approx(anchors[0], abs=1e-6)

    def test_segment_distance_is_to_the_segment_not_its_line(self):
        """A branch parallel to the BACKWARD extension of a bent trunk arm,
        1.06 mm off that line but nowhere near the arm itself, is anchored
        normally (mutation: ``_segment_distance`` clamp removed turns every
        trunk piece into an infinite line, and this leg into too_short)."""
        trunk = [(20.0, 100.0), (100.0, 100.0), (101.5, 100.0), (141.5, 60.0)]
        branch_pts = [(100.0, 100.0), (100.0 - 21.0, 100.0 + 21.0)]
        result = reinforce_net(_y_pcb(trunk, branch_pts), "N", all_runs=True, dry_run=True)
        branch = next(leg for leg in result.legs if leg.kind == "branch")
        assert branch.status == "full" and len(branch.anchors) == 2
        trunk_copper = _track_poly(trunk, 2.0)
        junction = _junction_anchor(branch, (100.0, 100.0))
        assert _anchor_gaps(junction, trunk_copper)[0] == pytest.approx(1.0, abs=1e-3)

    def test_t_joined_copper_counts_as_one_end_feature(self):
        """A leg starts on the middle of a stub that is itself T-joined onto
        a long same-net track (no shared vertices anywhere). The stub and
        the long track are one piece of copper, so the leg's anchors keep
        1 mm from the long track too (mutation: T-joins not grouped)."""
        pcb = PCB.create(width=200, height=200, center=False)
        pcb.add_trace((20.0, 100.0), (180.0, 100.0), width=2.0, layer="F.Cu", net="N")
        pcb.add_trace((100.0, 100.0), (100.0, 80.0), width=2.0, layer="F.Cu", net="N")
        pcb.add_trace((100.0, 97.6), (140.0, 97.6), width=1.0, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", all_runs=True, dry_run=True)
        long_track = _track_poly([(20.0, 100.0), (180.0, 100.0)], 2.0)
        leg = next(lg for lg in result.legs if lg.polyline[0][1] == pytest.approx(97.6))
        assert len(leg.anchors) in (0, 2)
        for a in leg.anchors:
            assert _anchor_gaps(a, long_track)[0] >= 1.0 - 1e-3
        assert leg.status == "too_short"

    @staticmethod
    def _two_close_legs(gap: float = 1.0, rules: DesignRules | None = None):
        pcb = PCB.create(width=200, height=200, center=False)
        pcb.add_trace((20.0, 100.0), (80.0, 100.0), width=0.3, layer="F.Cu", net="N")
        pcb.add_trace((20.0, 100.0 + gap), (79.0, 100.0 + gap), width=0.3, layer="F.Cu", net="N")
        return reinforce_net(pcb, "N", all_runs=True, dry_run=True, design_rules=rules)

    def test_anchor_does_not_overlap_another_legs_anchor(self):
        """Two separate same-net legs 1 mm apart: the second leg's anchors
        are nudged inward off the first leg's (mutation: other legs'
        anchors ignored, which stacks them 1 mm apart)."""
        result = self._two_close_legs()
        placed = [(a.x, a.y) for a in result.placed]
        for i, p in enumerate(placed):
            for q in placed[i + 1 :]:
                assert math.dist(p, q) >= _ANCHOR_PAD - 1e-9
        first, second = result.legs
        assert first.status == "full" and second.status == "full"
        assert min(a.x for a in second.anchors) > min(a.x for a in first.anchors)

    @pytest.mark.parametrize(
        ("gap", "annular", "hole_to_hole", "rule"),
        [(1.0, 0.25, 0.5, "hole_to_hole"), (1.9, 0.4, 0.2, "anchor_spacing")],
    )
    def test_anchor_overlap_without_nudge_room_is_a_named_refusal(
        self, monkeypatch, gap, annular, hole_to_hole, rule
    ):
        """With no nudge window, both of the second leg's anchors are
        refused with a named rule and the leg places none: ``hole_to_hole``
        when the drills are too close, ``anchor_spacing`` when only the
        (larger-ring) anchor pads overlap."""
        from kicad_tools.pcb import reinforce as reinforce_mod

        monkeypatch.setattr(reinforce_mod, "END_NUDGE_MAX_MM", 0.0)
        rules = DesignRules(
            min_trace_width_mm=0.127,
            min_clearance_mm=0.2,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=annular,
            min_hole_to_hole_mm=hole_to_hole,
        )
        result = self._two_close_legs(gap, rules)
        first, second = result.legs
        assert first.status == "full" and len(first.anchors) == 2
        assert second.status == "unanchored" and second.anchors == []
        assert [r.rule for r in result.refused] == [rule, rule]


class TestEndAnchorEmptyAndHoleSpacing:
    """Issue #8780: zero-length-only nets and the ``dedup_floor`` drill term."""

    def test_zero_length_only_net_raises_reinforce_error(self):
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20.0, 50.0), (20.0, 50.0), width=2.0, layer="F.Cu", net="P")
        with pytest.raises(ReinforceError, match="no walkable polyline"):
            reinforce_net(pcb, "P", dry_run=True)

    def test_zero_length_on_one_layer_real_copper_on_another(self):
        pcb = _padded_straight_pcb()
        pcb.add_trace((70.0, 50.0), (70.0, 50.0), width=2.0, layer="B.Cu", net="FUSED_LINE")
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
        assert result.placed_count == 2

    @staticmethod
    def _rules() -> DesignRules:
        return DesignRules(
            min_trace_width_mm=0.127,
            min_clearance_mm=0.2,
            min_via_drill_mm=0.3,
            min_via_diameter_mm=0.6,
            min_annular_ring_mm=0.25,
            min_hole_to_hole_mm=0.8,
        )

    @pytest.mark.parametrize("gap", [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 4.0])
    def test_anchors_keep_hole_to_hole_with_drill_term(self, gap):
        """The drill term wins (``drill + 0.8 > anchor_pad``): no two anchors,
        on one leg or on adjacent legs, sit closer than
        ``anchor_drill + min_hole_to_hole``. Fails if ``dedup_floor`` is
        replaced by ``anchor_pad`` in ``plan_pair`` or ``violation_at``."""
        rules = self._rules()
        floor = _ANCHOR_DRILL + rules.min_hole_to_hole_mm
        assert floor > _ANCHOR_PAD + 1e-6
        # Two short collinear legs ``gap`` apart; lengths chosen so the
        # two anchors of a leg fall between the pad and drill-term floors.
        leg_len = 2 * (1.0 + _ANCHOR_PAD / 2) + _ANCHOR_PAD + 0.1
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20.0, 50.0), (20.0 + leg_len, 50.0), width=0.5, layer="F.Cu", net="N")
        x2 = 20.0 + leg_len + gap
        pcb.add_trace((x2, 50.0), (x2 + leg_len, 50.0), width=0.5, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", design_rules=rules, dry_run=True)
        pts = [(a.x, a.y) for a in result.placed]
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
                assert d >= floor - 1e-6, (gap, pts)

    @pytest.mark.parametrize("gap", [0.05, 0.1, 0.2, 0.28])
    def test_adjacent_legs_keep_hole_to_hole_with_drill_term(self, gap):
        """Adjacent legs whose facing end anchors would land between the pad
        floor and the drill-term floor: the later leg's anchor must be
        nudged/refused, not placed (kills ``dedup_floor -> anchor_pad`` in
        ``violation_at``)."""
        rules = self._rules()
        floor = _ANCHOR_DRILL + rules.min_hole_to_hole_mm
        assert _ANCHOR_PAD < _ANCHOR_PAD + gap < floor
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20.0, 50.0), (30.0, 50.0), width=0.5, layer="F.Cu", net="N")
        pcb.add_trace((30.0 + gap, 50.0), (40.0 + gap, 50.0), width=0.5, layer="F.Cu", net="N")
        result = reinforce_net(
            pcb,
            "N",
            design_rules=rules,
            end_offset_mm=0.0,
            all_runs=True,
            dry_run=True,
            min_wire_length_mm=0.0,
        )
        assert result.placed_count >= 2
        pts = [(a.x, a.y) for a in result.placed]
        for i in range(len(pts)):
            for j in range(i + 1, len(pts)):
                d = math.hypot(pts[i][0] - pts[j][0], pts[i][1] - pts[j][1])
                assert d >= floor - 1e-6, (gap, pts)


def _random_pad(rng):
    shape = rng.choice(["rect", "oval", "roundrect", "circle"])
    if shape == "circle":
        d = rng.uniform(1.0, 4.0)
        return shape, (d, d), 0.0
    return shape, (rng.uniform(1.0, 4.0), rng.uniform(1.0, 7.0)), rng.choice([0, 30, 45, 90, 135])


class TestEndAnchorPropertySweep:
    """Seeded random sweep: every anchored leg has exactly 2 anchors, and
    every anchor's pad AND drill is >= the end offset from all copper the
    leg ends on (pads it overlaps; for a branch, the whole trunk). Gaps are
    measured with shapely, not the production helpers."""

    CASES = 300

    def _check(self, pcb, pads, legs_copper, result):
        for leg in result.legs:
            assert len(leg.anchors) in (0, 2), (leg.status, leg.reason)
            assert leg.status != "partial", leg.reason  # no foreign copper here
            if not leg.anchors:
                continue
            a, b = leg.anchors
            assert math.dist((a.x, a.y), (b.x, b.y)) >= _ANCHOR_PAD - 1e-9
            from shapely.geometry import Point

            end_copper = []
            for end in (leg.polyline[0], leg.polyline[-1]):
                cap = Point(end).buffer(leg.width_mm / 2.0, quad_segs=64)
                end_copper += [p for p in pads if p.intersects(cap)]
            if leg.kind == "branch":
                end_copper.append(legs_copper["trunk"])
            for anchor in leg.anchors:
                for copper in end_copper:
                    pad_gap, drill_gap = _anchor_gaps(anchor, copper)
                    assert pad_gap >= 1.0 - 1e-3
                    assert drill_gap >= 1.0 - 1e-3

    def test_random_bent_legs_between_pads(self):
        import random

        rng = random.Random(8742)
        anchored = 0
        for _ in range(self.CASES):
            pcb = PCB.create(width=300, height=300, center=False)
            width = rng.uniform(0.4, 2.0)
            c0 = (100.0, 100.0)
            c1 = (100.0 + rng.uniform(4.0, 25.0), 100.0 + rng.uniform(-4.0, 4.0))
            pads = []
            for ref, c in (("U1", c0), ("U2", c1)):
                shape, size, rot = _random_pad(rng)
                _add_shaped_pad_footprint(
                    pcb, ref=ref, x=c[0], y=c[1], net="N", shape=shape, size=size, rotation=rot
                )
                pads.append(_pad_poly(shape, size, rot, c))
            # Leave from the pad centre, or from just past its edge (copper
            # still overlapping it), then wander with sharp bends.
            start = c0
            if rng.random() < 0.3:
                edge = pads[0].exterior.coords
                ex = max(edge, key=lambda p: p[0])
                start = (ex[0] + rng.uniform(0.0, 0.45 * width), ex[1])
            pts = [start]
            for _k in range(rng.randint(1, 4)):
                pts.append((rng.uniform(c0[0] - 3, c1[0] + 3), rng.uniform(c0[1] - 8, c0[1] + 8)))
            pts.append(c1)
            pts = [p for i, p in enumerate(pts) if i == 0 or math.dist(p, pts[i - 1]) > 0.05]
            for a, b in zip(pts[:-1], pts[1:], strict=True):
                pcb.add_trace(a, b, width=width, layer="F.Cu", net="N")
            result = reinforce_net(pcb, "N", all_runs=True, dry_run=True)
            self._check(pcb, pads, {}, result)
            anchored += sum(1 for leg in result.legs if leg.anchors)
        assert anchored > self.CASES // 4  # the sweep is not vacuous

    def test_random_branches_off_bent_fragmented_trunks(self):
        import random

        rng = random.Random(87420)
        anchored = 0
        for _ in range(self.CASES // 2):
            width = rng.uniform(0.5, 3.0)
            piece = rng.choice([0.5, 1.0, 3.0, 100.0])
            bend = rng.uniform(1.0, 6.0)
            turn = math.radians(rng.choice([-60, -45, -30, 30, 45, 60]))
            far = (100.0 + bend + 50 * math.cos(turn), 100.0 + 50 * math.sin(turn))
            trunk = (
                _fragmented((20.0, 100.0), (100.0, 100.0), piece)[:-1]
                + _fragmented((100.0, 100.0), (100.0 + bend, 100.0), piece)[:-1]
                + _fragmented((100.0 + bend, 100.0), far, piece)
            )
            ang = math.radians(rng.choice([15, 25, 40, 60, 90, 120, 150, 165, -30, -60, -150]))
            blen = rng.uniform(12.0, 30.0)
            branch_pts = [(100.0, 100.0), (100 + blen * math.cos(ang), 100 + blen * math.sin(ang))]
            pcb = _y_pcb(trunk, branch_pts, width=width)
            result = reinforce_net(pcb, "N", all_runs=True, dry_run=True)
            kinds = {leg.kind for leg in result.legs}
            if "branch" not in kinds:
                continue  # the branch crossed the trunk: not a single Y
            self._check(pcb, [], {"trunk": _track_poly(trunk, width)}, result)
            anchored += sum(1 for leg in result.legs if leg.kind == "branch" and leg.anchors)
        assert anchored > self.CASES // 8


# --------------------------------------------------------------------------
# --clear / --replace (Issue #8747)
# --------------------------------------------------------------------------


def _anchor_dims(gauge: int = 16) -> tuple[float, float]:
    from kicad_tools.pcb.reinforce import _FALLBACK_DESIGN_RULES

    drill = anchor_drill_for_awg(gauge)
    return drill, anchor_pad_for_drill(drill, _FALLBACK_DESIGN_RULES.min_annular_ring_mm)


def _anchor_vias(pcb: PCB, net: str = "FUSED_LINE") -> list:
    drill, pad = _anchor_dims()
    return [
        v
        for v in pcb.vias
        if v.net_name == net and abs(v.drill - drill) < 1e-3 and abs(v.size - pad) < 1e-3
    ]


def _spaced_reinforced_pcb() -> PCB:
    pcb = _padded_straight_pcb(length=100.0)
    res = reinforce_net(pcb, "FUSED_LINE", anchor_mode="spaced", spacing_mm=20.0)
    assert res.placed_count > 2
    return pcb


class TestClearAnchors:
    def test_clear_removes_only_anchor_sized_same_net_vias(self):
        from kicad_tools.pcb.reinforce import clear_anchors

        pcb = _spaced_reinforced_pcb()
        n = len(_anchor_vias(pcb))
        pcb.add_via(60.0, 52.5, size=0.8, drill=0.4, net="FUSED_LINE")  # other size
        pcb.add_trace((30, 60), (60, 60), width=1.0, layer="F.Cu", net="OTHER")
        drill, pad = _anchor_dims()
        pcb.add_via(40.0, 60.0, size=pad, drill=drill, net="OTHER")  # other net

        res = clear_anchors(pcb, "FUSED_LINE")
        assert res.removed_count == n
        assert res.refused_count == 0
        assert _anchor_vias(pcb) == []
        assert len(pcb.vias) == 2  # the other-size and other-net vias survive

    def test_dry_run_lists_but_does_not_remove(self):
        from kicad_tools.pcb.reinforce import clear_anchors

        pcb = _spaced_reinforced_pcb()
        n = len(_anchor_vias(pcb))
        res = clear_anchors(pcb, "FUSED_LINE", dry_run=True)
        assert res.removed_count == n
        assert len(_anchor_vias(pcb)) == n

    def test_nothing_to_clear(self):
        from kicad_tools.pcb.reinforce import clear_anchors

        res = clear_anchors(_padded_straight_pcb(), "FUSED_LINE")
        assert res.removed_count == 0 and res.refused_count == 0

    def test_unknown_net_raises(self):
        from kicad_tools.pcb.reinforce import clear_anchors

        with pytest.raises(ReinforceError):
            clear_anchors(_padded_straight_pcb(), "NOPE")

    def test_only_layer_link_is_refused(self):
        from kicad_tools.pcb.reinforce import clear_anchors

        pcb = PCB.create(width=200, height=120, center=False)
        _add_pad_footprint(pcb, ref="RSH1", x=20, y=50, net="PGND")
        _add_pad_footprint(pcb, ref="J2", x=120, y=50, net="PGND")
        pcb.add_trace((20, 50), (70, 50), width=3.0, layer="F.Cu", net="PGND")
        drill, pad = _anchor_dims()
        pcb.add_via(70.0, 50.0, size=pad, drill=drill, layers=("F.Cu", "B.Cu"), net="PGND")
        pcb.add_trace((70, 50), (120, 50), width=3.0, layer="B.Cu", net="PGND")
        # A leaf anchor mid-run on F.Cu: removable.
        pcb.add_via(45.0, 50.0, size=pad, drill=drill, layers=("F.Cu", "B.Cu"), net="PGND")

        res = clear_anchors(pcb, "PGND")
        assert res.removed_count == 1
        assert res.refused_count == 1
        assert (res.refused[0].x, res.refused[0].y) == pytest.approx((70.0, 50.0))
        assert len(pcb.vias) == 1


class TestReinforceClearCLI:
    def _board(self, tmp_path) -> Path:
        pcb = _spaced_reinforced_pcb()
        path = tmp_path / "board.kicad_pcb"
        pcb.save(path)
        return path

    def test_clear_dry_run_writes_nothing(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        path = self._board(tmp_path)
        before = path.read_text()
        n = len(_anchor_vias(PCB.load(path)))
        rc = run_pcb_command(_reinforce_args(path, clear=True, dry_run=True))
        assert rc == 0
        assert path.read_text() == before
        assert f"{n} (would be removed)" in capsys.readouterr().out

    def test_clear_removes_all_and_places_none(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        path = self._board(tmp_path)
        rc = run_pcb_command(_reinforce_args(path, clear=True))
        assert rc == 0
        assert _anchor_vias(PCB.load(path)) == []
        assert "refill zone pours" in capsys.readouterr().out

    def test_clear_nothing_to_clear_exit_0(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb = _padded_straight_pcb()
        path = tmp_path / "b.kicad_pcb"
        pcb.save(path)
        assert run_pcb_command(_reinforce_args(path, clear=True)) == 0
        assert "Nothing to clear" in capsys.readouterr().out

    def test_replace_leaves_exactly_two_end_anchors(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        path = self._board(tmp_path)
        pcb = PCB.load(path)
        pcb.add_via(60.0, 53.0, size=0.8, drill=0.4, net="FUSED_LINE")  # unrelated
        pcb.save(path)
        rc = run_pcb_command(_reinforce_args(path, replace=True, anchor_mode="ends"))
        assert rc == 0
        out = PCB.load(path)
        assert len(_anchor_vias(out)) == 2
        assert len(out.vias) == 3

    def test_clear_and_replace_are_exclusive(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        path = self._board(tmp_path)
        assert run_pcb_command(_reinforce_args(path, clear=True, replace=True)) == 1


class TestReinforceClearMaskAndSidecar:
    """--clear/--replace remove the net's mask openings and sidecar wires (#8747)."""

    def _reinforced(self, tmp_path) -> Path:
        from kicad_tools.cli.commands.pcb import run_pcb_command

        path = tmp_path / "board.kicad_pcb"
        _padded_straight_pcb().save(path)
        assert run_pcb_command(_reinforce_args(path, anchor_mode="ends")) == 0
        pcb = PCB.load(path)
        assert len(_mask_lines(pcb, "F.Mask")) == 1
        assert len(_anchor_vias(pcb)) == 2
        assert (tmp_path / "board.kicad_pcb.reinforcement.json").is_file()
        return path

    def _wires(self, path: Path) -> list:
        from kicad_tools.pcb.reinforcement_sidecar import load_wires, sidecar_path

        return load_wires(sidecar_path(path))

    def test_clear_removes_openings_and_wire_records(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        path = self._reinforced(tmp_path)
        capsys.readouterr()
        assert run_pcb_command(_reinforce_args(path, clear=True)) == 0
        out = PCB.load(path)
        assert _anchor_vias(out) == []
        assert _mask_lines(out, "F.Mask") == []
        assert self._wires(path) == []
        assert "Mask openings cleared: 1" in capsys.readouterr().out

    def test_clear_dry_run_reports_openings_writes_nothing(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        path = self._reinforced(tmp_path)
        sidecar = path.with_name(path.name + ".reinforcement.json")
        before, side_before = path.read_text(), sidecar.read_text()
        capsys.readouterr()
        assert run_pcb_command(_reinforce_args(path, clear=True, dry_run=True)) == 0
        assert path.read_text() == before and sidecar.read_text() == side_before
        assert "Mask openings cleared: 1 (would be removed)" in capsys.readouterr().out

    def test_replace_leaves_no_stale_openings_or_wires(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        path = self._reinforced(tmp_path)
        # Move the anchors: a different end offset, so stale ones cannot coincide.
        rc = run_pcb_command(
            _reinforce_args(path, replace=True, anchor_mode="ends", end_offset=5.0)
        )
        assert rc == 0
        out = PCB.load(path)
        vias = _anchor_vias(out)
        assert len(vias) == 2
        lines = _mask_lines(out, "F.Mask")
        assert len(lines) == 1
        xs = sorted(v.position[0] for v in vias)
        assert sorted([lines[0].start[0], lines[0].end[0]]) == pytest.approx(xs, abs=0.02)
        wires = self._wires(path)
        assert len(wires) == 1
        assert sorted(a["x"] for a in wires[0]["anchors"]) == pytest.approx(xs, abs=0.02)

    def test_sidecar_identifies_anchor_not_matching_size(self, tmp_path):
        from kicad_tools.pcb.reinforce import clear_anchors
        from kicad_tools.pcb.reinforcement_sidecar import load_wires, sidecar_path

        path = self._reinforced(tmp_path)
        wires = load_wires(sidecar_path(path))
        pcb = PCB.load(path)
        # Anchor placed at a different gauge: the size rule misses it, the sidecar finds it.
        res = clear_anchors(pcb, "FUSED_LINE", wire_gauge_awg=12, sidecar_wires=wires)
        assert res.identified_by == "sidecar" and res.removed_count == 2
        assert len(res.openings_removed) == 1
        res = clear_anchors(PCB.load(path), "FUSED_LINE", wire_gauge_awg=12)
        assert res.identified_by == "size" and res.removed_count == 0

    def test_other_net_openings_and_wires_survive(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command
        from kicad_tools.pcb.reinforcement_sidecar import sidecar_path

        path = self._reinforced(tmp_path)
        other = _straight_run_pcb(net="OTHER", length=100.0)
        pcb = PCB.load(path)
        pcb.add_trace((20, 80), (120, 80), width=2.0, layer="F.Cu", net="OTHER")
        pcb.add_graphic_line((20, 80), (120, 80), "F.Mask", width=2.0)
        pcb.save(path)
        del other
        side = sidecar_path(path)
        wires = json.loads(side.read_text())
        wires["wires"].append({**wires["wires"][0], "net": "OTHER"})
        side.write_text(json.dumps(wires))
        assert run_pcb_command(_reinforce_args(path, clear=True)) == 0
        out = PCB.load(path)
        assert len(_mask_lines(out, "F.Mask")) == 1  # OTHER's opening
        assert [w["net"] for w in self._wires(path)] == ["OTHER"]


# --------------------------------------------------------------------------
# Solder-mask opening + tenting-off anchors (#8743)
# --------------------------------------------------------------------------


def _mask_lines(pcb: PCB, layer: str):
    return [g for g in pcb.graphic_lines if g.layer == layer]


class TestMaskOpening:
    def test_straight_run_opens_full_span_on_own_side(self, tmp_path):
        pcb = _straight_run_pcb(length=100.0)
        result = reinforce_net(pcb, "FUSED_LINE")
        (leg,) = result.legs
        assert leg.status == "full" and leg.mask_status == "opened"
        xs = sorted(a.x for a in result.placed)
        lines = _mask_lines(pcb, "F.Mask")
        assert len(lines) == 1 and not _mask_lines(pcb, "B.Mask")
        line = lines[0]
        assert sorted([line.start[0], line.end[0]]) == pytest.approx(xs, abs=1e-6)
        assert line.start[1] == pytest.approx(50.0) and line.end[1] == pytest.approx(50.0)
        assert line.width == pytest.approx(2.0)  # trace width, no margin
        # Survives save/load.
        path = tmp_path / "b.kicad_pcb"
        pcb.save(path)
        assert len(_mask_lines(PCB.load(path), "F.Mask")) == 1

    def test_mask_width_override(self):
        pcb = _straight_run_pcb()
        reinforce_net(pcb, "FUSED_LINE", mask_width_mm=1.3)
        (line,) = _mask_lines(pcb, "F.Mask")
        assert line.width == pytest.approx(1.3)

    def test_back_layer_leg_gives_b_mask(self):
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20, 50), (120, 50), width=2.0, layer="B.Cu", net="FUSED_LINE")
        result = reinforce_net(pcb, "FUSED_LINE")
        assert result.legs[0].mask_status == "opened"
        assert len(_mask_lines(pcb, "B.Mask")) == 1
        assert not _mask_lines(pcb, "F.Mask")

    def test_anchors_tenting_off_even_with_board_tenting_on(self):
        pcb = _straight_run_pcb()
        pcb.setup.tenting_front = True
        pcb.setup.tenting_back = True
        reinforce_net(pcb, "FUSED_LINE")
        anchors = [v for v in pcb.vias if v.net_name == "FUSED_LINE"]
        assert len(anchors) == 2
        for v in anchors:
            assert (v.tenting_front, v.tenting_back) == ("no", "no")

    def test_rerun_is_idempotent(self, tmp_path):
        pcb = _straight_run_pcb()
        reinforce_net(pcb, "FUSED_LINE")
        path = tmp_path / "b.kicad_pcb"
        pcb.save(path)
        again = PCB.load(path)
        result = reinforce_net(again, "FUSED_LINE")
        assert len(_mask_lines(again, "F.Mask")) == 1
        assert result.openings and all(o.existing for o in result.openings)

    def test_dry_run_writes_nothing_but_lists_openings(self):
        pcb = _straight_run_pcb()
        vias = len(pcb.vias)
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True)
        assert len(result.openings) == 1
        assert result.openings[0].layer == "F.Mask"
        assert not pcb.graphic_lines or not _mask_lines(pcb, "F.Mask")
        assert len(pcb.vias) == vias

    def test_off_in_spaced_mode_and_with_flag(self):
        spaced = _straight_run_pcb()
        res = reinforce_net(spaced, "FUSED_LINE", anchor_mode="spaced")
        assert not res.openings and not _mask_lines(spaced, "F.Mask")
        assert all(v.tenting_front is None for v in spaced.vias)
        off = _straight_run_pcb()
        reinforce_net(off, "FUSED_LINE", mask_opening=False)
        assert not _mask_lines(off, "F.Mask")
        assert all(v.tenting_front is None for v in off.vias)

    def test_voltage_map_refuses_opening_near_foreign_copper(self):
        from kicad_tools.router.pairwise_clearance import build_pairwise_clearance_table

        table = build_pairwise_clearance_table(
            {"FUSED_LINE": 150.0, "OTHER": 3.3}, dru=0.0, hv_threshold=30.0
        )
        required = table.required_clearance("FUSED_LINE", "OTHER")
        pcb = _straight_run_pcb()
        # Mid-run foreign track: anchors (near the ends) are clear, the
        # exposed 2 mm run (edge at y=51) is 0.44 mm from its edge.
        pcb.add_trace((60, 52.54), (70, 52.54), width=0.2, layer="F.Cu", net="OTHER")
        result = reinforce_net(pcb, "FUSED_LINE", pairwise_clearance=table)
        assert result.placed_count == 2
        assert not result.openings and not _mask_lines(pcb, "F.Mask")
        (ref,) = result.refused_openings
        assert ref.rule == "mask_opening_voltage_map_pairwise"
        assert ref.required_mm == pytest.approx(required)
        assert ref.actual_mm < required
        assert ref.obstacle_net == "OTHER"
        assert result.legs[0].mask_status == "refused"

    def test_cli_json_lists_openings_and_dry_run_writes_nothing(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb().save(pcb_path)
        rc = run_pcb_command(_reinforce_args(pcb_path, format="json", dry_run=True, mask_width=1.3))
        assert rc == 0
        data = json.loads(capsys.readouterr().out)
        (op,) = data["openings"]
        assert op["layer"] == "F.Mask" and op["width_mm"] == pytest.approx(1.3)
        assert data["legs"][0]["mask_status"] == "opened"
        assert not _mask_lines(PCB.load(pcb_path), "F.Mask")

    def test_cli_no_mask_opening(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _straight_run_pcb().save(pcb_path)
        assert run_pcb_command(_reinforce_args(pcb_path, mask_opening=False)) == 0
        assert not _mask_lines(PCB.load(pcb_path), "F.Mask")


class TestWireSpanReport:
    """Issue #8781: ``full`` must not hide a wire much shorter than its leg."""

    @staticmethod
    def _bus_leg_pcb(parallel_y: float) -> PCB:
        pcb = PCB.create(width=300, height=200, center=False)
        pcb.add_trace((20.0, 100.0), (300.0 - 20.0, 100.0), width=2.0, layer="F.Cu", net="N")
        pts = [(30.05, 100.0), (30.05, parallel_y), (250.0, parallel_y), (250.0, 140.0)]
        for a, b in zip(pts[:-1], pts[1:], strict=True):
            pcb.add_trace(a, b, width=2.0, layer="F.Cu", net="N")
        return pcb

    def test_parallel_leg_reports_short_span(self):
        result = reinforce_net(self._bus_leg_pcb(102.5), "N", all_runs=True, dry_run=True)
        legs = [leg for leg in result.legs if leg.status == "full" and leg.reason]
        assert legs, [(leg.status, leg.reason) for leg in result.legs]
        leg = legs[0]
        assert 0.0 < leg.wire_span_mm < leg.length_mm / 2
        assert "wire spans" in leg.reason

    def test_normal_leg_has_span_and_empty_reason(self):
        result = reinforce_net(_straight_run_pcb(), "FUSED_LINE", dry_run=True)
        (leg,) = result.legs
        assert leg.status == "full" and leg.reason == ""
        assert leg.wire_span_mm == pytest.approx(abs(leg.anchors[1].x - leg.anchors[0].x), abs=0.5)


class TestEndAnchorSameNetPadNotAtLegEnd:
    """Issue #8772: a same-net SMD pad that is not the copper the leg ends on
    (mid-leg, or the end pad of an adjacent leg of a Y) is still end copper
    for the clearance test."""

    def _assert_clear(self, result, pads, min_gap=1.0):
        for a in result.placed:
            for pad in pads:
                pad_gap, _ = _anchor_gaps(a, pad)
                assert pad_gap >= min_gap - 1e-3

    def test_mid_leg_smd_pad_within_reach_of_end(self):
        pcb = PCB.create(width=200, height=200, center=False)
        for x in (40.0, 43.0):
            _add_shaped_pad_footprint(
                pcb, ref=f"U{int(x)}", x=x, y=100.0, net="N", shape="circle", size=(1.5, 1.5)
            )
        _add_shaped_pad_footprint(
            pcb, ref="U9", x=140.0, y=100.0, net="N", shape="rect", size=(2.0, 6.0)
        )
        pcb.add_trace((40.0, 100.0), (140.0, 100.0), width=1.0, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", dry_run=True)
        pads = [
            _pad_poly("circle", (1.5, 1.5), 0.0, (40.0, 100.0)),
            _pad_poly("circle", (1.5, 1.5), 0.0, (43.0, 100.0)),
            _pad_poly("rect", (2.0, 6.0), 0.0, (140.0, 100.0)),
        ]
        self._assert_clear(result, pads)

    def test_y_trunk_anchor_clears_adjacent_leg_end_pad(self):
        pcb = PCB.create(width=200, height=200, center=False)
        for ref, x in (("A", 40.0), ("B", 46.0)):
            _add_shaped_pad_footprint(
                pcb,
                ref=ref,
                x=x,
                y=100.0,
                net="PWR",
                shape="rect",
                size=(2.0, 6.0),
                rotation=45.0,
            )
        pcb.add_trace((40, 100), (43, 100), width=1.0, layer="F.Cu", net="PWR")
        pcb.add_trace((43, 100), (46, 100), width=1.0, layer="F.Cu", net="PWR")
        pcb.add_trace((43, 100), (43, 94), width=1.0, layer="F.Cu", net="PWR")
        result = reinforce_net(pcb, "PWR", all_runs=True, dry_run=True)
        pads = [
            _pad_poly("rect", (2.0, 6.0), 45.0, (40.0, 100.0)),
            _pad_poly("rect", (2.0, 6.0), 45.0, (46.0, 100.0)),
        ]
        self._assert_clear(result, pads)


class TestMaxEndShiftSlide:
    """Issue #8797: a refused end candidate slides inward past the old 2 mm cap."""

    def _blocked(self):
        pcb = _straight_run_pcb(length=100.0)
        dy = 0.3 + _ANCHOR_PAD / 2 + 0.1
        # Walls the first ~10 mm of the leg's start end on the same layer.
        pcb.add_trace((15, 50 + dy), (30, 50 + dy), width=0.2, layer="F.Cu", net="OTHER")
        return pcb

    def test_anchor_slides_to_first_clear_position_and_reports_shift(self):
        result = reinforce_net(
            self._blocked(), "FUSED_LINE", net_class_clearances={"OTHER": 0.4}, dry_run=True
        )
        (leg,) = result.legs
        assert leg.status == "full"
        assert leg.anchors[0].shift_mm > 2.0
        assert leg.anchors[0].x > 30.0
        assert leg.anchors[1].shift_mm == pytest.approx(0.0)
        assert leg.uncovered_mm == pytest.approx(leg.length_mm - leg.wire_span_mm)
        assert "slid" in leg.reason and "uncovered" in leg.reason

    def test_zero_bound_keeps_leg_partial(self):
        result = reinforce_net(
            self._blocked(),
            "FUSED_LINE",
            net_class_clearances={"OTHER": 0.4},
            max_end_shift_mm=0.0,
            dry_run=True,
        )
        (leg,) = result.legs
        assert leg.status == "partial"
        assert "--max-end-shift" in leg.reason

    def test_negative_bound_rejected(self):
        with pytest.raises(ReinforceError, match="max_end_shift_mm"):
            reinforce_net(_straight_run_pcb(), "FUSED_LINE", max_end_shift_mm=-1.0)


# --------------------------------------------------------------------------
# Issue #8915: default minimum buttress-wire length (--min-wire-length)
# --------------------------------------------------------------------------


class TestMinWireLengthResolution:
    """``resolve_min_wire_length_mm``: the alias rule and the per-mode default."""

    def test_ends_default_is_15mm(self):
        assert DEFAULT_MIN_WIRE_LENGTH_MM == 15.0
        assert resolve_min_wire_length_mm(None, None, "ends") == 15.0

    def test_spaced_has_no_default(self):
        assert resolve_min_wire_length_mm(None, None, "spaced") is None

    def test_explicit_zero_beats_the_default(self):
        # 0.0 is falsy but NOT absent -- the distinction the argparse default
        # of None exists to preserve.
        assert resolve_min_wire_length_mm(0.0, None, "ends") == 0.0
        assert resolve_min_wire_length_mm(None, 0.0, "ends") == 0.0

    @pytest.mark.parametrize("mode", ["ends", "spaced"])
    def test_either_flag_alone_sets_the_value(self, mode):
        assert resolve_min_wire_length_mm(7.0, None, mode) == 7.0
        assert resolve_min_wire_length_mm(None, 7.0, mode) == 7.0

    @pytest.mark.parametrize("mode", ["ends", "spaced"])
    @pytest.mark.parametrize(("wire", "run"), [(5.0, 25.0), (25.0, 5.0), (0.0, 25.0), (25.0, 0.0)])
    def test_larger_wins_when_both_given(self, mode, wire, run):
        assert resolve_min_wire_length_mm(wire, run, mode) == max(wire, run)


class TestMinWireLengthEndsMode:
    """The owner's ruling on #8741: no wire on a leg under 15 mm by default."""

    @staticmethod
    def _leg(length: float, **kw):
        pcb = _padded_straight_pcb(length=length)
        result = reinforce_net(pcb, "FUSED_LINE", dry_run=True, **kw)
        (leg,) = result.legs
        return leg, result

    @pytest.mark.parametrize("length", [8.0, 10.0, 12.0, 14.99])
    def test_leg_below_default_minimum_gets_nothing(self, length):
        leg, result = self._leg(length)
        assert leg.status == "unanchored"
        assert leg.anchors == []
        assert result.placed_count == 0
        # Not a rule refusal: nothing was attempted, so nothing was refused.
        assert result.refused_count == 0
        # No mask opening either (#8743 only opens a fully anchored leg).
        assert result.openings == []
        assert leg.mask_status == ""
        assert leg.below_min_wire_length is True

    @pytest.mark.parametrize("length", [15.0, 20.0, 25.0])
    def test_leg_at_or_above_default_minimum_gets_two_anchors(self, length):
        leg, result = self._leg(length)
        assert leg.status == "full"
        assert len(leg.anchors) == 2
        assert result.placed_count == 2
        assert leg.below_min_wire_length is False

    def test_reason_names_the_15mm_minimum_and_the_flag(self):
        leg, _ = self._leg(12.0)
        assert "15 mm" in leg.reason
        assert "--min-wire-length" in leg.reason
        # ...and says WHY, per the first acceptance criterion.
        assert "through-hole at each end" in leg.reason

    def test_zero_restores_pre_8915_behaviour(self):
        leg, result = self._leg(12.0, min_wire_length_mm=0.0)
        assert leg.status == "full"
        assert len(leg.anchors) == 2
        assert result.placed_count == 2
        assert leg.below_min_wire_length is False

    def test_explicit_25_skips_a_20mm_leg(self):
        leg, result = self._leg(20.0, min_wire_length_mm=25.0)
        assert leg.status == "unanchored"
        assert result.placed_count == 0
        assert "25 mm" in leg.reason
        assert leg.below_min_wire_length is True

    def test_min_run_length_is_an_accepted_alias(self):
        """An existing ``--min-run-length`` invocation keeps working, and can
        still ask for LESS than the new default."""
        leg, _ = self._leg(12.0, min_run_length_mm=5.0)
        assert leg.status == "full"
        leg, _ = self._leg(20.0, min_run_length_mm=25.0)
        assert leg.status == "unanchored"

    @pytest.mark.parametrize(("wire", "run"), [(5.0, 25.0), (25.0, 5.0)])
    def test_larger_of_the_two_flags_wins(self, wire, run):
        """Neither flag may quietly lower a minimum the other asked for."""
        leg, _ = self._leg(20.0, min_wire_length_mm=wire, min_run_length_mm=run)
        assert leg.status == "unanchored"
        assert "25 mm" in leg.reason

    def test_other_unanchored_legs_are_not_marked_below_minimum(self):
        """Only the length filter sets the flag the sidecar keys off -- a leg
        merely not selected without --all-runs still wants a wire."""
        pcb = PCB.create(width=200, height=100, center=False)
        pcb.add_trace((20.0, 50.0), (60.0, 50.0), width=1.0, layer="F.Cu", net="N")
        pcb.add_trace((20.0, 70.0), (50.0, 70.0), width=1.0, layer="F.Cu", net="N")
        result = reinforce_net(pcb, "N", dry_run=True)
        short = [leg for leg in result.legs if leg.status != "full"]
        assert short, "expected at least one unselected leg"
        for leg in short:
            assert "only the longest leg" in leg.reason
            assert leg.below_min_wire_length is False


class TestMinWireLengthSpacedModeUnchanged:
    """``spaced`` takes no default minimum, so its output does not move."""

    def test_short_run_still_anchored_without_a_flag(self):
        pcb = _padded_straight_pcb(length=12.0)
        result = reinforce_net(pcb, "FUSED_LINE", anchor_mode="spaced", dry_run=True)
        assert result.placed_count > 0
        assert result.runs[0].anchors_placed > 0

    def test_explicit_value_still_applies_in_spaced_mode(self):
        pcb = _padded_straight_pcb(length=12.0)
        result = reinforce_net(
            pcb, "FUSED_LINE", anchor_mode="spaced", dry_run=True, min_wire_length_mm=15.0
        )
        assert result.placed_count == 0

    def test_spaced_matches_itself_under_the_old_keyword(self):
        """``min_wire_length_mm=X`` and ``min_run_length_mm=X`` are the same
        filter in spaced mode."""
        by_wire = reinforce_net(
            _padded_straight_pcb(length=12.0),
            "FUSED_LINE",
            anchor_mode="spaced",
            dry_run=True,
            min_wire_length_mm=15.0,
        )
        by_run = reinforce_net(
            _padded_straight_pcb(length=12.0),
            "FUSED_LINE",
            anchor_mode="spaced",
            dry_run=True,
            min_run_length_mm=15.0,
        )
        assert by_wire.placed_count == by_run.placed_count
        assert [(s.run_index, s.anchors_placed) for s in by_wire.runs] == [
            (s.run_index, s.anchors_placed) for s in by_run.runs
        ]


class TestMinWireLengthCLI:
    """``--min-wire-length`` end to end through ``kct pcb reinforce``."""

    @staticmethod
    def _json(tmp_path, length, **overrides):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _padded_straight_pcb(length=length).save(pcb_path)
        args = _reinforce_args(pcb_path, format="json", dry_run=True, **overrides)
        rc = run_pcb_command(args)
        return rc, pcb_path

    def test_json_reports_the_reason_and_the_effective_minimum(self, tmp_path, capsys):
        rc, _ = self._json(tmp_path, 12.0)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_wire_length_mm"] is None
        assert payload["min_wire_length_effective_mm"] == 15.0
        (leg,) = payload["legs"]
        assert leg["status"] == "unanchored"
        assert leg["anchors"] == []
        assert "15 mm" in leg["reason"]
        assert payload["anchors_placed"] == 0
        assert payload["openings"] == []

    def test_text_report_names_the_minimum_and_the_reason(self, tmp_path, capsys):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _padded_straight_pcb(length=12.0).save(pcb_path)
        rc = run_pcb_command(_reinforce_args(pcb_path, dry_run=True))
        assert rc == 0
        out = capsys.readouterr().out
        assert "Min wire length: 15 mm" in out
        assert "15 mm" in out and "--min-wire-length" in out

    def test_cli_zero_anchors_the_short_leg(self, tmp_path, capsys):
        rc, _ = self._json(tmp_path, 12.0, min_wire_length=0.0)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_wire_length_effective_mm"] == 0.0
        assert payload["anchors_placed"] == 2
        assert payload["legs"][0]["status"] == "full"

    def test_cli_25_skips_a_20mm_leg(self, tmp_path, capsys):
        rc, _ = self._json(tmp_path, 20.0, min_wire_length=25.0)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_wire_length_effective_mm"] == 25.0
        assert payload["anchors_placed"] == 0

    def test_cli_alias_and_larger_wins(self, tmp_path, capsys):
        rc, _ = self._json(tmp_path, 20.0, min_wire_length=5.0, min_run_length=25.0)
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_run_length_mm"] == 25.0
        assert payload["min_wire_length_mm"] == 5.0
        assert payload["min_wire_length_effective_mm"] == 25.0
        assert payload["anchors_placed"] == 0

    def test_cli_spaced_mode_keeps_no_minimum(self, tmp_path, capsys):
        rc, _ = self._json(tmp_path, 12.0, anchor_mode="spaced")
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["min_wire_length_effective_mm"] is None
        assert payload["anchors_placed"] > 0

    @pytest.mark.parametrize("flag", ["min_wire_length", "min_run_length"])
    def test_cli_rejects_a_negative_minimum(self, tmp_path, capsys, flag):
        rc, _ = self._json(tmp_path, 20.0, **{flag: -1.0})
        assert rc == 1
        assert "must not be negative" in capsys.readouterr().err

    def test_cli_writes_no_anchors_for_a_short_leg_in_place(self, tmp_path):
        from kicad_tools.cli.commands.pcb import run_pcb_command

        pcb_path = tmp_path / "board.kicad_pcb"
        _padded_straight_pcb(length=12.0).save(pcb_path)
        before = len(PCB.load(pcb_path).vias)
        assert run_pcb_command(_reinforce_args(pcb_path)) == 0
        assert len(PCB.load(pcb_path).vias) == before
