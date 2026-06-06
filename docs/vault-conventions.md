# Vault conventions (Project OS)

Workbench is opinionated about the vault it reads. It expects a plain folder of
Markdown files (an [Obsidian](https://obsidian.md) vault works perfectly, but
any folder of `.md` files does) laid out the **Project OS** way. This doc is the
whole convention — nothing here is hidden in code. If your vault follows it, the
sidebar, dashboards, demand-probe surface, and reviews board all light up.

You point Workbench at your vault with the `vault` symlink (see the README) or a
`vault_path` in Settings. Everything below is just files and frontmatter you
control in your editor — Workbench reads them live.

---

## The mental model: project = experiment

The one idea the whole system runs on: **everything is a project, and a project
is an experiment.** Big or small, serious or playful — that's *metadata, not
folders*. Each one runs the loop:

> **Observe → Hypothesize → Design → Run → Reflect → (Persist | Pause | Pivot)**

- **Experiment, not goal.** Failure is data, not guilt.
- **Micro-commitment:** a 2-minute act that just gets you moving (open the doc,
  send one message). Momentum does the rest.
- **Reflect** at the `review` date — never win/lose, always one of three
  adjustments: Persist, Pause, or Pivot.

This is encoded in frontmatter (`status`, `scope`, `hypothesis`,
`micro-commitment`, `review`), so the app can surface it. The loop, demand
probes, and anti-patterns are spelled out in `_System/Methods/Kaizen Loop.md` in
your own vault (create it from the snippet at the end of this doc, or write your
own — Workbench doesn't require it, it just reads what's there).

---

## Folder structure (the root *is* the index)

```
your-vault/
├── 00_Inbox/        unsorted capture; you triage from here
├── 10_Projects/     one folder or note per project = experiment
├── 20_Areas/        ongoing responsibilities, no end date (Work / Civic / Family / …)
├── 30_Resources/    reference material
├── 40_People/       contacts
├── _System/
│   ├── Agents/      agent definitions (the personas Workbench loads) — see README
│   ├── Methods/     your own conventions/playbooks (e.g. Kaizen Loop.md)
│   ├── Templates/   note templates you copy from
│   └── Reflections/ weekly reflection notes (the Reviews surface reads these)
└── _Archive/        superseded / retired notes
```

Rules of thumb:

- **The root is the single source of truth.** Status lives in *frontmatter*,
  never in folder location — you don't move a note between folders to mark
  progress. An `active` and a `done` project sit in the same `10_Projects/`.
- **Minimum to boot the app:** `10_Projects/` and `_System/Agents/`. The rest
  unlock features as you add them (`00_Inbox/` → inbox triage, `20_Areas/` →
  area grouping, `_System/Reflections/` → the reviews grid).

---

## Frontmatter: every note carries `type:`

Every note declares its kind in YAML frontmatter. That's how Workbench knows how
to render it. The important types:

| `type:`     | Folder            | Key extra frontmatter                                  |
|-------------|-------------------|--------------------------------------------------------|
| `project`   | `10_Projects/`    | `area:`, `status:`, `scope:`, `hypothesis:`, `review:` |
| `area`      | `20_Areas/`       | `area:` (self-identifying key)                         |
| `person`    | `40_People/`      | `relationship:`, `org:`                                |
| `resource`  | `30_Resources/`   | —                                                      |
| `reflection`| `_System/Reflections/` | `week:`, `date:`                                  |
| `agent`     | `_System/Agents/` | `model:`, `icon:` (see the README)                     |

### The two fields that drive the app

- **`status:`** — one of `idea` · `active` · `persist` · `pause` · `pivot` ·
  `done`. Active-status projects sort to the top of the sidebar; the reviews
  board queues `active`/`persist`/`pivot`.
- **`area:`** — on a project, this **must match an area note's own `area:`
  key**. That string match is the join: it's how projects group under their area
  in the sidebar and how an area dashboard's Dataview query finds its projects.
  (Dashboards are queries — never hand-maintain an index; fix the data in
  frontmatter.)

### A project = two valid shapes

Workbench treats both as one project:

- **Single file** — `10_Projects/Thing.md` (a "tiny" project).
- **Folder** — `10_Projects/Thing/Thing.md` is the main note; sibling files in
  the folder become context / sub-notes. Promote a single file to a folder when
  it grows artifacts.

### Demand probes (an opt-in project mode)

A low-touch `active` project that does one small action per week to *listen for
a bite*. Tag it `demand-probe` in `tags:` and Workbench pulls it into a
dedicated **Demand probes** section on Home, stalest-first. An optional `probe:`
block drives the at-a-glance line:

```yaml
tags: [demand-probe]
probe:
  last: 2026-05-28        # last outbound action (YYYY-MM-DD)
  bites: 0                # signals received
  cadence_days: 7         # optional; staleness warning fires after this
  channel: "LinkedIn DMs" # optional, free text
```

---

## Starter templates

Copy these into `_System/Templates/` (and adapt). New notes start from them.

### Project — `_System/Templates/Project.md`

```markdown
---
type: project
status: idea
scope: tiny          # tiny | small | big — size is metadata, not a verdict
area: Work           # must match an area note's `area:` key
hypothesis: "I will [action] for [duration]"
micro-commitment: "the 2-minute starter that bypasses resistance"
started: 2026-01-01
review: 2026-01-08
tags: []
---

# {{title}}

> Everything is a project = experiment. This is just data, not a verdict.

## Observe
What's working / not working / draining / energizing around this?

## Hypothesis
> **I will [action] for [duration].**

## Design
- Exciting? (genuinely curious about the outcome)
- Doable? (small enough to actually finish)
- Outside comfort zone? (if you know 99% of the result, it's a project, not an experiment)
- Touches reality? (exposes something to users / a market / the world — not only your head)

## Run — log
- [ ] 2026-01-01 — showed up

## Reflect — Persist / Pause / Pivot
_At `review` date, pick one and update `status`:_
- Persist → extend 1–2 weeks
- Pause → no capacity this season; not failure, just data
- Pivot → adjust scope/time/friction, try a variation

## Links
- Area::
- Related::
```

### Area — `_System/Templates/Area.md`

````markdown
---
type: area
area: "{{title}}"    # the self-identifying key projects point at
status: active
owner: me
review: 2026-01-01
tags: []
---

# {{title}}

Ongoing responsibility — no end date. Projects/experiments hang off this.

## Purpose
Why this area exists; what "good" looks like.

## Active projects
```dataview
TABLE status, scope, review
FROM "10_Projects"
WHERE area = this.area
SORT review ASC
```
````

### Weekly reflection — `_System/Templates/Weekly Reflection.md`

```markdown
---
type: reflection
week: 2026-W01
date: 2026-01-01
---

# Weekly Reflection — {{date}}

> Low-friction. Skipping a week is allowed — no guilt, just resume.

## Observe (no judgement)
- Energized me:
- Drained me:
- Worked / didn't:

## Decide — Persist / Pause / Pivot
_Update each due project's `status` + push its `review` date:_
- Persist →
- Pause →
- Pivot →

## Next week's micro-commitment(s)
- [ ]
```

---

## Bootstrapping a vault from scratch

1. Make a folder and the skeleton:
   ```bash
   mkdir -p my-vault/{00_Inbox,10_Projects,20_Areas,30_Resources,40_People,_Archive}
   mkdir -p my-vault/_System/{Agents,Methods,Templates,Reflections}
   ```
2. Add at least one **agent** in `_System/Agents/` (see the README — the body is
   the system prompt). Without one, the chat has no persona to load.
3. Create one **area** note (`20_Areas/Work.md` with `area: Work`) and one
   **project** that points at it (`area: Work`). Workbench should now show the
   project grouped under that area.
4. Point Workbench at the folder (the `vault` symlink, or `vault_path` in
   Settings) and reload.

Open it in Obsidian too if you want backlinks, graph view, and Dataview
dashboards — Workbench and Obsidian read the same plain files, no lock-in.
```
