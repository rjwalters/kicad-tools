"""``kct agent-guide``: the packaged, harness-neutral agent primer (issue #5960)."""

from __future__ import annotations

import json

import pytest

from kicad_tools.agent_skills.guide import (
    GUIDE_FILENAME,
    guide_sections,
    guide_source,
    render_guide,
)
from kicad_tools.agent_surfaces import HARNESSES, UnknownPlaceholderError
from kicad_tools.cli import main

# The five topics the issue requires, by a phrase each section must carry.
REQUIRED_TOPICS = {
    "workflow": "kct route",
    "json contract": "exactly one JSON document",
    "two-engine sign-off": "kicad-cli pcb drc --refill-zones",
    "harness setup": "kct mcp setup --client",
    "native backend pitfall": "kct build-native --check",
    "stale kct pitfall": "which -a kct",
    "kicad-cli hang pitfall": "#5877",
    "image readings are hypotheses": "What you read off an image is a hypothesis",
    "board images": "kct board-view net",
}


def test_guide_is_package_data() -> None:
    text = guide_source()
    assert text.startswith("# ")
    assert "<!-- per-harness -->" in text


@pytest.mark.parametrize(("topic", "phrase"), sorted(REQUIRED_TOPICS.items()))
def test_guide_covers_required_topics(topic: str, phrase: str) -> None:
    assert phrase in render_guide(), f"agent guide lost its {topic} section"


def test_guide_is_one_to_two_screens() -> None:
    assert len(render_guide("codex").splitlines()) <= 100


@pytest.mark.parametrize("harness", HARNESSES)
def test_harness_flag_renders_only_that_harness(harness: str) -> None:
    text = render_guide(harness)
    assert f"kct skills install --harness {harness}`" in text
    for other in HARNESSES:
        if other != harness:
            assert f"kct skills install --harness {other}`" not in text
    assert "{{" not in text and "<!--" not in text


def test_harness_examples_come_from_render() -> None:
    assert "`/kct:tapeout <board-path>`" in render_guide("claude-code")
    assert "`$kct-tapeout <board-path>`" in render_guide("codex")


def test_default_lists_every_harness() -> None:
    text = render_guide()
    for harness in HARNESSES:
        assert f"**{harness}**" in text
        assert f"kct skills install --harness {harness}`" in text


def test_unknown_harness_rejected() -> None:
    with pytest.raises(ValueError, match="unknown harness"):
        render_guide("notepad")


def test_placeholder_outside_block_rejected() -> None:
    source = "# t\n\nSee {{skills-dir}}.\n\n<!-- per-harness -->\n- x\n<!-- /per-harness -->\n"
    with pytest.raises(ValueError, match="outside the per-harness block"):
        render_guide("codex", source)


def test_unknown_placeholder_in_block_rejected() -> None:
    source = "# t\n\n<!-- per-harness -->\n- {{skil:help}}\n<!-- /per-harness -->\n"
    with pytest.raises(UnknownPlaceholderError):
        render_guide("codex", source)


def test_missing_block_rejected() -> None:
    with pytest.raises(ValueError, match="per-harness"):
        render_guide("codex", "# t\n\nbody\n")


def test_guide_sections_split_on_headings() -> None:
    sections = guide_sections("# Title\n\nintro\n\n## A\n\na body\n\n## B\nb\n")
    assert sections == [
        {"title": "Title", "body": "intro"},
        {"title": "A", "body": "a body"},
        {"title": "B", "body": "b"},
    ]


# --- CLI -----------------------------------------------------------------------


def test_cli_text_prints_the_guide(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["agent-guide"]) == 0
    assert capsys.readouterr().out == render_guide()


def test_cli_harness(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["agent-guide", "--harness", "codex"]) == 0
    assert capsys.readouterr().out == render_guide("codex")


@pytest.mark.parametrize("harness", [None, *HARNESSES])
def test_cli_json(harness: str | None, capsys: pytest.CaptureFixture[str]) -> None:
    from kicad_tools import __version__

    argv = ["agent-guide", "--format", "json"]
    if harness is not None:
        argv += ["--harness", harness]
    assert main(argv) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["version"] == __version__
    assert payload["harness"] == harness
    titles = [s["title"] for s in payload["sections"]]
    assert titles[0] == "kicad-tools agent guide"
    assert len(titles) == 7
    assert all(set(s) == {"title", "body"} and s["body"] for s in payload["sections"])


def test_cli_rejects_unknown_harness() -> None:
    with pytest.raises(SystemExit):
        main(["agent-guide", "--harness", "notepad"])


def test_guide_filename_constant() -> None:
    assert GUIDE_FILENAME == "AGENT_GUIDE.md"
