"""Pinned KiCad toolchain for committed native evidence (Issue #6962).

Native DRC/ERC evidence used to depend on how the producing machine's KiCad
*global* library tables happened to be set up.  Board 03's readiness bundle
gained 37 ``lib_footprint_issues`` warnings ("The current configuration does
not include the footprint library ...") purely because it was regenerated on a
host with no global ``fp-lib-table``; the copper had not changed.  This module
makes the library configuration part of the pinned toolchain instead:

* **Pinned tables.**  ``kicad_tools/data/kicad-config/10.0/{fp,sym}-lib-table``
  are flat copies of the stock tables shipped in CI's pinned
  ``kicad/kicad`` image (byte-identical to the KiCad 10.0.7 macOS app's
  template, too).  :func:`populate_config_home` copies them into a scratch
  ``KICAD_CONFIG_HOME``; :func:`kicad_cli_env` returns an environment that
  points kicad-cli at one.  Nothing reads or writes the user's GUI config.
* **Pinned image.**  :func:`pinned_kicad_image` is the image kct's Docker
  fallback runs by default.  It is the digest in ``.github/workflows/ci.yml``
  (``KICAD_IMAGE``, the single source of truth, #5682): read from that file
  when this module runs from a repository checkout, and otherwise the
  :data:`PINNED_KICAD_IMAGE` mirror, which a test keeps equal to it.
* **Qualified version.**  :func:`evidence_toolchain` is the context every
  evidence producer runs inside.  It isolates the config home, routes
  ``kicad-cli`` through the pinned image when ``KCT_KICAD_DOCKER=1``, and
  refuses any ``kicad-cli`` whose version is not :data:`PINNED_KICAD_VERSION`
  -- a host build of another release would silently record a different
  ``kicad_version`` and a different library snapshot.
* **Fail closed.**  :func:`require_library_environment` turns a
  ``lib_footprint_issues`` finding into :class:`KiCadEnvironmentError`, so a
  missing library table stops the producer instead of becoming a warning.
* **Canonical reports.**  :func:`canonical_drc_report` sorts the finding
  arrays and normalises ``date`` so two runs on the same toolchain write the
  same bytes.

The module imports only the standard library at import time:
``scripts/ci/init_kicad_libraries.py`` loads it by file path inside CI
containers before the project environment exists.

Run ``python -m kicad_tools.cli.kicad_toolchain --check`` to see which
toolchain an evidence producer would use, and whether it would be accepted.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Callable, Iterable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

#: Mirror of ``.github/workflows/ci.yml``'s ``KICAD_IMAGE`` (the source of
#: truth).  Used only when the workflow file is not available, e.g. from an
#: installed wheel; ``tests/test_kicad_toolchain_6962.py`` keeps them equal.
PINNED_KICAD_IMAGE = (
    "kicad/kicad@sha256:18693567392b80da435f9fa952ce3a3e534c66eb5a6033f5b9c80aa3b19dd3ec"
)

#: ``kicad-cli --version`` of :data:`PINNED_KICAD_IMAGE`.  The only version
#: committed native evidence may be produced with.
PINNED_KICAD_VERSION = "10.0.6"

#: The ``<major>.<minor>`` configuration directory the pinned tables belong to.
KICAD_CONFIG_VERSION = "10.0"

#: Library tables a KiCad config home must hold for headless native checks.
LIBRARY_TABLES = ("fp-lib-table", "sym-lib-table")

#: Directory holding ``<config version>/<table>`` copies of the pinned tables.
CONFIG_TABLES_ROOT = Path(__file__).resolve().parents[1] / "data" / "kicad-config"

KICAD_CONFIG_HOME_ENV = "KICAD_CONFIG_HOME"
#: Opt-in env var enabling the container fallback (``docker run kicad/kicad``).
KICAD_DOCKER_ENV = "KCT_KICAD_DOCKER"
#: Env var overriding the container fallback's image.
KICAD_DOCKER_IMAGE_ENV = "KCT_KICAD_DOCKER_IMAGE"
#: Mirrors ``runner.KICAD_CLI_PROBE_TIMEOUT_ENV``: a container start is slower
#: than a native launch, so the Docker shim raises the discovery probe budget.
_PROBE_TIMEOUT_ENV = "KCT_KICAD_CLI_PROBE_TIMEOUT"
_DOCKER_PROBE_TIMEOUT = "180"

#: The text of a ``lib_footprint_issues`` finding caused by a library table
#: that lacks the nickname (verified black-box on kicad-cli 10.0.6/10.0.7).
MISSING_LIBRARY_TEXT = "does not include the footprint library"

#: DRC report arrays whose order kicad-cli does not keep stable between runs.
_FINDING_ARRAYS = ("violations", "unconnected_items", "schematic_parity")

_CI_WORKFLOW = Path(".github") / "workflows" / "ci.yml"
_KICAD_IMAGE_LINE = re.compile(r"^  KICAD_IMAGE: *(\S+) *$", re.MULTILINE)
_VERSION_NUMBER = re.compile(r"(\d+\.\d+(?:\.\d+)?)")


class KiCadEnvironmentError(RuntimeError):
    """The KiCad environment cannot produce trustworthy evidence.

    Raised for an unqualified ``kicad-cli`` version, an unavailable Docker
    fallback, or a native report showing that kicad-cli could not resolve a
    library (``lib_footprint_issues``).  Producers stop and write nothing.
    """


# ---------------------------------------------------------------------------
# Pinned image
# ---------------------------------------------------------------------------


def ci_workflow_image(workflow: Path) -> str | None:
    """The single ``KICAD_IMAGE`` value in *workflow*, or ``None``."""
    try:
        text = workflow.read_text()
    except OSError:
        return None
    images = _KICAD_IMAGE_LINE.findall(text)
    return images[0] if len(images) == 1 else None


def _checkout_workflow() -> Path | None:
    """``ci.yml`` of the kicad-tools checkout this module runs from, if any."""
    here = Path(__file__).resolve()
    for parent in here.parents:
        package = parent / "src" / "kicad_tools"
        if package.is_dir() and here.is_relative_to(package):
            workflow = parent / _CI_WORKFLOW
            return workflow if workflow.is_file() else None
    return None


def pinned_kicad_image() -> str:
    """The image CI pins, read from ``ci.yml`` when available (#5682)."""
    workflow = _checkout_workflow()
    if workflow is not None:
        image = ci_workflow_image(workflow)
        if image:
            return image
    return PINNED_KICAD_IMAGE


def docker_image() -> str:
    """Image for kct's Docker fallback: ``KCT_KICAD_DOCKER_IMAGE`` or the CI pin."""
    return os.environ.get(KICAD_DOCKER_IMAGE_ENV, "").strip() or pinned_kicad_image()


def docker_fallback_enabled() -> bool:
    """True when the user opted into the container fallback via ``KCT_KICAD_DOCKER``."""
    return os.environ.get(KICAD_DOCKER_ENV, "").strip().lower() in ("1", "true", "yes", "on")


# ---------------------------------------------------------------------------
# Pinned library tables / isolated config home
# ---------------------------------------------------------------------------


def library_tables_dir(config_version: str = KICAD_CONFIG_VERSION) -> Path:
    """Directory holding the pinned tables for *config_version*."""
    directory = CONFIG_TABLES_ROOT / config_version
    missing = [name for name in LIBRARY_TABLES if not (directory / name).is_file()]
    if missing:
        raise KiCadEnvironmentError(
            f"No pinned KiCad {config_version} library tables ({', '.join(missing)} "
            f"missing under {directory})"
        )
    return directory


def populate_config_home(root: Path, config_version: str = KICAD_CONFIG_VERSION) -> Path:
    """Write the pinned tables into ``<root>/<config version>/`` and return *root*.

    Existing tables there are replaced: a config home used for evidence holds
    exactly the pinned tables, never whatever an earlier run left behind.
    """
    source = library_tables_dir(config_version)
    target = Path(root) / config_version
    target.mkdir(parents=True, exist_ok=True)
    for name in LIBRARY_TABLES:
        shutil.copyfile(source / name, target / name)
    return Path(root)


def check_config_home(root: Path, config_version: str = KICAD_CONFIG_VERSION) -> list[str]:
    """Problems that stop *root* from being a pinned config home (``[]`` = ok)."""
    source = library_tables_dir(config_version)
    problems = []
    for name in LIBRARY_TABLES:
        table = Path(root) / config_version / name
        if not table.is_file():
            problems.append(f"{table} is missing")
        elif table.read_bytes() != (source / name).read_bytes():
            problems.append(f"{table} differs from the pinned {name}")
    return problems


def kicad_cli_env(config_home: Path, base: Mapping[str, str] | None = None) -> dict[str, str]:
    """A subprocess environment pointing kicad-cli at *config_home*."""
    env = dict(os.environ if base is None else base)
    env[KICAD_CONFIG_HOME_ENV] = str(config_home)
    return env


@contextlib.contextmanager
def scratch_config_home(prefix: str = "kct-kicad-config-") -> Iterator[Path]:
    """A temporary config home holding only the pinned tables."""
    with tempfile.TemporaryDirectory(prefix=prefix) as tmp:
        yield populate_config_home(Path(tmp))


_PROCESS_CONFIG_HOME: list[Path] = []


def process_config_home() -> Path:
    """A pinned config home shared by this process (removed at exit)."""
    if _PROCESS_CONFIG_HOME and not check_config_home(_PROCESS_CONFIG_HOME[0]):
        return _PROCESS_CONFIG_HOME[0]
    import atexit

    root = Path(tempfile.mkdtemp(prefix="kct-kicad-config-"))
    atexit.register(shutil.rmtree, root, True)
    _PROCESS_CONFIG_HOME[:] = [populate_config_home(root)]
    return root


# ---------------------------------------------------------------------------
# Docker routing
# ---------------------------------------------------------------------------


def _distinct_mounts(paths: Iterable[Path]) -> list[str]:
    """Absolute mount points, dropping duplicates and ones nested in another."""
    roots: list[str] = []
    for path in paths:
        text = os.path.normpath(os.path.abspath(str(path)))
        if text == os.sep:
            raise KiCadEnvironmentError("refusing to bind-mount / into the KiCad container")
        if any(text == r or text.startswith(r.rstrip(os.sep) + os.sep) for r in roots):
            continue
        roots = [r for r in roots if not r.startswith(text.rstrip(os.sep) + os.sep)]
        roots.append(text)
    return roots


def docker_kicad_cli_prefix(
    mounts: Iterable[Path],
    config_home: Path,
    *,
    image: str | None = None,
    workdir: str | None = None,
) -> list[str]:
    """``docker run`` argv up to (and including) the image, ending at kicad-cli.

    Every mount is bound at its own path (and at its resolved path, when that
    differs) so host paths in kicad-cli's arguments and reports stay valid.  The container runs as the calling user
    with ``KICAD_CONFIG_HOME`` (and ``HOME``) pointing at *config_home*, so it
    never depends on the image user's own config -- ``--user 0:0`` and the
    default ``kicad`` user produce the same reports.
    """
    home = str(Path(config_home).absolute())
    cmd = ["docker", "run", "--rm", "-i", "--platform", "linux/amd64"]
    if hasattr(os, "getuid"):
        cmd += ["--user", f"{os.getuid()}:{os.getgid()}"]
    requested = [*mounts, Path(home)]
    # Bind each directory under both its given and its resolved spelling:
    # callers pass either (macOS ``/var/folders`` is ``/private/var/folders``).
    resolved = [Path(os.path.realpath(path)) for path in requested]
    for mount in _distinct_mounts([*requested, *resolved]):
        cmd += ["-v", f"{mount}:{mount}"]
    cmd += ["-e", f"{KICAD_CONFIG_HOME_ENV}={home}", "-e", f"HOME={home}"]
    if workdir is not None:
        cmd += ["-w", workdir]
    cmd += ["--entrypoint", "kicad-cli", image or docker_image()]
    return cmd


def write_docker_shim(directory: Path, mounts: Iterable[Path], config_home: Path) -> Path:
    """Write an executable ``kicad-cli`` that runs the pinned image's kicad-cli."""
    prefix = docker_kicad_cli_prefix(mounts, config_home)
    entry = prefix.index("--entrypoint")
    quoted = " ".join(shlex.quote(part) for part in prefix[:entry])
    tail = " ".join(shlex.quote(part) for part in prefix[entry:])
    directory.mkdir(parents=True, exist_ok=True)
    shim = directory / "kicad-cli"
    shim.write_text(
        "#!/bin/sh\n"
        "# kct evidence toolchain (Issue #6962): kicad-cli in the pinned image.\n"
        f'exec {quoted} -w "$PWD" {tail} "$@"\n'
    )
    shim.chmod(0o755)
    return shim


# ---------------------------------------------------------------------------
# Evidence toolchain
# ---------------------------------------------------------------------------


def version_number(version: str | None) -> str | None:
    """``"10.0.6"`` out of a ``kicad-cli --version`` answer."""
    match = _VERSION_NUMBER.search(version or "")
    return match.group(1) if match else None


def _reset_kicad_cli_lookup() -> None:
    try:
        from kicad_tools.cli import runner
    except ImportError:  # pragma: no cover - standalone (CI init) use
        return
    runner.find_kicad_cli.cache_clear()  # type: ignore[attr-defined]


def _locate_kicad_cli() -> str | None:
    try:
        from kicad_tools.cli.runner import find_kicad_cli
    except ImportError:  # pragma: no cover - standalone (CI init) use
        return shutil.which("kicad-cli")
    found = find_kicad_cli()
    return None if found is None else str(found)


@dataclass(frozen=True)
class EvidenceToolchain:
    """The KiCad toolchain an evidence producer is running on."""

    kicad_cli: str
    version: str
    config_home: Path
    docker_image: str | None


@contextlib.contextmanager
def evidence_toolchain(
    mounts: Iterable[Path] = (),
    *,
    required_version: str | None = PINNED_KICAD_VERSION,
) -> Iterator[EvidenceToolchain]:
    """Run the enclosed block on the pinned evidence toolchain.

    For the duration of the block ``KICAD_CONFIG_HOME`` names a scratch home
    holding only the pinned tables (inherited by every subprocess), and with
    ``KCT_KICAD_DOCKER=1`` a ``kicad-cli`` shim at the front of ``PATH`` runs
    the pinned image with *mounts*, the working directory and the temporary
    directory bound at their own paths.  Raises :class:`KiCadEnvironmentError`
    when no kicad-cli answers or its version is not *required_version*.
    """
    saved = {
        name: os.environ.get(name) for name in (KICAD_CONFIG_HOME_ENV, "PATH", _PROBE_TIMEOUT_ENV)
    }
    with tempfile.TemporaryDirectory(prefix="kct-kicad-toolchain-") as tmp:
        scratch = Path(tmp)
        home = populate_config_home(scratch / "config")
        try:
            os.environ[KICAD_CONFIG_HOME_ENV] = str(home)
            image = None
            if docker_fallback_enabled():
                if shutil.which("docker") is None:
                    raise KiCadEnvironmentError(f"{KICAD_DOCKER_ENV}=1 but docker is not on PATH")
                image = docker_image()
                shim = write_docker_shim(
                    scratch / "bin",
                    [*mounts, Path.cwd(), Path(tempfile.gettempdir()), scratch],
                    home,
                )
                os.environ["PATH"] = os.pathsep.join([str(shim.parent), saved["PATH"] or ""])
                os.environ.setdefault(_PROBE_TIMEOUT_ENV, _DOCKER_PROBE_TIMEOUT)
            _reset_kicad_cli_lookup()
            binary = _locate_kicad_cli()
            if binary is None:
                raise KiCadEnvironmentError(
                    "kicad-cli not found; native evidence needs KiCad "
                    f"{PINNED_KICAD_VERSION} or {KICAD_DOCKER_ENV}=1 with docker"
                )
            try:
                answer = subprocess.run(
                    [binary, "--version"], capture_output=True, text=True, timeout=600
                )
            except (OSError, subprocess.SubprocessError) as exc:
                raise KiCadEnvironmentError(f"`{binary} --version` failed: {exc}") from exc
            version = version_number(answer.stdout or answer.stderr)
            if answer.returncode != 0 or version is None:
                raise KiCadEnvironmentError(
                    f"`{binary} --version` did not report a version "
                    f"(exit {answer.returncode}): {(answer.stderr or answer.stdout).strip()}"
                )
            if required_version is not None and version != required_version:
                raise KiCadEnvironmentError(
                    f"kicad-cli {version} ({binary}) is not the qualified evidence "
                    f"toolchain: committed native evidence is produced with KiCad "
                    f"{required_version}, the version of CI's pinned image "
                    f"{pinned_kicad_image()}.  Re-run with {KICAD_DOCKER_ENV}=1 to "
                    "route kicad-cli through that image, or install KiCad "
                    f"{required_version}."
                )
            yield EvidenceToolchain(binary, version, home, image)
        finally:
            for name, value in saved.items():
                if value is None:
                    os.environ.pop(name, None)
                else:
                    os.environ[name] = value
            _reset_kicad_cli_lookup()


def run_on_evidence_toolchain(main: Callable[[], int | None], mounts: Iterable[Path] = ()) -> int:
    """Run an evidence script's ``main`` inside :func:`evidence_toolchain`.

    An environment refusal prints why and returns 2 (nothing written past the
    refusal point); otherwise ``main``'s own exit status is returned.
    """
    try:
        with evidence_toolchain(mounts):
            return int(main() or 0)
    except KiCadEnvironmentError as exc:
        print(f"REFUSED (KiCad environment, Issue #6962): {exc}", file=sys.stderr)
        return 2


# ---------------------------------------------------------------------------
# Report checks
# ---------------------------------------------------------------------------


def library_environment_findings(report: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    """``lib_footprint_issues`` findings in a kicad-cli DRC report."""
    entries = report.get("violations")
    if not isinstance(entries, list):
        return []
    return [
        entry
        for entry in entries
        if isinstance(entry, Mapping) and entry.get("type") == "lib_footprint_issues"
    ]


def require_library_environment(report: Mapping[str, Any], *, source: str = "native DRC") -> None:
    """Raise :class:`KiCadEnvironmentError` if *report* has library findings.

    "The current configuration does not include the footprint library" means
    the producing environment's library table lacks a nickname the board
    uses -- a missing or incomplete global ``fp-lib-table``, or a typo'd
    nickname.  Either way the report describes the environment, not the board,
    so it must not become evidence.
    """
    findings = library_environment_findings(report)
    if not findings:
        return
    nicknames = sorted(
        {
            match.group(1)
            for entry in findings
            for match in [re.search(r"library '([^']+)'", str(entry.get("description", "")))]
            if match
        }
    )
    missing = any(MISSING_LIBRARY_TEXT in str(entry.get("description", "")) for entry in findings)
    cause = (
        "kicad-cli's footprint library table does not include "
        + (", ".join(nicknames) if nicknames else "every library the board uses")
        + "; the producing environment has no or an incomplete global fp-lib-table"
        if missing
        else "kicad-cli could not resolve the board's footprint libraries"
    )
    raise KiCadEnvironmentError(
        f"{source} reports {len(findings)} lib_footprint_issues finding(s): {cause}.  "
        "Refusing to write evidence; run the producer through the pinned toolchain "
        "(kicad_tools.cli.kicad_toolchain.evidence_toolchain), or fix the nickname "
        "if the library genuinely does not exist."
    )


def _finding_key(entry: Any) -> str:
    return json.dumps(entry, sort_keys=True, separators=(",", ":"))


def canonical_drc_report(report: Mapping[str, Any], *, date: str | None = None) -> dict[str, Any]:
    """*report* with stable finding order and, given *date*, a fixed ``date``.

    kicad-cli does not keep the order of findings stable between runs, and it
    stamps a wall-clock ``date``.  Sorting each finding array by its canonical
    JSON text (the items inside a finding keep their order) makes two runs on
    the same toolchain byte-identical.
    """
    canonical = dict(report)
    for key in _FINDING_ARRAYS:
        value = canonical.get(key)
        if isinstance(value, list):
            canonical[key] = sorted(value, key=_finding_key)
    if date is not None and "date" in canonical:
        canonical["date"] = date
    return canonical


def write_canonical_drc_report(path: Path, *, date: str | None = None) -> dict[str, Any]:
    """Rewrite the kicad-cli JSON report at *path* canonically; return it."""
    data = json.loads(Path(path).read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a kicad-cli JSON report")
    canonical = canonical_drc_report(data, date=date)
    Path(path).write_text(json.dumps(canonical, indent=4, ensure_ascii=False) + "\n")
    return canonical


def finalize_native_drc_report(
    path: Path, *, date: str | None = None, source: str | None = None
) -> dict[str, Any]:
    """Refuse a report with library findings, else rewrite it canonically.

    The one call a script makes after ``kicad-cli pcb drc`` wrote a report it
    is about to commit as evidence.  On refusal the report is deleted.
    """
    path = Path(path)
    data = json.loads(path.read_text())
    if not isinstance(data, dict):
        raise ValueError(f"{path} is not a kicad-cli JSON report")
    try:
        require_library_environment(data, source=source or f"native DRC report {path.name}")
    except KiCadEnvironmentError:
        path.unlink(missing_ok=True)
        raise
    return write_canonical_drc_report(path, date=date)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _check(mounts: list[Path]) -> int:
    print(f"Pinned image         : {pinned_kicad_image()}")
    workflow = _checkout_workflow()
    print(f"  source             : {workflow if workflow else 'packaged mirror (no ci.yml)'}")
    print(f"Qualified kicad-cli  : {PINNED_KICAD_VERSION}")
    print(f"Pinned library tables: {library_tables_dir()}")
    print(f"Docker fallback      : {'on' if docker_fallback_enabled() else 'off'}")
    try:
        with evidence_toolchain(mounts) as toolchain:
            print(f"kicad-cli            : {toolchain.kicad_cli} ({toolchain.version})")
            problems = check_config_home(toolchain.config_home)
            if problems:
                raise KiCadEnvironmentError("; ".join(problems))
    except KiCadEnvironmentError as exc:
        print(f"Evidence toolchain   : REFUSED -- {exc}")
        return 1
    print("Evidence toolchain   : ok (isolated KICAD_CONFIG_HOME with the pinned tables)")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m kicad_tools.cli.kicad_toolchain",
        description="Inspect the pinned KiCad toolchain native evidence is produced with.",
    )
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument(
        "--check",
        action="store_true",
        help="Report the toolchain an evidence producer would use; exit 1 if it is refused.",
    )
    action.add_argument(
        "--populate",
        metavar="DIR",
        type=Path,
        help="Write the pinned library tables into DIR/<version>/ (a KICAD_CONFIG_HOME).",
    )
    action.add_argument("--print-image", action="store_true", help="Print the pinned image.")
    parser.add_argument(
        "--mount",
        action="append",
        type=Path,
        default=[],
        help="Extra directory the Docker fallback binds (with --check).",
    )
    args = parser.parse_args(argv)
    if args.print_image:
        print(pinned_kicad_image())
        return 0
    if args.populate is not None:
        print(populate_config_home(args.populate))
        return 0
    return _check(args.mount)


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
