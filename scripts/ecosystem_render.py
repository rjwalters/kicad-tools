#!/usr/bin/env python3
"""Render the README's Related Projects block from the ecosystem registry.

Issue #5839.  The README section between

    <!-- BEGIN kct:ecosystem -->
    <!-- END kct:ecosystem -->

is generated, never hand-edited.  ``--check`` (the default) fails when the
committed block differs from a fresh render, so editing
``src/kicad_tools/ecosystem/data/projects.toml`` without re-rendering is a
loud error rather than silent drift.  That is the same self-maintaining
ratchet contract as ``TestBoardsReadmeUpdated`` in
``tests/test_board_07_matchgroup_test.py``.

Usage::

    uv run python scripts/ecosystem_render.py            # check (exit 1 on drift)
    uv run python scripts/ecosystem_render.py --write    # update README.md
    uv run python scripts/ecosystem_render.py --stdout   # print the block
"""

from __future__ import annotations

import argparse
import difflib
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

from kicad_tools.ecosystem import (  # noqa: E402  (path bootstrap above)
    CATEGORY_HEADINGS,
    EcosystemProject,
    EcosystemRegistry,
    load_registry,
    validate_research_docs,
)

BEGIN_MARKER = "<!-- BEGIN kct:ecosystem -->"
END_MARKER = "<!-- END kct:ecosystem -->"

#: Bold lead-in per verdict. Empty means the summary already carries it.
VERDICT_LEADIN: dict[str, str] = {
    "benchmarked": "**Benchmarked against us.**",
    "ideas-adopted": "**Ideas adopted.**",
    "evaluated-not-adopted": "**Evaluated and not adopted.**",
    "complementary": "",
    "watch": "",
}

INTRO = """\
kicad-tools is one piece of a fast-moving KiCad automation ecosystem. This
section is generated from `src/kicad_tools/ecosystem/data/projects.toml` --
run `kct ecosystem where-we-sit` for our invariants and non-goals, `kct
ecosystem show <id>` for any entry below, and see [docs/ecosystem.md](docs/ecosystem.md)
for the full positioning narrative. Where we have evaluated a project in
depth, the linked note carries the method, the measurements and the verdict,
including the cases where the other tool wins.

Licenses are the SPDX id from the upstream `LICENSE` file, and are
load-bearing: an AGPL or unlicensed neighbour is studied at the capability
level only, never copied from. Facts below were last verified on the date each
registry entry records; `.github/workflows/ecosystem-drift.yml` re-checks them
weekly."""


def _stars(project: EcosystemProject) -> str:
    """Render the star count, or an empty string when the host reports none."""
    return f", {project.stars}★" if project.stars is not None else ""


def _license_phrase(project: EcosystemProject) -> str:
    """Render the parenthetical license/language/stars phrase."""
    license_text = "no `LICENSE` committed" if project.license == "NONE" else project.license
    parts = [license_text]
    if project.language:
        parts.append(project.language)
    return f"({', '.join(parts)}{_stars(project)})"


def _notes_clause(project: EcosystemProject) -> str:
    """Render the trailing link(s) to our own evaluation notes."""
    if not project.research_docs:
        return ""
    links = ", ".join(f"[{doc}]({doc})" for doc in project.research_docs)
    label = "Our evaluation" if len(project.research_docs) == 1 else "Our evaluations"
    return f" {label}: {links}."


def _entry(project: EcosystemProject) -> str:
    """Render one project as a README bullet."""
    leadin = VERDICT_LEADIN.get(project.verdict, "")
    summary = " ".join(project.summary.split())
    if leadin:
        # Summaries are written to follow an em-dash, so they start lowercase;
        # after a bold lead-in they begin a sentence instead.
        summary = summary[:1].upper() + summary[1:]
        body = f"{leadin} {summary}"
    else:
        body = summary
    return (
        f"- **[{project.name}]({project.repo_url})** {_license_phrase(project)} "
        f"-- {body}{_notes_clause(project)}"
    )


def render_block(registry: EcosystemRegistry) -> str:
    """Render the full generated block, markers included."""
    lines: list[str] = [BEGIN_MARKER, "", INTRO, ""]

    featured = [p for p in registry if p.verdict != "watch"]
    watching = [p for p in registry if p.verdict == "watch"]

    grouped: dict[str, list[EcosystemProject]] = {}
    for project in featured:
        grouped.setdefault(project.category, []).append(project)

    unknown = set(grouped) - set(CATEGORY_HEADINGS)
    if unknown:
        raise SystemExit(
            f"ecosystem_render: no README heading for category/categories {sorted(unknown)!r}; "
            "add one to CATEGORY_HEADINGS in src/kicad_tools/ecosystem/models.py"
        )

    for category, heading in CATEGORY_HEADINGS.items():
        members = grouped.get(category)
        if not members:
            continue
        lines.append(f"### {heading}")
        lines.append("")
        lines.extend(_entry(project) for project in members)
        lines.append("")

    if watching:
        lines.append("### Watching")
        lines.append("")
        lines.append(
            "Recorded in the registry with a re-evaluation trigger, deliberately "
            "not featured above yet:"
        )
        lines.append("")
        lines.extend(_entry(project) for project in watching)
        lines.append("")

    lines.append(END_MARKER)
    return "\n".join(lines)


def _split_readme(readme_text: str, readme_path: Path) -> tuple[str, str]:
    """Return the text before and after the generated block.

    Raises:
        SystemExit: If either marker is missing or they are out of order.
    """
    begin = readme_text.find(BEGIN_MARKER)
    end = readme_text.find(END_MARKER)
    if begin == -1 or end == -1:
        raise SystemExit(
            f"ecosystem_render: {readme_path} is missing the {BEGIN_MARKER} / "
            f"{END_MARKER} markers that delimit the generated block"
        )
    if end < begin:
        raise SystemExit(f"ecosystem_render: {readme_path} has the ecosystem markers reversed")
    return readme_text[:begin], readme_text[end + len(END_MARKER) :]


def main(argv: list[str] | None = None) -> int:
    """Check or rewrite the README's generated ecosystem block."""
    parser = argparse.ArgumentParser(description=__doc__ or "")
    action = parser.add_mutually_exclusive_group()
    action.add_argument(
        "--write", action="store_true", help="Rewrite the block in README.md in place"
    )
    action.add_argument("--stdout", action="store_true", help="Print the rendered block and exit 0")
    parser.add_argument(
        "--readme",
        type=Path,
        default=REPO_ROOT / "README.md",
        help="README to check or rewrite (default: repo README.md)",
    )
    args = parser.parse_args(argv)

    registry = load_registry()

    missing = validate_research_docs(registry, REPO_ROOT)
    if missing:
        for project_id, doc in missing:
            print(f"ERROR: {project_id}: research_docs path does not exist: {doc}", file=sys.stderr)
        return 1

    block = render_block(registry)

    if args.stdout:
        print(block)
        return 0

    readme_text = args.readme.read_text(encoding="utf-8")
    before, after = _split_readme(readme_text, args.readme)
    updated = f"{before}{block}{after}"

    if args.write:
        if updated == readme_text:
            print(f"{args.readme.name}: ecosystem block already up to date")
            return 0
        args.readme.write_text(updated, encoding="utf-8")
        print(f"{args.readme.name}: ecosystem block rewritten from {registry.source}")
        return 0

    if updated == readme_text:
        print(
            f"{args.readme.name}: ecosystem block matches the registry ({len(registry)} projects)"
        )
        return 0

    print(
        f"ERROR: {args.readme.name}'s ecosystem block is out of date with "
        f"{registry.source}.\nRe-run: uv run python scripts/ecosystem_render.py --write\n",
        file=sys.stderr,
    )
    diff = difflib.unified_diff(
        readme_text.splitlines(keepends=True),
        updated.splitlines(keepends=True),
        fromfile=f"{args.readme.name} (committed)",
        tofile=f"{args.readme.name} (rendered)",
    )
    sys.stderr.writelines(diff)
    return 1


if __name__ == "__main__":
    sys.exit(main())
