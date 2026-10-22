#!/usr/bin/env python3
"""Test the pure logic of ado_pr.py (remote parsing, payloads, safety defaults).

Tests live at the repo root, not in the skill folder, because skill folders
ship whole to every consumer.
"""

from __future__ import annotations

import importlib.util
import io
import sys
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "azure-devops-pullrequests" / "scripts" / "ado_pr.py"
spec = importlib.util.spec_from_file_location("ado_pr", SCRIPT)
ado_pr = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ado_pr)


class ParseRemoteTests(unittest.TestCase):
    def test_https_with_user(self):
        self.assertEqual(
            ado_pr.parse_remote("https://acme@dev.azure.com/acme/My%20Proj/_git/my-repo"),
            {"org": "acme", "project": "My Proj", "repo": "my-repo"},
        )

    def test_ssh(self):
        self.assertEqual(
            ado_pr.parse_remote("git@ssh.dev.azure.com:v3/acme/Proj/repo"),
            {"org": "acme", "project": "Proj", "repo": "repo"},
        )

    def test_non_ado_remote(self):
        self.assertIsNone(ado_pr.parse_remote("git@github.com:Digiman/ai-skills.git"))


class PrRefTests(unittest.TestCase):
    def test_id(self):
        self.assertEqual(ado_pr.parse_pr_ref("42"), {"id": 42})

    def test_url(self):
        self.assertEqual(
            ado_pr.parse_pr_ref("https://acme@dev.azure.com/acme/My%20Proj/_git/repo/pullrequest/42?_a=files"),
            {"id": 42, "org": "acme", "project": "My Proj", "repo": "repo"},
        )

    def test_rejects_other(self):
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            ado_pr.parse_pr_ref("https://github.com/o/r/pull/1")

    def test_url_overrides_remote_in_locate(self):
        args = mock.Mock(profile=None, repo=None, pr_ref={"id": 1, "org": "o", "project": "p", "repo": "r"})
        with mock.patch.object(ado_pr, "git", return_value="https://dev.azure.com/x/y/_git/z"), \
                mock.patch.object(ado_pr, "auth_header", return_value="h"):
            client, repo = ado_pr.locate(args)
        self.assertEqual((client.org, client.project, repo), ("o", "p", "r"))


class IdTests(unittest.TestCase):
    def test_dedupes_and_strips_hash(self):
        self.assertEqual(ado_pr.parse_ids("#12, 13 12"), [12, 13])

    def test_empty(self):
        self.assertEqual(ado_pr.parse_ids(""), [])

    def test_rejects_garbage(self):
        with self.assertRaises(ado_pr.AdoError):
            ado_pr.parse_ids("12,abc")


class PayloadTests(unittest.TestCase):
    def test_create_payload(self):
        p = ado_pr.build_create_payload("feature/1-x", "refs/heads/main", "T", "B", [1, 2], True)
        self.assertEqual(p["sourceRefName"], "refs/heads/feature/1-x")
        self.assertEqual(p["targetRefName"], "refs/heads/main")
        self.assertTrue(p["isDraft"])
        self.assertEqual(p["workItemRefs"], [{"id": "1"}, {"id": "2"}])

    def test_no_work_items_omits_refs(self):
        self.assertNotIn("workItemRefs", ado_pr.build_create_payload("a", "b", "T", "", [], False))

    def test_link_patch_and_idempotence(self):
        patch = ado_pr.build_link_patch("P", "R", 7)
        url = patch[0]["value"]["url"]
        self.assertEqual(url, "vstfs:///Git/PullRequestId/P%2fR%2f7")
        self.assertTrue(ado_pr.already_linked({"relations": [{"url": url.upper()}]}, url))
        self.assertFalse(ado_pr.already_linked({}, url))


class RenderTests(unittest.TestCase):
    PR = {"pullRequestId": 7, "title": "T", "status": "active", "isDraft": True, "mergeStatus": "succeeded",
          "sourceRefName": "refs/heads/feat", "targetRefName": "refs/heads/main",
          "createdBy": {"displayName": "Ann"}, "description": "body",
          "reviewers": [{"displayName": "Bob", "vote": -5, "isRequired": True}]}

    def test_render_pr(self):
        out = ado_pr.render_pr(self.PR, ["12"], "url")
        self.assertIn("PR 7: T [active, draft, merge: succeeded]", out)
        self.assertIn("feat -> main  by Ann", out)
        self.assertIn("Bob (required): waiting for author", out)
        self.assertIn("work items: #12", out)

    def test_render_pr_line(self):
        self.assertEqual(ado_pr.render_pr_line(self.PR), "PR 7 [active draft] T (feat -> main, Ann)")

    def test_threads_skip_system_and_resolved(self):
        threads = [
            {"id": 1, "status": "active", "threadContext": {"filePath": "/a.cs", "rightFileStart": {"line": 4}},
             "comments": [{"author": {"displayName": "Bob"}, "content": "fix\nthis"}]},
            {"id": 2, "status": "fixed", "comments": [{"author": {"displayName": "Bob"}, "content": "old"}]},
            {"id": 3, "status": "active", "comments": [{"commentType": "system", "content": "voted"}]},
        ]
        out = ado_pr.render_threads(threads)
        self.assertIn("[thread 1] active /a.cs:4", out)
        self.assertIn("Bob: fix this", out)
        self.assertNotIn("thread 2", out)
        self.assertNotIn("thread 3", out)
        self.assertIn("thread 2", ado_pr.render_threads(threads, include_resolved=True))
        self.assertEqual(ado_pr.render_threads([]), "(no open threads)")

    def test_render_checks(self):
        evals = [
            {"status": "rejected", "configuration": {"isBlocking": True, "type": {"displayName": "Build"}},
             "context": {"buildId": 9}},
            {"status": "approved", "configuration": {"type": {"displayName": "Minimum reviewers"}}},
            {"status": "notApplicable", "configuration": {"type": {"displayName": "Comments"}}},
        ]
        builds = {9: {"definition": {"name": "CI"}, "buildNumber": "20.1", "result": "failed"}}
        out = ado_pr.render_checks(evals, builds, {9: ["dotnet test"]})
        self.assertIn("rejected [blocking] Build - CI #20.1: failed; failed: dotnet test", out)
        self.assertIn("approved Minimum reviewers", out)
        self.assertNotIn("Comments", out)
        self.assertEqual(ado_pr.render_checks([], {}, {}), "(no policies)")

    def test_update_patch(self):
        self.assertEqual(ado_pr.build_update_patch(None, None, None), {})
        self.assertEqual(ado_pr.build_update_patch("T", None, False), {"title": "T", "isDraft": False})


class BodyTests(unittest.TestCase):
    def test_too_long(self):
        with mock.patch.object(ado_pr.Path, "read_text", return_value="x" * 4001):
            with self.assertRaises(ado_pr.AdoError):
                ado_pr.read_body("body.md")


class FakeClient:
    org, project = "o", "p"

    def __init__(self, existing=None, branch=True):
        self.existing, self.branch, self.created = existing or [], branch, []

    def repo(self, name):
        return {"id": "rid", "name": name, "defaultBranch": "refs/heads/main", "project": {"id": "pid"}}

    def branch_exists(self, *_):
        return self.branch

    def active_prs(self, *_):
        return self.existing

    def work_item(self, wid):
        return {"id": wid}

    def create_pr(self, repo_id, payload):
        self.created.append(payload)
        return {"pullRequestId": 99}

    def web_url(self, repo, pid):
        return f"url/{pid}"


class CreateFlowTests(unittest.TestCase):
    def run_create(self, client, *extra):
        argv = ["create", "--repo", "r", "--source", "feat", "--title", "T", "--work-items", "5", *extra]
        out = io.StringIO()
        with mock.patch.object(ado_pr, "locate", return_value=(client, "r")), redirect_stdout(out), redirect_stderr(out):
            code = ado_pr.main(argv)
        return code, out.getvalue()

    def test_dry_run_sends_nothing(self):
        c = FakeClient()
        code, out = self.run_create(c)
        self.assertEqual(code, 0)
        self.assertEqual(c.created, [])
        self.assertIn("nothing sent", out)

    def test_yes_creates(self):
        c = FakeClient()
        code, out = self.run_create(c, "--yes", "--draft")
        self.assertEqual(len(c.created), 1)
        self.assertTrue(c.created[0]["isDraft"])
        self.assertIn("created: PR 99 url/99", out)

    def test_existing_pr_not_duplicated(self):
        c = FakeClient(existing=[{"pullRequestId": 3}])
        code, out = self.run_create(c, "--yes")
        self.assertEqual(c.created, [])
        self.assertIn("exists: PR 3", out)

    def test_unpushed_branch_stops(self):
        c = FakeClient(branch=False)
        code, out = self.run_create(c, "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(c.created, [])


class ChecksFlowTests(unittest.TestCase):
    def test_missing_build_scope_degrades(self):
        class C(FakeClient):
            def pr(self, *_):
                return {"pullRequestId": 1}

            def policy_evaluations(self, *_):
                return [{"status": "rejected", "configuration": {"type": {"displayName": "Build"}}, "context": {"buildId": 5}}]

            def build(self, _):
                raise ado_pr.AdoError("HTTP 401 for GET build: token rejected or expired")

        out = io.StringIO()
        with mock.patch.object(ado_pr, "locate", return_value=(C(), "r")), redirect_stdout(out), redirect_stderr(out):
            code = ado_pr.main(["checks", "1"])
        self.assertEqual(code, 0)
        self.assertIn("rejected Build", out.getvalue())
        self.assertIn("Build (Read)", out.getvalue())


class UpdateFlowTests(unittest.TestCase):
    class C(FakeClient):
        def __init__(self, status="active"):
            super().__init__()
            self.status, self.patches = status, []

        def pr(self, repo_id, pid):
            return {"pullRequestId": pid, "title": "T", "status": self.status}

        def update_pr(self, repo_id, pid, patch):
            self.patches.append(patch)

    def run_update(self, client, *extra):
        out = io.StringIO()
        with mock.patch.object(ado_pr, "locate", return_value=(client, "r")), redirect_stdout(out), redirect_stderr(out):
            code = ado_pr.main(["update", "7", *extra])
        return code, out.getvalue()

    def test_dry_run_then_yes(self):
        c = self.C()
        self.run_update(c, "--ready")
        self.assertEqual(c.patches, [])
        self.run_update(c, "--ready", "--yes")
        self.assertEqual(c.patches, [{"isDraft": False}])

    def test_refuses_non_active(self):
        c = self.C(status="completed")
        code, _ = self.run_update(c, "--ready", "--yes")
        self.assertEqual(code, 1)
        self.assertEqual(c.patches, [])

    def test_nothing_to_update(self):
        code, _ = self.run_update(self.C())
        self.assertEqual(code, 1)


if __name__ == "__main__":
    unittest.main()
