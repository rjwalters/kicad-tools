#!/usr/bin/env python3
"""Render the shared contributor guidance into CLAUDE.md and AGENTS.md.

Issue #5964 (Epic #5952).  ``docs/contributing/agent-guidance.md`` is the
single source of the repo's project-specific contributor guidance.  Its body
(everything after the ``<!-- kct:guidance-body -->`` marker) is rendered
verbatim into the block between

    <!-- BEGIN KCT CONTRIBUTOR GUIDANCE -->
    <!-- END KCT CONTRIBUTOR GUIDANCE -->

in both ``CLAUDE.md`` (Claude Code) and ``AGENTS.md`` (Codex CLI, opencode and
other AGENTS.md-aware runtimes).  Text outside the markers -- including the
third-party REPO-SKILLS, LOOM ORCHESTRATION and SQUAD managed blocks -- is
never modified.  When a target has no block yet, ``--write`` inserts one at the
top of the file.

``--check`` (the default) fails when either committed block differs from a
fresh render, the same ratchet contract as ``scripts/ecosystem_render.py``.

Usage::

    uv run python scripts/render_agent_guidance.py            # check (exit 1 on drift)
    uv run python scripts/render_agent_guidance.py --write    # rewrite both blocks
    uv run python scripts/render_agent_guidance.py --stdout   # print the block
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SOURCE = REPO_ROOT / "docs" / "contributing" / "agent-guidance.md"
TARGETS: tuple[Path, ...] = (REPO_ROOT / "CLAUDE.md", REPO_ROOT / "AGENTS.md")

BODY_MARKER = "<!-- kct:guidance-body -->"
BEGIN_MARKER = "<!-- BEGIN KCT CONTRIBUTOR GUIDANCE -->"
END_MARKER = "<!-- END KCT CONTRIBUTOR GUIDANCE -->"
MANAGED_MARKER_PREFIXES = ("<!-- BEGIN ", "<!-- END ")
FIX_COMMAND = "uv run python scripts/render_agent_guidance.py --write"
GENERATED_NOTE = (
    "<!-- Generated from docs/contributing/agent-guidance.md. Do not edit inside "
    f"these markers; edit the source, then run: {FIX_COMMAND} -->"
)


class RenderError(Exception):
    """A source or target file is malformed (missing / duplicated / reversed markers)."""


def source_body(source_text: str, source_path: Path = SOURCE) -> str:
    """Return the renderable body of the source: the text after ``BODY_MARKER``."""
    if source_text.count(BODY_MARKER) != 1:
        raise RenderError(f"{source_path} must contain exactly one {BODY_MARKER} line")
    body = source_text.split(BODY_MARKER, 1)[1].strip("\n")
    if not body.strip():
        raise RenderError(f"{source_path} has no guidance after {BODY_MARKER}")
    for line in body.splitlines():
        if line.lstrip().startswith(MANAGED_MARKER_PREFIXES):
            raise RenderError(
                f"{source_path}: the guidance body must not contain a managed-block "
                f"marker line ({line.strip()!r}); it would duplicate a BEGIN/END "
                "marker in CLAUDE.md / AGENTS.md"
            )
    return body


def render_block(body: str) -> str:
    """Render the managed block, markers included (no trailing newline)."""
    return f"{BEGIN_MARKER}\n{GENERATED_NOTE}\n\n{body}\n\n{END_MARKER}"


def apply_block(target_text: str, block: str, target_path: Path) -> str:
    """Return ``target_text`` with its managed block replaced by ``block``.

    A target with neither marker gets the block prepended.  Anything else
    malformed raises rather than guessing, so no hand-written text is lost.
    """
    begins = target_text.count(BEGIN_MARKER)
    ends = target_text.count(END_MARKER)
    if begins == 0 and ends == 0:
        return f"{block}\n\n{target_text}" if target_text else f"{block}\n"
    if begins != 1 or ends != 1:
        raise RenderError(
            f"{target_path.name}: expected exactly one {BEGIN_MARKER} / {END_MARKER} "
            f"pair, found {begins} / {ends}"
        )
    begin = target_text.index(BEGIN_MARKER)
    end = target_text.index(END_MARKER)
    if end < begin:
        raise RenderError(f"{target_path.name}: contributor-guidance markers are reversed")
    return target_text[:begin] + block + target_text[end + len(END_MARKER) :]


def find_drift(
    source: Path | None = None, targets: tuple[Path, ...] | None = None
) -> list[tuple[Path, str, str]]:
    """Return ``(target, committed, rendered)`` for each target whose block is stale.

    Defaults to the module-level ``SOURCE`` / ``TARGETS``, read at call time.
    """
    source = SOURCE if source is None else source
    targets = TARGETS if targets is None else targets
    block = render_block(source_body(source.read_text(encoding="utf-8"), source))
    drift: list[tuple[Path, str, str]] = []
    for target in targets:
        committed = target.read_text(encoding="utf-8") if target.exists() else ""
        rendered = apply_block(committed, block, target)
        if rendered != committed:
            drift.append((target, committed, rendered))
    return drift


def main(argv: list[str] | None = None) -> int:
    """Check or rewrite the contributor-guidance block in each target."""
    parser = argparse.ArgumentParser(description=(__doc__ or "").split("\n\n")[0])
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--write", action="store_true", help="Rewrite the block in CLAUDE.md and AGENTS.md"
    )
    action.add_argument("--stdout", action="store_true", help="Print the rendered block and exit 0")
    args = parser.parse_args(argv)

    try:
        if args.stdout:
            print(render_block(source_body(SOURCE.read_text(encoding="utf-8"), SOURCE)))
            return 0
        drift = find_drift()
    except RenderError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1

    if args.write:
        for target, _, rendered in drift:
            target.write_text(rendered, encoding="utf-8")
            print(f"{target.name}: contributor-guidance block rewritten")
        if not drift:
            print("CLAUDE.md, AGENTS.md: contributor-guidance blocks already up to date")
        return 0

    if not drift:
        print("OK: CLAUDE.md and AGENTS.md match docs/contributing/agent-guidance.md")
        return 0

    for target, committed, rendered in drift:
        print(
            f"ERROR: {target.name}'s contributor-guidance block is out of date with "
            f"{SOURCE.relative_to(REPO_ROOT)}.",
            file=sys.stderr,
        )
        sys.stderr.writelines(
            difflib.unified_diff(
                committed.splitlines(keepends=True),
                rendered.splitlines(keepends=True),
                fromfile=f"{target.name} (committed)",
                tofile=f"{target.name} (rendered)",
            )
        )
    print(f"Re-run: {FIX_COMMAND}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(main())
