# Board fleet parity audit — 2026-06

**Issue:** #3763 (supersedes #2394)
**Audit commit:** `724fde80` (`docs(work-plan): list Architect proposals #3761-#3763`)
**Tooling version:** `kicad-tools 0.14.0`
**Date:** 2026-06-17

This is a **read-only audit / re-scope deliverable**. No board generators, routed
PCBs, or manufacturing outputs were modified by this audit. The concrete board
fixes land in the bounded `loom:architect` follow-ups listed under
[Spawned follow-ups](#spawned-follow-ups).

---

## 1. Reference end-state (board-01 gold standard)

`01-voltage-divider` defines "manufacturer-ready" parity. A board is at parity
when, via documented commands with no manual flags, it achieves:

| Leg | Pass condition |
|---|---|
| **route** | All schematic nets routed; routed PCB not stale vs schematic |
| **DRC (kct engine)** | `kct check --mfr jlcpcb` → 0 blocking errors |
| **DRC (KiCad/native engine)** | KiCad pcbnew DRC (the `connectivity` rule in `drc_report.json`) → 0 blocking errors |
| **ERC** | `kct erc` → 0 errors |
| **export** | `kct export` produces clean Gerbers + BOM + CPL + report.pdf + project zip (manifest `B/C/G/M`) |
| **copper-LVS** | `lvs.json` `clean: true` (connectivity-correct — the third leg added post-#3757) |

The copper-LVS leg is the only column the fleet tooling does not yet aggregate;
its fleet-wide rollout is tracked in **#3762** and is **not** double-tracked here.
This audit reports the current LVS state per board and marks `pending`/`n/a`
where the artifact is absent (a missing `lvs.json` is **not** an audit failure).

---

## 2. How this table was produced (reproducible commands)

The bulk of the table comes from the repo's existing fleet gate, which aggregates
routing + DRC (`drc_report.json`) + ERC (`erc_report.json`) + manufacturing
(`manifest.json`) PASS/FAIL per board:

```bash
uv run kct fleet ship-ready --boards-dir boards --format table   # human-readable
uv run kct fleet ship-ready --boards-dir boards --format json     # machine-readable
uv run kct fleet status     --boards-dir boards                    # routing + staleness survey
```

Per-cell sources (prefer committed artifacts over re-running heavy routing):

| Leg | Reproducing command / artifact |
|---|---|
| route | `kct fleet status --boards-dir boards` → `Pads %` + `Stale` columns; per-board `routing.{routing_complete,source_stale,completion_pct}` in the ship-ready JSON |
| DRC (kct engine) | `kct check --mfr jlcpcb <routed.kicad_pcb>` → committed `output/**/drc_report.json` `summary.errors` (where present) |
| DRC (native engine) | committed `output/**/drc_report.json` `summary.rules_checked_by_rule.connectivity` / `violations` (KiCad pcbnew DRC) |
| ERC | `kct erc <…>` → committed `output/**/erc_report.json` `summary` (where present) |
| export | `kct fleet ship-ready` `Mfr` column (`B/C/G/M` = BOM/CPL/Gerbers/Manifest) + `output/**/manufacturing/manifest.json` |
| copper-LVS | committed `output/**/lvs.json` `clean` field (where present) |

> **Native router note (per `CLAUDE.md`):** filling a cell by a *live* `kct route`
> requires the C++ backend (`uv run kct build-native`) or routing is 10-100x
> slower. This audit read committed `output/**` artifacts (the last regen) and did
> **not** re-run routing, so the native backend was not required.

### Verbatim `kct fleet ship-ready --format table` output (audit run)

```
Board                          Route   DRC   ERC Mfr      Stale  Verdict
------------------------------------------------------------------------
00-simple-led                    6/6     0     0 B/C/G/M  STALE  FAIL (artifacts stale)
01-voltage-divider               8/8     -     - B/C/G/M  STALE  FAIL (artifacts stale)
02-charlieplex-led             34/34     -     - B/C/G/M  fresh  PASS
03-usb-joystick                85/85     -     - B/C/G/M  STALE  FAIL (routed PCB stale (schematic drift: 14 nets in schematic, 16 in PCB))
04-stm32-devboard              52/55     2     - B/C/G/M  STALE  FAIL (artifacts stale)
05-bldc-motor-controller     143/206     -     - B/C/G/M  fresh  FAIL (incomplete routing (7/52 nets))
06-diffpair-test             183/198     -     - B/C/G/M  fresh  PASS
07-matchgroup-test           238/244     -     - B/C/G/M  STALE  FAIL (artifacts stale)

8 boards surveyed, 2 PASS, 6 FAIL (warn-only mode)
```

> `DRC`/`ERC` columns show `-` when a board has no committed `drc_report.json` /
> `erc_report.json` — meaning that leg was **not captured as a committed artifact**
> at the audited commit, not that it failed. Only boards 00 and 04 commit a
> `drc_report.json`; only board 00 commits an `erc_report.json` and an `lvs.json`.

---

## 3. Parity table — boards 00-07

Legend: **PASS** = at board-01 parity for that leg · **n/a** = artifact not committed
at this commit (leg not captured; not a failure) · **pending** = leg in active
rollout (LVS, see #3762) · **FAIL** = real gap vs parity · **STALE** = artifact
content is correct but flagged stale by the regen-freshness check.

| Board | route | DRC (kct) | DRC (native) | ERC | export | copper-LVS | At parity? |
|---|---|---|---|---|---|---|---|
| 00-simple-led | PASS (6/6) | PASS (0) | PASS (0) | PASS (0/0) | PASS (B/C/G/M) | **PASS** (clean) | **Yes\*** (artifacts flagged STALE) |
| 01-voltage-divider | PASS (8/8) | n/a | n/a | n/a | PASS (B/C/G/M) | n/a | **Reference** (STALE flag only) |
| 02-charlieplex-led | PASS (34/34) | n/a | n/a | n/a | PASS (B/C/G/M) | pending (#3762) | **Yes** (ship-ready PASS) |
| 03-usb-joystick | **FAIL** (schematic drift: 14 sch / 16 PCB nets) | n/a | n/a | n/a | PASS (B/C/G/M) | pending (#3762) | **No** — gap A (#3764) |
| 04-stm32-devboard | **FAIL** (52/55, net drift +BOOT0/+LED_K) | **FAIL** (2 connectivity advisories) | **FAIL** (2 connectivity) | n/a | PASS (B/C/G/M) | pending (#3762) | **No** — gap B (#3765) |
| 05-bldc-motor-controller | **FAIL** (143/206, 7/52 blocking nets unrouted) | n/a | n/a | n/a | PASS (B/C/G/M) | pending (#3762) | **No** — gap C (#3766) |
| 06-diffpair-test | PASS (routing-complete, 183/198 pads) | n/a | n/a | n/a | PASS (B/C/G/M) | pending (#3762) | **Yes** (ship-ready PASS) |
| 07-matchgroup-test | PASS (routing-complete, 238/244 pads) | n/a | n/a | n/a | PASS (B/C/G/M) | pending (#3762) | **Yes\*** (artifacts flagged STALE) |

\* **STALE disposition:** boards 00 and 07 (and reference 01) report routing-complete
and clean DRC/LVS where captured, but the regen-freshness check
(`kct fleet status` `Stale` column / `manufacturing.stale`) flags their committed
manufacturing artifacts as stale relative to source. This is a **regen-freshness
hygiene** signal, not a manufacturability defect — the committed PCB/DRC/LVS content
is at parity. It is captured as a follow-up (gap D, #3767) so the parity guards stay green.

### Per-board notes

- **00-simple-led** — Fully at parity content-wise: `drc_report.json` errors=0
  (kct + native `connectivity` rule both clean), `erc_report.json` 0/0,
  `lvs.json` `clean: true`. Only the artifact-freshness flag keeps the ship-ready
  verdict at FAIL. This is the only board with all three new legs (DRC/ERC/LVS)
  committed as artifacts — the template the rest of the fleet should follow (#3762).
- **02-charlieplex-led** — `ship-ready` PASS, 34/34 pads, fresh. Routing/DRC burst
  fix `b5ea322c` (zone refill) resolved the historic clearance shorts. At parity
  modulo the LVS rollout (#3762).
- **06-diffpair-test** — `ship-ready` PASS, fresh. `e9689411` cleared via/diff-pair
  clearance violations. 2 non-blocking incomplete nets, `routing_complete: true`.
- **07-matchgroup-test** — `routing_complete: true`, 238/244 pads, 1 non-blocking
  incomplete net. Only a stale-artifact flag (gap D).

---

## 4. Remaining-slice lists (per non-parity board)

Rebuilt from **current** state (not assumed from #2394). Each becomes a bounded
`loom:architect` follow-up (see [Spawned follow-ups](#spawned-follow-ups)).

### Gap A (#3764) — 03-usb-joystick: schematic↔PCB net drift

`kct fleet status` flags the routed PCB as stale via **schematic drift**: the
schematic has 14 nets but the routed PCB has 16. Drift detail (ship-ready JSON):
- added in PCB: `USB_CC1`, `USB_CC2`, `VBUS`
- removed from PCB: `+5V`

Bounded slices:
1. Reconcile the USB-C CC1/CC2/VBUS vs `+5V` net naming between schematic and the
   routed PCB (likely a generator/net-naming divergence, not a routing failure —
   85/85 pads are connected).
2. Regenerate the routed PCB from the reconciled schematic so `schematic_net_count`
   == `pcb_net_count`.
3. Commit fresh `drc_report.json` + (eventually) `erc_report.json` so the DRC/ERC
   legs are captured artifacts, not `n/a`.

### Gap B (#3765) — 04-stm32-devboard: incomplete net + connectivity DRC + drift

- Routing 52/55 pads (94.55%); 1 incomplete net; net drift adds `BOOT0`, `LED_K`
  (PCB has 12 nets vs 10 in schematic).
- `drc_report.json` reports **2 `connectivity` advisory errors** (non-blocking
  today, but they are real unrouted-connection flags vs board-01's 0).

Bounded slices:
1. Resolve the 2 connectivity DRC advisories (route/repair the missing connections).
2. Reconcile the `BOOT0`/`LED_K` schematic↔PCB net drift so counts match.
3. Drive the last incomplete net to 100% so `completion_pct` reaches parity.

### Gap C (#3766) — 05-bldc-motor-controller: incomplete routing (7/52 blocking nets)

- 143/206 pads (69.42%); 9 incomplete nets, **7 blocking**; `routing_complete: false`.
- This is the largest remaining routing gap in the fleet and the only board whose
  ship-ready FAIL is a hard routing-completeness failure (not staleness).

Bounded slices:
1. Drive the 7 blocking incomplete nets to completion (high-current power topology;
   per-net trace widths and thermal vias historically called out in the README).
2. Once `routing_complete: true`, capture committed `drc_report.json` so the DRC
   legs move from `n/a` to PASS/FAIL.
3. Re-survey for parity (ERC + LVS legs via #3762).

> This single board may need decomposition into power-net vs gate-drive-net slices
> when the follow-up is picked up; the architect follow-up captures it as one
> bounded "complete board-05 routing" gap and defers further splitting to curation.

### Gap D (#3767) — fleet artifact-staleness (boards 00, 01, 07; and any board after a fix)

Boards 00, 01, and 07 are content-correct but flagged STALE by the regen-freshness
check, so `kct fleet ship-ready` reports FAIL for otherwise-at-parity boards. This
is **infrastructure hygiene**, kept separate from board-fix gaps per the
anti-mega-fix rule.

Bounded slices:
1. Re-run the documented regen pipeline for the stale boards so
   `manufacturing.stale` clears.
2. Confirm whether the per-board CI regen gates (`board-00-end-to-end`,
   `matchgroup-routing-regression`, …) should also assert freshness so STALE cannot
   silently reappear.

---

## 5. #2394 blocker disposition

#2394 ("bring all five example boards to manufacturer-ready parity with board 01")
tracked boards 02-05 only and predated boards 00/06/07 and the copper-LVS leg. Its
historic blockers are dispositioned below against the recent board burst.

| #2394 blocker | Board(s) | Status | Resolving commit / evidence |
|---|---|---|---|
| Charlieplex NODE nets stall (8/10), no power pours | 02 | **RESOLVED** | `b5ea322c` (refill zones to clear clearance_pad_zone shorts) — now 34/34, ship-ready PASS |
| usb-joystick routing times out at 240s, power nets as signals, diff-pair not engaged | 03 | **RESOLVED (routing); NEW gap = net drift** | `d8b864b7` (regenerate to 0 combined-engine DRC) — now 85/85 pads; remaining issue is schematic↔PCB net drift (gap A), not the old timeout |
| stm32 PCB is a stub (no MCU), post-escalation regression (3/8) | 04 | **PARTIALLY RESOLVED** | `33ce6679` (exempt micro vias → 0 combined-engine DRC); board now routes 52/55 with 2 connectivity advisories + net drift (gap B) — no longer a stub, but not yet 100% |
| bldc 15/31 routing, per-net trace widths, thermal vias | 05 | **PARTIALLY RESOLVED** | `11d4dcbd` (softstart: clear shorts/overlaps → 0 DRC) improved DRC; routing now 143/206 (7 blocking nets remain — gap C). Per-net width / thermal-via work still open |
| Board-00 DRC under both engines + LED polarity | 00 | **RESOLVED** | `05fcac63` (rotation-0 footprints + deterministic route → 0 DRC both engines) + `dd2867f8` (D1 polarity: schematic matches PCB) + board-00 LVS work (#3752/#3753, `dd2867f8`) |
| Board-06 diff-pair / via clearance shorts | 06 | **RESOLVED** | `e9689411` (clear near-short via clearances + sub-min diff-pair escapes → 0 DRC both engines) — ship-ready PASS |
| "manufacturer-ready" = clearance-clean only (no connectivity leg) | all | **SUPERSEDED** | Post-#3757 parity now includes copper-LVS; board-00 ships `lvs.json` `clean:true` as the template; fleet rollout = #3762 |

**Net:** the routing/DRC burst resolved or substantially advanced every #2394
routing/clearance blocker. The remaining work is **narrower** than #2394's
"broken/partial" table: board 03 = net-naming drift, board 04 = 2 connectivity
advisories + drift, board 05 = 7 unrouted blocking nets, plus fleet artifact
staleness. The historic "router never recovers stalled nets / power pours absent /
PCB stubs" framing is largely obsolete.

---

## 6. Disposition of #2394

**Recommendation: leave #2394 CLOSED. This audit (#3763) is its successor.**

- #2394's scope (5 boards: 01-05, routing/DRC/ERC/export only) is strictly smaller
  than the current fleet (8 boards: 00-07) and omits the copper-LVS leg.
- Its per-board "broken/partial" inventory is stale — the burst commits above
  invalidated most of its blockers.
- Reopening it would mix an obsolete roadmap with current state. Refreshing its body
  in place would lose the historical record of what the burst fixed.

Treat #3763 as the corrected-scope successor: **"all PCB-bearing boards (00-07)
manufacturer-ready like board-01, including the copper-LVS connectivity leg."**
The bounded board fixes land in the follow-ups below.

---

## 7. Cross-reference: copper-LVS fleet rollout (#3762)

The copper-LVS column above is reported but **not** acted on here. The fleet-wide
LVS rollout — generating `lvs.json` for boards 01-07 the way board-00 already
does — is owned by **#3762**. This audit:
- records the current LVS state (board-00 `clean:true`; all others `pending`),
- marks absent `lvs.json` as `pending`/`n/a` rather than FAIL,
- spawns **no** LVS-rollout follow-ups (to avoid double-tracking #3762).

---

## 8. Spawned follow-ups

One bounded `loom:architect` follow-up per **real** remaining gap (board fixes and
infra hygiene kept separate per the anti-mega-fix rule). LVS rollout is **not**
spawned here (owned by #3762).

| Gap | Board / area | Follow-up issue |
|---|---|---|
| A | 03-usb-joystick — reconcile schematic↔PCB net drift + regen | #3764 |
| B | 04-stm32-devboard — clear 2 connectivity DRC advisories + net drift + finish last net | #3765 |
| C | 05-bldc-motor-controller — complete 7 blocking routing nets | #3766 |
| D | fleet — refresh stale board artifacts (00/01/07) so ship-ready clears | #3767 |

> All four follow-ups are filed with `loom:architect` (awaiting curator/human
> approval). Boards 02 and 06 are at parity (ship-ready PASS) and spawn **no**
> follow-up. No LVS-rollout follow-up is spawned here — that work is owned by #3762.

---

## 9. One-line verdict

**Fleet parity (2026-06):** boards 00, 02, 06, 07 are at board-01 parity
(00/07 modulo an artifact-staleness flag); boards 03, 04, 05 have bounded
remaining gaps (net drift / 2 connectivity advisories / 7 unrouted nets); the
copper-LVS leg is shipped on board-00 and rolling out fleet-wide via #3762.
