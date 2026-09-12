"""Call observability for the MCP tool dispatch boundary.

Records a bounded history of MCP tool invocations (name, duration, status,
error kind) so a calling LLM -- or a human debugging a session -- can ask
"what just happened?" instead of re-deriving it from stderr logs. This is an
original implementation inspired by the *idea* of call-log/ring-buffer
introspection tools seen in other MCP servers; no code was ported or
vendored from any AGPL-3.0 source (see issue #4897 / #4880).

Design note (data model)
-------------------------
- **Storage**: a single in-process ``collections.deque(maxlen=N)`` -- no
  file/JSONL persistence. This process already has no durable state across
  restarts (sessions are checkpointed elsewhere via
  :mod:`kicad_tools.mcp.tools.context`), so an in-memory buffer is
  consistent with the rest of the server and avoids introducing disk I/O
  (and a retention/rotation policy) on the hot dispatch path.
- **Bound**: capacity defaults to :data:`DEFAULT_CAPACITY` (200) entries.
  ``deque(maxlen=...)`` silently evicts the oldest entry once full, which is
  exactly the "ring buffer" behavior this module is documented to provide --
  memory use is O(capacity), never O(calls-ever-made). Tool names are
  truncated to 200 characters; aggregate tool keys are capped at capacity
  plus one ``<other>`` overflow bucket, including arbitrary unknown names.
- **Concurrency**: a single :class:`threading.Lock` guards both the deque
  and the all-time counters. The stdio server dispatches one request at a
  time, but the FastMCP HTTP transport can serve concurrent requests, so the
  recorder must be safe under concurrent ``record_*`` calls regardless of
  transport.
- **error_kind taxonomy**: tool handlers in this codebase fail two ways --
  either they raise an exception, or they return a ``{"success": False,
  "error": "..."}`` dict (see e.g. ``kicad_tools.mcp.tools.workflow``). Both
  paths funnel through :func:`classify_error`, which buckets the failure
  into one of a small, fixed set of kinds (below) using message-substring
  heuristics first and the exception's type name as a fallback (message
  text tends to be more specific than a base-class name like ``ValueError``
  in this codebase -- see :func:`classify_error`'s docstring for why the
  priority runs that direction). This is intentionally coarse (a handful of
  buckets, not one per exception class in the codebase) so the taxonomy
  stays useful for triage without becoming another thing to keep in sync as
  new exception types are added elsewhere in the codebase:

    * ``"not_found"``    -- a referenced file/session/net/... does not exist
    * ``"validation"``   -- caller-supplied arguments were rejected
    * ``"parse"``        -- a KiCad file could not be parsed
    * ``"permission"``   -- access/auth/forbidden
    * ``"timeout"``      -- an operation exceeded a time budget
    * ``"dependency"``   -- an optional dependency/import is missing
    * ``"io"``           -- a lower-level OS/file I/O failure
    * ``"internal"``     -- uncategorized exception (fallback for `raise`)
    * ``"handler_reported"`` -- ``success: False`` with no exception raised
      and no more specific kind inferred from the message
    * ``None``           -- the call succeeded
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

#: Ring buffer capacity. Bounded and fixed at construction time -- see the
#: module docstring "Design note" for the rationale.
DEFAULT_CAPACITY = 200

#: Maximum length (characters) an error message is truncated to before
#: being stored, so a handler that raises with a huge payload (e.g. an
#: exception whose message embeds a full file dump) cannot inflate a single
#: ring-buffer entry unboundedly.
MAX_ERROR_MESSAGE_LENGTH = 500
MAX_TOOL_NAME_LENGTH = 200
OTHER_TOOLS = "<other>"

# Ordered (pattern, error_kind) heuristics. Checked in order against the
# lowercased exception type name (for exceptions) or the lowercased message
# text (for dict-based failures and as an exception fallback). First match
# wins, so more specific patterns are listed before more general ones.
_TYPE_NAME_PATTERNS: tuple[tuple[str, str], ...] = (
    ("notfound", "not_found"),
    ("timeout", "timeout"),
    ("permission", "permission"),
    ("forbidden", "permission"),
    ("auth", "permission"),
    ("parse", "parse"),
    # Note: "ModuleNotFoundError" is caught by the "notfound" pattern above
    # (checked first) before it would reach these -- that is deliberate,
    # a missing module genuinely was "not found".
    ("importerror", "dependency"),
    ("dependencymissing", "dependency"),
    ("validation", "validation"),
    ("value", "validation"),
    ("configuration", "validation"),
    ("ioerror", "io"),
    ("oserror", "io"),
    ("fileformat", "io"),
)

_MESSAGE_PATTERNS: tuple[tuple[str, str], ...] = (
    ("unknown tool", "not_found"),
    ("not found", "not_found"),
    ("no such file", "not_found"),
    ("timed out", "timeout"),
    ("timeout", "timeout"),
    ("forbidden", "permission"),
    ("unauthorized", "permission"),
    ("permission denied", "permission"),
    ("failed to parse", "parse"),
    ("parse error", "parse"),
    ("module not available", "dependency"),
    ("package is required", "dependency"),
    ("required", "validation"),
    ("invalid", "validation"),
)


def classify_error(exc: BaseException | None, message: str | None = None) -> str:
    """Classify a tool failure into one of the fixed error-kind buckets.

    Args:
        exc: The exception raised by the handler, or ``None`` if the
            handler instead returned a ``{"success": False, ...}`` dict.
        message: The failure message -- ``str(exc)`` for the raised-exception
            path, or the handler's ``"error"`` / ``"error_message"`` string for the dict path.

    Returns:
        One of the kinds documented in the module docstring's "error_kind
        taxonomy" section. Never returns ``None`` -- callers only invoke
        this on the failure path; success is represented by never calling
        it, not by a special return value.

    Priority: the message text is checked first, the exception's type name
    second. Messages tend to be more informative than the type name in this
    codebase -- most call sites raise generic ``ValueError``/``RuntimeError``
    with a descriptive message (e.g. ``raise ValueError(f"Net {name!r} not
    found in design")`` in ``kicad_tools.mcp.tools.routing``) rather than a
    dedicated exception subclass, so a type-name-first check would
    misclassify those as generic buckets keyed off the base class name
    (e.g. "value" -> validation) instead of the more specific signal the
    message actually carries.
    """
    text = (message or (str(exc) if exc is not None else "")).lower()
    for pattern, kind in _MESSAGE_PATTERNS:
        if pattern in text:
            return kind

    if exc is not None:
        type_name = type(exc).__name__.lower()
        for pattern, kind in _TYPE_NAME_PATTERNS:
            if pattern in type_name:
                return kind
        return "internal"

    return "handler_reported"


def _truncate(message: str | None) -> str | None:
    if message is None:
        return None
    if len(message) <= MAX_ERROR_MESSAGE_LENGTH:
        return message
    return message[:MAX_ERROR_MESSAGE_LENGTH] + "...(truncated)"


@dataclass(frozen=True)
class CallRecord:
    """A single recorded MCP tool invocation.

    Attributes:
        tool_name: Name of the tool that was called.
        started_at: Wall-clock epoch seconds when the call started
            (``time.time()``), for display/ordering purposes.
        duration_ms: Wall-clock duration of the call in milliseconds.
        status: ``"ok"`` or ``"error"``.
        error_kind: One of the buckets documented in the module docstring,
            or ``None`` when ``status == "ok"``.
        error_message: Truncated failure message, or ``None`` on success.
    """

    tool_name: str
    started_at: float
    duration_ms: float
    status: str
    error_kind: str | None = None
    error_message: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Serialize this record to a plain dict (introspection tool output)."""
        return {
            "tool_name": self.tool_name,
            "started_at": self.started_at,
            "duration_ms": round(self.duration_ms, 3),
            "status": self.status,
            "error_kind": self.error_kind,
            "error_message": self.error_message,
        }


@dataclass
class CallRecorder:
    """Bounded ring buffer + running totals for MCP tool call observability.

    Thread-safe: every public method acquires an internal lock, so this may
    be shared across concurrent request handlers (relevant for the FastMCP
    HTTP transport; the stdio transport is single-threaded but sharing the
    same class keeps the two transports' behavior identical).
    """

    capacity: int = DEFAULT_CAPACITY
    _buffer: deque[CallRecord] = field(init=False, repr=False)
    _lock: threading.Lock = field(init=False, repr=False, compare=False)
    _total_calls: int = field(init=False, default=0)
    _total_errors: int = field(init=False, default=0)
    _calls_by_tool: dict[str, int] = field(init=False, default_factory=dict)
    _errors_by_kind: dict[str, int] = field(init=False, default_factory=dict)

    def __post_init__(self) -> None:
        if self.capacity <= 0:
            raise ValueError(f"capacity must be positive, got {self.capacity}")
        self._buffer = deque(maxlen=self.capacity)
        self._lock = threading.Lock()

    def record_success(
        self, tool_name: str, duration_ms: float, *, started_at: float | None = None
    ) -> None:
        """Record a successful call."""
        self._record(tool_name, duration_ms, status="ok", started_at=started_at)

    def record_handler_error(
        self, tool_name: str, duration_ms: float, message: str, *, started_at: float | None = None
    ) -> None:
        """Record a call whose handler returned ``{"success": False, ...}``
        without raising (no exception object available to classify)."""
        kind = classify_error(None, message)
        self._record(
            tool_name,
            duration_ms,
            status="error",
            started_at=started_at,
            error_kind=kind,
            error_message=_truncate(message),
        )

    def record_exception(
        self,
        tool_name: str,
        duration_ms: float,
        exc: BaseException,
        *,
        started_at: float | None = None,
    ) -> None:
        """Record a call whose handler raised ``exc``."""
        kind = classify_error(exc)
        self._record(
            tool_name,
            duration_ms,
            status="error",
            started_at=started_at,
            error_kind=kind,
            error_message=_truncate(str(exc)),
        )

    def _record(
        self,
        tool_name: str,
        duration_ms: float,
        *,
        status: str,
        started_at: float | None = None,
        error_kind: str | None = None,
        error_message: str | None = None,
    ) -> None:
        record = CallRecord(
            tool_name=tool_name[:MAX_TOOL_NAME_LENGTH],
            started_at=started_at if started_at is not None else time.time(),
            duration_ms=duration_ms,
            status=status,
            error_kind=error_kind,
            error_message=error_message,
        )
        with self._lock:
            self._buffer.append(record)
            self._total_calls += 1
            # Bound aggregate cardinality as well as individual record payloads.
            # New names beyond capacity share one overflow counter.
            key = record.tool_name
            if key not in self._calls_by_tool and len(self._calls_by_tool) >= self.capacity:
                key = OTHER_TOOLS
            self._calls_by_tool[key] = self._calls_by_tool.get(key, 0) + 1
            if status == "error":
                self._total_errors += 1
                if error_kind is not None:
                    self._errors_by_kind[error_kind] = self._errors_by_kind.get(error_kind, 0) + 1

    def recent_calls(
        self,
        limit: int | None = None,
        tool_name: str | None = None,
        status: str | None = None,
    ) -> list[CallRecord]:
        """Return recorded calls, most-recent-first.

        Args:
            limit: Maximum number of records to return (after filtering).
                ``None`` returns everything currently buffered.
            tool_name: If given, only return calls to this tool.
            status: If given (``"ok"`` or ``"error"``), only return calls
                with this status.
        """
        with self._lock:
            records = list(self._buffer)
        records.reverse()
        if tool_name is not None:
            records = [r for r in records if r.tool_name == tool_name]
        if status is not None:
            records = [r for r in records if r.status == status]
        if limit is not None:
            records = records[:limit]
        return records

    def stats(self) -> dict[str, Any]:
        """Return all-time aggregate statistics.

        Note that these counters are all-time (since process start) and are
        *not* bounded by the ring buffer's capacity -- they track totals
        even for entries the buffer has already evicted. Tool-key cardinality
        is bounded to capacity plus one overflow bucket.
        """
        with self._lock:
            total_calls = self._total_calls
            total_errors = self._total_errors
            calls_by_tool = dict(self._calls_by_tool)
            errors_by_kind = dict(self._errors_by_kind)
            buffered = len(self._buffer)
        return {
            "total_calls": total_calls,
            "total_errors": total_errors,
            "error_rate": (total_errors / total_calls) if total_calls else 0.0,
            "calls_by_tool": calls_by_tool,
            "errors_by_kind": errors_by_kind,
            "buffer_capacity": self.capacity,
            "buffer_size": buffered,
        }

    def clear(self) -> None:
        """Reset all state (buffer, totals). Intended for tests."""
        with self._lock:
            self._buffer.clear()
            self._total_calls = 0
            self._total_errors = 0
            self._calls_by_tool.clear()
            self._errors_by_kind.clear()


#: Process-wide recorder shared by both MCP dispatch boundaries
#: (``MCPServer.call_tool`` and the shared FastMCP dispatch) in
#: ``kicad_tools.mcp.server``.
CALL_RECORDER = CallRecorder()


def record_call(
    tool_name: str,
    handler: Any,
    arguments: dict[str, Any],
    recorder: CallRecorder | None = None,
) -> Any:
    """Invoke ``handler(arguments)``, recording the outcome in ``recorder``
    (:data:`CALL_RECORDER` by default), then return (or re-raise) exactly
    what the handler returned (or raised).

    This is the single choke point both MCP dispatch boundaries
    (``MCPServer.call_tool`` for stdio, the FastMCP dispatch for HTTP) route
    through, so instrumentation only needs to be added in one place.

    Args:
        recorder: Override the recorder instance -- used by tests to
            observe recording in isolation without touching the process-wide
            singleton. Production call sites never pass this.
    """
    target = recorder if recorder is not None else CALL_RECORDER
    started_at = time.time()
    start = time.monotonic()
    try:
        result = handler(arguments)
    except Exception as exc:
        duration_ms = (time.monotonic() - start) * 1000
        target.record_exception(tool_name, duration_ms, exc, started_at=started_at)
        raise
    duration_ms = (time.monotonic() - start) * 1000
    if isinstance(result, dict) and result.get("success") is False:
        message = str(result.get("error") or result.get("error_message") or "unknown error")
        target.record_handler_error(tool_name, duration_ms, message, started_at=started_at)
    else:
        target.record_success(tool_name, duration_ms, started_at=started_at)
    return result
