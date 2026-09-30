# Working on this repo

Repo of custom [Agent Skills](https://agentskills.io/specification). One
directory per skill under `skills/`.

## Layout

- `skills/<name>/SKILL.md` — required. YAML frontmatter, then the instructions.
- `skills/<name>/scripts/` — executable code the agent runs.
- `skills/<name>/references/` — docs the agent reads on demand.
- `skills/<name>/assets/` — templates and data.
- `scripts/install.sh` — links each skill into `~/.agents/skills`.

`.agents/skills` is a symlink to `skills/`. Do not add real directories under
`.agents/` — edit the target.

## Frontmatter contract

`name` and `description` are required. Everything else is optional.

- `name` — 1-64 chars, `a-z` `0-9` and hyphens, no leading, trailing, or
  consecutive hyphens. Must equal the directory name.
- `description` — 1-1024 chars. Carries the entire burden of triggering, so
  name the trigger conditions and the keywords a user would actually type, not
  just the capability.
- `compatibility` — only when the skill has real environment requirements.
- `metadata` — string-to-string map. Keep a `version` here.

## House rules

**Instructions over implementation.** A skill earns its keep by encoding
judgment an agent would otherwise get wrong. The body should say what to do,
what to refuse, and what to hand back to the human — not narrate mechanics the
agent can derive. When a script already encodes the whole analysis, say "run
the script" and stop; do not re-derive its output by hand.

**Every path is relative to the skill directory.** Never hardcode
`~/.config/...`, `~/.claude/...`, or an absolute repo path in `SKILL.md`. The
skill has to work from `~/.agents/skills/`, from a clone, and from a symlink.
Scripts should locate siblings with `Path(__file__)`.

**Say when to stop.** State the exit conditions explicitly: what the agent must
hand over verbatim, what it must not run, what counts as failure. A skill that
does not know where to stop will keep going.

**Keep `SKILL.md` under 500 lines.** Push detail into `references/` and let the
agent load it only when it matters.

**Prefer one level of reference depth.** `SKILL.md` → `references/foo.md`.
No chains.

## Verifying a change

```bash
./scripts/validate.py          # frontmatter + layout checks for every skill
```

Then run the skill end to end and read its real output. A skill whose script
crashes is worse than no skill, because the agent trusts the description and
loads the body before finding out.
