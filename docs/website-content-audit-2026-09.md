# Website content, navigation and presentation audit — 2026-09

**Issue:** #5281 (Epic #5278 Phase 1, item 1 of 3 — parallel with #5279, #5280)
**Verified at:** `6deffd41` (2026-09-12)
**Live site checked:** `https://kicad-tools.org` (build `c33e891b`, 2026-09-10 13:43 -07:00)
**Scope:** read-only audit / implementation-specification deliverable. No site
code, board data, or benchmark reports are modified by this document — see
[Section 6](#6-prioritized-phase-2-recommendations) for the bounded follow-ups
it proposes.

> **Citation style.** Claims below cite a file path and, where useful, a
> function/component/field name — never a line number — so they survive
> refactors (repo convention; see `tests/test_docs_source_citations.py`).
> Verify any claim with `rg -n '<name>' <path>`.

---

## 0. Method

This audit inspected, in order:

1. The Astro source for every page and shared component under `site/src/pages`,
   `site/src/components`, and `site/src/data` (all three pages: `index.astro`,
   `[slug].astro`, `benchmarks.astro`; all four components: `Header.astro`,
   `Footer.astro`, `BoardCard.astro`, `RenderGallery.astro`; the full data
   layer: `types.ts`, `loadBoards.ts`, `boardStatus.ts`, `readiness.ts`,
   `loadBenchmarks.ts`, `benchmarkTypes.ts`, `benchmarkFormat.ts`,
   `deepPcbReference.ts`, `galleryConfig.mjs`).
2. The live, currently-deployed site at `kicad-tools.org` (fetched directly —
   both `/` and `/benchmarks` — to see what visitors actually see today, not
   just what the templates could produce).
3. A from-scratch local build (`cd site && npm ci && npm test && npm run
   build && npm run check`) on a **fresh checkout with zero generated board
   artifacts**, to see the worst-case / typical-contributor experience, and to
   independently verify claims about reproducibility.
4. The underlying repository evidence each page's claims should trace back
   to: `boards/*/README.md` and `boards/*/output/readiness.json`,
   `benchmarks/external/results/*.json` and `results/README.md`,
   `boards/README.md`, `site/README.md`, `pyproject.toml`, `CHANGELOG.md`,
   `scripts/deploy-site.sh`, and `.github/workflows/ci.yml`.

All four steps succeeded without code changes: `npm test` — 6 files / 66
tests passed; `npm run build` — 12 static routes generated; `npm run check`
— 0 errors, 0 warnings (98 pre-existing `is:inline` hints, unrelated to this
audit). The site's code health is good. **This audit's findings are about
content, discoverability, and framing — not defects in the Astro/TypeScript
implementation.**

---

## 1. Inventory of primary pages and visitor paths

There are exactly three visitor-facing routes, plus one per board:

| Route | Source | Purpose |
|---|---|---|
| `/` | `site/src/pages/index.astro` | Gallery index: capabilities blurb + one card per board (`BoardCard.astro`) |
| `/<slug>` | `site/src/pages/[slug].astro` | Per-board detail: renders, readiness, metrics, interactive viewer, downloads |
| `/benchmarks` | `site/src/pages/benchmarks.astro` | External autorouter benchmark comparison vs. DeepPCB |

Every page shares `Header.astro` (site title + `/benchmarks` link + "View on
GitHub") and `Footer.astro` (build commit SHA, milestone badge, copyright,
KiCad trademark disclaimer, kicad.org links). There is no fourth page: no
docs/quickstart page or "about" page. The shared navigation has no prominent
documentation entry point: "View on GitHub" goes to the repo root. Contextual
links do exist on the benchmarks page, including
`docs/benchmark-external-report-schema.md` and
`benchmarks/external/README.md`; they serve benchmark readers rather than
providing a general getting-started path.

### 1.1 Content staleness vs. deployment staleness (these are different axes)

The live site's footer (`Footer.astro`) shows `Build: c33e891b · Milestone:
v0.19.0`. Two independent findings fall out of comparing that to the
repository:

- **Deployment staleness is currently small.** `c33e891b` is a real commit
  (`chore: resync installed Loom surfaces`, 2026-09-10 13:43 -07:00), 153
  commits and about two days behind this audit's `HEAD` (`6deffd41`,
  2026-09-12). Deployment is entirely manual — `site/README.md`'s
  "Deploying" section and `scripts/deploy-site.sh`'s own header comment both
  say there is **no CI auto-deploy**; a human runs `./scripts/deploy-site.sh`
  by hand whenever they choose to. Nothing on the site records *when* that
  last happened beyond the commit SHA, so a visitor (or the next builder) has
  no way to tell "deployed today" from "deployed six weeks ago" without
  manually cross-referencing GitHub commit timestamps. **Finding F3.**
- **Content staleness is real and independent of deployment recency.** The
  homepage's version badge and capability-list copy in `index.astro` are
  hardcoded to `v0.19.0` in three places (the `<meta name="description">`,
  the `.version-badge` span, and the "the 0.19.0 pipeline adds..." lead
  sentence), and `Footer.astro` hardcodes `Milestone: v0.19.0` in a fourth.
  `pyproject.toml`'s `version` field is `0.20.0` — a full release ahead —
  and `CHANGELOG.md`'s `[0.20.0]` entry describes real, shipped work (a
  targeted `kct route --complete` completion pass, route-time HV-isolation
  enforcement across every routing engine, a general `.kct_waivers.json`
  waiver mechanism) that the homepage's "Capabilities" section never
  mentions. This is a **content** staleness problem — the copy was not
  updated at the last release — not a deployment-lag problem: even a same-day
  deploy from `HEAD` today would still say "v0.19.0" until someone edits the
  four hardcoded strings by hand. **Finding F2.**

### 1.2 Board discoverability: reproducibility gap and a hidden-work gap

The `/` and `/<slug>` routes are driven entirely by `loadBoards.ts`, which
discovers directories under `boards/` and reads `output/board.json` (never
committed — generated by `kct board-metrics --all`) plus
`output/readiness.json` (**committed** — nine reports are tracked, for
boards 00–07 and 09 — and validated at build time by `readiness.ts`'s
`loadReadiness`). These two files differ in exactly the way that matters
here: a fresh checkout contains every tracked readiness report but no
`board.json` at all. Board09's readiness evidence is also *current*, not
merely present: all 20 of its declared input hashes match the committed
files at this revision. The gap is therefore in the consumer, not the
evidence — `loadBoard` returns a `no_artifacts` stub as soon as
`board.json` is missing, without ever calling `loadReadiness`, so the
committed readiness report never reaches a visitor. Two concrete, verified
findings:

- **F4 — real, verifiable work is invisible without extra tooling.**
  `boards/09-usbc-pd-power` is an actively-developed, real assembled-demo
  design (its own `README.md`: "This is a real assembled-demo design, not a
  synthetic routing fixture"). Its `output/readiness.json` (`status:
  "blocked"`, correctly — manufacturing is not ready) already records ten
  granular checks, eight of which pass: `native_erc_clean`,
  `native_drc_geometry_clean`, `critical_routes_connected`,
  `inner_ground_planes_valid`, `all_nets_connected`, `label_lvs_clean`,
  `copper_lvs_clean`, `analytical_screen_passed`. That is a fully-routed,
  DRC/ERC/LVS-clean board — genuinely representative, well-verified work.
  None of it reaches the site unless someone has already run `kct
  board-metrics --all` locally before building. Verified directly: a fresh
  `npm run build` in this worktree (no `board.json` anywhere) renders
  `09-usbc-pd-power` as the generic "not been built yet" stub (`isBuilt`
  false in `[slug].astro`), while the currently-*live* site (built on
  whoever's machine last ran `deploy-site.sh` after generating metrics)
  shows it with a "Needs work" chip and an "Interactive view" badge. The
  same board, two very different-looking cards, entirely dependent on a
  manual step nothing on the site surfaces.
- **F6 — the flip side, correctly unverified.** `boards/08-precision-acquisition`
  legitimately has no `readiness.json` at all — its own `README.md` says
  "Schematic, procurement and PCB implementation remain pending" — so
  its "Readiness unverified" / "No artifacts" card is accurate. No fix
  needed there; it is included here to show the loader is not
  systematically under- or over-reporting, just that F4's case (readiness
  data that exists but isn't surfaced without `board.json`) is a real gap.
- **F5 — a card that can never be reproduced, and is currently confusing.**
  `boards/external/softstart` is a **local-only symlink**
  (`../../../softstart/hardware/kicad`) to a private sibling repository,
  exactly as documented in `boards/README.md`'s "External Boards" section:
  "local-only symlinks ... not available in CI or fresh worktrees — they
  dangle unless the sibling repos are checked out locally." Unlike its
  sibling `chorus-test-revA`, `softstart` is **not** in
  `galleryConfig.mjs`'s `EXCLUDED_SLUGS`. Verified both ways: this audit's
  fresh worktree build (no sibling `softstart` checkout present) produced
  **10 boards, no `/softstart` route at all** (`discoverBoardDirs` silently
  skips the dangling symlink via `isDir`); the *live* site — built on a
  machine where the sibling repo happens to exist — shows an 11th card,
  `softstart`, under "Project gallery — Real-world boards built with
  kicad-tools," with **no thumbnail, no description, and no metrics**, just
  a bare "Readiness unverified" chip. That card is simultaneously (a)
  non-reproducible — no other contributor's build will ever show it, and
  (b) actively confusing to a visitor when it does appear, since it carries
  no information at all. It also happens to be a mains-voltage HV design
  with documented open creepage/leak findings
  (`docs/hv-pairwise-softstart-proof.md`) — not something that should be
  one accidental gallery inclusion away from public display before its
  owner chooses to publish it.

### 1.3 Benchmarks page

`benchmarks.astro`'s current content is the subject of #5279 (presentation)
and #5280 (report-contract semantics) and this audit does not re-litigate
their scope. Independently verified against
`benchmarks/external/results/README.md` and the two committed JSON reports:
every claim in the epic's "Observed baseline" section is accurate — both
committed runs (`pocketbeagle.zero-touch.json`,
`beagleconnect_freedom.zero-touch.json`, both `generated_at:
"2026-08-25T..."`) refused to route before laying any copper
(`copper.via_count: 0`, `copper.wirelength_mm: 0`), and their headline
31.8%/32.6% `completion.completion_pct` describes pre-route connectivity in
the stripped input, not router output — exactly as `results/README.md`
itself already says in prose ("The `completion_pct` figures ... are **not**
router output"). The live page does not carry that caveat forward into its
headline table; that correction is #5279's job. This audit's own
contribution here is narrow: see **F12** in §4 for a terminology point that
applies equally to `benchmarks.astro`'s methodology/failure-note copy and to
the gallery.

---

## 2. Homepage content recommendations

**F1 — the homepage never explains what kicad-tools *is*.** `index.astro`
opens straight into "kicad-tools demo gallery" / "PCB designs produced
end-to-end by the kicad-tools automated pipeline" and a bulleted
"Capabilities" list of CLI subcommands (`kct creepage --voltage-map`, `kct
zones hv-keepout`, ...). All of those commands genuinely exist in
`src/kicad_tools/cli/` — the claims are accurate — but a visitor arriving
with no prior context never learns the one-sentence framing the project's
own `README.md` leads with: **"Tools for AI agents to work with KiCad
projects"** — a Python toolkit/CLI that lets AI agents and automation parse,
analyze, and manipulate KiCad schematic/PCB files programmatically, with the
gallery as evidence of what it can produce, not the product itself. Recommend
adding one short, plain paragraph above the "Capabilities" section, adapted
directly from `README.md`'s existing framing (no new claim — just surfacing
one that already exists and is accurate) so the two surfaces describe the
project consistently.

**Do not promote a feature solely because its issue is closed.** The current
"Capabilities" list already passes this bar reasonably well — each bullet
names a real, currently-invocable CLI surface rather than an issue number —
and should stay evidence-first in any refresh: prefer "here is the command
and what it does" over "here is what issue #NNNN shipped."

**Representative examples.** The best current "credible example" the
homepage has is the "Demo boards" grid itself once `board.json` exists —
each card links through to a real render + metrics + downloadable
manufacturing package. That chain is good and should be preserved. The gap
is `09-usbc-pd-power` (§1.2, F4): it is exactly the kind of "active,
non-trivial, well-verified" example the homepage should be able to point to,
and currently cannot without a manual local step.

---

## 3. Gallery/board status semantics

The status model (`boardStatus.ts`'s `displayStatus` / `displayStatusLabel`,
backed by `readiness.ts`'s content-hash-validated `Readiness` contract) is
**already conservative in the right direction**: it never lets a stale
`board.json` status read as "Ready" once `readiness.json`'s hashes disagree
with current files (`loadReadiness` throws on any hash mismatch and falls
back to `unverified`), and `loadBoards.ts`'s `loadBoard` explicitly refreshes
`drc_violations` / `nets_routed_pct` / `lvs_clean` from the *current*
readiness metrics rather than trusting an old export. `[slug].astro` adds a
direct, honest caveat when a board is not `ready`: *"Downloads are design
artifacts and are not verified for ordering."* This is good precedent and
should be the model for the rest of the site's status language.

The gap is not correctness, it is **legend/explanation** (this is the same
gap as F13 below): a visitor sees chips like "PCB fabrication ready",
"Assembly ready", "LVS: 2 mismatches", "Readiness unverified", "Needs work",
"Partial", "No artifacts" with no in-page explanation of what has and has
not been checked for each. None of the existing labels claim more than the
underlying `checks` array supports (e.g. `readiness.status === "ready"` is
gated in `readiness.ts` behind requiring `kct_check`, `native_drc`,
`artifacts`, and `bom` checks *and* zero failed checks), so the underlying
guarantee is sound — routed/DRC/LVS/fabrication-ready status genuinely
cannot be confused with plain implementation progress in the data model.
What's missing is making that guarantee legible to a visitor who has not
read `boardStatus.ts`. See **Recommendation 2F**.

One additional, narrow finding: `[slug].astro`'s metrics table already
distinguishes stale export-only data from currently-revalidated data via its
label text (`"Export report nets routed"` vs. `"Nets routed"`, driven by
`board.readiness?.metrics?.nets_routed_pct !== undefined`) — this is exactly
the "distinguish implementation progress from verified status" mechanism
criterion 3 asks for, and it already exists in code. It is easy to miss as a
label-text difference alone; **Recommendation 2F**'s legend should call this
distinction out explicitly rather than relying on visitors noticing the word
"Export."

---

## 4. Navigation structure and terminology

**F11 — no prominent general documentation entry point.** `Header.astro`'s
nav is exactly `kicad-tools` (home) / `Benchmarks` / `View on GitHub` (repo
root). The benchmarks page links to its report-schema documentation and
reproduction README, but the shared navigation and homepage lack a
general documentation or getting-started link. See **Recommendation 2G**.

**F12 — visitor-facing copy leans on raw issue numbers as its primary
explanation.** `benchmarks.astro`'s methodology section and its
`FAILURE_NOTES` annotations, and to a lesser extent `index.astro`'s
Capabilities bullets, cite GitHub issue numbers ("see Epic #4932", "Filed as
#4945", "#4946") as load-bearing parts of the sentence, not as an optional
"see more" link. This is the literal thing Epic #5278's own success
criteria ask to avoid: *"without requiring familiarity with Loom issue
history."* The underlying engineering honesty this buys (a named,
followable, non-board-specific tracking issue for every known gap) is
valuable and should not be removed — the fix is ordering: every citation
should be preceded by a plain-language sentence that stands on its own, with
the issue number kept as a secondary, followable reference. This applies
equally to `benchmarks.astro` (coordinate with #5279, which already owns a
rewrite of that exact copy) and to any Phase 2 gallery copy.

**Proposed concise navigation structure** (bounded — no new frameworks, no
new top-level IA):

```
Home (/)            — plain "what is this" intro, capabilities, gallery
  └─ /<slug>         — per-board detail (unchanged structure)
Benchmarks (/benchmarks) — external autorouter results (per #5279/#5280)
Documentation (external link to README.md, or a future docs/ entry point)
GitHub (external, unchanged)
```

This keeps the current two-level structure (index → detail) and adds exactly
one new, low-risk nav entry. Phase 3's success criterion — "record the final
benchmark navigation decision" — remains explicitly out of scope for this
audit and for Phase 2; whether `/benchmarks` stays a top-level link or moves
elsewhere is deferred to Phase 3 as the epic specifies.

**Terminology consistency.** Status vocabulary is already single-sourced
(`boardStatus.ts`) and used identically by `BoardCard.astro` and
`[slug].astro` — no drift found there. The one inconsistency worth fixing is
tone/register: board `README.md` files (e.g. `09-usbc-pd-power`'s "output
rating is a design target until electrical and thermal bench tests") are
written in the same careful, non-overclaiming register the site's status
labels use, but the homepage's "Capabilities" bullets read more like
marketing copy ("Smarter via repair"). This is a minor, non-blocking
observation, not a defect — flagged for Phase 2's copy pass to keep register
consistent site-wide.

---

## 5. Mobile, typography, spacing, accessibility, consistency

Assessed from source (markup + CSS) and a static build; **no live-browser or
real-device rendering was performed in this environment** (no browser
available) — treat the items below as "found in source," not "confirmed on
a rendered viewport," and re-verify visually in Phase 2.

**Working well, worth preserving:**
- `benchmarks.astro`'s results table already handles overflow correctly:
  `min-width: 900px` inside a `.table-scroll { overflow-x: auto }` wrapper
  gives horizontal scroll on narrow viewports instead of squeezed,
  unreadable columns. Keep this pattern in any redesign.
- Status is never color-only: every `.chip-*` variant pairs its color with
  distinct text (`displayStatusLabel`), so colorblind visitors are not
  relying on hue alone.
- Every render/thumbnail `<img>` has descriptive `alt` text, including an
  explicit `"No render available for <name>"` / `"<label> render not
  available"` phrasing for placeholders (`BoardCard.astro`,
  `RenderGallery.astro`) — this is good existing practice.
- The interactive PCB viewer (`[slug].astro`) already has a real
  loading/error/timeout state machine (poll `embed.loaded`, 8s hard
  timeout, explicit fallback message pointing at the static renders) so a
  slow or failed KiCanvas load never leaves a blank box — solid, no action
  needed.
- Focus states are handled via `:focus-visible` (not just `:hover`)
  throughout `Header.astro`, `Footer.astro`, `BoardCard.astro`.

**F14 — triplicated global styles.** `index.astro`, `[slug].astro`, and
`benchmarks.astro` each carry an independent, byte-for-byte-near-identical
`<style is:global>` block defining the same `:root` custom properties
(`--bg`, `--text`, `--muted`, `--accent`, `--border`, `--card-bg`,
`--badge-bg`) and base body/font rules. Not visitor-facing today (all three
copies currently agree), but any future palette or typography change has to
be made in three places simultaneously or the pages silently drift apart.
Recommend extracting to one shared layout/global stylesheet as a bounded,
independently-testable Phase 2 item (**Recommendation 2I**).

**F19 — missing small accessibility affordances.** No skip-to-content link,
and no `aria-current="page"` on the active nav link in `Header.astro`. Low
severity, cheap to add (**Recommendation 2J**).

**F16 — unverified, not asserted as broken.** `Header.astro`'s nav has no
narrow-viewport wrap rule or hamburger, relying on the short label set
("kicad-tools" / "Benchmarks" / "View on GitHub") fitting a
`justify-content: space-between` flex row down to small widths. Plausible
but not confirmed without a rendered check in this environment — flagged as
a to-verify item for Phase 2's device pass rather than a confirmed defect.

**Not assessed / no fresh measurement taken:** color-contrast ratios were
not computed against WCAG thresholds (visual inspection of `--muted`
`#9aa7b2` on `--bg` `#0a0e12` suggests high contrast, but this is not a
substitute for a computed check); no Lighthouse or axe run was performed.
Both are appropriate, low-cost additions to Phase 2's verification step
rather than this audit (see §7).

---

## 6. Prioritized Phase 2 recommendations

Each item below is independently implementable and scoped to specific
files. None require a fresh benchmark run, a new framework, or a branding
project. Benchmark-page/report-contract work is intentionally **not**
duplicated here — see #5279 and #5280 for that scope.

### P0 — cheap, high-leverage, no design decisions required

**2A. Exclude `softstart` from the public gallery.**
- *Affected:* `site/src/data/galleryConfig.mjs` (`EXCLUDED_SLUGS`).
- *Acceptance criteria:* a fresh checkout's build is identical regardless of
  whether the sibling `softstart` repo happens to be checked out locally
  (no `/softstart` route, no card, on any machine); mirrors the existing
  `chorus-test-revA` treatment.
- *Evidence needed:* none — `boards/README.md` already documents `softstart`
  as a local-only, non-CI, non-reproducible fixture.

**2B. Stop hand-maintaining the version badge.**
- *Affected:* `site/src/pages/index.astro` (three literal `v0.19.0`
  occurrences), `site/src/components/Footer.astro` (`Milestone:` line).
- *Acceptance criteria:* the displayed version is derived from
  `pyproject.toml`'s `version` field at build time (same
  build-time-Node-`fs` pattern `loadBoards.ts`/`loadBenchmarks.ts` already
  use), so the next release cannot ship without the badge updating itself;
  add a loader unit test asserting the parsed value is non-empty and
  well-formed.
- *Evidence needed:* none — `pyproject.toml` is the existing source of
  truth for the project version.

**2C. Show a build/deploy date distinct from any evidence date.**
- *Affected:* `site/src/components/Footer.astro`.
- *Acceptance criteria:* footer shows an explicit build timestamp (build
  wall-clock, not a data field) alongside the existing commit SHA;
  `site/README.md`'s "Deploying" section gains one sentence stating the
  manual deploy cadence expectation (e.g. "deploy after any site-content or
  release change; there is no schedule").
- *Evidence needed:* none.

### P1 — the "make hard-to-discover work visible" ask

**2D. Add a plain-language opening paragraph to the homepage.**
- *Affected:* `site/src/pages/index.astro`.
- *Acceptance criteria:* a reader with no prior repo/issue context can state
  in one sentence what kicad-tools is and who it's for, using the existing
  `README.md` framing ("Tools for AI agents to work with KiCad projects")
  rather than a new claim.
- *Evidence needed:* none — adapts existing, already-accurate README copy.

**2E. Surface in-progress, readiness-tracked boards without a manufacturing
package.**
- *Affected:* the Python producer for `output/board.json` /
  `output/readiness.json` (owner to confirm exact entry point —
  `kct board-metrics`), `site/src/data/loadBoards.ts` (currently short-circuits
  to a bare `no_artifacts` stub whenever `output/board.json` is absent,
  regardless of whether `output/readiness.json` exists), `boardStatus.ts`,
  `BoardCard.astro`.
- *Acceptance criteria:* a board with a valid, hash-verified
  `readiness.json` but no `board.json` (today's `09-usbc-pd-power` case)
  shows real per-check progress (e.g. which of ERC/DRC/LVS/manufacturing
  passed) instead of an undifferentiated "No artifacts" card, and this
  status is never conflated with `ready`/fabrication-ready.
- *Evidence needed:* confirm current `kct board-metrics` behavior when
  `board.json` is absent but `readiness.json` exists — this audit only
  inspected the TypeScript consumer side (`loadBoards.ts`); the Python
  producer side needs a look before scoping the fix precisely.

**2F. Add a status legend.**
- *Affected:* `site/src/components/BoardCard.astro`,
  `site/src/pages/[slug].astro` (a small shared legend component is a
  reasonable implementation choice, not mandated).
- *Acceptance criteria:* every status chip/label a visitor can see is
  explained in place with one plain sentence, reusing existing wording
  patterns (e.g. `[slug].astro`'s "Downloads are design artifacts and are
  not verified for ordering"); wording must never claim more than
  `readiness.json`'s `checks` array actually verified.
- *Evidence needed:* none — `boardStatus.ts`/`readiness.ts`'s existing
  contract already defines exactly what each status variant does and does
  not guarantee.

### P2 — navigation, terminology, and visual consistency

**2G. Add a documentation link to the shared nav.**
- *Affected:* `site/src/components/Header.astro`.
- *Acceptance criteria:* nav gains one link to a real, current document
  (default: `README.md` — see unresolved choice #3 in §7); still fits a
  narrow viewport (verify visually, not just in code).

**2H. Plain-language-first copy on issue-heavy passages.**
- *Affected:* `site/src/pages/benchmarks.astro` (methodology +
  failure-mode annotations) — coordinate with #5279, which already owns a
  rewrite of this exact copy; do not duplicate, just ensure whichever PR
  lands it also puts a self-contained plain-language sentence before each
  issue-number citation.
- *Acceptance criteria:* no sentence's meaning depends on the reader
  clicking through to a GitHub issue; issue links remain present as
  secondary evidence.

**2I. De-duplicate the three global style blocks.**
- *Affected:* `index.astro`, `[slug].astro`, `benchmarks.astro` → one shared
  layout/global stylesheet.
- *Acceptance criteria:* zero unintended visual diff across all existing
  pages, verified by `npm run build` + `npm run check` plus a manual/visual
  diff of `dist/`; purely a refactor, independently shippable.

**2J. Small accessibility affordances.**
- *Affected:* `site/src/components/Header.astro`.
- *Acceptance criteria:* keyboard-only navigation reaches `<main>` in one
  tab stop (skip link); the active nav link carries `aria-current="page"`.

---

## 7. Missing evidence and unresolved editorial choices

**Missing evidence (explicitly not fabricated or inferred by this audit):**
- No fresh routing run was performed here — correctly out of scope; owned
  by #5279/#5280 and Phase 2-B.
- No live-browser or real-device mobile/accessibility testing was performed
  (no browser available in this environment); §5's mobile/accessibility
  findings are source-level only and should be re-verified visually before
  Phase 2 closes.
- No Lighthouse/axe run and no computed WCAG contrast ratios; visual
  inspection only.
- The actual cadence of `scripts/deploy-site.sh` runs (how often the site is
  redeployed in practice) is not recorded anywhere this audit could find —
  Recommendation 2C proposes fixing the display side of this; the process
  side (Phase 3's "document how to refresh") is Phase 3's job.
- Whether `kct board-metrics`'s Python side already has a code path for
  "readiness exists, board.json does not" was not verified — flagged
  explicitly in Recommendation 2E rather than assumed.

**Unresolved editorial choices, with a recommended default:**

1. *Should in-progress boards with only `readiness.json` (no manufacturing
   package) appear in the gallery at all?* **Recommended default: yes**, via
   Recommendation 2E's distinct "in development" status — hiding real,
   verifiable progress conflicts with the epic's own stated goal of making
   completed work discoverable, provided the status is never conflated with
   "Ready."
2. *Should `boards/external/softstart` remain symlinked into the repo at
   all, given it can never build reproducibly?* **Recommended default:**
   keep the symlink (it may be useful for the maintainer's own local
   testing) but exclude it from the *public* gallery immediately
   (Recommendation 2A) — independent of any broader decision about whether
   `softstart` is ever published.
3. *Where should the new "Documentation" nav link (2G) point —
   `README.md`, or a new curated `docs/` landing page?* **Recommended
   default: `README.md`** for Phase 2 (zero new content required, already
   accurate); a curated landing page is a reasonable future Phase 3+ idea
   but would itself need scoping and is not assumed here.
4. *How much issue-number citation is appropriate in visitor-facing copy?*
   **Recommended default:** keep citations as secondary "see more"
   references (valuable for credibility and matches the project's existing
   honesty-first tone), never as the sentence's only content (Finding
   F12 / Recommendation 2H).

---

## Appendix — reproduction

```bash
# Source-only checks (this audit's primary evidence):
rg -n "v0.19.0" site/src/pages/index.astro site/src/components/Footer.astro
rg -n "EXCLUDED_SLUGS" site/src/data/galleryConfig.mjs
cat boards/README.md   # "External Boards" section
cat boards/09-usbc-pd-power/output/readiness.json
cat benchmarks/external/results/README.md

# Generated-vs-committed distinction behind §1.2 (9 tracked readiness
# reports: boards 00-07 and 09; zero tracked board.json):
git ls-files 'boards/*/output/readiness.json'
git ls-files 'boards/*/output/board.json'   # empty

# Board09's readiness evidence is current, not just present -- all 20
# declared input hashes match the committed files at this revision:
python3 - <<'PY'
import hashlib, json, os
os.chdir("boards/09-usbc-pd-power")
inputs = json.load(open("output/readiness.json"))["inputs"]
bad = [p for p, h in inputs.items()
       if hashlib.sha256(open(p, "rb").read()).hexdigest() != h]
print(f"{len(inputs) - len(bad)}/{len(inputs)} input hashes match", bad)
PY

# Existing contextual documentation links (§1, F11) -- the benchmarks page
# does link into docs/, so the gap is the shared nav, not every page:
rg -n "docs/benchmark-external-report-schema.md|benchmarks/external/README.md" \
  site/src/pages/benchmarks.astro
rg -n "href" site/src/components/Header.astro   # no docs entry point

# Fresh-checkout build/test used to verify F4 and F5 independently:
cd site
npm ci
npm test            # 6 files / 66 tests passed at time of audit
npm run build        # 12 static routes; no board.json present -> no /softstart,
                      # 09-usbc-pd-power renders as "not been built yet"
npm run check         # 0 errors, 0 warnings, 98 pre-existing is:inline hints

# Live-site spot check used to verify F3/F4/F5 against what visitors see today:
curl -s https://kicad-tools.org/ | less
curl -sL https://kicad-tools.org/benchmarks | less
```
