"""Per-stage routing-quality instrumentation for ``kct route`` (issue #4732, slice 1).

#4615 measured *end-of-pipeline* copper (67% sub-0.25 mm fragments, 42%
staircase legs on a default ``kct route`` board) and #4646/#4651 shipped
the ``kct check`` reporting + opt-in gates -- but nothing said **which**
of the post-route stages leaves those artifacts behind.  Slice 1 is the
measurement that turns the #4732 hypothesis list into targeted work:

* a shared metric core over neutral segment records so the router's
  in-memory ``router.primitives.Segment`` is measured with exactly the
  definitions ``kct check`` reports (:mod:`kicad_tools.analysis.routing_quality`),
* a read-only :class:`~kicad_tools.router.quality_probe.StageQualityRecorder`
  sampled at the four post-route mutation boundaries
  (pre-optimize / post-optimize / post-nudge / post-finalize),
* an opt-in ``--report-stage-quality`` flag on ``kct route`` that prints
  the advisory table.

The load-bearing property under test is that this is *instrumentation*:
copper, exit codes, and default output are unchanged.  This file is
deliberately standalone (per the issue: do not extend
``tests/test_routing_quality_metrics.py``, which owns the #4646/#4651
check-side surface); slice 2's consolidation-pass unit tests land here
too.
"""

from __future__ import annotations

import argparse
import contextlib
import sys
from io import StringIO
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import pytest

from kicad_tools.analysis.routing_quality import (
    compute_routing_quality,
    compute_routing_quality_from_records,
)
from kicad_tools.router.layers import Layer
from kicad_tools.router.primitives import Route, Segment
from kicad_tools.router.quality_probe import (
    STAGE_POST_FINALIZE,
    STAGE_POST_NUDGE,
    STAGE_POST_OPTIMIZE,
    STAGE_PRE_OPTIMIZE,
    StageQualityRecorder,
    measure_routes,
    segment_records,
)
from kicad_tools.schema.pcb import Segment as SchemaSegment

# ---------------------------------------------------------------------------
# Scaffolding
# ---------------------------------------------------------------------------


class _StubPCB:
    """``compute_routing_quality`` only reads ``pcb.segments``."""

    def __init__(self, segments: list[SchemaSegment]):
        self.segments = segments


def _schema_seg(
    start: tuple[float, float],
    end: tuple[float, float],
    net: int = 1,
    layer: str = "F.Cu",
) -> SchemaSegment:
    return SchemaSegment(
        start=start,
        end=end,
        width=0.2,
        layer=layer,
        net_number=net,
        net_name=f"NET{net}",
    )


def _router_seg(
    start: tuple[float, float],
    end: tuple[float, float],
    net: int = 1,
    layer: Layer = Layer.F_CU,
) -> Segment:
    return Segment(
        x1=start[0],
        y1=start[1],
        x2=end[0],
        y2=end[1],
        width=0.2,
        layer=layer,
        net=net,
        net_name=f"NET{net}",
    )


def _route(segments: list[Segment], net: int = 1) -> Route:
    return Route(net=net, net_name=f"NET{net}", segments=segments, vias=[])


# A 4-step H/V staircase with 0.3 mm legs: every leg is both a fragment
# (< 0.25 mm is false here -- 0.3 mm is NOT a fragment) and a staircase
# step (< 0.6 mm legs meeting perpendicular partners).  Chosen so the two
# tracked fractions can be asserted independently.
_STAIRCASE_POINTS = [
    (0.0, 0.0),
    (0.3, 0.0),
    (0.3, 0.3),
    (0.6, 0.3),
    (0.6, 0.6),
    (0.9, 0.6),
]


def _staircase_router_segments() -> list[Segment]:
    return [
        _router_seg(a, b) for a, b in zip(_STAIRCASE_POINTS, _STAIRCASE_POINTS[1:], strict=False)
    ]


def _staircase_schema_segments() -> list[SchemaSegment]:
    return [
        _schema_seg(a, b) for a, b in zip(_STAIRCASE_POINTS, _STAIRCASE_POINTS[1:], strict=False)
    ]


# ---------------------------------------------------------------------------
# The neutral record core: schema PCB and router segments must agree
# ---------------------------------------------------------------------------


class TestRecordCoreParity:
    """``compute_routing_quality`` is now a thin adapter over the record core."""

    def test_pcb_path_delegates_to_record_core(self):
        """Measuring a PCB directly == measuring records built from it."""
        segments = _staircase_schema_segments()
        via_pcb = compute_routing_quality(_StubPCB(segments))
        via_records = compute_routing_quality_from_records(
            (s.start, s.end, s.layer, s.net_number) for s in segments
        )
        assert via_pcb == via_records

    def test_router_segments_measure_identically_to_schema_segments(self):
        """The #4732 adapter must not shift any metric.

        Same geometry expressed as router primitives (``x1/y1/x2/y2``, a
        ``Layer`` enum) and as schema segments (tuples, a layer string)
        must produce identical metrics -- otherwise a per-stage number
        could not be compared against what ``kct check`` reports.
        """
        from_router = measure_routes([_route(_staircase_router_segments())])
        from_schema = compute_routing_quality(_StubPCB(_staircase_schema_segments()))
        assert from_router == from_schema

    def test_empty_input_is_all_zero(self):
        metrics = measure_routes([])
        assert metrics.total_segments == 0
        assert metrics.fragment_fraction == 0.0
        assert metrics.staircase_fraction == 0.0

    def test_record_core_consumes_a_generator_once(self):
        """A one-shot iterable is materialized internally (two passes)."""
        segments = _staircase_schema_segments()
        metrics = compute_routing_quality_from_records(
            iter([(s.start, s.end, s.layer, s.net_number) for s in segments])
        )
        # The staircase join is a SECOND pass over the same population; if
        # the core did not materialize, it would silently see nothing.
        assert metrics.staircase_step_count == len(segments)


class TestRouterSegmentAdapter:
    def test_segment_records_shape(self):
        records = list(segment_records([_route([_router_seg((1.0, 2.0), (3.0, 4.0), net=7)])]))
        assert records == [((1.0, 2.0), (3.0, 4.0), Layer.F_CU, 7)]

    def test_layers_are_kept_distinct(self):
        """A staircase is only a staircase within one layer.

        Two perpendicular short legs sharing an endpoint on DIFFERENT
        layers must not be joined into a staircase step -- the adapter
        passes the ``Layer`` enum through as the layer key precisely so
        this stays true.
        """
        segs = [
            _router_seg((0.0, 0.0), (0.3, 0.0), layer=Layer.F_CU),
            _router_seg((0.3, 0.0), (0.3, 0.3), layer=Layer.B_CU),
        ]
        assert measure_routes([_route(segs)]).staircase_step_count == 0

    def test_fragments_counted_over_router_segments(self):
        """Sub-0.25 mm router segments are counted as fragments."""
        segs = [
            _router_seg((0.0, 0.0), (0.1, 0.0)),  # fragment
            _router_seg((0.1, 0.0), (0.2, 0.0)),  # fragment
            _router_seg((0.2, 0.0), (5.2, 0.0)),  # not a fragment
        ]
        metrics = measure_routes([_route(segs)])
        assert metrics.fragment_count == 2
        assert metrics.fragment_fraction == pytest.approx(2 / 3)


# ---------------------------------------------------------------------------
# The recorder
# ---------------------------------------------------------------------------


class TestStageQualityRecorder:
    def test_records_stages_in_order(self):
        rec = StageQualityRecorder()
        rec.record(STAGE_PRE_OPTIMIZE, [_route(_staircase_router_segments())])
        rec.record(STAGE_POST_OPTIMIZE, [_route([_router_seg((0.0, 0.0), (0.9, 0.6))])])
        assert [s.stage for s in rec.stages] == [STAGE_PRE_OPTIMIZE, STAGE_POST_OPTIMIZE]

    def test_metrics_are_per_stage(self):
        """Each stage's numbers reflect the copper AT that stage."""
        rec = StageQualityRecorder()
        rec.record(STAGE_PRE_OPTIMIZE, [_route(_staircase_router_segments())])
        rec.record(STAGE_POST_OPTIMIZE, [_route([_router_seg((0.0, 0.0), (0.9, 0.6))])])
        pre = rec.get(STAGE_PRE_OPTIMIZE)
        post = rec.get(STAGE_POST_OPTIMIZE)
        assert pre is not None and post is not None
        assert pre.staircase_fraction == 1.0
        assert post.staircase_fraction == 0.0

    def test_delta_is_last_minus_first(self):
        rec = StageQualityRecorder()
        rec.record(STAGE_PRE_OPTIMIZE, [_route(_staircase_router_segments())])
        rec.record(STAGE_POST_OPTIMIZE, [_route([_router_seg((0.0, 0.0), (0.9, 0.6))])])
        delta = rec.delta("staircase_fraction", STAGE_PRE_OPTIMIZE, STAGE_POST_OPTIMIZE)
        assert delta == pytest.approx(-1.0)

    def test_delta_missing_stage_is_none(self):
        rec = StageQualityRecorder()
        rec.record(STAGE_PRE_OPTIMIZE, [])
        assert rec.delta("fragment_fraction", STAGE_PRE_OPTIMIZE, STAGE_POST_NUDGE) is None

    def test_report_is_empty_when_nothing_recorded(self):
        """No stages -> no header, so the caller can print unconditionally."""
        assert StageQualityRecorder().format_report() == ""

    def test_report_lists_every_recorded_stage(self):
        rec = StageQualityRecorder()
        for stage in (
            STAGE_PRE_OPTIMIZE,
            STAGE_POST_OPTIMIZE,
            STAGE_POST_NUDGE,
            STAGE_POST_FINALIZE,
        ):
            rec.record(stage, [_route(_staircase_router_segments())])
        report = rec.format_report()
        for stage in (
            STAGE_PRE_OPTIMIZE,
            STAGE_POST_OPTIMIZE,
            STAGE_POST_NUDGE,
            STAGE_POST_FINALIZE,
        ):
            assert stage in report
        assert "advisory" in report

    def test_report_marks_a_regressing_stage_as_worse(self):
        """Hypothesis 4 (the nudge re-introduces jogs) must be legible.

        A stage that RAISES a tracked fraction is labelled ``worse`` --
        that is the whole diagnostic point of the per-stage table.
        """
        rec = StageQualityRecorder()
        rec.record(STAGE_POST_OPTIMIZE, [_route([_router_seg((0.0, 0.0), (0.9, 0.6))])])
        rec.record(STAGE_POST_NUDGE, [_route(_staircase_router_segments())])
        report = rec.format_report()
        assert "worse" in report
        assert f"{STAGE_POST_OPTIMIZE} -> {STAGE_POST_NUDGE}" in report

    def test_to_dict_is_machine_readable(self):
        rec = StageQualityRecorder()
        rec.record(STAGE_PRE_OPTIMIZE, [_route(_staircase_router_segments())])
        payload = rec.to_dict()
        assert payload["stages"][0]["stage"] == STAGE_PRE_OPTIMIZE
        assert payload["stages"][0]["staircase_fraction"] == 1.0

    def test_recorder_does_not_mutate_routes(self):
        """The probe is read-only -- that is what makes it copper-safe."""
        segments = _staircase_router_segments()
        route = _route(segments)
        before = [(s.x1, s.y1, s.x2, s.y2, s.layer, s.net) for s in route.segments]
        StageQualityRecorder().record(STAGE_PRE_OPTIMIZE, [route])
        after = [(s.x1, s.y1, s.x2, s.y2, s.layer, s.net) for s in route.segments]
        assert before == after
        assert route.segments is segments


# ---------------------------------------------------------------------------
# CLI wiring: flag exists on both parsers, is forwarded, defaults off
# ---------------------------------------------------------------------------


def _inner_route_parser() -> argparse.ArgumentParser:
    """Capture the inner ``route_cmd.main`` parser without routing."""
    from kicad_tools.cli.route_cmd import main as route_main

    captured: dict[str, argparse.ArgumentParser] = {}
    real_parse_args = argparse.ArgumentParser.parse_args

    def fake_parse_args(self, *args, **kwargs):
        if getattr(self, "prog", "") == "kicad-tools route":
            captured["parser"] = self
            raise SystemExit(0)
        return real_parse_args(self, *args, **kwargs)

    with patch.object(argparse.ArgumentParser, "parse_args", fake_parse_args):
        with contextlib.suppress(SystemExit):
            route_main([])

    assert "parser" in captured, "failed to capture inner route parser"
    return captured["parser"]


class TestFlagDefinedInBothParsers:
    """Guards the ``tests/test_cli_parser_drift.py`` inner/outer contract."""

    def test_flag_on_inner_route_cmd_parser(self):
        args = _inner_route_parser().parse_args(["board.kicad_pcb", "--report-stage-quality"])
        assert args.report_stage_quality is True

    def test_default_off_on_inner_parser(self):
        args = _inner_route_parser().parse_args(["board.kicad_pcb"])
        assert args.report_stage_quality is False

    def test_flag_on_unified_parser(self):
        from kicad_tools.cli.parser import create_parser

        args = create_parser().parse_args(["route", "board.kicad_pcb", "--report-stage-quality"])
        assert args.report_stage_quality is True

    def test_default_off_on_unified_parser(self):
        from kicad_tools.cli.parser import create_parser

        args = create_parser().parse_args(["route", "board.kicad_pcb"])
        assert args.report_stage_quality is False

    def test_help_documents_it_as_advisory(self):
        from kicad_tools.cli.route_cmd import main as route_main

        help_output = StringIO()
        with patch.object(sys, "stdout", help_output):
            with contextlib.suppress(SystemExit):
                route_main(["--help"])
        collapsed = " ".join(help_output.getvalue().split())
        assert "--report-stage-quality" in collapsed
        assert "advisory" in collapsed


def _dispatch(argv: list[str]) -> list[str]:
    """Parse ``argv`` with the real outer parser; return the inner argv."""
    from kicad_tools.cli.commands.routing import run_route_command
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(argv)
    with patch("kicad_tools.cli.route_cmd.main") as mock_main:
        mock_main.return_value = 0
        run_route_command(args)
        return list(mock_main.call_args[0][0])


class TestDispatcherForwarding:
    """``run_route_command`` forwards the flag only when it is set."""

    def test_forwarded_when_set(self, tmp_path: Path):
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20221018) (generator pcbnew))")
        sub_argv = _dispatch(["route", str(pcb), "--report-stage-quality"])
        assert "--report-stage-quality" in sub_argv

    def test_absent_when_unset(self, tmp_path: Path):
        """Flag-off argv stays byte-identical to before the change."""
        pcb = tmp_path / "board.kicad_pcb"
        pcb.write_text("(kicad_pcb (version 20221018) (generator pcbnew))")
        sub_argv = _dispatch(["route", str(pcb)])
        assert "--report-stage-quality" not in sub_argv


# ---------------------------------------------------------------------------
# CLI helper behavior (the three route_cmd wire-in shims)
# ---------------------------------------------------------------------------


class TestRouteCmdHelpers:
    def test_recorder_is_none_without_the_flag(self):
        from kicad_tools.cli import route_cmd

        assert route_cmd._make_stage_quality_recorder(SimpleNamespace()) is None
        assert (
            route_cmd._make_stage_quality_recorder(SimpleNamespace(report_stage_quality=False))
            is None
        )

    def test_recorder_is_built_with_the_flag(self):
        from kicad_tools.cli import route_cmd

        recorder = route_cmd._make_stage_quality_recorder(
            SimpleNamespace(report_stage_quality=True)
        )
        assert isinstance(recorder, StageQualityRecorder)

    def test_record_is_a_noop_when_disabled(self):
        from kicad_tools.cli import route_cmd

        # Must not raise even though ``None`` has no ``record``.
        route_cmd._record_stage_quality(None, STAGE_PRE_OPTIMIZE, object())

    def test_record_tolerates_a_router_without_routes(self):
        from kicad_tools.cli import route_cmd

        recorder = StageQualityRecorder()
        route_cmd._record_stage_quality(recorder, STAGE_PRE_OPTIMIZE, SimpleNamespace())
        assert recorder.get(STAGE_PRE_OPTIMIZE) is not None

    def test_record_tolerates_a_missing_router(self):
        from kicad_tools.cli import route_cmd

        recorder = StageQualityRecorder()
        route_cmd._record_stage_quality(recorder, STAGE_PRE_OPTIMIZE, None)
        assert recorder.stages == []

    def test_print_is_silent_when_quiet(self, capsys):
        from kicad_tools.cli import route_cmd

        recorder = StageQualityRecorder()
        recorder.record(STAGE_PRE_OPTIMIZE, [_route(_staircase_router_segments())])
        route_cmd._print_stage_quality_report(recorder, quiet=True)
        assert capsys.readouterr().out == ""

    def test_print_emits_the_table(self, capsys):
        from kicad_tools.cli import route_cmd

        recorder = StageQualityRecorder()
        recorder.record(STAGE_PRE_OPTIMIZE, [_route(_staircase_router_segments())])
        route_cmd._print_stage_quality_report(recorder, quiet=False)
        assert "Routing quality by stage" in capsys.readouterr().out

    def test_print_is_a_noop_when_disabled(self, capsys):
        from kicad_tools.cli import route_cmd

        route_cmd._print_stage_quality_report(None, quiet=False)
        assert capsys.readouterr().out == ""


# ---------------------------------------------------------------------------
# End-to-end pipeline: a real ``kct route`` on a tiny synthetic board
# ---------------------------------------------------------------------------

# Two 0805 footprints on a 20x15 mm board, two 2-pad signal nets.  Small
# enough to route in ~1 s on the base ``main()`` path (no escalation), so
# the per-stage wire-in is exercised for real rather than mocked.
_TINY_PCB = """(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (37 "F.SilkS" user "F.Silkscreen")
    (44 "Edge.Cuts" user)
    (49 "F.Fab" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "SIG_A")
  (net 2 "SIG_B")
  (gr_rect (start 100 100) (end 120 115)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
  (footprint "Resistor_SMD:R_0805_2012Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000001")
    (at 104 105)
    (property "Reference" "R1" (at 0 -1.5 0) (layer "F.SilkS")
      (uuid "10000000-0000-0000-0000-000000000001")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab")
      (uuid "10000000-0000-0000-0000-000000000002")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -0.9 0) (size 1 1.2) (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "SIG_A") (uuid "20000000-0000-0000-0000-000000000001"))
    (pad "2" smd rect (at 0.9 0) (size 1 1.2) (layers "F.Cu" "F.Paste" "F.Mask")
      (net 2 "SIG_B") (uuid "20000000-0000-0000-0000-000000000002"))
  )
  (footprint "Resistor_SMD:R_0805_2012Metric"
    (layer "F.Cu")
    (uuid "00000000-0000-0000-0000-000000000002")
    (at 115 111)
    (property "Reference" "R2" (at 0 -1.5 0) (layer "F.SilkS")
      (uuid "10000000-0000-0000-0000-000000000003")
      (effects (font (size 1 1) (thickness 0.15))))
    (property "Value" "1k" (at 0 1.5 0) (layer "F.Fab")
      (uuid "10000000-0000-0000-0000-000000000004")
      (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at -0.9 0) (size 1 1.2) (layers "F.Cu" "F.Paste" "F.Mask")
      (net 1 "SIG_A") (uuid "20000000-0000-0000-0000-000000000003"))
    (pad "2" smd rect (at 0.9 0) (size 1 1.2) (layers "F.Cu" "F.Paste" "F.Mask")
      (net 2 "SIG_B") (uuid "20000000-0000-0000-0000-000000000004"))
  )
)
"""

_ROUTE_ARGS = [
    "--grid",
    "0.1",
    "--layers",
    "2",
    "--skip-drc",
    "--seed",
    "42",
    # No cache: every invocation must really re-route, so the
    # copper-identical assertion below compares two genuine routes
    # rather than two reads of one cached result.
    "--no-cache",
]


def _run_route(tmp_path: Path, *extra: str) -> tuple[int, str, Path]:
    """Route the tiny board into ``tmp_path``; return (exit, stdout, output)."""
    from kicad_tools.cli.route_cmd import main as route_main

    tmp_path.mkdir(parents=True, exist_ok=True)
    pcb = tmp_path / "tiny.kicad_pcb"
    pcb.write_text(_TINY_PCB)
    out = tmp_path / "tiny_out.kicad_pcb"

    buffer = StringIO()
    with patch.object(sys, "stdout", buffer):
        code = route_main([str(pcb), "-o", str(out), *_ROUTE_ARGS, *extra])
    return code, buffer.getvalue(), out


def _copper_segments(pcb_text: str) -> list[str]:
    return [line.strip() for line in pcb_text.splitlines() if line.strip().startswith("(segment")]


class TestEndToEndPipeline:
    """The wire-in on the base ``main()`` path, exercised for real."""

    def test_stage_table_appears_with_the_flag(self, tmp_path: Path):
        code, stdout, out = _run_route(tmp_path, "--report-stage-quality")
        assert code == 0, stdout
        assert "Routing quality by stage" in stdout
        for stage in (
            STAGE_PRE_OPTIMIZE,
            STAGE_POST_OPTIMIZE,
            STAGE_POST_NUDGE,
            STAGE_POST_FINALIZE,
        ):
            assert stage in stdout, f"missing stage {stage!r} in:\n{stdout}"
        assert out.exists()

    def test_no_stage_table_without_the_flag(self, tmp_path: Path):
        code, stdout, _out = _run_route(tmp_path)
        assert code == 0, stdout
        assert "Routing quality by stage" not in stdout

    def test_optimizer_does_not_regress_the_tracked_fractions(self, tmp_path: Path):
        """post-* fractions must be <= pre-optimize on this board.

        Recording (not gating): the assertion is the weak, always-true-if-
        the-pipeline-is-sane direction.  Slice 2's job is to make the gap
        bigger; this pins that instrumentation reads a real improvement
        rather than a constant.
        """
        _code, stdout, _out = _run_route(tmp_path, "--report-stage-quality")
        rows = _parse_stage_table(stdout)
        assert set(rows) >= {STAGE_PRE_OPTIMIZE, STAGE_POST_FINALIZE}
        pre = rows[STAGE_PRE_OPTIMIZE]
        for stage in (STAGE_POST_OPTIMIZE, STAGE_POST_NUDGE, STAGE_POST_FINALIZE):
            assert rows[stage]["frag"] <= pre["frag"] + 1e-9, stage
            assert rows[stage]["stair"] <= pre["stair"] + 1e-9, stage

    def test_copper_is_identical_with_and_without_the_flag(self, tmp_path: Path):
        """The load-bearing invariant: instrumentation changes no copper.

        Same seed, same board, flag on vs. off -> identical
        ``(segment ...)`` geometry and identical exit code.
        """
        code_off, _out_text, out_off = _run_route(tmp_path / "off")
        code_on, _stdout, out_on = _run_route(tmp_path / "on", "--report-stage-quality")
        assert code_off == code_on == 0
        assert _copper_segments(out_off.read_text()) == _copper_segments(out_on.read_text())

    def test_no_optimize_still_bypasses_the_optimizer(self, tmp_path: Path):
        """``--no-optimize`` skips the optimize stage; the probe shows it."""
        code, stdout, _out = _run_route(tmp_path, "--report-stage-quality", "--no-optimize")
        assert code == 0, stdout
        rows = _parse_stage_table(stdout)
        assert STAGE_PRE_OPTIMIZE in rows
        assert STAGE_POST_OPTIMIZE not in rows


def _parse_stage_table(stdout: str) -> dict[str, dict[str, float]]:
    """Parse the advisory table's rows into ``{stage: {segs, frag, stair}}``."""
    rows: dict[str, dict[str, float]] = {}
    known = {
        STAGE_PRE_OPTIMIZE,
        STAGE_POST_OPTIMIZE,
        STAGE_POST_NUDGE,
        STAGE_POST_FINALIZE,
    }
    for line in stdout.splitlines():
        parts = line.split()
        if len(parts) < 4 or parts[0] not in known:
            continue
        # Skip the "a -> b:" delta lines (second token is an arrow).
        if parts[1] == "->":
            continue
        rows[parts[0]] = {
            "segs": float(parts[1]),
            "frag": float(parts[2]),
            "stair": float(parts[3]),
        }
    return rows
