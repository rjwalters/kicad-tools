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

The extractor reads inline code spans, indented code blocks and fenced code
blocks, and drops a leading ``uv run`` (issue #5979). Only the explicit generic
forms ``kct <command> ...`` and a bare ``kct ...`` are skipped.

The packaged ``/kct:*`` skills (``kicad_tools/agent_skills/kct/*.md``) are
checked too, but only their code blocks: those are the lines agents copy and
run, while their prose names commands in shorthand (``kct check``) on purpose.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import re
import shlex
from collections.abc import Callable
from importlib import resources

import pytest

from kicad_tools.agent_skills.guide import render_guide
from kicad_tools.agent_surfaces import HARNESSES, render
from kicad_tools.cli import normalize_argv, sync_cmd
from kicad_tools.cli.parser import create_parser

# Concrete values for the ``<placeholder>`` arguments in the guide and skills.
# A placeholder missing here fails the test, so a new one cannot silently skip
# parsing. Alternation placeholders (``<a|b>``) are expanded, not listed here.
PLACEHOLDERS = {
    "<sch>": "board.kicad_sch",
    "<pcb>": "board.kicad_pcb",
    "<pro>": "board.kicad_pro",
    "<fab>": "jlcpcb",
    "<tier>": "jlcpcb",
    "<board-path>": "boards/demo",
    "<board>": "boards/demo",
    "<board.kicad_pcb>": "board.kicad_pcb",
    "<dir>": "out",
    "<mm>": "6.4",
    "<csv>": "F.Cu,B.Cu",
    "<hv-net>": "HV_BUS",
    "<net>": "GND",
    "<V-rms>": "230",
    "<net_class_map.json>": "net_class_map.json",
    "<path/to/net_class_map.json>": "net_class_map.json",
    "\u2039board name\u203a": "Demo board",
    "<harness>": HARNESSES[0],
}

# Spans that stand for "some kct command", not a runnable one: ``kct <command>
# --help`` and a bare ``kct ...`` / ``uv run kct ...``. Only these explicit
# forms are exempt; a ``...`` elsewhere in a span is parsed like any argument.
_GENERIC_RE = re.compile(r"^kct (?:<command>(?:\s|$)|\.\.\.$)")

# An optional ``uv run`` prefix, as in ``uv run kct build-native``.
_KCT = r"(?:uv\s+run\s+)?kct\s"
# Inline code spans: `kct ...` (may wrap across a line break).
_INLINE_RE = re.compile(rf"`({_KCT}[^`]*)`")
# Indented code-block lines: "    kct ...".
_INDENTED_RE = re.compile(rf"^ {{4,}}({_KCT}.*)$", re.MULTILINE)
# Fenced code blocks: ``` or ~~~, with an optional info string (```bash).
_FENCE_RE = re.compile(
    r"^ {0,3}(`{3,}|~{3,})[^\n]*\n(.*?)^ {0,3}\1[ \t]*$", re.MULTILINE | re.DOTALL
)
# A command line inside a fenced block, after an optional "$ " prompt.
_FENCED_LINE_RE = re.compile(rf"^\s*(?:\$\s+)?({_KCT}.*)$")
# A shell comment trailing a fenced command: "kct x   # explanation".
_COMMENT_RE = re.compile(r"\s+#.*$")
# Alternation in a synopsis, expanded to one command per choice:
# braces ``--client {claude-code,codex}``, an angle-bracket value
# ``--hv-standard <iec60664|iec62368>`` and an optional group ``[--a | --b]``
# (an optional group with one member is checked with that member present).
_BRACES_RE = re.compile(r"\{([^{}]*,[^{}]*)\}")
_ANGLE_ALT_RE = re.compile(r"<([^<>\s]*\|[^<>\s]*)>")
_OPTIONAL_RE = re.compile(r"\[([^\[\]]*)\]")
_PLACEHOLDER_RE = re.compile(r"<[^<>\s]+>|\u2039[^\u203a]*\u203a")


def _sync_guard(ns: argparse.Namespace) -> str | None:
    # The same check sync_cmd.main runs, so the two cannot drift apart.
    return sync_cmd.apply_guard_error(ns.sync_apply, ns.sync_dry_run, ns.sync_confirm)


#: Command name -> check of a parsed namespace; returns an error or ``None``.
#: These are runtime guards inside the command, invisible to argparse.
GUARDS: dict[str, Callable[[argparse.Namespace], str | None]] = {
    "sync": _sync_guard,
}


def _expand_alternatives(cmd: str) -> list[str]:
    """Expand every synopsis alternation in *cmd* into concrete commands."""
    for pattern, separator in (
        (_ANGLE_ALT_RE, r"\|"),
        (_BRACES_RE, r","),
        (_OPTIONAL_RE, r"\s*\|\s*"),
    ):
        match = pattern.search(cmd)
        if match is not None:
            out: list[str] = []
            for choice in re.split(separator, match.group(1).strip()):
                expanded = cmd[: match.start()] + choice + cmd[match.end() :]
                out.extend(_expand_alternatives(" ".join(expanded.split())))
            return out
    return [cmd]


def _fenced_commands(markdown: str) -> list[str]:
    """Return the ``kct`` command lines inside fenced code blocks."""
    found: list[str] = []
    for match in _FENCE_RE.finditer(markdown):
        # Join backslash continuations so a wrapped command is one line.
        body = re.sub(r"\\\n", " ", match.group(2))
        for line in body.splitlines():
            line_match = _FENCED_LINE_RE.match(line)
            if line_match:
                found.append(_COMMENT_RE.sub("", line_match.group(1)))
    return found


def _strip_fences(markdown: str) -> str:
    # Fenced blocks are handled by _fenced_commands; blank them out so their
    # indented lines and backticks are not matched a second time.
    return _FENCE_RE.sub("", markdown)


def extract_invocations(markdown: str, *, code_blocks_only: bool = False) -> list[str]:
    """Return every ``kct`` command line in *markdown*, whitespace-normalised.

    A leading ``uv run`` is dropped, so ``uv run kct x`` is checked as ``kct x``.
    With *code_blocks_only*, inline code spans in prose are ignored.
    """
    prose = _strip_fences(markdown)
    found = [] if code_blocks_only else _INLINE_RE.findall(prose)
    found += _INDENTED_RE.findall(prose) + _fenced_commands(markdown)
    commands: list[str] = []
    for raw in found:
        cmd = re.sub(r"^uv run ", "", " ".join(raw.split()))
        if _GENERIC_RE.match(cmd):
            continue
        for expanded in _expand_alternatives(cmd):
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


def _skill_invocations() -> list[str]:
    commands: list[str] = []
    skills = resources.files("kicad_tools.agent_skills").joinpath("kct")
    for skill in sorted(skills.iterdir(), key=lambda p: p.name):
        if not skill.name.endswith(".md"):
            continue
        for harness in HARNESSES:
            text = render(skill.read_text(encoding="utf-8"), harness)
            for cmd in extract_invocations(text, code_blocks_only=True):
                if cmd not in commands:
                    commands.append(cmd)
    return commands


SKILL_COMMANDS = _skill_invocations()


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


def test_skills_have_invocations_to_check() -> None:
    # Guard the extractor: the skills' code blocks hold many kct commands.
    assert len(SKILL_COMMANDS) >= 15
    assert "kct build-native --check" in SKILL_COMMANDS


@pytest.mark.parametrize("cmd", SKILL_COMMANDS)
def test_skill_invocation_parses(cmd: str) -> None:
    error = check_invocation(cmd)
    assert error is None, f"/kct:* skill command `{cmd}` does not work as written: {error}"


@pytest.mark.parametrize(
    ("cmd", "fragment"),
    [
        ("kct no-such-command <pcb>", "invalid choice"),
        ("kct route <pcb> --no-such-flag", "unrecognized arguments"),
        ("kct sync <pro> --apply", "requires either --dry-run or --confirm"),
        ("kct route <pcb> ...", "unrecognized arguments"),
        ("kct reinforce <pcb> --all-runs", "invalid choice"),
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


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        # Fenced block, with an info string, a prompt and a trailing comment.
        ("```bash\n$ kct route --bogus   # note\n```\n", ["kct route --bogus"]),
        # A tilde fence and a backslash continuation.
        ("~~~\nkct route <pcb> \\\n  --bogus\n~~~\n", ["kct route <pcb> --bogus"]),
        # ``uv run`` is dropped, inline and fenced.
        ("`uv run kct route --bogus`", ["kct route --bogus"]),
        ("```\nuv run kct route --bogus\n```\n", ["kct route --bogus"]),
        # A literal ``...`` no longer exempts a span ...
        ("`kct route <pcb> --bogus ...`", ["kct route <pcb> --bogus ..."]),
        # ... only the explicit generic forms are skipped.
        ("`kct ...` `uv run kct ...` `kct <command> --help`", []),
        # A table cell.
        ("| a | `kct route --bogus` |", ["kct route --bogus"]),
        # Commands in a fenced block are not also matched as indented lines.
        ("```\n    kct route --bogus\n```\n", ["kct route --bogus"]),
    ],
)
def test_extractor_shapes(text: str, expected: list[str]) -> None:
    assert extract_invocations(text) == expected


def test_extractor_expands_synopsis_alternatives() -> None:
    text = "```\nkct audit <pcb> --hv-standard <a|b> [--x | --y] [--z]\n```\n"
    assert extract_invocations(text) == [
        "kct audit <pcb> --hv-standard a --x --z",
        "kct audit <pcb> --hv-standard a --y --z",
        "kct audit <pcb> --hv-standard b --x --z",
        "kct audit <pcb> --hv-standard b --y --z",
    ]


def test_code_blocks_only_ignores_prose_spans() -> None:
    text = "Prose `kct check`.\n\n```\nkct check <pcb>\n```\n"
    assert extract_invocations(text, code_blocks_only=True) == ["kct check <pcb>"]


def test_sync_guard_is_shared_with_the_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    # The CLI and this test call the same sync_cmd.apply_guard_error, so a
    # change to it is seen by both: patch it and both report the new verdict.
    assert check_invocation("kct sync <pro> --apply --dry-run") is None
    monkeypatch.setattr(sync_cmd, "apply_guard_error", lambda *flags: "patched guard")
    assert check_invocation("kct sync <pro> --apply --dry-run") == "patched guard"
    assert sync_cmd.main(["--apply", "--dry-run", "board.kicad_pro"]) == 1


def test_sync_apply_guard() -> None:
    assert sync_cmd.apply_guard_error(True, False, False) is not None
    assert sync_cmd.apply_guard_error(True, True, False) is None
    assert sync_cmd.apply_guard_error(True, False, True) is None
    assert sync_cmd.apply_guard_error(False, False, False) is None
