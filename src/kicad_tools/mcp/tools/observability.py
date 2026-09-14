"""MCP tool exposing call observability (issue #4897).

Provides ``get_recent_calls``, an introspection tool an LLM (or a human
debugging a session) can call to see what recently happened at the MCP
dispatch boundary: which tools were called, how long they took, whether
they succeeded, and -- for failures -- a coarse ``error_kind`` bucket plus
the (truncated) error message. Backed by the bounded ring buffer in
:mod:`kicad_tools.mcp.observability`; see that module's docstring for the
data-model design note (buffer size, in-memory storage rationale, and the
full ``error_kind`` taxonomy).

This mirrors the *behavior* of similar introspection tools seen in other
MCP servers -- not their code. Implemented from scratch; see #4897/#4880.
"""

from __future__ import annotations

from typing import Any

from kicad_tools.mcp.observability import CALL_RECORDER


def get_recent_calls(
    limit: int | None = 20,
    tool_name: str | None = None,
    status: str | None = None,
) -> dict[str, Any]:
    """Return recent MCP tool calls and aggregate call statistics.

    Args:
        limit: Maximum number of call records to return, most-recent-first.
            Defaults to 20. Pass a larger value (up to the ring buffer's
            capacity, see ``stats.buffer_capacity`` in the response) to see
            further back; there is nothing further back than that, older
            entries have already been evicted from the bounded buffer.
        tool_name: If given, only include calls to this tool.
        status: If given, only include calls with this status --
            ``"ok"`` or ``"error"``.

    Returns:
        Dictionary with:
        {
            "success": True,
            "calls": [
                {
                    "tool_name": "route_net",
                    "started_at": 1737000000.123,
                    "duration_ms": 842.5,
                    "status": "error",
                    "error_kind": "validation",
                    "error_message": "Net 'VCC' not found in design"
                },
                ...
            ],
            "stats": {
                "total_calls": 137,
                "total_errors": 4,
                "error_rate": 0.0292,
                "calls_by_tool": {"route_net": 12, ...},
                "errors_by_kind": {"validation": 3, "not_found": 1},
                "buffer_capacity": 200,
                "buffer_size": 137
            }
        }

    Note:
        ``stats`` is all-time (since process start), independent of
        ``limit``/``tool_name``/``status`` filtering applied to ``calls``.
    """
    if status is not None and status not in ("ok", "error"):
        return {
            "success": False,
            "error": f"Invalid status filter: {status!r}. Expected 'ok' or 'error'.",
        }

    records = CALL_RECORDER.recent_calls(limit=limit, tool_name=tool_name, status=status)
    return {
        "success": True,
        "calls": [r.to_dict() for r in records],
        "stats": CALL_RECORDER.stats(),
    }
