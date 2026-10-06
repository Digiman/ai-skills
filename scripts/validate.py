#!/usr/bin/env python3
"""Check every skill in skills/ against the Agent Skills spec.

Validates the parts of the spec that can be checked mechanically: frontmatter
required fields, name constraints, directory-name match, and the recommended
SKILL.md size.

Also enforces the packaging constraints that only bite at distribution time.
`npx skills` and `apm install` both copy a skill folder whole, and APM rejects
bundles containing symlinks or non-ASCII frontmatter, so those are errors here
rather than a failed install for a consumer. Exits non-zero if any skill fails.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

try:
    import yaml
except ImportError:
    print("needs PyYAML: pip3 install pyyaml", file=sys.stderr)
    sys.exit(2)

REPO_ROOT = Path(__file__).resolve().parent.parent
SKILLS_DIR = REPO_ROOT / "skills"
MANIFEST = REPO_ROOT / "apm.yml"
LICENSE_FILE = REPO_ROOT / "LICENSE"
# Every skill name in this repo starts with this prefix. All skills land in one
# flat directory per agent (~/.agents/skills), so a generic name like
# `code-review` silently overwrites an unrelated skill from another author.
# The spec has no namespace mechanism -- RFC
# agentskills/agentskills#312 proposed `collection/name` but closed without
# landing, and `name` still permits only a-z, 0-9 and hyphens -- so the prefix
# has to be part of the flat name.
NAME_PREFIX = "digiman-"
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_SKILL_MD_LINES = 500
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ALLOWED_FRONTMATTER = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}
# APM refuses to deploy non-ASCII frontmatter, so a non-ASCII value is an error
# here rather than a blocked install for every consumer.
ASCII_RE = re.compile(r"^[\x00-\x7f]*$")
# Directories that only matter to this repo. Anything else under a skill folder
# is shipped to consumers, so these must stay at the repo root. __pycache__
# counts because running the tests writes bytecode next to the script it loads.
REPO_ONLY_SKILL_SUBDIRS = {"tests", "__pycache__"}
TEXT_SUFFIXES = {".py", ".sh", ".md", ".json", ".yaml", ".yml", ".txt"}


def parse_skill(skill_dir: Path) -> list[str]:
    """Return a list of problems; empty means the skill is valid."""
    problems: list[str] = []
    skill_md = skill_dir / "SKILL.md"
    name = skill_dir.name

    if not skill_md.is_file():
        return ["missing SKILL.md"]

    text = skill_md.read_text(encoding="utf-8")
    if not text.startswith("---"):
        return ["SKILL.md does not open with YAML frontmatter"]

    parts = text.split("---", 2)
    if len(parts) < 3:
        return ["unterminated YAML frontmatter"]

    try:
        front = yaml.safe_load(parts[1]) or {}
    except yaml.YAMLError as exc:
        return [f"invalid YAML frontmatter: {exc}"]

    if not isinstance(front, dict):
        return ["frontmatter is not a mapping"]

    unknown = set(front) - ALLOWED_FRONTMATTER
    if unknown:
        problems.append(f"unknown frontmatter field(s): {', '.join(sorted(unknown))}")

    for field in ("name", "description"):
        if field not in front:
            problems.append(f"missing required field: {field}")

    fm_name = front.get("name")
    if fm_name is not None:
        if not isinstance(fm_name, str):
            problems.append("name must be a string")
        else:
            if not 1 <= len(fm_name) <= MAX_NAME:
                problems.append(f"name is {len(fm_name)} chars (max {MAX_NAME})")
            if not NAME_RE.match(fm_name):
                problems.append(f"name {fm_name!r} must be lowercase a-z/0-9 with single hyphens")
            if fm_name != name:
                problems.append(f"name {fm_name!r} does not match directory {name!r}")
            # Only flag a missing prefix when it is short enough that one fits.
            if not fm_name.startswith(NAME_PREFIX) and len(NAME_PREFIX) + len(fm_name) <= MAX_NAME:
                problems.append(
                    f"name {fm_name!r} is generic and would overwrite an unrelated skill "
                    f"of the same name. Prefix it: {NAME_PREFIX}{fm_name}")

    description = front.get("description")
    if description is not None:
        if not isinstance(description, str):
            problems.append("description must be a string")
        else:
            if not 1 <= len(description) <= MAX_DESCRIPTION:
                problems.append(f"description is {len(description)} chars (max {MAX_DESCRIPTION})")

    compat = front.get("compatibility")
    if compat is not None:
        if not isinstance(compat, str):
            problems.append("compatibility must be a string")
        elif len(compat) > MAX_COMPATIBILITY:
            problems.append(f"compatibility is {len(compat)} chars (max {MAX_COMPATIBILITY})")

    metadata = front.get("metadata")
    if metadata is not None and not isinstance(metadata, dict):
        problems.append("metadata must be a mapping")
    if isinstance(metadata, dict):
        for key, value in metadata.items():
            if not isinstance(value, str):
                problems.append(f"metadata.{key} must be a string, got {type(value).__name__}")
            elif not ASCII_RE.match(f"{key}{value}"):
                problems.append(f"metadata.{key} must be ASCII (APM rejects non-ASCII frontmatter)")
    # `metadata:` with nothing under it parses as None, so check presence of the
    # version key separately from the type of the mapping.
    if not isinstance(metadata, dict) or "version" not in metadata:
        problems.append("metadata.version is missing; keep it in step with apm.yml")

    for field in ("name", "description", "compatibility", "license"):
        value = front.get(field)
        if isinstance(value, str) and not ASCII_RE.match(value):
            problems.append(f"{field} must be ASCII (APM rejects non-ASCII frontmatter)")

    lines = len(parts[2].strip().splitlines())
    if lines > MAX_SKILL_MD_LINES:
        problems.append(f"body is {lines} lines (max {MAX_SKILL_MD_LINES}, move detail to references/)")

    body = parts[2]
    for ref in re.findall(r"\]\((references/[^)]+)\)", body):
        if not (skill_dir / ref).is_file():
            problems.append(f"broken reference link: {ref}")

    problems.extend(check_payload(skill_dir))
    return problems


def check_payload(skill_dir: Path) -> list[str]:
    """Packaging hazards: anything here breaks a consumer's install, not ours."""
    problems = []
    # A repo-only directory is reported once, however deep it is. The common
    # case is skills/<name>/scripts/__pycache__, so a top-level-only check would
    # miss the one that actually happens.
    reported: set[str] = set()
    for path in sorted(skill_dir.rglob("*")):
        rel = path.relative_to(skill_dir)
        offender = next((p for p in rel.parts[:-1] if p in REPO_ONLY_SKILL_SUBDIRS), None)
        if offender is None and path.is_dir() and path.name in REPO_ONLY_SKILL_SUBDIRS:
            offender = path.name
        if offender and offender not in reported:
            reported.add(offender)
            if offender == "__pycache__":
                problems.append(
                    f"{offender}/ inside the skill folder would install to every consumer. "
                    f"It is bytecode written by running the tests; the tests set "
                    f"sys.dont_write_bytecode so it should not exist. Delete it and "
                    f"check that a test is not importing from skills/.")
            else:
                problems.append(
                    f"{offender}/ lives inside the skill folder, so it installs to every "
                    f"consumer. Move it to tests/{skill_dir.name}/ at the repo root.")
            continue
        if offender:
            continue
        # APM refuses a bundle with any symlink, and filters them at deploy.
        if path.is_symlink():
            problems.append(
                f"{rel} is a symlink; APM rejects symlinks in a bundle. Use a real file.")
        elif path.is_file() and path.suffix in TEXT_SUFFIXES:
            try:
                path.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                problems.append(f"{rel} is not valid UTF-8")
    return problems


def check_manifest() -> list[str]:
    """apm.yml only has to satisfy the schema's two required fields."""
    if not MANIFEST.is_file():
        return ["apm.yml missing; a skills/<name>/ tree still installs, but APM "
                "synthesizes minimal metadata and apm pack warns"]
    try:
        data = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
    except yaml.YAMLError as exc:
        return [f"apm.yml is not valid YAML: {exc}"]
    if not isinstance(data, dict):
        return ["apm.yml is not a mapping"]
    problems = []
    for field in ("name", "version"):
        if not data.get(field):
            problems.append(f"apm.yml is missing required field: {field}")
    version = data.get("version")
    if isinstance(version, str) and not re.match(r"^\d+\.\d+\.\d+", version):
        problems.append(f"apm.yml version {version!r} is not semver")
    if data.get("license") and not LICENSE_FILE.is_file():
        problems.append(f"apm.yml declares license {data['license']!r} but no LICENSE file exists")
    return problems


def main() -> int:
    if not SKILLS_DIR.is_dir():
        print(f"no skills/ directory at {REPO_ROOT}", file=sys.stderr)
        return 2

    failed = False
    for problem in check_manifest():
        print(f"FAIL  apm.yml\n        {problem}")
        failed = True
    if MANIFEST.is_file() and not check_manifest():
        print("ok    apm.yml")

    skill_dirs = sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())
    if not skill_dirs:
        print("no skills found")
        return 1 if failed else 0

    for skill_dir in skill_dirs:
        problems = parse_skill(skill_dir)
        if problems:
            failed = True
            print(f"FAIL  {skill_dir.name}")
            for problem in problems:
                print(f"        {problem}")
        else:
            print(f"ok    {skill_dir.name}")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
