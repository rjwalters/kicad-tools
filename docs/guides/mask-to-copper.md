# Checking mask-to-copper clearance and exposure

The opt-in `mask_to_copper` check compares native solder-mask openings with
individually identified copper objects. It supplements the existing solder-mask
expansion, pad-size and annular-ring checks. See [mask geometry](mask-geometry.md)
for the underlying geometry inspection APIs.

Supply a process-specific clearance requirement with its source, process and
revision. A manufacturer profile, mask expansion or minimum mask-web width does
not supply this requirement. The example below uses a **fictional test policy**;
replace all four policy values with the requirement for your actual process.

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

## Declare a particular connected escape

The owning pad is excluded only by its source UUID. Other copper remains eligible
for a finding even when it has the same net or overlaps the owning pad. A pad
number is not a unique source identity.

An intentional escape declaration must identify one pad, one directly connected
segment/arc/via, the mask side and a rationale. First run the check without intent,
review its measurements and save its binding. Then add a declaration using that
exact binding. For example, the Python API can construct and audit it:

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
`overlap`; dispositions are `violation`, `uncertain` or `intentional`.

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

Native support and context gaps are reported explicitly. For example, plotted
text with unresolved project/CLI variables requires additional context and cannot
be treated as fully attributed merely because other copper covers it. Inspect
the retained Gerbers, source hashes and native object provenance when reviewing
results. A passing assessment applies to its recorded inputs and supplied policy;
it is not supplier approval or qualification of an entire manufacturing order.
