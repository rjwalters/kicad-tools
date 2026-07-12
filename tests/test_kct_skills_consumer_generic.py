"""Consumer-generic acceptance gates for `.claude/commands/kct/` skills (issue #4057).

The `kct` skill namespace is vendored into arbitrary consumer PCB-design repos by
`scripts/install-kct.sh` (Epic #4054). Skills authored under #4057 must therefore be
*consumer-generic*: no repo-internal path literals, a user-supplied board path (not a
hardcoded board), no hardcoded fab-tier enum (defer to
``kicad_tools.manufacturers.get_manufacturer_ids()``), and no CI-workflow assumptions.

These tests encode the curated grep-based acceptance gates so they are enforced in CI,
not just checked once at authoring time. ``ee-review.md`` is intentionally excluded from
the path-literal gate: it is board-05-cited as its own worked example (#3995), predates
#4057, and is out of scope for this issue.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
KCT_DIR = REPO_ROOT / ".claude" / "commands" / "kct"

# Skills shipped by #4057 (ee-review.md predates this issue and is exempt from the
# path-literal gate; README.md is the index, not a skill body).
NEW_SKILLS = (
    "manufacturing-readiness",
    "board-recipe-scaffold",
    "layout-journal",
)

# The manufacturing-readiness skill is the one that must not enumerate fab tiers.
MFR_SKILL = "manufacturing-readiness"

# Gate 1: repo-internal path literals that would break a consumer install.
REPO_INTERNAL_RE = re.compile(
    r"(boards/0[0-9]-|hardware/board-0[0-9]|scripts/ci/board0[0-9]|chorus|softstart)"
)

# Gate 3: concrete fab-tier ids from get_manufacturer_ids() must not appear as a
# hardcoded enum in the manufacturing-readiness skill's instructions.
TIER_ID_RE = re.compile(r"\b(jlcpcb-tier1|jlcpcb|pcbway|oshpark|seeed|flashpcb)\b")

# Gate 4: a directive reference to CI workflow / repo-internal ci scripts. A prose
# disclaimer ("does not depend on scripts/ci/*") is explicitly compliant, so lines
# that negate are not violations.
CI_REF_RE = re.compile(r"(\.github/workflows|scripts/ci/)")
NEGATION_RE = re.compile(r"\b(not|never|no)\b|does not|doesn't", re.IGNORECASE)


def _skill_path(name: str) -> Path:
    p = KCT_DIR / f"{name}.md"
    assert p.exists(), f"expected skill file missing: {p}"
    return p


def _lines(name: str) -> list[str]:
    return _skill_path(name).read_text(encoding="utf-8").splitlines()


@pytest.mark.parametrize("skill", NEW_SKILLS)
def test_no_repo_internal_path_literals(skill: str) -> None:
    """Gate 1: no boards/0N-, hardware/board-0N, scripts/ci/board0N, chorus, softstart."""
    hits = [
        f"{skill}.md:{i}: {line}"
        for i, line in enumerate(_lines(skill), 1)
        if REPO_INTERNAL_RE.search(line)
    ]
    assert not hits, "repo-internal path literals found:\n" + "\n".join(hits)


@pytest.mark.parametrize("skill", NEW_SKILLS)
def test_board_path_is_a_parameter(skill: str) -> None:
    """Gate 2: each skill defines and uses a user-supplied <board-path> token."""
    text = _skill_path(skill).read_text(encoding="utf-8")
    assert "## Arguments" in text, f"{skill}.md missing an ## Arguments section"
    assert "`<board-path>`" in text, f"{skill}.md does not define a <board-path> token"
    # The token must appear outside the Arguments table too (used in the body).
    assert text.count("<board-path>") >= 2, (
        f"{skill}.md defines <board-path> but never references it in the body"
    )


def test_no_hardcoded_fab_tier_enum() -> None:
    """Gate 3: the mfr-readiness skill must not enumerate concrete fab-tier ids.

    Tier discovery must defer to kct's own registry
    (kicad_tools.manufacturers.get_manufacturer_ids() / kct check --help).
    """
    hits = [
        f"{MFR_SKILL}.md:{i}: {line}"
        for i, line in enumerate(_lines(MFR_SKILL), 1)
        if TIER_ID_RE.search(line)
    ]
    assert not hits, (
        "hardcoded fab-tier ids found (defer to get_manufacturer_ids() instead):\n"
        + "\n".join(hits)
    )


def test_mfr_skill_defers_to_registry() -> None:
    """Gate 3 (positive): the skill points at the authoritative tier source."""
    text = _skill_path(MFR_SKILL).read_text(encoding="utf-8")
    assert "get_manufacturer_ids" in text, (
        f"{MFR_SKILL}.md should reference kicad_tools.manufacturers.get_manufacturer_ids()"
    )
    assert "--help" in text, (
        f"{MFR_SKILL}.md should point at `kct check --help`/`kct export --help` for tiers"
    )


@pytest.mark.parametrize("skill", NEW_SKILLS)
def test_no_ci_workflow_assumptions(skill: str) -> None:
    """Gate 4: no directive reference to .github/workflows/* or scripts/ci/*.

    A disclaimer line that negates ("does not depend on scripts/ci/*") is compliant.
    """
    hits = [
        f"{skill}.md:{i}: {line}"
        for i, line in enumerate(_lines(skill), 1)
        if CI_REF_RE.search(line) and not NEGATION_RE.search(line)
    ]
    assert not hits, "CI-workflow assumption found:\n" + "\n".join(hits)


@pytest.mark.parametrize("skill", NEW_SKILLS)
def test_frontmatter_matches_house_style(skill: str) -> None:
    """Every skill carries the ee-review.md frontmatter block."""
    text = _skill_path(skill).read_text(encoding="utf-8")
    assert text.startswith("---\n"), f"{skill}.md missing YAML frontmatter"
    head = text.split("---", 2)[1]
    for key in ("name:", "invocation:", "suggestedModel:", "description:"):
        assert key in head, f"{skill}.md frontmatter missing `{key}`"
    assert f"invocation: /kct:{skill}" in head, f"{skill}.md invocation must be /kct:{skill}"


def test_mfr_skill_states_cross_gate_is_mandatory() -> None:
    """The load-bearing rule: kicad-cli pcb drc --refill-zones is NOT optional."""
    text = _skill_path(MFR_SKILL).read_text(encoding="utf-8")
    assert "kicad-cli pcb drc --refill-zones" in text, (
        "manufacturing-readiness must codify the --refill-zones cross-gate"
    )
    # The doc must assert the cross-gate's non-optionality somewhere.
    assert re.search(r"not\s+optional|NOT optional|not skippable", text), (
        "manufacturing-readiness must state the cross-gate is not optional"
    )


@pytest.mark.parametrize("skill", NEW_SKILLS)
def test_readme_indexes_every_new_skill(skill: str) -> None:
    """Gate: README table lists every new skill in the same table as ee-review."""
    readme = (KCT_DIR / "README.md").read_text(encoding="utf-8")
    assert f"/kct:{skill}" in readme, f"README.md does not index /kct:{skill}"


def test_manufacturer_registry_is_the_authoritative_source() -> None:
    """Sanity: the registry the skill defers to actually exists and is non-empty.

    This locks the skill's contract to the real API rather than a memorized enum.
    """
    from kicad_tools.manufacturers import get_manufacturer_ids

    ids = get_manufacturer_ids()
    assert ids, "get_manufacturer_ids() returned no tiers"
    # Whatever the tiers are, the mfr skill must NOT hardcode them — cross-check
    # that none of the current ids appear verbatim in the skill body.
    text = _skill_path(MFR_SKILL).read_text(encoding="utf-8")
    leaked = [tid for tid in ids if re.search(rf"\b{re.escape(tid)}\b", text)]
    assert not leaked, f"skill body leaked live tier ids: {leaked}"
