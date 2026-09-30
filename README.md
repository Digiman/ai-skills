# ai-skills

Custom [Agent Skills](https://agentskills.io) — the open format Anthropic
released, adopted by Claude Code, opencode, Cursor, Copilot, Gemini CLI, and
others.

## Layout

```
skills/                     # one directory per skill
  dotnet-versions/
    SKILL.md                # required: frontmatter + instructions
    scripts/                # executable code the agent runs
.agents/skills -> ../skills # project-level discovery path (symlink)
scripts/install.sh          # symlink every skill into ~/.agents/skills
```

`.agents/skills/` is the cross-client discovery path from the spec, so an agent
opened on this repo finds the skills automatically. It is a symlink to `skills/`
so there is exactly one copy of each skill.

## Adding a skill

1. `mkdir -p skills/<name>/scripts`
2. Write `skills/<name>/SKILL.md` — see [AGENTS.md](AGENTS.md) for the house
   rules and the frontmatter contract.
3. `./scripts/install.sh` to link it into `~/.agents/skills`.

## Skills

| Skill | What it does |
| ----- | ------------ |
| [dotnet-versions](skills/dotnet-versions) | Audits installed .NET SDKs/runtimes, decides what to keep, prints sudo commands for the user to run. |
