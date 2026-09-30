"""Ecosystem registry: where kicad-tools sits among neighbouring projects.

The registry answers three questions that otherwise live only in a
reviewer's memory:

* **Have we already evaluated project X?** -- and if so, which note holds the
  measurements (:attr:`~kicad_tools.ecosystem.models.EcosystemProject.research_docs`).
* **May we reuse its code?** --
  :attr:`~kicad_tools.ecosystem.models.EcosystemProject.license_compat`
  distinguishes "copy with attribution" from "ideas only, in both
  directions".
* **Where does it sit relative to us?** -- upstream producer, overlapping
  peer, downstream consumer, or reference only.

Usage::

    >>> from kicad_tools.ecosystem import load_registry
    >>> registry = load_registry()
    >>> [p.name for p in registry.filter(relation="upstream")]
    ['atopile', 'SKiDL']

See ``docs/ecosystem.md`` for the human-facing positioning narrative, and
Issue #5839 for the design rationale.
"""

from __future__ import annotations

from .models import (
    CATEGORIES,
    LICENSE_COMPAT,
    RELATIONS,
    VERDICTS,
    EcosystemProject,
    Positioning,
    RegistryError,
)
from .registry import (
    DATA_DIR,
    REGISTRY_PATH,
    EcosystemRegistry,
    load_registry,
    validate_research_docs,
)

__all__ = [
    "CATEGORIES",
    "DATA_DIR",
    "LICENSE_COMPAT",
    "REGISTRY_PATH",
    "RELATIONS",
    "VERDICTS",
    "EcosystemProject",
    "EcosystemRegistry",
    "Positioning",
    "RegistryError",
    "load_registry",
    "validate_research_docs",
]
