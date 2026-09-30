"""Ecosystem command handlers (Issue #5839).

Surfaces the packaged registry at
``src/kicad_tools/ecosystem/data/projects.toml`` so an agent can answer
"where does kicad-tools sit?" and "have we already evaluated project X?"
without grepping ``docs/``.

Three sub-actions:

* ``kct ecosystem list`` -- the registry, optionally filtered
* ``kct ecosystem show <id>`` -- one project in full, with our verdict
* ``kct ecosystem where-we-sit`` -- our invariants, non-goals and the map
"""

from __future__ import annotations

import json
import sys
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from argparse import Namespace

    from kicad_tools.ecosystem import EcosystemProject, EcosystemRegistry

__all__ = ["run_ecosystem_command"]

#: Short gloss per relation, for the text listing.
RELATION_GLOSS: dict[str, str] = {
    "upstream": "produces files we consume",
    "peer": "overlaps our surface",
    "downstream": "consumes what we emit",
    "reference": "studied only",
}


def run_ecosystem_command(args: Namespace) -> int:
    """Handle ``kct ecosystem`` and its sub-actions."""
    from kicad_tools.ecosystem import RegistryError, load_registry

    subcommand = getattr(args, "ecosystem_command", None)
    if not subcommand:
        print("Usage: kct ecosystem <command> [options]")
        print("Commands: list, show, where-we-sit")
        return 1

    fmt = getattr(args, "ecosystem_format", "text")

    try:
        registry = load_registry()
    except RegistryError as exc:
        return _fail(f"ecosystem registry failed to load: {exc}", fmt)

    if subcommand == "list":
        return _run_list(args, registry, fmt)
    if subcommand == "show":
        return _run_show(args, registry, fmt)
    if subcommand == "where-we-sit":
        return _run_where_we_sit(registry, fmt)

    print(f"Unknown ecosystem subcommand: {subcommand}", file=sys.stderr)
    return 1


def _fail(message: str, fmt: str) -> int:
    """Report an error, honouring the single-JSON-document contract."""
    if fmt == "json":
        json.dump({"error": message}, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"ERROR: {message}", file=sys.stderr)
    return 1


def _run_list(args: Namespace, registry: EcosystemRegistry, fmt: str) -> int:
    """List registry entries, optionally filtered."""
    from kicad_tools.ecosystem import (
        CATEGORIES,
        CATEGORY_HEADINGS,
        LICENSE_COMPAT,
        RELATIONS,
        VERDICTS,
    )

    filters = {
        "category": (getattr(args, "ecosystem_category", None), CATEGORIES),
        "relation": (getattr(args, "ecosystem_relation", None), RELATIONS),
        "verdict": (getattr(args, "ecosystem_verdict", None), VERDICTS),
        "license_compat": (getattr(args, "ecosystem_license_compat", None), LICENSE_COMPAT),
    }

    for name, (value, allowed) in filters.items():
        if value is not None and value not in allowed:
            return _fail(
                f"--{name.replace('_', '-')}={value!r} is not one of {sorted(allowed)!r}",
                fmt,
            )

    selected = registry.filter(
        category=filters["category"][0],
        relation=filters["relation"][0],
        verdict=filters["verdict"][0],
        license_compat=filters["license_compat"][0],
    )

    if fmt == "json":
        json.dump(
            {
                "count": len(selected),
                "filters": {name: value for name, (value, _) in filters.items() if value},
                "projects": [project.to_dict() for project in selected],
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    if not selected:
        print("No projects match those filters.")
        return 0

    grouped: dict[str, list[EcosystemProject]] = {}
    for project in selected:
        grouped.setdefault(project.category, []).append(project)

    # Defense in depth: CATEGORY_HEADINGS is asserted to cover all of
    # CATEGORIES by a test (tests/test_ecosystem_registry.py), but a category
    # present in `selected` and absent here would otherwise silently print
    # fewer rows than the trailing count below reports (Issue #5843) -- fail
    # loudly instead, matching scripts/ecosystem_render.py's existing guard.
    unmapped = set(grouped) - set(CATEGORY_HEADINGS)
    if unmapped:
        return _fail(
            f"no heading for category/categories {sorted(unmapped)!r} in "
            "kicad_tools.ecosystem.CATEGORY_HEADINGS -- the listing would "
            "silently omit these projects",
            fmt,
        )

    for category, heading in CATEGORY_HEADINGS.items():
        members = grouped.get(category)
        if not members:
            continue
        print(f"\n{heading}")
        print("-" * len(heading))
        for project in members:
            stars = f"{project.stars}*" if project.stars is not None else "-"
            print(f"  {project.project_id:24} {project.name}")
            print(
                f"  {'':24} {project.relation} ({RELATION_GLOSS[project.relation]}),"
                f" verdict: {project.verdict}"
            )
            print(
                f"  {'':24} {project.license} [{project.license_compat}],"
                f" {project.language or 'n/a'}, {stars}"
            )
            if project.research_docs:
                # One note in the listing; `show` prints the full set.
                extra = len(project.research_docs) - 1
                note = project.research_docs[0] + (f" (+{extra} more)" if extra else "")
                print(_wrap(f"our notes: {note}", indent=" " * 27))

    print(f"\n{len(selected)} project(s). Registry: {registry.source}")
    print("Detail: kct ecosystem show <id>   |   Positioning: kct ecosystem where-we-sit")
    return 0


def _run_show(args: Namespace, registry: EcosystemRegistry, fmt: str) -> int:
    """Show one project in full."""
    project_id = getattr(args, "ecosystem_project_id", None)
    if not project_id:
        return _fail("kct ecosystem show requires a project id", fmt)

    try:
        project = registry.get(project_id)
    except KeyError as exc:
        # str(KeyError) is the repr of its argument, which re-quotes the
        # message; args[0] is the message itself.
        return _fail(str(exc.args[0]), fmt)

    if fmt == "json":
        json.dump(project.to_dict(), sys.stdout, indent=2)
        sys.stdout.write("\n")
        return 0

    print(f"{project.name}  ({project.project_id})")
    print(f"  {project.repo_url}")
    print()
    print(f"  category        {project.category}")
    print(f"  relation        {project.relation} ({RELATION_GLOSS[project.relation]})")
    print(f"  verdict         {project.verdict}")
    print(f"  language        {project.language or 'n/a'}")
    print(f"  license         {project.license} [{project.license_compat}]")
    print(
        "  code reuse      "
        + (
            "permitted, with attribution"
            if project.code_reuse_allowed
            else "NOT permitted -- ideas only, in both directions"
        )
    )
    stars = project.stars if project.stars is not None else "n/a"
    print(f"  stars           {stars}")
    print(f"  last push       {project.last_push or 'n/a'}")
    print(f"  last verified   {project.last_verified}")
    if project.pinned_commit:
        print(f"  pinned commit   {project.pinned_commit}")
    print()
    print(_wrap(project.summary, indent="  "))
    if project.research_docs:
        print()
        print("  Our evaluation notes:")
        for doc in project.research_docs:
            print(f"    - {doc}")
    if project.reeval_trigger:
        print()
        print("  Re-evaluation trigger:")
        print(_wrap(project.reeval_trigger, indent="    "))
    return 0


def _run_where_we_sit(registry: EcosystemRegistry, fmt: str) -> int:
    """Print our invariants, non-goals and the neighbour map."""
    positioning = registry.positioning
    by_relation: dict[str, list[str]] = {}
    for project in registry:
        by_relation.setdefault(project.relation, []).append(project.name)

    if fmt == "json":
        json.dump(
            {
                "invariants": list(positioning.invariants),
                "non_goals": list(positioning.non_goals),
                "neighbours_by_relation": by_relation,
                "project_count": len(registry),
                "code_reuse_permitted": [p.project_id for p in registry if p.code_reuse_allowed],
                "docs": "docs/ecosystem.md",
            },
            sys.stdout,
            indent=2,
        )
        sys.stdout.write("\n")
        return 0

    print("Where kicad-tools sits")
    print("======================")
    print()
    print("What is true of kicad-tools and not of most of the ecosystem:")
    for item in positioning.invariants:
        print(_bullet(item))
    print()
    print("What we deliberately do not do (and who does):")
    for item in positioning.non_goals:
        print(_bullet(item))
    print()
    print("Neighbours:")
    for relation in ("upstream", "peer", "downstream", "reference"):
        names = by_relation.get(relation)
        if not names:
            continue
        print(f"  {relation:10} ({RELATION_GLOSS[relation]})")
        print(_wrap(", ".join(names), indent="    "))
    print()
    print(f"{len(registry)} project(s) tracked. Full narrative: docs/ecosystem.md")
    return 0


def _bullet(text: str, width: int = 78) -> str:
    """Render ``text`` as a hanging-indent bullet."""
    import textwrap

    return textwrap.fill(
        text,
        width=width,
        initial_indent="  - ",
        subsequent_indent="    ",
        break_long_words=False,
        break_on_hyphens=False,
    )


def _wrap(text: str, indent: str = "", width: int = 78) -> str:
    """Wrap ``text`` to ``width``, prefixing every line with ``indent``.

    ``break_long_words`` / ``break_on_hyphens`` are off so a file path is
    never split across two lines, which would make it uncopyable.
    """
    import textwrap

    return textwrap.fill(
        " ".join(text.split()),
        width=width,
        initial_indent=indent,
        subsequent_indent=indent,
        break_long_words=False,
        break_on_hyphens=False,
    )
