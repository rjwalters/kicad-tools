"""Lint: agent-facing text stays harness-neutral (issue #5954, Epic #5952).

The ``kct`` skills (``.claude/commands/kct/*.md``) are rendered into several
agent harnesses, and the MCP tool descriptions reach whichever client launched
``kct mcp serve``. Neither may assume Claude Code. This test fails when any of
these patterns reappears in a skill body, a skill ``description``, or an MCP
tool / parameter description:

* a Claude Code tool name (```Read```, ```Bash```, ``TodoWrite``,
  ``AskUserQuestion``, "the Task tool", ...) or a subagent assumption;
* a hard-coded ``/kct:<name>`` invocation (write ``{{skill:<name>}}``);
* a ``CLAUDE.md`` / ``.claude/`` pointer, or the word "Claude" (write
  ``{{agent-guide}}`` / ``{{skill-file:<name>}}`` / ``{{skills-dir}}``);
* a ``boards/NN-*`` demo-board path (use a ``<board>`` placeholder).

The packaged agent primer behind ``kct agent-guide``
(``kicad_tools/agent_skills/AGENT_GUIDE.md``, issue #5960) is linted the same
way: it is printed verbatim to agents in every harness.

Exceptions
----------
1. **Marked notes.** A blockquote paragraph that opens with
   ``> **Claude Code only`` is exempt, but it must carry a fallback for other
   harnesses (the word "fallback" must appear in the note).
2. **ALLOWLIST.** For the rare case a marked note cannot express, add an exact
   ``(source, line substring)`` pair below. Stale entries fail the test, so the
   list cannot silently outlive the text it excuses.

The YAML frontmatter keys ``invocation`` and ``suggestedModel`` are Claude Code
dispatch metadata: the Codex renderer drops them, so they are not linted.
"""

from __future__ import annotations

import re
from collections.abc import Iterator
from pathlib import Path

import pytest

from kicad_tools.agent_surfaces import HARNESSES, PLACEHOLDER_RE, render

REPO_ROOT = Path(__file__).resolve().parents[1]
KCT_DIR = REPO_ROOT / ".claude" / "commands" / "kct"
SKILL_FILES = sorted(KCT_DIR.glob("*.md"))
GUIDE_FILE = REPO_ROOT / "src" / "kicad_tools" / "agent_skills" / "AGENT_GUIDE.md"
GUIDE_LABEL = "AGENT_GUIDE.md"

# Claude Code tool names. Bare English verbs ("Read the report") are fine, so the
# ambiguous names only count when backticked or followed by "tool"; names that
# are not English words count anywhere.
_AMBIGUOUS_TOOLS = r"Read|Write|Edit|Bash|Glob|Grep|Task|Agent|Skill|LS"
_UNAMBIGUOUS_TOOLS = (
    r"TodoWrite|TodoRead|AskUserQuestion|WebFetch|WebSearch|NotebookEdit|"
    r"NotebookRead|MultiEdit|ExitPlanMode|SlashCommand|BashOutput|KillShell"
)

FORBIDDEN: dict[str, re.Pattern[str]] = {
    "claude-tool-name": re.compile(
        rf"`(?:{_AMBIGUOUS_TOOLS}|{_UNAMBIGUOUS_TOOLS})`"
        rf"|\b(?:{_AMBIGUOUS_TOOLS})\s+tool\b"
        rf"|\b(?:{_UNAMBIGUOUS_TOOLS})\b"
    ),
    "subagent": re.compile(r"\bsub-?agents?\b", re.IGNORECASE),
    "kct-slash-invocation": re.compile(r"/kct:"),
    "claude-guide-or-dir": re.compile(r"CLAUDE\.md|\.claude/"),
    "claude-name": re.compile(r"\bClaude\b"),
    "demo-board-path": re.compile(r"\bboards/\d{2}-"),
}

# (source label, exact line substring) pairs excused from FORBIDDEN. Keep small.
ALLOWLIST: frozenset[tuple[str, str]] = frozenset()

MARKED_NOTE_RE = re.compile(r"^>\s*\*\*Claude Code only\b")


def _split_frontmatter(text: str) -> tuple[str, str]:
    if not text.startswith("---\n"):
        return "", text
    _, frontmatter, body = text.split("---", 2)
    return frontmatter, body


def _paragraphs(text: str) -> Iterator[tuple[int, list[str]]]:
    """Yield (first line number, lines) for each blank-line-separated block."""
    block: list[str] = []
    start = 1
    for number, line in enumerate(text.splitlines(), 1):
        if line.strip():
            if not block:
                start = number
            block.append(line)
        elif block:
            yield start, block
            block = []
    if block:
        yield start, block


def find_violations(label: str, text: str) -> list[str]:
    """Return ``label:line: [rule] text`` for each forbidden hit outside a marked note."""
    hits: list[str] = []
    for start, block in _paragraphs(text):
        if MARKED_NOTE_RE.match(block[0]):
            continue
        for offset, line in enumerate(block):
            if any(src == label and snippet in line for src, snippet in ALLOWLIST):
                continue
            for rule, pattern in FORBIDDEN.items():
                if pattern.search(line):
                    hits.append(f"{label}:{start + offset}: [{rule}] {line.strip()}")
    return hits


def marked_notes(text: str) -> list[str]:
    return ["\n".join(block) for _, block in _paragraphs(text) if MARKED_NOTE_RE.match(block[0])]


def _skill_surfaces() -> Iterator[tuple[str, str]]:
    """Yield (label, linted text) for each skill: its description and its body."""
    for path in SKILL_FILES:
        frontmatter, body = _split_frontmatter(path.read_text(encoding="utf-8"))
        for line in frontmatter.splitlines():
            if line.startswith("description:"):
                yield f"{path.name}[description]", line
        yield path.name, body


def _description_strings(node: object) -> Iterator[str]:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "description" and isinstance(value, str):
                yield value
            else:
                yield from _description_strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _description_strings(item)


def _mcp_surfaces() -> Iterator[tuple[str, str]]:
    from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

    for name, spec in sorted(TOOL_REGISTRY.items()):
        yield f"mcp:{name}", spec.description
        for index, text in enumerate(_description_strings(spec.parameters)):
            yield f"mcp:{name}[param {index}]", text


# --- the lint -----------------------------------------------------------------


def test_skill_sources_exist() -> None:
    assert len(SKILL_FILES) >= 2, f"no kct skills found under {KCT_DIR}"


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_skill_text_is_harness_neutral(path: Path) -> None:
    hits = [
        hit
        for label, text in _skill_surfaces()
        if label.split("[", 1)[0] == path.name
        for hit in find_violations(label, text)
    ]
    assert not hits, (
        "Claude-only text in a kct skill. Use a placeholder from "
        "kicad_tools.agent_surfaces, neutral wording, or a marked "
        "'> **Claude Code only' note with a fallback:\n" + "\n".join(hits)
    )


def test_mcp_tool_descriptions_are_harness_neutral() -> None:
    surfaces = list(_mcp_surfaces())
    assert len(surfaces) > 20, "MCP tool registry looks empty; the lint would be vacuous"
    hits = [hit for label, text in surfaces for hit in find_violations(label, text)]
    assert not hits, "Claude-only text in MCP tool descriptions:\n" + "\n".join(hits)


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
def test_marked_claude_only_notes_carry_a_fallback(path: Path) -> None:
    for note in marked_notes(path.read_text(encoding="utf-8")):
        assert "fallback" in note.lower(), (
            f"{path.name}: a 'Claude Code only' note must give other harnesses a fallback:\n{note}"
        )


def _guide_surfaces() -> Iterator[tuple[str, str]]:
    yield GUIDE_LABEL, GUIDE_FILE.read_text(encoding="utf-8")


def test_agent_guide_is_harness_neutral() -> None:
    hits = [hit for label, text in _guide_surfaces() for hit in find_violations(label, text)]
    assert not hits, (
        "Claude-only text in the agent guide. Put harness-specific examples in "
        "its <!-- per-harness --> block, using kicad_tools.agent_surfaces "
        "placeholders:\n" + "\n".join(hits)
    )


def test_agent_guide_marked_notes_carry_a_fallback() -> None:
    for note in marked_notes(GUIDE_FILE.read_text(encoding="utf-8")):
        assert "fallback" in note.lower(), f"{GUIDE_LABEL}: note without a fallback:\n{note}"


@pytest.mark.parametrize("harness", [*HARNESSES, None])
def test_agent_guide_placeholders_render(harness: str | None) -> None:
    from kicad_tools.agent_skills.guide import render_guide

    rendered = render_guide(harness, GUIDE_FILE.read_text(encoding="utf-8"))
    assert not PLACEHOLDER_RE.search(rendered)
    assert "<!-- per-harness -->" not in rendered


def test_allowlist_has_no_stale_entries() -> None:
    surfaces = dict(_skill_surfaces()) | dict(_mcp_surfaces()) | dict(_guide_surfaces())
    for label, snippet in ALLOWLIST:
        assert label in surfaces and snippet in surfaces[label], (
            f"stale ALLOWLIST entry {(label, snippet)!r}: the text it excuses is gone"
        )


@pytest.mark.parametrize("path", SKILL_FILES, ids=lambda p: p.name)
@pytest.mark.parametrize("harness", HARNESSES)
def test_every_placeholder_renders(path: Path, harness: str) -> None:
    """Every ``{{...}}`` token is in the vocabulary, and none survive rendering."""
    rendered = render(path.read_text(encoding="utf-8"), harness)
    assert not PLACEHOLDER_RE.search(rendered)


# --- the lint catches what it claims to catch -----------------------------------


@pytest.mark.parametrize(
    ("line", "rule"),
    [
        ("Use the `Read` tool to open the report.", "claude-tool-name"),
        ("Open it with the Bash tool.", "claude-tool-name"),
        ("Track progress with TodoWrite.", "claude-tool-name"),
        ("Ask via AskUserQuestion.", "claude-tool-name"),
        ("Spawn a subagent per lens.", "subagent"),
        ("Then run `/kct:tapeout <board-path>`.", "kct-slash-invocation"),
        ("See the repo's CLAUDE.md.", "claude-guide-or-dir"),
        ("Read .claude/commands/kct/help.md.", "claude-guide-or-dir"),
        ("Claude handles this.", "claude-name"),
        ("e.g. boards/05-bldc-motor-controller/output/x.kicad_pcb", "demo-board-path"),
    ],
)
def test_lint_flags_forbidden_patterns(line: str, rule: str) -> None:
    hits = find_violations("probe", line)
    assert any(f"[{rule}]" in hit for hit in hits), hits


@pytest.mark.parametrize(
    "line",
    [
        "Read the KiCad DRC report.",
        "Write the decision document with these sections.",
        "Hand off to `{{skill:manufacturing-readiness}} <board-path>`.",
        "Write the decision to `<board>/EE_REVIEW.md`.",
    ],
)
def test_lint_allows_neutral_text(line: str) -> None:
    assert find_violations("probe", line) == []


def test_marked_note_is_exempt_but_plain_text_is_not() -> None:
    text = (
        "> **Claude Code only.** Uses Claude Code subagents.\n"
        "> **Fallback:** run the passes sequentially.\n"
        "\n"
        "Spawn a subagent.\n"
    )
    hits = find_violations("probe", text)
    assert len(hits) == 1 and hits[0].startswith("probe:4:"), hits
    assert marked_notes(text) and "fallback" in marked_notes(text)[0].lower()


# --- the renderer -----------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "harness", "expected"),
    [
        ("{{skill:tapeout}}", "claude-code", "/kct:tapeout"),
        ("{{skill:tapeout}}", "codex", "$kct-tapeout"),
        ("{{skill-file:<command>}}", "claude-code", ".claude/commands/kct/<command>.md"),
        ("{{skill-file:*}}", "codex", ".agents/skills/kct-*/SKILL.md"),
        ("{{skills-dir}}", "codex", ".agents/skills/"),
        ("{{skills-readme}}", "codex", ".agents/skills/kct-help/README.md"),
        ("{{agent-guide}}", "claude-code", "CLAUDE.md"),
        ("{{agent-guide}}", "codex", "AGENTS.md"),
        ("**Arguments**: `$ARGUMENTS`", "codex", "**Arguments**: `$ARGUMENTS`"),
    ],
)
def test_render(text: str, harness: str, expected: str) -> None:
    assert render(text, harness) == expected


@pytest.mark.parametrize("text", ["{{skil:tapeout}}", "{{skills-dir:x}}", "{{skill}}"])
def test_render_rejects_unknown_placeholders(text: str) -> None:
    from kicad_tools.agent_surfaces import UnknownPlaceholderError

    with pytest.raises(UnknownPlaceholderError):
        render(text, "claude-code")


def test_render_rejects_unknown_harness() -> None:
    with pytest.raises(ValueError, match="unknown harness"):
        render("{{skill:help}}", "notepad")
