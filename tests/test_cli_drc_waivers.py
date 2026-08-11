"""End-to-end CLI tests for ``kct drc --waivers`` (Issue #4691).

Applies the schema-v2 ``.kct_waivers.json`` sidecar -- the same file, loader and
matching semantics ``kct check --waivers`` uses (Issue #4417) -- to the
``kicad-cli pcb drc`` cross-gate.  Everything here runs off *recorded* report
fixtures written to ``tmp_path``; no live ``kicad-cli`` invocation is needed.
"""

from __future__ import annotations

import json

from kicad_tools.cli import drc_cmd
from kicad_tools.drc.report import DRCReport
from kicad_tools.drc.violation import extract_item_refs

# A recorded ``kicad-cli pcb drc --format json`` report: one courtyard-overlap
# error between two whole footprints ("Footprint C52"-style items) and one
# clearance error between a pad and a track ("... of U3"-style items).
RECORDED_REPORT: dict = {
    "$schema": "https://schemas.kicad.org/drc.v1.json",
    "source": "board.kicad_pcb",
    "date": "2026-08-07T12:00:00+00:00",
    "kicad_version": "10.0.5",
    "coordinate_units": "mm",
    "violations": [
        {
            "type": "courtyards_overlap",
            "severity": "error",
            "description": "Courtyards overlap",
            "items": [
                {
                    "description": "Footprint C52",
                    "uuid": "8cdb0060-0000-0000-0000-000000000001",
                    "pos": {"x": 132.05, "y": 89.95},
                },
                {
                    "description": "Footprint U10",
                    "uuid": "d163e30f-0000-0000-0000-000000000002",
                    "pos": {"x": 131.0, "y": 92.0},
                },
            ],
        },
        {
            "type": "clearance",
            "severity": "error",
            "description": (
                "Clearance violation (netclass 'Default' clearance 0.2000 mm; actual 0.1000 mm)"
            ),
            "items": [
                {
                    "description": "Pad 6 [VBUS] of U3 on F.Cu",
                    "uuid": "aaaaaaaa-0000-0000-0000-000000000003",
                    "pos": {"x": 100.0, "y": 100.0},
                },
                {
                    "description": "Track [GND] on F.Cu",
                    "uuid": "bbbbbbbb-0000-0000-0000-000000000004",
                    "pos": {"x": 100.5, "y": 100.5},
                },
            ],
        },
    ],
    "unconnected_items": [],
    "schematic_parity": [],
}

COURTYARD_WAIVER = {
    "rule": "courtyards_overlap",
    "items": ["C52", "U10"],
    "reason": "EE-mandated tight decoupling, <=2mm from U10 VCC",
    "issue": "chorus#18",
}

CLEARANCE_WAIVER = {
    "rule": "clearance",
    "items": ["U3"],
    "reason": "documented star-ground tie",
    "issue": "chorus#20",
}


def _report(tmp_path, name="board-drc.json", data=None):
    path = tmp_path / name
    path.write_text(json.dumps(data if data is not None else RECORDED_REPORT))
    return path


def _waivers(path, entries):
    path.write_text(json.dumps({"version": 2, "waivers": entries}))
    return path


class TestItemNormalization:
    """The kicad-cli parser keeps descriptions, not uuids -- normalize them."""

    def test_footprint_style_description(self):
        assert extract_item_refs(["Footprint C52", "Footprint U10"]) == {"C52", "U10"}

    def test_pad_of_style_description(self):
        assert extract_item_refs(["Pad 6 [<no net>] of U3 on F.Cu"]) == {"U3"}

    def test_mixed_and_refless_items(self):
        refs = extract_item_refs(
            ["Footprint C52", "Pad 6 [VBUS] of U3 on F.Cu", "Track [GND] on F.Cu"]
        )
        assert refs == {"C52", "U3"}

    def test_refless_items_only(self):
        # Track/via-only findings carry no refs -- waivable via `nets` instead.
        assert extract_item_refs(["Track [GND] on F.Cu", "Via [VBUS] on F.Cu - B.Cu"]) == set()


class TestExitGate:
    def test_all_errors_waived_exits_zero(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        rc = drc_cmd.main([str(report), "--waivers", str(waiver)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Errors:     0" in out
        assert "Waived:     2" in out
        assert "WAIVED (documented exceptions, non-blocking):" in out
        assert "DRC PASSED - No unwaived violations" in out

    def test_unwaived_error_still_exits_nonzero(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER])
        rc = drc_cmd.main([str(report), "--waivers", str(waiver)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "Errors:     1" in out
        assert "Waived:     1" in out
        assert "DRC FAILED" in out

    def test_waived_never_counted_as_warning(self, tmp_path, capsys):
        """Issue #4696: a waived error must not inflate the warning bucket."""
        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        # --strict exits 2 on warnings; waived findings must not trip it.
        rc = drc_cmd.main([str(report), "--waivers", str(waiver), "--strict"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Warnings:   0" in out

    def test_no_flag_no_sidecar_unchanged(self, tmp_path, capsys):
        report = _report(tmp_path)
        rc = drc_cmd.main([str(report)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "Errors:     2" in out
        assert "Waived:" not in out
        assert "WAIVED" not in out


class TestMatchingSemantics:
    def test_exact_set_not_subset(self, tmp_path, capsys):
        """A 1-item waiver does NOT waive a 2-item finding."""
        report = _report(tmp_path)
        waiver = _waivers(
            tmp_path / "w.json",
            [{**COURTYARD_WAIVER, "items": ["C52"]}],
        )
        rc = drc_cmd.main([str(report), "--waivers", str(waiver)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "Errors:     2" in out
        assert "Waived:" not in out

    def test_order_insensitive(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [{**COURTYARD_WAIVER, "items": ["U10", "C52"]}])
        rc = drc_cmd.main([str(report), "--waivers", str(waiver), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 1  # the clearance error is still unwaived
        assert data["summary"]["waived"] == 1

    def test_nets_only_waiver(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(
            tmp_path / "w.json",
            [
                {
                    "rule": "clearance",
                    "nets": ["VBUS", "GND"],
                    "reason": "documented star-ground tie",
                    "issue": "chorus#20",
                }
            ],
        )
        rc = drc_cmd.main([str(report), "--waivers", str(waiver), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 1  # courtyard overlap remains
        assert data["summary"]["waived"] == 1
        waived = [v for v in data["violations"] if v.get("waived")]
        assert waived[0]["type_str"] == "clearance"

    def test_wrong_rule_id_does_not_match(self, tmp_path, capsys):
        report = _report(tmp_path)
        # kct's own rule id for the same family -- keyed to the other engine.
        waiver = _waivers(
            tmp_path / "w.json",
            [{**CLEARANCE_WAIVER, "rule": "clearance_pad_pad"}],
        )
        rc = drc_cmd.main([str(report), "--waivers", str(waiver)])
        out = capsys.readouterr().out
        assert rc == 1
        assert "Waived:" not in out


class TestJsonOutput:
    def test_status_waived_keeps_severity(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        rc = drc_cmd.main([str(report), "--waivers", str(waiver), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert data["summary"] == {"errors": 0, "warnings": 0, "waived": 2}
        for v in data["violations"]:
            assert v["waived"] is True
            assert v["status"] == "waived"
            # Manufacturing-gate safety: the underlying severity is preserved
            # so the severity-keyed kct audit gate stays blocking.
            assert v["severity"] == "error"
        courtyard = next(v for v in data["violations"] if v["type_str"] == "courtyards_overlap")
        assert courtyard["waiver_reason"] == COURTYARD_WAIVER["reason"]
        assert courtyard["waiver_issue"] == "chorus#18"

    def test_json_unchanged_without_waivers(self, tmp_path, capsys):
        report = _report(tmp_path)
        drc_cmd.main([str(report), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert data["summary"] == {"errors": 2, "warnings": 0}
        for v in data["violations"]:
            assert "status" not in v
            assert "waived" not in v


class TestUnusedWaivers:
    def test_unused_entry_is_visible_and_non_gating(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(
            tmp_path / "w.json",
            [
                COURTYARD_WAIVER,
                CLEARANCE_WAIVER,
                {
                    "rule": "courtyards_overlap",
                    "items": ["X9", "Y9"],
                    "reason": "stale entry",
                    "issue": "chorus#99",
                },
            ],
        )
        rc = drc_cmd.main([str(report), "--waivers", str(waiver)])
        out = capsys.readouterr().out
        assert rc == 0  # advisory only -- never gates
        assert "UNUSED WAIVERS (advisory, non-blocking):" in out
        assert "chorus#99" in out

    def test_kct_only_rule_id_reads_unused(self, tmp_path, capsys):
        """An entry keyed to kct's rule vocabulary is advisory, not an error."""
        report = _report(tmp_path)
        waiver = _waivers(
            tmp_path / "w.json",
            [
                COURTYARD_WAIVER,
                CLEARANCE_WAIVER,
                {
                    "rule": "clearance_pad_pad",
                    "items": ["U3", "C52"],
                    "reason": "kct-engine entry in the shared sidecar",
                    "issue": "chorus#21",
                },
            ],
        )
        rc = drc_cmd.main([str(report), "--waivers", str(waiver), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        unused = data["unused_waivers"]
        assert len(unused) == 1
        assert unused[0]["rule"] == "clearance_pad_pad"
        assert unused[0]["issue"] == "chorus#21"

    def test_no_unused_key_when_all_used(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        drc_cmd.main([str(report), "--waivers", str(waiver), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert "unused_waivers" not in data


class TestSidecarContract:
    def test_explicit_missing_path_hard_error(self, tmp_path, capsys):
        report = _report(tmp_path)
        rc = drc_cmd.main([str(report), "--waivers", str(tmp_path / "nope.json")])
        assert rc == 1
        assert "waivers file not found" in capsys.readouterr().err

    def test_explicit_malformed_hard_error(self, tmp_path, capsys):
        report = _report(tmp_path)
        bad = tmp_path / "bad.json"
        bad.write_text("{not json")
        rc = drc_cmd.main([str(report), "--waivers", str(bad)])
        assert rc == 1
        assert "Error:" in capsys.readouterr().err

    def test_explicit_wrong_version_hard_error(self, tmp_path, capsys):
        report = _report(tmp_path)
        bad = tmp_path / "v1.json"
        bad.write_text(json.dumps({"version": 1, "waivers": []}))
        rc = drc_cmd.main([str(report), "--waivers", str(bad)])
        assert rc == 1
        assert "unsupported waivers version" in capsys.readouterr().err

    def test_auto_discovered_sidecar_applies(self, tmp_path, capsys):
        report = _report(tmp_path)
        _waivers(tmp_path / ".kct_waivers.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        rc = drc_cmd.main([str(report)])
        captured = capsys.readouterr()
        assert rc == 0
        assert "auto-loaded waivers sidecar" in captured.err
        assert "Waived:     2" in captured.out

    def test_auto_discovered_output_subdir(self, tmp_path, capsys):
        (tmp_path / "output").mkdir()
        report = _report(tmp_path)
        _waivers(tmp_path / "output" / ".kct_waivers.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        rc = drc_cmd.main([str(report)])
        assert rc == 0
        assert "Waived:     2" in capsys.readouterr().out

    def test_auto_discovered_malformed_degrades(self, tmp_path, capsys):
        report = _report(tmp_path)
        (tmp_path / ".kct_waivers.json").write_text("{not json")
        rc = drc_cmd.main([str(report)])
        captured = capsys.readouterr()
        assert rc == 1
        assert "ignoring malformed waivers sidecar" in captured.err
        assert "Errors:     2" in captured.out


class TestFilterInteractions:
    def test_errors_only_excludes_waived(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER])
        rc = drc_cmd.main([str(report), "--waivers", str(waiver), "--errors-only"])
        out = capsys.readouterr().out
        assert rc == 1
        assert "courtyards_overlap" not in out
        assert "clearance" in out

    def test_summary_format_has_waived_column(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        rc = drc_cmd.main([str(report), "--waivers", str(waiver), "--format", "summary"])
        out = capsys.readouterr().out
        assert rc == 0
        header = next(line for line in out.splitlines() if line.startswith("Type"))
        assert "Waived" in header
        total = next(line for line in out.splitlines() if line.startswith("TOTAL"))
        assert total.split() == ["TOTAL", "0", "0", "2"]

    def test_summary_format_unchanged_without_waivers(self, tmp_path, capsys):
        report = _report(tmp_path)
        rc = drc_cmd.main([str(report), "--format", "summary"])
        out = capsys.readouterr().out
        assert rc == 1
        header = next(line for line in out.splitlines() if line.startswith("Type"))
        assert "Waived" not in header

    def test_mfr_mode_does_not_apply_waivers(self, tmp_path, capsys):
        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        drc_cmd.main([str(report), "--waivers", str(waiver), "--mfr", "jlcpcb"])
        assert "does not apply waivers" in capsys.readouterr().err


class TestTextReportInput:
    """The .rpt text-report path shares the same normalization."""

    RPT = """** Drc report for board.kicad_pcb **
** Created on 2026-08-07T12:00:00+0000 **

** Found 1 DRC violations **
[courtyards_overlap]: Courtyards overlap
    Rule: courtyard_overlap; error
    @(132.0500 mm, 89.9500 mm): Footprint C52
    @(131.0000 mm, 92.0000 mm): Footprint U10

** Found 0 Footprint errors **
** End of Report **
"""

    def test_rpt_input_waived(self, tmp_path, capsys):
        rpt = tmp_path / "board-drc.rpt"
        rpt.write_text(self.RPT)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER])
        rc = drc_cmd.main([str(rpt), "--waivers", str(waiver)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Waived:     1" in out


class TestOuterParserParity:
    """Issue #3159 drift trap: the flag must reach the inner parser."""

    def test_kct_drc_waivers_reaches_inner_command(self, tmp_path, capsys):
        from kicad_tools.cli import main as cli_main

        report = _report(tmp_path)
        waiver = _waivers(tmp_path / "w.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        rc = cli_main(["drc", str(report), "--waivers", str(waiver)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "Waived:     2" in out

    def test_outer_parser_exposes_waivers(self):
        from kicad_tools.cli.parser import create_parser

        args = create_parser().parse_args(["drc", "report.json", "--waivers", "w.json"])
        assert args.waivers == "w.json"


class TestReportModelUnaffected:
    def test_severity_keyed_consumers_still_see_error(self, tmp_path):
        """A waived finding keeps severity=error for the audit gate."""
        from kicad_tools.drc.waivers import apply_waivers_to_report
        from kicad_tools.validate.rules.waivers import waivers_from_dict

        report_path = _report(tmp_path)
        report = DRCReport.load(report_path)
        waivers = waivers_from_dict({"version": 2, "waivers": [COURTYARD_WAIVER]})
        result = apply_waivers_to_report(report, waivers)

        assert result.waived_count == 1
        waived = result.waived[0]
        assert waived.severity.value == "error"
        assert waived.is_error is False
        assert waived.is_waived is True
        # Re-applying is idempotent (already-waived findings are skipped).
        again = apply_waivers_to_report(report, waivers)
        assert again.waived_count == 0
        assert len(again.unused) == 1


# Two clearance findings that involve the SAME footprint ref (U3) against a
# ref-less track item, on different nets.  ``extract_item_refs`` drops the
# track, so both normalize to the identical ref set {"U3"}.
_TWO_MIXED_FINDINGS: dict = {
    **RECORDED_REPORT,
    "violations": [
        {
            "type": "clearance",
            "severity": "error",
            "description": "Clearance violation (GND)",
            "items": [
                {"description": "Pad 1 [VBUS] of U3 on F.Cu", "uuid": "1", "pos": {}},
                {"description": "Track [GND] on F.Cu", "uuid": "2", "pos": {}},
            ],
        },
        {
            "type": "clearance",
            "severity": "error",
            "description": "Clearance violation (SCL)",
            "items": [
                {"description": "Pad 2 [SDA] of U3 on F.Cu", "uuid": "3", "pos": {}},
                {"description": "Track [SCL] on F.Cu", "uuid": "4", "pos": {}},
            ],
        },
    ],
}


class TestPartiallyReflessFindings:
    """Issue #4765: "exact-set" is exact over the NORMALIZED ref set.

    A track/via item contributes no reference designator, so a finding that
    mixes a pad with a track normalizes to a SMALLER set than its item count
    suggests and is matched by a correspondingly small waiver.  That is
    deliberate -- narrowing it would retroactively un-waive existing
    sidecars -- so these tests pin the behaviour and demonstrate the ``nets``
    remedy that keeps such an entry specific.
    """

    def test_refless_item_is_dropped_from_the_normalized_set(self):
        assert extract_item_refs(["Pad 1 of U3", "Track [GND] on F.Cu"]) == {"U3"}

    def test_one_ref_waiver_matches_the_mixed_finding(self, tmp_path, capsys):
        report = _report(tmp_path, data=_TWO_MIXED_FINDINGS)
        waiver = _waivers(
            tmp_path / "w.json",
            [{"rule": "clearance", "items": ["U3"], "reason": "reviewed", "issue": "#4765"}],
        )
        rc = drc_cmd.main([str(report), "--waivers", str(waiver), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 0
        # Documented consequence: the entry waives the whole CLASS of U3
        # clearance findings, not only the one that was reviewed.
        assert data["summary"]["waived"] == 2

    def test_adding_nets_narrows_the_same_waiver_to_one_finding(self, tmp_path, capsys):
        report = _report(tmp_path, data=_TWO_MIXED_FINDINGS)
        waiver = _waivers(
            tmp_path / "w.json",
            [
                {
                    "rule": "clearance",
                    "items": ["U3"],
                    "nets": ["VBUS", "GND"],
                    "reason": "reviewed",
                    "issue": "#4765",
                }
            ],
        )
        rc = drc_cmd.main([str(report), "--waivers", str(waiver), "--format", "json"])
        data = json.loads(capsys.readouterr().out)
        assert rc == 1  # the SCL/SDA finding is NOT waived
        assert data["summary"]["waived"] == 1
        waived = [v for v in data["violations"] if v.get("waived")]
        assert waived[0]["nets"] == ["VBUS", "GND"]


class TestReportInputDiscoversBoardDir:
    """Issue #4765: ``boards/NN/output/drc.json`` must find ``boards/NN/``.

    ``discover_waivers_sidecar`` is anchored at the input path, which for
    report input is the report -- so its three probes collapse to the report
    directory and ``<report dir>/output/``, never the board directory where
    the sidecar lives for the ``.kicad_pcb`` path.
    """

    def _board_layout(self, tmp_path):
        board_dir = tmp_path / "boards" / "07"
        (board_dir / "output").mkdir(parents=True)
        report = _report(board_dir / "output", data=RECORDED_REPORT)
        return board_dir, report

    def test_board_dir_sidecar_is_discovered_for_report_input(self, tmp_path, capsys):
        board_dir, report = self._board_layout(tmp_path)
        sidecar = _waivers(board_dir / ".kct_waivers.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        rc = drc_cmd.main([str(report)])
        captured = capsys.readouterr()
        assert rc == 0
        assert f"auto-loaded waivers sidecar: {sidecar}" in captured.err
        assert "Waived:     2" in captured.out

    def test_report_dir_sidecar_still_wins_over_the_board_dir(self, tmp_path, capsys):
        board_dir, report = self._board_layout(tmp_path)
        _waivers(board_dir / ".kct_waivers.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        nearer = _waivers(board_dir / "output" / ".kct_waivers.json", [COURTYARD_WAIVER])
        rc = drc_cmd.main([str(report)])
        captured = capsys.readouterr()
        assert rc == 1  # only the courtyard finding is waived by the nearer file
        assert f"auto-loaded waivers sidecar: {nearer}" in captured.err

    def test_explicit_path_still_wins(self, tmp_path, capsys):
        board_dir, report = self._board_layout(tmp_path)
        _waivers(board_dir / ".kct_waivers.json", [COURTYARD_WAIVER, CLEARANCE_WAIVER])
        explicit = _waivers(tmp_path / "explicit.json", [COURTYARD_WAIVER])
        rc = drc_cmd.main([str(report), "--waivers", str(explicit)])
        captured = capsys.readouterr()
        assert rc == 1
        assert "auto-loaded waivers sidecar" not in captured.err
        assert "Waived:     1" in captured.out

    def test_missing_explicit_path_is_still_a_hard_error(self, tmp_path, capsys):
        board_dir, report = self._board_layout(tmp_path)
        _waivers(board_dir / ".kct_waivers.json", [COURTYARD_WAIVER])
        rc = drc_cmd.main([str(report), "--waivers", str(tmp_path / "nope.json")])
        assert rc == 1
        assert "waivers file not found" in capsys.readouterr().err

    def test_no_sidecar_anywhere_is_unchanged(self, tmp_path, capsys):
        _board_dir, report = self._board_layout(tmp_path)
        rc = drc_cmd.main([str(report)])
        captured = capsys.readouterr()
        assert rc == 1
        assert "auto-loaded waivers sidecar" not in captured.err
        assert "Errors:     2" in captured.out
