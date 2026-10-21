# Changelog

Collection releases, newest first. The collection version is `apm.yml` and
`.claude-plugin/plugin.json`; each skill also has its own version in its
`SKILL.md` (`metadata.version`). Format: one line per changed skill. See
`AGENTS.md` for the versioning rules.

## 0.3.0

- `azure-devops-pullrequests` 0.1.0: new. Create PRs and link work items;
  list, show, read open review comments and build/policy checks; edit title,
  description, or draft state. Writes are dry-run unless `--yes`.
- Versioning: skills now carry independent versions; `validate.py` requires
  only that the two manifests match and no skill is newer than the collection.

## 0.2.0

- `azure-devops-workitems` 0.2.0: `--fields`, `--prs`, `--tree`, `--branch`,
  `--mine`, and a setup guide.
- `dotnet-versions` 0.2.0: released with the collection.

## 0.1.0

- `dotnet-versions` 0.1.0: initial release.
