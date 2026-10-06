"""Tests for the ecosystem registry loader and CLI (Issue #5839).

Covers the controlled-vocabulary validation (the reason the registry is TOML
with a loader rather than a markdown list), the packaging contract that keeps
``kct ecosystem`` working from an installed wheel, the committed registry's
own integrity, and the CLI/MCP surfaces.

The drift-detection unit tests live in ``test_ecosystem_refresh.py``; the
README render gate lives in ``test_ecosystem_readme_block.py``.
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest

from kicad_tools.ecosystem import (
    CATEGORIES,
    CATEGORY_HEADINGS,
    LICENSE_COMPAT,
    REGISTRY_PATH,
    RELATIONS,
    VERDICTS,
    EcosystemProject,
    RegistryError,
    load_registry,
    validate_research_docs,
)

REPO_ROOT = Path(__file__).resolve().parent.parent

MINIMAL = """
[projects.example]
name = "Example"
slug = "acme/example"
repo_url = "https://github.com/acme/example"
vcs = "github"
category = "autorouter"
relation = "peer"
verdict = "complementary"
license = "MIT"
license_compat = "mit-clean"
summary = "An example project."
last_verified = "2026-09-30"

[positioning]
invariants = ["We are offline."]
non_goals = ["We do not ship a GUI: someone else does."]
"""


@pytest.fixture(scope="module")
def registry():
    """The committed registry, loaded once per module."""
    return load_registry()


def _write(tmp_path: Path, text: str) -> Path:
    path = tmp_path / "projects.toml"
    path.write_text(text, encoding="utf-8")
    return path


class TestValidation:
    """A typo in the data file must be a loud error, not a silent omission."""

    def test_minimal_registry_loads(self, tmp_path: Path) -> None:
        registry = load_registry(_write(tmp_path, MINIMAL))
        assert registry.ids == ["example"]
        assert registry.get("example").name == "Example"
        assert registry.positioning.invariants == ("We are offline.",)

    @pytest.mark.parametrize(
        ("field_name", "bad_value"),
        [
            ("category", "router"),
            ("relation", "sibling"),
            ("verdict", "good"),
            ("license_compat", "permissive"),
            ("vcs", "svn"),
        ],
    )
    def test_out_of_vocabulary_value_names_project_and_field(
        self, tmp_path: Path, field_name: str, bad_value: str
    ) -> None:
        text = MINIMAL.replace(
            f'{field_name} = "{_current(field_name)}"', f'{field_name} = "{bad_value}"'
        )
        with pytest.raises(RegistryError) as excinfo:
            load_registry(_write(tmp_path, text))
        message = str(excinfo.value)
        assert "example" in message, "error must name the offending project id"
        assert field_name in message, "error must name the offending field"

    def test_unknown_field_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace('name = "Example"', 'name = "Example"\nfavourite = "yes"')
        with pytest.raises(RegistryError, match="unknown field"):
            load_registry(_write(tmp_path, text))

    def test_missing_required_field_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace('summary = "An example project."\n', "")
        with pytest.raises(RegistryError, match="missing required field 'summary'"):
            load_registry(_write(tmp_path, text))

    @pytest.mark.parametrize(
        "bad_sha",
        ["main", "v1.0", "64df3f5", "64df3f582e8a862c9289205b5a608466bf21a7b"],
    )
    def test_pinned_commit_must_be_a_full_sha(self, tmp_path: Path, bad_sha: str) -> None:
        """A branch or tag pin makes a recorded measurement unreproducible."""
        text = MINIMAL.replace('license = "MIT"', f'license = "MIT"\npinned_commit = "{bad_sha}"')
        with pytest.raises(RegistryError, match="pinned_commit"):
            load_registry(_write(tmp_path, text))

    def test_full_sha_is_accepted(self, tmp_path: Path) -> None:
        sha = "64df3f582e8a862c9289205b5a608466bf21a7ba"
        text = MINIMAL.replace('license = "MIT"', f'license = "MIT"\npinned_commit = "{sha}"')
        assert load_registry(_write(tmp_path, text)).get("example").pinned_commit == sha

    def test_github_entry_requires_a_slug(self, tmp_path: Path) -> None:
        text = MINIMAL.replace('slug = "acme/example"\n', "")
        with pytest.raises(RegistryError, match="requires a slug"):
            load_registry(_write(tmp_path, text))

    def test_bad_date_is_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace('last_verified = "2026-09-30"', 'last_verified = "Sept 2026"')
        with pytest.raises(RegistryError, match="last_verified"):
            load_registry(_write(tmp_path, text))

    def test_negative_stars_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace('license = "MIT"', 'license = "MIT"\nstars = -1')
        with pytest.raises(RegistryError, match="stars"):
            load_registry(_write(tmp_path, text))

    def test_bad_project_id_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.replace("[projects.example]", "[projects.Example_Project]")
        with pytest.raises(RegistryError, match="must be lowercase"):
            load_registry(_write(tmp_path, text))

    def test_missing_positioning_table_rejected(self, tmp_path: Path) -> None:
        text = MINIMAL.split("[positioning]")[0]
        with pytest.raises(RegistryError, match="positioning"):
            load_registry(_write(tmp_path, text))

    @pytest.mark.parametrize(
        ("license_id", "detected"),
        [("MIT", "NOASSERTION"), ("GPL-3.0-or-later", "GPL-3.0")],
        ids=["pcbschemagen", "tracemaker"],
    )
    def test_upstream_license_detected_is_accepted(
        self, tmp_path: Path, license_id: str, detected: str
    ) -> None:
        text = MINIMAL.replace(
            'license = "MIT"',
            f'license = "{license_id}"\nupstream_license_detected = "{detected}"',
        )
        project = load_registry(_write(tmp_path, text)).get("example")
        assert project.license == license_id
        assert project.upstream_license_detected == detected

    def test_upstream_license_detected_defaults_to_empty(self, tmp_path: Path) -> None:
        assert (
            load_registry(_write(tmp_path, MINIMAL)).get("example").upstream_license_detected == ""
        )

    @pytest.mark.parametrize("bad", ["MIT License", "GPL 3", "-MIT", "MIT/Apache"])
    def test_upstream_license_detected_must_be_spdx_ish(self, tmp_path: Path, bad: str) -> None:
        text = MINIMAL.replace(
            'license = "MIT"', f'license = "MIT"\nupstream_license_detected = "{bad}"'
        )
        with pytest.raises(RegistryError, match="upstream_license_detected"):
            load_registry(_write(tmp_path, text))

    def test_upstream_license_detected_must_differ_from_license(self, tmp_path: Path) -> None:
        """An alias equal to ``license`` is a no-op that hides intent."""
        text = MINIMAL.replace(
            'license = "MIT"', 'license = "MIT"\nupstream_license_detected = "MIT"'
        )
        with pytest.raises(RegistryError, match="equals license"):
            load_registry(_write(tmp_path, text))

    def test_upstream_license_detected_must_be_a_string(self, tmp_path: Path) -> None:
        text = MINIMAL.replace('license = "MIT"', 'license = "MIT"\nupstream_license_detected = 3')
        with pytest.raises(RegistryError, match="upstream_license_detected"):
            load_registry(_write(tmp_path, text))

    def test_missing_file_names_the_path(self, tmp_path: Path) -> None:
        with pytest.raises(RegistryError, match="not found"):
            load_registry(tmp_path / "absent.toml")

    def test_invalid_toml_is_reported_as_such(self, tmp_path: Path) -> None:
        with pytest.raises(RegistryError, match="invalid TOML"):
            load_registry(_write(tmp_path, "[projects.example\nname = "))


def _current(field_name: str) -> str:
    """The valid value MINIMAL carries for ``field_name``."""
    return {
        "category": "autorouter",
        "relation": "peer",
        "verdict": "complementary",
        "license_compat": "mit-clean",
        "vcs": "github",
    }[field_name]


class TestPackaging:
    """``kct ecosystem`` must work from a wheel, not only from a checkout."""

    def test_registry_path_is_module_relative(self) -> None:
        """Resolved against the module, so CWD cannot change the answer."""
        assert REGISTRY_PATH.is_file()
        assert REGISTRY_PATH.parent.name == "data"
        assert REGISTRY_PATH.parent.parent.name == "ecosystem"

    def test_registry_lives_inside_the_package(self) -> None:
        """A top-level ecosystem/ dir would be absent from the wheel.

        ``pyproject.toml`` sets ``packages = ["src/kicad_tools"]``, so
        hatchling ships only package-internal files.
        """
        import kicad_tools

        package_root = Path(kicad_tools.__file__).resolve().parent
        assert package_root in REGISTRY_PATH.resolve().parents

    def test_wheel_target_ships_the_package_tree(self) -> None:
        """The hatchling target that makes the registry reachable in a wheel.

        Verified by building one: ``uv build --wheel`` puts
        ``kicad_tools/ecosystem/data/projects.toml`` inside the archive. This
        test asserts the config that guarantees it, without paying for a
        build on every run.
        """
        import sys as _sys

        if _sys.version_info >= (3, 11):
            import tomllib
        else:  # pragma: no cover
            import tomli as tomllib  # type: ignore[import-not-found]

        with (REPO_ROOT / "pyproject.toml").open("rb") as handle:
            config = tomllib.load(handle)

        packages = config["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]
        assert packages == ["src/kicad_tools"], (
            "the ecosystem registry is packaged by virtue of living under "
            "src/kicad_tools; changing this target would drop it from the wheel"
        )

    def test_loads_from_a_foreign_cwd(self, tmp_path: Path, monkeypatch) -> None:
        monkeypatch.chdir(tmp_path)
        assert len(load_registry()) > 0


class TestCommittedRegistry:
    """Integrity of the registry this repo actually ships."""

    def test_every_research_doc_exists(self, registry) -> None:
        """A renamed research note must not rot silently."""
        assert validate_research_docs(registry, REPO_ROOT) == []

    def test_ids_are_unique(self, registry) -> None:
        assert len(registry.ids) == len(set(registry.ids))

    def test_every_vocabulary_value_is_known(self, registry) -> None:
        for project in registry:
            assert project.category in CATEGORIES
            assert project.relation in RELATIONS
            assert project.verdict in VERDICTS
            assert project.license_compat in LICENSE_COMPAT

    def test_unlicensed_projects_are_not_marked_reusable(self, registry) -> None:
        """A README claiming MIT is not a license (Seeed-Studio case)."""
        for project in registry:
            if project.license == "NONE":
                assert not project.code_reuse_allowed, (
                    f"{project.project_id} commits no LICENSE but is marked "
                    f"{project.license_compat!r}"
                )

    def test_copyleft_projects_forbid_code_reuse(self, registry) -> None:
        """AGPL/GPL neighbours are capability-level only, in both directions."""
        for project in registry:
            if project.license.startswith(("AGPL", "GPL")):
                assert project.license_compat == "copyleft-ideas-only", (
                    f"{project.project_id} is {project.license} but marked "
                    f"{project.license_compat!r}"
                )
                assert not project.code_reuse_allowed

    def test_watch_entries_explain_themselves(self, registry) -> None:
        """A 'watch' verdict must say what would change our mind."""
        for project in registry.filter(verdict="watch"):
            assert project.reeval_trigger, (
                f"{project.project_id} is verdict=watch with no reeval_trigger; "
                "record what would promote it"
            )

    def test_benchmarked_entries_pin_a_commit(self, registry) -> None:
        """A measurement against a moving target is not a measurement."""
        for project in registry.filter(verdict="benchmarked"):
            assert project.pinned_commit, (
                f"{project.project_id} is verdict=benchmarked without a "
                "pinned_commit; the measurement is unreproducible"
            )

    def test_evaluated_entries_cite_a_note(self, registry) -> None:
        """'We evaluated it' requires a written evaluation."""
        for verdict in ("benchmarked", "evaluated-not-adopted", "ideas-adopted"):
            for project in registry.filter(verdict=verdict):
                assert project.research_docs, (
                    f"{project.project_id} claims verdict={verdict} with no "
                    "research_docs; cite the note or change the verdict"
                )

    def test_krt_summary_states_where_it_beats_us(self, registry) -> None:
        """The honesty rule, enforced on the one entry it matters most for.

        docs/research/kicad-routing-tools-comparison.md measured KRT as faster
        and more complete than `kct route` on the sparse and medium boards. A
        summary that quietly dropped that would make the registry a marketing
        document.
        """
        summary = registry.get("kicadroutingtools").summary.lower()
        assert "faster" in summary
        assert "more nets" in summary


class TestFiltering:
    """Filters are how an agent narrows the registry."""

    def test_filters_compose(self, registry) -> None:
        upstream = registry.filter(relation="upstream")
        assert upstream, "expected at least one upstream producer"
        both = registry.filter(relation="upstream", category="design-as-code")
        assert {p.project_id for p in both} <= {p.project_id for p in upstream}

    def test_filter_preserves_file_order(self, registry) -> None:
        """Order is editorial: the closest peer leads its section."""
        autorouters = [p.project_id for p in registry.filter(category="autorouter")]
        assert autorouters[0] == "kicadroutingtools"

    def test_unknown_id_lists_valid_ids(self, registry) -> None:
        with pytest.raises(KeyError) as excinfo:
            registry.get("nope")
        assert "kicadroutingtools" in str(excinfo.value)

    def test_to_dict_is_json_serializable(self, registry) -> None:
        json.dumps(registry.to_dict())


class TestCategoryHeadings:
    """Issue #5843: an unmapped category must fail loudly, not vanish.

    ``CATEGORY_HEADINGS`` (``src/kicad_tools/ecosystem/models.py``) is the
    single source both ``kct ecosystem list`` and
    ``scripts/ecosystem_render.py``'s README renderer import -- before this
    issue each kept its own independently-maintained copy, and only the
    renderer guarded against a category with no heading.
    """

    def test_every_category_has_a_heading(self) -> None:
        """A category added to the vocabulary without a heading must fail
        this test rather than silently disappearing from a listing."""
        assert set(CATEGORY_HEADINGS) == CATEGORIES

    def test_cli_list_guards_against_an_unmapped_category(self, monkeypatch, registry) -> None:
        """Reproduces the issue's own repro: drop one mapped category's
        heading and confirm the CLI now fails loudly instead of printing
        fewer projects than the trailing count claims."""
        import kicad_tools.ecosystem as ecosystem_pkg
        from kicad_tools.cli.commands.ecosystem import _run_list

        incomplete = dict(CATEGORY_HEADINGS)
        incomplete.pop("autorouter")
        monkeypatch.setattr(ecosystem_pkg, "CATEGORY_HEADINGS", incomplete)

        class _Args:
            ecosystem_category = None
            ecosystem_relation = None
            ecosystem_verdict = None
            ecosystem_license_compat = None

        assert _run_list(_Args(), registry, "text") != 0


class TestCli:
    """The `kct ecosystem` surface."""

    def _run(self, *args: str) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, "-m", "kicad_tools.cli", "ecosystem", *args],
            capture_output=True,
            text=True,
            cwd=REPO_ROOT,
            check=False,
        )

    def test_list_json_is_a_single_document(self) -> None:
        result = self._run("list", "--format", "json")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["count"] == len(load_registry())

    def test_list_filter_narrows(self) -> None:
        result = self._run("list", "--relation", "upstream", "--format", "json")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["filters"] == {"relation": "upstream"}
        assert all(p["relation"] == "upstream" for p in payload["projects"])

    def test_bad_filter_value_exits_nonzero_with_json(self) -> None:
        result = self._run("list", "--relation", "cousin", "--format", "json")
        assert result.returncode == 1
        assert "error" in json.loads(result.stdout)

    def test_show_json(self) -> None:
        result = self._run("show", "konnect", "--format", "json")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["license"] == "AGPL-3.0"
        assert payload["code_reuse_allowed"] is False

    def test_show_unknown_id_exits_nonzero_but_emits_valid_json(self) -> None:
        result = self._run("show", "definitely-not-a-project", "--format", "json")
        assert result.returncode == 1
        payload = json.loads(result.stdout)
        assert "valid ids" in payload["error"]

    def test_where_we_sit_json(self) -> None:
        result = self._run("where-we-sit", "--format", "json")
        assert result.returncode == 0, result.stderr
        payload = json.loads(result.stdout)
        assert payload["invariants"]
        assert payload["non_goals"]
        assert payload["docs"] == "docs/ecosystem.md"

    def test_text_output_runs(self) -> None:
        for args in (("list",), ("show", "atopile"), ("where-we-sit",)):
            result = self._run(*args)
            assert result.returncode == 0, result.stderr
            assert result.stdout.strip()

    def test_no_subcommand_prints_usage(self) -> None:
        result = self._run()
        assert result.returncode == 1
        assert "Usage" in result.stdout


class TestMcpTools:
    """The MCP surface must agree with the CLI."""

    def test_tools_are_registered(self) -> None:
        from kicad_tools.mcp.tools.registry import get_tool

        for name in ("ecosystem_list", "ecosystem_show"):
            assert get_tool(name).category == "ecosystem"

    def test_list_matches_registry(self) -> None:
        from kicad_tools.mcp.tools.ecosystem import ecosystem_list

        payload = ecosystem_list()
        assert payload["count"] == len(load_registry())
        assert "positioning" in payload

    def test_show_matches_cli_payload(self) -> None:
        from kicad_tools.mcp.tools.ecosystem import ecosystem_show

        assert ecosystem_show("kibot") == load_registry().get("kibot").to_dict()

    def test_bad_filter_returns_error_not_exception(self) -> None:
        from kicad_tools.mcp.tools.ecosystem import ecosystem_list

        assert "error" in ecosystem_list(category="nonsense")

    def test_unknown_id_returns_error(self) -> None:
        from kicad_tools.mcp.tools.ecosystem import ecosystem_show

        assert "error" in ecosystem_show("nope")


@pytest.mark.parametrize("project_id", ["kicadroutingtools", "pcbschemagen"])
def test_project_to_dict_round_trips_through_from_toml(project_id: str) -> None:
    """``to_dict`` must not silently drop a field ``from_toml`` accepts.

    ``pcbschemagen`` covers ``upstream_license_detected``.
    """
    registry = load_registry()
    project = registry.get(project_id)
    as_dict = project.to_dict()
    # Derived and renamed keys are not inputs.
    for derived in ("project_id", "code_reuse_allowed"):
        as_dict.pop(derived)
    rebuilt = EcosystemProject.from_toml(project.project_id, as_dict)
    assert rebuilt == project
