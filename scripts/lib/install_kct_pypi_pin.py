#!/usr/bin/env python3
"""PyPI-mode dependency editor for ``scripts/install-kct.sh`` (issue #8077).

``install-kct.sh`` installs kicad-tools into a consumer repo from PyPI by
default. In that mode the consumer's ``pyproject.toml`` should carry::

    dependencies = ["kicad-tools[<extras>]>=<version>", ...]

and **no** ``[tool.uv.sources]`` kicad-tools entry -- ``uv.lock`` is the exact
pin. This helper reads and rewrites that file as text (so comments and
formatting elsewhere survive) and needs no kicad-tools install. Only quoted
strings that are *elements of a dependency array* are ever read or rewritten:
``[project] dependencies``, ``[project.optional-dependencies]`` and
``[dependency-groups]``. A ``"kicad-tools"`` in ``keywords``, a
``description``, a comment or any other value is never touched (#8081 review).
The result is re-parsed with ``tomllib`` (Python >= 3.11) or ``tomli`` before
anything is written; with neither available the helper refuses (exit 1).

Usage::

    install_kct_pypi_pin.py state <pyproject> <version>
        Prints one of ``match`` / ``mismatch`` / ``absent``:
          match    -- a kicad-tools requirement with a lower bound >= <version>
                      and no [tool.uv.sources] kicad-tools entry (no-op);
          mismatch -- a kicad-tools requirement exists but is a git/path/URL
                      pin, an exact or missing lower bound, a lower bound
                      below <version>, is missing from [project].dependencies
                      (only in an extra or a group), or a sources entry is
                      still present;
          absent   -- no kicad-tools requirement in any dependency array.

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
from typing import NamedTuple

PACKAGE = "kicad-tools"

# The *content* of a dependency-array string that is a kicad-tools requirement.
# The name may be spelled with any PEP 503 separator. After name[extras] the
# rest must be empty or open a PEP 508 clause (version specifier, marker or
# URL) -- so `kicad-tools-extra` and prose like "kicad-tools based ..." are
# not requirements. Group 1 = extras (with brackets, optional), 2 = the rest.
_REQ_RE = re.compile(
    r"\s*kicad[-_.]tools(?![A-Za-z0-9._-])\s*(\[[^\]]*\])?(\s*(?:[<>=!~;@].*)?)\s*",
    re.IGNORECASE | re.DOTALL,
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


class _Str(NamedTuple):
    """A quoted string literal found by :class:`_Scanner`."""

    start: int  # span of the literal, quotes included
    end: int
    quote: str
    value: str  # raw text between the quotes
    path: tuple[str, ...]  # TOML key path of the value it belongs to
    in_array: bool  # a direct element of an array
    multiline: bool


class _Arr(NamedTuple):
    open: int  # index of `[`
    close: int  # index of `]`
    path: tuple[str, ...]


class _Scanner:
    """Just enough of a TOML lexer to locate string literals by key path.

    Comments are skipped, so nothing inside one is ever reported. Malformed
    input raises ValueError (the caller then writes nothing).
    """

    def __init__(self, text: str) -> None:
        self.t = text
        self.n = len(text)
        self.strings: list[_Str] = []
        self.arrays: list[_Arr] = []
        self.tables: dict[tuple[str, ...], int] = {}  # header path -> line-end index
        i, table = 0, ()
        while True:
            i = self._ws(i, newlines=True)
            if i >= self.n:
                break
            if self.t[i] == "[":
                i, table = self._header(i)
            else:
                i = self._keyval(i, table)
            i = self._ws(i, newlines=False)
            if i < self.n and self.t[i] not in "\r\n":
                raise ValueError(f"unexpected {self.t[i]!r} at offset {i}")

    def _ch(self, i: int) -> str:
        return self.t[i] if i < self.n else ""

    def _ws(self, i: int, newlines: bool) -> int:
        while i < self.n:
            c = self.t[i]
            if c in " \t" or (newlines and c in "\r\n"):
                i += 1
            elif c == "#":
                nl = self.t.find("\n", i)
                i = self.n if nl < 0 else nl
            else:
                break
        return i

    def _header(self, i: int) -> tuple[int, tuple[str, ...]]:
        double = self.t.startswith("[[", i)
        keys, i = self._key(i + (2 if double else 1))
        i = self._ws(i, newlines=False)
        close = "]]" if double else "]"
        if not self.t.startswith(close, i):
            raise ValueError(f"malformed table header at offset {i}")
        i += len(close)
        path = tuple(keys)
        if not double:
            nl = self.t.find("\n", i)
            self.tables.setdefault(path, self.n if nl < 0 else nl)
        return i, path

    def _key(self, i: int) -> tuple[list[str], int]:
        keys: list[str] = []
        while True:
            i = self._ws(i, newlines=False)
            c = self._ch(i)
            if c in ('"', "'"):
                end, multiline, value = self._string(i)
                if multiline:
                    raise ValueError(f"multi-line string used as a key at offset {i}")
                keys.append(value)
                i = end
            else:
                m = _BARE_KEY_RE.match(self.t, i)
                if not m:
                    raise ValueError(f"expected a key at offset {i}")
                keys.append(m.group(0))
                i = m.end()
            i = self._ws(i, newlines=False)
            if self._ch(i) != ".":
                return keys, i
            i += 1

    def _keyval(self, i: int, base: tuple[str, ...]) -> int:
        keys, i = self._key(i)
        if self._ch(i) != "=":
            raise ValueError(f"expected '=' at offset {i}")
        i = self._ws(i + 1, newlines=False)
        return self._value(i, base + tuple(keys), in_array=False)

    def _string(self, i: int) -> tuple[int, bool, str]:
        q = self.t[i]
        basic = q == '"'
        if self.t.startswith(q * 3, i):
            j = i + 3
            while j < self.n:
                if basic and self.t[j] == "\\":
                    j += 2
                elif self.t.startswith(q * 3, j):
                    end = j + 3
                    while end < self.n and self.t[end] == q and end - j < 5:
                        end += 1
                    return end, True, self.t[i + 3 : end - 3]
                else:
                    j += 1
            raise ValueError(f"unterminated string at offset {i}")
        j = i + 1
        while j < self.n:
            c = self.t[j]
            if basic and c == "\\":
                j += 2
            elif c == q:
                return j + 1, False, self.t[i + 1 : j]
            elif c == "\n":
                break
            else:
                j += 1
        raise ValueError(f"unterminated string at offset {i}")

    def _value(self, i: int, path: tuple[str, ...], in_array: bool) -> int:
        c = self._ch(i)
        if c in ('"', "'"):
            end, multiline, value = self._string(i)
            self.strings.append(_Str(i, end, c, value, path, in_array, multiline))
            return end
        if c == "[":
            open_ = i
            i += 1
            while True:
                i = self._ws(i, newlines=True)
                if self._ch(i) == "]":
                    self.arrays.append(_Arr(open_, i, path))
                    return i + 1
                i = self._value(i, path, in_array=True)
                i = self._ws(i, newlines=True)
                if self._ch(i) == ",":
                    i += 1
                elif self._ch(i) != "]":
                    raise ValueError(f"expected ',' or ']' at offset {i}")
        if c == "{":
            i += 1
            while True:
                i = self._ws(i, newlines=True)
                if self._ch(i) == "}":
                    return i + 1
                i = self._keyval(i, path)
                i = self._ws(i, newlines=True)
                if self._ch(i) == ",":
                    i += 1
                elif self._ch(i) != "}":
                    raise ValueError(f"expected ',' or '}}' at offset {i}")
        j = i
        while j < self.n and self.t[j] not in ",]}#\r\n":
            j += 1
        if j == i:
            raise ValueError(f"expected a value at offset {i}")
        return j


_BARE_KEY_RE = re.compile(r"[A-Za-z0-9_-]+")
_MAIN_DEPS = ("project", "dependencies")


def _is_dep_array(path: tuple[str, ...]) -> bool:
    """``[project] dependencies``, an optional-dependencies extra, or a dependency group."""
    return (
        path == _MAIN_DEPS
        or (len(path) == 3 and path[:2] == ("project", "optional-dependencies"))
        or (len(path) == 2 and path[0] == "dependency-groups")
    )


def _requirements(text: str) -> list[tuple[_Str, re.Match[str]]]:
    """Every kicad-tools requirement that is an element of a dependency array."""
    out = []
    for s in _Scanner(text).strings:
        if not s.in_array or s.multiline or not _is_dep_array(s.path):
            continue
        m = _REQ_RE.fullmatch(s.value)
        if m:
            out.append((s, m))
    return out


def _in_main(reqs: list[tuple[_Str, re.Match[str]]]) -> bool:
    return any(s.path == _MAIN_DEPS for s, _ in reqs)


def state(text: str, version: str) -> str:
    reqs = _requirements(text)
    if not reqs:
        return "absent"
    if not _in_main(reqs) or _has_source_entry(text.splitlines()):
        return "mismatch"
    if all(_floor_satisfies(m.group(2), version) for _, m in reqs):
        return "match"
    return "mismatch"


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
    """Rewrite each kicad-tools requirement literal in place (last first)."""
    for lit, m in sorted(_requirements(text), key=lambda r: r[0].start, reverse=True):
        extras, rest = re.sub(r"\s+", "", m.group(1) or ""), m.group(2)
        if _floor_satisfies(rest, version):
            spec = rest.split(";", 1)[0].strip()
        else:
            spec = f">={version}"
        marker = ""
        if ";" in rest:
            marker = "; " + rest.split(";", 1)[1].strip()
        new = f"{lit.quote}{PACKAGE}{extras}{spec}{marker}{lit.quote}"
        text = text[: lit.start] + new + text[lit.end :]
    return text


def _add_requirement(text: str, version: str) -> str:
    """Insert ``"kicad-tools>=<version>"`` as the first ``[project].dependencies`` element."""
    req = f'"{PACKAGE}>={version}"'
    scan = _Scanner(text)
    arr = next((a for a in scan.arrays if a.path == _MAIN_DEPS), None)
    if arr is None:
        if any(s.path == _MAIN_DEPS for s in scan.strings):
            raise ValueError("[project].dependencies is not an array")
        line_end = scan.tables.get(("project",))
        if line_end is None:
            raise ValueError("pyproject.toml has no [project] table")
        return text[:line_end] + f"\ndependencies = [{req}]" + text[line_end:]
    body = text[arr.open + 1 : arr.close]
    if "\n" not in body:
        sep = ", " if body.strip() else ""
        return text[: arr.open + 1] + req + sep + body.lstrip() + text[arr.close :]
    # Multi-line array: a new first line, indented like the next element.
    nl = text.index("\n", arr.open)
    indent = "    "
    for line in text[nl + 1 : arr.close].splitlines():
        if line.strip() and not line.lstrip().startswith("#"):
            indent = line[: len(line) - len(line.lstrip())]
            break
    return text[: nl + 1] + f"{indent}{req},\n" + text[nl + 1 :]


def apply(text: str, version: str) -> str:
    trailing_nl = text.endswith("\n")
    reqs = _requirements(text)
    if reqs:
        text = _rewrite_requirements(text, version)
    if not _in_main(reqs):
        text = _add_requirement(text, version)
    lines = _remove_source_entry(text.splitlines())
    out = "\n".join(lines)
    if trailing_nl:
        out += "\n"
    return out


def _load_toml(text: str) -> dict:
    try:
        import tomllib
    except ImportError:  # Python < 3.11
        try:
            import tomli as tomllib  # type: ignore[no-redef]
        except ImportError:
            raise ValueError(
                "cannot verify the edit: needs Python >= 3.11 (tomllib) or the "
                "tomli package; nothing was written"
            ) from None
    try:
        return tomllib.loads(text)
    except tomllib.TOMLDecodeError as exc:
        raise ValueError(f"pyproject.toml does not parse: {exc}") from None


def _is_kct(req: object) -> bool:
    return isinstance(req, str) and _REQ_RE.fullmatch(req) is not None


def _comparable(data: dict) -> dict:
    """``data`` minus everything the helper is allowed to change."""
    import copy

    data = copy.deepcopy(data)
    project = data.get("project", {})
    if isinstance(project.get("dependencies"), list):
        project["dependencies"] = [d for d in project["dependencies"] if not _is_kct(d)]
        if not project["dependencies"]:
            del project["dependencies"]  # an added `dependencies = [...]` line
    for table in (project.get("optional-dependencies"), data.get("dependency-groups")):
        if isinstance(table, dict):
            for key, reqs in table.items():
                if isinstance(reqs, list):
                    table[key] = [d for d in reqs if not _is_kct(d)]
    tool = data.get("tool", {})
    uv = tool.get("uv", {})
    sources = uv.get("sources")
    if isinstance(sources, dict):
        for key in [k for k in sources if k.lower().replace("_", "-") == PACKAGE]:
            del sources[key]
        if not sources:
            del uv["sources"]
            if not uv:
                del tool["uv"]
                if not tool:
                    del data["tool"]
    return data


def _verify(old: str, new: str, version: str) -> None:
    """Refuse to write unless the edit did exactly what it claims.

    The new file must parse, carry a kicad-tools requirement with a ``>=``
    floor of at least ``version`` in ``[project].dependencies``, have no
    ``[tool.uv.sources]`` kicad-tools entry, and differ from the old file
    only in kicad-tools requirements and that sources entry.
    """
    before, after = _load_toml(old), _load_toml(new)
    deps = after.get("project", {}).get("dependencies", [])
    if not any(
        _is_kct(d) and _floor_satisfies(_REQ_RE.fullmatch(d).group(2), version)  # type: ignore[union-attr]
        for d in deps
    ):
        raise ValueError(f"no kicad-tools>={version} requirement in [project].dependencies")
    sources = after.get("tool", {}).get("uv", {}).get("sources", {})
    if any(k.lower().replace("_", "-") == PACKAGE for k in sources):
        raise ValueError(
            "a [tool.uv.sources] kicad-tools entry is still present in a form this "
            "installer cannot edit; remove it by hand and re-run"
        )
    if _comparable(before) != _comparable(after):
        raise ValueError("the rewrite would change fields other than the kicad-tools dependency")


def main(argv: list[str]) -> int:
    if len(argv) != 4 or argv[1] not in {"state", "apply"}:
        print(__doc__, file=sys.stderr)
        return 1
    cmd, path, version = argv[1], argv[2], argv[3]
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    if cmd == "state":
        try:
            print(state(text, version))
        except ValueError as exc:
            print(f"error: {exc}", file=sys.stderr)
            return 1
        return 0
    try:
        new = apply(text, version)
        _verify(text, new, version)
    except ValueError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    if state(new, version) != "match":
        print("error: could not rewrite the kicad-tools dependency safely", file=sys.stderr)
        return 1
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(new)
    for lit, _ in _requirements(new):
        print(lit.value)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
