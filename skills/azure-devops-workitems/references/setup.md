# Setup

One-time, done by the user. The agent must not create or handle the token.

## 1. Create a Personal Access Token

Azure DevOps > User settings > Personal access tokens > New token.

| Scope | Needed for |
| ----- | ---------- |
| Work Items: Read | everything except `--prs` |
| Code: Read | `--prs` (resolving PR, commit and branch details) |

Use the shortest expiry you can live with. Read-only scopes only.

## 2. Create the config file

`~/.config/ado.env`, then `chmod 600 ~/.config/ado.env`:

```
ADO_ORG=yourorg
ADO_PROJECT=Your Project
ADO_PAT=paste-token-here
```

- `ADO_ORG` is the name in `https://dev.azure.com/<org>`.
- `ADO_PROJECT` may contain spaces or be URL-encoded (`Your%20Project`).
- Environment variables override the file. `ADO_CONFIG=/path/to/file` points at
  a different file.

## 3. Verify

```bash
python3 scripts/ado.py --check      # prints: ok: <org>/<project>
python3 scripts/ado.py <id>
```

## Alternatives and extras

- **No PAT:** leave `ADO_PAT` unset and run `az login`. The script then uses
  the Azure CLI token.
- **Second organization:** add prefixed keys and pass `--profile`:

  ```
  ADO_CLIENT_ORG=otherorg
  ADO_CLIENT_PROJECT=Other
  ADO_CLIENT_PAT=...
  ```

  `python3 scripts/ado.py 42 --profile client`. Missing prefixed keys fall back
  to the unprefixed ones.
- **Pasted URL:** a `dev.azure.com/.../_workitems/edit/<id>` URL supplies org
  and project itself, so only credentials are needed.

## Troubleshooting

| Symptom | Cause |
| ------- | ----- |
| `ADO_ORG and ADO_PROJECT are not set` | config file missing or wrong path |
| HTTP 401 | token expired, wrong org, or (for `--prs`) no Code: Read |
| HTTP 403 | token lacks Work Items: Read |
| HTTP 404 | wrong id, project, or org |
| `no credentials` | no `ADO_PAT` and no `az login` session |
