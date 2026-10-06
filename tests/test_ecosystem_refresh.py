"""Drift-detection unit tests for the ecosystem refresh script (Issue #5839).

Hermetic by construction: ``compare()`` is pure -- it takes a registry entry,
a recorded ``UpstreamFacts`` payload and an explicit date, and returns
findings.  No network, no clock, no ``gh``.  The probe functions that *do*
touch the network are exercised only for their failure handling, with
``subprocess`` and ``urllib`` patched.

Two fixtures encode drift that really happened, so the classifier is tested
against reality rather than invention:

* ``copperhead`` moved from ``chouhanindustries/copperhead`` to
  ``copperheadhq/copperhead`` between our research note and this issue;
* ``Seeed-Studio/kicad-mcp-server`` advertises MIT in its README and commits
  no ``LICENSE`` file.
"""

from __future__ import annotations

import importlib.util
import sys
from datetime import date
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
REFRESH_SCRIPT = REPO_ROOT / "scripts" / "ecosystem_refresh.py"


@pytest.fixture(scope="module")
def refresh():
    """Import ``scripts/ecosystem_refresh.py`` as a module."""
    spec = importlib.util.spec_from_file_location("ecosystem_refresh", REFRESH_SCRIPT)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ecosystem_refresh"] = module
    spec.loader.exec_module(module)
    return module


TODAY = date(2026, 9, 30)


def _project(**overrides):
    """Build a registry entry for the classifier under test."""
    from kicad_tools.ecosystem import EcosystemProject

    base = {
        "project_id": "example",
        "name": "Example",
        "repo_url": "https://github.com/acme/example",
        "vcs": "github",
        "category": "autorouter",
        "relation": "peer",
        "verdict": "complementary",
        "license": "MIT",
        "license_compat": "mit-clean",
        "summary": "An example.",
        "last_verified": "2026-09-30",
        "slug": "acme/example",
        "stars": 10,
        "last_push": "2026-09-30",
    }
    base.update(overrides)
    return EcosystemProject(**base)


def _facts(refresh, **overrides):
    """Build an ``UpstreamFacts`` payload matching ``_project`` by default."""
    base = {
        "license": "MIT",
        "stars": 10,
        "last_push": "2026-09-30",
        "archived": False,
        "full_name": "acme/example",
    }
    base.update(overrides)
    return refresh.UpstreamFacts(**base)


def _classes(findings) -> set[str]:
    return {finding.drift_class for finding in findings}


class TestNoDrift:
    def test_matching_facts_produce_no_findings(self, refresh) -> None:
        assert refresh.compare(_project(), _facts(refresh), TODAY) == []


class TestErrorClasses:
    """The three classes that invalidate a verdict or a link."""

    def test_license_change_is_an_error(self, refresh) -> None:
        findings = refresh.compare(_project(), _facts(refresh, license="AGPL-3.0"), TODAY)
        (finding,) = [f for f in findings if f.drift_class == "license-changed"]
        assert finding.severity == "error"
        assert "AGPL-3.0" in finding.detail
        assert 'license = "AGPL-3.0"' in finding.suggested_toml
        # The operative consequence must be named, not just the id change.
        assert "license_compat" in finding.detail

    def test_rename_is_an_error_and_suggests_both_fields(self, refresh) -> None:
        """The real copperhead move: chouhanindustries -> copperheadhq."""
        project = _project(
            project_id="copperhead",
            slug="chouhanindustries/copperhead",
            repo_url="https://github.com/chouhanindustries/copperhead",
            license="Apache-2.0",
            license_compat="permissive-ideas-only",
        )
        facts = _facts(refresh, license="Apache-2.0", full_name="copperheadhq/copperhead")
        findings = refresh.compare(project, facts, TODAY)
        (finding,) = [f for f in findings if f.drift_class == "renamed"]
        assert finding.severity == "error"
        assert 'slug = "copperheadhq/copperhead"' in finding.suggested_toml
        assert any("repo_url" in line for line in finding.suggested_toml), (
            "a rename rots the URL too, so the suggestion must update both"
        )

    def test_rename_detection_is_case_insensitive(self, refresh) -> None:
        """GitHub is case-insensitive on owner/repo; casing is not a rename."""
        facts = _facts(refresh, full_name="Acme/Example")
        assert "renamed" not in _classes(refresh.compare(_project(), facts, TODAY))

    def test_archived_is_an_error(self, refresh) -> None:
        findings = refresh.compare(_project(), _facts(refresh, archived=True), TODAY)
        (finding,) = [f for f in findings if f.drift_class == "archived"]
        assert finding.severity == "error"


class TestWarnClasses:
    def test_missing_license_upstream_is_a_warning(self, refresh) -> None:
        """The real Seeed-Studio case: README says MIT, no LICENSE file."""
        project = _project(
            project_id="seeed-kicad-mcp",
            slug="Seeed-Studio/kicad-mcp-server",
            license="MIT",
            license_compat="mit-clean",
        )
        facts = _facts(refresh, license="NONE", full_name="Seeed-Studio/kicad-mcp-server")
        findings = refresh.compare(project, facts, TODAY)
        # An MIT -> NONE transition is itself a license change.
        assert "license-changed" in _classes(findings)

    def test_unlicensed_entry_already_marked_is_not_flagged_again(self, refresh) -> None:
        """Once recorded as unlicensed, a missing LICENSE is not news."""
        project = _project(license="NONE", license_compat="unlicensed")
        facts = _facts(refresh, license="NONE")
        assert refresh.compare(project, facts, TODAY) == []

    def test_unlicensed_upstream_with_wrong_compat_is_flagged(self, refresh) -> None:
        project = _project(license="NONE", license_compat="mit-clean")
        facts = _facts(refresh, license="NONE")
        findings = refresh.compare(project, facts, TODAY)
        (finding,) = [f for f in findings if f.drift_class == "license-missing"]
        assert finding.severity == "warn"
        assert 'license_compat = "unlicensed"' in finding.suggested_toml

    def test_reeval_trigger_fires_only_on_error_class_drift(self, refresh) -> None:
        """A star-count delta must not wake a re-evaluation trigger."""
        project = _project(reeval_trigger="Re-check when it tags a release.")

        stale_only = refresh.compare(project, _facts(refresh, stars=99), TODAY)
        assert "stale-facts" in _classes(stale_only)
        assert "reeval-trigger" not in _classes(stale_only), (
            "a noisy warn tier gets ignored, which defeats the warn tier"
        )

        real = refresh.compare(project, _facts(refresh, license="AGPL-3.0"), TODAY)
        assert "reeval-trigger" in _classes(real)


class TestInfoClasses:
    def test_star_and_push_drift_are_info_with_suggestions(self, refresh) -> None:
        facts = _facts(refresh, stars=42, last_push="2026-10-01")
        findings = refresh.compare(_project(), facts, TODAY)
        (finding,) = [f for f in findings if f.drift_class == "stale-facts"]
        assert finding.severity == "info"
        assert "stars = 42" in finding.suggested_toml
        assert 'last_push = "2026-10-01"' in finding.suggested_toml
        assert f'last_verified = "{TODAY.isoformat()}"' in finding.suggested_toml

    def test_old_last_verified_is_reported_even_when_facts_match(self, refresh) -> None:
        project = _project(last_verified="2026-01-01")
        findings = refresh.compare(project, _facts(refresh), TODAY, max_age_days=90)
        (finding,) = [f for f in findings if f.drift_class == "stale-facts"]
        assert "days old" in finding.detail

    def test_fresh_last_verified_is_not_reported(self, refresh) -> None:
        project = _project(last_verified="2026-09-01")
        assert refresh.compare(project, _facts(refresh), TODAY, max_age_days=90) == []

    def test_unpollable_project_is_info_not_failure(self, refresh) -> None:
        """kipy and OmniLayout have no API; that is not an error."""
        project = _project(vcs="other", slug="", repo_url="https://omnieda.com")
        (finding,) = refresh.compare(project, _facts(refresh), TODAY)
        assert finding.drift_class == "unpollable"
        assert finding.severity == "info"

    def test_probe_failure_degrades_to_info(self, refresh) -> None:
        """One unreachable forge must not fail the whole run."""
        facts = refresh.UpstreamFacts(error="gh timed out")
        (finding,) = refresh.compare(_project(), facts, TODAY)
        assert finding.drift_class == "probe-failed"
        assert finding.severity == "info"
        assert "timed out" in finding.detail


class TestProbeFailureHandling:
    """The network-touching functions, with the network removed."""

    def test_github_probe_reports_missing_gh(self, refresh, monkeypatch) -> None:
        def _boom(*_args, **_kwargs):
            raise FileNotFoundError

        monkeypatch.setattr(refresh.subprocess, "run", _boom)
        assert "gh CLI not found" in (refresh.probe_github("acme/example").error or "")

    def test_github_probe_reports_nonzero_exit(self, refresh, monkeypatch) -> None:
        class _Proc:
            returncode = 1
            stdout = ""
            stderr = "HTTP 404: Not Found"

        monkeypatch.setattr(refresh.subprocess, "run", lambda *a, **k: _Proc())
        assert "404" in (refresh.probe_github("acme/nope").error or "")

    def test_github_probe_reports_unparseable_output(self, refresh, monkeypatch) -> None:
        class _Proc:
            returncode = 0
            stdout = "not json"
            stderr = ""

        monkeypatch.setattr(refresh.subprocess, "run", lambda *a, **k: _Proc())
        assert "unparseable" in (refresh.probe_github("acme/example").error or "")

    def test_gitlab_probe_reports_url_error(self, refresh, monkeypatch) -> None:
        import urllib.error

        def _boom(*_args, **_kwargs):
            raise urllib.error.URLError("no route to host")

        monkeypatch.setattr(refresh.urllib.request, "urlopen", _boom)
        assert "gitlab probe failed" in (
            refresh.probe_gitlab("kicad/code/kicad-python").error or ""
        )


class TestGitlabLicenseNormalization:
    """Issue #5843: GitLab license keys must map to correct SPDX casing.

    GitLab returns license keys lowercased (``mit``, ``apache-2.0``); SPDX
    ids are mixed-case for multi-part names (``Apache-2.0``,
    ``BSD-3-Clause``). A bare ``.upper()`` only happened to round-trip for
    single-word/all-caps-prefix ids -- these tests cover the success path
    that the pre-#5843 test suite never exercised.
    """

    @staticmethod
    def _mock_gitlab_response(refresh, monkeypatch, payload: dict) -> None:
        import json as _json

        class _FakeResponse:
            def __enter__(self):
                return self

            def __exit__(self, *_args):
                return False

            def read(self):
                return _json.dumps(payload).encode("utf-8")

        monkeypatch.setattr(refresh.urllib.request, "urlopen", lambda *_a, **_k: _FakeResponse())

    def _facts_for(self, refresh, monkeypatch, license_key: str | None, name: str = "") -> object:
        payload = {
            "license": ({"key": license_key, "name": name} if license_key is not None else None),
            "star_count": 42,
            "last_activity_at": "2026-09-30T12:00:00Z",
            "archived": False,
            "path_with_namespace": "acme/example",
        }
        self._mock_gitlab_response(refresh, monkeypatch, payload)
        return refresh.probe_gitlab("acme/example")

    def test_single_word_license_round_trips(self, refresh, monkeypatch) -> None:
        facts = self._facts_for(refresh, monkeypatch, "mit", "MIT License")
        assert facts.error is None
        assert facts.license == "MIT"

    @pytest.mark.parametrize(
        ("key", "expected"),
        [
            ("apache-2.0", "Apache-2.0"),
            ("bsd-3-clause", "BSD-3-Clause"),
            ("gpl-3.0", "GPL-3.0"),
            ("agpl-3.0", "AGPL-3.0"),
        ],
    )
    def test_multi_part_license_gets_correct_spdx_casing(
        self, refresh, monkeypatch, key, expected
    ) -> None:
        facts = self._facts_for(refresh, monkeypatch, key)
        assert facts.error is None
        assert facts.license == expected
        # The regression this guards: `.upper()` mismatches for a
        # mixed-case multi-part SPDX id.
        if key.upper() != expected:
            assert facts.license != key.upper()

    def test_no_license_reports_none(self, refresh, monkeypatch) -> None:
        facts = self._facts_for(refresh, monkeypatch, None)
        assert facts.error is None
        assert facts.license == "NONE"

    def test_unrecognized_license_key_degrades_to_probe_failed_not_license_changed(
        self, refresh, monkeypatch
    ) -> None:
        """An un-mappable key must not risk a false-positive error-severity
        ``license-changed`` finding -- it degrades to info-severity
        ``probe-failed`` instead (Issue #5843's suggested fallback)."""
        facts = self._facts_for(refresh, monkeypatch, "some-future-license", "Some Future License")
        assert facts.error is not None
        assert "unrecognized license key" in facts.error

        (finding,) = refresh.compare(_project(), facts, TODAY)
        assert finding.drift_class == "probe-failed"
        assert finding.severity == "info"


class TestTimestampNormalization:
    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-09-30T12:31:18Z", "2026-09-30"),
            ("2026-09-30T23:59:59+00:00", "2026-09-30"),
            (None, None),
            ("", None),
            ("not-a-date", None),
        ],
    )
    def test_to_date(self, refresh, raw, expected) -> None:
        assert refresh._to_date(raw) == expected


class TestLicenseDetectorAlias:
    """``upstream_license_detected`` suppresses a known detector mismatch only.

    PCBSchemaGen ships a verbatim MIT file GitHub reports as NOASSERTION, and
    TraceMaker's NOTICE says GPL-3.0-or-later while GitHub reports GPL-3.0.
    Without the alias the weekly drift run raised a false ``license-changed``
    error for each, and suggested overwriting the correct ``license``.
    """

    PCBSCHEMAGEN = {"license": "MIT", "upstream_license_detected": "NOASSERTION"}
    TRACEMAKER = {
        "license": "GPL-3.0-or-later",
        "license_compat": "copyleft-ideas-only",
        "upstream_license_detected": "GPL-3.0",
    }

    @pytest.mark.parametrize(
        ("entry", "detected"),
        [(PCBSCHEMAGEN, "NOASSERTION"), (TRACEMAKER, "GPL-3.0")],
        ids=["pcbschemagen-noassertion", "tracemaker-gpl-3.0"],
    )
    def test_detector_alias_counts_as_matching_license(self, refresh, entry, detected) -> None:
        findings = refresh.compare(_project(**entry), _facts(refresh, license=detected), TODAY)
        assert all(f.severity == "info" for f in findings), findings
        assert "license-changed" not in _classes(findings)
        assert "license-missing" not in _classes(findings)
        assert not any(
            line.startswith("license =") for f in findings for line in f.suggested_toml
        ), "must not suggest overwriting the human-read license"

    def test_alias_does_not_fire_reeval_trigger(self, refresh) -> None:
        project = _project(**self.PCBSCHEMAGEN, reeval_trigger="Re-check if it changes.")
        findings = refresh.compare(project, _facts(refresh, license="NOASSERTION"), TODAY)
        assert "reeval-trigger" not in _classes(findings)

    @pytest.mark.parametrize(
        ("entry", "detected"),
        [(PCBSCHEMAGEN, "AGPL-3.0"), (TRACEMAKER, "MIT"), (PCBSCHEMAGEN, "NONE")],
        ids=["mit-to-agpl", "gpl-to-mit", "license-removed"],
    )
    def test_real_change_is_still_an_error(self, refresh, entry, detected) -> None:
        """A value matching neither ``license`` nor the alias is real drift."""
        findings = refresh.compare(_project(**entry), _facts(refresh, license=detected), TODAY)
        (finding,) = [f for f in findings if f.drift_class == "license-changed"]
        assert finding.severity == "error"
        assert f'license = "{detected}"' in finding.suggested_toml
        assert entry["upstream_license_detected"] in finding.detail

    def test_detector_catching_up_reports_stale_alias_as_info(self, refresh) -> None:
        """Once GitHub classifies the file correctly the alias is dead weight."""
        project = _project(**self.TRACEMAKER)
        findings = refresh.compare(project, _facts(refresh, license="GPL-3.0-or-later"), TODAY)
        assert _classes(findings) == {"license-alias-stale"}
        (finding,) = findings
        assert finding.severity == "info"
        assert "upstream_license_detected" in finding.detail

    @pytest.mark.parametrize("project_id", ["pcbschemagen", "tracemaker"])
    def test_committed_entries_carry_the_alias(self, refresh, project_id) -> None:
        """The two known mismatches are recorded in the registry itself."""
        from kicad_tools.ecosystem import load_registry

        project = load_registry().get(project_id)
        assert project.upstream_license_detected
        findings = refresh.compare(
            project,
            _facts(
                refresh,
                license=project.upstream_license_detected,
                stars=project.stars,
                last_push=project.last_push,
                full_name=project.slug,
            ),
            date.fromisoformat(project.last_verified),
        )
        assert findings == []


class TestSeverityTable:
    def test_every_drift_class_has_a_severity(self, refresh) -> None:
        emitted = {
            "license-changed",
            "renamed",
            "archived",
            "license-missing",
            "reeval-trigger",
            "stale-facts",
            "license-alias-stale",
            "unpollable",
            "probe-failed",
        }
        assert emitted <= set(refresh.SEVERITY)

    def test_severities_are_known_values(self, refresh) -> None:
        assert set(refresh.SEVERITY.values()) <= {"error", "warn", "info"}


class TestRegistryIsPollable:
    """Every registry entry must be pollable or explicitly marked otherwise."""

    def test_github_and_gitlab_entries_carry_a_slug(self) -> None:
        from kicad_tools.ecosystem import load_registry

        for project in load_registry():
            if project.vcs in {"github", "gitlab"}:
                assert project.is_pollable, f"{project.project_id} cannot be polled"
            else:
                assert not project.is_pollable
