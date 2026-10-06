#!/usr/bin/env python3
"""Download the standalone .NET Uninstall Tool (macOS arm64) to ~/dotnet-core-uninstall.

The tool is no longer a NuGet global tool -- `dotnet tool install --global
dotnet-core-uninstall` fails because that package ID on nuget.org is a stale
v1.0.0. Ship it as a binary from the dotnet/cli-lab releases instead.
"""

from __future__ import annotations

import os
import platform
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RELEASE = "https://github.com/dotnet/cli-lab/releases/latest/download"
DEST = Path.home() / "dotnet-core-uninstall"
BIN = DEST / "dotnet-core-uninstall"

ARCH = {"arm64": "osx-arm64", "x86_64": "osx-x64"}
URL = f"{RELEASE}/dotnet-core-uninstall-{ARCH.get(platform.machine(), 'osx-arm64')}.tar.gz"


def main() -> int:
    if platform.system() != "Darwin":
        print("! This tool ships macOS builds only. See https://aka.ms/dotnet-core-uninstall-tool")
        return 1
    if BIN.is_file() and os.access(BIN, os.X_OK):
        print(f"Already installed: {BIN}")
        return 0

    print(f"Downloading {URL}")
    with tempfile.TemporaryDirectory() as td:
        tarball = Path(td) / "tool.tar.gz"
        urllib.request.urlretrieve(URL, tarball)
        DEST.mkdir(parents=True, exist_ok=True)
        with tarfile.open(tarball) as tf:
            tf.extractall(DEST)
    os.chmod(BIN, 0o755)

    print(f"Installed: {BIN}")
    print(f"Try it:    {BIN} list")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
