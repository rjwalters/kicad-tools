"""Contributor-guidance drift gate: CLAUDE.md and AGENTS.md (issue #5964).

``docs/contributing/agent-guidance.md`` is the single source of the repo's
project-specific contributor guidance. ``scripts/render_agent_guidance.py``
renders it into a managed block in both ``CLAUDE.md`` (Claude Code) and
``AGENTS.md`` (Codex CLI, opencode, other AGENTS.md-aware runtimes), so a
contributor sees the same guidance whichever harness they use.

Negative controls for this suite:

* hand-edit a line inside either rendered block -- ``test_committed_blocks_match_source``
  goes red and names the fix command;
* edit the source without re-rendering -- same test goes red;
* delete a marker -- ``test_markers_present_once_in_each_target`` goes red;
* add a harness tool name or ``CLAUDE.md`` pointer to the source body --
  ``test_source_body_is_harness_neutral`` goes red.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path

import pytest

from tests.test_agent_surface_neutrality import find_violations

REPO_ROOT = Path(__file__).resolve().parent.parent
RENDER_SCRIPT = REPO_ROOT / "scripts" / "render_agent_guidance.py"
FIX_COMMAND = "uv run python scripts/render_agent_guidance.py --write"

#: Third-party managed blocks that the installers (Repo Skills, Loom, Squad)
#: own. The renderer must leave each one present, once, outside our block.
THIRD_PARTY_BLOCKS: dict[str, tuple[str, ...]] = {
    "CLAUDE.md": ("REPO-SKILLS", "LOOM ORCHESTRATION", "SQUAD"),
    "AGENTS.md": ("LOOM ORCHESTRATION (AGENTS)", "SQUAD"),
}

#: The project-specific sections the guidance must carry (issue #5964 scope).
REQUIRED_SECTIONS = (
    "## Routing performance: build the C++ backend first",
    "## Type checks: distrust a local mypy error outside your diff",
    "## Slow single-test runs: check coverage overhead before suspecting a hang",
    '## "Have we already evaluated X?" -- ask the ecosystem registry',
    "## Changelog: every user-visible PR adds a `changelog.d/` fragment",
    "## Releasing",
)


@pytest.fixture(scope="module")
def render():
    """Import ``scripts/render_agent_guidance.py`` as a module."""
    spec = importlib.util.spec_from_file_location("render_agent_guidance", RENDER_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["render_agent_guidance"] = module
    spec.loader.exec_module(module)
    return module


def _block(render, text: str) -> str:
    begin = text.index(render.BEGIN_MARKER)
    end = text.index(render.END_MARKER) + len(render.END_MARKER)
    return text[begin:end]


def test_source_header_documents_both_modes(render) -> None:
    header = render.SOURCE.read_text(encoding="utf-8").split(render.BODY_MARKER)[0]
    assert FIX_COMMAND in header
    assert "uv run python scripts/render_agent_guidance.py " in header.replace(FIX_COMMAND, "")


@pytest.mark.parametrize("name", ["CLAUDE.md", "AGENTS.md"])
def test_markers_present_once_in_each_target(render, name: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    assert text.count(render.BEGIN_MARKER) == 1, f"{name}: re-run {FIX_COMMAND}"
    assert text.count(render.END_MARKER) == 1, f"{name}: re-run {FIX_COMMAND}"
    assert text.index(render.BEGIN_MARKER) < text.index(render.END_MARKER)


def test_committed_blocks_match_source(render) -> None:
    """Both rendered blocks equal a fresh render of the source."""
    drift = render.find_drift()
    stale = [target.name for target, _, _ in drift]
    assert not stale, (
        f"contributor-guidance block out of date in {', '.join(stale)}. Edit "
        f"docs/contributing/agent-guidance.md (never the rendered block), then run: "
        f"{FIX_COMMAND}"
    )


def test_both_targets_carry_identical_guidance(render) -> None:
    blocks = {
        name: _block(render, (REPO_ROOT / name).read_text(encoding="utf-8"))
        for name in ("CLAUDE.md", "AGENTS.md")
    }
    assert blocks["CLAUDE.md"] == blocks["AGENTS.md"]
    for heading in REQUIRED_SECTIONS:
        assert heading in blocks["AGENTS.md"], f"AGENTS.md is missing {heading!r}"


@pytest.mark.parametrize("name", sorted(THIRD_PARTY_BLOCKS))
def test_third_party_blocks_survive_outside_ours(render, name: str) -> None:
    text = (REPO_ROOT / name).read_text(encoding="utf-8")
    ours = _block(render, text)
    for label in THIRD_PARTY_BLOCKS[name]:
        begin, end = f"<!-- BEGIN {label} -->", f"<!-- END {label} -->"
        assert text.count(begin) == 1 and text.count(end) == 1, f"{name}: {label} block lost"
        assert begin not in ours, f"{name}: {label} block swallowed by the guidance block"


def test_project_guidance_lives_only_in_the_block(render) -> None:
    """No hand-written project section is left in CLAUDE.md outside the block."""
    text = (REPO_ROOT / "CLAUDE.md").read_text(encoding="utf-8")
    outside = text.replace(_block(render, text), "")
    for heading in REQUIRED_SECTIONS:
        assert heading not in outside, f"CLAUDE.md has {heading!r} outside the managed block"


def test_source_body_is_harness_neutral(render) -> None:
    """The #5954 neutrality lint applies to the shared guidance body."""
    body = render.source_body(render.SOURCE.read_text(encoding="utf-8"))
    assert find_violations("agent-guidance.md", body) == []


# --- renderer unit behaviour -------------------------------------------------


def test_write_replaces_only_the_block(render, tmp_path: Path) -> None:
    block = render.render_block("## New\n\nbody")
    target = tmp_path / "AGENTS.md"
    original = (
        "<!-- BEGIN SQUAD -->\nsquad\n<!-- END SQUAD -->\n\n"
        f"{render.BEGIN_MARKER}\nhand edit\n{render.END_MARKER}\ntail\n"
    )
    updated = render.apply_block(original, block, target)
    assert updated == f"<!-- BEGIN SQUAD -->\nsquad\n<!-- END SQUAD -->\n\n{block}\ntail\n"


def test_missing_block_is_prepended(render, tmp_path: Path) -> None:
    block = render.render_block("## New\n\nbody")
    updated = render.apply_block("<!-- BEGIN SQUAD -->\n<!-- END SQUAD -->\n", block, tmp_path)
    assert updated.startswith(block + "\n\n<!-- BEGIN SQUAD -->")


@pytest.mark.parametrize(
    "text",
    [
        "<!-- BEGIN KCT CONTRIBUTOR GUIDANCE -->\nno end\n",
        "<!-- END KCT CONTRIBUTOR GUIDANCE -->\n<!-- BEGIN KCT CONTRIBUTOR GUIDANCE -->\n",
    ],
)
def test_malformed_markers_raise(render, tmp_path: Path, text: str) -> None:
    with pytest.raises(render.RenderError):
        render.apply_block(text, render.render_block("x"), tmp_path / "CLAUDE.md")


@pytest.mark.parametrize(
    "marker",
    [
        "<!-- BEGIN KCT CONTRIBUTOR GUIDANCE -->",
        "<!-- END KCT CONTRIBUTOR GUIDANCE -->",
        "<!-- BEGIN SQUAD -->",
        "  <!-- END LOOM ORCHESTRATION -->",
    ],
)
def test_source_body_rejects_managed_markers(render, tmp_path: Path, marker: str) -> None:
    text = f"header\n{render.BODY_MARKER}\n## Section\n\n{marker}\n"
    with pytest.raises(render.RenderError, match="marker"):
        render.source_body(text, tmp_path / "agent-guidance.md")


def test_check_mode_names_fix_command(render, tmp_path: Path, capsys, monkeypatch) -> None:
    source = tmp_path / "agent-guidance.md"
    source.write_text(f"header\n{render.BODY_MARKER}\n\n## Section\n\ntext\n", encoding="utf-8")
    target = tmp_path / "AGENTS.md"
    block = render.render_block("## Section\n\ntext")
    target.write_text(block.replace("text", "hand-edited") + "\n", encoding="utf-8")
    monkeypatch.setattr(render, "SOURCE", source)
    monkeypatch.setattr(render, "REPO_ROOT", tmp_path)
    monkeypatch.setattr(render, "TARGETS", (target,))

    assert render.main([]) == 1
    err = capsys.readouterr().err
    assert "AGENTS.md's contributor-guidance block is out of date" in err
    assert f"Re-run: {FIX_COMMAND}" in err

    assert render.main(["--write"]) == 0
    assert target.read_text(encoding="utf-8") == block + "\n"
    assert render.main([]) == 0
