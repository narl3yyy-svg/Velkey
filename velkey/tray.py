"""System tray icon via the StatusNotifierItem D-Bus protocol (KDE, and GNOME
with the AppIndicator extension).  Pure Gio — no libappindicator needed."""
import os

from gi.repository import Gio, GLib

SNI_XML = """<node>
<interface name="org.kde.StatusNotifierItem">
  <property name="Category" type="s" access="read"/>
  <property name="Id" type="s" access="read"/>
  <property name="Title" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="WindowId" type="i" access="read"/>
  <property name="IconName" type="s" access="read"/>
  <property name="IconThemePath" type="s" access="read"/>
  <property name="IconPixmap" type="a(iiay)" access="read"/>
  <property name="ToolTip" type="(sa(iiay)ss)" access="read"/>
  <property name="ItemIsMenu" type="b" access="read"/>
  <property name="Menu" type="o" access="read"/>
  <method name="Activate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="SecondaryActivate"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="ContextMenu"><arg type="i" direction="in"/><arg type="i" direction="in"/></method>
  <method name="Scroll"><arg type="i" direction="in"/><arg type="s" direction="in"/></method>
  <signal name="NewIcon"/>
  <signal name="NewToolTip"/>
  <signal name="NewStatus"><arg type="s"/></signal>
</interface>
</node>"""

MENU_XML = """<node>
<interface name="com.canonical.dbusmenu">
  <property name="Version" type="u" access="read"/>
  <property name="TextDirection" type="s" access="read"/>
  <property name="Status" type="s" access="read"/>
  <property name="IconThemePath" type="as" access="read"/>
  <method name="GetLayout">
    <arg type="i" direction="in"/><arg type="i" direction="in"/><arg type="as" direction="in"/>
    <arg type="u" direction="out"/><arg type="(ia{sv}av)" direction="out"/>
  </method>
  <method name="GetGroupProperties">
    <arg type="ai" direction="in"/><arg type="as" direction="in"/><arg type="a(ia{sv})" direction="out"/>
  </method>
  <method name="GetProperty">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="out"/>
  </method>
  <method name="Event">
    <arg type="i" direction="in"/><arg type="s" direction="in"/><arg type="v" direction="in"/><arg type="u" direction="in"/>
  </method>
  <method name="EventGroup">
    <arg type="a(isvu)" direction="in"/><arg type="ai" direction="out"/>
  </method>
  <method name="AboutToShow"><arg type="i" direction="in"/><arg type="b" direction="out"/></method>
  <method name="AboutToShowGroup">
    <arg type="ai" direction="in"/><arg type="ai" direction="out"/><arg type="ai" direction="out"/>
  </method>
  <signal name="ItemsPropertiesUpdated"><arg type="a(ia{sv})"/><arg type="a(ias)"/></signal>
  <signal name="LayoutUpdated"><arg type="u"/><arg type="i"/></signal>
</interface>
</node>"""


ICONS_DIR = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "icons")


def icon_pixmaps(name="velkey", sizes=(16, 22, 24, 32, 48, 64)):
    """The icon as raw ARGB images, so the tray can show it even when the icon
    is not installed in the system theme (e.g. when running from ./run.sh)."""
    try:
        import gi
        gi.require_version("GdkPixbuf", "2.0")
        from gi.repository import GdkPixbuf
    except (ImportError, ValueError):
        return []
    out = []
    for size in sizes:
        f = os.path.join(ICONS_DIR, "hicolor", "%dx%d" % (size, size), "apps", name + ".png")
        if not os.path.exists(f):
            f = os.path.join(ICONS_DIR, "hicolor", "scalable", "apps", name + ".svg")
        try:
            pb = GdkPixbuf.Pixbuf.new_from_file_at_size(f, size, size)
        except Exception:
            continue
        if not pb.get_has_alpha():
            pb = pb.add_alpha(False, 0, 0, 0)
        w, h, stride, px = pb.get_width(), pb.get_height(), pb.get_rowstride(), pb.get_pixels()
        argb = bytearray()
        for y in range(h):
            row = px[y * stride:y * stride + w * 4]
            for x in range(0, w * 4, 4):
                r, g, b, a = row[x:x + 4]
                argb += bytes((a, r, g, b))  # network byte order ARGB32
        out.append((w, h, bytes(argb)))
    return out


class Tray:
    def __init__(self, on_activate, items, icon_name="velkey", icon_path=None):
        """items: list of (id, label, callback) — label None means separator."""
        self.on_activate = on_activate
        self.items = items
        self.icon_name = icon_name
        self.icon_path = icon_path if icon_path is not None else (ICONS_DIR if os.path.isdir(ICONS_DIR) else "")
        self.pixmaps = icon_pixmaps(icon_name)
        self.tooltip = "Velkey"
        self.bus = None
        self.ids = []
        self.owner = 0
        self.revision = 1
        self.ok = False

    def start(self):
        try:
            self.bus = Gio.bus_get_sync(Gio.BusType.SESSION)
            sni = Gio.DBusNodeInfo.new_for_xml(SNI_XML).interfaces[0]
            menu = Gio.DBusNodeInfo.new_for_xml(MENU_XML).interfaces[0]
            self.ids.append(self.bus.register_object("/StatusNotifierItem", sni, self._sni_call, self._sni_prop, None))
            self.ids.append(self.bus.register_object("/MenuBar", menu, self._menu_call, self._menu_prop, None))
            name = "org.kde.StatusNotifierItem-%d-1" % os.getpid()
            self.owner = Gio.bus_own_name_on_connection(self.bus, name, Gio.BusNameOwnerFlags.NONE, None, None)
            self.bus.call_sync("org.kde.StatusNotifierWatcher", "/StatusNotifierWatcher",
                               "org.kde.StatusNotifierWatcher", "RegisterStatusNotifierItem",
                               GLib.Variant("(s)", (name,)), None, Gio.DBusCallFlags.NONE, 2000, None)
            self.ok = True
        except Exception:
            self.stop()
            self.ok = False
        return self.ok

    def stop(self):
        if self.bus:
            for i in self.ids:
                try:
                    self.bus.unregister_object(i)
                except Exception:
                    pass
        self.ids = []
        if self.owner:
            Gio.bus_unown_name(self.owner)
            self.owner = 0
        self.ok = False

    def set_tooltip(self, text):
        self.tooltip = text
        if self.ok:
            self.bus.emit_signal(None, "/StatusNotifierItem", "org.kde.StatusNotifierItem", "NewToolTip", None)

    def set_items(self, items):
        self.items = items
        self.revision += 1
        if self.ok:
            self.bus.emit_signal(None, "/MenuBar", "com.canonical.dbusmenu", "LayoutUpdated",
                                 GLib.Variant("(ui)", (self.revision, 0)))

    # --- SNI
    def _sni_prop(self, conn, sender, path, iface, prop):
        v = {
            "Category": GLib.Variant("s", "ApplicationStatus"),
            "Id": GLib.Variant("s", "velkey"),
            "Title": GLib.Variant("s", "Velkey"),
            "Status": GLib.Variant("s", "Active"),
            "WindowId": GLib.Variant("i", 0),
            "IconName": GLib.Variant("s", self.icon_name),
            "IconThemePath": GLib.Variant("s", self.icon_path),
            "IconPixmap": GLib.Variant("a(iiay)", self.pixmaps),
            "ToolTip": GLib.Variant("(sa(iiay)ss)", (self.icon_name, [], "Velkey", self.tooltip)),
            "ItemIsMenu": GLib.Variant("b", False),
            "Menu": GLib.Variant("o", "/MenuBar"),
        }
        return v.get(prop)

    def _sni_call(self, conn, sender, path, iface, method, params, inv):
        if method in ("Activate", "SecondaryActivate"):
            GLib.idle_add(lambda: (self.on_activate(), False)[1])
        inv.return_value(None)

    # --- dbusmenu
    def _menu_prop(self, conn, sender, path, iface, prop):
        return {"Version": GLib.Variant("u", 3), "TextDirection": GLib.Variant("s", "ltr"),
                "Status": GLib.Variant("s", "normal"), "IconThemePath": GLib.Variant("as", [])}.get(prop)

    def _props(self, idx):
        if idx == 0:
            return {"children-display": GLib.Variant("s", "submenu")}
        _id, label, _cb = self.items[idx - 1]
        if label is None:
            return {"type": GLib.Variant("s", "separator")}
        return {"label": GLib.Variant("s", label), "enabled": GLib.Variant("b", True),
                "visible": GLib.Variant("b", True)}

    def _menu_call(self, conn, sender, path, iface, method, params, inv):
        if method == "GetLayout":
            children = [GLib.Variant("(ia{sv}av)", (i, self._props(i), [])) for i in range(1, len(self.items) + 1)]
            inv.return_value(GLib.Variant("(u(ia{sv}av))", (self.revision, (0, self._props(0), children))))
        elif method == "GetGroupProperties":
            ids, _names = params.unpack()
            ids = ids or list(range(0, len(self.items) + 1))
            inv.return_value(GLib.Variant("(a(ia{sv}))", ([(i, self._props(i)) for i in ids
                                                           if 0 <= i <= len(self.items)],)))
        elif method == "GetProperty":
            i, name = params.unpack()
            p = self._props(i) if 0 <= i <= len(self.items) else {}
            inv.return_value(GLib.Variant("(v)", (p.get(name, GLib.Variant("s", "")),)))
        elif method == "Event":
            i, ev, _data, _ts = params.unpack()
            if ev == "clicked" and 1 <= i <= len(self.items):
                cb = self.items[i - 1][2]
                if cb:
                    GLib.idle_add(lambda: (cb(), False)[1])
            inv.return_value(None)
        elif method == "EventGroup":
            evs = params.unpack()[0]
            for i, ev, _d, _t in evs:
                if ev == "clicked" and 1 <= i <= len(self.items) and self.items[i - 1][2]:
                    cb = self.items[i - 1][2]
                    GLib.idle_add(lambda cb=cb: (cb(), False)[1])
            inv.return_value(GLib.Variant("(ai)", ([],)))
        elif method == "AboutToShow":
            inv.return_value(GLib.Variant("(b)", (False,)))
        elif method == "AboutToShowGroup":
            inv.return_value(GLib.Variant("(aiai)", ([], [])))
        else:
            inv.return_value(None)
