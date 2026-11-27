"""Dark theme (GTK3 CSS on top of Adwaita-dark) and KeePass icon mapping."""
from gi.repository import Gdk, GdkPixbuf, Gtk

CSS = """
@define-color pk_bg #101216;
@define-color pk_surface #15181d;
@define-color pk_raised #1b1f26;
@define-color pk_hover #222731;
@define-color pk_border #262b35;
@define-color pk_text #e3e6ed;
@define-color pk_dim #8a92a3;
@define-color pk_accent ACCENT;
@define-color pk_danger #ff6b6b;
@define-color theme_selected_bg_color ACCENT;
@define-color accent_bg_color ACCENT;
@define-color accent_color ACCENT;

* { outline-color: alpha(@pk_accent, 0.5); }
window, .background, dialog, messagedialog, filechooser { background-color: @pk_bg; color: @pk_text; }
label { color: @pk_text; }
label.dim, .dim { color: @pk_dim; }
label.key { color: @pk_dim; font-size: 0.92em; }
label.section { color: @pk_dim; font-weight: 700; font-size: 0.78em; letter-spacing: 1px; }
label.title-big { font-size: 1.9em; font-weight: 800; letter-spacing: -0.5px; }
label.error { color: @pk_danger; }
label.otp-code { font-family: monospace; font-weight: 700; font-size: 1.15em; color: @pk_accent; letter-spacing: 2px; }
label.mono { font-family: monospace; }

menubar { background: @pk_raised; border-bottom: 1px solid @pk_border; padding: 0 4px; box-shadow: none; }
menubar > menuitem { padding: 5px 10px; color: @pk_text; border-radius: 6px; margin: 3px 0; }
menubar > menuitem:hover { background: @pk_hover; box-shadow: none; color: @pk_text; }
menu, .menu, .context-menu { background: @pk_raised; border: 1px solid @pk_border; padding: 5px; border-radius: 10px; }
menu menuitem { padding: 6px 12px; border-radius: 6px; color: @pk_text; min-height: 18px; }
menu menuitem:hover { background: @pk_accent; color: #ffffff; }
menu menuitem:hover label, menu menuitem:hover accelerator { color: #ffffff; }
menu menuitem:disabled label { color: alpha(@pk_text, 0.35); }
menu menuitem accelerator { color: @pk_dim; margin-left: 24px; }
menu separator { background: @pk_border; margin: 4px 6px; min-height: 1px; }
menu check, menu radio { margin-right: 8px; }

.pk-toolbar { background: @pk_raised; border-bottom: 1px solid @pk_border; padding: 6px 8px; }
.pk-toolbar button { background: none; border: none; box-shadow: none; border-radius: 8px; padding: 6px 8px; color: @pk_text; min-height: 22px; }
.pk-toolbar button:hover { background: @pk_hover; }
.pk-toolbar button:active { background: alpha(@pk_accent, 0.25); }
.pk-toolbar button:disabled image { color: alpha(@pk_text, 0.25); }
.pk-toolbar .tb-sep { background: @pk_border; min-width: 1px; margin: 6px 6px; }

entry, spinbutton { background: @pk_bg; border: 1px solid @pk_border; border-radius: 8px; color: @pk_text; padding: 6px 10px; box-shadow: none; caret-color: @pk_accent; min-height: 22px; }
entry:focus, spinbutton:focus { border-color: @pk_accent; box-shadow: 0 0 0 2px alpha(@pk_accent, 0.22); }
entry image { color: @pk_dim; }
entry image:hover { color: @pk_text; }
entry selection, textview text selection, label selection { background: alpha(@pk_accent, 0.55); color: #fff; }
spinbutton entry { border: none; box-shadow: none; background: transparent; }
spinbutton button { border: none; background: transparent; box-shadow: none; color: @pk_dim; }
.pk-toolbar entry.search { min-width: 260px; border-radius: 18px; padding-left: 12px; background: @pk_bg; }

textview, textview text { background: @pk_bg; color: @pk_text; caret-color: @pk_accent; }
.textframe { border: 1px solid @pk_border; border-radius: 8px; background: @pk_bg; padding: 6px; }

button { background: @pk_raised; border: 1px solid @pk_border; border-radius: 8px; color: @pk_text; box-shadow: none; text-shadow: none; padding: 6px 14px; -gtk-icon-shadow: none; }
button:hover { background: @pk_hover; }
button:active, button:checked { background: alpha(@pk_accent, 0.25); }
button:disabled { color: alpha(@pk_text, 0.35); background: @pk_raised; }
button.suggested-action { background: @pk_accent; border-color: @pk_accent; color: #ffffff; font-weight: 700; }
button.suggested-action:hover { background: shade(@pk_accent, 1.1); }
button.suggested-action:disabled { background: alpha(@pk_accent, 0.4); color: alpha(#fff, 0.6); }
button.destructive-action { background: @pk_danger; border-color: @pk_danger; color: #fff; font-weight: 700; }
button.flat, button.link { background: none; border: none; }
button.flat:hover { background: @pk_hover; }
button.recent { background: none; border: none; padding: 8px 12px; border-radius: 10px; }
button.recent:hover { background: @pk_hover; }
button.icon-pick { padding: 6px; }
combobox button { padding: 4px 10px; }

treeview.view { background-color: @pk_surface; color: @pk_text; }
treeview.view:hover { background-color: @pk_hover; }
treeview.view:selected { background-color: alpha(@pk_accent, 0.32); color: #ffffff; }
treeview.view:selected:focus { background-color: alpha(@pk_accent, 0.45); }
treeview.view.expander { color: @pk_dim; }
treeview.view.expander:hover { color: @pk_text; }
treeview.view.dnd { border: 1px solid @pk_accent; }
treeview header button { background: @pk_raised; color: @pk_dim; border: none; border-bottom: 1px solid @pk_border; border-right: 1px solid @pk_border; border-radius: 0; padding: 5px 10px; font-weight: 600; box-shadow: none; }
treeview header button:hover { color: @pk_text; background: @pk_hover; }
treeview.view.groups { padding: 4px 0; }
scrolledwindow { background: @pk_surface; }
scrolledwindow.plain { background: transparent; }
scrolledwindow undershoot, scrolledwindow overshoot { background: none; }

paned > separator { background: @pk_border; min-width: 1px; min-height: 1px; background-image: none; }
scrollbar { background: transparent; border: none; }
scrollbar slider { background: alpha(@pk_text, 0.18); border-radius: 10px; min-width: 7px; min-height: 7px; border: 2px solid transparent; }
scrollbar slider:hover { background: alpha(@pk_text, 0.32); }

.details { background: @pk_surface; padding: 14px 18px; }
.details label.value { color: @pk_text; }
.details label.heading { font-size: 1.25em; font-weight: 700; }
.statusbar { background: @pk_raised; border-top: 1px solid @pk_border; padding: 4px 12px; }
.statusbar label { color: @pk_dim; font-size: 0.9em; }

.unlock-card { background: @pk_surface; border: 1px solid @pk_border; border-radius: 18px; padding: 34px 36px; }
.welcome-bg { background: @pk_bg; }

notebook { background: @pk_bg; }
notebook > header { background: @pk_raised; border: none; border-bottom: 1px solid @pk_border; padding: 0 8px; }
notebook > header tab { padding: 9px 14px; border: none; color: @pk_dim; background: none; box-shadow: none; min-height: 0; }
notebook > header tab:hover { color: @pk_text; box-shadow: inset 0 -2px alpha(@pk_text, 0.2); }
notebook > header tab:checked { color: @pk_text; box-shadow: inset 0 -2px @pk_accent; }
notebook > header tab label { color: inherit; font-weight: 600; }
notebook > stack:not(:only-child) { background: @pk_bg; }

frame > border, .frame { border: 1px solid @pk_border; border-radius: 8px; }
list, listbox { background: @pk_surface; color: @pk_text; }
row:selected { background: alpha(@pk_accent, 0.35); }
row:hover { background: @pk_hover; }
flowbox flowboxchild { padding: 4px; border-radius: 6px; }
flowbox flowboxchild:selected { background: alpha(@pk_accent, 0.45); }
tooltip, tooltip.background { background: @pk_raised; border: 1px solid @pk_border; border-radius: 8px; color: @pk_text; }
tooltip label { color: @pk_text; }
levelbar trough { background: @pk_raised; border: none; border-radius: 4px; min-height: 6px; padding: 0; }
levelbar block { border-radius: 4px; min-height: 6px; border: none; }
levelbar block.filled.low { background: #ff6b6b; }
levelbar block.filled.high { background: #f5c451; }
levelbar block.filled.full { background: #3ddc97; }
levelbar block.empty { background: transparent; }
progressbar trough { background: @pk_raised; border: none; min-height: 3px; border-radius: 3px; }
progressbar progress { background: @pk_accent; border: none; min-height: 3px; border-radius: 3px; }
check, radio { background: @pk_bg; border: 1px solid @pk_border; color: #ffffff; }
check:checked, radio:checked { background: @pk_accent; border-color: @pk_accent; }
switch { background: @pk_raised; border: 1px solid @pk_border; }
switch:checked { background: @pk_accent; border-color: @pk_accent; }
switch slider { background: @pk_text; border: none; }
scale trough { background: @pk_raised; border: none; min-height: 4px; }
scale highlight { background: @pk_accent; border: none; }
scale slider { background: @pk_text; border: none; min-width: 14px; min-height: 14px; }
separator { background: @pk_border; }
infobar, .infobar { border-radius: 8px; }
popover, popover.background { background: @pk_raised; border: 1px solid @pk_border; border-radius: 10px; }
.dialog-action-area, dialog .dialog-action-box { padding: 10px 14px 14px 14px; }
dialog .dialog-vbox { padding: 0; }
.chip { background: alpha(@pk_accent, 0.18); color: @pk_text; border-radius: 10px; padding: 1px 8px; font-size: 0.85em; }
"""

_provider = None


def apply(accent="#6c8cff"):
    global _provider
    settings = Gtk.Settings.get_default()
    settings.set_property("gtk-theme-name", "Adwaita")
    settings.set_property("gtk-application-prefer-dark-theme", True)
    screen = Gdk.Screen.get_default()
    if _provider is not None:
        Gtk.StyleContext.remove_provider_for_screen(screen, _provider)
    _provider = Gtk.CssProvider()
    _provider.load_from_data(CSS.replace("ACCENT", accent).encode())
    Gtk.StyleContext.add_provider_for_screen(screen, _provider, Gtk.STYLE_PROVIDER_PRIORITY_USER)


# ---------------------------------------------------------------------------
# KeePass standard icons (PwIcon enum, 0..68) -> freedesktop icon names
# ---------------------------------------------------------------------------
KP_ICONS = [
    ("Key", ["dialog-password", "password-copy", "kwalletmanager"]),
    ("World", ["applications-internet", "internet-web-browser", "network-workgroup"]),
    ("Warning", ["dialog-warning"]),
    ("Network Server", ["network-server", "server-database"]),
    ("Marked Directory", ["folder-important", "folder-favorites", "folder"]),
    ("User Communication", ["user-available", "im-user", "system-users"]),
    ("Parts", ["applications-other", "application-x-executable"]),
    ("Notepad", ["accessories-text-editor", "text-x-generic"]),
    ("World Socket", ["network-wired", "network-workgroup"]),
    ("Identity", ["contact-new", "x-office-address-book", "avatar-default"]),
    ("Paper Ready", ["document-send", "mail-send"]),
    ("Digicam", ["camera-photo", "digikam"]),
    ("IR Communication", ["network-wireless", "bluetooth"]),
    ("Multi Keys", ["dialog-password", "password-copy"]),
    ("Energy", ["battery", "battery-full", "preferences-system-power"]),
    ("Scanner", ["scanner", "document-scan"]),
    ("World Star", ["starred", "emblem-favorite", "bookmarks"]),
    ("CD-ROM", ["media-optical", "drive-optical"]),
    ("Monitor", ["video-display", "computer"]),
    ("E-Mail", ["internet-mail", "mail-unread", "mail-message"]),
    ("Configuration", ["preferences-system", "configure", "applications-system"]),
    ("Clipboard Ready", ["edit-paste", "klipper"]),
    ("Paper New", ["document-new"]),
    ("Screen", ["video-display", "preferences-desktop-display"]),
    ("Energy Careful", ["battery-caution", "battery-low"]),
    ("E-Mail Box", ["mail-send-receive", "mail-folder-inbox"]),
    ("Disk", ["media-floppy", "drive-harddisk"]),
    ("Drive", ["drive-harddisk"]),
    ("Paper Q", ["help-browser", "help-contents"]),
    ("Terminal Encrypted", ["utilities-terminal", "konsole"]),
    ("Console", ["utilities-terminal", "konsole"]),
    ("Printer", ["printer", "document-print"]),
    ("Program Icons", ["applications-other", "view-app-grid"]),
    ("Run", ["system-run", "media-playback-start"]),
    ("Settings", ["preferences-other", "configure"]),
    ("World Computer", ["network-workgroup", "network-server"]),
    ("Archive", ["package-x-generic", "application-x-archive"]),
    ("Homebanking", ["accessories-calculator", "office-chart-line"]),
    ("Drive Windows", ["drive-harddisk", "drive-removable-media"]),
    ("Clock", ["appointment-soon", "alarm-symbolic", "clock"]),
    ("E-Mail Search", ["edit-find", "system-search"]),
    ("Paper Flag", ["emblem-important", "flag"]),
    ("Memory", ["media-flash", "drive-removable-media"]),
    ("Trash Bin", ["user-trash", "user-trash-full"]),
    ("Note", ["text-x-generic", "knotes"]),
    ("Expired", ["dialog-error", "process-stop"]),
    ("Info", ["dialog-information", "help-about"]),
    ("Package", ["package-x-generic", "application-x-archive"]),
    ("Folder", ["folder"]),
    ("Folder Open", ["folder-open", "folder"]),
    ("Folder Package", ["folder-tar", "package-x-generic"]),
    ("Lock Open", ["changes-allow", "object-unlocked"]),
    ("Paper Locked", ["document-encrypt", "document-encrypted", "lock"]),
    ("Checked", ["emblem-ok", "object-select", "checkbox-checked"]),
    ("Pen", ["document-edit", "edit-entry"]),
    ("Thumbnail", ["image-x-generic", "folder-pictures"]),
    ("Book", ["accessories-dictionary", "x-office-address-book"]),
    ("List", ["view-list", "view-list-details", "format-justify-left"]),
    ("User Key", ["user-info", "system-users", "avatar-default"]),
    ("Tool", ["applications-utilities", "tools"]),
    ("Home", ["user-home", "go-home"]),
    ("Star", ["starred", "emblem-favorite"]),
    ("Tux", ["tux", "applications-system", "preferences-system-linux"]),
    ("Feather", ["accessories-text-editor", "document-edit"]),
    ("Apple", ["applications-other"]),
    ("Wiki", ["accessories-dictionary", "help-browser"]),
    ("Money", ["wallet-open", "accessories-calculator", "money"]),
    ("Certificate", ["application-certificate", "certificate", "security-high"]),
    ("BlackBerry", ["phone", "smartphone", "multimedia-player"]),
]

_cache = {}


def kp_icon(icon_id, size=16):
    key = ("kp", icon_id, size)
    if key in _cache:
        return _cache[key]
    names = KP_ICONS[icon_id][1] if 0 <= icon_id < len(KP_ICONS) else ["dialog-password"]
    theme = Gtk.IconTheme.get_default()
    pb = None
    for n in names + ["dialog-password", "text-x-generic", "image-missing"]:
        try:
            pb = theme.load_icon(n, size, Gtk.IconLookupFlags.FORCE_SIZE)
            if pb:
                break
        except Exception:
            continue
    _cache[key] = pb
    return pb


def custom_icon(db, uid, size=16):
    if not uid:
        return None
    key = ("c", id(db), uid, size)
    if key in _cache:
        return _cache[key]
    data = db.custom_icon(uid)
    pb = pixbuf_from_bytes(data, size) if data else None
    _cache[key] = pb
    return pb


def pixbuf_from_bytes(data, size):
    try:
        loader = GdkPixbuf.PixbufLoader()
        loader.write(data)
        loader.close()
        pb = loader.get_pixbuf()
        if pb and (pb.get_width() != size or pb.get_height() != size):
            pb = pb.scale_simple(size, size, GdkPixbuf.InterpType.BILINEAR)
        return pb
    except Exception:
        return None


def node_icon(node, size=16):
    if node is None:
        return None
    cu = node.custom_icon_uuid
    if cu:
        pb = custom_icon(node.db, cu, size)
        if pb:
            return pb
    return kp_icon(node.icon_id, size)


def clear_cache():
    for k in [k for k in _cache if k[0] == "c"]:
        del _cache[k]


def symbolic(name, size=Gtk.IconSize.BUTTON):
    return Gtk.Image.new_from_icon_name(name, size)
