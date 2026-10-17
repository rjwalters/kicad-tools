"""Keep relative markdown links valid when a board file is copied into a bundle (Issue #6899).

``output/manufacturing/design-source/`` holds verbatim copies of board-level
files, so a link written for ``boards/<board>/`` (``firmware/README.md``,
``../../examples/...``) points nowhere from the copy.  :func:`rewrite_markdown_links`
fixes each in-repo relative link:

* it already resolves from the new location -> unchanged;
* its path exists under the bundle root -> rebased to the new location;
* otherwise -> demoted to plain text, ``label (target, not included in this bundle)``.

URLs, ``#anchors``, ``mailto:`` and anything in code fences or inline code are
untouched.  The rewrite is deterministic and a fixed point (running it on its
own output changes nothing), so archive and manifest hashes stay stable.
"""

from __future__ import annotations

import os
import re
import shutil
from pathlib import Path

NOT_INCLUDED = "not included in this bundle"

_INLINE_LINK = re.compile(r"(!?)\[([^\]]*)\]\(([^)\s]+)((?:\s+\"[^\"]*\")?)\)")
_REF_DEF = re.compile(r"^(\s{0,3})\[([^\]]+)\]:[ \t]*(\S+)(.*)$")
_CODE_SPAN = re.compile(r"(`+)(?:(?!\1).)+?\1")
_FENCE = re.compile(r"^\s{0,3}(```+|~~~+)")
_SCHEME = re.compile(r"^(?:[a-zA-Z][a-zA-Z0-9+.-]*:|//)")


def _resolve(target: str, dst_dir: Path, bundle_root: Path) -> str | None:
    """New target for *target*, or ``None`` when it must be demoted.

    Returns *target* itself when no rewrite is needed.
    """
    if not target or target.startswith(("#", "/")) or _SCHEME.match(target):
        return target
    m = re.search(r"[#?]", target)
    path, tail = (target[: m.start()], target[m.start() :]) if m else (target, "")
    if not path:
        return target
    if (dst_dir / path).exists():
        return target
    candidate = Path(os.path.normpath(bundle_root / path))
    if candidate.exists() and (candidate == bundle_root or bundle_root in candidate.parents):
        rel = Path(os.path.relpath(candidate, dst_dir)).as_posix()
        if path.endswith("/") and not rel.endswith("/"):
            rel += "/"
        return rel + tail
    return None


def _rewrite_segment(segment: str, dst_dir: Path, bundle_root: Path) -> str:
    def inline(m: re.Match) -> str:
        bang, label, target, title = m.groups()
        new = _resolve(target, dst_dir, bundle_root)
        if new is None:
            return f"{label} ({target}, {NOT_INCLUDED})"
        return f"{bang}[{label}]({new}{title})"

    return _INLINE_LINK.sub(inline, segment)


def _rewrite_line(line: str, dst_dir: Path, bundle_root: Path) -> str:
    ref = _REF_DEF.match(line)
    if ref:
        indent, label, target, rest = ref.groups()
        new = _resolve(target, dst_dir, bundle_root)
        eol = line[len(line.rstrip("\r\n")) :]
        rest = rest.rstrip("\r\n")
        if new is None:
            return f"{indent}{label}: {target} ({NOT_INCLUDED}){rest}{eol}"
        return f"{indent}[{label}]: {new}{rest}{eol}"
    out, pos = [], 0
    for span in _CODE_SPAN.finditer(line):
        out.append(_rewrite_segment(line[pos : span.start()], dst_dir, bundle_root))
        out.append(span.group(0))
        pos = span.end()
    out.append(_rewrite_segment(line[pos:], dst_dir, bundle_root))
    return "".join(out)


def rewrite_markdown_links(text: str, dst_dir: Path, bundle_root: Path) -> str:
    """Rewrite relative links in *text* so they hold when it lives in *dst_dir*.

    *bundle_root* is the directory the bundle ships (``output/manufacturing``).
    """
    dst_dir, bundle_root = Path(dst_dir), Path(os.path.normpath(bundle_root))
    out: list[str] = []
    fence: str | None = None
    for line in text.splitlines(keepends=True):
        m = _FENCE.match(line)
        if fence is None and m:
            fence = m.group(1)[0]
            out.append(line)
        elif fence is not None:
            if m and m.group(1)[0] == fence:
                fence = None
            out.append(line)
        else:
            out.append(_rewrite_line(line, dst_dir, bundle_root))
    return "".join(out)


def copy_into_bundle(origin: Path, dest: Path, bundle_root: Path) -> None:
    """``shutil.copy2`` that rewrites links when the file is markdown."""
    if origin.suffix.lower() != ".md":
        shutil.copy2(origin, dest)
        return
    text = origin.read_bytes().decode("utf-8")
    dest.write_bytes(rewrite_markdown_links(text, dest.parent, bundle_root).encode("utf-8"))
    shutil.copystat(origin, dest)
