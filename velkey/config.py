import json
import os

DEFAULTS = {
    "recent_files": [],
    "last_file": "",
    "last_keyfiles": {},
    "positions": {},          # database path -> {"group": uuid, "entry": uuid}
    "window": [1180, 720],
    "maximized": False,
    "pane_groups": 250,
    "pane_details": 420,
    "show_details": True,
    "show_toolbar": True,
    "clipboard_timeout": 12,
    "back_after_copy": True,
    "lock_idle_minutes": 0,
    "lock_on_screensaver": True,
    "lock_on_minimize": False,
    "close_to_tray": False,
    "minimize_to_tray": False,
    "start_minimized": False,
    "show_tray": True,
    "autotype_delay": 10,
    "autotype_hotkey": "Ctrl+Alt+Shift+Z",
    "autotype_match_url": True,
    "autotype_require_match": True,
    "autotype_password_field_only": True,
    "autotype_paste_unknown": True,   # paste characters the keyboard layout cannot type
    "autotype_always_show_dialog": False,
    "autotype_minimize_after": True,
    "portal_restore_token": "",
    "hide_passwords": True,
    "hide_usernames": False,
    "show_otp_column": True,
    "col_user": True,
    "col_pass": True,
    "col_url": True,
    "col_notes": True,
    "col_mod": False,
    "backup_on_save": True,
    "auto_save": False,
    "autosave_entries": True,
    "open_last": True,
    "accent": "#6c8cff",
    "column_widths": {},
    "sort": ["title", 0],
    "gen": {"length": 20, "upper": True, "lower": True, "digits": True, "minus": False,
            "underline": False, "space": False, "special": True, "brackets": False,
            "exclude_lookalike": False, "custom": ""},
}


class Config:
    def __init__(self):
        base = os.environ.get("XDG_CONFIG_HOME") or os.path.expanduser("~/.config")
        self.dir = os.path.join(base, "velkey")
        self.path = os.path.join(self.dir, "config.json")
        self.data = {}
        src = self.path
        legacy = os.path.join(base, "pass" + "key", "config.json")  # pre-rename settings
        if not os.path.exists(src) and os.path.exists(legacy):
            src = legacy
        try:
            with open(src) as f:
                self.data = json.load(f)
        except (OSError, ValueError):
            self.data = {}

    def get(self, key, default=None):
        if key in self.data:
            return self.data[key]
        if key in DEFAULTS:
            v = DEFAULTS[key]
            return json.loads(json.dumps(v)) if isinstance(v, (dict, list)) else v
        return default

    def set(self, key, value):
        self.data[key] = value
        self.save()

    def save(self):
        try:
            os.makedirs(self.dir, mode=0o700, exist_ok=True)
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(self.data, f, indent=2)
            os.replace(tmp, self.path)
        except OSError:
            pass

    def add_recent(self, path, keyfile=None):
        rec = [p for p in self.get("recent_files") if p != path]
        rec.insert(0, path)
        self.data["recent_files"] = rec[:10]
        self.data["last_file"] = path
        kf = self.get("last_keyfiles")
        if keyfile:
            kf[path] = keyfile
        else:
            kf.pop(path, None)
        self.data["last_keyfiles"] = kf
        self.save()
