#!/usr/bin/env python3
"""Create Azure DevOps pull requests and link work items, with compact output.

Usage:
  ado_pr.py create --title T [--body-file F|-] [--work-items 1,2] [--draft]
                   [--source BRANCH] [--target BRANCH] [--repo NAME] [--yes]
  ado_pr.py link <pr-id> --work-items 1,2 [--repo NAME] [--yes]
  ado_pr.py show <pr-id>                  # summary, reviewers, linked work items
  ado_pr.py list [--mine|--reviewing] [--status active|completed|abandoned|all] [--target B] [--top N]
  ado_pr.py comments <pr-id> [--all]      # open review threads (--all adds resolved)
  ado_pr.py checks <pr-id>                # build/policy status, failed build steps
  ado_pr.py update <pr-id> [--title T] [--body-file F|-] [--ready|--draft] [--yes]
  ado_pr.py --check

Anywhere <pr-id> is accepted, a pull request URL works too and supplies the
org, project and repo (no git remote or config needed except credentials).

Without --yes every write command (create, link, update) is a dry run: it
validates everything and prints what it would do, then sends nothing. Never
pushes, merges, completes, abandons, approves, or deletes anything.

Org, project and repo come from the git `origin` remote when it points at
Azure Repos; otherwise from ADO_ORG / ADO_PROJECT and --repo. Credentials:
ADO_PAT (env, ADO_CONFIG file, or ~/.config/ado.env), else `az login`.
Use --profile NAME to read ADO_<NAME>_ORG / _PROJECT / _PAT.
"""

from __future__ import annotations

import argparse
import base64
import json
import os
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "7.1"
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
MAX_BODY = 4000
HTTPS_RE = re.compile(r"https?://(?:[^@/]+@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/?#]+)")
PR_URL_RE = re.compile(r"https?://(?:[^@/]+@)?dev\.azure\.com/([^/]+)/([^/]+)/_git/([^/]+)/pullrequest/(\d+)")
SSH_RE = re.compile(r"(?:ssh://)?git@ssh\.dev\.azure\.com[:/]v3/([^/]+)/([^/]+)/([^/?#]+)")


class AdoError(Exception):
    pass


def parse_remote(url: str) -> dict[str, str] | None:
    """Extract org/project/repo from an Azure Repos remote URL."""
    m = HTTPS_RE.match(url.strip()) or SSH_RE.match(url.strip())
    if not m:
        return None
    org, project, repo = (urllib.parse.unquote(g) for g in m.groups())
    return {"org": org, "project": project, "repo": repo.removesuffix(".git")}


def parse_pr_ref(text: str) -> dict:
    """A PR id, or a pull request URL that also supplies org/project/repo."""
    if text.isdigit():
        return {"id": int(text)}
    m = PR_URL_RE.match(text.strip())
    if not m:
        raise argparse.ArgumentTypeError("expected a PR id or a dev.azure.com .../_git/<repo>/pullrequest/<id> URL")
    org, project, repo = (urllib.parse.unquote(g) for g in m.groups()[:3])
    return {"id": int(m[4]), "org": org, "project": project, "repo": repo}


def parse_ids(text: str) -> list[int]:
    ids: list[int] = []
    for part in re.split(r"[,\s]+", text.strip()):
        part = part.lstrip("#")
        if not part:
            continue
        if not part.isdigit():
            raise AdoError(f"not a work item id: {part!r}")
        if int(part) not in ids:
            ids.append(int(part))
    return ids


def short_ref(ref: str) -> str:
    return ref.removeprefix("refs/heads/")


def full_ref(branch: str) -> str:
    return branch if branch.startswith("refs/") else f"refs/heads/{branch}"


def artifact_url(project_id: str, repo_id: str, pr_id: int) -> str:
    return f"vstfs:///Git/PullRequestId/{project_id}%2f{repo_id}%2f{pr_id}"


def build_create_payload(source: str, target: str, title: str, body: str, work_items: list[int], draft: bool) -> dict:
    payload = {
        "sourceRefName": full_ref(source),
        "targetRefName": full_ref(target),
        "title": title,
        "description": body,
        "isDraft": draft,
    }
    if work_items:
        payload["workItemRefs"] = [{"id": str(i)} for i in work_items]
    return payload


def build_link_patch(project_id: str, repo_id: str, pr_id: int) -> list[dict]:
    return [{
        "op": "add",
        "path": "/relations/-",
        "value": {"rel": "ArtifactLink", "url": artifact_url(project_id, repo_id, pr_id),
                  "attributes": {"name": "Pull Request"}},
    }]


VOTES = {10: "approved", 5: "approved with suggestions", 0: "no vote", -5: "waiting for author", -10: "rejected"}
RESOLVED = {"fixed", "closed", "wontFix", "byDesign"}


def render_pr(pr: dict, work_items: list[str], url: str) -> str:
    flags = ", ".join(f for f in (pr.get("status"), "draft" if pr.get("isDraft") else "",
                                  f"merge: {pr['mergeStatus']}" if pr.get("mergeStatus") else "") if f)
    lines = [f"PR {pr['pullRequestId']}: {pr.get('title', '?')} [{flags}]",
             f"{short_ref(pr.get('sourceRefName', ''))} -> {short_ref(pr.get('targetRefName', ''))}"
             f"  by {pr.get('createdBy', {}).get('displayName', '?')}",
             url]
    revs = [f"{r.get('displayName', '?')}{' (required)' if r.get('isRequired') else ''}: {VOTES.get(r.get('vote', 0), r.get('vote'))}"
            for r in pr.get("reviewers", [])]
    if revs:
        lines.append("reviewers: " + "; ".join(revs))
    lines.append("work items: " + (", ".join(f"#{w}" for w in work_items) or "(none)"))
    if pr.get("description"):
        lines += ["", pr["description"].strip()]
    return "\n".join(lines)


def render_pr_line(pr: dict) -> str:
    draft = " draft" if pr.get("isDraft") else ""
    return (f"PR {pr['pullRequestId']} [{pr.get('status', '?')}{draft}] {pr.get('title', '?')} "
            f"({short_ref(pr.get('sourceRefName', ''))} -> {short_ref(pr.get('targetRefName', ''))}, "
            f"{pr.get('createdBy', {}).get('displayName', '?')})")


def render_threads(threads: list[dict], include_resolved: bool = False) -> str:
    out = []
    for t in threads:
        comments = [c for c in t.get("comments", []) if c.get("commentType") != "system" and not c.get("isDeleted")]
        status = t.get("status", "active")
        if not comments or (status in RESOLVED and not include_resolved):
            continue
        ctx = t.get("threadContext") or {}
        line = (ctx.get("rightFileStart") or ctx.get("leftFileStart") or {}).get("line")
        where = f"{ctx['filePath']}{f':{line}' if line else ''}" if ctx.get("filePath") else "(general)"
        out.append(f"[thread {t['id']}] {status} {where}")
        out += [f"  {c.get('author', {}).get('displayName', '?')}: {' '.join((c.get('content') or '').split())}" for c in comments]
    return "\n".join(out) or "(no open threads)"


def render_checks(evals: list[dict], builds: dict[int, dict], failures: dict[int, list[str]]) -> str:
    """One line per applicable policy; failing build policies get the build result and failed steps."""
    lines = []
    for e in evals:
        status = e.get("status", "?")
        if status == "notApplicable":
            continue
        cfg = e.get("configuration") or {}
        name = (cfg.get("type") or {}).get("displayName", "?")
        line = f"{status}{' [blocking]' if cfg.get('isBlocking') else ''} {name}"
        bid = (e.get("context") or {}).get("buildId")
        if bid and bid in builds:
            b = builds[bid]
            line += f" - {(b.get('definition') or {}).get('name', '?')} #{b.get('buildNumber', bid)}: {b.get('result') or b.get('status', '?')}"
            if failures.get(bid):
                line += "; failed: " + ", ".join(failures[bid][:5])
        lines.append(line)
    return "\n".join(lines) or "(no policies)"


def build_update_patch(title: str | None, body: str | None, draft: bool | None) -> dict:
    patch: dict = {}
    if title is not None:
        patch["title"] = title
    if body is not None:
        patch["description"] = body
    if draft is not None:
        patch["isDraft"] = draft
    return patch


def already_linked(item: dict, art_url: str) -> bool:
    return any((r.get("url") or "").lower() == art_url.lower() for r in item.get("relations") or [])


def load_env_file(path: Path) -> dict[str, str]:
    out: dict[str, str] = {}
    if not path.is_file():
        return out
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip().strip("'\"")
    return out


def resolve_config(profile: str | None, environ=None, file_values=None) -> dict[str, str]:
    environ = os.environ if environ is None else environ
    if file_values is None:
        file_values = load_env_file(Path(environ.get("ADO_CONFIG", Path.home() / ".config" / "ado.env")))
    merged = {**file_values, **{k: v for k, v in environ.items() if k.startswith("ADO_")}}
    cfg = {}
    for key in ("ORG", "PROJECT", "PAT"):
        names = ([f"ADO_{profile.upper()}_{key}"] if profile else []) + [f"ADO_{key}"]
        cfg[key.lower()] = next((merged[n] for n in names if merged.get(n)), "")
    cfg["project"] = urllib.parse.unquote(cfg["project"])
    return cfg


def auth_header(pat: str) -> str:
    if pat:
        return "Basic " + base64.b64encode(f":{pat}".encode()).decode()
    try:
        token = subprocess.run(
            ["az", "account", "get-access-token", "--resource", ADO_RESOURCE, "--query", "accessToken", "-o", "tsv"],
            capture_output=True, text=True, timeout=30, check=True,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        raise AdoError("no credentials: set ADO_PAT (Code + Work Items: Read & write) or run `az login`")
    return "Bearer " + token


class Client:
    def __init__(self, org: str, project: str, pat: str):
        self.org, self.project = org, project
        self.root = f"https://dev.azure.com/{urllib.parse.quote(org)}/{urllib.parse.quote(project)}"
        self.auth = auth_header(pat)

    def _send(self, method: str, path: str, body=None, content_type: str = "application/json"):
        return self._send_url(method, f"{self.root}/_apis/{path}", body, content_type)

    def _send_url(self, method: str, url: str, body=None, content_type: str = "application/json"):
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", self.auth)
        req.add_header("Accept", "application/json")
        if data is not None:
            req.add_header("Content-Type", content_type)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            hint = {401: "token rejected or expired",
                    403: "token lacks scope (Code and Work Items; Read & write for create/link/update)",
                    404: "repo, branch, project or org not found"}.get(e.code, "")
            detail = ""
            try:
                detail = json.loads(e.read().decode()).get("message", "")
            except (ValueError, OSError):
                pass
            raise AdoError(f"HTTP {e.code} for {method} {url.split('?')[0]}: {detail or hint}".rstrip(": "))
        except urllib.error.URLError as e:
            raise AdoError(f"network error: {e.reason}")

    def get(self, path: str):
        return self._send("GET", path)

    def repo(self, name: str) -> dict:
        return self.get(f"git/repositories/{urllib.parse.quote(name)}?api-version={API}")

    def branch_exists(self, repo_id: str, branch: str) -> bool:
        q = urllib.parse.quote(f"heads/{short_ref(branch)}")
        refs = self.get(f"git/repositories/{repo_id}/refs?filter={q}&api-version={API}").get("value", [])
        return any(r.get("name") == full_ref(branch) for r in refs)

    def active_prs(self, repo_id: str, source: str, target: str) -> list[dict]:
        q = (f"searchCriteria.status=active&searchCriteria.sourceRefName={urllib.parse.quote(full_ref(source))}"
             f"&searchCriteria.targetRefName={urllib.parse.quote(full_ref(target))}")
        return self.get(f"git/repositories/{repo_id}/pullrequests?{q}&api-version={API}").get("value", [])

    def create_pr(self, repo_id: str, payload: dict) -> dict:
        return self._send("POST", f"git/repositories/{repo_id}/pullrequests?api-version={API}", payload)

    def pr(self, repo_id: str, pr_id: int) -> dict:
        return self.get(f"git/repositories/{repo_id}/pullrequests/{pr_id}?api-version={API}")

    def work_item(self, wid: int) -> dict:
        return self.get(f"wit/workitems/{wid}?$expand=relations&api-version={API}")

    def link_work_item(self, wid: int, patch: list[dict]):
        return self._send("PATCH", f"wit/workitems/{wid}?api-version={API}", patch, "application/json-patch+json")

    def me(self) -> str:
        url = f"https://dev.azure.com/{urllib.parse.quote(self.org)}/_apis/connectionData?api-version={API}-preview"
        return self._send_url("GET", url)["authenticatedUser"]["id"]

    def list_prs(self, repo_id: str, status: str, top: int, creator=None, reviewer=None, target=None) -> list[dict]:
        q = {"searchCriteria.status": status, "$top": str(top), "api-version": API}
        if creator:
            q["searchCriteria.creatorId"] = creator
        if reviewer:
            q["searchCriteria.reviewerId"] = reviewer
        if target:
            q["searchCriteria.targetRefName"] = full_ref(target)
        return self.get(f"git/repositories/{repo_id}/pullrequests?{urllib.parse.urlencode(q)}").get("value", [])

    def pr_work_items(self, repo_id: str, pr_id: int) -> list[str]:
        return [w["id"] for w in self.get(f"git/repositories/{repo_id}/pullrequests/{pr_id}/workitems?api-version={API}").get("value", [])]

    def threads(self, repo_id: str, pr_id: int) -> list[dict]:
        return self.get(f"git/repositories/{repo_id}/pullrequests/{pr_id}/threads?api-version={API}").get("value", [])

    def policy_evaluations(self, project_id: str, pr_id: int) -> list[dict]:
        art = urllib.parse.quote(f"vstfs:///CodeReview/CodeReviewId/{project_id}/{pr_id}", safe="")
        return self.get(f"policy/evaluations?artifactId={art}&api-version={API}-preview.1").get("value", [])

    def build(self, build_id: int) -> dict:
        return self.get(f"build/builds/{build_id}?api-version={API}")

    def failed_steps(self, build_id: int) -> list[str]:
        records = self.get(f"build/builds/{build_id}/timeline?api-version={API}").get("records", [])
        return [r.get("name", "?") for r in records if r.get("type") == "Task" and r.get("result") == "failed"]

    def update_pr(self, repo_id: str, pr_id: int, patch: dict) -> dict:
        return self._send("PATCH", f"git/repositories/{repo_id}/pullrequests/{pr_id}?api-version={API}", patch)

    def web_url(self, repo_name: str, pr_id: int) -> str:
        return f"{self.root}/_git/{urllib.parse.quote(repo_name)}/pullrequest/{pr_id}"


def git(*args: str) -> str:
    try:
        return subprocess.run(["git", *args], capture_output=True, text=True, check=True, timeout=15).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""


def read_body(spec: str | None) -> str:
    if not spec:
        return ""
    try:
        body = sys.stdin.read() if spec == "-" else Path(spec).read_text()
    except OSError as e:
        raise AdoError(f"cannot read description: {e}")
    body = body.strip()
    if len(body) > MAX_BODY:
        raise AdoError(f"description is {len(body)} chars; Azure DevOps allows {MAX_BODY}. Shorten it.")
    return body


def locate(args) -> tuple[Client, str]:
    """Resolve org/project/repo (git remote first, then config) and build a client."""
    cfg = resolve_config(args.profile)
    remote = getattr(args, "pr_ref", None) or parse_remote(git("remote", "get-url", "origin")) or {}
    org = remote.get("org") or cfg["org"]
    project = remote.get("project") or cfg["project"]
    repo = args.repo or remote.get("repo") or ""
    if not org or not project:
        raise AdoError("org/project unknown: use an Azure Repos `origin` remote or set ADO_ORG and ADO_PROJECT")
    return Client(org, project, cfg["pat"]), repo


def need_repo(repo_name: str) -> str:
    if not repo_name:
        raise AdoError("repo unknown: pass --repo or run inside a clone with an Azure Repos `origin` remote")
    return repo_name


def cmd_create(args) -> int:
    client, repo_name = locate(args)
    repo_name = need_repo(repo_name)
    source = short_ref(args.source or git("rev-parse", "--abbrev-ref", "HEAD"))
    if not source or source == "HEAD":
        raise AdoError("source branch unknown (detached HEAD): pass --source")
    ids = parse_ids(args.work_items or "")
    body = read_body(args.body_file)
    repo = client.repo(repo_name)
    target = short_ref(args.target or repo.get("defaultBranch") or "")
    if not target:
        raise AdoError("target branch unknown: pass --target")
    if source == target:
        raise AdoError(f"source and target are both {source!r}")
    if not client.branch_exists(repo["id"], source):
        raise AdoError(f"branch {source!r} is not on the remote. Ask the user to push it; this skill never pushes.")
    existing = client.active_prs(repo["id"], source, target)
    if existing:
        pid = existing[0]["pullRequestId"]
        print(f"exists: PR {pid} already active for {source} -> {target}: {client.web_url(repo['name'], pid)}")
        print("not created; use `link` to attach work items to it")
        return 0
    for wid in ids:
        client.work_item(wid)  # fail on a wrong id before creating anything
    print(f"{'create' if args.yes else 'dry-run'}: {repo['name']}  {source} -> {target}{'  (draft)' if args.draft else ''}")
    print(f"title: {args.title}")
    print(f"work items: {', '.join(f'#{i}' for i in ids) or '(none)'}")
    print(f"description: {len(body)} chars")
    if not args.yes:
        print("nothing sent; re-run with --yes after the user confirms")
        return 0
    pr = client.create_pr(repo["id"], build_create_payload(source, target, args.title, body, ids, args.draft))
    print(f"created: PR {pr['pullRequestId']} {client.web_url(repo['name'], pr['pullRequestId'])}")
    return 0


def cmd_link(args) -> int:
    client, repo_name = locate(args)
    repo_name = need_repo(repo_name)
    ids = parse_ids(args.work_items or "")
    if not ids:
        raise AdoError("--work-items is required")
    repo = client.repo(repo_name)
    pr = client.pr(repo["id"], args.pr_id)
    art = artifact_url(repo["project"]["id"], repo["id"], args.pr_id)
    print(f"PR {args.pr_id}: {pr.get('title', '?')} [{pr.get('status', '?')}]")
    for wid in ids:
        if already_linked(client.work_item(wid), art):
            print(f"#{wid}: already linked")
        elif not args.yes:
            print(f"#{wid}: would link (dry-run; re-run with --yes after the user confirms)")
        else:
            client.link_work_item(wid, build_link_patch(repo["project"]["id"], repo["id"], args.pr_id))
            print(f"#{wid}: linked")
    return 0


def open_pr(args):
    client, repo_name = locate(args)
    repo = client.repo(need_repo(repo_name))
    return client, repo, client.pr(repo["id"], args.pr_id)


def cmd_show(args) -> int:
    client, repo, pr = open_pr(args)
    print(render_pr(pr, client.pr_work_items(repo["id"], args.pr_id), client.web_url(repo["name"], args.pr_id)))
    return 0


def cmd_list(args) -> int:
    client, repo_name = locate(args)
    repo = client.repo(need_repo(repo_name))
    me = client.me() if (args.mine or args.reviewing) else None
    prs = client.list_prs(repo["id"], args.status, args.top, creator=me if args.mine else None,
                          reviewer=me if args.reviewing else None, target=args.target)
    print("\n".join(render_pr_line(p) for p in prs) or "(no pull requests)")
    return 0


def cmd_checks(args) -> int:
    client, repo, _ = open_pr(args)
    evals = client.policy_evaluations(repo["project"]["id"], args.pr_id)
    builds: dict[int, dict] = {}
    failures: dict[int, list[str]] = {}
    no_build_scope = False
    for e in evals:
        bid = (e.get("context") or {}).get("buildId")
        if bid and e.get("status") != "notApplicable" and bid not in builds:
            try:
                builds[bid] = client.build(bid)
                if builds[bid].get("result") in ("failed", "partiallySucceeded"):
                    failures[bid] = client.failed_steps(bid)
            except AdoError as err:
                if "HTTP 401" not in str(err) and "HTTP 403" not in str(err):
                    raise
                no_build_scope = True
    print(render_checks(evals, builds, failures))
    if no_build_scope:
        print("note: build details unavailable; the token needs the Build (Read) scope")
    return 0


def cmd_comments(args) -> int:
    client, repo, _ = open_pr(args)
    print(render_threads(client.threads(repo["id"], args.pr_id), args.all))
    return 0


def cmd_update(args) -> int:
    client, repo, pr = open_pr(args)
    if pr.get("status") != "active":
        raise AdoError(f"PR {args.pr_id} is {pr.get('status')}; only active PRs can be updated")
    draft = True if args.draft else False if args.ready else None
    patch = build_update_patch(args.title, read_body(args.body_file) if args.body_file else None, draft)
    if not patch:
        raise AdoError("nothing to update: give --title, --body-file, --ready or --draft")
    print(f"{'update' if args.yes else 'dry-run'}: PR {args.pr_id} {pr.get('title', '?')}")
    for k, v in patch.items():
        print(f"  {k}: {v if k != 'description' else f'{len(v)} chars'}")
    if not args.yes:
        print("nothing sent; re-run with --yes after the user confirms")
        return 0
    client.update_pr(repo["id"], args.pr_id, patch)
    print(f"updated: {client.web_url(repo['name'], args.pr_id)}")
    return 0


def cmd_check(args) -> int:
    client, repo_name = locate(args)
    repo = client.repo(repo_name) if repo_name else None
    extra = f"/{repo['name']} (default {short_ref(repo.get('defaultBranch', ''))})" if repo else ""
    print(f"ok: {client.org}/{client.project}{extra}")
    return 0


def main(argv=None) -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--profile", default=argparse.SUPPRESS)
    common.add_argument("--repo", default=argparse.SUPPRESS, help="repository name (default: git origin remote)")
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter, parents=[common])
    p.set_defaults(profile=None, repo=None)
    p.add_argument("--check", action="store_true", help="verify config, credentials and repo access")
    sub = p.add_subparsers(dest="cmd")
    c = sub.add_parser("create", parents=[common], help="create a pull request")
    c.add_argument("--title", required=True)
    c.add_argument("--body-file", help="description file, or - for stdin")
    c.add_argument("--work-items", help="comma-separated ids to link")
    c.add_argument("--source", help="default: current branch")
    c.add_argument("--target", help="default: repository default branch")
    c.add_argument("--draft", action="store_true")
    c.add_argument("--yes", action="store_true", help="actually create (default is a dry run)")
    k = sub.add_parser("link", parents=[common], help="link work items to an existing PR")
    k.add_argument("pr_id", type=parse_pr_ref, metavar="pr-id|url")
    k.add_argument("--work-items", required=True)
    k.add_argument("--yes", action="store_true", help="actually link (default is a dry run)")
    for name, helptext in (("show", "summary, reviewers, linked work items"), ("comments", "open review threads"),
                           ("checks", "build and policy status")):
        sp = sub.add_parser(name, parents=[common], help=helptext)
        sp.add_argument("pr_id", type=parse_pr_ref, metavar="pr-id|url")
    sub.choices["comments"].add_argument("--all", action="store_true", help="include resolved threads")
    ls = sub.add_parser("list", parents=[common], help="list pull requests, one line each")
    ls.add_argument("--mine", action="store_true", help="created by me")
    ls.add_argument("--reviewing", action="store_true", help="I am a reviewer")
    ls.add_argument("--status", default="active", choices=["active", "completed", "abandoned", "all"])
    ls.add_argument("--target", help="target branch filter")
    ls.add_argument("--top", type=int, default=20)
    u = sub.add_parser("update", parents=[common], help="edit title/description or draft state")
    u.add_argument("pr_id", type=parse_pr_ref, metavar="pr-id|url")
    u.add_argument("--title")
    u.add_argument("--body-file", help="new description file, or - for stdin")
    state = u.add_mutually_exclusive_group()
    state.add_argument("--ready", action="store_true", help="publish a draft")
    state.add_argument("--draft", action="store_true", help="convert to draft")
    u.add_argument("--yes", action="store_true", help="actually update (default is a dry run)")
    args = p.parse_args(argv)
    if isinstance(getattr(args, "pr_id", None), dict):
        args.pr_ref = args.pr_id
        args.pr_id = args.pr_ref["id"]
    if not args.check and not args.cmd:
        p.error("give a command (create, link, show, list, comments, checks, update) or --check")
    try:
        if args.check:
            return cmd_check(args)
        return {"create": cmd_create, "link": cmd_link, "show": cmd_show, "list": cmd_list,
                "comments": cmd_comments, "checks": cmd_checks, "update": cmd_update}[args.cmd](args)
    except AdoError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
