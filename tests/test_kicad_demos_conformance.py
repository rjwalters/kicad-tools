"""KiCad-demos conformance corpus (issue #4880, item 5).

Uses KiCad's own bundled ``share/kicad/demos`` (hierarchy-heavy, multi-unit,
real eeschema/pcbnew output) as a local, free, version-matched oracle: every
``.kicad_sch``/``.kicad_pcb`` file found there is parsed with this repo's own
schema parsers, and must parse without an exception. Where feasible this also
asserts a form of round-trip equivalence: a parse -> serialize -> re-parse ->
serialize cycle is a fixed point, and the parsed high-level footprint count
matches the raw S-expression footprint-block count (a direct guard against
the "silently half-understood schema" defect shape of #4873/#4874 -- a
version-dispatch gap that quietly drops an entire component graph instead of
raising or warning).

**Skips the whole module when no KiCad install is found** (mirrors
``tests/creepage/test_export_rules.py``'s ``skipif`` pattern), so
contributors without KiCad installed are never blocked. Verified present
inside the ``kicad/kicad:10.0`` CI container image (``ci.yml``'s
``image: kicad/kicad:10.0`` job): ``/usr/share/kicad/demos`` ships 115
``.kicad_sch`` files and 19 ``.kicad_pcb`` files across ~17 demo projects
(``cm5_minima``, ``complex_hierarchy``, ``kit-dev-coldfire-xilinx_5213``,
``stickhub``, ``tiny_tapeout``, ...).

**Two of those 19 boards are enormous**: ``jetson-agx-thor-baseboard.kicad_pcb``
(~85 MB) and ``vme-wren.kicad_pcb`` (~70 MB) -- real production designs, not
toy fixtures. Every file still gets the core "parses without exception" check
regardless of size, but the *additional* round-trip-equivalence checks
(footprint-count cross-check, fixed-point re-serialization) are skipped above
``_PCB_ROUNDTRIP_SIZE_CAP_BYTES`` to keep this module's CI cost bounded --
each of those two files alone costs roughly a minute of parse+serialize time
at this size, and running the fixed-point check twice more per file (as an
earlier draft of this test did, by re-parsing independently in each test
function instead of sharing one cached parse) pushed a full local run past
ten minutes. All per-file parses are memoized (``functools.cache``) so each
demo file is parsed at most once across every test that exercises it.
"""

from __future__ import annotations

import functools
from pathlib import Path

import pytest

from kicad_tools.cli.runner import find_kicad_cli
from kicad_tools.schema.pcb import FOOTPRINT_TAGS, PCB
from kicad_tools.schema.schematic import Schematic
from kicad_tools.sexp import SExp, parse_string

# Above this size, skip the extra round-trip-equivalence checks (footprint
# count cross-check, fixed-point re-serialization) and rely on the
# "parses without exception" check alone -- see module docstring.
_PCB_ROUNDTRIP_SIZE_CAP_BYTES = 10 * 1024 * 1024  # 10 MB


def _find_kicad_demos_dir() -> Path | None:
    """Best-effort derivation of KiCad's bundled demos directory.

    Confirmed against the ``kicad/kicad:10.0`` Docker image used by CI
    (``.github/workflows/ci.yml``): ``kicad-cli`` resolves to
    ``/usr/bin/kicad-cli`` and the demos live at ``/usr/share/kicad/demos``
    -- i.e. ``kicad_cli.parent.parent / "share/kicad/demos"``. The macOS
    (``KiCad.app/Contents/MacOS/kicad-cli`` -> ``Contents/SharedSupport/demos``)
    and Windows candidates below are best-effort derivations from the same
    per-OS install locations ``find_kicad_cli()`` already searches -- they
    are NOT verified against a real install in this environment, so this
    function still returns ``None`` (and the module still skips) on a
    layout none of these candidates match, rather than guessing further.
    """
    kicad_cli = find_kicad_cli()
    if kicad_cli is None:
        return None

    candidates = (
        # Linux: /usr/bin/kicad-cli -> /usr/share/kicad/demos (verified in
        # the kicad/kicad:10.0 CI container).
        kicad_cli.parent.parent / "share" / "kicad" / "demos",
        # Some distro packagings version-namespace the share directory.
        kicad_cli.parent.parent / "share" / "kicad" / "9.0" / "demos",
        kicad_cli.parent.parent / "share" / "kicad" / "10.0" / "demos",
        # macOS app bundle: .../KiCad.app/Contents/MacOS/kicad-cli, so
        # parent.parent is Contents/.
        kicad_cli.parent.parent / "SharedSupport" / "demos",
        # Windows: C:/Program Files/KiCad/<ver>/bin/kicad-cli.exe
        kicad_cli.parent.parent / "share" / "demos",
    )
    for candidate in candidates:
        if candidate.is_dir():
            return candidate
    return None


KICAD_DEMOS_DIR = _find_kicad_demos_dir()

pytestmark = pytest.mark.skipif(
    KICAD_DEMOS_DIR is None,
    reason="KiCad demos directory not found (no KiCad install, or an install "
    "layout this test's candidate list doesn't recognize)",
)


def _demo_files(suffix: str) -> list[Path]:
    if KICAD_DEMOS_DIR is None:
        return []
    return sorted(KICAD_DEMOS_DIR.rglob(f"*{suffix}"))


PCB_DEMO_FILES = _demo_files(".kicad_pcb")
SCH_DEMO_FILES = _demo_files(".kicad_sch")


def _demo_id(path: Path) -> str:
    assert KICAD_DEMOS_DIR is not None
    return str(path.relative_to(KICAD_DEMOS_DIR))


@functools.cache
def _parse_raw(path: Path) -> SExp:
    """Parse a demo file's raw text exactly once per test session."""
    return parse_string(path.read_text(encoding="utf-8"))


@functools.cache
def _load_pcb(path: Path) -> PCB:
    """Construct a ``PCB`` from the memoized raw parse (no second file parse)."""
    return PCB(_parse_raw(path), path)


@functools.cache
def _load_schematic(path: Path) -> Schematic:
    """Construct a ``Schematic`` from the memoized raw parse (no second file parse)."""
    return Schematic(_parse_raw(path), path)


class TestKiCadDemosPCBConformance:
    """Every demo ``.kicad_pcb`` file parses cleanly with ``schema.pcb.PCB``."""

    @pytest.mark.parametrize("pcb_path", PCB_DEMO_FILES, ids=_demo_id)
    def test_parses_without_exception(self, pcb_path: Path) -> None:
        _load_pcb(pcb_path)

    @pytest.mark.parametrize("pcb_path", PCB_DEMO_FILES, ids=_demo_id)
    def test_footprint_count_matches_raw_sexp(self, pcb_path: Path) -> None:
        """No silent component-graph loss (the #4873-shaped defect class).

        The number of ``Footprint`` objects the schema layer produces must
        equal the number of top-level ``(footprint ...)``/``(module ...)``
        blocks in the raw S-expression tree -- a version-dispatch gap that
        silently drops a whole class of top-level child would show up here
        as a count mismatch instead of passing silently.
        """
        if pcb_path.stat().st_size > _PCB_ROUNDTRIP_SIZE_CAP_BYTES:
            pytest.skip("file exceeds the round-trip-equivalence size cap; see module docstring")
        pcb = _load_pcb(pcb_path)
        raw_sexp = _parse_raw(pcb_path)
        raw_count = sum(1 for child in raw_sexp.iter_children() if child.tag in FOOTPRINT_TAGS)
        assert len(pcb.footprints) == raw_count

    @pytest.mark.parametrize("pcb_path", PCB_DEMO_FILES, ids=_demo_id)
    def test_reserialize_reparse_is_stable_fixed_point(self, pcb_path: Path) -> None:
        """parse -> to_string() -> re-parse -> to_string() is a fixed point.

        This is the round-trip equivalence half of item 5's acceptance
        criteria: it does not assert byte-identity with KiCad's own
        formatting (the serializer intentionally reformats some whitespace),
        but it does assert that whatever the serializer emits on its first
        pass is stable under a second cycle -- the same fixed-point
        convention used by ``tests/test_kicad10_roundtrip_new_tokens.py``.
        """
        if pcb_path.stat().st_size > _PCB_ROUNDTRIP_SIZE_CAP_BYTES:
            pytest.skip("file exceeds the round-trip-equivalence size cap; see module docstring")
        first = _parse_raw(pcb_path).to_string()
        second = parse_string(first).to_string()
        assert first == second


class TestKiCadDemosSchematicConformance:
    """Every demo ``.kicad_sch`` file parses cleanly with ``schema.schematic.Schematic``."""

    @pytest.mark.parametrize("sch_path", SCH_DEMO_FILES, ids=_demo_id)
    def test_parses_without_exception(self, sch_path: Path) -> None:
        _load_schematic(sch_path)

    @pytest.mark.parametrize("sch_path", SCH_DEMO_FILES, ids=_demo_id)
    def test_reserialize_reparse_is_stable_fixed_point(self, sch_path: Path) -> None:
        """parse -> to_string() -> re-parse -> to_string() is a fixed point.

        See ``TestKiCadDemosPCBConformance.test_reserialize_reparse_is_stable_fixed_point``
        for the same convention applied to schematics. No demo schematic
        approaches the PCB size cap above (the largest is ~1.4 MB), so no
        cap is applied here.
        """
        first = _parse_raw(sch_path).to_string()
        second = parse_string(first).to_string()
        assert first == second


def test_demos_directory_tolerates_zero_files_found() -> None:
    """A found-but-empty demos directory must not crash collection.

    ``PCB_DEMO_FILES``/``SCH_DEMO_FILES`` are computed at import time (before
    any test runs) via ``Path.rglob``, which returns an empty list -- never
    raises -- for a directory with no matching files. This test simply
    documents and asserts that invariant explicitly; the module-level skip
    above already handles the "no KiCad install at all" case.
    """
    assert isinstance(PCB_DEMO_FILES, list)
    assert isinstance(SCH_DEMO_FILES, list)
