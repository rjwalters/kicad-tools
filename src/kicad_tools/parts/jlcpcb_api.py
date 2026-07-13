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
A one-off local smoke against ``open.jlcpcb.com`` with the owner's real key
**confirmed the Base64 digest encoding is correct**:

* Base64 digest (either scheme keyword) -> HTTP 403,
  ``"API insufficient permissions, access denied"`` -- i.e. the signature
  *verified*, but the app was not permitted (a portal/permission/IP matter).
* Hex digest -> HTTP 401, ``"The request signature verify failed"`` -- the
  signature was rejected.

So :data:`SIGNATURE_ENCODING` = ``"base64"`` is validated, and the scheme
keyword does not affect signature verification. The remaining 403 is an
owner-side developer-portal configuration matter (enable the Parts API product
for the app / whitelist the calling IP), not a client bug -- which is why the
feature ships inert-without-keys and defers that step to the owner.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime
from typing import Literal

from .lcsc import _categorize_part, _guess_package_type
from .models import Part, PartPrice

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

    def __init__(self, message: str, *, code: int | None = None):
        super().__init__(message)
        self.code = code


class JLCAuthError(JLCAPIError):
    """Bad signature / rejected credentials (auth-level failure).

    Actionable: re-check ``JLCPCB_ACCESS_KEY`` / ``JLCPCB_SECRET_KEY`` /
    ``JLCPCB_APP_ID`` against the developer portal, and confirm the signing
    variant (see :data:`AUTH_SCHEME` / :data:`SIGNATURE_ENCODING`) matches the
    live API.
    """


class JLCIPNotWhitelistedError(JLCAPIError):
    """The caller's public IP is not on the app's IP whitelist.

    The JLCPCB developer portal has an IP Whitelisting feature; requests from
    an un-whitelisted IP are rejected. The exact business ``code``/``message``
    for this case is **unconfirmed** without a live smoke test -- detection is
    a best-effort message/code heuristic (see :func:`_classify_business_error`)
    and should be refined once the owner runs the live smoke.
    """


class JLCQuotaError(JLCAPIError):
    """API quota or rate limit exceeded.

    Actionable: back off and retry later, or request a higher quota from the
    developer portal.
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
        self._session = None

    def _get_session(self):
        """Get or create the underlying ``requests`` session."""
        if self._session is None:
            import requests  # type: ignore[import-untyped]

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
            raise JLCAPIError(f"JLCPCB open-platform request failed: {e}") from e

        # Transport-level auth rejections may surface as HTTP status rather than
        # a business envelope; map the common ones before parsing JSON.
        status = response.status_code
        if status in (401, 403):
            raise JLCAuthError(
                f"JLCPCB open-platform rejected the request (HTTP {status}); "
                "verify credentials and that this IP is whitelisted.",
                code=status,
            )
        if status == 429:
            raise JLCQuotaError(
                "JLCPCB open-platform rate limit exceeded (HTTP 429).",
                code=status,
            )

        try:
            envelope = response.json()
        except ValueError as e:
            raise JLCAPIError(
                f"JLCPCB open-platform returned non-JSON response (HTTP {status})."
            ) from e

        if not isinstance(envelope, dict):
            raise JLCAPIError("JLCPCB open-platform returned an unexpected response shape.")

        code = envelope.get("code")
        success = envelope.get("success")
        if code == 200 and success:
            data = envelope.get("data")
            return {"data": data}

        # Business-level failure: classify into an actionable exception.
        raise _classify_business_error(code, envelope.get("message"))

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


def _classify_business_error(code: object, message: object) -> JLCAPIError:
    """Map a non-success business envelope to a specific exception.

    The precise business codes/messages for auth vs. IP-whitelist vs. quota are
    **not first-party-documented**; this uses a best-effort message/code
    heuristic (see per-branch comments) that the owner's live smoke should
    refine. Anything unrecognized becomes a generic :class:`JLCAPIError` rather
    than being conflated with a "part not found" miss.
    """
    msg = str(message) if message not in (None, "") else "unknown error"
    code_int: int | None
    try:
        code_int = int(str(code)) if code is not None else None
    except (TypeError, ValueError):
        code_int = None

    lowered = msg.lower()

    # Signature rejection -- the *signature itself* did not verify. Confirmed
    # message from the live API (2026-07, issue #4118 smoke): code 401,
    # "The request signature verify failed". This is the actionable "your
    # signing is wrong" case -- check the secret key and the signing variant.
    if (
        "signature" in lowered
        or "sign verify" in lowered
        or (code_int == 401 and ("verify" in lowered or "auth" in lowered))
    ):
        return JLCAuthError(
            f"JLCPCB open-platform signature verification failed (code={code_int}): {msg}. "
            "Verify your JLCPCB_SECRET_KEY and the signing variant "
            "(AUTH_SCHEME / SIGNATURE_ENCODING) in parts/jlcpcb_api.py.",
            code=code_int,
        )

    # Quota / rate limit -- heuristic (UNCONFIRMED; refine via live smoke).
    if (
        code_int == 429
        or "quota" in lowered
        or "rate limit" in lowered
        or "too many" in lowered
        or "frequenc" in lowered
    ):
        return JLCQuotaError(
            f"JLCPCB open-platform quota/rate limit exceeded (code={code_int}): {msg}.",
            code=code_int,
        )

    # Permission / IP-whitelist rejection -- the signature verified but the app
    # is not permitted. Confirmed message from the live API (2026-07 smoke):
    # code 403, "API insufficient permissions, access denied". This is a portal
    # configuration matter (enable the Parts API product for the app and/or add
    # this machine's public IP to the app's IP whitelist), NOT a signing bug.
    if (
        code_int == 403
        or "whitelist" in lowered
        or "ip " in lowered
        or "not allowed" in lowered
        or "permission" in lowered
        or "access denied" in lowered
        or "denied" in lowered
        or "forbidden" in lowered
    ):
        return JLCIPNotWhitelistedError(
            f"JLCPCB open-platform denied access (code={code_int}): {msg}. "
            "The signature verified, but the app lacks permission: enable the "
            "Parts/component API product for this app and/or add your public IP "
            "to the app's IP whitelist in the developer portal.",
            code=code_int,
        )

    # Other auth-ish failures (missing token, bad access key, etc.).
    if (
        code_int in (401, 403)
        or "auth" in lowered
        or "credential" in lowered
        or "access key" in lowered
        or "accesskey" in lowered
        or "secret" in lowered
        or "token" in lowered
    ):
        return JLCAuthError(
            f"JLCPCB open-platform authentication failed (code={code_int}): {msg}. "
            "Verify your access/secret keys and the signing variant "
            "(AUTH_SCHEME / SIGNATURE_ENCODING).",
            code=code_int,
        )

    return JLCAPIError(
        f"JLCPCB open-platform returned a business error (code={code_int}): {msg}.",
        code=code_int,
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
    )
