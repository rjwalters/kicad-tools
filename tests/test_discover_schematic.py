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

from kicad_tools.sync.discover import (
    pcb_matches_spec,
    resolve_schematic_for_pcb,
    resolve_schematic_with_source,
)


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


class TestSiblingRevisionUnderOneSpec:
    """Issue #9637: one project.kct names revision A; revision B sits beside it."""

    @staticmethod
    def _layout(tmp_path, artifacts: str = "") -> tuple[Path, Path]:
        a = tmp_path / "rev_a"
        b = tmp_path / "rev_b"
        a.mkdir()
        b.mkdir()
        for d in (a, b):
            _touch(d / "board.kicad_sch")
        pcb_a = _touch(a / "board_routed.kicad_pcb")
        pcb_b = _touch(b / "board_routed.kicad_pcb")
        (tmp_path / "project.kct").write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "test"\n'
            "  artifacts:\n"
            '    schematic: "rev_a/board.kicad_sch"\n'
            '    pcb: "rev_a/board.kicad_pcb"\n' + artifacts
        )
        return pcb_a, pcb_b

    def test_spec_board_still_resolves_via_project_kct(self, tmp_path):
        pcb_a, _ = self._layout(tmp_path)
        assert resolve_schematic_with_source(pcb_a) == (
            tmp_path / "rev_a" / "board.kicad_sch",
            "project.kct",
        )

    def test_sibling_revision_resolves_its_own_schematic(self, tmp_path):
        _, pcb_b = self._layout(tmp_path)
        assert resolve_schematic_with_source(pcb_b) == (
            tmp_path / "rev_b" / "board.kicad_sch",
            "sibling",
        )
        assert resolve_schematic_for_pcb(pcb_b) == tmp_path / "rev_b" / "board.kicad_sch"

    def test_explicit_pcb_routed_artifact_matches(self, tmp_path):
        pcb_a, pcb_b = self._layout(tmp_path, '    pcb_routed: "rev_a/board_routed.kicad_pcb"\n')
        assert pcb_matches_spec(pcb_a, tmp_path / "project.kct", _spec(tmp_path))
        assert not pcb_matches_spec(pcb_b, tmp_path / "project.kct", _spec(tmp_path))

    def test_spec_without_board_artifact_matches_any_pcb(self, tmp_path):
        _, pcb_b = self._layout(tmp_path)
        (tmp_path / "project.kct").write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "test"\n'
            "  artifacts:\n"
            '    schematic: "rev_a/board.kicad_sch"\n'
        )
        assert resolve_schematic_with_source(pcb_b) == (
            tmp_path / "rev_a" / "board.kicad_sch",
            "project.kct",
        )

    def test_spec_beside_pcb_is_that_boards_spec(self, tmp_path):
        # Spec in the PCB's own directory describes it even if the artifact
        # name differs (versioned boards, recipe fixtures).
        _touch(tmp_path / "custom.kicad_sch")
        pcb = _touch(tmp_path / "board_v5.kicad_pcb")
        (tmp_path / "project.kct").write_text(
            'kct_version: "1.0"\n'
            "project:\n"
            '  name: "test"\n'
            "  artifacts:\n"
            '    schematic: "custom.kicad_sch"\n'
            '    pcb: "board.kicad_pcb"\n'
        )
        assert resolve_schematic_with_source(pcb) == (tmp_path / "custom.kicad_sch", "project.kct")

    def test_missing_spec_schematic_falls_through(self, tmp_path):
        pcb_a, _ = self._layout(tmp_path)
        (tmp_path / "rev_a" / "board.kicad_sch").unlink()
        assert resolve_schematic_with_source(pcb_a) == (None, None)

    def test_source_labels(self, tmp_path):
        _touch(tmp_path / "root.kicad_sch")
        _touch(tmp_path / "root.kicad_pro")
        pcb = _touch(tmp_path / "root_v2.kicad_pcb")
        assert resolve_schematic_with_source(pcb)[1] == ".kicad_pro pairing"
        flat = tmp_path / "flat"
        flat.mkdir()
        _touch(flat / "only.kicad_sch")
        assert resolve_schematic_with_source(_touch(flat / "x.kicad_pcb"))[1] == "sole schematic"


def _spec(tmp_path):
    from kicad_tools.spec import load_spec

    return load_spec(tmp_path / "project.kct")


def test_check_reports_schematic_path_and_source_for_sibling_revision(tmp_path, capsys):
    """Issue #9637: kct check names the schematic LVS ran against and how."""
    from kicad_tools.cli.check_cmd import (
        MetaCheckResult,
        SubCheckResult,
        _resolve_check_schematic_with_source,
        print_meta_check_stanza,
    )

    _, pcb_b = TestSiblingRevisionUnderOneSpec._layout(tmp_path)
    sch, source = _resolve_check_schematic_with_source(None, pcb_b)
    assert sch == tmp_path / "rev_b" / "board.kicad_sch"
    assert source == "sibling"

    explicit = tmp_path / "rev_a" / "board.kicad_sch"
    assert _resolve_check_schematic_with_source(str(explicit), pcb_b) == (
        explicit.resolve(),
        "--schematic",
    )

    ok = SubCheckResult(status="PASSED", detail="ok")
    result = MetaCheckResult(
        drc=ok,
        erc=ok,
        lvs=ok,
        manifest=ok,
        schematic_path=str(sch),
        schematic_source=source,
    )
    result.compute_overall()
    assert result.to_dict()["schematic"] == {"path": str(sch), "source": "sibling"}
    print_meta_check_stanza(result)
    assert f"Schematic: {sch} (found via sibling)" in capsys.readouterr().out
