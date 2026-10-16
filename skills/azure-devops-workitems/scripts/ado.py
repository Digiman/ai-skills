#!/usr/bin/env python3
"""Read-only Azure DevOps work item reader that prints compact, token-cheap text.

Usage:
  ado.py <id|work-item-url> [--links] [--children] [--no-comments] [--profile NAME]
  ado.py --query "<WIQL>" [--top N]      # list ids + titles only
  ado.py --check                          # verify config/auth without fetching

Config (first match wins): environment, then the file named by ADO_CONFIG,
then ~/.config/ado.env (KEY=value lines). Keys: ADO_ORG, ADO_PROJECT, ADO_PAT.
With --profile NAME the keys are read as ADO_<NAME>_ORG, etc., falling back to
the unprefixed ones. If no PAT is set, an `az login` session is used.

Only GET requests (plus the read-only WIQL POST) are ever sent.
"""

from __future__ import annotations

import argparse
import base64
import html
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
URL_RE = re.compile(r"https?://dev\.azure\.com/([^/]+)/([^/]+)/_workitems/edit/(\d+)")
FIELDS = {
    "desc": "System.Description",
    "ac": "Microsoft.VSTS.Common.AcceptanceCriteria",
    "repro": "Microsoft.VSTS.TCM.ReproSteps",
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


class Client:
    def __init__(self, org: str, project: str, pat: str):
        self.base = f"https://dev.azure.com/{urllib.parse.quote(org)}/{urllib.parse.quote(project)}/_apis"
        self.auth = auth_header(pat)

    def _send(self, req: urllib.request.Request):
        req.add_header("Authorization", self.auth)
        req.add_header("Accept", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.loads(resp.read().decode() or "{}")
        except urllib.error.HTTPError as e:
            hint = {401: "token rejected or expired", 403: "token lacks Work Items (Read) scope",
                    404: "item, project or org not found (check ADO_ORG/ADO_PROJECT)"}.get(e.code, "")
            raise AdoError(f"HTTP {e.code} for {req.full_url.split('?')[0]}: {hint}".rstrip(": "))
        except urllib.error.URLError as e:
            raise AdoError(f"network error: {e.reason}")

    def get(self, path: str):
        return self._send(urllib.request.Request(f"{self.base}/{path}"))

    def wiql(self, query: str, top: int):
        body = json.dumps({"query": query}).encode()
        req = urllib.request.Request(f"{self.base}/wit/wiql?$top={top}&api-version={API}", data=body, method="POST")
        req.add_header("Content-Type", "application/json")
        return self._send(req)

    def item(self, wid: int):
        return self.get(f"wit/workitems/{wid}?$expand=relations&api-version={API}")

    def comments(self, wid: int):
        return self.get(f"wit/workItems/{wid}/comments?api-version=7.1-preview.4").get("comments", [])

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


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("target", nargs="?", help="work item id or dev.azure.com work item URL")
    p.add_argument("--links", action="store_true", help="show parent/children/related with titles")
    p.add_argument("--children", action="store_true", help="also print each child item in full")
    p.add_argument("--no-comments", action="store_true")
    p.add_argument("--query", help="WIQL query; prints id, type, state, title only")
    p.add_argument("--top", type=int, default=50)
    p.add_argument("--profile")
    p.add_argument("--check", action="store_true", help="validate config and credentials only")
    args = p.parse_args(argv)

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
        if args.query:
            ids = [w["id"] for w in client.wiql(args.query, args.top).get("workItems", [])]
            info = client.titles(ids)
            for i in ids:
                t = info.get(i, {})
                print(f"#{i} [{t.get('System.WorkItemType', '?')}] {t.get('System.State', '?')}: {t.get('System.Title', '?')}")
            return 0
        if wid is None:
            p.error("give a work item id, a URL, --query, or --check")
        item = client.item(wid)
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
