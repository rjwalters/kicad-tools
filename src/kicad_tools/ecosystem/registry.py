"""Loader for the ecosystem registry (Issue #5839).

The data file lives *inside the package* at
``src/kicad_tools/ecosystem/data/projects.toml`` and is resolved relative to
this module, exactly as :mod:`kicad_tools.explain.registry` resolves
``SPECS_DIR``.  That is not a style preference: ``pyproject.toml`` sets
``[tool.hatch.build.targets.wheel] packages = ["src/kicad_tools"]``, so
hatchling ships only package-internal files.  A top-level ``ecosystem/``
directory would load fine from a checkout and then be absent from every
``pip install kicad-tools``.

``research_docs`` paths are *not* checked against the filesystem at load
time, because ``docs/`` is not shipped in the wheel.  Use
:func:`validate_research_docs` from a repo-rooted context (tests, the README
renderer) where those files genuinely exist.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

from .models import EcosystemProject, Positioning, RegistryError

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[import-not-found]
    except ImportError:
        tomllib = None

__all__ = [
    "DATA_DIR",
    "REGISTRY_PATH",
    "EcosystemRegistry",
    "load_registry",
    "validate_research_docs",
]

#: Packaged data directory, resolved relative to this module so it works
#: identically from a checkout and from an installed wheel.
DATA_DIR = Path(__file__).parent / "data"

#: The canonical registry file.
REGISTRY_PATH = DATA_DIR / "projects.toml"


class EcosystemRegistry:
    """An ordered, validated collection of :class:`EcosystemProject`.

    Iteration and :meth:`filter` preserve the order of the data file.  That
    order is editorial -- it is the order the README renders in -- so the
    closest peer leads its section rather than whichever project happens to
    have the most stars.
    """

    def __init__(
        self,
        projects: list[EcosystemProject],
        positioning: Positioning | None = None,
        source: Path | None = None,
    ) -> None:
        self._projects = list(projects)
        self._by_id = {project.project_id: project for project in self._projects}
        self.positioning = positioning or Positioning()
        self.source = source

    def __len__(self) -> int:
        return len(self._projects)

    def __iter__(self):
        return iter(self._projects)

    @property
    def projects(self) -> list[EcosystemProject]:
        """All projects, in data-file order."""
        return list(self._projects)

    @property
    def ids(self) -> list[str]:
        """All project ids, in data-file order."""
        return [project.project_id for project in self._projects]

    def get(self, project_id: str) -> EcosystemProject:
        """Look up one project by id.

        Raises:
            KeyError: If no such project exists. The message lists the valid
                ids so a typo is self-correcting.
        """
        try:
            return self._by_id[project_id]
        except KeyError:
            raise KeyError(
                f"unknown project id {project_id!r}; valid ids: {', '.join(self.ids)}"
            ) from None

    def filter(
        self,
        *,
        category: str | None = None,
        relation: str | None = None,
        verdict: str | None = None,
        license_compat: str | None = None,
    ) -> list[EcosystemProject]:
        """Return projects matching every supplied predicate, in file order."""
        results = self._projects
        if category is not None:
            results = [p for p in results if p.category == category]
        if relation is not None:
            results = [p for p in results if p.relation == relation]
        if verdict is not None:
            results = [p for p in results if p.verdict == verdict]
        if license_compat is not None:
            results = [p for p in results if p.license_compat == license_compat]
        return list(results)

    def by_category(self) -> dict[str, list[EcosystemProject]]:
        """Group projects by category, preserving file order within a group."""
        grouped: dict[str, list[EcosystemProject]] = {}
        for project in self._projects:
            grouped.setdefault(project.category, []).append(project)
        return grouped

    def to_dict(self) -> dict[str, Any]:
        """Convert the whole registry to a JSON-serializable dictionary."""
        return {
            "count": len(self._projects),
            "positioning": self.positioning.to_dict(),
            "projects": [project.to_dict() for project in self._projects],
        }


def load_registry(path: Path | None = None) -> EcosystemRegistry:
    """Load and validate the registry.

    Args:
        path: Registry file to load. Defaults to the packaged
            :data:`REGISTRY_PATH`.

    Returns:
        A validated :class:`EcosystemRegistry`.

    Raises:
        RegistryError: If the file is missing, unparseable, or any project
            fails validation.
    """
    registry_path = REGISTRY_PATH if path is None else path

    if tomllib is None:  # pragma: no cover - only on 3.10 without tomli
        raise RegistryError(
            "reading the ecosystem registry needs a TOML parser: install "
            "'tomli' (declared for python_version < 3.11) or use Python 3.11+"
        )

    if not registry_path.is_file():
        raise RegistryError(f"ecosystem registry not found at {registry_path}")

    try:
        with registry_path.open("rb") as handle:
            raw = tomllib.load(handle)
    except tomllib.TOMLDecodeError as exc:
        raise RegistryError(f"{registry_path}: invalid TOML: {exc}") from exc

    tables = raw.get("projects")
    if not isinstance(tables, dict) or not tables:
        raise RegistryError(f"{registry_path}: expected a non-empty [projects.<id>] table")

    projects = [
        EcosystemProject.from_toml(project_id, table) for project_id, table in tables.items()
    ]

    raw_positioning = raw.get("positioning")
    if not isinstance(raw_positioning, dict):
        raise RegistryError(f"{registry_path}: expected a [positioning] table")
    positioning = Positioning.from_toml(raw_positioning)

    return EcosystemRegistry(projects, positioning=positioning, source=registry_path)


def validate_research_docs(registry: EcosystemRegistry, repo_root: Path) -> list[tuple[str, str]]:
    """Check that every ``research_docs`` path exists under ``repo_root``.

    Split out of :func:`load_registry` on purpose: ``docs/`` is not shipped
    in the wheel, so a filesystem check at load time would break the CLI for
    every pip-installed user.

    Args:
        registry: A loaded registry.
        repo_root: Repository root to resolve the paths against.

    Returns:
        ``(project_id, missing_path)`` pairs; empty when every path resolves.
    """
    missing: list[tuple[str, str]] = []
    for project in registry:
        for doc in project.research_docs:
            if not (repo_root / doc).is_file():
                missing.append((project.project_id, doc))
    return missing
