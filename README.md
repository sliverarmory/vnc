# vnc for Sliver Armory

This package exposes the `VNCDesktopCommand` native extension export from
[sliverarmory/libvncserver](https://github.com/sliverarmory/libvncserver) as one
Sliver command:

```text
vnc start --port 1234
vnc status
vnc stop
```

The source and packaged binaries are GPL-2.0-or-later; see `COPYING`.

The port is the exact TCP port, not a VNC display number. The target listener
binds only `127.0.0.1` and has no VNC password or TLS. On an interactive Sliver
session, forward a local port to `127.0.0.1:1234` on the target, then connect a
VNC viewer to the local forwarded port. Stop the server before updating the
package. Windows needs a logged-in interactive desktop; macOS needs Screen
Recording and Accessibility grants; Linux needs an X11 session and XCB/XTEST
runtime libraries. See the packaged `desktop-library.md` for details.

## Provenance and package build

`source.json` pins a published `desktop-vX.Y.Z` release, its exact source commit,
and the SHA-256 of its `SHA256SUMS` file. These two hash fields must be filled
with values verified from the public source release before CI can build.
`scripts/build_package.py` checks all seven source assets, each archive's
per-file manifest, all five platform binaries, and the adapter declaration.
It stages only the Sliver manifest, the five shared libraries, the upstream
GPL license, and the runtime guide.

```sh
gh release download desktop-v0.1.2 --repo sliverarmory/libvncserver --dir source-assets
python3 scripts/build_package.py --assets source-assets --out dist --tag v0.0.1
```

The archive uses `./extension.json` at its root. The source manifest keeps
`v0.0.0`; only the staged copy gets the exact package tag. The release workflow
signs `vnc.tar.gz` with this repository's Minisign key, puts the base64 of the
packaged manifest in the trusted comment, and publishes `vnc.tar.gz` and
`vnc.minisig` together. `minisign.pub` is the public verification key; the
private key is stored in the protected `release` environment secret `MINISIGN_PRIVATE_KEY` and a
durable secret manager, never in Git.

Before publication, CI loads each of the five packaged libraries through
Reflektor's in-memory native loader and calls `VNCDesktopCommand` with `status`
and an invalid port. This checks the package's loader, argument, and callback
ABI without opening a listener. The upstream desktop workflow tests the VNC
protocol and desktop backends separately.

Armory's index reads this repository's latest non-prerelease release. Keep the
asset names and manifest command stable across future releases. The package
version line is independent of the source library's `desktop-v` tag line.
