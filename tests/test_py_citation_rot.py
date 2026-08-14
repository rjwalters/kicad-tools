"""Line-citation / symbol-anchor guard for Python comments and docstrings.

``tests/test_docs_source_citations.py`` (issue #4764 / PR #4774) polices
the *markdown* documentation surface.  The identical rot class lives
inside Python comments and docstrings: a ``<file>.py:NNN`` citation is
correct on the day it is written and silently wrong after the next
insertion above it.  Issue #4796 measured every sampled citation in the
board scripts as stale — one of them (``escape.py:3303-3304``) pointed at
ring-distance code with no relationship at all to the QFN escape logic
the comment described.

**Fixed allowlist, not a glob.**  ``GUARDED_PY_FILES`` lists exact
repo-relative paths — the ten files issue #4796 cleaned.  A glob over
``src/`` would go red immediately: ~85 further ``.py:NNN`` citations
across 29 other ``src/`` files (``router/core.py``, ``router/escape.py``,
``cli/route_cmd.py``, ``validate/**`` …) are *deliberately* left alone by
that issue — those files are 5,000–18,000 lines and churn on nearly every
routing PR, so per-citation verification is a materially larger body of
work tracked as a separate follow-up.  Add rows here as that follow-up
lands; do not swap the tuple for a glob until the whole tree is clean.

``tests/**`` is excluded by construction (it asserts real line numbers),
as are the forensic trees the markdown guard documents.

Issue: #4796.
"""

from __future__ import annotations

import io
import re
import tokenize
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Exact repo-relative paths.  See the module docstring for why this is a
# fixed allowlist rather than a glob.
GUARDED_PY_FILES: tuple[str, ...] = (
    "src/kicad_tools/router/match_group_length.py",
    "boards/00-simple-led/generate_design.py",
    "boards/01-voltage-divider/generate_design.py",
    "boards/02-charlieplex-led/generate_design.py",
    "boards/03-usb-joystick/generate_design.py",
    "boards/03-usb-joystick/route_demo.py",
    "boards/04-stm32-devboard/generate_design.py",
    "boards/05-bldc-motor-controller/design.py",
    "boards/06-diffpair-test/generate_design.py",
    "boards/07-matchgroup-test/generate_design.py",
)

# A ``<file>.py:NNN`` citation.  Same pattern the markdown guards use.
LINE_CITATION_RE = re.compile(r"\.py:\d+")

# Source anchors the guarded Python files make, as
# ``(citing file, anchor, cited source file)`` — all repo-relative.
#
# Checked in BOTH directions per row: the anchor must still appear in the
# citing file (a rewrite cannot silently drop it back to a bare path) and
# in the source file it names (a rename goes red here instead of rotting).
#
# The regression this table exists for is PR #4774's first pass, which
# replaced rotten line numbers with two symbols that had never existed
# (``Footprint.to_sexp``, ``build_parser``).  A phantom symbol is worse
# than a stale line number because it *looks* verifiable.
CITED_SYMBOLS: tuple[tuple[str, str, str], ...] = (
    # src/kicad_tools/router/match_group_length.py — reuse/provenance notes
    (
        "src/kicad_tools/router/match_group_length.py",
        "tune_match_group",
        "src/kicad_tools/router/optimizer/serpentine.py",
    ),
    (
        "src/kicad_tools/router/match_group_length.py",
        "calculate_route_length",
        "src/kicad_tools/router/length.py",
    ),
    (
        "src/kicad_tools/router/match_group_length.py",
        "match_groups",
        "src/kicad_tools/router/length.py",
    ),
    (
        "src/kicad_tools/router/match_group_length.py",
        "measure_net_from_pcb",
        "src/kicad_tools/router/diffpair_length.py",
    ),
    (
        "src/kicad_tools/router/match_group_length.py",
        "_via_length",
        "src/kicad_tools/router/diffpair_length.py",
    ),
    (
        "src/kicad_tools/router/match_group_length.py",
        "_measure_route",
        "src/kicad_tools/router/diffpair_length.py",
    ),
    (
        "src/kicad_tools/router/match_group_length.py",
        "DetectionSource",
        "src/kicad_tools/router/diffpair_detection.py",
    ),
    # The five board scripts that mirror board 03's ``route_success``
    # fast-fail gate, which lives immediately after ``route_pcb``.
    (
        "boards/00-simple-led/generate_design.py",
        "route_pcb",
        "boards/03-usb-joystick/generate_design.py",
    ),
    (
        "boards/01-voltage-divider/generate_design.py",
        "route_pcb",
        "boards/03-usb-joystick/generate_design.py",
    ),
    (
        "boards/02-charlieplex-led/generate_design.py",
        "route_pcb",
        "boards/03-usb-joystick/generate_design.py",
    ),
    (
        "boards/04-stm32-devboard/generate_design.py",
        "route_pcb",
        "boards/03-usb-joystick/generate_design.py",
    ),
    (
        "boards/06-diffpair-test/generate_design.py",
        "route_pcb",
        "boards/03-usb-joystick/generate_design.py",
    ),
    # boards/03-usb-joystick — the build-step entry point and board 05's
    # zone-fill precedent.
    (
        "boards/03-usb-joystick/route_demo.py",
        "_run_step_route",
        "src/kicad_tools/cli/build_cmd.py",
    ),
    (
        "boards/03-usb-joystick/generate_design.py",
        "fill_zones_in_routed_pcb",
        "boards/05-bldc-motor-controller/design.py",
    ),
    # boards/04 — board 05's PR #3004 power-symbol / PWR_FLAG rail bridging.
    (
        "boards/04-stm32-devboard/generate_design.py",
        "create_bldc_controller",
        "boards/05-bldc-motor-controller/design.py",
    ),
    # boards/05 — self-citations (they drift apart on an insertion above
    # one but not the other, so they need anchors too).
    (
        "boards/05-bldc-motor-controller/design.py",
        "create_bldc_pcb",
        "boards/05-bldc-motor-controller/design.py",
    ),
    (
        "boards/05-bldc-motor-controller/design.py",
        "generate_diode_sma",
        "boards/05-bldc-motor-controller/design.py",
    ),
    (
        "boards/05-bldc-motor-controller/design.py",
        "route_pcb",
        "boards/05-bldc-motor-controller/design.py",
    ),
    (
        "boards/05-bldc-motor-controller/design.py",
        "validate_grid_resolution",
        "src/kicad_tools/router/io.py",
    ),
    # boards/06 — escape widths, neck-down, per-net-timeout forwarding and
    # the DRC nudge call sites.
    (
        "boards/06-diffpair-test/generate_design.py",
        "_create_fine_pitch_row_escapes",
        "src/kicad_tools/router/escape.py",
    ),
    (
        "boards/06-diffpair-test/generate_design.py",
        "get_neck_down_width",
        "src/kicad_tools/router/rules.py",
    ),
    (
        "boards/06-diffpair-test/generate_design.py",
        'getattr(args, "per_net_timeout", None) or None',
        "src/kicad_tools/cli/route_cmd.py",
    ),
    (
        "boards/06-diffpair-test/generate_design.py",
        "route_with_layer_escalation",
        "src/kicad_tools/cli/route_cmd.py",
    ),
    (
        "boards/06-diffpair-test/generate_design.py",
        "route_with_rule_relaxation",
        "src/kicad_tools/cli/route_cmd.py",
    ),
    (
        "boards/06-diffpair-test/generate_design.py",
        "route_with_combined_escalation",
        "src/kicad_tools/cli/route_cmd.py",
    ),
    (
        "boards/06-diffpair-test/generate_design.py",
        "_main_impl",
        "src/kicad_tools/cli/route_cmd.py",
    ),
    # boards/07 — the seed path, the CI gate's sidecar probe and the
    # coupled pathfinder pre-pass.
    (
        "boards/07-matchgroup-test/generate_design.py",
        "random.seed(args.seed)",
        "src/kicad_tools/cli/route_cmd.py",
    ),
    (
        "boards/07-matchgroup-test/generate_design.py",
        "find_net_class_map_sidecar",
        "scripts/ci/check_matchgroup_coverage.py",
    ),
    (
        "boards/07-matchgroup-test/generate_design.py",
        "CoupledPathfinder",
        "src/kicad_tools/router/diffpair_routing.py",
    ),
    (
        "boards/07-matchgroup-test/generate_design.py",
        "min_spacing_cells",
        "src/kicad_tools/router/diffpair_routing.py",
    ),
    (
        "boards/07-matchgroup-test/generate_design.py",
        "route_all_with_diffpairs",
        "src/kicad_tools/router/core.py",
    ),
)


def _anchor_re(anchor: str) -> re.Pattern[str]:
    """Whole-token match for ``anchor``.

    Lookarounds rather than ``\\b`` so a non-identifier anchor (a short
    verbatim phrase such as ``getattr(args, "per_net_timeout", None) or
    None``) is matched as reliably as an identifier one.  A near-miss
    rename (``_run_step_routeX``) still cannot satisfy the check by
    substring.
    """
    return re.compile(rf"(?<![0-9A-Za-z_]){re.escape(anchor)}(?![0-9A-Za-z_])")


def _prose_lines(path: Path) -> list[tuple[int, str]]:
    """Return ``(lineno, text)`` for every comment / string token in ``path``.

    Only prose is policed: comments, docstrings and string literals.  Real
    code is exempt, so a future diagnostic that legitimately *computes* a
    ``file.py:lineno`` string cannot be forced into this guard.
    """
    source = path.read_text(encoding="utf-8")
    prose: list[tuple[int, str]] = []
    for token in tokenize.generate_tokens(io.StringIO(source).readline):
        if token.type in (tokenize.COMMENT, tokenize.STRING):
            for offset, text in enumerate(token.string.splitlines()):
                prose.append((token.start[0] + offset, text))
    return prose


def test_guarded_py_files_exist() -> None:
    """Every path in ``GUARDED_PY_FILES`` must exist.

    A guard pointed at a moved or deleted file polices nothing and can
    never fail.  Renaming a board script must update this tuple.
    """
    missing = [rel for rel in GUARDED_PY_FILES if not (REPO_ROOT / rel).is_file()]
    assert not missing, (
        f"GUARDED_PY_FILES names files that do not exist: {missing}.  Either "
        f"the file moved (update the path) or it is gone (drop the row) — a "
        f"dangling entry is silent zero coverage."
    )


def test_guarded_py_files_are_exact_paths() -> None:
    """``GUARDED_PY_FILES`` must not contain glob metacharacters.

    The allowlist is deliberately fixed: ~85 ``.py:NNN`` citations across
    29 other ``src/`` files are out of scope for issue #4796 and a glob
    would sweep them in and go red on merge.  See the module docstring.
    """
    globbed = [rel for rel in GUARDED_PY_FILES if any(ch in rel for ch in "*?[")]
    assert not globbed, (
        f"Glob patterns in GUARDED_PY_FILES: {globbed}.  List each file "
        f"explicitly; this allowlist grows one cleaned file at a time."
    )


def test_no_line_number_citations() -> None:
    """No guarded Python file cites source by ``<file>.py:NNN``.

    Line numbers rot on every insertion above them and nothing else in
    the suite notices.  Cite ``symbol`` + ``path/to/file.py`` instead —
    that survives a refactor and ``test_cited_symbols_exist`` can verify
    it.
    """
    offenders: list[str] = []
    for rel in GUARDED_PY_FILES:
        path = REPO_ROOT / rel
        if not path.is_file():
            continue
        for lineno, text in _prose_lines(path):
            if LINE_CITATION_RE.search(text):
                offenders.append(f"{rel}:{lineno}: {text.strip()}")

    assert not offenders, (
        "Line-number source citations found in the guarded Python files:\n  "
        + "\n  ".join(offenders)
        + "\n\nReplace each with a symbol anchor — e.g. `_run_step_route` in "
        "`src/kicad_tools/cli/build_cmd.py` — and add the (citing file, "
        "symbol, source file) row to CITED_SYMBOLS."
    )


def test_cited_symbols_exist() -> None:
    """Every anchor in ``CITED_SYMBOLS`` exists in both files of its row.

    Negative controls for this test:

    * rename ``tune_match_group`` in
      ``src/kicad_tools/router/optimizer/serpentine.py`` — the
      source-side assertion goes red;
    * drop ``_run_step_route`` from ``boards/03-usb-joystick/route_demo.py``
      — the citing-side assertion goes red.
    """
    for citing_rel, anchor, source_rel in CITED_SYMBOLS:
        pattern = _anchor_re(anchor)

        citing_path = REPO_ROOT / citing_rel
        assert citing_path.is_file(), f"CITED_SYMBOLS names a missing file: {citing_rel}"
        assert pattern.search(citing_path.read_text(encoding="utf-8")), (
            f"{citing_rel} no longer mentions {anchor!r}.  Either restore the "
            f"anchor or drop its row from CITED_SYMBOLS in {Path(__file__).name}."
        )

        source_path = REPO_ROOT / source_rel
        assert source_path.is_file(), (
            f"{citing_rel} cites {source_rel}, which does not exist.  Update "
            f"the citing comment and CITED_SYMBOLS."
        )
        assert pattern.search(source_path.read_text(encoding="utf-8")), (
            f"{citing_rel} cites {anchor!r} in {source_rel}, but that name no "
            f"longer appears in the file.  It was probably renamed — update "
            f"the comment (and this row) to the live name."
        )


def test_every_guarded_file_is_covered_by_cited_symbols() -> None:
    """Each guarded file must contribute at least one ``CITED_SYMBOLS`` row.

    ``test_no_line_number_citations`` alone is satisfiable by deleting the
    citation outright.  Requiring a symbol row per file keeps the
    cross-reference itself alive: the fix for rot is re-anchoring, not
    forgetting.
    """
    cited = {citing_rel for citing_rel, _, _ in CITED_SYMBOLS}
    uncovered = [rel for rel in GUARDED_PY_FILES if rel not in cited]
    assert not uncovered, (
        f"Guarded files with no CITED_SYMBOLS row: {uncovered}.  Add the "
        f"(citing file, symbol, source file) row for each cross-reference "
        f"the file makes, so a rename on either side goes red."
    )
