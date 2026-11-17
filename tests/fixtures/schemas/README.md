# Vendored schemas (test fixtures)

## `sarif-schema-2.1.0.json`

The OASIS SARIF 2.1.0 JSON schema (errata01), used by
`tests/test_mistake_waivers_sarif_6006.py` to validate `--format sarif`
output offline (Issue #6006). It is a byte-identical, unmodified copy.

- Source: <https://docs.oasis-open.org/sarif/sarif/v2.1.0/errata01/os/schemas/sarif-schema-2.1.0.json>
- sha256: `c3b4bb2d6093897483348925aaa73af03b3e3f4bd4ca38cef26dcb4212a2682e`
- Copyright (c) OASIS Open 2020. All Rights Reserved.
- Distributed under the OASIS IPR Policy and copyright notice:
  <https://www.oasis-open.org/policies-guidelines/ipr/>

The schema is a test-only input; nothing under `src/` reads it, so it is not in
the wheel. It does ship in the sdist with the rest of `tests/`, together with
this notice, so the SARIF tests also run from an unpacked sdist. The OASIS
policy allows unmodified copies to be redistributed as long as this copyright
notice and the policy reference stay with them.

To refresh, download the file from the source URL, check the sha256 above
(update it here if OASIS publishes a new errata), and do not edit the file.
