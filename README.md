# Workbench

A local "agent OS" that turns a plain folder of Markdown (an Obsidian vault)
into the working memory of an AI workspace. You chat with an agent that reads and
writes your notes directly, organized around **Project OS** — the idea that
everything you do is a *project = experiment* running a simple loop
(Observe → Hypothesize → Design → Run → Reflect).

**Workbench is a spec, and each edition is an implementation of it in a stack.**
The contract every edition honors is the vault layout and conventions in
[`docs/vault-conventions.md`](docs/vault-conventions.md) — folder structure,
frontmatter, the project/area/inbox/review model. Pick whichever edition fits
your platform and taste; they all read and write the same plain Markdown, so
there's no lock-in and you can switch freely.

## Editions

| Edition | Stack | Status | |
| --- | --- | --- | --- |
| **Flet/Python** | [Flet](https://flet.dev) (Python desktop) | First edition — daily-driven | [`editions/flet-python/`](editions/flet-python/) |

The Flet edition was the first build, chosen for fast iteration. It is **not**
declared the final stack — Workbench is deliberately stack-agnostic, and other
editions (web, native, TUI, …) are welcome.

## Repository layout

```
.
├── docs/
│   └── vault-conventions.md   ← the shared spec every edition implements
├── editions/
│   └── flet-python/           ← Edition 1 (see its own README to run it)
└── LICENSE                    ← public domain (The Unlicense), covers everything
```

Anything shared across editions (the spec, and later any shared assets or
schemas) lives at the top level. Anything stack-specific — code, build config,
dependency locks — lives inside that edition's folder.

## Running an edition

Each edition has its own README with setup and run instructions. For the Flet
edition:

```bash
git clone git@github.com:nofatetech/ProjectOSWorkbench.git
cd ProjectOSWorkbench/editions/flet-python
# follow editions/flet-python/README.md
```

## Contributing an edition

An edition is any Workbench client that implements the spec in
[`docs/vault-conventions.md`](docs/vault-conventions.md): it reads a Project OS
vault, lets you chat with an agent over it, and reads/writes notes as plain
Markdown. To add one:

1. Create `editions/<stack-name>/` (name it by stack, e.g. `tauri-rust`,
   `web-svelte` — editions are parallel implementations, not a version series).
2. Implement against the shared spec; keep all stack-specific files inside your
   edition folder.
3. Add a self-contained README in your edition folder, and a row to the
   **Editions** table above.

If your design surfaces a gap or ambiguity in the spec, propose an edit to
`docs/vault-conventions.md` in the same change — the spec is meant to evolve as
editions teach us what the contract should be.

## License

Public domain — released under [The Unlicense](LICENSE). No copyright, no
attribution required. Copy, modify, sell, or do whatever you want with it. The
dedication covers the whole repository, every edition included.
