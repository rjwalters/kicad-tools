"""``kct check --migrate-waivers``: re-bind ev3 waivers to ev4 (issue #9454).

#9431 bumped the evidence version ``ev3`` -> ``ev4`` (footprint ``at`` hashed in
sheet coordinates), which made every ``ev3`` waiver stale at once.  The ev3
digest is exactly recomputable, so a waiver whose recomputed ev3 digest still
equals the recorded one is provably unchanged and can be re-bound.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from kicad_tools.cli import check_cmd
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCViolation
from kicad_tools.validate.evidence import (
    EvidenceContext,
    annotate_evidence,
    compute_evidence_hash,
)
from kicad_tools.validate.rules.waivers import (
    WAIVER_STALE_RULE_ID,
    apply_waivers,
    migrate_evidence_waivers,
    waivers_from_dict,
)

_BOARD = """\
(kicad_pcb
  (version 20240108)
  (generator "test")
  (generator_version "8.0")
  (general (thickness 1.6) (legacy_teardrops no))
  (paper "A4")
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (setup (pad_to_mask_clearance 0))
  (net 0 "")
  (net 1 "GND")
  (net 2 "VCC")
  (gr_rect (start {ox} {oy}) (end 180 160)
    (stroke (width 0.1) (type default)) (fill none) (layer "Edge.Cuts")
    (uuid "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"))
  (footprint "test:R"
    (layer "F.Cu")
    (uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
    (at 100 100)
    (property "Reference" "R1" (at 0 -2 0) (layer "F.SilkS") (uuid "11111111-1111-1111-1111-111111111111") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 1 "GND"))
  )
  (footprint "test:R"
    (layer "F.Cu")
    (uuid "dddddddd-dddd-dddd-dddd-dddddddddddd")
    (at {r2x} 100)
    (property "Reference" "R2" (at 0 -2 0) (layer "F.SilkS") (uuid "22222222-2222-2222-2222-222222222222") (effects (font (size 1 1) (thickness 0.15))))
    (pad "1" smd rect (at 0 0) (size 0.5 0.5) (layers "F.Cu") (net 2 "VCC"))
  )
)
"""

COURTYARD_R1 = "courtyard_missing|R1||F.Cu"
COURTYARD_R2 = "courtyard_missing|R2||F.Cu"
PAD_PAD = "clearance_pad_pad|R1-1,R2-1|GND,VCC|F.Cu"


def _write_board(tmp_path: Path, *, ox: float = 37.0, oy: float = 41.0, r2x: float = 100.6) -> Path:
    path = tmp_path / "board.kicad_pcb"
    path.write_text(_BOARD.format(ox=ox, oy=oy, r2x=r2x))
    return path


def _findings(board: Path) -> tuple[list[DRCViolation], PCB]:
    """The board's real ``kct check`` findings, annotated with ev4 hashes."""
    from kicad_tools.validate import DRCChecker

    pcb = PCB.load(board)
    results = DRCChecker(pcb, manufacturer="jlcpcb", layers=2, copper_oz=1.0).check_all()
    annotate_evidence(results, pcb)
    return [v for v in results.violations if isinstance(v, DRCViolation)], pcb


def _ev3_hash(board: Path, key: str) -> str:
    """The hash an ev3-era kct recorded for ``key`` (ev3 recompute, no stash needed)."""
    findings, pcb = _findings(board)
    ctx = EvidenceContext(pcb, recipe="ev3")
    from collections import Counter

    counts = Counter(f.key for f in findings)
    (finding,) = [f for f in findings if f.key == key]
    return compute_evidence_hash(finding, ctx, multiplicity=counts[key])


def _entry(key: str, evidence_hash: str, **extra: str) -> dict:
    return {
        "key": key,
        "evidence_hash": evidence_hash,
        "reason": "EE-reviewed, intentional",
        "reviewer": "ee",
        "date": "2026-10-06",
        "issue": "proj#12",
        **extra,
    }


def _write_waivers(tmp_path: Path, entries: list[dict]) -> Path:
    path = tmp_path / "board.kct-waivers.json"
    path.write_text(json.dumps({"version": 3, "waivers": entries}, indent=2) + "\n")
    return path


def _check_json(capsys, *argv: str) -> dict:
    check_cmd.main([*argv, "--format", "json", "--mfr", "jlcpcb"])
    out = capsys.readouterr().out
    return json.loads(out[out.index("{") :])


def _migrate(capsys, board: Path, waivers: Path, *extra: str) -> tuple[int, str]:
    code = check_cmd.main([str(board), "--waivers", str(waivers), "--migrate-waivers", *extra])
    return code, capsys.readouterr().out


# ---------------------------------------------------------------------------
# The recompute itself
# ---------------------------------------------------------------------------


class TestPriorRecipe:
    def test_ev3_differs_from_ev4_only_on_an_origin_offset_board(self, tmp_path):
        offset = _write_board(tmp_path)
        findings, pcb = _findings(offset)
        (f,) = [x for x in findings if x.key == COURTYARD_R1]
        assert pcb.board_origin != (0.0, 0.0)
        ev3 = compute_evidence_hash(f, EvidenceContext(pcb, recipe="ev3"))
        assert ev3.startswith("ev3:")
        assert f.evidence_hash.startswith("ev4:")
        assert ev3.split(":")[1] != f.evidence_hash.split(":")[1]

    def test_ev3_equals_ev4_digest_when_origin_is_zero(self, tmp_path):
        board = _write_board(tmp_path, ox=0.0, oy=0.0)
        findings, pcb = _findings(board)
        (f,) = [x for x in findings if x.key == COURTYARD_R1]
        ev3 = compute_evidence_hash(f, EvidenceContext(pcb, recipe="ev3"))
        assert ev3.split(":")[1] == f.evidence_hash.split(":")[1]

    def test_ev3_digests_match_the_frozen_pre_9437_tool(self, tmp_path):
        # Pinned literals (issue #9482): byte-identical to what the genuine
        # pre-#9437 kct recorded for this board, so the ev3 recipe is checked
        # independently of the helper that implements it.
        board = _write_board(tmp_path)
        assert {k: _ev3_hash(board, k) for k in (COURTYARD_R1, COURTYARD_R2, PAD_PAD)} == {
            COURTYARD_R1: "ev3:50a30c56e1a439d6",
            COURTYARD_R2: "ev3:61dfb3608161d65f",
            PAD_PAD: "ev3:9116bd87e75d1889",
        }

    def test_only_ev3_is_available(self):
        with pytest.raises(ValueError, match="ev2"):
            EvidenceContext(None, recipe="ev2")


# ---------------------------------------------------------------------------
# migrate_evidence_waivers
# ---------------------------------------------------------------------------


class TestMigration:
    def test_unchanged_evidence_is_rebound_and_metadata_preserved(self, tmp_path):
        board = _write_board(tmp_path)
        old = _ev3_hash(board, COURTYARD_R1)
        waivers = _write_waivers(tmp_path, [_entry(COURTYARD_R1, old)])
        findings, pcb = _findings(board)

        report = migrate_evidence_waivers(
            waivers,
            findings,
            EvidenceContext(pcb, recipe="ev3"),
            write=True,
            tool_version="9.9.9",
            date="2026-11-19",
        )

        assert [m.key for m in report.migrated] == [COURTYARD_R1]
        assert report.written
        (entry,) = json.loads(waivers.read_text())["waivers"]
        (f,) = [x for x in findings if x.key == COURTYARD_R1]
        assert entry["evidence_hash"] == f.evidence_hash
        assert entry["rebound_from"] == old
        assert entry["rebound_at"] == "2026-11-19"
        assert entry["rebound_by"] == "kct 9.9.9"
        # Reviewer, date, reason, issue are verbatim.
        assert entry["reviewer"] == "ee"
        assert entry["date"] == "2026-10-06"
        assert entry["reason"] == "EE-reviewed, intentional"
        assert entry["issue"] == "proj#12"
        # The loader accepts what was written and the finding is waived again.
        (loaded,) = waivers_from_dict(json.loads(waivers.read_text())).entries
        assert (loaded.rebound_from, loaded.rebound_by) == (old, "kct 9.9.9")

    def test_waived_count_is_preserved_across_the_bump(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        keys = [COURTYARD_R1, COURTYARD_R2, PAD_PAD]
        waivers = _write_waivers(tmp_path, [_entry(k, _ev3_hash(board, k)) for k in keys])

        before = _check_json(capsys, str(board), "--waivers", str(waivers))
        assert before["summary"]["waived"] == 0
        assert before["summary"]["stale_waivers"] == len(keys)

        code, out = _migrate(capsys, board, waivers, "--write")
        assert code == 0
        assert f"migrated: {len(keys)}" in out

        after = _check_json(capsys, str(board), "--waivers", str(waivers))
        assert after["summary"]["waived"] == len(keys)
        assert after["summary"]["stale_waivers"] == 0
        assert after["summary"]["orphan_waivers"] == 0

    def test_changed_evidence_is_not_migrated_and_stays_stale(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        waivers = _write_waivers(
            tmp_path,
            [
                _entry(COURTYARD_R1, _ev3_hash(board, COURTYARD_R1)),
                _entry(COURTYARD_R2, _ev3_hash(board, COURTYARD_R2)),
            ],
        )
        # Move R2 only: R2's evidence changed, R1's did not.
        _write_board(tmp_path, r2x=105.0)
        before = waivers.read_text()

        code, out = _migrate(capsys, board, waivers, "--write")

        assert code == 0
        assert "migrated: 1" in out and "stale (left alone): 1" in out
        assert f"stale: {COURTYARD_R2}" in out
        data = json.loads(waivers.read_text())["waivers"]
        assert before != waivers.read_text()
        by_key = {e["key"]: e for e in data}
        assert by_key[COURTYARD_R1]["evidence_hash"].startswith("ev4:")
        assert by_key[COURTYARD_R2]["evidence_hash"].startswith("ev3:")
        assert "rebound_from" not in by_key[COURTYARD_R2]

    def test_key_matching_no_finding_is_an_orphan_and_untouched(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        gone = "courtyard_missing|U9||F.Cu"
        waivers = _write_waivers(tmp_path, [_entry(gone, "ev3:0123456789abcdef")])
        before = waivers.read_text()

        code, out = _migrate(capsys, board, waivers, "--write")

        assert code == 0
        assert "orphan (left alone): 1" in out
        assert f"orphan (no current finding): {gone}" in out
        assert waivers.read_text() == before

    def test_write_keeps_orphan_and_stale_entries_byte_equal(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        gone = "courtyard_missing|U9||F.Cu"
        orphan = _entry(gone, "ev3:0123456789abcdef")
        stale = _entry(COURTYARD_R2, _ev3_hash(board, COURTYARD_R2))
        waivers = _write_waivers(
            tmp_path,
            [_entry(COURTYARD_R1, _ev3_hash(board, COURTYARD_R1)), orphan, stale],
        )
        _write_board(tmp_path, r2x=105.0)  # R2 evidence changes -> stale

        code, out = _migrate(capsys, board, waivers, "--write")

        assert code == 0 and "migrated: 1" in out
        data = json.loads(waivers.read_text())["waivers"]
        assert len(data) == 3
        assert data[1] == orphan
        assert data[2] == stale
        assert not list(tmp_path.glob(".*.tmp"))

    def test_orphan_lists_rewaive_candidates(self, tmp_path):
        board = _write_board(tmp_path)
        findings, pcb = _findings(board)
        # A legacy-named silk key: the finding is now keyed by geometry.
        silk = next(f for f in findings if f.rule_id == "silk_overlap")
        legacy_key = silk.key.replace("{11111111-1111-1111-1111-111111111111}", "")
        legacy_key = re.sub(r"\{[0-9a-f-]+\}", "", legacy_key)
        waivers = _write_waivers(tmp_path, [_entry(legacy_key, "ev3:0123456789abcdef")])

        report = migrate_evidence_waivers(waivers, findings, EvidenceContext(pcb, recipe="ev3"))

        assert [o.key for o in report.orphan] == [legacy_key]

    def test_orphan_path_ampacity_waiver_names_per_layer_candidates(self, tmp_path):
        board = _write_board(tmp_path)
        findings, pcb = _findings(board)

        # #8797/#8798: path_ampacity is now keyed per layer, so a waiver
        # written against the old empty-layer key matches nothing.
        def amp(layer: str) -> DRCViolation:
            return DRCViolation(
                rule_id="path_ampacity",
                severity="error",
                message="too much current",
                layer=layer,
                items=("Path1",),
                nets=("VBUS",),
            )

        current = [*findings, amp("F.Cu"), amp("B.Cu")]
        waivers = _write_waivers(
            tmp_path, [_entry("path_ampacity|Path1|VBUS|", "ev3:0123456789abcdef")]
        )

        report = migrate_evidence_waivers(waivers, current, EvidenceContext(pcb, recipe="ev3"))

        assert [o.key for o in report.orphan] == ["path_ampacity|Path1|VBUS|"]
        (orphan,) = report.orphan
        assert orphan.candidates == (
            "path_ampacity|Path1|VBUS|B.Cu",
            "path_ampacity|Path1|VBUS|F.Cu",
        )

    def test_legacy_version_entries_are_listed_by_key_not_counted(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        waivers = _write_waivers(
            tmp_path, [_entry("courtyard_missing|U9||F.Cu", "ev2:0123456789abcdef")]
        )
        before = waivers.read_text()

        code, out = _migrate(capsys, board, waivers, "--write")

        assert code == 0
        assert "legacy (left alone): 1" in out
        assert "untouched: 0" in out
        assert "legacy (left alone): courtyard_missing|U9||F.Cu  (ev2:0123456789abcdef)" in out
        assert waivers.read_text() == before

    def test_dry_run_changes_no_file_and_prints_the_same_report(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        waivers = _write_waivers(tmp_path, [_entry(COURTYARD_R1, _ev3_hash(board, COURTYARD_R1))])
        before = waivers.read_text()

        code, preview = _migrate(capsys, board, waivers)
        assert code == 0
        assert waivers.read_text() == before
        assert "dry-run" in preview and "would migrate: 1" in preview

        code, applied = _migrate(capsys, board, waivers, "--write")
        assert code == 0 and waivers.read_text() != before
        assert "migrated: 1" in applied

    def test_ev4_and_legacy_entries_are_untouched_and_mixed_file_works(self, tmp_path):
        board = _write_board(tmp_path)
        findings, pcb = _findings(board)
        (r2,) = [f for f in findings if f.key == COURTYARD_R2]
        legacy = {
            "rule": "courtyard_missing",
            "items": ["R9"],
            "reason": "legacy",
            "issue": "p#1",
        }
        waivers = _write_waivers(
            tmp_path,
            [
                _entry(COURTYARD_R1, _ev3_hash(board, COURTYARD_R1)),
                _entry(COURTYARD_R2, r2.evidence_hash),
                legacy,
            ],
        )

        report = migrate_evidence_waivers(
            waivers, findings, EvidenceContext(pcb, recipe="ev3"), write=True
        )

        assert len(report.migrated) == 1
        assert report.untouched == 2
        data = json.loads(waivers.read_text())["waivers"]
        assert data[1] == _entry(COURTYARD_R2, r2.evidence_hash)
        assert data[2] == legacy

    def test_key_multiplicity_greater_than_one_is_part_of_the_recompute(self, tmp_path):
        board = _write_board(tmp_path)
        findings, pcb = _findings(board)
        (base,) = [f for f in findings if f.key == COURTYARD_R1]
        twin = DRCViolation(**{**base.__dict__, "location": (1.0, 2.0), "evidence_hash": ""})
        ctx3 = EvidenceContext(pcb, recipe="ev3")
        ctx4 = EvidenceContext(pcb)
        both = [base, twin]
        annotated = [
            DRCViolation(
                **{**v.__dict__, "evidence_hash": compute_evidence_hash(v, ctx4, multiplicity=2)}
            )
            for v in both
        ]
        recorded = compute_evidence_hash(twin, ctx3, multiplicity=2)
        waivers = _write_waivers(tmp_path, [_entry(COURTYARD_R1, recorded)])

        report = migrate_evidence_waivers(waivers, annotated, ctx3, write=True)

        assert len(report.migrated) == 1
        assert report.migrated[0].new_hash == annotated[1].evidence_hash

    def test_invalid_file_is_never_clobbered(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        bad = tmp_path / "board.kct-waivers.json"
        bad.write_text('{"version": 3, "waivers": [{"key": "x|||", "reason": "r"}]}')
        before = bad.read_text()

        code = check_cmd.main([str(board), "--waivers", str(bad), "--migrate-waivers", "--write"])

        assert code == 1
        assert bad.read_text() == before


# ---------------------------------------------------------------------------
# The #9431 case, plain check, CLI contract
# ---------------------------------------------------------------------------


class TestOutlineEditAfterMigration:
    def test_outline_only_edit_leaves_migrated_waivers_valid(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        keys = [COURTYARD_R1, COURTYARD_R2]
        waivers = _write_waivers(tmp_path, [_entry(k, _ev3_hash(board, k)) for k in keys])
        _migrate(capsys, board, waivers, "--write")

        # Move only the Edge.Cuts top-left corner; no copper moves.
        _write_board(tmp_path, ox=20.0, oy=25.0)
        after = _check_json(capsys, str(board), "--waivers", str(waivers))

        assert after["summary"]["waived"] == len(keys)
        assert after["summary"]["stale_waivers"] == 0


class TestNeverImplicit:
    def test_plain_check_never_rewrites_the_waivers_file(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        waivers = _write_waivers(tmp_path, [_entry(COURTYARD_R1, _ev3_hash(board, COURTYARD_R1))])
        before = waivers.read_text()

        data = _check_json(capsys, str(board), "--waivers", str(waivers))

        assert waivers.read_text() == before
        assert data["summary"]["stale_waivers"] == 1

    def test_stale_advisory_for_ev3_names_the_command(self, tmp_path):
        board = _write_board(tmp_path)
        findings, _ = _findings(board)
        (f,) = [x for x in findings if x.key == COURTYARD_R1]
        waivers = waivers_from_dict(
            {"version": 3, "waivers": [_entry(COURTYARD_R1, _ev3_hash(board, COURTYARD_R1))]}
        )
        from kicad_tools.validate import DRCResults

        results = DRCResults(violations=[f])
        apply_waivers(results, waivers)

        (stale,) = [v for v in results.violations if v.rule_id == WAIVER_STALE_RULE_ID]
        assert "kct check BOARD --migrate-waivers" in stale.message

    def test_write_requires_migrate_waivers(self, tmp_path):
        board = _write_board(tmp_path)
        with pytest.raises(SystemExit):
            check_cmd.main([str(board), "--write"])

    def test_migrate_needs_an_existing_waivers_file(self, tmp_path, capsys):
        board = _write_board(tmp_path)
        code = check_cmd.main([str(board), "--migrate-waivers"])
        assert code == 1
        assert "no waivers file" in capsys.readouterr().err


def test_outer_kct_check_parser_accepts_and_forwards_migrate_flags(monkeypatch):
    """The flags must survive the outer ``kct check`` parser + shim, not just check_cmd."""
    from kicad_tools.cli.commands import validation
    from kicad_tools.cli.parser import create_parser

    args = create_parser().parse_args(["check", "board.kicad_pcb", "--migrate-waivers", "--write"])
    assert args.migrate_waivers is True
    assert args.migrate_write is True

    seen: list[str] = []
    monkeypatch.setattr(check_cmd, "main", lambda argv: seen.extend(argv) or 0)
    assert validation.run_check_command(args) == 0
    assert "--migrate-waivers" in seen
    assert "--write" in seen
