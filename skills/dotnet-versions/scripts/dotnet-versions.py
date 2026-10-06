#!/usr/bin/env python3
"""Inventory installed .NET SDKs/runtimes, decide what to keep, print sudo commands.

Read-only. Never removes anything. Emits the exact commands for the human to run.

Support status comes from Microsoft's published release metadata, never from
version numbers hardcoded here. The index is cached; if it cannot be fetched the
run continues and says so rather than guessing.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import shutil
import subprocess
import sys
import tarfile
import tempfile
import urllib.error
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

# Microsoft publishes the same feed the download page is rendered from. First
# two are official endpoints; the GitHub one is the dotnet/core mirror, used
# only if both CDN hosts are unreachable.
RELEASE_INDEX_URLS = (
    "https://builds.dotnet.microsoft.com/dotnet/release-metadata/releases-index.json",
    "https://dotnetcli.blob.core.windows.net/dotnet/release-metadata/releases-index.json",
    "https://raw.githubusercontent.com/dotnet/core/main/release-notes/releases-index.json",
)
DOWNLOAD_PAGE = "https://dotnet.microsoft.com/en-us/download/dotnet"
CACHE_DIR = Path.home() / ".cache" / "dotnet-versions"
CACHE_FILE = CACHE_DIR / "releases-index.json"
CACHE_MAX_AGE = dt.timedelta(hours=6)
FETCH_TIMEOUT = 20

# support-phase values as published, mapped to what we tell the user.
PHASE_LABEL = {
    "active": "supported",
    "go-live": "supported",
    "preview": "preview",
    "rc": "preview",
    "maintenance": "security-only",
    "eol": "UNSUPPORTED",
}
# Ordered worst-first, so the worst installed phase in a channel wins.
PHASE_SEVERITY = ("eol", "maintenance", "rc", "preview", "go-live", "active")


# ---------- version helpers ----------

def vparse(s: str) -> tuple[int, ...]:
    """'10.0.401' -> (10, 0, 401). Strips pre-release/build suffixes."""
    return tuple(int(p) for p in re.match(r"\d+(?:\.\d+)*", s).group().split("."))


def channel_of(version: str) -> str:
    """The 'X.Y' channel a version belongs to: 10.0.401 -> '10.0'."""
    parts = vparse(version)
    return f"{parts[0]}.{parts[1] if len(parts) > 1 else 0}"


def vstr(v: tuple[int, ...]) -> str:
    return ".".join(str(p) for p in v)


def feature_band(v: tuple[int, ...]) -> tuple[int, ...]:
    """8.0.425 -> (8, 0, 4). SDK feature band is the hundreds digit of the patch."""
    return (v[0], v[1], v[2] // 100)


def band_key(v: tuple[int, ...], band: str) -> tuple[int, ...]:
    if band == "major":
        return (v[0],)
    if band == "minor":
        return (v[0], v[1])
    return feature_band(v)


# ---------- release metadata ----------

def read_cache() -> dict | None:
    try:
        age = dt.datetime.now(dt.timezone.utc) - dt.datetime.fromtimestamp(
            CACHE_FILE.stat().st_mtime, dt.timezone.utc
        )
    except OSError:
        return None
    if age > CACHE_MAX_AGE:
        return None
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def write_cache(data: dict) -> None:
    try:
        CACHE_DIR.mkdir(parents=True, exist_ok=True)
        CACHE_FILE.write_text(json.dumps(data))
    except OSError:
        pass  # a cache we cannot write is not an error


def fetch_index(offline: bool, refresh: bool) -> tuple[dict | None, str]:
    """Return (index, source). index is None when unavailable, with a reason."""
    if offline:
        return read_cache(), "offline: no network access requested"
    if not refresh and (cached := read_cache()):
        return cached, f"cache ({CACHE_FILE})"

    errors = []
    for url in RELEASE_INDEX_URLS:
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "dotnet-versions-skill"})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            if "releases-index" in data:
                write_cache(data)
                return data, url
            errors.append(f"{url}: no 'releases-index' key")
        except (urllib.error.URLError, OSError, ValueError, json.JSONDecodeError) as exc:
            errors.append(f"{url}: {exc}")

    # Network down: a stale cache still beats nothing, but say how old.
    if cached := read_cache_force():
        return cached, f"stale cache (network failed: {errors[0]})"
    return None, f"release metadata unavailable ({errors[0]})"


def read_cache_force() -> dict | None:
    """Cache regardless of age. Only for when the network already failed."""
    try:
        return json.loads(CACHE_FILE.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def index_by_channel(index: dict) -> dict[str, dict]:
    out = {}
    for entry in index.get("releases-index", []):
        channel = entry.get("channel-version")
        if channel:
            out[channel] = entry
    return out


def phase_of(entry: dict) -> str:
    return (entry.get("support-phase") or "").lower()


def days_until_eol(entry: dict) -> int | None:
    raw = entry.get("eol-date")
    if not raw:
        return None
    try:
        eol = dt.date.fromisoformat(raw)
    except ValueError:
        return None
    return (eol - dt.datetime.now(dt.timezone.utc).date()).days


def is_prerelease(s: str | None) -> bool:
    return bool(s) and bool(re.search(r"-(rc|preview|alpha|beta)\b", s, re.I))


def channel_status(entry: dict) -> dict:
    """Everything the report needs to describe one channel, in one dict."""
    phase = phase_of(entry)
    latest_release = entry.get("latest-release")
    label = PHASE_LABEL.get(phase, phase or "unknown")
    if label == "supported" and is_prerelease(latest_release):
        label = "preview"
    return {
        "channel": entry.get("channel-version", "?"),
        "label": label,
        "phase": phase,
        "release_type": entry.get("release-type", "?"),
        "latest_release": latest_release,
        # SDK and runtime version schemes differ (X.0.4xx vs X.0.1x), so a
        # comparison is only ever valid within one of them.
        "latest_sdk": (entry.get("latest-sdk") or "").split("-")[0] or None,
        "latest_runtime": (entry.get("latest-runtime") or "").split("-")[0] or None,
        "eol_date": entry.get("eol-date"),
        "eol_in_days": days_until_eol(entry),
        "security": entry.get("security"),
    }


def support_status(version: str, channels: dict[str, dict], kind: str = "sdk") -> dict:
    """Classify an installed version against the published feed.

    `kind` selects which version scheme to compare against, so an SDK is never
    measured against a runtime patch number.
    """
    entry = channels.get(channel_of(version))
    if entry is None:
        return {"label": "unknown", "channel": channel_of(version)}
    status = channel_status(entry)
    latest = status["latest_sdk"] if kind == "sdk" else status["latest_runtime"]
    status["behind_latest"] = bool(latest) and vparse(version) < vparse(latest)
    return status


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


def apply_eol(keep: list, drop: list, eol_channels: set[str]) -> tuple[list, list]:
    """Move every version in an EOL channel out of keep and into drop.

    Being newest in its band does not earn an unsupported version a pass. The
    whole channel goes, so the band is left empty rather than half-removed.
    """
    if not eol_channels:
        return keep, drop
    new_keep = [kv for kv in keep if channel_of(kv[0]) not in eol_channels]
    moved = [kv for kv in keep if channel_of(kv[0]) in eol_channels]
    # drop may already hold older versions from these channels; every one of
    # them has to survive, or it is left installed and unreported.
    merged = {v: (v, r) for v, r in drop}
    merged.update({v: (v, r) for v, r in moved})
    return new_keep, sorted(merged.values(), key=lambda x: vparse(x[0]))


def eol_channels_of(channels: dict[str, dict], installed: list) -> set[str]:
    """Channels that are EOL *and* actually have something installed."""
    in_use = {channel_of(v) for v, _ in installed}
    return {ch for ch in in_use if phase_of(channels.get(ch, {})) == "eol"}


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

def report(kind: str, keep: list, drop: list, removable: set[str] | None,
          channels: dict[str, dict]) -> None:
    print(f"\n=== {kind.upper()}S ===")
    print(f"  {'version':<12} {'status':<14} {'support':<13} {'size':>7}  path")
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
            support = support_status(ver, channels, kind)["label"]
            print(f"  {ver:<12} {label:<14} {support:<13} {human(size):>7}  {root}{flag}")
    blocked = [] if removable is None else [ver for ver, _ in drop if ver not in removable]
    print(f"  reclaimable: {human(reclaim)}"
          + (f"  (minus {', '.join(blocked)}: tool cannot remove)" if blocked else ""))
    if removable is None:
        print("  (removability unverified: uninstall tool not available)")


def report_latest(channels: dict[str, dict], installed: list) -> None:
    """What Microsoft currently publishes, newest channel first."""
    order = sorted(channels.values(),
                   key=lambda e: vparse(e.get("channel-version", "0.0")), reverse=True)
    # Only channels someone actually has installed, plus the current GA LTS/STS.
    in_use = {channel_of(v) for v, _, _ in installed}
    shown = [e for e in order
             if channel_of(e.get("channel-version", "0.0")) in in_use
             or phase_of(e) in ("active", "maintenance", "go-live")]
    if not shown:
        return

    print(f"\n=== LATEST RELEASES ({DOWNLOAD_PAGE}) ===")
    print(f"  {'channel':<8} {'type':<5} {'latest':<12} {'support':<13} {'eol':<12} support ends in")
    for entry in shown[:8]:
        st = channel_status(entry)
        installed_here = st["channel"] in in_use
        marker = "*" if installed_here else " "
        eol = st["eol_date"] or "-"
        if st["eol_in_days"] is None:
            countdown = "-"
        elif st["eol_in_days"] < 0:
            countdown = f"passed {abs(st['eol_in_days'])}d ago"
        elif st["eol_in_days"] < 90:
            countdown = f"{st['eol_in_days']}d"
        else:
            countdown = f"{st['eol_in_days'] // 30}mo"
        # latest-release keeps the rc/preview suffix, so a go-live channel
        # reads as a pre-release instead of looking like a shipped version.
        latest = st["latest_release"] or "-"
        print(f" {marker}{st['channel']:<7} {st['release_type']:<5} {latest:<12} "
              f"{st['label']:<13} {eol:<12} {countdown}")
    if any(channel_of(e.get("channel-version", "0.0")) in in_use for e in order):
        print("  * installed here")


def report_support_warnings(installed: list[tuple[str, str, str]],
                            channels: dict[str, dict],
                            unavailable: bool) -> list[dict]:
    """One row per channel that has something installed, worst support first.

    `installed` is (version, root, kind) triples so an SDK is only ever compared
    against the channel's latest SDK and a runtime against its latest runtime.
    """
    if unavailable:
        print("\n=== SUPPORT STATUS ===")
        print("  ! Could not read Microsoft's release index, so no version was checked")
        print(f"    for support status. Re-run without --offline, or see {DOWNLOAD_PAGE}.")
        return []

    rows: dict[str, dict] = {}
    for ver, root, kind in installed:
        ch = channel_of(ver)
        st = support_status(ver, channels, kind)
        row = rows.get(ch)
        if row is None:
            row = rows[ch] = {"channel": ch, "label": st["label"], "phase": st.get("phase", ""),
                              "eol_date": st["eol_date"],
                              "eol_in_days": st["eol_in_days"], "release_type": st["release_type"],
                              "versions": [], "sdk_versions": [], "runtime_versions": [],
                              "notes": set(), "security": st.get("security")}
        # Worst label in the channel wins; support is a property of the channel,
        # so the SDK and the runtime rows never disagree.
        cur = PHASE_SEVERITY.index(row["phase"]) if row["phase"] in PHASE_SEVERITY else len(PHASE_SEVERITY)
        new = PHASE_SEVERITY.index(st["phase"]) if st.get("phase") in PHASE_SEVERITY else len(PHASE_SEVERITY)
        if new < cur:
            row.update(label=st["label"], phase=st.get("phase", ""))
        row["versions"].append(ver)
        row[f"{kind}_versions"].append(ver)

    # Behind-latest is a per-kind question about the newest thing installed:
    # an old patch sitting in the channel is not "behind" if the current one is
    # also there and about to be deleted.
    for row in rows.values():
        for kind in ("sdk", "runtime"):
            installed_here = row[f"{kind}_versions"]
            if not installed_here:
                continue
            newest = max(installed_here, key=vparse)
            st = support_status(newest, channels, kind)
            if st.get("behind_latest"):
                behind = st["latest_sdk"] if kind == "sdk" else st["latest_runtime"]
                row["notes"].add(f"{kind} behind {behind}")
        row["notes"] = sorted(row["notes"])

    if not rows:
        return []

    def sort_key(r: dict) -> tuple:
        label = r["label"]
        rank = ({"UNSUPPORTED": 0, "security-only": 1}.get(label, 2),
                r["eol_in_days"] if r["eol_in_days"] is not None else 9999)
        return rank

    ordered = sorted(rows.values(), key=sort_key)
    print("\n=== SUPPORT STATUS ===")
    print(f"  {'channel':<8} {'support':<13} {'eol':<12} {'installed':<22} notes")
    for r in ordered:
        days = r["eol_in_days"]
        if r["label"] == "UNSUPPORTED":
            note = f"out of support since {r['eol_date']}"
        elif r["label"] == "security-only":
            note = (f"security patches only, {days}d left" if days is not None and days < 90
                    else "security patches only")
            if days is not None and days < 0:
                note = "past EOL date, not yet marked eol upstream"
        elif r["label"] == "preview":
            note = "pre-release, not for production"
        elif r["label"] == "unknown":
            note = "channel not in the published index"
        else:
            note = "fully supported"
        if r.get("security") is False:
            note += "; upstream marks this channel as getting no security fixes"
        for extra in r["notes"]:
            note += f"; {extra}"
        installed_col = ", ".join(r["versions"])
        print(f"  {r['channel']:<8} {r['label']:<13} {r['eol_date'] or '-':<12} "
              f"{installed_col:<22} {note}")
    return ordered



def suggest_eol_removal(rows: list[dict], already_dropped: set[str]) -> None:
    """Warn about unsupported channels and say what re-running would do."""
    unsupported = [r for r in rows if r["label"] == "UNSUPPORTED"]
    if not unsupported:
        return
    kind = "channels" if len(unsupported) > 1 else "channel"
    verb = "receive" if len(unsupported) > 1 else "receives"
    print(f"\n  ! {len(unsupported)} installed {kind} no longer {verb} security updates:")
    for r in unsupported:
        print(f"    {r['channel']:<8} out of support since {r['eol_date']}  "
              f"({', '.join(r['versions'])})")
    # already_dropped is what this run is already going to remove, so the
    # suggestion only appears for versions that would survive without the flag.
    new = [v for r in unsupported for v in r["versions"] if v not in already_dropped]
    if new:
        size = sum(
            sum(disk_size(kind, v, "") for v in r[f"{kind}_versions"] if v not in already_dropped)
            for r in unsupported for kind in ("sdk", "runtime"))
        print(f"\n    Re-run with --include-eol to also drop {'it' if len(new) == 1 else 'them'}"
              f" (~{human(size)} more).")
    print("    Read the global.json pins below first: a repo pinned to one of these")
    print("    channels stops resolving an SDK once the whole band goes.")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--band", choices=["major", "minor", "feature"], default="major",
                    help="grouping unit for 'keep the latest' (default: major)")
    ap.add_argument("--scan-roots", nargs="*", default=DEFAULT_ROOTS,
                    help="dirs to scan for global.json")
    ap.add_argument("--scan-depth", type=int, default=6)
    ap.add_argument("--no-scan", action="store_true", help="skip the global.json check")
    ap.add_argument("--bootstrap", action="store_true", help="download the tool if missing")
    ap.add_argument("--include-eol", action="store_true",
                    help="also drop installed versions whose channel is out of support")
    ap.add_argument("--offline", action="store_true",
                    help="use only the cached release metadata, never fetch")
    ap.add_argument("--refresh", action="store_true",
                    help="ignore the cache and re-fetch release metadata")
    ap.add_argument("--json", action="store_true", help="machine-readable output")
    args = ap.parse_args()

    if not shutil.which("dotnet"):
        print("! dotnet not on PATH", file=sys.stderr)
        return 1

    print("Reading installed versions ...\n  dotnet --list-sdks")
    sdks = list_sdks()
    print("  dotnet --list-runtimes")
    runtimes = list_runtimes()
    if not sdks and not runtimes:
        print("\nNo .NET SDKs or runtimes reported by the dotnet CLI.", file=sys.stderr)
        return 1

    print("  fetching release metadata ...")
    index, source = fetch_index(args.offline, args.refresh)
    channels = index_by_channel(index) if index else {}
    unavailable = not channels
    if not args.json:
        print(f"  support data: {source}")

    keep_sdk, drop_sdk = classify(sdks, args.band)
    keep_rt, drop_rt = classify(runtimes, args.band)

    eol = eol_channels_of(channels, sdks + runtimes) if not unavailable else set()
    eol_actioned = bool(eol) and args.include_eol
    if eol_actioned:
        keep_sdk, drop_sdk = apply_eol(keep_sdk, drop_sdk, eol)
        keep_rt, drop_rt = apply_eol(keep_rt, drop_rt, eol)

    has_tool = ensure_tool(args.bootstrap)
    rem_sdks, rem_runtimes = tool_list() if has_tool else (None, None)
    sdk_dry = parse_dry_run(dry_run("sdk", [v for v, _ in keep_sdk])) if has_tool and drop_sdk else []
    rt_dry = parse_dry_run(dry_run("runtime", [v for v, _ in keep_rt])) if has_tool and drop_rt else []

    installed_triples = ([(v, r, "sdk") for v, r in sdks]
                         + [(v, r, "runtime") for v, r in runtimes])

    support_rows: list[dict] = []
    if not args.json:
        policy = f"keep the newest {args.band} of every band"
        if eol_actioned:
            policy += f"; also drop out-of-support channels: {', '.join(sorted(eol))}"
        print(f"\nPolicy: {policy}")
        report_latest(channels, installed_triples)
        support_rows = report_support_warnings(installed_triples, channels, unavailable)
        # Only what this run already deletes; anything else that is EOL survives
        # and is exactly what --include-eol would add.
        dropping = {v for v, _ in drop_sdk} | {v for v, _ in drop_rt}
        suggest_eol_removal(support_rows, dropping)
        report("sdk", keep_sdk, drop_sdk, rem_sdks, channels)
        report("runtime", keep_rt, drop_rt, rem_runtimes, channels)

        if not args.no_scan:
            rows = check_global_jsons(keep_sdk, args.scan_roots, args.scan_depth)
            print(f"\n=== global.json PINS ({len(rows)} found) ===")
            if not rows:
                print("  none found")
            for path, ver, policy_name, status in rows:
                print(f"  {ver:<10} rollForward={policy_name:<14} {status:<7} {path}")
            if any(r[3] == "BREAKS" for r in rows):
                print("\n  ! One or more pins have no satisfying SDK in the keep list.")
                print("    Add the needed band to --band, or keep that version explicitly.")

    if args.json:
        print(json.dumps({
            "band": args.band,
            "includeEol": args.include_eol,
            "supportSource": source,
            "supportUnavailable": unavailable,
            "eolChannels": sorted(eol),
            "sdk": {"keep": [v for v, _ in keep_sdk], "delete": [v for v, _ in drop_sdk]},
            "runtime": {"keep": [v for v, _ in keep_rt], "delete": [v for v, _ in drop_rt]},
            "dryRun": {"sdk": sdk_dry, "runtime": rt_dry},
            "support": support_rows or ([{"version": v, "kind": k, **support_status(v, channels, k)}
                                        for v, _, k in installed_triples] if not unavailable else []),
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
    elif not args.json:
        print("\nNothing to remove under this policy.")
        if support_rows and not any(r["label"] == "UNSUPPORTED" for r in support_rows):
            print("Every installed version is still within its channel's support window.")

    return 0


if __name__ == "__main__":
    sys.exit(main())
