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
the requests capability nor an offline fallback is available.

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
