"""Data models for the ecosystem registry (Issue #5839).

The registry records *relationships and verdicts* for the projects around
kicad-tools -- who produces the files we consume, who overlaps our surface,
whose license forbids code reuse, and what we concluded when we evaluated
them.  It deliberately holds no upstream source: see
``src/kicad_tools/ecosystem/data/projects.toml`` for the never-vendor rule it
inherits from ``benchmarks/external/boards.toml``.

Every controlled vocabulary below is validated at load time rather than left
as free text, so a typo in the data file is a loud error instead of a project
that silently vanishes from a filtered listing.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any

__all__ = [
    "CATEGORIES",
    "CATEGORY_HEADINGS",
    "LICENSE_COMPAT",
    "RELATIONS",
    "VERDICTS",
    "EcosystemProject",
    "Positioning",
    "RegistryError",
]


class RegistryError(ValueError):
    """Raised when the registry data file is structurally invalid.

    The message always names the offending project id and field so the error
    points at the data rather than at the loader.
    """


#: What kind of tool this is.
CATEGORIES: frozenset[str] = frozenset(
    {
        "autorouter",
        "design-as-code",
        "agent-interface",
        "fabrication",
        "bindings",
        "benchmark",
    }
)

#: Section heading per category, in canonical render order.
#:
#: This is the single source consumed by both ``kct ecosystem list``
#: (``kicad_tools.cli.commands.ecosystem``) and the README's generated block
#: (``scripts/ecosystem_render.py``) -- before Issue #5843 each kept its own
#: copy, so a category added to ``CATEGORIES`` without a matching heading
#: entry silently dropped that category's projects from the CLI listing
#: (the renderer already guarded against this; the CLI did not). Keeping one
#: dict here, plus the ``set(CATEGORY_HEADINGS) == CATEGORIES`` test in
#: ``tests/test_ecosystem_registry.py``, makes a missing heading a loud
#: failure at test time instead of a silent listing gap.
CATEGORY_HEADINGS: dict[str, str] = {
    "autorouter": "Autorouters",
    "design-as-code": "Design as code (upstream of us)",
    "agent-interface": "Agent and MCP interfaces",
    "fabrication": "Fabrication and CI",
    "bindings": "KiCad bindings",
    "benchmark": "Benchmarks and evaluation protocols",
}

#: How the project sits relative to kicad-tools in a pipeline.
#:
#: ``upstream``   -- produces files we consume (a netlist, a ``.kicad_pcb``)
#: ``peer``       -- overlaps some part of our own surface
#: ``downstream`` -- consumes what we emit
#: ``reference``  -- studied only; not in any pipeline with us
RELATIONS: frozenset[str] = frozenset({"upstream", "peer", "downstream", "reference"})

#: What we concluded after looking at it.
#:
#: ``complementary``         -- solves an adjacent problem; no conflict
#: ``benchmarked``           -- measured head-to-head against us
#: ``ideas-adopted``         -- we took design ideas (never code)
#: ``evaluated-not-adopted`` -- evaluated in depth and declined
#: ``watch``                 -- noted, deliberately not featured yet
VERDICTS: frozenset[str] = frozenset(
    {
        "complementary",
        "benchmarked",
        "ideas-adopted",
        "evaluated-not-adopted",
        "watch",
    }
)

#: Whether code may move between that project and this MIT repo.
#:
#: ``mit-clean``             -- MIT/BSD/ISC; code may move, with attribution
#: ``permissive-ideas-only`` -- Apache-2.0 and similar: one-way compatible in
#:                              principle, but this repo's standing decision
#:                              is ideas-only, to avoid mixed-license files
#:                              in an otherwise uniformly-MIT tree
#: ``copyleft-ideas-only``   -- GPL/AGPL; capability-level engagement only,
#:                              no code in *either* direction
#: ``unlicensed``            -- no LICENSE committed; treat as
#:                              all-rights-reserved, whatever a README claims
#: ``cloud-service``         -- open client in front of a closed service; the
#:                              operative constraint is the service, not the
#:                              client's license
LICENSE_COMPAT: frozenset[str] = frozenset(
    {
        "mit-clean",
        "permissive-ideas-only",
        "copyleft-ideas-only",
        "unlicensed",
        "cloud-service",
    }
)

_PROJECT_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]*$")
_SHA_RE = re.compile(r"^[0-9a-f]{40}$")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_REQUIRED_FIELDS = (
    "name",
    "repo_url",
    "vcs",
    "category",
    "relation",
    "verdict",
    "license",
    "license_compat",
    "summary",
    "last_verified",
)

_KNOWN_FIELDS = frozenset(
    _REQUIRED_FIELDS
    + (
        "slug",
        "language",
        "stars",
        "last_push",
        "pinned_commit",
        "research_docs",
        "reeval_trigger",
    )
)


@dataclass(frozen=True)
class EcosystemProject:
    """One project in the ecosystem registry.

    Attributes:
        project_id: Registry key (lowercase, ``[a-z0-9-]``).
        name: Display name as the upstream spells it.
        repo_url: Canonical URL a reader should follow.
        vcs: ``github``, ``gitlab`` or ``other``. ``other`` means the refresh
            script cannot poll it and its facts are human-maintained.
        slug: Host-relative ``owner/repo`` (or GitLab project path/id) used by
            ``scripts/ecosystem_refresh.py``. Empty when ``vcs == "other"``.
        category: One of :data:`CATEGORIES`.
        relation: One of :data:`RELATIONS`.
        verdict: One of :data:`VERDICTS`.
        license: SPDX id read from the upstream LICENSE file, or
            ``"NONE"`` when the project commits none.
        license_compat: One of :data:`LICENSE_COMPAT` -- the load-bearing
            field, since it decides whether code may be reused at all.
        language: Free-text implementation language(s), for the reader.
        stars: Star count as of ``last_verified``, or ``None`` when the host
            does not report one.
        last_push: ``YYYY-MM-DD`` of the newest upstream push as of
            ``last_verified``, or ``None``.
        last_verified: ``YYYY-MM-DD`` a human last confirmed these facts.
        pinned_commit: Full 40-hex SHA we benchmarked or evaluated, when one
            applies. Never a branch or a tag -- those move.
        research_docs: Repo-relative paths to our own evaluation notes.
        summary: README-voice prose. States the verdict in *both* directions
            where we have measured it.
        reeval_trigger: What would make this verdict worth revisiting.
    """

    project_id: str
    name: str
    repo_url: str
    vcs: str
    category: str
    relation: str
    verdict: str
    license: str
    license_compat: str
    summary: str
    last_verified: str
    slug: str = ""
    language: str = ""
    stars: int | None = None
    last_push: str | None = None
    pinned_commit: str = ""
    research_docs: tuple[str, ...] = field(default_factory=tuple)
    reeval_trigger: str = ""

    @property
    def is_pollable(self) -> bool:
        """Whether ``scripts/ecosystem_refresh.py`` can poll this project."""
        return self.vcs in {"github", "gitlab"} and bool(self.slug)

    @property
    def code_reuse_allowed(self) -> bool:
        """Whether upstream code may be copied into this MIT repo at all.

        ``False`` for copyleft, unlicensed and cloud-service projects.  A
        ``True`` here still means "with attribution", never "without
        attribution".
        """
        return self.license_compat == "mit-clean"

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "project_id": self.project_id,
            "name": self.name,
            "repo_url": self.repo_url,
            "vcs": self.vcs,
            "slug": self.slug,
            "category": self.category,
            "relation": self.relation,
            "verdict": self.verdict,
            "license": self.license,
            "license_compat": self.license_compat,
            "code_reuse_allowed": self.code_reuse_allowed,
            "language": self.language,
            "stars": self.stars,
            "last_push": self.last_push,
            "last_verified": self.last_verified,
            "pinned_commit": self.pinned_commit,
            "research_docs": list(self.research_docs),
            "summary": self.summary,
            "reeval_trigger": self.reeval_trigger,
        }

    @classmethod
    def from_toml(cls, project_id: str, raw: dict[str, Any]) -> EcosystemProject:
        """Build a project from one ``[projects.<id>]`` table.

        Args:
            project_id: The table key.
            raw: The table contents as parsed by ``tomllib``.

        Returns:
            A validated :class:`EcosystemProject`.

        Raises:
            RegistryError: If any field is missing, unknown, or outside its
                controlled vocabulary. The message names the project id and
                the field.
        """
        if not _PROJECT_ID_RE.match(project_id):
            raise RegistryError(
                f"project id {project_id!r}: must be lowercase alphanumeric with "
                "hyphens (matching ^[a-z0-9][a-z0-9-]*$)"
            )

        unknown = set(raw) - _KNOWN_FIELDS
        if unknown:
            raise RegistryError(
                f"{project_id}: unknown field(s) {sorted(unknown)!r}; "
                f"known fields are {sorted(_KNOWN_FIELDS)!r}"
            )

        for required in _REQUIRED_FIELDS:
            if not raw.get(required):
                raise RegistryError(f"{project_id}: missing required field {required!r}")

        _check_vocab(project_id, "category", raw["category"], CATEGORIES)
        _check_vocab(project_id, "relation", raw["relation"], RELATIONS)
        _check_vocab(project_id, "verdict", raw["verdict"], VERDICTS)
        _check_vocab(project_id, "license_compat", raw["license_compat"], LICENSE_COMPAT)
        _check_vocab(project_id, "vcs", raw["vcs"], frozenset({"github", "gitlab", "other"}))

        last_verified = str(raw["last_verified"])
        if not _DATE_RE.match(last_verified):
            raise RegistryError(f"{project_id}: last_verified {last_verified!r} must be YYYY-MM-DD")

        last_push = raw.get("last_push")
        if last_push is not None:
            last_push = str(last_push)
            if not _DATE_RE.match(last_push):
                raise RegistryError(f"{project_id}: last_push {last_push!r} must be YYYY-MM-DD")

        pinned_commit = str(raw.get("pinned_commit", ""))
        if pinned_commit and not _SHA_RE.match(pinned_commit):
            raise RegistryError(
                f"{project_id}: pinned_commit {pinned_commit!r} must be a full "
                "40-character hex SHA (never a branch or tag, which move)"
            )

        stars = raw.get("stars")
        if stars is not None:
            if not isinstance(stars, int) or isinstance(stars, bool) or stars < 0:
                raise RegistryError(f"{project_id}: stars must be a non-negative integer")

        vcs = str(raw["vcs"])
        slug = str(raw.get("slug", ""))
        if vcs in {"github", "gitlab"} and not slug:
            raise RegistryError(
                f"{project_id}: vcs={vcs!r} requires a slug so the refresh script "
                'can poll it (use vcs="other" for a project with no API)'
            )

        research_docs = raw.get("research_docs", [])
        if not isinstance(research_docs, list) or not all(
            isinstance(doc, str) for doc in research_docs
        ):
            raise RegistryError(f"{project_id}: research_docs must be a list of strings")

        return cls(
            project_id=project_id,
            name=str(raw["name"]),
            repo_url=str(raw["repo_url"]),
            vcs=vcs,
            category=str(raw["category"]),
            relation=str(raw["relation"]),
            verdict=str(raw["verdict"]),
            license=str(raw["license"]),
            license_compat=str(raw["license_compat"]),
            summary=str(raw["summary"]).strip(),
            last_verified=last_verified,
            slug=slug,
            language=str(raw.get("language", "")),
            stars=stars,
            last_push=last_push,
            pinned_commit=pinned_commit,
            research_docs=tuple(research_docs),
            reeval_trigger=str(raw.get("reeval_trigger", "")).strip(),
        )


@dataclass(frozen=True)
class Positioning:
    """Where kicad-tools sits, as data rather than prose.

    Held in the registry's ``[positioning]`` table so ``kct ecosystem
    where-we-sit``, ``docs/ecosystem.md`` and any future consumer read one
    source instead of three drifting copies.

    Attributes:
        invariants: Properties true of kicad-tools and not of most of the
            registry -- what makes us a distinct tool rather than a
            reimplementation.
        non_goals: What we deliberately do not do, each naming who does it
            instead. A non-goal with no owner is a gap, not a non-goal.
    """

    invariants: tuple[str, ...] = field(default_factory=tuple)
    non_goals: tuple[str, ...] = field(default_factory=tuple)

    def to_dict(self) -> dict[str, Any]:
        """Convert to a JSON-serializable dictionary."""
        return {
            "invariants": list(self.invariants),
            "non_goals": list(self.non_goals),
        }

    @classmethod
    def from_toml(cls, raw: dict[str, Any]) -> Positioning:
        """Build from the ``[positioning]`` table.

        Raises:
            RegistryError: If either key is present but not a list of
                strings, or if either list is empty.
        """
        invariants = _string_list("positioning", "invariants", raw.get("invariants", []))
        non_goals = _string_list("positioning", "non_goals", raw.get("non_goals", []))
        if not invariants:
            raise RegistryError("positioning: invariants must be a non-empty list")
        if not non_goals:
            raise RegistryError("positioning: non_goals must be a non-empty list")
        return cls(invariants=invariants, non_goals=non_goals)


def _string_list(owner: str, field_name: str, value: Any) -> tuple[str, ...]:
    """Coerce ``value`` to a tuple of non-empty strings, or raise."""
    if not isinstance(value, list) or not all(isinstance(item, str) for item in value):
        raise RegistryError(f"{owner}: {field_name} must be a list of strings")
    stripped = tuple(item.strip() for item in value)
    if any(not item for item in stripped):
        raise RegistryError(f"{owner}: {field_name} contains an empty string")
    return stripped


def _check_vocab(project_id: str, field_name: str, value: Any, allowed: frozenset[str]) -> None:
    """Raise :class:`RegistryError` unless ``value`` is in ``allowed``."""
    if value not in allowed:
        raise RegistryError(
            f"{project_id}: {field_name}={value!r} is not one of {sorted(allowed)!r}"
        )
