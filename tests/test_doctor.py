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
    PYPROJECT_DEPENDENCY,
    PYPROJECT_PROJECT_VERSION,
    DriftReport,
    RecordStatus,
    check_version_drift,
    normalize_version,
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


def test_cli_advisory_exit_zero_on_drift(tmp_path, capsys):
    from kicad_tools.cli import main

    write_consumer_pyproject_tag(tmp_path, "v0.0.1")  # guaranteed drift
    rc = main(["doctor", "--root", str(tmp_path)])
    assert rc == 0  # advisory by default
    assert "DRIFT" in capsys.readouterr().out


def test_cli_strict_exit_one_on_drift(tmp_path, capsys):
    from kicad_tools.cli import main

    write_consumer_pyproject_tag(tmp_path, "v0.0.1")
    rc = main(["doctor", "--root", str(tmp_path), "--strict"])
    assert rc == 1


def test_cli_strict_exit_zero_when_clean(tmp_path, capsys):
    from kicad_tools import __version__
    from kicad_tools.cli import main

    write_consumer_pyproject_tag(tmp_path, f"v{__version__}")
    rc = main(["doctor", "--root", str(tmp_path), "--strict"])
    assert rc == 0


def test_cli_json_format(tmp_path, capsys):
    from kicad_tools.cli import main

    write_consumer_pyproject_tag(tmp_path, "v0.0.1")
    rc = main(["doctor", "--root", str(tmp_path), "--format", "json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["check"] == "version-drift"
    assert payload["has_drift"] is True


def test_cli_strict_exit_zero_on_informational_sha(tmp_path):
    """--strict must not fail on informational (sha/editable) records."""
    from kicad_tools.cli import main

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
