# Parts lookup coverage

A supplier request failure is not evidence that a part is absent from the catalog.
`LCSCClient.lookup()` returns a `Part` for useful matches and `None` for a successful
live no-match. It now raises `LCSCUnavailableError` when failed live requests and
missing offline coverage cannot establish an answer. This intentionally changes
the old outage behavior, which returned `None` or an empty successful search.

Use `lookup_result()` when query coverage is needed alongside the part:

```python
answer = client.lookup_result("C1987701")
# answer.part: original Part object or None
# answer.source: official, anonymous, offline, cache, live, or none
# answer.status: found, not_found, offline_miss, or unavailable
# answer.diagnostics: safe source failures and coverage limitations
```

An offline hit remains useful for identifying a part. An offline miss only means
the snapshot did not resolve it. Neither a cached response nor an offline result
verifies current inventory. Query coverage is separate from the part's original
stock observation timestamp and provenance; this change does not refresh them.
The optional dependency error remains `LCSCDependencyMissingError` when neither
the requests capability nor an offline fallback is available. It remains an
`ImportError` and also subclasses `LCSCUnavailableError`, carrying partial batch
results and unresolved IDs so consumers preserve cached prices and mark unknown
availability explicitly.

Search returns a `SearchResult` with `source`, `coverage` (`live`, `offline`, or
`incomplete`; `unknown` for externally constructed legacy results), and
`diagnostics`. Successful empty live pages are distinct from offline empty pages.
Malformed/business-error envelopes and transport failures attempt the offline
fallback; if none is usable, search raises `LCSCUnavailableError`. A partial page
with unparseable entries carries incomplete coverage. A missing exact-MPN match
in partial coverage does not prove absence.

`lookup_many()` keeps its part-mapping interface. Its `BatchLookupResult` also
exposes per-part `sources` and `diagnostics`. If unresolved IDs lack a verified
answer, it raises `LCSCUnavailableError` with `partial_results` and
`unavailable_parts`. Callers can retain successful matches without interpreting
unknown IDs as out of stock. The availability consumers do this and emit an
explicit unavailable status/error; cost estimation preserves successful prices
and labels unresolved pricing as estimated. BOM suggestion/enrichment propagates
unavailable failures instead of recording false no-match results.

The lookup/search CLI includes coverage in JSON and explains unavailable or
offline results in text. Empty results and unavailable operations return nonzero;
JSON remains machine-readable in either case.

No replacement anonymous endpoint is assumed. HTTP 403/404 may mean the service
or endpoint is unavailable, not that the requested component does not exist.
Official authenticated error classification remains the official client's job;
source orchestration retains safe exception types/codes without copying raw
response bodies or credentials. No live supplier request is needed to test these
semantics.

## Authorised-distributor stock (`kct parts stock`)

`kct parts stock <MPN>... -q <qty> [--format table|json]` reads Mouser and DigiKey
through their official product-search APIs (issue #8892). It is separate from the
LCSC/JLCPCB sources above and never falls back to them.

Credentials are environment-only (never CLI flags, never written to output or the
cache): `MOUSER_API_KEY`; `DIGIKEY_CLIENT_ID` + `DIGIKEY_CLIENT_SECRET`
(OAuth2 client-credentials; the token is held in memory only). A source without
credentials is skipped with a message naming the missing variables; the others
still run. Optional: `DIGIKEY_LOCALE_SITE` (default `US`),
`DIGIKEY_LOCALE_CURRENCY` (default `USD`).

| Field | Mouser | DigiKey |
|---|---|---|
| Stock on hand | `AvailabilityInStock` | `QuantityAvailable` (all packagings) |
| Unit price at quantity | `PriceBreaks` | first priced variation's `StandardPricing` |
| Lead time | `LeadTime` (as given, e.g. "84 Days") | `ManufacturerLeadWeeks` |
| Lifecycle | `LifecycleStatus` | `ProductStatus.Status` |
| Packaging | `Packaging` product attribute, if present | variation `PackageType` |
| Distributor PN | `MouserPartNumber` | variation `DigiKeyProductNumber` |
| Region | fixed label `mouser-account-default` (the key's account site) | `DIGIKEY_LOCALE_SITE` |

Every offer carries `fetched_at` (UTC, time of the API read). Price is the highest
break at or below the requested quantity; below the lowest break the lowest break is
used and flagged. Multiple MPNs give a comparison sorted purchasable-now (stock >=
quantity) first, then unit price. Cached offers (default TTL 60 min, `--no-cache`
to bypass) show their age; stock moves, so treat cached figures accordingly.

Rate limits: live calls are counted per source per UTC day in the local cache
(`distributors.db` beside `parts.db`) and refused once the quota is reached
(defaults 1000/day each, overridable with `MOUSER_DAILY_QUOTA` /
`DIGIKEY_DAILY_QUOTA`; **verify against each vendor's current terms** -- the defaults
were not confirmed against live documentation). Cache hits do not count. Per-minute
limits are not tracked; an HTTP 429 is reported and the lookup skipped.

Not covered: orders, carts, quotes; other distributors; non-exact MPN search
(only exact MPN matches are returned); DigiKey packaging variations beyond the
first priced one; BOM integration (tracked in #8893). Test fixtures are
hand-written to the documented response shapes, not captured live; real-key
behaviour has not been verified in CI.
