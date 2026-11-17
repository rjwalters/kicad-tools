# Why Champion's PR Auto-Merge Workflow Lives in One File, Not Two

`champion-reference.md`'s "Complete Auto-Merge Workflow Script" section used to
carry a second, full copy of the end-to-end merge script, duplicating
`champion-pr-merge.md`. That duplicate diverged from `champion-pr-merge.md` over
time — it lacked Step 5.5 Follow-on Issue Creation and repeated the same bugs
(invalid `gh pr checks --json` fields, etc.) — forcing every fix to be applied
twice. It was removed to eliminate the drift (issue #3781).

`champion-pr-merge.md` is now the single source of truth for the script;
`champion-reference.md` keeps only the edge-case behavior descriptions and
decision matrix, which reference the script rather than restate it.
