# Clean-room reimplementation of GPL KiCad behaviour

kicad-tools is MIT. KiCad is GPL. Copying KiCad's *expression* — its function
breakdown, identifiers, or step order — makes this tree a GPL derivative, which
is what happened twice before this policy existed: PR #6661 (diff-pair
coupling) and PR #6779 (track length calculation). The first response was a
blanket ban: never read KiCad source, probe the pinned kicad-cli image as a
black box only. That ban was stricter than the established practice for the
problem and blocked work probing cannot recover (the embedded-model checksum,
[research/3d-model-coverage.md](research/3d-model-coverage.md)).

**Operator ruling, 2026-11-19:** standard **two-party clean-room
reimplementation** is allowed. Copyright protects expression, not behaviour;
a reader/writer split that keeps the writer away from the source's expression
does not derive from it.

This file is repo policy, not legal advice. When in doubt about a source not
covered below, ask the operator before reading it.

## Scope

The protocol applies to:

- **KiCad itself** (GPL) — its source, and any behaviour the public
  file-format and CLI documentation does not define;
- **GPL derivatives of KiCad**, e.g. PCBJam's WebAssembly port of pcbnew;
- **AGPL neighbours** tracked in the ecosystem registry
  (`copyleft-ideas-only`): the same copyleft logic applies, so their
  *behaviour* may also be reimplemented through this protocol. Recorded in
  [ecosystem.md](ecosystem.md).

It does **not** apply to **unlicensed neighbours** (`unlicensed`): with no
license there is no permission to study a work for reimplementation at all,
and a reader/writer split does not change that. They stay ideas-only.

## The two-party protocol

### 1. Reader (spec author)

One agent may read the GPL source for a topic. It produces a **behaviour
spec only**:

- inputs, outputs, rules, units;
- edge cases and error cases;
- test vectors: inputs with their expected outputs.

The spec must **not** contain code, pseudo-code that follows the original's
structure, or internal identifiers beyond public file-format and CLI names.

Afterwards the reader is **tainted for that topic**: it must not write,
co-write, or review the implementation, and must not discuss the source's
expression with the writer.

### 2. Spec review

Before handover, a third agent (or the Judge) reviews the spec for leaked
expression: structural echoes of the source (function breakdown,
identifier-shaped names, step order) and content beyond the allowed list
above. The spec moves to the writer only after this review.

### 3. Writer (implementer)

A different agent — one that has never seen the source — implements from:

- the reviewed spec;
- public documentation;
- black-box probes on the pinned kicad-cli image.

The writer never sees the source, the reader's raw notes, or any diff
against the original.

### 4. Provenance

Commit the reviewed spec under `docs/clean-room/<topic>.md`, started from
[TEMPLATE.md](clean-room/TEMPLATE.md). The provenance block records which
source version the reader studied and which agents read, reviewed, and
wrote. The implementing PR links the spec.

### 5. Acceptance

Black-box probes against the pinned kicad-cli image remain the test of
correctness. A clean spec changes how the code may be written, not what
"correct" means.

## Judge checklist for clean-room PRs

On any PR that implements a clean-room spec:

1. **Reader-is-not-writer.** Compare the PR author (and committers) against
   the reader agent id in the spec's provenance. Provenance names are agent
   ids, not people; an exact or aliasing match fails the PR. If the reader
   wrote or committed implementation code, the taint rule was broken — send
   the PR back.
2. **Structural-port check against the spec.** Run the structural-port check
   from the #6661/#6779 post-mortems — same function breakdown, same
   identifiers, same step order — comparing the implementation **against the
   committed spec, not against KiCad source**. The reviewer consults the
   spec; it does not open the GPL source.
3. **Spec review happened.** Provenance names a spec reviewer distinct from
   both reader and writer. A spec with no reviewer is not handover-ready.
4. **Acceptance is probes.** The PR's verification is black-box probes on
   the pinned image (or the repo's equivalent oracle, e.g.
   `scripts/research/embedded_checksum_probe.py verify`).

## Relationship to the other license rules

- The registry's `license_compat` vocabulary and the code-direction rules
  live in [ecosystem.md](ecosystem.md). This protocol changes none of them
  except one: `copyleft-ideas-only` behaviour may now cross as a spec, not
  as code. Code still moves in neither direction.
- Running a GPL tool as an external process (e.g. `kicad-library-tools`)
  remains the pattern for anything that needs the tool itself, not a
  reimplementation of it.
