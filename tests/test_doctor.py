"""Tests for ``kct doctor`` version-record drift checking (issue #4347).

The core logic in :mod:`kicad_tools.doctor` is exercised against synthetic
fixtures rooted at ``tmp_path`` so we can construct matching / drifted /
partial / dev-checkout record sets deterministically without touching the real
repo. The CLI glue is smoke-tested via ``kicad_tools.cli.main``.
"""

from __future__ import annotations

import json

import pytest

from kicad_tools.doctor import (
    CLAUDE_MD,
    INSTALL_METADATA,
    KCT_PATH,
    KICAD_CLI,
    NATIVE_BACKEND,
    PYPROJECT_DEPENDENCY,
    PYPROJECT_PROJECT_VERSION,
    PYTHON_ENV,
    DriftReport,
    EnvironmentReport,
    PreflightResult,
    PreflightStatus,
    RecordStatus,
    check_environment,
    check_kct_path,
    check_kicad_cli,
    check_native_backend,
    check_python_env,
    check_version_drift,
    environment_to_dict,
    normalize_version,
    render_environment_text,
    render_text,
    report_to_dict,
)

INSTALLED = "0.18.0"


# ---------------------------------------------------------------------------
# Fixture builders
# ---------------------------------------------------------------------------


def write_consumer_pyproject_tag(root, version, name="my-board"):
    """A consumer pyproject with a uv git-tag pin for kicad-tools."""
    (root / "pyproject.toml").write_text(
        "[project]\n"
        f'name = "{name}"\n'
        'version = "0.1.0"\n'
        'dependencies = ["kicad-tools"]\n'
        "\n"
        "[tool.uv.sources]\n"
        'kicad-tools = { git = "https://github.com/rjwalters/kicad-tools", '
        f'tag = "{version}" }}\n',
        encoding="utf-8",
    )


def write_metadata(root, version):
    kct = root / ".kct"
    kct.mkdir(exist_ok=True)
    (kct / "install-metadata.json").write_text(
        json.dumps({"kct_version": version, "install_date": "2026-07-18"}),
        encoding="utf-8",
    )


def write_claude_md(root, version, *, extra_before="# My Board\n\n"):
    (root / "CLAUDE.md").write_text(
        f"{extra_before}"
        "<!-- BEGIN KICAD-TOOLS -->\n"
        f"## kicad-tools ({version})\n"
        "\n"
        "This repo uses kicad-tools.\n"
        "<!-- END KICAD-TOOLS -->\n",
        encoding="utf-8",
    )


def get(report: DriftReport, name: str):
    for r in report.records:
        if r.name == name:
            return r
    raise AssertionError(f"record {name!r} not in report")


# --- environment-preflight fixture builders (issue #4542) ------------------


def ok_result(name=NATIVE_BACKEND):
    return PreflightResult(name, PreflightStatus.OK, "synthetic ok", None)


def fail_result(name=KICAD_CLI):
    return PreflightResult(name, PreflightStatus.FAIL, "synthetic fail", "do the thing")


def warn_result(name=NATIVE_BACKEND):
    return PreflightResult(name, PreflightStatus.WARN, "synthetic warn", "do the thing")


def patch_environment(monkeypatch, *checks):
    """Replace the CLI's environment probe with a synthetic report.

    Keeps the CLI tests hermetic: the real preflights depend on the host
    (KiCad install, native ``.so``, PATH contents) and spawn subprocesses.
    """
    report = EnvironmentReport(checks=list(checks))
    monkeypatch.setattr("kicad_tools.doctor.check_environment", lambda **kwargs: report)
    return report


def probe_info_available(build=12, required=12):
    """A ``probe_backend_info()``-shaped dict for an installed backend."""
    return {
        "available": True,
        "version": "1.0.0",
        "build_version": build,
        "required_build_version": required,
        "extension_path": "/some/where/router_cpp.cpython-312-darwin.so",
        "unavailable_reason": None,
        "probe": {"mode": "in-process", "failed": False, "error": None},
    }


def probe_info_unavailable(reason="No module named 'kicad_tools.router.router_cpp'"):
    return {
        "available": False,
        "version": None,
        "build_version": None,
        "required_build_version": 12,
        "extension_path": None,
        "unavailable_reason": reason,
        "probe": {"mode": "in-process", "failed": False, "error": None},
    }


def probe_info_failed(error="probe subprocess timed out"):
    return {
        "available": False,
        "probe": {"mode": "subprocess", "failed": True, "error": error},
    }


# ---------------------------------------------------------------------------
# normalize_version
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("0.18.0", "0.18.0"),
        ("v0.18.0", "0.18.0"),
        ("V0.18.0", "0.18.0"),
        ("  v0.18.0  ", "0.18.0"),
    ],
)
def test_normalize_version(raw, expected):
    assert normalize_version(raw) == expected


# ---------------------------------------------------------------------------
# Clean / matching consumer
# ---------------------------------------------------------------------------


def test_all_records_match(tmp_path):
    write_consumer_pyproject_tag(tmp_path, f"v{INSTALLED}")
    write_metadata(tmp_path, INSTALLED)
    write_claude_md(tmp_path, INSTALLED)

    report = check_version_drift(tmp_path, INSTALLED)

    assert not report.has_drift
    assert report.reconcile_command is None
    assert get(report, PYPROJECT_DEPENDENCY).status is RecordStatus.OK
    assert get(report, INSTALL_METADATA).status is RecordStatus.OK
    assert get(report, CLAUDE_MD).status is RecordStatus.OK
    # A consumer is not the source checkout.
    assert get(report, PYPROJECT_PROJECT_VERSION).status is RecordStatus.NOT_PRESENT


def test_v_prefix_normalizes_across_records(tmp_path):
    # Dependency tag has the v-prefix; metadata / CLAUDE.md do not.
    write_consumer_pyproject_tag(tmp_path, f"v{INSTALLED}")
    write_metadata(tmp_path, INSTALLED)
    write_claude_md(tmp_path, INSTALLED)

    report = check_version_drift(tmp_path, f"v{INSTALLED}")  # ground truth w/ v
    assert not report.has_drift


# ---------------------------------------------------------------------------
# Drift
# ---------------------------------------------------------------------------


def test_all_records_drift(tmp_path):
    write_consumer_pyproject_tag(tmp_path, "v0.16.0")
    write_metadata(tmp_path, "0.16.0")
    write_claude_md(tmp_path, "0.16.0")

    report = check_version_drift(tmp_path, INSTALLED)

    assert report.has_drift
    assert {r.name for r in report.stale_records} == {
        PYPROJECT_DEPENDENCY,
        INSTALL_METADATA,
        CLAUDE_MD,
    }
    assert report.reconcile_command == f"install-kct.sh --tag v{INSTALLED}"
    for name in (PYPROJECT_DEPENDENCY, INSTALL_METADATA, CLAUDE_MD):
        assert get(report, name).status is RecordStatus.DRIFT


def test_partial_drift(tmp_path):
    # metadata matches, dependency + CLAUDE.md are stale.
    write_consumer_pyproject_tag(tmp_path, "v0.16.0")
    write_metadata(tmp_path, INSTALLED)
    write_claude_md(tmp_path, "0.17.0")

    report = check_version_drift(tmp_path, INSTALLED)

    assert report.has_drift
    assert get(report, INSTALL_METADATA).status is RecordStatus.OK
    assert get(report, PYPROJECT_DEPENDENCY).status is RecordStatus.DRIFT
    assert get(report, CLAUDE_MD).status is RecordStatus.DRIFT
    assert {r.name for r in report.stale_records} == {PYPROJECT_DEPENDENCY, CLAUDE_MD}


# ---------------------------------------------------------------------------
# Dev / source checkout
# ---------------------------------------------------------------------------


def test_dev_checkout_source_version_ok(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "kicad-tools"\nversion = "0.18.0"\n',
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)

    assert not report.has_drift
    assert get(report, PYPROJECT_PROJECT_VERSION).status is RecordStatus.OK
    # Consumer records absent -> not_present, never error.
    assert get(report, PYPROJECT_DEPENDENCY).status is RecordStatus.NOT_PRESENT
    assert get(report, INSTALL_METADATA).status is RecordStatus.NOT_PRESENT
    assert get(report, CLAUDE_MD).status is RecordStatus.NOT_PRESENT


def test_dev_checkout_source_version_drift(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "kicad-tools"\nversion = "0.17.0"\n',
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    assert report.has_drift
    assert get(report, PYPROJECT_PROJECT_VERSION).status is RecordStatus.DRIFT


# ---------------------------------------------------------------------------
# Empty root: everything absent, never errors
# ---------------------------------------------------------------------------


def test_empty_root_all_not_present(tmp_path):
    report = check_version_drift(tmp_path, INSTALLED)
    assert not report.has_drift
    assert all(r.status is RecordStatus.NOT_PRESENT for r in report.records)


# ---------------------------------------------------------------------------
# Dependency-pin edge cases
# ---------------------------------------------------------------------------


def test_sha_pin_is_informational_not_drift(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "my-board"\n'
        'version = "0.1.0"\n'
        'dependencies = ["kicad-tools"]\n'
        "\n"
        "[tool.uv.sources]\n"
        'kicad-tools = { git = "https://github.com/rjwalters/kicad-tools", '
        'rev = "deadbeefcafe" }\n',
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    rec = get(report, PYPROJECT_DEPENDENCY)
    assert rec.status is RecordStatus.UNPINNED_TO_SHA
    assert not report.has_drift


def test_editable_path_is_informational_not_drift(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "my-board"\n'
        'version = "0.1.0"\n'
        'dependencies = ["kicad-tools"]\n'
        "\n"
        "[tool.uv.sources]\n"
        'kicad-tools = { path = "../kicad-tools", editable = true }\n',
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    rec = get(report, PYPROJECT_DEPENDENCY)
    assert rec.status is RecordStatus.EDITABLE
    assert not report.has_drift


def test_inline_git_dependency_string_tag(tmp_path):
    # No [tool.uv.sources] table; pin lives inline in the dependency string.
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "my-board"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        '  "kicad-tools @ git+https://github.com/rjwalters/kicad-tools@v0.16.0",\n'
        "]\n",
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    rec = get(report, PYPROJECT_DEPENDENCY)
    assert rec.status is RecordStatus.DRIFT
    assert rec.recorded_version == "v0.16.0"


def test_inline_git_dependency_sha(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "my-board"\n'
        'version = "0.1.0"\n'
        "dependencies = [\n"
        '  "kicad-tools @ git+https://github.com/rjwalters/kicad-tools@abc1234",\n'
        "]\n",
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    assert get(report, PYPROJECT_DEPENDENCY).status is RecordStatus.UNPINNED_TO_SHA


def test_exact_pypi_pin(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-board"\nversion = "0.1.0"\ndependencies = ["kicad-tools==0.18.0"]\n',
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    assert get(report, PYPROJECT_DEPENDENCY).status is RecordStatus.OK


def test_range_pypi_spec_is_informational(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-board"\nversion = "0.1.0"\ndependencies = ["kicad-tools>=0.10"]\n',
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    rec = get(report, PYPROJECT_DEPENDENCY)
    assert rec.status is RecordStatus.EDITABLE
    assert not report.has_drift


def test_no_kicad_tools_dependency(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "my-board"\nversion = "0.1.0"\ndependencies = ["requests"]\n',
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    assert get(report, PYPROJECT_DEPENDENCY).status is RecordStatus.NOT_PRESENT


# ---------------------------------------------------------------------------
# Malformed records never crash
# ---------------------------------------------------------------------------


def test_malformed_claude_md_unterminated(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# Board\n<!-- BEGIN KICAD-TOOLS -->\n## kicad-tools (0.16.0)\n(no end marker)\n",
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    rec = get(report, CLAUDE_MD)
    assert rec.status is RecordStatus.MALFORMED
    assert not report.has_drift  # malformed is not drift


def test_malformed_claude_md_end_before_begin(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# Board\n<!-- END KICAD-TOOLS -->\n<!-- BEGIN KICAD-TOOLS -->\n",
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    assert get(report, CLAUDE_MD).status is RecordStatus.MALFORMED


def test_claude_md_block_without_header(tmp_path):
    (tmp_path / "CLAUDE.md").write_text(
        "# Board\n<!-- BEGIN KICAD-TOOLS -->\nno header here\n<!-- END KICAD-TOOLS -->\n",
        encoding="utf-8",
    )
    report = check_version_drift(tmp_path, INSTALLED)
    assert get(report, CLAUDE_MD).status is RecordStatus.MALFORMED


def test_claude_md_no_marker_block_is_not_present(tmp_path):
    (tmp_path / "CLAUDE.md").write_text("# Board\n\nJust notes, no kicad-tools block.\n")
    report = check_version_drift(tmp_path, INSTALLED)
    assert get(report, CLAUDE_MD).status is RecordStatus.NOT_PRESENT


def test_malformed_metadata_json(tmp_path):
    kct = tmp_path / ".kct"
    kct.mkdir()
    (kct / "install-metadata.json").write_text("{ not valid json", encoding="utf-8")
    report = check_version_drift(tmp_path, INSTALLED)
    assert get(report, INSTALL_METADATA).status is RecordStatus.MALFORMED


def test_metadata_missing_version_field(tmp_path):
    kct = tmp_path / ".kct"
    kct.mkdir()
    (kct / "install-metadata.json").write_text('{"install_date": "2026-07-18"}')
    report = check_version_drift(tmp_path, INSTALLED)
    assert get(report, INSTALL_METADATA).status is RecordStatus.MALFORMED


def test_malformed_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("this is = not [valid toml", encoding="utf-8")
    report = check_version_drift(tmp_path, INSTALLED)
    # Both pyproject-derived records degrade to malformed, no crash.
    assert get(report, PYPROJECT_DEPENDENCY).status is RecordStatus.MALFORMED
    assert get(report, PYPROJECT_PROJECT_VERSION).status is RecordStatus.MALFORMED


# ---------------------------------------------------------------------------
# JSON shape
# ---------------------------------------------------------------------------


def test_json_shape(tmp_path):
    write_consumer_pyproject_tag(tmp_path, "v0.16.0")
    write_metadata(tmp_path, "0.16.0")
    write_claude_md(tmp_path, "0.16.0")

    payload = report_to_dict(check_version_drift(tmp_path, INSTALLED))

    assert payload["check"] == "version-drift"
    assert payload["installed_version"] == INSTALLED
    assert payload["has_drift"] is True
    assert payload["ok"] is False
    assert payload["reconcile_command"] == f"install-kct.sh --tag v{INSTALLED}"
    assert isinstance(payload["records"], list)
    assert len(payload["records"]) == 4
    for rec in payload["records"]:
        assert set(rec) == {"name", "path", "status", "recorded_version", "detail"}

    # Round-trips as JSON.
    assert json.loads(json.dumps(payload))["has_drift"] is True


def test_json_shape_clean(tmp_path):
    write_consumer_pyproject_tag(tmp_path, f"v{INSTALLED}")
    payload = report_to_dict(check_version_drift(tmp_path, INSTALLED))
    assert payload["ok"] is True
    assert payload["has_drift"] is False
    assert payload["reconcile_command"] is None


# ---------------------------------------------------------------------------
# Text rendering
# ---------------------------------------------------------------------------


def test_render_text_drift_names_records_and_reconcile(tmp_path):
    write_consumer_pyproject_tag(tmp_path, "v0.16.0")
    write_metadata(tmp_path, "0.16.0")
    text = render_text(check_version_drift(tmp_path, INSTALLED))
    assert "DRIFT" in text
    assert f"install-kct.sh --tag v{INSTALLED}" in text
    assert PYPROJECT_DEPENDENCY in text


def test_render_text_clean(tmp_path):
    write_consumer_pyproject_tag(tmp_path, f"v{INSTALLED}")
    text = render_text(check_version_drift(tmp_path, INSTALLED))
    assert "no version-record drift" in text


# ---------------------------------------------------------------------------
# CLI glue + exit codes
# ---------------------------------------------------------------------------


def test_cli_advisory_exit_zero_on_drift(tmp_path, capsys, monkeypatch):
    from kicad_tools.cli import main

    patch_environment(monkeypatch, ok_result())
    write_consumer_pyproject_tag(tmp_path, "v0.0.1")  # guaranteed drift
    rc = main(["doctor", "--root", str(tmp_path)])
    assert rc == 0  # advisory by default
    assert "DRIFT" in capsys.readouterr().out


def test_cli_strict_exit_one_on_drift(tmp_path, capsys, monkeypatch):
    from kicad_tools.cli import main

    patch_environment(monkeypatch, ok_result())
    write_consumer_pyproject_tag(tmp_path, "v0.0.1")
    rc = main(["doctor", "--root", str(tmp_path), "--strict"])
    assert rc == 1


def test_cli_strict_exit_zero_when_clean(tmp_path, capsys, monkeypatch):
    from kicad_tools import __version__
    from kicad_tools.cli import main

    patch_environment(monkeypatch, ok_result())
    write_consumer_pyproject_tag(tmp_path, f"v{__version__}")
    rc = main(["doctor", "--root", str(tmp_path), "--strict"])
    assert rc == 0


def test_cli_json_format(tmp_path, capsys, monkeypatch):
    from kicad_tools.cli import main

    patch_environment(monkeypatch, ok_result())
    write_consumer_pyproject_tag(tmp_path, "v0.0.1")
    rc = main(["doctor", "--root", str(tmp_path), "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["check"] == "version-drift"
    assert payload["has_drift"] is True


def test_cli_strict_exit_zero_on_informational_sha(tmp_path, monkeypatch):
    """--strict must not fail on informational (sha/editable) records."""
    from kicad_tools.cli import main

    patch_environment(monkeypatch, ok_result())
    (tmp_path / "pyproject.toml").write_text(
        "[project]\n"
        'name = "my-board"\n'
        'version = "0.1.0"\n'
        'dependencies = ["kicad-tools"]\n'
        "\n"
        "[tool.uv.sources]\n"
        'kicad-tools = { git = "https://github.com/rjwalters/kicad-tools", '
        'rev = "deadbeef" }\n',
        encoding="utf-8",
    )
    rc = main(["doctor", "--root", str(tmp_path), "--strict"])
    assert rc == 0


# ---------------------------------------------------------------------------
# Environment preflight: native-backend (issue #4542)
# ---------------------------------------------------------------------------


def test_native_backend_ok_when_available_and_current():
    result = check_native_backend(probe_fn=probe_info_available)
    assert result.name == NATIVE_BACKEND
    assert result.status is PreflightStatus.OK
    assert "1.0.0" in result.detail
    assert "12/12" in result.detail
    assert "router_cpp.cpython-312-darwin.so" in result.detail
    assert result.remedy is None


def test_native_backend_warn_when_stale_build():
    result = check_native_backend(probe_fn=lambda: probe_info_available(build=11, required=12))
    assert result.status is PreflightStatus.WARN
    assert "stale" in result.detail
    assert result.remedy == "kct build-native"


def test_native_backend_warn_when_unavailable():
    result = check_native_backend(probe_fn=lambda: probe_info_unavailable("no compiler"))
    assert result.status is PreflightStatus.WARN
    assert "no compiler" in result.detail
    assert result.remedy == "kct build-native"


def test_native_backend_fail_when_probe_cannot_run():
    result = check_native_backend(probe_fn=probe_info_failed)
    assert result.status is PreflightStatus.FAIL
    assert "probe subprocess timed out" in result.detail
    assert result.remedy is not None


# ---------------------------------------------------------------------------
# Environment preflight: kicad-cli
# ---------------------------------------------------------------------------


def _cli_at(path="/usr/bin/kicad-cli"):
    from pathlib import Path

    return lambda: Path(path)


def test_kicad_cli_ok_with_v8():
    result = check_kicad_cli(find_cli_fn=_cli_at(), version_fn=lambda cli: "8.0.6")
    assert result.name == KICAD_CLI
    assert result.status is PreflightStatus.OK
    assert "/usr/bin/kicad-cli" in result.detail
    assert "8.0.6" in result.detail
    assert result.remedy is None


def test_kicad_cli_ok_with_newer_major():
    result = check_kicad_cli(find_cli_fn=_cli_at(), version_fn=lambda cli: "9.0.0")
    assert result.status is PreflightStatus.OK


def test_kicad_cli_warn_below_compat_floor():
    result = check_kicad_cli(find_cli_fn=_cli_at(), version_fn=lambda cli: "7.0.11")
    assert result.status is PreflightStatus.WARN
    assert "7.0.11" in result.detail
    assert "8+" in result.detail
    assert result.remedy is not None


def test_kicad_cli_warn_when_version_unqueryable():
    result = check_kicad_cli(find_cli_fn=_cli_at(), version_fn=lambda cli: None)
    assert result.status is PreflightStatus.WARN
    assert "could not be queried" in result.detail


def test_kicad_cli_warn_when_version_unparseable():
    result = check_kicad_cli(find_cli_fn=_cli_at(), version_fn=lambda cli: "nightly-build")
    assert result.status is PreflightStatus.WARN
    assert "unrecognized" in result.detail


def test_kicad_cli_fail_when_not_found():
    result = check_kicad_cli(find_cli_fn=lambda: None, version_fn=lambda cli: "8.0.6")
    assert result.status is PreflightStatus.FAIL
    assert result.remedy == "Install KiCad 8 from https://www.kicad.org/download/"


# ---------------------------------------------------------------------------
# Environment preflight: python-env
# ---------------------------------------------------------------------------


def test_python_env_ok():
    result = check_python_env(has_shapely_fn=lambda: True, version_info=(3, 12, 1))
    assert result.name == PYTHON_ENV
    assert result.status is PreflightStatus.OK
    assert "3.12.1" in result.detail
    assert "shapely" in result.detail
    assert result.remedy is None


def test_python_env_ok_on_live_interpreter():
    # The suite itself runs on >= 3.10, so the live floor comparison passes.
    result = check_python_env(has_shapely_fn=lambda: True)
    assert result.status is PreflightStatus.OK


def test_python_env_fail_below_floor():
    result = check_python_env(has_shapely_fn=lambda: True, version_info=(3, 9, 5))
    assert result.status is PreflightStatus.FAIL
    assert "3.9.5" in result.detail
    assert "3.10" in result.detail
    assert result.remedy is not None


def test_python_env_fail_without_shapely_uses_hint_verbatim():
    from kicad_tools._shapely import SHAPELY_INSTALL_HINT

    result = check_python_env(has_shapely_fn=lambda: False, version_info=(3, 12, 1))
    assert result.status is PreflightStatus.FAIL
    assert "shapely" in result.detail
    assert result.remedy == SHAPELY_INSTALL_HINT


def test_python_env_fail_reports_both_problems():
    result = check_python_env(has_shapely_fn=lambda: False, version_info=(3, 8, 0))
    assert result.status is PreflightStatus.FAIL
    assert "3.8.0" in result.detail
    assert "shapely" in result.detail


# ---------------------------------------------------------------------------
# Environment preflight: kct-path (PATH-shadowed install)
# ---------------------------------------------------------------------------


def test_kct_path_ok_when_not_on_path():
    result = check_kct_path(installed_version="0.20.0", which_fn=lambda name: None)
    assert result.name == KCT_PATH
    assert result.status is PreflightStatus.OK
    assert result.remedy is None


def test_kct_path_ok_when_versions_match():
    result = check_kct_path(
        installed_version="0.20.0",
        which_fn=lambda name: "/home/u/.local/bin/kct",
        path_version_fn=lambda path: "kicad-tools 0.20.0",
    )
    assert result.status is PreflightStatus.OK
    assert "/home/u/.local/bin/kct" in result.detail


def test_kct_path_warn_when_path_kct_is_older():
    result = check_kct_path(
        installed_version="0.20.0",
        which_fn=lambda name: "/home/u/.local/bin/kct",
        path_version_fn=lambda path: "kicad-tools 0.15.1",
    )
    assert result.status is PreflightStatus.WARN
    assert "/home/u/.local/bin/kct" in result.detail
    assert "0.15.1" in result.detail
    assert "0.20.0" in result.detail
    assert "OLDER" in result.detail
    assert "invalid choice" in result.detail
    assert result.remedy is not None
    assert "uv run kct" in result.remedy


def test_kct_path_warn_when_path_kct_is_newer():
    result = check_kct_path(
        installed_version="0.18.0",
        which_fn=lambda name: "/usr/local/bin/kct",
        path_version_fn=lambda path: "kicad-tools 0.20.0",
    )
    assert result.status is PreflightStatus.WARN
    assert "0.18.0" in result.detail
    assert "0.20.0" in result.detail
    assert "OLDER" not in result.detail


def test_kct_path_warn_when_version_unqueryable():
    result = check_kct_path(
        installed_version="0.20.0",
        which_fn=lambda name: "/home/u/.local/bin/kct",
        path_version_fn=lambda path: None,
    )
    assert result.status is PreflightStatus.WARN
    assert "could not be queried" in result.detail


def test_kct_path_warn_when_version_unparseable():
    result = check_kct_path(
        installed_version="0.20.0",
        which_fn=lambda name: "/home/u/.local/bin/kct",
        path_version_fn=lambda path: "no digits here",
    )
    assert result.status is PreflightStatus.WARN
    assert "unrecognized" in result.detail


# ---------------------------------------------------------------------------
# Environment preflight: aggregate + rendering + fail-soft
# ---------------------------------------------------------------------------


def _injected_environment(**overrides):
    kwargs = {
        "installed_version": "0.20.0",
        "probe_fn": probe_info_available,
        "find_cli_fn": _cli_at(),
        "version_fn": lambda cli: "8.0.6",
        "has_shapely_fn": lambda: True,
        "version_info": (3, 12, 1),
        "which_fn": lambda name: None,
        "path_version_fn": lambda path: None,
    }
    kwargs.update(overrides)
    return check_environment(**kwargs)


def test_environment_fixed_check_order():
    report = _injected_environment()
    assert [c.name for c in report.checks] == [NATIVE_BACKEND, KICAD_CLI, PYTHON_ENV, KCT_PATH]
    assert report.ok is True
    assert report.has_fail is False
    assert report.has_warn is False


def test_environment_fail_soft_on_raising_probe():
    def exploding_probe():
        raise RuntimeError("boom")

    report = _injected_environment(probe_fn=exploding_probe)
    native = report.checks[0]
    assert native.name == NATIVE_BACKEND
    assert native.status is PreflightStatus.FAIL
    assert "RuntimeError" in native.detail
    assert "boom" in native.detail
    assert report.has_fail is True
    assert report.ok is False


def test_environment_warn_does_not_clear_ok_flag_semantics():
    report = _injected_environment(probe_fn=probe_info_unavailable)
    assert report.has_warn is True
    assert report.has_fail is False
    assert report.ok is True  # ok mirrors --strict semantics: warns are advisory


def test_environment_to_dict_shape_and_determinism():
    payload_a = environment_to_dict(_injected_environment())
    payload_b = environment_to_dict(_injected_environment())
    assert set(payload_a) == {"check", "ok", "has_fail", "has_warn", "checks"}
    assert payload_a["check"] == "environment"
    assert len(payload_a["checks"]) == 4
    for check in payload_a["checks"]:
        assert set(check) == {"name", "status", "detail", "remedy"}
    # Same environment in => byte-identical report out.
    assert json.dumps(payload_a, sort_keys=True) == json.dumps(payload_b, sort_keys=True)


def test_render_environment_text_clean():
    text = render_environment_text(_injected_environment())
    assert "environment preflight" in text
    assert "OK: environment preflight passed." in text
    assert NATIVE_BACKEND in text


def test_render_environment_text_fail_names_check_and_remedy():
    text = render_environment_text(_injected_environment(find_cli_fn=lambda: None))
    assert "FAIL: environment preflight failed: kicad-cli" in text
    assert "remedy: Install KiCad 8" in text


def test_render_environment_text_warn_is_advisory():
    text = render_environment_text(_injected_environment(probe_fn=probe_info_unavailable))
    assert "WARN: environment degraded (advisory): native-backend" in text
    assert "remedy: kct build-native" in text


# ---------------------------------------------------------------------------
# CLI glue: environment group (issue #4542)
# ---------------------------------------------------------------------------


def test_cli_default_exit_zero_with_failing_preflight(tmp_path, capsys, monkeypatch):
    from kicad_tools import __version__
    from kicad_tools.cli import main

    patch_environment(monkeypatch, fail_result())
    write_consumer_pyproject_tag(tmp_path, f"v{__version__}")  # clean drift
    rc = main(["doctor", "--root", str(tmp_path)])
    assert rc == 0  # advisory by default
    assert "FAIL" in capsys.readouterr().out


def test_cli_strict_exit_one_on_preflight_fail(tmp_path, monkeypatch):
    from kicad_tools import __version__
    from kicad_tools.cli import main

    patch_environment(monkeypatch, fail_result())
    write_consumer_pyproject_tag(tmp_path, f"v{__version__}")  # clean drift
    rc = main(["doctor", "--root", str(tmp_path), "--strict"])
    assert rc == 1


def test_cli_strict_exit_zero_on_preflight_warn_only(tmp_path, monkeypatch):
    from kicad_tools import __version__
    from kicad_tools.cli import main

    patch_environment(monkeypatch, warn_result(), ok_result(KICAD_CLI))
    write_consumer_pyproject_tag(tmp_path, f"v{__version__}")
    rc = main(["doctor", "--root", str(tmp_path), "--strict"])
    assert rc == 0


def test_cli_json_includes_environment_additively(tmp_path, capsys, monkeypatch):
    from kicad_tools.cli import main

    patch_environment(monkeypatch, ok_result(), fail_result())
    write_consumer_pyproject_tag(tmp_path, "v0.0.1")
    rc = main(["doctor", "--root", str(tmp_path), "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # Existing drift keys keep their shape.
    assert payload["check"] == "version-drift"
    assert payload["has_drift"] is True
    assert len(payload["records"]) == 4
    # New content lands under the additive "environment" key.
    env = payload["environment"]
    assert env["check"] == "environment"
    assert env["ok"] is False
    assert env["has_fail"] is True
    assert [c["status"] for c in env["checks"]] == ["ok", "fail"]


def test_cli_text_renders_both_groups(tmp_path, capsys, monkeypatch):
    from kicad_tools import __version__
    from kicad_tools.cli import main

    patch_environment(monkeypatch, ok_result())
    write_consumer_pyproject_tag(tmp_path, f"v{__version__}")
    rc = main(["doctor", "--root", str(tmp_path)])
    assert rc == 0
    out = capsys.readouterr().out
    assert "kct doctor: version-record drift" in out
    assert "kct doctor: environment preflight" in out
