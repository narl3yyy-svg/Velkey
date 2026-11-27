# Changelog

Velkey uses [semantic versioning](https://semver.org): `MAJOR.MINOR.PATCH`.
While the major number is 0, Velkey is young and things may still change between minor versions.

The version is set in one place, [`velkey/__init__.py`](velkey/__init__.py). To release a new one: change it there, add a section here, commit, then tag it (`git tag -a v0.2.0 -m "Velkey 0.2.0"` and `git push origin v0.2.0`).

## [0.1.0] – 2026-11-27

First numbered release.

### Databases
- Reads and writes KeePass KDBX 3.1 / 4.x (AES-256 or ChaCha20, AES-KDF / Argon2d / Argon2id), password and/or key file.
- Several databases open at once in tabs. The open tabs come back (locked) on the next start.
- Remembers the group and entry you were on in each database, even if you closed without saving.
- Saves automatically when you add or edit an entry. Deleting an entry, group or custom icon waits for <kbd>Ctrl</kbd>+<kbd>S</kbd>, and <kbd>Ctrl</kbd>+<kbd>Z</kbd> undoes it.
- Export selected entries to a password-protected `.kdbx`, and import them into another database (warns about duplicates). Copy entries between open databases.
- Custom icons: add, delete, remove unused; dark icons get a light tile so they stay visible.

### Auto-Type
- Global hot key <kbd>Ctrl</kbd>+<kbd>Alt</kbd>+<kbd>Shift</kbd>+<kbd>Z</kbd> on KDE, GNOME, X11 and macOS.
- Only types into the window or site that matches the entry: real page URL from browser session files (Chrome-family, Firefox) or Safari, window rules (`https://…` or `??:URL:`), never just a look-alike title.
- The password only goes into a password box where the app reports its fields.

### Everyday use
- TOTP codes, copied with <kbd>Ctrl</kbd>+<kbd>T</kbd>.
- The clipboard is wiped after 12 s and kept out of clipboard-manager history.
- After copying, the window you were using comes back to the front.
- Double-click: URL opens in the browser; user name / password / OTP are copied; title or notes edit the entry.

### Platforms
- Linux: `./run.sh` or `./install.sh`.
- macOS (Apple silicon and Intel): `./runmac.sh` or `./installmac.sh` (installs `Velkey.app`).

[0.1.0]: https://github.com/narl3yyy-svg/Velkey/releases/tag/v0.1.0
