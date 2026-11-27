"""Find the URL of the page shown in a browser window.

Browsers on Linux do not put the URL in the window title, and Wayland gives
no way to read another app's address bar. Every mainstream browser however
keeps a live session file listing each tab's title and URL, so the window
title ("Gmail - Helium") can be mapped back to the page URL.

  * Chromium family (Chrome, Chromium, Helium, Brave, Vivaldi, Edge, Opera…):
    <profile>/Sessions/Session_*   (SNSS command log, written ~2.5 s after a
    page loads)
  * Firefox family (Firefox, LibreWolf, Zen, Floorp…):
    <profile>/sessionstore-backups/recovery.jsonlz4   (written every ~15 s)

Only read locally, never stored or sent anywhere.
"""
import glob
import json
import os
import struct
import time

HOME = os.path.expanduser("~")
MAX_AGE = 3 * 24 * 3600      # ignore session files of browsers not used lately
SEPARATORS = (" - ", " — ", " – ", " | ")

_CHROMIUM_GLOBS = [
    "~/.config/*/*/Sessions/Session_*",           # google-chrome/Default, net.imput.helium/Default
    "~/.config/*/*/*/Sessions/Session_*",         # BraveSoftware/Brave-Browser/Default
    "~/.var/app/*/config/*/*/Sessions/Session_*",  # Flatpak
    "~/.var/app/*/config/*/*/*/Sessions/Session_*",
    "~/snap/*/common/*/*/Sessions/Session_*",      # Snap chromium
    "~/snap/*/current/.config/*/*/Sessions/Session_*",
]
_FIREFOX_GLOBS = [
    "~/.mozilla/firefox/*/sessionstore-backups/recovery.jsonlz4",
    "~/.librewolf/*/sessionstore-backups/recovery.jsonlz4",
    "~/.zen/*/sessionstore-backups/recovery.jsonlz4",
    "~/.floorp/*/sessionstore-backups/recovery.jsonlz4",
    "~/.var/app/*/.mozilla/firefox/*/sessionstore-backups/recovery.jsonlz4",
    "~/snap/firefox/common/.mozilla/firefox/*/sessionstore-backups/recovery.jsonlz4",
]

_cache = {}  # path -> (mtime, size, [(title, url), ...] oldest first)


# ------------------------------------------------------------------ parsers
def _chromium_tabs(data):
    """(title, url) of the page each open tab currently shows, from an SNSS log."""
    if data[:4] != b"SNSS":
        return []
    navs = {}      # tab id -> {navigation index: (title, url)}
    selected = {}  # tab id -> current navigation index
    last = {}      # tab id -> navigation index updated last
    pos = 8
    n = len(data)
    while pos + 2 <= n:
        size = struct.unpack_from("<H", data, pos)[0]
        pos += 2
        if size == 0 or pos + size > n:
            break
        cid = data[pos]
        body = data[pos + 1:pos + size]
        pos += size
        try:
            if cid == 7:  # kCommandSetSelectedNavigationIndex {tab, index}
                tab, idx = struct.unpack_from("<ii", body, 0)
                selected[tab] = idx
                continue
            if cid == 16:  # kCommandTabClosed {tab, time}
                tab = struct.unpack_from("<i", body, 0)[0]
                navs.pop(tab, None)
                continue
            if cid != 6:  # kCommandUpdateTabNavigation (pickled)
                continue
            tab, idx = struct.unpack_from("<ii", body, 4)
            p = 4 + 8  # pickle payload size, tab id, navigation index
            ln = struct.unpack_from("<i", body, p)[0]
            p += 4
            url = body[p:p + ln].decode("utf-8", "replace")
            p += (ln + 3) & ~3
            ln = struct.unpack_from("<i", body, p)[0]
            p += 4
            title = body[p:p + 2 * ln].decode("utf-16-le", "replace")
        except (struct.error, ValueError):
            continue
        navs.setdefault(tab, {})[idx] = (title, url)
        last[tab] = idx
    out = []
    for tab, entries in navs.items():
        cur = entries.get(selected.get(tab, last.get(tab)))
        if cur and cur[1].startswith(("http://", "https://")):
            out.append(cur)
    return out


def _lz4_block(src):
    dst = bytearray()
    i, n = 0, len(src)
    while i < n:
        tok = src[i]
        i += 1
        lit = tok >> 4
        if lit == 15:
            while True:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        dst += src[i:i + lit]
        i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        ml = tok & 15
        if ml == 15:
            while True:
                b = src[i]
                i += 1
                ml += b
                if b != 255:
                    break
        ml += 4
        start = len(dst) - off
        if off >= ml:
            dst += dst[start:start + ml]
        else:
            for k in range(ml):
                dst.append(dst[start + k])
    return bytes(dst)


def _firefox_tabs(data):
    if data[:8] != b"mozLz40\0":
        return []
    js = json.loads(_lz4_block(data[12:]))
    out = []
    for w in js.get("windows", []):
        for t in w.get("tabs", []):
            entries = t.get("entries") or []
            idx = int(t.get("index") or len(entries)) - 1
            if 0 <= idx < len(entries):
                e = entries[idx]
                url = e.get("url", "")
                if url.startswith(("http://", "https://")):
                    out.append((e.get("title") or "", url))
    return out


def _tabs(path, parser):
    try:
        st = os.stat(path)
    except OSError:
        return []
    c = _cache.get(path)
    if c and c[0] == st.st_mtime and c[1] == st.st_size:
        return c[2]
    try:
        with open(path, "rb") as f:
            tabs = parser(f.read())
    except Exception:
        tabs = []
    _cache[path] = (st.st_mtime, st.st_size, tabs)
    return tabs


# ------------------------------------------------------------------- lookup
BROWSERS = ("helium", "chrome", "chromium", "brave", "vivaldi", "edge", "opera", "yandex", "thorium",
            "cromite", "ungoogled", "firefox", "librewolf", "zen", "floorp", "waterfox", "mullvad", "tor browser",
            "iceweasel", "icecat")


def _page_titles(window_title):
    """'Gmail - Helium' -> ['Gmail - Helium', 'Gmail']. Only a trailing browser
    name is removed, so 'Login - Evil Site - Helium' never becomes 'Login'."""
    t = (window_title or "").strip()
    out = [t] if t else []
    cut = max((t.rfind(s) for s in SEPARATORS), default=-1)
    if cut > 0 and any(b in t[cut:].lower() for b in BROWSERS):
        out.append(t[:cut].strip())
    return out


def site(url):
    """Registrable domain: https://accounts.google.com/x -> google.com"""
    host = url.split("://", 1)[-1].split("/", 1)[0].split("@")[-1].split(":", 1)[0].lower().strip(".")
    parts = host.split(".")
    if len(parts) >= 3 and parts[-2] in ("co", "com", "org", "net", "ac", "gov", "edu") and len(parts[-1]) == 2:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _session_files():
    now = time.time()
    files = []
    for pattern, parser in [(g, _chromium_tabs) for g in _CHROMIUM_GLOBS] + \
                           [(g, _firefox_tabs) for g in _FIREFOX_GLOBS]:
        for p in glob.glob(os.path.expanduser(pattern)):
            try:
                m = os.path.getmtime(p)
            except OSError:
                continue
            if now - m < MAX_AGE:
                files.append((m, p, parser))
    files.sort(reverse=True)  # most recently written (= the browser in use) first
    return files


def url_for_window(window_title):
    """URL of the page whose title matches the browser window title, or ''."""
    candidates = _page_titles(window_title)
    if not candidates:
        return ""
    for cand in candidates:
        for _m, path, parser in _session_files():  # browser used most recently first
            hits = [url for title, url in _tabs(path, parser) if title == cand]
            if hits:
                # two open tabs with this title on different sites: refuse to guess
                return hits[-1] if len({site(u) for u in hits}) == 1 else ""
    return ""
