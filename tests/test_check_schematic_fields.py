"""Tests for the schematic field-geometry lint (Issue #4595).

Covers the ``sch_fields`` check category: the ``sch_field_offset`` and
``sch_field_overlap`` rules in
``kicad_tools.validate.rules.schematic_fields``, plus the ``kct check``
CLI wiring (``--only``/``--skip``, ``--sch-field-threshold``, warning
severity / exit-code contract, deterministic output).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from kicad_tools.schema import Schematic
from kicad_tools.schema.field_geometry import field_offset_mm, placed_body_bbox
from kicad_tools.validate.rules.schematic_fields import (
    DEFAULT_SCH_FIELD_THRESHOLD_MM,
    RULE_FIELD_OFFSET,
    RULE_FIELD_OVERLAP,
    check_schematic_fields,
    field_text_bbox,
)

# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------

# Device:R body rectangle spans x in [-1.016, 1.016], y in [-2.54, 2.54]
# (library coords); the placed bbox at rotation 0 is symmetric about the
# instance position.
_LIB_SYMBOLS = """	(lib_symbols
		(symbol "Device:R"
			(symbol "R_0_1"
				(rectangle (start -1.016 -2.54) (end 1.016 2.54) (stroke (width 0.254)) (fill (type none)))
			)
			(symbol "R_1_1"
				(pin passive line (at 0 3.81 270) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
				(pin passive line (at 0 -3.81 90) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "2" (effects (font (size 1.27 1.27)))))
			)
		)
		(symbol "Test:Asym"
			(symbol "Asym_0_1"
				(polyline (pts (xy 0 0) (xy 4 0) (xy 4 2) (xy 0 2) (xy 0 0)) (stroke (width 0.254)) (fill (type none)))
			)
			(symbol "Asym_1_1"
				(pin passive line (at 0 0 0) (length 1.27) (name "~" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
			)
		)
		(symbol "power:GND"
			(symbol "GND_0_1"
				(polyline (pts (xy 0 0) (xy 0 -1.27)) (stroke (width 0)) (fill (type none)))
			)
			(symbol "GND_1_1"
				(pin power_in line (at 0 0 270) (length 0) (name "GND" (effects (font (size 1.27 1.27)))) (number "1" (effects (font (size 1.27 1.27)))))
			)
		)
	)
"""


def _sch_document(body: str, uuid: str = "test-sch-uuid-0001") -> str:
    return (
        "(kicad_sch\n"
        "\t(version 20231120)\n"
        '\t(generator "kicadtools_test")\n'
        '\t(generator_version "8.0")\n'
        f'\t(uuid "{uuid}")\n'
        '\t(paper "A4")\n'
        f"{_LIB_SYMBOLS}"
        f"{body}"
        ")\n"
    )


def _symbol(
    ref: str,
    at: tuple[float, float],
    ref_at: tuple[float, float],
    *,
    lib_id: str = "Device:R",
    value: str = "10k",
    rotation: float = 0,
    mirror: str = "",
    ref_hidden: bool = False,
    value_at: tuple[float, float] | None = None,
    value_hidden: bool = True,
    ref_justify: str = "",
) -> str:
    """Render one placed-symbol s-expression."""
    mirror_line = f"\t\t(mirror {mirror})\n" if mirror else ""
    ref_effects = "(effects (font (size 1.27 1.27))"
    if ref_justify:
        ref_effects += f" (justify {ref_justify})"
    ref_effects += " hide)" if ref_hidden else ")"
    if value_at is None:
        value_at = at
    value_effects = "(effects (font (size 1.27 1.27))"
    value_effects += " hide)" if value_hidden else ")"
    return (
        "\t(symbol\n"
        f'\t\t(lib_id "{lib_id}")\n'
        f"\t\t(at {at[0]} {at[1]} {rotation})\n"
        f"{mirror_line}"
        "\t\t(unit 1)\n"
        "\t\t(in_bom yes)\n"
        "\t\t(on_board yes)\n"
        f'\t\t(uuid "uuid-{ref}")\n'
        f'\t\t(property "Reference" "{ref}"\n'
        f"\t\t\t(at {ref_at[0]} {ref_at[1]} 0)\n"
        f"\t\t\t{ref_effects}\n"
        "\t\t)\n"
        f'\t\t(property "Value" "{value}"\n'
        f"\t\t\t(at {value_at[0]} {value_at[1]} 0)\n"
        f"\t\t\t{value_effects}\n"
        "\t\t)\n"
        f'\t\t(pin "1" (uuid "uuid-{ref}-p1"))\n'
        f'\t\t(pin "2" (uuid "uuid-{ref}-p2"))\n'
        "\t)\n"
    )


def _write_sch(tmp_path: Path, body: str, name: str = "test.kicad_sch") -> Path:
    path = tmp_path / name
    path.write_text(_sch_document(body))
    return path


def _by_rule(results, rule_id: str):
    return [v for v in results.violations if v.rule_id == rule_id]


# ---------------------------------------------------------------------------
# Unit: sch_field_offset
# ---------------------------------------------------------------------------


class TestFieldOffset:
    def test_adrift_field_flagged_once(self, tmp_path: Path) -> None:
        # R1's Reference is ~56 mm from its body; R2 is healthy (~2.5 mm).
        body = _symbol("R1", (100, 100), (140, 60)) + _symbol("R2", (200, 100), (200, 95))
        sch = _write_sch(tmp_path, body)

        results = check_schematic_fields(sch)
        offsets = _by_rule(results, RULE_FIELD_OFFSET)
        assert len(offsets) == 1
        v = offsets[0]
        assert v.severity == "warning"
        assert v.items == ("R1",)
        assert "R1.Reference" in v.message
        assert "from body" in v.message
        assert f"threshold {DEFAULT_SCH_FIELD_THRESHOLD_MM:.1f}mm" in v.message
        assert v.required_value == DEFAULT_SCH_FIELD_THRESHOLD_MM
        assert v.actual_value is not None and v.actual_value > 50
        # No overlaps in this layout.
        assert not _by_rule(results, RULE_FIELD_OVERLAP)

    def test_all_warning_severity_never_errors(self, tmp_path: Path) -> None:
        body = _symbol("R1", (100, 100), (140, 60))
        sch = _write_sch(tmp_path, body)
        results = check_schematic_fields(sch)
        assert results.violations
        assert all(v.severity == "warning" for v in results.violations)
        assert results.error_count == 0
        assert results.passed  # warnings never fail the gate

    def test_threshold_override_respected(self, tmp_path: Path) -> None:
        body = _symbol("R1", (100, 100), (140, 60)) + _symbol("R2", (200, 100), (200, 95))
        sch = _write_sch(tmp_path, body)

        # Huge threshold: nothing fires.
        assert not check_schematic_fields(sch, threshold_mm=100.0).violations
        # Tiny threshold: the healthy R2 fires too.
        offsets = _by_rule(check_schematic_fields(sch, threshold_mm=1.0), RULE_FIELD_OFFSET)
        assert {v.items[0] for v in offsets} == {"R1", "R2"}

    def test_exact_threshold_does_not_fire(self, tmp_path: Path) -> None:
        """The comparison is strictly-greater-than: offset == threshold is OK."""
        body = _symbol("R1", (100, 100), (100, 90))
        sch = _write_sch(tmp_path, body)

        # Compute the exact same offset the lint measures, via the shared
        # field_geometry metric.
        schematic = Schematic.load(sch)
        inst = schematic.symbols[0]
        lib = schematic.get_lib_symbol_resolved(inst.lib_id)
        assert lib is not None
        bbox = placed_body_bbox(lib, inst.position, rotation=inst.rotation, mirror=inst.mirror)
        offset = field_offset_mm((100.0, 90.0), bbox)
        assert offset > 0

        assert not check_schematic_fields(sch, threshold_mm=offset).violations
        fired = check_schematic_fields(sch, threshold_mm=offset - 0.01)
        assert len(_by_rule(fired, RULE_FIELD_OFFSET)) == 1

    def test_rotation_consumed_from_field_geometry(self, tmp_path: Path) -> None:
        """A 90-degree rotation swaps the R body extents (±1.016 x -> ±2.54 x);
        the lint must measure against the rotated bbox."""
        # Field at (110, 100): rotated dx = 110 - 102.54 = 7.46 mm;
        # if rotation were ignored, dx = 110 - 101.016 = 8.98 mm.
        body = _symbol("R1", (100, 100), (110, 100), rotation=90)
        sch = _write_sch(tmp_path, body)

        assert not check_schematic_fields(sch, threshold_mm=8.0).violations
        fired = _by_rule(check_schematic_fields(sch, threshold_mm=7.0), RULE_FIELD_OFFSET)
        assert len(fired) == 1
        assert fired[0].actual_value == pytest.approx(7.46, abs=0.01)

    def test_mirror_consumed_from_field_geometry(self, tmp_path: Path) -> None:
        """Mirroring an asymmetric body must move the measured bbox."""
        base = _symbol("A1", (100, 100), (107, 100), lib_id="Test:Asym")
        mirrored = _symbol("A1", (100, 100), (107, 100), lib_id="Test:Asym", mirror="x")

        sch_base = _write_sch(tmp_path, base, name="base.kicad_sch")
        sch_mirrored = _write_sch(tmp_path, mirrored, name="mirrored.kicad_sch")

        # Expected offsets from the shared geometry module (not hardcoded).
        def expected(sch_path: Path) -> float:
            schematic = Schematic.load(sch_path)
            inst = schematic.symbols[0]
            lib = schematic.get_lib_symbol_resolved(inst.lib_id)
            assert lib is not None
            bbox = placed_body_bbox(lib, inst.position, rotation=inst.rotation, mirror=inst.mirror)
            return field_offset_mm((107.0, 100.0), bbox)

        off_base = expected(sch_base)
        off_mirrored = expected(sch_mirrored)
        assert off_base != pytest.approx(off_mirrored)

        threshold = (off_base + off_mirrored) / 2
        results_base = check_schematic_fields(sch_base, threshold_mm=threshold)
        results_mirrored = check_schematic_fields(sch_mirrored, threshold_mm=threshold)
        # Exactly one of the two placements is beyond the midpoint threshold.
        fired = [
            bool(_by_rule(results_base, RULE_FIELD_OFFSET)),
            bool(_by_rule(results_mirrored, RULE_FIELD_OFFSET)),
        ]
        assert sorted(fired) == [False, True]


# ---------------------------------------------------------------------------
# Unit: sch_field_overlap
# ---------------------------------------------------------------------------


class TestFieldOverlap:
    def test_field_over_another_body_flagged(self, tmp_path: Path) -> None:
        # R2's Reference text sits directly on R1's body.  R1's own fields
        # are hidden to isolate the collision.
        body = _symbol("R1", (100, 100), (100, 100), ref_hidden=True) + _symbol(
            "R2", (110, 100), (100, 100)
        )
        sch = _write_sch(tmp_path, body)

        results = check_schematic_fields(sch)
        overlaps = _by_rule(results, RULE_FIELD_OVERLAP)
        assert len(overlaps) == 1
        v = overlaps[0]
        assert v.severity == "warning"
        assert v.items == ("R2", "R1")
        assert "R2.Reference text overlaps R1 body" in v.message

    def test_superimposed_fields_flagged(self, tmp_path: Path) -> None:
        """Two different symbols' fields at the same coordinates (the
        ``+3.3VA9``-style composite) must be flagged once."""
        body = _symbol("R1", (100, 100), (100, 90)) + _symbol("R2", (140, 100), (100, 90))
        sch = _write_sch(tmp_path, body)

        results = check_schematic_fields(sch)
        overlaps = _by_rule(results, RULE_FIELD_OVERLAP)
        assert len(overlaps) == 1
        v = overlaps[0]
        assert set(v.items) == {"R1", "R2"}
        assert "text overlaps" in v.message and ".Reference" in v.message

    def test_own_body_and_same_symbol_fields_not_flagged(self, tmp_path: Path) -> None:
        # Reference AND Value both visible, both inside the symbol's own
        # body: neither the own-body nor the same-symbol field pair counts.
        body = _symbol("R1", (100, 100), (100, 100), value_at=(100, 100), value_hidden=False)
        sch = _write_sch(tmp_path, body)
        assert not check_schematic_fields(sch).violations

    def test_justify_left_shifts_text_bbox(self) -> None:
        bbox_center = field_text_bbox("R1", (100.0, 100.0))
        bbox_left = field_text_bbox("R1", (100.0, 100.0), justify="left")
        bbox_right = field_text_bbox("R1", (100.0, 100.0), justify="right")
        assert bbox_left[0] == pytest.approx(100.0)
        assert bbox_right[2] == pytest.approx(100.0)
        assert bbox_center[0] < 100.0 < bbox_center[2]

    def test_vertical_text_swaps_axes(self) -> None:
        h = field_text_bbox("R1234", (0.0, 0.0))
        v = field_text_bbox("R1234", (0.0, 0.0), rotation=90)
        assert (h[2] - h[0]) == pytest.approx(v[3] - v[1])
        assert (h[3] - h[1]) == pytest.approx(v[2] - v[0])


# ---------------------------------------------------------------------------
# Unit: scope / skip contracts (mirrors kct sch tidy, issue #4596)
# ---------------------------------------------------------------------------


class TestSkipContracts:
    def test_hidden_field_skipped(self, tmp_path: Path) -> None:
        body = _symbol("R1", (100, 100), (140, 60), ref_hidden=True)
        sch = _write_sch(tmp_path, body)
        assert not check_schematic_fields(sch).violations

    def test_power_symbol_skipped(self, tmp_path: Path) -> None:
        body = _symbol("#PWR01", (100, 100), (160, 40), lib_id="power:GND", value="GND")
        sch = _write_sch(tmp_path, body)
        assert not check_schematic_fields(sch).violations

    def test_unresolvable_lib_id_skipped_with_warning(self, tmp_path: Path, capsys) -> None:
        body = _symbol("R9", (100, 100), (150, 50), lib_id="Nope:Missing")
        sch = _write_sch(tmp_path, body)
        results = check_schematic_fields(sch)
        assert not results.violations
        err = capsys.readouterr().err
        assert "R9" in err and "Nope:Missing" in err and "skipped" in err

    def test_missing_schematic_is_graceful(self, tmp_path: Path, capsys) -> None:
        results = check_schematic_fields(tmp_path / "does_not_exist.kicad_sch")
        assert not results.violations
        assert "not found" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Unit: hierarchy
# ---------------------------------------------------------------------------


class TestHierarchy:
    def _root_with_sub(self, tmp_path: Path, sub_name: str) -> Path:
        sheet = (
            "\t(sheet\n"
            "\t\t(at 100 50)\n"
            "\t\t(size 20 15)\n"
            '\t\t(uuid "sheet-uuid-0001")\n'
            f'\t\t(property "Sheetname" "Sub"\n'
            "\t\t\t(at 100 49 0)\n"
            "\t\t\t(effects (font (size 1.27 1.27)))\n"
            "\t\t)\n"
            f'\t\t(property "Sheetfile" "{sub_name}"\n'
            "\t\t\t(at 100 66 0)\n"
            "\t\t\t(effects (font (size 1.27 1.27)))\n"
            "\t\t)\n"
            "\t)\n"
        )
        root = tmp_path / "root.kicad_sch"
        root.write_text(_sch_document(sheet, uuid="root-uuid"))
        return root

    def test_defect_on_sub_sheet_found(self, tmp_path: Path) -> None:
        root = self._root_with_sub(tmp_path, "sub.kicad_sch")
        (tmp_path / "sub.kicad_sch").write_text(
            _sch_document(_symbol("C13", (113.03, 82.55), (132.588, 44.958)), uuid="sub-uuid")
        )

        results = check_schematic_fields(root)
        offsets = _by_rule(results, RULE_FIELD_OFFSET)
        assert len(offsets) == 1
        assert "C13.Reference" in offsets[0].message
        assert "[sub.kicad_sch]" in offsets[0].message

    def test_missing_sub_sheet_is_graceful(self, tmp_path: Path, capsys) -> None:
        root = self._root_with_sub(tmp_path, "gone.kicad_sch")
        results = check_schematic_fields(root)
        assert not results.violations
        assert "gone.kicad_sch" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# Unit: determinism
# ---------------------------------------------------------------------------


class TestDeterminism:
    def test_findings_sorted_and_stable(self, tmp_path: Path) -> None:
        # Deliberately declare C2 before C1 in the file: output must be
        # sorted by reference, not file order.
        body = _symbol("C2", (200, 100), (150, 40)) + _symbol("C1", (100, 100), (60, 40))
        sch = _write_sch(tmp_path, body)

        first = [v.message for v in check_schematic_fields(sch).violations]
        second = [v.message for v in check_schematic_fields(sch).violations]
        assert first == second
        refs = [v.items[0] for v in check_schematic_fields(sch).violations]
        assert refs == sorted(refs)

    def test_rules_checked_bookkeeping(self, tmp_path: Path) -> None:
        sch = _write_sch(tmp_path, _symbol("R1", (100, 100), (100, 95)))
        results = check_schematic_fields(sch)
        assert results.rules_checked == 2
        assert results.rules_checked_by_rule == {
            RULE_FIELD_OFFSET: 1,
            RULE_FIELD_OVERLAP: 1,
        }


# ---------------------------------------------------------------------------
# CLI integration: kct check wiring
# ---------------------------------------------------------------------------

_PCB_TEMPLATE = """(kicad_pcb
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
  (gr_rect (start 100 100) (end 150 150)
    (stroke (width 0.1) (type default))
    (fill none)
    (layer "Edge.Cuts")
  )
)
"""


@pytest.fixture
def board_with_defective_schematic(tmp_path: Path) -> Path:
    """A minimal PCB with a sibling schematic containing one adrift field
    (R1.Reference ~56 mm) and one superimposed cross-symbol field pair."""
    pcb = tmp_path / "demo.kicad_pcb"
    pcb.write_text(_PCB_TEMPLATE)
    body = (
        _symbol("R1", (100, 100), (140, 60))
        + _symbol("R2", (200, 100), (180, 90))
        + _symbol("R3", (240, 100), (180, 90))
    )
    (tmp_path / "demo.kicad_sch").write_text(_sch_document(body))
    return pcb


@pytest.fixture
def stub_meta_subchecks(monkeypatch):
    """Stub the ERC/LVS/Manifest sub-checks to PASSED so the CLI tests are
    hermetic (no kicad-cli dependence) and the exit code is driven purely
    by the DRC/lint pipeline under test."""
    from kicad_tools.cli import check_cmd

    def _passed(*_args, **_kwargs):
        return check_cmd.SubCheckResult(status="PASSED", detail="stubbed for test")

    monkeypatch.setattr(check_cmd, "_erc_subcheck", _passed)
    monkeypatch.setattr(check_cmd, "_lvs_subcheck", _passed)
    monkeypatch.setattr(check_cmd, "_manifest_subcheck", _passed)


class TestCheckCliWiring:
    def _main(self, argv):
        from kicad_tools.cli.check_cmd import main

        return main(argv)

    def test_warnings_only_exit_zero(
        self, board_with_defective_schematic, stub_meta_subchecks, capsys
    ) -> None:
        rc = self._main([str(board_with_defective_schematic)])
        out = capsys.readouterr().out
        assert rc == 0
        assert "sch_field_offset" in out
        assert "sch_field_overlap" in out

    def test_strict_makes_warnings_fatal(
        self, board_with_defective_schematic, stub_meta_subchecks, capsys
    ) -> None:
        # Pre-existing global contract (same as silk_over_copper): warnings
        # are fatal under --strict; not special-cased for sch_fields.
        rc = self._main([str(board_with_defective_schematic), "--only", "sch_fields", "--strict"])
        capsys.readouterr()
        assert rc == 2

    def test_skip_silences_category(
        self, board_with_defective_schematic, stub_meta_subchecks, capsys
    ) -> None:
        rc = self._main([str(board_with_defective_schematic), "--skip", "sch_fields"])
        out = capsys.readouterr().out
        assert rc == 0
        assert "sch_field" not in out

    def test_only_runs_category_alone_json(
        self, board_with_defective_schematic, stub_meta_subchecks, capsys
    ) -> None:
        rc = self._main(
            [str(board_with_defective_schematic), "--only", "sch_fields", "--format", "json"]
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        violations = payload["violations"]
        assert violations
        assert {v["rule_id"] for v in violations} == {RULE_FIELD_OFFSET, RULE_FIELD_OVERLAP}
        assert all(v["severity"] == "warning" for v in violations)
        assert payload["summary"]["errors"] == 0
        assert payload["summary"]["passed"] is True
        # Per-rule bookkeeping is always present for CI consumers.
        assert payload["summary"]["rules_checked_by_rule"] == {
            RULE_FIELD_OFFSET: 1,
            RULE_FIELD_OVERLAP: 1,
        }

    def test_threshold_flag_plumbed(
        self, board_with_defective_schematic, stub_meta_subchecks, capsys
    ) -> None:
        rc = self._main(
            [
                str(board_with_defective_schematic),
                "--only",
                "sch_fields",
                "--sch-field-threshold",
                "1000",
                "--format",
                "json",
            ]
        )
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        # The huge threshold silences the offset rule; the overlap pair remains.
        assert {v["rule_id"] for v in payload["violations"]} == {RULE_FIELD_OVERLAP}

    def test_outer_kct_parser_forwards_threshold(
        self, board_with_defective_schematic, stub_meta_subchecks, capsys
    ) -> None:
        """The flag must survive the kct -> commands/validation.py ->
        check_cmd forwarding chain (the #4633 drift-bug class)."""
        from kicad_tools.cli.parser import create_parser

        parser = create_parser()
        args = parser.parse_args(
            [
                "check",
                str(board_with_defective_schematic),
                "--only",
                "sch_fields",
                "--sch-field-threshold",
                "1000",
                "--format",
                "json",
            ]
        )
        from kicad_tools.cli.commands.validation import run_check_command

        rc = run_check_command(args)
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert {v["rule_id"] for v in payload["violations"]} == {RULE_FIELD_OVERLAP}

    def test_consecutive_runs_byte_identical(
        self, board_with_defective_schematic, stub_meta_subchecks, capsys
    ) -> None:
        rc1 = self._main(
            [str(board_with_defective_schematic), "--only", "sch_fields", "--format", "json"]
        )
        out1 = capsys.readouterr().out
        rc2 = self._main(
            [str(board_with_defective_schematic), "--only", "sch_fields", "--format", "json"]
        )
        out2 = capsys.readouterr().out
        assert rc1 == rc2 == 0
        assert out1 == out2

    def test_drc_only_never_runs_sch_fields(self, board_with_defective_schematic, capsys) -> None:
        # Documented decision (#4595): --drc-only pins the legacy PCB-only
        # stdout/exit contract, so the schematic lint is not run there.
        self._main([str(board_with_defective_schematic), "--drc-only"])
        out = capsys.readouterr().out
        assert "sch_field" not in out

    def test_no_schematic_category_silent(
        self, tmp_path: Path, stub_meta_subchecks, capsys
    ) -> None:
        pcb = tmp_path / "lonely.kicad_pcb"
        pcb.write_text(_PCB_TEMPLATE)
        rc = self._main([str(pcb), "--only", "sch_fields", "--format", "json"])
        payload = json.loads(capsys.readouterr().out)
        assert rc == 0
        assert payload["violations"] == []
