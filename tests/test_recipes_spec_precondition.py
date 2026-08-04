"""Spec-precondition gate for mutating board recipes (issue #4539).

:func:`kicad_tools.recipes.precondition.require_spec` is the **pre**-condition
half of the recipe contract (``kicad_tools.recipes.gate`` is the post-condition
half, #3912): before a board recipe mutates anything it asserts the board
carries captured intent -- a non-empty, parseable ``project.kct``.

Two layers of guard, mirroring ``tests/test_migrated_boards_gate_3912.py``:

1. **Structural** -- every one of the 8 board recipe entry points imports the
   shared helper and calls ``require_spec(__file__)``, anchored on ``__file__``
   and never on ``Path.cwd()`` / ``sys.argv``.
2. **Behavioural** -- ``tmp_path``-based exercises of the L0 -> L1 -> L2 ladder.

.. important::

   **The negative tests in :class:`TestGateActuallyFires` are the point of this
   file.** All 8 boards already ship a ``project.kct``, so at L1 the whole fleet
   passes on day one and an implementation that simply ``return True``-s is
   indistinguishable from a correct one when run over ``boards/``. Every test in
   that class is constructed so that a gate stubbed to always pass FAILS it.
   Do not weaken them.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

from kicad_tools.recipes.precondition import (
    SPEC_FILENAME,
    STRICT_ENV_VAR,
    SpecPreconditionResult,
    discover_spec,
    require_spec,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

# Every mutating board recipe entry point. Board 05's recipe is ``design.py``;
# the rest are ``generate_design.py``. Secondary scripts
# (``generate_schematic.py`` / ``generate_pcb.py`` / ``route_demo.py``) are
# sub-stages or standalone demos and are deliberately out of scope (#4539).
RECIPE_ENTRY_POINTS = [
    "boards/00-simple-led/generate_design.py",
    "boards/01-voltage-divider/generate_design.py",
    "boards/02-charlieplex-led/generate_design.py",
    "boards/03-usb-joystick/generate_design.py",
    "boards/04-stm32-devboard/generate_design.py",
    "boards/05-bldc-motor-controller/design.py",
    "boards/06-diffpair-test/generate_design.py",
    "boards/07-matchgroup-test/generate_design.py",
]

VALID_SPEC = """\
kct_version: "1.0"

project:
  name: "Precondition Fixture"
  revision: "A"

intent:
  summary: |
    A minimal but semantically valid spec used to exercise the L2 gate.
"""

# Replicates board-01's shape as of origin/main @ 83d7625e: ``progress.phase``
# names a phase that is absent from ``progress.phases``, so ``load_spec``
# succeeds (L1) but ``validate_spec`` reports an error (not L2).
L2_INVALID_SPEC = """\
kct_version: "1.0"

project:
  name: "L2 Invalid Fixture"
  revision: "A"

progress:
  phase: complete
  phases:
    concept:
      status: completed
    schematic:
      status: completed
"""

MALFORMED_YAML = 'kct_version: "1.0"\nproject:\n  name: [unclosed\n'


@pytest.fixture(autouse=True)
def _clear_strict_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """The helper reads ``KCT_REQUIRE_SPEC``; never inherit the ambient value."""
    monkeypatch.delenv(STRICT_ENV_VAR, raising=False)


def _make_board(tmp_path: Path, spec_text: str | None, name: str = "board") -> Path:
    """Create a fake board dir with a recipe file and (optionally) a spec.

    Returns the path of the recipe file (the discovery anchor).
    """
    board_dir = tmp_path / name
    board_dir.mkdir(parents=True, exist_ok=True)
    recipe = board_dir / "generate_design.py"
    recipe.write_text("# fake recipe\n")
    if spec_text is not None:
        (board_dir / SPEC_FILENAME).write_text(spec_text)
    return recipe


# --------------------------------------------------------------------------
# Structural guard: all 8 entry points call the shared helper
# --------------------------------------------------------------------------
@pytest.fixture(params=RECIPE_ENTRY_POINTS)
def recipe_source(request: pytest.FixtureRequest) -> str:
    path = REPO_ROOT / request.param
    assert path.is_file(), f"recipe entry point not found: {path}"
    return path.read_text()


class TestRecipeWiring:
    def test_imports_shared_helper(self, recipe_source: str) -> None:
        assert "from kicad_tools.recipes.precondition import require_spec" in recipe_source

    def test_calls_helper_anchored_on_dunder_file(self, recipe_source: str) -> None:
        # Exactly one call, and it MUST be anchored on ``__file__``.
        assert recipe_source.count("require_spec(") == 1
        assert "require_spec(__file__)" in recipe_source

    def test_does_not_anchor_on_cwd_or_argv(self, recipe_source: str) -> None:
        # CI runs recipes from the repo root with an out-of-tree output dir;
        # a cwd- or argv-anchored search would find nothing and the gate would
        # fire spuriously on all 8 boards (#4539).
        for forbidden in (
            "require_spec(Path.cwd())",
            "require_spec(sys.argv",
            "require_spec(output_dir",
        ):
            assert forbidden not in recipe_source

    def test_precedes_first_mutating_step(self, recipe_source: str) -> None:
        """The call must come before the recipe's first ``create_*`` step."""
        call_at = recipe_source.index("require_spec(__file__)")
        first_write = min(
            idx
            for idx in (
                recipe_source.find("create_project("),
                recipe_source.find("output_dir.mkdir"),
            )
            if idx != -1
        )
        # ``create_project`` is also *defined* above ``main()``; what matters is
        # that the call site precedes the invocation inside ``main()``.
        main_at = recipe_source.index("def main()")
        invocation = recipe_source.index("create_project(", main_at)
        assert call_at < invocation, "require_spec() must run before create_project()"
        assert first_write >= 0


class TestFleetSpecsExistAndParse:
    """Every shipped board must satisfy the DEFAULT (L1) gate."""

    def test_all_eight_boards_pass_l1(self) -> None:
        failures = []
        for rel in RECIPE_ENTRY_POINTS:
            result = require_spec(REPO_ROOT / rel, strict=False)
            if not result.ok:
                failures.append((rel, result.level, result.reasons))
        assert not failures, f"boards failing the default L1 gate: {failures}"


# --------------------------------------------------------------------------
# THE NEGATIVE TESTS -- a gate stubbed to always pass MUST fail these
# --------------------------------------------------------------------------
class TestGateActuallyFires:
    def test_missing_spec_is_advisory_and_falsy(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """MANDATORY negative test (#4539 "Vacuity risk").

        An anchor with no ``project.kct`` anywhere in its bounded ancestry must
        produce the advisory and a falsy verdict. An always-``True``
        implementation fails here.
        """
        recipe = _make_board(tmp_path, spec_text=None)
        result = require_spec(recipe, strict=False, max_depth=1)

        assert result.ok is False
        assert bool(result) is False
        assert result.level == "none"
        assert result.required_level == "L1"
        assert result.found_path is None
        assert result.searched  # the walk reports what it looked at

        err = capsys.readouterr().err
        assert "spec precondition" in err
        assert SPEC_FILENAME in err

    def test_missing_spec_in_strict_mode_exits_nonzero(self, tmp_path: Path) -> None:
        recipe = _make_board(tmp_path, spec_text=None)
        with pytest.raises(SystemExit) as excinfo:
            require_spec(recipe, strict=True, max_depth=1)
        assert excinfo.value.code == 1

    def test_strict_mode_via_env_var_exits_nonzero(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(STRICT_ENV_VAR, "1")
        recipe = _make_board(tmp_path, spec_text=None)
        with pytest.raises(SystemExit) as excinfo:
            require_spec(recipe, max_depth=1)
        assert excinfo.value.code == 1

    def test_empty_spec_is_falsy(self, tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
        recipe = _make_board(tmp_path, spec_text="")
        result = require_spec(recipe, strict=False, max_depth=1)
        assert result.ok is False
        assert result.level == "none"
        assert "is empty" in capsys.readouterr().err

    def test_whitespace_only_spec_is_falsy(self, tmp_path: Path) -> None:
        recipe = _make_board(tmp_path, spec_text="   \n\n\t\n")
        result = require_spec(recipe, strict=False, max_depth=1)
        assert result.ok is False
        assert result.level == "none"

    def test_malformed_yaml_is_falsy_and_raises_nothing(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recipe = _make_board(tmp_path, spec_text=MALFORMED_YAML)
        result = require_spec(recipe, strict=False, max_depth=1)  # must not raise
        assert result.ok is False
        assert result.level == "L0", "L0 attained (exists, non-empty) but L1 not"
        assert result.checked is True
        assert "does not parse" in capsys.readouterr().err

    def test_malformed_yaml_in_strict_mode_exits_nonzero(self, tmp_path: Path) -> None:
        recipe = _make_board(tmp_path, spec_text=MALFORMED_YAML)
        with pytest.raises(SystemExit):
            require_spec(recipe, strict=True, max_depth=1)


class TestLadderLevels:
    def test_valid_spec_passes_quietly(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        recipe = _make_board(tmp_path, spec_text=VALID_SPEC)
        result = require_spec(recipe, strict=False, max_depth=1)
        assert result.ok is True
        assert result.level == "L2"
        assert result.found_path == recipe.parent / SPEC_FILENAME
        assert capsys.readouterr().err == "", "a passing gate must print nothing"

    def test_l2_invalid_spec_passes_default_but_fails_strict(
        self, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Board-01's shape: parses (L1) but fails semantic validation (not L2).

        This is why L2 is NOT the default -- a default-on L2 would fail
        ``boards/01-voltage-divider``'s CI job for a bookkeeping defect.
        """
        recipe = _make_board(tmp_path, spec_text=L2_INVALID_SPEC)

        default_result = require_spec(recipe, strict=False, max_depth=1)
        assert default_result.ok is True
        assert default_result.level == "L1"
        assert capsys.readouterr().err == ""

        with pytest.raises(SystemExit) as excinfo:
            require_spec(recipe, strict=True, max_depth=1)
        assert excinfo.value.code == 1
        err = capsys.readouterr().err
        assert "not found in phases" in err

    def test_board_01_is_the_real_l2_offender(self) -> None:
        """Pins the measured fleet split: 8/8 at L1, 7/8 at L2.

        If board-01's spec is repaired later (explicitly out of scope for
        #4539), this test tells you exactly which assertion to update.
        """
        l1_pass = [
            rel for rel in RECIPE_ENTRY_POINTS if require_spec(REPO_ROOT / rel, strict=False).ok
        ]
        assert len(l1_pass) == 8

        l2_fail = []
        for rel in RECIPE_ENTRY_POINTS:
            try:
                require_spec(REPO_ROOT / rel, strict=True)
            except SystemExit:
                l2_fail.append(rel)
        assert l2_fail == ["boards/01-voltage-divider/generate_design.py"]


class TestDiscoveryAnchor:
    def test_anchors_on_recipe_file_not_cwd(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The sibling spec is found even when cwd is somewhere else entirely.

        This is the CI invocation shape: ``uv run python
        boards/00-simple-led/generate_design.py /tmp/board00-ci`` runs from the
        repo root, so the anchor must be ``Path(__file__).parent``.
        """
        recipe = _make_board(tmp_path, spec_text=VALID_SPEC, name="board-anchored")
        elsewhere = tmp_path / "elsewhere"
        elsewhere.mkdir()
        monkeypatch.chdir(elsewhere)

        result = require_spec(recipe, strict=False, max_depth=1)
        assert result.ok is True
        assert result.found_path == recipe.parent / SPEC_FILENAME

    def test_cwd_spec_is_not_used_when_anchor_has_none(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A ``project.kct`` in the *cwd* must not rescue a spec-less board."""
        recipe = _make_board(tmp_path, spec_text=None, name="board-no-spec")
        elsewhere = tmp_path / "cwd-with-spec"
        elsewhere.mkdir()
        (elsewhere / SPEC_FILENAME).write_text(VALID_SPEC)
        monkeypatch.chdir(elsewhere)

        result = require_spec(recipe, strict=False, max_depth=1)
        assert result.ok is False
        assert result.found_path is None

    def test_output_dir_spec_is_not_used(self, tmp_path: Path) -> None:
        """A spec in the out-of-tree output dir must not satisfy the gate."""
        recipe = _make_board(tmp_path, spec_text=None, name="board-no-spec2")
        out_dir = tmp_path / "out-of-tree"
        out_dir.mkdir()
        (out_dir / SPEC_FILENAME).write_text(VALID_SPEC)

        result = require_spec(recipe, strict=False, max_depth=1)
        assert result.ok is False

    def test_finds_spec_in_ancestor(self, tmp_path: Path) -> None:
        """The upward walk finds a spec that only exists in an ancestor."""
        (tmp_path / SPEC_FILENAME).write_text(VALID_SPEC)
        recipe = _make_board(tmp_path, spec_text=None, name="nested")
        result = require_spec(recipe, strict=False, max_depth=4)
        assert result.ok is True
        assert result.found_path == tmp_path / SPEC_FILENAME

    def test_walk_is_bounded_and_terminates_at_root(self) -> None:
        """A huge max_depth must terminate at ``/`` rather than loop forever."""
        found, searched = discover_spec(Path("/"), filename="definitely-absent.kct", max_depth=99)
        assert found is None
        assert len(searched) == 1  # stopped immediately at the filesystem root

    def test_walk_is_depth_bounded(self, tmp_path: Path) -> None:
        deep = tmp_path / "a" / "b" / "c" / "d"
        deep.mkdir(parents=True)
        (tmp_path / SPEC_FILENAME).write_text(VALID_SPEC)
        # depth 2 only reaches ``d`` and ``c`` -- not tmp_path.
        found, searched = discover_spec(deep, max_depth=2)
        assert found is None
        assert len(searched) == 2

    def test_defaults_to_calling_module_file(self, tmp_path: Path) -> None:
        """``require_spec()`` with no anchor uses the caller's ``__file__``."""
        recipe = _make_board(tmp_path, spec_text=VALID_SPEC, name="implicit")
        namespace: dict[str, object] = {
            "__file__": str(recipe),
            "require_spec": require_spec,
        }
        exec("result = require_spec(strict=False, max_depth=1)", namespace)  # noqa: S102
        result = namespace["result"]
        assert isinstance(result, SpecPreconditionResult)
        assert result.ok is True
        assert result.found_path == recipe.parent / SPEC_FILENAME


class TestEscapeHatchAndPosture:
    def test_explicit_spec_path_bypasses_discovery(self, tmp_path: Path) -> None:
        """Consumer-repo escape hatch: point at an artifact anywhere."""
        recipe = _make_board(tmp_path, spec_text=None, name="board-hatch")
        alt = tmp_path / "somewhere" / "custom.kct"
        alt.parent.mkdir()
        alt.write_text(VALID_SPEC)

        assert require_spec(recipe, spec_path=alt, strict=False).ok is True
        assert require_spec(recipe, spec_path=tmp_path / "nope.kct", strict=False).ok is False

    def test_gate_is_read_only(self, tmp_path: Path) -> None:
        """The gate reports; it never repairs. Bytes and mtime are untouched."""
        recipe = _make_board(tmp_path, spec_text=L2_INVALID_SPEC, name="board-ro")
        spec_file = recipe.parent / SPEC_FILENAME
        before = spec_file.read_bytes()
        before_stat = spec_file.stat()
        listing_before = sorted(p.name for p in recipe.parent.iterdir())

        require_spec(recipe, strict=False, max_depth=1)
        with pytest.raises(SystemExit):
            require_spec(recipe, strict=True, max_depth=1)

        assert spec_file.read_bytes() == before
        assert spec_file.stat().st_mtime_ns == before_stat.st_mtime_ns
        assert sorted(p.name for p in recipe.parent.iterdir()) == listing_before

    def test_module_never_writes_specs(self) -> None:
        """Static guard: no write call may appear in the helper.

        Matches on *call* syntax so the module docstring may still name the
        forbidden APIs when explaining why they are forbidden.
        """
        source = (REPO_ROOT / "src" / "kicad_tools" / "recipes" / "precondition.py").read_text()
        for forbidden in (
            "save_spec(",
            "append_decision(",
            ".write_text(",
            ".write_bytes(",
            ".unlink(",
            ".mkdir(",
            "shutil.",
            "subprocess.",
        ):
            assert forbidden not in source, f"precondition helper must not use {forbidden}"
        # ``import save_spec`` would be equally damning.
        assert "import save_spec" not in source
        assert "save_spec," not in source.split('"""', 2)[-1]

    def test_no_l3_unrecognized_key_check(self) -> None:
        """L3 (schema completeness) is explicitly out of scope for #4539.

        PR #4619 makes ``kct spec validate`` report unrecognized keys; gating on
        those would reject 8/8 boards and pre-empt a deferred schema-design
        decision. The helper must say so and must not implement it.
        """
        source = (REPO_ROOT / "src" / "kicad_tools" / "recipes" / "precondition.py").read_text()
        assert "no L3" in source or "There is deliberately no L3" in source
        assert "unrecognized" in source, "the ceiling must be documented in-module"
        # The actual check must not exist.
        assert "model_extra" not in source
        assert "unrecognized_keys" not in source

    def test_internal_error_is_swallowed_not_raised(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bug inside the helper must never crash a recipe -- even in strict mode."""
        import kicad_tools.recipes.precondition as precondition_mod

        def _boom(*args: object, **kwargs: object) -> None:
            raise RuntimeError("simulated helper bug")

        monkeypatch.setattr(precondition_mod, "discover_spec", _boom)
        recipe = _make_board(tmp_path, spec_text=VALID_SPEC, name="board-boom")

        result = precondition_mod.require_spec(recipe, strict=True)  # must NOT raise
        assert result.ok is False
        assert result.checked is False
        err = capsys.readouterr().err
        assert "could not be checked" in err

    def test_env_var_falsy_values_stay_advisory(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        recipe = _make_board(tmp_path, spec_text=None, name="board-falsy")
        for value in ("0", "false", "no", "off", ""):
            monkeypatch.setenv(STRICT_ENV_VAR, value)
            result = require_spec(recipe, max_depth=1)  # must not raise
            assert result.ok is False
            assert result.strict is False

    def test_explicit_strict_false_overrides_env(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv(STRICT_ENV_VAR, "1")
        recipe = _make_board(tmp_path, spec_text=None, name="board-override")
        result = require_spec(recipe, strict=False, max_depth=1)
        assert result.strict is False
        assert result.ok is False

    def test_exported_from_package(self) -> None:
        import kicad_tools.recipes as recipes_pkg

        assert recipes_pkg.require_spec is require_spec
        assert "require_spec" in recipes_pkg.__all__
        assert "SpecPreconditionResult" in recipes_pkg.__all__


class TestNoBoardSpecsModified:
    """#4539 does not repair board content -- it only reports."""

    def test_board_01_spec_still_l2_invalid(self) -> None:
        from kicad_tools.spec.parser import validate_spec

        valid, errors = validate_spec(REPO_ROOT / "boards/01-voltage-divider/project.kct")
        assert valid is False
        assert any("not found in phases" in e for e in errors)


def test_recipes_import_cleanly() -> None:
    """Importing the helper must not touch the filesystem or the environment."""
    env_before = dict(os.environ)
    modules_before = set(sys.modules)
    import kicad_tools.recipes.precondition  # noqa: F401

    assert dict(os.environ) == env_before
    assert "kicad_tools.recipes.precondition" in modules_before | set(sys.modules)
