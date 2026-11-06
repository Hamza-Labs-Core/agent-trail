---
description: Load the trail, the current snapshot plus the events merged since it, before starting work
argument-hint: "[topic to focus on]"
---

Load the Claude trail for this repository. Read `.trail.toml` for the paths (defaults:
`docs/trail/current.md`, `docs/trail/events`).

1. `git fetch -q origin main` (or the configured remote and main branch).
2. Read the current snapshot in full.
3. List the events added since its `up_to_commit` and read each one:
   `git diff --name-only --diff-filter=A "$(sed -n 's/^up_to_commit: //p' <current>)" origin/main -- <events>`
   If there's no current snapshot yet, run `trail index` and read the events that touch the
   task (or the newest 20).
4. If `.trail.toml` names a `legacy` file and the topic needs history, search that file with
   `grep -n`. Don't read it whole.

Then summarize, in a few lines, the lessons that bear on $ARGUMENTS (or on the work at hand):
what to keep doing, what to avoid, and what's still open with its issue number.
