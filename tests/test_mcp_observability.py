"""Tests for MCP call observability (issue #4897).

Covers the bounded ring buffer / call recorder in
``kicad_tools.mcp.observability`` and the ``get_recent_calls`` introspection
tool that exposes it, including the two dispatch-boundary wrappers in
``kicad_tools.mcp.server`` (stdio ``MCPServer.call_tool`` and the FastMCP
tool wrapper).
"""

from __future__ import annotations

import pytest

from kicad_tools.mcp.observability import (
    CALL_RECORDER,
    CallRecorder,
    classify_error,
    record_call,
)
from kicad_tools.mcp.tools.observability import get_recent_calls


class TestClassifyError:
    """Tests for the error_kind classification heuristics."""

    def test_none_message_and_exception_defaults_handler_reported(self):
        assert classify_error(None, None) == "handler_reported"

    def test_file_not_found_exception(self):
        assert classify_error(FileNotFoundError("board.kicad_pcb missing")) == "not_found"

    def test_value_error_is_validation(self):
        assert classify_error(ValueError("bad argument")) == "validation"

    def test_timeout_error(self):
        assert classify_error(TimeoutError("operation timed out")) == "timeout"

    def test_import_error_is_dependency(self):
        assert classify_error(ImportError("shapely is not installed")) == "dependency"

    def test_permission_error(self):
        assert classify_error(PermissionError("access denied")) == "permission"

    def test_unrecognized_exception_falls_back_to_internal(self):
        assert classify_error(RuntimeError("something broke")) == "internal"

    def test_dict_based_failure_classified_from_message(self):
        assert classify_error(None, "Net 'VCC' not found in design") == "not_found"
        assert classify_error(None, "Missing required field: pcb_path") == "validation"
        assert classify_error(None, "Permission denied for /etc/shadow") == "permission"

    def test_dict_based_failure_with_unrecognized_message(self):
        assert classify_error(None, "the sky is falling") == "handler_reported"


class TestCallRecorder:
    """Tests for the bounded ring buffer + aggregate stats."""

    def test_rejects_non_positive_capacity(self):
        with pytest.raises(ValueError):
            CallRecorder(capacity=0)

    def test_record_success_appends_ok_entry(self):
        recorder = CallRecorder(capacity=10)
        recorder.record_success("route_net", 12.5)

        calls = recorder.recent_calls()
        assert len(calls) == 1
        record = calls[0]
        assert record.tool_name == "route_net"
        assert record.status == "ok"
        assert record.error_kind is None
        assert record.error_message is None
        assert record.duration_ms == pytest.approx(12.5)

    def test_record_exception_captures_kind_and_message(self):
        """A bare ValueError with a specific message classifies by message
        content (not_found), not by its generic base-class type name."""
        recorder = CallRecorder(capacity=10)
        recorder.record_exception("route_net", 5.0, ValueError("Net 'GND' not found"))

        record = recorder.recent_calls()[0]
        assert record.status == "error"
        assert record.error_kind == "not_found"
        assert "not found" in (record.error_message or "")

    def test_record_handler_error_captures_kind_and_message(self):
        recorder = CallRecorder(capacity=10)
        recorder.record_handler_error("export_gerbers", 3.0, "Schematic file not found: x.sch")

        record = recorder.recent_calls()[0]
        assert record.status == "error"
        assert record.error_kind == "not_found"
        assert record.error_message == "Schematic file not found: x.sch"

    def test_ring_buffer_bounds_growth(self):
        """The buffer never grows past its configured capacity."""
        recorder = CallRecorder(capacity=5)
        for i in range(1000):
            recorder.record_success(f"tool_{i}", 1.0)

        assert len(recorder.recent_calls()) == 5
        assert recorder.stats()["buffer_size"] == 5
        assert recorder.stats()["buffer_capacity"] == 5

    def test_ring_buffer_wraparound_keeps_most_recent(self):
        """Once full, the oldest entry is evicted -- the buffer always
        holds the *most recent* N calls, not the first N."""
        recorder = CallRecorder(capacity=3)
        for i in range(10):
            recorder.record_success(f"tool_{i}", 1.0)

        calls = recorder.recent_calls()
        # Most-recent-first: tool_9, tool_8, tool_7
        assert [c.tool_name for c in calls] == ["tool_9", "tool_8", "tool_7"]

    def test_all_time_totals_survive_wraparound(self):
        """Aggregate totals are not bounded by the buffer -- they count
        every call ever made, even ones already evicted from the buffer."""
        recorder = CallRecorder(capacity=2)
        for i in range(50):
            recorder.record_success("route_net", 1.0)
        for _ in range(7):
            recorder.record_exception("route_net", 1.0, ValueError("bad"))

        stats = recorder.stats()
        assert stats["total_calls"] == 57
        assert stats["total_errors"] == 7
        assert stats["calls_by_tool"]["route_net"] == 57
        assert stats["errors_by_kind"]["validation"] == 7
        assert stats["error_rate"] == pytest.approx(7 / 57)
        # But the buffer itself only ever holds `capacity` entries.
        assert stats["buffer_size"] == 2

    def test_recent_calls_filters_by_tool_name(self):
        recorder = CallRecorder(capacity=10)
        recorder.record_success("route_net", 1.0)
        recorder.record_success("export_gerbers", 1.0)
        recorder.record_success("route_net", 1.0)

        filtered = recorder.recent_calls(tool_name="route_net")
        assert len(filtered) == 2
        assert all(c.tool_name == "route_net" for c in filtered)

    def test_recent_calls_filters_by_status(self):
        recorder = CallRecorder(capacity=10)
        recorder.record_success("route_net", 1.0)
        recorder.record_exception("route_net", 1.0, ValueError("bad"))

        errors = recorder.recent_calls(status="error")
        assert len(errors) == 1
        assert errors[0].status == "error"

    def test_recent_calls_respects_limit(self):
        recorder = CallRecorder(capacity=10)
        for i in range(5):
            recorder.record_success(f"tool_{i}", 1.0)

        assert len(recorder.recent_calls(limit=2)) == 2

    def test_error_message_truncated(self):
        recorder = CallRecorder(capacity=10)
        huge_message = "x" * 5000
        recorder.record_handler_error("tool", 1.0, huge_message)

        record = recorder.recent_calls()[0]
        assert record.error_message is not None
        assert len(record.error_message) < 5000
        assert record.error_message.endswith("...(truncated)")

    def test_clear_resets_everything(self):
        recorder = CallRecorder(capacity=10)
        recorder.record_success("tool", 1.0)
        recorder.clear()

        assert recorder.recent_calls() == []
        stats = recorder.stats()
        assert stats["total_calls"] == 0
        assert stats["buffer_size"] == 0


class TestRecordCall:
    """Tests for the record_call() dispatch-boundary wrapper."""

    def test_successful_call_returns_result_and_records_ok(self):
        recorder = CallRecorder(capacity=10)

        def handler(args: dict) -> dict:
            return {"success": True, "value": args["x"] * 2}

        result = record_call("double", handler, {"x": 21}, recorder=recorder)

        assert result == {"success": True, "value": 42}
        record = recorder.recent_calls()[0]
        assert record.tool_name == "double"
        assert record.status == "ok"
        assert record.duration_ms >= 0.0

    def test_handler_reported_failure_is_recorded_but_result_passed_through(self):
        recorder = CallRecorder(capacity=10)

        def handler(_args: dict) -> dict:
            return {"success": False, "error": "board.kicad_pcb not found"}

        result = record_call("export_gerbers", handler, {}, recorder=recorder)

        assert result == {"success": False, "error": "board.kicad_pcb not found"}
        record = recorder.recent_calls()[0]
        assert record.status == "error"
        assert record.error_kind == "not_found"

    def test_raising_handler_is_recorded_and_exception_reraised(self):
        """A tool that raises rather than returning an error status: the
        call must still be recorded, AND the exception must propagate
        unchanged (record_call must not swallow it)."""
        recorder = CallRecorder(capacity=10)

        def handler(_args: dict) -> dict:
            raise ValueError("Net 'VCC' not found in design")

        with pytest.raises(ValueError, match="not found"):
            record_call("route_net", handler, {}, recorder=recorder)

        record = recorder.recent_calls()[0]
        assert record.tool_name == "route_net"
        assert record.status == "error"
        assert record.error_kind == "not_found"
        assert "not found" in (record.error_message or "")

    def test_default_recorder_is_the_process_wide_singleton(self):
        CALL_RECORDER.clear()

        def handler(_args: dict) -> dict:
            return {"success": True}

        record_call("noop", handler, {})

        assert len(CALL_RECORDER.recent_calls()) == 1
        CALL_RECORDER.clear()


class TestGetRecentCallsTool:
    """Tests for the get_recent_calls introspection MCP tool."""

    def setup_method(self):
        CALL_RECORDER.clear()

    def teardown_method(self):
        CALL_RECORDER.clear()

    def test_output_shape_when_empty(self):
        result = get_recent_calls()

        assert result["success"] is True
        assert result["calls"] == []
        stats = result["stats"]
        assert stats["total_calls"] == 0
        assert stats["buffer_capacity"] > 0

    def test_reports_recorded_calls(self):
        def ok_handler(_args: dict) -> dict:
            return {"success": True}

        def failing_handler(_args: dict) -> dict:
            raise FileNotFoundError("board.kicad_pcb missing")

        record_call("board_summary", ok_handler, {})
        with pytest.raises(FileNotFoundError):
            record_call("route_net", failing_handler, {})

        result = get_recent_calls()
        assert result["success"] is True
        assert len(result["calls"]) == 2
        names = {c["tool_name"] for c in result["calls"]}
        assert names == {"board_summary", "route_net"}

        statuses = {c["tool_name"]: c["status"] for c in result["calls"]}
        assert statuses["board_summary"] == "ok"
        assert statuses["route_net"] == "error"

        failing_record = next(c for c in result["calls"] if c["tool_name"] == "route_net")
        assert failing_record["error_kind"] == "not_found"

        stats = result["stats"]
        assert stats["total_calls"] == 2
        assert stats["total_errors"] == 1

    def test_filters_by_tool_name_and_status(self):
        def ok_handler(_args: dict) -> dict:
            return {"success": True}

        def failing_handler(_args: dict) -> dict:
            raise ValueError("bad input")

        record_call("a", ok_handler, {})
        record_call("b", ok_handler, {})
        with pytest.raises(ValueError):
            record_call("a", failing_handler, {})

        result = get_recent_calls(tool_name="a")
        assert {c["tool_name"] for c in result["calls"]} == {"a"}
        assert len(result["calls"]) == 2

        result = get_recent_calls(status="error")
        assert len(result["calls"]) == 1
        assert result["calls"][0]["status"] == "error"

    def test_invalid_status_filter_returns_failure(self):
        result = get_recent_calls(status="bogus")

        assert result["success"] is False
        assert "error" in result

    def test_limit_is_respected(self):
        def ok_handler(_args: dict) -> dict:
            return {"success": True}

        for i in range(5):
            record_call(f"tool_{i}", ok_handler, {})

        result = get_recent_calls(limit=2)
        assert len(result["calls"]) == 2


class TestServerDispatchIntegration:
    """Confirm both MCP dispatch boundaries in server.py route through the
    observability recorder, per the acceptance criteria in issue #4897."""

    def setup_method(self):
        CALL_RECORDER.clear()

    def teardown_method(self):
        CALL_RECORDER.clear()

    def test_mcpserver_call_tool_records_calls(self):
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()
        result = server.call_tool("list_mistake_categories", {})

        assert isinstance(result, dict)
        calls = CALL_RECORDER.recent_calls(tool_name="list_mistake_categories")
        assert len(calls) == 1
        assert calls[0].status == "ok"

    def test_mcpserver_call_tool_records_failures_via_handle_request(self):
        from kicad_tools.mcp.server import MCPServer

        server = MCPServer()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": "route_net", "arguments": {}},
            }
        )

        # route_net requires pcb_path; missing it should surface as a
        # JSON-RPC error while still being recorded as a failed call.
        assert "error" in response
        calls = CALL_RECORDER.recent_calls(tool_name="route_net")
        assert len(calls) == 1
        assert calls[0].status == "error"

    def test_get_recent_calls_tool_is_registered(self):
        from kicad_tools.mcp.tools.registry import get_tool

        tool = get_tool("get_recent_calls")
        assert tool is not None
        assert tool.category == "observability"
        assert callable(tool.handler)

        result = tool.handler({})
        assert result["success"] is True
        assert "calls" in result
        assert "stats" in result


@pytest.mark.parametrize(
    "name,args,kind",
    [
        ("missing_tool", {}, "not_found"),
        ("get_recent_calls", {"limit": "invalid"}, "validation"),
        ("route_net", {}, "validation"),
    ],
)
def test_stdio_rejected_dispatch_is_recorded(name, args, kind):
    from kicad_tools.mcp.server import MCPServer

    CALL_RECORDER.clear()
    try:
        response = MCPServer().handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {"name": name, "arguments": args},
            }
        )
        assert "error" in response
        assert CALL_RECORDER.stats()["total_calls"] == 1
        assert CALL_RECORDER.recent_calls()[0].error_kind == kind
    finally:
        CALL_RECORDER.clear()


def test_fastmcp_protocol_discovery_dispatch_and_diagnostics():
    import asyncio
    from datetime import timedelta

    pytest.importorskip("mcp")
    from mcp.shared.memory import create_connected_server_and_client_session

    from kicad_tools.mcp.server import create_fastmcp_server
    from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

    async def exercise():
        server = create_fastmcp_server(http_mode=True)
        async with create_connected_server_and_client_session(
            server, read_timeout_seconds=timedelta(seconds=10)
        ) as client:
            discovery = await client.list_tools()
            assert {t.name: t.inputSchema for t in discovery.tools} == {
                name: spec.parameters for name, spec in TOOL_REGISTRY.items()
            }
            initial = await client.call_tool("get_recent_calls", {})
            assert not initial.isError
            assert initial.structuredContent["calls"] == []
            assert CALL_RECORDER.stats()["total_calls"] == 1
            for name, args, kind in [
                ("missing_tool", {}, "not_found"),
                ("get_recent_calls", {"limit": "invalid"}, "validation"),
                ("route_net", {}, "validation"),
            ]:
                result = await client.call_tool(name, args)
                assert result.isError
                assert CALL_RECORDER.recent_calls()[0].error_kind == kind
            assert CALL_RECORDER.stats()["total_calls"] == 4
            filtered = await client.call_tool(
                "get_recent_calls",
                {
                    "limit": 1,
                    "tool_name": "route_net",
                    "status": "error",
                },
            )
            assert not filtered.isError
            assert len(filtered.structuredContent["calls"]) == 1
            assert filtered.structuredContent["calls"][0]["tool_name"] == "route_net"
            assert filtered.structuredContent["stats"]["total_errors"] == 3
            assert CALL_RECORDER.stats()["total_calls"] == 5
            # A handler-reported failure is also counted once.
            spec = TOOL_REGISTRY["list_mistake_categories"]
            from unittest.mock import patch

            with patch.object(spec, "handler", return_value={"success": False, "error": "timeout"}):
                # Construct after patch: the dispatcher snapshots registry handlers.
                other = create_fastmcp_server(http_mode=True)
                result = await other.call_tool(spec.name, {})
                assert result["success"] is False
            assert CALL_RECORDER.stats()["total_calls"] == 6
            assert CALL_RECORDER.recent_calls()[0].error_kind == "timeout"

    CALL_RECORDER.clear()
    try:
        asyncio.run(exercise())
    finally:
        CALL_RECORDER.clear()


def test_recorder_bounds_unique_name_counters_and_payloads():
    from kicad_tools.mcp.observability import MAX_TOOL_NAME_LENGTH, OTHER_TOOLS

    recorder = CallRecorder(capacity=3)
    for i in range(100):
        recorder.record_exception(f"{i}:" + "x" * 1000, 0, ValueError("Unknown tool"))
    stats = recorder.stats()
    assert stats["total_calls"] == stats["total_errors"] == 100
    assert stats["buffer_size"] == 3
    assert len(stats["calls_by_tool"]) == 4
    assert stats["calls_by_tool"][OTHER_TOOLS] == 97
    assert sum(stats["calls_by_tool"].values()) == 100
    assert all(len(r.tool_name) <= MAX_TOOL_NAME_LENGTH for r in recorder.recent_calls())


@pytest.mark.parametrize("fails", [False, True])
def test_recorded_start_precedes_handler_completion(monkeypatch, fails):
    from kicad_tools.mcp import observability

    recorder = CallRecorder()
    clock = {"now": 100.0}
    monkeypatch.setattr(observability.time, "time", lambda: clock["now"])
    monkeypatch.setattr(observability.time, "monotonic", lambda: clock["now"])

    def handler(args):
        clock["now"] = 103.0
        if fails:
            raise ValueError("invalid argument")
        return {"success": True}

    if fails:
        with pytest.raises(ValueError):
            record_call("test", handler, {}, recorder=recorder)
    else:
        record_call("test", handler, {}, recorder=recorder)
    record = recorder.recent_calls()[0]
    assert record.started_at == 100.0
    assert record.duration_ms == 3000.0


@pytest.mark.parametrize("transport", ["stdio", "fastmcp"])
@pytest.mark.parametrize("field", ["error", "error_message", "empty_error_fallback"])
@pytest.mark.parametrize("long_message", [False, True])
def test_dispatch_preserves_handler_diagnostics(monkeypatch, transport, field, long_message):
    import asyncio

    from kicad_tools.mcp.observability import MAX_ERROR_MESSAGE_LENGTH
    from kicad_tools.mcp.server import MCPServer, create_fastmcp_server
    from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

    if transport == "fastmcp":
        pytest.importorskip("mcp")
    message = "Strategy module not available: The 'cmaes' package is required for placement"
    if long_message:
        message += "x" * (MAX_ERROR_MESSAGE_LENGTH * 2)
    result = {
        "success": False,
        "error_message" if field == "empty_error_fallback" else field: message,
    }
    if field == "empty_error_fallback":
        result["error"] = ""
    spec = TOOL_REGISTRY["list_mistake_categories"]
    monkeypatch.setattr(spec, "handler", lambda args: result)
    server = MCPServer() if transport == "stdio" else create_fastmcp_server(http_mode=True)

    async def exercise():
        async def call(name, arguments):
            if transport == "stdio":
                return server.call_tool(name, arguments)
            return await server.call_tool(name, arguments)

        assert await call(spec.name, {}) == result
        history = await call("get_recent_calls", {"tool_name": spec.name})
        assert history["stats"]["total_calls"] == 1
        assert history["stats"]["total_errors"] == 1
        assert len(history["calls"]) == 1
        record = history["calls"][0]
        expected = (
            message[:MAX_ERROR_MESSAGE_LENGTH] + "...(truncated)" if long_message else message
        )
        assert record["error_message"] == expected
        assert record["error_kind"] == "dependency"
        assert CALL_RECORDER.stats()["total_calls"] == 2
        assert CALL_RECORDER.stats()["total_errors"] == 1

    CALL_RECORDER.clear()
    try:
        asyncio.run(exercise())
    finally:
        CALL_RECORDER.clear()
