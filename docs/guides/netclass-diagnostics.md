# Read-only netclass diagnostics

`diagnose_netclasses` inspects an already loaded project dictionary and an
optional board-net inventory. It performs no disk I/O and never creates default
settings or calls the writer-oriented getters. Existing project readers,
writers, templates, routing and HV policy are unchanged.

```python
from kicad_tools.core.project_file import load_project
from kicad_tools.core.netclass_diagnostics import diagnose_netclasses

project = load_project("example.kicad_pro")
report = diagnose_netclasses(project, ["USB", "USB_D+", "VCC"])
```

The caller supplies board net names; this API does not load a board. Omit the
inventory (or pass `None`) for unevaluated matches (`None`). An explicit empty
list produces evaluated empty matches (`[]`) and `no_matches` diagnostics.
Lists, tuples, sets and frozensets of strings are accepted; invalid collections
produce `invalid_inventory` and no matching, rather than misleading partial
results. Matches are sorted and unique; inventory order and duplicates do not
change membership. The original collections remain unchanged.

## Report structure

- `classes`: every class entry with original `index`, `name` and copied `entry`.
  Duplicate names produce `duplicate_class`; they remain in the report.
- `default_status`: `declared` only for a declared class named `Default`, otherwise
  `absent`. This describes declarations found, not KiCad's implicit fallback.
- `settings_status`: `missing`, `invalid` or `present`; inspect this and diagnostics
  before interpreting an empty declaration list.
- `patterns`: every entry with original `index`, copied `entry`, `pattern`,
  `target`, `target_defined`, syntax `status` and `matches`. Syntax status is
  `supported`, `unsupported` or `invalid`; target validity is independent.
  Malformed entries and overlapping/duplicate assignments stay visible.
- `assignments`: explicit net-name/class-array entries, retaining object entry
  `index`, array `target_index`, `target`, `target_defined` and `net_present`.
  Empty arrays are retained as rows with `entry: []`; unsupported forms retain
  their raw `entry`. `net_present` is exact inventory membership, not a class
  precedence decision.
- `diagnostics`: ordered `{code, path, message}` records. Undefined targets are
  reported even when their pattern is invalid or unsupported. An invalid sibling
  does not hide valid entries. Class validation concerns names/assignment
  structure, not the validity of electrical values such as clearance.

All retained entries are deep copies, so editing a returned entry cannot mutate
the project. The diagnostic itself never writes or migrates project data.
Output order follows source entry order; the same inputs give the same report.
This is an inspection API, not a complete `.kicad_pro` schema validator.

## Supported matching contract (KiCad 10.0.5)

KiCad's netclass matcher combines **anchored regular-expression OR anchored
wildcard membership**, case sensitively. A literal `USB` matches `USB`, not
`USB_D`. Calling `StartsWith` in the source does not turn these anchored matchers
into ordinary prefix matching. In particular `USB_*` matches both `USB_D`
(wildcard) and `USB` (regex repetition). `USB?` also matches `US` (optional `B`)
and `USB1` (wildcard). Python `fnmatch` alone cannot implement this contract.

The implemented subset is ASCII letters, digits, underscore, space, hyphen,
colon, `*` and `?`, with no adjacent quantifiers. Other syntax is explicitly
`unsupported_pattern`, including backslashes/escaping, brackets, parentheses,
periods, slashes, regex anchors, and Unicode **in patterns**. It is not silently
interpreted as a glob. The supported subset evaluates both interpretations;
invalid leading regex quantifiers still allow the wildcard interpretation.
The independent implementation uses position sets, avoiding regex backtracking.

Net names may contain punctuation, hierarchical slashes and Unicode. Native
verification includes non-BMP Unicode and newlines: wildcard characters can
consume newlines, and the anchored end also accepts the position before a final
newline. KiCad does not assign the unnamed (empty-name) net; it therefore never
appears among matches, even for `*`. Empty patterns are valid, with the same
anchor behavior. The oracle tests include these unusual cases to make the
contract explicit rather than assuming Python regex defaults.

This API reports **each pattern's membership**, not an effective winning class,
composite class or electrical constraints. It makes no claim about other KiCad
versions, arbitrary regex syntax, GUI round trips or custom class-pair rules.

## Explicit assignment coverage

`netclass_assignments` supports the KiCad 10.0.5 object mapping net-name strings
to arrays of class-name strings. All targets, including duplicate targets, are
inspected for definition presence; malformed array members are diagnosed
individually. Missing/null assignments mean no explicit entries. Other forms,
including historical string-valued assignments, are explicitly unsupported.
Legacy `classes[].nets` fields are also flagged unsupported, rather than
silently implying complete coverage. No migration or writer behavior changes.

## Reproducible native evidence

The recorded corpus is in `tests/fixtures/netclass_diagnostics/oracle.json`.
It was generated using **KiCad 10.0.5**, native Linux amd64 `pcbnew`, through
`NET_SETTINGS.SetNetclassPatternAssignment` and `GetEffectiveNetClass`.
Each pattern gets fresh settings and a single isolated `Probe` class so other
assignments and class precedence cannot mask membership. Exact pattern/net/result
triples include supported and deliberately unsupported syntax. Tests compare
supported results with the diagnostic and require unsupported rows to remain
unevaluated. Unsupported native results are evidence of the boundary, not claims
that the diagnostic implements those patterns.

Regenerate with KiCad's Python environment, from the repository root:

```sh
python3 tests/fixtures/netclass_diagnostics/capture_kicad.py > tests/fixtures/netclass_diagnostics/oracle.json
```

The capture script rejects any version other than 10.0.5. It performs no project
or board writes. The captured oracle is independent of this Python diagnostic.
Source references are pinned to the same release:

- [KiCad 10.0.5 `eda_pattern_match.cpp`](https://github.com/KiCad/kicad-source-mirror/blob/10.0.5/common/eda_pattern_match.cpp): `CTX_NETCLASS`, anchored regex/wildcard construction and `StartsWith`.
- [KiCad 10.0.5 `net_settings.cpp`](https://github.com/KiCad/kicad-source-mirror/blob/10.0.5/common/project/net_settings.cpp): assignment JSON loading and `GetEffectiveNetClass`.

Only behavior was studied; no KiCad implementation was copied into this MIT
module. No Konnect source was consulted or copied.
