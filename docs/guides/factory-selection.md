# Compare factory-selected component evidence

`kicad_tools.export.factory_selection.compare_factory_selection` compares original
UTF-8 CSV bytes against an explicitly approved per-reference identity plan. It
performs no network operations, file writes, substitutions or orders.

JLCPCB's [component matching guidelines](https://jlcpcb.com/help/article/component-matching-guidelines-for-pcba-orders)
(updated September 9, 2026; consulted September 11) say assembly covers selected
items, while unmatched or unselected items can remain unpopulated. Presence in a
BOM or generated expected list therefore does not establish factory selection.
The guidelines do not define a stable CSV schema: supply the actual columns and
selection tokens explicitly, after examining the exported evidence.

```python
from kicad_tools.export.factory_selection import (
    ExpectedPart, SelectionMapping, compare_factory_selection,
)

# Illustrative schema/identities, not supplier-guaranteed headers or parts.
expected = [ExpectedPart("Y1", catalog_id="C123", mpn="EXACT-TCXO-MPN")]
mapping = SelectionMapping(
    reference="Designators", selected="Selection",
    selected_values=("Selected",), unselected_values=("Not selected",),
    catalog_id="Catalog ID", mpn="Manufacturer Part", identity="both",
    group_separator=",",
)
report = compare_factory_selection(
    expected,
    b'Designators,Selection,Catalog ID,Manufacturer Part\n'
    b'Y1,Selected,C123,EXACT-TCXO-MPN\n',
    mapping,
    source_kind="factory_selected_csv",
)
assert report["comparison_matches"]
assert not report["factory_matched"]
assert not report["release_eligible"]
```

References use ASCII letters followed by digits, case-sensitive. Group fields
use a caller-chosen comma or semicolon; comma groups must be CSV-quoted.
Surrounding reference whitespace is stripped. Ranges, empty members and implicit
separator detection are rejected. CSV quoting and UTF-8 BOM are supported.
Identity and selection strings are exact and case-sensitive, including their
whitespace. Selection tokens must be tuple/list sequences with nonempty, unique and disjoint
string members. Scalar strings/bytes, mappings, sets and malformed members are
rejected before membership checks.

Identity policy is `catalog_id`, `mpn`, or `both`. Every expected reference must
provide the configured approved identities. Both means both must match; it does
not mean either match suffices. There is no generic, footprint, value or fuzzy
substitution rule. Duplicate references (even identical duplicates), unexpected
references, missing/unselected parts and identity changes prevent a match.
Malformed CSV, unknown selection tokens or unsupported source kinds produce an
`incomplete` report. Validly parsed differences produce `mismatch`; a successful
comparison has status `compared`. Invalid expected plans/mappings raise
`ValueError` instead of creating evidence from an ambiguous policy.

Reports contain SHA256 of the original CSV bytes, canonical sorted expected
records and comparison policy, plus every expected reference's rows and issues.
Byte-level CSV changes alter its hash even if parsed selections are identical.
The expected hash binds these identity records, not an entire source PCB or
future immutable handoff plan. Per-reference `matches` describes that row's
checks only; consume the report's global `comparison_matches` for the complete
comparison, because a malformed/extra row can invalidate otherwise matching
references.

`source_kind="factory_selected_csv"` is a caller declaration. Other kinds,
including `generated_expected_csv`, cannot pass. Hashing and the source-kind tag
do not authenticate supplier origin or stop someone deliberately mislabeling
bytes. Human provenance review and a valid immutable plan remain separate
integration prerequisites in #5056. This standalone module always reports
`factory_matched=false` and `release_eligible=false`; successful comparison alone
never authorizes assembly.
