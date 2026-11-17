#!/usr/bin/env python3
"""Assemble ``changelog.d/`` fragments into a versioned CHANGELOG section.

Motivation (issue #5775, Phase 1 of epic #5774): CHANGELOG entries used to be
written at release time, and went missing -- 20 user-visible issues had none
when v0.22.0 was cut (#5772).  Editing ``[Unreleased]`` at PR time does not
scale either: ~20 merges a day all editing the top of one section conflict
constantly.  So each user-visible PR adds a *fragment* file instead, and this
script turns the accumulated fragments into the release section.

Fragment format
---------------
One file per user-visible change, in ``changelog.d/``::

    changelog.d/<issue>.<kind>.md
    changelog.d/<issue>.<kind>.<slug>.md     # several same-kind entries, one issue

``<kind>`` is one of :data:`KINDS` (``upgrade``, ``fixed``, ``added``,
``changed``, ``performance``).  The body is the finished bullet text in the
existing CHANGELOG voice, citing the issue, e.g.::

    - **`kct route` honours the project clearance** (Issue #5645). ...

A body that does not start with ``- `` is turned into one bullet (continuation
lines indented two spaces).  ``upgrade`` fragments become the release's
"Upgrade notes / behaviour changes" section.  ``README.md`` and dotfiles in the
directory are ignored; any other file whose name does not match the pattern is
an error, so a typo (``5775.fix.md``) fails loudly instead of being dropped.

Output
------
``## [X.Y.Z] - YYYY-MM-DD`` with sections in the existing order -- Summary
(optional), Upgrade notes / behaviour changes, Fixed, Added, Changed,
Performance, then any other heading already present in ``[Unreleased]`` -- one
heading per section, plus the ``[X.Y.Z]: .../releases/tag/vX.Y.Z`` footer link.
Existing ``[Unreleased]`` content under a known heading is kept, *before* the
fragments for that heading; ``[Unreleased]`` is left empty.  Consumed fragments
are deleted.  The output depends only on the inputs (no timestamps unless
``--date`` is omitted), so it is byte-stable.

Importable interface (for the Phase 2 release workflow)
-------------------------------------------------------
- :data:`KINDS`, :data:`SECTION_TITLES`, :data:`FRAGMENT_NAME`
- :func:`parse_fragment_name` -- ``"5775.fixed.md"`` -> ``(5775, "fixed", None)``
- :func:`collect_fragments` -- read and validate a fragment directory
- :func:`fragment_issue_numbers` -- issues a fragment set documents
- :func:`assemble` -- pure ``(changelog_text, fragments, version, date) -> text``
- :func:`render_release_section` -- just the ``## [X.Y.Z]`` block

Usage
-----
    uv run python scripts/changelog_assemble.py --version 0.23.0
    uv run python scripts/changelog_assemble.py --version 0.23.0 --dry-run
    uv run python scripts/changelog_assemble.py --list              # JSON inventory
    uv run python scripts/changelog_assemble.py --check             # validate names

Exit codes
----------
    0 -- success.
    1 -- nothing to assemble (no fragments and an empty ``[Unreleased]``).
    2 -- usage / validation error (bad fragment name, version already present).
"""

from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DEFAULT_FRAGMENTS_DIR = REPO_ROOT / "changelog.d"
DEFAULT_CHANGELOG = REPO_ROOT / "CHANGELOG.md"
DEFAULT_REPO_SLUG = "rjwalters/kicad-tools"

#: Fragment kinds, in the order their sections appear in a release.
KINDS: tuple[str, ...] = ("upgrade", "fixed", "added", "changed", "performance")

#: The ``###`` heading each kind renders under.
SECTION_TITLES: dict[str, str] = {
    "upgrade": "Upgrade notes / behaviour changes",
    "fixed": "Fixed",
    "added": "Added",
    "changed": "Changed",
    "performance": "Performance",
}

#: ``<issue>.<kind>[.<slug>].md``
FRAGMENT_NAME = re.compile(
    r"^(?P<issue>\d+)\.(?P<kind>" + "|".join(KINDS) + r")(?:\.(?P<slug>[A-Za-z0-9_-]+))?\.md$"
)

#: Files in the fragment directory that are not fragments.
IGNORED_NAMES = frozenset({"README.md"})

_VERSION = re.compile(r"^\d+\.\d+\.\d+(?:[-.+][0-9A-Za-z.+-]+)?$")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_FOOTER_LINK = re.compile(
    r"^\[\d+\.\d+\.\d+[^\]]*\]:\s*https://github\.com/([^/\s]+/[^/\s]+)/", re.MULTILINE
)


class FragmentError(ValueError):
    """A fragment directory or fragment file is malformed."""


@dataclass(frozen=True)
class Fragment:
    """One ``changelog.d/`` entry."""

    path: Path
    issue: int
    kind: str
    slug: str | None
    text: str

    @property
    def sort_key(self) -> tuple[int, int, str]:
        return (KINDS.index(self.kind), self.issue, self.slug or "")

    def cited_issues(self) -> set[int]:
        """The file's own issue plus every ``#N`` its body mentions."""
        return {self.issue} | {int(n) for n in re.findall(r"#(\d+)\b", self.text)}


def parse_fragment_name(name: str) -> tuple[int, str, str | None] | None:
    """Parse ``<issue>.<kind>[.<slug>].md``; ``None`` if ``name`` is not one."""
    match = FRAGMENT_NAME.match(name)
    if match is None:
        return None
    return int(match.group("issue")), match.group("kind"), match.group("slug")


def is_ignored_name(name: str) -> bool:
    return name in IGNORED_NAMES or name.startswith(".")


def normalize_bullet(text: str) -> str:
    """Return ``text`` as one Markdown bullet with trailing whitespace stripped."""
    lines = [line.rstrip() for line in text.strip("\n").splitlines()]
    while lines and not lines[-1]:
        lines.pop()
    if not lines:
        return ""
    if lines[0].startswith("- "):
        return "\n".join(lines)
    out = ["- " + lines[0].lstrip()]
    out.extend(("  " + line) if line else "" for line in lines[1:])
    return "\n".join(out)


def collect_fragments(directory: Path) -> list[Fragment]:
    """Read every fragment in ``directory``, sorted by (kind, issue, slug).

    Raises :class:`FragmentError` naming every malformed file (bad name, empty
    body, or a sub-directory) so one run reports all of them.
    """
    if not directory.is_dir():
        return []
    fragments: list[Fragment] = []
    errors: list[str] = []
    for path in sorted(directory.iterdir()):
        if is_ignored_name(path.name):
            continue
        parsed = parse_fragment_name(path.name)
        if parsed is None or not path.is_file():
            errors.append(
                f"{path.name}: not a fragment name; expected <issue>.<kind>[.<slug>].md "
                f"with kind in {{{', '.join(KINDS)}}}"
            )
            continue
        issue, kind, slug = parsed
        text = normalize_bullet(path.read_text(encoding="utf-8"))
        if not text:
            errors.append(f"{path.name}: empty fragment")
            continue
        fragments.append(Fragment(path=path, issue=issue, kind=kind, slug=slug, text=text))
    if errors:
        raise FragmentError("invalid changelog fragment(s):\n  " + "\n  ".join(errors))
    return sorted(fragments, key=lambda f: f.sort_key)


def fragment_issue_numbers(fragments: list[Fragment]) -> set[int]:
    """Every issue a fragment set documents (file names and body citations)."""
    issues: set[int] = set()
    for fragment in fragments:
        issues |= fragment.cited_issues()
    return issues


# --- CHANGELOG plumbing -----------------------------------------------------


def _section_bounds(changelog_text: str, section: str) -> tuple[int, int, int] | None:
    """``(heading_start, body_start, body_end)`` of ``## [<section>]``."""
    heading = re.compile(rf"^##\s*\[{re.escape(section)}\][^\n]*\n?", re.MULTILINE)
    match = heading.search(changelog_text)
    if match is None:
        return None
    nxt = re.compile(r"^##\s", re.MULTILINE).search(changelog_text, match.end())
    end = nxt.start() if nxt else len(changelog_text)
    return match.start(), match.end(), end


def _kind_for_heading(title: str) -> str | None:
    lowered = title.strip().lower()
    if lowered == "summary":
        return "summary"
    if lowered.startswith("upgrade"):
        return "upgrade"
    for kind in KINDS:
        if lowered == SECTION_TITLES[kind].lower():
            return kind
    return None


def parse_unreleased(changelog_text: str) -> tuple[str, dict[str, str], list[tuple[str, str]]]:
    """Split ``[Unreleased]`` into ``(preamble, known_sections, extra_sections)``.

    ``known_sections`` maps ``"summary"`` or a :data:`KINDS` member to that
    heading's stripped body.  ``extra_sections`` keeps any other ``###`` heading
    (e.g. ``Removed``) as ``(title, body)`` in document order.
    """
    bounds = _section_bounds(changelog_text, "Unreleased")
    if bounds is None:
        return "", {}, []
    body = changelog_text[bounds[1] : bounds[2]]
    parts = re.split(r"^###[ \t]+(.+?)[ \t]*$", body, flags=re.MULTILINE)
    preamble = parts[0].strip()
    known: dict[str, str] = {}
    extra: list[tuple[str, str]] = []
    for title, content in zip(parts[1::2], parts[2::2], strict=True):
        content = content.strip()
        if not content:
            continue
        kind = _kind_for_heading(title)
        if kind is None:
            extra.append((title.strip(), content))
        elif kind in known:
            known[kind] = known[kind] + "\n\n" + content
        else:
            known[kind] = content
    return preamble, known, extra


def repo_slug_from_changelog(changelog_text: str) -> str:
    match = _FOOTER_LINK.search(changelog_text)
    return match.group(1) if match else DEFAULT_REPO_SLUG


def render_release_section(
    version: str,
    date: str,
    fragments: list[Fragment],
    *,
    summary: str | None = None,
    unreleased: tuple[str, dict[str, str], list[tuple[str, str]]] = ("", {}, []),
) -> str:
    """Render the ``## [version] - date`` block (ends with one newline)."""
    preamble, known, extra = unreleased
    blocks: list[str] = [f"## [{version}] - {date}"]
    summary_parts = [p for p in (summary and summary.strip(), known.get("summary")) if p]
    if preamble:
        summary_parts.append(preamble)
    if summary_parts:
        blocks.append("### Summary")
        blocks.append("\n\n".join(summary_parts))
    for kind in KINDS:
        entries: list[str] = []
        if known.get(kind):
            entries.append(known[kind])
        entries.extend(f.text for f in fragments if f.kind == kind)
        if entries:
            blocks.append(f"### {SECTION_TITLES[kind]}")
            blocks.append("\n\n".join(entries))
    for title, content in extra:
        blocks.append(f"### {title}")
        blocks.append(content)
    return "\n\n".join(blocks) + "\n"


def has_content(
    fragments: list[Fragment], unreleased: tuple[str, dict[str, str], list[tuple[str, str]]]
) -> bool:
    preamble, known, extra = unreleased
    return bool(fragments or preamble or known or extra)


def assemble(
    changelog_text: str,
    fragments: list[Fragment],
    version: str,
    date: str,
    *,
    summary: str | None = None,
    repo_slug: str | None = None,
) -> str:
    """Return ``changelog_text`` with ``[Unreleased]`` + fragments cut as ``version``.

    Pure: touches no files.  ``[Unreleased]`` is kept (empty) above the new
    section, and the footer link is inserted above the newest existing one.
    """
    if not _VERSION.match(version):
        raise FragmentError(f"not a version: {version!r}")
    if not _DATE.match(date):
        raise FragmentError(f"not a YYYY-MM-DD date: {date!r}")
    if _section_bounds(changelog_text, version) is not None:
        raise FragmentError(f"CHANGELOG already has a [{version}] section")
    bounds = _section_bounds(changelog_text, "Unreleased")
    if bounds is None:
        raise FragmentError("CHANGELOG has no ## [Unreleased] heading")

    unreleased = parse_unreleased(changelog_text)
    section = render_release_section(
        version, date, fragments, summary=summary, unreleased=unreleased
    )
    head = changelog_text[: bounds[0]]
    tail = changelog_text[bounds[2] :]
    new_text = f"{head}## [Unreleased]\n\n{section}"
    new_text += ("\n" + tail) if tail else ""

    slug = repo_slug or repo_slug_from_changelog(changelog_text)
    link = f"[{version}]: https://github.com/{slug}/releases/tag/v{version}\n"
    footer = _FOOTER_LINK.search(new_text)
    if footer is not None:
        new_text = new_text[: footer.start()] + link + new_text[footer.start() :]
    else:
        new_text = new_text.rstrip("\n") + "\n\n" + link
    return new_text


# --- CLI --------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Assemble changelog.d/ fragments into a versioned CHANGELOG section.",
    )
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--version", metavar="X.Y.Z", help="release version to cut")
    mode.add_argument(
        "--list", action="store_true", help="print the fragment inventory as JSON and exit"
    )
    mode.add_argument(
        "--check", action="store_true", help="validate fragment names/bodies and exit"
    )
    parser.add_argument(
        "--date", help="release date YYYY-MM-DD (default: today, UTC)", metavar="DATE"
    )
    parser.add_argument("--summary-file", type=Path, help="Markdown for the ### Summary section")
    parser.add_argument("--changelog", type=Path, default=DEFAULT_CHANGELOG)
    parser.add_argument("--fragments-dir", type=Path, default=DEFAULT_FRAGMENTS_DIR)
    parser.add_argument("--repo", help="OWNER/NAME for the footer link (default: from CHANGELOG)")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="print the new release section; write nothing, delete nothing",
    )
    parser.add_argument(
        "--keep-fragments", action="store_true", help="rewrite CHANGELOG but keep fragment files"
    )
    args = parser.parse_args(argv)

    try:
        fragments = collect_fragments(args.fragments_dir)
    except FragmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.check:
        print(f"{len(fragments)} valid fragment(s) in {args.fragments_dir}")
        return 0
    if args.list:
        print(
            json.dumps(
                [
                    {
                        "path": str(f.path.relative_to(args.fragments_dir.parent))
                        if f.path.is_relative_to(args.fragments_dir.parent)
                        else str(f.path),
                        "issue": f.issue,
                        "kind": f.kind,
                        "slug": f.slug,
                    }
                    for f in fragments
                ],
                indent=2,
            )
        )
        return 0

    if not args.changelog.is_file():
        print(f"error: no such CHANGELOG: {args.changelog}", file=sys.stderr)
        return 2
    changelog_text = args.changelog.read_text(encoding="utf-8")
    if not has_content(fragments, parse_unreleased(changelog_text)):
        print("nothing to assemble: no fragments and an empty [Unreleased]", file=sys.stderr)
        return 1

    date = args.date or _dt.datetime.now(_dt.timezone.utc).date().isoformat()
    summary = args.summary_file.read_text(encoding="utf-8") if args.summary_file else None
    try:
        new_text = assemble(
            changelog_text, fragments, args.version, date, summary=summary, repo_slug=args.repo
        )
    except FragmentError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2

    if args.dry_run:
        print(
            render_release_section(
                args.version,
                date,
                fragments,
                summary=summary,
                unreleased=parse_unreleased(changelog_text),
            ),
            end="",
        )
        return 0

    args.changelog.write_text(new_text, encoding="utf-8")
    if not args.keep_fragments:
        for fragment in fragments:
            fragment.path.unlink()
    print(
        f"assembled [{args.version}] - {date} from {len(fragments)} fragment(s)"
        + ("" if args.keep_fragments else "; fragments deleted")
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
