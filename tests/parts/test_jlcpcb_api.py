"""Tests for the official JLCPCB open-platform (BYO-key) parts client.

No test in this file makes a real network request. Signing is verified against
a hand-computed known-answer vector; the HTTP layer is fully mocked. The
credentials used everywhere are obviously fake -- real keys must never appear
in tests, fixtures, or logs (issue #4118).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from unittest import mock

import pytest

from kicad_tools.parts import (
    JLCAPIError,
    JLCAuthError,
    JLCCredentials,
    JLCIPNotWhitelistedError,
    JLCOpenAPIClient,
    JLCQuotaError,
    LCSCClient,
    Part,
    jlcpcb_api,
)
from kicad_tools.parts import lcsc as lcsc_mod

# --------------------------------------------------------------------------
# Fake credentials (NEVER real). Reused across the suite.
# --------------------------------------------------------------------------
FAKE_APP_ID = "fake-app-id"
FAKE_ACCESS_KEY = "fake-access-key"
FAKE_SECRET_KEY = "fake-secret-key-do-not-use"

FAKE_ENV = {
    "JLCPCB_APP_ID": FAKE_APP_ID,
    "JLCPCB_ACCESS_KEY": FAKE_ACCESS_KEY,
    "JLCPCB_SECRET_KEY": FAKE_SECRET_KEY,
}


def _fake_creds() -> JLCCredentials:
    return JLCCredentials(FAKE_APP_ID, FAKE_ACCESS_KEY, FAKE_SECRET_KEY)


# --------------------------------------------------------------------------
# Known-answer signing test
# --------------------------------------------------------------------------


def test_string_to_sign_shape():
    sts = jlcpcb_api._string_to_sign(
        "post",  # lower-cased on purpose -- must be upper-cased
        "/overseas/openapi/component/getComponentDetailByCode",
        "1700000000",
        "00112233445566778899aabbccddeeff",
        '{"componentCodes":["C2040"]}',
    )
    assert sts == (
        "POST\n"
        "/overseas/openapi/component/getComponentDetailByCode\n"
        "1700000000\n"
        "00112233445566778899aabbccddeeff\n"
        '{"componentCodes":["C2040"]}\n'
    )
    # Trailing newline after BODY (convergent across all 3 reference sources).
    assert sts.endswith('"]}\n')


def test_sign_known_answer_base64():
    """Hand-computed base64 HMAC-SHA256 over a fixed method/path/ts/nonce/body."""
    sts = jlcpcb_api._string_to_sign(
        "POST",
        "/overseas/openapi/component/getComponentDetailByCode",
        "1700000000",
        "00112233445566778899aabbccddeeff",
        '{"componentCodes":["C2040"]}',
    )
    # Recompute independently in the test, then assert the literal string too.
    expected_digest = hmac.new(
        FAKE_SECRET_KEY.encode("utf-8"), sts.encode("utf-8"), hashlib.sha256
    ).digest()
    expected_b64 = base64.b64encode(expected_digest).decode("ascii")

    assert jlcpcb_api.SIGNATURE_ENCODING == "base64"
    assert jlcpcb_api._sign(FAKE_SECRET_KEY, sts) == expected_b64
    # Literal known-answer value (pins the algorithm end-to-end).
    assert expected_b64 == "bCac32rIS+6RPiHcQxfWJVvYr1+nUODKc2OKtgwPups="


def test_encode_signature_hex_variant():
    """The hex encoding (PCB-surface variant) must be a single flip-point."""
    digest = b"\x00\x01\x02\x03"
    with mock.patch.object(jlcpcb_api, "SIGNATURE_ENCODING", "hex"):
        assert jlcpcb_api._encode_signature(digest) == "00010203"
    with mock.patch.object(jlcpcb_api, "SIGNATURE_ENCODING", "base64"):
        assert jlcpcb_api._encode_signature(digest) == base64.b64encode(digest).decode()


def test_auth_header_shape():
    header = jlcpcb_api._build_auth_header(
        _fake_creds(),
        nonce="abc",
        timestamp="1700000000",
        signature="SIG",
    )
    assert header.startswith(f"{jlcpcb_api.AUTH_SCHEME} ")
    assert f'appid="{FAKE_APP_ID}"' in header
    assert f'accesskey="{FAKE_ACCESS_KEY}"' in header
    assert 'nonce="abc"' in header
    assert 'timestamp="1700000000"' in header
    assert 'signature="SIG"' in header
    # Secret key MUST NOT be transmitted.
    assert FAKE_SECRET_KEY not in header


def test_compact_json_no_spaces():
    assert jlcpcb_api._compact_json({"componentCodes": ["C2040", "C1"]}) == (
        '{"componentCodes":["C2040","C1"]}'
    )


# --------------------------------------------------------------------------
# Credential loading (env contract)
# --------------------------------------------------------------------------


def test_credentials_from_env_complete():
    creds = JLCCredentials.from_env(FAKE_ENV)
    assert creds is not None
    assert creds.app_id == FAKE_APP_ID
    assert creds.access_key == FAKE_ACCESS_KEY
    assert creds.secret_key == FAKE_SECRET_KEY


@pytest.mark.parametrize(
    "env",
    [
        {},
        {"JLCPCB_APP_ID": FAKE_APP_ID},
        {"JLCPCB_APP_ID": FAKE_APP_ID, "JLCPCB_ACCESS_KEY": FAKE_ACCESS_KEY},
        # Present but blank -> treated as missing.
        {
            "JLCPCB_APP_ID": FAKE_APP_ID,
            "JLCPCB_ACCESS_KEY": FAKE_ACCESS_KEY,
            "JLCPCB_SECRET_KEY": "   ",
        },
    ],
)
def test_credentials_from_env_incomplete_returns_none(env):
    assert JLCCredentials.from_env(env) is None


def test_credentials_strip_whitespace():
    creds = JLCCredentials.from_env(
        {
            "JLCPCB_APP_ID": f"  {FAKE_APP_ID}  ",
            "JLCPCB_ACCESS_KEY": FAKE_ACCESS_KEY,
            "JLCPCB_SECRET_KEY": FAKE_SECRET_KEY,
        }
    )
    assert creds is not None
    assert creds.app_id == FAKE_APP_ID


# --------------------------------------------------------------------------
# Mock HTTP scaffolding
# --------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, *, status_code: int = 200, json_data=None, raise_json=False):
        self.status_code = status_code
        self._json_data = json_data
        self._raise_json = raise_json

    def json(self):
        if self._raise_json:
            raise ValueError("not json")
        return self._json_data


class _FakeSession:
    """Captures the outgoing request and returns a canned response."""

    def __init__(self, response: _FakeResponse):
        self._response = response
        self.calls: list[dict] = []

    def post(self, url, data=None, headers=None, timeout=None):
        self.calls.append({"url": url, "data": data, "headers": headers, "timeout": timeout})
        return self._response

    def close(self):
        pass


def _client_with_response(response: _FakeResponse) -> tuple[JLCOpenAPIClient, _FakeSession]:
    client = JLCOpenAPIClient(_fake_creds())
    session = _FakeSession(response)
    client._session = session
    return client, session


# --------------------------------------------------------------------------
# Successful component-detail parsing
# --------------------------------------------------------------------------

_SAMPLE_COMPONENT = {
    "componentCode": "C2040",
    "componentModel": "RC0402FR-0710KL",
    "componentBrand": "YAGEO",
    "componentSpecification": "0402",
    "description": "10kOhms ±1% 62.5mW 0402 Chip Resistor",
    "libraryType": "base",
    "datasheetUrl": "https://example.invalid/ds.pdf",
    "stockCount": 123456,
    "priceRanges": [
        {"startQuantity": 100, "unitPrice": 0.0015},
        {"startQuantity": 10, "unitPrice": 0.0030},
    ],
}


def test_get_component_detail_by_codes_parses_part():
    resp = _FakeResponse(json_data={"code": 200, "success": True, "data": [_SAMPLE_COMPONENT]})
    client, session = _client_with_response(resp)

    parts = client.get_component_detail_by_codes(["c2040"])
    assert "C2040" in parts
    part = parts["C2040"]
    assert isinstance(part, Part)
    assert part.lcsc_part == "C2040"
    assert part.mfr_part == "RC0402FR-0710KL"
    assert part.manufacturer == "YAGEO"
    assert part.package == "0402"
    assert part.stock == 123456
    assert part.is_basic is True
    assert part.is_preferred is False
    assert part.datasheet_url == "https://example.invalid/ds.pdf"
    # priceRanges sorted ascending by quantity.
    assert [p.quantity for p in part.prices] == [10, 100]
    assert part.prices[0].unit_price == 0.0030

    # Request was signed and body was compact JSON with an array.
    call = session.calls[0]
    assert call["url"].endswith("/overseas/openapi/component/getComponentDetailByCode")
    assert call["data"] == b'{"componentCodes":["C2040"]}'
    auth = call["headers"]["Authorization"]
    assert auth.startswith(f"{jlcpcb_api.AUTH_SCHEME} ")
    # Secret must never be on the wire.
    assert FAKE_SECRET_KEY not in auth
    assert FAKE_SECRET_KEY.encode() not in call["data"]


def test_get_component_detail_empty_codes_no_request():
    client, session = _client_with_response(_FakeResponse(json_data={}))
    assert client.get_component_detail_by_codes(["", "  "]) == {}
    assert session.calls == []


# --------------------------------------------------------------------------
# Error mapping -> distinct exception types
# --------------------------------------------------------------------------


def test_http_401_maps_to_auth_error():
    client, _ = _client_with_response(_FakeResponse(status_code=401, json_data={}))
    with pytest.raises(JLCAuthError):
        client.get_component_detail_by_codes(["C1"])


def test_http_429_maps_to_quota_error():
    client, _ = _client_with_response(_FakeResponse(status_code=429, json_data={}))
    with pytest.raises(JLCQuotaError):
        client.get_component_detail_by_codes(["C1"])


def test_business_error_bad_signature_maps_to_auth():
    resp = _FakeResponse(json_data={"code": 4001, "success": False, "message": "Invalid signature"})
    client, _ = _client_with_response(resp)
    with pytest.raises(JLCAuthError) as exc:
        client.get_component_detail_by_codes(["C1"])
    assert "AUTH_SCHEME" in str(exc.value) or "authentication" in str(exc.value).lower()


def test_business_error_ip_not_whitelisted():
    resp = _FakeResponse(
        json_data={"code": 4003, "success": False, "message": "IP not in whitelist"}
    )
    client, _ = _client_with_response(resp)
    with pytest.raises(JLCIPNotWhitelistedError):
        client.get_component_detail_by_codes(["C1"])


def test_business_error_quota():
    resp = _FakeResponse(
        json_data={"code": 4290, "success": False, "message": "Request quota exceeded"}
    )
    client, _ = _client_with_response(resp)
    with pytest.raises(JLCQuotaError):
        client.get_component_detail_by_codes(["C1"])


def test_business_error_generic():
    resp = _FakeResponse(json_data={"code": 5000, "success": False, "message": "Internal error"})
    client, _ = _client_with_response(resp)
    with pytest.raises(JLCAPIError) as exc:
        client.get_component_detail_by_codes(["C1"])
    # Not misclassified as a more specific subtype.
    assert type(exc.value) is JLCAPIError


def test_non_json_response_maps_to_api_error():
    client, _ = _client_with_response(_FakeResponse(status_code=200, raise_json=True))
    with pytest.raises(JLCAPIError):
        client.get_component_detail_by_codes(["C1"])


def test_business_error_real_signature_failure_maps_to_auth():
    """Confirmed live message (issue #4118 smoke): 401 signature-verify-failed."""
    resp = _FakeResponse(
        json_data={
            "code": 401,
            "success": False,
            "message": "The request signature verify failed",
        }
    )
    client, _ = _client_with_response(resp)
    with pytest.raises(JLCAuthError):
        client.get_component_detail_by_codes(["C1"])


def test_business_error_real_permission_denied_maps_to_whitelist():
    """Confirmed live message (issue #4118 smoke): 403 insufficient permissions.

    Signature verified but the app is not permitted -- a portal/IP matter, so
    it maps to the actionable IP/permission exception rather than a generic auth
    failure.
    """
    resp = _FakeResponse(
        json_data={
            "code": 403,
            "success": False,
            "message": "API insufficient permissions, access denied",
        }
    )
    client, _ = _client_with_response(resp)
    with pytest.raises(JLCIPNotWhitelistedError):
        client.get_component_detail_by_codes(["C1"])


def test_classify_business_error_hierarchy():
    # All auth/quota/whitelist errors subclass JLCAPIError so a broad except
    # still catches them, but the specific type is preserved.
    assert isinstance(jlcpcb_api._classify_business_error(None, "bad signature"), JLCAuthError)
    assert isinstance(
        jlcpcb_api._classify_business_error(None, "IP whitelist"), JLCIPNotWhitelistedError
    )
    assert isinstance(jlcpcb_api._classify_business_error(None, "quota"), JLCQuotaError)
    for exc_type in (JLCAuthError, JLCIPNotWhitelistedError, JLCQuotaError):
        assert issubclass(exc_type, JLCAPIError)


# --------------------------------------------------------------------------
# LCSCClient tier selection
# --------------------------------------------------------------------------


def test_lcsc_client_keyless_does_not_build_official(monkeypatch):
    """With no keys, the official tier is a silent no-op (returns None)."""
    for var in ("JLCPCB_APP_ID", "JLCPCB_ACCESS_KEY", "JLCPCB_SECRET_KEY"):
        monkeypatch.delenv(var, raising=False)
    client = LCSCClient(use_cache=False)
    assert client._get_official_client() is None
    # Memoized as the sentinel, not re-probed.
    assert client._official_client is False


def test_lcsc_client_partial_keys_behaves_keyless(monkeypatch):
    monkeypatch.setenv("JLCPCB_APP_ID", FAKE_APP_ID)
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", FAKE_ACCESS_KEY)
    monkeypatch.delenv("JLCPCB_SECRET_KEY", raising=False)
    client = LCSCClient(use_cache=False)
    assert client._get_official_client() is None


def test_lcsc_client_all_keys_builds_official(monkeypatch):
    monkeypatch.setenv("JLCPCB_APP_ID", FAKE_APP_ID)
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", FAKE_ACCESS_KEY)
    monkeypatch.setenv("JLCPCB_SECRET_KEY", FAKE_SECRET_KEY)
    # requests is available in the test env; if not, the tier is a no-op and
    # this assertion is skipped rather than failing spuriously.
    client = LCSCClient(use_cache=False)
    official = client._get_official_client()
    if official is None:
        pytest.skip("requests not installed; official tier inert")
    assert isinstance(official, JLCOpenAPIClient)


def test_lcsc_client_disabled_flag(monkeypatch):
    monkeypatch.setenv("JLCPCB_APP_ID", FAKE_APP_ID)
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", FAKE_ACCESS_KEY)
    monkeypatch.setenv("JLCPCB_SECRET_KEY", FAKE_SECRET_KEY)
    client = LCSCClient(use_cache=False, use_official_api=False)
    assert client._get_official_client() is None


def test_lcsc_lookup_prefers_official_tier(monkeypatch):
    """When keys are present and the official API resolves, use it first."""
    monkeypatch.setenv("JLCPCB_APP_ID", FAKE_APP_ID)
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", FAKE_ACCESS_KEY)
    monkeypatch.setenv("JLCPCB_SECRET_KEY", FAKE_SECRET_KEY)

    client = LCSCClient(use_cache=False)

    fake_part = Part(lcsc_part="C2040", description="from official")
    fake_official = mock.Mock()
    fake_official.get_component_detail_by_codes.return_value = {"C2040": fake_part}
    # Inject the resolved official client directly (skip env/deps probe).
    client._official_client = fake_official

    # _fetch_part must NOT be reached when the official tier hits.
    with mock.patch.object(client, "_fetch_part", side_effect=AssertionError("scrape used")):
        part = client.lookup("C2040")
    assert part is fake_part
    fake_official.get_component_detail_by_codes.assert_called_once()


def test_lcsc_lookup_official_miss_falls_through(monkeypatch):
    """An official-tier miss falls through to the anonymous scrape API."""
    monkeypatch.setenv("JLCPCB_APP_ID", FAKE_APP_ID)
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", FAKE_ACCESS_KEY)
    monkeypatch.setenv("JLCPCB_SECRET_KEY", FAKE_SECRET_KEY)

    client = LCSCClient(use_cache=False)
    fake_official = mock.Mock()
    fake_official.get_component_detail_by_codes.return_value = {}  # miss
    client._official_client = fake_official

    scrape_part = Part(lcsc_part="C2040", description="from scrape")
    with (
        mock.patch.object(lcsc_mod, "_requests_installed", return_value=True),
        mock.patch.object(client, "_fetch_part", return_value=scrape_part),
    ):
        part = client.lookup("C2040")
    assert part is scrape_part


def test_lcsc_lookup_official_error_falls_through(monkeypatch):
    """An official-tier exception is logged and falls through, not raised."""
    monkeypatch.setenv("JLCPCB_APP_ID", FAKE_APP_ID)
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", FAKE_ACCESS_KEY)
    monkeypatch.setenv("JLCPCB_SECRET_KEY", FAKE_SECRET_KEY)

    client = LCSCClient(use_cache=False)
    fake_official = mock.Mock()
    fake_official.get_component_detail_by_codes.side_effect = JLCAuthError("bad sig")
    client._official_client = fake_official

    scrape_part = Part(lcsc_part="C2040", description="from scrape")
    with (
        mock.patch.object(lcsc_mod, "_requests_installed", return_value=True),
        mock.patch.object(client, "_fetch_part", return_value=scrape_part),
    ):
        part = client.lookup("C2040")
    assert part is scrape_part


def test_lcsc_lookup_many_prefers_official(monkeypatch):
    monkeypatch.setenv("JLCPCB_APP_ID", FAKE_APP_ID)
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", FAKE_ACCESS_KEY)
    monkeypatch.setenv("JLCPCB_SECRET_KEY", FAKE_SECRET_KEY)

    client = LCSCClient(use_cache=False)
    p1 = Part(lcsc_part="C1", description="one")
    fake_official = mock.Mock()
    fake_official.get_component_detail_by_codes.return_value = {"C1": p1}
    client._official_client = fake_official

    # C2 not returned by official -> falls through to scrape.
    p2 = Part(lcsc_part="C2", description="two")
    with (
        mock.patch.object(lcsc_mod, "_requests_installed", return_value=True),
        mock.patch.object(client, "_fetch_part", return_value=p2),
    ):
        result = client.lookup_many(["C1", "C2"])
    assert result["C1"] is p1
    assert result["C2"] is p2
    # Official was asked for both codes in one batch call.
    fake_official.get_component_detail_by_codes.assert_called_once()


def test_lcsc_search_never_uses_official(monkeypatch):
    """search() has no official endpoint and must not touch the official tier."""
    monkeypatch.setenv("JLCPCB_APP_ID", FAKE_APP_ID)
    monkeypatch.setenv("JLCPCB_ACCESS_KEY", FAKE_ACCESS_KEY)
    monkeypatch.setenv("JLCPCB_SECRET_KEY", FAKE_SECRET_KEY)

    client = LCSCClient(use_cache=False)
    fake_official = mock.Mock()
    client._official_client = fake_official

    # search() imports ``requests`` in its body; stub it so the test runs even
    # without the optional ``parts`` extra installed.
    requests_stub = mock.Mock()
    with (
        mock.patch.dict("sys.modules", {"requests": requests_stub}),
        mock.patch.object(lcsc_mod, "_requests_installed", return_value=True),
        mock.patch.object(client, "_make_request", return_value=None),
    ):
        client.search("100nF 0402")
    fake_official.get_component_detail_by_codes.assert_not_called()
