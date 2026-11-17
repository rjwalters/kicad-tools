"""Render MCP server registrations for non-Claude harnesses (issue #5953).

``kct mcp setup --client codex|opencode`` registers ``kct mcp serve`` with
Codex CLI and opencode.  This module holds the *pure* part of that work: given
the existing config text (or ``None`` when the file does not exist) and the
server command, it returns the new text plus what changed.  Path resolution and
file I/O live in :mod:`kicad_tools.cli.commands.mcp`, so the renderers can be
golden-tested against strings without touching any real home directory.

Both renderers share three guarantees:

* **Preserve.** Every other key in the file -- other MCP servers, model
  settings, comments and layout (Codex TOML is edited textually, never
  re-serialized) -- survives.  Extra keys a user put on the ``kct`` entry itself
  (an ``env`` table, a ``timeout``) survive too; only the command is rewritten.
* **Idempotent.** When the entry already points at the same command, the input
  text is returned unchanged with status ``"unchanged"``.
* **Never clobber.** An unparseable existing file raises
  :class:`ClientConfigError` instead of being replaced.

Codex CLI (``codex mcp add kct -- <kct> mcp serve`` writes the same thing)::

    [mcp_servers.kct]
    command = "/abs/path/kct"
    args = ["mcp", "serve"]

opencode has two config schemas.  opencode v2 (verified against v2.0.22, which
is also what ``opencode mcp add`` writes) nests servers under ``mcp.servers``
and has no ``enabled`` key (it uses ``disabled``)::

    {"mcp": {"servers": {"kct": {"type": "local", "command": ["/abs/kct", "mcp", "serve"]}}}}

opencode v1 keys servers directly under ``mcp``.  v2 still *reads* that legacy
shape and migrates it, but v1 would mistake a v2 ``servers`` key for a server
named "servers", so the schema is selectable::

    {"mcp": {"kct": {"type": "local", "command": ["/abs/kct", "mcp", "serve"], "enabled": true}}}
"""

from __future__ import annotations

import copy
import json
import re
import sys
from typing import Any

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on 3.10
    import tomli as tomllib  # type: ignore[import-not-found]

__all__ = [
    "SERVER_NAME",
    "STATUS_ADDED",
    "STATUS_REPLACED",
    "STATUS_UNCHANGED",
    "OPENCODE_SCHEMAS",
    "ClientConfigError",
    "codex_entry",
    "opencode_entry",
    "parse_opencode_version",
    "render_codex_config",
    "render_opencode_config",
]

#: Name the server is registered under in Codex and opencode.
SERVER_NAME = "kct"

STATUS_ADDED = "added"
STATUS_REPLACED = "replaced"
STATUS_UNCHANGED = "unchanged"

#: Supported opencode config schemas.
OPENCODE_SCHEMAS = ("v2", "v1")


class ClientConfigError(ValueError):
    """The existing client config cannot be updated safely."""


# ---------------------------------------------------------------------------
# Codex CLI: $CODEX_HOME/config.toml
# ---------------------------------------------------------------------------


def codex_entry(command: str, args: list[str]) -> dict[str, Any]:
    """Return the ``[mcp_servers.kct]`` keys kct owns."""
    return {"command": command, "args": list(args)}


def _toml_str(value: str) -> str:
    # JSON basic strings are valid TOML basic strings (\", \\, \n, \uXXXX).
    return json.dumps(value)


def _codex_block_lines(entry: dict[str, Any]) -> list[str]:
    args = ", ".join(_toml_str(a) for a in entry["args"])
    return [
        f"command = {_toml_str(entry['command'])}\n",
        f"args = [{args}]\n",
    ]


def _scan_toml_value(line: str, depth: int, ml: str | None) -> tuple[int, str | None]:
    """Advance bracket depth / multi-line-string state across one line."""
    i = 0
    n = len(line)
    while i < n:
        if ml is not None:
            end = line.find(ml, i)
            if end == -1:
                return depth, ml
            i = end + 3
            ml = None
            continue
        ch = line[i]
        if ch == "#":
            break
        if line.startswith('"""', i) or line.startswith("'''", i):
            ml = line[i : i + 3]
            i += 3
            continue
        if ch == '"':
            i += 1
            while i < n and line[i] != '"':
                i += 2 if line[i] == "\\" else 1
            i += 1
            continue
        if ch == "'":
            end = line.find("'", i + 1)
            i = n if end == -1 else end + 1
            continue
        if ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
        i += 1
    return depth, ml


def _split_dotted(name: str) -> list[str]:
    parts = []
    for part in re.findall(r'"(?:[^"\\]|\\.)*"|\'[^\']*\'|[^.]+', name):
        part = part.strip()
        if len(part) >= 2 and part[0] == part[-1] and part[0] in "\"'":
            part = part[1:-1]
        parts.append(part)
    return parts


def _toml_statements(lines: list[str]) -> list[tuple[int, int, str, Any]]:
    """Split TOML lines into ``(start, end, kind, name)`` logical statements.

    ``kind`` is ``"trivia"`` (blank/comment), ``"table"`` (``[a.b]``),
    ``"array_table"`` (``[[a.b]]``) or ``"kv"``; ``name`` is the dotted key as a
    list of parts for headers and the bare key text for key/value pairs.  A
    multi-line value (array, inline table, multi-line string) spans all its
    lines, so a continuation line that happens to start with ``[`` is never
    mistaken for a table header.
    """
    out: list[tuple[int, int, str, Any]] = []
    i = 0
    n = len(lines)
    while i < n:
        stripped = lines[i].strip()
        if not stripped or stripped.startswith("#"):
            out.append((i, i + 1, "trivia", None))
            i += 1
            continue
        if stripped.startswith("["):
            is_array = stripped.startswith("[[")
            body = stripped[2:] if is_array else stripped[1:]
            close = body.find("]]" if is_array else "]")
            name = _split_dotted(body[:close] if close != -1 else body)
            out.append((i, i + 1, "array_table" if is_array else "table", name))
            i += 1
            continue
        start = i
        depth = 0
        ml: str | None = None
        while i < n:
            depth, ml = _scan_toml_value(lines[i], depth, ml)
            i += 1
            if depth <= 0 and ml is None:
                break
        key = stripped.split("=", 1)[0].strip()
        out.append((start, i, "kv", ".".join(_split_dotted(key))))
    return out


def _load_toml(text: str, what: str) -> dict[str, Any]:
    try:
        data: dict[str, Any] = tomllib.loads(text)
        return data
    except tomllib.TOMLDecodeError as exc:
        raise ClientConfigError(f"{what} is not valid TOML ({exc}); not modifying it") from exc


def render_codex_config(
    existing: str | None, command: str, args: list[str]
) -> tuple[str, str, dict[str, Any]]:
    """Return ``(new_text, status, entry)`` for a Codex ``config.toml``.

    ``existing`` is the current file text, or ``None`` when it does not exist.
    The edit is textual so comments, ordering and unrelated tables are kept
    byte-for-byte; the result is parsed back and compared with the expected
    data, and any mismatch raises :class:`ClientConfigError` rather than
    producing a config Codex would read differently.
    """
    entry = codex_entry(command, args)
    text = existing or ""
    data = _load_toml(text, "Codex config")

    servers = data.get("mcp_servers", {})
    if not isinstance(servers, dict):
        raise ClientConfigError("Codex config has a non-table `mcp_servers`; not modifying it")
    current = servers.get(SERVER_NAME)
    if current is not None and not isinstance(current, dict):
        raise ClientConfigError(
            f"Codex config has a non-table `mcp_servers.{SERVER_NAME}`; not modifying it"
        )
    if current is not None and all(current.get(k) == v for k, v in entry.items()):
        return text, STATUS_UNCHANGED, entry

    lines = text.splitlines(keepends=True)
    statements = _toml_statements(lines)
    header_idx = next(
        (
            idx
            for idx, (_s, _e, kind, name) in enumerate(statements)
            if kind == "table" and name == ["mcp_servers", SERVER_NAME]
        ),
        None,
    )

    if header_idx is None:
        if current is not None:
            raise ClientConfigError(
                f"Codex config defines `mcp_servers.{SERVER_NAME}` without a "
                f"`[mcp_servers.{SERVER_NAME}]` table header (dotted keys or an "
                "inline table); edit it by hand or remove it and re-run"
            )
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        if lines and lines[-1].strip():
            lines.append("\n")
        new_lines = lines + [f"[mcp_servers.{SERVER_NAME}]\n"] + _codex_block_lines(entry)
        status = STATUS_ADDED
    else:
        header_line = statements[header_idx][0]
        drop: set[int] = set()
        for start, end, kind, name in statements[header_idx + 1 :]:
            if kind in ("table", "array_table"):
                break
            if kind == "kv" and name in entry:
                drop.update(range(start, end))
        kept = [line for idx, line in enumerate(lines) if idx not in drop]
        # Header line index is unchanged: only lines after it were dropped.
        if not kept[header_line].endswith("\n"):
            kept[header_line] += "\n"
        new_lines = kept[: header_line + 1] + _codex_block_lines(entry) + kept[header_line + 1 :]
        status = STATUS_REPLACED

    new_text = "".join(new_lines)

    expected = copy.deepcopy(data)
    expected.setdefault("mcp_servers", {}).setdefault(SERVER_NAME, {}).update(entry)
    if _load_toml(new_text, "Rewritten Codex config") != expected:
        raise ClientConfigError(
            "Could not update the Codex config without changing other settings; "
            f"add the [mcp_servers.{SERVER_NAME}] table by hand"
        )
    return new_text, status, entry


# ---------------------------------------------------------------------------
# opencode: opencode.json
# ---------------------------------------------------------------------------


def opencode_entry(command: str, args: list[str], schema: str = "v2") -> dict[str, Any]:
    """Return the opencode ``local`` server entry for ``schema``."""
    if schema not in OPENCODE_SCHEMAS:
        raise ValueError(f"unknown opencode schema {schema!r}")
    entry: dict[str, Any] = {"type": "local", "command": [command, *args]}
    if schema == "v1":
        entry["enabled"] = True
    return entry


def parse_opencode_version(output: str) -> int | None:
    """Return the major version from ``opencode --version`` output, if any."""
    match = re.search(r"(\d+)\.\d+(?:\.\d+)?", output)
    return int(match.group(1)) if match else None


def render_opencode_config(
    existing: str | None, command: str, args: list[str], schema: str = "v2"
) -> tuple[str, str, dict[str, Any]]:
    """Return ``(new_text, status, entry)`` for an ``opencode.json``.

    ``schema="v2"`` writes ``mcp.servers.kct`` and removes a legacy
    ``mcp.kct`` entry if one exists (v2 would otherwise migrate both into the
    same slot).  ``schema="v1"`` writes ``mcp.kct``.  Keys a user added to an
    existing ``kct`` entry (``environment``, ``timeout``, ...) are kept.
    """
    desired = opencode_entry(command, args, schema)
    text = existing if existing is not None else ""
    if text.strip():
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ClientConfigError(
                f"opencode config is not plain JSON ({exc}); comments (JSONC) are "
                "not supported, so not modifying it"
            ) from exc
    else:
        data = {}
    if not isinstance(data, dict):
        raise ClientConfigError("opencode config is not a JSON object; not modifying it")

    mcp = data.get("mcp", {})
    if not isinstance(mcp, dict):
        raise ClientConfigError("opencode config has a non-object `mcp`; not modifying it")

    if schema == "v2":
        servers = mcp.get("servers", {})
        if not isinstance(servers, dict):
            raise ClientConfigError(
                "opencode config has a non-object `mcp.servers`; not modifying it"
            )
        legacy = mcp.get(SERVER_NAME)
        current = servers.get(SERVER_NAME)
        base = current if isinstance(current, dict) else {}
        if not base and isinstance(legacy, dict):
            base = {k: v for k, v in legacy.items() if k != "enabled"}
        merged = {**base, **desired}
        if current == merged and legacy is None:
            return text, STATUS_UNCHANGED, merged
        status = STATUS_REPLACED if (current is not None or legacy is not None) else STATUS_ADDED
        new_mcp = {k: v for k, v in mcp.items() if k != SERVER_NAME}
        new_servers = dict(servers)
        new_servers[SERVER_NAME] = merged
        new_mcp["servers"] = new_servers
    else:
        if isinstance(mcp.get("servers"), dict) and "type" not in mcp["servers"]:
            raise ClientConfigError(
                "opencode config uses the v2 `mcp.servers` layout, which opencode v1 "
                "cannot read; re-run with --opencode-schema v2"
            )
        current = mcp.get(SERVER_NAME)
        base = current if isinstance(current, dict) else {}
        merged = {**base, **desired}
        if current == merged:
            return text, STATUS_UNCHANGED, merged
        status = STATUS_REPLACED if current is not None else STATUS_ADDED
        new_mcp = dict(mcp)
        new_mcp[SERVER_NAME] = merged

    new_data = dict(data)
    new_data["mcp"] = new_mcp
    return json.dumps(new_data, indent=2) + "\n", status, merged
