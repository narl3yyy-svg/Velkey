# Security Policy

Velkey holds people's passwords, so security reports are taken seriously and handled privately.

## Reporting a vulnerability

**Please do not open a public issue for security problems.**

Use GitHub's private reporting instead: open the **Security** tab of this repository and click **Report a vulnerability**. Only the maintainer can see the report.

Please include:

- what an attacker could do, and what they need first (local access, a malicious `.kdbx` file, a malicious web page…)
- steps to reproduce, using **demo data only**. Never send real passwords or real database files.
- the Velkey version (`velkey --version`), your distribution and desktop (KDE / GNOME, Wayland / X11)

You should get a first reply within 7 days. Fixed issues are credited in the release notes unless you prefer otherwise.

## Supported versions

Only the latest version on the `main` branch receives security fixes.

## How Velkey protects your data

- **Encryption:** databases use the standard KeePass KDBX 4 format (AES-256 or ChaCha20, Argon2 key derivation). The cryptography itself is done by the system's **OpenSSL** library, not custom code. Every file is integrity-checked (HMAC), so a modified or corrupted database is rejected.
- **Nothing leaves your computer:** Velkey has no network code, no telemetry and no auto-update.
- **Clipboard:** copied passwords are wiped after a timeout (12 s by default), and marked as secret so clipboard managers such as Klipper don't keep them in their history.
- **Auto-Type** only types into a window or page that matches the entry: the real page URL when it can be read, never just a look-alike title. Where the app reports its fields, the password only goes into a password box.
- **Locking:** the workspace locks on demand, when idle, when the screen locks, or when minimized (configurable).
- **Saving** writes a new file and swaps it in atomically, so a crash can't leave a half-written database. New databases are readable only by you (`0600`).

## Known limits

- Velkey is written in Python, and Python can't reliably wipe secrets from memory. Someone who can read your session's memory (root, or malware running as you) can get unlocked passwords. That's true of every password manager on a compromised system.
- On X11, any app on the same display can read keystrokes, so Auto-Type text can be observed there. Wayland doesn't have this problem.
- Velkey hasn't had an independent security audit yet.
