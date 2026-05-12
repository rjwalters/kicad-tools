"""Regression tests for issue #2782 — silent write-path failure on routed PCB.

Two complementary regressions are exercised here:

1. **Byte-identical write-path failure (issue #2782)** — if the router
   reports success but the on-disk routed PCB is byte-identical to the
   unrouted input, the build pipeline must fail loudly rather than ship
   an unrouted PCB downstream as a routed one.  Covered by
   :class:`TestRoutePostconditionByteIdentical`.

2. **Spec-driven routed-PCB path (issue #2782 + ProjectArtifacts.pcb_routed)** —
   when ``project.kct`` declares ``project.artifacts.pcb_routed``, the
   build pipeline writes to that exact path instead of the historical
   ``<stem>_routed.kicad_pcb`` sibling.  Covered by
   :class:`TestResolveRoutedPcbPath`.

3. **Board 06 project.kct schema reconciliation (issue #2782)** — board
   06's ``project.kct`` was the artefact that surfaced this issue;
   ``load_spec`` must accept it with zero validation errors.  Covered
   by :class:`TestBoardProjectKctLoads`.

A separate :class:`TestBoardSpecRoundtrip` test runs every board's
``project.kct`` through the loader to guard against future schema
drift on the example boards.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from kicad_tools.cli.build_cmd import (
    BuildContext,
    _check_route_postcondition,
    _resolve_routed_pcb_path,
)
from kicad_tools.spec.parser import load_spec
from kicad_tools.spec.schema import (
    ComponentSuggestion,
    ProjectArtifacts,
    Suggestions,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
BOARDS_DIR = REPO_ROOT / "boards"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


_PCB_TEMPLATE = """\
(kicad_pcb
  (version 20240108)
  (generator {generator})
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VIN")
  (net 2 "VOUT")
  (gr_rect
    (start 0 0)
    (end 50 50)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge-uuid")
  )
  (footprint "Test:U1"
    (layer "F.Cu")
    (at 10 10)
    (uuid "fp1-uuid")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "VIN"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "VOUT"))
  )
  (footprint "Test:U2"
    (layer "F.Cu")
    (at 30 10)
    (uuid "fp2-uuid")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "VIN"))
    (pad "2" smd rect (at 2 0) (size 1 1) (layers "F.Cu") (net 2 "VOUT"))
  )
)
"""


def _write_unrouted_pcb(path: Path, generator: str = '"kicad-tools-demo"') -> None:
    path.write_text(_PCB_TEMPLATE.format(generator=generator))


# ---------------------------------------------------------------------------
# Postcondition: byte-identical input vs output (issue #2782)
# ---------------------------------------------------------------------------


class TestRoutePostconditionByteIdentical:
    """Routed PCB == unrouted PCB must trigger a loud postcondition failure.

    The route step's success exit + byte-identical output on a board
    that has multi-pad signal nets is exactly the "silent write-path
    failure" symptom this issue is named after.  The postcondition
    must surface this with a non-success ``BuildResult`` so the build
    pipeline aborts before downstream steps consume the unrouted PCB.
    """

    def test_byte_identical_files_fail_postcondition(self, tmp_path: Path) -> None:
        """Identical bytes + multi-pad signal nets => loud failure."""
        input_pcb = tmp_path / "board.kicad_pcb"
        routed_pcb = tmp_path / "board_routed.kicad_pcb"
        _write_unrouted_pcb(input_pcb)
        # Copy bytes (exact, including any line-ending quirks)
        routed_pcb.write_bytes(input_pcb.read_bytes())

        result = _check_route_postcondition(input_pcb=input_pcb, routed_pcb=routed_pcb)

        assert result is not None, (
            "Postcondition must fail when routed PCB is byte-identical to input"
        )
        assert result.success is False
        assert "byte-identical" in result.message
        assert "#2782" in result.message

    def test_non_identical_routed_with_segments_passes(self, tmp_path: Path) -> None:
        """Routed PCB with at least one segment is accepted."""
        input_pcb = tmp_path / "board.kicad_pcb"
        routed_pcb = tmp_path / "board_routed.kicad_pcb"
        _write_unrouted_pcb(input_pcb)

        # Build a routed PCB that differs from the input AND contains
        # one real copper segment.
        routed_text = input_pcb.read_text().replace(
            '(generator "kicad-tools-demo")',
            '(generator "pcbnew")',
        )
        # Insert one segment before the final closing paren.
        routed_text = routed_text.rstrip().rstrip(")") + (
            "  (segment (start 10 10) (end 12 10) (width 0.25) "
            '(layer "F.Cu") (net 1) (uuid "seg-1"))\n)\n'
        )
        routed_pcb.write_text(routed_text)

        result = _check_route_postcondition(input_pcb=input_pcb, routed_pcb=routed_pcb)
        # Either None (parser accepted -> segments > 0 -> postcondition
        # silent) or success=True; the failure path must NOT fire.
        if result is not None:
            assert result.success is True, f"Postcondition fired unexpectedly: {result.message}"

    def test_byte_identical_same_path_does_not_fire(self, tmp_path: Path) -> None:
        """If input and routed paths resolve to the same file, do not fire.

        Some pipelines route in-place (input.kicad_pcb -> input.kicad_pcb).
        That's a separate failure mode the postcondition should not
        spuriously flag, since 'byte-identical' is tautological when
        ``input.resolve() == routed.resolve()``.
        """
        pcb = tmp_path / "board.kicad_pcb"
        _write_unrouted_pcb(pcb)

        result = _check_route_postcondition(input_pcb=pcb, routed_pcb=pcb)
        # The byte-identical branch must early-out on same-path; the
        # zero-segments branch may still fire (it does here), but the
        # message must NOT reference issue #2782's wording.
        if result is not None:
            assert "byte-identical" not in result.message

    def test_no_signal_nets_does_not_fire(self, tmp_path: Path) -> None:
        """A PCB with no multi-pad signal nets => postcondition stays silent."""
        # PCB with single-pad-per-net (no routable signal nets).
        pcb_text = """\
(kicad_pcb
  (version 20240108)
  (generator "kicad-tools-demo")
  (general (thickness 1.6))
  (layers
    (0 "F.Cu" signal)
    (31 "B.Cu" signal)
    (44 "Edge.Cuts" user)
  )
  (net 0 "")
  (net 1 "VCC")
  (gr_rect
    (start 0 0)
    (end 10 10)
    (stroke (width 0.15) (type solid))
    (fill none)
    (layer "Edge.Cuts")
    (uuid "edge")
  )
  (footprint "Test:U1"
    (layer "F.Cu")
    (at 1 1)
    (uuid "fp1")
    (pad "1" smd rect (at 0 0) (size 1 1) (layers "F.Cu") (net 1 "VCC"))
  )
)
"""
        input_pcb = tmp_path / "board.kicad_pcb"
        routed_pcb = tmp_path / "board_routed.kicad_pcb"
        input_pcb.write_text(pcb_text)
        routed_pcb.write_bytes(input_pcb.read_bytes())

        result = _check_route_postcondition(input_pcb=input_pcb, routed_pcb=routed_pcb)
        # No routable signal nets => zero segments is legitimate, and
        # byte-identical is also legitimate (router had nothing to do).
        assert result is None


# ---------------------------------------------------------------------------
# Spec-driven routed-PCB path (ProjectArtifacts.pcb_routed)
# ---------------------------------------------------------------------------


class TestResolveRoutedPcbPath:
    """``_resolve_routed_pcb_path`` honours the spec when ``pcb_routed`` is set."""

    def test_uses_spec_pcb_routed_when_declared(self, tmp_path: Path) -> None:
        """``spec.project.artifacts.pcb_routed`` overrides the default sibling path."""
        from kicad_tools.spec.schema import ProjectMetadata, ProjectSpec

        pcb_file = tmp_path / "input" / "board.kicad_pcb"
        pcb_file.parent.mkdir(parents=True)
        pcb_file.touch()

        spec = ProjectSpec(
            project=ProjectMetadata(
                name="Test",
                artifacts=ProjectArtifacts(
                    pcb="input/board.kicad_pcb",
                    pcb_routed="output/custom_routed.kicad_pcb",
                ),
            ),
        )

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            spec=spec,
            pcb_file=pcb_file,
            output_dir=None,
        )
        resolved = _resolve_routed_pcb_path(ctx)
        assert resolved == tmp_path / "output" / "custom_routed.kicad_pcb"

    def test_falls_back_to_output_dir_when_no_spec(self, tmp_path: Path) -> None:
        """No spec / no ``pcb_routed`` field => use ``--output`` dir if provided."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.touch()
        output_dir = tmp_path / "custom-output"

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            spec=None,
            pcb_file=pcb_file,
            output_dir=output_dir,
        )
        resolved = _resolve_routed_pcb_path(ctx)
        assert resolved == output_dir / "board_routed.kicad_pcb"

    def test_falls_back_to_sibling_path_by_default(self, tmp_path: Path) -> None:
        """No spec / no output dir => use historical sibling path."""
        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.touch()

        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            spec=None,
            pcb_file=pcb_file,
            output_dir=None,
        )
        resolved = _resolve_routed_pcb_path(ctx)
        assert resolved == tmp_path / "board_routed.kicad_pcb"

    def test_absolute_pcb_routed_is_respected(self, tmp_path: Path) -> None:
        """Absolute ``pcb_routed`` paths are used verbatim."""
        from kicad_tools.spec.schema import ProjectMetadata, ProjectSpec

        pcb_file = tmp_path / "board.kicad_pcb"
        pcb_file.touch()
        abs_dest = (tmp_path / "abs-output" / "board_routed.kicad_pcb").resolve()

        spec = ProjectSpec(
            project=ProjectMetadata(
                name="Test",
                artifacts=ProjectArtifacts(
                    pcb="board.kicad_pcb",
                    pcb_routed=str(abs_dest),
                ),
            ),
        )
        ctx = BuildContext(
            project_dir=tmp_path,
            spec_file=None,
            spec=spec,
            pcb_file=pcb_file,
            output_dir=tmp_path / "ignored",
        )
        assert _resolve_routed_pcb_path(ctx) == abs_dest


# ---------------------------------------------------------------------------
# Schema reconciliation
# ---------------------------------------------------------------------------


class TestBoardProjectKctLoads:
    """Board 06's project.kct must load with zero Pydantic validation errors.

    Prior to issue #2782's resolution, ``load_spec`` raised 9 errors:
    1 for ``requirements.manufacturing.layers.stackup`` (list inside
    ``dict[str, int]``) and 8 for ``suggestions.components.<name>``
    (bare string instead of ``ComponentSuggestion``).  The schema now
    accepts both shapes via top-level promotion and string-shorthand
    unwrapping.
    """

    def test_board_06_project_kct_loads_clean(self) -> None:
        """No validation errors on the curated board 06 project spec."""
        path = BOARDS_DIR / "06-diffpair-test" / "project.kct"
        if not path.exists():
            pytest.skip(f"Board 06 spec not present at {path}")

        # If this raises ValidationError, pytest will surface the
        # individual field errors verbatim -- exactly what we want.
        spec = load_spec(path)

        # Sanity-check the fields that drove the original failure.
        assert spec.requirements is not None
        assert spec.requirements.manufacturing is not None
        assert spec.requirements.manufacturing.layers == {"preferred": 4}
        # Stackup was promoted from layers.stackup to top-level stackup.
        assert spec.requirements.manufacturing.stackup is not None
        assert len(spec.requirements.manufacturing.stackup) == 4
        assert spec.suggestions is not None
        assert spec.suggestions.components is not None
        # The bare-string shorthand is normalised to preferred=[<string>].
        first = next(iter(spec.suggestions.components.values()))
        assert first.preferred is not None
        assert len(first.preferred) >= 1

    def test_pcb_routed_is_picked_up_from_spec(self) -> None:
        """``ProjectArtifacts.pcb_routed`` is a real field, not silently ignored."""
        path = BOARDS_DIR / "06-diffpair-test" / "project.kct"
        if not path.exists():
            pytest.skip(f"Board 06 spec not present at {path}")

        spec = load_spec(path)
        assert spec.project.artifacts is not None
        assert spec.project.artifacts.pcb_routed == ("output/diffpair_test_routed.kicad_pcb")


class TestComponentSuggestionShorthand:
    """``ComponentSuggestion`` accepts bare strings via shorthand."""

    def test_bare_string_unwrapped_to_preferred(self) -> None:
        suggestions = Suggestions.model_validate({"components": {"regulator": "LM7805 5V LDO"}})
        assert suggestions.components is not None
        regulator = suggestions.components["regulator"]
        assert regulator.preferred == ["LM7805 5V LDO"]
        assert regulator.rationale is None
        assert regulator.avoid is None

    def test_full_dict_form_still_works(self) -> None:
        suggestion = ComponentSuggestion.model_validate(
            {
                "preferred": ["LM7805", "TPS562201"],
                "rationale": "Common, well-documented",
            }
        )
        assert suggestion.preferred == ["LM7805", "TPS562201"]
        assert suggestion.rationale == "Common, well-documented"


class TestBoardSpecRoundtrip:
    """Every board's ``project.kct`` must load with zero errors.

    Guards against future schema drift on the example boards.  This is
    parametrised over the directories under ``boards/`` so adding a
    new board automatically extends coverage.
    """

    @pytest.mark.parametrize(
        "board_dir",
        sorted(p.name for p in BOARDS_DIR.glob("[0-9]*") if (p / "project.kct").exists()),
    )
    def test_board_spec_loads(self, board_dir: str) -> None:
        path = BOARDS_DIR / board_dir / "project.kct"
        spec = load_spec(path)
        # Minimal sanity: project name is required by the schema.
        assert spec.project.name
