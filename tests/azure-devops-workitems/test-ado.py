#!/usr/bin/env python3
"""Test the pure logic of ado.py (config, HTML cleaning, rendering, relations).

Tests live at the repo root, not in the skill folder, because skill folders
ship whole to every consumer.
"""

from __future__ import annotations

import importlib.util
import sys
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


if __name__ == "__main__":
    unittest.main()
