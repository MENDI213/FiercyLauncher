# FiercyLauncher 1.0.0.4 — beta audit notes

## What was reviewed and changed

- **Release/update versioning:** the client version, local `content/launcher.json`, and beta news are aligned to 1.0.0.4. The minimum version is 1.0.0.3 so existing testers are not locked out while the release is being rolled out. The minimum-version gate is only enforced after the requirement is checked against a real GitHub Release; stale static JSON cannot demand a version newer than the published release.
- **Old updater compatibility:** the application does not pass new command-line switches to the previously published `FiercyUpdater.exe` from v1.0.0.3. The downloaded archive is SHA-256 checked before hand-off; the new Python helper can verify the `.sha256` sidecar again.
- **GitHub network traffic:** release API calls do not follow arbitrary redirects. Static JSON, checksum, banner and update downloads use HTTPS and a restricted GitHub-owned host allowlist. Static JSON responses are limited to 1 MiB; banners are limited to 8 MiB.
- **Release archives and imports:** ZIP validation rejects traversal paths, absolute paths, duplicate case-insensitive paths, symlinks, encrypted entries, reserved Windows names, excessive entry counts, oversized files and unreasonable compression ratios. Modpack paths are independently checked; Modrinth file downloads require manifest hashes; imported overrides use temporary files and atomic replacement.
- **Filesystem protection:** updater and installer reject symlinks/junctions in destination paths and do not overwrite hard-linked files. Local instance imports validate version IDs and inheritance chains, reject symlink roots and cycles, and copy through temporary files. Map imports stage extraction and never replace an existing world.
- **Profile persistence:** changing a profile's Minecraft version or loader is blocked in `ProfileManager`, not only in UI controls. Corrupt profile registries are backed up instead of being silently discarded.
- **Mod installation:** remote file downloads require HTTPS and an allowlisted host, use bounded sizes and atomic temporary files, and verify provided hashes.
- **Packaging:** release source archives exclude local profiles, logs, crash reports, Minecraft saves, runtime files, caches, virtual environments, credentials, tokens, private keys and database files.

## Product changes in this beta

- Sidebar label is **Моды и сборки**.
- New build wizard fixes the Minecraft version and loader type after creation; only the loader version can be changed later.
- Modrinth modpacks can be browsed without an existing profile; selecting one creates a matching profile before import.
- Installed-mod right-click is handled by the context menu rather than forwarding the filename into catalog search.
- The FiercyCity page is intentionally blank. Its user-authored contents belong in `_build_fiercycity_page()` in `launcher_window.py`.
- UI copy was rewritten in Russian; news now appears on the Game page.
- RAM selection uses a detected-memory slider with a safety reserve.
- Discord basic game detection is explained in settings. Custom Discord Rich Presence cannot be created without an Application ID; players can add `FiercyLauncher.exe` in Discord's Registered Games to enable basic “Playing FiercyLauncher” detection.

## Verification performed in this environment

- `python -m compileall -q .` — passed.
- `PYTHONPATH=. pytest -q` — 40 tests passed.
- `python update_helper.py --help` — passed; `--sha256` is optional to remain compatible with previously installed updater executables.
- ZIP release archive integrity and excluded-file scan — to be recorded in the release package generation step.

## Required Windows beta checks (not performed here)

This environment does not provide a Windows desktop, PyQt5 runtime, PyInstaller Windows toolchain or a live Minecraft installation. Before announcing the public beta, test on clean Windows 10/11 x64:

1. Install over a clean folder and over an existing v1.0.0.3 install; verify desktop shortcut opt-in.
2. Update v1.0.0.3 to v1.0.0.4 and allow the old `FiercyUpdater.exe` helper to replace the app files and relaunch it.
3. Install and launch Vanilla, Forge, Fabric and NeoForge 21.1.267 for Minecraft 1.21.1; test NeoForge separately with a fresh game root.
4. Sign in through Microsoft OAuth, launch a licensed account, refresh/clear the session, and check Windows Credential Manager behaviour.
5. Test UI at 100%, 125%, 150% and 200% display scaling and window sizes from 900x600 to 1920x1080.
6. Install and remove Modrinth mods, resource packs, shaders and a full modpack; import a large map and a local installation; verify existing worlds/profiles are unchanged.
7. Test Discord Registered Games detection with Discord Desktop running.
8. Test network failure, cancellation, slow GitHub CDN, incomplete downloads, invalid checksums and corrupted archives.

This report records source-level hardening and automated checks; it is not a claim that all Windows integration tests have passed or that absolute security can be guaranteed.
