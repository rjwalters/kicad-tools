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
* each guide stays under the size cap (50 lines for README, 100 for the
  numbered guides) so we keep the "don't create monster doc files"
  invariant from Epic #2556 Phase 4M.

Issue: #2725.  Epic: #2661 (Phase 3M).  Mirrors ``test_diffpair_docs.py``.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MATCH_GROUP_DOCS = REPO_ROOT / "docs" / "guides" / "match-groups"
ROUTING_GUIDE = REPO_ROOT / "docs" / "guides" / "routing.md"


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
