"""Tests for the report renderers module.

Verifies Markdown-to-HTML conversion, base64 image embedding,
CSS integration, PDF rendering guards, and edge cases.
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture
def simple_markdown() -> str:
    """A minimal Markdown document for testing."""
    return (
        "# Test Report\n\n"
        "This is a test report.\n\n"
        "## DRC Results\n\n"
        "| Rule | Status |\n"
        "|------|--------|\n"
        "| Clearance | Pass |\n"
        "| Width | Fail |\n\n"
        "```python\nprint('hello')\n```\n"
    )


@pytest.fixture
def markdown_with_image() -> str:
    """Markdown containing an image reference."""
    return "# Board Layout\n\n![Front Copper](front_copper.png)\n\nSome description.\n"


@pytest.fixture
def markdown_with_multiple_images() -> str:
    """Markdown containing multiple image references."""
    return "# Layers\n\n![Front](front.png)\n\n![Back](back.png)\n\n![Missing](missing.png)\n\n"


@pytest.fixture
def figures_dir(tmp_path: Path) -> Path:
    """Create a temporary figures directory with a test PNG file."""
    figs = tmp_path / "figures"
    figs.mkdir()
    # Minimal valid PNG: 1x1 transparent pixel
    # This is the smallest valid PNG file (67 bytes)
    png_header = (
        b"\x89PNG\r\n\x1a\n"  # PNG signature
        b"\x00\x00\x00\rIHDR"
        b"\x00\x00\x00\x01"  # width = 1
        b"\x00\x00\x00\x01"  # height = 1
        b"\x08\x02"  # bit depth 8, color type 2 (RGB)
        b"\x00\x00\x00"  # compression, filter, interlace
        b"\x90wS\xde"  # CRC
        b"\x00\x00\x00\x0cIDATx"
        b"\x9cc\xf8\x0f\x00\x00\x01\x01\x00\x05"
        b"\x18\xd8N"  # CRC
        b"\x00\x00\x00\x00IEND"
        b"\xaeB`\x82"  # CRC
    )
    (figs / "front_copper.png").write_bytes(png_header)
    (figs / "front.png").write_bytes(png_header)
    (figs / "back.png").write_bytes(png_header)
    return figs


# ---------------------------------------------------------------------------
# render_html tests
# ---------------------------------------------------------------------------


class TestRenderHtml:
    """Tests for the render_html function."""

    def test_produces_valid_html(self, simple_markdown: str) -> None:
        """Output contains required HTML5 structural elements."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        assert "<!DOCTYPE html>" in result
        assert "<html" in result
        assert "<head>" in result
        assert "<body>" in result
        assert "</html>" in result

    def test_contains_embedded_css(self, simple_markdown: str) -> None:
        """CSS is embedded inline in a <style> tag, not as a link."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        assert "<style>" in result
        # Check some characteristic CSS content
        assert ".drc-error" in result
        assert ".drc-warning" in result
        assert ".drc-ok" in result

    def test_markdown_converted_to_html(self, simple_markdown: str) -> None:
        """Markdown headings, tables, and code blocks are converted to HTML."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        assert "<h1>" in result or "<h1 " in result
        assert "<h2>" in result or "<h2 " in result
        assert "<table>" in result
        assert "<code>" in result or "<code " in result

    def test_tables_wrapped_for_responsive_layout(self, simple_markdown: str) -> None:
        """Tables are wrapped in a scrollable container div."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        assert '<div class="table-wrapper">' in result

    def test_embeds_figures_as_base64(
        self,
        markdown_with_image: str,
        figures_dir: Path,
    ) -> None:
        """PNG images from figures_dir are embedded as base64 data URIs."""
        from kicad_tools.report.renderers import render_html

        result = render_html(markdown_with_image, figures_dir=figures_dir)
        assert "data:image/png;base64," in result
        # Original filename should NOT appear as a src attribute
        assert 'src="front_copper.png"' not in result

    def test_embeds_multiple_figures(
        self,
        markdown_with_multiple_images: str,
        figures_dir: Path,
    ) -> None:
        """Multiple existing PNGs are embedded; missing ones are left unchanged."""
        from kicad_tools.report.renderers import render_html

        result = render_html(markdown_with_multiple_images, figures_dir=figures_dir)
        # Two images should be embedded
        base64_count = result.count("data:image/png;base64,")
        assert base64_count == 2
        # Missing image reference should be preserved unchanged
        assert 'src="missing.png"' in result

    def test_no_figures_dir_preserves_paths(self, markdown_with_image: str) -> None:
        """When figures_dir is None, image src attributes are left as-is."""
        from kicad_tools.report.renderers import render_html

        result = render_html(markdown_with_image, figures_dir=None)
        assert "data:image/png;base64," not in result

    def test_empty_markdown(self) -> None:
        """Empty Markdown input does not raise an error."""
        from kicad_tools.report.renderers import render_html

        result = render_html("")
        assert "<!DOCTYPE html>" in result
        assert "<body>" in result

    def test_markdown_no_images(self, simple_markdown: str, figures_dir: Path) -> None:
        """Markdown without image references renders without error even with a figures_dir."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown, figures_dir=figures_dir)
        assert "data:image/png;base64," not in result
        assert "<!DOCTYPE html>" in result

    def test_empty_figures_dir(self, markdown_with_image: str, tmp_path: Path) -> None:
        """Empty figures directory does not crash; images are left as relative paths."""
        from kicad_tools.report.renderers import render_html

        empty_dir = tmp_path / "empty_figs"
        empty_dir.mkdir()

        result = render_html(markdown_with_image, figures_dir=empty_dir)
        assert "data:image/png;base64," not in result
        assert "<!DOCTYPE html>" in result

    def test_non_png_files_not_embedded(self, tmp_path: Path) -> None:
        """Non-PNG files in figures directory are not embedded."""
        from kicad_tools.report.renderers import render_html

        figs = tmp_path / "figs"
        figs.mkdir()
        (figs / "diagram.svg").write_text("<svg></svg>")

        md = "![Diagram](diagram.svg)\n"
        result = render_html(md, figures_dir=figs)
        assert "data:image/png;base64," not in result

    def test_self_contained_single_file(self, simple_markdown: str) -> None:
        """Output is a self-contained document with no external references."""
        from kicad_tools.report.renderers import render_html

        result = render_html(simple_markdown)
        # Should not contain <link rel="stylesheet"> or external references
        assert "<link " not in result
        assert 'rel="stylesheet"' not in result


# ---------------------------------------------------------------------------
# render_pdf tests
# ---------------------------------------------------------------------------


class TestRenderPdf:
    """Tests for the render_pdf function."""

    def test_missing_weasyprint_raises_import_error(self, tmp_path: Path) -> None:
        """render_pdf raises ImportError with install hint when weasyprint is absent."""
        from kicad_tools.report.renderers import render_pdf

        with patch(
            "kicad_tools.report.renderers._weasyprint_available",
            return_value=False,
        ):
            with pytest.raises(ImportError, match="weasyprint"):
                render_pdf("<html></html>", tmp_path / "report.pdf")

    def test_import_error_contains_install_instructions(self, tmp_path: Path) -> None:
        """The ImportError message tells the user how to install weasyprint."""
        from kicad_tools.report.renderers import render_pdf

        with patch(
            "kicad_tools.report.renderers._weasyprint_available",
            return_value=False,
        ):
            with pytest.raises(ImportError, match=r"pip install.*kicad-tools\[report\]"):
                render_pdf("<html></html>", tmp_path / "report.pdf")

    def test_creates_parent_directories(self, tmp_path: Path) -> None:
        """render_pdf creates parent directories if they do not exist."""
        import sys

        from kicad_tools.report.renderers import render_pdf

        output = tmp_path / "deep" / "nested" / "report.pdf"

        # Create a fake weasyprint module with a mock HTML class
        mock_html_cls = type(
            "MockHTML",
            (),
            {
                "__init__": lambda self, string: None,
                "write_pdf": lambda self, path: Path(path).write_bytes(b"%PDF-mock"),
            },
        )

        fake_weasyprint = type(sys)("weasyprint")
        fake_weasyprint.HTML = mock_html_cls
        sys.modules["weasyprint"] = fake_weasyprint

        try:
            with patch(
                "kicad_tools.report.renderers._weasyprint_available",
                return_value=True,
            ):
                render_pdf("<html></html>", output)
                assert output.parent.exists()
                assert output.exists()
        finally:
            del sys.modules["weasyprint"]


# ---------------------------------------------------------------------------
# CSS tests
# ---------------------------------------------------------------------------


class TestCss:
    """Tests for the CSS template."""

    def test_css_file_exists(self) -> None:
        """The report.css template file exists on disk."""
        from kicad_tools.report.renderers import _TEMPLATES_DIR

        css_path = _TEMPLATES_DIR / "report.css"
        assert css_path.exists()

    def test_css_classes_present(self) -> None:
        """CSS contains the required DRC badge classes."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert ".drc-error" in css
        assert ".drc-warning" in css
        assert ".drc-ok" in css

    def test_css_has_print_media_query(self) -> None:
        """CSS contains @media print rules for PDF layout."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert "@media print" in css

    def test_css_has_table_styles(self) -> None:
        """CSS contains table styling rules."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert "table" in css
        assert "table-layout" in css

    def test_css_has_image_constraints(self) -> None:
        """CSS constrains images with max-width."""
        from kicad_tools.report.renderers import _load_css

        css = _load_css()
        assert "max-width" in css


# ---------------------------------------------------------------------------
# Internal helper tests
# ---------------------------------------------------------------------------


class TestEmbedImages:
    """Tests for the _embed_images helper."""

    def test_replaces_existing_png(self, figures_dir: Path) -> None:
        """Existing PNG src is replaced with a data URI."""
        from kicad_tools.report.renderers import _embed_images

        html = '<img src="front_copper.png" alt="test">'
        result = _embed_images(html, figures_dir)
        assert result.startswith('<img src="data:image/png;base64,')
        assert 'alt="test">' in result

    def test_preserves_missing_images(self, figures_dir: Path) -> None:
        """References to non-existent files are left unchanged."""
        from kicad_tools.report.renderers import _embed_images

        html = '<img src="nonexistent.png" alt="gone">'
        result = _embed_images(html, figures_dir)
        assert result == html

    def test_replaces_png_with_alt_before_src(self, figures_dir: Path) -> None:
        """Handles markdown-library output where alt appears before src."""
        from kicad_tools.report.renderers import _embed_images

        # This is the format Python-Markdown produces
        html = '<img alt="Front Copper" src="front_copper.png" />'
        result = _embed_images(html, figures_dir)
        assert "data:image/png;base64," in result
        assert 'src="front_copper.png"' not in result
        assert 'alt="Front Copper"' in result

    def test_handles_no_img_tags(self, figures_dir: Path) -> None:
        """HTML without img tags passes through unchanged."""
        from kicad_tools.report.renderers import _embed_images

        html = "<p>No images here</p>"
        result = _embed_images(html, figures_dir)
        assert result == html


class TestWrapTables:
    """Tests for the _wrap_tables helper."""

    def test_wraps_table_in_div(self) -> None:
        """Tables are wrapped in a div with class table-wrapper."""
        from kicad_tools.report.renderers import _wrap_tables

        html = "<table><tr><td>cell</td></tr></table>"
        result = _wrap_tables(html)
        assert '<div class="table-wrapper"><table>' in result
        assert "</table></div>" in result

    def test_wraps_multiple_tables(self) -> None:
        """Multiple tables in the document are each wrapped."""
        from kicad_tools.report.renderers import _wrap_tables

        html = "<table><tr><td>1</td></tr></table><p>gap</p><table><tr><td>2</td></tr></table>"
        result = _wrap_tables(html)
        assert result.count('<div class="table-wrapper">') == 2
        assert result.count("</table></div>") == 2

    def test_no_table_passes_through(self) -> None:
        """HTML without tables passes through unchanged."""
        from kicad_tools.report.renderers import _wrap_tables

        html = "<p>No tables</p>"
        result = _wrap_tables(html)
        assert result == html


class TestWrapHtml:
    """Tests for the _wrap_html helper."""

    def test_produces_complete_document(self) -> None:
        """Output is a complete HTML5 document."""
        from kicad_tools.report.renderers import _wrap_html

        result = _wrap_html("<p>body</p>", "body { color: red; }")
        assert "<!DOCTYPE html>" in result
        assert "<html lang=" in result
        assert "<meta charset=" in result
        assert "<p>body</p>" in result
        assert "body { color: red; }" in result

    def test_css_in_style_tag(self) -> None:
        """CSS is embedded in a <style> tag, not a <link>."""
        from kicad_tools.report.renderers import _wrap_html

        css = ".test { color: blue; }"
        result = _wrap_html("<p>test</p>", css)
        assert "<style>" in result
        assert css in result
        assert "<link" not in result


class TestWeasprintAvailable:
    """Tests for the _weasyprint_available guard."""

    def test_returns_false_when_not_installed(self) -> None:
        """Returns False when weasyprint cannot be imported."""
        import sys

        # Temporarily hide weasyprint if it exists
        saved = sys.modules.get("weasyprint")
        sys.modules["weasyprint"] = None  # type: ignore[assignment]
        try:
            # Need to reload to reset the check
            from kicad_tools.report.renderers import _weasyprint_available

            # Mock the import to raise ImportError
            with patch.dict(sys.modules, {"weasyprint": None}):
                # _weasyprint_available tries 'import weasyprint', which will
                # raise ImportError when module is None in sys.modules
                result = _weasyprint_available()
                assert result is False
        finally:
            if saved is not None:
                sys.modules["weasyprint"] = saved
            else:
                sys.modules.pop("weasyprint", None)
