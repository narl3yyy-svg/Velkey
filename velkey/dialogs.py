"""Dialogs: entry/group editors, password generator, icon picker, database
settings, master key, options, auto-type entry selection."""
import copy
import datetime
import math
import os
import secrets
import string
import xml.etree.ElementTree as ET

import gi
gi.require_version("Gtk", "3.0")
from gi.repository import Gdk, GLib, Gtk, Pango  # noqa: E402

from . import autotype, kdbx, otp, theme


# ---------------------------------------------------------------------------
# small helpers
# ---------------------------------------------------------------------------
def message(parent, text, secondary=None, kind=Gtk.MessageType.INFO):
    d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=kind,
                          buttons=Gtk.ButtonsType.OK, text=text)
    if secondary:
        d.format_secondary_text(secondary)
    d.run()
    d.destroy()


def error(parent, text, secondary=None):
    message(parent, text, secondary, Gtk.MessageType.ERROR)


def confirm(parent, text, secondary=None, ok_label="OK", destructive=False):
    d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION,
                          buttons=Gtk.ButtonsType.NONE, text=text)
    if secondary:
        d.format_secondary_text(secondary)
    d.add_button("Cancel", Gtk.ResponseType.CANCEL)
    b = d.add_button(ok_label, Gtk.ResponseType.OK)
    b.get_style_context().add_class("destructive-action" if destructive else "suggested-action")
    d.set_default_response(Gtk.ResponseType.OK)
    r = d.run()
    d.destroy()
    return r == Gtk.ResponseType.OK


def ask_save(parent, text):
    """Returns 'save', 'discard' or 'cancel'."""
    d = Gtk.MessageDialog(transient_for=parent, modal=True, message_type=Gtk.MessageType.QUESTION,
                          buttons=Gtk.ButtonsType.NONE, text=text)
    d.format_secondary_text("Your changes will be lost if you don't save them.")
    d.add_button("Don't Save", 1)
    d.add_button("Cancel", Gtk.ResponseType.CANCEL)
    b = d.add_button("Save", Gtk.ResponseType.OK)
    b.get_style_context().add_class("suggested-action")
    d.set_default_response(Gtk.ResponseType.OK)
    r = d.run()
    d.destroy()
    return {1: "discard", Gtk.ResponseType.OK: "save"}.get(r, "cancel")


def password_entry(text="", placeholder="", visible=False):
    e = Gtk.Entry()
    e.set_text(text or "")
    e.set_visibility(visible)
    e.set_placeholder_text(placeholder)
    e.set_input_purpose(Gtk.InputPurpose.PASSWORD)
    e.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY,
                              "view-conceal-symbolic" if visible else "view-reveal-symbolic")
    e.set_icon_tooltip_text(Gtk.EntryIconPosition.SECONDARY, "Show / hide")

    def toggle(entry, pos, ev):
        v = not entry.get_visibility()
        entry.set_visibility(v)
        entry.set_icon_from_icon_name(Gtk.EntryIconPosition.SECONDARY,
                                      "view-conceal-symbolic" if v else "view-reveal-symbolic")
        if hasattr(entry, "_on_vis"):
            entry._on_vis(v)
    e.connect("icon-press", toggle)
    return e


def quality_bits(pw):
    if not pw:
        return 0
    pool = 0
    if any(c.islower() for c in pw):
        pool += 26
    if any(c.isupper() for c in pw):
        pool += 26
    if any(c.isdigit() for c in pw):
        pool += 10
    if any(c in string.punctuation for c in pw):
        pool += 32
    if " " in pw:
        pool += 1
    if any(ord(c) > 127 for c in pw):
        pool += 100
    pool = max(pool, 2)
    uniq = len(set(pw))
    eff_len = min(len(pw), uniq * 2 + (len(pw) - uniq) * 0.25)
    return int(eff_len * math.log2(pool))


def quality_widget():
    box = Gtk.Box(spacing=10)
    bar = Gtk.LevelBar()
    bar.set_min_value(0)
    bar.set_max_value(128)
    bar.add_offset_value("low", 48)
    bar.add_offset_value("high", 80)
    bar.add_offset_value("full", 128)
    bar.set_hexpand(True)
    bar.set_valign(Gtk.Align.CENTER)
    lbl = Gtk.Label(label="0 bits")
    lbl.get_style_context().add_class("dim")
    lbl.set_width_chars(9)
    lbl.set_xalign(1)
    box.pack_start(bar, True, True, 0)
    box.pack_start(lbl, False, False, 0)

    def update(pw):
        b = quality_bits(pw)
        bar.set_value(min(b, 128))
        lbl.set_text("%d bits" % b)
    box.update = update
    return box


def key_label(text):
    l = Gtk.Label(label=text)
    l.set_xalign(1)
    l.get_style_context().add_class("key")
    return l


def text_view(text="", mono=False, height=120):
    tv = Gtk.TextView()
    tv.set_wrap_mode(Gtk.WrapMode.WORD_CHAR)
    tv.get_buffer().set_text(text or "")
    tv.set_left_margin(4)
    tv.set_right_margin(4)
    if mono:
        tv.set_monospace(True)
    sw = Gtk.ScrolledWindow()
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    sw.set_min_content_height(height)
    sw.add(tv)
    sw.get_style_context().add_class("textframe")
    sw.get_style_context().add_class("plain")
    return sw, tv


def tv_text(tv):
    b = tv.get_buffer()
    return b.get_text(b.get_start_iter(), b.get_end_iter(), True)


def grid():
    g = Gtk.Grid()
    g.set_row_spacing(10)
    g.set_column_spacing(12)
    g.set_border_width(18)
    return g


def make_dialog(parent, title, ok_label="OK", width=560, height=-1):
    d = Gtk.Dialog(title=title, transient_for=parent, modal=True)
    d.set_default_size(width, height)
    d.add_button("Cancel", Gtk.ResponseType.CANCEL)
    if ok_label:
        b = d.add_button(ok_label, Gtk.ResponseType.OK)
        b.get_style_context().add_class("suggested-action")
        b.set_can_default(True)
        d.set_default_response(Gtk.ResponseType.OK)
    return d


def simple_list(columns, height=160):
    """TreeView with text columns. Returns (scrolled, view, store)."""
    store = Gtk.ListStore(*([object] + [str] * len(columns)))
    view = Gtk.TreeView(model=store)
    for i, (title, expand) in enumerate(columns):
        r = Gtk.CellRendererText()
        r.set_property("ellipsize", Pango.EllipsizeMode.END)
        c = Gtk.TreeViewColumn(title, r, text=i + 1)
        c.set_expand(expand)
        c.set_resizable(True)
        view.append_column(c)
    sw = Gtk.ScrolledWindow()
    sw.set_min_content_height(height)
    sw.set_policy(Gtk.PolicyType.AUTOMATIC, Gtk.PolicyType.AUTOMATIC)
    sw.set_shadow_type(Gtk.ShadowType.IN)
    sw.add(view)
    return sw, view, store


def selected_row(view):
    model, it = view.get_selection().get_selected()
    return (model, it) if it else (model, None)


def button_column(*buttons):
    box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
    for label, cb in buttons:
        b = Gtk.Button(label=label)
        b.connect("clicked", lambda _b, f=cb: f())
        box.pack_start(b, False, False, 0)
    return box


def fmt_dt(d):
    if d is None:
        return ""
    return d.astimezone().strftime("%Y-%m-%d %H:%M:%S")


def parse_local_dt(text):
    text = text.strip()
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            d = datetime.datetime.strptime(text, fmt)
            return d.astimezone(datetime.timezone.utc)
        except ValueError:
            continue
    return None


# ---------------------------------------------------------------------------
# password generator
# ---------------------------------------------------------------------------
CHARSETS = [
    ("upper", "Upper-case (A, B, C, ...)", string.ascii_uppercase),
    ("lower", "Lower-case (a, b, c, ...)", string.ascii_lowercase),
    ("digits", "Digits (0, 1, 2, ...)", string.digits),
    ("minus", "Minus (-)", "-"),
    ("underline", "Underline (_)", "_"),
    ("space", "Space ( )", " "),
    ("special", "Special (!, $, %, &, ...)", "!\"#$%&'*+,./:;=?@\\^`|~"),
    ("brackets", "Brackets ([, ], {, }, (, ), <, >)", "[]{}()<>"),
]
LOOKALIKE = set("O0Il1|")


def generate_password(opts):
    pools = []
    for key, _label, chars in CHARSETS:
        if opts.get(key):
            pools.append(chars)
    if opts.get("custom"):
        pools.append(opts["custom"])
    if opts.get("exclude_lookalike"):
        pools = ["".join(c for c in p if c not in LOOKALIKE) for p in pools]
    pools = [p for p in pools if p]
    if not pools:
        return ""
    allchars = "".join(sorted(set("".join(pools))))
    length = max(1, int(opts.get("length", 20)))
    while True:
        pw = [secrets.choice(allchars) for _ in range(length)]
        # make sure every selected set is represented when possible
        if length < len(pools) or all(any(c in p for c in pw) for p in pools):
            return "".join(pw)


class GeneratorDialog(Gtk.Dialog):
    def __init__(self, parent, config, use_mode=True):
        super().__init__(title="Password Generator", transient_for=parent, modal=True)
        self.config = config
        self.set_default_size(520, -1)
        self.add_button("Close" if not use_mode else "Cancel", Gtk.ResponseType.CANCEL)
        if use_mode:
            b = self.add_button("Use Password", Gtk.ResponseType.OK)
            b.get_style_context().add_class("suggested-action")
        self.opts = config.get("gen")
        g = grid()
        self.out = Gtk.Entry()
        self.out.get_style_context().add_class("mono")
        attrs = Pango.AttrList()
        attrs.insert(Pango.attr_family_new("monospace"))
        attrs.insert(Pango.attr_scale_new(1.15))
        self.out.set_attributes(attrs)
        self.out.set_hexpand(True)
        regen = Gtk.Button.new_from_icon_name("view-refresh-symbolic", Gtk.IconSize.BUTTON)
        regen.set_tooltip_text("Generate again")
        regen.connect("clicked", lambda *_: self.regen())
        copyb = Gtk.Button.new_from_icon_name("edit-copy-symbolic", Gtk.IconSize.BUTTON)
        copyb.set_tooltip_text("Copy")
        copyb.connect("clicked", lambda *_: Gtk.Clipboard.get(Gdk.SELECTION_CLIPBOARD).set_text(self.out.get_text(), -1))
        row = Gtk.Box(spacing=6)
        row.pack_start(self.out, True, True, 0)
        row.pack_start(regen, False, False, 0)
        row.pack_start(copyb, False, False, 0)
        g.attach(row, 0, 0, 2, 1)
        self.quality = quality_widget()
        g.attach(self.quality, 0, 1, 2, 1)
        self.out.connect("changed", lambda e: self.quality.update(e.get_text()))

        g.attach(key_label("Length"), 0, 2, 1, 1)
        lrow = Gtk.Box(spacing=8)
        self.length_adj = Gtk.Adjustment(value=self.opts.get("length", 20), lower=4, upper=128, step_increment=1)
        scale = Gtk.Scale(orientation=Gtk.Orientation.HORIZONTAL, adjustment=self.length_adj)
        scale.set_draw_value(False)
        scale.set_hexpand(True)
        spin = Gtk.SpinButton(adjustment=self.length_adj)
        lrow.pack_start(scale, True, True, 0)
        lrow.pack_start(spin, False, False, 0)
        g.attach(lrow, 1, 2, 1, 1)
        self.length_adj.connect("value-changed", lambda *_: self.regen())

        sets = Gtk.Grid()
        sets.set_column_spacing(18)
        sets.set_row_spacing(6)
        self.checks = {}
        for i, (key, label, _c) in enumerate(CHARSETS):
            cb = Gtk.CheckButton(label=label)
            cb.set_active(bool(self.opts.get(key)))
            cb.connect("toggled", lambda *_: self.regen())
            self.checks[key] = cb
            sets.attach(cb, i % 2, i // 2, 1, 1)
        self.lookalike = Gtk.CheckButton(label="Exclude look-alike characters (O, 0, I, l, 1, |)")
        self.lookalike.set_active(bool(self.opts.get("exclude_lookalike")))
        self.lookalike.connect("toggled", lambda *_: self.regen())
        sets.attach(self.lookalike, 0, 4, 2, 1)
        g.attach(key_label("Characters"), 0, 3, 1, 1)
        g.attach(sets, 1, 3, 1, 1)
        g.attach(key_label("Also use"), 0, 4, 1, 1)
        self.custom = Gtk.Entry()
        self.custom.set_placeholder_text("additional characters")
        self.custom.set_text(self.opts.get("custom", ""))
        self.custom.connect("changed", lambda *_: self.regen())
        g.attach(self.custom, 1, 4, 1, 1)
        self.get_content_area().add(g)
        self.regen()
        self.show_all()

    def collect(self):
        o = {k: cb.get_active() for k, cb in self.checks.items()}
        o["length"] = int(self.length_adj.get_value())
        o["exclude_lookalike"] = self.lookalike.get_active()
        o["custom"] = self.custom.get_text()
        return o

    def regen(self):
        self.opts = self.collect()
        self.out.set_text(generate_password(self.opts))

    def result(self):
        self.config.set("gen", self.collect())
        return self.out.get_text()


# ---------------------------------------------------------------------------
# icon picker
# ---------------------------------------------------------------------------
class IconPicker(Gtk.Dialog):
    def __init__(self, parent, db, icon_id, custom_uuid):
        super().__init__(title="Choose Icon", transient_for=parent, modal=True)
        self.db = db
        self.set_default_size(560, 460)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        b = self.add_button("Select", Gtk.ResponseType.OK)
        b.get_style_context().add_class("suggested-action")
        self.result = (icon_id, custom_uuid)
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(14)
        l = Gtk.Label(label="STANDARD ICONS", xalign=0)
        l.get_style_context().add_class("section")
        box.pack_start(l, False, False, 0)
        self.std = Gtk.FlowBox()
        self.std.set_max_children_per_line(14)
        self.std.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.std.set_homogeneous(True)
        for i, (name, _n) in enumerate(theme.KP_ICONS):
            img = Gtk.Image.new_from_pixbuf(theme.kp_icon(i, 24))
            img.set_tooltip_text(name)
            img._icon = i
            self.std.add(img)
            if i == icon_id and not custom_uuid:
                self.std.select_child(img.get_parent())
        self.std.connect("child-activated", lambda *_: self.response(Gtk.ResponseType.OK))
        self.std.connect("selected-children-changed", self._std_sel)
        box.pack_start(self.std, False, False, 0)
        l = Gtk.Label(label="CUSTOM ICONS", xalign=0)
        l.get_style_context().add_class("section")
        box.pack_start(l, False, False, 6)
        self.cust = Gtk.FlowBox()
        self.cust.set_max_children_per_line(14)
        self.cust.set_selection_mode(Gtk.SelectionMode.SINGLE)
        self.cust.connect("child-activated", lambda *_: self.response(Gtk.ResponseType.OK))
        self.cust.connect("selected-children-changed", self._cust_sel)
        self._fill_custom(custom_uuid)
        sw = Gtk.ScrolledWindow()
        sw.set_vexpand(True)
        sw.add(self.cust)
        sw.get_style_context().add_class("plain")
        box.pack_start(sw, True, True, 0)
        add = Gtk.Button(label="Add Custom Icon from File…")
        add.connect("clicked", self._add)
        add.set_halign(Gtk.Align.START)
        box.pack_start(add, False, False, 0)
        self.get_content_area().add(box)
        self.show_all()

    def _fill_custom(self, sel=None):
        for c in self.cust.get_children():
            self.cust.remove(c)
        for uid, data in self.db.custom_icons():
            pb = theme.pixbuf_from_bytes(data, 24)
            if pb is None:
                continue
            img = Gtk.Image.new_from_pixbuf(pb)
            img._uuid = uid
            self.cust.add(img)
            if uid == sel:
                self.cust.select_child(img.get_parent())
        self.cust.show_all()

    def _std_sel(self, fb):
        s = fb.get_selected_children()
        if s:
            self.cust.unselect_all()
            self.result = (s[0].get_child()._icon, "")

    def _cust_sel(self, fb):
        s = fb.get_selected_children()
        if s:
            self.std.unselect_all()
            self.result = (self.result[0], s[0].get_child()._uuid)

    def _add(self, *_):
        fc = Gtk.FileChooserDialog(title="Choose Icon Image", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Open", Gtk.ResponseType.OK)
        f = Gtk.FileFilter()
        f.set_name("Images")
        for p in ("*.png", "*.ico", "*.jpg", "*.jpeg", "*.svg", "*.gif", "*.bmp"):
            f.add_pattern(p)
        fc.add_filter(f)
        if fc.run() == Gtk.ResponseType.OK:
            path = fc.get_filename()
            try:
                from gi.repository import GdkPixbuf
                pb = GdkPixbuf.Pixbuf.new_from_file_at_scale(path, 64, 64, True)
                ok, data = pb.save_to_bufferv("png", [], [])
                icons = self.db.meta.find("CustomIcons")
                if icons is None:
                    icons = ET.SubElement(self.db.meta, "CustomIcons")
                ic = ET.SubElement(icons, "Icon")
                uid = kdbx.new_uuid_b64()
                ET.SubElement(ic, "UUID").text = uid
                ET.SubElement(ic, "Data").text = kdbx.b64(bytes(data))
                ET.SubElement(ic, "Name").text = os.path.basename(path)
                ET.SubElement(ic, "LastModificationTime").text = kdbx.fmt_time_iso(kdbx.now_utc())
                self.db.modified = True
                self._fill_custom(uid)
                self.result = (self.result[0], uid)
            except Exception as e:
                error(self, "Could not load image", str(e))
        fc.destroy()


def icon_button(db, icon_id, custom_uuid):
    btn = Gtk.Button()
    btn.get_style_context().add_class("icon-pick")
    btn.state = [icon_id, custom_uuid]
    img = Gtk.Image()
    btn.add(img)

    def refresh():
        pb = theme.custom_icon(db, btn.state[1], 24) if btn.state[1] else None
        img.set_from_pixbuf(pb or theme.kp_icon(btn.state[0], 24))
    refresh()

    def clicked(_b):
        d = IconPicker(btn.get_toplevel(), db, btn.state[0], btn.state[1])
        if d.run() == Gtk.ResponseType.OK:
            btn.state = list(d.result)
            refresh()
        d.destroy()
    btn.connect("clicked", clicked)
    btn.set_tooltip_text("Change icon")
    return btn


# ---------------------------------------------------------------------------
# field / association sub-dialogs
# ---------------------------------------------------------------------------
def edit_field_dialog(parent, name="", value="", protected=False, existing=()):
    d = make_dialog(parent, "Edit String Field" if name else "Add String Field", width=460)
    g = grid()
    ne = Gtk.Entry()
    ne.set_text(name)
    ne.set_hexpand(True)
    g.attach(key_label("Name"), 0, 0, 1, 1)
    g.attach(ne, 1, 0, 1, 1)
    sw, tv = text_view(value, height=110)
    g.attach(key_label("Value"), 0, 1, 1, 1)
    g.attach(sw, 1, 1, 1, 1)
    pc = Gtk.CheckButton(label="Enable in-memory protection (hide value)")
    pc.set_active(protected)
    g.attach(pc, 1, 2, 1, 1)
    err = Gtk.Label(xalign=0)
    err.get_style_context().add_class("error")
    g.attach(err, 1, 3, 1, 1)
    d.get_content_area().add(g)
    d.show_all()
    while True:
        if d.run() != Gtk.ResponseType.OK:
            d.destroy()
            return None
        n = ne.get_text().strip()
        if not n:
            err.set_text("Please enter a name.")
            continue
        if n in kdbx.STANDARD_FIELDS:
            err.set_text("This name is reserved for a standard field.")
            continue
        if n != name and n in existing:
            err.set_text("A field with this name already exists.")
            continue
        res = (n, tv_text(tv), pc.get_active())
        d.destroy()
        return res


def edit_assoc_dialog(parent, window="", seq=""):
    d = make_dialog(parent, "Auto-Type Window Association", width=520)
    g = grid()
    we = Gtk.Entry()
    we.set_text(window)
    we.set_hexpand(True)
    we.set_placeholder_text("e.g.  *Firefox*   or   //regex//")
    g.attach(key_label("Target window"), 0, 0, 1, 1)
    g.attach(we, 1, 0, 1, 1)
    hint = Gtk.Label(label="Wildcards * and ? are supported. Case-insensitive.", xalign=0)
    hint.get_style_context().add_class("dim")
    g.attach(hint, 1, 1, 1, 1)
    se = Gtk.Entry()
    se.set_text(seq)
    se.set_placeholder_text("leave empty to use the entry's default sequence")
    g.attach(key_label("Sequence"), 0, 2, 1, 1)
    g.attach(se, 1, 2, 1, 1)
    d.get_content_area().add(g)
    d.show_all()
    r = d.run()
    res = (we.get_text().strip(), se.get_text().strip()) if r == Gtk.ResponseType.OK else None
    d.destroy()
    if res and not res[0]:
        return None
    return res


PLACEHOLDER_HELP = ("{TITLE} {USERNAME} {PASSWORD} {URL} {NOTES} {TOTP} {S:Field}\n"
                    "{TAB} {ENTER} {SPACE} {BS} {DEL} {UP} {DOWN} {LEFT} {RIGHT} {HOME} {END} {ESC} {F1}…\n"
                    "{DELAY 500}  {DELAY=50}  {CLEARFIELD}   + Shift  ^ Ctrl  % Alt  ~ Enter")


# ---------------------------------------------------------------------------
# entry editor
# ---------------------------------------------------------------------------
class EntryDialog(Gtk.Dialog):
    def __init__(self, parent, entry, config, is_new=False):
        super().__init__(title="Add Entry" if is_new else "Edit Entry", transient_for=parent, modal=True)
        self.entry = entry
        self.db = entry.db
        self.config = config
        self.set_default_size(680, 600)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = self.add_button("OK", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        nb = Gtk.Notebook()
        nb.append_page(self._page_entry(), Gtk.Label(label="Entry"))
        nb.append_page(self._page_advanced(), Gtk.Label(label="Advanced"))
        nb.append_page(self._page_otp(), Gtk.Label(label="OTP"))
        nb.append_page(self._page_autotype(), Gtk.Label(label="Auto-Type"))
        nb.append_page(self._page_history(), Gtk.Label(label="History"))
        nb.append_page(self._page_props(), Gtk.Label(label="Properties"))
        self.get_content_area().pack_start(nb, True, True, 0)
        self.connect("response", self._on_response)
        self.show_all()
        self._sync_repeat(self.pw.get_visibility())
        self.title_e.grab_focus()

    # ---- pages
    def _page_entry(self):
        e = self.entry
        g = grid()
        r = 0
        g.attach(key_label("Title"), 0, r, 1, 1)
        trow = Gtk.Box(spacing=8)
        self.title_e = Gtk.Entry()
        self.title_e.set_text(e.title)
        self.title_e.set_hexpand(True)
        self.title_e.set_activates_default(True)
        self.icon_btn = icon_button(self.db, e.icon_id, e.custom_icon_uuid)
        trow.pack_start(self.title_e, True, True, 0)
        trow.pack_start(self.icon_btn, False, False, 0)
        g.attach(trow, 1, r, 1, 1)
        r += 1
        g.attach(key_label("User name"), 0, r, 1, 1)
        self.user_e = Gtk.Entry()
        self.user_e.set_text(e.username)
        self.user_e.set_activates_default(True)
        g.attach(self.user_e, 1, r, 1, 1)
        r += 1
        g.attach(key_label("Password"), 0, r, 1, 1)
        prow = Gtk.Box(spacing=8)
        self.pw = password_entry(e.password, visible=not self.config.get("hide_passwords"))
        self.pw.set_activates_default(True)
        self.pw._on_vis = self._sync_repeat
        genb = Gtk.Button.new_from_icon_name("system-run-symbolic", Gtk.IconSize.BUTTON)
        genb.set_tooltip_text("Generate password")
        genb.connect("clicked", self._generate)
        prow.pack_start(self.pw, True, True, 0)
        prow.pack_start(genb, False, False, 0)
        g.attach(prow, 1, r, 1, 1)
        r += 1
        self.repeat_lbl = key_label("Repeat")
        g.attach(self.repeat_lbl, 0, r, 1, 1)
        self.repeat = Gtk.Entry()
        self.repeat.set_visibility(False)
        self.repeat.set_text(e.password)
        self.repeat.set_activates_default(True)
        g.attach(self.repeat, 1, r, 1, 1)
        r += 1
        g.attach(key_label("Quality"), 0, r, 1, 1)
        self.quality = quality_widget()
        self.quality.update(e.password)
        self.pw.connect("changed", lambda w: self.quality.update(w.get_text()))
        g.attach(self.quality, 1, r, 1, 1)
        r += 1
        g.attach(key_label("URL"), 0, r, 1, 1)
        self.url_e = Gtk.Entry()
        self.url_e.set_text(e.url)
        self.url_e.set_activates_default(True)
        g.attach(self.url_e, 1, r, 1, 1)
        r += 1
        g.attach(key_label("Tags"), 0, r, 1, 1)
        self.tags_e = Gtk.Entry()
        self.tags_e.set_text(", ".join(e.tags))
        self.tags_e.set_placeholder_text("comma separated")
        g.attach(self.tags_e, 1, r, 1, 1)
        r += 1
        nl = key_label("Notes")
        nl.set_valign(Gtk.Align.START)
        g.attach(nl, 0, r, 1, 1)
        sw, self.notes_tv = text_view(e.notes, height=130)
        sw.set_vexpand(True)
        g.attach(sw, 1, r, 1, 1)
        r += 1
        g.attach(key_label("Expires"), 0, r, 1, 1)
        erow = Gtk.Box(spacing=8)
        self.exp_cb = Gtk.CheckButton()
        self.exp_cb.set_active(e.expires)
        self.exp_e = Gtk.Entry()
        exp = e.expiry_time if e.expires else (kdbx.now_utc() + datetime.timedelta(days=365))
        self.exp_e.set_text(fmt_dt(exp))
        self.exp_e.set_sensitive(e.expires)
        self.exp_cb.connect("toggled", lambda c: self.exp_e.set_sensitive(c.get_active()))
        preset = Gtk.MenuButton(label="Presets")
        menu = Gtk.Menu()
        for label, days in (("1 week", 7), ("2 weeks", 14), ("1 month", 30), ("3 months", 91),
                            ("6 months", 182), ("1 year", 365)):
            mi = Gtk.MenuItem(label=label)
            mi.connect("activate", self._preset, days)
            menu.append(mi)
        menu.show_all()
        preset.set_popup(menu)
        erow.pack_start(self.exp_cb, False, False, 0)
        erow.pack_start(self.exp_e, True, True, 0)
        erow.pack_start(preset, False, False, 0)
        g.attach(erow, 1, r, 1, 1)
        return g

    def _preset(self, _mi, days):
        self.exp_cb.set_active(True)
        self.exp_e.set_text(fmt_dt(kdbx.now_utc() + datetime.timedelta(days=days)))

    def _sync_repeat(self, visible):
        self.repeat.set_visible(not visible)
        self.repeat_lbl.set_visible(not visible)
        if visible:
            self.repeat.set_text(self.pw.get_text())

    def _generate(self, *_):
        d = GeneratorDialog(self, self.config)
        if d.run() == Gtk.ResponseType.OK:
            pw = d.result()
            self.pw.set_text(pw)
            self.repeat.set_text(pw)
        d.destroy()

    def _page_advanced(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(18)
        l = Gtk.Label(label="STRING FIELDS", xalign=0)
        l.get_style_context().add_class("section")
        box.pack_start(l, False, False, 0)
        row = Gtk.Box(spacing=10)
        sw, self.fields_view, self.fields_store = simple_list([("Name", False), ("Value", True)])
        self.fields = [list(f) for f in self.entry.custom_fields() if f[0] not in otp.OTP_FIELDS]
        self._fill_fields()
        self.fields_view.connect("row-activated", lambda *_: self._field_edit())
        row.pack_start(sw, True, True, 0)
        row.pack_start(button_column(("Add", self._field_add), ("Edit", self._field_edit),
                                     ("Delete", self._field_del)), False, False, 0)
        box.pack_start(row, True, True, 0)
        l = Gtk.Label(label="ATTACHMENTS", xalign=0)
        l.get_style_context().add_class("section")
        box.pack_start(l, False, False, 8)
        row = Gtk.Box(spacing=10)
        sw, self.att_view, self.att_store = simple_list([("Name", True), ("Size", False)], 120)
        self.attachments = list(self.entry.attachments())
        self._fill_att()
        row.pack_start(sw, True, True, 0)
        row.pack_start(button_column(("Attach…", self._att_add), ("Save…", self._att_save),
                                     ("Delete", self._att_del)), False, False, 0)
        box.pack_start(row, True, True, 0)
        return box

    def _fill_fields(self):
        self.fields_store.clear()
        for f in self.fields:
            self.fields_store.append([f, f[0], "••••••••" if f[2] else f[1].replace("\n", " ")])

    def _field_add(self):
        r = edit_field_dialog(self, existing=[f[0] for f in self.fields])
        if r:
            self.fields.append(list(r))
            self._fill_fields()

    def _field_edit(self):
        m, it = selected_row(self.fields_view)
        if not it:
            return
        f = m[it][0]
        r = edit_field_dialog(self, f[0], f[1], f[2], existing=[x[0] for x in self.fields])
        if r:
            f[:] = list(r)
            self._fill_fields()

    def _field_del(self):
        m, it = selected_row(self.fields_view)
        if it:
            self.fields.remove(m[it][0])
            self._fill_fields()

    def _fill_att(self):
        self.att_store.clear()
        for a in self.attachments:
            n = len(a[1])
            size = "%d B" % n if n < 1024 else ("%.1f KB" % (n / 1024) if n < 1048576 else "%.1f MB" % (n / 1048576))
            self.att_store.append([a, a[0], size])

    def _att_add(self):
        fc = Gtk.FileChooserDialog(title="Attach Files", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Attach", Gtk.ResponseType.OK)
        fc.set_select_multiple(True)
        if fc.run() == Gtk.ResponseType.OK:
            for p in fc.get_filenames():
                try:
                    with open(p, "rb") as f:
                        data = f.read()
                    name = os.path.basename(p)
                    self.attachments = [a for a in self.attachments if a[0] != name]
                    self.attachments.append((name, data))
                except OSError as e:
                    error(self, "Could not read file", str(e))
            self._fill_att()
        fc.destroy()

    def _att_save(self):
        m, it = selected_row(self.att_view)
        if not it:
            return
        name, data = m[it][0]
        fc = Gtk.FileChooserDialog(title="Save Attachment", transient_for=self, action=Gtk.FileChooserAction.SAVE)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Save", Gtk.ResponseType.OK)
        fc.set_current_name(name)
        fc.set_do_overwrite_confirmation(True)
        if fc.run() == Gtk.ResponseType.OK:
            try:
                with open(fc.get_filename(), "wb") as f:
                    f.write(data)
            except OSError as e:
                error(self, "Could not save file", str(e))
        fc.destroy()

    def _att_del(self):
        m, it = selected_row(self.att_view)
        if it:
            self.attachments.remove(m[it][0])
            self._fill_att()

    def _page_otp(self):
        g = grid()
        cfg = otp.from_entry(self.entry)
        intro = Gtk.Label(label="Time-based one-time passwords (TOTP). Stored in KeePass 2.47+ format, "
                                "so codes also work in KeePass, KeePassXC and mobile apps.", xalign=0)
        intro.set_line_wrap(True)
        intro.set_max_width_chars(60)
        intro.get_style_context().add_class("dim")
        g.attach(intro, 0, 0, 2, 1)
        g.attach(key_label("Secret"), 0, 1, 1, 1)
        self.otp_secret = password_entry("", "Base32 secret or otpauth:// URI", visible=False)
        self.otp_secret.set_hexpand(True)
        existing = ""
        if self.entry.get("TimeOtp-Secret-Base32"):
            existing = self.entry.get("TimeOtp-Secret-Base32")
        elif self.entry.get("otp"):
            existing = self.entry.get("otp")
        elif cfg is not None:
            import base64
            existing = base64.b32encode(cfg.secret).decode().rstrip("=")
        self.otp_secret.set_text(existing)
        g.attach(self.otp_secret, 1, 1, 1, 1)
        g.attach(key_label("Digits"), 0, 2, 1, 1)
        self.otp_digits = Gtk.SpinButton.new_with_range(6, 10, 1)
        self.otp_digits.set_value(cfg.digits if cfg else 6)
        self.otp_digits.set_halign(Gtk.Align.START)
        g.attach(self.otp_digits, 1, 2, 1, 1)
        g.attach(key_label("Period (seconds)"), 0, 3, 1, 1)
        self.otp_period = Gtk.SpinButton.new_with_range(5, 600, 5)
        self.otp_period.set_value(cfg.period if cfg else 30)
        self.otp_period.set_halign(Gtk.Align.START)
        g.attach(self.otp_period, 1, 3, 1, 1)
        g.attach(key_label("Algorithm"), 0, 4, 1, 1)
        self.otp_algo = Gtk.ComboBoxText()
        for a in ("SHA1", "SHA256", "SHA512"):
            self.otp_algo.append(a, a)
        self.otp_algo.set_active_id(cfg.algo if cfg else "SHA1")
        self.otp_algo.set_halign(Gtk.Align.START)
        g.attach(self.otp_algo, 1, 4, 1, 1)
        g.attach(key_label("Current code"), 0, 5, 1, 1)
        self.otp_preview = Gtk.Label(xalign=0)
        self.otp_preview.get_style_context().add_class("otp-code")
        self.otp_preview.set_selectable(True)
        g.attach(self.otp_preview, 1, 5, 1, 1)
        for w in (self.otp_secret, self.otp_digits, self.otp_period):
            w.connect("changed", lambda *_: self._otp_tick())
        self.otp_algo.connect("changed", lambda *_: self._otp_tick())
        self._otp_timer = GLib.timeout_add(1000, self._otp_tick)
        self.connect("destroy", lambda *_: GLib.source_remove(self._otp_timer))
        self._otp_tick()
        return g

    def _otp_cfg(self):
        s = self.otp_secret.get_text().strip()
        if not s:
            return None
        if otp.looks_like_code(s):
            raise ValueError("code, not secret")
        if s.lower().startswith("otpauth://"):
            return otp.parse_uri(s)
        return otp.OtpConfig(otp.b32decode(s), int(self.otp_digits.get_value()),
                             int(self.otp_period.get_value()), self.otp_algo.get_active_id() or "SHA1")

    def _otp_tick(self):
        try:
            c = self._otp_cfg()
            if c is None:
                self.otp_preview.set_text("—")
            else:
                self.otp_preview.set_text("%s   (%ds)" % (c.code(), c.remaining()))
        except Exception:
            if otp.looks_like_code(self.otp_secret.get_text()):
                self.otp_preview.set_text("that is a one-time code — paste the setup key instead")
            else:
                self.otp_preview.set_text("invalid secret")
        return True

    def _page_autotype(self):
        e = self.entry
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(18)
        self.at_enabled = Gtk.CheckButton(label="Enable Auto-Type for this entry")
        self.at_enabled.set_active(e.autotype_enabled)
        box.pack_start(self.at_enabled, False, False, 0)
        self.at_inherit = Gtk.RadioButton.new_with_label(None, "Inherit default sequence from the group")
        self.at_custom = Gtk.RadioButton.new_with_label_from_widget(self.at_inherit, "Use custom sequence:")
        box.pack_start(self.at_inherit, False, False, 0)
        box.pack_start(self.at_custom, False, False, 0)
        self.at_seq = Gtk.Entry()
        self.at_seq.set_text(e.autotype_sequence or (e.parent.effective_sequence() if e.parent else autotype.DEFAULT_SEQUENCE))
        self.at_custom.set_active(bool(e.autotype_sequence))
        self.at_seq.set_sensitive(bool(e.autotype_sequence))
        self.at_custom.connect("toggled", lambda r: self.at_seq.set_sensitive(r.get_active()))
        box.pack_start(self.at_seq, False, False, 0)
        hint = Gtk.Label(label=PLACEHOLDER_HELP, xalign=0)
        hint.get_style_context().add_class("dim")
        hint.get_style_context().add_class("mono")
        hint.set_selectable(True)
        box.pack_start(hint, False, False, 0)
        l = Gtk.Label(label="WINDOW ASSOCIATIONS", xalign=0)
        l.get_style_context().add_class("section")
        box.pack_start(l, False, False, 8)
        row = Gtk.Box(spacing=10)
        sw, self.assoc_view, self.assoc_store = simple_list([("Target Window", True), ("Sequence", True)], 120)
        self.assocs = [list(a) for a in e.associations()]
        self._fill_assoc()
        self.assoc_view.connect("row-activated", lambda *_: self._assoc_edit())
        row.pack_start(sw, True, True, 0)
        row.pack_start(button_column(("Add", self._assoc_add), ("Edit", self._assoc_edit),
                                     ("Delete", self._assoc_del)), False, False, 0)
        box.pack_start(row, True, True, 0)
        return box

    def _fill_assoc(self):
        self.assoc_store.clear()
        for a in self.assocs:
            self.assoc_store.append([a, a[0], a[1] or "(default)"])

    def _assoc_add(self):
        r = edit_assoc_dialog(self)
        if r:
            self.assocs.append(list(r))
            self._fill_assoc()

    def _assoc_edit(self):
        m, it = selected_row(self.assoc_view)
        if not it:
            return
        a = m[it][0]
        r = edit_assoc_dialog(self, a[0], a[1])
        if r:
            a[:] = list(r)
            self._fill_assoc()

    def _assoc_del(self):
        m, it = selected_row(self.assoc_view)
        if it:
            self.assocs.remove(m[it][0])
            self._fill_assoc()

    def _page_history(self):
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        box.set_border_width(18)
        row = Gtk.Box(spacing=10)
        sw, self.hist_view, self.hist_store = simple_list(
            [("Modified", False), ("Title", True), ("User Name", True), ("Password", False)], 300)
        self._fill_hist()
        row.pack_start(sw, True, True, 0)
        row.pack_start(button_column(("Restore", self._hist_restore), ("Delete", self._hist_del)),
                       False, False, 0)
        box.pack_start(row, True, True, 0)
        self.hist_restore = None
        return box

    def _fill_hist(self):
        self.hist_store.clear()
        for h in reversed(self.entry.history()):
            self.hist_store.append([h, fmt_dt(h.modified), h.title, h.username, "••••••••"])

    def _hist_restore(self):
        m, it = selected_row(self.hist_view)
        if not it:
            return
        h = m[it][0]
        if confirm(self, "Restore this version?", "The current state will be saved to the history.", "Restore"):
            self.hist_restore = h
            self.response(Gtk.ResponseType.OK)

    def _hist_del(self):
        m, it = selected_row(self.hist_view)
        if it:
            self.entry.delete_history(m[it][0])
            self._fill_hist()

    def _page_props(self):
        e = self.entry
        g = grid()
        r = 0

        def color_row(label, value):
            nonlocal r
            g.attach(key_label(label), 0, r, 1, 1)
            hb = Gtk.Box(spacing=8)
            cb = Gtk.CheckButton()
            btn = Gtk.ColorButton()
            rgba = Gdk.RGBA()
            if value and rgba.parse(value):
                btn.set_rgba(rgba)
                cb.set_active(True)
            else:
                btn.set_sensitive(False)
            cb.connect("toggled", lambda c: btn.set_sensitive(c.get_active()))
            hb.pack_start(cb, False, False, 0)
            hb.pack_start(btn, False, False, 0)
            g.attach(hb, 1, r, 1, 1)
            r += 1
            return cb, btn
        self.fg = color_row("Text color", e.fg_color)
        self.bg = color_row("Background color", e.bg_color)
        g.attach(key_label("Override URL"), 0, r, 1, 1)
        self.override_url = Gtk.Entry()
        self.override_url.set_text(kdbx._text(e.el, "OverrideURL"))
        self.override_url.set_hexpand(True)
        self.override_url.set_placeholder_text("e.g. cmd://firefox --private-window {URL}")
        g.attach(self.override_url, 1, r, 1, 1)
        r += 1
        import base64
        for label, val in (("UUID", base64.b64decode(e.uuid).hex().upper() if e.uuid else ""),
                           ("Created", fmt_dt(e.created)), ("Modified", fmt_dt(e.modified)),
                           ("Accessed", fmt_dt(e.accessed))):
            g.attach(key_label(label), 0, r, 1, 1)
            v = Gtk.Label(label=val, xalign=0)
            v.set_selectable(True)
            g.attach(v, 1, r, 1, 1)
            r += 1
        return g

    # ---- apply
    def _on_response(self, d, resp):
        if resp != Gtk.ResponseType.OK or self.hist_restore is not None:
            return
        if not self.pw.get_visibility() and self.pw.get_text() != self.repeat.get_text():
            error(self, "Passwords do not match", "Please repeat the password correctly, or show it to skip repeating.")
            self.stop_emission_by_name("response")
            return
        if self.exp_cb.get_active() and parse_local_dt(self.exp_e.get_text()) is None:
            error(self, "Invalid expiry date", "Use the format YYYY-MM-DD HH:MM")
            self.stop_emission_by_name("response")
            return
        try:
            self._otp_cfg()
        except Exception:
            if otp.looks_like_code(self.otp_secret.get_text()):
                error(self, "That is a one-time code, not the secret",
                      "Paste the setup key the website shows next to the QR code (letters A–Z and "
                      "digits 2–7, often labelled “Can't scan the code?”), or the otpauth:// link.")
            else:
                error(self, "Invalid OTP secret", "The secret must be Base32 encoded or an otpauth:// URI.")
            self.stop_emission_by_name("response")
            return

    def apply(self):
        """Write the dialog state into the entry. Returns True if anything changed."""
        e = self.entry
        if self.hist_restore is not None:
            e.restore_history(self.hist_restore)
            return True
        before = snapshot(e.el)
        e.title = self.title_e.get_text()
        e.username = self.user_e.get_text()
        e.password = self.pw.get_text()
        e.url = self.url_e.get_text()
        e.notes = tv_text(self.notes_tv)
        e.tags = [t.strip() for t in self.tags_e.get_text().replace(";", ",").split(",") if t.strip()]
        e.icon_id, e.custom_icon_uuid = self.icon_btn.state[0], self.icon_btn.state[1]
        if self.exp_cb.get_active():
            e.set_expiry(True, parse_local_dt(self.exp_e.get_text()))
        else:
            e.set_expiry(False)
        # custom fields (keep OTP fields managed by OTP tab)
        for k, _v, _p in e.custom_fields():
            if k not in otp.OTP_FIELDS:
                e.remove(k)
        for k, v, p in self.fields:
            e.set(k, v, p)
        cur_secret = self.otp_secret.get_text().strip()
        cfg_before = otp.from_entry(e)
        cfg_new = self._otp_cfg()
        changed_otp = (cfg_before is None) != (cfg_new is None) or (
            cfg_new is not None and (cfg_before.secret, cfg_before.digits, cfg_before.period, cfg_before.algo)
            != (cfg_new.secret, cfg_new.digits, cfg_new.period, cfg_new.algo))
        if changed_otp:
            otp.store(e, cur_secret, int(self.otp_digits.get_value()), int(self.otp_period.get_value()),
                      self.otp_algo.get_active_id() or "SHA1")
        if [list(a) for a in e.attachments()] != [list(a) for a in self.attachments]:
            e.set_attachments(self.attachments)
        e.autotype_enabled = self.at_enabled.get_active()
        e.autotype_sequence = self.at_seq.get_text().strip() if self.at_custom.get_active() else ""
        e.set_associations([tuple(a) for a in self.assocs])
        fg = self.fg[1].get_rgba() if self.fg[0].get_active() else None
        bg = self.bg[1].get_rgba() if self.bg[0].get_active() else None

        def hexc(c):
            return "#%02X%02X%02X" % (int(c.red * 255), int(c.green * 255), int(c.blue * 255)) if c else ""
        e.set_colors(hexc(fg), hexc(bg))
        kdbx._set_text(e.el, "OverrideURL", self.override_url.get_text())
        after = snapshot(e.el)
        if ET.tostring(before) != ET.tostring(after):
            push_history(e, before)
            e.touch()
            return True
        return False


def snapshot(el):
    s = copy.deepcopy(el)
    h = s.find("History")
    if h is not None:
        s.remove(h)
    return s


def push_history(entry, snap):
    h = entry.el.find("History")
    if h is None:
        h = ET.SubElement(entry.el, "History")
    h.append(snap)
    entry.trim_history()


# ---------------------------------------------------------------------------
# group editor
# ---------------------------------------------------------------------------
class GroupDialog(Gtk.Dialog):
    def __init__(self, parent, group, is_new=False):
        super().__init__(title="Add Group" if is_new else "Edit Group", transient_for=parent, modal=True)
        self.group = group
        self.set_default_size(520, 460)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = self.add_button("OK", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        nb = Gtk.Notebook()
        g = grid()
        g.attach(key_label("Name"), 0, 0, 1, 1)
        row = Gtk.Box(spacing=8)
        self.name_e = Gtk.Entry()
        self.name_e.set_text(group.name)
        self.name_e.set_hexpand(True)
        self.name_e.set_activates_default(True)
        self.icon_btn = icon_button(group.db, group.icon_id, group.custom_icon_uuid)
        row.pack_start(self.name_e, True, True, 0)
        row.pack_start(self.icon_btn, False, False, 0)
        g.attach(row, 1, 0, 1, 1)
        nl = key_label("Notes")
        nl.set_valign(Gtk.Align.START)
        g.attach(nl, 0, 1, 1, 1)
        sw, self.notes_tv = text_view(group.notes)
        sw.set_vexpand(True)
        g.attach(sw, 1, 1, 1, 1)
        g.attach(key_label("Expires"), 0, 2, 1, 1)
        erow = Gtk.Box(spacing=8)
        self.exp_cb = Gtk.CheckButton()
        self.exp_cb.set_active(group.expires)
        self.exp_e = Gtk.Entry()
        self.exp_e.set_text(fmt_dt(group.expiry_time if group.expires else kdbx.now_utc() + datetime.timedelta(days=365)))
        self.exp_e.set_sensitive(group.expires)
        self.exp_cb.connect("toggled", lambda c: self.exp_e.set_sensitive(c.get_active()))
        erow.pack_start(self.exp_cb, False, False, 0)
        erow.pack_start(self.exp_e, True, True, 0)
        g.attach(erow, 1, 2, 1, 1)
        nb.append_page(g, Gtk.Label(label="General"))

        g2 = grid()
        g2.attach(key_label("Auto-Type"), 0, 0, 1, 1)
        self.at_combo = self._tri_combo(group.autotype_enabled)
        g2.attach(self.at_combo, 1, 0, 1, 1)
        g2.attach(key_label("Searching"), 0, 1, 1, 1)
        self.search_combo = self._tri_combo(group.searching_enabled)
        g2.attach(self.search_combo, 1, 1, 1, 1)
        self.inherit = Gtk.RadioButton.new_with_label(None, "Inherit default auto-type sequence from parent")
        self.custom = Gtk.RadioButton.new_with_label_from_widget(self.inherit, "Override default sequence:")
        g2.attach(self.inherit, 0, 2, 2, 1)
        g2.attach(self.custom, 0, 3, 2, 1)
        self.seq = Gtk.Entry()
        self.seq.set_hexpand(True)
        self.seq.set_text(group.default_sequence or group.effective_sequence())
        self.custom.set_active(bool(group.default_sequence))
        self.seq.set_sensitive(bool(group.default_sequence))
        self.custom.connect("toggled", lambda r: self.seq.set_sensitive(r.get_active()))
        g2.attach(self.seq, 0, 4, 2, 1)
        hint = Gtk.Label(label=PLACEHOLDER_HELP, xalign=0)
        hint.get_style_context().add_class("dim")
        hint.get_style_context().add_class("mono")
        g2.attach(hint, 0, 5, 2, 1)
        nb.append_page(g2, Gtk.Label(label="Behavior"))
        self.get_content_area().pack_start(nb, True, True, 0)
        self.show_all()

    @staticmethod
    def _tri_combo(val):
        c = Gtk.ComboBoxText()
        c.append("null", "Inherit from parent group")
        c.append("true", "Enabled")
        c.append("false", "Disabled")
        c.set_active_id("null" if val is None else ("true" if val else "false"))
        c.set_hexpand(True)
        return c

    def apply(self):
        g = self.group
        g.name = self.name_e.get_text().strip() or "New Group"
        g.notes = tv_text(self.notes_tv)
        g.icon_id, g.custom_icon_uuid = self.icon_btn.state
        d = parse_local_dt(self.exp_e.get_text()) if self.exp_cb.get_active() else None
        g.set_expiry(self.exp_cb.get_active() and d is not None, d)
        tri = {"null": None, "true": True, "false": False}
        g.autotype_enabled = tri[self.at_combo.get_active_id()]
        g.searching_enabled = tri[self.search_combo.get_active_id()]
        g.default_sequence = self.seq.get_text().strip() if self.custom.get_active() else ""
        g.touch()


# ---------------------------------------------------------------------------
# master key / new database
# ---------------------------------------------------------------------------
class MasterKeyDialog(Gtk.Dialog):
    def __init__(self, parent, title="Change Master Key", intro=None):
        super().__init__(title=title, transient_for=parent, modal=True)
        self.set_default_size(520, -1)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = self.add_button("OK", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        self.set_default_response(Gtk.ResponseType.OK)
        g = grid()
        r = 0
        if intro:
            l = Gtk.Label(label=intro, xalign=0)
            l.set_line_wrap(True)
            l.get_style_context().add_class("dim")
            g.attach(l, 0, r, 2, 1)
            r += 1
        self.use_pw = Gtk.CheckButton(label="Master password")
        self.use_pw.set_active(True)
        g.attach(self.use_pw, 0, r, 2, 1)
        r += 1
        g.attach(key_label("Password"), 0, r, 1, 1)
        self.pw = password_entry()
        self.pw.set_hexpand(True)
        self.pw.set_activates_default(True)
        self.pw._on_vis = lambda v: (self.repeat.set_visible(not v), self.rl.set_visible(not v))
        g.attach(self.pw, 1, r, 1, 1)
        r += 1
        self.rl = key_label("Repeat")
        g.attach(self.rl, 0, r, 1, 1)
        self.repeat = Gtk.Entry()
        self.repeat.set_visibility(False)
        self.repeat.set_activates_default(True)
        g.attach(self.repeat, 1, r, 1, 1)
        r += 1
        g.attach(key_label("Quality"), 0, r, 1, 1)
        q = quality_widget()
        self.pw.connect("changed", lambda w: q.update(w.get_text()))
        g.attach(q, 1, r, 1, 1)
        r += 1
        self.use_kf = Gtk.CheckButton(label="Key file")
        g.attach(self.use_kf, 0, r, 2, 1)
        r += 1
        krow = Gtk.Box(spacing=6)
        self.kf = Gtk.Entry()
        self.kf.set_hexpand(True)
        self.kf.set_placeholder_text("path to key file")
        self.kf.connect("changed", lambda e: self.use_kf.set_active(bool(e.get_text())))
        br = Gtk.Button(label="Browse…")
        br.connect("clicked", self._browse)
        cr = Gtk.Button(label="Create…")
        cr.connect("clicked", self._create)
        krow.pack_start(self.kf, True, True, 0)
        krow.pack_start(br, False, False, 0)
        krow.pack_start(cr, False, False, 0)
        g.attach(krow, 0, r, 2, 1)
        r += 1
        self.err = Gtk.Label(xalign=0)
        self.err.get_style_context().add_class("error")
        g.attach(self.err, 0, r, 2, 1)
        self.get_content_area().add(g)
        self.show_all()
        self.pw.grab_focus()

    def _browse(self, *_):
        fc = Gtk.FileChooserDialog(title="Select Key File", transient_for=self, action=Gtk.FileChooserAction.OPEN)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Select", Gtk.ResponseType.OK)
        if fc.run() == Gtk.ResponseType.OK:
            self.kf.set_text(fc.get_filename())
        fc.destroy()

    def _create(self, *_):
        fc = Gtk.FileChooserDialog(title="Create Key File", transient_for=self, action=Gtk.FileChooserAction.SAVE)
        fc.add_buttons("Cancel", Gtk.ResponseType.CANCEL, "Create", Gtk.ResponseType.OK)
        fc.set_current_name("Database.keyx")
        fc.set_do_overwrite_confirmation(True)
        if fc.run() == Gtk.ResponseType.OK:
            p = fc.get_filename()
            try:
                kdbx.create_keyfile(p)
                self.kf.set_text(p)
                message(self, "Key file created",
                        "Keep a backup of this key file somewhere safe. Without it the database cannot be opened.")
            except OSError as e:
                error(self, "Could not create key file", str(e))
        fc.destroy()

    def get_result(self):
        """Loop until valid; returns (password or None, keyfile or None) or None if cancelled."""
        while True:
            if self.run() != Gtk.ResponseType.OK:
                return None
            pw = self.pw.get_text() if self.use_pw.get_active() else None
            kf = self.kf.get_text().strip() if self.use_kf.get_active() else None
            if self.use_pw.get_active() and not self.pw.get_visibility() and pw != self.repeat.get_text():
                self.err.set_text("The passwords do not match.")
                continue
            if not pw and not kf:
                self.err.set_text("Enter a password and/or choose a key file.")
                continue
            if kf and not os.path.isfile(kf):
                self.err.set_text("The key file does not exist.")
                continue
            if pw == "" and not kf:
                self.err.set_text("The password is empty.")
                continue
            if pw is not None and pw == "" and kf:
                pw = None
            if pw is not None and len(pw) < 8 and not confirm(
                    self, "Weak master password", "Your master password is very short. Use it anyway?", "Use it"):
                continue
            return pw, kf


# ---------------------------------------------------------------------------
# database settings
# ---------------------------------------------------------------------------
class DatabaseSettingsDialog(Gtk.Dialog):
    def __init__(self, parent, db):
        super().__init__(title="Database Settings", transient_for=parent, modal=True)
        self.db = db
        self.set_default_size(560, 460)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = self.add_button("OK", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        nb = Gtk.Notebook()
        g = grid()
        g.attach(key_label("Database name"), 0, 0, 1, 1)
        self.name = Gtk.Entry()
        self.name.set_text(db.meta_get("DatabaseName"))
        self.name.set_hexpand(True)
        g.attach(self.name, 1, 0, 1, 1)
        dl = key_label("Description")
        dl.set_valign(Gtk.Align.START)
        g.attach(dl, 0, 1, 1, 1)
        sw, self.desc = text_view(db.meta_get("DatabaseDescription"), height=90)
        g.attach(sw, 1, 1, 1, 1)
        g.attach(key_label("Default user name"), 0, 2, 1, 1)
        self.defuser = Gtk.Entry()
        self.defuser.set_text(db.meta_get("DefaultUserName"))
        g.attach(self.defuser, 1, 2, 1, 1)
        g.attach(key_label("Format"), 0, 3, 1, 1)
        fl = Gtk.Label(label="KDBX %d.%d  (saved as KDBX 4.1)" % db.version, xalign=0)
        fl.get_style_context().add_class("dim")
        g.attach(fl, 1, 3, 1, 1)
        nb.append_page(g, Gtk.Label(label="General"))

        g = grid()
        g.attach(key_label("Encryption"), 0, 0, 1, 1)
        self.cipher = Gtk.ComboBoxText()
        self.cipher.append("aes", "AES-256 (recommended)")
        self.cipher.append("chacha", "ChaCha20 (256-bit)")
        self.cipher.set_active_id("chacha" if db.cipher == kdbx.CIPHER_CHACHA else "aes")
        self.cipher.set_hexpand(True)
        g.attach(self.cipher, 1, 0, 1, 1)
        k = db.kdf_settings()
        g.attach(key_label("Key derivation"), 0, 1, 1, 1)
        self.kdf = Gtk.ComboBoxText()
        self.kdf.append("argon2d", "Argon2d (recommended)")
        self.kdf.append("argon2id", "Argon2id")
        self.kdf.append("aes", "AES-KDF")
        self.kdf.set_active_id({kdbx.KDF_ARGON2D: "argon2d", kdbx.KDF_ARGON2ID: "argon2id"}.get(k["uuid"], "aes"))
        g.attach(self.kdf, 1, 1, 1, 1)
        self.iter_lbl = key_label("Iterations")
        g.attach(self.iter_lbl, 0, 2, 1, 1)
        self.iters = Gtk.SpinButton.new_with_range(1, 1 << 40, 1)
        self.iters.set_value(k["rounds"] if k["uuid"] == kdbx.KDF_AES else k["iterations"])
        g.attach(self.iters, 1, 2, 1, 1)
        self.mem_lbl = key_label("Memory (MiB)")
        g.attach(self.mem_lbl, 0, 3, 1, 1)
        self.mem = Gtk.SpinButton.new_with_range(1, 1 << 20, 1)
        self.mem.set_value(max(1, k["memory"] // (1024 * 1024)))
        g.attach(self.mem, 1, 3, 1, 1)
        self.par_lbl = key_label("Parallelism")
        g.attach(self.par_lbl, 0, 4, 1, 1)
        self.par = Gtk.SpinButton.new_with_range(1, 64, 1)
        self.par.set_value(k["parallelism"])
        g.attach(self.par, 1, 4, 1, 1)
        test = Gtk.Button(label="Benchmark 1-second delay")
        test.connect("clicked", self._bench)
        test.set_halign(Gtk.Align.START)
        g.attach(test, 1, 5, 1, 1)
        g.attach(key_label("Compression"), 0, 6, 1, 1)
        self.gzip = Gtk.CheckButton(label="GZip")
        self.gzip.set_active(db.compression == 1)
        g.attach(self.gzip, 1, 6, 1, 1)
        self._kdf_kind = self.kdf.get_active_id()
        self.kdf.connect("changed", self._kdf_changed)
        nb.append_page(g, Gtk.Label(label="Security"))

        g = grid()
        self.rb = Gtk.CheckButton(label="Use a recycle bin")
        self.rb.set_active(kdbx._bool(db.meta_get("RecycleBinEnabled", "True"), True))
        g.attach(self.rb, 0, 0, 2, 1)
        items, size = db.history_limits()
        self.hist_items_cb = Gtk.CheckButton(label="Limit history items per entry")
        self.hist_items_cb.set_active(items >= 0)
        g.attach(self.hist_items_cb, 0, 1, 1, 1)
        self.hist_items = Gtk.SpinButton.new_with_range(0, 10000, 1)
        self.hist_items.set_value(items if items >= 0 else 10)
        g.attach(self.hist_items, 1, 1, 1, 1)
        self.hist_size_cb = Gtk.CheckButton(label="Limit history size per entry (MiB)")
        self.hist_size_cb.set_active(size >= 0)
        g.attach(self.hist_size_cb, 0, 2, 1, 1)
        self.hist_size = Gtk.SpinButton.new_with_range(1, 10000, 1)
        self.hist_size.set_value(max(1, size // 1048576) if size >= 0 else 6)
        g.attach(self.hist_size, 1, 2, 1, 1)
        nb.append_page(g, Gtk.Label(label="Recycle Bin & History"))
        self.get_content_area().pack_start(nb, True, True, 0)
        self.show_all()
        self._kdf_vis()

    def _kdf_changed(self, combo):
        new = combo.get_active_id()
        if (new == "aes") != (self._kdf_kind == "aes"):
            self.iters.set_value(600000 if new == "aes" else 10)
        self._kdf_kind = new
        self._kdf_vis()

    def _kdf_vis(self):
        argon = self.kdf.get_active_id() != "aes"
        for w in (self.mem, self.mem_lbl, self.par, self.par_lbl):
            w.set_visible(argon)
        self.iter_lbl.set_text("Iterations" if argon else "Transform rounds")

    def _bench(self, *_):
        import time
        from . import crypto
        win = self.get_window()
        win.set_cursor(Gdk.Cursor.new_from_name(Gdk.Display.get_default(), "wait"))
        while Gtk.events_pending():
            Gtk.main_iteration()
        k = bytes(32)
        if self.kdf.get_active_id() == "aes":
            t = time.time()
            crypto.aes_kdf(k, k, 1000000)
            dt = time.time() - t
            self.iters.set_value(int(1000000 / max(dt, 1e-3)))
        else:
            mem = int(self.mem.get_value()) * 1024
            par = int(self.par.get_value())
            t = time.time()
            crypto.argon2(k, k, 2, mem, par, variant="id" if self.kdf.get_active_id() == "argon2id" else "d")
            dt = (time.time() - t) / 2
            self.iters.set_value(max(2, int(1.0 / max(dt, 1e-3))))
        win.set_cursor(None)

    def apply(self):
        db = self.db
        if self.name.get_text() != db.meta_get("DatabaseName"):
            db.meta_set("DatabaseName", self.name.get_text(), "DatabaseNameChanged")
        if tv_text(self.desc) != db.meta_get("DatabaseDescription"):
            db.meta_set("DatabaseDescription", tv_text(self.desc), "DatabaseDescriptionChanged")
        if self.defuser.get_text() != db.meta_get("DefaultUserName"):
            db.meta_set("DefaultUserName", self.defuser.get_text(), "DefaultUserNameChanged")
        cipher = kdbx.CIPHER_CHACHA if self.cipher.get_active_id() == "chacha" else kdbx.CIPHER_AES
        if cipher != db.cipher:
            db.cipher = cipher
            db.modified = True
        comp = 1 if self.gzip.get_active() else 0
        if comp != db.compression:
            db.compression = comp
            db.modified = True
        kid = {"argon2d": kdbx.KDF_ARGON2D, "argon2id": kdbx.KDF_ARGON2ID, "aes": kdbx.KDF_AES}[self.kdf.get_active_id()]
        k = db.kdf_settings()
        it = int(self.iters.get_value())
        if kid == kdbx.KDF_AES:
            if k["uuid"] != kid or k["rounds"] != it:
                db.set_kdf(kid, rounds=it)
        else:
            mem = int(self.mem.get_value()) * 1024 * 1024
            par = int(self.par.get_value())
            if k["uuid"] != kid or k["iterations"] != it or k["memory"] != mem or k["parallelism"] != par:
                db.set_kdf(kid, iterations=it, memory=mem, parallelism=par)
        rb = "True" if self.rb.get_active() else "False"
        if rb != db.meta_get("RecycleBinEnabled", "True"):
            db.meta_set("RecycleBinEnabled", rb, "RecycleBinChanged")
        hi = str(int(self.hist_items.get_value())) if self.hist_items_cb.get_active() else "-1"
        hs = str(int(self.hist_size.get_value()) * 1048576) if self.hist_size_cb.get_active() else "-1"
        if hi != db.meta_get("HistoryMaxItems") or hs != db.meta_get("HistoryMaxSize"):
            db.meta_set("HistoryMaxItems", hi)
            db.meta_set("HistoryMaxSize", hs)
            for e in db.all_entries(include_recycle=True):
                e.trim_history()


# ---------------------------------------------------------------------------
# options
# ---------------------------------------------------------------------------
class HotkeyButton(Gtk.Box):
    """Click, then press the key combination to use as the global hot key."""
    DEFAULT = "Ctrl+Alt+Shift+Z"

    def __init__(self, spec, hotkey=None):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.spec = spec
        self.hotkey = hotkey
        self.recording = False
        self.btn = Gtk.Button()
        self.btn.set_hexpand(True)
        self.btn.set_tooltip_text("Click, then press the new key combination")
        self.lbl = Gtk.Label()
        self.btn.add(self.lbl)
        self.btn.connect("clicked", lambda *_: self._start())
        self.btn.connect("key-press-event", self._key)
        self.btn.connect("focus-out-event", lambda *_: self._stop())
        reset = Gtk.Button.new_from_icon_name("edit-undo-symbolic", Gtk.IconSize.BUTTON)
        reset.set_tooltip_text("Reset to %s" % self.DEFAULT)
        reset.connect("clicked", lambda *_: self._set(self.DEFAULT))
        self.hint = Gtk.Label(xalign=0)
        self.hint.get_style_context().add_class("dim")
        row = Gtk.Box(spacing=8)
        row.pack_start(self.btn, True, True, 0)
        row.pack_start(reset, False, False, 0)
        self.pack_start(row, False, False, 0)
        self.pack_start(self.hint, False, False, 0)
        self.connect("destroy", lambda *_: self._stop())
        self._render()

    def _render(self):
        if self.recording:
            self.lbl.set_markup("<i>Press the new shortcut…  (Esc to cancel)</i>")
        else:
            self.lbl.set_markup("<b>%s</b>" % GLib.markup_escape_text(self.spec.replace("+", " + ")))

    def _start(self):
        if self.recording:
            return
        self.recording = True
        if self.hotkey:
            self.hotkey.block(True)  # so the current shortcut can be pressed again
        self.btn.grab_focus()
        self._render()

    def _stop(self):
        if self.recording:
            self.recording = False
            if self.hotkey:
                self.hotkey.block(False)
            self._render()
        return False

    def _set(self, spec):
        self.spec = spec
        self.hint.set_text("")
        self._stop()
        self._render()

    def _key(self, w, ev):
        if not self.recording:
            return False
        if ev.keyval == Gdk.KEY_Escape and not (ev.state & Gdk.ModifierType.MODIFIER_MASK & ~Gdk.ModifierType.MOD2_MASK):
            self._stop()
            return True
        spec = autotype.hotkey_from_event(ev)
        if spec is None:
            return True  # only modifiers so far: keep waiting
        if not autotype.hotkey_valid(spec):
            self.hint.set_text("Use at least one of Ctrl, Alt, Shift or Meta together with a key.")
            return True
        self._set(spec)
        return True


class OptionsDialog(Gtk.Dialog):
    def __init__(self, parent, config, hotkey_status="", hotkey=None):
        super().__init__(title="Options", transient_for=parent, modal=True)
        self.hotkey = hotkey
        self.config = config
        self.set_default_size(560, 480)
        self.add_button("Cancel", Gtk.ResponseType.CANCEL)
        ok = self.add_button("OK", Gtk.ResponseType.OK)
        ok.get_style_context().add_class("suggested-action")
        self.w = {}
        nb = Gtk.Notebook()

        def page(title, rows):
            g = grid()
            for i, row in enumerate(rows):
                kind, key, label = row[:3]
                if kind == "check":
                    cb = Gtk.CheckButton(label=label)
                    cb.set_active(bool(config.get(key)))
                    g.attach(cb, 0, i, 2, 1)
                    self.w[key] = cb
                elif kind == "spin":
                    lo, hi, suffix = row[3], row[4], row[5]
                    g.attach(key_label(label), 0, i, 1, 1)
                    hb = Gtk.Box(spacing=8)
                    sp = Gtk.SpinButton.new_with_range(lo, hi, 1)
                    sp.set_value(config.get(key))
                    hb.pack_start(sp, False, False, 0)
                    l = Gtk.Label(label=suffix)
                    l.get_style_context().add_class("dim")
                    hb.pack_start(l, False, False, 0)
                    g.attach(hb, 1, i, 1, 1)
                    self.w[key] = sp
                elif kind == "hotkey":
                    g.attach(key_label(label), 0, i, 1, 1)
                    hb = HotkeyButton(config.get(key), self.hotkey)
                    g.attach(hb, 1, i, 1, 1)
                    self.w[key] = hb
                elif kind == "entry":
                    g.attach(key_label(label), 0, i, 1, 1)
                    en = Gtk.Entry()
                    en.set_text(config.get(key))
                    en.set_hexpand(True)
                    g.attach(en, 1, i, 1, 1)
                    self.w[key] = en
                elif kind == "color":
                    g.attach(key_label(label), 0, i, 1, 1)
                    cb = Gtk.ColorButton()
                    rgba = Gdk.RGBA()
                    rgba.parse(config.get(key))
                    cb.set_rgba(rgba)
                    cb.set_halign(Gtk.Align.START)
                    g.attach(cb, 1, i, 1, 1)
                    self.w[key] = cb
                elif kind == "label":
                    l = Gtk.Label(label=label, xalign=0)
                    l.set_line_wrap(True)
                    l.get_style_context().add_class("dim")
                    g.attach(l, 0, i, 2, 1)
            nb.append_page(g, Gtk.Label(label=title))

        page("Security", [
            ("spin", "clipboard_timeout", "Clear clipboard after", 0, 3600, "seconds (0 = never)"),
            ("spin", "lock_idle_minutes", "Lock after inactivity", 0, 1440, "minutes (0 = never)"),
            ("check", "lock_on_screensaver", "Lock when the session is locked / screensaver starts"),
            ("check", "lock_on_minimize", "Lock when the window is minimized"),
        ])
        page("Interface", [
            ("check", "show_tray", "Show icon in system tray"),
            ("check", "minimize_to_tray", "Minimize to tray instead of taskbar"),
            ("check", "close_to_tray", "Close button minimizes to tray (keeps global Auto-Type running)"),
            ("check", "start_minimized", "Start minimized"),
            ("check", "hide_passwords", "Hide passwords behind asterisks"),
            ("check", "hide_usernames", "Hide user names behind asterisks"),
            ("check", "show_otp_column", "Show OTP column in entry list"),
            ("color", "accent", "Accent color"),
        ])
        page("Auto-Type", [
            ("hotkey", "autotype_hotkey", "Global hot key"),
            ("label", "", "Click the button and press the key combination you want. "
                          "Backend: %s." % hotkey_status),
            ("spin", "autotype_delay", "Delay between keystrokes", 0, 1000, "ms"),
            ("check", "autotype_match_url", "Also match an entry by its URL / domain (e.g. google.com ↔ “Google — Firefox”)"),
            ("check", "autotype_require_match", "Only type into a window that matches the entry (recommended)"),
            ("check", "autotype_password_field_only", "Only type the password into a password box (in apps that report their fields)"),
            ("check", "autotype_always_show_dialog", "Always show the entry selection dialog"),
            ("check", "autotype_minimize_after", "Minimize Velkey after Auto-Type from the main window"),
        ])
        page("Files", [
            ("check", "open_last", "Remember and open the last used database on startup"),
            ("check", "backup_on_save", "Keep a backup copy (.bak) of the previous file version when saving"),
            ("check", "auto_save", "Automatically save after every change"),
        ])
        self.get_content_area().pack_start(nb, True, True, 0)
        self.show_all()

    def apply(self):
        for key, w in self.w.items():
            if isinstance(w, HotkeyButton):
                self.config.data[key] = w.spec
            elif isinstance(w, Gtk.CheckButton):
                self.config.data[key] = w.get_active()
            elif isinstance(w, Gtk.SpinButton):
                self.config.data[key] = int(w.get_value())
            elif isinstance(w, Gtk.ColorButton):
                c = w.get_rgba()
                self.config.data[key] = "#%02x%02x%02x" % (int(c.red * 255), int(c.green * 255), int(c.blue * 255))
            elif isinstance(w, Gtk.Entry):
                self.config.data[key] = w.get_text().strip()
        self.config.save()


# ---------------------------------------------------------------------------
# auto-type entry selection
# ---------------------------------------------------------------------------
class AutoTypePicker(Gtk.Window):
    def __init__(self, db, matches, window_title, on_pick):
        super().__init__(title="Auto-Type — Select Entry")
        self.set_default_size(640, 380)
        self.set_position(Gtk.WindowPosition.CENTER)
        self.set_keep_above(True)
        self.set_type_hint(Gdk.WindowTypeHint.DIALOG)
        self.set_icon_name("velkey")
        self.db = db
        self.on_pick = on_pick
        self.matches = matches
        self.all = matches is None
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=10)
        box.set_border_width(14)
        t = Gtk.Label(xalign=0)
        t.set_markup("<b>Select an entry to auto-type</b>")
        box.pack_start(t, False, False, 0)
        if window_title:
            text = "Target window: " + window_title
        else:
            text = ("The active window could not be detected. Choose an entry; it will be typed into "
                    "the window that regains focus.")
        sub = Gtk.Label(label=text, xalign=0)
        sub.set_line_wrap(True)
        sub.set_max_width_chars(80)
        sub.get_style_context().add_class("dim")
        box.pack_start(sub, False, False, 0)
        self.search = Gtk.SearchEntry()
        self.search.set_placeholder_text("Filter…")
        self.search.connect("search-changed", lambda *_: self._fill())
        self.search.connect("activate", lambda *_: self._choose())
        self.search.connect("key-press-event", self._search_key)
        box.pack_start(self.search, False, False, 0)
        self.store = Gtk.ListStore(object, str, object, str, str, str)
        self.view = Gtk.TreeView(model=self.store)
        c = Gtk.TreeViewColumn("Title")
        pr = Gtk.CellRendererPixbuf()
        c.pack_start(pr, False)
        c.add_attribute(pr, "pixbuf", 2)
        tr = Gtk.CellRendererText()
        tr.set_property("ellipsize", Pango.EllipsizeMode.END)
        c.pack_start(tr, True)
        c.add_attribute(tr, "text", 1)
        c.set_expand(True)
        self.view.append_column(c)
        for i, title in ((3, "User Name"), (4, "Group")):
            r = Gtk.CellRendererText()
            r.set_property("ellipsize", Pango.EllipsizeMode.END)
            col = Gtk.TreeViewColumn(title, r, text=i)
            col.set_expand(True)
            self.view.append_column(col)
        self.view.connect("row-activated", lambda *_: self._choose())
        sw = Gtk.ScrolledWindow()
        sw.add(self.view)
        sw.set_vexpand(True)
        box.pack_start(sw, True, True, 0)
        bb = Gtk.Box(spacing=8)
        bb.set_halign(Gtk.Align.END)
        cancel = Gtk.Button(label="Cancel")
        cancel.connect("clicked", lambda *_: self.destroy())
        ok = Gtk.Button(label="Auto-Type")
        ok.get_style_context().add_class("suggested-action")
        ok.connect("clicked", lambda *_: self._choose())
        bb.pack_start(cancel, False, False, 0)
        bb.pack_start(ok, False, False, 0)
        box.pack_start(bb, False, False, 0)
        self.add(box)
        self.connect("key-press-event", self._key)
        self._fill()
        self.show_all()
        self.present()
        self.search.grab_focus()

    def _source(self):
        if self.all:
            out = []
            for e in self.db.all_entries():
                if e.autotype_enabled:
                    out.append((e, e.effective_sequence()))
            return out
        return self.matches

    def _fill(self):
        terms = self.search.get_text().lower().split()
        self.store.clear()
        for e, seq in self._source():
            if terms and not e.matches(terms):
                continue
            p = e.parent
            self.store.append([(e, seq), e.title, theme.node_icon(e), e.username, p.name if p else "", ""])
        if len(self.store):
            self.view.get_selection().select_path(Gtk.TreePath.new_first())

    def _search_key(self, w, ev):
        if ev.keyval in (Gdk.KEY_Down, Gdk.KEY_Up):
            self.view.grab_focus()
            return False
        return False

    def _key(self, w, ev):
        if ev.keyval == Gdk.KEY_Escape:
            self.destroy()
            return True
        return False

    def _choose(self):
        m, it = self.view.get_selection().get_selected()
        if not it:
            return
        e, seq = m[it][0]
        self.destroy()
        self.on_pick(e, seq)
