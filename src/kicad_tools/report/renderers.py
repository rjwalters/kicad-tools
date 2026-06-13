"""Report renderers for converting Markdown reports to HTML and PDF.

Provides functions to render Markdown content (from ReportGenerator) into
self-contained HTML documents with embedded CSS and base64-encoded images,
and optionally to PDF via weasyprint.  Also provides interactive HTML
reports with embedded PCB visualization via :func:`render_interactive_html`.
"""

from __future__ import annotations

import base64
import logging
import re
import shutil
import subprocess
import sys
from pathlib import Path

_TEMPLATES_DIR = Path(__file__).parent / "templates"

logger = logging.getLogger(__name__)

# Guard against repeated emission of the libgobject install hint when
# _weasyprint_available() is called many times in a single process (e.g.
# fleet exports over multiple boards). The hint is informational, not an
# error — it points the operator to the missing system library.
_LIBGOBJECT_HINT_LOGGED = False


def _log_libgobject_hint(exc: BaseException) -> None:
    """Emit a one-shot, platform-aware install hint for the libgobject failure.

    WeasyPrint depends on native libraries (libgobject, pango, cairo,
    gdk-pixbuf, libffi) that it dlopen's at import time via ``ctypes.CDLL``.
    When any of those libraries is missing from the host, ``import weasyprint``
    raises ``OSError`` (not ``ImportError``). This helper surfaces a single
    actionable warning per process so the operator can install the missing
    system dependencies if they want PDF reports.
    """
    global _LIBGOBJECT_HINT_LOGGED
    if _LIBGOBJECT_HINT_LOGGED:
        return
    _LIBGOBJECT_HINT_LOGGED = True

    if sys.platform == "darwin":
        install_hint = "brew install glib pango cairo gdk-pixbuf libffi"
    elif sys.platform.startswith("linux"):
        install_hint = "apt install libglib2.0-0 libpango-1.0-0 libcairo2 libgdk-pixbuf-2.0-0"
    else:
        install_hint = "install glib/pango/cairo/gdk-pixbuf system libraries for your platform"

    logger.warning(
        "weasyprint unavailable: missing system library (likely libgobject). "
        "PDF report rendering will be skipped. To enable: %s. "
        "Underlying error: %s",
        install_hint,
        exc,
    )


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

    # Strip YAML front matter (used by pandoc) and extract metadata
    markdown_content, metadata = _strip_yaml_front_matter(markdown_content)

    # Replace LaTeX page breaks with HTML markers
    markdown_content = markdown_content.replace("\\newpage", "<!-- page-break -->")

    html_body = md_lib.markdown(
        markdown_content,
        extensions=["tables", "fenced_code", "toc"],
    )

    # Convert page-break markers to CSS page-break divs
    html_body = html_body.replace(
        "<p><!-- page-break --></p>",
        '<div class="page-break"></div>',
    )
    # Handle case where comment isn't wrapped in <p>
    html_body = html_body.replace(
        "<!-- page-break -->",
        '<div class="page-break"></div>',
    )

    css = _load_css()

    if figures_dir is not None:
        figures_dir = Path(figures_dir)
        html_body = _embed_images(html_body, figures_dir)

    # Wrap tables in a scrollable div for responsive layout
    html_body = _wrap_tables(html_body)

    # Prepend title block from YAML metadata
    if metadata:
        title_html = _metadata_to_title_block(metadata)
        html_body = title_html + html_body

    title = metadata.get("title", "KiCad Design Report") if metadata else "KiCad Design Report"
    return _wrap_html(html_body, css, title=title)


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

    try:
        import weasyprint
    except OSError as exc:
        # Defense in depth: if libgobject/pango/cairo became unavailable
        # between the _weasyprint_available() check and this import, surface
        # the same actionable hint as the availability probe and re-raise as
        # ImportError so callers' existing ``except ImportError`` paths
        # degrade gracefully.
        _log_libgobject_hint(exc)
        raise ImportError(
            "PDF output requires weasyprint and its system libraries "
            "(libgobject/pango/cairo/gdk-pixbuf). See warning above for install hint."
        ) from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    weasyprint.HTML(string=html_content).write_pdf(str(output_path))


def render_pdf_pandoc(
    markdown_path: Path | str,
    output_path: Path | str,
    pdf_engine: str = "xelatex",
) -> None:
    """Render a Markdown file to PDF via pandoc and a TeX engine.

    Args:
        markdown_path: Path to the Markdown source file.
        output_path: Destination path for the PDF file.
        pdf_engine: TeX engine to use (xelatex, pdflatex, or lualatex).

    Raises:
        RuntimeError: If pandoc exits with a non-zero status.
        FileNotFoundError: If pandoc is not installed.
    """
    markdown_path = Path(markdown_path)
    output_path = Path(output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)

    cmd = [
        "pandoc",
        str(markdown_path),
        "-o",
        str(output_path),
        f"--pdf-engine={pdf_engine}",
        "--from=markdown+yaml_metadata_block+pipe_tables+raw_tex",
        "--variable=geometry:margin=1in",
        "--variable=colorlinks:true",
    ]

    result = subprocess.run(cmd, capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"pandoc failed (exit {result.returncode}): {result.stderr}")


def _pandoc_available() -> bool:
    """Check whether pandoc is installed and accessible."""
    return shutil.which("pandoc") is not None


def _weasyprint_available() -> bool:
    """Check whether weasyprint can be imported AND its native deps load.

    WeasyPrint dlopen's libgobject/pango/cairo/gdk-pixbuf at import time. On
    hosts where those system libraries are missing (common on macOS without
    Homebrew installs, or on stripped-down Linux containers), the ``import
    weasyprint`` statement raises ``OSError`` from ``ctypes.CDLL`` rather than
    ``ImportError``. Both modes mean the renderer is unavailable, so we
    catch them together and degrade gracefully to pandoc or to the Markdown
    report alone (see :func:`pdf_renderer_available`).
    """
    try:
        import weasyprint  # noqa: F401

        return True
    except ImportError:
        return False
    except OSError as exc:
        _log_libgobject_hint(exc)
        return False


def pdf_renderer_available() -> str | None:
    """Return the name of the best available PDF renderer, or None.

    Checks weasyprint first (higher-quality output), then pandoc+TeX.

    Returns:
        ``"weasyprint"``, ``"pandoc"``, or ``None``.
    """
    if _weasyprint_available():
        return "weasyprint"
    if _pandoc_available():
        return "pandoc"
    return None


def _strip_yaml_front_matter(content: str) -> tuple[str, dict]:
    """Strip YAML front matter from Markdown content.

    Returns the content without the front matter block and a dict of
    parsed metadata fields.  Only simple ``key: value`` pairs are
    extracted; nested structures are ignored.
    """
    if not content.startswith("---"):
        return content, {}

    # Find closing ---
    end = content.find("\n---", 3)
    if end == -1:
        return content, {}

    front_matter = content[3:end].strip()
    body = content[end + 4 :]  # skip past "\n---"

    metadata: dict[str, str] = {}
    for line in front_matter.splitlines():
        line = line.strip()
        if ":" in line and not line.startswith("-") and not line.startswith("#"):
            key, _, value = line.partition(":")
            value = value.strip().strip('"').strip("'")
            if value:
                metadata[key.strip()] = value

    return body, metadata


def _metadata_to_title_block(metadata: dict) -> str:
    """Convert YAML metadata to an HTML title block."""
    parts = []
    parts.append('<div class="cover-block">')
    if "title" in metadata:
        parts.append(f"<h1>{metadata['title']}</h1>")
    if "subtitle" in metadata:
        parts.append(f'<p class="cover-meta">{metadata["subtitle"]}</p>')
    if "date" in metadata:
        parts.append(f'<p class="cover-meta">{metadata["date"]}</p>')
    if "author" in metadata:
        parts.append(f'<p class="cover-meta">{metadata["author"]}</p>')
    parts.append("</div>")
    return "\n".join(parts)


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


def _wrap_html(body: str, css: str, title: str = "KiCad Design Report") -> str:
    """Wrap an HTML body fragment in a complete HTML5 document.

    The CSS is embedded inline in a ``<style>`` tag so the output is
    a single self-contained file.

    Args:
        body: HTML body content.
        css: CSS stylesheet content.
        title: HTML ``<title>`` text.

    Returns:
        Complete HTML5 document string.
    """
    return f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="generator" content="kicad-tools report renderer">
<title>{title}</title>
<style>
{css}
</style>
</head>
<body>
{body}
</body>
</html>"""


def render_interactive_html(
    pcb_path: Path,
    drc_violations: list[dict] | None = None,
    project_name: str | None = None,
    date: str | None = None,
) -> str:
    """Generate a self-contained interactive HTML report.

    Delegates to :func:`kicad_tools.report.interactive.render_interactive_html`.
    See that function for full documentation.

    Args:
        pcb_path: Path to ``.kicad_pcb`` file.
        drc_violations: Optional pre-computed DRC violations.
        project_name: Project display name.
        date: Report date string.

    Returns:
        Complete HTML document string.
    """
    from kicad_tools.report.interactive import (
        render_interactive_html as _render,
    )

    return _render(
        pcb_path=pcb_path,
        drc_violations=drc_violations,
        project_name=project_name,
        date=date,
    )
