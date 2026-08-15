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
5. Advisory by default: without ``--census-advisory-gate`` the preflight
   returns 0 for a missing, malformed, empty and valid report alike, and never
   raises.
6. The opt-in go/no-go gate (sections 8-9): it fails a route only for a
   prediction that is applicable, trusted, and inert at/above the threshold --
   and every "cannot trust this" path (no report, not-applicable, stale,
   census-disabled, unknown schema, crash) is pinned as a GO, because those are
   the cases where a gate would fail builds for the wrong reason.

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
    GATE_EXIT_CODE,
    GATE_REASON_BELOW_THRESHOLD,
    GATE_REASON_CENSUS_DISABLED,
    GATE_REASON_INERT,
    GATE_REASON_NO_BOARD_CROSSOVERS,
    GATE_REASON_NO_REPORT,
    GATE_REASON_NOT_APPLICABLE,
    GATE_REASON_SATURATED,
    GATE_REASON_SCHEMA_MISMATCH,
    GATE_REASON_STALE,
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
    emit_gate_decision,
    evaluate_gate,
    parse_gate_threshold_pct,
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
        # #4799 gate slice: inertness (saturated + single-site-legal) is what
        # the go/no-go keys on, so it is part of the machine form too.
        "predicted_inert",
        "predicted_inert_pct",
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


# --------------------------------------------------------------------------
# 8. the opt-in go/no-go gate (--census-advisory-gate)
# --------------------------------------------------------------------------
#
# The gate is the enforcement half of the advisory: same prediction, now
# allowed to refuse a route.  Its whole design surface is "when may this fail
# a build?", so most of what follows pins the cases where it must NOT.


def _saturated_advisory(*, board_nets=None, count: int = 10, saturated: int = 10):
    records = [_record("PCIE_TX-", legal=0) for _ in range(saturated)]
    records += [_record("PCIE_TX-", legal=7, distinct_v1=4) for _ in range(count - saturated)]
    return build_advisory(_report_from(records), board_nets)


def test_gate_says_no_go_on_a_saturated_prediction():
    decision = evaluate_gate(_saturated_advisory())

    assert decision.gated is True
    assert decision.reason == GATE_REASON_SATURATED
    assert decision.exit_code == GATE_EXIT_CODE == 9
    assert decision.predicted_inert_pct == 100.0
    assert decision.threshold_pct == 90.0


def test_no_go_message_names_the_worst_nets_and_the_fix_layer():
    records = [_record("PCIE_TX-", legal=0) for _ in range(8)]
    records += [_record("USB2_D+", legal=0) for _ in range(2)]
    decision = evaluate_gate(build_advisory(_report_from(records)))
    text = decision.format_human()

    assert "NO-GO" in text
    assert "PCIE_TX- inert 8/8" in text
    assert "USB2_D+ inert 2/2" in text
    # The actionable half: which layer can fix this, and how to proceed anyway.
    assert "placement / escape planning" in text
    assert "not the router" in text
    assert "exit 9" in text
    assert "--census-advisory-gate" in text
    assert "KCT_CROSSTAIL_CENSUS_REPORT" in text


def test_gate_keys_on_inert_not_saturation_alone():
    """Every legal set having a single site is just as inert as legal=0."""
    records = [_record("A", legal=5, distinct_v1=1) for _ in range(10)]
    advisory = build_advisory(_report_from(records))

    assert advisory.predicted_saturated == 0
    assert advisory.predicted_inert == 10

    decision = evaluate_gate(advisory)
    assert decision.gated is True
    # Distinct token: nothing was saturated, so calling it "saturated" would
    # send the reader looking for a legality wall that is not there.
    assert decision.reason == GATE_REASON_INERT


def test_gate_never_fires_without_a_report():
    decision = evaluate_gate(None)
    assert decision.gated is False
    assert decision.reason == GATE_REASON_NO_REPORT
    assert decision.exit_code == 0
    assert "GO (no-report)" in decision.format_human()


def test_gate_never_fires_on_a_not_applicable_report():
    """0 crossovers scanned is not 0% saturated -- and not a failure either."""
    decision = evaluate_gate(build_advisory(_report_from([])))

    assert decision.gated is False
    assert decision.reason == GATE_REASON_NOT_APPLICABLE
    assert decision.exit_code == 0
    assert "not a 0%-saturated result" in decision.detail


def test_gate_never_fires_on_a_suppressed_stale_prediction():
    """A measurement of some other design must not fail this board's route."""
    records = [_record(f"OLD{i}", legal=0) for i in range(4)]
    advisory = build_advisory(_report_from(records), {"OLD0", "SOMETHING_ELSE"})
    assert advisory.stale is True
    assert advisory.summary.verdict == VERDICT_SATURATED  # would gate if trusted

    decision = evaluate_gate(advisory)
    assert decision.gated is False
    assert decision.reason == GATE_REASON_STALE
    assert "different design" in decision.detail or "stale" in decision.detail


def test_gate_never_fires_on_a_census_disabled_report():
    records = [_record("A", legal=0) for _ in range(4)]
    advisory = build_advisory(_report_from(records, census_enabled=False))

    decision = evaluate_gate(advisory)
    assert decision.gated is False
    assert decision.reason == GATE_REASON_CENSUS_DISABLED


def test_gate_never_fires_on_an_unknown_schema_version():
    payload = _report_from([_record("A", legal=0) for _ in range(4)])
    future = LoadedCensusReport(
        path=None,
        schema_version=SCHEMA_VERSION + 7,
        generated_at=payload.generated_at,
        census_enabled=True,
        records=payload.records,
        stored_summary=payload.stored_summary,
    )
    decision = evaluate_gate(build_advisory(future))

    assert decision.gated is False
    assert decision.reason == GATE_REASON_SCHEMA_MISMATCH
    assert str(SCHEMA_VERSION + 7) in decision.detail


def test_gate_never_fires_when_no_predicted_crossover_survives_the_cross_check():
    """Defensive: an applicable report that contributes nothing to this board."""
    empty_prediction = CrossingTailAdvisory(
        source="<payload>",
        generated_at=None,
        age_seconds=None,
        census_enabled=True,
        summary=build_advisory(_report_from([_record("A", legal=0)])).summary,
        nets=(),
        board_nets_known=True,
        nets_in_report=1,
        nets_on_board=1,
        coverage_pct=100.0,
        predicted_crossovers=0,
        predicted_saturated=0,
        stale=False,
    )
    decision = evaluate_gate(empty_prediction)
    assert decision.gated is False
    assert decision.reason == GATE_REASON_NO_BOARD_CROSSOVERS


def test_gate_does_not_fire_below_the_threshold():
    decision = evaluate_gate(_saturated_advisory(count=10, saturated=5))

    assert decision.gated is False
    assert decision.reason == GATE_REASON_BELOW_THRESHOLD
    assert decision.predicted_inert_pct == 50.0
    assert "50.0% < threshold 90.0%" in decision.detail
    assert decision.format_human().startswith("[crosstail-gate] GO (below-threshold)")


def test_gate_threshold_is_tunable_in_both_directions():
    advisory = _saturated_advisory(count=10, saturated=5)
    assert evaluate_gate(advisory, threshold_pct=50.0).gated is True
    assert evaluate_gate(advisory, threshold_pct=50.1).gated is False
    # Exactly at the bound gates: the same >= convention the verdict uses.
    assert evaluate_gate(advisory, threshold_pct=50.0).threshold_pct == 50.0


def test_gate_defaults_to_the_threshold_the_printed_verdict_used():
    """Verdict and gate must not be able to disagree by construction."""
    advisory = build_advisory(
        _report_from([_record("A", legal=0), _record("B", legal=9, distinct_v1=5)]),
        saturated_threshold_pct=40.0,
    )
    assert advisory.summary.verdict == VERDICT_SATURATED
    assert evaluate_gate(advisory).threshold_pct == 40.0
    assert evaluate_gate(advisory).gated is True


def test_gate_decision_to_dict_is_machine_readable():
    payload = evaluate_gate(_saturated_advisory()).to_dict()

    assert payload["gate"] == "crosstail-census"
    assert payload["gated"] is True
    assert payload["reason"] == GATE_REASON_SATURATED
    assert payload["exit_code"] == GATE_EXIT_CODE
    assert payload["predicted_inert_pct"] == 100.0
    assert payload["worst_nets"] == ["PCIE_TX- inert 10/10"]


def test_go_decisions_do_not_name_worst_nets():
    """A GO block listing 'worst nets' reads like a failure that was ignored."""
    decision = evaluate_gate(_saturated_advisory(count=10, saturated=1))
    assert decision.worst_nets == ()
    assert "worst nets" not in decision.format_human()


def test_emit_gate_decision_prints_and_never_raises(capsys):
    decision = emit_gate_decision(_saturated_advisory())
    assert decision.gated is True
    assert "[crosstail-gate] NO-GO" in capsys.readouterr().err


def test_advisory_trailer_is_honest_about_an_armed_gate():
    advisory = _saturated_advisory()
    assert "ADVISORY ONLY" in advisory.format_human()
    armed = advisory.format_human(gating=True)
    assert "ADVISORY ONLY" not in armed
    assert "GATE ARMED" in armed


def test_gate_threshold_parser_rejects_nonsense():
    assert parse_gate_threshold_pct("90") == 90.0
    assert parse_gate_threshold_pct("0") == 0.0
    for bad in ("abc", "-1", "101", ""):
        with pytest.raises(ValueError):
            parse_gate_threshold_pct(bad)


# --------------------------------------------------------------------------
# 9. the gate on the CLI surface
# --------------------------------------------------------------------------


def _saturated_report(tmp_path, net: str = "MIPI_CLK-", n: int = 10):
    collector = CrossingTailCensusCollector()
    for _ in range(n):
        collector.add(_record(net, legal=0))
    return write_report(tmp_path / "census.json", collector, census_enabled=True)


def test_route_preflight_gates_only_when_asked(tmp_path, capsys, monkeypatch):
    from kicad_tools.cli import route_cmd

    monkeypatch.delenv(ADVISORY_ENV_VAR, raising=False)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    target = _saturated_report(tmp_path)

    # Default (flag absent): byte-identical to the #4862 advisory -- 0.
    advisory_only = SimpleNamespace(census_advisory=str(target))
    assert route_cmd._census_advisory_preflight(pcb, advisory_only) == 0
    assert "ADVISORY ONLY" in capsys.readouterr().err

    # Armed on the same report: NO-GO.
    gated = SimpleNamespace(census_advisory=str(target), census_advisory_gate=True)
    assert route_cmd._census_advisory_preflight(pcb, gated) == GATE_EXIT_CODE
    err = capsys.readouterr().err
    assert "[crosstail-gate] NO-GO" in err
    assert "GATE ARMED" in err


def test_route_preflight_gate_pct_implies_the_gate(tmp_path, capsys, monkeypatch):
    from kicad_tools.cli import route_cmd

    monkeypatch.delenv(ADVISORY_ENV_VAR, raising=False)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    target = _saturated_report(tmp_path)

    args = SimpleNamespace(census_advisory=str(target), census_advisory_gate_pct=99.9)
    assert route_cmd._census_advisory_preflight(pcb, args) == GATE_EXIT_CODE

    # ...and a threshold above the measurement lets it through.
    loose = SimpleNamespace(census_advisory=str(target), census_advisory_gate_pct=100.1)
    assert route_cmd._census_advisory_preflight(pcb, loose) == 0
    assert "GO (below-threshold)" in capsys.readouterr().err


def test_route_preflight_gate_is_loud_when_armed_with_no_report(tmp_path, capsys, monkeypatch):
    from kicad_tools.cli import route_cmd

    monkeypatch.delenv(ADVISORY_ENV_VAR, raising=False)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)

    args = SimpleNamespace(census_advisory_gate=True)
    assert route_cmd._census_advisory_preflight(pcb, args) == 0
    err = capsys.readouterr().err
    assert "GO (no-report)" in err
    # Armed-but-inert must not look like a passed prediction.
    assert "nothing to gate on" in err


def test_route_preflight_gate_never_blocks_on_a_broken_report(tmp_path, monkeypatch):
    from kicad_tools.cli import route_cmd

    monkeypatch.delenv(ADVISORY_ENV_VAR, raising=False)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)

    broken = tmp_path / "broken.json"
    broken.write_text("{")
    args = SimpleNamespace(census_advisory=str(broken), census_advisory_gate=True)
    assert route_cmd._census_advisory_preflight(pcb, args) == 0

    missing = SimpleNamespace(
        census_advisory=str(tmp_path / "absent.json"), census_advisory_gate=True
    )
    assert route_cmd._census_advisory_preflight(pcb, missing) == 0


def test_route_preflight_gate_never_blocks_when_it_crashes(tmp_path, monkeypatch):
    """A predictor that raises is not evidence about the board."""
    from kicad_tools.cli import route_cmd
    from kicad_tools.router import crosstail_advisory as mod

    monkeypatch.delenv(ADVISORY_ENV_VAR, raising=False)

    def _boom(*_a, **_k):
        raise RuntimeError("kaboom")

    monkeypatch.setattr(mod, "emit_advisory", _boom)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    args = SimpleNamespace(
        census_advisory=str(_saturated_report(tmp_path)), census_advisory_gate=True
    )
    assert route_cmd._census_advisory_preflight(pcb, args) == 0


def test_route_main_aborts_with_exit_nine_before_any_router_work(tmp_path, capsys, monkeypatch):
    """End-to-end: the gate's return value actually reaches the exit code.

    Also the point of a *pre*-route gate -- no output board is produced,
    because nothing downstream of the preflight ever ran.
    """
    from kicad_tools.cli import route_cmd

    monkeypatch.delenv(ADVISORY_ENV_VAR, raising=False)
    pcb = tmp_path / "b.kicad_pcb"
    pcb.write_text(_MINIMAL_PCB)
    target = _saturated_report(tmp_path)

    rc = route_cmd.main([str(pcb), "--census-advisory", str(target), "--census-advisory-gate"])

    assert rc == GATE_EXIT_CODE
    assert "[crosstail-gate] NO-GO" in capsys.readouterr().err
    assert not (tmp_path / "b_routed.kicad_pcb").exists()


def test_route_help_documents_the_gate_and_its_exit_code(capsys):
    from kicad_tools.cli import route_cmd

    with pytest.raises(SystemExit):
        route_cmd.main(["--help"])
    out = capsys.readouterr().out
    assert "--census-advisory-gate" in out
    assert "--census-advisory-gate-pct" in out
    assert "9  --census-advisory-gate" in out


def test_outer_kct_parser_accepts_and_forwards_the_gate_flags():
    from kicad_tools.cli import route_cmd
    from kicad_tools.cli.commands import routing
    from kicad_tools.cli.parser import create_parser

    captured: list[list[str]] = []

    def _fake_route_main(argv):
        captured.append(list(argv))
        return 0

    original = route_cmd.main
    route_cmd.main = _fake_route_main
    try:
        args = create_parser().parse_args(
            [
                "route",
                "board.kicad_pcb",
                "--census-advisory",
                "census.json",
                "--census-advisory-gate",
                "--census-advisory-gate-pct",
                "75",
            ]
        )
        assert args.census_advisory_gate is True
        assert args.census_advisory_gate_pct == 75.0
        assert routing.run_route_command(args) == 0

        plain = create_parser().parse_args(["route", "board.kicad_pcb"])
        assert plain.census_advisory_gate is False
        assert plain.census_advisory_gate_pct is None
        assert routing.run_route_command(plain) == 0
    finally:
        route_cmd.main = original

    with_flags, without_flags = captured
    assert "--census-advisory-gate" in with_flags
    assert with_flags[with_flags.index("--census-advisory-gate-pct") + 1] == "75.0"
    # Flag-off path stays byte-identical to the advisory-only behaviour.
    assert "--census-advisory-gate" not in without_flags
    assert "--census-advisory-gate-pct" not in without_flags


def test_outer_parser_rejects_an_out_of_range_threshold():
    from kicad_tools.cli.parser import create_parser

    with pytest.raises(SystemExit):
        create_parser().parse_args(
            ["route", "board.kicad_pcb", "--census-advisory-gate-pct", "150"]
        )
