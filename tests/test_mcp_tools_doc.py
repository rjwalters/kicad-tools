"""Keep docs/mcp/tools.md in sync with the registered MCP tools (Issue #7111)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from kicad_tools.mcp.tools.registry import TOOL_REGISTRY

ROOT = Path(__file__).resolve().parent.parent
TOOLS_MD = ROOT / "docs" / "mcp" / "tools.md"

# Registered tools that tools.md deliberately does not document yet.
# Each entry needs a reason; shrink this list rather than growing it.
_PENDING = "registered after tools.md was written; docs backfill pending (Issue #7111 follow-up)"
UNDOCUMENTED_ALLOWLIST: dict[str, str] = {
    "adapt_pattern": _PENDING,
    "annotate_decision": _PENDING,
    "board_view": _PENDING,
    "clear_intent": _PENDING,
    "create_checkpoint": _PENDING,
    "create_pcb_from_schematic": _PENDING,
    "declare_interface": _PENDING,
    "declare_power_rail": _PENDING,
    "detect_mistakes": _PENDING,
    "ecosystem_list": _PENDING,
    "ecosystem_show": _PENDING,
    "evaluate_placement": _PENDING,
    "get_component_requirements": _PENDING,
    "get_decision_history": _PENDING,
    "get_design_intent": _PENDING,
    "get_recent_calls": _PENDING,
    "get_session_context": _PENDING,
    "get_session_summary": _PENDING,
    "list_intents": _PENDING,
    "list_mistake_categories": _PENDING,
    "list_pattern_components": _PENDING,
    "optimize_placement": _PENDING,
    "placement_place_unplaced": _PENDING,
    "record_decision": _PENDING,
    "resolve_placement_overlaps": _PENDING,
    "restore_checkpoint": _PENDING,
    "route_net_auto": _PENDING,
    "screenshot_board": _PENDING,
    "screenshot_schematic": _PENDING,
    "validate_assembly_bom": _PENDING,
    "validate_pattern": _PENDING,
}

_ROW = re.compile(r"^\|\s*`(?P<name>[^`]+)`\s*\|[^|]*\|\s*(?P<req>Yes|No)\s*\|")


def _parse_doc() -> dict[str, dict[str, bool]]:
    """Map documented tool name -> {param name: required}."""
    sections: dict[str, dict[str, bool]] = {}
    current: str | None = None
    in_params = False
    for line in TOOLS_MD.read_text().splitlines():
        m = re.match(r"^###\s+(\S+)\s*$", line)
        if m:
            current = m.group(1)
            sections[current] = {}
            in_params = False
            continue
        if line.startswith("## "):
            current = None
            continue
        if current is None:
            continue
        if line.startswith("**Parameters**"):
            in_params = True
            continue
        if line.startswith("**") and in_params:
            in_params = False
        if in_params:
            row = _ROW.match(line)
            if row:
                sections[current][row["name"]] = row["req"] == "Yes"
    return sections


DOC = _parse_doc()


def test_doc_has_tool_sections() -> None:
    assert len(DOC) > 10


@pytest.mark.parametrize("tool", sorted(DOC))
def test_documented_tool_is_registered(tool: str) -> None:
    assert tool in TOOL_REGISTRY, f"{tool} is documented in tools.md but not registered"


@pytest.mark.parametrize("tool", sorted(t for t in DOC if t in TOOL_REGISTRY))
def test_documented_params_match_schema(tool: str) -> None:
    schema = TOOL_REGISTRY[tool].parameters
    props = set(schema.get("properties", {}))
    required = set(schema.get("required", []))
    documented = DOC[tool]
    assert set(documented) - props == set(), f"{tool}: documented but not in schema"
    assert props - set(documented) == set(), f"{tool}: in schema but undocumented"
    for name, req in documented.items():
        assert req == (name in required), f"{tool}.{name}: Required mismatch"


def test_registered_tools_are_documented() -> None:
    missing = {t for t in TOOL_REGISTRY if t not in DOC and t not in UNDOCUMENTED_ALLOWLIST}
    assert not missing, f"registered tools missing from tools.md: {sorted(missing)}"


@pytest.mark.parametrize(
    "doc", ["docs/mcp/tools.md", "docs/mcp/README.md", "README.md", "docs/reference/api.md"]
)
def test_analyze_board_not_a_tool_name(doc: str) -> None:
    text = (ROOT / doc).read_text()
    assert "analyze_board" not in text, f"{doc} still names the renamed tool analyze_board"


def test_allowlist_has_no_stale_entries() -> None:
    """Allowlisted tools that are now documented must leave the debt list."""
    stale = {t for t in UNDOCUMENTED_ALLOWLIST if t in DOC}
    assert not stale, f"documented tools still allowlisted, remove them: {sorted(stale)}"
    unknown = {t for t in UNDOCUMENTED_ALLOWLIST if t not in TOOL_REGISTRY}
    assert not unknown, f"allowlisted tools not registered, remove them: {sorted(unknown)}"


def test_output_path_descriptions_match_real_default() -> None:
    """Only route_net defaults to a non-overwriting <stem>_routed output."""
    for tool in ("placement_place_unplaced", "commit_session", "route_net"):
        desc = TOOL_REGISTRY[tool]["input_schema"]["properties"]["output_path"]["description"]
        if tool == "route_net":
            assert "_routed" in desc
        else:
            assert "_routed" not in desc, f"{tool}: still overwrites, description must say so"
            assert "overwrites" in desc
