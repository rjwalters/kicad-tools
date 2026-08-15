"""Issue #4799: replaying a crossing-tail census report as a pre-route prediction.

Slice 1 (#4852) wrote the census out as JSON at the *end* of a run.  This
module pins the consumption side -- the part that makes the census a leading
indicator rather than a post-mortem:

1. The loader round-trips exactly what ``crosstail_census.write_report`` wrote,
   and rejects (loudly) anything that is not a census report.
2. The per-net prediction: which nets carried the saturated crossovers, worst
   first, deterministically ordered.
3. The board cross-check: a report's nets that no longer exist on the board
   being routed do not contribute to the prediction, and a report whose nets
   barely overlap is declared **stale** instead of predicting confidently from
   a different design's measurement.
4. "Not applicable" (nothing was ever scanned) stays distinct from "0%
   saturated" -- the same distinction slice 1 drew, carried through the replay.
5. Advisory ONLY: the preflight returns 0 for a missing, malformed, empty and
   valid report alike, and never raises.

No board fixture and no routing: every test drives fabricated records, so this
file runs in milliseconds -- which is also the point of the feature (a leading
indicator that costs what the route costs is not one).
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest

from kicad_tools.router.crosstail_advisory import (
    ADVISORY_ENV_VAR,
    STALE_COVERAGE_PCT_THRESHOLD,
    WORST_NETS_SHOWN,
    CensusReportError,
    CrossingTailAdvisory,
    LoadedCensusReport,
    NetPrediction,
    advisory_path_from_env,
    board_net_names,
    build_advisory,
    emit_advisory,
)
from kicad_tools.router.crosstail_census import (
    SCHEMA_VERSION,
    VERDICT_NOT_APPLICABLE,
    VERDICT_ORDERING_LEVERS,
    VERDICT_SATURATED,
    CrossingTailCensusCollector,
    CrossingTailCensusRecord,
    write_report,
)

# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _record(net: str, legal: int, distinct_v1: int = 1, census_s: float = 0.01):
    return CrossingTailCensusRecord(
        net_name=net,
        head=(1.0, 2.0),
        goal=(3.0, 4.0),
        legal=legal,
        total=225,
        distinct_v1=distinct_v1,
        census_s=census_s,
    )


def _report_from(records, *, path=None, census_enabled: bool = True):
    """Build a LoadedCensusReport the way a real report would deserialize."""
    collector = CrossingTailCensusCollector()
    for record in records:
        collector.add(record)
    payload = collector.to_report_dict(census_enabled=census_enabled)
    return LoadedCensusReport.from_payload(payload, path=path)


# --------------------------------------------------------------------------
# 1. loader
# --------------------------------------------------------------------------


def test_loader_round_trips_the_slice_one_writer(tmp_path):
    """What write_report() writes is exactly what the advisory reads back."""
    collector = CrossingTailCensusCollector()
    originals = [
        _record("MIPI_CLK-", legal=0),
        _record("MIPI_CLK+", legal=3, distinct_v1=2, census_s=0.02),
    ]
    for record in originals:
        collector.add(record)
    target = write_report(tmp_path / "census.json", collector, census_enabled=True)

    loaded = LoadedCensusReport.from_path(target)

    assert loaded.schema_version == SCHEMA_VERSION
    assert loaded.census_enabled is True
    assert loaded.generated_at is not None
    assert loaded.records == tuple(originals)
    # The stored summary is retained for cross-checking, not trusted blindly.
    assert loaded.stored_summary["crossovers_scanned"] == 2


def test_loader_rejects_a_file_that_is_not_a_census_report(tmp_path):
    other = tmp_path / "other.json"
    other.write_text(json.dumps({"report": "offboard", "summary": {}}))
    with pytest.raises(CensusReportError, match="expected 'crosstail-census'"):
        LoadedCensusReport.from_path(other)


def test_loader_rejects_malformed_json(tmp_path):
    broken = tmp_path / "broken.json"
    broken.write_text("{not json")
    with pytest.raises(CensusReportError, match="not valid JSON"):
        LoadedCensusReport.from_path(broken)


def test_loader_rejects_a_missing_file(tmp_path):
    with pytest.raises(CensusReportError, match="cannot read"):
        LoadedCensusReport.from_path(tmp_path / "nope.json")


def test_loader_rejects_a_non_object_document(tmp_path):
    listy = tmp_path / "list.json"
    listy.write_text("[1, 2, 3]")
    with pytest.raises(CensusReportError, match="not a JSON object"):
        LoadedCensusReport.from_path(listy)


def test_loader_rejects_a_non_list_crossovers_block():
    with pytest.raises(CensusReportError, match="non-list 'crossovers'"):
        LoadedCensusReport.from_payload({"report": "crosstail-census", "crossovers": {"net": 1}})


def test_loader_tolerates_missing_and_junk_fields():
    """A truncated record must degrade to zeros, not explode mid-preflight."""
    loaded = LoadedCensusReport.from_payload(
        {
            "report": "crosstail-census",
            "crossovers": [
                {"net_name": "N1"},
                {"net_name": "N2", "legal": "seven", "head": "nope", "census_s": None},
                "not-a-mapping",
            ],
        }
    )
    assert [r.net_name for r in loaded.records] == ["N1", "N2"]
    assert loaded.records[0].legal == 0
    assert loaded.records[1].legal == 0
    assert loaded.records[1].head == (0.0, 0.0)
    assert loaded.records[1].census_s == 0.0


# --------------------------------------------------------------------------
# 2. per-net prediction
# --------------------------------------------------------------------------


def test_per_net_rollup_counts_and_orders_worst_first():
    report = _report_from(
        [
            _record("QUIET", legal=9, distinct_v1=4),
            _record("BAD", legal=0),
            _record("BAD", legal=0),
            _record("BAD", legal=0),
            _record("MEH", legal=0),
            _record("MEH", legal=2, distinct_v1=1),
        ]
    )
    advisory = build_advisory(report)

    assert [n.net_name for n in advisory.nets] == ["BAD", "MEH", "QUIET"]
    bad, meh, quiet = advisory.nets
    assert (bad.crossovers, bad.saturated, bad.saturated_pct) == (3, 3, 100.0)
    assert (meh.crossovers, meh.saturated, meh.no_ordering_lever) == (2, 1, 1)
    assert meh.inert == 2
    assert (quiet.saturated, quiet.no_ordering_lever, quiet.inert) == (0, 0, 0)
    assert quiet.saturated_pct == 0.0


def test_net_ordering_ties_break_alphabetically_not_by_insertion():
    report = _report_from([_record("ZZZ", legal=0), _record("AAA", legal=0)])
    assert [n.net_name for n in build_advisory(report).nets] == ["AAA", "ZZZ"]


def test_prediction_totals_match_the_re_aggregated_summary():
    records = [_record("A", legal=0), _record("A", legal=0), _record("B", legal=5, distinct_v1=3)]
    advisory = build_advisory(_report_from(records))

    assert advisory.summary.crossovers_scanned == 3
    assert advisory.summary.saturated == 2
    assert advisory.predicted_crossovers == 3
    assert advisory.predicted_saturated == 2
    assert advisory.predicted_saturated_pct == 66.7


def test_saturated_report_carries_the_saturated_verdict_forward():
    records = [_record(f"N{i}", legal=0) for i in range(19)] + [
        _record("OK", legal=4, distinct_v1=3)
    ]
    advisory = build_advisory(_report_from(records))
    assert advisory.summary.verdict == VERDICT_SATURATED
    assert advisory.applicable is True


def test_open_lattice_reports_ordering_levers_available():
    records = [_record(f"N{i}", legal=100, distinct_v1=10) for i in range(10)]
    advisory = build_advisory(_report_from(records))
    assert advisory.summary.verdict == VERDICT_ORDERING_LEVERS
    assert advisory.predicted_saturated == 0
    assert "reading:" not in advisory.format_human()


# --------------------------------------------------------------------------
# 3. board cross-check / staleness
# --------------------------------------------------------------------------


def test_nets_absent_from_the_board_do_not_contribute_to_the_prediction():
    report = _report_from(
        [
            _record("STILL_HERE", legal=0),
            _record("STILL_HERE", legal=0),
            _record("DELETED", legal=0),
            _record("ALSO_HERE", legal=6, distinct_v1=3),
        ]
    )
    advisory = build_advisory(report, {"STILL_HERE", "ALSO_HERE", "UNRELATED"})

    assert advisory.board_nets_known is True
    assert (advisory.nets_in_report, advisory.nets_on_board) == (3, 2)
    assert advisory.coverage_pct == 66.7
    # DELETED's saturated crossover is excluded; STILL_HERE's two are not.
    assert advisory.predicted_crossovers == 3
    assert advisory.predicted_saturated == 2
    assert advisory.stale is False
    assert {n.net_name: n.present_on_board for n in advisory.nets} == {
        "STILL_HERE": True,
        "DELETED": False,
        "ALSO_HERE": True,
    }


def test_unknown_board_nets_keep_every_record_and_say_so():
    report = _report_from([_record("A", legal=0), _record("B", legal=0)])
    advisory = build_advisory(report, None)

    assert advisory.board_nets_known is False
    assert advisory.predicted_crossovers == 2
    assert all(n.present_on_board is None for n in advisory.nets)
    assert "board cross-check: skipped" in advisory.format_human()


def test_a_report_from_a_different_board_is_declared_stale():
    report = _report_from([_record(f"OLD{i}", legal=0) for i in range(4)])
    advisory = build_advisory(report, {"OLD0", "SOMETHING_ELSE"})

    assert advisory.coverage_pct == 25.0
    assert advisory.stale is True
    # Stale means "no prediction", even though the prior run did measure.
    assert advisory.summary.applicable is True
    assert advisory.applicable is False
    assert any("different design" in w for w in advisory.warnings)
    text = advisory.format_human()
    assert "WARNING" in text
    # "Suppressed" has to mean it: no predicted counts on screen for a report
    # that was just rejected, or a reader will use them anyway.
    assert "prediction SUPPRESSED" in text
    assert "predicted this run" not in text
    assert "worst nets" not in text
    # ...while the rejected numbers stay available to tooling.
    assert advisory.to_dict()["predicted_saturated"] == 1


def test_coverage_exactly_at_the_threshold_is_not_stale():
    report = _report_from([_record("A", legal=0), _record("B", legal=0)])
    advisory = build_advisory(report, {"A"})
    assert advisory.coverage_pct == 50.0 == STALE_COVERAGE_PCT_THRESHOLD
    assert advisory.stale is False


def test_staleness_threshold_is_tunable():
    report = _report_from([_record("A", legal=0), _record("B", legal=0)])
    assert build_advisory(report, {"A"}, stale_coverage_pct=80.0).stale is True


# --------------------------------------------------------------------------
# 4. not-applicable and other honesty guards
# --------------------------------------------------------------------------


def test_an_empty_report_predicts_nothing_rather_than_zero_percent():
    advisory = build_advisory(_report_from([]))

    assert advisory.summary.verdict == VERDICT_NOT_APPLICABLE
    assert advisory.applicable is False
    assert advisory.predicted_crossovers == 0
    text = advisory.format_human()
    assert "NOT APPLICABLE" in text
    assert "not a 0%-saturated result" in text
    # An empty report must not be dressed up as a healthy board.
    assert "verdict=ordering-levers-available" not in text


def test_a_census_disabled_report_warns_that_nothing_was_measured():
    advisory = build_advisory(_report_from([], census_enabled=False))
    assert any("census disabled" in w for w in advisory.warnings)


def test_a_schema_version_mismatch_warns_instead_of_failing():
    advisory = build_advisory(
        LoadedCensusReport.from_payload(
            {
                "report": "crosstail-census",
                "schema_version": SCHEMA_VERSION + 7,
                "census_enabled": True,
                "crossovers": [{"net_name": "A", "legal": 0, "total": 225}],
            }
        )
    )
    assert any("schema_version" in w for w in advisory.warnings)
    assert advisory.applicable is True


def test_a_summary_that_disagrees_with_its_records_warns_and_re_aggregates():
    advisory = build_advisory(
        LoadedCensusReport.from_payload(
            {
                "report": "crosstail-census",
                "schema_version": SCHEMA_VERSION,
                "census_enabled": True,
                "summary": {"crossovers_scanned": 999, "saturated": 999},
                "crossovers": [{"net_name": "A", "legal": 0, "total": 225}],
            }
        )
    )
    assert advisory.summary.crossovers_scanned == 1
    assert any("stored summary claims 999" in w for w in advisory.warnings)


def test_report_age_is_reported_and_clock_skew_is_clamped():
    now = datetime(2026, 8, 14, 12, 0, tzinfo=timezone.utc)
    past = LoadedCensusReport.from_payload(
        {
            "report": "crosstail-census",
            "generated_at": (now - timedelta(hours=3)).isoformat(),
            "crossovers": [],
        }
    )
    assert past.age_seconds(now=now) == pytest.approx(10800.0)

    future = LoadedCensusReport.from_payload(
        {
            "report": "crosstail-census",
            "generated_at": (now + timedelta(hours=3)).isoformat(),
            "crossovers": [],
        }
    )
    assert future.age_seconds(now=now) == 0.0

    unstamped = LoadedCensusReport.from_payload(
        {"report": "crosstail-census", "generated_at": "not-a-date", "crossovers": []}
    )
    assert unstamped.age_seconds(now=now) is None


# --------------------------------------------------------------------------
# 5. human block / dict shape
# --------------------------------------------------------------------------


def test_human_block_is_uniformly_tagged_and_marked_advisory():
    report = _report_from([_record("A", legal=0), _record("B", legal=1)])
    text = build_advisory(report, {"A", "B"}).format_human()
    assert all(line.startswith("[crosstail-advisory]") for line in text.splitlines())
    assert "ADVISORY ONLY" in text
    assert "#4799" in text


def test_human_block_elides_beyond_the_worst_offenders():
    report = _report_from([_record(f"NET{i:02d}", legal=0) for i in range(WORST_NETS_SHOWN + 3)])
    line = next(
        ln for ln in build_advisory(report).format_human().splitlines() if "worst nets" in ln
    )
    assert line.count("/1") == WORST_NETS_SHOWN
    assert "(+3 more)" in line


def test_human_block_names_only_nets_that_feed_the_prediction():
    """A net excluded from the counts must not headline the "worst" line."""
    report = _report_from([_record("HERE", legal=0), _record("GONE", legal=0)])
    advisory = build_advisory(report, {"HERE"})

    line = next(ln for ln in advisory.format_human().splitlines() if "worst nets" in ln)
    assert "HERE 1/1" in line
    assert "GONE" not in line
    # ...but the full roll-up, absent nets included, survives in the dict form.
    assert {n["net_name"] for n in advisory.to_dict()["nets"]} == {"HERE", "GONE"}


def test_saturated_prediction_states_the_upstream_reading():
    report = _report_from([_record(f"N{i}", legal=0) for i in range(10)])
    text = build_advisory(report).format_human()
    assert "ordering levers are inert" in text
    assert "placement / escape planning" in text


def test_to_dict_exposes_a_stable_field_set():
    advisory = build_advisory(_report_from([_record("A", legal=0)]), {"A"})
    payload = advisory.to_dict()
    assert payload["advisory"] == "crosstail-census"
    assert set(payload) == {
        "advisory",
        "schema_version",
        "source",
        "generated_at",
        "age_seconds",
        "census_enabled",
        "applicable",
        "stale",
        "board_nets_known",
        "nets_in_report",
        "nets_on_board",
        "coverage_pct",
        "predicted_crossovers",
        "predicted_saturated",
        "predicted_saturated_pct",
        "prior_summary",
        "nets",
        "warnings",
    }
    assert set(payload["nets"][0]) == {
        "net_name",
        "crossovers",
        "saturated",
        "saturated_pct",
        "no_ordering_lever",
        "inert",
        "present_on_board",
    }
    # Machine output must survive a JSON round trip unchanged.
    assert json.loads(json.dumps(payload)) == payload


def test_net_prediction_handles_a_zero_crossover_bucket():
    assert NetPrediction("X", crossovers=0, saturated=0, no_ordering_lever=0).saturated_pct == 0.0


# --------------------------------------------------------------------------
# 6. board net extraction
# --------------------------------------------------------------------------


_MINIMAL_PCB = """(kicad_pcb (version 20221018) (generator test)
  (net 0 "")
  (net 1 "GND")
  (net 2 "MIPI_CLK-")
)
"""


def test_board_net_names_reads_declared_nets_and_drops_net_zero(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    assert board_net_names(pcb) == {"GND", "MIPI_CLK-"}


def test_board_net_names_is_unknown_for_missing_or_netless_boards(tmp_path):
    assert board_net_names(tmp_path / "missing.kicad_pcb") is None
    empty = tmp_path / "empty.kicad_pcb"
    empty.write_text("(kicad_pcb (version 20221018) (generator test))\n")
    # Unknown, NOT an empty set: an empty set would mark every report net
    # absent and condemn a perfectly good report as stale.
    assert board_net_names(empty) is None


# --------------------------------------------------------------------------
# 7. env surface + the never-blocking preflight
# --------------------------------------------------------------------------


def test_advisory_path_from_env_reads_and_trims(tmp_path):
    assert advisory_path_from_env({}) is None
    assert advisory_path_from_env({ADVISORY_ENV_VAR: "   "}) is None
    assert advisory_path_from_env({ADVISORY_ENV_VAR: f" {tmp_path}/r.json "}) == (
        tmp_path / "r.json"
    )


def test_emit_advisory_prints_the_block_and_returns_the_advisory(tmp_path, capsys):
    collector = CrossingTailCensusCollector()
    collector.add(_record("MIPI_CLK-", legal=0))
    target = write_report(tmp_path / "census.json", collector, census_enabled=True)

    advisory = emit_advisory(target)

    assert isinstance(advisory, CrossingTailAdvisory)
    assert advisory.predicted_saturated == 1
    assert "[crosstail-advisory]" in capsys.readouterr().err


def test_emit_advisory_cross_checks_against_a_real_board(tmp_path):
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    collector = CrossingTailCensusCollector()
    collector.add(_record("MIPI_CLK-", legal=0))
    collector.add(_record("GHOST", legal=0))
    target = write_report(tmp_path / "census.json", collector, census_enabled=True)

    advisory = emit_advisory(target, pcb)

    assert advisory is not None
    assert advisory.board_nets_known is True
    assert advisory.predicted_saturated == 1  # GHOST is not on this board


def test_emit_advisory_never_raises_on_a_missing_or_broken_report(tmp_path, capsys):
    assert emit_advisory(tmp_path / "absent.json") is None
    broken = tmp_path / "broken.json"
    broken.write_text("{")
    assert emit_advisory(broken) is None
    err = capsys.readouterr().err
    assert err.count("[crosstail-advisory] no prediction") == 2


def test_route_preflight_is_advisory_and_always_returns_zero(tmp_path, capsys, monkeypatch):
    """The #4156 offboard gate blocks; this one must never be able to."""
    from kicad_tools.cli import route_cmd

    monkeypatch.delenv(ADVISORY_ENV_VAR, raising=False)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)

    # No report requested at all: silent no-op.
    assert route_cmd._census_advisory_preflight(pcb, SimpleNamespace()) == 0
    assert capsys.readouterr().err == ""

    # Requested but absent: diagnostic, still 0.
    args = SimpleNamespace(census_advisory=str(tmp_path / "absent.json"))
    assert route_cmd._census_advisory_preflight(pcb, args) == 0
    assert "no prediction" in capsys.readouterr().err

    # Requested and valid: block printed, still 0.
    collector = CrossingTailCensusCollector()
    collector.add(_record("MIPI_CLK-", legal=0))
    target = write_report(tmp_path / "census.json", collector, census_enabled=True)
    args = SimpleNamespace(census_advisory=str(target))
    assert route_cmd._census_advisory_preflight(pcb, args) == 0
    assert "pre-route prediction" in capsys.readouterr().err


def test_route_preflight_falls_back_to_the_env_var(tmp_path, capsys, monkeypatch):
    from kicad_tools.cli import route_cmd

    collector = CrossingTailCensusCollector()
    collector.add(_record("MIPI_CLK-", legal=0))
    target = write_report(tmp_path / "census.json", collector, census_enabled=True)
    monkeypatch.setenv(ADVISORY_ENV_VAR, str(target))

    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    assert route_cmd._census_advisory_preflight(pcb, SimpleNamespace()) == 0
    assert "pre-route prediction" in capsys.readouterr().err


def test_route_help_documents_the_flag(capsys):
    from kicad_tools.cli import route_cmd

    with pytest.raises(SystemExit):
        route_cmd.main(["--help"])
    out = capsys.readouterr().out
    assert "--census-advisory" in out
    assert "Advisory only" in out


def test_outer_kct_parser_accepts_and_forwards_the_flag():
    """`kct route` parses with the OUTER parser and shells the inner one.

    Adding the flag to route_cmd.py alone leaves `kct route --census-advisory`
    dying with "unrecognized arguments" -- which is exactly what happened
    before this test existed.
    """
    from kicad_tools.cli import route_cmd
    from kicad_tools.cli.commands import routing
    from kicad_tools.cli.parser import create_parser

    captured: list[list[str]] = []

    def _fake_route_main(argv):
        captured.append(list(argv))
        return 0

    original = route_cmd.main
    route_cmd.main = _fake_route_main  # imported inside run_route_command
    try:
        args = create_parser().parse_args(
            ["route", "board.kicad_pcb", "--census-advisory", "census.json"]
        )
        assert args.census_advisory == "census.json"
        assert routing.run_route_command(args) == 0

        # Unset stays unset: the flag-off path must be byte-identical.
        plain = create_parser().parse_args(["route", "board.kicad_pcb"])
        assert plain.census_advisory is None
        assert routing.run_route_command(plain) == 0
    finally:
        route_cmd.main = original

    with_flag, without_flag = captured
    assert "--census-advisory" in with_flag
    assert with_flag[with_flag.index("--census-advisory") + 1] == "census.json"
    assert "--census-advisory" not in without_flag
