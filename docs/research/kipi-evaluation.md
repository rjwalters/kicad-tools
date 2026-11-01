# KiPI evaluation (Issue #8185)

[KiPI](https://github.com/drewsum/KiPI) (`drewsum/KiPI`, GPL-3.0-or-later,
Python, 83 stars) is a power-integrity analyzer: DC voltage drop / current
density / via currents / electro-thermal, AC PDN impedance, and switcher
coupled noise. It runs as a KiCad 9/10 IPC plugin or standalone.

Read at commit `842b4ce` (v1.0.0, pushed 2026-10-31); evaluated 2026-11-01.
Registry entry: `kct ecosystem show kipi`.

**Licence boundary: ideas only.** GPL-3.0 code must not enter this MIT
repo, in either direction. This note records capabilities, public function
names and one measured result. No KiPI code or data files are in this diff.
The trial below used a throwaway venv in the worktree (deleted afterwards)
and a scratch driver script kept outside the repo.

## Q1. Is there a headless path?

**Partly: a library path, no solver CLI.**

- `kipi` (`kipi.__main__:main`) only opens the Qt GUI. Its single non-GUI
  flag, `--dump`, extracts a board from a *running* KiCad over IPC.
- The solver is importable and GUI-free. The relevant entry points:
  - `kipi.sources.kicad_pcb.source.KiCadFileSource(path).load()` reads a
    `.kicad_pcb` file into a `BoardSnapshot` (no KiCad needed; it loaded
    `boards/09-usbc-pd-power` in about 1 s).
  - `kipi.dc.solver.run(snapshot, NetProblem(...))` meshes, solves and
    post-processes one net. `NetProblem`, `VoltageSource`, `CurrentLoad`,
    `SeriesLink` and `SolveSettings` describe the rail.
  - The upstream test suite (`tests/dc/test_solver.py`) drives exactly this
    API against closed-form results, so it is exercised, not incidental.
- What is *not* headless: the rail definition (which pads are the VRM, which
  are loads, load currents, series parts) is authored in the GUI and the
  power-tree model (`kipi.dc.pi.*`).
  There is no documented file format for "this board's rails" that a script
  can feed in; we would have to build `NetProblem`s ourselves.
- Install footprint: the solver needs only numpy, scipy, shapely, triangle
  and pyamg. The package metadata also lists PySide6, pyvista and plotly as
  hard dependencies, but the solver and `.kicad_pcb` reader import without
  them (we did not install them).
- AC and coupled-noise solvers (`kipi.ac`, `kipi.noise`) were not tried.

**Upstream ask** (not filed by us): a `kipi solve board.kicad_pcb rails.yaml
--json` command, since KiPI already has `PyYAML` and a YAML I/O module
(`kipi/core/yaml_io.py`).

## Q2. Trial on one board

- **Board**: `boards/09-usbc-pd-power/output/usbc_pd_power.kicad_pcb`
  (this repo; the `gf180-drone-fc` and `tcim-demo-pcba` boards named in the
  issue are not in this repo).
- **Rail**: `+5V_OUT`, buck output U3.8 (modelled as 5.000 V source) to
  connector J2.1 (modelled as a 3.0 A load). 13 track segments on F.Cu and
  B.Cu, 6 vias, no zones.
- **Setup**: `uv venv` in the worktree; `uv pip install numpy shapely scipy
  triangle pyamg PyYAML matplotlib plotly`; `PYTHONPATH` to a shallow clone;
  a 25-line driver calling the API above with `SolveSettings(max_edge_mm=0.3)`.
- **Runtime**: board read about 1 s, mesh + solve 0.1 s. One rail, no grids.
- **Output**: load-pad mean 4.9013 V, i.e. **98.7 mV drop at 3 A**; source
  current 3.0000 A; energy-balance error 2e-11; peak current density about
  776 A/mm^2.
- **Hand-calc check**: minimum-resistance path through the same track
  graph, rho*L/(w*t) with rho = 1.724e-5 ohm*mm (KiPI's
  `RHO_CU_OHM_MM`), t = 0.035 mm, vias taken as zero ohms, pad copper ignored: 40.5 mohm, i.e.
  **121.5 mV at 3 A**. KiPI is 19% lower. That is the expected direction:
  the hand-calc takes only the single best path and ignores pad copper and
  the parallel paths the 6 vias and two layers offer. It is a sanity check
  (same order, right sign of error), **not** an accuracy validation. Upstream
  validates the solver against closed-form cases in `tests/dc/test_solver.py`
  and the energy balance above is a self-consistency check, which is the
  stronger evidence.
- **Caveats**: one rail on one board; the source/load pads and the 3 A are
  our assumptions, not read from the design; AC/noise/thermal untested.
- **Reproduce**: shallow-clone `drewsum/KiPI` at `842b4ce`, create a venv
  with the packages above, and run the API calls in Q1 on the board and rail
  named here.

## Q3. Integration shape, ranked

(a) **Document a recipe, no integration.** Tell users to run KiPI (GUI or
the library calls) themselves. Zero code, zero licence risk. Does not feed
`kct check` or agents.

(b) **Optional subprocess adapter** (`kct analyze power`) that shells out to
a user-installed KiPI and parses its output. Legal as an arm's-length
process boundary, but today KiPI has no stable CLI or output schema, so we
would pin to internals. Blocked on the upstream ask in Q1.

(c) **Native DC IR-drop check in kct**, clean-room from the physics (sheet
resistance, FEM on a Delaunay mesh) with KiPI as an *external oracle* in
tests only. Highest value (works offline, in `kct check` and for agents) and
highest cost; the licence rule means written from textbooks, never from
KiPI's source.

**Ranking: (a) now, then (c), with (b) only if upstream adds a CLI.** (a) is
free and answers the user need. (c) is the durable option; the trial shows
the answer is cheap to compute (0.1 s) and checkable against hand-calcs.
(b) is the worst fit: it couples us to an unstable surface and puts a GPL
tool on our critical path.

## Q4. Follow-up

**Not now, with reason.** No follow-up issue is filed from this PR: the
issue asked for shape-specific issues "for the chosen shape", and the chosen
shape (a) needs no code. Shape (c) needs a product decision (is DC IR-drop
in scope for `kct check`?) before it is worth an issue; shape (b) needs
upstream to ship a CLI first. Re-evaluate on either trigger (see the
registry's `reeval_trigger`).
