# Setup

One-time, done by the user. The agent must not create or handle the token.

## 1. Create a Personal Access Token

Azure DevOps > User settings > Personal access tokens > New token.

| Scope | Needed for |
| ----- | ---------- |
| Code: Read | `show`, `list`, `comments`, `checks` |
| Build: Read | `checks` (build result and failed steps) |
| Code: Read & write | `create`, `update` |
| Work Items: Read & write | linking a work item (`link`, or `--work-items` on `create`) |

Only the read commands work with a read-only token. Creating, updating, or
linking needs a **write** token, unlike the one for `azure-devops-workitems`.
Keep the expiry short. Use a separate profile (below) if you want to keep the
read-only token for reading.

## 2. Config

Same file as `azure-devops-workitems`: `~/.config/ado.env`, `chmod 600`.

```
ADO_ORG=yourorg
ADO_PROJECT=Your Project
ADO_PAT=paste-token-here
```

For a separate write token, add a profile and pass `--profile pr`:

```
ADO_PR_PAT=paste-write-token-here
```

Missing prefixed keys fall back to the unprefixed ones. With an Azure Repos
`origin` remote, org, project and repo are taken from the remote, so only the
PAT is required. With no PAT set, `az login` is used.

## 3. Verify

```bash
python3 scripts/ado_pr.py --check     # prints: ok: <org>/<project>/<repo> (default <branch>)
```

`--check` only reads, so it passes with a read-only token. A write failure
(HTTP 403) appears only on `--yes`.

## Troubleshooting

| Symptom | Cause |
| ------- | ----- |
| `org/project unknown` | `origin` is not an Azure Repos URL and `ADO_ORG`/`ADO_PROJECT` are unset |
| `repo unknown` | pass `--repo NAME` |
| `branch ... is not on the remote` | push the branch first |
| HTTP 403 on `--yes` | token lacks Code or Work Items write scope |
| work item not found (404) | wrong id, or it is in another project |
