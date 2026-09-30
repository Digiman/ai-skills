#!/usr/bin/env python3
"""Check every skill in skills/ against the Agent Skills spec.

Validates the parts of the spec that can be checked mechanically: frontmatter
required fields, name constraints, directory-name match, and the recommended
SKILL.md size. Exits non-zero if any skill fails.
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
MAX_NAME = 64
MAX_DESCRIPTION = 1024
MAX_COMPATIBILITY = 500
MAX_SKILL_MD_LINES = 500
NAME_RE = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ALLOWED_FRONTMATTER = {"name", "description", "license", "compatibility", "metadata", "allowed-tools"}


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

    lines = len(parts[2].strip().splitlines())
    if lines > MAX_SKILL_MD_LINES:
        problems.append(f"body is {lines} lines (max {MAX_SKILL_MD_LINES}, move detail to references/)")

    body = parts[2]
    for ref in re.findall(r"\]\((references/[^)]+)\)", body):
        if not (skill_dir / ref).is_file():
            problems.append(f"broken reference link: {ref}")

    return problems


def main() -> int:
    if not SKILLS_DIR.is_dir():
        print(f"no skills/ directory at {REPO_ROOT}", file=sys.stderr)
        return 2

    skill_dirs = sorted(d for d in SKILLS_DIR.iterdir() if d.is_dir())
    if not skill_dirs:
        print("no skills found")
        return 0

    failed = False
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
