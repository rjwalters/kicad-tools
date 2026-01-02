"""Tests for kicad_tools.utils module."""

from pathlib import Path

from kicad_tools.utils import ensure_parent_dir


class TestEnsureParentDir:
    """Tests for ensure_parent_dir function."""

    def test_creates_parent_directory(self, tmp_path: Path) -> None:
        """Test that parent directories are created."""
        file_path = tmp_path / "nested" / "dir" / "file.txt"
        assert not file_path.parent.exists()

        result = ensure_parent_dir(file_path)

        assert result == file_path
        assert file_path.parent.exists()
        assert file_path.parent.is_dir()

    def test_returns_original_path(self, tmp_path: Path) -> None:
        """Test that the original path is returned for chaining."""
        file_path = tmp_path / "test" / "file.txt"

        result = ensure_parent_dir(file_path)

        assert result is file_path

    def test_existing_parent_dir(self, tmp_path: Path) -> None:
        """Test behavior when parent directory already exists."""
        file_path = tmp_path / "existing" / "file.txt"
        file_path.parent.mkdir(parents=True)

        result = ensure_parent_dir(file_path)

        assert result == file_path
        assert file_path.parent.exists()

    def test_chaining_with_write(self, tmp_path: Path) -> None:
        """Test that the function can be chained with write operations."""
        file_path = tmp_path / "chain" / "test.txt"
        content = "test content"

        ensure_parent_dir(file_path).write_text(content)

        assert file_path.exists()
        assert file_path.read_text() == content

    def test_deeply_nested_path(self, tmp_path: Path) -> None:
        """Test with deeply nested directory structure."""
        file_path = tmp_path / "a" / "b" / "c" / "d" / "e" / "file.txt"

        result = ensure_parent_dir(file_path)

        assert result == file_path
        assert file_path.parent.exists()
