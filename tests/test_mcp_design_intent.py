"""Tests for the get_design_intent MCP tool.

Covers direct function behavior (presence combinations, normalization,
validation errors), registry discoverability, and tools/call dispatch
through the MCP server -- including structured error behavior.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

pytest.importorskip("pydantic")
pytest.importorskip("yaml")

import pydantic

from kicad_tools.mcp.tools.design_intent import get_design_intent
from kicad_tools.mcp.tools.registry import TOOL_REGISTRY, get_tool, list_tools

MINIMAL_HEADER = 'kct_version: "1.0"\n\nproject:\n  name: "Test Project"\n'


def _write(tmp_path: Path, name: str, content: str) -> Path:
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return path


class TestPresenceCombinations:
    """Constraints-only, decisions-only, both, neither, and absent intent."""

    def test_constraints_only(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + (
            "\nintent:\n"
            "  summary: A board that does things.\n"
            "  constraints:\n"
            "    - Must fit a 2-layer JLCPCB stackup\n"
            "    - No components taller than 5mm\n"
        )
        path = _write(tmp_path, "project.kct", content)

        result = get_design_intent(str(path))

        assert result["summary"] == "A board that does things."
        assert result["constraints"] == [
            "Must fit a 2-layer JLCPCB stackup",
            "No components taller than 5mm",
        ]
        assert result["decisions"] == []

    def test_decisions_only(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + (
            "\ndecisions:\n"
            "  - topic: MCU selection\n"
            "    choice: RP2040\n"
            "    rationale: Cheap, well-documented, dual core\n"
        )
        path = _write(tmp_path, "project.kct", content)

        result = get_design_intent(str(path))

        assert result["summary"] is None
        assert result["constraints"] == []
        assert len(result["decisions"]) == 1
        assert result["decisions"][0]["topic"] == "MCU selection"
        assert result["decisions"][0]["choice"] == "RP2040"
        assert result["decisions"][0]["rationale"] == "Cheap, well-documented, dual core"

    def test_both_constraints_and_decisions(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + (
            "\nintent:\n"
            "  summary: Board with both.\n"
            "  constraints:\n"
            "    - Single 3.3V rail only\n"
            "\ndecisions:\n"
            "  - topic: Connector\n"
            "    choice: USB-C\n"
            "    rationale: Modern, reversible\n"
        )
        path = _write(tmp_path, "project.kct", content)

        result = get_design_intent(str(path))

        assert result["summary"] == "Board with both."
        assert result["constraints"] == ["Single 3.3V rail only"]
        assert len(result["decisions"]) == 1
        assert result["decisions"][0]["topic"] == "Connector"

    def test_neither_constraints_nor_decisions(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + "\nintent:\n  summary: Bare-bones board.\n"
        path = _write(tmp_path, "project.kct", content)

        result = get_design_intent(str(path))

        assert result["summary"] == "Bare-bones board."
        assert result["constraints"] == []
        assert result["decisions"] == []

    def test_absent_intent(self, tmp_path: Path) -> None:
        """No intent block at all -- summary is None, arrays are empty."""
        content = MINIMAL_HEADER
        path = _write(tmp_path, "project.kct", content)

        result = get_design_intent(str(path))

        assert result["summary"] is None
        assert result["constraints"] == []
        assert result["decisions"] == []


class TestNullNormalization:
    """Explicit null constraints/decisions normalize to empty arrays."""

    def test_explicit_null_constraints(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + "\nintent:\n  summary: Board.\n  constraints: null\n"
        path = _write(tmp_path, "project.kct", content)

        result = get_design_intent(str(path))

        assert result["constraints"] == []

    def test_explicit_null_decisions(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + "\ndecisions: null\n"
        path = _write(tmp_path, "project.kct", content)

        result = get_design_intent(str(path))

        assert result["decisions"] == []


class TestValidMinimalProject:
    def test_valid_minimal_project(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "project.kct", MINIMAL_HEADER)

        result = get_design_intent(str(path))

        assert result["spec_path"] == str(path.absolute())
        assert result["summary"] is None
        assert result["constraints"] == []
        assert result["decisions"] == []


class TestInvalidInput:
    """Malformed/invalid input must raise, never return a successful empty result."""

    def test_invalid_present_intent_missing_summary(self, tmp_path: Path) -> None:
        # intent block present but missing its required `summary` field.
        content = MINIMAL_HEADER + "\nintent:\n  constraints:\n    - Some constraint\n"
        path = _write(tmp_path, "project.kct", content)

        with pytest.raises(pydantic.ValidationError):
            get_design_intent(str(path))

    def test_malformed_yaml(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + "\nintent: [unterminated\n"
        path = _write(tmp_path, "project.kct", content)

        with pytest.raises(ValueError):
            get_design_intent(str(path))

    def test_empty_document(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "project.kct", "")

        with pytest.raises(ValueError):
            get_design_intent(str(path))

    def test_non_mapping_document(self, tmp_path: Path) -> None:
        path = _write(tmp_path, "project.kct", "- a\n- list\n- not a mapping\n")

        with pytest.raises(ValueError):
            get_design_intent(str(path))

    def test_missing_file(self, tmp_path: Path) -> None:
        missing = tmp_path / "does_not_exist.kct"

        with pytest.raises(FileNotFoundError):
            get_design_intent(str(missing))

    def test_directory_input(self, tmp_path: Path) -> None:
        directory = tmp_path / "a_directory.kct"
        directory.mkdir()

        with pytest.raises(OSError):
            get_design_intent(str(directory))


class TestDecisionFidelity:
    """Decision ordering, fields, and date serialization."""

    def test_decision_order_and_fields_preserved(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + (
            "\ndecisions:\n"
            "  - topic: First\n"
            "    choice: A\n"
            "    rationale: Because A\n"
            "    date: 2026-01-15\n"
            "    phase: concept\n"
            "    author: robb\n"
            "    alternatives:\n"
            "      - B\n"
            "      - C\n"
            "  - topic: Second\n"
            "    choice: D\n"
            "    rationale: Because D\n"
        )
        path = _write(tmp_path, "project.kct", content)

        result = get_design_intent(str(path))

        decisions = result["decisions"]
        assert [d["topic"] for d in decisions] == ["First", "Second"]

        first = decisions[0]
        assert first["choice"] == "A"
        assert first["rationale"] == "Because A"
        assert first["date"] == "2026-01-15"
        assert first["phase"] == "concept"
        assert first["author"] == "robb"
        assert first["alternatives"] == ["B", "C"]

        second = decisions[1]
        assert second["choice"] == "D"
        assert "date" not in second  # optional field absent in source stays absent

    def test_dated_decisions_are_json_serializable(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + (
            "\ndecisions:\n  - topic: T\n    choice: C\n    rationale: R\n    date: 2025-12-25\n"
        )
        path = _write(tmp_path, "project.kct", content)

        result = get_design_intent(str(path))

        json_str = json.dumps(result)
        reparsed = json.loads(json_str)
        assert reparsed["decisions"][0]["date"] == "2025-12-25"


class TestSourcePreservation:
    def test_source_bytes_unchanged(self, tmp_path: Path) -> None:
        content = MINIMAL_HEADER + (
            "\nintent:\n  summary: Preserve me.\n  constraints:\n    - Keep this\n"
            "\ndecisions:\n"
            "  - topic: T\n    choice: C\n    rationale: R\n"
        )
        path = _write(tmp_path, "project.kct", content)
        before = path.read_bytes()

        get_design_intent(str(path))

        after = path.read_bytes()
        assert before == after


class TestRegistryDiscoverability:
    """tools/list discoverability."""

    def test_registered_under_context_category(self) -> None:
        tool = get_tool("get_design_intent")

        assert tool is not None
        assert tool.category == "context"

    def test_in_context_category_listing(self) -> None:
        context_tools = list_tools(category="context")
        names = [t.name for t in context_tools]

        assert "get_design_intent" in names

    def test_registered_in_tool_registry(self) -> None:
        assert "get_design_intent" in TOOL_REGISTRY

    def test_has_spec_path_parameter(self) -> None:
        tool = get_tool("get_design_intent")

        assert tool is not None
        assert "spec_path" in tool.parameters["properties"]
        assert tool.parameters["required"] == ["spec_path"]

    def test_description_distinguishes_constraints_and_decisions(self) -> None:
        tool = get_tool("get_design_intent")

        assert tool is not None
        description = tool.description.lower()
        assert "constraint" in description
        assert "decision" in description


class TestToolCallDispatch:
    """Exercise actual tools/call dispatch through the MCP server."""

    def test_tools_call_success(self, tmp_path: Path) -> None:
        from kicad_tools.mcp.server import create_server

        content = MINIMAL_HEADER + "\nintent:\n  summary: Dispatch test.\n"
        path = _write(tmp_path, "project.kct", content)

        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 1,
                "method": "tools/call",
                "params": {
                    "name": "get_design_intent",
                    "arguments": {"spec_path": str(path)},
                },
            }
        )

        assert "result" in response
        payload = json.loads(response["result"]["content"][0]["text"])
        assert payload["summary"] == "Dispatch test."
        assert payload["constraints"] == []
        assert payload["decisions"] == []

    def test_tools_call_missing_file_is_structured_error(self, tmp_path: Path) -> None:
        from kicad_tools.mcp.server import create_server

        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 2,
                "method": "tools/call",
                "params": {
                    "name": "get_design_intent",
                    "arguments": {"spec_path": str(tmp_path / "missing.kct")},
                },
            }
        )

        assert "error" in response
        assert "result" not in response

    def test_tools_call_malformed_yaml_is_structured_error(self, tmp_path: Path) -> None:
        from kicad_tools.mcp.server import create_server

        path = _write(tmp_path, "project.kct", MINIMAL_HEADER + "\nintent: [unterminated\n")

        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 3,
                "method": "tools/call",
                "params": {
                    "name": "get_design_intent",
                    "arguments": {"spec_path": str(path)},
                },
            }
        )

        assert "error" in response

    def test_tools_call_directory_is_structured_error(self, tmp_path: Path) -> None:
        from kicad_tools.mcp.server import create_server

        directory = tmp_path / "a_directory.kct"
        directory.mkdir()

        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 4,
                "method": "tools/call",
                "params": {
                    "name": "get_design_intent",
                    "arguments": {"spec_path": str(directory)},
                },
            }
        )

        assert "error" in response

    def test_tools_call_invalid_intent_missing_summary_is_structured_error(
        self, tmp_path: Path
    ) -> None:
        from kicad_tools.mcp.server import create_server

        content = MINIMAL_HEADER + "\nintent:\n  constraints:\n    - x\n"
        path = _write(tmp_path, "project.kct", content)

        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 5,
                "method": "tools/call",
                "params": {
                    "name": "get_design_intent",
                    "arguments": {"spec_path": str(path)},
                },
            }
        )

        assert "error" in response

    def test_get_design_intent_appears_in_tools_list(self) -> None:
        from kicad_tools.mcp.server import create_server

        server = create_server()
        response = server.handle_request(
            {
                "jsonrpc": "2.0",
                "id": 6,
                "method": "tools/list",
                "params": {},
            }
        )

        tool_names = [t["name"] for t in response["result"]["tools"]]
        assert "get_design_intent" in tool_names
