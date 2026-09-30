#!/usr/bin/env bash
# Symlink every skill in skills/ into ~/.agents/skills so installed agents
# (Claude Code, opencode, ...) discover them. Safe to re-run.
set -euo pipefail

repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
skills_dir="$repo_root/skills"

# Both roots, if the client directories exist: ~/.agents/skills is the
# cross-client standard, ~/.claude/skills is where Claude Code looks.
roots=("$HOME/.agents/skills")
[ -d "$HOME/.claude/skills" ] && roots+=("$HOME/.claude/skills")

[ -d "$skills_dir" ] || { echo "no skills/ directory at $repo_root" >&2; exit 1; }

linked=0
for skill_dir in "$skills_dir"/*/; do
  name="$(basename "$skill_dir")"
  [ -f "$skill_dir/SKILL.md" ] || { echo "skip  $name (no SKILL.md)" >&2; continue; }

  for target in "${roots[@]}"; do
    pretty="$(printf '%s' "$target" | sed "s|^$HOME|~|")"
    mkdir -p "$target"
    dest="$target/$name"
    if [ -L "$dest" ]; then
      echo "ok    $name -> $pretty"
      continue
    fi
    if [ -e "$dest" ]; then
      echo "error $name exists at $dest and is not a symlink — move it aside" >&2
      exit 1
    fi
    ln -sfn "$skill_dir" "$dest"
    echo "link  $name -> $pretty"
    linked=$((linked + 1))
  done
done

echo
echo "$linked symlink(s) created"
