"""Sanity tests for the diff-pair user-documentation set.

Guards the doc/code coupling for ``docs/guides/diff-pairs/``:

* the protocol recipes in ``05-protocol-recipes.md`` are valid Python that
  produces ``NetClassRouting`` instances whose ``effective_*`` accessors
  return finite floats (i.e. the recipe actually sets the fields it claims to
  demonstrate);
* every diff-pair / impedance ``rule_id`` registered in
  ``ViolationType.from_string``'s alias table is documented in
  ``06-drc-rules.md`` (forward direction), and every rule_id documented in
  ``06-drc-rules.md`` exists in the alias table (reverse direction);
* the stale Router-method API references previously documented in
  ``docs/guides/routing.md`` (``router.auto_detect_diff_pairs``,
  ``router.route_diff_pairs``, ``router.add_diff_pair``,
  ``router.set_length_match``, ``router.enable_serpentine``, and the
  diff-pair-shaped ``router.route_net("USB_D")`` example) are gone;
* no guide cites source by ``<file>.py:NNN`` line number (issue #4764 —
  line numbers rot on every refactor; guides must anchor on symbols),
  and every symbol a guide anchors on still exists in the file it names.

Issue: #2659.  Epic: #2556 (Phase 4M).  Line-citation guard: #4764.
"""

from __future__ import annotations

import math
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
DIFFPAIR_DOCS = REPO_ROOT / "docs" / "guides" / "diff-pairs"
ROUTING_GUIDE = REPO_ROOT / "docs" / "guides" / "routing.md"

# Stale API tokens removed from docs/guides/routing.md by this issue.  These
# names never existed on ``Router``/``Autorouter`` in ``src/kicad_tools/``;
# they were placeholder docs for an API that was never implemented.
STALE_API_TOKENS = (
    "auto_detect_diff_pairs",
    "route_diff_pairs",
    "add_diff_pair",
    "set_length_match",
    "enable_serpentine",
)

# A ``<file>.py:NNN`` citation anywhere in a guide.  Issue #4764 measured 20
# of 32 such citations rotten (62.5%) across the diff-pair / match-group
# guide trees, one of them off by 8,569 lines.  ``core.py`` is ~19k lines and
# ``route_cmd.py`` ~15k, so line citations into them have no useful
# half-life.  Guides must name the symbol and the file instead.
LINE_CITATION_RE = re.compile(r"\.py:\d+")

# Symbol anchors the diff-pair guides make, as
# ``(guide filename, symbol, source file relative to the repo root)``.
#
# Each row is checked in BOTH directions: the symbol must still appear in the
# guide (so a doc edit cannot silently drop or misspell the anchor) and in the
# source file the guide names (so a source-side rename goes red here instead
# of rotting quietly).  Seeded from the issue #4764 inventory.
CITED_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    # README — the entry point still carries a symbol+file anchor
    ("README.md", "NET_CLASS_HIGH_SPEED", "src/kicad_tools/router/rules.py"),
    # 01 — declaring pairs
    ("01-declaring-pairs.md", "diffpair_partner", "src/kicad_tools/router/rules.py"),
    (
        "01-declaring-pairs.md",
        "parse_differential_signal",
        "src/kicad_tools/router/diffpair.py",
    ),
    (
        "01-declaring-pairs.md",
        "is_single_ended_refused",
        "src/kicad_tools/router/diffpair.py",
    ),
    ("01-declaring-pairs.md", "should_engage_coupled", "src/kicad_tools/router/diffpair.py"),
    # 02 — clearance and net classes
    ("02-clearance-and-classes.md", "NetClassRouting", "src/kicad_tools/router/rules.py"),
    ("02-clearance-and-classes.md", "intra_pair_clearance", "src/kicad_tools/router/rules.py"),
    ("02-clearance-and-classes.md", "coupled_routing", "src/kicad_tools/router/rules.py"),
    ("02-clearance-and-classes.md", "NET_CLASS_HIGH_SPEED", "src/kicad_tools/router/rules.py"),
    (
        "02-clearance-and-classes.md",
        "effective_intra_pair_clearance",
        "src/kicad_tools/router/rules.py",
    ),
    # 03 — impedance-driven sizing
    ("03-impedance-and-sizing.md", "NetClassRouting", "src/kicad_tools/router/rules.py"),
    ("03-impedance-and-sizing.md", "target_diff_impedance", "src/kicad_tools/router/rules.py"),
    (
        "03-impedance-and-sizing.md",
        "impedance_tolerance_percent",
        "src/kicad_tools/router/rules.py",
    ),
    (
        "03-impedance-and-sizing.md",
        "StackupMismatchWarning",
        "src/kicad_tools/router/diffpair_impedance.py",
    ),
    (
        "03-impedance-and-sizing.md",
        "apply_impedance_driven_sizing",
        "src/kicad_tools/router/diffpair_impedance.py",
    ),
    # 04 — length matching
    (
        "04-length-matching.md",
        "DiffPairLengthTracker",
        "src/kicad_tools/router/diffpair_length.py",
    ),
    ("04-length-matching.md", "skew_tolerance_mm", "src/kicad_tools/router/rules.py"),
    ("04-length-matching.md", "effective_skew_tolerance", "src/kicad_tools/router/rules.py"),
    ("04-length-matching.md", "update_diffpair_skew", "src/kicad_tools/router/core.py"),
    # 06 — DRC rules
    (
        "06-drc-rules.md",
        "DiffPairClearanceIntraRule",
        "src/kicad_tools/validate/rules/diffpair_clearance_intra.py",
    ),
    (
        "06-drc-rules.md",
        "DiffPairRoutingContinuityRule",
        "src/kicad_tools/validate/rules/diffpair_routing_continuity.py",
    ),
    (
        "06-drc-rules.md",
        "DiffPairLengthSkewRule",
        "src/kicad_tools/validate/rules/diffpair_length_skew.py",
    ),
    ("06-drc-rules.md", "ImpedanceRule", "src/kicad_tools/validate/rules/impedance.py"),
    ("06-drc-rules.md", "ViolationType", "src/kicad_tools/drc/violation.py"),
    ("06-drc-rules.md", "from_string", "src/kicad_tools/drc/violation.py"),
    ("06-drc-rules.md", "_ALIASES", "src/kicad_tools/drc/violation.py"),
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
    3. that instance's ``effective_intra_pair_clearance()`` and
       ``effective_skew_tolerance()`` must return finite floats.

    This catches the failure mode where a recipe builds a ``NetClassRouting``
    but forgets to set the fields the recipe is *supposed* to demonstrate
    (e.g. omitting ``skew_tolerance_mm`` from the MIPI block).
    """
    from kicad_tools.router.rules import NetClassRouting

    recipes_path = DIFFPAIR_DOCS / "05-protocol-recipes.md"
    blocks = _read_python_code_blocks(recipes_path)
    assert len(blocks) >= 4, (
        f"Expected at least 4 protocol recipes (USB 2.0 / USB 3.0 / PCIe / MIPI), "
        f"got {len(blocks)} in {recipes_path}"
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

        # Each NetClassRouting's effective_* accessors must return finite floats.
        for ncr in ncrs:
            ipc = ncr.effective_intra_pair_clearance()
            assert isinstance(ipc, float) and math.isfinite(ipc) and ipc > 0.0, (
                f"effective_intra_pair_clearance() on {ncr.name!r} in block {i} "
                f"returned non-finite/non-positive: {ipc!r}"
            )
            skew = ncr.effective_skew_tolerance()
            assert isinstance(skew, float) and math.isfinite(skew) and skew > 0.0, (
                f"effective_skew_tolerance() on {ncr.name!r} in block {i} "
                f"returned non-finite/non-positive: {skew!r}"
            )


def _diffpair_rule_ids_from_violation_alias_table() -> set[str]:
    """Return every ``diffpair_``-prefixed (plus ``impedance``) rule_id alias
    registered in ``ViolationType.from_string``'s ``aliases`` table.

    The aliases table is the canonical CLI surface — adding a rule_id there
    without doc coverage is the drift this test exists to prevent.
    """
    from kicad_tools.drc.violation import ViolationType

    # Build the alias table the same way ``from_string`` does internally:
    # iterate the enum and collect every ``.value`` that participates in
    # diff-pair / impedance DRC.
    public_rule_ids: set[str] = set()
    for vt in ViolationType:
        v = vt.value
        if isinstance(v, str) and (v.startswith("diffpair_") or v == "impedance"):
            public_rule_ids.add(v)
    return public_rule_ids


def _rule_ids_documented_in_drc_guide() -> set[str]:
    """Return every rule_id referenced in the 06-drc-rules.md guide.

    A rule_id is recognized either as an inline-code reference like
    ``` `diffpair_clearance_intra` ``` or as a ``--rules=<id>`` flag value.
    """
    doc_path = DIFFPAIR_DOCS / "06-drc-rules.md"
    text = doc_path.read_text(encoding="utf-8")
    # Inline code: `diffpair_*` or `impedance`
    found = set(re.findall(r"`(diffpair_[a-z_]+|impedance)`", text))
    # CLI flag values: --rules=<id> (comma-separated also accepted)
    for m in re.finditer(r"--rules=([A-Za-z0-9_,]+)", text):
        for token in m.group(1).split(","):
            token = token.strip()
            if token.startswith("diffpair_") or token == "impedance":
                found.add(token)
    return found


def test_rule_ids_match_code() -> None:
    """Bidirectional doc/code coupling for diff-pair DRC rule_ids.

    * Forward: every ``diffpair_``/``impedance`` rule_id in the
      ``ViolationType`` enum must appear in ``06-drc-rules.md``.  Prevents a
      new DRC rule from being added to the enum (and exposed on the CLI)
      without documentation.
    * Reverse: every rule_id mentioned in ``06-drc-rules.md`` must exist in
      ``ViolationType``.  Prevents docs from advertising a fictional CLI flag.
    """
    code_ids = _diffpair_rule_ids_from_violation_alias_table()
    doc_ids = _rule_ids_documented_in_drc_guide()

    missing_from_doc = code_ids - doc_ids
    assert not missing_from_doc, (
        f"rule_id(s) registered in ViolationType but not documented in "
        f"06-drc-rules.md: {sorted(missing_from_doc)}.  "
        f"Doc coverage is required before a rule_id can ship on the CLI."
    )

    missing_from_code = doc_ids - code_ids
    assert not missing_from_code, (
        f"rule_id(s) documented in 06-drc-rules.md but not present in "
        f"ViolationType: {sorted(missing_from_code)}.  "
        f"Either remove the doc reference or add the rule to the enum."
    )


def test_no_stale_api_references() -> None:
    """``docs/guides/routing.md`` no longer references fictional Router methods.

    The original diff-pair section (lines 191-225, pre-issue) documented six
    Router methods that never existed in ``src/kicad_tools/``.  This issue
    deleted that section and replaced it with a 10-line summary that links
    into ``docs/guides/diff-pairs/``.
    """
    text = ROUTING_GUIDE.read_text(encoding="utf-8")
    found = [tok for tok in STALE_API_TOKENS if tok in text]
    assert not found, (
        f"Stale Router-method tokens still present in {ROUTING_GUIDE}: "
        f"{found}.  These APIs do not exist in src/kicad_tools/; the docs "
        f"must not reference them."
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
    for path in sorted(DIFFPAIR_DOCS.glob("*.md")):
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if LINE_CITATION_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Line-number source citations found in the diff-pair guides:\n  "
        + "\n  ".join(offenders)
        + "\n\nReplace each with a symbol anchor — e.g. "
        "`update_diffpair_skew` in `src/kicad_tools/router/core.py` — and "
        "add the (guide, symbol, source file) row to CITED_SYMBOLS."
    )


def test_cited_symbols_exist() -> None:
    """Every symbol the diff-pair guides anchor on exists where they say.

    Checked in both directions per ``CITED_SYMBOLS`` row: the symbol must
    still be present in the guide (a doc rewrite cannot silently drop the
    anchor and leave a bare, useless file path) and in the source file the
    guide names (a source-side rename must fail here rather than rot).
    """
    for guide_name, symbol, source_rel in CITED_SYMBOLS:
        # Whole-word match so a near-miss rename (``update_diffpair_skewz``)
        # cannot satisfy the check by substring.
        word = re.compile(rf"\b{re.escape(symbol)}\b")

        guide_path = DIFFPAIR_DOCS / guide_name
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
    """All six guides + README exist under ``docs/guides/diff-pairs/``."""
    expected = [
        "README.md",
        "01-declaring-pairs.md",
        "02-clearance-and-classes.md",
        "03-impedance-and-sizing.md",
        "04-length-matching.md",
        "05-protocol-recipes.md",
        "06-drc-rules.md",
    ]
    for name in expected:
        path = DIFFPAIR_DOCS / name
        assert path.is_file(), f"Missing required guide: {path}"


def test_guide_length_caps() -> None:
    """Each guide ≤ 100 lines; README ≤ 50 lines.

    Enforces the epic's "don't create monster doc files" rule and the
    curator's tightened acceptance criterion.  Counted as newline-delimited
    lines (not "lines excluding code blocks") because the former is what a
    reader actually scrolls through.
    """
    for path in sorted(DIFFPAIR_DOCS.glob("*.md")):
        limit = 50 if path.name == "README.md" else 100
        n_lines = sum(1 for _ in path.open(encoding="utf-8"))
        assert n_lines <= limit, f"{path} has {n_lines} lines, exceeds cap of {limit}"
