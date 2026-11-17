"""MCP tools for the ecosystem registry (Issue #5839).

Read-only.  Lets an agent working in this repo ask where kicad-tools sits and
whether a neighbouring project has already been evaluated -- the same data
``kct ecosystem`` prints, in the same shape, so the CLI and the MCP surface
can never disagree.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = ["ecosystem_list", "ecosystem_show"]


def ecosystem_list(
    category: str | None = None,
    relation: str | None = None,
    verdict: str | None = None,
    license_compat: str | None = None,
) -> dict[str, Any]:
    """List tracked ecosystem projects, optionally filtered.

    Args:
        category: ``autorouter``, ``design-as-code``, ``agent-interface``,
            ``fabrication``, ``bindings`` or ``benchmark``.
        relation: ``upstream``, ``peer``, ``downstream`` or ``reference``.
        verdict: ``complementary``, ``benchmarked``, ``ideas-adopted``,
            ``evaluated-not-adopted`` or ``watch``.
        license_compat: ``mit-clean``, ``permissive-ideas-only``,
            ``copyleft-ideas-only``, ``unlicensed`` or ``cloud-service``.

    Returns:
        ``{"count", "filters", "projects", "positioning"}`` on success, or
        ``{"error": ...}`` when a filter value is outside its vocabulary or
        the registry fails to load.
    """
    from kicad_tools.ecosystem import (
        CATEGORIES,
        LICENSE_COMPAT,
        RELATIONS,
        VERDICTS,
        RegistryError,
        load_registry,
    )

    for name, value, allowed in (
        ("category", category, CATEGORIES),
        ("relation", relation, RELATIONS),
        ("verdict", verdict, VERDICTS),
        ("license_compat", license_compat, LICENSE_COMPAT),
    ):
        if value is not None and value not in allowed:
            return {"error": f"{name}={value!r} is not one of {sorted(allowed)!r}"}

    try:
        registry = load_registry()
    except RegistryError as exc:
        logger.error("ecosystem registry failed to load: %s", exc)
        return {"error": f"ecosystem registry failed to load: {exc}"}

    selected = registry.filter(
        category=category,
        relation=relation,
        verdict=verdict,
        license_compat=license_compat,
    )
    filters = {
        "category": category,
        "relation": relation,
        "verdict": verdict,
        "license_compat": license_compat,
    }
    return {
        "count": len(selected),
        "filters": {key: value for key, value in filters.items() if value},
        "projects": [project.to_dict() for project in selected],
        "positioning": registry.positioning.to_dict(),
    }


def ecosystem_show(project_id: str) -> dict[str, Any]:
    """Show one ecosystem project in full.

    Args:
        project_id: Registry id, e.g. ``kicadroutingtools``.

    Returns:
        The project's fields, including ``code_reuse_allowed`` and any
        ``research_docs`` holding our own evaluation, or ``{"error": ...}``
        with the valid ids when ``project_id`` is unknown.
    """
    from kicad_tools.ecosystem import RegistryError, load_registry

    try:
        registry = load_registry()
    except RegistryError as exc:
        logger.error("ecosystem registry failed to load: %s", exc)
        return {"error": f"ecosystem registry failed to load: {exc}"}

    try:
        project = registry.get(project_id)
    except KeyError as exc:
        # str(KeyError) is the repr of its argument; args[0] is the message.
        return {"error": str(exc.args[0])}

    return project.to_dict()
