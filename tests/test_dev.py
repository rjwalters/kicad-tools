"""Tests for the kicad_tools.dev module - version mismatch detection."""

from pathlib import Path
from unittest.mock import patch

import pytest


class TestGetInstalledVersion:
    """Tests for get_installed_version function."""

    def test_returns_version_string(self):
        """Should return a version string when package is installed."""
        from kicad_tools.dev import get_installed_version

        version = get_installed_version()
        # Version should be a string (could be actual version or "unknown")
        assert isinstance(version, str)

    def test_returns_unknown_on_import_error(self):
        """Should return 'unknown' if importlib.metadata fails."""
        from kicad_tools.dev import get_installed_version

        # Patch the version function at the point where it's used
        with patch("importlib.metadata.version", side_effect=Exception("fail")):
            version = get_installed_version()
            assert version == "unknown"


class TestGetSourceVersion:
    """Tests for get_source_version function."""

    def test_finds_source_version_from_pyproject(self, tmp_path: Path):
        """Should read version from pyproject.toml."""
        from kicad_tools.dev import get_source_version

        # Create a mock pyproject.toml
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            """
[project]
name = "test-project"
version = "1.2.3"
"""
        )

        version = get_source_version(tmp_path)
        assert version == "1.2.3"

    def test_returns_none_when_no_pyproject(self, tmp_path: Path):
        """Should return None if pyproject.toml doesn't exist."""
        from kicad_tools.dev import get_source_version

        version = get_source_version(tmp_path)
        assert version is None

    def test_returns_none_on_parse_error(self, tmp_path: Path):
        """Should return None if pyproject.toml is invalid."""
        from kicad_tools.dev import get_source_version

        # Create invalid TOML
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text("this is not valid toml {{{")

        version = get_source_version(tmp_path)
        assert version is None


class TestCheckVersionMatch:
    """Tests for check_version_match function."""

    def test_returns_true_when_versions_match(self, tmp_path: Path):
        """Should return match=True when versions are the same."""
        from kicad_tools.dev import check_version_match, get_installed_version

        # Create pyproject.toml with same version as installed
        installed = get_installed_version()
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            f"""
[project]
name = "kicad-tools"
version = "{installed}"
"""
        )

        match, inst_ver, src_ver = check_version_match(tmp_path)
        assert match is True
        assert inst_ver == installed
        assert src_ver == installed

    def test_returns_false_when_versions_differ(self, tmp_path: Path):
        """Should return match=False when versions differ."""
        from kicad_tools.dev import check_version_match

        # Create pyproject.toml with different version
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            """
[project]
name = "kicad-tools"
version = "99.99.99"
"""
        )

        match, inst_ver, src_ver = check_version_match(tmp_path)
        assert match is False
        assert src_ver == "99.99.99"

    def test_returns_true_when_source_not_found(self, tmp_path: Path):
        """Should return match=True when source dir has no pyproject.toml."""
        from kicad_tools.dev import check_version_match

        # Empty directory - no pyproject.toml
        match, inst_ver, src_ver = check_version_match(tmp_path)
        assert match is True
        assert src_ver is None


class TestWarnIfStale:
    """Tests for warn_if_stale function."""

    def test_returns_true_when_versions_match(self, tmp_path: Path, capsys):
        """Should return True and not print warning when versions match."""
        from kicad_tools import dev
        from kicad_tools.dev import get_installed_version, warn_if_stale

        # Reset warning state
        dev._warned = False

        # Create pyproject.toml with same version as installed
        installed = get_installed_version()
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            f"""
[project]
name = "kicad-tools"
version = "{installed}"
"""
        )

        result = warn_if_stale(tmp_path)
        assert result is True

        captured = capsys.readouterr()
        assert "mismatch" not in captured.err.lower()

    def test_returns_false_and_warns_when_mismatch(self, tmp_path: Path, capsys):
        """Should return False and print warning when versions differ."""
        from kicad_tools import dev
        from kicad_tools.dev import warn_if_stale

        # Reset warning state
        dev._warned = False

        # Create pyproject.toml with different version
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            """
[project]
name = "kicad-tools"
version = "99.99.99"
"""
        )

        result = warn_if_stale(tmp_path)
        assert result is False

        captured = capsys.readouterr()
        assert "mismatch" in captured.err.lower()
        assert "99.99.99" in captured.err

    def test_only_warns_once_without_force(self, tmp_path: Path, capsys):
        """Should only warn once unless force=True."""
        from kicad_tools import dev
        from kicad_tools.dev import warn_if_stale

        # Reset warning state
        dev._warned = False

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            """
[project]
name = "kicad-tools"
version = "99.99.99"
"""
        )

        # First call should warn
        warn_if_stale(tmp_path)
        captured1 = capsys.readouterr()
        assert "mismatch" in captured1.err.lower()

        # Second call should not warn (already warned)
        warn_if_stale(tmp_path)
        captured2 = capsys.readouterr()
        assert "mismatch" not in captured2.err.lower()

        # With force=True, should warn again
        warn_if_stale(tmp_path, force=True)
        captured3 = capsys.readouterr()
        assert "mismatch" in captured3.err.lower()


class TestRequireSourceVersion:
    """Tests for require_source_version function."""

    def test_does_not_raise_when_versions_match(self, tmp_path: Path):
        """Should not raise when versions match."""
        from kicad_tools.dev import get_installed_version, require_source_version

        installed = get_installed_version()
        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            f"""
[project]
name = "kicad-tools"
version = "{installed}"
"""
        )

        # Should not raise
        require_source_version(tmp_path)

    def test_raises_when_versions_differ(self, tmp_path: Path):
        """Should raise RuntimeError when versions differ."""
        from kicad_tools.dev import require_source_version

        pyproject = tmp_path / "pyproject.toml"
        pyproject.write_text(
            """
[project]
name = "kicad-tools"
version = "99.99.99"
"""
        )

        with pytest.raises(RuntimeError) as exc_info:
            require_source_version(tmp_path)

        assert "mismatch" in str(exc_info.value).lower()
        assert "99.99.99" in str(exc_info.value)
