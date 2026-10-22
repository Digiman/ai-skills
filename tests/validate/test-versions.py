#!/usr/bin/env python3
"""Test the version rules in scripts/validate.py.

Collection manifests must match each other; a skill may be older than the
collection but never newer, and must be X.Y.Z.
"""

from __future__ import annotations

import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.dont_write_bytecode = True
SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "validate.py"
spec = importlib.util.spec_from_file_location("validate", SCRIPT)
validate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(validate)


class VersionTests(unittest.TestCase):
    def run_check(self, apm: str, plugin: str, skills: dict[str, str]) -> list[str]:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "apm.yml").write_text(f"name: x\nversion: {apm}\n")
            (root / "plugin.json").write_text(f'{{"name": "x", "version": "{plugin}"}}')
            dirs = []
            for name, version in skills.items():
                d = root / name
                d.mkdir()
                (d / "SKILL.md").write_text(f'---\nname: {name}\ndescription: d\nmetadata:\n  version: "{version}"\n---\n')
                dirs.append(d)
            with mock.patch.object(validate, "MANIFEST", root / "apm.yml"), \
                    mock.patch.object(validate, "PLUGIN_MANIFEST", root / "plugin.json"):
                return validate.check_versions(dirs)

    def test_independent_skill_versions_pass(self):
        self.assertEqual(self.run_check("0.3.0", "0.3.0", {"a": "0.1.0", "b": "0.2.0", "c": "0.3.0"}), [])

    def test_manifests_must_match(self):
        self.assertIn("collection versions disagree", self.run_check("0.3.0", "0.2.0", {"a": "0.1.0"})[0])

    def test_skill_newer_than_collection_fails(self):
        self.assertIn("newer than the collection", self.run_check("0.3.0", "0.3.0", {"a": "0.10.0"})[0])

    def test_not_semver_fails(self):
        self.assertIn("not semver", self.run_check("0.3.0", "0.3.0", {"a": "v1"})[0])


if __name__ == "__main__":
    unittest.main()
