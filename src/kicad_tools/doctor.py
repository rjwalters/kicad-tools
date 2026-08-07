"""Environment / installation health checks for kicad-tools (``kct doctor``).

Two check groups run (issue #4347 + #4542):

1. **version-record drift** -- the original check (details below).
2. **environment preflight** -- runtime-prerequisite probes (native router
   backend, ``kicad-cli``, Python/shapely, and a PATH-shadowed ``kct``), each
   reported ``ok`` / ``warn`` / ``fail`` with a human ``detail`` and an
   actionable ``remedy``. Every preflight is fail-soft: a missing tool is a
   reported ``fail`` result, never an exception.

JSON shape (``kct doctor --format json``): the top-level dict is the
version-drift report exactly as before (``"check": "version-drift"``,
``installed_version``, ``root``, ``has_drift``, ``ok``, ``reconcile_command``,
``records``) plus one **additive** key ``"environment"``::

    {"check": "version-drift", ...unchanged drift keys...,
     "environment": {
         "check": "environment",
         "ok": bool,        # True when NO preflight is status "fail"
                            # (warns are advisory and do not clear this)
         "has_fail": bool,
         "has_warn": bool,
         "checks": [{"name": str, "status": "ok"|"warn"|"fail",
                     "detail": str, "remedy": str | None}, ...]}}

Preflight status semantics (deterministic, fixed check order):

- ``native-backend`` -- wraps ``probe_backend_info()``. ``ok`` when available
  and build-version-current; ``warn`` when not available or stale (the
  pure-Python fallback is legitimate but slow); ``fail`` only when the probe
  itself could not run.
- ``kicad-cli`` -- ``fail`` when not found anywhere; ``warn`` when found but
  the version is unqueryable/unparseable or the major version is below the
  KiCad 8 compatibility floor; ``ok`` otherwise.
- ``python-env`` -- ``fail`` when the interpreter is below the 3.10 floor or
  shapely (a core dependency) is not importable; ``ok`` otherwise.
- ``kct-path`` -- ``warn`` when a ``kct`` on PATH resolves to a different
  version than the active environment (a stale shadowing install makes new
  subcommands look like ``invalid choice``); ``ok`` when absent or matching.

The first check group, **version-record drift** (issue #4347):

When ``kicad-tools`` is installed into a consumer repo by
``scripts/install-kct.sh``, the installer stamps the resolved version into
several *records* that live in the consumer's tree:

1. ``pyproject.toml`` -- the ``kicad-tools`` uv dependency pin
   (git ``tag = "vX"``, ``rev = "<sha>"``, or a local ``path`` install).
2. ``.kct/install-metadata.json`` -- the ``kct_version`` field.
3. ``CLAUDE.md`` -- the ``## kicad-tools (X)`` header inside the
   ``<!-- BEGIN KICAD-TOOLS -->`` / ``<!-- END KICAD-TOOLS -->`` markers.

A fourth record type is the kicad-tools **source checkout** itself, whose
``pyproject.toml`` carries the canonical ``[project] version`` (record type
``pyproject-project-version``). Records 1 and 4 are mutually exclusive: a repo
is either a consumer that *depends on* kicad-tools, or the kicad-tools source
checkout.

``uv``-bumping the installed package (or checking out a new source tag) updates
the ground-truth version -- ``importlib.metadata.version("kicad-tools")``,
surfaced as :data:`kicad_tools.__version__` -- but the records above are only
rewritten when the installer is re-run. They silently go stale. This module
compares each record against the installed version (ground truth) and reports
``ok`` / ``drift`` / ``not_present`` (plus a few informational states) per
record, names the stale record(s), and prints the reconcile command.

The core is intentionally CLI-free so it can be unit-tested against synthetic
fixtures rooted at a temporary ``--root`` directory.
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from dataclasses import dataclass
from enum import Enum
from pathlib import Path

if sys.version_info >= (3, 11):
    import tomllib
else:  # pragma: no cover - exercised only on Python < 3.11
    try:
        import tomli as tomllib  # type: ignore[import-not-found]
    except ImportError:
        tomllib = None

__all__ = [
    "RecordStatus",
    "RecordResult",
    "DriftReport",
    "check_version_drift",
    "normalize_version",
    "render_text",
    "report_to_dict",
    "PACKAGE_NAME",
    "PYPROJECT_PROJECT_VERSION",
    "PYPROJECT_DEPENDENCY",
    "INSTALL_METADATA",
    "CLAUDE_MD",
    # Environment preflight (issue #4542)
    "PreflightStatus",
    "PreflightResult",
    "EnvironmentReport",
    "check_environment",
    "check_native_backend",
    "check_kicad_cli",
    "check_python_env",
    "check_kct_path",
    "environment_to_dict",
    "render_environment_text",
    "NATIVE_BACKEND",
    "KICAD_CLI",
    "PYTHON_ENV",
    "KCT_PATH",
    "PYTHON_FLOOR",
    "KICAD_MAJOR_FLOOR",
]

PACKAGE_NAME = "kicad-tools"

# Record-type identifiers (stable keys for the JSON shape).
PYPROJECT_PROJECT_VERSION = "pyproject-project-version"
PYPROJECT_DEPENDENCY = "pyproject-dependency"
INSTALL_METADATA = "install-metadata"
CLAUDE_MD = "claude-md"

# CLAUDE.md marker pair (must match scripts/install-kct.sh KCT_MARK_BEGIN/END).
_CLAUDE_MARK_BEGIN = "<!-- BEGIN KICAD-TOOLS -->"
_CLAUDE_MARK_END = "<!-- END KICAD-TOOLS -->"
# Header line inside the marker block: "## kicad-tools (X)".
_CLAUDE_HEADER_RE = re.compile(r"^\s*##\s+kicad-tools\s*\(([^)]+)\)\s*$")

# The reconcile command template. Records are only rewritten by the installer,
# so re-running it at the installed tag reconciles every stale record at once.
_RECONCILE_TEMPLATE = "install-kct.sh --tag v{version}"

# A version that looks like a release tag (e.g. "0.18.0", "v1.2.3rc1"). Used to
# distinguish a git ``tag`` pin from a bare commit sha / branch name.
_VERSION_LIKE_RE = re.compile(r"^v?\d+\.\d+")


class RecordStatus(str, Enum):
    """Per-record drift status.

    Only :attr:`DRIFT` is failure-worthy under ``--strict``; every other
    status is advisory (a missing / editable / sha-pinned record is a normal,
    non-error condition).
    """

    OK = "ok"
    """Recorded version matches the installed version."""

    DRIFT = "drift"
    """Recorded version differs from the installed version (stale record)."""

    NOT_PRESENT = "not_present"
    """The record's file / marker / dependency is absent."""

    UNPINNED_TO_SHA = "unpinned-to-sha"
    """Dependency pinned to a git sha / branch / rev -- no comparable version."""

    EDITABLE = "editable"
    """Local path / editable dependency, or a floating spec -- no version pin."""

    MALFORMED = "malformed"
    """The record exists but could not be parsed (e.g. broken CLAUDE.md markers)."""


@dataclass
class RecordResult:
    """The drift-check outcome for a single version record."""

    name: str
    """Record-type identifier (one of the module-level constants)."""

    path: str
    """Repo-relative path of the file that holds (or would hold) the record."""

    status: RecordStatus
    """The drift status for this record."""

    recorded_version: str | None
    """The version stamped in the record, or ``None`` when not applicable."""

    detail: str
    """Human-readable explanation of the status."""

    @property
    def is_drift(self) -> bool:
        return self.status is RecordStatus.DRIFT


@dataclass
class DriftReport:
    """Aggregate result of a version-record drift check."""

    installed_version: str
    """Ground-truth installed package version."""

    root: str
    """Absolute path of the directory the records were resolved against."""

    records: list[RecordResult]
    """One :class:`RecordResult` per known record type (always all four)."""

    @property
    def has_drift(self) -> bool:
        """True when any record's recorded version is stale."""
        return any(r.is_drift for r in self.records)

    @property
    def stale_records(self) -> list[RecordResult]:
        """The records that are in :attr:`RecordStatus.DRIFT`."""
        return [r for r in self.records if r.is_drift]

    @property
    def reconcile_command(self) -> str | None:
        """The command that reconciles stale records, or ``None`` when clean."""
        if not self.has_drift:
            return None
        return _RECONCILE_TEMPLATE.format(version=self.installed_version)


def normalize_version(version: str) -> str:
    """Normalize a version string for comparison.

    Strips a leading ``v``/``V`` prefix and surrounding whitespace so that
    ``v0.18.0`` and ``0.18.0`` compare equal.
    """
    v = version.strip()
    if v[:1] in ("v", "V"):
        v = v[1:]
    return v


def _versions_match(recorded: str, installed: str) -> bool:
    return normalize_version(recorded) == normalize_version(installed)


# ---------------------------------------------------------------------------
# pyproject.toml parsing (records 1 and 2)
# ---------------------------------------------------------------------------


def _load_pyproject(path: Path) -> dict | None:
    """Parse a pyproject.toml, returning ``None`` on any read/parse failure."""
    if tomllib is None:  # pragma: no cover - only on Python < 3.11 w/o tomli
        return None
    try:
        with path.open("rb") as fh:
            data = tomllib.load(fh)
    except (OSError, ValueError):
        # ValueError covers tomllib.TOMLDecodeError (its base class).
        return None
    return data if isinstance(data, dict) else None


def _dependency_requirement_name(spec: str) -> str:
    """Extract the distribution name from a PEP 508 requirement string.

    ``"kicad-tools @ git+..."`` -> ``"kicad-tools"``;
    ``"kicad-tools>=1.0"`` -> ``"kicad-tools"``.
    """
    # The name is everything up to the first of: whitespace, @, comparison, [.
    match = re.match(r"^\s*([A-Za-z0-9._-]+)", spec)
    return match.group(1) if match else ""


def _find_dependency_spec(project: dict) -> str | None:
    """Return the raw requirement string for the kicad-tools dependency."""
    deps = project.get("dependencies")
    if not isinstance(deps, list):
        return None
    for dep in deps:
        if isinstance(dep, str) and _dependency_requirement_name(dep) == PACKAGE_NAME:
            return dep
    return None


def _classify_git_ref(ref: str) -> tuple[RecordStatus, str | None, str]:
    """Classify a git ``@<ref>`` pin into (status, recorded_version, detail)."""
    if _VERSION_LIKE_RE.match(ref):
        return (RecordStatus.OK, ref, "")  # caller compares to decide ok/drift
    return (
        RecordStatus.UNPINNED_TO_SHA,
        None,
        f"pinned to git rev '{ref}' (not a version tag); cannot check for drift",
    )


def _classify_uv_source(source: dict) -> tuple[RecordStatus, str | None, str]:
    """Classify a ``[tool.uv.sources].kicad-tools`` table entry."""
    if "path" in source:
        return (
            RecordStatus.EDITABLE,
            None,
            f"local path dependency ({source['path']}); no version pin to check",
        )
    if "tag" in source:
        return (RecordStatus.OK, str(source["tag"]), "")
    for key in ("rev", "branch"):
        if key in source:
            return (
                RecordStatus.UNPINNED_TO_SHA,
                None,
                f"pinned to git {key} '{source[key]}'; cannot check for drift",
            )
    if "git" in source:
        return (
            RecordStatus.UNPINNED_TO_SHA,
            None,
            "git source without a tag pin; cannot check for drift",
        )
    return (RecordStatus.EDITABLE, None, "dependency source has no version pin")


def _classify_dependency_spec(spec: str) -> tuple[RecordStatus, str | None, str]:
    """Classify a PEP 508 requirement string for the kicad-tools dependency."""
    # git+URL@ref form: "kicad-tools @ git+https://.../kicad-tools@v0.18.0"
    at_git = re.search(r"git\+[^@\s]+@([^\s#]+)", spec)
    if at_git:
        return _classify_git_ref(at_git.group(1))
    # Exact PyPI pin: "kicad-tools==0.18.0"
    exact = re.search(r"==\s*([^\s,;]+)", spec)
    if exact:
        return (RecordStatus.OK, exact.group(1), "")
    # Bare name or a range spec (>=, ~=, ...): no exact version to compare.
    return (
        RecordStatus.EDITABLE,
        None,
        "dependency present but not pinned to an exact version; no drift check",
    )


def _check_pyproject_records(root: Path, installed: str) -> tuple[RecordResult, RecordResult]:
    """Resolve both pyproject-derived records (project-version + dependency)."""
    rel = "pyproject.toml"
    pyproject_path = root / rel
    project_result = RecordResult(
        PYPROJECT_PROJECT_VERSION,
        rel,
        RecordStatus.NOT_PRESENT,
        None,
        "no pyproject.toml at root",
    )
    dep_result = RecordResult(
        PYPROJECT_DEPENDENCY,
        rel,
        RecordStatus.NOT_PRESENT,
        None,
        "no pyproject.toml at root",
    )

    if not pyproject_path.is_file():
        return project_result, dep_result

    data = _load_pyproject(pyproject_path)
    if data is None:
        malformed = "pyproject.toml is missing or could not be parsed"
        return (
            RecordResult(PYPROJECT_PROJECT_VERSION, rel, RecordStatus.MALFORMED, None, malformed),
            RecordResult(PYPROJECT_DEPENDENCY, rel, RecordStatus.MALFORMED, None, malformed),
        )

    project = data.get("project", {}) if isinstance(data.get("project"), dict) else {}

    # Record 4: is this the kicad-tools SOURCE checkout?
    if project.get("name") == PACKAGE_NAME:
        recorded = project.get("version")
        if isinstance(recorded, str):
            if _versions_match(recorded, installed):
                project_result = RecordResult(
                    PYPROJECT_PROJECT_VERSION,
                    rel,
                    RecordStatus.OK,
                    recorded,
                    "source [project] version matches the installed package",
                )
            else:
                project_result = RecordResult(
                    PYPROJECT_PROJECT_VERSION,
                    rel,
                    RecordStatus.DRIFT,
                    recorded,
                    (
                        f"source [project] version {recorded!r} != installed "
                        f"{installed!r} (rebuild/reinstall the package: 'uv sync')"
                    ),
                )
        else:
            project_result = RecordResult(
                PYPROJECT_PROJECT_VERSION,
                rel,
                RecordStatus.MALFORMED,
                None,
                "[project] has no string version field",
            )
        # A source checkout is never also a consumer of itself.
        dep_result = RecordResult(
            PYPROJECT_DEPENDENCY,
            rel,
            RecordStatus.NOT_PRESENT,
            None,
            "this is the kicad-tools source checkout, not a consumer",
        )
        return project_result, dep_result

    # Otherwise this is (potentially) a consumer: record 1 not present.
    project_result = RecordResult(
        PYPROJECT_PROJECT_VERSION,
        rel,
        RecordStatus.NOT_PRESENT,
        None,
        "pyproject.toml is not the kicad-tools source (no matching [project] name)",
    )

    # Record 1: the kicad-tools dependency pin. Prefer the uv source table,
    # which is where `uv add` records the git tag / rev / path.
    tool = data.get("tool", {})
    uv_sources = {}
    if isinstance(tool, dict):
        uv = tool.get("uv", {})
        if isinstance(uv, dict) and isinstance(uv.get("sources"), dict):
            uv_sources = uv["sources"]

    status: RecordStatus
    recorded_version: str | None
    detail: str
    source_entry = uv_sources.get(PACKAGE_NAME)
    if isinstance(source_entry, dict):
        status, recorded_version, detail = _classify_uv_source(source_entry)
    else:
        spec = _find_dependency_spec(project)
        if spec is None:
            return project_result, dep_result  # dep_result stays NOT_PRESENT
        status, recorded_version, detail = _classify_dependency_spec(spec)

    dep_result = _finalize_versioned_record(
        PYPROJECT_DEPENDENCY, rel, status, recorded_version, detail, installed
    )
    return project_result, dep_result


def _finalize_versioned_record(
    name: str,
    rel: str,
    status: RecordStatus,
    recorded_version: str | None,
    detail: str,
    installed: str,
) -> RecordResult:
    """Turn a provisional (status, version) into ok/drift when comparable.

    When ``status`` came back as :attr:`RecordStatus.OK` with a
    ``recorded_version`` string, this compares it to ``installed`` and demotes
    to :attr:`RecordStatus.DRIFT` on mismatch. Informational statuses
    (unpinned/editable/malformed) pass through unchanged.
    """
    if status is RecordStatus.OK and recorded_version is not None:
        if _versions_match(recorded_version, installed):
            return RecordResult(
                name,
                rel,
                RecordStatus.OK,
                recorded_version,
                f"records v{normalize_version(recorded_version)} (matches installed)",
            )
        return RecordResult(
            name,
            rel,
            RecordStatus.DRIFT,
            recorded_version,
            (
                f"records v{normalize_version(recorded_version)} but installed is "
                f"v{normalize_version(installed)}"
            ),
        )
    return RecordResult(name, rel, status, recorded_version, detail)


# ---------------------------------------------------------------------------
# .kct/install-metadata.json (record 2)
# ---------------------------------------------------------------------------


def _check_install_metadata(root: Path, installed: str) -> RecordResult:
    rel = ".kct/install-metadata.json"
    path = root / ".kct" / "install-metadata.json"
    if not path.is_file():
        return RecordResult(
            INSTALL_METADATA, rel, RecordStatus.NOT_PRESENT, None, "file not present"
        )
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return RecordResult(INSTALL_METADATA, rel, RecordStatus.MALFORMED, None, "invalid JSON")
    recorded = data.get("kct_version") if isinstance(data, dict) else None
    if not isinstance(recorded, str):
        return RecordResult(
            INSTALL_METADATA,
            rel,
            RecordStatus.MALFORMED,
            None,
            "no string 'kct_version' field",
        )
    return _finalize_versioned_record(
        INSTALL_METADATA, rel, RecordStatus.OK, recorded, "", installed
    )


# ---------------------------------------------------------------------------
# CLAUDE.md marker block (record 3)
# ---------------------------------------------------------------------------


def _extract_claude_md_version(text: str) -> tuple[str | None, str | None]:
    """Extract the kicad-tools header version from CLAUDE.md text.

    Returns ``(version, error)``. Exactly one is non-None:
    - ``(version, None)`` -- header found inside a well-formed marker block.
    - ``(None, None)`` -- no kicad-tools marker block at all.
    - ``(None, error)`` -- markers present but malformed / no header.
    """
    if _CLAUDE_MARK_BEGIN not in text:
        return None, None

    depth = 0
    header_version: str | None = None
    for line in text.splitlines():
        if _CLAUDE_MARK_BEGIN in line:
            depth += 1
            continue
        if _CLAUDE_MARK_END in line:
            if depth == 0:
                return None, "END marker appears before BEGIN marker"
            depth -= 1
            continue
        if depth > 0 and header_version is None:
            m = _CLAUDE_HEADER_RE.match(line)
            if m:
                header_version = m.group(1).strip()
    if depth != 0:
        return None, "unterminated BEGIN marker (no matching END)"
    if header_version is None:
        return None, "marker block present but no '## kicad-tools (X)' header"
    return header_version, None


def _check_claude_md(root: Path, installed: str) -> RecordResult:
    rel = "CLAUDE.md"
    path = root / rel
    if not path.is_file():
        return RecordResult(CLAUDE_MD, rel, RecordStatus.NOT_PRESENT, None, "file not present")
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return RecordResult(CLAUDE_MD, rel, RecordStatus.MALFORMED, None, "could not read file")

    version, error = _extract_claude_md_version(text)
    if error is not None:
        return RecordResult(CLAUDE_MD, rel, RecordStatus.MALFORMED, None, error)
    if version is None:
        return RecordResult(
            CLAUDE_MD, rel, RecordStatus.NOT_PRESENT, None, "no kicad-tools marker block"
        )
    return _finalize_versioned_record(CLAUDE_MD, rel, RecordStatus.OK, version, "", installed)


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def check_version_drift(root: Path | str, installed_version: str) -> DriftReport:
    """Check every version record under ``root`` against ``installed_version``.

    ``installed_version`` is the ground truth -- normally
    :data:`kicad_tools.__version__`. Passing it in (rather than reading it
    here) keeps the core testable with synthetic versions.

    Never raises for missing / malformed records: each degrades to a
    ``not_present`` or ``malformed`` :class:`RecordResult`.
    """
    root_path = Path(root).resolve()
    project_result, dep_result = _check_pyproject_records(root_path, installed_version)
    records = [
        dep_result,
        _check_install_metadata(root_path, installed_version),
        _check_claude_md(root_path, installed_version),
        project_result,
    ]
    return DriftReport(
        installed_version=installed_version,
        root=str(root_path),
        records=records,
    )


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------

_STATUS_GLYPH = {
    RecordStatus.OK: "ok       ",
    RecordStatus.DRIFT: "DRIFT    ",
    RecordStatus.NOT_PRESENT: "n/a      ",
    RecordStatus.UNPINNED_TO_SHA: "info     ",
    RecordStatus.EDITABLE: "info     ",
    RecordStatus.MALFORMED: "malformed",
}


def report_to_dict(report: DriftReport) -> dict:
    """Serialize a :class:`DriftReport` to the agent-facing JSON shape."""
    return {
        "check": "version-drift",
        "installed_version": report.installed_version,
        "root": report.root,
        "has_drift": report.has_drift,
        "ok": not report.has_drift,
        "reconcile_command": report.reconcile_command,
        "records": [
            {
                "name": r.name,
                "path": r.path,
                "status": r.status.value,
                "recorded_version": r.recorded_version,
                "detail": r.detail,
            }
            for r in report.records
        ],
    }


def render_text(report: DriftReport) -> str:
    """Render a :class:`DriftReport` as human-readable text."""
    lines = [
        "kct doctor: version-record drift",
        f"  installed version (ground truth): {report.installed_version}",
        f"  root: {report.root}",
        "",
    ]
    for r in report.records:
        glyph = _STATUS_GLYPH.get(r.status, r.status.value)
        recorded = r.recorded_version if r.recorded_version is not None else "-"
        lines.append(f"  [{glyph}] {r.name:<26} recorded={recorded}")
        if r.detail:
            lines.append(f"              {r.detail}")
    lines.append("")
    if report.has_drift:
        stale = ", ".join(r.name for r in report.stale_records)
        lines.append(f"DRIFT: stale record(s): {stale}")
        lines.append(f"Reconcile with: {report.reconcile_command}")
    else:
        lines.append("OK: no version-record drift detected.")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Environment preflight (issue #4542)
# ---------------------------------------------------------------------------
#
# Runtime-prerequisite probes. Each preflight WRAPS existing detection logic
# (``probe_backend_info``, ``find_kicad_cli``/``get_kicad_version``,
# ``has_shapely``) rather than reimplementing it, and every probe callable is
# dependency-injectable so tests can exercise each status row without a real
# KiCad install or compiled ``.so``. See the module docstring for the status
# semantics and the JSON shape.

# Preflight identifiers (stable keys for the JSON shape, fixed order).
NATIVE_BACKEND = "native-backend"
KICAD_CLI = "kicad-cli"
PYTHON_ENV = "python-env"
KCT_PATH = "kct-path"

# Compatibility floors.
PYTHON_FLOOR = (3, 10)
KICAD_MAJOR_FLOOR = 8

# Remedies (actionable commands). The KiCad wording mirrors the install hint
# in ``kicad_tools.cli.runner`` ("Install KiCad 8 from ...").
_REMEDY_BUILD_NATIVE = "kct build-native"
_REMEDY_PROBE_FAILED = "kct build-native --check"
_REMEDY_INSTALL_KICAD = "Install KiCad 8 from https://www.kicad.org/download/"
_REMEDY_PYTHON_FLOOR = (
    "reinstall kicad-tools under Python >= "
    f"{PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]} (the package cannot have installed correctly)"
)
_REMEDY_USE_UV_RUN = (
    "invoke via 'uv run kct' (or re-run the installer) so the "
    "project-pinned version is used instead of the PATH-resolved one"
)

# Leading numeric version in a free-form version string (e.g. "8.0.6",
# "kicad-tools 0.20.0", "9.0.0-rc1"). Used defensively -- unparseable output
# degrades to a warn, never a crash.
_LEADING_VERSION_RE = re.compile(r"(\d+(?:\.\d+)+(?:[0-9A-Za-z.+-]*))")


class PreflightStatus(str, Enum):
    """Per-preflight status.

    Only :attr:`FAIL` is failure-worthy under ``--strict``; :attr:`WARN` is
    advisory (a legitimate-but-degraded environment, made loud).
    """

    OK = "ok"
    WARN = "warn"
    FAIL = "fail"


@dataclass
class PreflightResult:
    """The outcome of a single environment preflight."""

    name: str
    """Preflight identifier (one of the module-level constants)."""

    status: PreflightStatus
    """The preflight status (``ok`` / ``warn`` / ``fail``)."""

    detail: str
    """Human-readable explanation of the status."""

    remedy: str | None
    """Actionable command / instruction; ``None`` when ``ok``."""

    @property
    def is_fail(self) -> bool:
        return self.status is PreflightStatus.FAIL

    @property
    def is_warn(self) -> bool:
        return self.status is PreflightStatus.WARN


@dataclass
class EnvironmentReport:
    """Aggregate result of the environment-preflight check group."""

    checks: list[PreflightResult]
    """One :class:`PreflightResult` per preflight, in fixed order."""

    @property
    def has_fail(self) -> bool:
        return any(c.is_fail for c in self.checks)

    @property
    def has_warn(self) -> bool:
        return any(c.is_warn for c in self.checks)

    @property
    def ok(self) -> bool:
        """True when no preflight failed (warns are advisory; see docstring)."""
        return not self.has_fail

    @property
    def failed_checks(self) -> list[PreflightResult]:
        return [c for c in self.checks if c.is_fail]


def _extract_version(raw: str) -> str | None:
    """Pull the first dotted numeric version out of free-form output."""
    m = _LEADING_VERSION_RE.search(raw)
    return m.group(1) if m else None


def _numeric_version_tuple(version: str) -> tuple[int, ...] | None:
    """Parse the leading dotted-integer components of a version string."""
    m = re.match(r"(\d+(?:\.\d+)*)", normalize_version(version))
    if not m:
        return None
    return tuple(int(part) for part in m.group(1).split("."))


def check_native_backend(
    probe_fn: Callable[[], dict] | None = None,
) -> PreflightResult:
    """Preflight the C++ router backend via ``probe_backend_info()``.

    ``warn`` (not ``fail``) when the extension is missing: the pure-Python
    fallback is a legitimate -- 10-100x slower -- environment; doctor's job is
    to make it loud, not to break consumer repos without a compiler. ``fail``
    is reserved for a probe that could not run at all.
    """
    if probe_fn is None:
        # Lazy import: ``cpp_backend`` import has side effects (extension
        # dlopen) that must not run for ``kct doctor --help``.
        from kicad_tools.router.cpp_backend import probe_backend_info

        probe_fn = probe_backend_info

    info = probe_fn()
    probe = info.get("probe") or {}
    if probe.get("failed"):
        error = probe.get("error") or "unknown probe error"
        return PreflightResult(
            NATIVE_BACKEND,
            PreflightStatus.FAIL,
            f"backend probe could not run: {error}",
            _REMEDY_PROBE_FAILED,
        )

    if info.get("available"):
        build_version = info.get("build_version")
        required = info.get("required_build_version")
        extension_path = info.get("extension_path")
        extension = Path(extension_path).name if extension_path else "unknown extension"
        if build_version == required:
            return PreflightResult(
                NATIVE_BACKEND,
                PreflightStatus.OK,
                (
                    f"available (version {info.get('version')}, "
                    f"build {build_version}/{required}, {extension})"
                ),
                None,
            )
        return PreflightResult(
            NATIVE_BACKEND,
            PreflightStatus.WARN,
            (
                f"available but stale: build version {build_version} != "
                f"required {required} ({extension})"
            ),
            _REMEDY_BUILD_NATIVE,
        )

    reason = info.get("unavailable_reason") or "not installed"
    return PreflightResult(
        NATIVE_BACKEND,
        PreflightStatus.WARN,
        f"not available: {reason} (pure-Python routing fallback is 10-100x slower)",
        _REMEDY_BUILD_NATIVE,
    )


def check_kicad_cli(
    find_cli_fn: Callable[[], Path | None] | None = None,
    version_fn: Callable[[Path], str | None] | None = None,
) -> PreflightResult:
    """Preflight ``kicad-cli`` via ``find_kicad_cli()`` + ``get_kicad_version()``."""
    if find_cli_fn is None:
        from kicad_tools.cli.runner import find_kicad_cli

        find_cli_fn = find_kicad_cli
    if version_fn is None:
        from kicad_tools.cli.runner import get_kicad_version

        version_fn = get_kicad_version

    cli = find_cli_fn()
    if cli is None:
        return PreflightResult(
            KICAD_CLI,
            PreflightStatus.FAIL,
            "kicad-cli not found on PATH or in known install locations",
            _REMEDY_INSTALL_KICAD,
        )

    version = version_fn(cli)
    if version is None:
        return PreflightResult(
            KICAD_CLI,
            PreflightStatus.WARN,
            f"found at {cli} but its version could not be queried",
            _REMEDY_INSTALL_KICAD,
        )

    major_match = re.match(r"\s*(\d+)", version)
    if major_match is None:
        return PreflightResult(
            KICAD_CLI,
            PreflightStatus.WARN,
            f"found at {cli} but reported an unrecognized version: {version!r}",
            _REMEDY_INSTALL_KICAD,
        )

    major = int(major_match.group(1))
    if major < KICAD_MAJOR_FLOOR:
        return PreflightResult(
            KICAD_CLI,
            PreflightStatus.WARN,
            (
                f"found at {cli} but version {version} is below the "
                f"KiCad {KICAD_MAJOR_FLOOR}+ compatibility floor"
            ),
            _REMEDY_INSTALL_KICAD,
        )

    return PreflightResult(
        KICAD_CLI,
        PreflightStatus.OK,
        f"found at {cli} (KiCad {version})",
        None,
    )


def check_python_env(
    has_shapely_fn: Callable[[], bool] | None = None,
    version_info: tuple[int, int, int] | None = None,
) -> PreflightResult:
    """Preflight the Python interpreter floor + the shapely core dependency.

    Both conditions are ``fail`` when unmet: an interpreter below the
    ``requires-python`` floor or an unimportable core dependency means a
    broken / partial install, not a degraded-but-working one.
    """
    if has_shapely_fn is None:
        from kicad_tools._shapely import has_shapely

        has_shapely_fn = has_shapely
    vi = version_info if version_info is not None else sys.version_info[:3]

    problems: list[str] = []
    remedies: list[str] = []
    if (vi[0], vi[1]) < PYTHON_FLOOR:
        problems.append(
            f"Python {vi[0]}.{vi[1]}.{vi[2]} is below the required "
            f"{PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]} floor"
        )
        remedies.append(_REMEDY_PYTHON_FLOOR)
    if not has_shapely_fn():
        from kicad_tools._shapely import SHAPELY_INSTALL_HINT

        problems.append("shapely (core dependency) is not importable")
        remedies.append(SHAPELY_INSTALL_HINT)

    if problems:
        return PreflightResult(
            PYTHON_ENV,
            PreflightStatus.FAIL,
            "; ".join(problems),
            "; ".join(remedies),
        )
    return PreflightResult(
        PYTHON_ENV,
        PreflightStatus.OK,
        (
            f"Python {vi[0]}.{vi[1]}.{vi[2]} "
            f"(>= {PYTHON_FLOOR[0]}.{PYTHON_FLOOR[1]}) with shapely importable"
        ),
        None,
    )


def _query_kct_path_version(kct_path: str) -> str | None:
    """Run ``<kct_path> --version`` and return its output, ``None`` on failure."""
    import subprocess

    try:
        result = subprocess.run(
            [kct_path, "--version"],
            capture_output=True,
            text=True,
            timeout=30,
        )
    except Exception:
        return None
    if result.returncode != 0:
        return None
    return result.stdout.strip() or None


def check_kct_path(
    installed_version: str | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    path_version_fn: Callable[[str], str | None] | None = None,
) -> PreflightResult:
    """Preflight for a PATH ``kct`` shadowing the active environment's version.

    A stale global ``kct`` (e.g. ``~/.local/bin/kct``) resolves first on PATH
    and silently runs older behavior; new subcommands then surface as
    ``invalid choice`` -- reading as a missing feature rather than a wrong
    binary. ``warn`` on any version mismatch, naming both paths and versions,
    with extra emphasis when the PATH-resolved ``kct`` is *older* (the
    dangerous direction).
    """
    if which_fn is None:
        import shutil

        which_fn = shutil.which
    if path_version_fn is None:
        path_version_fn = _query_kct_path_version
    if installed_version is None:
        from kicad_tools import __version__

        installed_version = __version__

    kct_path = which_fn("kct")
    if not kct_path:
        return PreflightResult(
            KCT_PATH,
            PreflightStatus.OK,
            (
                f"no 'kct' on PATH; the active environment "
                f"(v{normalize_version(installed_version)}, e.g. 'uv run kct') "
                "cannot be shadowed"
            ),
            None,
        )

    raw = path_version_fn(kct_path)
    if raw is None:
        return PreflightResult(
            KCT_PATH,
            PreflightStatus.WARN,
            (
                f"'kct' on PATH at {kct_path} but its version could not be "
                f"queried; it may shadow the active environment "
                f"(v{normalize_version(installed_version)})"
            ),
            _REMEDY_USE_UV_RUN,
        )

    path_version = _extract_version(raw)
    if path_version is None:
        return PreflightResult(
            KCT_PATH,
            PreflightStatus.WARN,
            (
                f"'kct' on PATH at {kct_path} reported an unrecognized "
                f"version ({raw!r}); it may shadow the active environment "
                f"(v{normalize_version(installed_version)})"
            ),
            _REMEDY_USE_UV_RUN,
        )

    if _versions_match(path_version, installed_version):
        return PreflightResult(
            KCT_PATH,
            PreflightStatus.OK,
            (
                f"PATH 'kct' at {kct_path} matches the active environment "
                f"(v{normalize_version(path_version)})"
            ),
            None,
        )

    path_tuple = _numeric_version_tuple(path_version)
    installed_tuple = _numeric_version_tuple(installed_version)
    older = path_tuple is not None and installed_tuple is not None and path_tuple < installed_tuple
    if older:
        detail = (
            f"PATH 'kct' at {kct_path} is v{normalize_version(path_version)}, "
            f"OLDER than the active environment's "
            f"v{normalize_version(installed_version)}; bare 'kct' silently runs "
            "the stale install, and newer subcommands appear as 'invalid choice'"
        )
    else:
        detail = (
            f"PATH 'kct' at {kct_path} is v{normalize_version(path_version)} "
            f"but the active environment is "
            f"v{normalize_version(installed_version)}"
        )
    return PreflightResult(KCT_PATH, PreflightStatus.WARN, detail, _REMEDY_USE_UV_RUN)


def _run_preflight(name: str, fn: Callable[[], PreflightResult]) -> PreflightResult:
    """Run one preflight fail-soft: an unexpected raise becomes a ``fail`` row."""
    try:
        return fn()
    except Exception as exc:  # noqa: BLE001 - never-raise convention
        return PreflightResult(
            name,
            PreflightStatus.FAIL,
            f"check raised unexpectedly: {exc.__class__.__name__}: {exc}",
            None,
        )


def check_environment(
    *,
    installed_version: str | None = None,
    probe_fn: Callable[[], dict] | None = None,
    find_cli_fn: Callable[[], Path | None] | None = None,
    version_fn: Callable[[Path], str | None] | None = None,
    has_shapely_fn: Callable[[], bool] | None = None,
    version_info: tuple[int, int, int] | None = None,
    which_fn: Callable[[str], str | None] | None = None,
    path_version_fn: Callable[[str], str | None] | None = None,
) -> EnvironmentReport:
    """Run every environment preflight (fixed order) and aggregate the results.

    All probe callables are injectable for tests; production defaults are
    resolved lazily inside each check. Never raises: a probe that blows up
    unexpectedly degrades to a ``fail`` :class:`PreflightResult`.
    """
    checks = [
        _run_preflight(NATIVE_BACKEND, lambda: check_native_backend(probe_fn=probe_fn)),
        _run_preflight(
            KICAD_CLI,
            lambda: check_kicad_cli(find_cli_fn=find_cli_fn, version_fn=version_fn),
        ),
        _run_preflight(
            PYTHON_ENV,
            lambda: check_python_env(has_shapely_fn=has_shapely_fn, version_info=version_info),
        ),
        _run_preflight(
            KCT_PATH,
            lambda: check_kct_path(
                installed_version=installed_version,
                which_fn=which_fn,
                path_version_fn=path_version_fn,
            ),
        ),
    ]
    return EnvironmentReport(checks=checks)


# ---------------------------------------------------------------------------
# Environment rendering
# ---------------------------------------------------------------------------

_PREFLIGHT_GLYPH = {
    PreflightStatus.OK: "ok  ",
    PreflightStatus.WARN: "warn",
    PreflightStatus.FAIL: "FAIL",
}


def environment_to_dict(report: EnvironmentReport) -> dict:
    """Serialize an :class:`EnvironmentReport` to the agent-facing JSON shape."""
    return {
        "check": "environment",
        "ok": report.ok,
        "has_fail": report.has_fail,
        "has_warn": report.has_warn,
        "checks": [
            {
                "name": c.name,
                "status": c.status.value,
                "detail": c.detail,
                "remedy": c.remedy,
            }
            for c in report.checks
        ],
    }


def render_environment_text(report: EnvironmentReport) -> str:
    """Render an :class:`EnvironmentReport` as human-readable text."""
    lines = ["kct doctor: environment preflight", ""]
    for c in report.checks:
        glyph = _PREFLIGHT_GLYPH.get(c.status, c.status.value)
        lines.append(f"  [{glyph}] {c.name:<26} {c.detail}")
        if c.remedy:
            lines.append(f"         remedy: {c.remedy}")
    lines.append("")
    if report.has_fail:
        failing = ", ".join(c.name for c in report.failed_checks)
        lines.append(f"FAIL: environment preflight failed: {failing}")
    elif report.has_warn:
        warning = ", ".join(c.name for c in report.checks if c.is_warn)
        lines.append(f"WARN: environment degraded (advisory): {warning}")
    else:
        lines.append("OK: environment preflight passed.")
    return "\n".join(lines)
