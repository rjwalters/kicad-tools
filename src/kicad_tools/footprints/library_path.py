"""KiCad library path detection utilities.

Detects the location of KiCad's standard footprint libraries on different platforms.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import platform
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path

from kicad_tools.kicad_install import windows_kicad_base_dirs, windows_kicad_install_roots

# Default library paths for each platform
_KICAD_LIBRARY_PATHS: dict[str, list[str | Path]] = {
    "Darwin": [  # macOS
        # KiCad 8.x standard installation
        "/Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
        Path.home() / "Applications/KiCad/KiCad.app/Contents/SharedSupport/footprints",
        # KiCad versioned installations (e.g., KiCad 8.0)
        "/Applications/KiCad/kicad.app/Contents/SharedSupport/footprints",
        # User-specific KiCad libraries
        Path.home() / "Library/Application Support/kicad/footprints",
        Path.home() / ".local/share/kicad/8.0/footprints",
        Path.home() / ".local/share/kicad/footprints",
        # Homebrew installation
        "/opt/homebrew/share/kicad/footprints",
        "/usr/local/share/kicad/footprints",
    ],
    "Linux": [
        "/usr/share/kicad/footprints",
        "/usr/local/share/kicad/footprints",
        Path.home() / ".local/share/kicad/footprints",
        # KiCad 8.x versioned user paths
        Path.home() / ".local/share/kicad/8.0/footprints",
        # Flatpak
        Path.home() / ".var/app/org.kicad.KiCad/data/kicad/footprints",
    ],
    "Windows": [
        # Unversioned installs (Program Files, Program Files (x86), AppData) are
        # derived from windows_kicad_base_dirs() in detect_kicad_library_path().
        Path.home() / "Documents/KiCad/8.0/footprints",
    ],
}

# Standard library mappings: footprint name patterns -> library directories
# These are common footprint libraries in KiCad's standard installation
STANDARD_LIBRARY_MAPPINGS = {
    # Capacitors
    "C_": "Capacitor_SMD.pretty",
    "CP_": "Capacitor_SMD.pretty",
    # Resistors
    "R_": "Resistor_SMD.pretty",
    # Inductors
    "L_": "Inductor_SMD.pretty",
    # LEDs
    "LED_": "LED_SMD.pretty",
    # Crystals
    "Crystal_": "Crystal.pretty",
    # Connectors
    "Conn_": "Connector_PinHeader_2.54mm.pretty",
    "PinHeader_": "Connector_PinHeader_2.54mm.pretty",
    "USB_": "Connector_USB.pretty",
    # ICs and packages
    "SOIC-": "Package_SO.pretty",
    "SOP-": "Package_SO.pretty",
    "SSOP-": "Package_SO.pretty",
    "TSSOP-": "Package_SO.pretty",
    "QFP-": "Package_QFP.pretty",
    "QFN-": "Package_DFN_QFN.pretty",
    "DFN-": "Package_DFN_QFN.pretty",
    "BGA-": "Package_BGA.pretty",
    "SOT-": "Package_TO_SOT_SMD.pretty",
    "TO-": "Package_TO_SOT_SMD.pretty",
    "LQFP-": "Package_QFP.pretty",
}


@dataclass(frozen=True)
class LibraryRead:
    """One footprint file resolved from a detected standard library (Issue #9526).

    Emitted by the read funnel (:meth:`LibraryPaths.get_footprint_file` and
    :meth:`LibraryPaths.find_footprint_by_name`) so an evidence producer can
    record *which* standard-library geometry it consumed: the ``.kicad_mod``
    files ``PCB.add_footprint`` embeds come from whatever root
    :func:`detect_kicad_library_path` resolved, which no version file pins.
    Reads made through explicit paths (project ``fp-lib-table`` entries,
    repo-local ``.pretty`` directories, ``add_footprint_from_file``) never go
    through the funnel and are deliberately not recorded.
    """

    root: Path
    """The library root the read resolved under."""
    source: str
    """That root's ``detect_kicad_library_path`` source: 'config'/'env'/'auto'."""
    path: Path
    """The resolved ``.kicad_mod`` file."""
    sha256: str
    """Digest of the bytes the producer consumed, snapshotted when the file
    was first read (Issue #9526 review).  Recorded here -- not re-read at
    evidence time -- so a later edit of the library file cannot rewrite what
    the producer actually used; the parser reuses the same snapshot
    (:func:`recorded_footprint_bytes`), so the digest always describes the
    geometry that was placed."""


@dataclass
class _ReadScope:
    """One evidence run's collection.

    ``reads`` records each consumed file (deduplicated per ``(root, path)``
    in first-seen order); ``snapshots`` keeps the bytes each read returned so
    ``PCB.add_footprint_from_file`` parses exactly what the digest describes,
    however many times the same footprint is placed (Issue #9526 review).
    """

    reads: dict[tuple[str, str], LibraryRead] = field(default_factory=dict)
    snapshots: dict[str, bytes] = field(default_factory=dict)


#: Stack of per-evidence-run scopes.  Each :func:`library_read_scope` (one
#: :func:`kicad_tools.cli.kicad_toolchain.evidence_toolchain` context) pushes
#: a fresh scope so consecutive runs never see each other's reads and a
#: nested exit restores the outer collection untouched (Issue #9526 review).
#: Outside any scope nothing is recorded *or read*: plain path resolution
#: stays free of provenance reads (Issue #9526 review).
_READ_SCOPES: list[_ReadScope] = []


def _active_scope() -> _ReadScope | None:
    """The scope new reads record into, or None outside any evidence run."""
    return _READ_SCOPES[-1] if _READ_SCOPES else None


def record_library_read(root: Path, source: str, path: Path) -> None:
    """Record *path* as consumed from the library *root* (idempotent).

    Only an active :func:`library_read_scope` collects -- that is, an evidence
    producer.  Plain lookups (``kct sch set-footprint`` validation and
    friends) resolve paths without opening anything, keeping their best-effort
    error handling (Issue #9526 review).  Inside a scope the bytes are read
    once, at consumption: digested into the record and snapshotted for the
    parser, together with the *source* the lookup resolved under -- so later
    file edits or environment changes cannot rewrite the record, and repeated
    placements reuse the recorded bytes instead of re-reading the file.
    """
    scope = _active_scope()
    if scope is None:
        return
    key = (str(root), str(path))
    if key not in scope.reads:
        data = Path(path).read_bytes()
        scope.reads[key] = LibraryRead(
            root=Path(root),
            source=source,
            path=Path(path),
            sha256=hashlib.sha256(data).hexdigest(),
        )
        scope.snapshots[str(path)] = data


def recorded_library_reads() -> tuple[LibraryRead, ...]:
    """The standard-library reads recorded in the active scope, first-seen order.

    Empty outside any :func:`library_read_scope` -- no producer, no record."""
    scope = _active_scope()
    return tuple(scope.reads.values()) if scope else ()


def recorded_footprint_bytes(path: str | Path) -> bytes | None:
    """The bytes the active scope recorded for *path*, if it has.

    ``PCB.add_footprint_from_file`` parses from this snapshot instead of
    reopening the file, so the geometry it places is always the bytes the
    provenance digest describes -- across repeated placements of one
    footprint and across a mid-run library edit (Issue #9526 review)."""
    scope = _active_scope()
    if scope is None:
        return None
    return scope.snapshots.get(str(Path(path)))


@contextlib.contextmanager
def library_read_scope() -> Iterator[None]:
    """Collect standard-library reads into a fresh scope (Issue #9526 review).

    Reads made inside the ``with`` block go to a collection that vanishes at
    exit; the outer collection is restored untouched.  One scope per
    :func:`kicad_tools.cli.kicad_toolchain.evidence_toolchain` entry keeps
    each evidence run's provenance its own, with no manual reset between
    consecutive runs.
    """
    _READ_SCOPES.append(_ReadScope())
    try:
        yield
    finally:
        _READ_SCOPES.pop()


def reset_library_reads() -> None:
    """Forget every recorded read, in every scope (test isolation)."""
    _READ_SCOPES.clear()


@dataclass
class LibraryPaths:
    """Container for KiCad library paths."""

    footprints_path: Path | None
    """Path to the footprints directory, or None if not found."""

    source: str
    """Where the path came from: 'auto', 'config', or 'env'."""

    @property
    def found(self) -> bool:
        """Whether a valid footprints path was found."""
        return self.footprints_path is not None and self.footprints_path.exists()

    def get_library_path(self, library_name: str) -> Path | None:
        """Get path to a specific footprint library directory.

        Args:
            library_name: Library name with or without .pretty extension
                          (e.g., "Capacitor_SMD" or "Capacitor_SMD.pretty")

        Returns:
            Path to the library directory if found, None otherwise.
        """
        if not self.footprints_path:
            return None

        # Ensure .pretty extension
        if not library_name.endswith(".pretty"):
            library_name = f"{library_name}.pretty"

        lib_path = self.footprints_path / library_name
        if lib_path.exists() and lib_path.is_dir():
            return lib_path

        return None

    def get_footprint_file(
        self, library_name: str, footprint_name: str, fallback_search: bool = True
    ) -> Path | None:
        """Get path to a specific footprint file.

        Args:
            library_name: Library name (e.g., "Capacitor_SMD")
            footprint_name: Footprint name (e.g., "C_0402_1005Metric")
            fallback_search: If True and the library isn't found, search all
                           available libraries for the footprint (default: True)

        Returns:
            Path to the .kicad_mod file if found, None otherwise.
        """
        lib_path = self.get_library_path(library_name)

        # Ensure .kicad_mod extension for filename
        fp_filename = footprint_name
        if not fp_filename.endswith(".kicad_mod"):
            fp_filename = f"{fp_filename}.kicad_mod"

        if lib_path:
            fp_path = lib_path / fp_filename
            if fp_path.exists():
                root = self.footprints_path
                if root is not None:
                    record_library_read(root, self.source, fp_path)
                return fp_path

        # If library wasn't found or footprint not in library, try fallback search
        if fallback_search:
            result = self.find_footprint_by_name(footprint_name)
            if result:
                return result

        return None

    def find_footprint_by_name(self, footprint_name: str) -> Path | None:
        """Search all available libraries for a footprint by name.

        This is a fallback search when the explicitly named library isn't found.
        Useful for finding footprints when library paths differ between KiCad versions.

        Args:
            footprint_name: Footprint name (e.g., "Converter_ACDC_Hi-Link_HLK-PMxx")

        Returns:
            Path to the .kicad_mod file if found, None otherwise.
        """
        if not self.footprints_path or not self.footprints_path.exists():
            return None

        # Ensure .kicad_mod extension
        fp_filename = footprint_name
        if not fp_filename.endswith(".kicad_mod"):
            fp_filename = f"{fp_filename}.kicad_mod"

        # Search all .pretty directories
        for lib_dir in self.footprints_path.iterdir():
            if lib_dir.is_dir() and lib_dir.name.endswith(".pretty"):
                fp_path = lib_dir / fp_filename
                if fp_path.exists():
                    record_library_read(self.footprints_path, self.source, fp_path)
                    return fp_path

        return None


def detect_kicad_library_path(config_override: str | Path | None = None) -> LibraryPaths:
    """Detect the KiCad footprint library path.

    Checks in order:
    1. Explicit config override
    2. KICAD_FOOTPRINT_DIR environment variable
    3. Platform-specific default locations

    Args:
        config_override: Optional explicit path from configuration

    Returns:
        LibraryPaths with the detected path and source information.
    """
    # 1. Check config override
    if config_override:
        path = Path(config_override)
        if path.exists():
            return LibraryPaths(footprints_path=path, source="config")

    # 2. Check environment variable
    env_path = os.environ.get("KICAD_FOOTPRINT_DIR")
    if env_path:
        path = Path(env_path)
        if path.exists():
            return LibraryPaths(footprints_path=path, source="env")

    # 3. Check platform-specific defaults
    system = platform.system()
    default_paths = [Path(p) for p in _KICAD_LIBRARY_PATHS.get(system, [])]
    if system == "Windows":
        # Versioned installs (newest first), then unversioned, then user dirs.
        default_paths = (
            [root / "share" / "kicad" / "footprints" for root in windows_kicad_install_roots()]
            + [base / "share" / "kicad" / "footprints" for base in windows_kicad_base_dirs()]
            + default_paths
        )

    for path in default_paths:
        path = Path(path)
        if path.exists() and path.is_dir():
            return LibraryPaths(footprints_path=path, source="auto")

    # Not found
    return LibraryPaths(footprints_path=None, source="auto")


def guess_standard_library(footprint_name: str) -> str | None:
    """Guess the standard library name for a footprint.

    Uses naming conventions to guess which KiCad standard library
    a footprint belongs to.

    Args:
        footprint_name: The footprint name (e.g., "C_0402_1005Metric")

    Returns:
        Library name (e.g., "Capacitor_SMD") if guessed, None otherwise.
    """
    for prefix, library in STANDARD_LIBRARY_MAPPINGS.items():
        if footprint_name.startswith(prefix):
            # Return without .pretty extension
            return library.removesuffix(".pretty")

    return None


def parse_library_id(lib_id: str) -> tuple[str | None, str]:
    """Parse a full library ID into library name and footprint name.

    KiCad library IDs can be in format "Library:FootprintName" or just "FootprintName".

    Args:
        lib_id: The library ID (e.g., "Capacitor_SMD:C_0402_1005Metric")

    Returns:
        Tuple of (library_name, footprint_name). library_name may be None.
    """
    if ":" in lib_id:
        library, footprint = lib_id.split(":", 1)
        return library, footprint
    return None, lib_id


def compare_footprint_lib_ids(schematic_id: str, pcb_id: str) -> str:
    """Classify how a schematic footprint lib ID relates to a PCB one.

    Returns one of:

    * ``"match"`` -- same footprint name, and the library nicknames agree
      (or neither side has one).
    * ``"nickname_missing"`` -- same footprint name, but exactly one side
      lacks the ``Library:`` nickname (a legacy board written before
      ``PCB.add_footprint`` kept it, Issue #7213). kicad-cli's parity
      check reports this as ``footprint_symbol_mismatch``; it is the same
      package, so callers should treat it as informational, not drift.
    * ``"mismatch"`` -- different footprint names, or both sides carry a
      nickname and the nicknames differ (``Capacitor_SMD:X`` vs
      ``Resistor_SMD:X``).
    """
    sch_lib, sch_name = parse_library_id(schematic_id)
    pcb_lib, pcb_name = parse_library_id(pcb_id)
    if sch_name != pcb_name:
        return "mismatch"
    if sch_lib and pcb_lib:
        return "match" if sch_lib == pcb_lib else "mismatch"
    if sch_lib or pcb_lib:
        return "nickname_missing"
    return "match"


def list_available_libraries(paths: LibraryPaths) -> list[str]:
    """List all available footprint libraries.

    Args:
        paths: LibraryPaths object with detected paths

    Returns:
        List of library names (without .pretty extension).
    """
    if not paths.footprints_path or not paths.footprints_path.exists():
        return []

    libraries = []
    for item in paths.footprints_path.iterdir():
        if item.is_dir() and item.name.endswith(".pretty"):
            libraries.append(item.name.removesuffix(".pretty"))

    return sorted(libraries)


def list_project_libraries(
    schematic_path: Path | None,
    global_paths: LibraryPaths,
    *,
    use_project_table: bool = True,
) -> list[tuple[str, Path, str]]:
    """Return ``(nickname, .pretty_dir, origin)`` triples merged from sources.

    Discovers the project ``fp-lib-table`` co-located with *schematic_path*
    (walked upward to the ``.kicad_pro`` root) and merges it with the
    global library scan (the existing ``footprints_path/*.pretty`` walker).

    Project entries are listed first and "win" on nickname collision -- this
    matches KiCad's own resolution order (project table is consulted before
    the global table).  Each tuple's third element is ``"project"`` or
    ``"global"`` so callers can surface provenance.

    Args:
        schematic_path: Path to a ``.kicad_sch`` or ``.kicad_pro`` file used
            to locate the project fp-lib-table.  When ``None``, only the
            global libraries are returned.
        global_paths: Detected global library paths.
        use_project_table: When ``False``, the project table is skipped
            (useful for ``--no-project-lib`` and CI reproducibility).

    Returns:
        List of ``(nickname, library_dir, origin)``.  Empty when nothing is
        discoverable in either source.
    """
    # Local import to avoid a top-level cycle (fp_lib_table imports the
    # sexp parser, which is fine, but library_path is a low-level helper
    # that other footprints code imports broadly).
    from kicad_tools.footprints.fp_lib_table import (
        find_project_fp_lib_table,
        parse_fp_lib_table,
    )

    result: list[tuple[str, Path, str]] = []
    seen: set[str] = set()

    # Project libraries first -- they win on collision.
    if use_project_table and schematic_path is not None:
        table_path = find_project_fp_lib_table(Path(schematic_path))
        if table_path is not None:
            for entry in parse_fp_lib_table(table_path):
                if entry.type != "KiCad":
                    # Legacy/Github types are skipped silently here; callers
                    # who need to warn can re-parse via parse_fp_lib_table.
                    continue
                if entry.resolved_path is None:
                    continue
                if not entry.resolved_path.is_dir():
                    continue
                if entry.name in seen:
                    continue
                seen.add(entry.name)
                result.append((entry.name, entry.resolved_path, "project"))

    # Global libraries from the directory scan.
    for lib_name in list_available_libraries(global_paths):
        if lib_name in seen:
            continue
        lib_dir = global_paths.get_library_path(lib_name)
        if lib_dir is None:
            continue
        seen.add(lib_name)
        result.append((lib_name, lib_dir, "global"))

    return result
