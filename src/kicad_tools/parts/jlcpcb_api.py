"""
Official JLCPCB open-platform (BYO-key) parts client.

This module speaks the *authenticated* JLCPCB open-platform REST API directly.
Users bring their own credentials (registered at the JLCPCB developer portal);
kicad-tools ships only the signed client. When the three credential env vars
are absent the feature is entirely inert -- :class:`JLCOpenAPIClient` is never
constructed and :class:`~kicad_tools.parts.lcsc.LCSCClient` behaves byte-for-byte
as it did without keys (anonymous scrape API + offline jlcparts catalog).

Credential contract (env-only, matching the ``OCTOPART_API_KEY`` precedent in
``datasheet/manager.py``; no dotenv dependency is added -- users get ``.env``
support from their shell/direnv):

* ``JLCPCB_APP_ID``     -- application id (public)
* ``JLCPCB_ACCESS_KEY`` -- access key (public)
* ``JLCPCB_SECRET_KEY`` -- secret key (HMAC key material, never transmitted)

All three must be present and non-empty (after ``.strip()``) for the tier to
activate; a partial set behaves exactly like the keyless path.

Signing scheme -- IMPORTANT provenance note
--------------------------------------------
JLCPCB does **not** publish a first-party REST reference for the signing
algorithm (only Java-only SDKs, which third-party live-probing reports are
stale vs. the live API). The construction below is the convergent result of
three independent reverse-engineering projects (see issue #4118 for citations):
``wavenumber-eng/supply-chain-monkey`` (Parts/component surface, live-probed),
``Jackster/JLCPCB-API`` and ``mattpainter701/kicad_automations`` (PCB-ordering
surface). All three agree on the string-to-sign construction:

    string_to_sign = f"{METHOD}\n{PATH}\n{TIMESTAMP}\n{NONCE}\n{BODY}\n"

with uppercase HTTP method, the request path, Unix epoch **seconds** as a
decimal string, a 32-char hex nonce, and the exact compact-JSON request body
(empty string for no-body requests), each field newline-terminated including
a trailing newline after ``BODY``.

The sources **disagree** on two points that could not be resolved without a
live smoke test, so each is isolated here as a single named constant that can
be flipped without touching any request logic:

* :data:`AUTH_SCHEME` -- the ``Authorization`` scheme keyword. The Parts
  surface (this module's target) uses ``"JOP"``; the PCB surface uses
  ``"JOP-HMAC-SHA256"``.
* :data:`SIGNATURE_ENCODING` -- how the raw HMAC-SHA256 digest is encoded.
  The Parts surface uses Base64; the PCB surface uses lowercase hex.

This module implements the **Parts variant** (``JOP`` + Base64) as the default,
because this issue targets the ``open.jlcpcb.com`` component endpoints, and the
``wavenumber-eng`` client is the only source that reports live-probing success
against exactly those endpoints. If the owner's local live smoke shows the live
API wants the other variant, flip the two constants below -- no other change is
needed.

Live-smoke result (2026-07, issue #4118)
----------------------------------------
A one-off local smoke against ``open.jlcpcb.com`` observed a permission denial
with Base64 signatures and a signature rejection with hex signatures. This
supports the current Base64 default, but a permission denial alone does not
establish that the signature verified. HTTP 403 can reflect IP restrictions,
product permissions, or another access policy; classification uses the explicit
server reason when available.

"""

from __future__ import annotations

import base64
import contextlib
import hashlib
import hmac
import json
import logging
import os
import re
import secrets
import time
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal

from .lcsc import _categorize_part, _guess_package_type
from .models import Part, PartPrice

if TYPE_CHECKING:
    # ``requests`` is an optional runtime dependency (imported lazily in
    # ``_get_session``), but it ships inline types, so a type-checking-only
    # import is enough to annotate the cached session attribute.
    import requests

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------
# Endpoint configuration (Parts/component surface, host open.jlcpcb.com)
# --------------------------------------------------------------------------
JLC_OPENAPI_BASE = "https://open.jlcpcb.com"

# POST body {"componentCodes": ["C2040", ...]} -- array required.
COMPONENT_DETAIL_PATH = "/overseas/openapi/component/getComponentDetailByCode"

# --------------------------------------------------------------------------
# Signing spec flip-points (see module docstring / issue #4118).
#
# These two constants are the ONLY places the Parts-vs-PCB signing variants
# differ. A live smoke test that fails on the default (Parts) variant is
# corrected by flipping these -- do not touch _sign()/_build_auth_header().
# --------------------------------------------------------------------------
# Scheme keyword placed at the front of the Authorization header.
#   Parts surface (wavenumber-eng, live-probed): "JOP"
#   PCB surface (Jackster, mattpainter701):      "JOP-HMAC-SHA256"
AUTH_SCHEME = "JOP"

# Encoding applied to the raw HMAC-SHA256 digest bytes.
#   Parts surface: "base64"
#   PCB surface:   "hex"
SIGNATURE_ENCODING = "base64"

# Environment variable names (independently corroborated by
# wavenumber-eng/supply-chain-monkey's .env.template).
ENV_APP_ID = "JLCPCB_APP_ID"
ENV_ACCESS_KEY = "JLCPCB_ACCESS_KEY"
ENV_SECRET_KEY = "JLCPCB_SECRET_KEY"


class JLCAPIError(Exception):
    """Base class for official JLCPCB open-platform API failures.

    Distinct from :class:`~kicad_tools.parts.lcsc.LCSCForbiddenError` (which
    describes the *anonymous* scrape API) and from
    :class:`~kicad_tools.parts.lcsc.LCSCDependencyMissingError` (missing
    ``requests`` extra). Carries the JLCPCB business ``code`` and ``message``
    when they are available so callers can log something actionable.
    """

    def __init__(self, message: str, *, code: int | None = None, http_status: int | None = None):
        super().__init__(message)
        self.code = code
        self.http_status = http_status


class JLCAuthError(JLCAPIError):
    """Bad signature / rejected credentials (auth-level failure).

    Actionable: re-check ``JLCPCB_ACCESS_KEY`` / ``JLCPCB_SECRET_KEY`` /
    ``JLCPCB_APP_ID`` against the developer portal, and confirm the signing
    variant (see :data:`AUTH_SCHEME` / :data:`SIGNATURE_ENCODING`) matches the
    live API.
    """


class JLCIPNotWhitelistedError(JLCAPIError):
    """The caller's public IP is not on the app's IP whitelist.

    Selected only for an explicit IP/whitelist reason in the server response.
    A generic HTTP 403 or product permission denial is insufficient evidence.
    """


class JLCPermissionError(JLCAPIError):
    """Access or product permission denied, without diagnosing an IP restriction."""


class JLCQuotaError(JLCAPIError):
    """API quota or rate limit exceeded.

    Actionable: back off and retry later, or request a higher quota from the
    developer portal.
    """


class JLCIncompleteResponseError(JLCAPIError):
    """The HTTP/business envelope succeeded but the payload shape is unusable.

    Distinct from a transport failure or a per-code miss: the server accepted
    the request, but ``data`` was not the documented list, so no per-code
    stock evidence -- verified, missing, or malformed -- can be attributed.
    """


class JLCCredentials:
    """Immutable credential triplet for the official JLCPCB open-platform API.

    Use :meth:`from_env` to load from the environment (the normal path). All
    three values must be non-empty after ``.strip()`` for credentials to be
    considered complete; :meth:`from_env` returns ``None`` otherwise so the
    caller can silently fall through to the keyless tiers.
    """

    __slots__ = ("app_id", "access_key", "secret_key")

    def __init__(self, app_id: str, access_key: str, secret_key: str):
        self.app_id = app_id
        self.access_key = access_key
        self.secret_key = secret_key

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> JLCCredentials | None:
        """Load credentials from the environment, or ``None`` if incomplete.

        Reads :data:`ENV_APP_ID`, :data:`ENV_ACCESS_KEY`, :data:`ENV_SECRET_KEY`
        via plain ``os.environ.get`` (no dotenv). Missing or blank values are
        treated as "keyless" -- this returns ``None`` and is **not** an error;
        the caller falls through to the anonymous / offline tiers.

        Args:
            environ: Mapping to read instead of ``os.environ`` (for testing).

        Returns:
            A complete :class:`JLCCredentials`, or ``None`` when any of the
            three variables is missing or empty.
        """
        env = os.environ if environ is None else environ
        app_id = (env.get(ENV_APP_ID) or "").strip()
        access_key = (env.get(ENV_ACCESS_KEY) or "").strip()
        secret_key = (env.get(ENV_SECRET_KEY) or "").strip()
        if not (app_id and access_key and secret_key):
            return None
        return cls(app_id=app_id, access_key=access_key, secret_key=secret_key)


def _encode_signature(digest: bytes) -> str:
    """Encode raw HMAC-SHA256 digest bytes per :data:`SIGNATURE_ENCODING`."""
    if SIGNATURE_ENCODING == "base64":
        return base64.b64encode(digest).decode("ascii")
    if SIGNATURE_ENCODING == "hex":
        return digest.hex()
    raise ValueError(f"Unsupported SIGNATURE_ENCODING: {SIGNATURE_ENCODING!r}")


def _string_to_sign(method: str, path: str, timestamp: str, nonce: str, body: str) -> str:
    """Build the canonical string-to-sign (convergent across all 3 sources).

    ``f"{METHOD}\\n{PATH}\\n{TIMESTAMP}\\n{NONCE}\\n{BODY}\\n"`` -- note the
    trailing newline after ``BODY``.
    """
    return f"{method.upper()}\n{path}\n{timestamp}\n{nonce}\n{body}\n"


def _sign(secret_key: str, string_to_sign: str) -> str:
    """Return the encoded HMAC-SHA256 signature of ``string_to_sign``.

    The ``secret_key`` is used only as HMAC key material and is never placed in
    a request. Encoding of the digest is governed by :data:`SIGNATURE_ENCODING`.
    """
    digest = hmac.new(
        secret_key.encode("utf-8"),
        string_to_sign.encode("utf-8"),
        hashlib.sha256,
    ).digest()
    return _encode_signature(digest)


def _build_auth_header(
    credentials: JLCCredentials,
    *,
    nonce: str,
    timestamp: str,
    signature: str,
) -> str:
    """Assemble the ``Authorization`` header value.

    Format (per the reverse-engineered samples): the scheme keyword followed by
    quoted ``key="value"`` pairs. Field order has not been shown to matter.
    """
    fields = (
        f'appid="{credentials.app_id}"',
        f'accesskey="{credentials.access_key}"',
        f'nonce="{nonce}"',
        f'timestamp="{timestamp}"',
        f'signature="{signature}"',
    )
    return f"{AUTH_SCHEME} " + ", ".join(fields)


def _compact_json(payload: dict) -> str:
    """Serialize a request body to compact JSON (no spaces).

    The signature is computed over the *exact* body string sent on the wire,
    so the same string must be used for both signing and transmission.
    """
    return json.dumps(payload, separators=(",", ":"), ensure_ascii=False)


class JLCOpenAPIClient:
    """Signed client for the official JLCPCB open-platform Parts surface.

    Only the batch component-detail lookup is implemented (this issue's scope):
    there is no confirmed official keyword/MPN search endpoint, so
    :class:`~kicad_tools.parts.lcsc.LCSCClient.search` keeps using the anonymous
    / offline path even when keys are present.

    Example::

        creds = JLCCredentials.from_env()
        if creds is not None:
            client = JLCOpenAPIClient(creds)
            parts = client.get_component_detail_by_codes(["C2040"])
    """

    def __init__(
        self,
        credentials: JLCCredentials,
        *,
        base_url: str = JLC_OPENAPI_BASE,
        timeout: float = 30.0,
    ):
        """Initialize the client.

        Args:
            credentials: Complete credential triplet (see :class:`JLCCredentials`).
            base_url: API host (override for testing).
            timeout: Per-request timeout in seconds.
        """
        self.credentials = credentials
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._session: requests.Session | None = None

    def _get_session(self):
        """Get or create the underlying ``requests`` session."""
        if self._session is None:
            import requests

            self._session = requests.Session()
        return self._session

    def _post_signed(self, path: str, payload: dict) -> dict:
        """POST a signed request and return the parsed ``data`` envelope value.

        Signs ``METHOD\\nPATH\\nTIMESTAMP\\nNONCE\\nBODY\\n`` with the secret
        key, attaches the :data:`AUTH_SCHEME` ``Authorization`` header, and
        unwraps the ``{"code", "success", "data", "message"}`` envelope.

        Raises:
            JLCAuthError / JLCIPNotWhitelistedError / JLCQuotaError / JLCAPIError:
                On a non-success business envelope, classified by
                :func:`_classify_business_error`.
        """
        body = _compact_json(payload)
        nonce = secrets.token_hex(16)  # 32 hex chars, per every observed sample
        timestamp = str(int(time.time()))  # Unix epoch SECONDS
        string_to_sign = _string_to_sign("POST", path, timestamp, nonce, body)
        signature = _sign(self.credentials.secret_key, string_to_sign)

        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Authorization": _build_auth_header(
                self.credentials,
                nonce=nonce,
                timestamp=timestamp,
                signature=signature,
            ),
        }

        # ``requests.RequestException`` is the base of every transport error we
        # want to wrap. It is only importable with the optional ``parts`` extra;
        # fall back to ``Exception`` so this module (and its tests) load without
        # ``requests`` installed. The session itself still requires ``requests``
        # -- but tests inject a fake session, so the import stays optional here.
        request_exc: type[BaseException]
        try:
            import requests

            request_exc = requests.RequestException
        except ImportError:
            request_exc = Exception

        url = f"{self.base_url}{path}"
        try:
            response = self._get_session().post(
                url,
                data=body.encode("utf-8"),
                headers=headers,
                timeout=self.timeout,
            )
        except request_exc as e:
            reason = _safe_error_reason(
                str(e),
                (
                    self.credentials.app_id,
                    self.credentials.access_key,
                    self.credentials.secret_key,
                    headers["Authorization"],
                    signature,
                ),
            )
            raise JLCAPIError(f"JLCPCB open-platform request failed: {reason}") from None

        status = response.status_code
        envelope = None
        # Error pages may be HTML or oversized. Bound JSON parsing for failed
        # HTTP responses; component-detail success payloads may legitimately be large.
        if status < 400 or len(response.content) <= 65536:
            with contextlib.suppress(ValueError):
                envelope = response.json()

        if not isinstance(envelope, dict):
            if status >= 400:
                raise _classify_business_error(None, None, http_status=status)
            raise JLCAPIError(
                f"JLCPCB open-platform returned a non-JSON or unexpected response (HTTP {status}).",
                http_status=status,
            )

        code = envelope.get("code")
        if 200 <= status < 300 and code == 200 and envelope.get("success"):
            return {"data": envelope.get("data")}

        # Never include echoed request credentials, signatures, or headers in
        # the exception. Only a bounded, sanitized message field is retained.
        reason = _safe_error_reason(
            envelope.get("message"),
            (
                self.credentials.app_id,
                self.credentials.access_key,
                self.credentials.secret_key,
                headers["Authorization"],
                signature,
            ),
        )
        raise _classify_business_error(code, reason, http_status=status)

    def get_component_detail_by_codes(self, codes: list[str]) -> dict[str, Part]:
        """Look up component detail for one or more LCSC codes.

        Wraps ``POST /overseas/openapi/component/getComponentDetailByCode``.
        The body requires a ``componentCodes`` **array** (a bare string is
        rejected by the live API).

        Args:
            codes: LCSC part numbers (e.g. ``["C2040", "C25804"]``). Blank
                entries are dropped.

        Returns:
            Dict mapping the returned ``componentCode`` (upper-cased) to a
            :class:`Part`. Codes with no match are simply absent.
        """
        cleaned = [c.strip().upper() for c in codes if c and c.strip()]
        if not cleaned:
            return {}

        result = self._post_signed(COMPONENT_DETAIL_PATH, {"componentCodes": cleaned})
        data = result.get("data")
        components = data if isinstance(data, list) else []

        parts: dict[str, Part] = {}
        for component in components:
            if not isinstance(component, dict):
                continue
            try:
                part = _parse_official_component(component)
            except Exception as e:  # noqa: BLE001 -- one bad row must not abort the batch
                logger.warning(f"Failed to parse official JLCPCB component: {e}")
                continue
            if part.lcsc_part:
                parts[part.lcsc_part.upper()] = part
        return parts

    def get_component_detail_raw(self, codes: list[str]) -> list[dict[str, Any]]:
        """Return raw per-code response objects, with no ``Part`` projection.

        Unlike :meth:`get_component_detail_by_codes`, this performs **no**
        field coercion: ``stockCount`` is returned exactly as the server sent
        it (absent, ``null``, a string, negative, or a genuine non-negative
        int), so a caller that must distinguish verified-zero stock from
        unknown/missing/malformed evidence has the raw material to do so
        instead of silently observing ``Part.stock == 0`` for both.

        Args:
            codes: LCSC part numbers. Blank entries are dropped; an empty
                result after cleaning returns ``[]`` without a request.

        Returns:
            The raw ``data`` list entries that are JSON objects (non-object
            entries are dropped, not coerced). Codes with no match are simply
            absent -- this method does not report per-code misses itself.

        Raises:
            JLCIncompleteResponseError: the response envelope succeeded but
                ``data`` was not a list.
            JLCAPIError (and subclasses): transport, auth, permission, IP
                whitelist, or quota failures, exactly as for
                :meth:`get_component_detail_by_codes`.
        """
        cleaned = [c.strip().upper() for c in codes if c and c.strip()]
        if not cleaned:
            return []

        result = self._post_signed(COMPONENT_DETAIL_PATH, {"componentCodes": cleaned})
        data = result.get("data")
        if not isinstance(data, list):
            raise JLCIncompleteResponseError(
                "JLCPCB open-platform returned a non-list component-detail payload."
            )
        return [component for component in data if isinstance(component, dict)]

    def close(self) -> None:
        """Close the underlying HTTP session, if any."""
        if self._session is not None:
            self._session.close()
            self._session = None

    def __enter__(self) -> JLCOpenAPIClient:
        return self

    def __exit__(self, exc_type, exc_val, exc_tb) -> Literal[False]:
        self.close()
        return False


def _safe_error_reason(message: object, secrets_to_redact: tuple[str, ...] = ()) -> str:
    """Retain a short plain-text reason, never a serialized response envelope."""
    if not isinstance(message, str) or not message:
        return "unknown error"
    if len(message) > 65536:
        return "server error reason too large"
    for secret in sorted(secrets_to_redact, key=len, reverse=True):
        if secret:
            message = message.replace(secret, "[redacted]")
    return " ".join("".join(c if c.isprintable() else " " for c in message).split())[:512]


def _classify_business_error(
    code: object, message: object, *, http_status: int | None = None
) -> JLCAPIError:
    """Use explicit reasons first, then status fallbacks without inferring IP or signature validity."""
    msg = _safe_error_reason(message)
    try:
        code_int = int(str(code)) if code is not None else None
    except (TypeError, ValueError):
        code_int = None
    lowered = msg.lower()
    details = f"(HTTP {http_status}, code={code_int}): {msg}"
    error_type: type[JLCAPIError]
    if "signature" in lowered or "sign verify" in lowered:
        error_type = JLCAuthError
        reason = "authentication failed (signature verification); check the signing configuration"
    elif any(term in lowered for term in ("quota", "rate limit", "too many", "frequenc")):
        error_type = JLCQuotaError
        reason = "quota/rate limit exceeded"
    elif "whitelist" in lowered or (
        re.search(r"\bip\b", lowered)
        and any(term in lowered for term in ("not allowed", "denied", "reject", "forbidden"))
    ):
        error_type = JLCIPNotWhitelistedError
        reason = "IP access restriction; check the app's IP whitelist"
    elif any(term in lowered for term in ("permission", "access denied", "forbidden")):
        error_type = JLCPermissionError
        reason = "access denied; check app/product permissions"
    elif any(
        term in lowered
        for term in ("auth", "credential", "access key", "accesskey", "secret", "token")
    ):
        error_type = JLCAuthError
        reason = "authentication failed"
    elif code_int == 429 or http_status == 429:
        error_type = JLCQuotaError
        reason = "quota/rate limit exceeded"
    elif code_int == 401 or http_status == 401:
        error_type = JLCAuthError
        reason = "authentication failed"
    elif code_int == 403 or http_status == 403:
        error_type = JLCPermissionError
        reason = "access denied"
    else:
        error_type = JLCAPIError
        reason = "request failed"
    return error_type(
        f"JLCPCB open-platform {reason} {details}.", code=code_int, http_status=http_status
    )


def _parse_official_component(data: dict) -> Part:
    """Translate an official-API component object into a :class:`Part`.

    The official Parts surface uses **different field names** from the anonymous
    scrape API (e.g. ``componentModel`` vs ``componentModelEn``,
    ``priceRanges``/``startQuantity``/``unitPrice`` vs
    ``prices``/``startNumber``/``productPrice``), so this is a distinct parser
    rather than a reuse of ``LCSCClient._parse_component``.
    """
    # Price breaks: priceRanges is a list of {startQuantity, unitPrice}.
    prices: list[PartPrice] = []
    for price_break in data.get("priceRanges") or []:
        if not isinstance(price_break, dict):
            continue
        qty = price_break.get("startQuantity")
        unit_price = price_break.get("unitPrice")
        if qty is None or unit_price is None:
            continue
        try:
            qty_i = int(qty)
            price_f = float(unit_price)
        except (TypeError, ValueError):
            continue
        if qty_i > 0 and price_f > 0:
            prices.append(PartPrice(quantity=qty_i, unit_price=price_f))
    prices.sort(key=lambda p: p.quantity)

    def _str(name: str) -> str:
        value = data.get(name)
        return str(value) if value not in (None, "") else ""

    package = _str("componentSpecification")
    description = _str("description") or _str("componentModel")
    category = _categorize_part(description, package)

    # libraryType distinguishes Basic vs Preferred (vs Extended).
    library_type = _str("libraryType").lower()

    code = _str("componentCode")

    # Datasheet: datasheetUrl preferred, dataManualUrl as a fallback.
    datasheet_url = _str("datasheetUrl") or _str("dataManualUrl")

    try:
        stock = int(data.get("stockCount") or 0)
    except (TypeError, ValueError):
        stock = 0

    return Part(
        lcsc_part=code,
        mfr_part=_str("componentModel"),
        manufacturer=_str("componentBrand") or _str("brandName"),
        description=description,
        category=category,
        package=package,
        package_type=_guess_package_type(package),
        stock=stock,
        prices=prices,
        is_basic=library_type == "base" or library_type == "basic",
        is_preferred=library_type == "preferred",
        datasheet_url=datasheet_url,
        product_url=f"https://jlcpcb.com/partdetail/{code}" if code else "",
        fetched_at=datetime.now(),
        stock_source="live",
    )
