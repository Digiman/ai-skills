---
name: dotnet-versions
description: Use when the user asks to clean up, prune, or update installed .NET / dotnet SDKs and runtimes, or mentions dotnet uninstall, dotnet-core-uninstall, unsupported or out-of-support .NET versions, which .NET versions are still supported, old .NET versions taking disk space, or "which dotnet versions can I remove". Inventories installed SDKs/runtimes, checks each against Microsoft's published release index for support status and end-of-life, applies a keep-the-latest-per-band policy, checks global.json pins, and prints sudo commands for the user to run manually. Never removes anything itself.
compatibility: macOS on Apple silicon; requires the dotnet CLI on PATH and network access for the support check (falls back to a cache, then to "unknown"). Downloads dotnet-core-uninstall to ~/dotnet-core-uninstall on first run.
license: MIT
metadata:
  version: "0.1.0"
---

# .NET version maintenance

Audit installed .NET SDKs and runtimes, decide what is redundant, and hand the
user commands to run. **Never run a removal.** The uninstall tool needs `sudo`
and a non-interactive agent cannot supply a password, so the last step always
belongs to the human.

No version number is hardcoded here or in the script. What is latest, what is
still supported, and when support ends all come from Microsoft's published
release index, the same feed the download page is rendered from.

## The policy

Two independent rules. The band rule is about redundancy; the support rule is
about whether a version should be running at all.

**Band rule.** Keep exactly one version per band — the newest. Delete the rest.

**Support rule.** A channel Microsoft marks end-of-life gets no pass for being
the newest in its band. Being the only 7.x you have does not make 7.x worth
keeping. But this only ever *suggests* removal, gated behind `--include-eol`,
because dropping a whole channel is the one action that can break a repo the
user never mentioned.

Override the band when the user wants finer granularity:

| `--band`  | Keeps                  | Use when                              |
| --------- | ---------------------- | ------------------------------------- |
| `major`   | one per `X.0` (default) | user supports several .NET majors     |
| `minor`   | one per `X.Y`          | pinning a specific minor              |
| `feature` | one per `X.Y.Nxx` feature band | a preview or a specific band is needed |

## Run it

Run from this skill's directory — the one holding this `SKILL.md` — so the
relative paths resolve.

```bash
python3 scripts/dotnet-versions.py
```

Useful flags: `--band <major|minor|feature>`, `--include-eol`, `--no-scan`,
`--json`, `--bootstrap`, `--offline`, `--refresh`, `--scan-roots <dirs...>`,
`--scan-depth <n>`, `--help`.

One-time setup if the uninstall tool is missing:

```bash
python3 scripts/bootstrap-uninstall-tool.py
```

## How to run the workflow

1. **Run the script.** It is the whole analysis — do not re-derive the version
   inventory by hand from `dotnet --info`, and do not look up support status by
   browsing the download page. The script already read the release index.
2. **Relay the `SUPPORT STATUS` section first**, before any commands. If a
   channel is `UNSUPPORTED`, the user needs to hear that regardless of what the
   band rule decided. Say which versions, when support ended, and that they get
   no security patches.
3. **Do not add `--include-eol` on your own.** It is a suggestion in the output
   for the user to approve. If they ask to remove unsupported versions, re-run
   with the flag and hand over the new commands.
4. **Read the `global.json PINS` section.** Any pin marked `BREAKS` means a
   repo cannot resolve an SDK under the keep list. Do not emit removal commands
   until that is resolved, either by widening `--band` or by telling the user
   which extra version to keep. This matters most for `--include-eol`: removing
   a whole channel is what breaks pins.
5. **Check the cross-check section.** The script runs the tool's own `dry-run`
   and refuses to print commands if the result disagrees with the intended set.
   If it reports `MISMATCH`, stop and investigate — do not hand over commands.
6. **Hand over the commands verbatim** from the `COMMANDS` section. Present them
   in a fenced block, note the reclaimable disk space, and stop. Do not run
   them, do not prefix them with anything, do not "helpfully" simplify them.

Exit codes: `0` fine (including "nothing to remove"), `1` withheld commands
because the tool was missing or the dry-run disagreed.

## Reading the output

- `LATEST RELEASES` — what Microsoft currently publishes per channel, from
  <https://dotnet.microsoft.com/en-us/download/dotnet>. `*` marks channels with
  something installed locally. The rightmost column is how long support has
  left, which is the number to lead with when it is under a year.
- `SUPPORT STATUS` — one row per installed channel, worst first.
  - `UNSUPPORTED` — past end of life. No security patches. Suggest removal.
  - `security-only` — still supported, but only for security fixes. Report the
    days remaining; this is the actionable warning.
  - `supported` — actively supported.
  - `preview` — a pre-release, not for production.
  - `unknown` — the channel was not in the index, or the index could not be
    read. Say the support status is unverified rather than guessing.
- `KEEP (latest)` / `DELETE` — the band decision, per version.
- `[tool cannot remove]` — present but not under `/usr/local/share/dotnet`, or
  installed from a zip/script. The tool cannot touch these; the user must delete
  them by hand.
- `reclaimable` — space freed, minus anything the tool cannot reach.
- `global.json PINS` — one line per pin: version, `rollForward` policy,
  `ok`/`BREAKS`, path.

## What the script checks, and why it matters

**The release index is the single source of truth.** It is fetched from
Microsoft's CDN, cached under `~/.cache/dotnet-versions/` for six hours, and
falls back to a stale cache if the network fails. If it cannot be read at all,
every version reports `unknown` and the run continues — the tool reports what it
knows and does not invent a support verdict. Use `--offline` to force the cache
and `--refresh` to force a re-fetch.

**Support is a property of the channel, not the version.** Microsoft publishes
one `support-phase` per `X.0` channel, so an SDK and a runtime in the same
channel can never disagree about whether it is supported.

**SDK and runtime version schemes are not comparable.** SDKs number as
`X.0.4xx` by feature band, runtimes as `X.0.1x` by patch. Comparing an SDK
against the latest runtime would flag almost everything as stale, so each is
measured against its own scheme. "Behind latest" is also judged on the newest
version installed, not on an older one that is about to be deleted anyway.

**A `go-live` channel whose release carries an `-rc` suffix is a preview**, not
a supported release, and is labelled as such.

`dotnet --list-runtimes` prints `Microsoft.NETCore.App` and
`Microsoft.AspNetCore.App` with identical versions. They are deduplicated, or
every runtime looks like a duplicate of itself and the tool would generate a
removal command for a version it is meant to keep.

`global.json` pins matter because `rollForward` does not always cross a major
boundary. A repo pinned to an older patch in a feature band with
`rollForward: latestFeature` resolves to the newest SDK in that band and
nothing else — delete that SDK and the repo stops building, even though a newer
major is installed. The script models the `patch`/`feature`/`minor`/`major`
policies (and the `latest*` aliases) and flags pins the keep list would break.

The generated commands use `--all-but <keep-list>` rather than enumerating
deletions. A whitelist cannot accidentally sweep up a version installed between
the analysis and the run.

## Installing new versions

Out of scope for the script — that is a `dotnet-install.sh` or pkg step, and
it does not need `sudo` if installed to `$HOME/.dotnet`. The user asked about
removing old versions; if they also want to add one, install first, re-run the
script, and hand over the commands for the now-superseded versions. The
`LATEST RELEASES` section tells them which version is current to install.

## Tests

The decision logic has unit tests, because a machine that is already tidy never
exercises the EOL, delete, or pin-breaking paths. They live at the repo root
rather than here, because this whole folder is what gets installed into a
consumer's harness:

```bash
python3 tests/dotnet-versions/test-decisions.py
```
