# Fresh-design sweep findings (2026-07-05)

Session scratchpad. One section per board as reports land. Issue candidates get dedup'd at the end.

## Board 02 — charlieplex-led ✅ (report complete)
Worktree: `.claude/worktrees/agent-aca4ed8736b45bdad`

**Result: HEALTHY.** 8/8 signal nets (10/10 incl. pour-carried), 24 vias, 328.2mm, 0 kicad-cli DRC violations, kct check PASSED (7 copper_sliver warnings only). #659 caveat did NOT reproduce — negotiated routing converged in 4 iterations, no manual intervention. Build total 83.5s (A* core only 7.5s).

**Promotion candidate: YES** (pending decision on committed-artifact churn noise, see F4).

Friction:
1. **[HIGH] `kct build` fails a healthy board — manifest-freshness ordering.** route step (embedded export #3264) → page-fit rewrites PCB → verify's `kct check` manifest meta-check sees PCB newer than manifest → exit 2 → build_cmd.py:2297 maps ANY nonzero to "DRC found issues". Build's own later export step heals it (post-hoc kct check passes). 3 sub-bugs: (a) step ordering vs manifest check, (b) exit-code misattribution at build_cmd.py:2297, (c) contradictory output ("SUCCESS: All nets routed" 20 lines above "Build failed").
2. **[MED] Checker disagreement:** kct check 7 copper_sliver warnings vs kicad-cli 0 violations (same file, refilled zones).
3. **[MED] MFG bundle exported twice per build** (~16s each, ~40% of build time): route_demo.py embeds `kct export` (#3264 banner) AND build pipeline has its own export step.
4. **[MED] Rebuild dirties tracked output with noise:** tabs-vs-spaces re-serialization + fresh UUIDs of all tracked s-expr files (955-line diff, content identical); untracked un-gitignored side files (`*.kct.json`, `.kicad_prl` x2, `drc_report.json`, `erc_report.json`). `git diff` reproducibility review impossible.
5. **[LOW] Entry-point multiplicity** (5 scripts; README Files table incomplete; generate_design.py duplicates kct build). Board README stale: says "routes ~5-6 of 8 nets", actually 8/8.
6. **[LOW] Misleading preflight BOM warning:** "14/14 missing LCSC part number" fires before spec-overlay enrichment; final BOM fully populated.
7. **[LOW] Committed artifact staleness:** 14 lib_footprint_mismatch kicad-cli warnings on COMMITTED routed board (fresh has 0); 197 vs 397 segments (seeded determinism is per-router-version — worth documenting).
8. Cosmetic: schematic generator prints steps 1,2,4,5,6 (skips 3).

Positives: native build 6.4s w/ clear messaging; grid auto-selection log excellent; export preflight table/render/board-metrics one-shot. #3900/#3901/#3902 did not manifest.

## Board 01 — voltage-divider ✅ (report complete)
Worktree: `.claude/worktrees/agent-aff4c667233e79055`

**Result: board HEALTHY, build UX broken.** 3/3 nets, kct check Overall PASSED, kicad-cli 2 silk_over_copper warnings only — **checkers agree exactly**. But `kct build` run 1 → "Build failed (13/14)" (verify before export: manifest NOT RUN → exit 2 → "DRC found issues" at build_cmd.py:2297); run 2 fails at step 1 with EMPTY error (generate_design.py's internal kct check hits Manifest STALE).

**Promotion candidate: YES with caveats** — final on-disk state is coherent (3/3, PASSED, manifest fresh), but see F3 nondeterminism.

Friction (dedup keys vs board 02 noted):
1. **[HIGH] Fresh build can never go green — verify-before-export manifest chicken-and-egg** (SAME ROOT as B02-F1: manifest meta-check + build_cmd.py:2297 misattribution; B01 shows the no-manifest variant, B02 the stale-manifest variant).
2. **[HIGH] Second build run fails at schematic step with empty error message** (generate_design.py internal check, manifest STALE variant). Run 1 and run 2 fail at DIFFERENT steps with different misleading errors.
3. **[HIGH] Stage-order nondeterminism: script zones-based design vs build route-step signal-routed design.** zones step says "skip zone creation, route as signals (#2740)" while route step says "Auto-pour: honoring caller-forced pour GND, --skip-nets GND". Final artifact = whichever stage wrote last: committed 15seg/0via/2zones READY vs script 21/2/3 vs build-route 53/3/0-zones WARNING.
4. **[MED] `--step verify` checks the UNROUTED pcb when routed exists** (ctx.routed_pcb_file only set by route step, never discovered from disk).
5. **[MED] generate_design.py duplicates full kct build pipeline** (~2x work; 76s wall for 4-component board, router 1.1s; script SUMMARY "PASS" prints inside "Build failed") (rhymes with B02-F3 double-export).
6. **[LOW/MED] Export preflight "N/N missing LCSC" false alarm before spec-overlay enrichment** (SAME as B02-F6).
7. **[LOW] Generator places refdes silk over pad copper** (J1/J2) → verdict WARNING on simplest board.
8. **[LOW] Router diagnostics noise:** persistent "overflow: 2" + "Oscillation detected / all 4 escape strategies exhausted" on a trivial 3-net board that succeeds in 1.1s; route log says 21seg/2via while file has 53/3.
9. **[LOW] Untracked un-gitignored byproducts** (SAME as B02-F4b).
10. **[LOW] nits:** render --help says .png writes .svg; no per-step timings in build; ERC warns missing 'kicad_tools_pwr' lib on own schematic; inactive diffpair rule warnings on class-less board.

Positives: setup fast; checkers agree exactly; export bundle complete; .kicad_pro/BOM/CPL byte-identical regen.

## Board 00 — simple-led ✅ (report complete)
Worktree: `.claude/worktrees/agent-a59238ebf05d25b0d`

**Result: board HEALTHY (3/3 nets, 0 errors both checkers), build ALWAYS exits FAILED.** kct check PASSED (3 warnings: 2 copper_sliver + 1 silk_over_copper); kicad-cli 0 errors/1 silk warning.

**Promotion candidate: YES** (final on-disk state passes both checkers after manual export).

Friction (with precise root causes — use these for the issue):
1. **[CRITICAL] same manifest/verify bug** — verify (build_cmd.py:2277) invokes `kct check` WITHOUT `--allow-incomplete`; outer route/page-fit rewrite PCB after inner export → manifest STALE always. drc_report.json meta shows manifest FAILED + drc PASSED simultaneously.
2. **[CRITICAL] rerun/fresh-clone path fails at step 1, empty error** — script's `--allow-incomplete` forgives MISSING manifest but not STALE; failure-message builder (build_cmd.py:312/347) surfaces only stderr (blank). Non-idempotent recipe; this is the REAL new-user path (clone with committed outputs).
3. **[HIGH] `--step verify` verifies UNROUTED pcb + clobbers drc_report.json with wrong-artifact failing report** (same ctx.routed_pcb_file bug as B01-F4, worse consequence).
4. **[HIGH] every nonzero kct check exit mapped to "DRC found issues"** (build_cmd.py:2296-2297) — same as B01/B02.
5. **[MED] drc_report.json left "overall": FAILED on disk after passing build** — written pre-export, never refreshed; fleet/CI reading JSON gets stale verdict.
6. **[MED] copper_sliver: kct-only finding, kicad-cli silent** (same class as B02-F2); committed artifacts predate the rule (29 vs 32 rules). Triage: real thin pour necks vs Python-filler approximation artifact?
7. **[MED] outer route DISCARDS script's #3737 deterministic 2-seg route, replaces with own 5-seg** — the workaround only protects the script path; shipping artifact ≠ kct build product. Double-pipeline (~2x work, export 2×10s of 44s build) same family as B01-F5/B02-F3.
8. **[LOW] "Routing: PARTIAL — 1/2 nets" on fully-connected board** (pour-skipped VCC counted unrouted; adjacent to #3901 theme, distinct).
9. **[LOW] render --help .png vs .svg** (same as B01-F10a).
10. **[LOW] LCSC preflight false alarm** (same as B01-F6/B02-F6).

## Board 03 — usb-joystick ✅ (report complete)
Worktree: `.claude/worktrees/agent-ab840f220d84abdc9`

**Result: routing EXCELLENT (13/13 on 2 layers in 24s, copper bit-reproducible vs committed, D+/D- skew 0.000mm), pipeline BROKEN in new ways.** "May not complete on 2 layers" status is obsolete. kct build exits 1 at route step (8/9).

**Promotion candidate: PARTIAL** — copper is identical to committed; but kct build path lacks stitching vias + .kicad_pro/.dru, and needs the profile split-brain resolved. Committed artifact remains superior (19 vias incl. stitching).

Friction:
1. **[CRITICAL] Board 03 silently un-migrated from diff-pair routing.** #3308/#3410 recipe consolidation replaced route_all_with_diffpairs(enabled=True) with plain `kct route` (no --differential-pairs) at generate_design.py:561; also pinned in tests/router/test_board03_routing_baseline.py. Phase A/B bypassed; boards/README.md "footgun not active" claim now FALSE. Skew 0.000mm is post-hoc luck. (#3089 latency was why auto-detect was deferred.)
2. **[CRITICAL] kct build circular gate:** route_demo.py post-gate runs full kct check → LVS fails on 2 GND opens that only later stitch would fix → route step exit 1 → build aborts 8/9; stitch/verify/export never run. (Different mechanism from B00/B01/B02 manifest bug — same "pipeline can't go green" family.)
3. **[HIGH] GND stitching vias only in generate_design.py:main()** — neither route_demo.py nor build's generic stitch step ("no cross-layer plane pads — skipped") adds the 4 vias (#3789/#3848); build output permanently carries GND opens; fresh 15 vs committed 19 vias.
4. **[HIGH] Build never writes .kicad_pro/.kicad_dru next to routed PCB** → kicad-cli judges with defaults: 87 false track_width errors (0.15mm JOY_BTN vs default 0.2). With committed constraint files restored: 0 errors. Route step itself printed "PASS withheld — native KiCad DRC is authoritative" over false positives.
5. **[MED] Manufacturer-profile split-brain:** recipe routes for jlcpcb-tier1 (via-in-pad OK), build/export/board.json judge at jlcpcb → 4 via_in_pad errors, report says CRITICAL, board.json status "partial" despite 100% routed tier1-clean. Same PCB scores 0/4/5/88 violations depending on surface.
6. **[MED] net-status says GND "Complete (33 pads, 100%)" while LVS shows 2 opens, kicad-cli 14 unconnected** — assumes pour connectivity without checking filled copper; POUR_DISCONTINUOUS (#3905) can never engage. Adjacent to #3901, reproduced at net-status level.
7. **[MED] Contradictory banner:** "All nets routed, but 0 DRC violation(s) detected! / Review DRC errors" then exit 1 (fragile stdout scrape in route_demo.py:run_drc).
8. **[LOW/MED] diff-pair check needs manual --net-class-map (sidecar not auto-loaded) and reports NO measured skew/gap values even --verbose.**
9. **[LOW] project.kct stale (60x40 vs real 80x60)**; sync drift "[OK] sync: drift detected".
10. **[LOW] No live progress/per-step timings in kct build** (route subprocess capture_output silent 24s).
11. **[LOW] Export: JLCPCB API 403 → 4 unmatched BOM parts; "[WARN] drc: No DRC report found"** (drc_report.json only written by generate_design-only step).

Positives: copper determinism excellent (per-net lengths identical to 3 decimals); checkers agree perfectly once constraint files present; net-status --why honest about advisory pour residuals.

## Board 04 — stm32-devboard ✅ (report complete)
Worktree: `.claude/worktrees/agent-a5325130dcdf60b87`

**Result: STATUS PROMOTION — "schematic only" is STALE.** Via generate_design.py recipe: 12/12 nets, kicad-cli 0 violations/0 unconnected, copper-LVS 0/0, full mfg package. Fresh copper equivalent to committed (147 seg/29 vias). #3773 zone-regen-shorts warning did NOT reproduce (README + project.kct progress stale).
**But `kct build` can NEVER succeed on this board** — it re-runs its own route over the recipe's passing artifact with the WRONG profile (jlcpcb vs jlcpcb-tier1: different via sizes, no fix_osc_escape/tie_power_pads/quantize) → clobbers it, fails preflight (GND 1/18). 266s vs 83s.

**Promotion candidate: YES — update boards/README row 04, board README #3773 warning, project.kct progress.** Regen via generate_design.py, NOT kct build.

Friction:
1. **[HIGH] First run fails / second passes (state-dependent):** run writes routed .kicad_pro/.dru/.prl sidecars; next run's in-route kicad-cli DRC sees them → different verdict. Fresh dir: "Repaired 0/4 (rolled back — connectivity regression)" → exit 3 → PARTIAL, despite route itself printing 9/9 SUCCESS. (Same constraint-sidecar family as B03-F4.)
2. **[HIGH] kct build clobbers recipe artifact w/ wrong-profile re-route then fails** (profile split-brain, same family as B03-F5; plus double-pipeline family B00/B01/B02).
3. **[HIGH] Checker disagreement:** kct tier1 FAILED (3 errors: 1 GND pour advisory #3901/#3905 family + 2 drill-clearance real-but-JLC-specific) vs kicad-cli 0/0.
4. **[MED] Four verdicts one artifact:** build gate PASS (allowlist) / export preflight FAIL / kct check FAILED / board.json "partial" w/ 100% routed. Allowlist exists only inside build gate.
5. **[MED] --step verify DRCs unrouted pcb** (same as B00-F3/B01-F4).
6. **[MED] --dry-run "fails"** because preview has no outputs yet ("Build failed (7/9)").
7. **[MED] BOM enrichment nondeterministic:** JLCPCB API 403; from-spec counts drift 8/2→9/1→10/0→11/0 across identical runs; C12-C15 → C1525 vs committed C49678.
8. **[MED] Footprint order nondeterministic between seeded runs** (copper identical; diffs useless).
9. **[LOW] Permanent sync drift J1 sch=SWD-6 pcb=SWD; project.kct 50x25 vs real 60x40** (same stale-spec family as B03-F9).
10. **[LOW] placement check (1 warning) vs optimize-placement (INFEASIBLE ovl=58.26) vs optimizer initial eval (ovl=1.87) — three scores, same placement; CMA-ES +44% wirelength vs hand.**
11. **[LOW] 47+ pad_grid warnings on fixed-pitch LQFP-48.**
12. **[LOW] Board README's `kct fleet status --boards-dir boards/04-stm32-devboard` → "No boards found".**

## Board 06 — diffpair-test ✅ (report complete)
Worktree: `.claude/worktrees/agent-a3fafe42edcf94681`

**Result: all 21 nets connected, LVS clean, mfg bundle fine — but 0/9 PAIRS ROUTE COUPLED.** Full pipeline 580s (Phase A now budget-bounded ~25s at 0/9 convergence — fresh #3089 datapoint). kicad-cli: 1 real error (0.0999 vs 0.1016 clearance, self-inflicted by pour-repair/quantize) + 21 warns; kct check 6 err (3 = pour connectivity FALSE POSITIVES) or 23 with sidecar (17 diff-pair violations, allowlisted). Fresh DRC count 23 matches CI deterministic baseline.

**Promotion candidate: NO for gallery "clean" purposes** (status=partial by design; testbench). Fresh artifact FAILS test_fleet_45_census (pinned exemption uuid gone).

Friction:
1. **[HIGH] kct check connectivity false-positives on pour-carried nets** ("GND 15 of 122 pads stranded" vs kicad-cli 0 unconnected + copper-union audit 1 component). POUR_DISCONTINUOUS (#3905) classifies correctly but DRC rule still errors → CI advisory-filter hack. #3482-family. SAME family as B03-F6, B04-F3.
2. **[HIGH] kct check misses sub-16µm marginal clearance kicad-cli errors on** (0.0999 &lt; 0.1016; #3855 dogleg-bulge mode; no post-quantize clearance re-validation). Softstart process-rule reproduced IN OUR OWN PIPELINE.
3. **[HIGH] 0/9 coupled routing on the flagship diff-pair bench** — all pairs "iteration budget exceeded after 1000 iterations" in 0.3-4.5s; independent fallback violates every Phase 1-3 constraint; 17/23 errors allowlisted. Bench's purpose exercised only via rules firing, never success. (#3089/#3508-adjacent but distinct: convergence, not latency.)
4. **[MED] Budget-exit diagnostics lie:** prints "budget exceeded (120s)" when ITERATION budget fired in 0.3-4.5s (diffpair_routing.py:4521-4534 always formats per_pair_timeout); says "1000 iterations" while config is 2000 (split per phase silently).
5. **[MED] The #3880 "must-not-fire" 360s backstop FIRED** (C++ open-set exhausted 2 nets, post-route clearance validation exhausted 5 resumes on 2, Python fallback burned 220s in escapes; saved only by banked iter-2 snapshot).
6. **[MED] Fresh regen breaks test_fleet_45_census** — DOCUMENTED_OFF_ANGLE pins seed-specific uuid; any artifact refresh needs hand-curated pin + QUANTIZE_SKIP_UUIDS.
7. **[MED] boards/README in-process justification VERIFIED FALSE:** NetClassRouting round-trips intra_pair_clearance etc. losslessly; kct route --net-class-map (#2996) consumes it. Actually inexpressible: per_pair_max_iterations, enable_shadow_construction, width re-solve. Doc steers boards away from production CLI for obsolete reason.
8. **[MED] `kct build boards/06` cannot reproduce the board** — _run_step_route only looks for route_demo.py/route.py; falls back to generic kct route (no diffpair config/pour pipeline) or "Using existing routed PCB". Canonical entry = generate_design.py only.
9. **[LOW] Board README CI-gate allowlist stale (28 documented vs 24 actual, wrong composition).**
10. **[LOW] project.kct 80x60 vs actual 100x80** (stale-spec family: B03-F9, B04-F9).
11. **[LOW] PCB-first fixture: ERC 181 / label-LVS 198 by-design noise conflated into recipe's "DRC: FAIL" summary (run_drc not --drc-only).**
12. **[LOW] Silent 41s (Phase B wrap-up) and 37s (optimizer) phases, zero log lines.**
13. **[LOW] Post-passes self-inflict 3 of 6 base errors** (stitch/repair via-in-pad U1-12; same-net hole-to-hole 0.309/0.457mm).

Positives: pour-repair loop converged honestly; DRC count reproduced CI's 23 under CPU contention; report.md's kicad-cli-integrated gate is the most accurate of the three; PCIE/MIPI/USB2 pair copper byte-identical across runs.

## Board 07 — matchgroup-test ✅ (report complete)
Worktree: `.claude/worktrees/agent-a1e048d88c81b8f0c` (committed artifacts backed up to /tmp/board07-committed)

**Result: fresh 27/31 routed (BETTER than committed 24/31; docs claim 28/31 — all three disagree).** Pipeline 1052s exit 1; negotiated loop ran 920s against --timeout 600. kct check 28 err (w/ sidecar) / kicad-cli 55 violations incl. **4 shorting_items ERRORS the pipeline shipped as "LVS PASS"**.

**Promotion candidate: NO** (testbench, partial by design, and fresh artifact has real shorts).

Friction:
1. **[CRITICAL-adjacent HIGH] Stitcher knowingly ships power-power short; copper-LVS misses it.** Stitch log: "1 pour pad(s) used the connectivity fallback (placed despite a marginal cross-net clearance graze) — +1V8: U4.C3" → kicad-cli "Items shorting two nets (+1V2 and +1V8)" ×3 + DQ1/DQ6 short; copper-LVS "PASS: 0 shorts/0 opens", board.json lvs_clean:true. SECOND instance of cross-gate catching what kct misses (B06-F2 family; softstart 2119450 precedent).
2. **[HIGH] match_group_length_skew via-blind:** checker.py:580 derive_group_skew_data(board_thickness_mm=None) → vias count 0mm. ADDR_BUS "PASS 0.0019mm" vs 1.600mm via-inclusive (3.2× over); via-count mismatch invisible.
3. **[HIGH] Pairs-only match groups structurally never checked** (match_group_skew.py skips empty net_ids — "no single-ended members in Phase 1B scope"): MIPI 0.05mm + HDMI 0.075mm tolerances are dead letters. Only 1 of 4 bench scenarios ever gated.
4. **[HIGH] Determinism self-voids under load:** negotiated 920.5s vs --timeout 600 → best-partial; fresh ≠ committed ≠ diagnostic-runs (3-way md5). (#3438 reach chaos known; budget-overrun interaction new.)
5. **[MED] kct check silently drops 10 errors without --net-class-map sidecar (28→18), no warning** (route warns loudly; check doesn't). Same trap as B03-F8/B06.
6. **[MED] No per-net length / target-vs-achieved reporting anywhere** (router/tuner/checker); agent had to script against internals. Tuner prints only reason buckets.
7. **[MED] Committed artifact contradicts docs:** 24/31 & 35 errors vs "28/31, 23 DRC" notes and tolerance floor 28.
8. **[MED] kct build entry would route WITHOUT seed/sidecar/length-match flags** (generic pipeline; rule no-op per F5). Canonical entry only in diagnostic-runs/README. Same family as B06-F8.
9. **[LOW] "Progress: 31/31 nets routed total" printed with 8 unrouted; in-route DRC 143 vs final 28; three violation totals across surfaces.**
10. **[LOW] C++ backend abandoned 16×/run** ("post-route clearance validation failed; exhausted 5 resume attempts → pure-Python A*") — fallbacks consumed the wall budget (Phase-2 channel routing 933s). SAME pattern as B05 probe's per-net stalls and B06-F5. → Candidate systemic issue: C++ resume-attempt exhaustion cascades to Python fallback = dominant latency source.
11. **[LOW] Tuner proposes self-overlapping meanders (rollback at -0.375mm < 0.100mm)** — #3440-adjacent (clamp landed, geometry still rolls back).
12. **[LOW] Cosmetics:** grid memory-cap UserWarning ×2 pre-[seed]; "99% pads off-grid" (#3441-adjacent); meta "LVS FAILED (244 label mismatches)" on PCB-first fixture vs copper-LVS PASS.

Positives: ADDR_BUS tuner equalized 8 lengths to 1.9µm (genuinely impressive); pour-repair converged r3; export/render/metrics fine.

## Board 05 — bldc scratch probe ✅ (report complete)
Worktree: `.claude/worktrees/agent-a1b3da7f203071097`

**Committed baseline:** 46/52 complete, 0 shorts, fleet census 9 passed. kct tier1: 7E; kicad-cli: 112 violations (55 lib_footprint_mismatch etc.) + 69 unconnected (pour-strandable). 4 ISENSE Kelvins incomplete (PLACEMENT_BOUND/CONGESTION_SATURATED — #3766 confirmed as placement). POUR_DISCONTINUOUS (#3905) working.
**Fresh regen (1842s, exit 1):** 40/52 + **2 REAL SHORTS** (NRST↔OSC_IN, PWM_AH↔OSC_OUT vias) + 1 near-short. PHASE_A/B/C skipped by design (design.py:2947 #3766 note). Loses ISENSE_A+ and PWM_BH vs committed; strands +3V3. Export bundle still stamped "PASS".
**Divergence: pipeline is 6 nets + 2 shorts behind hand-routed artifact.** Roadmap: (a) short-free fine-pitch routing, (b) stitch step in design.py flow, (c) EE relayout for ISENSE (#3766).

New friction (beyond clusters already drafted):
- **[HIGH→Cluster A] Auto-grid knowingly picks short-producing grid** ("memory budget cap forces grid 0.1mm > clearance/2 (0.075mm)... may produce clearance violations") → exactly that happened; no post-route short ripup.
- **[HIGH→Cluster A] `kct export --skip-preflight` in design.py → "Manufacturing bundle: PASS" on a board with 2 net shorts.**
- **[MED] kct check reports shorts as anonymous negative clearances** ("Segment to via clearance -0.188mm" — no net names; kicad-cli names both).
- **[MED-HIGH→D3 confirmed] 15 C++→Python fallbacks burned ~15 of 30.7 min, all on nets that failed anyway** (~60s each × 8 nets × 2 attempts). Fail-fast/unreachability caching needed.
- **[MED] Escalation ladder [4L,4L] repeats identical ~7.5min route for +0 nets** (attempt 2 even traded PWM_CH for HALL_A); prints "escalating" after final rung.
- **[MED] design.py (documented entry) has NO stitch step** → 58 stranded pour pads, LVS drowning in 105 opens; net-status even prints the exact `kct stitch` fix.
- **[LOW-MED] DRC step advertises sidecar that doesn't exist** (route step never wrote net_class_map.json but check suggests passing it).
- **[LOW] 11 "unintended connection" UserWarnings during schematic gen while ERC passes; "DRC nudge 0/6"; entry duality design.py vs kct build (different step sets, route overwrites in place).**
- #3900: "0 pins escaped" form not reproduced, but failure attribution still uniformly `blocked_path (blocked_path)` — no cause differentiation (comment on #3900, don't file new).

## Pending: board 05 final report only

---

# DRAFT SYNTHESIS — deduplicated issue set (pending board 05 confirmation)

## Cluster A — CORRECTNESS (file first, highest priority)
- **A1 [CRITICAL] Copper-LVS misses real shorts the stitcher knowingly creates.** B07: stitch "connectivity fallback (marginal graze)" pad → 3× +1V2/+1V8 shorting_items + DQ1/DQ6 short per kicad-cli; copper-LVS "PASS 0/0", lvs_clean:true. Stitcher should reject the fallback OR LVS must catch it. (Softstart 2119450 precedent — this is the same class INSIDE our pipeline.)
- **A2 [HIGH] kct check misses sub-16µm marginal clearances kicad-cli errors on.** B06: 0.0999 vs 0.1016 after pour-repair/quantize; no post-quantize clearance re-validation (#3855 mode). Fix: post-quantize/post-repair re-validation + close the kct-vs-kicad-cli gap.
- **A3 [HIGH] Pour-connectivity DRC false positives** ("GND 15/122 stranded" while copper is one component). B03/B04/B06. net-status got #3905; the connectivity DRC rule + net-status "Complete" (B03 variant: says complete while real opens exist — BOTH directions wrong) need the same copper-aware model. Supersedes/extends #3901/#3482.
- **A4 [HIGH] match_group_length_skew via-blind** (checker.py:580, board_thickness None → vias 0mm; ADDR_BUS 3.2× over yet PASS).
- **A5 [HIGH] Pairs-only match groups never checked** (match_group_skew.py Phase 1B scope skip; MIPI/HDMI tolerances dead letters).

## Cluster B — kct build orchestration (one epic + sub-issues)
- **B1 [CRITICAL] `kct build` cannot go green on ANY board (0/8).** Variants: manifest verify-before-export chicken-and-egg (B00/B01/B02, build_cmd.py:2277 no --allow-incomplete); route-step circular LVS gate pre-stitch (B03); clobbers recipe artifact w/ wrong profile then fails (B04); can't find recipe at all → generic route (B06/B07: _run_step_route only knows route_demo.py/route.py). Second-run non-idempotence (stale manifest; B00/B01) + empty error messages (build_cmd.py:312/347 stderr-only).
- **B2 [HIGH] Every nonzero kct check exit reported as "DRC found issues"** (build_cmd.py:2296-2297) + contradictory SUCCESS/FAIL banners.
- **B3 [HIGH] --step verify checks unrouted PCB & clobbers drc_report.json** (ctx.routed_pcb_file never rediscovered from disk). B00/B01/B04.
- **B4 [MED] Double pipeline: generate_design.py duplicates build steps (2× export ~16-30s each, 2× route, outer route discards script routes incl. #3737 workaround).**
- **B5 [MED] drc_report.json left "overall: FAILED" after passing build; --dry-run "fails" on missing outputs.**

## Cluster C — profile & verdict split-brain
- **C1 [HIGH] Manufacturer-profile split-brain:** recipes route jlcpcb-tier1, build/export/board.json judge jlcpcb → same PCB scores 0/4/5/88 (B03) or PASS/FAIL/FAILED/partial (B04). project.kct target_fab vs recipe --manufacturer must unify.
- **C2 [HIGH] Build never emits .kicad_pro/.kicad_dru next to routed PCB** → kicad-cli judges with defaults (87 false track_width, B03) + state-dependent first-vs-second-run verdicts (B04). One fix: constraint-sidecar emission step.
- **C3 [MED] kct check silently drops rules without --net-class-map sidecar** (B07 28→18; B03/B06) — auto-load conventional sidecar path or warn.

## Cluster D — router core
- **D1 [HIGH] CoupledPathfinder 0/9 convergence on flagship bench** (iteration budget 1000 exceeded in 0.3-4.5s per pair; fallback violates all Phase 1-3 constraints). Distinct from #3089 latency.
- **D2 [HIGH] Board 03 silently un-migrated from --differential-pairs** (#3308/#3410 consolidation; boards/README claim now false; also pinned in test_board03_routing_baseline).
- **D3 [MED] C++ resume-exhaustion cascade:** "post-route clearance validation failed; exhausted 5 resume attempts → pure-Python A*" 16×/run B07 (933s), B05 probe per-net 60-200s stalls, B06 backstop fired. Dominant latency source; also voids --deterministic-budget under load (B07 920s vs 600 timeout; B06 #3880 backstop).
- **D4 [MED] Misleading diagnostics:** budget-exit prints wrong budget type/values (diffpair_routing.py:4521-34); "Progress 31/31" w/ 8 unrouted; "PARTIAL 1/2" on complete pour board (B00); oscillation noise on trivial boards (B01).

## Cluster E — regen/artifact hygiene
- **E1 [MED] Artifact-refresh brittleness:** test_fleet_45_census pins seed-specific uuids (B06 fails on ANY fresh regen); tabs-vs-spaces + uuid churn make diffs useless (B02 955-line no-op diff; B04 footprint-order nondeterminism); un-gitignored byproducts (B01/B02: *.kct.json, .prl, drc/erc/lvs reports).
- **E2 [LOW-MED] LCSC/BOM: preflight "N/N missing" false alarm pre-enrichment (B00/01/02); JLCPCB API 403 + drifting from-spec counts + C1525-vs-C49678 divergence (B04).**

## Cluster F — docs/specs stale (one cleanup PR, not issues)
- boards/README: B02 "5-6 of 8 nets" (is 8/8); B03 "may not complete" (13/13 in 24s); B04 "schematic only" (fully routes, kicad-cli 0/0); diff-pair "footgun not active" (false, D2); JSON-roundtrip justification (verified false, B06-F7); B06 allowlist 28 vs 24.
- project.kct stale: B03 60x40 (real 80x60), B04 50x25 (real 60x40) + progress:schematic, B06 80x60 (real 100x80); B04 README #3773 warning obsolete (did not reproduce, copper-LVS 0/0).
- B04 README `kct fleet status --boards-dir` command doesn't work ("No boards found").
- render --help .png vs .svg (B00/B01).

## Promotion decisions (for site refresh)
- 00, 01, 02: promote fresh artifacts (healthy both checkers). 02 is now fully green — update status table.
- 03: keep committed (has stitching vias); fresh copper identical anyway. Update status text.
- 04: PROMOTE past "schematic only" — regen via generate_design.py in main tree (needs 2 runs due to B04-F1 state bug, or copy from worktree).
- 05: committed artifact stays (artifact-first). No site change beyond current.
- 06, 07: testbenches, status=partial by design; do NOT ship fresh 07 artifact (contains real shorts). Site shows them as testbenches.
