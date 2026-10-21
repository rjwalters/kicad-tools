"""Guard tests: LCSC/EasyEDA STEP bodies are never committed or shipped (#7686).

Operator ruling Q44: EasyEDA/LCSC 3D models are per-user, opt-in, fetched at
use time only, and never committed to the repo, shipped in a package or
embedded in anything published.  Fixture-only: no network, and the user's real
LCSC cache is read (never written) purely as an extra local safety net.
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from unittest import mock

import pytest

from kicad_tools.pcb import lcsc_models
from kicad_tools.pcb.lcsc_guard import (
    find_lcsc_step_violations,
    lcsc_cache_digests,
    tracked_step_files,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
FAKE_STEP = b"ISO-10303-21;\r\nHEADER;\r\n/* lcsc body */\r\nEND-ISO-10303-21;\r\n"
FAKE_INFO = json.dumps(
    {
        "result": {
            "packageDetail": {
                "dataStr": {"shape": ["SVGNODE~" + json.dumps({"attrs": {"uuid": "abc123"}})]}
            }
        }
    }
).encode()


@pytest.fixture
def lcsc_cache(tmp_path, monkeypatch):
    """A fixture LCSC cache populated through the real (mocked-network) fetch path."""
    monkeypatch.setenv(lcsc_models.FETCH_INTERVAL_ENV_VAR, "0")
    lcsc_models.reset_fetch_budget()
    cache = tmp_path / "cache"

    def fake_get(url):
        return FAKE_INFO if "components" in url else FAKE_STEP

    with mock.patch.object(lcsc_models, "_http_get", fake_get):
        path = lcsc_models.resolve_lcsc_step("C12345", cache_dir=cache, fetch=True)
    assert path is not None
    yield cache
    lcsc_models.reset_fetch_budget()


class TestGuardDetection:
    def test_cache_entry_has_source_lcsc_metadata(self, lcsc_cache):
        meta = json.loads((lcsc_cache / "C12345.meta.json").read_text())
        assert meta["source"] == "lcsc"
        assert meta["model_uuid"] == "abc123"
        assert len(meta["sha256"]) == 64

    def test_copy_of_cached_body_under_boards_is_flagged(self, lcsc_cache, tmp_path):
        digests = lcsc_cache_digests(lcsc_cache)
        leaked = tmp_path / "boards" / "99-x" / "models" / "part.step"
        leaked.parent.mkdir(parents=True)
        leaked.write_bytes(FAKE_STEP)
        violations = find_lcsc_step_violations([leaked], digests)
        assert len(violations) == 1
        assert "sha256 matches LCSC cache entry" in violations[0]

    def test_stp_suffix_is_flagged_too(self, lcsc_cache, tmp_path):
        leaked = tmp_path / "src" / "pkg" / "data" / "part.stp"
        leaked.parent.mkdir(parents=True)
        leaked.write_bytes(FAKE_STEP)
        assert find_lcsc_step_violations([leaked], lcsc_cache_digests(lcsc_cache))

    def test_replaced_cache_body_is_still_caught_by_metadata_digest(self, lcsc_cache, tmp_path):
        (lcsc_cache / "C12345.step").write_bytes(FAKE_STEP + b"tampered")
        leaked = tmp_path / "boards" / "part.step"
        leaked.parent.mkdir(parents=True)
        leaked.write_bytes(FAKE_STEP)  # the originally fetched bytes
        assert find_lcsc_step_violations([leaked], lcsc_cache_digests(lcsc_cache))

    def test_source_lcsc_sibling_metadata_is_flagged_without_cache(self, tmp_path):
        step = tmp_path / "boards" / "x" / "body.step"
        step.parent.mkdir(parents=True)
        step.write_bytes(b"ISO-10303-21; something else entirely")
        (step.parent / "body.meta.json").write_text(json.dumps({"source": "lcsc"}))
        violations = find_lcsc_step_violations([step], {})
        assert violations and "source=lcsc" in violations[0]

    def test_unrelated_step_is_not_flagged(self, lcsc_cache, tmp_path):
        other = tmp_path / "boards" / "own.step"
        other.parent.mkdir(parents=True)
        other.write_bytes(b"ISO-10303-21; self-generated")
        assert find_lcsc_step_violations([other], lcsc_cache_digests(lcsc_cache)) == []

    def test_non_step_files_are_ignored(self, lcsc_cache, tmp_path):
        f = tmp_path / "boards" / "readme.txt"
        f.parent.mkdir(parents=True)
        f.write_bytes(FAKE_STEP)
        assert find_lcsc_step_violations([f], lcsc_cache_digests(lcsc_cache)) == []

    def test_staged_lcsc_step_is_found_via_git_index(self, lcsc_cache, tmp_path):
        repo = tmp_path / "repo"
        (repo / "boards").mkdir(parents=True)
        subprocess.run(["git", "init", "-q", str(repo)], check=True)
        leaked = repo / "boards" / "model.step"
        leaked.write_bytes(FAKE_STEP)
        subprocess.run(["git", "-C", str(repo), "add", "boards/model.step"], check=True)
        tracked = tracked_step_files(repo)
        assert tracked == [leaked]
        assert find_lcsc_step_violations(tracked, lcsc_cache_digests(lcsc_cache))


class TestRepoIsClean:
    def test_no_tracked_step_matches_fixture_or_user_cache(self, lcsc_cache):
        """Passes on main: no LCSC body is tracked under boards/ or src/."""
        digests = lcsc_cache_digests(lcsc_cache)
        digests.update(lcsc_cache_digests())  # the developer's real cache, if any
        assert find_lcsc_step_violations(tracked_step_files(REPO_ROOT), digests) == []

    def test_no_tracked_lcsc_metadata_records(self):
        """A ``C<n>.meta.json`` (source=lcsc) must not be committed either."""
        out = subprocess.run(
            ["git", "-C", str(REPO_ROOT), "ls-files", "--", "boards", "src"],
            capture_output=True,
            text=True,
            check=True,
        ).stdout.splitlines()
        offenders = []
        for name in out:
            if not name.endswith(".meta.json"):
                continue
            try:
                if json.loads((REPO_ROOT / name).read_text()).get("source") == "lcsc":
                    offenders.append(name)
            except (OSError, ValueError, AttributeError):
                continue
        assert offenders == []
