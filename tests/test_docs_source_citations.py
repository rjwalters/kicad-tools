"""Line-citation / symbol-anchor guard for the reference + board docs.

``test_diffpair_docs.py`` and ``test_match_group_docs.py`` police the two
guide subtrees they own.  This module covers the rest of the
reader-facing documentation surface — the files issue #4764 also had to
fix but which no guard watched:

* ``docs/reference/*.md`` — ``api.md`` (footprint attribute round-trip)
  and ``cli.md`` (exit-code ladders) both cite source;
* ``docs/*.md`` — the top-level docs.  Added for issue #4831: every
  top-level doc *except* ``placement-pad-anchoring-audit.md`` already
  complied, and that one had rotted three times in eight days (86
  ``.py:NNN`` citations, 60+ resolving to the wrong symbol after PRs
  #4857/#4863/#4870 shifted the placement modules).  Each of those PRs
  appended a "line citations below predate this patch" disclaimer rather
  than fixing them — the exact silent-rot mode this module exists to make
  loud.  Blast radius of the glob was therefore one file;
* ``boards/*/README.md`` — board READMEs cite the CLI/build plumbing
  that produces their artifacts;
* ``docs/guides/*.md`` — the top-level guides (``routing.md``,
  ``drc-and-validation.md``, …).  Issue #4764's acceptance criterion is
  phrased over ``docs/guides/``, but the two per-subtree suites only
  reach ``docs/guides/diff-pairs/`` and ``docs/guides/match-groups/``.

The guide subtrees are included here too.  The overlap with the two
suites above is deliberate: this module is the single place that states
the whole policed documentation surface, so extending it cannot be
forgotten when a new doc tree appears.

**Deliberately NOT policed — forensic trees, where the line number *is*
the evidence:** ``docs/investigations/**``, ``docs/research/**`` and
``boards/*/diagnostic-runs/**`` pin a claim to the code as it stood at
the time of the investigation; "re-anchor on a symbol" would destroy the
record.  Every glob below is non-recursive precisely so a forensic
subdirectory can never be swept in by accident.

Issue: #4764.
"""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Non-recursive globs, relative to the repo root.  See the module
# docstring for why recursion is banned here.
GUARDED_DOC_GLOBS: tuple[str, ...] = (
    "docs/*.md",
    "docs/guides/*.md",
    "docs/guides/diff-pairs/*.md",
    "docs/guides/match-groups/*.md",
    "docs/reference/*.md",
    "boards/*/README.md",
)

# A ``<file>.py:NNN`` citation.  Same pattern the two guide suites use.
LINE_CITATION_RE = re.compile(r"\.py:\d+")

# Source anchors these docs make, as
# ``(doc path relative to the repo root, anchor, source file relative to
# the repo root)``.
#
# Checked in BOTH directions per row: the anchor must still appear in the
# doc (a rewrite cannot silently drop it and leave a bare file path) and
# in the source file the doc names (a source-side rename goes red here
# instead of rotting quietly).
#
# ``Footprint.to_sexp`` is the regression this table exists for: issue
# #4764's first pass replaced a rotten ``pcb.py:459-462`` citation with a
# method that has never existed, which is strictly worse than the line
# number because a symbol anchor *looks* verifiable.  The real
# round-trip path is ``Footprint.__setattr__`` -> ``_sync_attr_node``.
CITED_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    # docs/reference/api.md — footprint attribute round-trip
    ("docs/reference/api.md", "Footprint", "src/kicad_tools/schema/pcb.py"),
    ("docs/reference/api.md", "__setattr__", "src/kicad_tools/schema/pcb.py"),
    ("docs/reference/api.md", "_sync_attr_node", "src/kicad_tools/schema/pcb.py"),
    ("docs/reference/api.md", "_attr_unknown_tokens", "src/kicad_tools/schema/pcb.py"),
    # docs/reference/cli.md — exit-code ladders
    ("docs/reference/cli.md", "_main_impl", "src/kicad_tools/cli/route_cmd.py"),
    ("docs/reference/cli.md", "exit codes:", "src/kicad_tools/cli/route_cmd.py"),
    ("docs/reference/cli.md", "Exit Codes:", "src/kicad_tools/cli/fleet_cmd.py"),
    # boards/03-usb-joystick/README.md — the three route entry points
    (
        "boards/03-usb-joystick/README.md",
        "_run_step_route",
        "src/kicad_tools/cli/build_cmd.py",
    ),
    (
        "boards/03-usb-joystick/README.md",
        "route_pcb",
        "boards/03-usb-joystick/generate_design.py",
    ),
    # docs/placement-pad-anchoring-audit.md — the centre-vs-pad inventory
    # (#4831).  These are the anchors the audit's load-bearing claims rest
    # on: the objective it says is centre-anchored by default, the pad-map
    # vehicle M1/M2 actually shipped, the estimator it reports as still
    # having no call site in src/, and the fidelity split M2 created.
    (
        "docs/placement-pad-anchoring-audit.md",
        "compute_wirelength",
        "src/kicad_tools/placement/cost.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "evaluate_placement",
        "src/kicad_tools/placement/cost.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "compute_domain_cohesion",
        "src/kicad_tools/placement/cost.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "compute_area",
        "src/kicad_tools/placement/cost.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "compute_hpwl",
        "src/kicad_tools/placement/wirelength.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "build_pad_position_map",
        "src/kicad_tools/placement/wirelength.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "compare_wirelength_estimators",
        "src/kicad_tools/placement/wirelength.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "compute_per_footprint_ratsnest",
        "src/kicad_tools/placement/wirelength.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "_evaluate_fidelity_0",
        "src/kicad_tools/placement/multi_fidelity.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "_evaluate_fidelity_1",
        "src/kicad_tools/placement/multi_fidelity.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "check_placement_drc",
        "src/kicad_tools/placement/drc.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "_validate_max_distance",
        "src/kicad_tools/optim/constraints.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "_create_cluster_springs",
        "src/kicad_tools/optim/placement.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "total_wire_length",
        "src/kicad_tools/optim/placement.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "_compute_net_anchor_weight",
        "src/kicad_tools/cli/optimize_placement_cmd.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "_auto_detect_anchored_refs",
        "src/kicad_tools/cli/route_cmd.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "_resolve_placement_feedback_anchors",
        "src/kicad_tools/cli/route_cmd.py",
    ),
    (
        "docs/placement-pad-anchoring-audit.md",
        "_evaluate_vector",
        "src/kicad_tools/mcp/tools/optimize_placement.py",
    ),
    # docs/hv-pairwise-softstart-proof.md — the #4507 code-side
    # re-verification.  The load-bearing claim is that the foreign-pad
    # predicate is defined once and is now consulted from the coupled
    # finish-gate loop too (the one call site PR #4911 fixed), so anchor
    # both the definition and the call site rather than a line number.
    (
        "docs/hv-pairwise-softstart-proof.md",
        "pairwise_pad_blocked",
        "src/kicad_tools/router/lattice/obstacles.py",
    ),
    (
        "docs/hv-pairwise-softstart-proof.md",
        "pairwise_pad_blocked",
        "src/kicad_tools/router/lattice/pathfinder.py",
    ),
    (
        "docs/hv-pairwise-softstart-proof.md",
        "_route_pair_impl",
        "src/kicad_tools/router/lattice/pathfinder.py",
    ),
)


def _anchor_re(anchor: str) -> re.Pattern[str]:
    """Whole-token match for ``anchor``.

    Lookarounds rather than ``\\b`` so a non-identifier anchor (``Exit
    Codes:``) is matched as reliably as an identifier one: ``\\b`` after a
    trailing ``:`` demands a following word character and would never
    match.  A near-miss rename (``_run_step_routeX``) still cannot
    satisfy the check by substring.
    """
    return re.compile(rf"(?<![0-9A-Za-z_]){re.escape(anchor)}(?![0-9A-Za-z_])")


def _guarded_docs() -> list[Path]:
    """Every markdown file covered by ``GUARDED_DOC_GLOBS``, deduplicated."""
    seen: dict[Path, None] = {}
    for pattern in GUARDED_DOC_GLOBS:
        for path in REPO_ROOT.glob(pattern):
            if path.is_file():
                seen[path] = None
    return sorted(seen)


def test_every_glob_matches_at_least_one_doc() -> None:
    """No glob in ``GUARDED_DOC_GLOBS`` may match zero files.

    A guard that silently polices an empty set cannot fail.  If a doc
    tree is renamed or removed, this goes red instead of the coverage
    quietly evaporating.
    """
    empty = [pattern for pattern in GUARDED_DOC_GLOBS if not list(REPO_ROOT.glob(pattern))]
    assert not empty, (
        f"GUARDED_DOC_GLOBS entries match no files: {empty}.  Either the "
        f"doc tree moved (update the glob) or it is gone (drop the glob) "
        f"— an unmatched glob is silent zero coverage."
    )


def test_no_line_number_citations() -> None:
    """No policed doc cites source by ``<file>.py:NNN`` line number.

    Line numbers rot on every refactor and nothing else in the suite
    notices.  Issue #4764 measured 20 of 32 such citations wrong, one of
    them off by 8,569 lines.  Cite ``symbol`` + ``path/to/file.py``
    instead — that survives a refactor and ``test_cited_symbols_exist``
    can verify it.

    Forensic trees are exempt by construction; see the module docstring.
    """
    offenders: list[str] = []
    for path in _guarded_docs():
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if LINE_CITATION_RE.search(line):
                offenders.append(f"{path.relative_to(REPO_ROOT)}:{lineno}: {line.strip()}")

    assert not offenders, (
        "Line-number source citations found in the policed docs:\n  "
        + "\n  ".join(offenders)
        + "\n\nReplace each with a symbol anchor — e.g. `_run_step_route` in "
        "`src/kicad_tools/cli/build_cmd.py` — and add the (doc, symbol, "
        "source file) row to CITED_SYMBOLS."
    )


def test_cited_symbols_exist() -> None:
    """Every anchor in ``CITED_SYMBOLS`` exists in the doc and in the source.

    Negative controls for this test:

    * rename ``_sync_attr_node`` in ``src/kicad_tools/schema/pcb.py`` —
      the source-side assertion goes red;
    * drop ``_run_step_route`` from ``boards/03-usb-joystick/README.md``
      — the doc-side assertion goes red.
    """
    for doc_rel, anchor, source_rel in CITED_SYMBOLS:
        pattern = _anchor_re(anchor)

        doc_path = REPO_ROOT / doc_rel
        assert doc_path.is_file(), f"CITED_SYMBOLS names a missing doc: {doc_rel}"
        assert pattern.search(doc_path.read_text(encoding="utf-8")), (
            f"{doc_rel} no longer mentions {anchor!r}.  Either restore the "
            f"anchor or drop its row from CITED_SYMBOLS in {Path(__file__).name}."
        )

        source_path = REPO_ROOT / source_rel
        assert source_path.is_file(), (
            f"{doc_rel} cites {source_rel}, which does not exist.  Update "
            f"the doc and CITED_SYMBOLS."
        )
        assert pattern.search(source_path.read_text(encoding="utf-8")), (
            f"{doc_rel} cites {anchor!r} in {source_rel}, but that name no "
            f"longer appears in the file.  It was probably renamed — update "
            f"the doc (and this row) to the live name."
        )


def test_every_guarded_doc_glob_is_non_recursive() -> None:
    """``GUARDED_DOC_GLOBS`` must not use ``**``.

    The forensic trees (``docs/investigations/``, ``docs/research/``,
    ``boards/*/diagnostic-runs/``) are excluded *by construction* — a
    recursive glob would sweep them in and demand that evidence be
    re-anchored on symbols, destroying the record.
    """
    recursive = [pattern for pattern in GUARDED_DOC_GLOBS if "**" in pattern]
    assert not recursive, (
        f"Recursive globs in GUARDED_DOC_GLOBS: {recursive}.  List each "
        f"directory explicitly instead; recursion would pull in the "
        f"forensic trees where line-number citations are the evidence."
    )
