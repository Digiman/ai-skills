# Working on this repo

Repo of custom [Agent Skills](https://agentskills.io/specification). One
directory per skill under `skills/`, distributed three ways: the `skills` CLI
(`npx skills add Digiman/ai-skills`), [APM](https://microsoft.github.io/apm/)
(`apm install Digiman/ai-skills`), and as a Claude Code plugin
(`/plugin install digiman-skills@digiman`).

Two distinct identities, deliberately: the **repo** is `Digiman/ai-skills`, and
the **collection** the manifests publish is `digiman-skills`. Do not collapse
them. `Digiman/ai-skills` is the source path; `digiman-skills` is the install
identifier and the namespace plugin skills appear under.

## Layout

- `skills/<name>/SKILL.md` — required. YAML frontmatter, then the instructions.
- `skills/<name>/scripts/` — executable code the agent runs.
- `skills/<name>/references/` — docs the agent reads on demand.
- `skills/<name>/assets/` — templates and data.
- `tests/<name>/test-*.py` — tests for the decision logic. Repo root, not in
  the skill: see "What ships" below.
- `scripts/install.sh` — links each skill into `~/.agents/skills`.
- `scripts/validate.py` — spec, packaging, and manifest checks.

Do not create `.agents/skills/` here. `npx skills` scans both `skills/` and
`.agents/skills/`, so a symlink between them doubles the discovery surface,
and APM deploys *to* `.agents/skills/`, so a symlink there makes its own
deploy a silent no-op. `./scripts/install.sh` covers the local machine.

## What ships

Both package managers copy a skill folder **whole**. Anything under
`skills/<name>/` lands in every consumer's harness directory. So:

- Tests, fixtures, and repo tooling stay at the repo root. `validate.py`
  fails if a `tests/` directory appears inside a skill folder.
- No symlinks inside a skill folder. APM rejects a bundle containing any
  symlink, and filters them at deploy time. `validate.py` fails on one.
- Frontmatter stays ASCII. APM refuses to deploy non-ASCII frontmatter, which
  would block install for every consumer.

These are errors rather than style nits: each one fails on someone else's
machine, not yours.

## Frontmatter contract

`name` and `description` are required. Everything else is optional.

- `name` — 1-64 chars, `a-z` `0-9` and hyphens, no leading, trailing, or
  consecutive hyphens. Must equal the directory name. Must start with
  `digiman-`. See "Names are the install identity".
- `description` — 1-1024 chars. Carries the entire burden of triggering, so
  name the trigger conditions and the keywords a user would actually type, not
  just the capability.
- `compatibility` — only when the skill has real environment requirements.
- `metadata` — string-to-string map. Keep a `version` here.

## Names are the install identity

Every skill from every source installs into one flat directory per agent
(`~/.agents/skills/<name>/`). There is no scoping. Two authors who both ship a
`code-review` cannot coexist — the second install wins.

`npx skills` overwrites silently. I confirmed this: installing a
`code-review` over an unrelated one replaced it with no prompt and no warning.
APM is safer — it refuses and asks for `--force` — but neither tool prevents it.

**This repo's answer is the Claude plugin path, not a name prefix.** Skill
names stay plain (`dotnet-versions`), and the collection carries the identity
(`digiman-skills`). Plugin skills are namespaced as
`/digiman-skills:dotnet-versions` and live in the plugin cache rather than
`~/.claude/skills/`, so they cannot collide with a flat install at all.

The spec has no namespace mechanism. RFC
[agentskills#312](https://github.com/agentskills/agentskills/issues/312)
proposed `collection/skill-name`, closed without landing, and `name` still
permits only `a-z`, `0-9` and hyphens. The plugin namespace is a Claude Code
convention layered on top, not a spec feature — so on the `npx skills` and APM
paths there is nothing protecting a generic name. If you add a skill whose name
is common (`code-review`, `tdd`, `research`), say so in its PR; renaming later
is a breaking change for anyone who installed the old name.

## Three manifests, one skill tree

`skills/<name>/SKILL.md` is the only thing that must exist. The manifests are
thin wrappers over the same tree, and each needs a different piece of metadata:

- `apm.yml` — APM package identity. Only `name` and `version` are required.
- `.claude-plugin/plugin.json` — Claude Code plugin. Only `name` is required.
- `.claude-plugin/marketplace.json` — the marketplace index, required for
  `/plugin marketplace add` to work.

Set `name: digiman-skills` in the first two and `name: digiman` in the
marketplace, so the commands read `/plugin marketplace add Digiman/ai-skills`
then `/plugin install digiman-skills@digiman`.

Two traps `validate.py` guards:

- A marketplace entry with `source: "./"` **disables** the default `skills/`
  scan, so it must declare `skills: ["./skills/"]` or the plugin loads empty.
- `plugin.json` `name` must not start with `claude-`, `anthropic-`,
  `anthropics-`, or `cc-plugin-`, and must not be one of Anthropic's own
  reserved names.

## Versioning

Semver, two levels:

- **Collection** — `version` in `apm.yml` and `.claude-plugin/plugin.json`,
  always equal. This is the release number consumers install. Bump it on every
  release to the highest bump among the skills that changed; a new skill is a
  minor bump.
- **Skill** — `metadata.version` in each `SKILL.md`, bumped only when that
  skill changes. Patch for fixes and docs, minor for a new command or flag,
  major for a breaking change to output, flags, or the safety contract. A
  rename is breaking: the name is the install identity.

`validate.py` enforces that the two manifests match and that no skill is newer
than the collection. Record every release in `CHANGELOG.md`, one line per
changed skill.

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

**No hardcoded facts that go stale.** Version numbers, prices, and API
responses belong to a fetch, not to the file. A skill that says "latest is
10.0.401" is wrong within a month. Put volatile data behind a script, cache
it, and degrade to "unknown" rather than guessing when the fetch fails.

**Test the decision logic, not the formatting.** A machine where the skill has
nothing to do will never exercise the interesting paths, so unit-test them
against synthetic inputs. `python3 tests/<name>/test-*.py`.

**Keep `SKILL.md` under 500 lines.** Push detail into `references/` and let the
agent load it only when it matters.

**Prefer one level of reference depth.** `SKILL.md` → `references/foo.md`.
No chains.

## Verifying a change

```bash
./scripts/validate.py          # frontmatter, layout, manifests
python3 tests/*/test-*.py      # decision logic
apm audit --file skills/<name>/SKILL.md   # APM's own security scan
npx skills add . --list        # confirm discovery finds each skill exactly once
```

`validate.py` covers the `.claude-plugin` manifests, so a broken manifest fails
here rather than in `claude plugin validate`. Claude Code is not required for
that.

Then run the skill end to end and read its real output. A skill whose script
crashes is worse than no skill, because the agent trusts the description and
loads the body before finding out.
