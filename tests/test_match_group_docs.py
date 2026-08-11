"""Sanity tests for the match-group user-documentation set.

Guards the doc/code coupling for ``docs/guides/match-groups/``:

* the protocol recipes in ``05-protocol-recipes.md`` are valid Python that
  produces ``NetClassRouting`` instances whose ``effective_length_match_*``
  accessors return the values the recipe claims to demonstrate;
* every ``match_group_*``-prefixed ``rule_id`` registered in
  ``ViolationType``'s alias table is documented in ``06-drc-rule.md``
  (forward direction), and every match-group ``rule_id`` documented in
  ``06-drc-rule.md`` exists in the alias table (reverse direction);
* the cascade-safety constants documented in ``04-cascade-safety.md``
  match the live values in ``router/match_group_tuning.py``;
* all seven guides are present;
* no guide cites source by ``<file>.py:NNN`` line number (issue #4764 —
  line numbers rot on every refactor; guides must anchor on symbols),
  and every symbol a guide anchors on still exists in the file it names;
* each guide stays under the size cap (50 lines for README, 100 for the
  numbered guides) so we keep the "don't create monster doc files"
  invariant from Epic #2556 Phase 4M.

Issue: #2725.  Epic: #2661 (Phase 3M).  Mirrors ``test_diffpair_docs.py``.
Line-citation guard: #4764.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MATCH_GROUP_DOCS = REPO_ROOT / "docs" / "guides" / "match-groups"
ROUTING_GUIDE = REPO_ROOT / "docs" / "guides" / "routing.md"

# A ``<file>.py:NNN`` citation anywhere in a guide.  See issue #4764: the
# whole "Source line" column of 04-cascade-safety.md was wrong while
# ``test_cascade_safety_constants_match_code`` guarded the column next to it.
LINE_CITATION_RE = re.compile(r"\.py:\d+")

# Symbol anchors the match-group guides make, as
# ``(guide filename, symbol, source file relative to the repo root)``.
#
# Each row is checked in BOTH directions: the symbol must still appear in the
# guide (so a doc edit cannot silently drop or misspell the anchor) and in the
# source file the guide names (so a source-side rename goes red here instead
# of rotting quietly).  ``_gather_explicit_groups`` is the regression this
# table exists for — 03-group-of-pairs.md cited it under its pre-rename name
# ``_collect_explicit_match_groups`` for long enough that a line-number
# refresh could not have repaired the citation.
CITED_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    # 01 — declaring groups
    ("01-declaring-groups.md", "length_match_group", "src/kicad_tools/router/rules.py"),
    (
        "01-declaring-groups.md",
        "detect_match_groups",
        "src/kicad_tools/router/match_group_detection.py",
    ),
    (
        "01-declaring-groups.md",
        "BUS_GROUP_PATTERNS",
        "src/kicad_tools/router/match_group_detection.py",
    ),
    ("01-declaring-groups.md", "add_match_group", "src/kicad_tools/router/core.py"),
    # 02 — reference selection
    ("02-reference-selection.md", "NetClassRouting", "src/kicad_tools/router/rules.py"),
    ("02-reference-selection.md", "length_match_reference", "src/kicad_tools/router/rules.py"),
    (
        "02-reference-selection.md",
        "effective_length_match_reference",
        "src/kicad_tools/router/rules.py",
    ),
    (
        "02-reference-selection.md",
        "tune_match_group",
        "src/kicad_tools/router/optimizer/serpentine.py",
    ),
    (
        "02-reference-selection.md",
        "_resolve_reference",
        "src/kicad_tools/router/match_group_detection.py",
    ),
    (
        "02-reference-selection.md",
        "_resolve_clock_sentinel",
        "src/kicad_tools/router/match_group_detection.py",
    ),
    (
        "02-reference-selection.md",
        "reference_net_id",
        "src/kicad_tools/router/match_group_detection.py",
    ),
    (
        "02-reference-selection.md",
        "MatchGroupTracker",
        "src/kicad_tools/router/match_group_length.py",
    ),
    (
        "02-reference-selection.md",
        "get_reference_length",
        "src/kicad_tools/router/match_group_length.py",
    ),
    # 03 — groups whose members are diff pairs
    ("03-group-of-pairs.md", "NetClassRouting", "src/kicad_tools/router/rules.py"),
    (
        "03-group-of-pairs.md",
        "tune_match_group_v2",
        "src/kicad_tools/router/match_group_tuning.py",
    ),
    (
        "03-group-of-pairs.md",
        "_gather_explicit_groups",
        "src/kicad_tools/router/match_group_detection.py",
    ),
    ("03-group-of-pairs.md", "_main_impl", "src/kicad_tools/cli/route_cmd.py"),
    ("03-group-of-pairs.md", "length_match_groups", "src/kicad_tools/cli/route_cmd.py"),
    # 04 — cascade safety
    (
        "04-cascade-safety.md",
        "MAX_INSERTS_PER_GROUP_MEMBER_SMALL",
        "src/kicad_tools/router/match_group_tuning.py",
    ),
    (
        "04-cascade-safety.md",
        "MAX_INSERTS_PER_GROUP_MEMBER_LARGE",
        "src/kicad_tools/router/match_group_tuning.py",
    ),
    (
        "04-cascade-safety.md",
        "MAX_TOTAL_INSERTS_PER_GROUP",
        "src/kicad_tools/router/match_group_tuning.py",
    ),
    ("04-cascade-safety.md", "TuneResult", "src/kicad_tools/router/match_group_tuning.py"),
    (
        "04-cascade-safety.md",
        "tune_match_group_v2",
        "src/kicad_tools/router/match_group_tuning.py",
    ),
    # 06 — the DRC rule
    (
        "06-drc-rule.md",
        "MatchGroupTracker",
        "src/kicad_tools/router/match_group_length.py",
    ),
    (
        "06-drc-rule.md",
        "MatchGroupLengthSkewRule",
        "src/kicad_tools/validate/rules/match_group_length_skew.py",
    ),
    ("06-drc-rule.md", "update_match_group_skew", "src/kicad_tools/router/core.py"),
    (
        "06-drc-rule.md",
        "derive_group_skew_data",
        "src/kicad_tools/validate/match_group_skew.py",
    ),
    ("06-drc-rule.md", "MATCH_GROUP_LENGTH_SKEW", "src/kicad_tools/drc/violation.py"),
    ("06-drc-rule.md", "_ALIASES", "src/kicad_tools/drc/violation.py"),
    # 07 — CLI and sidecar
    ("07-cli-and-sidecar.md", "_main_impl", "src/kicad_tools/cli/route_cmd.py"),
    ("07-cli-and-sidecar.md", "apply_match_group_tuning", "src/kicad_tools/router/core.py"),
    ("07-cli-and-sidecar.md", "net_class_map_to_dict", "src/kicad_tools/router/rules.py"),
)


def _read_python_code_blocks(md_path: Path) -> list[str]:
    """Extract every ```python fenced block from a markdown file.

    Blocks are returned in document order.  Indented code blocks and
    other-language blocks are ignored.
    """
    text = md_path.read_text(encoding="utf-8")
    pattern = re.compile(r"^```python\s*\n(.*?)^```\s*$", re.DOTALL | re.MULTILINE)
    return [m.group(1) for m in pattern.finditer(text)]


def test_protocol_recipes_compile() -> None:
    """Each fenced ``python`` block in 05-protocol-recipes.md must:

    1. execute end-to-end under ``exec()`` without raising,
    2. construct at least one ``NetClassRouting`` instance, and
    3. that instance's ``effective_length_match_tolerance()`` must return
       a finite positive float (the recipes are about length-matching;
       a recipe that constructs a ``NetClassRouting`` but forgets to set
       ``length_match_tolerance_mm`` is broken).

    Mirrors ``test_diffpair_docs.test_protocol_recipes_compile``.
    """
    from kicad_tools.router.rules import NetClassRouting

    recipes_path = MATCH_GROUP_DOCS / "05-protocol-recipes.md"
    blocks = _read_python_code_blocks(recipes_path)
    assert len(blocks) >= 4, (
        f"Expected at least 4 protocol recipes (DDR / MIPI / HDMI / "
        f"address-bus), got {len(blocks)} in {recipes_path}"
    )

    for i, src in enumerate(blocks):
        ns: dict[str, object] = {}
        try:
            exec(compile(src, f"{recipes_path}:block-{i}", "exec"), ns)
        except Exception as e:  # noqa: BLE001
            pytest.fail(f"Recipe block {i} failed to exec: {e!r}\n---\n{src}")

        # At least one NetClassRouting must be constructed in the block.
        ncrs = [v for v in ns.values() if isinstance(v, NetClassRouting)]
        assert ncrs, f"Recipe block {i} produced no NetClassRouting instance:\n---\n{src}"

        # Each NetClassRouting's effective_length_match_tolerance() must
        # return a finite positive float.  This catches the failure mode
        # where a recipe builds a NetClassRouting but forgets to set
        # length_match_tolerance_mm.
        for ncr in ncrs:
            tol = ncr.effective_length_match_tolerance()
            assert isinstance(tol, float) and math.isfinite(tol) and tol > 0.0, (
                f"effective_length_match_tolerance() on {ncr.name!r} in "
                f"block {i} returned non-finite/non-positive: {tol!r}"
            )


def _match_group_rule_ids_from_violation_alias_table() -> set[str]:
    """Return every ``match_group_``-prefixed rule_id alias registered in
    ``ViolationType``.

    The enum values are the canonical CLI surface — adding a rule_id there
    without doc coverage is the drift this test exists to prevent.
    """
    from kicad_tools.drc.violation import ViolationType

    public_rule_ids: set[str] = set()
    for vt in ViolationType:
        v = vt.value
        if isinstance(v, str) and v.startswith("match_group_"):
            public_rule_ids.add(v)
    return public_rule_ids


def _rule_ids_documented_in_drc_guide() -> set[str]:
    """Return every ``match_group_*`` rule_id referenced in 06-drc-rule.md.

    A rule_id is recognized either as inline-code like
    ``` `match_group_length_skew` ``` or as a ``--rules=<id>`` flag value.
    """
    doc_path = MATCH_GROUP_DOCS / "06-drc-rule.md"
    text = doc_path.read_text(encoding="utf-8")
    found = set(re.findall(r"`(match_group_[a-z_]+)`", text))
    for m in re.finditer(r"--rules=([A-Za-z0-9_,]+)", text):
        for token in m.group(1).split(","):
            token = token.strip()
            if token.startswith("match_group_"):
                found.add(token)
    return found


def test_rule_ids_match_code() -> None:
    """Bidirectional doc/code coupling for match-group DRC rule_ids.

    * Forward: every ``match_group_*`` rule_id in the ``ViolationType``
      enum must appear in ``06-drc-rule.md``.  Prevents a new DRC rule
      from being added to the enum (and exposed on the CLI) without
      documentation.
    * Reverse: every match-group rule_id mentioned in
      ``06-drc-rule.md`` must exist in ``ViolationType``.  Prevents
      docs from advertising a fictional CLI flag.
    """
    code_ids = _match_group_rule_ids_from_violation_alias_table()
    doc_ids = _rule_ids_documented_in_drc_guide()

    missing_from_doc = code_ids - doc_ids
    assert not missing_from_doc, (
        f"rule_id(s) registered in ViolationType but not documented in "
        f"06-drc-rule.md: {sorted(missing_from_doc)}.  Doc coverage is "
        f"required before a rule_id can ship on the CLI."
    )

    missing_from_code = doc_ids - code_ids
    assert not missing_from_code, (
        f"rule_id(s) documented in 06-drc-rule.md but not present in "
        f"ViolationType: {sorted(missing_from_code)}.  Either remove "
        f"the doc reference or add the rule to the enum."
    )


def test_cascade_safety_constants_match_code() -> None:
    """Cascade-safety constants in 04-cascade-safety.md match source.

    Three constants in ``router/match_group_tuning.py`` govern when the
    tuner gives up.  The doc must quote them verbatim with their current
    values, so a future PR that tightens the budget cannot silently
    leave docs stale.
    """
    from kicad_tools.router.match_group_tuning import (
        MAX_INSERTS_PER_GROUP_MEMBER_LARGE,
        MAX_INSERTS_PER_GROUP_MEMBER_SMALL,
        MAX_TOTAL_INSERTS_PER_GROUP,
    )

    text = (MATCH_GROUP_DOCS / "04-cascade-safety.md").read_text(encoding="utf-8")
    expected = [
        f"MAX_INSERTS_PER_GROUP_MEMBER_SMALL={MAX_INSERTS_PER_GROUP_MEMBER_SMALL}",
        f"MAX_INSERTS_PER_GROUP_MEMBER_LARGE={MAX_INSERTS_PER_GROUP_MEMBER_LARGE}",
        f"MAX_TOTAL_INSERTS_PER_GROUP={MAX_TOTAL_INSERTS_PER_GROUP}",
    ]
    for token in expected:
        assert token in text, (
            f"Cascade-safety doc 04-cascade-safety.md is missing the "
            f"literal token {token!r}.  When match_group_tuning.py "
            f"updates a constant, this doc must be updated in the same PR."
        )


def test_no_stale_api_references() -> None:
    """``docs/guides/routing.md`` does not reference dead match-group APIs.

    Currently we don't have a known list of stale tokens for match
    groups (the API has only ever been documented under
    ``docs/guides/match-groups/``).  This test is a placeholder that
    asserts ``routing.md`` references the match-groups guide directory
    (the cross-link must exist) so future cleanups can't accidentally
    delete it.
    """
    text = ROUTING_GUIDE.read_text(encoding="utf-8")
    assert "match-groups/" in text, (
        f"{ROUTING_GUIDE} no longer references the match-groups guide "
        f"directory.  Add the cross-link back; the routing guide must "
        f"point users to docs/guides/match-groups/."
    )


def test_no_line_number_citations() -> None:
    """No guide cites source code by ``<file>.py:NNN`` line number.

    Line numbers rot on every refactor and nothing else in the suite
    notices.  Issue #4749 fixed the ``rules.py`` subset; issue #4764 found
    20 of 32 remaining citations wrong, including one off by 8,569 lines.
    Cite ``symbol`` + ``path/to/file.py`` instead — that survives a
    refactor and ``test_cited_symbols_exist`` can verify it.
    """
    offenders: list[str] = []
    for path in sorted(MATCH_GROUP_DOCS.glob("*.md")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if LINE_CITATION_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Line-number source citations found in the match-group guides:\n  "
        + "\n  ".join(offenders)
        + "\n\nReplace each with a symbol anchor — e.g. "
        "`_gather_explicit_groups` in "
        "`src/kicad_tools/router/match_group_detection.py` — and add the "
        "(guide, symbol, source file) row to CITED_SYMBOLS."
    )


def test_cited_symbols_exist() -> None:
    """Every symbol the match-group guides anchor on exists where they say.

    Checked in both directions per ``CITED_SYMBOLS`` row: the symbol must
    still be present in the guide (a doc rewrite cannot silently drop the
    anchor and leave a bare, useless file path) and in the source file the
    guide names (a source-side rename must fail here rather than rot).

    Negative control for this test: change ``_gather_explicit_groups`` back
    to ``_collect_explicit_match_groups`` in ``03-group-of-pairs.md`` and
    this test goes red on the "guide no longer mentions" assertion.
    """
    for guide_name, symbol, source_rel in CITED_SYMBOLS:
        # Whole-word match so a near-miss rename (``detect_match_groupsz``)
        # cannot satisfy the check by substring.
        word = re.compile(rf"\b{re.escape(symbol)}\b")

        guide_path = MATCH_GROUP_DOCS / guide_name
        assert guide_path.is_file(), f"CITED_SYMBOLS names a missing guide: {guide_path}"
        guide_text = guide_path.read_text(encoding="utf-8")
        assert word.search(guide_text), (
            f"{guide_path.relative_to(REPO_ROOT)} no longer mentions the "
            f"symbol {symbol!r}.  Either restore the anchor or drop its row "
            f"from CITED_SYMBOLS in {Path(__file__).name}."
        )

        source_path = REPO_ROOT / source_rel
        assert source_path.is_file(), (
            f"{guide_path.relative_to(REPO_ROOT)} cites {source_rel}, which "
            f"does not exist.  Update the guide and CITED_SYMBOLS."
        )
        assert word.search(source_path.read_text(encoding="utf-8")), (
            f"{guide_path.relative_to(REPO_ROOT)} cites symbol {symbol!r} in "
            f"{source_rel}, but that name no longer appears in the file.  "
            f"It was probably renamed — update the guide (and this row) to "
            f"the live name."
        )


def test_all_guides_present() -> None:
    """All seven guides + README exist under ``docs/guides/match-groups/``."""
    expected = [
        "README.md",
        "01-declaring-groups.md",
        "02-reference-selection.md",
        "03-group-of-pairs.md",
        "04-cascade-safety.md",
        "05-protocol-recipes.md",
        "06-drc-rule.md",
        "07-cli-and-sidecar.md",
    ]
    for name in expected:
        path = MATCH_GROUP_DOCS / name
        assert path.is_file(), f"Missing required guide: {path}"


def test_guide_length_caps() -> None:
    """Each guide ≤ 100 lines; README ≤ 50 lines.

    Enforces the epic's "don't create monster doc files" rule and the
    curator's tightened acceptance criterion.  Counted as
    newline-delimited lines (not "lines excluding code blocks") because
    the former is what a reader actually scrolls through.
    """
    for path in sorted(MATCH_GROUP_DOCS.glob("*.md")):
        limit = 50 if path.name == "README.md" else 100
        n_lines = sum(1 for _ in path.open(encoding="utf-8"))
        assert n_lines <= limit, f"{path} has {n_lines} lines, exceeds cap of {limit}"
