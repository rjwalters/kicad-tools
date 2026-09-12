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
