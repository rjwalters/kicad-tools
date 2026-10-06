"""Every ``kct`` command line in the agent guide must work as written (issue #5966).

The primer (``kct agent-guide``, issue #5960) is followed word for word by
agents, so a command that the CLI rejects is a broken step. Its sync step once
said ``--analyze``, then ``--apply``, but ``kct sync --apply`` refuses to run
without ``--confirm`` or ``--dry-run``.

This test pulls every ``kct ...`` invocation out of the guide, rendered for
every harness so the per-harness block is covered too. It substitutes the
``<placeholder>`` arguments and parses each line with the real CLI parser. An
unknown command or flag fails the test. So does a violation of one of the
known guard conditions in :data:`GUARDS`, which the parser itself cannot see.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
from collections.abc import Callable

import pytest

from kicad_tools.agent_skills.guide import render_guide
from kicad_tools.agent_surfaces import HARNESSES
from kicad_tools.cli import normalize_argv
from kicad_tools.cli.parser import create_parser

# Concrete values for the guide's ``<placeholder>`` arguments. A placeholder
# missing here fails the test, so a new one cannot silently skip parsing.
PLACEHOLDERS = {
    "<sch>": "board.kicad_sch",
    "<pcb>": "board.kicad_pcb",
    "<pro>": "board.kicad_pro",
    "<fab>": "jlcpcb",
    "<board-path>": "boards/demo",
    "<harness>": HARNESSES[0],
}

# Generic forms that are prose, not runnable commands (``kct <command> --help``).
GENERIC_MARKERS = ("<command>", "...")

# Inline code spans: `kct ...` (may wrap across a line break).
_INLINE_RE = re.compile(r"`(kct\s[^`]*)`")
# Indented code-block lines: "    kct ...".
_BLOCK_RE = re.compile(r"^ {4,}(kct\s.*)$", re.MULTILINE)
# Brace alternation, as in ``--client {claude-code,codex}``.
_BRACES_RE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_PLACEHOLDER_RE = re.compile(r"<[^<>\s]+>")


def _sync_guard(ns: argparse.Namespace) -> str | None:
    # Mirrors kicad_tools.cli.sync_cmd.main: --apply needs a safety flag.
    if ns.sync_apply and not (ns.sync_dry_run or ns.sync_confirm):
        return "`kct sync --apply` requires --dry-run or --confirm"
    return None


#: Command name -> check of a parsed namespace; returns an error or ``None``.
#: These are runtime guards inside the command, invisible to argparse.
GUARDS: dict[str, Callable[[argparse.Namespace], str | None]] = {
    "sync": _sync_guard,
}


def _expand_braces(cmd: str) -> list[str]:
    match = _BRACES_RE.search(cmd)
    if match is None:
        return [cmd]
    out: list[str] = []
    for choice in match.group(1).split(","):
        out.extend(_expand_braces(cmd[: match.start()] + choice + cmd[match.end() :]))
    return out


def extract_invocations(markdown: str) -> list[str]:
    """Return every ``kct`` command line in *markdown*, whitespace-normalised."""
    found = _INLINE_RE.findall(markdown) + _BLOCK_RE.findall(markdown)
    commands: list[str] = []
    for raw in found:
        cmd = " ".join(raw.split())
        if any(marker in cmd for marker in GENERIC_MARKERS):
            continue
        for expanded in _expand_braces(cmd):
            if expanded not in commands:
                commands.append(expanded)
    return commands


def _substitute(cmd: str) -> str:
    for placeholder, value in PLACEHOLDERS.items():
        cmd = cmd.replace(placeholder, value)
    leftover = _PLACEHOLDER_RE.findall(cmd)
    assert not leftover, f"no test value for placeholder(s) {leftover} in `{cmd}`"
    return cmd


def check_invocation(cmd: str) -> str | None:
    """Parse *cmd* with the real CLI parser; return an error message or ``None``."""
    # Drop the leading "kct", then apply the same shorthands main() does.
    argv = normalize_argv(shlex.split(_substitute(cmd))[1:])
    parser = create_parser()
    stderr = io.StringIO()
    try:
        with contextlib.redirect_stderr(stderr), contextlib.redirect_stdout(io.StringIO()):
            ns = parser.parse_args(argv)
    except SystemExit as exc:
        # --help / --version exit 0 after printing; anything else is a parse error.
        if exc.code in (0, None):
            return None
        return stderr.getvalue().strip().splitlines()[-1] if stderr.getvalue() else str(exc)
    guard = GUARDS.get(getattr(ns, "command", None) or "")
    return guard(ns) if guard else None


def _all_invocations() -> list[str]:
    commands: list[str] = []
    for harness in (None, *HARNESSES):
        for cmd in extract_invocations(render_guide(harness)):
            if cmd not in commands:
                commands.append(cmd)
    return commands


GUIDE_COMMANDS = _all_invocations()


def test_guide_has_invocations_to_check() -> None:
    # Guard the extractor itself: the guide is full of kct commands.
    assert len(GUIDE_COMMANDS) >= 15
    assert any(cmd.startswith("kct sync ") for cmd in GUIDE_COMMANDS)
    for harness in HARNESSES:
        assert f"kct skills install --harness {harness}" in GUIDE_COMMANDS
        assert f"kct mcp setup --client {harness}" in GUIDE_COMMANDS


@pytest.mark.parametrize("cmd", GUIDE_COMMANDS)
def test_guide_invocation_parses(cmd: str) -> None:
    error = check_invocation(cmd)
    assert error is None, f"agent guide command `{cmd}` does not work as written: {error}"


@pytest.mark.parametrize(
    ("cmd", "fragment"),
    [
        ("kct no-such-command <pcb>", "invalid choice"),
        ("kct route <pcb> --no-such-flag", "unrecognized arguments"),
        ("kct sync <pro> --apply", "requires --dry-run or --confirm"),
    ],
)
def test_checker_rejects_broken_invocations(cmd: str, fragment: str) -> None:
    error = check_invocation(cmd)
    assert error is not None and fragment in error, error


def test_extractor_handles_wrapped_spans_and_brace_sets() -> None:
    text = (
        "Run `kct agent-guide --harness\n<harness>` or `uv run kct ...`.\n\n"
        "    kct mcp setup --client {a,b}\n"
    )
    assert extract_invocations(text) == [
        "kct agent-guide --harness <harness>",
        "kct mcp setup --client a",
        "kct mcp setup --client b",
    ]
