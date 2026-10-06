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


# Claude Code plugin / marketplace manifest.
# https://code.claude.com/docs/en/plugins/manifest-reference
# https://code.claude.com/docs/en/plugins/marketplace-reference
PLUGIN_DIR = REPO_ROOT / ".claude-plugin"
PLUGIN_MANIFEST = PLUGIN_DIR / "plugin.json"
MARKETPLACE_MANIFEST = PLUGIN_DIR / "marketplace.json"
# name must be non-empty with no spaces, '@', ':', path separators, or
# control/bidi characters. kebab-case is the documented recommendation.
PLUGIN_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
RESERVED_PLUGIN_PREFIXES = ("claude-", "anthropic-", "anthropics-", "cc-plugin-")
RESERVED_PLUGIN_NAMES = {"claude", "anthropic", "anthropics", "claude-code", "claude-mods"}


def _load_json(path: Path, problems: list[str]) -> dict | None:
    import json
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        problems.append(f"{path.name} is not valid JSON: {exc}")
        return None
    if not isinstance(data, dict):
        problems.append(f"{path.name} must be a JSON object")
        return None
    return data


def check_plugin_manifests() -> list[str]:
    """Both are optional, so a missing one is not an error -- a broken one is."""
    problems: list[str] = []
    plugin = _load_json(PLUGIN_MANIFEST, problems) if PLUGIN_MANIFEST.is_file() else None
    market = _load_json(MARKETPLACE_MANIFEST, problems) if MARKETPLACE_MANIFEST.is_file() else None

    if plugin:
        name = plugin.get("name")
        if not name or not isinstance(name, str):
            problems.append("plugin.json is missing required field: name")
        else:
            lowered = name.lower()
            if not PLUGIN_NAME_RE.match(name):
                problems.append(
                    f"plugin.json name {name!r} must be non-empty with no spaces, '@', ':', "
                    f"or path separators; use kebab-case")
            if lowered.startswith(RESERVED_PLUGIN_PREFIXES):
                problems.append(f"plugin.json name {name!r} uses an Anthropic-reserved prefix")
            if lowered in RESERVED_PLUGIN_NAMES:
                problems.append(f"plugin.json name {name!r} is reserved by Anthropic")
            if "claude" in lowered.replace("-", "").replace(".", "").split("_") or \
               re.search(r"(^|[-_.])(claude|anthropic)([-_.]|$)", lowered):
                problems.append(
                    f"plugin.json name {name!r} puts 'claude' or 'anthropic' in the name; "
                    f"that is an Anthropic-reserved pattern")
        if not plugin.get("description"):
            problems.append("plugin.json has no description; claude plugin validate warns")
        author = plugin.get("author")
        if author is not None:
            if isinstance(author, str):
                pass
            elif isinstance(author, dict):
                if not author.get("name"):
                    problems.append("plugin.json author.name is required when author is an object")
            else:
                problems.append("plugin.json author must be a string or an object")

    if market:
        for field in ("name", "owner", "plugins"):
            if field not in market:
                problems.append(f"marketplace.json is missing required field: {field}")
        if "description" not in market and "metadata" not in market:
            problems.append("marketplace.json has no description; claude plugin validate warns")
        owner = market.get("owner")
        if isinstance(owner, dict) and not owner.get("name"):
            problems.append("marketplace.json owner.name is required")
        elif owner is not None and not isinstance(owner, dict):
            problems.append("marketplace.json owner must be an object")
        entries = market.get("plugins")
        if not isinstance(entries, list):
            problems.append("marketplace.json plugins must be an array")
        else:
            for i, entry in enumerate(entries):
                if not isinstance(entry, dict):
                    problems.append(f"marketplace.json plugins[{i}] must be an object")
                    continue
                for field in ("name", "source"):
                    if not entry.get(field):
                        problems.append(f"marketplace.json plugins[{i}] is missing {field}")
                # The docs say to keep the two names the same.
                if plugin and entry.get("name") and entry["name"] != plugin.get("name"):
                    problems.append(
                        f"marketplace.json plugins[{i}].name {entry['name']!r} differs from "
                        f"plugin.json name {plugin.get('name')!r}; the docs say keep them equal")
                # A relative source must start with './' unless metadata.pluginRoot is set.
                src = entry.get("source")
                if (isinstance(src, str) and not src.startswith((".", "http", "git", "/"))
                        and not (market.get("metadata") or {}).get("pluginRoot")):
                    problems.append(
                        f"marketplace.json plugins[{i}].source {src!r} must start with './' "
                        f"unless metadata.pluginRoot is set")

    # A `source: "./"` entry disables the default skills/ scan, so `skills` must
    # then be declared explicitly or the plugin loads with nothing in it.
    if market and plugin:
        for i, entry in enumerate(market.get("plugins") or []):
            if not isinstance(entry, dict):
                continue
            if entry.get("source") == "./" and not entry.get("skills"):
                problems.append(
                    f"marketplace.json plugins[{i}].source is './', which stops the default "
                    f"skills/ scan; declare `skills: [\"./skills/\"]` or nothing loads")
    return problems


def check_versions(skill_dirs: list[Path]) -> list[str]:
    """apm.yml, plugin.json and each SKILL.md metadata.version must agree.

    Three manifests and N skills is exactly the setup where one gets bumped
    and the others quietly drift, and a consumer then sees two versions.
    """
    versions: dict[str, str] = {}
    try:
        apm = yaml.safe_load(MANIFEST.read_text(encoding="utf-8")) or {}
        if isinstance(apm, dict) and isinstance(apm.get("version"), str):
            versions["apm.yml"] = apm["version"]
    except (OSError, yaml.YAMLError):
        pass
    plugin_problems: list[str] = []
    if PLUGIN_MANIFEST.is_file():
        plugin = _load_json(PLUGIN_MANIFEST, plugin_problems)
        if plugin and isinstance(plugin.get("version"), str):
            versions["plugin.json"] = plugin["version"]

    for skill_dir in skill_dirs:
        front = _read_frontmatter(skill_dir / "SKILL.md")
        if not isinstance(front, dict):
            continue
        meta = front.get("metadata")
        if isinstance(meta, dict) and isinstance(meta.get("version"), str):
            versions[f"{skill_dir.name}/SKILL.md"] = meta["version"]

    problems = []
    distinct = set(versions.values())
    if len(distinct) > 1:
        detail = ", ".join(f"{k}={v}" for k, v in sorted(versions.items()))
        problems.append(f"versions disagree: {detail}. Keep apm.yml, plugin.json, "
                        f"and every SKILL.md metadata.version in step.")
    return problems


def versions_present(skill_dirs: list[Path]) -> bool:
    """True when there is more than one version field worth cross-checking."""
    count = 1 if MANIFEST.is_file() else 0
    count += 1 if PLUGIN_MANIFEST.is_file() else 0
    for skill_dir in skill_dirs:
        front = _read_frontmatter(skill_dir / "SKILL.md")
        if isinstance(front, dict) and isinstance(front.get("metadata"), dict) \
                and "version" in front["metadata"]:
            count += 1
    return count > 1


def _read_frontmatter(skill_md: Path) -> dict | None:
    try:
        text = skill_md.read_text(encoding="utf-8")
    except OSError:
        return None
    if not text.startswith("---"):
        return None
    parts = text.split("---", 2)
    if len(parts) < 3:
        return None
    try:
        data = yaml.safe_load(parts[1])
    except yaml.YAMLError:
        return None
    return data if isinstance(data, dict) else None


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

    plugin_problems = check_plugin_manifests()
    if plugin_problems:
        failed = True
        print("FAIL  .claude-plugin")
        for problem in plugin_problems:
            print(f"        {problem}")
    elif PLUGIN_MANIFEST.is_file() or MARKETPLACE_MANIFEST.is_file():
        print("ok    .claude-plugin")

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

    version_problems = check_versions(skill_dirs)
    if version_problems:
        failed = True
        print("FAIL  versions")
        for problem in version_problems:
            print(f"        {problem}")
    elif versions_present(skill_dirs):
        print("ok    versions")

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
