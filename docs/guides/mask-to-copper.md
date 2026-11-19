# Checking mask-to-copper clearance and exposure

The `mask_to_copper` check (opt-in, except under `--mfr jlcpcb`; see below) compares native solder-mask openings with
individually identified copper objects. It supplements the existing solder-mask
expansion, pad-size and annular-ring checks. See [mask geometry](mask-geometry.md)
for the underlying geometry inspection APIs.

To repair a track that enters its own pad off-centre (the `same-net own-pad
entry` relation) instead of only flagging it, run the `fix-drc` repair pass:
`kct fix-drc board.kicad_pcb --only mask-copper` — see the
[CLI reference](../reference/cli.md#fix-drc).

Supply a process-specific clearance requirement with its source, process and
revision. A manufacturer profile, mask expansion or minimum mask-web width does
not supply this requirement. The example below uses a **fictional test policy**;
replace all four policy values with the requirement for your actual process.

## Default-on under `--mfr` (JLCPCB)

`kct check <board> --mfr jlcpcb` runs `mask_to_copper` with no
`--mask-copper-config`, using an **interim** policy (JLCDFM observation, epic
#8838; to be replaced by the #8839 threshold table): openings closer than
0.09 mm to other copper are errors, those at or below 0.15 mm are warnings that
do not fail the verdict. Mask expansion is the board's native effective
expansion. An explicit `--mask-copper-config` overrides the default policy,
`--skip mask_to_copper` disables it, and runs without `--mfr` (or with a
manufacturer that has no cited policy) stay opt-in. Without native KiCad 10.0.5
the result is `incomplete` with the reason shown, never a silent pass.
`MaskCopperPolicy.warning_clearance_mm` enables the warning tier in custom
requests too.

## Run through the CLI

Save this request as `mask-request.json`:

```json
{
  "schema": "kct.mask-copper-request.v1",
  "policy": {
    "clearance_mm": 0.1,
    "source": "Fictional test specification, section 3",
    "process": "Example process only",
    "revision": "example-1"
  },
  "intents": [],
  "native": {
    "native_command": ["kicad-cli"],
    "native_python_command": ["/path/to/kicad/python3"],
    "artifact_dir": "mask-review"
  }
}
```

Choose the Python interpreter that can import KiCad's `pcbnew` module. The CLI
and Python must both use KiCad 10.0.5. The object-attribution profile reproduces
that version's plotting branches; other versions require separate verification
and currently produce incomplete coverage. `native_command` and
`native_python_command` are argument lists, so container launchers can be used
without shell interpolation. Both processes must see the temporary source files
at the same absolute paths; set `native.scratch_dir` to a shared mounted directory
when needed.

```bash
kct check board.kicad_pcb --only mask_to_copper \
  --mask-copper-config mask-request.json --drc-only \
  --format json --output mask-report.json
```

The config engages the check; an incompatible `--only` or `--skip` selection is
an error. Selecting `--only mask_to_copper` without a policy produces a `not_run`
assessment. An ordinary check without either selection or config does not run
native mask analysis.

Both stdout JSON and the saved report retain `mask_copper_assessments`, including
policy, source binding, coverage reasons, measurements, intent audit and native
geometry provenance. An engaged assessment with violations, numerical uncertainty
or incomplete coverage prevents a pass and returns exit code 2. The
`--allow-incomplete` flag does not waive this assessment. Invalid requests are
command errors.

## Reading the findings

Each `mask_to_copper` finding names both sides of the exposure:

```text
Mask-to-copper overlap: violation; exposed area 0.12 mm²; assessment measurement 7;
opening pad:R1/1; exposed copper segment (net GND); same-net own-pad entry
```

`opening` is `pad:<reference>/<pad number>` when the owning pad is identified from
the source, otherwise the opening's kind with a UUID prefix (as in the finding's
`items`). `exposed copper` is the kind and net of the copper found inside or near
the opening. The trailing relation distinguishes the classes a fab report uses:

- `same-net own-pad entry` — the conductor shares the opening pad's net and
  terminates inside the opening (for a track, an endpoint lies within it; for a
  via, its center does): the pad's own escape, not a defect neighbour.
- `same-net pass-through` — same net, but the track merely passes or bends across
  the opening without terminating there.
- `same-net` — same net, but the entry/pass-through distinction does not apply, for
  one of two reasons: the conductor is an area (a zone pour) or another pad, so it
  has no terminating endpoint to test; or the opening has no single owner pad (a
  merged mask region), so there is no pad to test an endpoint against — a track or
  via over such an opening has endpoints, but they are not judged.
- `different-net` — the copper belongs to another net.

While the assessment's coverage is incomplete, its findings are provisional: they
are capped at warning severity and each message says `provisional: coverage
incomplete`, so an unfinished run never inflates the error count. A closing
`mask_to_copper_openings` info finding counts the findings per opening, so one pad
with many findings reads as one opening.

### Copper on the pad's own land: `within_own_pad`

A track that enters its own pad lies partly under it, so the pad's opening covers
that part. That copper is the pad's own exposed land, and the one track width
the track puts under mask where it leaves the pad is unavoidable; JLCDFM does not
flag such an entry. Such a measurement reads `within_own_pad` at **info** (issue
#8858):

```text
Mask-to-copper overlap: within_own_pad; exposed area 0.42 mm²; beyond owner pad
copper 0 mm²; beside owner excess 0 mm²; ...; opening pad:R1/1; exposed copper
segment (net VOUT); same-net own-pad entry
```

The class is granted for what the copper does, not for touching the pad. All of
these must hold, otherwise the measurement keeps its verdict:

- the opening has one owner, a plain `rect`, `roundrect`, `circle` or `oval` pad
  with a mask margin of 0 or more (custom, padstack, chamfered and
  solder-mask-defined pads keep their verdict);
- the copper is on the pad's identified net. That is the net pcbnew loads: a
  floating stub touching only one pad loads with that pad's net. A pad with no
  net never qualifies.
- the copper overlaps the pad's copper by more than the construction budget `u`;
- **nothing beyond the pad is exposed**. `u` is tolerated once at the pad edge,
  so a track crossing any positive expansion ring wider than `u` (about 10 µm)
  stays an error;
- **nothing beside the pad beyond one clean entry**. The copper under mask within
  the policy clearance (0.09 mm) of the opening may not exceed what one straight
  crossing at 45° or steeper puts there (1.45 × track width × clearance). Only a
  straight segment with exactly one end inside the pad gets that allowance. A
  segment lying wholly on the pad gets none.

So these stay errors:

- a track that runs along or straddles the pad edge, or overlaps it by more than
  the allowance above (JLCDFM danger, #8914). The allowance is an **area
  budget**, not a ban on touching the edge: a straight entry may put up to
  1.45 × track width × 0.09 mm of copper under mask within 0.09 mm of the
  opening. A perpendicular entry uses 1.0 of that factor, so the rest can be
  copper beside an adjacent pad side, and the finding stays info. Measured on
  the pinned image (kicad-cli 10.0.6), 0 mm expansion: a 0.2 mm track entering
  a 1 × 1 mm rect pad perpendicular, with its centreline 0.05 mm inside the
  pad's side edge, 0.1 mm deep; a 0.4 mm track entering a 0.3 mm-wide pad
  0.1 mm deep; and a 0.3 mm track entering a 0.25 mm-wide pin 0.2 mm deep are
  all info, although the track's shoulder sits under mask against the pad's side
  at 0 mm;
- a straight **pass-through** with no end on the pad: it is not a clean entry,
  and the fab evidence does not decide it;
- an entry shallower than about 45°;
- arcs;
- vias touching the pad, because their ring leaves copper beside the opening;
- pours under and around the pad, unless they are the pad's own pour joined by
  thermal spokes (`own_pour_thermal`, below); a reviewed pour can also be
  declared instead, see below.

Same-net copper *beside* a pad without touching it is still measured at the
policy clearance. Each measurement records `exposed_beyond_owner_mm2` (copper
exposed beyond the pad) and `beside_owner_excess_mm2` (copper beside it in excess
of one clean entry), so a verdict can be audited. It also records
`within_own_pad_failed_condition` (issue #9305): the condition number (1-5, as
listed above) that kept a same-net, single-pad-owner finding at its measured
verdict — None when the class qualified or was not attempted, e.g. for
different-net copper or a merged opening. The trailing `same-net own-pad
entry` / `same-net pass-through` label is triage text from the copper outline
and can differ from the class for very wide or very short tracks. The class uses
the track's authored ends. Evidence and per-board counts:
[mask-to-copper-own-pad-8858](../research/mask-to-copper-own-pad-8858.md).

### Pads of one footprint: `within_own_footprint`

A footprint can put a rectangular ground pad under circular ground pads
(`RF_Module:ESP32-C3-WROOM-02`), so each circular opening exposes the rectangular
pad. When the opening is a single-pad opening (not a merged region), the
relation is `overlap`, and the conductor and the opening's owner are pads of
**one** footprint on one identical non-empty net, the measurement reads
`within_own_footprint` at **info** (issue #9420), so the evidence stays listed.
Merged openings, copper beside an opening, pads of different footprints or nets,
and any track, via or pour keep their measured verdict. The fab's own DFM view
cannot see footprints or nets, so this changes how kct reports the finding, not
whether the fab accepts the board.

The same pair rule applies to the 2 oz pad mask-bridge check inside
`physical_copper_gap` (`Pad mask bridge ... < 0.2000mm`): two pads of one
footprint on one identical non-empty net read at **info**, still listed. There
the fab evidence is located: JLCDFM measured the module's two 0.19 mm pad pairs
and rated them Good under pad spacing, with an empty Soldermask bridge row. The
rest of `physical_copper_gap` (pads of different footprints, different or
missing nets, pad-to-track/via/zone gaps) is unchanged.

### A pad's own pour joined by thermal spokes: `own_pour_thermal`

A pad inside its own same-net pour, joined to it by thermal spokes, has spoke
copper crossing the pad edge under mask. JLCDFM listed nothing for the four such
pads on board 01 (relief gaps 0.2 and 0.3 mm, 0 mm expansion; issue #8858): they
were absent from its "Solder mask opening exposing trace" list, not rated Good,
while different-net pour around the next pads was listed. Such a measurement
reads `own_pour_thermal` at **info** (issue #9284):

```text
Mask-to-copper overlap: own_pour_thermal; exposed area 0.6 mm²; beyond owner pad
copper 0 mm²; ...; opening pad:R2/2; exposed copper zone (net GND); same-net;
4 thermal spokes (declared spoke 0.3 mm, relief gap 0.2 mm)
```

All of these must hold, otherwise the measurement keeps its verdict:

- the opening has one owner, a plain `rect`, `roundrect`, `circle` or `oval` pad
  with a mask margin of 0 or more, on the zone's identified net;
- **pcbnew declares thermal relief** between the pad and this zone. The pad's own
  zone-connection override wins, then its footprint's, then the zone's setting;
  "thermal relief for through-hole pads only" counts only on a plated
  through-hole pad. The relief gap and spoke width are the pad's overrides when
  set, otherwise the zone's. A solid, "none" or unreadable connection never
  qualifies, and a custom `zone_connection` DRC rule already makes the native run
  incomplete;
- **the fill shows the spokes**: it overlaps the pad by more than `u`;
- **nothing beyond the pad is exposed**, exactly as for `within_own_pad`, so pour
  or spoke copper in any positive expansion ring wider than `u` stays an error;
- **within the examined band only spokes**. The band is the policy's warning
  clearance (0.15 mm for JLCPCB), or its danger clearance when it has no warning
  tier. Every connected piece of fill under mask within that band of the opening
  must touch the pad, run out to the band's outer edge, meet each edge of the
  band along at most 1.45 × spoke width, not wrap around the pad, and hold at
  most 1.45 × spoke width × band. There are 1 to 4 such spokes and nothing else.

So the copper this class excuses is the spokes and only the spokes: each spoke's
crossing of the pad edge out to 0.15 mm. The pour body must lie at least 0.15 mm
from the opening. These stay errors:

- a solid pad-to-pour connection (untested by the fab), whatever the fill looks
  like, and a thermal declaration whose fill is solid around the pad;
- a same-net pour not connected to the pad, a separate same-net zone or island
  beside the opening, or extra copper of the same zone in the band;
- a pour body inside 0.15 mm of the opening, even with spokes. The fab result
  covers relief gaps of 0.2 and 0.3 mm only; board 04's 0.1 mm relief gaps stay
  errors;
- spokes wider than declared, or more than four pieces in the band;
- pour copper exposed through a positive expansion;
- different-net pour, which keeps its measured verdict (a pour 0.13 mm from a
  different-net opening is a warning, as JLCDFM rates it).

The measurement records `pour_spokes`, `pour_relief_gap_mm` and
`pour_spoke_width_mm`; for this class `beside_owner_excess_mm2` is the non-spoke
copper found in the band (0). A solid-connection fab result would be the evidence
to revisit the class.

## Declare a particular connected escape

The owning pad is excluded only by its source UUID. Other copper remains eligible
for a finding even when it has the same net or overlaps the owning pad. A pad
number is not a unique source identity.

An intentional escape declaration must identify one pad, one directly connected
conductor (`segment`/`arc`/`via`/`zone`), the mask side and a rationale. A
`zone` is declarable when it is the pad's own same-net pour: the direct
connection is proven by the pad's copper overlapping the zone's fill region on
the declared copper side, beyond the geometric uncertainty. First run the check
without intent, review its measurements and save its binding. Then add a
declaration using that exact binding. For example, the Python API can construct
and audit it:

```python
from kicad_tools.validate.mask_copper import (
    MaskCopperPolicy,
    MaskEscapeIntent,
    check_mask_to_copper,
)

policy = MaskCopperPolicy(
    0.1, "Fictional test specification, section 3", "Example process only", "example-1"
)
native = {"native_python_command": ["/path/to/kicad/python3"]}
initial = check_mask_to_copper("board.kicad_pcb", policy, **native)
if initial.coverage != "complete":
    raise RuntimeError(initial.reasons)

# Replace these with reviewed UUIDs from the assessment/source PCB.
intent = MaskEscapeIntent(
    binding=initial.binding,
    owner_uuid="00000000-0000-0000-0000-000000000001",
    conductor_uuid="00000000-0000-0000-0000-000000000002",
    mask_side="F.Mask",
    rationale="Reviewed direct escape from this pad",
)
reviewed = check_mask_to_copper("board.kicad_pcb", policy, [intent], **native)
print(reviewed.to_dict())
```

For CLI use, insert `intent.to_dict()` into the request's `intents` array. The
serialized declaration has `binding`, `owner_uuid`, `conductor_uuid`, `mask_side`,
`rationale` and `scope` (currently `connected_escape`). Binding contains SHA256
values for the PCB, project, custom rules and export profile; absent sidecars
are represented by `null`. Keep the same native options when creating and using
an intent.

Changing source bytes, adding/removing sidecars or changing the bound export
profile invalidates the old declaration. Rejected declarations remain in
`intent_audit` with their reason and never suppress a measurement. Same-net
membership alone is insufficient: the checker also requires direct physical
connection on the selected copper side. Accepted declarations retain the measured
exposure with disposition `intentional`; they do not erase it.

Merged openings can create opening area beyond individual openings. Those
regions retain the contributing source identities and are not assigned to one
owner. A single-pad escape declaration does not waive a merged-region finding.

## Interpret results

Measurements record location in millimetres, mask side, copper layer, opening and
conductor UUIDs, clearance in millimetres and exposed area in square millimetres.
Exposure area is not penetration depth. Relations are `disjoint`, `touching` or
`overlap`; dispositions are `violation`, `uncertain`, `warning`, `intentional`,
`within_own_pad` or `own_pour_thermal`.

The comparison uses a recorded numerical construction budget, including native
polygon approximation and Gerber reconstruction. A result at the threshold,
within that budget, is uncertain and cannot establish a pass. This numerical
budget is not a fabrication tolerance or an exception to the process policy.
Negative expansion is recorded as mask-defined geometry, with expansion
provenance; that classification does not excuse exposure of neighboring copper.

Coverage is `complete`, `incomplete` or `not_run`. Always inspect coverage and
`passed`, not only the number of violations. Missing policy, native failures,
unsupported attribution, ambiguous identities or changed source inputs cannot
produce a clean assessment. Proven measurements may accompany incomplete
coverage and remain useful for investigation.

The checker stages immutable PCB/project/rule bytes, exports all four outer mask
and copper layers, and reads individual native objects by UUID in full board
context. It does not save or refill the authored board. Native object attribution
retains overlapping conductors independently; subtracting an owning pad from a
whole copper union would lose that evidence. Whole-layer agreement is an
additional coverage check, not proof of ownership.

Native objects on the inspected layers are checked against the source inventory
before any owning-pad exclusion. Uninventoried objects (including copper targets
and dimensions) make coverage incomplete even when other copper hides them in
the whole-layer union. This does not qualify their individual geometry.

Native support and context gaps are reported explicitly. For example, plotted
text with unresolved project/CLI variables requires additional context and cannot
be treated as fully attributed merely because other copper covers it. Inspect
the retained Gerbers, source hashes and native object provenance when reviewing
results. A passing assessment applies to its recorded inputs and supplied policy;
it is not supplier approval or qualification of an entire manufacturing order.
