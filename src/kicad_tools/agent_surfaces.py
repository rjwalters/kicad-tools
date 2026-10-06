"""Harness-neutral placeholders for the ``kct`` agent skills (issue #5954).

The ``kct`` skill sources are shared by every agent harness kicad-tools installs
into. Their bodies never hard-code one harness's invocation syntax or install
paths; instead they use a small placeholder vocabulary that a per-harness
renderer fills in:

==========================  ==================================  ========================================  ======================================
Placeholder                 Claude Code                         Codex CLI                                 opencode
==========================  ==================================  ========================================  ======================================
``{{skill:<name>}}``        ``/kct:<name>``                     ``$kct-<name>``                           ``/kct/<name>``
``{{skill-file:<name>}}``   ``.claude/commands/kct/<name>.md``  ``.agents/skills/kct-<name>/SKILL.md``    ``.opencode/commands/kct/<name>.md``
``{{skills-dir}}``          ``.claude/commands/kct/``           ``.agents/skills/``                       ``.opencode/commands/kct/``
``{{skills-readme}}``       ``.claude/commands/kct/README.md``  ``.agents/skills/kct-help/README.md``     ``.opencode/commands/kct/README.md``
``{{agent-guide}}``         ``CLAUDE.md``                       ``AGENTS.md``                             ``AGENTS.md``
==========================  ==================================  ========================================  ======================================

opencode (v2) installs the skills as *commands*: a nested
``.opencode/commands/kct/<name>.md`` is the command ``/kct/<name>`` (nested
paths become command names with ``/`` separators), which is the closest
equivalent of a Claude Code ``/kct:<name>`` slash command. See
``docs/agent-surfaces.md`` for why commands rather than opencode skills (#5951).

``<name>`` may itself be a placeholder token such as ``<command>`` or the glob
``*``. ``$ARGUMENTS`` is not a placeholder: it is the argument token Claude Code,
Codex prompts and opencode commands all understand, so it is left as is.

``scripts/install-kct.sh`` carries the Claude Code and Codex columns as ``sed``
rules; the installer tests check that its output equals :func:`render` so the
two cannot drift. The packaged installer, ``kct skills install`` (#5950, #5951),
calls :func:`render` directly and is the only installer with an opencode target.
"""

from __future__ import annotations

import re

__all__ = ["HARNESSES", "PLACEHOLDER_RE", "UnknownPlaceholderError", "render"]

#: Harnesses :func:`render` knows how to target.
HARNESSES: tuple[str, ...] = ("claude-code", "codex", "opencode")

#: Matches any ``{{...}}`` token, known or not, so typos are caught rather than
#: silently shipped.
PLACEHOLDER_RE = re.compile(r"\{\{([a-z-]+)(?::([^{}\s]+))?\}\}")

_PARAM_TEMPLATES: dict[str, dict[str, str]] = {
    "skill": {
        "claude-code": "/kct:{arg}",
        "codex": "$kct-{arg}",
        "opencode": "/kct/{arg}",
    },
    "skill-file": {
        "claude-code": ".claude/commands/kct/{arg}.md",
        "codex": ".agents/skills/kct-{arg}/SKILL.md",
        "opencode": ".opencode/commands/kct/{arg}.md",
    },
}

_BARE_VALUES: dict[str, dict[str, str]] = {
    "skills-dir": {
        "claude-code": ".claude/commands/kct/",
        "codex": ".agents/skills/",
        "opencode": ".opencode/commands/kct/",
    },
    "skills-readme": {
        "claude-code": ".claude/commands/kct/README.md",
        "codex": ".agents/skills/kct-help/README.md",
        "opencode": ".opencode/commands/kct/README.md",
    },
    "agent-guide": {
        "claude-code": "CLAUDE.md",
        "codex": "AGENTS.md",
        "opencode": "AGENTS.md",
    },
}


class UnknownPlaceholderError(ValueError):
    """Raised when a skill source uses a placeholder outside the vocabulary."""


def render(text: str, harness: str) -> str:
    """Fill every skill placeholder in *text* for *harness*.

    Raises:
        ValueError: if *harness* is not one of :data:`HARNESSES`.
        UnknownPlaceholderError: if *text* contains an unknown or malformed
            ``{{...}}`` placeholder.
    """
    if harness not in HARNESSES:
        raise ValueError(f"unknown harness {harness!r}; expected one of {HARNESSES}")

    def _sub(match: re.Match[str]) -> str:
        kind, arg = match.group(1), match.group(2)
        if kind in _PARAM_TEMPLATES and arg is not None:
            return _PARAM_TEMPLATES[kind][harness].format(arg=arg)
        if kind in _BARE_VALUES and arg is None:
            return _BARE_VALUES[kind][harness]
        raise UnknownPlaceholderError(f"unknown skill placeholder: {match.group(0)}")

    return PLACEHOLDER_RE.sub(_sub, text)
