"""
LCSC/JLCPCB datasheet source.

Uses the existing LCSCClient to fetch datasheet URLs from JLCPCB's API.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING
from urllib.parse import urljoin, urlparse

from ..models import DatasheetResult
from ..utils import calculate_part_confidence
from .base import HTTPDatasheetSource, requires_requests

if TYPE_CHECKING:
    pass

logger = logging.getLogger(__name__)

# Complete URL tokens (absolute or protocol-relative) inside an HTML/JSON
# body. The *whole* token is captured — query string included — and the
# ".pdf" filter is applied afterwards on the parsed path, so a URL that
# merely contains ".pdf" mid-path (e.g. a ``/cover.pdf.png`` thumbnail) is
# never truncated into a phantom ``.pdf`` link.
_URL_TOKEN_RE = re.compile(r"""(?:https?:)?//[^\s"'<>\\]+""", re.IGNORECASE)

# Hosts LCSC serves the real datasheet files from, most preferred first.
# ``datasheet.lcsc.com`` is what the real viewer page links to (measured on the
# captured page for C47787); ``wmsc.lcsc.com`` is a secondary LCSC file host.
_LCSC_FILE_HOSTS = ("datasheet.lcsc.com", "wmsc.lcsc.com")


def _resolve_lcsc_pdf_url(html: str, base_url: str) -> str | None:
    """
    Find the real PDF URL inside an LCSC datasheet viewer page.

    ``www.lcsc.com/datasheet/...`` answers with an HTML viewer page; the actual
    file lives on ``datasheet.lcsc.com/datasheet/pdf/...`` and is referenced
    from the page (possibly inside embedded JSON with ``\\/`` or ``\\u002F``
    escapes). The same page also links LCSC's pdf.js viewer
    (``.../viewer.html?file=<encoded .pdf URL>``), whose ``.pdf`` sits in the
    query string; only URLs whose *path* ends in ``.pdf`` are candidates, so
    that decoy is ignored regardless of where it appears in the markup. URLs
    are extracted as complete tokens before that check, so a ``/cover.pdf.png``
    thumbnail cannot be cut short at its first ``.pdf``.

    Args:
        html: Decoded body of the viewer page.
        base_url: URL the page was fetched from (excluded from the results and
            used to resolve protocol-relative links).

    Returns:
        The part's PDF URL on an LCSC file host (preferring
        ``datasheet.lcsc.com``), or ``None`` when the page carries no such
        product-specific reference — including when it only links third-party
        PDFs (footer links, related products), so the caller falls through to
        its next candidate instead of caching an unrelated file.
    """
    text = html.replace("\\u002F", "/").replace("\\/", "/").replace("&amp;", "&")
    candidates: list[str] = []
    for match in _URL_TOKEN_RE.findall(text):
        # Trim trailing prose punctuation so a sentence-ending "." or closing
        # ")" cannot break the path check below.
        url = urljoin(base_url, match.rstrip(".,;:!?)"))
        if (
            url != base_url
            and url not in candidates
            and urlparse(url).path.lower().endswith(".pdf")
        ):
            candidates.append(url)
    for host in _LCSC_FILE_HOSTS:
        for url in candidates:
            if urlparse(url).hostname == host:
                return url
    # No LCSC file-host link: the page names no product-specific datasheet
    # (the live viewer's previewPdfUrl and datasheet iframe point at these
    # hosts). Any other PDF link is not evidence of *this* part's datasheet,
    # so return None instead of handing back the first unrelated file.
    return None


class LCSCDatasheetSource(HTTPDatasheetSource):
    """
    Datasheet source using LCSC/JLCPCB API.

    Wraps the existing LCSCClient to search for parts and extract datasheet URLs.

    Example::

        source = LCSCDatasheetSource()
        results = source.search("STM32F103C8T6")
        for result in results:
            print(f"{result.part_number}: {result.datasheet_url}")
    """

    def __init__(self, timeout: float = 30.0):
        """
        Initialize the LCSC datasheet source.

        Args:
            timeout: Request timeout in seconds
        """
        super().__init__(timeout=timeout)

    @property
    def name(self) -> str:
        return "lcsc"

    def _get_default_headers(self) -> dict[str, str]:
        """Get default HTTP headers for LCSC requests."""
        return {
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "en-US,en;q=0.9",
            "Content-Type": "application/json",
            "Origin": "https://jlcpcb.com",
            "Referer": "https://jlcpcb.com/parts",
            "User-Agent": (
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
        }

    @requires_requests
    def search(self, part_number: str) -> list[DatasheetResult]:
        """
        Search LCSC for datasheets matching the part number.

        Args:
            part_number: Part number to search for

        Returns:
            List of DatasheetResult objects with datasheet URLs
        """
        from kicad_tools.parts.lcsc import LCSCClient

        results = []
        client = LCSCClient(use_cache=True, timeout=self.timeout)

        try:
            # Try exact LCSC part lookup first if it looks like an LCSC part
            if part_number.upper().startswith("C") and part_number[1:].isdigit():
                part = client.lookup(part_number)
                if part and part.datasheet_url:
                    results.append(
                        DatasheetResult(
                            part_number=part.mfr_part or part.lcsc_part,
                            manufacturer=part.manufacturer,
                            description=part.description,
                            datasheet_url=part.datasheet_url,
                            source=self.name,
                            confidence=1.0,
                        )
                    )
                    return results

            # Search for the part
            search_result = client.search(part_number, page_size=10)
            for part in search_result.parts:
                if part.datasheet_url:
                    confidence = calculate_part_confidence(part_number, part.mfr_part or "")

                    results.append(
                        DatasheetResult(
                            part_number=part.mfr_part or part.lcsc_part,
                            manufacturer=part.manufacturer,
                            description=part.description,
                            datasheet_url=part.datasheet_url,
                            source=self.name,
                            confidence=confidence,
                        )
                    )

        except Exception as e:
            logger.warning(f"LCSC search failed for '{part_number}': {e}")

        return results

    @requires_requests
    def download(self, result: DatasheetResult, output_path: Path) -> Path:
        """
        Download a datasheet, resolving LCSC's HTML viewer page if needed.

        LCSC's ``www.lcsc.com/datasheet/...`` URLs often answer HTTP 200 with an
        HTML viewer page instead of the PDF. When that happens the page is
        scanned for the embedded real file URL (``datasheet.lcsc.com/...``) and the
        download is retried once with it. Every written file is still gated by
        the PDF magic-byte check.

        Raises:
            DatasheetDownloadError: If the PDF cannot be obtained. The message
                always names the URL(s) tried so the user can open them by hand.
        """
        import requests

        from ..exceptions import DatasheetDownloadError

        url = result.datasheet_url
        try:
            return super().download(result, output_path)
        except DatasheetDownloadError as first_error:
            # Only a non-PDF payload (HTML viewer page) is worth resolving;
            # network/HTTP errors are surfaced as-is.
            if "non-PDF content" not in str(first_error):
                raise

            try:
                response = self._get_session().get(url, timeout=self.timeout, allow_redirects=True)
                response.raise_for_status()
                body = response.content
                html = body.decode(response.encoding or "utf-8", errors="replace")
            except (requests.RequestException, LookupError) as e:
                raise DatasheetDownloadError(
                    f"{first_error}; could not re-fetch the page to find the PDF link ({e}). "
                    f"Open {url} in a browser to get the datasheet by hand"
                ) from e

            resolved = _resolve_lcsc_pdf_url(html, url)
            if resolved is None:
                raise DatasheetDownloadError(
                    f"{first_error}; no PDF link found on the page. "
                    f"Open {url} in a browser to get the datasheet by hand"
                ) from first_error

            logger.info("LCSC viewer page %s resolved to %s", url, resolved)
            try:
                return self._download_file(self._get_session(), resolved, output_path, self.timeout)
            except DatasheetDownloadError as retry_error:
                raise DatasheetDownloadError(
                    f"{first_error}; resolved PDF link {resolved} also failed: {retry_error}. "
                    f"Open {url} or {resolved} in a browser to get the datasheet by hand"
                ) from retry_error
