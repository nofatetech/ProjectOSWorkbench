# Testing the chat tools

How to exercise the agent's tool-calling (Phase B) by hand in the running app.

## Prerequisites
- **Settings → force-mock OFF** and a real **OpenRouter API key** set.
- **Settings → "Enable tools" ON** (default on).
- You're in a **project chat thread** (so the project's vault folder + working_dir
  context apply).

While a turn runs you'll see `› tool(args)` indicator lines in the reply as each
tool fires. Use the 🐛 button in the chat header to inspect the exact prompt sent.

## Available tools
`read_vault_note` · `write_vault_note` · `list_dir` · `move_note` · `run_shell`

- Relative paths resolve against the **vault root**; absolute paths are used as-is.
- `run_shell` runs in the project's **working_dir** (or the vault root if none).
- Trust mode: writes/commands apply immediately (no confirm). The user manages git.

## Examples (type these into a chat)

**1. Read a note**
> Read `10_Projects/Project OS/Workbench.md` and summarize the v0.3 plan in 3 bullets.

Expect: `› read_vault_note(10_Projects/Project OS/Workbench.md)` then a summary.

**2. List a folder**
> What's in my `00_Inbox` folder right now?

Expect: `› list_dir(00_Inbox)` then the file list.

**3. Write a new note** (safe throwaway)
> Create a note at `00_Inbox/tool-test.md` with the text "hello from the agent" and today's date in frontmatter.

Expect: `› write_vault_note(00_Inbox/tool-test.md)`. Verify in Obsidian, then delete.

**4. Move / rename**
> Rename `00_Inbox/tool-test.md` to `00_Inbox/tool-test-renamed.md`.

Expect: `› move_note(00_Inbox/tool-test.md)`.

**5. run_shell** (in the project's working_dir)
> Run `git status` and tell me if there are uncommitted changes.

Expect: `› run_shell(git status)` then a summary. (`ls` works too.)

**6. Multi-step (chains several tools)**
> Look through `10_Projects`, find which projects have `status: active` in their frontmatter, and list them.

Expect several `› list_dir(...)` / `› read_vault_note(...)` lines, then the answer.

## Tips
- Start with **read / list** (read-only) before write/move/shell.
- If it answers *without* a `›` line, it skipped the tool — nudge: "use the tools
  to check, don't guess."
- Errors surface inline as `[tool error: …]` or `[OpenRouter HTTP …]` — those
  strings pinpoint the layer.
