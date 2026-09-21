# Access witness — format note

*Epic #5508, Phase 1b (issues #5517 / #5610).*

The **access witness** answers one question per routing terminal: *did a
committed route take away this pad's last legal way out, and if so, which one?*
It is produced by replaying the [commit journal](../reference/commit-journal.md)
against the pad-access predicate, offline and read-only — no search runs, no
routing decision is consulted, and attaching a witness cannot change a board.

It exists to replace a guess. `kct net-status --why` already emits a
`PLACEMENT_BOUND` / `CONGESTION_SATURATED` classification and a `blocking nets`
list, but both are inferred from the *finished* board: a line-of-sight scan
cannot distinguish copper that closed a pad from copper that merely ended up
near it, and it has no notion of *when* anything happened. The witness is
derived from the order copper actually landed in, so it can name a pass, an
iteration and a journal record.

## Where it lives

Inside the `<stem>.access_witness.json` sidecar `kct route` writes next to the
routed board, as a `witness` key beside the `journal` key:

```jsonc
{
  "schema_version": 1,              // journal schema
  "source": { "pcb": "board_routed.kicad_pcb" },
  "journal": { /* ordered commit records -- see commit-journal.md */ },
  "witness": {                      // absent when the run stranded nothing
    "schema_version": 1,            // witness schema, versioned independently
    "record_count": 261,
    "evaluations": 184,
    "truncated": false,
    "clearance": {
      "trace_width": 0.2,
      "trace_clearance": 0.2,
      "via_clearance": 0.2,
      "min_hole_to_hole": 0.5
    },
    "pads": [ /* one entry per tracked terminal */ ]
  }
}
```

A run that is **deadline-killed** (`kct route --timeout`, exit 124) writes one
too, beside its partial snapshot — `<stem>_partial.access_witness.json` (#5639).
Those runs are precisely the ones where "did a commit strand this pad?" is being
asked, so the interrupt save path serializes the journal first and attaches the
witness second: if the SIGTERM → SIGKILL grace window closes mid-replay, the
commit order still survives. The path is named in the run's `.timeout.json`
receipt under `access_witness`.

The two `schema_version` fields are deliberately independent: the journal's
record shape and the witness's verdict shape can move separately, and a reader
of one should not have to care about the other. An unrecognised version is a
hard error on load, never a silent partial parse.

## Per-pad fields

| Field | Meaning |
|-------|---------|
| `ref`, `pin` | The terminal, e.g. `U3` / `1` |
| `net`, `net_name` | Its net |
| `access_at_escape_end` | `non-empty` / `empty` / `not-evaluated` at the end of the escape pre-phase — the replay's baseline, and Epic #5508 Phase 2's reference point |
| `final_access` | The same vocabulary, after the last journal record |
| `first_closed_at` | `{"pass": ..., "iteration": ...}` of the record that first *took the access set from non-empty to empty*, or `null` — a pad that was already empty before the first record is never attributed to a later one (#5639) |
| `first_closed_index` | That record's index in `journal.records`, so it can be read verbatim |
| `first_closed_kind` | That record's kind: `commit`, `rip`, `restore` or `rollback` |
| `closing_nets` | Net names of the copper that rejected every remaining candidate. Copper that belongs to no net (the board outline, a keepout, an unconnected pad — KiCad net 0) is reported as the sentinel `<no-net>`, never as a synthetic `net0` (#5639); `closing_copper_class` and `closing_refs` say which it was |
| `closing_refs` | The same items as `ref.pin` labels (`ref` alone for route copper) |
| `closing_copper_class` | Phase 1a's classification: `foreign_pad`, `route_segment`, `route_via`, `fixed_fill`, `keepout`, `reserved_hard`, `kelvin_isolated`, `board_edge` |
| `closing_markings` | Raster markings read at that copper — **descriptive labels only**, never part of the legality decision |
| `reopened` | `true` when a later rip-up gave the terminal a way out again |

## Reading it correctly

Four combinations mean genuinely different things, and conflating them is the
mistake the witness exists to prevent:

| `access_at_escape_end` | `first_closed_at` | `final_access` | Verdict |
|---|---|---|---|
| `non-empty` | set | `empty` | **A commit stranded the pad.** The named record is the culprit |
| `non-empty` | `null` | `non-empty` | **Nothing stranded it.** The pad was reachable throughout and the search declined to reach it — issue #5509's category, not this epic's |
| `empty` | `null` | `empty` | **Placement stranded it.** The pad had no legal first move before the search committed anything; no commit can be blamed |
| `empty` | set | `empty` | **Placement stranded it, a rip-up briefly freed it, and a later commit closed it again.** The named record is a real closure — of the *re-opened* access set, not of the original one — so the pad's first cause is still placement |

The last row is narrow and was, until #5639, also produced spuriously: a pad
that was already empty before the search began got the first record that
merely *re-evaluated* it recorded as its closure (board-07's `U5.1`..`U5.8`
were "closed" by `board_edge` copper, which no commit can place). Attribution
now requires an observed non-empty → empty transition, so the row means only
what it says.

A `reopened: true` entry is a further case: the pad *was* stranded at
`first_closed_at`, a later rip-up freed it, and the closing record must not be
presented as the final cause. This is why rip-ups are journaled at all — a
commit-only journal would keep counting ripped copper and report a stranding
that no longer exists.

## Which terminals are tracked

By default, every pad of every net that ended the run unrouted: the union of
`Autorouter.get_failed_nets()` (nets with no committed copper — an escape stub
does not count) and the source/target pads of `Autorouter.routing_failures`
(which also catches a partially routed net whose last pad never landed).

A caller can name terminals explicitly instead — that is how the negative
control is written, since the point there is to interrogate a pad that is
*not* stranded.

## Cost and truncation

The replay is O(tracked pads) per journal record, and each evaluation is a full
geometric sweep over the copper committed so far. Two caps keep a diagnostic
from becoming the run's dominant cost:

- at most 64 terminals, taken in sorted `(ref, pin)` order so the truncation is
  deterministic;
- at most 5000 access-set evaluations.

Hitting either sets `truncated: true`. A pad the replay never reached reports
`not-evaluated` rather than `empty` — "we did not look" must never read as
"there was no way out".

## Known caveat (#5509)

The via-site branch of the access set is computed with today's clearance
resolver, which is why `clearance` travels with the verdict: until #5509 Phase
2 lands, a witness can be off by exactly the resolver's delta (0.15 vs 0.20 mm
on board 05). Phase 1c flags that by comparing the recorded numbers, rather
than assuming them.

## Also available from

- `kct net-status --why` (text and `--format json`) — see
  [`../reference/cli.md`](../reference/cli.md#-why-the-access-witness).
- `kct route --format json`, under the top-level `access_witness` key.
- `rescue_diagnostics.format_stranding_report`, as an indented `witness:` line
  per terminal.

All three render nothing when no sidecar / journal is available, so their
output is unchanged for a board that was never routed by `kct route`.
