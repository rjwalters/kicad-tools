"""The harness-neutral agent primer behind ``kct agent-guide`` (issue #5960).

The primer is package data (``AGENT_GUIDE.md`` beside this module), so it ships
in the wheel with the skills. Most of it is plain, harness-neutral Markdown.
The one harness-specific part is the block between ``<!-- per-harness -->``
and ``<!-- /per-harness -->``. It uses the :mod:`kicad_tools.agent_surfaces`
placeholders (``{{skill:<name>}}``, ``{{skills-dir}}``, ...) plus the literal
token ``<harness>``, which is replaced by the harness id:

* with a harness, the block is rendered once, through
  :func:`kicad_tools.agent_surfaces.render`, for that harness;
* without one, it is rendered once per harness in
  :data:`kicad_tools.agent_surfaces.HARNESSES`, each under its own label.

Placeholders are only allowed inside that block, because the rest of the guide
must read the same in every harness. A new harness in ``HARNESSES`` (opencode,
#5951) shows up here, and in ``kct agent-guide --harness``, with no edit.
"""

from __future__ import annotations

import re
from importlib import resources

from kicad_tools.agent_surfaces import HARNESSES, render

__all__ = [
    "GUIDE_FILENAME",
    "guide_sections",
    "guide_source",
    "render_guide",
]

#: The packaged primer, relative to :mod:`kicad_tools.agent_skills`.
GUIDE_FILENAME = "AGENT_GUIDE.md"

_BLOCK_RE = re.compile(r"<!-- per-harness -->\n(.*?)<!-- /per-harness -->\n", re.DOTALL)


def guide_source() -> str:
    """Return the packaged primer, placeholders unrendered."""
    return (
        resources.files("kicad_tools.agent_skills")
        .joinpath(GUIDE_FILENAME)
        .read_text(encoding="utf-8")
    )


def _render_block(block: str, harness: str) -> str:
    return render(block.replace("<harness>", harness), harness)


def render_guide(harness: str | None = None, source: str | None = None) -> str:
    """Return the primer as Markdown, with the per-harness block filled in.

    Raises:
        ValueError: if *harness* is not one of :data:`HARNESSES`, or the guide
            has no per-harness block.
        UnknownPlaceholderError: if the guide uses a placeholder outside the
            per-harness block, or an unknown one inside it.
    """
    text = guide_source() if source is None else source
    if harness is not None and harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r}; expected one of {HARNESSES}")
    match = _BLOCK_RE.search(text)
    if match is None:
        raise ValueError(f"{GUIDE_FILENAME} has no <!-- per-harness --> block")
    block = match.group(1)
    if harness is not None:
        filled = _render_block(block, harness)
    else:
        filled = "\n".join(f"**{h}**\n\n{_render_block(block, h)}" for h in HARNESSES)
    before, after = text[: match.start()], text[match.end() :]
    # Rendering the rest with any harness both rejects stray placeholders and
    # proves there are none: harness-specific text belongs in the block.
    for part in (before, after):
        if render(part, HARNESSES[0]) != part:
            raise ValueError(f"{GUIDE_FILENAME} has a placeholder outside the per-harness block")
    return before + filled + after


def guide_sections(markdown: str) -> list[dict[str, str]]:
    """Split rendered guide Markdown into ``[{"title", "body"}]`` by heading.

    The ``#`` title and its intro form the first section; each ``##`` heading
    starts a new one.
    """
    sections: list[dict[str, str]] = []
    title: str | None = None
    lines: list[str] = []

    def _flush() -> None:
        if title is not None:
            sections.append({"title": title, "body": "\n".join(lines).strip()})

    for line in markdown.splitlines():
        heading = re.match(r"^(#{1,2}) (.+)$", line)
        if heading:
            _flush()
            title, lines = heading.group(2).strip(), []
        else:
            lines.append(line)
    _flush()
    return sections
