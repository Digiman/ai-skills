# ai-skills

Custom [Agent Skills](https://agentskills.io) — the open format Anthropic
released, adopted by Claude Code, opencode, Cursor, GitHub Copilot, Gemini CLI,
and 75+ others.

Built on the standard, so the same repo installs with **three** tools. Nothing
is duplicated: every one of them reads `skills/<name>/SKILL.md` directly.

| Tool | Command |
| ---- | ------- |
| skills CLI | `npx skills add digiman/ai-skills` |
| APM | `apm install digiman/ai-skills` |
| Claude Code plugin | `/plugin marketplace add digiman/ai-skills` then `/plugin install ai-skills@digiman` |

## Install

### Claude Code, as a plugin

```text
/plugin marketplace add digiman/ai-skills
/plugin install ai-skills@digiman
```

Skills arrive as `/ai-skills:dotnet-versions`, and Claude Code keeps them in
its plugin cache rather than copying them into `~/.claude/skills/`. That is the
one install path where a generic skill name cannot overwrite an unrelated skill
from another author — see [Skill names](#skill-names).

To try it without installing, start Claude Code with `--plugin-dir` pointing at
a clone of this repo.

### With the skills CLI

```bash
npx skills add digiman/ai-skills                    # every skill, detected agents
npx skills add digiman/ai-skills --skill dotnet-versions -a claude-code -a opencode
npx skills add digiman/ai-skills --list             # preview without installing
```

### With APM

Add to the consuming project's `apm.yml`:

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

All paths read straight from git — no registry, no build step. To produce an
offline bundle or a Claude plugin artifact, `apm pack --archive`.

### On another machine

Nothing is machine-specific, so a fresh machine needs one command:

```bash
npx skills add digiman/ai-skills -g        # or: apm install digiman/ai-skills
```

`-g` installs for the current user (`~/.agents/skills/`); drop it to install
into the project instead. Both tools read straight from git, so there is no
build step and no registry.

To link from a local clone instead — useful while authoring:

```bash
git clone https://github.com/digiman/ai-skills && cd ai-skills
./scripts/install.sh                       # symlinks into ~/.agents/skills and ~/.claude/skills
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
.claude-plugin/plugin.json    # Claude Code plugin manifest
.claude-plugin/marketplace.json  # Claude Code marketplace index
.agents/skills/               # NOT used -- see below
```

## Skill names

Names are plain and unprefixed, the way the ecosystem expects. The **repo**
carries the identity — `digiman/ai-skills` — not each skill's name.

That is safe for the plugin path and has one sharp edge on the other two. All
skills install into a single flat directory per agent, so two authors shipping
`code-review` collide, and the second install wins. Measured on this machine:
`npx skills` overwrote an unrelated `code-review` with no prompt and no
warning; APM refused and required `--force`. Installing as a Claude plugin
avoids this entirely, because plugin skills are namespaced
(`/ai-skills:dotnet-versions`) and live in the plugin cache.

If you ever add a skill whose name is common (`code-review`, `tdd`, `research`,
`implement`), either rely on the plugin install path or expect to rename it
later. The spec has no namespace mechanism: RFC
[agentskills#312](https://github.com/agentskills/agentskills/issues/312)
proposed `collection/skill-name`, closed without landing, and `name` still
permits only `a-z`, `0-9` and hyphens.

**Renaming is a breaking change** for anyone who installed the old name.

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
| [digiman-dotnet-versions](skills/digiman-dotnet-versions) | Audits installed .NET SDKs/runtimes against Microsoft's release index for support status and EOL, decides what to keep, checks `global.json` pins, and prints `sudo` commands for you to run. Never removes anything itself. |

## Verifying a change

```bash
./scripts/validate.py            # frontmatter, layout, packaging hazards
python3 tests/*/test-*.py        # decision logic
apm audit --file skills/<name>/SKILL.md   # APM's own security scan
npx skills add . --list        # confirm discovery finds each skill exactly once
```

CI runs all of these on every push. `validate.py` also checks the two
`.claude-plugin` manifests, so `claude plugin validate` should never be the
thing that tells you a manifest is broken.

## License

[MIT](LICENSE)