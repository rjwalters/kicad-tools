"""
LCSC/JLCPCB parts client.

Fetches parts data from JLCPCB/LCSC for assembly service integration.
Uses the same API endpoints as the KiCad JLCPCB Plugin.

Includes rate limiting and exponential backoff to handle API rate limits.
"""

from __future__ import annotations

import logging
import random
import re
import time
from datetime import datetime
from pathlib import Path
from threading import Lock
from typing import TYPE_CHECKING, Literal

from .cache import PartsCache
from .models import (
    BOMAvailability,
    PackageType,
    Part,
    PartAvailability,
    PartCategory,
    PartPrice,
    SearchResult,
)

if TYPE_CHECKING:
    from ..schema.bom import BOMItem
    from .jlcparts_catalog import JlcpartsCatalog
    from .jlcpcb_api import JLCOpenAPIClient

logger = logging.getLogger(__name__)


# Canonical install hint for the optional ``parts`` extra.  Mirrors the
# ``CMAES_INSTALL_HINT`` convention established for the ``placement`` extra
# (issue #4100 / PR #4111): name the declared extra and give both the
# ``uv sync`` and ``pip install`` incantations so a base-install failure is
# actionable rather than a silent degrade.  Unlike ``cmaes``, ``parts`` is
# NOT pulled in transitively by the ``dev`` or ``all`` extras.
# Base install incantation shared by every parts-extra error.  Kept separate so
# download-context callers (``sync-catalog``) can surface *only* the actionable
# install advice without the self-referential "run sync-catalog" clause below
# (issue #4295 -- that command is the very one failing on missing ``requests``).
PARTS_EXTRA_INSTALL_HINT = (
    "The 'requests' library is required for LCSC API access. "
    "Install it with the 'parts' extra: uv sync --extra parts "
    '(or: pip install "kicad-tools[parts]").'
)

# General hint for lookup/search, where the offline jlcparts catalog *is* a
# valid fallback, so pointing the user at ``kct parts sync-catalog`` is helpful.
PARTS_INSTALL_HINT = (
    PARTS_EXTRA_INSTALL_HINT + " Alternatively, sync the offline jlcparts catalog "
    "with `kct parts sync-catalog` to look up parts without the live API."
)

# Hint for the *downloader* itself (``sync_catalog``).  ``requests`` is required
# unconditionally to run it, so the offline-catalog suggestion would be circular
# (issue #4295): it cannot recommend the command that is currently failing.
PARTS_DOWNLOAD_INSTALL_HINT = PARTS_EXTRA_INSTALL_HINT


class LCSCForbiddenError(Exception):
    """Raised when the JLCPCB API returns 403 Forbidden.

    This indicates the API is globally unavailable (e.g. authentication
    required, geo-blocking) and further requests should not be attempted.
    """


class LCSCDependencyMissingError(ImportError):
    """Raised when the optional ``parts`` extra (``requests``) is absent.

    This is a *capability* failure -- the requested LCSC matcher cannot run
    at all -- and is deliberately distinct from a per-part "no match found"
    result.  Callers (BOM enrichment, ``kct export``) short-circuit on the
    first occurrence and surface it as a hard, actionable failure instead of
    degrading silently to an empty ``LCSC Part #`` column (issue #4104).

    Subclasses :class:`ImportError` so that call sites which historically
    caught / expected the bare ``ImportError`` (e.g. the live-API-only
    ``lookup`` no-fallback path, issue #4108) keep working, while newer
    callers can catch the more specific type. ``suggest_for_component``'s
    ``isinstance(e, ImportError)`` promotion therefore still fires whether the
    underlying layer raised a bare ``ImportError`` or this subclass.

    The message is the canonical :data:`PARTS_INSTALL_HINT`, which now also
    points at the offline ``kct parts sync-catalog`` alternative (issue #4108).
    """


class RateLimiter:
    """
    Thread-safe rate limiter that enforces a minimum interval between requests.

    Uses a simple token bucket algorithm with a single token and configurable
    refill interval.
    """

    def __init__(self, min_interval: float = 1.0):
        """
        Initialize the rate limiter.

        Args:
            min_interval: Minimum seconds between requests (default: 1.0)
        """
        self.min_interval = min_interval
        self._last_request_time: float = 0.0
        self._lock = Lock()

    def wait(self) -> None:
        """Block until a request is allowed under the rate limit."""
        with self._lock:
            now = time.monotonic()
            elapsed = now - self._last_request_time
            if elapsed < self.min_interval:
                sleep_time = self.min_interval - elapsed
                logger.debug(f"Rate limiter sleeping {sleep_time:.2f}s")
                time.sleep(sleep_time)
            self._last_request_time = time.monotonic()


# JLCPCB API endpoints (same as KiCad JLCPCB Plugin)
JLCPCB_API_BASE = "https://jlcpcb.com/api"
LCSC_API_BASE = "https://wmsc.lcsc.com/ftps/wm"

# Part lookup endpoint
PART_LOOKUP_URL = (
    f"{JLCPCB_API_BASE}/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentDetail"
)

# Search endpoint
SEARCH_URL = f"{JLCPCB_API_BASE}/overseas-pcb-order/v1/shoppingCart/smtGood/selectSmtComponentList"


def _requests_installed() -> bool:
    """Return True if the optional ``requests`` dependency is importable."""
    import importlib.util

    return importlib.util.find_spec("requests") is not None


class _RequestsUnavailableSentinel(BaseException):
    """Stable sentinel exception used when the ``requests`` extra is absent.

    Returned by :func:`_request_exception_type` so an ``except`` clause that
    references ``requests.RequestException`` degrades to a class that (a) is
    stable across calls (so tests can construct and raise it) and (b) is never
    raised by the real code path when ``requests`` is genuinely missing (that
    path is handled up front).
    """


def _request_exception_type() -> type[BaseException]:
    """Return ``requests.RequestException`` if importable, else a sentinel.

    Callers use this in an ``except`` clause so a network failure is caught
    without a hard top-level ``requests`` import. When the ``requests`` extra is
    absent (or a test stubs ``_requests_installed`` without installing the real
    module), the returned sentinel is a *stable* module-level class so the
    ``except`` reference and any test that raises it agree on identity.
    """
    try:
        import requests  # type: ignore[import-untyped]
    except ImportError:
        return _RequestsUnavailableSentinel
    # ``requests`` ships no type stubs here, so ``RequestException`` is ``Any``;
    # the explicit annotation keeps callers' ``except`` type-safe.
    exc_type: type[BaseException] = requests.RequestException
    return exc_type


def _requires_requests(func):
    """Decorator to check if requests is available."""

    def wrapper(*args, **kwargs):
        if not _requests_installed():
            raise LCSCDependencyMissingError(PARTS_INSTALL_HINT)
        return func(*args, **kwargs)

    return wrapper


def _categorize_part(description: str, package: str) -> PartCategory:
    """Guess category from description and package."""
    desc_lower = description.lower()

    # Check crystal/resonator before resistor since "resonator" contains " res"
    if any(x in desc_lower for x in ["crystal", "oscillator", "resonator"]):
        return PartCategory.CRYSTAL
    if any(x in desc_lower for x in ["resistor", "res ", " res", "ohm"]):
        return PartCategory.RESISTOR
    if any(x in desc_lower for x in ["capacitor", "cap ", " cap", "farad", "mlcc"]):
        return PartCategory.CAPACITOR
    if any(x in desc_lower for x in ["inductor", "ind ", " ind", "henry", "choke"]):
        return PartCategory.INDUCTOR
    if any(x in desc_lower for x in ["diode", "rectifier", "schottky", "zener"]):
        return PartCategory.DIODE
    if any(x in desc_lower for x in ["transistor", "mosfet", "bjt", "jfet"]):
        return PartCategory.TRANSISTOR
    if any(
        x in desc_lower
        for x in ["mcu", "microcontroller", "op amp", "opamp", "regulator", "eeprom", "flash"]
    ):
        return PartCategory.IC
    if any(x in desc_lower for x in ["connector", "header", "socket", "jack", "plug"]):
        return PartCategory.CONNECTOR
    if any(x in desc_lower for x in ["led", "light emitting"]):
        return PartCategory.LED
    if any(x in desc_lower for x in ["switch", "button", "tactile"]):
        return PartCategory.SWITCH
    if any(x in desc_lower for x in ["relay"]):
        return PartCategory.RELAY
    if any(x in desc_lower for x in ["fuse", "ptc", "polyfuse"]):
        return PartCategory.FUSE

    return PartCategory.OTHER


def _guess_package_type(package: str) -> PackageType:
    """Guess package type (SMD vs through-hole)."""
    package_lower = package.lower()

    # Common SMD packages
    smd_patterns = [
        r"^\d{4}$",  # 0402, 0603, 0805, etc.
        r"^\d{4}_\d{4}",  # 0402_1005Metric
        r"^smd",
        r"^smt",
        r"^soic",
        r"^sop",
        r"^ssop",
        r"^tssop",
        r"^qfp",
        r"^lqfp",
        r"^tqfp",
        r"^qfn",
        r"^dfn",
        r"^bga",
        r"^sot",
        r"^sc-",
        r"^to-252",
        r"^to-263",
        r"^dpak",
        r"^d2pak",
    ]

    for pattern in smd_patterns:
        if re.search(pattern, package_lower):
            return PackageType.SMD

    # Common through-hole packages
    th_patterns = [
        r"^dip",
        r"^pdip",
        r"^to-92",
        r"^to-220",
        r"^to-247",
        r"^axial",
        r"^radial",
        r"^through",
    ]

    for pattern in th_patterns:
        if re.search(pattern, package_lower):
            return PackageType.THROUGH_HOLE

    return PackageType.UNKNOWN


class LCSCClient:
    """
    Client for LCSC/JLCPCB parts API.

    Provides methods for looking up parts by LCSC number and searching
    the parts catalog. Results are cached locally to reduce API calls.

    Example::

        client = LCSCClient()

        # Single part lookup
        part = client.lookup("C123456")
        if part:
            print(f"{part.mfr_part}: ${part.best_price:.4f}")

        # Search for parts
        results = client.search("100nF 0402")
        for part in results:
            print(f"{part.lcsc_part}: {part.description}")

        # Check BOM availability
        from kicad_tools.schema.bom import extract_bom
        bom = extract_bom("project.kicad_sch")
        availability = client.check_bom(bom.items)

    Note:
        Requires the `requests` library. Install with::

            pip install kicad-tools[parts]
    """

    # Default headers matching browser requests
    DEFAULT_HEADERS = {
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

    def __init__(
        self,
        cache: PartsCache | None = None,
        use_cache: bool = True,
        timeout: float = 30.0,
        rate_limit: float = 3.0,
        max_retries: int = 3,
        base_retry_delay: float = 2.0,
        use_local_catalog: bool = True,
        catalog_path: Path | None = None,
        use_official_api: bool = True,
    ):
        """
        Initialize the client.

        Args:
            cache: Custom cache instance (default: creates new PartsCache)
            use_cache: Whether to use caching (default: True)
            timeout: Request timeout in seconds
            rate_limit: Minimum seconds between API requests (default: 3.0)
                Set to 0 to disable rate limiting.
            max_retries: Maximum retry attempts on rate limit errors (default: 3)
            base_retry_delay: Initial delay in seconds before retry (default: 2.0)
            use_local_catalog: Whether to fall back to the offline jlcparts
                catalog (``kct parts sync-catalog``) when the live API is
                unavailable. If the catalog file does not exist this is a
                silent no-op, so the live-API path is byte-for-byte unchanged
                when no catalog has been synced (default: True).
            catalog_path: Override path to the local jlcparts SQLite catalog
                (default: ``~/.cache/kicad-tools/jlcparts.sqlite3``).
            use_official_api: Whether to consult the official JLCPCB
                open-platform API (BYO key) ahead of the anonymous scrape API
                for ``lookup()``/``lookup_many()``. This tier only activates
                when all three of ``JLCPCB_APP_ID``/``JLCPCB_ACCESS_KEY``/
                ``JLCPCB_SECRET_KEY`` are set in the environment; without keys
                it is a silent no-op, so keyless behavior is byte-for-byte
                unchanged (default: True). See issue #4118.
        """
        self.cache = cache if cache is not None else PartsCache() if use_cache else None
        self.timeout = timeout
        self._session = None
        self._rate_limiter = RateLimiter(rate_limit) if rate_limit > 0 else None
        self._max_retries = max_retries
        self._base_retry_delay = base_retry_delay
        self._api_forbidden = False
        self._use_local_catalog = use_local_catalog
        self._catalog_path = catalog_path
        self._catalog: JlcpartsCatalog | None = None
        self._use_official_api = use_official_api
        # Resolved lazily on first lookup. ``None`` = not yet probed;
        # ``False`` (the sentinel) = probed, no keys/deps; otherwise the client.
        self._official_client: JLCOpenAPIClient | Literal[False] | None = None

    def _get_catalog(self) -> JlcpartsCatalog | None:
        """Return the lazily-constructed offline catalog reader, or None.

        Returns ``None`` when the offline fallback is disabled. The reader
        itself is a silent no-op when the backing catalog file is absent, so
        constructing it has no effect on the live-API path.
        """
        if not self._use_local_catalog:
            return None
        if self._catalog is None:
            from .jlcparts_catalog import JlcpartsCatalog

            self._catalog = JlcpartsCatalog(self._catalog_path)
        return self._catalog

    def _get_official_client(self) -> JLCOpenAPIClient | None:
        """Return the official JLCPCB open-platform client, or ``None``.

        Returns ``None`` (silently, not an error) when the official-API tier is
        disabled, the ``requests`` extra is absent, or the three credential env
        vars are not all present -- in which case ``lookup()``/``lookup_many()``
        fall through to the anonymous scrape API and the offline catalog exactly
        as they did without keys. The result is memoized so the environment /
        credential check runs at most once per client. See issue #4118.
        """
        if not self._use_official_api:
            return None
        if self._official_client is None:
            # Not yet resolved -- probe env + deps exactly once.
            self._official_client = self._build_official_client() or False
        if self._official_client is False:
            return None
        return self._official_client

    def _build_official_client(self) -> JLCOpenAPIClient | None:
        """Construct the official client if keys + ``requests`` are available."""
        if not _requests_installed():
            return None
        from .jlcpcb_api import JLCCredentials, JLCOpenAPIClient

        credentials = JLCCredentials.from_env()
        if credentials is None:
            # Missing / partial keys == keyless. Not an error.
            return None
        return JLCOpenAPIClient(credentials, timeout=self.timeout)

    def _get_session(self):
        """Get or create requests session."""
        if self._session is None:
            import requests

            self._session = requests.Session()
            self._session.headers.update(self.DEFAULT_HEADERS)
        return self._session

    def _make_request(self, url: str, payload: dict) -> dict | None:
        """
        Make a rate-limited request with automatic retry on transient errors.

        Args:
            url: API endpoint URL
            payload: JSON payload to send

        Returns:
            Response JSON data, or None if the request fails after all retries

        Raises:
            LCSCForbiddenError: If the API returns 403 Forbidden (circuit breaker).
        """
        import requests

        # Circuit breaker: if a previous request got 403, skip immediately
        if self._api_forbidden:
            raise LCSCForbiddenError(
                "JLCPCB API returned 403 Forbidden -- skipping request (circuit breaker active)"
            )

        session = self._get_session()

        # Apply rate limiting before making the request
        if self._rate_limiter:
            self._rate_limiter.wait()

        last_exception: Exception | None = None

        for attempt in range(self._max_retries + 1):
            try:
                response = session.post(url, json=payload, timeout=self.timeout)
                response.raise_for_status()
                return response.json()
            except requests.HTTPError as e:
                status_code = e.response.status_code if e.response is not None else 0

                # 403 is not transient -- trip the circuit breaker and raise
                if status_code == 403:
                    self._api_forbidden = True
                    raise LCSCForbiddenError(
                        "JLCPCB API returned 403 Forbidden -- "
                        "API may require authentication or be geo-blocked"
                    ) from e

                # Only retry on rate limit and server errors
                if status_code not in (429, 500, 502, 503, 504):
                    raise

                last_exception = e

                if attempt == self._max_retries:
                    logger.warning(
                        f"Max retries ({self._max_retries}) reached, last status: {status_code}"
                    )
                    raise

                # Calculate delay with exponential backoff
                delay = min(
                    self._base_retry_delay * (2**attempt),
                    60.0,  # Max 60 second delay
                )

                # Add jitter (±25% randomization) to prevent thundering herd
                delay = delay * (0.75 + random.random() * 0.5)

                logger.info(
                    f"Request failed with {status_code}, retrying in {delay:.1f}s "
                    f"(attempt {attempt + 1}/{self._max_retries})"
                )
                time.sleep(delay)

            except requests.RequestException as e:
                # For connection errors, also retry
                last_exception = e

                if attempt == self._max_retries:
                    logger.warning(f"Max retries ({self._max_retries}) reached: {e}")
                    raise

                delay = min(self._base_retry_delay * (2**attempt), 60.0)
                delay = delay * (0.75 + random.random() * 0.5)

                logger.info(
                    f"Request failed ({e}), retrying in {delay:.1f}s "
                    f"(attempt {attempt + 1}/{self._max_retries})"
                )
                time.sleep(delay)

        # Should not reach here, but handle edge case
        if last_exception:
            raise last_exception
        return None

    def lookup(self, lcsc_part: str, bypass_cache: bool = False) -> Part | None:
        """
        Look up a single part by LCSC number.

        Resolution order: local response cache -> official JLCPCB open-platform
        API (only when BYO keys are present, issue #4118) -> live JLCPCB scrape
        API -> offline jlcparts catalog. The official tier is a silent no-op
        without the three credential env vars, so the keyless path is
        byte-for-byte unchanged. The catalog is only consulted when the higher
        tiers are unavailable (403 circuit breaker, network error, or the
        ``requests`` extra is not installed) and a synced catalog exists.

        Args:
            lcsc_part: LCSC part number (e.g., "C123456")
            bypass_cache: If True, always fetch from API

        Returns:
            Part if found, None otherwise

        Raises:
            LCSCDependencyMissingError: If the ``requests`` extra is missing
                *and* no offline catalog is available. Subclasses
                ``ImportError``, so callers that historically caught the bare
                ``ImportError`` (no offline fallback configured) still work.
        """
        # Normalize part number
        lcsc_part = lcsc_part.upper()
        if not lcsc_part.startswith("C"):
            lcsc_part = f"C{lcsc_part}"

        # Check cache first
        if self.cache and not bypass_cache:
            cached = self.cache.get(lcsc_part)
            if cached:
                logger.debug(f"Cache hit for {lcsc_part}")
                return cached

        catalog = self._get_catalog()

        # Tier 0: official JLCPCB open-platform API (only when BYO keys are
        # present -- see issue #4118). Silent no-op without keys/deps, so the
        # keyless path below is byte-for-byte unchanged.
        official = self._get_official_client()
        if official is not None:
            try:
                found = official.get_component_detail_by_codes([lcsc_part])
            except Exception as e:
                # Auth/quota/whitelist/network failure -- log the actionable
                # message and fall through to the anonymous / offline tiers.
                logger.warning(f"Official JLCPCB API lookup failed for {lcsc_part}: {e}")
            else:
                official_part = found.get(lcsc_part.upper())
                if official_part is not None:
                    if self.cache:
                        self.cache.put(official_part)
                    return official_part

        # If requests is unavailable, the live API cannot be reached. Preserve
        # the historical (Import)Error only when there is no offline fallback.
        if not _requests_installed():
            if catalog is None or not catalog.available:
                raise LCSCDependencyMissingError(PARTS_INSTALL_HINT)
        else:
            # Fetch from live API (authoritative for freshness/pricing).
            part: Part | None = None
            try:
                part = self._fetch_part(lcsc_part)
            except Exception as e:
                # 403 circuit breaker (LCSCForbiddenError) or other API failure
                # -- fall through to the offline catalog rather than giving up.
                logger.warning(f"Live API lookup failed for {lcsc_part}: {e}")

            if part is not None:
                if self.cache:
                    self.cache.put(part)
                return part

        # Live API unavailable / miss -- try the offline jlcparts catalog.
        if catalog is not None:
            catalog_part = catalog.lookup(lcsc_part)
            if catalog_part is not None:
                logger.debug(f"Offline catalog hit for {lcsc_part}")
                if self.cache:
                    self.cache.put(catalog_part)
                return catalog_part

        return None

    def _fetch_part(
        self,
        lcsc_part: str,
        _failures: list[tuple[str, str]] | None = None,
    ) -> Part | None:
        """Fetch part from JLCPCB API.

        Args:
            lcsc_part: LCSC part number to fetch.
            _failures: Optional collector for deferred live-API failures. When a
                list is supplied, a transport-level failure (e.g. a 404 from a
                drifted endpoint) appends ``(part, reason)`` here instead of
                emitting a per-part warning, so the caller can summarize the
                situation once after the offline-catalog fallback runs. The log
                is demoted to ``debug`` because the failure is transparently
                handled by that fallback (see issue #4299).
        """
        import requests

        payload = {"componentCode": lcsc_part}

        try:
            data = self._make_request(PART_LOOKUP_URL, payload)
        except requests.RequestException as e:
            logger.debug(f"API request failed for {lcsc_part}: {e}")
            if _failures is not None:
                _failures.append((lcsc_part, str(e)))
            return None

        if data is None:
            return None

        # Check for success
        if data.get("code") != 200:
            logger.debug(f"API returned error for {lcsc_part}: {data.get('message')}")
            return None

        component = data.get("data")
        if not component:
            return None

        return self._parse_component(component)

    def _parse_component(self, data: dict) -> Part:
        """Parse component data from API response."""
        # Extract price breaks
        prices = []
        price_list = data.get("prices") or data.get("priceList") or []
        for price_break in price_list:
            qty = price_break.get("startNumber", 0)
            unit_price = price_break.get("productPrice", 0)
            if qty > 0 and unit_price > 0:
                prices.append(PartPrice(quantity=qty, unit_price=unit_price))

        # Sort by quantity
        prices.sort(key=lambda p: p.quantity)

        # Get package info
        package = data.get("encapStandard") or data.get("package") or ""
        package_type = _guess_package_type(package)

        # Get description
        description = data.get("componentModelEn") or data.get("describe") or ""

        # Categorize
        category = _categorize_part(description, package)

        return Part(
            lcsc_part=data.get("componentCode", ""),
            mfr_part=data.get("componentModelEn") or data.get("manufacturerPartNumber") or "",
            manufacturer=data.get("componentBrandEn") or data.get("manufacturer") or "",
            description=description,
            category=category,
            package=package,
            package_type=package_type,
            stock=data.get("stockCount", 0),
            min_order=data.get("minOrder", 1),
            prices=prices,
            is_basic=data.get("componentLibraryType") == "base",
            is_preferred=data.get("componentLibraryType") == "preferred",
            datasheet_url=data.get("dataManualUrl") or "",
            product_url=f"https://jlcpcb.com/partdetail/{data.get('componentCode', '')}",
            fetched_at=datetime.now(),
        )

    def search(
        self,
        query: str,
        page: int = 1,
        page_size: int = 20,
        in_stock: bool = False,
        basic_only: bool = False,
        package: str | None = None,
    ) -> SearchResult:
        """
        Search for parts by free-text query (value + optional package).

        Resolution order mirrors :meth:`lookup`: the live JLCPCB scrape API is
        authoritative when reachable; on a 403 circuit breaker
        (:class:`LCSCForbiddenError`) or other network failure, and when a
        synced offline jlcparts catalog is available, results are served from
        the catalog instead of raising / returning empty. This keeps parametric
        matching (``kct parts suggest`` / ``kct export --auto-lcsc``, both of
        which route through :class:`~kicad_tools.cost.suggest.PartMatcher` ->
        ``search``) working offline. When the offline fallback is disabled
        (``use_local_catalog=False``) or no catalog is synced, the historical
        live-API-only behavior is preserved byte-for-byte (403 propagates, other
        request errors return an empty :class:`SearchResult`).

        Args:
            query: Search query string
            page: Page number (1-indexed)
            page_size: Results per page (max 100)
            in_stock: Only return in-stock parts
            basic_only: Only return JLCPCB basic parts
            package: Optional package filter applied to the offline-catalog
                fallback (e.g. ``"0402"``); ignored by the live API path.

        Returns:
            SearchResult with matching parts

        Raises:
            LCSCForbiddenError: If the live API returns 403 *and* no offline
                catalog is available to fall back to (unchanged behavior).
            LCSCDependencyMissingError: If the ``requests`` extra is missing
                *and* no offline catalog is available.
        """
        catalog = self._get_catalog()

        # When requests is unavailable the live API cannot be reached at all.
        # Serve directly from the offline catalog if one is synced; otherwise
        # preserve the historical dependency error.
        if not _requests_installed():
            if catalog is not None and catalog.available:
                return self._catalog_search(query, page, page_size, in_stock, package, catalog)
            raise LCSCDependencyMissingError(PARTS_INSTALL_HINT)

        payload = {
            "keyword": query,
            "pageSize": min(page_size, 100),
            "currentPage": page,
        }

        if in_stock:
            payload["stockCountMin"] = 1

        if basic_only:
            payload["componentLibraryType"] = "base"

        try:
            data = self._make_request(SEARCH_URL, payload)
        except LCSCForbiddenError:
            # 403 circuit breaker -- fall through to the offline catalog rather
            # than propagating the error, when a catalog is available.
            if catalog is not None and catalog.available:
                logger.warning("Live search API 403'd -- falling back to offline catalog")
                return self._catalog_search(query, page, page_size, in_stock, package, catalog)
            raise
        except _request_exception_type() as e:
            logger.error(f"Search request failed: {e}")
            # Other network failure -- fall through to the offline catalog when
            # available, else preserve the historical empty-result behavior.
            if catalog is not None and catalog.available:
                logger.warning("Live search API failed -- falling back to offline catalog")
                return self._catalog_search(query, page, page_size, in_stock, package, catalog)
            return SearchResult(query=query)

        if data is None or data.get("code") != 200:
            message = data.get("message") if data else "Unknown error"
            logger.warning(f"Search API returned error: {message}")
            return SearchResult(query=query)

        result_data = data.get("data") or {}
        # The live JLCPCB API returns the keys present but explicitly null for a
        # no-match query (e.g. "componentPageInfo": {"list": null, "total": 0}).
        # dict.get(key, default) only substitutes the default when the key is
        # ABSENT, not when it is present-but-null, so use `... or {}` / `... or []`
        # to coerce explicit null. A genuine no-match then flows through the
        # existing unmatched path instead of raising 'NoneType' is not iterable.
        page_info = result_data.get("componentPageInfo") or {}
        components = page_info.get("list") or []
        total = page_info.get("total") or 0

        parts = []
        for comp in components:
            try:
                part = self._parse_component(comp)
                parts.append(part)
                # Cache search results
                if self.cache:
                    self.cache.put(part)
            except Exception as e:
                logger.warning(f"Failed to parse component: {e}")
                continue

        return SearchResult(
            query=query,
            parts=parts,
            total_count=total,
            page=page,
            page_size=page_size,
        )

    def _catalog_search(
        self,
        query: str,
        page: int,
        page_size: int,
        in_stock: bool,
        package: str | None,
        catalog: JlcpartsCatalog,
    ) -> SearchResult:
        """Serve a :meth:`search` from the offline jlcparts catalog.

        Wraps :meth:`JlcpartsCatalog.search` and packages the resulting
        candidate pool into a :class:`SearchResult` shaped identically to the
        live-API path so downstream ranking (``PartMatcher``) is unchanged.
        Results are capped at ``page_size * 5`` (matching the live API's
        ``page_size`` cap intent while leaving headroom for the caller's
        confidence re-ranking).
        """
        min_stock = 1 if in_stock else 0
        parts = catalog.search(
            query,
            package=package,
            min_stock=min_stock,
            limit=max(page_size * 5, page_size),
        )
        if self.cache:
            for part in parts:
                self.cache.put(part)
        return SearchResult(
            query=query,
            parts=parts,
            total_count=len(parts),
            page=page,
            page_size=page_size,
        )

    def lookup_many(
        self,
        lcsc_parts: list[str],
        bypass_cache: bool = False,
    ) -> dict[str, Part]:
        """
        Look up multiple parts.

        Uses cache where possible, batch-fetches missing parts from the official
        open-platform API when BYO keys are present, then from the live scrape
        API, then fills any still-missing parts from the offline jlcparts catalog
        (see :meth:`lookup` for the full resolution order).

        Args:
            lcsc_parts: List of LCSC part numbers
            bypass_cache: If True, always fetch from API

        Returns:
            Dict mapping part numbers to Parts

        Raises:
            LCSCDependencyMissingError: If the ``requests`` extra is missing
                *and* no offline catalog is available. Subclasses
                ``ImportError`` for backward compatibility.
        """
        if not lcsc_parts:
            return {}

        # Normalize part numbers
        parts = [p.upper() if p.upper().startswith("C") else f"C{p.upper()}" for p in lcsc_parts]

        result = {}

        # Check cache first
        if self.cache and not bypass_cache:
            cached = self.cache.get_many(parts)
            result.update(cached)
            parts = [p for p in parts if p not in cached]

        catalog = self._get_catalog()

        # Tier 0: official JLCPCB open-platform API (BYO keys, issue #4118).
        # Batch-fetch all still-missing parts in a single signed request. Silent
        # no-op without keys/deps -- keyless behavior is byte-for-byte unchanged.
        official = self._get_official_client()
        if official is not None and parts:
            try:
                found = official.get_component_detail_by_codes(parts)
            except Exception as e:
                logger.warning(f"Official JLCPCB API batch lookup failed: {e}")
            else:
                for part_num in parts:
                    official_part = found.get(part_num.upper())
                    if official_part is not None:
                        result[part_num] = official_part
                        if self.cache:
                            self.cache.put(official_part)
                parts = [p for p in parts if p not in result]

        # Live-API failures are deferred here and summarized once below, rather
        # than logged per-part: when the offline catalog then resolves the part
        # (the common case for a drifted 404 endpoint or a 403 geo-block) the
        # per-part warnings are pure noise (issue #4299).
        live_failures: list[tuple[str, str]] = []

        if not _requests_installed():
            if catalog is None or not catalog.available:
                raise LCSCDependencyMissingError(PARTS_INSTALL_HINT)
        else:
            # Fetch remaining from the live API (authoritative when reachable).
            for i, part_num in enumerate(parts):
                try:
                    part = self._fetch_part(part_num, _failures=live_failures)
                except Exception as e:
                    # 403 circuit breaker or other hard API failure -- stop
                    # hammering the API and fall back to the offline catalog for
                    # the rest. Every remaining part would hit the same wall, so
                    # record them all and defer the log to the summary below.
                    logger.debug(f"Live API lookup failed for {part_num}: {e}")
                    live_failures.extend((p, str(e)) for p in parts[i:])
                    break
                if part:
                    result[part_num] = part
                    if self.cache:
                        self.cache.put(part)

        # Fill any still-missing parts from the offline jlcparts catalog.
        missing = [p for p in parts if p not in result]
        if missing and catalog is not None:
            for part_num, part in catalog.lookup_many(missing).items():
                result[part_num] = part
                if self.cache:
                    self.cache.put(part)

        # Summarize any live-API failures in a single line. If the catalog
        # covered them the failure is transparently handled (one WARNING so the
        # condition is still visible once, not N times); parts resolved by
        # neither the live API nor the catalog stay explicitly visible.
        if live_failures:
            reason = live_failures[0][1]
            failed = [p for p, _ in live_failures]
            resolved = [p for p in failed if p in result]
            unresolved = [p for p in failed if p not in result]
            details: list[str] = []
            if resolved:
                details.append(f"{len(resolved)} resolved from the offline catalog")
            if unresolved:
                preview = ", ".join(sorted(unresolved)[:10])
                details.append(f"{len(unresolved)} unresolved ({preview})")
            logger.warning(f"Live JLC API unavailable ({reason}); " + "; ".join(details))

        return result

    def check_bom(
        self,
        items: list[BOMItem],
        bypass_cache: bool = False,
    ) -> BOMAvailability:
        """
        Check availability for BOM items.

        Args:
            items: List of BOM items (must have .lcsc field)
            bypass_cache: If True, always fetch from API

        Returns:
            BOMAvailability with check results
        """
        # Collect LCSC part numbers from BOM
        lcsc_parts = []
        for item in items:
            if hasattr(item, "lcsc") and item.lcsc:
                lcsc_parts.append(item.lcsc)

        # Fetch all parts
        parts_map = self.lookup_many(list(set(lcsc_parts)), bypass_cache=bypass_cache)

        # Build availability results
        results = []
        for item in items:
            lcsc = getattr(item, "lcsc", None) or ""

            avail = PartAvailability(
                reference=item.reference,
                value=item.value,
                footprint=item.footprint,
                lcsc_part=lcsc,
                quantity_needed=item.quantity,
            )

            if not lcsc:
                avail.error = "No LCSC part number"
            elif lcsc.upper() in parts_map:
                part = parts_map[lcsc.upper()]
                avail.part = part
                avail.matched = True
                avail.in_stock = part.in_stock
                avail.quantity_available = part.stock
            else:
                avail.error = "Part not found"

            results.append(avail)

        return BOMAvailability(
            items=results,
            checked_at=datetime.now(),
        )

    def close(self) -> None:
        """Close the HTTP session(s)."""
        if self._session:
            self._session.close()
            self._session = None
        if self._official_client and self._official_client is not False:
            self._official_client.close()
            self._official_client = None

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()
        return False
