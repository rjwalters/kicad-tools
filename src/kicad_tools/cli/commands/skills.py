"""``kct skills`` handlers (issue #5950).

``kct skills install`` renders the packaged ``kct`` agent skills
(``kicad_tools/agent_skills/kct/*.md``) for one harness and writes them into a
project or user skills directory, so a ``pip install kicad-tools`` is enough to
get the ``/kct:*`` skills without cloning this repository.

* ``kct skills install`` -- write the skills (default: ``.claude/commands/kct/``)
* ``kct skills install --list`` -- list packaged skills and their install state
* ``kct skills install --check`` -- exit 1 if the installed copy has drifted
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace
    from pathlib import Path

__all__ = ["run_skills_command"]


def run_skills_command(args: Namespace) -> int:
    """Handle ``kct skills`` and its sub-actions."""
    subcommand = getattr(args, "skills_command", None)
    if subcommand == "install":
        return _run_install(args)
    print("Usage: kct skills install [--harness H] [--target DIR | --user] [--list | --check]")
    return 1


def _emit_json(payload: dict[str, object]) -> None:
    json.dump(payload, sys.stdout, indent=2)
    sys.stdout.write("\n")


def _run_install(args: Namespace) -> int:
    from kicad_tools.agent_skills import resolve_target

    fmt = getattr(args, "skills_format", "text")
    harness = args.skills_harness
    try:
        target = resolve_target(harness, args.skills_target, user=args.skills_user)
    except ValueError as exc:
        if fmt == "json":
            _emit_json({"error": str(exc)})
        else:
            print(f"ERROR: {exc}", file=sys.stderr)
        return 2

    if args.skills_list:
        return _list(harness, target, fmt)
    if args.skills_check:
        return _check(harness, target, fmt)
    return _install(harness, target, fmt, dry_run=args.skills_dry_run, prune=args.skills_prune)


def _install(harness: str, target: Path, fmt: str, *, dry_run: bool, prune: bool) -> int:
    from kicad_tools.agent_skills import install_skills

    report = install_skills(target, harness, dry_run=dry_run, prune=prune)
    if fmt == "json":
        _emit_json(report.to_dict())
        return 0

    verb = "would write" if dry_run else "wrote"
    print(f"kct skills ({harness}) -> {target}")
    for rel in report.written:
        print(f"  {verb}: {rel}")
    if report.unchanged:
        print(f"  unchanged: {len(report.unchanged)} file(s)")
    for rel in report.removed:
        print(f"  {'would remove' if dry_run else 'removed'}: {rel}")
    leftover = [rel for rel in report.stale if rel not in report.removed]
    for rel in leftover:
        print(f"  stale (no longer packaged; --prune removes it): {rel}")
    total = len(report.written) + len(report.unchanged)
    print(
        f"{'Dry run: ' if dry_run else ''}{len(report.written)} written, {total} packaged file(s)"
    )
    return 0


def _check(harness: str, target: Path, fmt: str) -> int:
    from kicad_tools.agent_skills import check_installed

    report = check_installed(target, harness)
    if fmt == "json":
        _emit_json(report.to_dict())
        return 0 if report.in_sync else 1

    print(f"kct skills ({harness}) at {target}")
    for label, paths in (
        ("missing", report.missing),
        ("drifted", report.drifted),
        ("stale", report.stale),
    ):
        for rel in paths:
            print(f"  {label}: {rel}")
    if report.in_sync:
        print(f"OK: {len(report.ok)} file(s) match the packaged skills")
        return 0
    print(
        "DRIFT: installed skills differ from the packaged version; "
        "run `kct skills install` (add --prune to drop stale files)"
    )
    return 1


def _list(harness: str, target: Path, fmt: str) -> int:
    from kicad_tools.agent_skills import (
        LAYOUTS,
        check_installed,
        packaged_skill_sources,
        split_frontmatter,
    )

    layout = LAYOUTS[harness]
    report = check_installed(target, harness)
    state: dict[str, str] = {}
    for status, paths in (
        ("installed", report.ok),
        ("missing", report.missing),
        ("drifted", report.drifted),
    ):
        for rel in paths:
            state[rel] = status

    # Map each packaged source to the file(s) it renders to.
    skills: list[dict[str, object]] = []
    for source in packaged_skill_sources():
        if source.is_readme:
            continue
        files = layout.build([source])
        statuses = {state.get(rel, "missing") for rel in files}
        status = statuses.pop() if len(statuses) == 1 else "drifted"
        fields, _ = split_frontmatter(source.text)
        skills.append(
            {
                "name": source.name,
                "invocation": layout.invocation(source.name),
                "description": fields.get("description", ""),
                "status": status,
                "files": sorted(files),
            }
        )

    if fmt == "json":
        _emit_json(
            {"harness": harness, "target": str(target), "skills": skills, "stale": report.stale}
        )
        return 0

    print(f"Packaged kct skills ({harness}; install state at {target}):")
    width = max(len(layout.invocation(str(s["name"]))) for s in skills)
    for skill in skills:
        print(f"  {skill['invocation']:<{width}}  [{skill['status']}]")
    for rel in report.stale:
        print(f"  stale: {rel}")
    return 0
