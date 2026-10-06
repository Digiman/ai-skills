# Working on this repo

Repo of custom [Agent Skills](https://agentskills.io/specification). One
directory per skill under `skills/`, distributed with either the `skills` CLI
(`npx skills add owner/repo`) or [APM](https://microsoft.github.io/apm/)
(`apm install owner/repo`).

## Layout

- `skills/<name>/SKILL.md` — required. YAML frontmatter, then the instructions.
- `skills/<name>/scripts/` — executable code the agent runs.
- `skills/<name>/references/` — docs the agent reads on demand.
- `skills/<name>/assets/` — templates and data.
- `tests/<name>/test-*.py` — tests for the decision logic. Repo root, not in
  the skill: see "What ships" below.
- `scripts/install.sh` — links each skill into `~/.agents/skills`.
- `scripts/validate.py` — spec and packaging checks.
- `apm.yml` — the APM manifest. Keep its `version` in step with
  `metadata.version` in each `SKILL.md`.

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

So every skill in this repo is named `digiman-<thing>`. `dotnet-versions`
became `digiman-dotnet-versions`. The directory name and the `name:` field both
carry the prefix, since the directory name *is* the install target.

The spec has no namespace mechanism. RFC
[agentskills#312](https://github.com/agentskills/agentskills/issues/312)
proposed `collection/skill-name`, closed without landing, and `name` still
permits only `a-z`, `0-9` and hyphens. Prefixing with hyphens is the convention
that stays inside the spec — the same one
[zapier/agent-skills](https://github.com/zapier/agent-skills/blob/main/AGENTS.md)
uses.

`validate.py` fails a skill whose name is not prefixed.

**Renaming is a breaking change.** Anyone who installed the old name keeps a
stale directory that nothing will overwrite or clean up.

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
./scripts/validate.py          # frontmatter + layout checks for every skill
python3 tests/*/test-*.py      # decision logic
apm audit --file skills/<name>/SKILL.md   # APM's own security scan
npx skills add . --list        # confirm discovery finds each skill exactly once
```

Then run the skill end to end and read its real output. A skill whose script
crashes is worse than no skill, because the agent trusts the description and
loads the body before finding out.
