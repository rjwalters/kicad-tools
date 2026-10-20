"""Board 06/07 synthetic-fixture DRC gates are keyed by identity (Issue #7367).

Before #7367 both coverage gates let every ``diffpair_length_skew`` /
``diffpair_routing_continuity`` (and on board 07 ``match_group_length_skew``)
finding through by rule name, and bounded them only with a bare total in
``.github/routed-drc-tolerance.yml`` (board 06: 18, board 07: 8) plus board
06's ``DIFFPAIR_VIOLATION_BASELINE`` (18).  A new pair could fail in place of
a fixed one, and the identity-allowed impedance findings still used up the
board 07 total (12 > 8 on the seed-42 re-route).  These tests pin the
replacement: every blocking finding is a named entry or a failure, a listed
entry that stops firing is stale, and only unallowed findings count against
the (now strict 0) total.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
TOLERANCE = ROOT / ".github" / "routed-drc-tolerance.yml"
BOARD06_KEY = "boards/06-diffpair-test/regression-fixture/diffpair_test_routed.kicad_pcb"
BOARD07_KEY = "boards/07-matchgroup-test/regression-fixture/matchgroup_test_routed.kicad_pcb"


def load(name: str):
    spec = importlib.util.spec_from_file_location(
        f"{name}_7367", ROOT / "scripts" / "ci" / f"{name}.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[f"{name}_7367"] = module
    spec.loader.exec_module(module)
    return module


def violation(rule: str, identity: str, severity: str = "error") -> dict:
    return {"rule_id": rule, "severity": severity, "items": identity.split("/"), "nets": []}


def violations_for(allowance) -> list[dict]:
    """One violation record per allowance entry: the measured re-route."""
    return [violation(rule, ident) for rule, ident in sorted(allowance)]


def rule_counts(violations: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for v in violations:
        counts[v["rule_id"]] = counts.get(v["rule_id"], 0) + 1
    return counts


def identities_of(violations: list[dict]) -> dict[str, set[str]]:
    return load("identity_allowance").error_identities(violations)


# ---------------------------------------------------------------------------
# No count gates the fixture keys
# ---------------------------------------------------------------------------


def test_fixture_keys_have_no_tolerance_entry():
    tolerances = yaml.safe_load(TOLERANCE.read_text())["tolerances"]
    assert BOARD06_KEY not in tolerances
    assert BOARD07_KEY not in tolerances


@pytest.mark.parametrize("gate", ["check_diffpair_coverage", "check_matchgroup_coverage"])
def test_fixture_keys_resolve_to_strict_zero(gate):
    allowlist = load(gate).load_allowlist(TOLERANCE)
    assert allowlist.get(BOARD06_KEY, 0) == 0
    assert allowlist.get(BOARD07_KEY, 0) == 0


def test_count_constants_removed():
    diffpair = load("check_diffpair_coverage")
    matchgroup = load("check_matchgroup_coverage")
    assert not hasattr(diffpair, "DIFFPAIR_VIOLATION_BASELINE")
    assert not hasattr(diffpair, "check_zero_violations")
    assert not hasattr(matchgroup, "check_zero_violations")


@pytest.mark.parametrize(
    ("gate", "allowance_name"),
    [
        ("check_diffpair_coverage", "DIFFPAIR_FIXTURE_ALLOWED_FINDINGS"),
        ("check_matchgroup_coverage", "MATCHGROUP_FIXTURE_ALLOWED_FINDINGS"),
    ],
)
def test_no_via_in_pad_entries(gate, allowance_name):
    """The committed fixtures' via_in_pad drift is not allowed (out of scope)."""
    allowance = getattr(load(gate), allowance_name)
    assert not [key for key in allowance if key[0] == "via_in_pad"]


# ---------------------------------------------------------------------------
# Board 07
# ---------------------------------------------------------------------------


class TestBoard07:
    def gate(self):
        return load("check_matchgroup_coverage")

    def measured(self):
        """The seed-42 re-route: every allowed identity fires, plus advisory opens."""
        gate = self.gate()
        return violations_for(gate.board_allowance(BOARD07_KEY)) + [
            violation("connectivity", "TMDS_D0_N")
        ]

    def test_skew_pairs_are_listed_by_pair(self):
        gate = self.gate()
        pairs = {
            i for r, i in gate.MATCHGROUP_FIXTURE_ALLOWED_FINDINGS if r == "diffpair_length_skew"
        }
        assert pairs, "the seed-42 re-route measures skew pairs; they must be named"
        assert all("/" in p for p in pairs)

    def test_measured_findings_pass_with_zero_budget(self):
        """(a) the measured identities: nothing unexpected, nothing stale, 0 unallowed."""
        gate = self.gate()
        found = self.measured()
        ids = identities_of(found)
        assert gate.unexpected_baseline_errors(BOARD07_KEY, rule_counts(found), ids) == {}
        assert gate.stale_allowances(BOARD07_KEY, ids) == []
        assert gate.unallowed_blocking(BOARD07_KEY, found) == []

    def test_swapped_skew_pair_fails_within_old_count(self):
        """(b) one listed pair fixed, an unlisted pair failing: total unchanged, still fails."""
        gate = self.gate()
        found = self.measured()
        listed = next(v for v in found if v["rule_id"] == "diffpair_length_skew")
        found.remove(listed)
        found.append(violation("diffpair_length_skew", "DDR_DQS_N/DDR_DQS_P_new"))
        blocking = [v for v in found if v["rule_id"] != "connectivity"]
        ids = identities_of(found)
        unexpected = gate.unexpected_baseline_errors(BOARD07_KEY, rule_counts(found), ids)
        assert unexpected == {"diffpair_length_skew": 1}
        assert gate.unallowed_blocking(BOARD07_KEY, found) == [
            ("diffpair_length_skew", "DDR_DQS_N/DDR_DQS_P_new")
        ]
        assert len(blocking) == len(gate.board_allowance(BOARD07_KEY))

    def test_unlisted_continuity_finding_fails(self):
        """No rule is let through by name any more (the old ``expected`` bypass)."""
        gate = self.gate()
        found = self.measured() + [violation("diffpair_routing_continuity", "TMDS_D2_N/TMDS_D2_P")]
        ids = identities_of(found)
        assert gate.unexpected_baseline_errors(BOARD07_KEY, rule_counts(found), ids) == {
            "diffpair_routing_continuity": 1
        }

    def test_unlisted_match_group_fails(self):
        gate = self.gate()
        found = self.measured() + [violation("match_group_length_skew", "ADDR_BUS")]
        ids = identities_of(found)
        assert gate.unexpected_baseline_errors(BOARD07_KEY, rule_counts(found), ids) == {
            "match_group_length_skew": 1
        }
        assert gate.unallowed_blocking(BOARD07_KEY, found) == [
            ("match_group_length_skew", "ADDR_BUS")
        ]

    def test_fixed_skew_pair_is_stale(self):
        """(c) a listed pair that stops firing names its entry and tracker."""
        gate = self.gate()
        found = self.measured()
        listed = next(v for v in found if v["rule_id"] == "diffpair_length_skew")
        found.remove(listed)
        ids = identities_of(found)
        stale = gate.stale_allowances(BOARD07_KEY, ids)
        ident = "/".join(sorted(listed["items"]))
        issue = gate.MATCHGROUP_FIXTURE_ALLOWED_FINDINGS[("diffpair_length_skew", ident)]
        assert stale == [("diffpair_length_skew", ident, issue)]
        assert f"#{issue}" in gate.format_stale(stale)

    def test_identity_allowed_findings_do_not_count(self):
        """(c') the operator-ruling precondition: 12 allowed findings use no budget."""
        gate = self.gate()
        found = self.measured()
        assert len([v for v in found if v["rule_id"] != "connectivity"]) > 8
        assert gate.unallowed_blocking(BOARD07_KEY, found) == []

    def test_advisory_connectivity_is_not_blocking(self):
        gate = self.gate()
        found = [violation("connectivity", "TMDS_D0_N")]
        assert (
            gate.unexpected_baseline_errors(BOARD07_KEY, {"connectivity": 1}, identities_of(found))
            == {}
        )
        assert gate.unallowed_blocking(BOARD07_KEY, found) == []

    def test_other_key_gets_no_allowance(self):
        gate = self.gate()
        found = self.measured()
        other = "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"
        assert len(gate.unallowed_blocking(other, found)) == len(found) - 1
        assert gate.stale_allowances(other, {}) == []

    def test_every_tracker_is_cited(self):
        gate = self.gate()
        issues = set(gate.board_allowance(BOARD07_KEY).values())
        assert all(isinstance(i, int) and i > 0 for i in issues)
        # Closed trackers that used to be cited (#3916 pair-only skew, #7339
        # DDR byte, #7332 impedance) must not come back.
        assert issues.isdisjoint({3916, 7339, 7332})


# ---------------------------------------------------------------------------
# Board 06
# ---------------------------------------------------------------------------


class TestBoard06:
    def gate(self):
        return load("check_diffpair_coverage")

    def measured(self):
        return violations_for(self.gate().DIFFPAIR_FIXTURE_ALLOWED_FINDINGS)

    def test_no_skew_pair_is_allowed(self):
        """Every pair meets its ``max_skew_mm`` on the seed-42 re-route (#7548)."""
        gate = self.gate()
        skew = [k for k in gate.DIFFPAIR_FIXTURE_ALLOWED_FINDINGS if k[0] == "diffpair_length_skew"]
        assert skew == []
        assert not hasattr(gate, "DIFFPAIR_SKEW_TRACKER")

    def test_a_new_skew_finding_is_unallowed(self):
        gate = self.gate()
        found = self.measured() + [violation("diffpair_length_skew", "MIPI_D0+/MIPI_D0-")]
        ids = identities_of(found)
        assert gate.unexpected_baseline_errors(BOARD06_KEY, rule_counts(found), ids) == {
            "diffpair_length_skew": 1
        }
        assert len(gate.unallowed_blocking(BOARD06_KEY, found)) == 1

    def test_measured_findings_pass_with_zero_budget(self):
        gate = self.gate()
        found = self.measured()
        ids = identities_of(found)
        assert gate.unexpected_baseline_errors(BOARD06_KEY, rule_counts(found), ids) == {}
        assert gate.stale_allowances(BOARD06_KEY, ids) == []
        assert gate.unallowed_blocking(BOARD06_KEY, found) == []

    def test_seven_continuity_findings_fail_within_old_baseline(self):
        """(d) the free slots under the old 18: every one is now a failure."""
        gate = self.gate()
        found = self.measured()
        extra = [violation("diffpair_routing_continuity", f"PAIR{n}+/PAIR{n}-") for n in range(7)]
        found += extra
        assert len(found) <= 18
        ids = identities_of(found)
        assert gate.unexpected_baseline_errors(BOARD06_KEY, rule_counts(found), ids) == {
            "diffpair_routing_continuity": len(extra)
        }
        assert len(gate.unallowed_blocking(BOARD06_KEY, found)) == len(extra)

    def test_fixed_allowed_finding_is_stale(self):
        gate = self.gate()
        found = self.measured()
        listed = next(v for v in found if v["rule_id"] == "diffpair_uncoupled_length")
        found.remove(listed)
        stale = gate.stale_allowances(BOARD06_KEY, identities_of(found))
        ident = "/".join(sorted(listed["items"]))
        assert stale == [
            (
                "diffpair_uncoupled_length",
                ident,
                gate.DIFFPAIR_FIXTURE_ALLOWED_FINDINGS[("diffpair_uncoupled_length", ident)],
            )
        ]

    def test_without_identities_nothing_is_trusted(self):
        gate = self.gate()
        assert gate.unexpected_baseline_errors(BOARD06_KEY, {"diffpair_length_skew": 9}) == {
            "diffpair_length_skew": 9
        }

    def test_every_tracker_is_cited(self):
        issues = set(self.gate().DIFFPAIR_FIXTURE_ALLOWED_FINDINGS.values())
        assert all(isinstance(i, int) and i > 0 for i in issues)
        assert issues.isdisjoint({7312, 3540, 3544, 4019})


def test_unallowed_findings_counts_records_and_skips_warnings():
    identity = load("identity_allowance")
    allowance = {("impedance", "A"): 1}
    found = [
        violation("impedance", "A"),
        violation("impedance", "A"),
        violation("impedance", "B"),
        violation("impedance", "B"),
        violation("clearance", "C", severity="warning"),
        violation("connectivity", "D"),
    ]
    assert identity.unallowed_findings(
        allowance, found, advisory=lambda rule: rule == "connectivity"
    ) == [("impedance", "B"), ("impedance", "B")]


# ---------------------------------------------------------------------------
# End to end through check_board (subprocesses stubbed): the exit code
# ---------------------------------------------------------------------------


def _stub_matchgroup(monkeypatch, tmp_path, found: list[dict], fired_groups: set[str]):
    gate = load("check_matchgroup_coverage")
    board = tmp_path / "07-matchgroup-test"
    board.mkdir()
    pcb = board / "matchgroup_test_routed.kicad_pcb"

    def kct_check(_pcb, _sidecar, *, error_rules=None, error_violations=None):
        error_violations.extend(found)
        for v in found:
            error_rules[v["rule_id"]] = error_rules.get(v["rule_id"], 0) + 1
        return len([v for v in found if v["rule_id"] != "connectivity"])

    monkeypatch.setattr(gate, "re_route_board", lambda *_a: True)
    monkeypatch.setattr(gate, "find_routed_pcb", lambda _b: pcb)
    monkeypatch.setattr(gate, "find_net_class_map_sidecar", lambda *_a: pcb)
    monkeypatch.setattr(gate, "build_net_class_map_for_board", lambda _b: {"x": object()})
    monkeypatch.setattr(gate, "load_board_recipe_module", lambda _b: None)
    monkeypatch.setattr(gate, "recipe_baseline_key", lambda _p: BOARD07_KEY)
    monkeypatch.setattr(gate, "count_errors_via_kct_check", kct_check)
    monkeypatch.setattr(
        gate,
        "compute_rule_coverage",
        lambda *_a: ({"match_group_length_skew": 4}, {}, fired_groups),
    )
    return gate, board


class TestBoard07CheckBoard:
    def measured(self, gate):
        return violations_for(gate.board_allowance(BOARD07_KEY)) + [
            violation("connectivity", "TMDS_D0_N")
        ]

    def groups(self, gate):
        return set(gate.MATCHGROUP_ALLOWED_GROUPS[BOARD07_KEY])

    def test_measured_reroute_passes_at_strict_zero(self, monkeypatch, tmp_path, capsys):
        gate = load("check_matchgroup_coverage")
        found = self.measured(gate)
        gate, board = _stub_matchgroup(monkeypatch, tmp_path, found, self.groups(gate))
        allowlist = gate.load_allowlist(TOLERANCE)
        assert gate.check_board(board, allowlist, 42, skip_route=False) == 0
        out = capsys.readouterr().out
        assert "Unallowed blocking findings: 0 (allowed: 0)" in out

    def test_one_unlisted_finding_fails_even_far_below_old_total(
        self, monkeypatch, tmp_path, capsys
    ):
        gate = load("check_matchgroup_coverage")
        found = self.measured(gate)
        listed = [v for v in found if v["rule_id"] == "diffpair_length_skew"]
        # Fix every listed skew pair but one, and add one unlisted pair: the
        # total drops well below the old 8, the gate still fails.
        for v in listed[1:]:
            found.remove(v)
        found.append(violation("diffpair_length_skew", "NEW_N/NEW_P"))
        gate, board = _stub_matchgroup(monkeypatch, tmp_path, found, self.groups(gate))
        assert gate.check_board(board, {}, 42, skip_route=False) == 2
        out = capsys.readouterr().out
        assert "NEW_N/NEW_P" in out
        assert "Stale identity allowance" in out

    def test_stale_entry_fails_on_the_bundle_path_too(self, monkeypatch, tmp_path, capsys):
        """The E2E job's ``--skip-route`` bundle carries the same identities
        (measured, #7367), so staleness is enforced there as well."""
        gate = load("check_matchgroup_coverage")
        found = [v for v in self.measured(gate) if v["rule_id"] != "impedance"]
        gate, board = _stub_matchgroup(monkeypatch, tmp_path, found, self.groups(gate))
        assert gate.check_board(board, {}, 42, skip_route=True) == 2
        assert "Stale identity allowance" in capsys.readouterr().out
