#!/usr/bin/env python3
"""Azure DevOps work item reader and commenter that prints compact, token-cheap text.

Usage:
  ado.py <id|work-item-url> [--links] [--children] [--no-comments] [--profile NAME]
  ado.py <id> --fields Priority,AreaPath  # only these fields (short or full names); "all" lists every non-empty one
  ado.py <id> --prs                       # linked PRs, commits, branches (resolved)
  ado.py <id> --tree                      # whole hierarchy, titles only, this item marked
  ado.py <id> --branch [--prefix feature] # suggested branch name + PR title/body
  ado.py <id> --create-branch [--repo DIR]  # create and check out that branch locally
  ado.py --mine [--sprint]                # my open items (optionally current sprint)
  ado.py --query "<WIQL>" [--top N]      # list ids + titles only
  ado.py --check                          # verify config/auth without fetching

Writes (need a PAT with the Work Items: Read & write scope):
  ado.py <id> --comment "text" [--comment-format html|markdown]
  ado.py <id> --comment-file notes.md     # - reads stdin
  ado.py <id> --set State=Active --set "Title=New title"
  ado.py <id> --add-tag backend --add-tag needs-review
  ado.py <id> --set AssignedTo=@me
  ... plus --yes                           # without it every write is a dry run

Config (first match wins): environment, then the file named by ADO_CONFIG,
then ~/.config/ado.env (KEY=value lines). Keys: ADO_ORG, ADO_PROJECT, ADO_PAT.
With --profile NAME the keys are read as ADO_<NAME>_ORG, etc., falling back to
the unprefixed ones. If no PAT is set, an `az login` session is used.

Reads send GET requests (plus the read-only WIQL POST). Writes send a PATCH to
the work item and a POST to its comments, and only when --yes is given.
--create-branch runs git checkout -b in a local clone and never pushes.
"""

from __future__ import annotations

import argparse
import base64
import html
import json
import os
import re
import subprocess
import unicodedata
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

API = "7.1"
COMMENTS_API = "7.1-preview.4"
ADO_RESOURCE = "499b84ac-1321-427f-aa17-267ca6975798"
URL_RE = re.compile(r"https?://dev\.azure\.com/([^/]+)/([^/]+)/_workitems/edit/(\d+)")
FIELDS = {
    "desc": "System.Description",
    "ac": "Microsoft.VSTS.Common.AcceptanceCriteria",
    "repro": "Microsoft.VSTS.TCM.ReproSteps",
}
# Fields whose value is rich text: Azure DevOps renders HTML there, not Markdown.
HTML_FIELDS = {
    "System.Description",
    "Microsoft.VSTS.Common.AcceptanceCriteria",
    "Microsoft.VSTS.TCM.ReproSteps",
}
# Fields that hold a person; "@me" in a --set value means the authenticated user.
IDENTITY_FIELDS = {"System.AssignedTo"}
# Markup that counts as deliberate HTML; anything else is escaped.
HTML_TAG_RE = re.compile(
    r"</?(div|p|br|b|i|em|strong|u|s|ul|ol|li|a|code|pre|h[1-6]|table|thead|tbody|tr|td|th|span|hr|img|blockquote)\b[^>]*>",
    re.I)
# Short names for --set. Anything not listed here is matched against the
# fields the item actually has, so custom fields work by their own name.
FIELD_ALIASES = {
    "title": "System.Title",
    "state": "System.State",
    "reason": "System.Reason",
    "description": "System.Description",
    "tags": "System.Tags",
    "assignedto": "System.AssignedTo",
    "areapath": "System.AreaPath",
    "iterationpath": "System.IterationPath",
    "type": "System.WorkItemType",
    "acceptancecriteria": "Microsoft.VSTS.Common.AcceptanceCriteria",
    "reprosteps": "Microsoft.VSTS.TCM.ReproSteps",
    "priority": "Microsoft.VSTS.Common.Priority",
    "severity": "Microsoft.VSTS.Common.Severity",
    "storypoints": "Microsoft.VSTS.Scheduling.StoryPoints",
    "remainingwork": "Microsoft.VSTS.Scheduling.RemainingWork",
}


class AdoError(Exception):
    pass


def clean(text: str | None) -> str:
    """HTML to plain text, keeping paragraph/list line breaks."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</li>|</div>|</h\d>", "\n", text, flags=re.I)
    text = re.sub(r"<li[^>]*>", "- ", text, flags=re.I)
    text = re.sub(r"<[^>]+>", "", text)
    text = html.unescape(text).replace("\xa0", " ")
    text = re.sub(r"[ \t]+\n", "\n", text)
    return re.sub(r"\n{2,}", "\n", text).strip()


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
        file_values = load_env_file(Path(os.environ.get("ADO_CONFIG", Path.home() / ".config" / "ado.env")))
    merged = {**file_values, **{k: v for k, v in environ.items() if k.startswith("ADO_")}}
    cfg = {}
    for key in ("ORG", "PROJECT", "PAT"):
        names = ([f"ADO_{profile.upper()}_{key}"] if profile else []) + [f"ADO_{key}"]
        cfg[key.lower()] = next((merged[n] for n in names if merged.get(n)), "")
    # Project may be stored URL-encoded (HSE%20PnG); normalise so it is encoded once.
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
        raise AdoError("no credentials: set ADO_PAT (Work Items: Read) or run `az login`")
    return "Bearer " + token


def error_detail(err: urllib.error.HTTPError) -> str:
    """Azure DevOps puts the useful part of a failure in the body: rule ids
    like TF401262 for a refused state transition, or the missing permission.
    An empty body means our own hint is better."""
    try:
        body = json.loads(err.read().decode())
    except (ValueError, OSError):
        return ""
    return str(body.get("message", "")) if isinstance(body, dict) else ""


class Client:
    def __init__(self, org: str, project: str, pat: str):
        self.org, self.project = org, project
        self.base = f"https://dev.azure.com/{urllib.parse.quote(org)}/{urllib.parse.quote(project)}/_apis"
        self.auth = auth_header(pat)

    def _send(self, req: urllib.request.Request):
        req.add_header("Authorization", self.auth)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            method = req.get_method()
            git = "/_apis/git/" in req.full_url
            # The WIQL POST is a read; only a change to a work item needs write scope.
            write = method in ("PATCH", "POST") and "wit/workitems" in req.full_url.lower()
            hint = {401: "token rejected, expired, or lacks Code (Read) scope" if git else "token rejected or expired",
                    403: ("token lacks Work Items (Read & write) scope" if write else "token lacks Work Items (Read) scope"),
                    404: "item, project or org not found (check ADO_ORG/ADO_PROJECT)"}.get(e.code, "")
            raise AdoError(f"HTTP {e.code} for {method} {req.full_url.split('?')[0]}: {error_detail(e) or hint}".rstrip(": "))
        except urllib.error.URLError as e:
            raise AdoError(f"network error: {e.reason}")

    def get(self, path: str):
        return self._send(urllib.request.Request(f"{self.base}/{path}"))

    def wiql(self, query: str, top: int):
        body = json.dumps({"query": query}).encode()
        req = urllib.request.Request(f"{self.base}/wit/wiql?$top={top}&api-version={API}", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        return self._send(req)

    def get_in(self, project: str, path: str):
        root = self.base.rsplit("/", 2)[0]
        return self._send(urllib.request.Request(f"{root}/{project}/_apis/{path}"))

    def items(self, ids: list[int]) -> list[dict]:
        out: list[dict] = []
        for i in range(0, len(ids), 200):
            chunk = ",".join(map(str, ids[i:i + 200]))
            out += self.get(f"wit/workitems?ids={chunk}&$expand=relations&api-version={API}").get("value", [])
        return out

    def item(self, wid: int):
        return self.get(f"wit/workitems/{wid}?$expand=relations&api-version={API}")

    def comments(self, wid: int):
        return self.get(f"wit/workItems/{wid}/comments?api-version={COMMENTS_API}").get("comments", [])

    def post_comment(self, wid: int, text: str, fmt: str = "html") -> dict:
        """Add a comment to the work item's discussion thread."""
        body = json.dumps({"text": text}).encode()
        req = urllib.request.Request(
            f"{self.base}/wit/workItems/{wid}/comments?format={fmt}&api-version={COMMENTS_API}",
            data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        return self._send(req)

    def update_item(self, wid: int, patch: list[dict], validate_only: bool = False) -> dict:
        """Patch work item fields. validate_only runs the server's rules without saving."""
        query = f"validateOnly={'true' if validate_only else 'false'}&api-version={API}"
        req = urllib.request.Request(f"{self.base}/wit/workitems/{wid}?{query}",
                                     data=json.dumps(patch).encode(), method="PATCH")
        req.add_header("Content-Type", "application/json-patch+json")
        return self._send(req)

    def me(self) -> dict:
        """The authenticated user's identity, for --set AssignedTo=@me."""
        url = f"https://dev.azure.com/{urllib.parse.quote(self.org)}/_apis/connectionData?api-version={API}-preview"
        req = urllib.request.Request(url)
        return self._send(req).get("authenticatedUser", {}).get("id", {})

    def titles(self, ids: list[int]) -> dict[int, dict]:
        if not ids:
            return {}
        fields = "System.Id,System.Title,System.WorkItemType,System.State"
        data = self.get(f"wit/workitems?ids={','.join(map(str, ids[:200]))}&fields={fields}&api-version={API}")
        return {v["id"]: v["fields"] for v in data.get("value", [])}


def relation_ids(item: dict) -> dict[str, list[int]]:
    """Group related work item ids by parent / children / related."""
    kinds = {"System.LinkTypes.Hierarchy-Reverse": "parent", "System.LinkTypes.Hierarchy-Forward": "children",
             "System.LinkTypes.Related": "related"}
    out: dict[str, list[int]] = {"parent": [], "children": [], "related": []}
    for rel in item.get("relations") or []:
        kind = kinds.get(rel.get("rel"))
        if kind and "/workItems/" in rel["url"]:
            out[kind].append(int(rel["url"].rsplit("/", 1)[1]))
    return out


ARTIFACT_RE = re.compile(r"^vstfs:///Git/(PullRequestId|Commit|Ref)/([^/]+)$")


def parse_artifact(rel: dict) -> dict | None:
    """Classify an ArtifactLink as an Azure Repos PR/commit/branch or a GitHub link."""
    if rel.get("rel") != "ArtifactLink":
        return None
    url = rel.get("url", "")
    name = (rel.get("attributes") or {}).get("name", "")
    m = ARTIFACT_RE.match(url)
    if m:
        parts = urllib.parse.unquote(m[2]).split("/")
        if len(parts) == 3:
            kind = {"PullRequestId": "pr", "Commit": "commit", "Ref": "branch"}[m[1]]
            return {"kind": kind, "project": parts[0], "repo": parts[1], "id": parts[2]}
    if url.startswith("https://github.com/"):
        return {"kind": "github", "name": name, "url": url}
    return None


def describe_artifact(client: "Client", art: dict) -> str:
    """One line per link; degrades to the raw id if the lookup fails."""
    try:
        if art["kind"] == "github":
            return f"{art['name'] or 'GitHub'}: {art['url']}"
        base = f"git/repositories/{art['repo']}"
        if art["kind"] == "pr":
            d = client.get_in(art["project"], f"{base}/pullRequests/{art['id']}?api-version={API}")
            src = d.get("sourceRefName", "").removeprefix("refs/heads/")
            dst = d.get("targetRefName", "").removeprefix("refs/heads/")
            return (f"PR !{d['pullRequestId']} [{d.get('status')}] {d['repository']['name']}: {d.get('title')} "
                    f"({src} -> {dst}, by {d['createdBy']['displayName']})")
        if art["kind"] == "commit":
            d = client.get_in(art["project"], f"{base}/commits/{art['id']}?api-version={API}")
            first = (d.get("comment") or "").splitlines()[0] if d.get("comment") else ""
            return f"commit {art['id'][:8]}: {first} ({d.get('author', {}).get('name', '?')})"
        branch = art["id"].removeprefix("GB")
        return f"branch {branch}"
    except AdoError as e:
        return f"{art['kind']} {art.get('id', art.get('url'))} (unresolved: {e})"


def resolve_field(fields: dict, name: str) -> str | None:
    """Map a short name (Priority) to a full reference name, case-insensitively."""
    lower = {k.lower(): k for k in fields}
    for cand in (name, f"System.{name}", f"Microsoft.VSTS.Common.{name}", f"Microsoft.VSTS.Scheduling.{name}"):
        if cand.lower() in lower:
            return lower[cand.lower()]
    hits = [k for k in fields if k.lower().endswith("." + name.lower())]
    return hits[0] if len(hits) == 1 else None


def resolve_write_field(name: str, fields: dict) -> str:
    """Full reference name for a field we are about to set."""
    key = name.strip()
    if "." in key:
        return key
    alias = FIELD_ALIASES.get(key.lower())
    if alias:
        return alias
    resolved = resolve_field(fields, key)
    if resolved:
        return resolved
    raise AdoError(f"unknown field {name!r}; run --fields all to see the names on this item")


def parse_assignments(values: list[str]) -> list[tuple[str, str]]:
    """Parse --set FIELD=VALUE. Only the first '=' separates name from value."""
    out: list[tuple[str, str]] = []
    for raw in values:
        if "=" not in raw:
            raise AdoError(f"--set needs FIELD=VALUE, got {raw!r}")
        name, _, value = raw.partition("=")
        if not name.strip():
            raise AdoError(f"--set needs a field name, got {raw!r}")
        out.append((name.strip(), value))
    return out


def merge_tags(current: str, additions: list[str]) -> str:
    """Add tags to the existing ones, keeping order and dropping duplicates."""
    tags = [t.strip() for t in (current or "").split(";") if t.strip()]
    for add in additions:
        tag = add.strip()
        if tag and not any(t.lower() == tag.lower() for t in tags):
            tags.append(tag)
    return "; ".join(tags)


def text_to_html(text: str) -> str:
    """Plain text to the HTML Azure DevOps renders. Text that already carries
    real markup is left alone, so a caller can pass HTML deliberately; anything
    else is escaped, so a stray '<' cannot inject markup."""
    if HTML_TAG_RE.search(text):
        return text
    escaped = html.escape(text, quote=False)
    return escaped.replace("\r\n", "\n").replace("\r", "\n").replace("\n", "<br />")


def prepare_value(field: str, value: str) -> str:
    """Rich text fields take HTML; everything else is stored as given."""
    return text_to_html(value) if field in HTML_FIELDS else value


def build_patch(changes: list[tuple[str, str]]) -> list[dict]:
    """JSON-patch ops for a work item update. 'add' covers create and replace."""
    return [{"op": "add", "path": f"/fields/{name}", "value": value} for name, value in changes]


def truncate(text: str, limit: int = 120) -> str:
    text = text.replace("\n", " ").strip()
    return text if len(text) <= limit else text[:limit] + "..."


def field_display(value) -> str:
    """One-line form of a field value for the plan."""
    if value is None or value == "":
        return "(empty)"
    if isinstance(value, dict):
        return value.get("displayName") or value.get("uniqueName") or "(identity)"
    return truncate(clean(value) if isinstance(value, str) else str(value))


def identity_value(me: dict) -> str:
    """The value to write for an identity field: the unique name if there is one."""
    return me.get("uniqueName") or me.get("displayName") or ""


def render_fields(item: dict, names: list[str]) -> str:
    f = item["fields"]
    if names == ["all"]:
        wanted = sorted(k for k, v in f.items() if v not in (None, ""))
    else:
        wanted = [resolve_field(f, n) or n for n in names]
    lines = [f"#{item['id']} {f.get('System.Title')}"]
    for key in wanted:
        v = f.get(key)
        if isinstance(v, dict):
            v = v.get("displayName") or v.get("name") or json.dumps(v)
        text = clean(v) if isinstance(v, str) else str(v)
        if names == ["all"] and len(text) > 120:
            text = text[:120] + "..."
        lines.append(f"{key}: {text if v is not None else '(not set or unknown field)'}")
    return "\n".join(lines)


def slugify(title: str, limit: int = 50) -> str:
    """Branch-safe slug: drops leading [TAG] prefixes, ascii-only, cut on a word boundary."""
    title = re.sub(r"^(\s*\[[^\]]*\])+\s*", "", title)
    title = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    words = re.sub(r"[^a-z0-9]+", "-", title).strip("-").split("-")
    slug = ""
    for w in words:
        if not w:
            continue
        nxt = f"{slug}-{w}" if slug else w
        if len(nxt) > limit:
            break
        slug = nxt
    return slug or "work-item"


def suggest_branch(item: dict, prefix: str | None = None) -> dict[str, str]:
    f = item["fields"]
    wtype = f.get("System.WorkItemType", "")
    prefix = prefix or ("bugfix" if wtype == "Bug" else "features")
    title = f.get("System.Title", "")
    clean_title = re.sub(r"^(\s*\[[^\]]*\])+\s*", "", title) or title
    return {
        "branch": f"{prefix}/{item['id']}-{slugify(title)}",
        "pr_title": f"{clean_title} (#{item['id']})",
        "pr_body": f"Resolves #{item['id']}: {title}",
    }


def git(*args: str) -> tuple[int, str]:
    """Run a git command. Returns (exit code, output)."""
    try:
        p = subprocess.run(["git", *args], capture_output=True, text=True, timeout=15)
        return p.returncode, (p.stdout or p.stderr or "").strip()
    except (OSError, subprocess.SubprocessError) as e:
        return 1, str(e)


def create_branch(name: str, path: str) -> str:
    """Create and check out a local branch. Never pushes, never touches the
    remote. Returns where the branch was made, so the caller can say it."""
    code, root = git("-C", path, "rev-parse", "--show-toplevel")
    if code != 0 or not root:
        raise AdoError(f"{path} is not inside a git repository")
    code, current = git("-C", root, "rev-parse", "--abbrev-ref", "HEAD")
    if code == 0 and current == name:
        return f"already checked out in {root}"
    code, _ = git("-C", root, "show-ref", "--verify", "--quiet", f"refs/heads/{name}")
    if code == 0:
        raise AdoError(f"branch {name} already exists in {root}; check it out or use --prefix")
    code, err = git("-C", root, "checkout", "-b", name)
    if code != 0:
        raise AdoError(f"git checkout -b {name} failed: {err}")
    return f"created in {root}"


def mine_wiql(sprint: bool) -> str:
    where = ("[System.AssignedTo] = @Me AND [System.State] NOT IN ('Closed','Done','Removed','Resolved') "
             "AND [System.TeamProject] = @project")
    if sprint:
        where += " AND [System.IterationPath] = @CurrentIteration"
    return f"SELECT [System.Id] FROM WorkItems WHERE {where} ORDER BY [System.IterationPath], [Microsoft.VSTS.Common.Priority]"


def build_tree(client: "Client", wid: int, max_nodes: int = 300) -> tuple[dict[int, dict], dict[int, list[int]], int]:
    """Climb to the root, then walk down level by level. Returns (items, children, root)."""
    items: dict[int, dict] = {}
    root = wid
    cur = client.item(wid)
    items[wid] = cur
    while True:
        parents = relation_ids(cur)["parent"]
        if not parents or parents[0] in items:
            break
        root = parents[0]
        cur = client.item(root)
        items[root] = cur
    children: dict[int, list[int]] = {}
    frontier = [root]
    while frontier and len(items) < max_nodes:
        nxt: list[int] = []
        for i in frontier:
            children[i] = relation_ids(items[i])["children"]
            nxt += children[i]
        missing = [c for c in nxt if c not in items][: max_nodes - len(items)]
        for it in client.items(missing):
            items[it["id"]] = it
        frontier = [c for c in nxt if c in items]
    return items, children, root


def render_tree(items: dict[int, dict], children: dict[int, list[int]], root: int, mark: int) -> str:
    lines: list[str] = []

    def walk(i: int, depth: int):
        f = items[i]["fields"]
        flag = "  <-- here" if i == mark else ""
        lines.append(f"{'  ' * depth}#{i} [{f.get('System.WorkItemType')}] {f.get('System.Title')} ({f.get('System.State')}){flag}")
        for c in children.get(i, []):
            if c in items:
                walk(c, depth + 1)

    walk(root, 0)
    return "\n".join(lines)


def render_item(item: dict, comments: list[dict] | None, links: dict[str, list[int]] | None, titles: dict) -> str:
    f = item["fields"]
    assigned = (f.get("System.AssignedTo") or {}).get("displayName", "-")
    lines = [
        f"#{item['id']} [{f.get('System.WorkItemType')}] {f.get('System.Title')}",
        f"State: {f.get('System.State')} | Assigned: {assigned} | Iteration: {f.get('System.IterationPath')}",
    ]
    tags = f.get("System.Tags")
    if tags:
        lines.append(f"Tags: {tags}")
    for label, key in (("", "desc"), ("Acceptance criteria", "ac"), ("Repro", "repro")):
        body = clean(f.get(FIELDS[key]))
        if body:
            lines += ["", f"{label}:" if label else "Description:", body]
    if links:
        lines.append("")
        for kind, ids in links.items():
            for i in ids:
                t = titles.get(i, {})
                lines.append(f"{kind}: #{i} [{t.get('System.WorkItemType', '?')}] {t.get('System.Title', '?')} ({t.get('System.State', '?')})")
    if comments is not None:
        lines += ["", "--- Comments ---"]
        for c in comments:
            lines.append(f"[{c.get('createdDate', '')[:10]}] {c['createdBy']['displayName']}: {clean(c.get('text'))}")
        if not comments:
            lines.append("(none)")
    return "\n".join(lines)


def render_plan(item: dict, changes: list[tuple[str, str, str, str]], comment: str | None, fmt: str,
                label: str = "dry-run") -> str:
    """What a write would do, before anything is sent."""
    f = item.get("fields", {})
    lines = [f"{label}: #{item['id']} [{f.get('System.WorkItemType', '?')}] {f.get('System.Title', '?')}"]
    for ref, old, new, _ in changes:
        lines.append(f"  {ref}: {old} -> {new}")
    if comment is not None:
        lines.append(f"  comment ({fmt}):")
        for line in comment.splitlines() or [""]:
            lines.append(f"    | {line}")
    return "\n".join(lines)


def read_text(spec: str) -> str:
    try:
        return sys.stdin.read() if spec == "-" else Path(spec).read_text(encoding="utf-8")
    except OSError as e:
        raise AdoError(f"cannot read {spec}: {e}")


def cmd_write(args, client: "Client", item: dict) -> int:
    """Update fields and/or add a comment. Nothing is sent without --yes."""
    wid = item["id"]
    fields = item.get("fields", {})

    values: dict[str, str] = {}
    for name, value in parse_assignments(args.set or []):
        ref = resolve_write_field(name, fields)
        if ref in values:
            raise AdoError(f"--set {name} was given twice")
        values[ref] = value
    if args.add_tag:
        base = values.get("System.Tags", fields.get("System.Tags") or "")
        values["System.Tags"] = merge_tags(base, args.add_tag)
    for ref, value in list(values.items()):
        if ref in IDENTITY_FIELDS and value.strip().lower() == "@me":
            values[ref] = identity_value(client.me())
            if not values[ref]:
                raise AdoError("cannot resolve the current user for @me")

    changes = []
    for ref, value in values.items():
        prepared = prepare_value(ref, value)
        changes.append((ref, field_display(fields.get(ref)), field_display(prepared), prepared))

    comment = None
    payload = None
    if args.comment_file:
        comment = read_text(args.comment_file)
    elif args.comment is not None:
        comment = args.comment
    if comment is not None:
        comment = comment.strip()
        if not comment:
            raise AdoError("the comment is empty")
        payload = text_to_html(comment) if args.comment_format == "html" else comment

    print(render_plan(item, changes, comment, args.comment_format, "dry-run" if not args.yes else "write"))
    url = f"https://dev.azure.com/{urllib.parse.quote(client.org)}/{urllib.parse.quote(client.project)}/_workitems/edit/{wid}"

    if not args.yes:
        if changes:
            try:
                client.update_item(wid, build_patch([(r, v) for r, _, _, v in changes]), validate_only=True)
                print("server check: ok, Azure DevOps accepts this change")
            except AdoError as e:
                if "HTTP 401" in str(e) or "HTTP 403" in str(e):
                    print(f"server check skipped: {e}")
                else:
                    print(f"server check failed: {e}", file=sys.stderr)
                    return 1
        print("nothing sent; re-run with --yes after the user confirms")
        return 0

    if changes:
        updated = client.update_item(wid, build_patch([(r, v) for r, _, _, v in changes]))
        print(f"updated: #{wid} rev {updated.get('rev', '?')}")
    if payload is not None:
        result = client.post_comment(wid, payload, args.comment_format)
        print(f"commented: #{wid} comment {result.get('id', '?')}")
    print(f"url: {url}")
    return 0


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", nargs="?", help="work item id or dev.azure.com work item URL")
    p.add_argument("--links", action="store_true", help="show parent/children/related with titles")
    p.add_argument("--children", action="store_true", help="also print each child item in full")
    p.add_argument("--no-comments", action="store_true")
    p.add_argument("--fields", help="comma-separated field names, or 'all'")
    p.add_argument("--prs", action="store_true", help="linked PRs, commits, branches")
    p.add_argument("--tree", action="store_true", help="full hierarchy as titles only")
    p.add_argument("--branch", action="store_true", help="suggest branch name and PR title/body")
    p.add_argument("--create-branch", action="store_true",
                   help="create and check out that branch locally (never pushes)")
    p.add_argument("--repo", help="working copy for --create-branch (default: current directory)")
    p.add_argument("--prefix", help="branch prefix override (default feature, bugfix for Bug)")
    p.add_argument("--mine", action="store_true", help="my open items")
    p.add_argument("--sprint", action="store_true", help="with --mine: current iteration only")
    p.add_argument("--query", help="WIQL query; prints id, type, state, title only")
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--profile")
    p.add_argument("--check", action="store_true", help="validate config and credentials only")
    p.add_argument("--comment", help="comment text to add to the work item")
    p.add_argument("--comment-file", help="file holding the comment text; - for stdin")
    p.add_argument("--comment-format", choices=["html", "markdown"], default="html",
                   help="how the comment text is written (default: html)")
    p.add_argument("--set", action="append", dest="set", metavar="FIELD=VALUE",
                   help="set a field; repeatable")
    p.add_argument("--add-tag", action="append", dest="add_tag", metavar="TAG",
                   help="add a tag, keeping the existing ones; repeatable")
    p.add_argument("--yes", action="store_true", help="actually write (default is a dry run)")
    args = p.parse_args(argv)

    writes = bool(args.comment is not None or args.comment_file or args.set or args.add_tag)
    if args.comment is not None and args.comment_file:
        p.error("use either --comment or --comment-file, not both")
    if args.yes and not writes:
        p.error("--yes needs --comment, --comment-file, --set or --add-tag")
    if writes and (args.query or args.mine or args.tree):
        p.error("a write cannot be combined with --query, --mine or --tree")

    cfg = resolve_config(args.profile)
    org, project = cfg["org"], cfg["project"]
    wid = None
    if args.target:
        m = URL_RE.search(args.target)
        if m:
            org, project, wid = urllib.parse.unquote(m[1]), urllib.parse.unquote(m[2]), int(m[3])
        elif args.target.isdigit():
            wid = int(args.target)
        else:
            p.error("target must be a work item id or a dev.azure.com work item URL")
    try:
        if not org or not project:
            raise AdoError("ADO_ORG and ADO_PROJECT are not set (env or ~/.config/ado.env)")
        client = Client(org, project, cfg["pat"])
        if args.check:
            client.get(f"wit/workitemtypes?api-version={API}")
            print(f"ok: {org}/{project}")
            return 0
        if args.mine:
            args.query = mine_wiql(args.sprint)
        if args.query:
            ids = [w["id"] for w in client.wiql(args.query, args.top).get("workItems", [])]
            info = client.titles(ids)
            for i in ids:
                t = info.get(i, {})
                print(f"#{i} [{t.get('System.WorkItemType', '?')}] {t.get('System.State', '?')}: {t.get('System.Title', '?')}")
            return 0
        if wid is None:
            p.error("give a work item id, a URL, --query, or --check")
        if args.tree:
            items, children, root = build_tree(client, wid)
            print(render_tree(items, children, root, wid))
            return 0
        item = client.item(wid)
        if writes:
            if args.fields or args.branch or args.prs:
                p.error("--comment, --set and --add-tag cannot be combined with --fields, --branch or --prs")
            return cmd_write(args, client, item)
        if args.create_branch:
            if args.fields or args.prs:
                p.error("--create-branch cannot be combined with --fields or --prs")
            suggestion = suggest_branch(item, args.prefix)
            where = create_branch(suggestion["branch"], args.repo or ".")
            print(f"branch: {suggestion['branch']}  ({where})")
            print(f"pr_title: {suggestion['pr_title']}")
            print(f"pr_body: {suggestion['pr_body']}")
            return 0
        if args.fields:
            print(render_fields(item, [n.strip() for n in args.fields.split(",") if n.strip()]))
            return 0
        if args.branch:
            for k, v in suggest_branch(item, args.prefix).items():
                print(f"{k}: {v}")
            return 0
        if args.prs:
            arts = [a for a in map(parse_artifact, item.get("relations") or []) if a]
            print("\n".join(describe_artifact(client, a) for a in arts) or "(no linked PRs or commits)")
            return 0
        links = relation_ids(item) if (args.links or args.children) else None
        titles = client.titles(sorted({i for v in links.values() for i in v})) if links else {}
        print(render_item(item, None if args.no_comments else client.comments(wid), links, titles))
        if args.children:
            for cid in links["children"]:
                print("\n=====\n")
                print(render_item(client.item(cid), None if args.no_comments else client.comments(cid), None, {}))
        return 0
    except AdoError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
