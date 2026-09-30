---
name: dotnet-versions
description: Use when the user asks to clean up, prune, or update installed .NET / dotnet SDKs and runtimes, or mentions dotnet uninstall, dotnet-core-uninstall, old .NET versions taking disk space, or "which dotnet versions can I remove". Inventories installed SDKs/runtimes, applies a keep-the-latest-per-band policy, checks global.json pins, and prints sudo commands for the user to run manually. Never removes anything itself.
compatibility: macOS on Apple silicon; requires the dotnet CLI on PATH. Downloads dotnet-core-uninstall to ~/dotnet-core-uninstall on first run.
metadata:
  version: "0.1.0"
---

# .NET version maintenance

Audit installed .NET SDKs and runtimes, decide what is redundant, and hand the
user commands to run. **Never run a removal.** The uninstall tool needs `sudo`
and a non-interactive agent cannot supply a password, so the last step always
belongs to the human.

## The policy

Keep exactly one version per band — the newest. Delete the rest. Default band
is `major`, so keeping one of 8.x, 9.x, and 10.x means keeping 8.0.425,
9.0.318, 10.0.401 and removing every other installed version.

Override the band when the user wants finer granularity:

| `--band`  | Keeps                                              | Use when                              |
| --------- | -------------------------------------------------- | ------------------------------------- |
| `major`   | one per `X.0` (default)                            | user supports several .NET majors     |
| `minor`   | one per `X.Y`                                      | pinning a specific minor              |
| `feature` | one per `X.Y.Nxx` feature band                     | a preview or a specific band is needed |

## Run it

Run from this skill's directory — the one holding this `SKILL.md` — so the
relative paths resolve.

```bash
python3 scripts/dotnet-versions.py
```

Useful flags: `--band <major|minor|feature>`, `--no-scan`, `--json`,
`--bootstrap`, `--scan-roots <dirs...>`, `--scan-depth <n>`, `--help`.

One-time setup if the uninstall tool is missing:

```bash
python3 scripts/bootstrap-uninstall-tool.py
```

## How to run the workflow

1. **Run the script.** It is the whole analysis — do not re-derive the version
   inventory by hand from `dotnet --info`.
2. **Read the `global.json PINS` section.** Any pin marked `BREAKS` means a
   repo cannot resolve an SDK under the keep list. Do not emit removal commands
   until that is resolved, either by widening `--band` or by telling the user
   which extra version to keep.
3. **Check the cross-check section.** The script runs the tool's own `dry-run`
   and refuses to print commands if the result disagrees with the intended set.
   If it reports `MISMATCH`, stop and investigate — do not hand over commands.
4. **Hand over the commands verbatim** from the `COMMANDS` section. Present them
   in a fenced block, note the reclaimable disk space, and stop. Do not run
   them, do not prefix them with anything, do not "helpfully" simplify them.

Exit codes: `0` fine (including "nothing to remove"), `1` withheld commands
because the tool was missing or the dry-run disagreed.

## Reading the output

- `KEEP (latest)` / `DELETE` — the decision, per version.
- `[tool cannot remove]` — present but not under `/usr/local/share/dotnet`, or
  installed from a zip/script. The tool cannot touch these; the user must delete
  them by hand.
- `reclaimable` — space freed, minus anything the tool cannot reach.
- `global.json PINS` — one line per pin: version, `rollForward` policy,
  `ok`/`BREAKS`, path.

## What the script checks, and why it matters

`dotnet --list-runtimes` prints `Microsoft.NETCore.App` and
`Microsoft.AspNetCore.App` with identical versions. They are deduplicated, or
every runtime looks like a duplicate of itself and the tool would generate a
removal command for a version it is meant to keep.

`global.json` pins matter because `rollForward` does not always cross a major
boundary. A repo pinned to `8.0.408` with `rollForward: latestFeature` resolves
to the newest `8.0.4xx` SDK and nothing else — delete `8.0.425` and that repo
stops building, even though SDK 10 is installed. The script models the
`patch`/`feature`/`minor`/`major` policies (and the `latest*` aliases) and
flags pins the keep list would break.

The generated commands use `--all-but <keep-list>` rather than enumerating
deletions. A whitelist cannot accidentally sweep up a version installed between
the analysis and the run.

## Installing new versions

Out of scope for the script — that is a `dotnet-install.sh` or pkg step, and
it does not need `sudo` if installed to `$HOME/.dotnet`. The user asked about
removing old versions; if they also want to add one, install first, re-run the
script, and hand over the commands for the now-superseded versions.
