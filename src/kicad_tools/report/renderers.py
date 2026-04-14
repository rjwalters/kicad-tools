"""Report renderers for converting Markdown reports to HTML and PDF.

Provides functions to render Markdown content (from ReportGenerator) into
self-contained HTML documents with embedded CSS and base64-encoded images,
and optionally to PDF via weasyprint.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"


def render_html(
    markdown_content: str,
    figures_dir: Path | str | None = None,
) -> str:
    """Convert Markdown report to a self-contained HTML document.

    The output is a complete HTML file with embedded CSS (from report.css)
    and, when a figures directory is provided, all referenced PNG images
    converted to inline base64 data URIs.

    Args:
        markdown_content: Markdown source text (e.g. from ReportGenerator).
        figures_dir: Directory containing PNG figures referenced in the Markdown.
            Accepts both :class:`~pathlib.Path` objects and plain strings.
            If None, image ``src`` attributes are left unchanged.

    Returns:
        A complete, self-contained HTML document string.
    """
    try:
        import markdown as md_lib
    except ImportError as exc:
        raise ImportError(
            "HTML output requires the 'markdown' package. "
            "Install with: pip install 'kicad-tools[report]'"
        ) from exc

    html_body = md_lib.markdown(
        markdown_content,
        extensions=["tables", "fenced_code", "toc"],
    )

    css = _load_css()

    if figures_dir is not None:
        figures_dir = Path(figures_dir)
        html_body = _embed_images(html_body, figures_dir)

    # Wrap tables in a scrollable div for responsive layout
    html_body = _wrap_tables(html_body)

    return _wrap_html(html_body, css)


def render_pdf(html_content: str, output_path: Path | str) -> None:
    """Render an HTML document to PDF via weasyprint.

    Args:
        html_content: Complete HTML document string (from ``render_html``).
        output_path: Destination path for the PDF file. Accepts both
            :class:`~pathlib.Path` objects and plain strings.

    Raises:
        ImportError: If weasyprint is not installed, with an actionable
            message directing the user to install it.
    """
    output_path = Path(output_path)
    if not _weasyprint_available():
        raise ImportError(
            "PDF output requires weasyprint. Install with: pip install 'kicad-tools[report]'"
        )

    import weasyprint

    output_path.parent.mkdir(parents=True, exist_ok=True)
    weasyprint.HTML(string=html_content).write_pdf(str(output_path))


def _weasyprint_available() -> bool:
    """Check whether weasyprint can be imported."""
    try:
        import weasyprint  # noqa: F401

        return True
    except ImportError:
        return False


def _load_css() -> str:
    """Load the report CSS template from the templates directory.

    Returns:
        CSS content as a string.
    """
    css_path = _TEMPLATES_DIR / "report.css"
    return css_path.read_text(encoding="utf-8")


def _embed_images(html: str, figures_dir: Path) -> str:
    """Replace ``<img src="filename.png">`` references with base64 data URIs.

    Only PNG files that exist within ``figures_dir`` are embedded.
    Non-existent files and non-PNG references are left unchanged.

    Args:
        html: HTML body string containing ``<img>`` tags.
        figures_dir: Directory to look for image files.

    Returns:
        HTML string with matching image sources replaced by data URIs.
    """

    def _replacer(match: re.Match) -> str:
        src = match.group(1)
        # Use only the filename to avoid double-nesting when src already
        # contains a directory prefix like "figures/" from template rendering.
        img_path = figures_dir / Path(src).name
        if img_path.exists() and img_path.suffix.lower() == ".png":
            data = base64.b64encode(img_path.read_bytes()).decode("ascii")
            return match.group(0).replace(
                f'src="{src}"',
                f'src="data:image/png;base64,{data}"',
            )
        return match.group(0)

    # Match src="..." anywhere within an <img ...> tag.
    # The markdown library may place alt before src, e.g.
    #   <img alt="..." src="filename.png" />
    return re.sub(r'<img\b[^>]*\bsrc="([^"]+)"', _replacer, html)


def _wrap_tables(html: str) -> str:
    """Wrap ``<table>`` elements in a scrollable div for responsive layout.

    Args:
        html: HTML body string.

    Returns:
        HTML string with tables wrapped in ``<div class="table-wrapper">``.
    """
    return re.sub(
        r"(<table>)",
        r'<div class="table-wrapper">\1',
        html,
    ).replace("</table>", "</table></div>")


def _wrap_html(body: str, css: str) -> str:
    """Wrap an HTML body fragment in a complete HTML5 document.

    The CSS is embedded inline in a ``<style>`` tag so the output is
    a single self-contained file.

    Args:
        body: HTML body content.
        css: CSS stylesheet content.

    Returns:
        Complete HTML5 document string.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="kicad-tools report renderer">
<title>KiCad Design Report</title>
<style>
{css}
</style>
</head>
<body>
{body}
</body>
</html>"""
