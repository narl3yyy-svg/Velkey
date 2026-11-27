"""Main window: KeePass-style layout (menu, toolbar, group tree, entry list,
entry details, status bar) with an embedded unlock screen."""
import base64
import csv
import os
import re
import shlex
import shutil
import subprocess
import threading
import time

from gi.repository import Gdk, GdkPixbuf, Gio, GLib, Gtk, Pango

from . import autotype, browser, dialogs, kdbx, otp, theme

APP_NAME = "Velkey"
MASK = "••••••••"
TARGET_ENTRY = "VELKEY_ENTRY"
TARGET_GROUP = "VELKEY_GROUP"

# entry list store columns
C_OBJ, C_TITLE, C_USER, C_PASS, C_URL, C_NOTES, C_ICON, C_FG, C_BG, C_STRIKE, C_MOD, C_GROUP = range(12)


class _Doc:
    """One database tab: its file, and the database once unlocked."""

    def __init__(self, path=None):
        self.path = path
        self.db = None
        self.keyfile = None
        self.group_uuid = None   # view state, restored when switching back
        self.filter = None
        self.search = ""
        self.pending = []        # unsaved deletions (kdbx.ChangeLog), newest last
        self.dirty = False       # other unsaved changes
        self.page = None
        self.label = None
        self.icon = None


class MainWindow(Gtk.ApplicationWindow):
    # the current tab's database; all other code works on these
    db = property(lambda s: s.doc.db, lambda s, v: setattr(s.doc, "db", v))
    db_path = property(lambda s: s.doc.path, lambda s, v: setattr(s.doc, "path", v))
    keyfile = property(lambda s: s.doc.keyfile, lambda s, v: setattr(s.doc, "keyfile", v))

    def __init__(self, app):
        super().__init__(application=app, title=APP_NAME)
        self.app = app
        self.config = app.config
        self._docs = {}  # notebook page -> _Doc
        self.doc = _Doc()
        self.current_group = None
        self.filter_mode = None      # None | ("search", terms) | ("special", name)
        self._clip_text = None
        self._clip_timer = None
        self._clip_deadline = 0
        self._status_timer = None
        self._last_activity = time.time()
        self._pending_autotype = None
        self._backed_up = set()
        self._otp_cache = {}
        self._drag_items = None
        self._details_otp = None
        self._suspend_group_sel = False
        self.action_widgets = []      # (widget, condition)
        self.set_icon_name("velkey")
        w, h = self.config.get("window")
        self.set_default_size(w, h)
        if self.config.get("maximized"):
            self.maximize()

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        root.pack_start(self._build_menubar(), False, False, 0)
        self.toolbar = self._build_toolbar()
        root.pack_start(self.toolbar, False, False, 0)
        self.tabs = Gtk.Notebook()
        self.tabs.set_scrollable(True)
        self.tabs.set_show_border(False)
        self.tabs.get_style_context().add_class("db-tabs")
        self.tabs.connect("switch-page", self._on_switch_tab)
        self.tabs.connect("page-reordered", lambda *_: None)
        root.pack_start(self.tabs, False, False, 0)
        self.stack = Gtk.Stack()
        self.stack.set_transition_type(Gtk.StackTransitionType.CROSSFADE)
        self.stack.set_transition_duration(120)
        self.stack.add_named(self._build_unlock_page(), "unlock")
        self.stack.add_named(self._build_db_page(), "db")
        root.pack_start(self.stack, True, True, 0)
        root.pack_start(self._build_statusbar(), False, False, 0)
        self.add(root)

        self.connect("key-press-event", self._on_key)
        self.connect("delete-event", self._on_delete)
        self.connect("window-state-event", self._on_state)
        self.connect("event", self._on_any_event)
        self.connect("configure-event", self._on_configure)
        GLib.timeout_add(1000, self._tick)
        GLib.timeout_add_seconds(10, self._idle_check)
        self.show_all()
        self.toolbar.set_visible(self.config.get("show_toolbar"))
        self.details_box.set_visible(self.config.get("show_details"))
        self._apply_column_visibility()
        self._add_tab(self.doc)
        self._restore_tabs()

    # ================================================================= tabs
    def _add_tab(self, doc):
        page = Gtk.Box()
        head = Gtk.Box(spacing=6)
        doc.icon = Gtk.Image.new_from_icon_name("system-lock-screen-symbolic", Gtk.IconSize.MENU)
        doc.label = Gtk.Label()
        doc.label.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        close = Gtk.Button.new_from_icon_name("window-close-symbolic", Gtk.IconSize.MENU)
        close.set_relief(Gtk.ReliefStyle.NONE)
        close.set_can_focus(False)
        close.set_tooltip_text("Close this database (Ctrl+W)")
        close.connect("clicked", lambda *_: self._close_tab(doc))
        head.pack_start(doc.icon, False, False, 0)
        head.pack_start(doc.label, False, False, 0)
        head.pack_start(close, False, False, 0)
        head.show_all()
        page.show()
        doc.page = page
        self._docs[page] = doc
        n = self.tabs.append_page(page, head)
        self.tabs.set_tab_reorderable(page, True)
        self._update_tab(doc)
        self._update_tab_bar()
        return n

    def docs(self):
        """Open tabs, in the order shown."""
        return [self._docs[self.tabs.get_nth_page(i)] for i in range(self.tabs.get_n_pages())]

    def _update_tab_bar(self):
        self.tabs.set_visible(self.tabs.get_n_pages() > 1)

    def _update_tab(self, doc):
        if doc.label is None:
            return
        if doc.db is not None:
            name = doc.db.name or os.path.splitext(os.path.basename(doc.db.path or "New Database"))[0]
            text = name + ("*" if doc.db.modified else "")
        elif doc.path:
            text = os.path.splitext(os.path.basename(doc.path))[0]
        else:
            text = "Start"
        doc.label.set_text(text)
        doc.label.set_width_chars(min(len(text), 30))  # room for the full name (long ones get "…" in the middle)
        doc.icon.set_visible(doc.db is None and bool(doc.path))
        tip = (doc.path or "").replace(os.path.expanduser("~"), "~")
        if doc.db is None and doc.path:
            tip += "\nLocked"
        doc.label.get_parent().set_tooltip_text(tip or None)

    def _remember_view(self):
        d = self.doc
        d.group_uuid = self.current_group.uuid if self.current_group is not None else None
        d.filter = self.filter_mode
        d.search = self.search.get_text()

    def _on_switch_tab(self, nb, page, num):
        doc = self._docs.get(page)
        if doc is None or doc is self.doc:
            return
        if self.doc.page in self._docs:
            self._remember_view()
        self.doc = doc
        self._show_doc()

    def _show_doc(self):
        d = self.doc
        if d.db is None:
            self.show_unlock(d.path)
            return
        group, flt, search = d.group_uuid, d.filter, d.search
        self.current_group = None
        self.filter_mode = None
        self._suspend_group_sel = True
        self.group_store.clear()  # rows of the previous tab's database
        self.entry_store.clear()
        self._suspend_group_sel = False
        self.search.set_text("")
        self.load_db(d.db)
        if group:
            self._select_group_uuid(group)
        if search:
            self.search.set_text(search)
        elif flt and flt[0] == "special":
            self.special(flt[1])

    def _switch_to(self, doc):
        n = self.tabs.page_num(doc.page)
        if n >= 0 and doc is not self.doc:
            self.tabs.set_current_page(n)

    def _new_tab(self):
        doc = _Doc()
        n = self._add_tab(doc)
        self.tabs.set_current_page(n)
        return doc

    def _find_doc(self, path):
        for d in self.docs():
            if path and d.path and (d.path == path or (os.path.exists(d.path) and os.path.exists(path)
                                                        and os.path.samefile(d.path, path))):
                return d
        return None

    def _make_room(self):
        """Use the current tab if it is empty, otherwise open a new one."""
        if self.doc.db is not None or self.doc.path:
            self._new_tab()

    def _close_tab(self, doc):
        self._switch_to(doc)
        self.close_database()

    def next_tab(self):
        n = self.tabs.get_n_pages()
        if n > 1:
            self.tabs.set_current_page((self.tabs.get_current_page() + 1) % n)

    def prev_tab(self):
        n = self.tabs.get_n_pages()
        if n > 1:
            self.tabs.set_current_page((self.tabs.get_current_page() - 1) % n)

    def _any_unlocked(self):
        return any(d.db is not None for d in self.docs())

    def _restore_tabs(self):
        """Reopen the databases that were open last time (locked)."""
        files = []
        if self.config.get("open_last"):
            files = [p for p in self.config.get("open_files", []) if p and os.path.exists(p)]
            last = self.config.get("last_file")
            if last and os.path.exists(last) and last not in files:
                files.append(last)
        if not files:
            self.show_unlock(None)
            return
        self.show_unlock(files[0])
        for p in files[1:]:
            self._new_tab()
            self.show_unlock(p)
        last = self._find_doc(self.config.get("last_file"))
        if last is not None:
            self._switch_to(last)

    def _save_other(self, doc):
        """Save a database that is open in a tab other than the current one."""
        db = doc.db
        if db is None or not db.path or not self.config.get("auto_save"):
            return
        try:
            if os.path.exists(db.path) and db.file_mtime and abs(os.path.getmtime(db.path) - db.file_mtime) > 0.001:
                return  # changed on disk: let the user decide when they open the tab
            if self.config.get("backup_on_save") and db.path not in self._backed_up and os.path.exists(db.path):
                shutil.copy2(db.path, db.path + ".bak")
                self._backed_up.add(db.path)
            kdbx.save_without(db, doc.pending)
            doc.dirty = False
        except Exception as e:
            self.set_status("Could not save %s: %s" % (os.path.basename(db.path), e), 8)
        self._update_tab(doc)

    # ================================================================ build
    def _menu(self, bar, title, items):
        top = Gtk.MenuItem.new_with_mnemonic(title)
        menu = Gtk.Menu()
        top.set_submenu(menu)
        self._fill_menu(menu, items)
        bar.append(top)
        return menu

    def _fill_menu(self, menu, items):
        for it in items:
            if it is None:
                menu.append(Gtk.SeparatorMenuItem())
                continue
            label, cb, accel, cond = (list(it) + [None, None])[:4]
            if isinstance(cb, list):
                mi = Gtk.MenuItem.new_with_mnemonic(label)
                sub = Gtk.Menu()
                self._fill_menu(sub, cb)
                mi.set_submenu(sub)
            elif isinstance(cb, tuple) and cb[0] == "check":
                mi = Gtk.CheckMenuItem.new_with_mnemonic(label)
                mi.set_active(bool(self.config.get(cb[1])))
                mi.connect("toggled", lambda m, k=cb[1], f=cb[2]: f(k, m.get_active()))
            else:
                mi = Gtk.MenuItem.new_with_mnemonic(label)
                if cb:
                    mi.connect("activate", lambda _m, f=cb: f())
            if accel:
                key, mods = Gtk.accelerator_parse(accel)
                child = mi.get_child()
                if isinstance(child, Gtk.AccelLabel):
                    child.set_accel(key, mods)
            if cond:
                self.action_widgets.append((mi, cond))
            menu.append(mi)

    def _build_menubar(self):
        bar = Gtk.MenuBar()
        self.recent_menu = Gtk.Menu()
        self._menu(bar, "_File", [
            ("_New…", self.new_database, "<Control>n"),
            ("_Open…", self.open_database, "<Control>o"),
            ("Open _Recent", None),
            ("_Close", self.close_database, "<Control>w", "db"),
            None,
            ("_Save", self.save, "<Control>s", "db"),
            ("Save _As…", self.save_as, "<Control><Shift>s", "db"),
            None,
            ("_Import Entries from KeePass File…", self.import_entries, None, "db"),
            ("Export Selected _Entries…", self.export_entries, None, "entry"),
            ("_Export to CSV…", self.export_csv, None, "db"),
            None,
            ("Database _Settings…", self.database_settings, None, "db"),
            ("Change _Master Key…", self.change_master_key, None, "db"),
            None,
            ("_Lock Workspace", self.toggle_lock, "<Control>l"),
            ("E_xit", self.quit_app, "<Control>q"),
        ])
        file_menu = bar.get_children()[0].get_submenu()
        file_menu.get_children()[2].set_submenu(self.recent_menu)
        self._menu(bar, "_Group", [
            ("_Add Group…", self.add_group, None, "db"),
            ("_Edit Group…", self.edit_group, "F2", "group"),
            ("_Delete Group", self.delete_group, None, "group"),
            None,
            ("Empty _Recycle Bin", self.empty_recycle_bin, None, "db"),
        ])
        self._menu(bar, "_Entry", [
            ("_Add Entry…", self.add_entry, "<Control>i", "db"),
            ("_Edit Entry…", self.edit_entry, "Return", "entry"),
            ("D_uplicate Entry", self.duplicate_entry, "<Control>k", "entry"),
            ("_Delete Entry", self.delete_entries, "Delete", "entry"),
            ("_Undo Delete", self.undo_delete, "<Control>z", "db"),
            None,
            ("Copy to Other Data_base…", self.copy_to_other_database, None, "entry"),
            ("E_xport Selected Entries…", self.export_entries, None, "entry"),
            ("_Import Entries…", self.import_entries, None, "db"),
            None,
            ("Copy _User Name", self.copy_username, "<Control>b", "entry"),
            ("Copy _Password", self.copy_password, "<Control>c", "entry"),
            ("Copy _TOTP", self.copy_otp, "<Control>t", "entry"),
            ("Copy U_RL", self.copy_url, "<Control><Shift>u", "entry"),
            ("_Open URL", self.open_url, "<Control>u", "entry"),
            None,
            ("Perform Auto-_Type", self.autotype_selected, "<Control>v", "entry"),
            None,
            ("Select _All", self.select_all, "<Control>a", "db"),
        ])
        self._menu(bar, "F_ind", [
            ("_Find…", self.focus_search, "<Control>f", "db"),
            None,
            ("Show _All Entries", lambda: self.special("all"), None, "db"),
            ("_Expired Entries", lambda: self.special("expired"), None, "db"),
            ("Entries Expiring in _7 Days", lambda: self.special("soon"), None, "db"),
            ("Entries with _OTP", lambda: self.special("otp"), None, "db"),
            ("_Duplicate Passwords", lambda: self.special("dupes"), None, "db"),
            ("_Weak Passwords", lambda: self.special("weak"), None, "db"),
            ("Recently _Modified", lambda: self.special("recent"), None, "db"),
        ])
        self._menu(bar, "_View", [
            ("Show _Toolbar", ("check", "show_toolbar", self._toggle_view)),
            ("Show Entry _Details", ("check", "show_details", self._toggle_view)),
            None,
            ("_Columns", [
                ("User Name", ("check", "col_user", self._toggle_col)),
                ("Password", ("check", "col_pass", self._toggle_col)),
                ("URL", ("check", "col_url", self._toggle_col)),
                ("Notes", ("check", "col_notes", self._toggle_col)),
                ("OTP", ("check", "show_otp_column", self._toggle_col)),
                ("Last Modified", ("check", "col_mod", self._toggle_col)),
            ]),
            ("_Hide Passwords", ("check", "hide_passwords", self._toggle_view)),
            ("Hide User _Names", ("check", "hide_usernames", self._toggle_view)),
        ])
        self._menu(bar, "_Tools", [
            ("_Password Generator…", self.password_generator, "<Control>g"),
            None,
            ("_Options…", self.options, None),
        ])
        self._menu(bar, "_Help", [
            ("_Keyboard Shortcuts", self.show_shortcuts, "F1"),
            ("_About Velkey", self.about),
        ])
        return bar

    def _tb_button(self, box, icon, tip, cb, cond=None):
        b = Gtk.Button.new_from_icon_name(icon, Gtk.IconSize.LARGE_TOOLBAR)
        b.set_tooltip_text(tip)
        b.set_relief(Gtk.ReliefStyle.NONE)
        b.connect("clicked", lambda *_: cb())
        b.set_can_focus(False)
        box.pack_start(b, False, False, 0)
        if cond:
            self.action_widgets.append((b, cond))
        return b

    def _tb_sep(self, box):
        s = Gtk.Separator(orientation=Gtk.Orientation.VERTICAL)
        s.get_style_context().add_class("tb-sep")
        box.pack_start(s, False, False, 0)

    def _build_toolbar(self):
        box = Gtk.Box(spacing=2)
        box.get_style_context().add_class("pk-toolbar")
        self._tb_button(box, "document-new-symbolic", "New database (Ctrl+N)", self.new_database)
        self._tb_button(box, "document-open-symbolic", "Open database (Ctrl+O)", self.open_database)
        self._tb_button(box, "document-save-symbolic", "Save (Ctrl+S)", self.save, "db")
        self._tb_sep(box)
        self._tb_button(box, "list-add-symbolic", "Add entry (Ctrl+I)", self.add_entry, "db")
        self._tb_button(box, "document-edit-symbolic", "Edit entry (Enter)", self.edit_entry, "entry")
        self._tb_button(box, "edit-delete-symbolic", "Delete entry (Del)", self.delete_entries, "entry")
        self._tb_sep(box)
        self._tb_button(box, "avatar-default-symbolic", "Copy user name (Ctrl+B)", self.copy_username, "entry")
        self._tb_button(box, "dialog-password-symbolic", "Copy password (Ctrl+C)", self.copy_password, "entry")
        self._tb_button(box, "velkey-totp-symbolic", "Copy TOTP (Ctrl+T)", self.copy_otp, "entry")
        self._tb_button(box, "velkey-autotype-symbolic", "Perform Auto-Type (Ctrl+V)", self.autotype_selected, "entry")
        self._tb_sep(box)
        self._tb_button(box, "system-lock-screen-symbolic", "Lock workspace (Ctrl+L)", self.toggle_lock)
        spacer = Gtk.Box()
        box.pack_start(spacer, True, True, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Search  (Ctrl+F)")
        self.search.get_style_context().add_class("search")
        self.search.connect("search-changed", self._on_search)
        self.search.connect("activate", lambda *_: self._focus_list())
        self.search.connect("key-press-event", self._search_key)
        self.action_widgets.append((self.search, "db"))
        box.pack_end(self.search, False, False, 4)
        return box

    def _build_statusbar(self):
        box = Gtk.Box(spacing=12)
        box.get_style_context().add_class("statusbar")
        self.status = Gtk.Label(xalign=0)
        self.status.set_ellipsize(Pango.EllipsizeMode.END)
        self.status_right = Gtk.Label(xalign=1)
        box.pack_start(self.status, True, True, 0)
        box.pack_end(self.status_right, False, False, 0)
        return box

    # ------------------------------------------------------------- unlock UI
    def _build_unlock_page(self):
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.get_style_context().add_class("welcome-bg")
        center = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=14)
        center.set_halign(Gtk.Align.CENTER)
        center.set_valign(Gtk.Align.CENTER)
        center.set_size_request(440, -1)
        logo = Gtk.Image.new_from_icon_name("velkey", Gtk.IconSize.DIALOG)
        logo.set_pixel_size(84)
        center.pack_start(logo, False, False, 0)
        t = Gtk.Label(label=APP_NAME)
        t.get_style_context().add_class("title-big")
        center.pack_start(t, False, False, 0)
        self.unlock_file = Gtk.Label()
        self.unlock_file.get_style_context().add_class("dim")
        self.unlock_file.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
        self.unlock_file.set_max_width_chars(50)
        center.pack_start(self.unlock_file, False, False, 0)

        card = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=12)
        card.get_style_context().add_class("unlock-card")
        self.unlock_pw = dialogs.password_entry(placeholder="Master password")
        self.unlock_pw.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "dialog-password-symbolic")
        self.unlock_pw.connect("activate", lambda *_: self.do_unlock())
        card.pack_start(self.unlock_pw, False, False, 0)
        krow = Gtk.Box(spacing=6)
        self.unlock_kf = Gtk.Entry()
        self.unlock_kf.set_placeholder_text("Key file (optional)")
        self.unlock_kf.set_icon_from_icon_name(Gtk.EntryIconPosition.PRIMARY, "document-properties-symbolic")
        self.unlock_kf.connect("activate", lambda *_: self.do_unlock())
        kb = Gtk.Button.new_from_icon_name("document-open-symbolic", Gtk.IconSize.BUTTON)
        kb.set_tooltip_text("Browse for key file")
        kb.connect("clicked", self._browse_keyfile)
        krow.pack_start(self.unlock_kf, True, True, 0)
        krow.pack_start(kb, False, False, 0)
        card.pack_start(krow, False, False, 0)
        self.unlock_err = Gtk.Label(xalign=0)
        self.unlock_err.get_style_context().add_class("error")
        self.unlock_err.set_line_wrap(True)
        card.pack_start(self.unlock_err, False, False, 0)
        self.unlock_btn = Gtk.Button()
        ub = Gtk.Box(spacing=8)
        ub.set_halign(Gtk.Align.CENTER)
        self.unlock_spinner = Gtk.Spinner()
        ub.pack_start(self.unlock_spinner, False, False, 0)
        self.unlock_btn_lbl = Gtk.Label(label="Unlock")
        ub.pack_start(self.unlock_btn_lbl, False, False, 0)
        self.unlock_btn.add(ub)
        self.unlock_btn.get_style_context().add_class("suggested-action")
        self.unlock_btn.connect("clicked", lambda *_: self.do_unlock())
        card.pack_start(self.unlock_btn, False, False, 4)
        self.unlock_card = card
        center.pack_start(card, False, False, 6)

        links = Gtk.Box(spacing=8)
        links.set_halign(Gtk.Align.CENTER)
        b1 = Gtk.Button(label="Open Database…")
        b1.connect("clicked", lambda *_: self.open_database())
        b2 = Gtk.Button(label="Create New Database…")
        b2.connect("clicked", lambda *_: self.new_database())
        links.pack_start(b1, False, False, 0)
        links.pack_start(b2, False, False, 0)
        center.pack_start(links, False, False, 0)
        self.recent_lbl = Gtk.Label(label="RECENT", xalign=0)
        self.recent_lbl.get_style_context().add_class("section")
        center.pack_start(self.recent_lbl, False, False, 8)
        self.recent_box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=2)
        center.pack_start(self.recent_box, False, False, 0)
        sw = Gtk.ScrolledWindow()
        sw.get_style_context().add_class("plain")
        sw.add(center)
        outer.pack_start(sw, True, True, 0)
        return outer

    def _browse_keyfile(self, *_):
        fc = Gtk.FileChooserDialog(title="Select Key File", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
        if fc.run() == Gtk.ResponseType.OK:
            self.unlock_kf.set_text(fc.get_filename())
        fc.destroy()
        self.unlock_pw.grab_focus()

    def _refresh_recent(self):
        for c in self.recent_box.get_children():
            self.recent_box.remove(c)
        for c in self.recent_menu.get_children():
            self.recent_menu.remove(c)
        rec = [p for p in self.config.get("recent_files") if os.path.exists(p)]
        self.recent_lbl.set_visible(bool(rec))
        for p in rec:
            b = Gtk.Button()
            b.get_style_context().add_class("recent")
            hb = Gtk.Box(spacing=10)
            img = Gtk.Image.new_from_icon_name("dialog-password-symbolic", Gtk.IconSize.BUTTON)
            hb.pack_start(img, False, False, 0)
            vb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
            n = Gtk.Label(label=os.path.basename(p), xalign=0)
            d = Gtk.Label(label=os.path.dirname(p).replace(os.path.expanduser("~"), "~"), xalign=0)
            d.get_style_context().add_class("dim")
            d.set_ellipsize(Pango.EllipsizeMode.MIDDLE)
            vb.pack_start(n, False, False, 0)
            vb.pack_start(d, False, False, 0)
            hb.pack_start(vb, True, True, 0)
            b.add(hb)
            b.connect("clicked", lambda _b, path=p: self.open_path(path))
            self.recent_box.pack_start(b, False, False, 0)
            mi = Gtk.MenuItem(label=p.replace(os.path.expanduser("~"), "~"))
            mi.connect("activate", lambda _m, path=p: self.open_path(path))
            self.recent_menu.append(mi)
        if not rec:
            mi = Gtk.MenuItem(label="(empty)")
            mi.set_sensitive(False)
            self.recent_menu.append(mi)
        self.recent_menu.show_all()
        self.recent_box.show_all()

    # ------------------------------------------------------------- db page
    def _build_db_page(self):
        self.hpaned = Gtk.Paned(orientation=Gtk.Orientation.HORIZONTAL)
        self.hpaned.set_position(self.config.get("pane_groups"))
        # groups
        self.group_store = Gtk.TreeStore(object, str, GdkPixbuf.Pixbuf, int)
        self.group_view = Gtk.TreeView(model=self.group_store)
        self.group_view.set_headers_visible(False)
        self.group_view.get_style_context().add_class("groups")
        self.group_view.set_enable_search(False)
        col = Gtk.TreeViewColumn()
        pr = Gtk.CellRendererPixbuf()
        pr.set_padding(4, 3)
        col.pack_start(pr, False)
        col.add_attribute(pr, "pixbuf", 2)
        tr = Gtk.CellRendererText()
        tr.set_property("ellipsize", Pango.EllipsizeMode.END)
        col.pack_start(tr, True)
        col.add_attribute(tr, "text", 1)
        col.add_attribute(tr, "weight", 3)
        self.group_view.append_column(col)
        self.group_view.get_selection().connect("changed", self._on_group_sel)
        self.group_view.connect("row-expanded", lambda v, it, p: self._set_expanded(it, True))
        self.group_view.connect("row-collapsed", lambda v, it, p: self._set_expanded(it, False))
        self.group_view.connect("button-press-event", self._group_button)
        self.group_view.connect("row-activated", lambda *_: self.edit_group())
        tgt_e = Gtk.TargetEntry.new(TARGET_ENTRY, Gtk.TargetFlags.SAME_APP, 1)
        tgt_g = Gtk.TargetEntry.new(TARGET_GROUP, Gtk.TargetFlags.SAME_APP, 2)
        self.group_view.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, [tgt_g], Gdk.DragAction.MOVE)
        self.group_view.enable_model_drag_dest([tgt_e, tgt_g], Gdk.DragAction.MOVE)
        self.group_view.connect("drag-data-get", self._group_drag_get)
        self.group_view.connect("drag-data-received", self._group_drag_received)
        gsw = Gtk.ScrolledWindow()
        gsw.add(self.group_view)
        self.hpaned.pack1(gsw, False, False)

        # entries
        self.vpaned = Gtk.Paned(orientation=Gtk.Orientation.VERTICAL)
        self.entry_store = Gtk.ListStore(object, str, str, str, str, str, GdkPixbuf.Pixbuf, str, str, bool, str, str)
        for c in (C_TITLE, C_USER, C_URL, C_NOTES, C_GROUP, C_MOD):
            self.entry_store.set_sort_func(c, self._sort_str, c)
        self.entry_view = Gtk.TreeView(model=self.entry_store)
        self.entry_view.set_enable_search(False)
        self.entry_view.set_rubber_banding(True)
        self.entry_view.get_selection().set_mode(Gtk.SelectionMode.MULTIPLE)
        self.entry_view.get_selection().connect("changed", self._on_entry_sel)
        self.entry_view.connect("button-press-event", self._entry_button)
        self.entry_view.connect("row-activated", self._entry_activated)
        self.entry_view.enable_model_drag_source(Gdk.ModifierType.BUTTON1_MASK, [tgt_e], Gdk.DragAction.MOVE)
        self.entry_view.connect("drag-data-get", self._entry_drag_get)
        self.columns = {}
        widths = self.config.get("column_widths")

        def add_col(key, title, cid, width, icon=False):
            c = Gtk.TreeViewColumn(title)
            if icon:
                pr = Gtk.CellRendererPixbuf()
                pr.set_padding(4, 0)
                c.pack_start(pr, False)
                c.add_attribute(pr, "pixbuf", C_ICON)
            r = Gtk.CellRendererText()
            r.set_property("ellipsize", Pango.EllipsizeMode.END)
            r.set_padding(4, 5)
            c.pack_start(r, True)
            if cid is not None:
                c.add_attribute(r, "text", cid)
                c.add_attribute(r, "strikethrough", C_STRIKE)
                c.add_attribute(r, "foreground", C_FG)
                c.add_attribute(r, "cell-background", C_BG)
                c.set_sort_column_id(cid)
            c.set_resizable(True)
            c.set_sizing(Gtk.TreeViewColumnSizing.FIXED)
            c.set_fixed_width(widths.get(key, width))
            c.set_reorderable(True)
            c.set_min_width(40)
            self.entry_view.append_column(c)
            self.columns[key] = (c, r)
            return c, r
        add_col("title", "Title", C_TITLE, 220, icon=True)
        add_col("user", "User Name", C_USER, 170)
        add_col("pass", "Password", C_PASS, 110)
        add_col("url", "URL", C_URL, 210)
        add_col("notes", "Notes", C_NOTES, 180)
        oc, orr = add_col("otp", "OTP", None, 110)
        oc.set_cell_data_func(orr, self._otp_cell)
        orr.set_property("family", "monospace")
        add_col("mod", "Last Modified", C_MOD, 150)
        add_col("group", "Group", C_GROUP, 140)
        esw = Gtk.ScrolledWindow()
        esw.add(self.entry_view)
        self.vpaned.pack1(esw, True, False)

        # details
        self.details_box = Gtk.ScrolledWindow()
        self.details_box.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self.details = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.details.get_style_context().add_class("details")
        self.details_box.add(self.details)
        self.details_box.set_size_request(-1, 120)
        self.vpaned.pack2(self.details_box, False, True)
        self.hpaned.pack2(self.vpaned, True, False)
        GLib.idle_add(self._restore_vpaned)
        return self.hpaned

    def _restore_vpaned(self):
        h = self.vpaned.get_allocated_height()
        if h > 200:
            self.vpaned.set_position(max(150, h - int(self.config.get("pane_details") * 0.5)))
        else:
            GLib.timeout_add(100, self._restore_vpaned)
        return False

    @staticmethod
    def _sort_str(model, a, b, col):
        x = (model.get_value(a, col) or "").casefold()
        y = (model.get_value(b, col) or "").casefold()
        return (x > y) - (x < y)

    def _apply_column_visibility(self):
        vis = {"user": "col_user", "pass": "col_pass", "url": "col_url", "notes": "col_notes",
               "otp": "show_otp_column", "mod": "col_mod"}
        for key, cfg in vis.items():
            default = key not in ("mod",)
            v = self.config.get(cfg, default)
            self.columns[key][0].set_visible(bool(v))
        self.columns["group"][0].set_visible(self.filter_mode is not None)

    def _toggle_col(self, key, value):
        self.config.set(key, value)
        self._apply_column_visibility()

    def _toggle_view(self, key, value):
        self.config.set(key, value)
        if key == "show_toolbar":
            self.toolbar.set_visible(value)
        elif key == "show_details":
            self.details_box.set_visible(value)
        else:
            self.refresh_entries()

    # ============================================================ state mgmt
    def update_actions(self):
        has_db = self.db is not None
        sel = self.selected_entries()
        g = self.selected_group()
        for w, cond in self.action_widgets:
            if cond == "db":
                w.set_sensitive(has_db)
            elif cond == "entry":
                w.set_sensitive(has_db and bool(sel))
            elif cond == "group":
                w.set_sensitive(has_db and g is not None)
        self.update_title()

    def update_title(self):
        self._update_tab(self.doc)
        if self.db is None:
            self.set_title(("Locked — " if self.db_path else "") + APP_NAME)
            return
        name = self.db.name or os.path.splitext(os.path.basename(self.db.path or "New Database"))[0]
        self.set_title("%s%s — %s" % (name, "*" if self.db.modified else "", APP_NAME))
        if self.app.tray and self.app.tray.ok:
            self.app.tray.set_tooltip("%s — %s" % (APP_NAME, name))

    def set_status(self, text, seconds=0):
        self.status.set_text(text)
        if self._status_timer:
            GLib.source_remove(self._status_timer)
            self._status_timer = None
        if seconds:
            def back():
                self._status_timer = None
                self._default_status()
                return False
            self._status_timer = GLib.timeout_add_seconds(seconds, back)

    def _default_status(self):
        if self.db is None:
            self.status.set_text("Locked" if self.db_path else "Ready")
            return
        n = len(self.entry_store)
        s = len(self.selected_entries())
        what = ""
        if self.filter_mode:
            what = "  ·  %s" % (self.filter_mode[2] if len(self.filter_mode) > 2 else "Search results")
        self.status.set_text("%d of %d selected%s" % (s, n, what))

    def changed(self, refresh=True, deletion=False):
        if self.db is None:
            return
        self.db.modified = True
        if not deletion:
            self.doc.dirty = True
        if refresh:
            self.refresh_all()
        self.update_title()
        if self.config.get("auto_save") and self.db.path:
            GLib.idle_add(lambda: (self.save(quiet=True), False)[1])

    # ============================================================ unlock / open
    def show_unlock(self, path=None):
        self.db = None
        if path and not os.path.exists(path):
            path = None
        self.db_path = path
        self.entry_store.clear()
        self.group_store.clear()
        self._otp_cache.clear()
        self.current_group = None
        self.filter_mode = None
        self.search.set_text("")
        self._clear_details()
        self._refresh_recent()
        self.unlock_err.set_text("")
        self.unlock_pw.set_text("")
        self.unlock_card.set_visible(path is not None)
        if path:
            self.unlock_file.set_text(path.replace(os.path.expanduser("~"), "~"))
            kf = self.config.get("last_keyfiles").get(path, "")
            self.unlock_kf.set_text(kf)
        else:
            self.unlock_file.set_text("Open a KeePass database (.kdbx) to get started")
        self.stack.set_visible_child_name("unlock")
        self.update_actions()
        self._default_status()
        if path:
            GLib.idle_add(lambda: (self.unlock_pw.grab_focus(), False)[1])

    def _set_unlock_busy(self, busy):
        for w in (self.unlock_pw, self.unlock_kf, self.unlock_btn):
            w.set_sensitive(not busy)
        if busy:
            self.unlock_spinner.start()
            self.unlock_spinner.show()
            self.unlock_btn_lbl.set_text("Unlocking…")
        else:
            self.unlock_spinner.stop()
            self.unlock_spinner.hide()
            self.unlock_btn_lbl.set_text("Unlock")

    def do_unlock(self):
        if not self.db_path:
            return
        pw = self.unlock_pw.get_text()
        kf = self.unlock_kf.get_text().strip() or None
        if kf and not os.path.isfile(kf):
            self.unlock_err.set_text("Key file not found.")
            return
        path = self.db_path
        doc = self.doc
        self.unlock_err.set_text("")
        self._set_unlock_busy(True)

        def work():
            try:
                db = kdbx.Database.open(path, pw if (pw or not kf) else None, kf)
                GLib.idle_add(self._unlock_done, db, kf, None, doc)
            except Exception as e:  # noqa
                GLib.idle_add(self._unlock_done, None, kf, e, doc)
        threading.Thread(target=work, daemon=True).start()

    def _unlock_done(self, db, kf, err, doc=None):
        self._set_unlock_busy(False)
        if doc is not None and doc is not self.doc:
            # the user switched to another tab while this one was unlocking
            if err is None and doc.page in self._docs:
                doc.db, doc.keyfile = db, kf
                self.config.add_recent(db.path, kf)
                self._update_tab(doc)
            return False
        if err is not None:
            if isinstance(err, kdbx.InvalidKey):
                self.unlock_err.set_text("Wrong master password or key file.")
            elif isinstance(err, FileNotFoundError):
                self.unlock_err.set_text("File not found.")
            else:
                self.unlock_err.set_text(str(err) or err.__class__.__name__)
            self.unlock_pw.grab_focus()
            self.unlock_pw.select_region(0, -1)
            return False
        self.unlock_pw.set_text("")
        self.keyfile = kf
        self.config.add_recent(db.path, kf)
        self.load_db(db)
        if self._pending_autotype is not None:
            title = self._pending_autotype
            self._pending_autotype = None
            self.iconify()
            GLib.timeout_add(500, lambda: (self._global_autotype_with(title, ""), False)[1])
        return False

    def load_db(self, db):
        if self.doc.db is not db:
            self.doc.pending, self.doc.dirty = [], False
        self.db = db
        self.db_path = db.path
        theme.clear_cache()
        self._otp_cache.clear()
        self.filter_mode = None
        self.current_group = None
        self.stack.set_visible_child_name("db")
        self.refresh_all()
        # select last selected group
        last = db.meta_get("LastSelectedGroup")
        if last:
            self._select_group_uuid(last)
        if self.selected_group() is None:
            self._select_group_uuid(db.root_group.uuid)
        self.entry_view.grab_focus()
        self.update_actions()

    def open_database(self):
        fc = Gtk.FileChooserDialog(title="Open Database", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        f = Gtk.FileFilter()
        f.set_name("KeePass databases (*.kdbx)")
        f.add_pattern("*.kdbx")
        f.add_pattern("*.KDBX")
        fc.add_filter(f)
        a = Gtk.FileFilter()
        a.set_name("All files")
        a.add_pattern("*")
        fc.add_filter(a)
        if self.db_path:
            fc.set_current_folder(os.path.dirname(self.db_path))
        if fc.run() == Gtk.ResponseType.OK:
            path = fc.get_filename()
            fc.destroy()
            self.open_path(path, confirmed=True)
            return
        fc.destroy()

    def open_path(self, path, confirmed=False):
        """Open `path` in its own tab (or switch to it if it is already open)."""
        self.present()
        found = self._find_doc(path)
        if found is not None:
            self._switch_to(found)
            return
        self._make_room()
        self.show_unlock(path)

    def new_database(self):
        fc = Gtk.FileChooserDialog(title="Create New Database", transient_for=self, action=Gtk.FileChooserAction.SAVE)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        fc.set_current_name("Database.kdbx")
        fc.set_do_overwrite_confirmation(True)
        r = fc.run()
        path = fc.get_filename()
        fc.destroy()
        if r != Gtk.ResponseType.OK or not path:
            return
        if not path.lower().endswith(".kdbx"):
            path += ".kdbx"
        d = dialogs.MasterKeyDialog(self, "Create Master Key",
                                    "Choose the master key for the new database. If you forget it, the "
                                    "database cannot be recovered.")
        res = d.get_result()
        d.destroy()
        if res is None:
            return
        pw, kf = res
        name = os.path.splitext(os.path.basename(path))[0]
        self.set_status("Creating database…")
        while Gtk.events_pending():
            Gtk.main_iteration()
        try:
            db = kdbx.Database.create(path, pw, kf, name)
            db.save()
        except Exception as e:
            dialogs.error(self, "Could not create database", str(e))
            return
        self._make_room()
        self.keyfile = kf
        self.config.add_recent(path, kf)
        self.load_db(db)
        self.set_status("Database created", 4)

    def _ask_deletions(self, verb="closing"):
        """Unsaved deletions: ask whether to save them. 'save'/'discard'/'cancel'."""
        n = sum(1 for log in self.doc.pending for op in log.ops
                if op[0] == "detach" and op[1].tag in ("Entry", "Group"))
        return dialogs.ask_save(
            self, "Save the deletion of %d %s before %s?" % (n, "item" if n == 1 else "items", verb),
            "If you don't save it, the deleted items stay in the database file (everything else is saved).",
            save_label="Save Deletion", discard_label="Keep Them")

    def confirm_close(self):
        """Ask to save changes. Returns False if the user cancelled."""
        if self.db is None or not self.db.modified:
            return True
        if self.doc.pending:
            r = self._ask_deletions()
            if r == "cancel":
                return False
            if r == "save":
                return self.save()
            # keep the deleted items: save everything else, if there is anything
            return not (self.doc.dirty and self.db.path) or self.save(quiet=True)
        if self.config.get("auto_save") and self.db.path:
            return self.save(quiet=True)
        r = dialogs.ask_save(self, "Save changes to \"%s\"?" % os.path.basename(self.db.path or "database"))
        if r == "save":
            return self.save()
        return r == "discard"

    def close_database(self):
        if not self.confirm_close():
            return
        if self.tabs.get_n_pages() > 1:
            doc = self.doc
            doc.db = None
            n = self.tabs.page_num(doc.page)
            self.tabs.remove_page(n)  # switches to the neighbouring tab
            del self._docs[doc.page]
            self._update_tab_bar()
            return
        path = self.db_path
        self.show_unlock(path)
        self.unlock_card.set_visible(path is not None)

    # ================================================================== save
    def save(self, quiet=False, keep_deletions=None):
        """Save the current tab. Unsaved deletions are only written by an
        explicit save (Ctrl+S, or answering Save); automatic saves leave them out."""
        keep = quiet if keep_deletions is None else keep_deletions
        db = self.db
        if db is None:
            return False
        if not db.path:
            return self.save_as()
        try:
            if os.path.exists(db.path) and db.file_mtime and abs(os.path.getmtime(db.path) - db.file_mtime) > 0.001:
                if quiet or not dialogs.confirm(
                        self, "The database file was changed by another program",
                        "It may have been modified by a sync tool or another KeePass instance. "
                        "Overwrite it with your version?", "Overwrite", destructive=True):
                    if quiet:
                        self.set_status("Auto-save skipped: file changed on disk", 8)
                    return False
            if self.config.get("backup_on_save") and db.path not in self._backed_up and os.path.exists(db.path):
                shutil.copy2(db.path, db.path + ".bak")
                self._backed_up.add(db.path)
            if db._transformed is None:
                self._run_busy("Deriving key…", lambda: db._derive(db._composite), lambda r: setattr(db, "_transformed", r))
            cur = self.selected_group()
            if cur is not None:
                kdbx._set_text(db.meta, "LastSelectedGroup", cur.uuid)
            if keep and self.doc.pending:
                kdbx.save_without(db, self.doc.pending)
            else:
                db.save()
                self.doc.pending = []
            self.doc.dirty = False
        except Exception as e:
            dialogs.error(self, "Saving failed", str(e))
            return False
        self.update_title()
        if self.doc.pending:
            self.set_status("Saved, except the deletion (Ctrl+S saves it, Ctrl+Z undoes it)", 6)
        else:
            self.set_status("Saved", 3)
        return True

    def _autosave_entry(self):
        """After Add / Edit → OK."""
        if self.db is not None and self.db.path and self.config.get("autosave_entries") \
                and not self.config.get("auto_save"):  # auto_save already saves in changed()
            self.save(quiet=True)

    def undo_delete(self):
        if self.db is None:
            return
        if not self.doc.pending:
            self.set_status("Nothing to undo", 3)
            return
        log = self.doc.pending.pop()
        log.revert()
        self.db.modified = bool(self.doc.pending) or self.doc.dirty
        self.refresh_all()
        self.update_title()
        self.set_status("Deletion undone" + ("" if self.db.modified else " (the file already has these items)"), 5)

    def _run_busy(self, text, fn, done):
        """Run fn in a thread while keeping the UI alive (blocking caller)."""
        res = {}

        def work():
            try:
                res["v"] = fn()
            except Exception as e:  # noqa
                res["e"] = e
        self.set_status(text)
        win = self.get_window()
        if win:
            win.set_cursor(Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "wait"))
        t = threading.Thread(target=work, daemon=True)
        t.start()
        while t.is_alive():
            Gtk.main_iteration_do(False)
            time.sleep(0.01)
        if win:
            win.set_cursor(None)
        if "e" in res:
            raise res["e"]
        done(res["v"])

    def save_as(self):
        if self.db is None:
            return False
        fc = Gtk.FileChooserDialog(title="Save Database As", transient_for=self, action=Gtk.FileChooserAction.SAVE)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        fc.set_do_overwrite_confirmation(True)
        fc.set_current_name(os.path.basename(self.db.path or "Database.kdbx"))
        if self.db.path:
            fc.set_current_folder(os.path.dirname(self.db.path))
        r = fc.run()
        path = fc.get_filename()
        fc.destroy()
        if r != Gtk.ResponseType.OK or not path:
            return False
        if not path.lower().endswith(".kdbx"):
            path += ".kdbx"
        try:
            self.db.file_mtime = None
            self.db.save(path)
            self.doc.pending, self.doc.dirty = [], False
        except Exception as e:
            dialogs.error(self, "Saving failed", str(e))
            return False
        self.db_path = path
        self.config.add_recent(path, self.keyfile)
        self.update_title()
        self.set_status("Saved as %s" % path, 4)
        return True

    def export_csv(self):
        if self.db is None:
            return
        if not dialogs.confirm(self, "Export to unencrypted CSV?",
                               "All passwords will be written in plain text. Anyone with access to the "
                               "file can read them. Delete it securely when you are done.", "Export",
                               destructive=True):
            return
        fc = Gtk.FileChooserDialog(title="Export CSV", transient_for=self, action=Gtk.FileChooserAction.SAVE)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Export", Gtk.ResponseType.OK)
        fc.set_current_name("export.csv")
        fc.set_do_overwrite_confirmation(True)
        r = fc.run()
        path = fc.get_filename()
        fc.destroy()
        if r != Gtk.ResponseType.OK:
            return
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w", newline="") as f:
                w = csv.writer(f)
                w.writerow(["Group", "Title", "Username", "Password", "URL", "Notes", "TOTP", "Last Modified"])
                for e in self.db.all_entries():
                    p = e.parent
                    cfg = otp.from_entry(e)
                    uri = ""
                    if cfg:
                        uri = "otpauth://totp/%s?secret=%s&period=%d&digits=%d&algorithm=%s" % (
                            GLib.uri_escape_string(e.title, None, False),
                            base64.b32encode(cfg.secret).decode().rstrip("="), cfg.period, cfg.digits, cfg.algo)
                    w.writerow([p.path() if p else "", e.title, e.username, e.password, e.url, e.notes, uri,
                                dialogs.fmt_dt(e.modified)])
            self.set_status("Exported to %s" % path, 5)
        except OSError as ex:
            dialogs.error(self, "Export failed", str(ex))

    # ===================================================== entries <-> files / tabs
    def _doc_name(self, d):
        if d.db is not None and d.db.name:
            return d.db.name
        return os.path.splitext(os.path.basename(d.path or "New Database"))[0]

    def _target_group(self):
        g = self.selected_group() if self.filter_mode is None else None
        rb = self.db.recycle_bin()
        if g is None or (rb is not None and g.is_descendant_of(rb)):
            g = self.db.root_group
        return g

    def _skip_duplicates(self, entries, db, db_name):
        """Warn when `db` already has some of `entries`. Returns the entries to
        copy (all, or only the new ones), or None if the user cancelled."""
        dups = kdbx.find_duplicates(entries, db)
        if not dups:
            return entries
        n, total = len(dups), len(entries)
        d = dialogs.make_dialog(self, "Already in this database", ok_label=None, width=560)  # has Cancel
        d.add_button("Import All Anyway" if n < total else "Import Anyway", 1)
        if n < total:
            skip = d.add_button("Skip Duplicates", Gtk.ResponseType.OK)
            skip.get_style_context().add_class("suggested-action")
            d.set_default_response(Gtk.ResponseType.OK)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(14)
        head = Gtk.Label(xalign=0)
        head.set_line_wrap(True)
        if n == total:
            text = ("<b>“%s” already has %s.</b>" %
                    (GLib.markup_escape_text(db_name), "this entry" if n == 1 else "all %d of these entries" % n))
        else:
            text = ("<b>%d of the %d entries are already in “%s”.</b>\nSkip them and add only the %d new %s?" %
                    (n, total, GLib.markup_escape_text(db_name), total - n,
                     "entry" if total - n == 1 else "entries"))
        head.set_markup(text)
        box.pack_start(head, False, False, 0)
        lines = []
        for e, have, same in dups:
            where = have.parent.path() if have.parent is not None else db_name
            what = "same password" if same else "same entry, but different details (maybe an older or newer password)"
            lines.append("•  <b>%s</b>  %s\n    <span alpha='70%%'>in %s · %s</span>" % (
                GLib.markup_escape_text(e.title or "(no title)"),
                GLib.markup_escape_text("(%s)" % e.username if e.username else ""),
                GLib.markup_escape_text(where), what))
        lst = Gtk.Label(xalign=0, yalign=0)
        lst.set_markup("\n".join(lines))
        lst.set_selectable(True)
        sw = Gtk.ScrolledWindow()
        sw.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        sw.set_min_content_height(min(60 + 44 * n, 260))
        sw.add(lst)
        box.pack_start(sw, True, True, 0)
        d.get_content_area().add(box)
        d.show_all()
        r = d.run()
        d.destroy()
        if r == 1:
            return entries
        if r == Gtk.ResponseType.OK:
            skip = {id(e.el) for e, _h, _s in dups}
            return [e for e in entries if id(e.el) not in skip]
        return None

    def export_entries(self):
        """Save the selected entries to a new, password-protected .kdbx file."""
        entries = self.selected_entries()
        if not entries or self.db is None:
            return
        fc = Gtk.FileChooserDialog(title="Export Entries", transient_for=self, action=Gtk.FileChooserAction.SAVE)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Export", Gtk.ResponseType.OK)
        fc.set_do_overwrite_confirmation(True)
        name = entries[0].title if len(entries) == 1 and entries[0].title else "Exported entries"
        fc.set_current_name(re.sub(r'[\\/:*?"<>|]+', "_", name) + ".kdbx")
        if self.db_path:
            fc.set_current_folder(os.path.dirname(self.db_path))
        r = fc.run()
        path = fc.get_filename()
        fc.destroy()
        if r != Gtk.ResponseType.OK or not path:
            return
        if not path.lower().endswith(".kdbx"):
            path += ".kdbx"
        if self._find_doc(path) is not None:
            dialogs.error(self, "That database is open", "Choose a new file name for the exported entries.")
            return
        d = dialogs.MasterKeyDialog(self, "Protect the Exported File",
                                    "The exported file is encrypted. Choose a password for it: you will need "
                                    "it to import %s into another database." %
                                    ("the entry" if len(entries) == 1 else "the %d entries" % len(entries)))
        res = d.get_result()
        d.destroy()
        if res is None:
            return
        pw, kf = res
        try:
            self._run_busy("Exporting…", lambda: kdbx.export_entries(entries, path, pw, kf), lambda _r: None)
            os.chmod(path, 0o600)
        except Exception as e:
            dialogs.error(self, "Export failed", str(e))
            return
        self.set_status("Exported %d %s to %s" % (len(entries), "entry" if len(entries) == 1 else "entries",
                                                  path.replace(os.path.expanduser("~"), "~")), 8)

    def import_entries(self):
        """Copy the entries of another .kdbx file into the selected group."""
        if self.db is None:
            return
        fc = Gtk.FileChooserDialog(title="Import Entries", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        f = Gtk.FileFilter()
        f.set_name("KeePass databases (*.kdbx)")
        f.add_pattern("*.kdbx")
        f.add_pattern("*.KDBX")
        fc.add_filter(f)
        if self.db_path:
            fc.set_current_folder(os.path.dirname(self.db_path))
        r = fc.run()
        path = fc.get_filename()
        fc.destroy()
        if r != Gtk.ResponseType.OK or not path:
            return
        open_doc = self._find_doc(path)
        if open_doc is not None and open_doc.db is not None:
            src = open_doc.db  # already unlocked in a tab
        else:
            src = None
            err = ""
            while src is None:
                res = dialogs.ask_key(self, path, err)
                if res is None:
                    return
                pw, kf = res
                try:
                    box = {}
                    self._run_busy("Opening…", lambda: kdbx.Database.open(path, pw if (pw or not kf) else None, kf),
                                   lambda db: box.setdefault("db", db))
                    src = box["db"]
                except kdbx.InvalidKey:
                    err = "Wrong password or key file."
                except Exception as e:
                    dialogs.error(self, "Could not open the file", str(e))
                    return
                finally:
                    self._default_status()
        if src is self.db:
            dialogs.error(self, "That is this database", "Choose another file to import from.")
            return
        entries = src.all_entries()
        if not entries:
            dialogs.message(self, "Nothing to import", "The file has no entries.")
            return
        target = self._target_group()
        entries = self._skip_duplicates(entries, self.db, self._doc_name(self.doc))
        if not entries:
            if entries is not None:
                self.set_status("Nothing imported", 6)
            return
        copies = kdbx.copy_entries(entries, target)
        self.changed()
        if len(copies) == 1:
            self._select_group_uuid(copies[0].parent.uuid)
            self._select_entry_el(copies[0].el)
        self.set_status("Imported %d %s into “%s”" % (len(copies), "entry" if len(copies) == 1 else "entries",
                                                    target.name), 8)

    def copy_to_other_database(self):
        others = [d for d in self.docs() if d is not self.doc and d.db is not None]
        if not self.selected_entries():
            return
        if not others:
            dialogs.message(self, "No other database is open",
                            "Open the other database (File → Open), unlock it, then come back to this tab "
                            "and copy the entry again. To move entries between computers, use "
                            "Entry → Export Selected Entries instead.")
            return
        if len(others) == 1:
            self.copy_to_database(others[0])
            return
        m = Gtk.Menu()
        for d in others:
            mi = Gtk.MenuItem(label=self._doc_name(d))
            mi.connect("activate", lambda _m, d=d: self.copy_to_database(d))
            m.append(mi)
        m.attach_to_widget(self.entry_view, None)
        m.show_all()
        m.popup_at_widget(self.entry_view, Gdk.Gravity.NORTH, Gdk.Gravity.NORTH, None)

    def copy_to_database(self, doc):
        entries = self.selected_entries()
        if not entries or doc.db is None or doc is self.doc:
            return
        entries = self._skip_duplicates(entries, doc.db, self._doc_name(doc))
        if not entries:
            return
        copies = kdbx.copy_entries(entries, doc.db.root_group)
        self._save_other(doc)
        self._update_tab(doc)
        self.set_status("Copied %d %s to “%s”" % (len(copies), "entry" if len(copies) == 1 else "entries",
                                                self._doc_name(doc)), 8)

    # ============================================================= lock/quit
    def toggle_lock(self):
        if self._any_unlocked():
            self.lock()
        elif self.db_path:
            self.present()
            self.unlock_pw.grab_focus()

    def lock(self, force=False):
        """Lock every open database (the whole workspace)."""
        start = self.doc
        for d in self.docs():
            if d.db is None:
                continue
            if d is not self.doc:
                if not d.db.modified:
                    d.db = None
                    self._update_tab(d)
                    continue
                self._switch_to(d)
            if not self._lock_current(force):
                return False
        if start is not self.doc and start.page in self._docs:
            self._switch_to(start)
        return True

    def _lock_current(self, force=False):
        if self.db is None:
            return True
        if self.db.modified and self.doc.pending and not force:
            r = self._ask_deletions("locking")
            if r == "cancel":
                return False
            if r == "save" and not self.save():
                return False
            if r == "discard" and self.doc.dirty and self.db.path and not self.save(quiet=True):
                return False
        elif self.db.modified:
            if self.config.get("auto_save") and self.db.path:
                self.save(quiet=True)
            elif not force:
                r = dialogs.ask_save(self, "Save changes before locking?")
                if r == "cancel":
                    return False
                if r == "save" and not self.save():
                    return False
            else:
                # forced (screensaver / idle): never lose data silently
                if self.db.path and not self.save(quiet=True):
                    return False
        self.clear_clipboard()
        for w in self.list_toplevels_modal():
            w.destroy()
        self.show_unlock(self.db_path)
        return True

    def list_toplevels_modal(self):
        return [w for w in Gtk.Window.list_toplevels()
                if w is not self and isinstance(w, Gtk.Dialog) and w.get_transient_for() is self]

    def quit_app(self):
        start = self.doc
        for d in self.docs():
            if d.db is not None and d.db.modified:
                self._switch_to(d)
                if not self.confirm_close():
                    return
        if start.page in self._docs:
            self._switch_to(start)
        self.config.data["open_files"] = [d.path for d in self.docs() if d.path]
        if self.db_path:
            self.config.data["last_file"] = self.db_path
        self.clear_clipboard()
        self._save_geometry()
        self.app.quit()

    def _on_delete(self, *_):
        if self.config.get("close_to_tray") and self.app.tray and self.app.tray.ok:
            self.hide()
            return True
        self.quit_app()
        return True

    def _on_state(self, w, ev):
        if ev.changed_mask & Gdk.WindowState.ICONIFIED and ev.new_window_state & Gdk.WindowState.ICONIFIED:
            if self.config.get("lock_on_minimize") and self._any_unlocked():
                GLib.idle_add(lambda: (self.lock(force=True), False)[1])
            if self.config.get("minimize_to_tray") and self.app.tray and self.app.tray.ok:
                GLib.idle_add(lambda: (self.hide(), False)[1])
        if ev.changed_mask & Gdk.WindowState.MAXIMIZED:
            self.config.data["maximized"] = bool(ev.new_window_state & Gdk.WindowState.MAXIMIZED)
        return False

    def _on_configure(self, w, ev):
        if not self.is_maximized():
            self.config.data["window"] = list(self.get_size())
        return False

    def _save_geometry(self):
        self.config.data["pane_groups"] = self.hpaned.get_position()
        h = self.vpaned.get_allocated_height()
        if h > 0 and self.details_box.get_visible():
            self.config.data["pane_details"] = int((h - self.vpaned.get_position()) * 2)
        self.config.data["column_widths"] = {k: c.get_width() for k, (c, r) in self.columns.items() if c.get_width() > 0}
        self.config.save()

    def toggle_visible(self):
        if self.get_visible() and self.is_active():
            self.hide()
        else:
            self.show()
            self.deiconify()
            self.present()

    def _on_any_event(self, w, ev):
        if ev.type in (Gdk.EventType.KEY_PRESS, Gdk.EventType.BUTTON_PRESS, Gdk.EventType.MOTION_NOTIFY,
                       Gdk.EventType.SCROLL):
            self._last_activity = time.time()
        return False

    def _idle_check(self):
        mins = self.config.get("lock_idle_minutes")
        if mins and self._any_unlocked() and time.time() - self._last_activity > mins * 60:
            self.lock(force=True)
        return True

    def on_screensaver(self, active):
        if active and self.config.get("lock_on_screensaver") and self._any_unlocked():
            self.lock(force=True)

    # ============================================================ groups tree
    def refresh_all(self):
        sel_uuid = self.current_group.uuid if self.current_group else None
        self._suspend_group_sel = True
        self.group_store.clear()
        if self.db is None:
            self._suspend_group_sel = False
            return
        rb = self.db.recycle_bin()

        def add(parent_it, g):
            it = self.group_store.append(parent_it, [g, g.name, theme.node_icon(g),
                                                     Pango.Weight.BOLD if parent_it is None else Pango.Weight.NORMAL])
            for c in g.groups:
                add(it, c)
            return it
        top = self.db.root_group
        add(None, top)

        def expand(model, path, it):
            g = model[it][0]
            if g.expanded or path.get_depth() == 1:
                self.group_view.expand_row(path, False)
            return False
        self.group_store.foreach(expand)
        self._suspend_group_sel = False
        if self.filter_mode is None and sel_uuid:
            if not self._select_group_uuid(sel_uuid):
                self._select_group_uuid(top.uuid)
        else:
            self.refresh_entries()

    def _select_group_uuid(self, uid):
        found = []

        def f(model, path, it):
            if model[it][0].uuid == uid:
                found.append(path)
                return True
            return False
        self.group_store.foreach(f)
        if found:
            self.group_view.expand_to_path(found[0])
            self.group_view.get_selection().select_path(found[0])
            self.group_view.scroll_to_cell(found[0], None, False, 0, 0)
            return True
        return False

    def _set_expanded(self, it, value):
        g = self.group_store[it][0]
        if g.expanded != value:
            g.expanded = value  # not a "real" modification, KeePass does not mark dirty either

    def selected_group(self):
        m, it = self.group_view.get_selection().get_selected()
        return m[it][0] if it else None

    def _on_group_sel(self, sel):
        if self._suspend_group_sel:
            return
        g = self.selected_group()
        if g is not None:
            self.current_group = g
            if self.filter_mode is not None:
                self.filter_mode = None
                self.search.handler_block_by_func(self._on_search)
                self.search.set_text("")
                self.search.handler_unblock_by_func(self._on_search)
                self._apply_column_visibility()
            self.refresh_entries()
        self.update_actions()

    def _group_button(self, view, ev):
        if ev.button == 3 and self.db is not None:
            r = view.get_path_at_pos(int(ev.x), int(ev.y))
            if r:
                view.get_selection().select_path(r[0])
            m = Gtk.Menu()
            self._fill_menu(m, [
                ("Add _Group…", self.add_group),
                ("_Edit Group…", self.edit_group),
                ("_Delete Group", self.delete_group),
                None,
                ("Add _Entry…", self.add_entry),
                None,
                ("Empty _Recycle Bin", self.empty_recycle_bin),
            ])
            m.attach_to_widget(view, None)
            m.show_all()
            m.popup_at_pointer(ev)
            return True
        return False

    # drag & drop
    def _entry_drag_get(self, view, ctx, data, info, t):
        self._drag_items = ("entries", self.selected_entries())
        data.set(data.get_target(), 8, b"entries")

    def _group_drag_get(self, view, ctx, data, info, t):
        g = self.selected_group()
        self._drag_items = ("group", g)
        data.set(data.get_target(), 8, b"group")

    def _group_drag_received(self, view, ctx, x, y, data, info, t):
        view.stop_emission_by_name("drag-data-received")
        r = view.get_dest_row_at_pos(x, y)
        items, self._drag_items = self._drag_items, None
        if not r or not items or self.db is None:
            Gtk.drag_finish(ctx, False, False, t)
            return
        path, pos = r
        target = self.group_store[path][0]
        if items[0] == "entries":
            for e in items[1]:
                if e.parent is None or e.parent.el is not target.el:
                    e.move_to(target)
            ok = bool(items[1])
        else:
            g = items[1]
            ok = g is not None and g.parent is not None and not target.is_descendant_of(g) and \
                (g.parent.el is not target.el)
            if ok:
                g.move_to(target)
        Gtk.drag_finish(ctx, ok, False, t)
        if ok:
            self.changed()

    # ============================================================== entries
    def refresh_entries(self):
        sel = {e.el for e in self.selected_entries()}
        self.entry_store.clear()
        self._otp_cache.clear()
        if self.db is None:
            self._clear_details()
            return
        if self.filter_mode is not None:
            entries = self._filtered_entries()
        else:
            g = self.current_group or self.db.root_group
            entries = g.entries
        hide_pw = self.config.get("hide_passwords")
        hide_user = self.config.get("hide_usernames")
        for e in entries:
            title = e.title
            user = MASK if (hide_user and e.username) else autotype.resolve_refs(self.db, e.username)
            pw = e.password
            pwd = (MASK if pw else "") if hide_pw else autotype.resolve_refs(self.db, pw)
            notes = e.notes.strip().split("\n", 1)[0] if e.notes else ""
            p = e.parent
            self.entry_store.append([e, title, user, pwd, e.url, notes, theme.node_icon(e),
                                     e.fg_color or None, e.bg_color or None, e.is_expired,
                                     dialogs.fmt_dt(e.modified), p.name if p else ""])
        # restore selection
        if sel:
            for row in self.entry_store:
                if row[C_OBJ].el in sel:
                    self.entry_view.get_selection().select_iter(row.iter)
        self._on_entry_sel(None)

    def _filtered_entries(self):
        mode = self.filter_mode
        db = self.db
        if mode[0] == "search":
            terms = mode[1]
            out = []
            for e in db.all_entries():
                g = e.parent
                if g is not None and not g.effective("searching_enabled"):
                    continue
                if e.matches(terms):
                    out.append(e)
            return out
        name = mode[1]
        allent = db.all_entries()
        now = kdbx.now_utc()
        if name == "all":
            return allent
        if name == "expired":
            return [e for e in allent if e.is_expired]
        if name == "soon":
            import datetime
            lim = now + datetime.timedelta(days=7)
            return [e for e in allent if e.expires and e.expiry_time and e.expiry_time <= lim]
        if name == "otp":
            return [e for e in allent if otp.has_otp(e)]
        if name == "dupes":
            seen = {}
            for e in allent:
                if e.password:
                    seen.setdefault(e.password, []).append(e)
            return [e for lst in seen.values() if len(lst) > 1 for e in lst]
        if name == "weak":
            return [e for e in allent if e.password and dialogs.quality_bits(e.password) < 64]
        if name == "recent":
            return sorted(allent, key=lambda e: e.modified or now, reverse=True)[:50]
        return allent

    def special(self, name):
        if self.db is None:
            return
        labels = {"all": "All entries", "expired": "Expired entries", "soon": "Expiring within 7 days",
                  "otp": "Entries with OTP", "dupes": "Entries sharing a password",
                  "weak": "Weak passwords (< 64 bits)", "recent": "Recently modified"}
        self.filter_mode = ("special", name, labels.get(name, name))
        self._suspend_group_sel = True
        self.group_view.get_selection().unselect_all()
        self._suspend_group_sel = False
        self._apply_column_visibility()
        self.refresh_entries()

    def _on_search(self, entry):
        if self.db is None:
            return
        text = entry.get_text().strip().lower()
        if text:
            self.filter_mode = ("search", text.split(), "Search: %s" % entry.get_text().strip())
            self._suspend_group_sel = True
            self.group_view.get_selection().unselect_all()
            self._suspend_group_sel = False
            self._apply_column_visibility()
            self.refresh_entries()
            if len(self.entry_store):
                self.entry_view.get_selection().select_path(Gtk.TreePath.new_first())
        else:
            self.filter_mode = None
            self._apply_column_visibility()
            if self.current_group is not None and self._select_group_uuid(self.current_group.uuid):
                pass
            else:
                self._select_group_uuid(self.db.root_group.uuid)
            self.refresh_entries()

    def _search_key(self, w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            w.set_text("")
            self.entry_view.grab_focus()
            return True
        if ev.keyval == Gdk.KEY_Down:
            self._focus_list()
            return True
        return False

    def _focus_list(self):
        if len(self.entry_store):
            if not self.selected_entries():
                self.entry_view.get_selection().select_path(Gtk.TreePath.new_first())
            self.entry_view.grab_focus()

    def focus_search(self):
        self.search.grab_focus()
        self.search.select_region(0, -1)

    def selected_entries(self):
        m, paths = self.entry_view.get_selection().get_selected_rows()
        return [m[p][C_OBJ] for p in paths]

    def selected_entry(self):
        s = self.selected_entries()
        return s[0] if s else None

    def select_all(self):
        self.entry_view.get_selection().select_all()

    def _on_entry_sel(self, sel):
        self.update_actions()
        self._default_status()
        self._show_details(self.selected_entry())

    def _otp_cell(self, col, cell, model, it, data):
        e = model[it][C_OBJ]
        key = id(e.el)
        if key not in self._otp_cache:
            self._otp_cache[key] = otp.from_entry(e)
        cfg = self._otp_cache[key]
        if cfg is None:
            cell.set_property("text", "")
        else:
            try:
                c = cfg.code()
                if len(c) == 6:
                    c = c[:3] + " " + c[3:]
                cell.set_property("text", "%s  %2ds" % (c, cfg.remaining()))
            except Exception:
                cell.set_property("text", "error")

    def _tick(self):
        if self.db is not None and self.stack.get_visible_child_name() == "db":
            if self.columns["otp"][0].get_visible() and any(v for v in self._otp_cache.values()):
                self.entry_view.queue_draw()
            if self._details_otp:
                lbl, bar, cfg = self._details_otp
                c = cfg.code()
                rem = cfg.remaining()
                lbl.set_text(c[:3] + " " + c[3:] if len(c) == 6 else c)
                bar.set_fraction(rem / cfg.period)
                bar.set_tooltip_text("%d s remaining" % rem)
        if self._clip_timer and self._clip_deadline:
            left = int(self._clip_deadline - time.time() + 0.99)
            if left > 0:
                self.status_right.set_text("Clipboard clears in %ds" % left)
        return True

    def _entry_button(self, view, ev):
        if self.db is None:
            return False
        r = view.get_path_at_pos(int(ev.x), int(ev.y))
        if ev.button == 3:
            if r and not view.get_selection().path_is_selected(r[0]):
                view.get_selection().unselect_all()
                view.get_selection().select_path(r[0])
            self._entry_menu(ev)
            return True
        return False

    def _entry_activated(self, view, path, col):
        """Double-click: URL opens in the default browser, user name / password /
        OTP are copied, anything else edits. Enter always edits."""
        ev = Gtk.get_current_event()
        if ev is not None and ev.type in (Gdk.EventType.BUTTON_PRESS, Gdk.EventType._2BUTTON_PRESS):
            key = next((k for k, (c, _r) in self.columns.items() if c is col), None)
            actions = {"user": self.copy_username, "pass": self.copy_password, "url": self.open_url,
                       "otp": self.copy_otp}
            if key in actions:
                actions[key]()
                return
        self.edit_entry()

    def _entry_menu(self, ev):
        n = len(self.selected_entries())
        has = bool(n)
        groups = []

        def walk(g, depth):
            groups.append((g, depth))
            for c in g.groups:
                walk(c, depth + 1)
        walk(self.db.root_group, 0)
        move = [("    " * d + g.name.replace("_", "__"), (lambda g=g: self.move_selected(g))) for g, d in groups]
        items = [
            ("Copy _User Name", self.copy_username, "<Control>b", "entry"),
            ("Copy _Password", self.copy_password, "<Control>c", "entry"),
            ("Copy _TOTP", self.copy_otp, "<Control>t", "entry"),
            ("Copy U_RL", self.copy_url, "<Control><Shift>u", "entry"),
            ("_Open URL", self.open_url, "<Control>u", "entry"),
            ("Perform Auto-T_ype", self.autotype_selected, "<Control>v", "entry"),
            None,
            ("_Add Entry…", self.add_entry, "<Control>i"),
            ("_Edit Entry…", self.edit_entry, "Return", "entry"),
            ("D_uplicate Entry", self.duplicate_entry, "<Control>k", "entry"),
            ("_Move to Group", move),
            ("Copy to Data_base", [(self._doc_name(d).replace("_", "__"), (lambda d=d: self.copy_to_database(d)))
                                   for d in self.docs() if d is not self.doc and d.db is not None]
             or [("(unlock another database in a tab first)", None)]),
            ("E_xport Selected Entry…" if n <= 1 else "E_xport %d Selected Entries…" % n,
             self.export_entries, None, "entry"),
            ("_Import Entries…", self.import_entries),
            None,
            ("_Delete Entry" if n <= 1 else "_Delete %d Entries" % n, self.delete_entries, "Delete", "entry"),
        ]
        m = Gtk.Menu()
        self._fill_menu(m, items)
        before = len(self.action_widgets)
        for mi in m.get_children():
            if not has and isinstance(mi, Gtk.MenuItem) and mi.get_label() not in ("_Add Entry…", "_Import Entries…"):
                if not isinstance(mi, Gtk.SeparatorMenuItem):
                    mi.set_sensitive(False)
        # do not keep transient menu items in the action list
        self.action_widgets = [a for a in self.action_widgets if a[0].get_parent() is not m]
        del before
        m.attach_to_widget(self.entry_view, None)
        m.show_all()
        m.popup_at_pointer(ev)

    def move_selected(self, group):
        moved = False
        for e in self.selected_entries():
            if e.parent is None or e.parent.el is not group.el:
                e.move_to(group)
                moved = True
        if moved:
            self.changed()

    # ------------------------------------------------------------- details
    def _clear_details(self):
        self._details_otp = None
        for c in self.details.get_children():
            self.details.remove(c)

    def _show_details(self, e):
        self._clear_details()
        if e is None or self.db is None:
            if self.db is not None and self.current_group is not None and self.filter_mode is None:
                g = self.current_group
                h = Gtk.Label(label=g.name, xalign=0)
                h.get_style_context().add_class("heading")
                self.details.pack_start(h, False, False, 0)
                info = Gtk.Label(label="%d entries  ·  %d groups%s" % (
                    len(g.entries), len(g.groups), ("\n\n" + g.notes) if g.notes else ""), xalign=0)
                info.get_style_context().add_class("dim")
                info.set_line_wrap(True)
                info.set_selectable(True)
                self.details.pack_start(info, False, False, 6)
                self.details.show_all()
            return
        top = Gtk.Box(spacing=10)
        img = Gtk.Image.new_from_pixbuf(theme.node_icon(e, 32))
        top.pack_start(img, False, False, 0)
        tb = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        h = Gtk.Label(label=e.title or "(untitled)", xalign=0)
        h.get_style_context().add_class("heading")
        h.set_selectable(True)
        h.set_ellipsize(Pango.EllipsizeMode.END)
        tb.pack_start(h, False, False, 0)
        p = e.parent
        sub = Gtk.Label(label=p.path() if p else "", xalign=0)
        sub.get_style_context().add_class("dim")
        sub.set_ellipsize(Pango.EllipsizeMode.START)
        tb.pack_start(sub, False, False, 0)
        top.pack_start(tb, True, True, 0)
        if e.tags:
            for t in e.tags[:6]:
                c = Gtk.Label(label=t)
                c.get_style_context().add_class("chip")
                c.set_valign(Gtk.Align.CENTER)
                top.pack_end(c, False, False, 0)
        self.details.pack_start(top, False, False, 0)

        g = Gtk.Grid()
        g.set_column_spacing(14)
        g.set_row_spacing(6)
        g.set_margin_top(12)
        row = [0]

        def add(label, value, secret=False, copy=None, link=False, widget=None):
            k = Gtk.Label(label=label, xalign=1)
            k.get_style_context().add_class("key")
            k.set_valign(Gtk.Align.START)
            g.attach(k, 0, row[0], 1, 1)
            if widget is None:
                v = Gtk.Label(xalign=0)
                v.set_selectable(True)
                v.set_line_wrap(True)
                v.set_line_wrap_mode(Pango.WrapMode.WORD_CHAR)
                v.set_hexpand(True)
                v.get_style_context().add_class("value")
                if link and value:
                    href = value if "://" in value else "https://" + value
                    v.set_markup('<a href="%s">%s</a>' % (GLib.markup_escape_text(href),
                                                          GLib.markup_escape_text(value)))
                    v.connect("activate-link", lambda _l, _u: (self.open_url(e), True)[1])
                else:
                    v.set_text(MASK if (secret and value) else value)
                widget = v
                if secret and value:
                    eye = Gtk.Button.new_from_icon_name("view-reveal-symbolic", Gtk.IconSize.MENU)
                    eye.get_style_context().add_class("flat")
                    eye.set_tooltip_text("Show / hide")

                    def toggle(_b, lab=v, val=value):
                        lab.set_text(val if lab.get_text() == MASK else MASK)
                    eye.connect("clicked", toggle)
                    v.set_hexpand(False)
                    box = Gtk.Box(spacing=4)
                    box.set_hexpand(True)
                    box.pack_start(v, False, False, 0)
                    box.pack_start(eye, False, False, 0)
                    widget = box
            if copy:
                cb = Gtk.Button.new_from_icon_name("edit-copy-symbolic", Gtk.IconSize.MENU)
                cb.get_style_context().add_class("flat")
                cb.set_tooltip_text("Copy")
                cb.set_valign(Gtk.Align.START)
                cb.connect("clicked", lambda _b: copy())
                box = Gtk.Box(spacing=4)
                box.pack_start(widget, True, True, 0)
                box.pack_start(cb, False, False, 0)
                widget = box
            g.attach(widget, 1, row[0], 1, 1)
            row[0] += 1

        hide = self.config.get("hide_passwords")
        if e.username:
            add("User name", autotype.resolve_refs(self.db, e.username), copy=self.copy_username)
        if e.password:
            add("Password", autotype.resolve_refs(self.db, e.password), secret=hide, copy=self.copy_password)
        cfg = otp.from_entry(e)
        if cfg:
            ob = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
            ol = Gtk.Label(xalign=0)
            ol.get_style_context().add_class("otp-code")
            ol.set_selectable(True)
            pb = Gtk.ProgressBar()
            pb.set_size_request(120, -1)
            pb.set_halign(Gtk.Align.START)
            ob.pack_start(ol, False, False, 0)
            ob.pack_start(pb, False, False, 0)
            self._details_otp = (ol, pb, cfg)
            c = cfg.code()
            ol.set_text(c[:3] + " " + c[3:] if len(c) == 6 else c)
            pb.set_fraction(cfg.remaining() / cfg.period)
            add("TOTP", None, widget=ob, copy=self.copy_otp)
        if e.url:
            add("URL", e.url, link=True, copy=self.copy_url)
        if e.expires:
            add("Expires", dialogs.fmt_dt(e.expiry_time) + ("  (expired)" if e.is_expired else ""))
        for k, v, prot in e.custom_fields():
            if k in otp.OTP_FIELDS or k.startswith("KPEX_") or k.startswith("KPH:"):
                continue
            add(k, v, secret=prot, copy=lambda v=v, k=k: self.copy(v, k))
        atts = e.attachments()
        if atts:
            add("Attachments", ", ".join(n for n, _d in atts))
        add("Modified", dialogs.fmt_dt(e.modified))
        add("Created", dialogs.fmt_dt(e.created))
        if e.notes:
            add("Notes", e.notes)
        self.details.pack_start(g, False, False, 0)
        self.details.show_all()
        self._tick()

    # ========================================================== clipboard
    def copy(self, text, what="Value"):
        if not text:
            self.set_status("%s is empty" % what, 3)
            return
        self._clip_own(text)
        self._back_to_previous()
        timeout = int(self.config.get("clipboard_timeout"))
        if self._clip_timer:
            GLib.source_remove(self._clip_timer)
            self._clip_timer = None
        if timeout > 0:
            self._clip_deadline = time.time() + timeout
            self._clip_timer = GLib.timeout_add_seconds(timeout, self._clip_expire)
            self.set_status("%s copied to clipboard" % what, 4)
        else:
            self.set_status("%s copied to clipboard" % what, 4)

    def _back_to_previous(self):
        """After a copy from the main window, hand focus back to the window the
        user was working in (e.g. the browser), so Velkey falls behind it."""
        if not self.config.get("back_after_copy") or not self.is_active():
            return  # copied from a dialog, or Velkey is not in front anyway
        if self.app.probe.focus_previous(os.getpid()):
            return
        win = self.get_window()
        if win is not None and autotype.session_type() == "x11":
            win.lower()

    # The clipboard is offered with KDE's password-manager hint so clipboard
    # managers (Klipper, CopyQ…) do not keep the password in their history.
    # Velkey remembers whether it still owns the clipboard instead of reading
    # it back: on Wayland another app's clipboard cannot be read without focus.
    _CLIP_TARGETS = ("UTF8_STRING", "text/plain;charset=utf-8", "text/plain", "TEXT", "STRING", "COMPOUND_TEXT",
                     "x-kde-passwordManagerHint")

    def _clip_widget(self):
        w = getattr(self, "_clip_w", None)
        if w is None:
            w = self._clip_w = Gtk.Invisible()
            w.connect("selection-get", self._clip_get)
            w.connect("selection-clear-event", self._clip_lost)
        return w

    def _clip_own(self, text):
        sel = Gdk.SELECTION_CLIPBOARD
        self._clip_text = text
        if autotype.IS_MAC:  # macOS: GTK's own clipboard talks to the system pasteboard
            self._clip_owned = False
            Gtk.Clipboard.get(sel).set_text(text, -1)
            return
        w = self._clip_widget()
        # same order as GtkClipboard: take ownership first, then announce the
        # formats (the other way round, Wayland drops the very first copy)
        self._clip_owned = Gtk.selection_owner_set(w, sel, Gtk.get_current_event_time())
        if not self._clip_owned:  # fall back to plain GTK (no hint)
            Gtk.Clipboard.get(sel).set_text(text, -1)
            return
        Gtk.selection_clear_targets(w, sel)
        for i, t in enumerate(self._CLIP_TARGETS):
            Gtk.selection_add_target(w, sel, Gdk.Atom.intern(t, False), i)

    def _clip_get(self, w, data, info, _time):
        if self._CLIP_TARGETS[info] == "x-kde-passwordManagerHint":
            data.set(data.get_target(), 8, b"secret")
        else:
            data.set_text(self._clip_text or "", -1)  # blank once cleared

    def _clip_lost(self, *_):
        # something else was copied: it is not ours to clear any more
        self._clip_owned = False
        self._clip_text = None
        return False

    def _clip_expire(self):
        self._clip_timer = None
        self.clear_clipboard()
        return False

    def clear_clipboard(self):
        if self._clip_text is None:
            return
        ours = self._clip_text
        self._clip_text = None
        self._clip_deadline = 0
        self.status_right.set_text(self._hotkey_text())
        if getattr(self, "_clip_owned", False):
            # Replace the password with blank text (still marked secret) instead of
            # emptying the clipboard: an empty clipboard makes Klipper put back the
            # newest item of its history, which would then be pasted.
            self._clip_own("")
            self._clip_text = None
        else:
            # fallback path (plain set_text): clear only if it still holds our text
            cb = Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD)
            if cb.wait_is_text_available() and cb.wait_for_text() == ours:
                cb.set_text("", -1)
                cb.clear()
        if self._clip_timer:
            GLib.source_remove(self._clip_timer)
            self._clip_timer = None

    def _hotkey_text(self):
        hk = self.app.hotkey
        if hk is None or autotype.IS_MAC:
            return ""
        if hk.backend == "external":
            return "Global Auto-Type: bind  velkey --auto-type"
        if hk.error:
            return "Auto-Type hot key: %s" % hk.error
        return "Global Auto-Type: %s" % hk.spec

    def _entry_arg(self, e):
        return e if isinstance(e, kdbx.Entry) else self.selected_entry()

    def copy_username(self, e=None):
        e = self._entry_arg(e)
        if e:
            self.copy(autotype.resolve_refs(self.db, e.username), "User name")
            e.touch(modify=False)

    def copy_password(self, e=None):
        e = self._entry_arg(e)
        if e:
            self.copy(autotype.resolve_refs(self.db, e.password), "Password")
            e.touch(modify=False)

    def copy_url(self, e=None):
        e = self._entry_arg(e)
        if e:
            self.copy(e.url, "URL")

    def copy_otp(self, e=None):
        e = self._entry_arg(e)
        if not e:
            return
        cfg = otp.from_entry(e)
        if cfg is None:
            self.set_status("This entry has no TOTP configured (Edit Entry → OTP)", 4)
            return
        left = cfg.remaining()
        if left <= 4:
            # the code expires before it can be pasted: copy the next one right away
            # (copying later, from a timer, could happen after you left Velkey)
            self.copy(cfg.code(time.time() + cfg.period), "TOTP code")
            self.set_status("Next TOTP code copied (the current one expires in %d s)" % left, 5)
            return
        self.copy(cfg.code(), "TOTP code")

    def open_url(self, e=None):
        e = self._entry_arg(e)
        if not e:
            return
        url = kdbx._text(e.el, "OverrideURL") or e.url
        if not url:
            return

        def sub(m):
            v = autotype.field_value(e, m.group(1).upper())
            return v if v is not None else m.group(0)
        url = re.sub(r"\{([^{}]+)\}", sub, url)
        try:
            if url.lower().startswith("cmd://"):
                subprocess.Popen(shlex.split(url[6:]), start_new_session=True)
            else:
                if "://" not in url and not url.startswith("mailto:"):
                    url = "https://" + url
                Gio.AppInfo.launch_default_for_uri(url, None)
            e.touch(modify=False)
        except Exception as ex:
            dialogs.error(self, "Could not open URL", str(ex))

    # ========================================================== auto-type
    def autotype_selected(self):
        """Toolbar / Ctrl+V: type the selected entry into the window behind Velkey,
        but only if that window belongs to the entry (title, URL / domain or a
        window association), so a password is never typed into the wrong site."""
        e = self.selected_entry()
        if not e or self.app.typer.busy:
            return
        self.iconify()  # hand focus back to the window the user came from
        GLib.timeout_add(600, lambda: (self.app.probe.get_active(
            lambda title, cls="": self._autotype_selected_into(e, title, cls)), False)[1])

    def _autotype_selected_into(self, e, title, cls):
        require = self.config.get("autotype_require_match")
        if (cls and "velkey" in cls.lower()) or (title and title.endswith("— " + APP_NAME)):
            self._autotype_refused("Click into the login form of the site first, then use Auto-Type.")
            return
        seq = autotype.match_sequence(e, title, True, browser.url_for_window(title))
        if seq is None and require:
            if not title:
                why = "Velkey could not detect the active window, so it cannot check that it belongs to “%s”." % e.title
            else:
                why = "The active window “%s” does not match “%s” (title, URL or window association)." % (title, e.title)
            self._autotype_refused(why + " Nothing was typed.")
            return
        def done(err):
            self._autotype_done(err)
            if not self.config.get("autotype_minimize_after"):
                self.deiconify()
        self.app.typer.type_sequence(e, seq or e.effective_sequence(), done, initial_delay=0.1,
                                     field_check=self._field_check(self.app.probe.last_pid))
        e.touch(modify=False)

    def _autotype_refused(self, why):
        self.deiconify()
        self.present()
        self.set_status("Auto-Type cancelled: " + why, 10)
        self.app.notify("Auto-Type cancelled", why)

    def _field_check(self, pid):
        """Checker for 'only type the password into a password box', or None."""
        if not self.config.get("autotype_password_field_only"):
            return None
        tr = self.app.focus
        return lambda wait: tr.wait_for_password_field(pid) if wait else tr.field_from_thread(pid)

    def _autotype_done(self, err):
        if err:
            self.set_status("Auto-Type: %s" % err, 10)
            self.app.notify("Auto-Type", err)

    def global_autotype(self):
        if self.app.typer.busy:
            return
        if getattr(self, "_picker", None) is not None:
            # a picker is already open: bring it back instead of opening another
            self._picker.present()
            self.app.probe.activate(None, self._picker.get_title())
            return
        self.app.probe.get_active(self._global_autotype_with)

    def _global_autotype_with(self, title, cls="", _tries=0):
        if (cls and "velkey" in cls.lower()) or (title and title.endswith("— " + APP_NAME)):
            # Velkey itself is focused: behave like KeePass and ignore
            return
        if not self._any_unlocked():
            if not self.db_path:
                self.present()
                return
            self._pending_autotype = title
            self.show()
            self.deiconify()
            self.present()
            self.set_status("Unlock the database to continue Auto-Type", 6)
            self.unlock_pw.grab_focus()
            return
        target = self.app.probe.last_id
        check = self._field_check(self.app.probe.last_pid)
        hk = self.app.hotkey
        wait = hk.wait_released if hk is not None else None
        url = browser.url_for_window(title)
        open_dbs = [d.db for d in self.docs() if d.db is not None]  # every unlocked tab
        matches = []
        for db in open_dbs:
            matches += autotype.find_matches(db, title, self.config.get("autotype_match_url"), url)
        require = self.config.get("autotype_require_match")
        if not matches and not url and title and _tries < 2:
            # browsers save the tab list ~2.5 s after a page loads: look again shortly
            GLib.timeout_add(1300, lambda: (self._global_autotype_with(title, cls, _tries + 1), False)[1])
            return
        if not matches and (title or require):
            # never type a password into a window that does not belong to it
            where = " (%s)" % url.split("://", 1)[-1].split("/", 1)[0] if url else ""
            msg = ("No entry matches the window “%s”%s. Nothing was typed." % (title, where)) if title else \
                  "The active window could not be detected. Nothing was typed."
            self.app.notify("Auto-Type", msg)
            self.set_status(msg, 8)
            return
        if len(matches) == 1 and not self.config.get("autotype_always_show_dialog"):
            e, seq = matches[0]
            self.app.typer.type_sequence(e, seq, self._autotype_done, initial_delay=0.05, wait_before=wait,
                                         field_check=check)
            e.touch(modify=False)
            return

        def pick(e, seq):
            e.touch(modify=False)
            # the picker had focus: give it back to the target window before typing
            self.app.probe.activate(target, title)
            GLib.timeout_add(300, lambda: (self.app.typer.type_sequence(e, seq, self._autotype_done,
                                                                        initial_delay=0.15,
                                                                        field_check=check), False)[1])
        picker = dialogs.AutoTypePicker(self.db or open_dbs[0], matches or None, title, pick)
        self._picker = picker
        picker.connect("destroy", lambda *_: setattr(self, "_picker", None))
        # Wayland: a window from a background app does not get focus on its own
        GLib.timeout_add(150, lambda: (self.app.probe.activate(None, picker.get_title()), False)[1])

    # ================================================================ edit
    def add_entry(self):
        if self.db is None:
            return
        g = self.current_group if (self.current_group is not None and self.filter_mode is None) else self.db.root_group
        e = g.add_entry()
        e.username = self.db.meta_get("DefaultUserName")
        e.password = dialogs.generate_password(self.config.get("gen"))
        d = dialogs.EntryDialog(self, e, self.config, is_new=True)
        r = d.run()
        if r == Gtk.ResponseType.OK:
            d.apply()
            e.el.find("History").clear() if e.el.find("History") is not None else None
            d.destroy()
            self.changed()
            self._select_entry_el(e.el)
            self._autosave_entry()
        else:
            d.destroy()
            g.el.remove(e.el)
            self.db.invalidate()
            self.refresh_entries()

    def _select_entry_el(self, el):
        for row in self.entry_store:
            if row[C_OBJ].el is el:
                sel = self.entry_view.get_selection()
                sel.unselect_all()
                sel.select_iter(row.iter)
                self.entry_view.scroll_to_cell(row.path, None, False, 0, 0)
                break

    def edit_entry(self):
        e = self.selected_entry()
        if e is None:
            return
        d = dialogs.EntryDialog(self, e, self.config)
        r = d.run()
        if r == Gtk.ResponseType.OK:
            changed = d.apply()
            d.destroy()
            theme.clear_cache()
            if changed:
                self.changed()
                self._select_entry_el(e.el)
                self._autosave_entry()
        else:
            d.destroy()
            if self.db.modified:
                self.update_title()

    def duplicate_entry(self):
        sel = self.selected_entries()
        if not sel:
            return
        last = None
        for e in sel:
            last = e.duplicate()
        self.changed()
        if last:
            self._select_entry_el(last.el)
        self._autosave_entry()

    def delete_entries(self):
        sel = self.selected_entries()
        if not sel or self.db is None:
            return
        rb = self.db.recycle_bin()
        rb_enabled = kdbx._bool(self.db.meta_get("RecycleBinEnabled", "True"), True)
        in_rb = rb is not None and all(e.parent and e.parent.is_descendant_of(rb) for e in sel)
        what = ("“%s”" % sel[0].title) if len(sel) == 1 else "%d entries" % len(sel)
        log = kdbx.ChangeLog(self.db)
        if rb_enabled and not in_rb:
            if not dialogs.confirm(self, "Move %s to the recycle bin?" % what, None, "Move to Recycle Bin"):
                return
            rb = log.recycle_bin()
            for e in sel:
                log.move(e, rb)
        else:
            if not dialogs.confirm(self, "Permanently delete %s?" % what,
                                   "It is not saved until you press Ctrl+S, and Ctrl+Z brings it back until then.",
                                   "Delete", destructive=True):
                return
            for e in sel:
                log.remove(e)
        self._deleted(log, what)

    def add_group(self):
        if self.db is None:
            return
        parent = self.selected_group() or self.db.root_group
        g = parent.add_group("New Group")
        d = dialogs.GroupDialog(self, g, is_new=True)
        if d.run() == Gtk.ResponseType.OK:
            d.apply()
            d.destroy()
            self.current_group = g
            self.changed()
        else:
            d.destroy()
            parent.el.remove(g.el)
            self.db.invalidate()

    def edit_group(self):
        g = self.selected_group()
        if g is None:
            return
        d = dialogs.GroupDialog(self, g)
        if d.run() == Gtk.ResponseType.OK:
            d.apply()
            theme.clear_cache()
            self.changed()
        d.destroy()

    def delete_group(self):
        g = self.selected_group()
        if g is None or self.db is None:
            return
        if g.parent is None:
            dialogs.error(self, "The root group cannot be deleted.")
            return
        rb = self.db.recycle_bin()
        rb_enabled = kdbx._bool(self.db.meta_get("RecycleBinEnabled", "True"), True)
        in_rb = rb is not None and g.is_descendant_of(rb)
        n = len(self.db.all_entries(include_recycle=True, group=g))
        log = kdbx.ChangeLog(self.db)
        parent = g.parent
        if rb_enabled and not in_rb:
            if not dialogs.confirm(self, "Move group “%s” to the recycle bin?" % g.name,
                                   "It contains %d entries." % n, "Move to Recycle Bin"):
                return
            log.move(g, log.recycle_bin())
        else:
            if not dialogs.confirm(self, "Permanently delete group “%s”?" % g.name,
                                   "It contains %d entries. It is not saved until you press Ctrl+S, and "
                                   "Ctrl+Z brings it back until then." % n, "Delete", destructive=True):
                return
            log.remove(g)
        self.current_group = parent if parent is not None and not in_rb else self.db.root_group
        self._deleted(log, "group “%s”" % g.name)

    def _deleted(self, log, what):
        """A deletion happened: keep it unsaved (and undoable) until Ctrl+S."""
        if not log.ops:
            return
        self.doc.pending.append(log)
        self.changed(deletion=True)
        self.set_status("Deleted %s. Not saved yet: Ctrl+Z to undo, Ctrl+S to save." % what, 10)

    def empty_recycle_bin(self):
        if self.db is None:
            return
        rb = self.db.recycle_bin()
        if rb is None or (not rb.entries and not rb.groups):
            self.set_status("The recycle bin is empty", 3)
            return
        n = len(self.db.all_entries(include_recycle=True, group=rb))
        if not dialogs.confirm(self, "Empty the recycle bin?",
                               "%d entries will be permanently deleted. This cannot be undone." % n,
                               "Empty Recycle Bin", destructive=True):
            return
        log = kdbx.ChangeLog(self.db)
        for c in list(rb.el):
            if c.tag == "Entry":
                log.remove(kdbx.Entry(self.db, c))
            elif c.tag == "Group":
                log.remove(kdbx.Group(self.db, c))
        self._deleted(log, "the recycle bin contents")

    # ================================================================ tools
    def database_settings(self):
        if self.db is None:
            return
        d = dialogs.DatabaseSettingsDialog(self, self.db)
        if d.run() == Gtk.ResponseType.OK:
            d.apply()
            self.changed()
        d.destroy()

    def change_master_key(self):
        if self.db is None:
            return
        d = dialogs.MasterKeyDialog(self)
        res = d.get_result()
        d.destroy()
        if res is None:
            return
        pw, kf = res
        self.db.set_credentials(pw, kf)
        self.keyfile = kf
        self.config.add_recent(self.db.path, kf)
        self.changed(refresh=False)
        if self.save(keep_deletions=True):
            dialogs.message(self, "Master key changed", "The database has been saved with the new master key.")

    def password_generator(self):
        d = dialogs.GeneratorDialog(self, self.config, use_mode=False)
        d.run()
        d.result()
        d.destroy()

    def options(self):
        hk = self.app.hotkey.backend if self.app.hotkey else "none"
        old_hk = self.config.get("autotype_hotkey")
        if self.app.hotkey and self.app.hotkey.spec != old_hk:
            old_hk = self.app.hotkey.spec  # changed in the desktop's own settings
            self.config.set("autotype_hotkey", old_hk)
        d = dialogs.OptionsDialog(self, self.config, "%s, typing via %s" % (
            hk, getattr(self.app.typer.backend(), "name", "none")), hotkey=self.app.hotkey)
        if d.run() == Gtk.ResponseType.OK:
            d.apply()
            theme.apply(self.config.get("accent"))
            self.app.update_tray()
            if self.config.get("autotype_hotkey") != old_hk:
                self.app.restart_hotkey()
                hk = self.app.hotkey
                if hk.error:
                    dialogs.error(self, "Could not set the global hot key", hk.error)
                else:
                    self.set_status("Global Auto-Type is now %s" % hk.spec, 5)
            self.status_right.set_text(self._hotkey_text())
            self._apply_column_visibility()
            self.refresh_entries()
        d.destroy()

    def show_shortcuts(self):
        rows = [
            ("Ctrl+Alt+Shift+Z", "Global Auto-Type (works in any application)"),
            ("Ctrl+V", "Perform Auto-Type with the selected entry"),
            ("Ctrl+B", "Copy user name"), ("Ctrl+C", "Copy password"), ("Ctrl+T", "Copy TOTP code"),
            ("Ctrl+U", "Open URL"), ("Ctrl+Shift+U", "Copy URL"),
            ("Ctrl+I", "Add entry"), ("Enter", "Edit entry"), ("Ctrl+K", "Duplicate entry"),
            ("Delete", "Delete entry (saved only with Ctrl+S)"), ("Ctrl+Z", "Undo delete"), ("F2", "Edit group"),
            ("Ctrl+F / Ctrl+E", "Search"), ("Ctrl+L", "Lock workspace"), ("Ctrl+S", "Save"),
            ("Ctrl+O", "Open database (new tab)"), ("Ctrl+N", "New database"), ("Ctrl+W", "Close database tab"),
            ("Ctrl+Tab", "Next database tab"), ("Ctrl+Shift+Tab", "Previous database tab"),
            ("Ctrl+G", "Password generator"), ("Ctrl+Q", "Exit"),
            ("Double-click", "User name / password: copy · URL: open · Title: edit"),
        ]
        d = dialogs.make_dialog(self, "Keyboard Shortcuts", ok_label=None, width=520)
        g = dialogs.grid()
        for i, (k, v) in enumerate(rows):
            kl = Gtk.Label(label=k, xalign=1)
            kl.get_style_context().add_class("mono")
            g.attach(kl, 0, i, 1, 1)
            vl = Gtk.Label(label=v, xalign=0)
            vl.get_style_context().add_class("dim")
            g.attach(vl, 1, i, 1, 1)
        d.get_content_area().add(g)
        d.get_widget_for_response(Gtk.ResponseType.CANCEL).set_label("Close")
        d.show_all()
        d.run()
        d.destroy()

    def about(self):
        d = Gtk.AboutDialog(transient_for=self, modal=True)
        d.set_program_name(APP_NAME)
        d.set_version(self.app.version)
        d.set_logo_icon_name("velkey")
        d.set_comments("A fast, native KeePass-compatible password manager for Linux.\n"
                       "Reads and writes KDBX 3.1 / 4.x databases.")
        d.set_license_type(Gtk.License.GPL_3_0)
        d.run()
        d.destroy()

    # ============================================================ keyboard
    SHORTCUTS = [
        ("<Control>n", "new_database", None, True), ("<Control>o", "open_database", None, True),
        ("<Control>s", "save", "db", True), ("<Control><Shift>s", "save_as", "db", True),
        ("<Control>w", "close_database", "db", True), ("<Control>l", "toggle_lock", None, True),
        ("<Control>q", "quit_app", None, True), ("<Control>f", "focus_search", "db", True),
        ("<Control>e", "focus_search", "db", True), ("<Control>g", "password_generator", None, True),
        ("<Control>i", "add_entry", "db", False), ("<Control>k", "duplicate_entry", "entry", False),
        ("Delete", "delete_entries", "entry", False), ("<Control>b", "copy_username", "entry", False),
        ("<Control>c", "copy_password", "entry", False), ("<Control>t", "copy_otp", "entry", False),
        ("<Control>u", "open_url", "entry", False), ("<Control><Shift>u", "copy_url", "entry", False),
        ("<Control>v", "autotype_selected", "entry", False), ("<Control>a", "select_all", "db", False),
        ("F2", "edit_group", "group", True), ("F1", "show_shortcuts", None, True),
        ("<Control>z", "undo_delete", "db", False),
        ("<Control>Tab", "next_tab", None, True), ("<Control><Shift>Tab", "prev_tab", None, True),
        ("<Control>Page_Down", "next_tab", None, True), ("<Control>Page_Up", "prev_tab", None, True),
    ]

    def _on_key(self, w, ev):
        focus = self.get_focus()
        in_text = isinstance(focus, (Gtk.Entry, Gtk.TextView))
        mods = ev.state & (Gdk.ModifierType.CONTROL_MASK | Gdk.ModifierType.SHIFT_MASK |
                           Gdk.ModifierType.MOD1_MASK | Gdk.ModifierType.SUPER_MASK)
        if autotype.IS_MAC and ev.state & (Gdk.ModifierType.META_MASK | Gdk.ModifierType.MOD2_MASK):
            mods |= Gdk.ModifierType.CONTROL_MASK  # ⌘ works like Ctrl
        kv = Gdk.keyval_to_lower(ev.keyval)
        if ev.keyval == Gdk.KEY_ISO_Left_Tab:
            kv = Gdk.KEY_Tab
        for accel, meth, cond, in_text_ok in self.SHORTCUTS:
            k, m = Gtk.accelerator_parse(accel)
            if k == kv and m == mods:
                if in_text and not in_text_ok:
                    return False
                if focus is self.group_view and meth == "delete_entries":
                    meth = "delete_group"
                if cond == "db" and self.db is None:
                    return False
                if cond == "entry" and not self.selected_entries():
                    return False
                if cond == "group" and self.selected_group() is None:
                    return False
                getattr(self, meth)()
                return True
        # type-to-search from the entry list
        if (focus is self.entry_view or focus is self.group_view) and not mods and self.db is not None:
            ch = Gdk.keyval_to_unicode(ev.keyval)
            if ch and chr(ch).isprintable() and not chr(ch).isspace():
                self.search.grab_focus()
                self.search.set_text(chr(ch))
                self.search.set_position(-1)
                return True
        return False
