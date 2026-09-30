#!/usr/bin/env python3
"""Inventory installed .NET SDKs/runtimes, decide what to keep, print sudo commands.

Read-only. Never removes anything. Emits the exact commands for the human to run.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.request
from pathlib import Path

DOTNET_ROOT = Path("/usr/local/share/dotnet")
TOOL_DIR = Path.home() / "dotnet-core-uninstall"
TOOL_BIN = TOOL_DIR / "dotnet-core-uninstall"
RELEASE_URL = (
    "https://github.com/dotnet/cli-lab/releases/latest/download/"
    "dotnet-core-uninstall-osx-arm64.tar.gz"
)
DEFAULT_ROOTS = ["~/Projects", "~/Developer", "~/dev", "~/src", "~/code", "~/source", "~/Documents"]
SKIP_DIRS = {"node_modules", "bin", "obj", ".git", ".vs", ".vscode", "Library", "Applications"}


# ---------- version helpers ----------

def vparse(s: str) -> tuple[int, ...]:
    return tuple(int(p) for p in s.split("."))


def feature_band(v: tuple[int, ...]) -> tuple[int, ...]:
    """8.0.425 -> (8, 0, 4). SDK feature band is the hundreds digit of the patch."""
    return (v[0], v[1], v[2] // 100)


def band_key(v: tuple[int, ...], band: str) -> tuple[int, ...]:
    if band == "major":
        return (v[0],)
    if band == "minor":
        return (v[0], v[1])
    return feature_band(v)


# ---------- dotnet inventory ----------

def run(cmd: list[str]) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=120).stdout


def list_sdks() -> list[tuple[str, str]]:
    out = []
    for line in run(["dotnet", "--list-sdks"]).splitlines():
        m = re.match(r"^(\S+)\s+\[(.+)\]$", line.strip())
        if m:
            out.append((m.group(1), m.group(2)))
    return out


def list_runtimes() -> list[tuple[str, str]]:
    """Deduplicated across shared frameworks: AspNetCore + NETCore ship same versions."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for line in run(["dotnet", "--list-runtimes"]).splitlines():
        m = re.match(r"^(\S+)\s+(\S+)\s+\[(.+)\]$", line.strip())
        if not m:
            continue
        version = m.group(2)
        if version not in seen:
            seen.add(version)
            out.append((version, m.group(3)))
    return out


def dir_size(path: Path) -> int:
    if not path.is_dir():
        return 0
    return int(
        subprocess.run(["du", "-sk", str(path)], capture_output=True, text=True).stdout.split()[0]
    ) * 1024


def disk_size(kind: str, version: str, install_root: str) -> int:
    if kind == "sdk":
        return dir_size(DOTNET_ROOT / "sdk" / version)
    total = 0
    for fw in ("Microsoft.NETCore.App", "Microsoft.AspNetCore.App"):
        total += dir_size(DOTNET_ROOT / "shared" / fw / version)
    return total


def human(n: int) -> str:
    return f"{n / 1024**2:.0f}M" if n < 1024**3 else f"{n / 1024**3:.1f}G"


# ---------- classification ----------

def classify(versions: list[tuple[str, str]], band: str) -> tuple[list, list]:
    """Return (keep, drop). Keep = highest version in each band."""
    groups: dict[tuple, list[tuple[str, str]]] = {}
    for ver, root in versions:
        groups.setdefault(band_key(vparse(ver), band), []).append((ver, root))
    keep, drop = [], []
    for key in sorted(groups):
        members = sorted(groups[key], key=lambda x: vparse(x[0]))
        keep.append(members[-1])
        drop.extend(members[:-1])
    return sorted(keep, key=lambda x: vparse(x[0])), sorted(drop, key=lambda x: vparse(x[0]))


# ---------- global.json constraint check ----------

def find_global_jsons(roots: list[str], max_depth: int) -> list[Path]:
    found = []
    for root in roots:
        base = Path(os.path.expanduser(root))
        if not base.is_dir():
            continue
        base_depth = len(base.parts)
        for dirpath, dirnames, filenames in os.walk(base):
            dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS and not d.startswith(".")]
            if len(Path(dirpath).parts) - base_depth >= max_depth:
                dirnames[:] = []
            if "global.json" in filenames:
                found.append(Path(dirpath) / "global.json")
    return sorted(found)


def satisfies(pin: tuple[int, ...], policy: str, kept: list[tuple[int, ...]]) -> bool:
    """Does any kept SDK satisfy this global.json pin under its rollForward policy?"""
    if policy == "disable":
        return pin in kept
    bands = {feature_band(v) for v in kept}
    for cand in kept:
        if cand < pin:
            continue
        if cand[0] != pin[0]:
            continue
        if policy in ("patch", "latestPatch"):
            ok = feature_band(cand) == feature_band(pin)
        elif policy in ("feature", "latestFeature"):
            ok = cand[1] == pin[1] and feature_band(cand)[2] >= feature_band(pin)[2]
        elif policy in ("minor", "latestMinor"):
            ok = cand[1] >= pin[1]
        else:  # major / latestMajor
            ok = True
        if ok:
            return True
    return False


def check_global_jsons(kept_sdks: list[tuple[str, str]], roots: list[str], max_depth: int) -> list[tuple]:
    kept = [vparse(v) for v, _ in kept_sdks]
    rows = []
    for gj in find_global_jsons(roots, max_depth):
        try:
            data = json.loads(gj.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            rows.append((str(gj), "?", f"parse error: {exc}", "?"))
            continue
        sdk = data.get("sdk") or {}
        ver = sdk.get("version")
        if not ver:
            continue
        policy = sdk.get("rollForward", "patch")
        ok = satisfies(vparse(ver), policy, kept)
        rows.append((str(gj), ver, policy, "ok" if ok else "BREAKS"))
    return rows


# ---------- uninstall tool ----------

def ensure_tool(auto: bool) -> bool:
    if TOOL_BIN.is_file() and os.access(TOOL_BIN, os.X_OK):
        return True
    if not auto:
        print(f"! {TOOL_BIN} not found. Bootstrap it with:\n"
              f"  python3 {Path(__file__).with_name('bootstrap-uninstall-tool.py')}", file=sys.stderr)
        return False
    if not shutil.which("dotnet") or sys.platform != "darwin":
        print("! Cannot auto-download: tool releases are macOS-only.", file=sys.stderr)
        return False
    print(f"  bootstrapping {TOOL_BIN} ...", file=sys.stderr)
    with tempfile.TemporaryDirectory() as td:
        tarball = Path(td) / "tool.tar.gz"
        urllib.request.urlretrieve(RELEASE_URL, tarball)
        TOOL_DIR.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball) as tf:
            tf.extractall(TOOL_DIR)
    os.chmod(TOOL_BIN, 0o755)
    return TOOL_BIN.is_file()


def tool_list() -> tuple[set[str], set[str]]:
    """Parse `dotnet-core-uninstall list` into (removable_sdks, removable_runtimes)."""
    sdk_line = rt_line = ""
    sdks: set[str] = set()
    runtimes: set[str] = set()
    for raw in run([str(TOOL_BIN), "list"]).splitlines():
        line = raw.strip()
        if line.startswith(".NET Core SDKs:"):
            sdk_line, rt_line = line, ""
            continue
        if line.startswith(".NET Core Runtimes:"):
            rt_line, sdk_line = line, ""
            continue
        m = re.match(r"^(\d+\.\d+\.\d+)\s+\(", line)
        if not m:
            continue
        if sdk_line:
            sdks.add(m.group(1))
        elif rt_line:
            runtimes.add(m.group(1))
    return sdks, runtimes


def dry_run(kind: str, keep: list[str]) -> str:
    return run([str(TOOL_BIN), "dry-run", f"--{kind}", "--all-but", *keep])


def parse_dry_run(out: str) -> list[str]:
    m = re.search(r"Specified versions:\n(.*?)\n\*\*\* END", out, re.S)
    if not m:
        return []
    return re.findall(r"(\d+\.\d+\.\d+)", m.group(1))


# ---------- report ----------

def report(kind: str, keep: list, drop: list, removable: set[str] | None) -> None:
    print(f"\n=== {kind.upper()}S ===")
    print(f"  {'version':<12} {'status':<14} {'size':>7}  path")
    reclaim = 0
    for label, items in (("KEEP (latest)", keep), ("DELETE", drop)):
        for ver, root in items:
            size = disk_size(kind, ver, root)
            if label == "DELETE":
                reclaim += size
            if removable is None:
                flag = ""
            else:
                flag = "" if ver in removable else "  [tool cannot remove]"
            print(f"  {ver:<12} {label:<14} {human(size):>7}  {root}{flag}")
    blocked = [] if removable is None else [ver for ver, _ in drop if ver not in removable]
    print(f"  reclaimable: {human(reclaim)}"
          + (f"  (minus {', '.join(blocked)}: tool cannot remove)" if blocked else ""))
    if removable is None:
        print("  (removability unverified: uninstall tool not available)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--band", choices=["major", "minor", "feature"], default="major",
                    help="grouping unit for 'keep the latest' (default: major)")
    ap.add_argument("--scan-roots", nargs="*", default=DEFAULT_ROOTS,
                    help="dirs to scan for global.json")
    ap.add_argument("--scan-depth", type=int, default=6)
    ap.add_argument("--no-scan", action="store_true", help="skip the global.json check")
    ap.add_argument("--bootstrap", action="store_true", help="download the tool if missing")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if not shutil.which("dotnet"):
        print("! dotnet not on PATH", file=sys.stderr)
        return 1

    print("Reading installed versions ...\n  dotnet --list-sdks")
    sdks = list_sdks()
    print("  dotnet --list-runtimes")
    runtimes = list_runtimes()

    keep_sdk, drop_sdk = classify(sdks, args.band)
    keep_rt, drop_rt = classify(runtimes, args.band)

    has_tool = ensure_tool(args.bootstrap)
    rem_sdks, rem_runtimes = tool_list() if has_tool else (None, None)
    sdk_dry = parse_dry_run(dry_run("sdk", [v for v, _ in keep_sdk])) if has_tool and drop_sdk else []
    rt_dry = parse_dry_run(dry_run("runtime", [v for v, _ in keep_rt])) if has_tool and drop_rt else []

    if not args.json:
        print(f"Band policy: keep the newest {args.band} of every band")
        report("sdk", keep_sdk, drop_sdk, rem_sdks)
        report("runtime", keep_rt, drop_rt, rem_runtimes)

        if not args.no_scan:
            rows = check_global_jsons(keep_sdk, args.scan_roots, args.scan_depth)
            print(f"\n=== global.json PINS ({len(rows)} found) ===")
            if not rows:
                print("  none found")
            for path, ver, policy, status in rows:
                print(f"  {ver:<10} rollForward={policy:<14} {status:<7} {path}")
            if any(r[3] == "BREAKS" for r in rows):
                print("\n  ! One or more pins have no satisfying SDK in the keep list.")
                print("    Add the needed band to --band, or keep that version explicitly.")

    if args.json:
        print(json.dumps({
            "band": args.band,
            "sdk": {"keep": [v for v, _ in keep_sdk], "delete": [v for v, _ in drop_sdk]},
            "runtime": {"keep": [v for v, _ in keep_rt], "delete": [v for v, _ in drop_rt]},
            "dryRun": {"sdk": sdk_dry, "runtime": rt_dry},
        }, indent=2))

    if drop_sdk or drop_rt:
        if not has_tool:
            print("\n=== REMOVAL COMMANDS UNAVAILABLE ===")
            print("  The uninstall tool is not installed, so removability is unverified and no")
            print("  commands were generated. Bootstrap it and re-run:")
            print(f"    python3 {Path(__file__).with_name('bootstrap-uninstall-tool.py')}")
            print(f"    python3 {Path(__file__).name} --bootstrap")
            return 1

        mismatched = []
        print("\n=== TOOL DRY-RUN CROSS-CHECK ===")
        for kind, want, got in (("sdk", [v for v, _ in drop_sdk], sdk_dry),
                                ("runtime", [v for v, _ in drop_rt], rt_dry)):
            if not want:
                continue
            if set(got) == set(want):
                print(f"  {kind:<8} tool agrees: removes {', '.join(sorted(got))}")
            else:
                mismatched.append(kind)
                print(f"  {kind:<8} MISMATCH")
                print(f"           intended: {', '.join(want) or '(none)'}")
                print(f"           tool:     {', '.join(got) or '(none)'}")
        if mismatched:
            print(f"\n  ! {', '.join(mismatched)} dry-run did not match the intended set.")
            print("    Commands withheld. Resolve the mismatch, then re-run.")
            return 1

        print("\n=== COMMANDS (run manually; they need sudo) ===")
        if drop_sdk:
            keep = " ".join(v for v, _ in keep_sdk)
            print(f"sudo {TOOL_BIN} dry-run --sdk --all-but {keep}")
            print(f"sudo {TOOL_BIN} remove  --sdk --all-but {keep}")
        if drop_rt:
            keep = " ".join(v for v, _ in keep_rt)
            print(f"sudo {TOOL_BIN} dry-run --runtime --all-but {keep}")
            print(f"sudo {TOOL_BIN} remove  --runtime --all-but {keep}")
        print("\n  Run the dry-run lines first and confirm the output, then the remove lines.")
    else:
        print("\nNothing to remove: exactly one version per band is already installed.")

    return 0



if __name__ == "__main__":
    sys.exit(main())
