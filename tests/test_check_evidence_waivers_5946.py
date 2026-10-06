"""Evidence-bound waivers, ``kct check --diff`` and coverage states (issue #5946)."""

from __future__ import annotations

import json
import shutil
import subprocess
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.cli.check_diff import DiffSideError, diff_reports, resolve_side, strip_driver_args
from kicad_tools.drc.waivers import apply_waivers_to_report
from kicad_tools.validate import DRCResults, DRCViolation
from kicad_tools.validate import coverage as cov
from kicad_tools.validate.evidence import compute_evidence_hash, finding_key
from kicad_tools.validate.rules.waivers import (
    WAIVER_STALE_RULE_ID,
    WAIVER_UNUSED_RULE_ID,
    apply_waivers,
    discover_waivers_sidecar,
    load_waivers,
    waivers_from_dict,
    write_keyed_waivers,
)

FIXTURE = Path(__file__).parent / "fixtures" / "projects"


def _v(**kw) -> DRCViolation:
    base = {
        "rule_id": "clearance_pad_pad",
        "severity": "error",
        "message": "too close",
        "location": (10.0, 20.0),
        "layer": "F.Cu",
        "actual_value": 0.1,
        "required_value": 0.2,
        "items": ("U1", "C3"),
        "nets": ("VCC", "GND"),
    }
    base.update(kw)
    return DRCViolation(**base)


def _keyed(v: DRCViolation, evidence_hash: str | None = None) -> dict:
    return {
        "key": v.key,
        "evidence_hash": evidence_hash or compute_evidence_hash(v),
        "reason": "reviewed",
        "reviewer": "ee",
        "date": "2026-10-06",
    }


# ---------------------------------------------------------------------------
# Stable key + evidence hash
# ---------------------------------------------------------------------------


class TestKeyAndEvidence:
    def test_key_is_sorted_and_order_insensitive(self):
        a = _v(items=("U1", "C3"), nets=("VCC", "GND"))
        b = _v(items=("C3", "U1"), nets=("GND", "VCC"))
        assert a.key == b.key == "clearance_pad_pad|C3,U1|GND,VCC|F.Cu"
        assert finding_key("r", (), (), None) == "r|||"

    def test_key_ignores_location_and_message(self):
        assert _v().key == _v(location=(99.0, 1.0), message="other").key

    def test_hash_tracks_geometry_and_is_float_noise_stable(self):
        base = compute_evidence_hash(_v())
        assert base.startswith("ev1:")
        assert compute_evidence_hash(_v(location=(10.0000001, 20.0))) == base
        assert compute_evidence_hash(_v(location=(10.5, 20.0))) != base
        assert compute_evidence_hash(_v(actual_value=0.15)) != base
        # Wording is not evidence.
        assert compute_evidence_hash(_v(message="reworded")) == base

    def test_to_dict_carries_key_and_hash(self):
        v = replace(_v(), evidence_hash="ev1:abc")
        d = v.to_dict()
        assert d["key"] == v.key
        assert d["evidence_hash"] == "ev1:abc"
        assert "waiver_status" not in d


# ---------------------------------------------------------------------------
# Schema v3 parsing
# ---------------------------------------------------------------------------


class TestSchemaV3:
    def test_keyed_entry_parses(self):
        v = _v()
        w = waivers_from_dict({"version": 3, "waivers": [_keyed(v)]})
        entry = w.entries[0]
        assert entry.key == v.key
        assert entry.rule == "clearance_pad_pad"
        assert entry.reviewer == "ee"

    def test_keyed_entry_needs_version_3(self):
        with pytest.raises(ValueError, match="needs waivers version 3"):
            waivers_from_dict({"version": 2, "waivers": [_keyed(_v())]})

    @pytest.mark.parametrize("missing", ["evidence_hash", "reason", "reviewer", "date"])
    def test_keyed_entry_requires_fields(self, missing):
        raw = _keyed(_v())
        del raw[missing]
        with pytest.raises(ValueError, match=missing):
            waivers_from_dict({"version": 3, "waivers": [raw]})

    def test_keyed_entry_rejects_legacy_fields_and_bad_date(self):
        raw = {**_keyed(_v()), "rule": "x"}
        with pytest.raises(ValueError, match="must not also carry 'rule'"):
            waivers_from_dict({"version": 3, "waivers": [raw]})
        raw = {**_keyed(_v()), "date": "yesterday"}
        with pytest.raises(ValueError, match="ISO date"):
            waivers_from_dict({"version": 3, "waivers": [raw]})

    def test_version_2_files_still_load(self):
        w = waivers_from_dict(
            {
                "version": 2,
                "waivers": [{"rule": "r", "items": ["U1"], "reason": "x", "issue": "#1"}],
            }
        )
        assert w.entries[0].key is None and w.entries[0].evidence_hash is None

    def test_per_board_sidecar_is_discovered_first(self, tmp_path):
        pcb = tmp_path / "b.kicad_pcb"
        pcb.write_text("")
        (tmp_path / ".kct_waivers.json").write_text("{}")
        assert discover_waivers_sidecar(pcb) == tmp_path / ".kct_waivers.json"
        (tmp_path / "b.kct-waivers.json").write_text("{}")
        assert discover_waivers_sidecar(pcb) == tmp_path / "b.kct-waivers.json"


# ---------------------------------------------------------------------------
# Applying waivers: waived / stale / unused
# ---------------------------------------------------------------------------


class TestApply:
    def test_matching_evidence_waives(self):
        v = replace(_v(), evidence_hash=compute_evidence_hash(_v()))
        results = DRCResults(violations=[v])
        apply_waivers(results, waivers_from_dict({"version": 3, "waivers": [_keyed(v)]}))
        assert results.waived_count == 1
        assert results.error_count == 0

    def test_changed_evidence_is_stale_and_not_suppressed(self):
        reviewed = _v()
        moved = _v(location=(11.0, 20.0))
        moved = replace(moved, evidence_hash=compute_evidence_hash(moved))
        results = DRCResults(violations=[moved])
        apply_waivers(results, waivers_from_dict({"version": 3, "waivers": [_keyed(reviewed)]}))

        assert results.waived_count == 0
        assert results.error_count == 1  # the finding returned
        active = results.violations[0]
        assert active.stale_waiver_hash == compute_evidence_hash(reviewed)
        assert active.to_dict()["waiver_status"] == "stale"
        stale = [v for v in results.violations if v.rule_id == WAIVER_STALE_RULE_ID]
        assert len(stale) == 1 and stale[0].severity == "warning"
        assert moved.evidence_hash in stale[0].message
        assert not [v for v in results.violations if v.rule_id == WAIVER_UNUSED_RULE_ID]

    def test_key_naming_no_finding_is_unused(self):
        results = DRCResults(violations=[])
        apply_waivers(results, waivers_from_dict({"version": 3, "waivers": [_keyed(_v())]}))
        assert [v.rule_id for v in results.violations] == [WAIVER_UNUSED_RULE_ID]

    def test_legacy_entry_with_evidence_hash_goes_stale_too(self):
        v = replace(_v(), evidence_hash="ev1:now")
        entry = {
            "rule": v.rule_id,
            "items": list(v.items),
            "reason": "x",
            "issue": "#1",
            "evidence_hash": "ev1:then",
        }
        results = DRCResults(violations=[v])
        apply_waivers(results, waivers_from_dict({"version": 3, "waivers": [entry]}))
        assert results.error_count == 1
        assert results.violations[0].stale_waiver_hash == "ev1:then"

    def test_kicad_cli_path_ignores_keyed_entries(self):
        report = SimpleNamespace(violations=[])
        w = waivers_from_dict({"version": 3, "waivers": [_keyed(_v())]})
        assert apply_waivers_to_report(report, w).unused == []


class TestWriter:
    def test_write_replace_and_preserve(self, tmp_path):
        path = tmp_path / "b.kct-waivers.json"
        legacy = {"rule": "r", "items": ["U9"], "reason": "x", "issue": "#1"}
        path.write_text(json.dumps({"version": 2, "waivers": [legacy]}))

        a = replace(_v(), evidence_hash="ev1:aaa")
        assert write_keyed_waivers(path, [a, a], reason="ok", reviewer="me") == 1
        data = json.loads(path.read_text())
        assert data["version"] == 3
        assert data["waivers"][0] == legacy
        assert data["waivers"][1]["evidence_hash"] == "ev1:aaa"

        # Re-waiving the same key replaces the old (stale) entry.
        b = replace(_v(), evidence_hash="ev1:bbb")
        write_keyed_waivers(path, [b], reason="re-reviewed", reviewer="me", issue="#9")
        entries = load_waivers(path).entries
        assert [e.evidence_hash for e in entries if e.key] == ["ev1:bbb"]
        assert entries[-1].issue == "#9"

    def test_refuses_to_clobber_a_malformed_file(self, tmp_path):
        path = tmp_path / "w.json"
        path.write_text("{nope")
        with pytest.raises(ValueError):
            write_keyed_waivers(
                path, [replace(_v(), evidence_hash="ev1:x")], reason="r", reviewer="me"
            )
        assert path.read_text() == "{nope"


# ---------------------------------------------------------------------------
# Coverage states
# ---------------------------------------------------------------------------


def _checker(**kw):
    pcb = SimpleNamespace(zones=kw.pop("zones", []), nets=kw.pop("nets", {}))
    base = {
        "pcb": pcb,
        "net_class_map": None,
        "current_path_specs": [],
        "physical_copper_gap_mm": None,
    }
    base.update(kw)
    return SimpleNamespace(**base)


class TestCoverage:
    def test_unfilled_zone_makes_zone_clearance_unknown_and_blocking(self):
        zone = SimpleNamespace(keepout=None, net_name="GND", filled_polygons=[])
        c = cov.precheck("segment_zone", _checker(zones=[zone]))
        assert c.state == "unknown" and c.blocking and c.is_blocking_unknown
        assert c.status.startswith("unknown:zone_fills_absent")
        filled = SimpleNamespace(keepout=None, net_name="GND", filled_polygons=[[(0, 0)]])
        assert cov.precheck("via_zone", _checker(zones=[filled])) is None

    def test_diff_pair_nets_without_map_are_unknown(self):
        nets = {1: SimpleNamespace(name="USB_D+"), 2: SimpleNamespace(name="USB_D-")}
        c = cov.precheck("diffpair_length_skew", _checker(nets=nets))
        assert c.state == "unknown" and c.blocking
        c = cov.precheck("diffpair_length_skew", _checker())
        assert c.status == "skipped:no_diff_pairs_detected"

    def test_inputs_and_opt_ins(self, tmp_path):
        assert cov.precheck("ampacity", _checker()).status == "skipped:no_declared_targets"
        assert cov.precheck("sch_fields", _checker()).status == "unknown:needs_input:schematic"
        assert not cov.precheck("sch_fields", _checker()).blocking
        assert cov.precheck("sch_fields", _checker(), drc_only=True).status == "skipped:drc_only"
        pcb = tmp_path / "b.kicad_pcb"
        assert cov.precheck("netclass_floor", _checker(), pcb_path=pcb).state == "skipped"
        pcb.with_suffix(".kicad_pro").write_text("{broken")
        assert (
            cov.precheck("netclass_floor", _checker(), pcb_path=pcb).status
            == "unknown:unreadable_kicad_pro"
        )

    def test_blocking_unknowns_reads_json(self):
        data = {
            "a": {"state": "unknown", "blocking": True},
            "b": {"state": "unknown", "blocking": False},
            "c": {"state": "checked", "blocking": True},
        }
        assert cov.blocking_unknowns(data) == ["a"]


# ---------------------------------------------------------------------------
# --diff helpers
# ---------------------------------------------------------------------------


def _row(key: str, h: str, severity: str = "error", waived: bool = False) -> dict:
    return {"key": key, "evidence_hash": h, "severity": severity, "waived": waived}


class TestDiffReports:
    def test_introduced_resolved_changed_unchanged(self):
        old = {"violations": [_row("a", "1"), _row("b", "1"), _row("c", "1")]}
        new = {
            "violations": [
                _row("a", "1"),
                _row("b", "2"),
                _row("d", "1", "warning"),
                _row("e", "1", waived=True),
            ]
        }
        d = diff_reports(old, new)
        s = d["summary"]
        assert (s["unchanged"], s["changed"], s["introduced"], s["resolved"]) == (1, 1, 1, 1)
        assert d["introduced"][0]["key"] == "d"
        assert s["introduced_errors"] == 0 and s["introduced_warnings"] == 1
        assert d["resolved"][0]["key"] == "c"

    def test_duplicate_keys_pair_as_multiset(self):
        old = {"violations": [_row("a", "1")]}
        new = {"violations": [_row("a", "1"), _row("a", "1")]}
        assert diff_reports(old, new)["summary"]["introduced"] == 1

    def test_coverage_changes(self):
        old = {"violations": [], "coverage": {"x": {"status": "checked"}}}
        new = {"violations": [], "coverage": {"x": {"status": "unknown:needs_input:y"}}}
        assert diff_reports(old, new)["coverage_changes"] == {
            "x": {"old": "checked", "new": "unknown:needs_input:y"}
        }

    def test_strip_driver_args(self):
        argv = ["--diff", "a", "b", "--mfr", "jlcpcb", "--format", "json", "-o", "x", "--strict"]
        assert strip_driver_args(argv) == ["--mfr", "jlcpcb", "--strict"]
        assert strip_driver_args(["--form=json", "--outp", "f", "--only", "clearance"]) == [
            "--only",
            "clearance",
        ]

    def test_git_revision_side(self, tmp_path, monkeypatch):
        if shutil.which("git") is None:
            pytest.skip("git not installed")
        repo = tmp_path / "repo"
        (repo / "boards" / "x").mkdir(parents=True)
        board = repo / "boards" / "x" / "x.kicad_pcb"
        board.write_text("rev1")
        (repo / "boards" / "x" / "x.kicad_pro").write_text("{}")
        env = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", "r1"], check=True)
        board.write_text("rev2")
        monkeypatch.chdir(repo)

        scratch = tmp_path / "scratch"
        side = resolve_side("HEAD:boards/x/x.kicad_pcb", scratch, "old")
        assert side.read_text() == "rev1"
        assert side.with_suffix(".kicad_pro").is_file()  # sidecars come along
        assert resolve_side(str(board), scratch, "new") == board.resolve()

    def test_option_like_revision_rejected(self, tmp_path, monkeypatch):
        if shutil.which("git") is None:
            pytest.skip("git not installed")
        repo = tmp_path / "repo"
        (repo / "boards").mkdir(parents=True)
        (repo / "boards" / "b.kicad_pcb").write_text("x")
        env = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        subprocess.run(["git", "-C", str(repo), "add", "."], check=True)
        subprocess.run(["git", "-C", str(repo), *env, "commit", "-qm", "r1"], check=True)
        monkeypatch.chdir(repo)
        victim = tmp_path / "x"
        victim.write_text("keep")
        for spec in (
            f"--output={victim}:boards/b.kicad_pcb",
            f"--output={tmp_path}/full.tar:HEAD/b",
        ):
            with pytest.raises(DiffSideError):
                resolve_side(spec, tmp_path / "scratch", "old")
        assert victim.read_text() == "keep"
        assert not (tmp_path / "full.tar").exists()

    def test_rev_path_escaping_repo_is_clean_error(self, tmp_path, monkeypatch):
        if shutil.which("git") is None:
            pytest.skip("git not installed")
        repo = tmp_path / "repo"
        repo.mkdir()
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        monkeypatch.chdir(repo)
        with pytest.raises(DiffSideError):
            resolve_side("HEAD:../../etc/passwd", tmp_path / "scratch", "old")


# ---------------------------------------------------------------------------
# End to end on the fixture board
# ---------------------------------------------------------------------------


def _check_json(capsys, *argv: str) -> tuple[int, dict]:
    code = check_cmd.main([*argv, "--format", "json"])
    out = capsys.readouterr().out
    return code, json.loads(out)


@pytest.fixture
def project(tmp_path) -> Path:
    dest = tmp_path / "proj"
    shutil.copytree(FIXTURE, dest)
    return dest / "test_project.kicad_pcb"


def _move_d1(pcb: Path) -> None:
    text = pcb.read_text()
    marker = '(uuid "fp-d1-uuid")\n\t\t(at 140 50)'
    assert marker in text
    pcb.write_text(text.replace(marker, '(uuid "fp-d1-uuid")\n\t\t(at 141 50)'))


def test_waiver_round_trip_goes_stale_when_geometry_changes(project, capsys):
    """AC: waive, change geometry under the waiver, finding returns as stale."""
    _, before = _check_json(capsys, str(project))
    assert "coverage" in before and "coverage_blocking_unknown" in before["summary"]
    target = next(v for v in before["violations"] if "D1" in ",".join(v["items"]))
    key = target["key"]

    code, waived = _check_json(
        capsys,
        str(project),
        "--waive",
        key,
        "--waive-reason",
        "intentional for test",
        "--waive-reviewer",
        "pytest",
    )
    sidecar = project.with_name("test_project.kct-waivers.json")
    assert sidecar.is_file()
    assert any(v["key"] == key and v["waived"] for v in waived["violations"])
    assert waived["summary"]["errors"] == before["summary"]["errors"] - (
        1 if target["severity"] == "error" else 0
    )

    _move_d1(project)
    _, after = _check_json(capsys, str(project))
    returned = [v for v in after["violations"] if v["key"] == key]
    assert returned and not any(v["waived"] for v in returned)
    assert all(v.get("waiver_status") == "stale" for v in returned)
    assert after["summary"]["stale_waivers"] == len(returned)
    assert any(v["rule_id"] == WAIVER_STALE_RULE_ID for v in after["violations"])


def test_waive_unknown_key_is_an_error(project, capsys):
    code = check_cmd.main(
        [str(project), "--waive", "nope|||", "--waive-reason", "r", "--waive-reviewer", "me"]
    )
    assert code == 1
    assert "names no current finding" in capsys.readouterr().err


def test_diff_two_revisions(project, tmp_path, capsys):
    old = tmp_path / "old" / "test_project.kicad_pcb"
    shutil.copytree(project.parent, old.parent)
    _move_d1(project)

    code = check_cmd.main(["--diff", str(old), str(project), "--format", "json"])
    data = json.loads(capsys.readouterr().out)  # stdout is the diff document alone
    assert code in (0, 2)
    assert data["old"]["spec"] == str(old)
    s = data["summary"]
    assert s["changed"] >= 1  # D1 findings persisted with moved evidence
    assert s["introduced"] == s["resolved"] == 0
    assert any("D1" in c["key"] for c in data["changed"])
    assert all(c["old"]["evidence_hash"] != c["new"]["evidence_hash"] for c in data["changed"])


def test_diff_rejects_positional_pcb(project):
    with pytest.raises(SystemExit):
        check_cmd.main([str(project), "--diff", str(project), str(project)])
