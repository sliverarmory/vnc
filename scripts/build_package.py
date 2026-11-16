#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-2.0-or-later
"""Build a vnc Armory archive from one pinned desktop release's verified assets."""

import argparse
import gzip
import hashlib
import io
import json
import re
import stat
import struct
import tarfile
import zipfile
from pathlib import Path, PurePosixPath


ROOT = Path(__file__).resolve().parents[1]
PLATFORMS = {
    "linux-x86_64": ("lib/libvncdesktop.so", "bin/linux-amd64/libvncdesktop.so", "elf-amd64"),
    "linux-arm64": ("lib/libvncdesktop.so", "bin/linux-arm64/libvncdesktop.so", "elf-arm64"),
    "macos-x86_64": ("lib/libvncdesktop.dylib", "bin/darwin-amd64/libvncdesktop.dylib", "macho-amd64"),
    "macos-arm64": ("lib/libvncdesktop.dylib", "bin/darwin-arm64/libvncdesktop.dylib", "macho-arm64"),
    "windows-x86_64": ("bin/vncdesktop.dll", "bin/windows-amd64/vncdesktop.dll", "pe-amd64"),
}


def require(condition, message):
    if not condition:
        raise ValueError(message)


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def read_json(data, label):
    try:
        return json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid {label}: {exc}") from exc


def source_config(path):
    source = read_json(path.read_bytes(), str(path))
    require(source.get("repository") == "sliverarmory/libvncserver", "unexpected source repository")
    require(re.fullmatch(r"desktop-v[0-9]+\.[0-9]+\.[0-9]+", source.get("tag", "")),
            "source tag must be desktop-vX.Y.Z")
    require(re.fullmatch(r"[0-9a-f]{40}", source.get("commit", "")),
            "pin the source release's 40-character commit in source.json")
    require(re.fullmatch(r"[0-9a-f]{64}", source.get("sha256sums_sha256", "")),
            "pin the SHA-256 of source SHA256SUMS in source.json")
    return source


def expected_asset_names(source):
    version = source["tag"].removeprefix("desktop-v")
    return {
        platform: f"vncdesktop-{version}-{platform}" +
        (".zip" if platform.startswith("windows-") else ".tar.gz")
        for platform in PLATFORMS
    }


def verify_release_assets(directory, source):
    names = expected_asset_names(source)
    expected = set(names.values()) | {"release.json", "SHA256SUMS"}
    actual = {path.name for path in directory.iterdir() if path.is_file()}
    require(actual == expected, f"source release asset set differs: missing {expected - actual}, extra {actual - expected}")

    sums = (directory / "SHA256SUMS").read_bytes()
    require(sha256(sums) == source["sha256sums_sha256"], "source SHA256SUMS does not match pinned hash")
    entries = {}
    for line in sums.decode("ascii").splitlines():
        match = re.fullmatch(r"([0-9a-f]{64})  ([^/\\\s]+)", line)
        require(match is not None, f"invalid SHA256SUMS line: {line!r}")
        digest, name = match.groups()
        require(name not in entries, f"duplicate SHA256SUMS entry: {name}")
        entries[name] = digest
    require(set(entries) == expected - {"SHA256SUMS"}, "SHA256SUMS asset list differs")
    for name, digest in entries.items():
        require(sha256((directory / name).read_bytes()) == digest, f"source asset checksum failed: {name}")

    release = read_json((directory / "release.json").read_bytes(), "release.json")
    require(release.get("schema_version") == 1, "unexpected release.json schema")
    require(release.get("tag") == source["tag"], "source release tag mismatch")
    require(release.get("source_commit") == source["commit"], "source release commit mismatch")
    metadata = release.get("assets")
    require(isinstance(metadata, list) and len(metadata) == len(PLATFORMS), "source release asset count mismatch")
    by_platform = {item.get("platform"): item for item in metadata}
    require(set(by_platform) == set(PLATFORMS), "source release platform set mismatch")
    for platform, name in names.items():
        item = by_platform[platform]
        require(item.get("name") == name, f"source release name mismatch for {platform}")
        require(item.get("sha256") == entries[name], f"source release hash mismatch for {platform}")
        require(item.get("size") == (directory / name).stat().st_size,
                f"source release size mismatch for {platform}")
    return names


def archive_files(path, prefix):
    """Read only regular, normalized paths beneath the expected archive root."""
    result = {}
    if path.suffix == ".zip":
        with zipfile.ZipFile(path) as archive:
            for info in archive.infolist():
                if info.is_dir():
                    continue
                mode = info.external_attr >> 16
                require(stat.S_IFMT(mode) != stat.S_IFLNK, f"archive symlink: {info.filename}")
                name = info.filename
                require(not name.endswith("/"), f"invalid archive path: {name}")
                require(name.startswith(prefix + "/"), f"archive path outside root: {name}")
                relative = name[len(prefix) + 1:]
                parts = PurePosixPath(relative).parts
                require(parts and str(PurePosixPath(relative)) == relative and
                        "." not in parts and ".." not in parts and "\\" not in relative,
                        f"unsafe archive path: {name}")
                require(relative not in result, f"duplicate archive file: {relative}")
                result[relative] = archive.read(info)
    else:
        with tarfile.open(path, "r:gz") as archive:
            for member in archive.getmembers():
                require(member.isfile(), f"archive contains non-file: {member.name}")
                name = member.name
                require(name.startswith(prefix + "/"), f"archive path outside root: {name}")
                relative = name[len(prefix) + 1:]
                parts = PurePosixPath(relative).parts
                require(parts and str(PurePosixPath(relative)) == relative and
                        "." not in parts and ".." not in parts and "\\" not in relative,
                        f"unsafe archive path: {name}")
                require(relative not in result, f"duplicate archive file: {relative}")
                result[relative] = archive.extractfile(member).read()
    return result


def verify_binary(data, kind):
    require(len(data) > 1024, f"{kind} library is unexpectedly small")
    if kind.startswith("elf-"):
        require(data[:6] == b"\x7fELF\x02\x01", f"{kind} is not a 64-bit little-endian ELF")
        machine = struct.unpack_from("<H", data, 18)[0]
        require(machine == (62 if kind == "elf-amd64" else 183), f"{kind} machine mismatch")
    elif kind.startswith("macho-"):
        require(data[:4] == b"\xcf\xfa\xed\xfe", f"{kind} is not a little-endian Mach-O")
        machine = struct.unpack_from("<I", data, 4)[0]
        require(machine == (0x01000007 if kind == "macho-amd64" else 0x0100000C),
                f"{kind} CPU type mismatch")
    else:
        require(data[:2] == b"MZ" and len(data) >= 0x40, f"{kind} is not a PE image")
        pe_offset = struct.unpack_from("<I", data, 0x3C)[0]
        require(data[pe_offset:pe_offset + 4] == b"PE\0\0", f"{kind} has no PE signature")
        require(struct.unpack_from("<H", data, pe_offset + 4)[0] == 0x8664,
                f"{kind} machine mismatch")


def stage_files(directory, source, names, tag):
    files = {}
    license_bytes = None
    guide_bytes = None
    for platform, (library_path, destination, binary_kind) in PLATFORMS.items():
        version = source["tag"].removeprefix("desktop-v")
        prefix = f"vncdesktop-{version}-{platform}"
        content = archive_files(directory / names[platform], prefix)
        manifest = read_json(content.pop("manifest.json", b""), f"{platform} manifest.json")
        require(manifest.get("source_commit") == source["commit"], f"{platform} commit mismatch")
        require(manifest.get("platform") == platform, f"{platform} manifest platform mismatch")
        checksums = manifest.get("sha256")
        require(isinstance(checksums, dict) and set(checksums) == set(content),
                f"{platform} archive file list differs from manifest")
        for name, digest in checksums.items():
            require(sha256(content[name]) == digest, f"{platform} installed file checksum failed: {name}")
        require(b"VNCDesktopCommand" in content["include/rfb/desktop.h"],
                f"{platform} header lacks Sliver ABI declaration")
        binary = content[library_path]
        verify_binary(binary, binary_kind)
        files[destination] = binary
        # CPack writes CRLF for Windows text files. Verify the archive's
        # original hashes above, then compare and package canonical LF text.
        current_license = content["share/vncdesktop/COPYING"].replace(b"\r\n", b"\n")
        current_guide = content["share/vncdesktop/desktop-library.md"].replace(b"\r\n", b"\n")
        require(b"\r" not in current_license and b"\r" not in current_guide,
                f"{platform} runtime text contains unsupported line endings")
        if license_bytes is None:
            license_bytes, guide_bytes = current_license, current_guide
        else:
            require(current_license == license_bytes, "source licenses differ across platforms")
            require(current_guide == guide_bytes, "source guides differ across platforms")

    files["COPYING"] = license_bytes
    files["desktop-library.md"] = guide_bytes
    source_manifest = (ROOT / "extension.json").read_bytes()
    require(source_manifest.count(b'"version": "v0.0.0"') == 1, "source manifest version placeholder missing")
    staged = source_manifest.replace(b'"version": "v0.0.0"', f'"version": "{tag}"'.encode())
    manifest = read_json(staged, "staged extension.json")
    require(manifest.get("version") == tag, "staged manifest version differs from package tag")
    declared = {entry["path"] for command in manifest["commands"] for entry in command["files"]}
    require(declared == set(files) - {"COPYING", "desktop-library.md"},
            "staged binaries differ from declared manifest paths")
    files["extension.json"] = staged
    return files


def write_archive(files, output):
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("wb") as raw:
        with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
            with tarfile.open(fileobj=gz, mode="w") as archive:
                for name in sorted(files):
                    data = files[name]
                    info = tarfile.TarInfo("./" + name)
                    info.size = len(data)
                    info.mtime = 0
                    info.mode = 0o644
                    info.uid = info.gid = 0
                    info.uname = info.gname = ""
                    archive.addfile(info, io.BytesIO(data))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--assets", type=Path, required=True, help="Downloaded desktop release assets")
    parser.add_argument("--out", type=Path, required=True, help="Output directory")
    parser.add_argument("--tag", required=True, help="Exact vX.Y.Z package tag")
    parser.add_argument("--source-config", type=Path, default=ROOT / "source.json")
    args = parser.parse_args()
    require(re.fullmatch(r"v[0-9]+\.[0-9]+\.[0-9]+", args.tag), "package tag must be vX.Y.Z")
    source = source_config(args.source_config)
    names = verify_release_assets(args.assets, source)
    files = stage_files(args.assets, source, names, args.tag)
    archive = args.out / "vnc.tar.gz"
    write_archive(files, archive)
    print(f"Built {archive} from {source['repository']} {source['tag']} ({source['commit']})")
    print(f"SHA256 {sha256(archive.read_bytes())}")


if __name__ == "__main__":
    try:
        main()
    except (OSError, ValueError, KeyError, IndexError, UnicodeError, zipfile.BadZipFile,
            tarfile.TarError) as error:
        raise SystemExit(f"package build failed: {error}") from error
