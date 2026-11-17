"""Tests for the sch fix-annotation command.

Covers hierarchy-aware power/flag-symbol annotation repair:

- Net-name-styled power refs (#GNDD, #+3.3V) get canonical #PWR0xx designators.
- Duplicate power refs across sheets get distinct, non-conflicting numbers.
- Inconsistent zero-padding (#PWR40 vs #PWR040) is normalized.
- Missing (instances) blocks are created with the correct project + path.
- The net-neutrality gate detects membership changes (mocked at the
  netlist boundary, decoupled from kicad-cli availability).
- --dry-run makes no changes; --backup creates .bak files.
- Real component refs (R1, C3) are left untouched.

Fixtures are synthetic multi-sheet schematics built in this file — the
chorus design referenced by the issue is local-only and unavailable in CI.
"""

from pathlib import Path

import pytest

from kicad_tools.cli.export_netlist import Net, Netlist, NetNode
from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.cli.sch_fix_annotation import (
    _extract_all_symbols,
    _is_flag_symbol,
    _is_power_symbol,
    _needs_reassignment,
    build_rename_plan,
    diff_net_membership,
    run_fix_annotation,
)

# ---------------------------------------------------------------------------
# Synthetic hierarchical fixtures
# ---------------------------------------------------------------------------

ROOT_UUID = "aaaaaaaa-aaaa-aaaa-aaaa-aaaaaaaaaaaa"
SUB_SHEET_UUID = "bbbbbbbb-bbbb-bbbb-bbbb-bbbbbbbbbbbb"


def _root_schematic(*symbol_blocks: str) -> str:
    """Build a root schematic containing *symbol_blocks* plus a sub-sheet."""
    body = "\n".join(symbol_blocks)
    return f"""\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "{ROOT_UUID}")
\t(paper "A4")
\t(lib_symbols
\t)
{body}
\t(sheet
\t\t(at 150 50)
\t\t(size 20 15)
\t\t(uuid "{SUB_SHEET_UUID}")
\t\t(property "Sheetname" "sub"
\t\t\t(at 150 49 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Sheetfile" "sub.kicad_sch"
\t\t\t(at 150 65.5 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t)
)
"""


def _sub_schematic(*symbol_blocks: str) -> str:
    body = "\n".join(symbol_blocks)
    return f"""\
(kicad_sch
\t(version 20231120)
\t(generator "test")
\t(generator_version "8.0")
\t(uuid "cccccccc-cccc-cccc-cccc-cccccccccccc")
\t(paper "A4")
\t(lib_symbols
\t)
{body}
)
"""


def _power_symbol_no_instance(lib_id: str, ref: str, uuid: str) -> str:
    """A power symbol with a net-name-styled ref and NO (instances) block."""
    return f"""\
\t(symbol
\t\t(lib_id "{lib_id}")
\t\t(at 100 50 0)
\t\t(property "Reference" "{ref}"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "{lib_id.split(":", 1)[1]}"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-{uuid}")
\t\t)
\t\t(uuid "{uuid}")
\t)"""


def _power_symbol_with_instance(lib_id: str, ref: str, uuid: str, project: str, path: str) -> str:
    """A power symbol with a correct (instances) block for *project*."""
    return f"""\
\t(symbol
\t\t(lib_id "{lib_id}")
\t\t(at 100 50 0)
\t\t(property "Reference" "{ref}"
\t\t\t(at 100 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "{lib_id.split(":", 1)[1]}"
\t\t\t(at 100 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-{uuid}")
\t\t)
\t\t(uuid "{uuid}")
\t\t(instances
\t\t\t(project "{project}"
\t\t\t\t(path "{path}"
\t\t\t\t\t(reference "{ref}")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)"""


def _real_component(lib_id: str, ref: str, value: str, uuid: str) -> str:
    return f"""\
\t(symbol
\t\t(lib_id "{lib_id}")
\t\t(at 120 50 0)
\t\t(property "Reference" "{ref}"
\t\t\t(at 120 48 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(property "Value" "{value}"
\t\t\t(at 120 52 0)
\t\t\t(effects (font (size 1.27 1.27)))
\t\t)
\t\t(pin "1"
\t\t\t(uuid "pin-a-{uuid}")
\t\t)
\t\t(pin "2"
\t\t\t(uuid "pin-b-{uuid}")
\t\t)
\t\t(uuid "{uuid}")
\t\t(instances
\t\t\t(project "root"
\t\t\t\t(path "/{ROOT_UUID}"
\t\t\t\t\t(reference "{ref}")
\t\t\t\t\t(unit 1)
\t\t\t\t)
\t\t\t)
\t\t)
\t)"""


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------


class TestClassification:
    def test_is_power_symbol(self):
        assert _is_power_symbol({"lib_id": "power:GND"})
        assert _is_power_symbol({"lib_id": "power:+3.3V"})
        assert not _is_power_symbol({"lib_id": "Device:R"})

    def test_is_flag_symbol(self):
        assert _is_flag_symbol({"lib_id": "power:PWR_FLAG"})
        assert not _is_flag_symbol({"lib_id": "power:GND"})

    def test_net_name_styled_needs_reassignment(self):
        assert _needs_reassignment({"lib_id": "power:GNDD", "reference": "#GNDD"})
        assert _needs_reassignment({"lib_id": "power:+3.3V", "reference": "#+3.3V"})

    def test_unpadded_needs_reassignment(self):
        # Single-digit / unpadded numbers are not canonical (KiCad zero-pads).
        assert _needs_reassignment({"lib_id": "power:GND", "reference": "#PWR2"})

    def test_canonical_ref_ok(self):
        assert not _needs_reassignment({"lib_id": "power:GND", "reference": "#PWR01"})
        assert not _needs_reassignment({"lib_id": "power:PWR_FLAG", "reference": "#FLG03"})

    def test_family_mismatch_needs_reassignment(self):
        # A flag symbol carrying a #PWR ref is wrong; a ground symbol carrying
        # a #FLG ref is wrong.
        assert _needs_reassignment({"lib_id": "power:PWR_FLAG", "reference": "#PWR01"})
        assert _needs_reassignment({"lib_id": "power:GND", "reference": "#FLG01"})


# ---------------------------------------------------------------------------
# Symbol extraction (all symbols, power not skipped)
# ---------------------------------------------------------------------------


class TestExtractAllSymbols:
    def test_extracts_power_symbol_without_instances(self):
        text = _sub_schematic(
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-1"),
        )
        syms = _extract_all_symbols(text, "root")
        assert len(syms) == 1
        assert syms[0]["reference"] == "#GNDD"
        assert syms[0]["lib_id"] == "power:GND"
        assert syms[0]["has_project_instance"] is False

    def test_detects_correct_instance(self):
        text = _sub_schematic(
            _power_symbol_with_instance("power:GND", "#PWR01", "u-gnd-2", "root", f"/{ROOT_UUID}"),
        )
        syms = _extract_all_symbols(text, "root")
        assert len(syms) == 1
        assert syms[0]["has_project_instance"] is True

    def test_detects_wrong_project(self):
        text = _sub_schematic(
            _power_symbol_with_instance("power:GND", "#PWR01", "u-gnd-3", "other", f"/{ROOT_UUID}"),
        )
        syms = _extract_all_symbols(text, "root")
        assert syms[0]["has_project_instance"] is False
        assert syms[0]["has_wrong_project"] is True


# ---------------------------------------------------------------------------
# Rename plan
# ---------------------------------------------------------------------------


class TestBuildRenamePlan:
    def _power(self, lib_id, ref, uuid):
        return {"lib_id": lib_id, "reference": ref, "uuid": uuid}

    def test_net_name_styled_gets_canonical(self):
        plan = build_rename_plan(
            [self._power("power:GND", "#GNDD", "u1")],
        )
        assert plan["u1"]["new"] == "#PWR01"

    def test_duplicate_refs_get_distinct_numbers(self):
        plan = build_rename_plan(
            [
                self._power("power:GND", "#GNDD", "u1"),
                self._power("power:GND", "#GNDD", "u2"),
            ],
        )
        assert plan["u1"]["new"] == "#PWR01"
        assert plan["u2"]["new"] == "#PWR02"
        assert plan["u1"]["new"] != plan["u2"]["new"]

    def test_canonical_numbers_reserved(self):
        # An existing correct #PWR01 must not collide with a fresh assignment.
        plan = build_rename_plan(
            [
                self._power("power:GND", "#PWR01", "u_ok"),  # canonical, kept
                self._power("power:+3.3V", "#+3.3V", "u_new"),  # needs number
            ],
        )
        assert "u_ok" not in plan  # unchanged
        assert plan["u_new"]["new"] == "#PWR02"  # skipped reserved 01

    def test_flag_symbols_get_flg_prefix(self):
        plan = build_rename_plan(
            [self._power("power:PWR_FLAG", "#FLG", "u1")],
        )
        assert plan["u1"]["new"] == "#FLG01"

    def test_padding_normalization_no_collision(self):
        # #PWR40 round-trips through the 2-digit convention (canonical, kept),
        # but #PWR040 does not (it would format to #PWR40) so it is a
        # duplicate-looking ref that must be normalized to a fresh number
        # without colliding with the reserved 40.
        plan = build_rename_plan(
            [
                self._power("power:GND", "#PWR40", "u1"),
                self._power("power:GND", "#PWR040", "u2"),
            ],
        )
        assert "u1" not in plan  # #PWR40 is canonical, unchanged
        assert plan["u2"]["new"] == "#PWR01"  # #PWR040 normalized, 40 reserved

    def test_cross_sheet_canonical_duplicate_reassigned(self):
        # Two *individually-canonical* #PWR40 symbols on different sheets are a
        # real cross-sheet annotation collision even though each looks fine in
        # isolation.  First-occurrence-wins: the first holder keeps #PWR40; the
        # later duplicate is reassigned to a fresh, non-reserved number.
        plan = build_rename_plan(
            [
                self._power("power:+3V3", "#PWR40", "u1"),
                self._power("power:+5V", "#PWR40", "u2"),
            ],
        )
        assert "u1" not in plan  # first holder keeps #PWR40
        assert plan["u2"]["old"] == "#PWR40"
        assert plan["u2"]["new"] == "#PWR01"  # reassigned, 40 reserved
        assert len(plan) == 1  # exactly one of the duplicates renamed

    def test_three_way_canonical_duplicate_reassigned(self):
        # Three colliding #PWR40: only the first survives; the other two get
        # distinct fresh numbers (no collision among the reassignments).
        plan = build_rename_plan(
            [
                self._power("power:GND", "#PWR40", "u1"),
                self._power("power:GND", "#PWR40", "u2"),
                self._power("power:GND", "#PWR40", "u3"),
            ],
        )
        assert "u1" not in plan
        assert plan["u2"]["new"] == "#PWR01"
        assert plan["u3"]["new"] == "#PWR02"
        assert plan["u2"]["new"] != plan["u3"]["new"]

    def test_real_components_absent(self):
        # Only power symbols are passed to build_rename_plan by the caller;
        # a symbol that is already canonical is omitted from the plan.
        plan = build_rename_plan(
            [self._power("power:GND", "#PWR01", "u1")],
        )
        assert plan == {}


# ---------------------------------------------------------------------------
# Net-neutrality gate comparison logic (mocked at the boundary)
# ---------------------------------------------------------------------------


def _net(name, *nodes):
    return Net(
        code=0,
        name=name,
        nodes=[NetNode(reference=r, pin=p) for r, p in nodes],
    )


class TestDiffNetMembership:
    def test_rename_is_neutral_after_translation(self):
        before = Netlist(nets=[_net("GND", ("#GNDD", "1"), ("R1", "2"))])
        after = Netlist(nets=[_net("GND", ("#PWR01", "1"), ("R1", "2"))])
        ref_rename = {"#GNDD": "#PWR01"}
        diffs = diff_net_membership(before, after, ref_rename)
        assert diffs == []

    def test_dropped_node_detected(self):
        before = Netlist(nets=[_net("GND", ("#GNDD", "1"), ("R1", "2"))])
        # After drops R1.2 — a genuine electrical change.
        after = Netlist(nets=[_net("GND", ("#PWR01", "1"))])
        ref_rename = {"#GNDD": "#PWR01"}
        diffs = diff_net_membership(before, after, ref_rename)
        assert diffs, "expected the gate to flag the dropped node"

    def test_added_node_detected(self):
        before = Netlist(nets=[_net("GND", ("#GNDD", "1"))])
        after = Netlist(nets=[_net("GND", ("#PWR01", "1"), ("R9", "1"))])
        ref_rename = {"#GNDD": "#PWR01"}
        diffs = diff_net_membership(before, after, ref_rename)
        assert diffs

    def test_power_only_rename_neutral_even_without_translation(self):
        # Power/flag nodes (#-prefixed) are dropped from the membership snapshot
        # entirely: they connect by *value*, so their designators are
        # electrically meaningless.  A pure power rename is therefore net-neutral
        # even with an EMPTY rename map — the drop, not the translation, is what
        # makes it neutral now.  A power-only net collapses to the empty
        # membership on both sides.
        before = Netlist(nets=[_net("GND", ("#GNDD", "1"))])
        after = Netlist(nets=[_net("GND", ("#PWR01", "1"))])
        assert diff_net_membership(before, after, {}) == []
        assert diff_net_membership(before, after, {"#GNDD": "#PWR01"}) == []

    def test_cross_sheet_duplicate_power_rename_is_neutral(self):
        # Regression for the net-neutrality gate abort on cross-sheet duplicates.
        # Two #PWR40 symbols on DIFFERENT rails (+3.3V vs +5V); the repair
        # renames exactly one to #PWR41.  ref_rename is keyed by the shared old
        # string "#PWR40", so the old membership (which included power nodes)
        # would translate BOTH #PWR40 nodes to #PWR41 -> a spurious diff that
        # aborts the whole repair.  With power nodes dropped, the gate compares
        # only the real-component connectivity (U1.3 / U1.5), which is invariant.
        before = Netlist(
            nets=[
                _net("+3V3", ("#PWR40", "1"), ("U1", "3")),
                _net("+5V", ("#PWR40", "1"), ("U1", "5")),
            ]
        )
        after = Netlist(
            nets=[
                _net("+3V3", ("#PWR40", "1"), ("U1", "3")),
                _net("+5V", ("#PWR41", "1"), ("U1", "5")),
            ]
        )
        ref_rename = {"#PWR40": "#PWR41"}
        assert diff_net_membership(before, after, ref_rename) == []


# ---------------------------------------------------------------------------
# End-to-end run (mocked net gate) — text mutation on disk
# ---------------------------------------------------------------------------


def _write_hierarchy(tmp_path: Path, root_text: str, sub_text: str) -> Path:
    root = tmp_path / "root.kicad_sch"
    root.write_text(root_text, encoding="utf-8")
    (tmp_path / "sub.kicad_sch").write_text(sub_text, encoding="utf-8")
    return root


class TestRunFixAnnotationDryRun:
    def test_dry_run_makes_no_changes(self, tmp_path, capsys):
        root_text = _root_schematic(
            _real_component("Device:R", "R1", "10k", "u-r1"),
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-root"),
        )
        sub_text = _sub_schematic(
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-sub"),
        )
        root = _write_hierarchy(tmp_path, root_text, sub_text)
        before = root.read_text()

        rc = run_fix_annotation(root, dry_run=True, backup=False)
        assert rc == 0
        assert root.read_text() == before  # unchanged
        out = capsys.readouterr().out
        assert "#GNDD -> #PWR01" in out
        assert "Dry run" in out

    def test_dry_run_leaves_real_components_untouched_in_plan(self, tmp_path, capsys):
        root_text = _root_schematic(
            _real_component("Device:R", "R1", "10k", "u-r1"),
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-root"),
        )
        sub_text = _sub_schematic()
        root = _write_hierarchy(tmp_path, root_text, sub_text)
        run_fix_annotation(root, dry_run=True, backup=False)
        out = capsys.readouterr().out
        assert "R1" not in out.replace("#PWR", "")  # R1 never appears as a rename


class TestRunFixAnnotationSkipNetCheck:
    def test_writes_and_renames_power_symbols(self, tmp_path):
        root_text = _root_schematic(
            _real_component("Device:R", "R1", "10k", "u-r1"),
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-root"),
        )
        sub_text = _sub_schematic(
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-sub"),
            _power_symbol_no_instance("power:+3.3V", "#+3.3V", "u-p3v3"),
        )
        root = _write_hierarchy(tmp_path, root_text, sub_text)

        rc = run_fix_annotation(root, backup=False, skip_net_check=True)
        assert rc == 0

        root_after = root.read_text()
        sub_after = (tmp_path / "sub.kicad_sch").read_text()

        # Root power ref renamed and given an instances block.
        assert '(property "Reference" "#PWR01"' in root_after
        assert "#GNDD" not in root_after
        assert f'(path "/{ROOT_UUID}"' in root_after

        # Sub-sheet power refs renamed with distinct numbers.
        assert '(property "Reference" "#PWR02"' in sub_after
        assert '(property "Reference" "#PWR03"' in sub_after
        assert "#GNDD" not in sub_after
        assert "#+3.3V" not in sub_after
        # Sub-sheet instance path is the hierarchy path (root/sheet).
        assert f'(path "/{ROOT_UUID}/{SUB_SHEET_UUID}"' in sub_after

        # Real component reference untouched.
        assert '(property "Reference" "R1"' in root_after

    def test_cross_sheet_canonical_duplicate_renames_exactly_one(self, tmp_path):
        # Two individually-canonical #PWR40 power symbols on different rails,
        # one per sheet.  The old per-symbol canonicality check skipped BOTH
        # (each looked fine in isolation), so the annotation error survived the
        # repair.  Project-wide uniqueness must reassign exactly one: the first
        # holder (root, processed first) keeps #PWR40; the sub-sheet duplicate
        # gets a fresh number.
        root_text = _root_schematic(
            _power_symbol_with_instance(
                "power:+3V3", "#PWR40", "u-root-40", "root", f"/{ROOT_UUID}"
            ),
        )
        sub_text = _sub_schematic(
            _power_symbol_with_instance(
                "power:+5V",
                "#PWR40",
                "u-sub-40",
                "root",
                f"/{ROOT_UUID}/{SUB_SHEET_UUID}",
            ),
        )
        root = _write_hierarchy(tmp_path, root_text, sub_text)

        rc = run_fix_annotation(root, backup=False, skip_net_check=True)
        assert rc == 0

        root_after = root.read_text()
        sub_after = (tmp_path / "sub.kicad_sch").read_text()
        combined = root_after + sub_after

        # Exactly one symbol keeps #PWR40; the duplicate is reassigned to #PWR01.
        assert combined.count('(property "Reference" "#PWR40"') == 1
        assert combined.count('(property "Reference" "#PWR01"') == 1
        # The first holder (root) keeps #PWR40; the sub-sheet duplicate renamed.
        assert '(property "Reference" "#PWR40"' in root_after
        assert '(property "Reference" "#PWR01"' in sub_after
        # The renamed symbol's (instances) block is updated in step — no stale
        # #PWR40 designator left behind for kicad-cli to keep flagging.
        assert '(reference "#PWR01")' in sub_after
        assert '(reference "#PWR40")' not in sub_after

    def test_backup_creates_bak_file(self, tmp_path):
        root_text = _root_schematic(
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-root"),
        )
        sub_text = _sub_schematic()
        root = _write_hierarchy(tmp_path, root_text, sub_text)

        rc = run_fix_annotation(root, backup=True, skip_net_check=True)
        assert rc == 0
        backups = list(tmp_path.glob("root_backup_*.kicad_sch"))
        assert len(backups) == 1

    def test_renames_symbol_with_existing_instance(self, tmp_path):
        # A power symbol with a well-formed instance but a net-name-styled ref
        # gets renamed AND its instance (reference "...") is updated in step.
        root_text = _root_schematic(
            _power_symbol_with_instance(
                "power:GND", "#GNDD", "u-gnd-root", "root", f"/{ROOT_UUID}"
            ),
        )
        sub_text = _sub_schematic()
        root = _write_hierarchy(tmp_path, root_text, sub_text)

        rc = run_fix_annotation(root, backup=False, skip_net_check=True)
        assert rc == 0
        after = root.read_text()
        assert '(property "Reference" "#PWR01"' in after
        assert '(reference "#PWR01")' in after
        assert "#GNDD" not in after

    def test_nothing_to_fix_reports_clean(self, tmp_path, capsys):
        root_text = _root_schematic(
            _power_symbol_with_instance(
                "power:GND", "#PWR01", "u-gnd-root", "root", f"/{ROOT_UUID}"
            ),
        )
        sub_text = _sub_schematic()
        root = _write_hierarchy(tmp_path, root_text, sub_text)

        rc = run_fix_annotation(root, backup=False, skip_net_check=True)
        assert rc == 0
        assert "No power/flag annotation errors found." in capsys.readouterr().out


class TestNetGateIntegration:
    """Exercise run_fix_annotation with a mocked net gate (no kicad-cli)."""

    def test_gate_failure_aborts_write(self, tmp_path, monkeypatch):
        root_text = _root_schematic(
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-root"),
        )
        sub_text = _sub_schematic()
        root = _write_hierarchy(tmp_path, root_text, sub_text)
        before = root.read_text()

        import kicad_tools.cli.sch_fix_annotation as mod

        monkeypatch.setattr(mod, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
        # Simulate the gate detecting a net change.
        monkeypatch.setattr(
            mod,
            "_run_net_gate",
            lambda *a, **k: (False, ["  REMOVED (x1): {R1.2}"]),
        )

        rc = run_fix_annotation(root, backup=False)
        assert rc == 2
        assert root.read_text() == before  # no write on gate failure

    def test_gate_pass_allows_write(self, tmp_path, monkeypatch):
        root_text = _root_schematic(
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-root"),
        )
        sub_text = _sub_schematic()
        root = _write_hierarchy(tmp_path, root_text, sub_text)

        import kicad_tools.cli.sch_fix_annotation as mod

        monkeypatch.setattr(mod, "find_kicad_cli", lambda: Path("/fake/kicad-cli"))
        monkeypatch.setattr(mod, "_run_net_gate", lambda *a, **k: (True, []))

        rc = run_fix_annotation(root, backup=False)
        assert rc == 0
        assert '(property "Reference" "#PWR01"' in root.read_text()

    def test_missing_kicad_cli_errors_without_skip(self, tmp_path, monkeypatch):
        root_text = _root_schematic(
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-root"),
        )
        sub_text = _sub_schematic()
        root = _write_hierarchy(tmp_path, root_text, sub_text)
        before = root.read_text()

        import kicad_tools.cli.sch_fix_annotation as mod

        monkeypatch.setattr(mod, "find_kicad_cli", lambda: None)

        rc = run_fix_annotation(root, backup=False)
        assert rc == 1
        assert root.read_text() == before  # nothing written


# ---------------------------------------------------------------------------
# kicad-cli-dependent integration (skipped when kicad-cli unavailable)
# ---------------------------------------------------------------------------


@pytest.mark.skipif(find_kicad_cli() is None, reason="kicad-cli not installed")
class TestNetGateWithKicadCli:
    def test_real_gate_passes_on_neutral_rename(self, tmp_path):
        # A schematic whose only defect is a net-name-styled power ref; the
        # rename must be net-neutral and the gate must allow the write.
        root_text = _root_schematic(
            _power_symbol_no_instance("power:GND", "#GNDD", "u-gnd-root"),
        )
        sub_text = _sub_schematic()
        root = _write_hierarchy(tmp_path, root_text, sub_text)

        rc = run_fix_annotation(root, backup=False)
        # 0 (neutral, written) is the expected outcome; a non-zero here would
        # indicate the gate found a spurious diff.
        assert rc == 0
        assert "#GNDD" not in root.read_text()
