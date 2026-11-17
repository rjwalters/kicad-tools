"""CI guard: every MCP tool schema property must be read by its handler.

A schema property the handler never reads silently succeeds for the caller,
so the advertised ``inputSchema`` lies. Unknown arguments are rejected via
``additionalProperties: False`` (set by ``_make_params``).
"""

from __future__ import annotations

import ast
import inspect
import textwrap
from typing import Any

import jsonschema
import pytest

import kicad_tools.mcp.tools  # noqa: F401  (populates TOOL_REGISTRY)
from kicad_tools.mcp.tools.registry import TOOL_REGISTRY, ToolSpec

# tool name -> reason (with issue link). Empty: no known violations.
ALLOWLIST: dict[str, str] = {}


def _read_keys(handler: Any) -> tuple[set[str], list[str]]:
    """Return (constant keys read from the first arg, unresolved-use notes)."""
    tree = ast.parse(textwrap.dedent(inspect.getsource(handler)))
    func = next(n for n in ast.walk(tree) if isinstance(n, ast.FunctionDef))
    name = func.args.args[0].arg
    keys: set[str] = set()
    unresolved: list[str] = []
    resolved_nodes: set[int] = set()

    for node in ast.walk(func):
        if (
            isinstance(node, ast.Subscript)
            and isinstance(node.value, ast.Name)
            and node.value.id == name
        ):
            resolved_nodes.add(id(node.value))
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                keys.add(node.slice.value)
            else:
                unresolved.append(f"non-constant subscript at line {node.lineno}")
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == name
            and node.func.attr in ("get", "pop")
        ):
            resolved_nodes.add(id(node.func.value))
            arg = node.args[0] if node.args else None
            if isinstance(arg, ast.Constant) and isinstance(arg.value, str):
                keys.add(arg.value)
            else:
                unresolved.append(f"non-constant .{node.func.attr}() at line {node.lineno}")

        elif (
            isinstance(node, ast.Compare)
            and len(node.ops) == 1
            and isinstance(node.ops[0], (ast.In, ast.NotIn))
            and isinstance(node.comparators[0], ast.Name)
            and node.comparators[0].id == name
        ):
            resolved_nodes.add(id(node.comparators[0]))
            left = node.left
            if isinstance(left, ast.Constant) and isinstance(left.value, str):
                keys.add(left.value)
            else:
                unresolved.append(f"non-constant membership test at line {node.lineno}")

    for node in ast.walk(func):
        if isinstance(node, ast.Name) and node.id == name and isinstance(node.ctx, ast.Load):
            if id(node) not in resolved_nodes:
                unresolved.append(f"bare use of '{name}' at line {node.lineno}")
    return keys, unresolved


def find_violations(tools: dict[str, ToolSpec], allowlist: dict[str, str]) -> list[str]:
    problems: list[str] = []
    for tool_name, spec in sorted(tools.items()):
        if tool_name in allowlist:
            continue
        keys, unresolved = _read_keys(spec.handler)
        if unresolved:
            problems.append(f"{tool_name}: unresolved params use ({'; '.join(unresolved)})")
            continue
        for prop in spec.parameters.get("properties", {}):
            if prop not in keys:
                problems.append(f"{tool_name}.{prop}: schema property never read by handler")
    return problems


def test_registry_is_populated():
    assert len(TOOL_REGISTRY) > 0


def test_all_schema_properties_are_read_by_handlers():
    problems = find_violations(TOOL_REGISTRY, ALLOWLIST)
    assert not problems, "Unused/unresolvable MCP schema params:\n" + "\n".join(problems)


def test_allowlist_entries_are_real_and_documented():
    for name, reason in ALLOWLIST.items():
        assert name in TOOL_REGISTRY, f"stale allowlist entry {name}"
        assert "#" in reason, f"allowlist entry {name} needs an issue link"


@pytest.mark.parametrize("name", sorted(TOOL_REGISTRY))
def test_schema_rejects_unknown_arguments(name):
    spec = TOOL_REGISTRY[name]
    assert spec.parameters.get("additionalProperties") is False
    errors = list(jsonschema.Draft7Validator(spec.parameters).iter_errors({"__bogus_arg__": 1}))
    assert any("__bogus_arg__" in e.message for e in errors)


def test_checker_flags_unread_property():
    def handler(params):
        return params["used"]

    spec = ToolSpec(
        name="synthetic",
        description="",
        parameters={"type": "object", "properties": {"used": {}, "unused": {}}},
        handler=handler,
    )
    assert find_violations({"synthetic": spec}, {}) == [
        "synthetic.unused: schema property never read by handler"
    ]


def test_checker_flags_unresolved_use():
    def handler(params):
        return dict(params)

    spec = ToolSpec(
        name="whole",
        description="",
        parameters={"type": "object", "properties": {"a": {}}},
        handler=handler,
    )
    problems = find_violations({"whole": spec}, {})
    assert len(problems) == 1 and "unresolved" in problems[0]
