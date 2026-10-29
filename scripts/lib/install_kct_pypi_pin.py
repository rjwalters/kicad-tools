#!/usr/bin/env python3
"""PyPI-mode dependency editor for ``scripts/install-kct.sh`` (issue #8077).

``install-kct.sh`` installs kicad-tools into a consumer repo from PyPI by
default. In that mode the consumer's ``pyproject.toml`` should carry::

    dependencies = ["kicad-tools[<extras>]>=<version>", ...]

and **no** ``[tool.uv.sources]`` kicad-tools entry -- ``uv.lock`` is the exact
pin. This helper reads and rewrites that file as text (so comments and
formatting elsewhere survive) and is stdlib-only, so it runs under any
``python3`` without kicad-tools installed.

Usage::

    install_kct_pypi_pin.py state <pyproject> <version>
        Prints one of ``match`` / ``mismatch`` / ``absent``:
          match    -- a kicad-tools requirement with a lower bound >= <version>
                      and no [tool.uv.sources] kicad-tools entry (no-op);
          mismatch -- a kicad-tools requirement exists but is a git/path/URL
                      pin, an exact or missing lower bound, a lower bound
                      below <version>, or a sources entry is still present;
          absent   -- no kicad-tools requirement at all.

    install_kct_pypi_pin.py apply <pyproject> <version>
        Rewrites the file so the state becomes ``match``: every kicad-tools
        requirement string becomes ``kicad-tools[<its extras>]>=<version>``
        (an existing higher floor and any environment marker are kept), a
        requirement is added to ``[project].dependencies`` when absent, and the
        ``[tool.uv.sources]`` kicad-tools entry is removed (an emptied sources
        table is removed too). Prints the resulting requirement(s).

Exit status: 0 on success, 1 on a usage error or a file this cannot edit
safely (nothing is written in that case).
"""

from __future__ import annotations

import re
import sys

PACKAGE = "kicad-tools"

# A quoted kicad-tools requirement string. The name may be spelled with any
# PEP 503 separator; the negative lookahead keeps `kicad-tools-extra` out.
# Group 1 = quote, 2 = extras (with brackets, optional), 3 = the rest.
_REQ_RE = re.compile(
    r"""(["'])kicad[-_.]tools(?![A-Za-z0-9._-])\s*(\[[^\]"']*\])?((?:(?!\1)[^\n])*)\1(?!\s*=)""",
    re.IGNORECASE,
)
_LOWER_BOUND_RE = re.compile(r">=\s*v?([0-9][^\s,;]*)")
_HEADER_RE = re.compile(r"^\s*\[\[?\s*([^\]]+?)\s*\]\]?\s*(#.*)?$")
_SOURCE_KEY_RE = re.compile(r"""^\s*["']?kicad[-_.]tools["']?\s*=""", re.IGNORECASE)


def _version_tuple(version: str) -> tuple[int, ...]:
    m = re.match(r"v?(\d+(?:\.\d+)*)", version.strip())
    if not m:
        return ()
    parts = tuple(int(p) for p in m.group(1).split("."))
    while len(parts) > 1 and parts[-1] == 0:
        parts = parts[:-1]
    return parts


def _floor_satisfies(rest: str, version: str) -> bool:
    """Does a requirement's specifier part have a lower bound >= version?"""
    spec = rest.split(";", 1)[0]
    if "@" in spec or "==" in spec or "===" in spec:
        return False
    m = _LOWER_BOUND_RE.search(spec)
    if not m:
        return False
    floor = _version_tuple(m.group(1))
    return bool(floor) and floor >= _version_tuple(version)


def _table_name(line: str) -> str | None:
    """The table name if ``line`` is a TOML table header, else None."""
    if line.lstrip().startswith("#"):
        return None
    m = _HEADER_RE.match(line)
    if not m:
        return None
    return re.sub(r"\s*\.\s*", ".", m.group(1)).replace('"', "").replace("'", "")


def _requirements(text: str) -> list[re.Match[str]]:
    return list(_REQ_RE.finditer(text))


def _has_source_entry(lines: list[str]) -> bool:
    table = None
    for line in lines:
        name = _table_name(line)
        if name is not None:
            table = name
            if name.lower() in {"tool.uv.sources.kicad-tools", "tool.uv.sources.kicad_tools"}:
                return True
            continue
        if table == "tool.uv.sources" and _SOURCE_KEY_RE.match(line):
            return True
    return False


def state(text: str, version: str) -> str:
    reqs = _requirements(text)
    if not reqs:
        return "absent"
    if _has_source_entry(text.splitlines()):
        return "mismatch"
    if all(_floor_satisfies(m.group(3), version) for m in reqs):
        return "match"
    return "mismatch"


def _bracket_depth(s: str) -> int:
    """Net ``[`` minus ``]`` outside quoted strings (for multi-line values)."""
    depth, quote = 0, ""
    for ch in s:
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "\"'":
            quote = ch
        elif ch == "#":
            break
        elif ch == "[":
            depth += 1
        elif ch == "]":
            depth -= 1
    return depth


def _remove_source_entry(lines: list[str]) -> list[str]:
    out: list[str] = []
    table = None
    skipping_table = False
    i = 0
    while i < len(lines):
        line = lines[i]
        name = _table_name(line)
        if name is not None:
            table = name
            skipping_table = name.lower() in {
                "tool.uv.sources.kicad-tools",
                "tool.uv.sources.kicad_tools",
            }
            if skipping_table:
                i += 1
                continue
            out.append(line)
            i += 1
            continue
        if skipping_table:
            i += 1
            continue
        if table == "tool.uv.sources" and _SOURCE_KEY_RE.match(line):
            # Consume a multi-line array value (`kicad-tools = [ {...}, ... ]`).
            depth = _bracket_depth(line.split("=", 1)[1])
            i += 1
            while depth > 0 and i < len(lines):
                depth += _bracket_depth(lines[i])
                i += 1
            continue
        out.append(line)
        i += 1
    return _drop_empty_sources_table(out)


def _drop_empty_sources_table(lines: list[str]) -> list[str]:
    for idx, line in enumerate(lines):
        if _table_name(line) != "tool.uv.sources":
            continue
        end = idx + 1
        while end < len(lines) and _table_name(lines[end]) is None:
            end += 1
        body = [ln for ln in lines[idx + 1 : end] if ln.strip()]
        if body:
            return lines
        before = lines[:idx]
        after = lines[end:]
        # Collapse the blank run left where the table was.
        while before and not before[-1].strip():
            before.pop()
        if after:
            before.append("")
        trimmed = before + after
        return trimmed
    return lines


def _rewrite_requirements(text: str, version: str) -> str:
    def sub(m: re.Match[str]) -> str:
        quote, extras, rest = m.group(1), m.group(2) or "", m.group(3)
        extras = re.sub(r"\s+", "", extras)
        if _floor_satisfies(rest, version):
            spec = rest.split(";", 1)[0].strip()
        else:
            spec = f">={version}"
        marker = ""
        if ";" in rest:
            marker = "; " + rest.split(";", 1)[1].strip()
        return f"{quote}{PACKAGE}{extras}{spec}{marker}{quote}"

    return _REQ_RE.sub(sub, text)


def _add_requirement(lines: list[str], version: str) -> list[str]:
    req = f'"{PACKAGE}>={version}"'
    start = None
    for idx, line in enumerate(lines):
        if _table_name(line) == "project":
            start = idx
            break
    if start is None:
        raise ValueError("pyproject.toml has no [project] table")
    end = start + 1
    while end < len(lines) and _table_name(lines[end]) is None:
        end += 1
    for idx in range(start + 1, end):
        m = re.match(r"^(\s*dependencies\s*=\s*)\[(.*)$", lines[idx])
        if not m:
            continue
        head, tail = m.group(1), m.group(2)
        if _bracket_depth("[" + tail) == 0:
            # Single-line array: insert as the first element.
            inner = tail[: tail.rindex("]")]
            after = tail[tail.rindex("]") :]
            if inner.strip():
                lines[idx] = f"{head}[{req}, {inner.strip()}{after}"
            else:
                lines[idx] = f"{head}[{req}{after}"
            return lines
        # Multi-line array: insert after the opening line.
        indent = "    "
        for nxt in lines[idx + 1 : end]:
            if nxt.strip() and not nxt.strip().startswith("]"):
                indent = nxt[: len(nxt) - len(nxt.lstrip())]
                break
        lines.insert(idx + 1, f"{indent}{req},")
        return lines
    lines.insert(start + 1, f"dependencies = [{req}]")
    return lines


def apply(text: str, version: str) -> str:
    trailing_nl = text.endswith("\n")
    if _requirements(text):
        text = _rewrite_requirements(text, version)
        lines = text.splitlines()
    else:
        lines = _add_requirement(text.splitlines(), version)
    lines = _remove_source_entry(lines)
    out = "\n".join(lines)
    if trailing_nl:
        out += "\n"
    return out


def _verify(text: str) -> None:
    """Refuse to write a file that no longer parses (Python >= 3.11 only)."""
    try:
        import tomllib
    except ImportError:  # pragma: no cover - Python < 3.11
        return
    data = tomllib.loads(text)
    sources = data.get("tool", {}).get("uv", {}).get("sources", {})
    if any(k.lower().replace("_", "-") == PACKAGE for k in sources):
        raise ValueError(
            "a [tool.uv.sources] kicad-tools entry is still present in a form this "
            "installer cannot edit; remove it by hand and re-run"
        )


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in {"state", "apply"}:
        print(__doc__, file=sys.stderr)
        return 1
    cmd, path, version = argv[1], argv[2], argv[3]
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if cmd == "state":
        print(state(text, version))
        return 0
    try:
        new = apply(text, version)
        _verify(new)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if state(new, version) != "match":
        print("error: could not rewrite the kicad-tools dependency safely", file=sys.stderr)
        return 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    for m in _requirements(new):
        print(m.group(0).strip("\"'"))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
