"""``kct readiness --known-issue``: disclosed open issues in readiness.json (Issue #7261).

An open ``defect`` keeps a board from ``ready``; ``exception`` and ``advisory``
entries are disclosed and keep it ready.  The list is hash-bound evidence:
``read_readiness()`` rejects a report whose list was edited after the run.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from kicad_tools.cli import readiness_cmd
from kicad_tools.cli.board_readiness import read_readiness
from kicad_tools.cli.known_issues import (
    KnownIssueError,
    identity_key,
    known_error_allowance,
    known_warning_allowance,
    match_warning_allowance,
    merge_known_issues,
    parse_known_issue,
    validate_known_issues,
)
from tests.test_kct_readiness_cmd import FakeEngines, make_board, run

CHECKED_AT = "2026-10-18T12:00:00+00:00"


def _run(board: Path, *extra: str) -> tuple[int, dict]:
    return run(board, FakeEngines(board), "--checked-at", CHECKED_AT, *extra)


# ---------------------------------------------------------------------------
# Parsing / validation (offline, format-only)
# ---------------------------------------------------------------------------


def test_parse_splits_number_left_and_kind_right_so_summary_may_contain_colons():
    entry = parse_known_issue("7250:USB pair: 71.6 ohm vs 90 ohm:defect", "2026-10-18")
    assert entry == {
        "issue": 7250,
        "kind": "defect",
        "summary": "USB pair: 71.6 ohm vs 90 ohm",
        "recorded": "2026-10-18",
    }


def test_parse_accepts_a_warning_allowance_suffix():
    entry = parse_known_issue(
        "7220:one-sided thermal: C10/C11:advisory:thermal_asymmetry@C11-2,thermal_asymmetry@C10-2",
        "2026-10-18",
    )
    assert entry["kind"] == "advisory"
    assert entry["summary"] == "one-sided thermal: C10/C11"
    assert entry["warnings"] == [
        {"rule": "thermal_asymmetry", "ref": "C10", "pad": "C10-2"},
        {"rule": "thermal_asymmetry", "ref": "C11", "pad": "C11-2"},
    ]
    assert known_warning_allowance([entry]) == {
        ("thermal_asymmetry", "C10", "C10-2"): 7220,
        ("thermal_asymmetry", "C11", "C11-2"): 7220,
    }


_ID = {"rule": "via_at_pad_edge", "ref": "R3", "pad": "R3-2"}
_BASE = {"issue": 7220, "kind": "advisory", "summary": "s", "recorded": "2026-10-18"}


@pytest.mark.parametrize(
    "warnings",
    [
        {"via_at_pad_edge": 2},  # the old count form: no dual format
        [],
        [{"rule": "via_at_pad_edge", "ref": "R3"}],  # missing pad
        [{**_ID, "extra": "x"}],
        [{**_ID, "pad": " R3-2"}],
        [_ID, _ID],  # duplicate identity
        [{**_ID, "pad": "R4-2", "ref": "R4"}, _ID],  # unsorted
    ],
)
def test_validation_rejects_non_identity_warnings(warnings):
    with pytest.raises(KnownIssueError):
        validate_known_issues([{**_BASE, "warnings": warnings}])


def test_validation_rejects_an_identity_listed_by_two_issues():
    other = {**_BASE, "issue": 7221, "warnings": [_ID]}
    with pytest.raises(KnownIssueError):
        validate_known_issues([{**_BASE, "warnings": [_ID]}, other])


def _warning(rule, items):
    return {"rule_id": rule, "severity": "warning", "items": items}


_ALLOW = {("via_at_pad_edge", "R3", "R3-2"): 7220, ("via_at_pad_edge", "R4", "R4-2"): 7220}
_R3 = _warning("via_at_pad_edge", ["Via@1/1", "R3-2"])
_R4 = _warning("via_at_pad_edge", ["Via@2/2", "R4-2"])


def test_match_accepts_exactly_the_listed_identities():
    assert match_warning_allowance([_R3, _R4], _ALLOW) == ([], [])


def test_match_rejects_a_swapped_identity_at_the_same_count():
    swapped = _warning("via_at_pad_edge", ["Via@3/3", "R9-1"])
    unaccepted, stale = match_warning_allowance([_R3, swapped], _ALLOW)
    assert len(unaccepted) == 1 and "R9-1" in unaccepted[0]
    assert stale == ["via_at_pad_edge R4-2 (#7220)"]


def test_match_flags_a_stale_identity():
    assert match_warning_allowance([_R3], _ALLOW) == ([], ["via_at_pad_edge R4-2 (#7220)"])


def test_match_rejects_a_repeated_unkeyable_or_waived_aware_finding():
    unkeyable = _warning("diffpair_uncoupled_length", ["USB_D+", "USB_D-"])
    waived = {**_R4, "waived": True}
    unaccepted, _ = match_warning_allowance([_R3, _R3, unkeyable, waived], _ALLOW)
    assert len(unaccepted) == 2  # the repeated R3-2 and the unkeyable rule


@pytest.mark.parametrize(
    "spec",
    [
        "7250:summary:bogus",  # kind not in the enum
        "0:summary:defect",  # number must be positive
        "abc:summary:defect",
        "7250::defect",  # empty summary
        "7250-defect",  # no separators
        "7250:defect",  # no summary/kind split
    ],
)
def test_parse_rejects_malformed_values(spec):
    with pytest.raises(KnownIssueError):
        parse_known_issue(spec, "2026-10-18")


def test_validation_rejects_duplicates_unsorted_and_unknown_fields():
    a = {"issue": 1, "kind": "advisory", "summary": "a", "recorded": "2026-10-18"}
    b = {"issue": 2, "kind": "defect", "summary": "b", "recorded": "2026-10-18"}
    assert validate_known_issues([a, b]) == [a, b]
    for bad in ([a, a], [b, a], [{**a, "extra": 1}], [{**a, "recorded": "yesterday"}], {}):
        with pytest.raises(KnownIssueError):
            validate_known_issues(bad)


def test_merge_carries_forward_keeps_recorded_date_and_resolves_by_number():
    old = [
        {"issue": 6269, "kind": "exception", "summary": "paid drill", "recorded": "2026-10-01"},
        {"issue": 7250, "kind": "defect", "summary": "USB 71.6 ohm", "recorded": "2026-10-01"},
    ]
    restated = {
        "issue": 6269,
        "kind": "exception",
        "summary": "paid drill",
        "recorded": "2026-10-18",
    }
    merged = merge_known_issues(old, [restated], resolved=[7250])
    assert merged == [old[0]]  # unchanged restatement keeps its original date
    with pytest.raises(KnownIssueError):
        merge_known_issues(old, [], resolved=[1])  # not recorded on this board


# ---------------------------------------------------------------------------
# CLI behaviour
# ---------------------------------------------------------------------------


def test_bad_known_issue_refuses_with_exit_2_and_writes_nothing(tmp_path):
    board = make_board(tmp_path)
    for spec in ("7250:x:bogus", "nope"):
        code = readiness_cmd.main(
            [str(board), "--mfr", "jlcpcb", "--generate", "--known-issue", spec],
            engines=FakeEngines(board).bundle(),
        )
        assert code == 2
    code = readiness_cmd.main(
        [
            str(board),
            "--mfr",
            "jlcpcb",
            "--generate",
            "--known-issue",
            "1:a:advisory",
            "--known-issue",
            "1:b:advisory",
        ],
        engines=FakeEngines(board).bundle(),
    )
    assert code == 2
    assert not (board / "output" / "readiness.json").exists()
    assert not (board / "output" / "manufacturing").exists()


def test_advisory_and_exception_keep_ready_and_are_disclosed(tmp_path):
    board = make_board(tmp_path)
    code, report = _run(
        board,
        "--known-issue",
        "7220:one-sided thermal connection:advisory:thermal_asymmetry@C10-2",
        "--known-issue",
        "6269:paid 0.15 mm drill (ruling #7147):exception",
    )

    assert code == 0
    assert report["status"] == "ready"
    assert report["manufacturer"] == "jlcpcb"
    assert [e["issue"] for e in report["known_issues"]] == [6269, 7220]
    assert {e["recorded"] for e in report["known_issues"]} == {"2026-10-18"}
    readme = (board / "output" / "manufacturing" / "README.txt").read_text()
    waivers = readme.split("Accepted-risk waivers", 1)[1]
    assert "#6269 [exception] paid 0.15 mm drill (ruling #7147)" in waivers
    assert "#7220 [advisory] one-sided thermal connection" in waivers
    assert "no accepted-risk waivers" not in waivers
    verdict = read_readiness(board)
    assert verdict["status"] == "ready"
    assert verdict["known_issues"] == report["known_issues"]


def test_an_open_defect_blocks_and_names_the_issue(tmp_path):
    board = make_board(tmp_path)
    code, report = _run(board, "--known-issue", "7250:USB pair 71.6 ohm vs 90 ohm spec:defect")

    assert code == 1
    assert report["status"] == "blocked"
    assert "known defect #7250: USB pair 71.6 ohm vs 90 ohm spec" in report["blockers"]
    # Every automated check still passed: the defect alone blocks.
    assert all(check["status"] == "passed" for check in report["checks"])


def test_recorded_issues_are_carried_forward_until_resolved(tmp_path):
    board = make_board(tmp_path)
    code, first = _run(board, "--known-issue", "7220:thermal:advisory")
    assert code == 0

    code, second = _run(board)  # regeneration with no flags keeps the disclosure
    assert code == 0
    assert second["known_issues"] == first["known_issues"]

    code, third = _run(board, "--resolve-known-issue", "7220")
    assert code == 0
    assert "known_issues" not in third
    assert read_readiness(board)["status"] == "ready"
    readme = (board / "output" / "manufacturing" / "README.txt").read_text()
    assert "no accepted-risk waivers" in readme


def test_no_known_issues_writes_no_evidence_file_and_no_field(tmp_path):
    board = make_board(tmp_path)
    code, report = _run(board)
    assert code == 0
    assert "known_issues" not in report
    assert not any(name.endswith("known-issues.json") for name in report["inputs"])


# ---------------------------------------------------------------------------
# Hash binding
# ---------------------------------------------------------------------------


def _ready_board_with_advisory(tmp_path: Path) -> Path:
    board = make_board(tmp_path)
    code, report = _run(board, "--known-issue", "7220:thermal:advisory")
    assert code == 0
    assert any(name.endswith("known-issues.json") for name in report["inputs"])
    return board


def _edit_report(board: Path, edit) -> None:
    path = board / "output" / "readiness.json"
    report = json.loads(path.read_text())
    edit(report)
    path.write_text(json.dumps(report))


def test_editing_the_recorded_list_is_detected(tmp_path):
    board = _ready_board_with_advisory(tmp_path)
    _edit_report(board, lambda r: r["known_issues"][0].update(summary="something else"))
    verdict = read_readiness(board)
    assert verdict["status"] == "unverified"
    assert "differs from its hashed evidence" in verdict["blockers"][0]


def test_editing_a_recorded_warning_identity_is_detected(tmp_path):
    board = _ready_board_with_advisory(tmp_path)
    ident = {"rule": "via_at_pad_edge", "ref": "R3", "pad": "R3-2"}
    _edit_report(board, lambda r: r["known_issues"][0].update(warnings=[ident]))
    # The report now names an identity the hashed known-issues.json does not.
    verdict = read_readiness(board)
    assert verdict["status"] == "unverified"
    assert "differs from its hashed evidence" in verdict["blockers"][0]


def test_dropping_the_list_from_the_report_is_detected(tmp_path):
    board = _ready_board_with_advisory(tmp_path)
    _edit_report(board, lambda r: r.pop("known_issues"))
    assert read_readiness(board)["status"] == "unverified"


def test_editing_the_evidence_file_is_stale_evidence(tmp_path):
    board = _ready_board_with_advisory(tmp_path)
    evidence = board / "output" / "readiness" / "known-issues.json"
    evidence.write_text(json.dumps({"known_issues": []}))
    verdict = read_readiness(board)
    assert verdict["status"] == "unverified"
    assert "stale" in verdict["blockers"][0]


def test_unbound_list_is_rejected(tmp_path):
    board = make_board(tmp_path)
    assert _run(board)[0] == 0
    entry = {"issue": 1, "kind": "advisory", "summary": "x", "recorded": "2026-10-18"}
    _edit_report(board, lambda r: r.update(known_issues=[entry]))
    verdict = read_readiness(board)
    assert verdict["status"] == "unverified"
    assert "not bound" in verdict["blockers"][0]


def test_ready_with_a_bound_defect_is_rejected(tmp_path):
    """A hand-flipped ``ready`` cannot coexist with a recorded defect."""
    board = make_board(tmp_path)
    assert _run(board)[0] == 0
    entry = {"issue": 7250, "kind": "defect", "summary": "x", "recorded": "2026-10-18"}
    evidence = board / "output" / "readiness" / "known-issues.json"
    evidence.write_text(json.dumps({"known_issues": [entry]}))
    digest = hashlib.sha256(evidence.read_bytes()).hexdigest()

    def edit(report):
        report["known_issues"] = [entry]
        report["inputs"]["output/readiness/known-issues.json"] = digest

    _edit_report(board, edit)
    verdict = read_readiness(board)
    assert verdict["status"] == "unverified"
    assert "known defect #7250" in verdict["blockers"][0]


def test_unified_cli_forwards_known_issue_flags(tmp_path, monkeypatch):
    board = make_board(tmp_path)
    seen: dict = {}

    def fake_main(argv, engines=None):
        seen["argv"] = argv
        return 0

    monkeypatch.setattr(readiness_cmd, "main", fake_main)
    from kicad_tools.cli import main as cli_main

    argv = ["readiness", str(board), "--known-issue", "1:a:advisory", "--resolve-known-issue", "2"]
    assert cli_main(argv) == 0
    assert seen["argv"] == [
        str(board),
        "--known-issue",
        "1:a:advisory",
        "--resolve-known-issue",
        "2",
    ]


def test_unbind_and_drop_leaves_an_orphaned_evidence_file(tmp_path):
    """Removing the ``inputs`` entry and the list must not verify as ready (#7323)."""
    board = _ready_board_with_advisory(tmp_path)

    def edit(report):
        report.pop("known_issues")
        del report["inputs"]["output/readiness/known-issues.json"]

    _edit_report(board, edit)
    verdict = read_readiness(board)
    assert verdict["status"] == "unverified"
    assert "orphaned known-issues evidence" in verdict["blockers"][0]


def test_stray_known_issues_file_next_to_ready_board_is_rejected(tmp_path):
    board = make_board(tmp_path)
    assert _run(board)[0] == 0
    assert read_readiness(board)["status"] == "ready"
    (board / "output" / "readiness" / "known-issues.json").write_text('{"known_issues": []}')
    assert read_readiness(board)["status"] == "unverified"


def _verify(board: Path) -> tuple[int, dict]:
    code = readiness_cmd.main(
        [str(board), "--mfr", "jlcpcb", "--checked-at", CHECKED_AT],
        engines=FakeEngines(board).bundle(),
    )
    path = board / "output" / "readiness-verification" / "readiness.json"
    return code, json.loads(path.read_text())


def test_verify_does_not_orphan_the_committed_known_issues_file(tmp_path):
    """``--verify`` binds its own folder; the committed one is not an orphan (#7337)."""
    board = _ready_board_with_advisory(tmp_path)
    assert (board / "output" / "readiness" / "known-issues.json").is_file()
    code, report = _verify(board)
    assert code == 0, report["blockers"]
    assert report["status"] == "ready"
    assert "output/readiness-verification/known-issues.json" in report["inputs"]
    assert "output/readiness/known-issues.json" not in report["inputs"]
    assert [e["issue"] for e in report["known_issues"]] == [7220]
    # The committed record still validates beside the verification folder.
    assert read_readiness(board)["status"] == "ready"


def test_stray_known_issues_file_in_the_records_own_evidence_folder_is_rejected(tmp_path):
    """Scoping the scan (#7337) keeps the unlisted-sibling tamper case (#7323)."""
    board = _ready_board_with_advisory(tmp_path)
    nested = board / "output" / "readiness" / "fill-saved"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "known-issues.json").write_text('{"known_issues": []}')
    verdict = read_readiness(board)
    assert verdict["status"] == "unverified"
    assert "output/readiness/fill-saved/known-issues.json" in verdict["blockers"][0]


# ---------------------------------------------------------------------------
# Identity-keyed ``errors`` allowance on an exception (Issue #7355)
# ---------------------------------------------------------------------------

#: Three paid-drill vias, each a distinct ``rule@x/y`` identity (mm, 0.01).
VIA_A = ("dimension_via_drill", 10.0, 20.5)
VIA_B = ("dimension_via_drill", 11.25, -3.0)
VIA_C = ("netclass_via_drill", 30.0, 5.0)
IDS = [
    {"rule": "dimension_via_drill", "at": [10.0, 20.5]},
    {"rule": "dimension_via_drill", "at": [11.25, -3.0]},
    {"rule": "netclass_via_drill", "at": [30.0, 5.0]},
]
SPEC = "6269:paid drill:exception:" + ",".join(
    f"!{i['rule']}@{i['at'][0]:.2f}/{i['at'][1]:.2f}" for i in IDS
)


class _LocatedErrorEngines(FakeEngines):
    """FakeEngines whose ``kct check`` errors are the given ``(rule, x, y)`` findings.

    A ``None`` position drops the violation's location.  ``summary_errors``
    overrides the report's ``summary.errors`` so it can disagree with the
    listed violations.
    """

    def __init__(
        self, board: Path, found: list[tuple], summary_errors: int | None = None, **kwargs
    ) -> None:
        super().__init__(board, check_errors=len(found), **kwargs)
        self.found = found
        self.summary_errors = summary_errors

    def kct_check(self, pcb, mfr, report_path, extra):
        result = super().kct_check(pcb, mfr, report_path, extra)
        report = json.loads(report_path.read_text())
        errors = [v for v in report["violations"] if v["severity"] == "error"]
        for violation, (rule, x, y) in zip(errors, self.found, strict=True):
            violation["rule_id"] = rule
            violation["location"] = None if x is None else [x, y]
        if self.summary_errors is not None:
            report["summary"]["errors"] = self.summary_errors
        report_path.write_text(json.dumps(report, indent=2) + "\n")
        return result


def _sub(tmp_path: Path, name: str) -> Path:
    (tmp_path / name).mkdir()
    return tmp_path / name


def _run_errors(board: Path, found: list[tuple], spec: str = SPEC, **kwargs) -> tuple[int, dict]:
    fake = _LocatedErrorEngines(board, found, meta_overall="FAILED", drc_status="FAILED", **kwargs)
    return run(board, fake, "--checked-at", CHECKED_AT, "--known-issue", spec)


def test_parse_accepts_error_identities_on_an_exception():
    entry = parse_known_issue(SPEC, "2026-10-18")
    assert entry["errors"] == IDS
    assert "warnings" not in entry
    assert known_error_allowance([entry]) == {(i["rule"], *i["at"]): 6269 for i in IDS}


def test_parse_canonicalises_identities_and_splits_warning_allowance():
    entry = parse_known_issue("1:s:exception:thermal_asymmetry@U1-2,!via@1.5/-0", "2026-10-18")
    assert entry["warnings"] == [{"rule": "thermal_asymmetry", "ref": "U1", "pad": "U1-2"}]
    assert entry["errors"] == [{"rule": "via", "at": [1.5, 0.0]}]


@pytest.mark.parametrize(
    "spec",
    [
        "1:s:exception:!via=3",  # the retired rule-count form
        "1:s:exception:!via@1/2,!via@1.001/2",  # same identity twice after rounding
        "1:s:exception:!via@1",  # no y
    ],
)
def test_parse_rejects_malformed_error_allowances(spec):
    with pytest.raises(KnownIssueError):
        parse_known_issue(spec, "2026-10-18")


@pytest.mark.parametrize("kind", ["defect", "advisory"])
def test_errors_allowance_is_rejected_off_an_exception(kind):
    with pytest.raises(KnownIssueError, match="only an exception"):
        parse_known_issue(f"1:s:{kind}:!via@1/2", "2026-10-18")
    entry = {"issue": 1, "kind": kind, "summary": "s", "recorded": "2026-10-18"}
    with pytest.raises(KnownIssueError, match="only an exception"):
        validate_known_issues([{**entry, "errors": [{"rule": "v", "at": [1.0, 2.0]}]}])


@pytest.mark.parametrize(
    "errors",
    [
        {"dimension_via_drill": 7},  # the retired count map
        [],
        ["via@1.00/2.00"],  # the retired string form
        [{"rule": "via", "at": [1.001, 2.0]}],  # not rounded to 0.01 mm
        [{"rule": "via", "at": [1.0]}],  # not a position
        [{"rule": "via", "ref": "U1", "at": [1.0, 2.0]}],  # not exactly {rule, at}
        [{"rule": "b", "at": [1.0, 2.0]}, {"rule": "a", "at": [1.0, 2.0]}],  # unsorted
        [{"rule": "a", "at": [1.0, 2.0]}, {"rule": "a", "at": [1.0, 2.0]}],  # duplicate
    ],
)
def test_validation_rejects_non_canonical_error_lists(errors):
    entry = {"issue": 1, "kind": "exception", "summary": "s", "recorded": "2026-10-18"}
    with pytest.raises(KnownIssueError):
        validate_known_issues([{**entry, "errors": errors}])


def test_exact_identity_match_passes_and_the_unwaived_run_blocks(tmp_path):
    code, report = _run_errors(make_board(_sub(tmp_path, "a")), [VIA_A, VIA_B, VIA_C])
    assert code == 0, report["blockers"]
    assert report["status"] == "ready"
    assert (
        "accepted exactly"
        in next(c for c in report["checks"] if c["name"] == "kct_check")["detail"]
    )
    # Without the allowance the same errors block.
    other = _sub(tmp_path, "b")
    board = make_board(other)
    fake = _LocatedErrorEngines(
        board, [VIA_A, VIA_B, VIA_C], meta_overall="FAILED", drc_status="FAILED"
    )
    code, report = run(
        board, fake, "--checked-at", CHECKED_AT, "--known-issue", "6269:paid drill:exception"
    )
    assert code != 0 and report["status"] == "blocked"


@pytest.mark.parametrize(
    ("found", "fragment"),
    [
        # Same rules and the same counts, but one via moved: a swapped identity.
        ([VIA_A, ("dimension_via_drill", 99.0, 99.0), VIA_C], "not recorded"),
        # A listed identity no longer fires (stale allowance).
        ([VIA_A, VIA_B], "no longer fires"),
        # An extra error at an unlisted identity.
        ([VIA_A, VIA_B, VIA_C, ("clearance", 1.0, 1.0)], "not recorded"),
        # A listed identity reported twice (a higher count).
        ([VIA_A, VIA_B, VIA_C, VIA_A], "repeated"),
        # An error with no location cannot be matched to an identity.
        ([VIA_A, VIA_B, VIA_C, ("clearance", None, None)], "no usable location"),
    ],
)
def test_any_identity_mismatch_blocks(tmp_path, found, fragment):
    code, report = _run_errors(make_board(tmp_path), found)
    assert code != 0
    assert report["status"] == "blocked"
    assert any(fragment in b for b in report["blockers"]), report["blockers"]


@pytest.mark.parametrize("delta", [1, -1])
def test_a_summary_count_that_disagrees_with_the_violations_blocks(tmp_path, delta):
    # The listed violations match the allowance exactly, but the report says
    # there are more (or fewer) errors than it lists: not fully attributable.
    found = [VIA_A, VIA_B, VIA_C]
    code, report = _run_errors(make_board(tmp_path), found, summary_errors=len(found) + delta)
    assert code != 0
    assert report["status"] == "blocked"
    check = next(c for c in report["checks"] if c["name"] == "kct_check")
    assert check["status"] == "failed"
    assert "accepted exactly" not in check["detail"]


def test_a_location_inside_the_same_0_01_mm_cell_is_the_same_identity(tmp_path):
    jitter = ("dimension_via_drill", 10.004, 20.496)
    code, report = _run_errors(make_board(tmp_path), [jitter, VIA_B, VIA_C])
    assert code == 0, report["blockers"]


def test_error_allowance_does_not_waive_another_failed_gate(tmp_path):
    board = make_board(tmp_path)
    code, report = _run_errors(board, [VIA_A, VIA_B, VIA_C], erc_status="NOT RUN")
    assert code != 0
    assert report["status"] != "ready"


def test_error_allowance_waives_drc_only_and_keeps_a_failed_erc_blocking(tmp_path):
    board = make_board(tmp_path)
    code, report = _run_errors(board, [VIA_A, VIA_B, VIA_C], erc_status="FAILED")
    assert code != 0
    assert report["status"] == "blocked"
    check = next(c for c in report["checks"] if c["name"] == "kct_check")
    assert check["status"] == "failed"
    assert any("erc" in b for b in report["blockers"]), report["blockers"]
    assert not any("drc" in b for b in report["blockers"]), report["blockers"]


def test_widening_the_allowance_in_known_issues_json_is_detected(tmp_path):
    board = make_board(tmp_path)
    code, _ = _run_errors(board, [VIA_A, VIA_B, VIA_C])
    assert code == 0
    evidence = board / "output" / "readiness" / "known-issues.json"
    data = json.loads(evidence.read_text())
    data["known_issues"][0]["errors"].append({"rule": "clearance", "at": [1.0, 1.0]})
    evidence.write_text(json.dumps(data))
    verdict = read_readiness(board)
    assert verdict["status"] == "unverified"
    assert "stale" in verdict["blockers"][0]
    # Editing the report's copy too (so only the hashed file disagrees) is caught as well.
    _edit_report(
        board,
        lambda r: r["known_issues"][0]["errors"].append({"rule": "clearance", "at": [1.0, 1.0]}),
    )
    assert read_readiness(board)["status"] == "unverified"


def test_an_identity_cannot_be_listed_by_two_issues():
    one = parse_known_issue("1:a:exception:!via@1/2", "2026-10-18")
    two = parse_known_issue("2:b:exception:!via@1/2", "2026-10-18")
    with pytest.raises(KnownIssueError, match="listed by both #1 and #2"):
        validate_known_issues([one, two])


def test_both_identity_shapes_share_one_checker_and_one_key():
    pad = {"rule": "via", "ref": "U1", "pad": "U1-1"}
    at = {"rule": "via", "at": [1.0, 2.0]}
    assert identity_key(pad) != identity_key(at)
    assert sorted([identity_key(pad), identity_key(at)]) == [identity_key(at), identity_key(pad)]
    entry = {"issue": 1, "kind": "exception", "summary": "s", "recorded": "2026-10-18"}
    # Each field accepts only its own shape, with the same checker's errors.
    with pytest.raises(
        KnownIssueError, match=r"warnings identity must be exactly \{rule, ref, pad\}"
    ):
        validate_known_issues([{**entry, "warnings": [at]}])
    with pytest.raises(KnownIssueError, match=r"errors identity must be exactly \{rule, at"):
        validate_known_issues([{**entry, "errors": [pad]}])
    # Sorting and ownership use the same key: a repeated key is rejected in either field.
    for field, item in (("warnings", pad), ("errors", at)):
        with pytest.raises(KnownIssueError, match="more than once"):
            validate_known_issues([{**entry, field: [item, item]}])
