#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Independently check a produced Armory archive and optional trusted comment."""

import argparse
import base64
import json
import re
import tarfile
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("archive", type=Path)
    parser.add_argument("tag")
    parser.add_argument("--signature", type=Path)
    args = parser.parse_args()
    if not re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", args.tag):
        raise SystemExit("invalid package tag")
    with tarfile.open(args.archive, "r:gz") as archive:
        members = archive.getmembers()
        names = [member.name for member in members]
        expected = {
            "./extension.json", "./COPYING", "./desktop-library.md",
            "./bin/linux-amd64/libvncdesktop.so",
            "./bin/linux-arm64/libvncdesktop.so",
            "./bin/darwin-amd64/libvncdesktop.dylib",
            "./bin/darwin-arm64/libvncdesktop.dylib",
            "./bin/windows-amd64/vncdesktop.dll",
        }
        if set(names) != expected or len(names) != len(expected):
            raise SystemExit(f"archive path set differs: {names}")
        if not all(member.isfile() and member.size > 0 for member in members):
            raise SystemExit("archive contains an empty or non-file entry")
        manifest_bytes = archive.extractfile("./extension.json").read()
        manifest = json.loads(manifest_bytes)
        command = manifest["commands"]
        if manifest["version"] != args.tag or manifest["package_name"] != "vnc":
            raise SystemExit("manifest version or package name differs")
        if len(command) != 1 or command[0]["command_name"] != "vnc" \
                or command[0]["entrypoint"] != "VNCDesktopCommand":
            raise SystemExit("manifest command differs")
        if {"./" + entry["path"] for entry in command[0]["files"]} != expected - {
                "./extension.json", "./COPYING", "./desktop-library.md"}:
            raise SystemExit("manifest artifact paths differ")

    if args.signature:
        lines = args.signature.read_text().splitlines()
        if len(lines) < 4 or not lines[2].startswith("trusted comment: "):
            raise SystemExit("signature lacks trusted comment")
        trusted = base64.b64decode(lines[2][len("trusted comment: "):], validate=True)
        if trusted != manifest_bytes:
            raise SystemExit("signature trusted comment differs from packaged manifest")
    print("Archive layout, manifest, and trusted comment passed")


if __name__ == "__main__":
    main()
