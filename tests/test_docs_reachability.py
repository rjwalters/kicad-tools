"""Every ``docs/**/*.md`` is reachable from ``docs/README.md`` (Issue #6947).

Reachability follows relative markdown links transitively, so a note may be
linked from a folder index (``docs/research/README.md``) rather than directly
from the top-level index.
"""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DOCS = ROOT / "docs"
INDEX = DOCS / "README.md"

# Intentional exclusions, as paths relative to ``docs/``. Keep this small.
ALLOWLIST: frozenset[str] = frozenset()

_LINK = re.compile(r"\[[^\]]*\]\(([^)\s]+)(?:\s+\"[^\"]*\")?\)")
_FENCE = re.compile(r"^(```|~~~).*?^\1", re.DOTALL | re.MULTILINE)
_INLINE_CODE = re.compile(r"`[^`\n]*`")


def _links(path: Path) -> list[Path]:
    text = _INLINE_CODE.sub("", _FENCE.sub("", path.read_text(encoding="utf-8")))
    found = []
    for target in _LINK.findall(text):
        if re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", target) or target.startswith("#"):
            continue
        target = target.split("#", 1)[0].split("?", 1)[0]
        if not target:
            continue
        resolved = (path.parent / target).resolve()
        if resolved.is_dir():
            resolved = resolved / "README.md"
        found.append(resolved)
    return found


def reachable_from(start: Path) -> set[Path]:
    seen: set[Path] = set()
    queue = [start.resolve()]
    while queue:
        current = queue.pop()
        if current in seen or current.suffix != ".md" or not current.is_file():
            continue
        seen.add(current)
        queue.extend(_links(current))
    return seen


def unreachable_docs() -> list[str]:
    seen = reachable_from(INDEX)
    missing = []
    for md in sorted(DOCS.rglob("*.md")):
        rel = md.relative_to(DOCS).as_posix()
        if md.resolve() not in seen and rel not in ALLOWLIST:
            missing.append(rel)
    return missing


def test_every_doc_is_reachable_from_docs_readme():
    missing = unreachable_docs()
    assert not missing, (
        "docs/**/*.md not linked (directly or via a folder index) from docs/README.md:\n  "
        + "\n  ".join(missing)
        + "\nLink each from docs/README.md or the relevant folder index."
    )


def test_allowlist_entries_exist_and_are_needed():
    seen = reachable_from(INDEX)
    for rel in ALLOWLIST:
        path = DOCS / rel
        assert path.is_file(), f"stale allowlist entry: {rel}"
        assert path.resolve() not in seen, f"allowlist entry is now reachable: {rel}"
