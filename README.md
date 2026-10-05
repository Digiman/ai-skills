# ai-skills

Custom [Agent Skills](https://agentskills.io) — the open format Anthropic
released, adopted by Claude Code, opencode, Cursor, GitHub Copilot, Gemini CLI,
and 75+ others.

Built on the standard, so the same repo installs with **either** package
manager. Nothing is duplicated: both read `skills/<name>/SKILL.md` directly.

## Install

With the `skills` CLI (npm), which also publishes to [skills.sh](https://skills.sh):

```bash
npx skills add digiman/ai-skills                    # every skill, detected agents
npx skills add digiman/ai-skills --skill dotnet-versions -a claude-code -a opencode
npx skills add digiman/ai-skills --list             # preview without installing
```

With [APM](https://microsoft.github.io/apm/) (Microsoft's Agent Package
Manager) — add to the consuming project's `apm.yml`:

```yaml
dependencies:
  apm:
    - digiman/ai-skills
    # pin it, or pick a subset:
    # - digiman/ai-skills#v0.1.0
    # - git: digiman/ai-skills
    #   skills: [dotnet-versions]
```

```bash
apm install
```

Both resolve straight from git — no registry, no build step. To produce an
offline bundle or a Claude plugin artifact, `apm pack --archive`.

To link the skills into your own machine without either CLI:

```bash
./scripts/install.sh          # symlinks into ~/.agents/skills and ~/.claude/skills
```

## Layout

```
skills/<name>/SKILL.md        # the standard: one directory per skill
           scripts/           # executable code the agent runs
           references/        # docs loaded on demand
           assets/            # templates and data
tests/<name>/test-*.py        # decision-logic tests -- repo root on purpose
scripts/install.sh            # link skills into ~/.agents/skills
scripts/validate.py           # spec + packaging checks for every skill
apm.yml                       # APM manifest
.agents/skills/               # NOT used -- see below
```

**Two deliberate choices.** There is no `.agents/skills` directory in this repo.
`npx skills` scans both `skills/` and `.agents/skills/`, so a symlink between
them doubles the discovery surface; APM deploys *to* `.agents/skills/`, so a
symlink there turns its own deploy into a silent no-op. Use `./scripts/install.sh`
for your machine instead.

Tests live at the repo root rather than inside a skill, because both package
managers copy a skill folder whole — anything under `skills/<name>/` installs
into every consumer's harness. `scripts/validate.py` fails the build if that
invariant breaks.

## Adding a skill

```bash
mkdir -p skills/<name>/scripts
$EDITOR skills/<name>/SKILL.md
./scripts/validate.py
python3 tests/<name>/test-*.py      # if you added decision logic
./scripts/install.sh                 # link it locally
```

Read [AGENTS.md](AGENTS.md) for the frontmatter contract and the house rules
before writing one.

## Skills

| Skill | What it does |
| ----- | ------------ |
| [dotnet-versions](skills/dotnet-versions) | Audits installed .NET SDKs/runtimes against Microsoft's release index for support status and EOL, decides what to keep, checks `global.json` pins, and prints `sudo` commands for you to run. Never removes anything itself. |

## Verifying a change

```bash
./scripts/validate.py            # frontmatter, layout, packaging hazards
python3 tests/*/test-*.py        # decision logic
apm audit --file skills/<name>/SKILL.md   # APM's own security scan
```

CI runs all three on every push.

## License

[MIT](LICENSE)