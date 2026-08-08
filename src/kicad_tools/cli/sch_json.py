"""Machine-output helpers for the mutating ``kct sch`` subcommands.

Issue #4674 (mechanical follow-up to #4543) adds the canonical
``--format json`` idiom to the prose-only subcommands.  The ``sch`` mutating
family is unusual in that its 16 leaves live in 16 separate inner modules
(``sch_add_component.py``, ``sch_set_value.py``, ...) that each grew their own
free-form prose reporting.  Rather than rewrite 16 report writers, this module
provides the one shared wrapper they all route through.

Contract (per ``docs/reference/machine-output.md``)::

    {
      "command":   "add-junction",          # the sch leaf name
      "schematic": "board.kicad_sch",       # the file operated on
      "dry_run":   true,                    # was this a preview?
      "success":   true,                    # exit code == 0
      ...                                   # per-command change summary
      "error":     "..."                    # only when success is false
    }

Exactly one JSON document goes to stdout; the command's human prose is
captured and discarded, and its stderr diagnostics are replayed to the real
stderr *after* the document so the machine channel stays clean.  Exit codes
are unchanged in both modes, and text mode is byte-identical to before
(:func:`run_with_json_summary` calls the runner directly when the format is
not ``json``).

Inner modules add their per-command change summary with :func:`record` /
:func:`append`, which are no-ops outside an active JSON emission -- so the
same call sites work unmodified in text mode.
"""

from __future__ import annotations

import contextlib
import io
import sys
from collections.abc import Callable
from typing import Any

from kicad_tools.cli.format_options import FORMAT_JSON, add_format_flag, emit_json

__all__ = [
    "add_format_flag",
    "append",
    "record",
    "run_with_json_summary",
    "wants_json",
]


# Stack of in-flight change summaries.  A stack (rather than a single slot)
# keeps nesting well-defined if one sch command ever drives another.
_ACTIVE: list[dict[str, Any]] = []


def wants_json(output_format: str | None) -> bool:
    """Return ``True`` when *output_format* selects the machine channel."""
    return output_format == FORMAT_JSON


def record(**fields: Any) -> None:
    """Merge *fields* into the enclosing JSON change summary.

    No-op when no emission is active (i.e. in text mode), so inner modules
    can call it unconditionally.
    """
    if _ACTIVE:
        _ACTIVE[-1].update(fields)


def append(key: str, item: Any) -> None:
    """Append *item* to the list at *key* in the enclosing change summary.

    No-op in text mode, like :func:`record`.
    """
    if _ACTIVE:
        _ACTIVE[-1].setdefault(key, []).append(item)


def _emit(
    command: str,
    schematic: Any,
    dry_run: bool,
    returncode: int,
    fields: dict[str, Any],
    out_buf: io.StringIO,
    err_buf: io.StringIO,
) -> None:
    """Print the single JSON document, then replay captured stderr."""
    payload: dict[str, Any] = {
        "command": command,
        "schematic": str(schematic),
        "dry_run": bool(dry_run),
        "success": returncode == 0,
    }
    payload.update(fields)
    if returncode != 0 and "error" not in payload:
        # Inner modules report failures as prose; surface the last thing they
        # said as the structured error rather than losing it.
        message = err_buf.getvalue().strip() or out_buf.getvalue().strip()
        payload["error"] = message or f"sch {command} failed"
    emit_json(payload)
    diagnostics = err_buf.getvalue()
    if diagnostics:
        sys.stderr.write(diagnostics)


def run_with_json_summary(
    command: str,
    schematic: Any,
    runner: Callable[[], int | None],
    *,
    output_format: str | None = "text",
    dry_run: bool = False,
) -> int:
    """Run *runner*, emitting a JSON change summary when JSON was requested.

    Args:
        command: The ``sch`` leaf name (``"add-junction"``, ``"set-value"``, ...).
        schematic: The schematic path the command operates on.
        runner: Zero-arg callable performing the command; returns an exit code
            (``None`` is treated as ``0``, matching the family's convention).
        output_format: The resolved ``--format`` value.
        dry_run: Whether this invocation was a preview.

    Returns:
        The runner's exit code, unchanged in both modes.
    """
    if not wants_json(output_format):
        return runner() or 0

    fields: dict[str, Any] = {}
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    _ACTIVE.append(fields)
    try:
        try:
            with contextlib.redirect_stdout(out_buf), contextlib.redirect_stderr(err_buf):
                returncode = runner() or 0
        except SystemExit as exc:
            # Several inner modules exit rather than return; still emit the
            # document (with the same exit code) before unwinding.
            code = exc.code
            returncode = code if isinstance(code, int) else (0 if code is None else 1)
            _emit(command, schematic, dry_run, returncode, fields, out_buf, err_buf)
            raise
        except BaseException:
            # Never swallow diagnostics when an unexpected error escapes.
            sys.stderr.write(err_buf.getvalue())
            raise
    finally:
        _ACTIVE.pop()

    _emit(command, schematic, dry_run, returncode, fields, out_buf, err_buf)
    return returncode
