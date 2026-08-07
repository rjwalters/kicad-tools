"""Tests for the LLM-free doc-drift lint (Issue #4540).

Covers the ``kct:doc-pin`` marker grammar, the ``drc-tolerance``
resolver (hit / absent-entry-resolves-to-0 / unresolvable path), the
bootstrap carve-outs (zero markers, missing README, undiscoverable repo
root, missing tolerance file), the advisory INFO-severity guarantee
(``error_count`` never moves), the JSON shape, the CLI dispatcher
integration, and a committed-artifact guard asserting the onboarded
board-06 / board-07 markers are clean against the committed
``.github/routed-drc-tolerance.yml`` (the self-maintaining ratchet
contract: a future yml ratchet without the README edit fails here).
"""

from __future__ import annotations

from pathlib import Path

from kicad_tools.cli import check_cmd
from kicad_tools.schema.pcb import PCB
from kicad_tools.validate import DRCChecker
from kicad_tools.validate.doc_drift import (
    RULE_STALE_PIN,
    RULE_UNRESOLVABLE_PIN,
    DocPin,
    check_doc_drift,
    find_repo_root,
    parse_doc_pins,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

BOARD_06_PCB = REPO_ROOT / "boards/06-diffpair-test/output/diffpair_test_routed.kicad_pcb"
BOARD_07_PCB = REPO_ROOT / "boards/07-matchgroup-test/output/matchgroup_test_routed.kicad_pcb"


def _make_repo(
    tmp_path: Path,
    *,
    tolerance_yaml: str | None = "tolerances:\n  boards/x/output/board.kicad_pcb: 8\n",
    readme: str | None = None,
    git_dir: bool = True,
) -> Path:
    """Build a minimal repo fixture; returns the PCB path.

    Layout::

        <tmp>/repo/.git/
        <tmp>/repo/.github/routed-drc-tolerance.yml
        <tmp>/repo/boards/x/README.md
        <tmp>/repo/boards/x/output/board.kicad_pcb
    """
    repo = tmp_path / "repo"
    board_dir = repo / "boards" / "x"
    output_dir = board_dir / "output"
    output_dir.mkdir(parents=True)
    if git_dir:
        (repo / ".git").mkdir()
    if tolerance_yaml is not None:
        gh = repo / ".github"
        gh.mkdir()
        (gh / "routed-drc-tolerance.yml").write_text(tolerance_yaml)
    if readme is not None:
        (board_dir / "README.md").write_text(readme)
    pcb_path = output_dir / "board.kicad_pcb"
    pcb_path.write_text("(kicad_pcb)")
    return pcb_path


def _marker(value: str = "8", key: str = "boards/x/output/board.kicad_pcb") -> str:
    return f"<!-- kct:doc-pin drc-tolerance {key} = {value} -->\n"


class TestMarkerGrammar:
    def test_valid_marker_parsed(self, tmp_path: Path) -> None:
        doc = tmp_path / "README.md"
        doc.write_text("prose\n" + _marker("8") + "more prose\n")
        pins = parse_doc_pins(doc)
        assert pins == [
            DocPin(
                resolver="drc-tolerance",
                key="boards/x/output/board.kicad_pcb",
                claimed="8",
                doc_path=doc,
                line=2,
            )
        ]

    def test_tolerant_whitespace(self, tmp_path: Path) -> None:
        doc = tmp_path / "README.md"
        doc.write_text("<!--   kct:doc-pin   drc-tolerance   a/b.kicad_pcb=42   -->\n")
        (pin,) = parse_doc_pins(doc)
        assert pin.resolver == "drc-tolerance"
        assert pin.key == "a/b.kicad_pcb"
        assert pin.claimed == "42"

    def test_malformed_markers_ignored_as_prose(self, tmp_path: Path) -> None:
        doc = tmp_path / "README.md"
        doc.write_text(
            # Missing '=' separator.
            "<!-- kct:doc-pin drc-tolerance a/b.kicad_pcb 8 -->\n"
            # Wrong prefix.
            "<!-- kct:docpin drc-tolerance a/b.kicad_pcb = 8 -->\n"
            # Multi-line comment (markers are single-line only).
            "<!-- kct:doc-pin drc-tolerance\n a/b.kicad_pcb = 8 -->\n"
            # Plain prose mentioning the syntax.
            "use kct:doc-pin markers = good\n"
        )
        assert parse_doc_pins(doc) == []

    def test_missing_doc_returns_empty(self, tmp_path: Path) -> None:
        assert parse_doc_pins(tmp_path / "nope.md") == []

    def test_multiple_markers_all_parsed(self, tmp_path: Path) -> None:
        doc = tmp_path / "README.md"
        doc.write_text(_marker("8") + "prose\n" + _marker("9", key="c/d.kicad_pcb"))
        pins = parse_doc_pins(doc)
        assert [(p.claimed, p.line) for p in pins] == [("8", 1), ("9", 3)]


class TestFindRepoRoot:
    def test_finds_git_dir(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, tolerance_yaml=None)
        assert find_repo_root(pcb_path.parent) == (tmp_path / "repo").resolve()

    def test_finds_tolerance_file_without_git(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, git_dir=False)
        assert find_repo_root(pcb_path.parent) == (tmp_path / "repo").resolve()

    def test_no_root_returns_none(self, tmp_path: Path) -> None:
        # pytest tmp dirs are outside any git checkout, so the walk to
        # the filesystem root finds no sentinel.
        deep = tmp_path / "a" / "b"
        deep.mkdir(parents=True)
        assert find_repo_root(deep) is None


class TestDrcToleranceResolver:
    def test_matching_pin_no_findings(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, readme=_marker("8"))
        results = check_doc_drift(pcb_path)
        assert results.violations == []
        assert results.rules_checked_by_rule[RULE_STALE_PIN] == 1
        assert results.rules_checked_by_rule[RULE_UNRESOLVABLE_PIN] == 1

    def test_stale_pin_fires_once_with_both_values(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, readme="line1\n" + _marker("80"))
        results = check_doc_drift(pcb_path)
        assert len(results.violations) == 1
        v = results.violations[0]
        assert v.rule_id == RULE_STALE_PIN
        assert v.severity == "info"
        # Message names the doc file:line, both values, and the source.
        assert "boards/x/README.md:2" in v.message
        assert "80" in v.message
        assert "pins 8" in v.message
        assert "routed-drc-tolerance.yml" in v.message

    def test_absent_entry_resolves_to_zero(self, tmp_path: Path) -> None:
        # The pinned key is NOT in the tolerances map: absence == strict 0.
        yaml_text = "tolerances:\n  boards/other/output/o.kicad_pcb: 3\n"
        pcb_path = _make_repo(tmp_path, tolerance_yaml=yaml_text, readme=_marker("0"))
        assert check_doc_drift(pcb_path).violations == []

    def test_absent_entry_mismatch_fires_stale(self, tmp_path: Path) -> None:
        yaml_text = "tolerances:\n  boards/other/output/o.kicad_pcb: 3\n"
        pcb_path = _make_repo(tmp_path, tolerance_yaml=yaml_text, readme=_marker("5"))
        (v,) = check_doc_drift(pcb_path).violations
        assert v.rule_id == RULE_STALE_PIN
        assert "strict 0" in v.message

    def test_nonexistent_key_path_is_unresolvable(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(
            tmp_path, readme=_marker("8", key="boards/typo/output/board.kicad_pcb")
        )
        (v,) = check_doc_drift(pcb_path).violations
        assert v.rule_id == RULE_UNRESOLVABLE_PIN
        assert v.severity == "info"
        assert "does not exist" in v.message

    def test_unknown_resolver_is_unresolvable(self, tmp_path: Path) -> None:
        readme = "<!-- kct:doc-pin bogus-resolver some/key = 8 -->\n"
        pcb_path = _make_repo(tmp_path, readme=readme)
        (v,) = check_doc_drift(pcb_path).violations
        assert v.rule_id == RULE_UNRESOLVABLE_PIN
        assert "bogus-resolver" in v.message
        assert "drc-tolerance" in v.message  # Known resolvers are listed.

    def test_non_integer_yaml_entry_is_unresolvable(self, tmp_path: Path) -> None:
        yaml_text = "tolerances:\n  boards/x/output/board.kicad_pcb: lots\n"
        pcb_path = _make_repo(tmp_path, tolerance_yaml=yaml_text, readme=_marker("8"))
        (v,) = check_doc_drift(pcb_path).violations
        assert v.rule_id == RULE_UNRESOLVABLE_PIN

    def test_multiple_markers_all_evaluated(self, tmp_path: Path) -> None:
        readme = _marker("8") + "prose\n" + _marker("99")
        pcb_path = _make_repo(tmp_path, readme=readme)
        violations = check_doc_drift(pcb_path).violations
        assert [v.rule_id for v in violations] == [RULE_STALE_PIN]
        assert "README.md:3" in violations[0].message


class TestCarveOuts:
    def test_zero_markers_silent_pass(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, readme="no markers here\n")
        assert check_doc_drift(pcb_path).violations == []

    def test_missing_readme_silent_pass(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, readme=None)
        assert check_doc_drift(pcb_path).violations == []

    def test_no_repo_root_silent_pass(self, tmp_path: Path) -> None:
        # Markers exist but no .git / tolerance-file sentinel anywhere up
        # the tree: silent pass.
        board_dir = tmp_path / "boards" / "x"
        output_dir = board_dir / "output"
        output_dir.mkdir(parents=True)
        (board_dir / "README.md").write_text(_marker("8"))
        pcb_path = output_dir / "board.kicad_pcb"
        pcb_path.write_text("(kicad_pcb)")
        assert check_doc_drift(pcb_path).violations == []

    def test_repo_without_tolerance_file_silent_pass(self, tmp_path: Path) -> None:
        # .git present (root discoverable) but no tolerance file: the
        # drc-tolerance resolver skips silently (external consumers).
        pcb_path = _make_repo(tmp_path, tolerance_yaml=None, readme=_marker("8"))
        assert check_doc_drift(pcb_path).violations == []


class TestAdvisoryGuarantee:
    """INFO severity: findings never move error/warning counts."""

    def test_findings_are_info_only(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, readme=_marker("80"))
        results = check_doc_drift(pcb_path)
        assert results.info_count == 1
        assert results.error_count == 0
        assert results.warning_count == 0
        assert results.passed is True

    def test_json_shape(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, readme=_marker("80"))
        (v,) = check_doc_drift(pcb_path).violations
        data = v.to_dict()
        assert data["rule_id"] == RULE_STALE_PIN
        assert data["severity"] == "info"
        assert data["status"] == "info"
        assert data["waived"] is False
        assert isinstance(data["message"], str)

    def test_rules_classified_advisory_for_reporting(self) -> None:
        # Reporting-taxonomy entries (Issue #3803 style) so the findings
        # land in the advisory-quality bucket, never the fab-blocking one.
        for rule_id in (RULE_STALE_PIN, RULE_UNRESOLVABLE_PIN):
            assert DRCChecker.category_for_rule(rule_id) == DRCChecker.CATEGORY_ADVISORY
        # And the GATING advisory set is untouched (orthogonal axis).
        assert RULE_STALE_PIN not in DRCChecker.ADVISORY_RULE_IDS
        assert RULE_UNRESOLVABLE_PIN not in DRCChecker.ADVISORY_RULE_IDS


class TestDispatcherIntegration:
    """The CLI category wiring in ``run_selected_checks``."""

    @staticmethod
    def _checker() -> DRCChecker:
        pcb = PCB.create(width=10.0, height=10.0, layers=2)
        return DRCChecker(pcb, manufacturer="jlcpcb", layers=2)

    def test_category_registered(self) -> None:
        assert "doc_drift" in check_cmd.CHECK_CATEGORIES

    def test_no_pcb_path_is_silent_noop(self) -> None:
        results = check_cmd.run_selected_checks(
            self._checker(), only_set={"doc_drift"}, skip_set=set()
        )
        assert results.violations == []
        assert results.rules_checked == 0

    def test_pcb_path_dispatches_check(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, readme=_marker("80"))
        results = check_cmd.run_selected_checks(
            self._checker(),
            only_set={"doc_drift"},
            skip_set=set(),
            pcb_path=pcb_path,
        )
        assert [v.rule_id for v in results.violations] == [RULE_STALE_PIN]
        assert results.error_count == 0

    def test_skip_suppresses_category(self, tmp_path: Path) -> None:
        pcb_path = _make_repo(tmp_path, readme=_marker("80"))
        results = check_cmd.run_selected_checks(
            self._checker(),
            only_set={"doc_drift"},
            skip_set={"doc_drift"},
            pcb_path=pcb_path,
        )
        assert results.violations == []


class TestCommittedArtifactsClean:
    """The onboarded markers must be clean on the committed tree.

    This is the self-maintaining ratchet contract from Issue #4540: any
    future PR that ratchets ``.github/routed-drc-tolerance.yml`` for
    board 06 / board 07 without updating the board README's doc-pin
    marker (and prose) fails HERE, closing the cross-PR staleness gap
    that ``annotate_drift_warning`` (#2590) explicitly does not cover.
    """

    def test_board06_markers_clean(self) -> None:
        assert BOARD_06_PCB.is_file(), "board-06 committed artifact missing"
        results = check_doc_drift(BOARD_06_PCB)
        assert results.violations == [], [v.message for v in results.violations]

    def test_board06_readme_has_marker(self) -> None:
        pins = parse_doc_pins(BOARD_06_PCB.parent.parent / "README.md")
        assert any(p.resolver == "drc-tolerance" for p in pins)

    def test_board07_markers_clean(self) -> None:
        assert BOARD_07_PCB.is_file(), "board-07 committed artifact missing"
        results = check_doc_drift(BOARD_07_PCB)
        assert results.violations == [], [v.message for v in results.violations]

    def test_board07_readme_has_marker(self) -> None:
        pins = parse_doc_pins(BOARD_07_PCB.parent.parent / "README.md")
        assert any(p.resolver == "drc-tolerance" for p in pins)
