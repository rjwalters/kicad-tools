"""Tests for versioned-basename schematic discovery (issue #4350).

``resolve_schematic_for_pcb`` must resolve the root ``.kicad_sch`` even when the
board artifact carries a *version* suffix (``_v24``, ``_v23_mfg``, ...) that is
not a known pipeline-stage suffix.  It does so via ``.kicad_pro``/``.kicad_sch``
stem pairing (step 3) and a sole-schematic fallback (step 4), while an ambiguity
guard returns ``None`` rather than comparing copper against the wrong design.

These tests only care about *which path* is resolved, so the fixture files are
empty placeholders -- discovery keys on existence, not content.
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.sync.discover import resolve_schematic_for_pcb


def _touch(path: Path) -> Path:
    path.write_text("")
    return path


class TestVersionedBasenameDiscovery:
    def test_versioned_board_resolves_root_via_pro_pairing(self, tmp_path):
        # Repro layout: versioned board + its own .kicad_pro (no matching
        # .kicad_sch), plus an unversioned root project pair.
        _touch(tmp_path / "chorus-test-revA.kicad_sch")
        _touch(tmp_path / "chorus-test-revA.kicad_pro")
        _touch(tmp_path / "chorus-test-revA_v24.kicad_pro")
        pcb = _touch(tmp_path / "chorus-test-revA_v24.kicad_pcb")

        resolved = resolve_schematic_for_pcb(pcb)
        assert resolved == tmp_path / "chorus-test-revA.kicad_sch"

    def test_hierarchical_children_do_not_defeat_pro_pairing(self, tmp_path):
        # Root project pair + child sub-sheets (which have .kicad_sch but no
        # matching .kicad_pro).  Pairing must pick the root, not a child.
        _touch(tmp_path / "board.kicad_sch")
        _touch(tmp_path / "board.kicad_pro")
        _touch(tmp_path / "mcu.kicad_sch")
        _touch(tmp_path / "power.kicad_sch")
        _touch(tmp_path / "board_v3.kicad_pro")
        pcb = _touch(tmp_path / "board_v3.kicad_pcb")

        resolved = resolve_schematic_for_pcb(pcb)
        assert resolved == tmp_path / "board.kicad_sch"

    def test_sole_schematic_fallback_without_pro(self, tmp_path):
        # Flat project: a single .kicad_sch with no paired .kicad_pro at all.
        sch = _touch(tmp_path / "root.kicad_sch")
        pcb = _touch(tmp_path / "root_v2.kicad_pcb")

        resolved = resolve_schematic_for_pcb(pcb)
        assert resolved == sch


class TestAmbiguityGuard:
    def test_two_paired_roots_none_matching_returns_none(self, tmp_path):
        # Two distinct root project pairs, neither matching the board stem ->
        # ambiguous, must not guess.
        _touch(tmp_path / "alpha.kicad_sch")
        _touch(tmp_path / "alpha.kicad_pro")
        _touch(tmp_path / "beta.kicad_sch")
        _touch(tmp_path / "beta.kicad_pro")
        pcb = _touch(tmp_path / "gamma_v1.kicad_pcb")

        assert resolve_schematic_for_pcb(pcb) is None

    def test_multiple_schematics_no_pairing_returns_none(self, tmp_path):
        # Several child .kicad_sch files, no root .kicad_pro pairing -> the
        # sole-schematic fallback must not fire.
        _touch(tmp_path / "mcu.kicad_sch")
        _touch(tmp_path / "power.kicad_sch")
        pcb = _touch(tmp_path / "board_v1.kicad_pcb")

        assert resolve_schematic_for_pcb(pcb) is None

    def test_empty_directory_returns_none(self, tmp_path):
        pcb = _touch(tmp_path / "board_v9.kicad_pcb")
        assert resolve_schematic_for_pcb(pcb) is None


class TestPrecedencePreserved:
    def test_exact_stem_match_wins_over_pairing(self, tmp_path):
        # An exact <stem>.kicad_sch still wins even when another paired root
        # exists.  (Board stem match takes precedence over the fallback.)
        _touch(tmp_path / "board.kicad_sch")
        _touch(tmp_path / "board.kicad_pro")
        _touch(tmp_path / "other.kicad_sch")
        _touch(tmp_path / "other.kicad_pro")
        pcb = _touch(tmp_path / "board.kicad_pcb")

        resolved = resolve_schematic_for_pcb(pcb)
        assert resolved == tmp_path / "board.kicad_sch"

    def test_stage_suffix_strip_still_resolves(self, tmp_path):
        _touch(tmp_path / "board.kicad_sch")
        pcb = _touch(tmp_path / "board_routed.kicad_pcb")

        resolved = resolve_schematic_for_pcb(pcb)
        assert resolved == tmp_path / "board.kicad_sch"

    def test_project_kct_artifacts_schematic_wins(self, tmp_path):
        # project.kct artifacts.schematic must take precedence over both the
        # pro-pairing and sole-schematic fallbacks.
        _touch(tmp_path / "custom_name.kicad_sch")
        # A distractor root pair the fallback would otherwise resolve.
        _touch(tmp_path / "board.kicad_sch")
        _touch(tmp_path / "board.kicad_pro")
        pcb = _touch(tmp_path / "board_v5.kicad_pcb")
        (tmp_path / "project.kct").write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "test"\n'
            "  artifacts:\n"
            '    schematic: "custom_name.kicad_sch"\n'
            '    pcb: "board_v5.kicad_pcb"\n'
        )

        resolved = resolve_schematic_for_pcb(pcb)
        assert resolved == tmp_path / "custom_name.kicad_sch"
