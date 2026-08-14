"""Issue #4799: the crossing-tail census as a structured, aggregated report.

The #4580 census already computes, per crossover, everything a capacity
indicator needs -- how many of the 225 via-site candidates were legal and how
many distinct ``v1`` barrels those legal candidates used.  It emitted that as
free text, one crossover at a time, so the only way to get the *distribution*
(the number a "is this lattice saturated?" question actually asks) was to
scrape stdout.

This module pins the structured surface that replaces the scrape:

1. The per-crossover record and its derived predicates (``saturated``,
   ``no_ordering_lever``) -- no board fixture required.
2. The aggregate summary: counts, percentages, the denominator choice, and the
   advisory verdict thresholds.
3. The "not applicable" result a run that never synthesizes a crossover must
   produce -- explicitly distinct from "0% saturated".
4. The JSON document and the env-gated flush.
5. Report-ONLY: capture changes neither the route that ships nor the #4635
   ``_census_elapsed_s`` budget credit.

Fixtures for (5) are imported from ``tests/test_diffpair_shadow.py``, the
module that owns the #4580 census block -- the same convention
``tests/test_diffpair_census_budget.py`` uses.
"""

from __future__ import annotations

import json
import re

import pytest

from kicad_tools.router.crosstail_census import (
    CENSUS_COLLECTOR,
    REPORT_ENV_VAR,
    SATURATED_PCT_ADVISORY_THRESHOLD,
    SCHEMA_VERSION,
    VERDICT_NO_ORDERING_LEVER,
    VERDICT_NOT_APPLICABLE,
    VERDICT_ORDERING_LEVERS,
    VERDICT_SATURATED,
    CrossingTailCensusCollector,
    CrossingTailCensusRecord,
    CrossingTailCensusSummary,
    flush_report,
    report_path_from_env,
    write_report,
)
from tests.test_diffpair_census_budget import _SealedPathfinder
from tests.test_diffpair_shadow import (
    _crossing_router,
    _crossing_tail,
    _tail_pads,
)


@pytest.fixture(autouse=True)
def _clean_process_collector():
    """The collector is process-wide; keep tests from seeing each other's records."""
    CENSUS_COLLECTOR.reset()
    yield
    CENSUS_COLLECTOR.reset()


def _census_on(monkeypatch) -> None:
    import kicad_tools.router.diffpair_routing as dpr_mod

    monkeypatch.setattr(dpr_mod, "_CROSSTAIL_CENSUS", True)


def _record(legal: int, distinct_v1: int, *, total: int = 225, census_s: float = 0.0):
    return CrossingTailCensusRecord(
        net_name="USB3_TX1-",
        head=(5.0, 5.0),
        goal=(8.0, 5.0),
        legal=legal,
        total=total,
        distinct_v1=distinct_v1,
        census_s=census_s,
    )


# ---------------------------------------------------------------------------
# The per-crossover record
# ---------------------------------------------------------------------------


def test_record_classifies_a_saturated_crossover():
    """``legal=0`` is the saturated extreme: the lattice offered nothing."""
    record = _record(legal=0, distinct_v1=0)

    assert record.saturated is True
    assert record.no_ordering_lever is False, (
        "a saturated crossover has no ordering lever either, but folding the "
        "two together hides which failure a board is actually hitting"
    )


def test_record_classifies_a_single_site_legal_set():
    """Legal candidates that share one ``v1`` carry the same barrel every time."""
    record = _record(legal=2, distinct_v1=1)

    assert record.saturated is False
    assert record.no_ordering_lever is True


def test_record_with_real_ordering_choice_is_neither():
    record = _record(legal=17, distinct_v1=9)

    assert record.saturated is False
    assert record.no_ordering_lever is False


def test_record_dict_carries_the_header_field_names():
    """The JSON keys mirror the ``[crosstail-census]`` header, so the two agree."""
    payload = _record(legal=2, distinct_v1=1, census_s=0.0164).to_dict()

    assert payload == {
        "net_name": "USB3_TX1-",
        "head": [5.0, 5.0],
        "goal": [8.0, 5.0],
        "legal": 2,
        "total": 225,
        "distinct_v1": 1,
        "census_s": 0.0164,
        "saturated": False,
        "no_ordering_lever": True,
    }


# ---------------------------------------------------------------------------
# The aggregate summary
# ---------------------------------------------------------------------------


def test_summary_counts_and_percentages():
    """The board-06 shape: overwhelmingly saturated, a couple of legal sets."""
    records = [_record(legal=0, distinct_v1=0) for _ in range(19)]
    records.append(_record(legal=2, distinct_v1=1))

    summary = CrossingTailCensusSummary.from_records(records)

    assert summary.crossovers_scanned == 20
    assert summary.saturated == 19
    assert summary.saturated_pct == 95.0
    assert summary.applicable is True


def test_no_ordering_lever_percentage_is_over_the_unsaturated_set():
    """Denominator choice is load-bearing.

    "Of the crossovers that had a choice, how many had no real choice" is the
    actionable figure; against the full scan it would be diluted to near-zero
    by the saturated majority and mean nothing.
    """
    records = [_record(legal=0, distinct_v1=0) for _ in range(90)]
    records += [_record(legal=3, distinct_v1=1) for _ in range(5)]
    records += [_record(legal=3, distinct_v1=3) for _ in range(5)]

    summary = CrossingTailCensusSummary.from_records(records)

    assert summary.no_ordering_lever == 5
    assert summary.no_ordering_lever_pct == 50.0  # 5 of the 10 unsaturated
    assert summary.saturated_pct == 90.0


def test_summary_reports_max_distinct_v1_and_total_census_seconds():
    records = [
        _record(legal=1, distinct_v1=1, census_s=0.25),
        _record(legal=8, distinct_v1=4, census_s=0.75),
    ]

    summary = CrossingTailCensusSummary.from_records(records)

    assert summary.distinct_v1_max == 4
    assert summary.census_s_total == pytest.approx(1.0)


def test_verdict_flips_at_the_documented_threshold():
    """The advisory heuristic is a documented constant, not a magic number."""
    saturated = [_record(legal=0, distinct_v1=0) for _ in range(9)]
    open_one = [_record(legal=5, distinct_v1=5)]

    at_threshold = CrossingTailCensusSummary.from_records(saturated + open_one)
    below = CrossingTailCensusSummary.from_records(saturated[:-1] + open_one * 2)

    assert SATURATED_PCT_ADVISORY_THRESHOLD == 90.0
    assert at_threshold.saturated_pct == 90.0
    assert at_threshold.verdict == VERDICT_SATURATED
    assert below.saturated_pct < SATURATED_PCT_ADVISORY_THRESHOLD
    assert below.verdict == VERDICT_ORDERING_LEVERS


def test_inert_verdict_catches_a_lattice_saturation_alone_would_call_healthy():
    """An all-singleton legal set is as inert as an empty one.

    ``saturated_pct`` is 0% here -- every crossover had a legal candidate --
    but every legal set sits on ONE ``v1``, so no ordering key can move any of
    them.  Keying the verdict on saturation alone would report this lattice as
    having levers it does not have.
    """
    records = [_record(legal=3, distinct_v1=1) for _ in range(10)]

    summary = CrossingTailCensusSummary.from_records(records)

    assert summary.saturated_pct == 0.0
    assert summary.inert_pct == 100.0
    assert summary.verdict == VERDICT_NO_ORDERING_LEVER
    assert "advisory" in summary.format_human()


def test_inert_pct_unions_saturated_and_lever_less_crossovers():
    records = [_record(legal=0, distinct_v1=0) for _ in range(6)]
    records += [_record(legal=2, distinct_v1=1) for _ in range(2)]
    records += [_record(legal=2, distinct_v1=2) for _ in range(2)]

    summary = CrossingTailCensusSummary.from_records(records)

    assert summary.saturated_pct == 60.0
    assert summary.no_ordering_lever == 2
    assert summary.inert_pct == 80.0
    assert summary.verdict == VERDICT_ORDERING_LEVERS  # 80% < the 90% threshold


def test_verdict_threshold_is_overridable_without_changing_the_data():
    records = [_record(legal=0, distinct_v1=0), _record(legal=4, distinct_v1=2)]

    strict = CrossingTailCensusSummary.from_records(records, saturated_threshold_pct=50.0)
    lenient = CrossingTailCensusSummary.from_records(records, saturated_threshold_pct=99.0)

    assert strict.saturated_pct == lenient.saturated_pct == 50.0
    assert strict.verdict == VERDICT_SATURATED
    assert lenient.verdict == VERDICT_ORDERING_LEVERS


def test_empty_scan_is_not_applicable_not_zero_percent_saturated():
    """Boards 05 / 07 never exercise this path -- say so, don't invent a figure."""
    summary = CrossingTailCensusSummary.from_records([])

    assert summary.crossovers_scanned == 0
    assert summary.applicable is False
    assert summary.verdict == VERDICT_NOT_APPLICABLE
    assert summary.saturated_pct == 0.0
    assert summary.no_ordering_lever_pct == 0.0


def test_human_summary_spells_out_the_not_applicable_case():
    text = CrossingTailCensusSummary.from_records([]).format_human()

    assert "0 crossovers scanned" in text
    assert "NOT APPLICABLE" in text
    assert "not a 0% saturation result" in text
    assert all(line.startswith("[crosstail-census-summary]") for line in text.splitlines())


def test_human_summary_carries_the_advisory_only_when_saturated():
    saturated = CrossingTailCensusSummary.from_records(
        [_record(legal=0, distinct_v1=0) for _ in range(10)]
    ).format_human()
    healthy = CrossingTailCensusSummary.from_records(
        [_record(legal=9, distinct_v1=6) for _ in range(10)]
    ).format_human()

    assert "advisory" in saturated
    assert "placement / escape planning" in saturated
    assert "10/10 (100.0%)" in saturated
    assert "advisory" not in healthy
    assert f"verdict={VERDICT_ORDERING_LEVERS}" in healthy


# ---------------------------------------------------------------------------
# The collector and the JSON document
# ---------------------------------------------------------------------------


def test_collector_accumulates_in_scan_order_and_resets():
    collector = CrossingTailCensusCollector()
    collector.add(_record(legal=0, distinct_v1=0))
    collector.add(_record(legal=4, distinct_v1=2))

    assert [r.legal for r in collector.records] == [0, 4]
    assert collector.summary().crossovers_scanned == 2

    collector.reset()
    assert collector.records == ()
    assert collector.summary().verdict == VERDICT_NOT_APPLICABLE


def test_report_document_is_envelope_summary_and_detail(tmp_path):
    collector = CrossingTailCensusCollector()
    collector.add(_record(legal=0, distinct_v1=0))
    collector.add(_record(legal=2, distinct_v1=1, census_s=0.5))

    path = write_report(tmp_path / "nested" / "census.json", collector, census_enabled=True)
    payload = json.loads(path.read_text())

    assert path.exists(), "the report must create its parent directory"
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["report"] == "crosstail-census"
    assert payload["census_enabled"] is True
    assert re.match(r"\d{4}-\d\d-\d\dT", payload["generated_at"])
    assert payload["summary"]["crossovers_scanned"] == 2
    assert payload["summary"]["saturated"] == 1
    assert payload["summary"]["saturated_pct"] == 50.0
    assert [c["legal"] for c in payload["crossovers"]] == [0, 2]


def test_report_document_is_deterministic_sorted_json(tmp_path):
    """Machine-output contract: one document, sorted keys (see machine-output.md)."""
    collector = CrossingTailCensusCollector()
    collector.add(_record(legal=1, distinct_v1=1))

    text = write_report(tmp_path / "census.json", collector, census_enabled=False).read_text()
    keys = list(json.loads(text).keys())

    assert keys == sorted(keys)
    assert text.endswith("\n")
    assert json.loads(text)["summary"]["verdict"] == VERDICT_NO_ORDERING_LEVER


def test_report_for_a_run_that_scanned_nothing(tmp_path):
    """The board-05 / board-07 shape: a clean file that says "not applicable"."""
    path = write_report(
        tmp_path / "census.json", CrossingTailCensusCollector(), census_enabled=False
    )
    payload = json.loads(path.read_text())

    assert payload["crossovers"] == []
    assert payload["summary"]["applicable"] is False
    assert payload["summary"]["crossovers_scanned"] == 0
    assert payload["summary"]["verdict"] == VERDICT_NOT_APPLICABLE


# ---------------------------------------------------------------------------
# The env-gated flush
# ---------------------------------------------------------------------------


def test_report_path_is_none_when_unset_or_blank():
    assert report_path_from_env({}) is None
    assert report_path_from_env({REPORT_ENV_VAR: "   "}) is None
    assert report_path_from_env({REPORT_ENV_VAR: "/tmp/x.json"}).name == "x.json"


def test_flush_is_a_no_op_without_the_env_var(tmp_path, capsys):
    collector = CrossingTailCensusCollector()
    collector.add(_record(legal=0, distinct_v1=0))

    assert flush_report(collector, env={}) is None
    assert capsys.readouterr().err == "", "a disabled diagnostic must stay silent"


def test_flush_writes_and_announces_the_report(tmp_path, capsys):
    target = tmp_path / "census.json"
    collector = CrossingTailCensusCollector()
    collector.add(_record(legal=0, distinct_v1=0))

    written = flush_report(collector, env={REPORT_ENV_VAR: str(target)})

    assert written == target
    assert json.loads(target.read_text())["summary"]["saturated"] == 1
    err = capsys.readouterr().err
    assert "report written to" in err
    assert "1 crossover(s) scanned" in err


def test_empty_flush_does_not_clobber_a_report_with_records(tmp_path, capsys):
    """Child processes inherit the env var; their empty flush must stand down.

    A board script shells out to ``kicad-cli`` and helper ``python`` runs, each
    of which would otherwise overwrite the parent's real measurement with its
    own "0 crossovers scanned" document -- invisibly, because the child's
    stderr is captured.  Observed while measuring board-06 for #4799.
    """
    target = tmp_path / "census.json"
    measured = CrossingTailCensusCollector()
    measured.add(_record(legal=0, distinct_v1=0))
    flush_report(measured, env={REPORT_ENV_VAR: str(target)})
    capsys.readouterr()

    assert flush_report(CrossingTailCensusCollector(), env={REPORT_ENV_VAR: str(target)}) is None
    assert json.loads(target.read_text())["summary"]["crossovers_scanned"] == 1
    assert "kept" in capsys.readouterr().err


def test_empty_flush_still_writes_when_the_existing_report_is_empty(tmp_path, capsys):
    """The no-clobber rule protects measurements, not stale empty files."""
    target = tmp_path / "census.json"
    write_report(target, CrossingTailCensusCollector())

    assert flush_report(CrossingTailCensusCollector(), env={REPORT_ENV_VAR: str(target)}) == target
    assert json.loads(target.read_text())["summary"]["verdict"] == VERDICT_NOT_APPLICABLE


def test_a_run_with_records_always_overwrites(tmp_path, capsys):
    target = tmp_path / "census.json"
    write_report(target, CrossingTailCensusCollector())
    measured = CrossingTailCensusCollector()
    measured.add(_record(legal=4, distinct_v1=3))

    assert flush_report(measured, env={REPORT_ENV_VAR: str(target)}) == target
    assert json.loads(target.read_text())["summary"]["crossovers_scanned"] == 1


def test_flush_never_raises_when_the_path_is_unwritable(tmp_path, capsys):
    """Report-only means non-blocking: a bad path must not abort a 25-min route."""
    unwritable = tmp_path / "census.json" / "nope.json"
    (tmp_path / "census.json").write_text("not a directory")

    assert (
        flush_report(CrossingTailCensusCollector(), env={REPORT_ENV_VAR: str(unwritable)}) is None
    )
    assert "NOT written" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Capture is wired to the real census -- and changes nothing
# ---------------------------------------------------------------------------


def test_capture_is_off_when_the_census_is_off():
    """A default run pays nothing and records nothing."""
    dpr = _crossing_router()

    assert _crossing_tail(dpr) is not None
    assert dpr._census_records == []
    assert CENSUS_COLLECTOR.records == ()


def test_capture_matches_the_printed_header(monkeypatch, capsys):
    """The record and the header are two views of ONE measurement."""
    _census_on(monkeypatch)
    dpr = _crossing_router()

    assert _crossing_tail(dpr) is not None

    header = next(
        line for line in capsys.readouterr().out.splitlines() if "[crosstail-census] net=" in line
    )
    match = re.search(r"legal=(\d+)/(\d+) distinct_v1=(\d+) census_s=([0-9.]+)", header)
    assert match is not None, header
    assert len(dpr._census_records) == 1
    record = dpr._census_records[0]
    assert record.legal == int(match.group(1))
    assert record.total == int(match.group(2))
    assert record.distinct_v1 == int(match.group(3))
    assert record.census_s == pytest.approx(float(match.group(4)), abs=5e-5)
    assert record.net_name == "USB3_TX1-"
    assert record.head == (5.0, 5.0)


def test_capture_also_lands_in_the_process_collector(monkeypatch, capsys):
    """The JSON report is written from the process-wide collector, not a router."""
    _census_on(monkeypatch)
    dpr = _crossing_router()

    assert _crossing_tail(dpr) is not None
    capsys.readouterr()

    assert len(CENSUS_COLLECTOR.records) == 1
    assert CENSUS_COLLECTOR.records[0] == dpr._census_records[0]
    assert CENSUS_COLLECTOR.summary().crossovers_scanned == 1


def test_saturated_crossover_is_captured_as_saturated(monkeypatch, capsys):
    """The measurement that matters: a whole-lattice miss, recorded as such."""
    _census_on(monkeypatch)
    dpr = _crossing_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))

    tail = dpr._synthesize_crossing_tail(_SealedPathfinder(), head, goal, 0, [])
    capsys.readouterr()

    assert tail is None
    record = dpr._census_records[0]
    assert record.legal == 0
    assert record.total == 225
    assert record.saturated is True
    assert CENSUS_COLLECTOR.summary().saturated_pct == 100.0
    assert CENSUS_COLLECTOR.summary().verdict == VERDICT_SATURATED


def test_capture_does_not_change_the_route_that_ships(monkeypatch, capsys):
    """Report-only, at the level the acceptance criterion cares about.

    The censused tail must still be the un-instrumented first-legal one --
    same vias, same segments -- with the capture path active.
    """
    baseline = _crossing_tail(_crossing_router())
    capsys.readouterr()

    _census_on(monkeypatch)
    dpr = _crossing_router()
    censused = _crossing_tail(dpr)
    capsys.readouterr()

    assert baseline is not None and censused is not None
    assert dpr._census_records, "non-vacuity: the capture must actually have run"
    assert [(v.x, v.y, v.layers) for v in censused.vias] == [
        (v.x, v.y, v.layers) for v in baseline.vias
    ]
    assert [(s.x1, s.y1, s.x2, s.y2, s.layer) for s in censused.segments] == [
        (s.x1, s.y1, s.x2, s.y2, s.layer) for s in baseline.segments
    ]


def test_capture_does_not_disturb_the_4635_budget_credit(monkeypatch, capsys):
    """The #4635 lesson re-asserted against the new code.

    The credit must still be exactly the value the header reports -- the
    capture appends AFTER the credit is stamped, so it can neither inflate nor
    deflate it.
    """
    _census_on(monkeypatch)
    dpr = _crossing_router()

    assert _crossing_tail(dpr) is not None
    capsys.readouterr()

    assert dpr._census_records[0].census_s == pytest.approx(dpr._census_elapsed_s, abs=5e-5)


def test_saturated_crossover_still_credits_zero(monkeypatch, capsys):
    """Both modes scan the whole lattice, so there is nothing to credit."""
    _census_on(monkeypatch)
    dpr = _crossing_router()
    head, goal = _tail_pads((5.0, 5.0), (8.0, 5.0))

    dpr._synthesize_crossing_tail(_SealedPathfinder(), head, goal, 0, [])
    capsys.readouterr()

    assert dpr._census_elapsed_s == 0.0
    assert dpr._census_records[0].census_s == 0.0
