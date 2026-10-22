#!/usr/bin/env python3
"""Test the pure logic of ado.py (config, HTML cleaning, rendering, relations).

Tests live at the repo root, not in the skill folder, because skill folders
ship whole to every consumer.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / "skills" / "azure-devops-workitems" / "scripts" / "ado.py"
spec = importlib.util.spec_from_file_location("ado", SCRIPT)
ado = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ado)


class CleanTests(unittest.TestCase):
    def test_strips_html_and_entities(self):
        self.assertEqual(ado.clean("<div>a&nbsp;b</div><ul><li>x &amp; y</li></ul>"), "a b\n- x & y")

    def test_none(self):
        self.assertEqual(ado.clean(None), "")


class ConfigTests(unittest.TestCase):
    def test_env_beats_file_and_project_is_decoded(self):
        cfg = ado.resolve_config(None, {"ADO_ORG": "env"}, {"ADO_ORG": "file", "ADO_PROJECT": "HSE%20PnG"})
        self.assertEqual((cfg["org"], cfg["project"]), ("env", "HSE PnG"))

    def test_profile_falls_back_to_default(self):
        file_values = {"ADO_ORG": "a", "ADO_PROJECT": "p", "ADO_X_ORG": "b"}
        cfg = ado.resolve_config("x", {}, file_values)
        self.assertEqual((cfg["org"], cfg["project"], cfg["pat"]), ("b", "p", ""))


class RelationTests(unittest.TestCase):
    def test_groups_and_ignores_non_workitem_links(self):
        item = {"relations": [
            {"rel": "System.LinkTypes.Hierarchy-Reverse", "url": "https://x/_apis/wit/workItems/1"},
            {"rel": "System.LinkTypes.Hierarchy-Forward", "url": "https://x/_apis/wit/workItems/2"},
            {"rel": "ArtifactLink", "url": "vstfs:///Git/Commit/abc"},
        ]}
        self.assertEqual(ado.relation_ids(item), {"parent": [1], "children": [2], "related": []})


class RenderTests(unittest.TestCase):
    def test_render_includes_ac_and_comments(self):
        item = {"id": 7, "fields": {"System.WorkItemType": "User Story", "System.Title": "T", "System.State": "Active",
                                    "System.IterationPath": "P\\S1", "System.Description": "<p>d</p>",
                                    "Microsoft.VSTS.Common.AcceptanceCriteria": "<li>ok</li>"}}
        comments = [{"createdDate": "2026-01-02T00:00", "createdBy": {"displayName": "Ann"}, "text": "<p>hi</p>"}]
        out = ado.render_item(item, comments, None, {})
        self.assertIn("#7 [User Story] T", out)
        self.assertIn("Acceptance criteria:\n- ok", out)
        self.assertIn("[2026-01-02] Ann: hi", out)

    def test_no_comments_omits_section(self):
        item = {"id": 1, "fields": {"System.Title": "T"}}
        self.assertNotIn("Comments", ado.render_item(item, None, None, {}))

    def test_url_parsing(self):
        m = ado.URL_RE.search("https://dev.azure.com/Org/My%20Proj/_workitems/edit/42")
        self.assertEqual((m[1], m[2], m[3]), ("Org", "My%20Proj", "42"))


class ArtifactTests(unittest.TestCase):
    def test_azure_repos_pr(self):
        rel = {"rel": "ArtifactLink", "url": "vstfs:///Git/PullRequestId/p%2Fr%2F12", "attributes": {"name": "Pull Request"}}
        self.assertEqual(ado.parse_artifact(rel), {"kind": "pr", "project": "p", "repo": "r", "id": "12"})

    def test_commit_branch_github_and_other(self):
        self.assertEqual(ado.parse_artifact({"rel": "ArtifactLink", "url": "vstfs:///Git/Commit/p%2Fr%2Fabc"})["kind"], "commit")
        self.assertEqual(ado.parse_artifact({"rel": "ArtifactLink", "url": "vstfs:///Git/Ref/p%2Fr%2FGBmain"})["id"], "GBmain")
        gh = {"rel": "ArtifactLink", "url": "https://github.com/o/r/pull/3", "attributes": {"name": "GitHub Pull Request"}}
        self.assertEqual(ado.parse_artifact(gh)["kind"], "github")
        self.assertIsNone(ado.parse_artifact({"rel": "Hyperlink", "url": "https://x"}))
        self.assertIsNone(ado.parse_artifact({"rel": "ArtifactLink", "url": "vstfs:///Build/Build/1"}))


class BranchTests(unittest.TestCase):
    def item(self, wtype, title):
        return {"id": 9, "fields": {"System.WorkItemType": wtype, "System.Title": title}}

    def test_strips_tag_prefixes_and_prefixes_by_type(self):
        self.assertEqual(ado.suggest_branch(self.item("Task", "[BE] Add imported flag"))["branch"], "features/9-add-imported-flag")
        self.assertEqual(ado.suggest_branch(self.item("Bug", "Crash on save!"))["branch"], "bugfix/9-crash-on-save")
        self.assertEqual(ado.suggest_branch(self.item("Bug", "x"), "hotfix")["branch"], "hotfix/9-x")

    def test_slug_limit_ascii_and_empty(self):
        self.assertLessEqual(len(ado.slugify("word " * 40)), 50)
        self.assertEqual(ado.slugify("Caf\u00e9 r\u00e9sum\u00e9"), "cafe-resume")
        self.assertEqual(ado.slugify("[X] !!!"), "work-item")

    def test_pr_title_has_no_tag_prefix(self):
        self.assertEqual(ado.suggest_branch(self.item("Task", "[FE] Show badge"))["pr_title"], "Show badge (#9)")


class CreateBranchTests(unittest.TestCase):
    """The git side of --create-branch, against a throwaway repository."""

    def setUp(self):
        if not shutil.which("git"):
            self.skipTest("git is not installed")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.repo = Path(self.tmp.name)
        self.git("init", "-q")
        self.git("commit", "-q", "--allow-empty", "-m", "init")
        self.original_branch = self.git("rev-parse", "--abbrev-ref", "HEAD").stdout.strip()

    def git(self, *args):
        return subprocess.run(["git", "-C", str(self.repo), "-c", "user.email=t@t",
                               "-c", "user.name=t", *args],
                              capture_output=True, text=True, check=True)

    def test_creates_and_checks_out_the_branch(self):
        where = ado.create_branch("features/9-add-flag", str(self.repo))
        self.assertIn("created in", where)
        self.assertIn(str(self.repo), where)
        self.assertIn("features/9-add-flag", self.git("branch", "--list").stdout)

    def test_refuses_an_existing_branch(self):
        ado.create_branch("features/9-add-flag", str(self.repo))
        self.git("checkout", "-q", self.original_branch)
        with self.assertRaises(ado.AdoError):
            ado.create_branch("features/9-add-flag", str(self.repo))

    def test_refuses_a_path_outside_a_repository(self):
        outside = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, outside)
        with self.assertRaises(ado.AdoError):
            ado.create_branch("features/9-add-flag", outside)


class MineAndTreeTests(unittest.TestCase):
    def test_wiql_sprint_toggle(self):
        self.assertIn("@Me", ado.mine_wiql(False))
        self.assertNotIn("@CurrentIteration", ado.mine_wiql(False))
        self.assertIn("@CurrentIteration", ado.mine_wiql(True))

    def test_render_tree_marks_current_and_indents(self):
        mk = lambda t: {"fields": {"System.WorkItemType": t, "System.Title": t, "System.State": "New"}}
        out = ado.render_tree({1: mk("E"), 2: mk("S"), 3: mk("T")}, {1: [2], 2: [3]}, 1, 3)
        self.assertEqual(out.splitlines()[2], "    #3 [T] T (New)  <-- here")


class FieldTests(unittest.TestCase):
    F = {"System.Title": "T", "Microsoft.VSTS.Common.Priority": 2, "System.AssignedTo": {"displayName": "Ann"}}

    def test_short_names_resolve_case_insensitively(self):
        self.assertEqual(ado.resolve_field(self.F, "priority"), "Microsoft.VSTS.Common.Priority")
        self.assertEqual(ado.resolve_field(self.F, "System.Title"), "System.Title")
        self.assertIsNone(ado.resolve_field(self.F, "Nope"))

    def test_render_fields(self):
        out = ado.render_fields({"id": 1, "fields": self.F}, ["Priority", "AssignedTo", "Nope"])
        self.assertIn("Microsoft.VSTS.Common.Priority: 2", out)
        self.assertIn("System.AssignedTo: Ann", out)
        self.assertIn("Nope: (not set or unknown field)", out)

    def test_all_skips_empty(self):
        out = ado.render_fields({"id": 1, "fields": {**self.F, "X.Empty": ""}}, ["all"])
        self.assertNotIn("X.Empty", out)


class WriteTests(unittest.TestCase):
    """The logic behind --comment, --set and --add-tag."""

    def test_plain_text_becomes_html_with_line_breaks(self):
        self.assertEqual(ado.text_to_html("a\nb"), "a<br />b")
        self.assertEqual(ado.text_to_html("AT&T <x>"), "AT&amp;T &lt;x&gt;")

    def test_existing_html_is_left_alone(self):
        self.assertEqual(ado.text_to_html("<div>keep</div>"), "<div>keep</div>")

    def test_angle_brackets_that_are_not_markup_are_escaped(self):
        self.assertEqual(ado.text_to_html("see <https://x.dev> now"), "see &lt;https://x.dev&gt; now")

    def test_only_rich_text_fields_are_converted(self):
        self.assertEqual(ado.prepare_value("System.Description", "a\nb"), "a<br />b")
        self.assertEqual(ado.prepare_value("System.Title", "a\nb"), "a\nb")

    def test_parse_assignments_splits_on_first_equal(self):
        self.assertEqual(ado.parse_assignments(["Title=a=b", "State=Active"]),
                         [("Title", "a=b"), ("State", "Active")])

    def test_parse_assignments_rejects_junk(self):
        with self.assertRaises(ado.AdoError):
            ado.parse_assignments(["Title"])
        with self.assertRaises(ado.AdoError):
            ado.parse_assignments(["=value"])

    def test_resolve_write_field(self):
        fields = {"System.Title": "T", "Custom.Stage": "x"}
        self.assertEqual(ado.resolve_write_field("State", fields), "System.State")
        self.assertEqual(ado.resolve_write_field("acceptancecriteria", fields),
                         "Microsoft.VSTS.Common.AcceptanceCriteria")
        self.assertEqual(ado.resolve_write_field("Custom.Stage", fields), "Custom.Stage")
        self.assertEqual(ado.resolve_write_field("Stage", fields), "Custom.Stage")
        with self.assertRaises(ado.AdoError):
            ado.resolve_write_field("Nope", fields)

    def test_merge_tags_keeps_existing_and_skips_duplicates(self):
        self.assertEqual(ado.merge_tags("web; front", ["backend", "WEB"]), "web; front; backend")
        self.assertEqual(ado.merge_tags("", ["one"]), "one")

    def test_build_patch_uses_add_ops(self):
        self.assertEqual(ado.build_patch([("System.State", "Active")]),
                         [{"op": "add", "path": "/fields/System.State", "value": "Active"}])

    def test_field_display_is_one_line(self):
        self.assertEqual(ado.field_display({"displayName": "Ann"}), "Ann")
        self.assertEqual(ado.field_display(None), "(empty)")
        self.assertEqual(ado.field_display("<div>a</div>\n<div>b</div>"), "a b")

    def test_identity_value_prefers_unique_name(self):
        self.assertEqual(ado.identity_value({"uniqueName": "a@b.c", "displayName": "A"}), "a@b.c")
        self.assertEqual(ado.identity_value({"displayName": "A"}), "A")


if __name__ == "__main__":
    unittest.main()
