---
description: Build the trail snapshot for each v* tag that has none, in one trail-only pull request
argument-hint: "[vX.Y.Z]"
---

Build Agent trail snapshots. Follow section 4 of the `trail` skill.

1. `git fetch -q --tags origin` and update the main branch. Work on a new branch from it.
2. The tags to build: $ARGUMENTS if given. Otherwise run `trail unsnapshotted`, which lists
   every tag newer than the oldest snapshot that has no snapshot, oldest first; before the
   first snapshot exists, it lists only the newest tag. If the list is empty, say so and stop.
3. For each tag, oldest first:
   - `trail skeleton --tag <tag> > <snapshots>/<tag>.md` (paths from `.trail.toml`; default
     `docs/trail/snapshots`).
   - Read every event the skeleton's comment lists as new, and fold each one into Keep doing,
     Stop doing, What we learned and Open. Every item cites an event (`#N`, `iN`,
     `dYYYYMMDD-slug`), and `issue #N` names an issue without citing it. Merge duplicates and
     keep their citations. Drop an item only when a newer event shows it's no longer true.
   - At a minor version (X.Y.0), rebuild instead: set `built_from: full`, read every event,
     and write the four sections fresh.
   - Delete the comment. Leave `Missing events` and `Index` exactly as printed.
4. Copy the newest snapshot to the current path (default `docs/trail/current.md`).
5. `trail check` must pass.
6. Open one pull request that changes only the trail paths (a trail-only PR, which is never
   tagged), and merge it when its checks pass, following the repository's merge rules.
