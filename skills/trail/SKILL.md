---
name: trail
description: The Claude trail, a repository's record of what Claude sessions learned. Use it at the start of every session in a repository with a .trail.toml (read the current snapshot), whenever a pull request is about to merge or has merged (write its event), when the trail hook says a merged PR has no event, before a session ends or compacts, and after a v* tag (build the snapshot). It covers the events and snapshots folders and the `trail` command.
---

# The Claude trail

The trail keeps what sessions learned, so the next session starts from it. It isn't a
changelog and it isn't a list of pull requests (git already has that). It holds what only the
session saw: the approach that failed, the root cause found after the wrong guesses, the rule
that was corrected partway through, the gotcha that cost an hour.

The repository's `.trail.toml` sets where things live. The defaults are below; read the file
when it changes them:

| | default |
| --- | --- |
| events | `docs/trail/events/` |
| snapshots | `docs/trail/snapshots/vX.Y.Z.md` |
| current snapshot | `docs/trail/current.md` |
| event names | `NNNN-slug.md` (PR, zero-padded to `events.pr_digits`), `iNNNN-slug.md` (issue), `dYYYYMMDD-slug.md` (day) |

The plugin puts `trail` on the PATH. Without the plugin, run
`uvx --from git+https://github.com/Hamza-Labs-Core/claude-trail trail …`.

## 1. At session start: read

`/trail:read` does this. By hand:

```bash
cat docs/trail/current.md
git fetch -q origin main
git diff --name-only --diff-filter=A "$(sed -n 's/^up_to_commit: //p' docs/trail/current.md)" origin/main -- docs/trail/events
```

Read those files too: the snapshot doesn't have them yet. Before the first snapshot exists,
`trail index` lists every event; read the ones that touch your task. If `.trail.toml` names a
`legacy` file (a frozen trail from before the events), search it with `grep -n` when a topic
needs history. Don't read the whole file.

## 2. For each pull request you merge: write its event

`/trail:event` does this for the current branch's pull request.

Write the event **in the pull request itself**, before it merges, while you still remember the
work. Each event is its own file, so events never conflict. Name it `<events>/NNNN-slug.md`
with the PR's number. Open the PR first to get the number.

Write what only this session knows, not what the diff already shows:
- **What happened**: the change in two or three sentences, the root cause, the numbers.
- **What we learned**: the gotcha, the wrong first guess and why it was wrong, what a tool or
  the code really does, a rule that was corrected.
- **Keep doing / Stop doing**: only when the work showed one. Don't pad: leave the section out.
- **Open**: what's left, with its issue number. File the issue first.

```markdown
---
schema: 1
kind: pr                # pr | incident | decision | correction | release | migration
pr: 634
title: YouTube sources from linked videos
date: 2026-11-04        # the day the work happened (or merged)
session: <your session id>   # the name of your transcript file
prs: [628]              # optional: other pull requests this event is also about
---

## What happened
...

## What we learned
...
```

Something learned **after** the merge (CI on main, the release, the live update) goes in a
second event for the same PR, `NNNN-after-merge.md`, in a trail-only pull request. A
trail-only pull request changes only the `trail_only` paths. Work with no PR is named for its
issue (`iNNNN-slug.md`) or its day (`dYYYYMMDD-slug.md`), with the right `kind`.

Check before you push:

```bash
trail check
```

Rules: use real PR, issue and sha numbers only (from `gh` and `git`); never write secrets
(`trail check` refuses the common shapes); state requirements and decisions neutrally;
never edit an event after it merges. Write a `kind: correction` event with `corrects:`
instead.

## 3. When the hook speaks

The plugin's hook runs at Stop, PreCompact, SessionStart and SessionEnd in a repository with a
`.trail.toml`. It lists pull requests this session merged (`gh pr merge N` in the transcript,
merged on the main branch) that have no event on any branch or worktree. When it blocks a Stop
or reminds you after a compact, write those events now. At startup it may list PRs that
earlier sessions left without one: write them if you have the context.

## 4. After a v* tag: build the snapshot

`/trail:snapshot` builds every missing one. `trail unsnapshotted` lists the tags that have no
snapshot. For each tag, oldest first:

```bash
trail skeleton --tag vX.Y.Z > docs/trail/snapshots/vX.Y.Z.md
```

The skeleton carries the newest snapshot older than the tag forward (its sections) and lists
the new events in a comment. Fold each new event in:
- Add its lessons to the right section with its citation (`#634`, `i649`, `d20261104-slug`).
  An issue that is only mentioned is written `issue #649`, which cites nothing.
- Merge items that say the same thing, and keep all their citations.
- Drop an item only when a newer event shows it's no longer true.

Then:
1. Delete the comment.
2. Leave `Missing events` and `Index` exactly as printed.
3. Copy the snapshot to the current path.
4. Run `trail check`.
5. Merge it as a trail-only pull request.

At a minor version (v0.4.0, v0.5.0 …), rebuild from every event instead (`built_from: full`):
read all the events and write the sections fresh, so the summary doesn't drift. If a tag was
skipped, the next snapshot covers every event since the last one.

A session builds snapshots, never a model in CI. Trail-only commits are never tagged
(`trail is-trail-only` tells tag automation which commits those are).
