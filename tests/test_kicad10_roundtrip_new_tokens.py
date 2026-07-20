"""Round-trip fidelity guard for KiCad-10 tokens the parser post-dates.

The S-expression layer (``src/kicad_tools/sexp/parser.py``) is deliberately
token-tolerant: it parses any well-formed S-expression into a generic ``SExp``
tree and ``SExp.to_string()`` re-emits whatever it parsed. Unknown tokens are
preserved structurally, so ``parse -> to_string`` should be lossless. That
lossless property is *asserted* elsewhere only for tokens the toolkit already
knew about (pads, nets, ``(attr ...)`` flags, keepout enums). KiCad 10 added
PCB tokens that post-date the parser's original design:

- **hatched zone fill mode** -- ``(fill yes (mode hatch) ...)`` plus the
  hatch geometry tokens (``hatch_thickness``, ``hatch_gap``,
  ``hatch_orientation``, ``hatch_border_algorithm``, ``hatch_min_hole_area``).
  This is distinct from the zone *outline display* hint ``(hatch edge 0.5)``
  that already round-trips on routed boards.
- **native rounded-rectangle representation** -- a pad ``roundrect`` shape
  carrying ``chamfer_ratio`` / ``(chamfer top_left)``, distinct from the plain
  ``roundrect_rratio`` that already round-trips.
- **inner-layer footprint objects** -- graphics (``fp_line`` / ``fp_rect``) on
  inner copper layers (``In1.Cu`` / ``In2.Cu``) referenced from inside a
  ``(footprint ...)``.

This module closes that coverage gap with a fidelity guard: a checked-in
KiCad-10-authored fixture (``tests/fixtures/test_kicad10_save_board_new_tokens.kicad_pcb``,
produced by ``kicad-cli pcb upgrade --force`` so it reflects genuine KiCad-10
emission) plus tests that parse it, re-emit it, and assert the new tokens
survive byte-meaningfully (and, where kicad-cli is available, that KiCad still
loads the re-emitted file).

Barcode note: KiCad 10 barcode objects are omitted from this fixture. The
barcode token has no stable hand-authorable form here (an invalid guess
segfaults the loader), and authoring one requires the KiCad GUI which is not
available in this environment. The remaining new tokens (hatch fill, chamfered
rounded-rect, inner-layer footprint objects) exercise the same
``to_string()`` / ``_needs_quoting()`` / inline-heuristic paths, so the
fidelity guard holds without it. See issue #4380.

Scope guard (issue #4380 vs #4378): these tests MUST NOT assert anything about
the writer's *emitted default* version stamps -- that is #4378's territory.
They assert only that the fixture's OWN embedded ``(version ...)`` /
``(generator_version ...)`` stamps round-trip verbatim (preserved via
``_original_str`` / ``_originally_quoted`` in the parser) and that the new
tokens survive re-emission.
"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

from kicad_tools.sexp import parse_file
from kicad_tools.sexp.parser import parse_string

# CI runners may lack KiCad; skip-guard the load assertion exactly like the
# existing round-trip tests do.
KICAD_CLI = shutil.which("kicad-cli")

FIXTURE_PATH = Path(__file__).parent / "fixtures" / "test_kicad10_save_board_new_tokens.kicad_pcb"

# Substrings that must survive a parse -> to_string cycle. Each corresponds to a
# KiCad-10 token that post-dates the parser's original schema-free design.
NEW_TOKEN_SUBSTRINGS = (
    # hatched zone FILL MODE (not the (hatch edge ...) outline-display hint)
    "(mode hatch)",
    "hatch_thickness",
    "hatch_gap",
    "hatch_orientation",
    "hatch_border_algorithm",
    "hatch_min_hole_area",
    # native rounded-rect representation with a chamfer
    "roundrect",
    "chamfer_ratio",
    "(chamfer top_left)",
    # inner-layer footprint objects
    "In1.Cu",
    "In2.Cu",
)

# Bare token *names* (the atom immediately after an opening paren) that the
# re-emitted output must still contain -- guards against a heuristic dropping a
# whole token rather than merely reformatting it.
NEW_TOKEN_NAMES = frozenset(
    {
        "hatch_thickness",
        "hatch_gap",
        "hatch_orientation",
        "hatch_border_algorithm",
        "hatch_min_hole_area",
        "roundrect_rratio",
        "chamfer_ratio",
        "chamfer",
        "fp_line",
        "fp_rect",
    }
)


def _token_names(text: str) -> set[str]:
    """Collect the set of S-expression token names present in ``text``.

    A token name is the bare atom immediately following an opening paren, e.g.
    ``mode`` in ``(mode hatch)`` or ``hatch_thickness`` in
    ``(hatch_thickness 0.5)``.
    """
    return set(re.findall(r"\(([A-Za-z_][A-Za-z0-9_]*)", text))


@pytest.fixture
def fixture_path() -> Path:
    if not FIXTURE_PATH.exists():
        pytest.skip(f"Fixture not found: {FIXTURE_PATH}")
    return FIXTURE_PATH


class TestKiCad10NewTokenFidelity:
    """parse -> to_string fidelity for KiCad-10 tokens the parser post-dates."""

    def test_new_tokens_survive_reemission(self, fixture_path: Path) -> None:
        """Each new KiCad-10 token survives a parse -> to_string cycle."""
        out = parse_file(fixture_path).to_string()
        for token in NEW_TOKEN_SUBSTRINGS:
            assert token in out, (
                f"KiCad-10 token {token!r} was dropped or mangled by "
                f"SExp.to_string(); the fix belongs in the serializer "
                f"(to_string / _needs_quoting / inline heuristics)."
            )

    def test_no_silent_token_drop(self, fixture_path: Path) -> None:
        """The re-emitted token-name set is a superset of the new-token set.

        Guards against a formatting/inline heuristic dropping a whole token
        rather than merely reformatting its whitespace.
        """
        out = parse_file(fixture_path).to_string()
        emitted = _token_names(out)
        missing = NEW_TOKEN_NAMES - emitted
        assert not missing, f"re-emitted output dropped token names: {sorted(missing)}"

    def test_fixed_point_stability(self, fixture_path: Path) -> None:
        """A second parse/serialize cycle is byte-identical (a fixed point).

        Mirrors the fixed-point assertion in
        ``test_bare_backslash_token_roundtrips_quoted_and_escaped``: the first
        emission may reformat KiCad's whitespace, but re-parsing and
        re-emitting that output must reproduce it byte-for-byte.
        """
        out = parse_file(fixture_path).to_string()
        assert parse_string(out).to_string() == out

    def test_fixture_own_stamps_roundtrip_verbatim(self, fixture_path: Path) -> None:
        """The fixture's OWN version stamps round-trip verbatim.

        Scope guard (issue #4380): this asserts the fixture's embedded stamps
        are preserved by the parse/re-emit path -- it does NOT assert anything
        about the writer's *emitted default* stamps, which is #4378's scope.
        """
        out = parse_file(fixture_path).to_string()
        assert "(version 20260206)" in out
        assert '(generator_version "10.0")' in out

    def test_hatch_fill_mode_distinct_from_outline_hint(self, fixture_path: Path) -> None:
        """The fixture carries the hatched FILL MODE, not just the outline hint.

        ``(hatch edge 0.5)`` is the zone outline-display hint that already
        round-trips on routed boards; ``(fill yes (mode hatch) ...)`` is the
        distinct KiCad-10 hatched *fill mode* this guard targets. Both may be
        present, but the fill-mode form is the load-bearing one.
        """
        out = parse_file(fixture_path).to_string()
        assert "(mode hatch)" in out, "fixture must carry the hatched fill mode"

    @pytest.mark.skipif(KICAD_CLI is None, reason="kicad-cli not installed")
    def test_reemitted_file_loads_in_kicad(self, fixture_path: Path, tmp_path: Path) -> None:
        """The re-emitted fixture loads in KiCad without a parse failure."""
        assert KICAD_CLI is not None  # narrowed by the skipif guard
        out = parse_file(fixture_path).to_string()
        output_path = tmp_path / "roundtrip_new_tokens.kicad_pcb"
        output_path.write_text(out)

        result = subprocess.run(
            [KICAD_CLI, "pcb", "drc", str(output_path), "-o", str(tmp_path / "drc.json")],
            capture_output=True,
            text=True,
        )
        # kicad-cli returns 0 on success or if violations found; it prints
        # "Failed to load board" only on a parse/load failure.
        assert "Failed to load board" not in result.stderr, (
            f"KiCad failed to load the re-emitted file.\n"
            f"stdout: {result.stdout}\n"
            f"stderr: {result.stderr}"
        )
