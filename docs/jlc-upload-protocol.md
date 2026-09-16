# JLCPCB Gerber upload protocol

`manufacturers.jlc_upload.RequestsUploadTransport` implements only Gerber
upload and preview retrieval. The caller supplies credentials explicitly and
must provide an immutable submission plan and its bound human review.
The upload ledger is persisted before sending. A timeout, disconnect,
redirect, malformed response or conflicting identity leaves the upload
uncertain and blocks automatic retry until explicit reconciliation.

## Contract evidence

On 2026-09-16, static inspection of these user-downloaded first-party SDK
artifacts was independently reproduced (Squad review 101; issue #5145).
No SDK implementation is copied into this repository or used at runtime.

| Artifact | SHA-256 | Embedded Maven coordinate |
| --- | --- | --- |
| `8752621283844595712-Core SDK.jar` | `64888e13e524c57f399fb7704d93d2a63e2583f879df6b9b49f4d4d54462ce92` | `com.jlc.openapi:jlc-openapi-sdk-core-java:1.0.0` |
| `8784131861131657216-Business SDK.rar` (ZIP/JAR) | `3330fdc60fcf88466e7672c4e5623a1e8b67aa4aa28033ef4500e0191f25efdf` | `com.szjlc.overseas:overseas-openapi-sdk-java:1.0.6-SNAPSHOT` |

Download-origin metadata identifies the JLC overseas public-project artifact
bucket and `https://api.jlcpcb.com/` referrer. This corroborates the user's
origin, but is not a publisher signature. Portal version labels differ from
embedded Maven versions. Unauthenticated retrieval of the artifact objects
returned 403; remote-byte equality was not established. SDK classes were
statically inspected, not executed.

Public [signing documentation](https://api.jlcpcb.com/docs/api-request-signature)
confirms five newline-terminated fields and Base64 HMAC-SHA256;
[key documentation](https://api.jlcpcb.com/docs/configure-api-key) describes
application-scoped keys. The [API list](https://api.jlcpcb.com/docs/api-list)
describes upload and separate preview operations. Public documentation was
read through the site's documentation CMS.

## Wire format

The only destination is `https://open.jlcpcb.com`, with these operations:

| Operation | POST path | Body |
| --- | --- | --- |
| Upload | `/overseas/openapi/pcb/uploadGerber` | Multipart `meta` and `file` |
| Preview | `/overseas/openapi/pcb/audit/get` | JSON string `key`, optional integer `language` |

`UploadGerberFileRequest`, `UploadHttpRequest`, `OkHttpClientAdapter`,
`JopApiClient`, `AbstractHttpClient`, `Md5Kit` and `HexKit` establish the
upload path, method, part names, metadata signing and lowercase hexadecimal
raw-file MD5 in `Content-MD5`. The metadata string `{}` is an explicit static
inference from the fieldless request, transient inherited file fields and
Gson exclusion strategies. It is not a live serialization/acceptance test.
Requests generates the multipart boundary. The exact metadata string is
signed; the multipart encoding is not the signature body.

`UploadGerberFileResult` declares a string file key. `GetPcbAuditInfoRequest`
establishes the preview path, key and nullable integer language;
`GetPcbAuditInfoResult` declares an object payload. `HttpResult` defines
success as integer `code == 200`, without a required `success` boolean.
Python additionally requires successful HTTP status and valid operation data.
If `success` is present, it must be a boolean; false refuses success.
Contradictory success/code values and malformed envelopes do not establish
an upload rejection or receipt and require reconciliation. A well-typed
non-200 code with absent/false `success` establishes business rejection.
The inspected core validator does not establish a mandatory response-signature
protocol; TLS verification remains mandatory.

## Transport and evidence boundaries

Production sessions are created only when sending, after upload preflight and
durable intent. Requests does not follow redirects or retry. Adapters configured
for retries are refused. Prepared requests do not inherit session auth, cookies
or hooks; sends use TLS verification and no environment-derived proxies.
An injected session is a testing seam and always produces `mock-protocol-only`
receipts. Its transport identity cannot be reassigned. Tests block real
Requests HTTP sends and explicitly supply synthetic send implementations.

Offline tests establish request construction and state-machine behavior, not
factory acceptance. During development, one legacy test unexpectedly sent its
synthetic 33-byte fixture using the three fake credential constants and received
HTTP 401 (`application not exists`). No user board or real credential was used;
this is not evidence of accepted upload. The test suite now blocks all default
Session sends, including that legacy path.

No order, payment, quotation, guessed fabrication enums, BOM/CPL API or automatic
human approval is implemented. PCBA submission remains an explicit website
handoff. A real factory acceptance test has not been performed.
