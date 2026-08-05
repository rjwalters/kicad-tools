"""Cross-consumer parity for net-class-map sidecar resolution (Issue #4634).

Four independent consumers probe for a board's net-class-map sidecar:

1. ``kicad_tools.cli.check_cmd._discover_net_class_map_sidecar`` --
   ``kct check``'s auto-discovery.
2. ``scripts/ci/net_class_map_resolver.resolve_net_class_map_sidecar`` --
   the routed-DRC CI merge gate.
3. ``kicad_tools.report.net_class_map.resolve_committed_net_class_map`` --
   the ``kct export`` report surface.
4. ``scripts/ci/check_matchgroup_coverage.find_net_class_map_sidecar`` --
   the match-group CI merge gate.

PR #4629 (#4601) taught consumer 1 -- and only consumer 1 -- about the
stem-keyed ``<pcb_stem>.net_class_map.json`` convention.  In a directory
holding both that file and the bare ``net_class_map.json``, ``kct check``
therefore loaded a *different* rule set than the two CI gates that guard
merge, with no warning on either side.

These tests pin the consolidation:

* **AC2/AC3** -- all four agree on the filename set and on stem-keyed
  beating bare within a directory.
* **AC4** -- each consumer's *directory* scope is unchanged.  Consumers
  2/3/4 must still refuse a sidecar that only exists in a directory
  ``kct check`` reaches but they never did; widening them would silently
  change which sidecar a board resolves, which is the exact bug class this
  issue exists to kill.
* **AC5** -- the three in-repo boards that commit a bare sidecar resolve
  byte-identically to before.
* **AC6** -- the shared helper module is a stdlib-only leaf.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.cli.check_cmd import (
    _discover_net_class_map_sidecar,
    _net_class_map_sidecar_candidates,
)
from kicad_tools.report.net_class_map import resolve_committed_net_class_map
from kicad_tools.sidecars import (
    NET_CLASS_MAP_SIDECAR_BASENAME,
    first_existing_net_class_map_sidecar,
    net_class_map_sidecar_candidates,
    net_class_map_sidecar_names,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
CI_DIR = REPO_ROOT / "scripts" / "ci"
SIDECAR_MODULE = REPO_ROOT / "src" / "kicad_tools" / "sidecars.py"

# Boards that commit a bare ``net_class_map.json`` next to their routed PCB
# (AC5 fixtures -- must keep resolving exactly what they resolve today).
COMMITTED_SIDECAR_BOARDS = (
    "03-usb-joystick",
    "06-diffpair-test",
    "07-matchgroup-test",
)


def _load_ci_module(name: str):
    """Import a ``scripts/ci`` script by file path (no package on that path)."""
    path = CI_DIR / f"{name}.py"
    spec = importlib.util.spec_from_file_location(f"{name}_parity_test_mod", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _make_board(root: Path, stem: str = "demo_routed") -> Path:
    """Create ``root/boards/demo/output/<stem>.kicad_pcb`` and return the PCB."""
    out = root / "boards" / "demo" / "output"
    out.mkdir(parents=True, exist_ok=True)
    pcb = out / f"{stem}.kicad_pcb"
    pcb.write_text("(kicad_pcb)")
    return pcb


def _resolve_all_four(pcb: Path) -> dict[str, Path | None]:
    """Resolve the sidecar with every consumer, from the same PCB path."""
    resolver = _load_ci_module("net_class_map_resolver")
    matchgroup = _load_ci_module("check_matchgroup_coverage")
    board_dir = pcb.resolve().parent.parent

    with resolver.resolve_net_class_map_sidecar(pcb) as ci_sidecar:
        ci_resolved = None if ci_sidecar is None else Path(ci_sidecar).resolve()

    return {
        "check_cmd": _maybe_resolve(_discover_net_class_map_sidecar(pcb)),
        "ci_routed_drc": ci_resolved,
        "report": _maybe_resolve(resolve_committed_net_class_map(pcb)),
        "ci_matchgroup": _maybe_resolve(matchgroup.find_net_class_map_sidecar(board_dir, pcb)),
    }


def _maybe_resolve(path: Path | None) -> Path | None:
    return None if path is None else Path(path).resolve()


# ---------------------------------------------------------------------------
# AC2 -- the shared name contract
# ---------------------------------------------------------------------------


class TestSharedNameContract:
    def test_stem_keyed_name_precedes_bare_name(self) -> None:
        assert net_class_map_sidecar_names("board_v24") == [
            "board_v24.net_class_map.json",
            "net_class_map.json",
        ]

    def test_bare_basename_constant(self) -> None:
        assert NET_CLASS_MAP_SIDECAR_BASENAME == "net_class_map.json"

    def test_empty_stem_yields_bare_name_only(self) -> None:
        assert net_class_map_sidecar_names("") == ["net_class_map.json"]

    def test_check_cmd_candidates_delegate_to_the_shared_helper(self, tmp_path: Path) -> None:
        """``check_cmd``'s private alias must be the shared probe, verbatim."""
        pcb = _make_board(tmp_path)
        assert _net_class_map_sidecar_candidates(pcb) == net_class_map_sidecar_candidates(pcb)

    def test_check_cmd_candidate_order(self, tmp_path: Path) -> None:
        """The pre-#4634 probe order is preserved exactly (3 dirs x 2 names)."""
        pcb_dir = tmp_path / "boards" / "demo"
        pcb_dir.mkdir(parents=True)
        pcb = pcb_dir / "demo_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        assert _net_class_map_sidecar_candidates(pcb) == [
            pcb_dir / "demo_routed.net_class_map.json",
            pcb_dir / "net_class_map.json",
            pcb_dir / "output" / "demo_routed.net_class_map.json",
            pcb_dir / "output" / "net_class_map.json",
            pcb_dir.parent / "output" / "demo_routed.net_class_map.json",
            pcb_dir.parent / "output" / "net_class_map.json",
        ]

    def test_check_cmd_candidates_dedup_in_the_board_output_layout(self, tmp_path: Path) -> None:
        """``boards/NN/output/<pcb>`` collapses dir 1 and dir 3, as before."""
        pcb = _make_board(tmp_path)
        pcb_dir = pcb.parent
        assert _net_class_map_sidecar_candidates(pcb) == [
            pcb_dir / "demo_routed.net_class_map.json",
            pcb_dir / "net_class_map.json",
            pcb_dir / "output" / "demo_routed.net_class_map.json",
            pcb_dir / "output" / "net_class_map.json",
        ]


# ---------------------------------------------------------------------------
# AC3 -- all four consumers resolve the SAME file
# ---------------------------------------------------------------------------


class TestCrossConsumerParity:
    def test_all_four_prefer_the_stem_keyed_sidecar(self, tmp_path: Path) -> None:
        """A directory holding BOTH forms must resolve identically everywhere.

        This is the regression test for the divergence: before #4634,
        ``check_cmd`` returned the stem-keyed file while the two CI gates
        and the report surface returned the bare one.  It fails if ANY
        single consumer is reverted to the bare-only probe.
        """
        pcb = _make_board(tmp_path)
        stem_keyed = pcb.parent / "demo_routed.net_class_map.json"
        stem_keyed.write_text(json.dumps({"which": "stem-keyed"}))
        bare = pcb.parent / NET_CLASS_MAP_SIDECAR_BASENAME
        bare.write_text(json.dumps({"which": "bare"}))

        resolved = _resolve_all_four(pcb)

        assert set(resolved.values()) == {stem_keyed.resolve()}, resolved
        for consumer, path in resolved.items():
            assert path is not None
            payload = json.loads(path.read_text())
            assert payload["which"] == "stem-keyed", consumer

    def test_all_four_fall_back_to_the_bare_sidecar(self, tmp_path: Path) -> None:
        """With only the bare name present, all four still agree."""
        pcb = _make_board(tmp_path)
        bare = pcb.parent / NET_CLASS_MAP_SIDECAR_BASENAME
        bare.write_text(json.dumps({"which": "bare"}))

        resolved = _resolve_all_four(pcb)
        assert set(resolved.values()) == {bare.resolve()}, resolved

    def test_all_four_accept_a_stem_keyed_only_directory(self, tmp_path: Path) -> None:
        """The stem-keyed name alone is enough for every consumer."""
        pcb = _make_board(tmp_path)
        stem_keyed = pcb.parent / "demo_routed.net_class_map.json"
        stem_keyed.write_text(json.dumps({"which": "stem-keyed"}))

        resolved = _resolve_all_four(pcb)
        assert set(resolved.values()) == {stem_keyed.resolve()}, resolved

    def test_stem_must_match_exactly_no_unsuffixing(self, tmp_path: Path) -> None:
        """``board_v23.net_class_map.json`` never applies to ``board_v24``.

        No consumer may glob or un-suffix: a foreign-stem sidecar is not a
        candidate at all, so every consumer falls through to the bare name
        (here absent) and reports "no sidecar".
        """
        pcb = _make_board(tmp_path, stem="board_v24")
        foreign = pcb.parent / "board_v23.net_class_map.json"
        foreign.write_text(json.dumps({"which": "v23"}))

        resolved = _resolve_all_four(pcb)
        assert set(resolved.values()) == {None}, resolved

    def test_foreign_stem_falls_through_to_bare_everywhere(self, tmp_path: Path) -> None:
        """A foreign-stem sidecar loses to the bare name for all four."""
        pcb = _make_board(tmp_path, stem="board_v24")
        (pcb.parent / "board_v23.net_class_map.json").write_text(json.dumps({"which": "v23"}))
        bare = pcb.parent / NET_CLASS_MAP_SIDECAR_BASENAME
        bare.write_text(json.dumps({"which": "bare"}))

        resolved = _resolve_all_four(pcb)
        assert set(resolved.values()) == {bare.resolve()}, resolved

    def test_routed_suffix_is_not_stripped(self, tmp_path: Path) -> None:
        """``demo_routed.kicad_pcb`` does not look for ``demo.net_class_map.json``."""
        pcb = _make_board(tmp_path, stem="demo_routed")
        (pcb.parent / "demo.net_class_map.json").write_text(json.dumps({"which": "unsuffixed"}))

        resolved = _resolve_all_four(pcb)
        assert set(resolved.values()) == {None}, resolved


# ---------------------------------------------------------------------------
# AC4 -- directory scope is NOT widened
# ---------------------------------------------------------------------------


class TestDirectoryScopeUnchanged:
    def test_only_check_cmd_reaches_the_nested_output_dir(self, tmp_path: Path) -> None:
        """A sidecar in ``pcb_dir/output`` is ``kct check``-only, as before.

        Consumers 2/3 search ``pcb_dir`` only.  If this test starts
        passing for them, their directory scope was silently widened and
        boards would begin resolving sidecars they do not resolve today.
        """
        pcb = tmp_path / "demo_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")
        nested = tmp_path / "output"
        nested.mkdir()
        sidecar = nested / NET_CLASS_MAP_SIDECAR_BASENAME
        sidecar.write_text(json.dumps({"which": "nested"}))

        resolver = _load_ci_module("net_class_map_resolver")

        assert _maybe_resolve(_discover_net_class_map_sidecar(pcb)) == sidecar.resolve()
        assert resolve_committed_net_class_map(pcb) is None
        with resolver.resolve_net_class_map_sidecar(pcb) as ci_sidecar:
            # No committed sidecar in ``pcb_dir`` and no importable board
            # recipe -> the resolver degrades to "no map", as it did before.
            assert ci_sidecar is None

    def test_matchgroup_gate_searches_only_board_output(self, tmp_path: Path) -> None:
        """Consumer 4 stays pinned to ``board_dir/output``."""
        matchgroup = _load_ci_module("check_matchgroup_coverage")
        board_dir = tmp_path / "boards" / "demo"
        out = board_dir / "output"
        out.mkdir(parents=True)
        pcb = out / "demo_routed.kicad_pcb"
        pcb.write_text("(kicad_pcb)")

        # A sidecar in the BOARD dir (not board_dir/output) must be ignored.
        (board_dir / NET_CLASS_MAP_SIDECAR_BASENAME).write_text("{}")
        assert matchgroup.find_net_class_map_sidecar(board_dir, pcb) is None

        # ...and one in board_dir/output must be found.
        found = out / NET_CLASS_MAP_SIDECAR_BASENAME
        found.write_text("{}")
        assert _maybe_resolve(matchgroup.find_net_class_map_sidecar(board_dir, pcb)) == (
            found.resolve()
        )

    def test_first_existing_helper_honours_caller_directory_order(self, tmp_path: Path) -> None:
        """Nearer directories win; the helper never invents extra ones."""
        near = tmp_path / "near"
        far = tmp_path / "far"
        near.mkdir()
        far.mkdir()
        (far / NET_CLASS_MAP_SIDECAR_BASENAME).write_text("{}")

        assert first_existing_net_class_map_sidecar([near], "demo") is None
        assert first_existing_net_class_map_sidecar([near, far], "demo") == (
            far / NET_CLASS_MAP_SIDECAR_BASENAME
        )

    def test_first_existing_helper_prefers_stem_keyed_in_the_nearest_dir(
        self, tmp_path: Path
    ) -> None:
        near = tmp_path / "near"
        near.mkdir()
        (near / NET_CLASS_MAP_SIDECAR_BASENAME).write_text("{}")
        stem_keyed = near / "demo.net_class_map.json"
        stem_keyed.write_text("{}")

        assert first_existing_net_class_map_sidecar([near], "demo") == stem_keyed


# ---------------------------------------------------------------------------
# AC5 -- byte-identical resolution on the in-repo boards
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("board_name", COMMITTED_SIDECAR_BOARDS)
class TestInRepoBoardParity:
    def test_all_four_resolve_the_committed_bare_sidecar(self, board_name: str) -> None:
        board_dir = REPO_ROOT / "boards" / board_name
        out = board_dir / "output"
        expected = out / NET_CLASS_MAP_SIDECAR_BASENAME
        if not expected.is_file():
            pytest.skip(f"{board_name} has no committed sidecar in this checkout")

        pcbs = sorted(out.glob("*_routed.kicad_pcb"))
        assert pcbs, f"{board_name} has no routed PCB artifact"
        pcb = pcbs[0]

        resolved = _resolve_all_four(pcb)
        assert set(resolved.values()) == {expected.resolve()}, resolved

    def test_no_stem_keyed_sidecar_shadows_the_committed_one(self, board_name: str) -> None:
        """The in-repo boards commit bare names only (exposure is zero)."""
        out = REPO_ROOT / "boards" / board_name / "output"
        stem_keyed = [
            p
            for p in out.glob(f"*.{NET_CLASS_MAP_SIDECAR_BASENAME}")
            if p.name != NET_CLASS_MAP_SIDECAR_BASENAME
        ]
        assert stem_keyed == [], f"unexpected stem-keyed sidecar in {out}: {stem_keyed}"


# ---------------------------------------------------------------------------
# AC6 -- the helper is a stdlib-only leaf
# ---------------------------------------------------------------------------


class TestHelperImportHygiene:
    def test_module_level_imports_are_stdlib_only(self) -> None:
        """Static check: ``sidecars.py`` imports nothing outside the stdlib."""
        tree = ast.parse(SIDECAR_MODULE.read_text())
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom):
                if node.level:  # a relative import is by definition not stdlib
                    roots.add(f"<relative level {node.level}>")
                elif node.module:
                    roots.add(node.module.split(".")[0])

        non_stdlib = roots - set(sys.stdlib_module_names)
        assert non_stdlib == set(), f"non-stdlib imports in sidecars.py: {non_stdlib}"

    def test_module_imports_in_isolation_without_the_package(self) -> None:
        """Loading the file directly pulls in no kicad_tools / jinja2 modules.

        This is the substantive AC6 property: the helper itself adds no
        dependency.  Note it is deliberately NOT phrased as
        ``import kicad_tools.sidecars`` -- see
        ``test_package_import_pulls_no_optional_report_extra`` below.
        """
        script = (
            "import importlib.util, sys\n"
            f"spec = importlib.util.spec_from_file_location('_sidecars_iso', r'{SIDECAR_MODULE}')\n"
            "mod = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(mod)\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] in "
            "{'kicad_tools', 'jinja2', 'yaml', 'numpy'})\n"
            "assert mod.NET_CLASS_MAP_SIDECAR_BASENAME == 'net_class_map.json'\n"
            "print(bad)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
        )
        assert proc.stdout.strip() == "[]", proc.stdout

    def test_package_import_pulls_no_optional_report_extra(self) -> None:
        """``import kicad_tools.sidecars`` must not require jinja2.

        The eager ``kicad_tools/__init__.py`` means importing ANY
        ``kicad_tools`` submodule also imports ``kicad_tools.router`` --
        that is pre-existing and identical for all four consumers, which
        already import from the package.  What matters here is the
        constraint that motivated putting the helper OUTSIDE
        ``kicad_tools.report``: resolving a filename must never pull the
        optional ``[report]`` rendering extra.
        """
        script = (
            "import sys\n"
            "import kicad_tools.sidecars\n"
            "bad = sorted(m for m in sys.modules if m.split('.')[0] == 'jinja2' "
            "or m.startswith('kicad_tools.report'))\n"
            "print(bad)\n"
        )
        proc = subprocess.run(
            [sys.executable, "-c", script],
            capture_output=True,
            text=True,
            check=True,
            cwd=REPO_ROOT,
        )
        assert proc.stdout.strip() == "[]", proc.stdout

    def test_helper_does_not_import_from_scripts(self) -> None:
        """``src/`` must never depend on ``scripts/`` (excluded from the wheel)."""
        source = SIDECAR_MODULE.read_text()
        assert "import scripts" not in source
        assert "from scripts" not in source
