# Operator decisions: ranked options, one helper (#9344)

**Load when**: you are about to put `loom:operator-decision` on an issue, or
you are repairing an issue labelled `loom:decision-malformed`.

An operator decision is not free prose. The operator should be able to read
the question, see the options you already weighed, ranked, and vote with one
click. The downstream dashboard (2AMLogic/loom-ui) validates this shape and
bounces anything else back to its author, swapping the label to
`loom:decision-malformed`. The convention is `/repo:decide`
(rjwalters/repo#486).

## The contract

- A one-line **question**, plus 1-2 lines of **context**.
- **2-4 options, ranked best to worst.** The first is the one you recommend.
- Every option has a **why**: what it wins, what it gives up compared with the
  options above it, and, for the last one, what makes it the worst.
- `recommended` equals the first option's `id`.

If you cannot name two genuinely different options, each with a reason, the
analysis is not finished, and the issue is not an operator decision yet. A
one-option "please advise" is refused.

## The helper

Never hand-apply `loom:operator-decision`. Write the decision as JSON and let
`loom-daemon operator-decision` validate it, render it into the body, and
apply the label:

```bash
cat > /tmp/decision.json <<'EOF'
{"question": "Should the cache move to SQLite?",
 "context": "The flat-file cache takes 4s to load at 50k entries.",
 "options": [
   {"id": "a", "label": "Move to SQLite",
    "why": "Fixes load time and adds indexed lookups; costs one migration."},
   {"id": "b", "label": "Shard the flat file",
    "why": "No new dependency, but only halves load time; worse than (a) at scale."},
   {"id": "c", "label": "Leave it",
    "why": "Zero work; worst because load time keeps growing with the cache."}],
 "recommended": "a",
 "deadline": "2026-10-10",
 "context_links": ["#1234"]}
EOF

# Check only (exit 0 valid, 1 refused with every reason on stderr):
loom-daemon operator-decision validate --input /tmp/decision.json

# Relabel an existing issue (preview first with --dry-run):
loom-daemon operator-decision apply 1234 --input /tmp/decision.json \
  --also-label loom:operator-only --remove-label loom:building

# Or file a new one:
loom-daemon operator-decision apply --new --title "Cache backend" \
  --input /tmp/decision.json --also-label loom:operator-only
```

`--input -` reads the JSON from stdin.

### What `apply` does

1. **Validates.** On any failure it prints every reason as a `REASON=` line on
   stderr and exits 1. No forge call is made and nothing changes.
2. **Renders** a managed section: a fenced ` ```decision ` JSON block (array
   order is the ranking), then a readable numbered list with the first option
   marked `(recommended)`.
3. **Writes the body first.** In relabel mode it re-reads the issue right
   before writing. The section goes at the top and the existing body is kept
   under `## Original report`. Re-applying replaces the section rather than
   stacking a second one, so the same input applied twice gives the same body.
   A hand-written ` ```decision ` block is replaced too.
4. **Labels only after the body write succeeds.** It adds
   `loom:operator-decision` and any `--also-label` labels. Then it removes any
   `--remove-label` labels that are present, and `loom:decision-malformed` if
   present, since a valid body is the repair.

`--dry-run` prints the planned body and label changes and makes no mutation.
`--new` files through `.loom/scripts/create-issue.sh`, so the duplicate
backstop and the filing lock apply. Its exit codes 3 (duplicate) and 75
(deferred) are passed through unchanged.

### Reason codes

| Code | Meaning |
|------|---------|
| `invalid_json` | The input is not decision JSON |
| `no_question` | `question` is empty |
| `too_few_options` | Fewer than 2 options |
| `too_many_options` | More than 4 options |
| `empty_id` / `empty_label` | An option has no `id` / `label` |
| `missing_why` | An option has no `why` key (option named) |
| `empty_why` | An option's `why` is empty or whitespace (option named) |
| `duplicate_id` | Two options share an `id` |
| `recommended_missing` | `recommended` is not set |
| `unknown_recommended` | `recommended` names no option |
| `recommended_not_first` | `recommended` is not the first (best-ranked) option |

### Exit codes

`0` valid or applied, `1` refused (contract), `2` bad usage or unreadable
input, `4` a forge read or write failed. With `--new`, `create-issue.sh`'s
`3`/`75` pass through.

## Repairing `loom:decision-malformed`

Curator owns the repair (#10057; see "Repairing `loom:decision-malformed`" in
the Curator role). The bounce comment names the failing reasons. Rebuild the
decision JSON from options already in the thread (never invent options) and
run `apply` on the issue. That rewrites the body, restores
`loom:operator-decision`, and clears the bounce label. If there is no real
operator call to reconstruct, do not force one through the helper: re-route
the issue per the Curator role instead.
