"""KeePass KDBX 3.1 / 4.x reader and KDBX 4 writer.

The decrypted XML document is kept as an ElementTree so that every element we
do not explicitly understand (plugin data, custom data, etc.) survives a
load/save round trip untouched.  Protected values are kept decrypted in memory
while the database is open and are re-encrypted on save.
"""
import base64
import copy
import datetime as _dt
import gzip
import io
import os
import struct
import uuid as _uuid
import xml.etree.ElementTree as ET

from . import crypto

SIG1 = 0x9AA2D903
SIG2 = 0xB54BFB67

CIPHER_AES = bytes.fromhex("31c1f2e6bf714350be5805216afc5aff")
CIPHER_CHACHA = bytes.fromhex("d6038a2b8b6f4cb5a524339a31dbb59a")
CIPHER_TWOFISH = bytes.fromhex("ad68f29f576f4bb9a36ad47af965346c")
KDF_AES = bytes.fromhex("c9d9f39a628a4460bf740d08c18a4fea")
KDF_AES_KDBX3 = bytes.fromhex("7c02bb8279a74ac0927d114a00648238")
KDF_ARGON2D = bytes.fromhex("ef636ddf8c29444b91f7a9a403e30a0c")
KDF_ARGON2ID = bytes.fromhex("9e298b1956db4773b23dfc3ec6f0a1e6")

CIPHER_NAMES = {CIPHER_AES: "AES-256", CIPHER_CHACHA: "ChaCha20", CIPHER_TWOFISH: "Twofish"}
KDF_NAMES = {KDF_AES: "AES-KDF", KDF_ARGON2D: "Argon2d", KDF_ARGON2ID: "Argon2id"}

STREAM_SALSA20 = 2
STREAM_CHACHA20 = 3

TIME_TAGS = {
    "CreationTime", "LastModificationTime", "LastAccessTime", "ExpiryTime",
    "LocationChanged", "DeletionTime", "DatabaseNameChanged", "DatabaseDescriptionChanged",
    "DefaultUserNameChanged", "MasterKeyChanged", "RecycleBinChanged",
    "EntryTemplatesGroupChanged", "SettingsChanged", "LastModificationTime",
}
STANDARD_FIELDS = ("Title", "UserName", "Password", "URL", "Notes")
_EPOCH = _dt.datetime(1, 1, 1, tzinfo=_dt.timezone.utc)


class KdbxError(Exception):
    pass


class InvalidKey(KdbxError):
    pass


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def now_utc():
    return _dt.datetime.now(_dt.timezone.utc).replace(microsecond=0)


def parse_time(text):
    if not text:
        return None
    text = text.strip()
    try:
        if "-" in text[1:] and ":" in text:
            t = text.replace("Z", "+00:00")
            d = _dt.datetime.fromisoformat(t)
            if d.tzinfo is None:
                d = d.replace(tzinfo=_dt.timezone.utc)
            return d
        raw = base64.b64decode(text)
        if len(raw) == 8:
            secs = struct.unpack("<q", raw)[0]
            return _EPOCH + _dt.timedelta(seconds=secs)
    except Exception:
        return None
    return None


def fmt_time_iso(d):
    return d.astimezone(_dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def fmt_time_kdbx4(d):
    secs = int((d - _EPOCH).total_seconds())
    return base64.b64encode(struct.pack("<q", secs)).decode()


def new_uuid_b64():
    return base64.b64encode(_uuid.uuid4().bytes).decode()


def b64(data):
    return base64.b64encode(data).decode()


def _sub(parent, tag, text=None):
    e = ET.SubElement(parent, tag)
    if text is not None:
        e.text = text
    return e


def _text(e, tag, default=""):
    c = e.find(tag)
    if c is None or c.text is None:
        return default
    return c.text


def _set_text(parent, tag, value):
    c = parent.find(tag)
    if c is None:
        c = _sub(parent, tag)
    c.text = value
    return c


def _bool(text, default=False):
    if text is None or text == "":
        return default
    return text.strip().lower() == "true"


# --------------------------------------------------------------------------
# Variant dictionary (KDBX 4 KDF parameters / public custom data)
# --------------------------------------------------------------------------
VD_UINT32, VD_UINT64, VD_BOOL, VD_INT32, VD_INT64, VD_STRING, VD_BYTES = 0x04, 0x05, 0x08, 0x0C, 0x0D, 0x18, 0x42


def read_variant_dict(data):
    r = io.BytesIO(data)
    ver = struct.unpack("<H", r.read(2))[0]
    if (ver & 0xFF00) > 0x0100:
        raise KdbxError("Unsupported KDF parameter format")
    out = {}
    while True:
        t = r.read(1)
        if not t or t[0] == 0:
            break
        t = t[0]
        klen = struct.unpack("<i", r.read(4))[0]
        key = r.read(klen).decode("utf-8")
        vlen = struct.unpack("<i", r.read(4))[0]
        val = r.read(vlen)
        if t == VD_UINT32:
            v = struct.unpack("<I", val)[0]
        elif t == VD_UINT64:
            v = struct.unpack("<Q", val)[0]
        elif t == VD_BOOL:
            v = val != b"\x00"
        elif t == VD_INT32:
            v = struct.unpack("<i", val)[0]
        elif t == VD_INT64:
            v = struct.unpack("<q", val)[0]
        elif t == VD_STRING:
            v = val.decode("utf-8")
        else:
            v = val
        out[key] = (t, v)
    return out


def write_variant_dict(d):
    w = io.BytesIO()
    w.write(struct.pack("<H", 0x0100))
    for key, (t, v) in d.items():
        if t == VD_UINT32:
            val = struct.pack("<I", v)
        elif t == VD_UINT64:
            val = struct.pack("<Q", v)
        elif t == VD_BOOL:
            val = b"\x01" if v else b"\x00"
        elif t == VD_INT32:
            val = struct.pack("<i", v)
        elif t == VD_INT64:
            val = struct.pack("<q", v)
        elif t == VD_STRING:
            val = v.encode("utf-8")
        else:
            val = bytes(v)
        k = key.encode("utf-8")
        w.write(bytes([t]) + struct.pack("<i", len(k)) + k + struct.pack("<i", len(val)) + val)
    w.write(b"\x00")
    return w.getvalue()


# --------------------------------------------------------------------------
# Composite key
# --------------------------------------------------------------------------
def keyfile_hash(path):
    with open(path, "rb") as f:
        data = f.read()
    # XML key file (v1.0 / v2.0)
    if data.lstrip()[:1] == b"<":
        try:
            root = ET.fromstring(data)
            if root.tag == "KeyFile":
                ver = (root.findtext("Meta/Version") or "1.0").strip()
                d = root.find("Key/Data")
                if d is not None and d.text:
                    if ver.startswith("2"):
                        raw = bytes.fromhex("".join(d.text.split()))
                        h = d.get("Hash")
                        if h and crypto.sha256(raw)[:4] != bytes.fromhex(h):
                            raise KdbxError("Key file is corrupted (hash mismatch)")
                        return raw
                    return base64.b64decode(d.text.strip())
        except ET.ParseError:
            pass
    if len(data) == 32:
        return data
    if len(data) == 64:
        try:
            return bytes.fromhex(data.decode("ascii"))
        except (ValueError, UnicodeDecodeError):
            pass
    return crypto.sha256(data)


def create_keyfile(path):
    raw = crypto.random_bytes(32)
    hexs = raw.hex().upper()
    groups = " ".join(hexs[i:i + 8] for i in range(0, 64, 8))
    h = crypto.sha256(raw)[:4].hex().upper()
    xml = ('<?xml version="1.0" encoding="utf-8"?>\n<KeyFile>\n\t<Meta>\n\t\t<Version>2.0</Version>\n'
           '\t</Meta>\n\t<Key>\n\t\t<Data Hash="%s">\n\t\t\t%s\n\t\t</Data>\n\t</Key>\n</KeyFile>\n' % (h, groups))
    with open(path, "w") as f:
        f.write(xml)


def composite_key(password, keyfile):
    parts = b""
    if password is not None and (password != "" or not keyfile):
        parts += crypto.sha256(password.encode("utf-8"))
    if keyfile:
        parts += keyfile_hash(keyfile)
    return crypto.sha256(parts)


# --------------------------------------------------------------------------
# Database
# --------------------------------------------------------------------------
class Database:
    def __init__(self):
        self.path = None
        self.root = None            # ET root element <KeePassFile>
        self.binaries = []          # list of [bytes, protected]
        self.version = (4, 0)
        self.cipher = CIPHER_AES
        self.compression = 1
        self.kdf = {}               # variant dict (KDBX4 style)
        self.public_custom_data = None
        self._composite = None
        self._transformed = None    # cached transformed key for current kdf salt
        self.modified = False
        self.file_mtime = None

    # ---------------------------------------------------------------- open
    @classmethod
    def open(cls, path, password=None, keyfile=None):
        db = cls()
        db.path = path
        with open(path, "rb") as f:
            data = f.read()
        db.file_mtime = os.path.getmtime(path)
        db._composite = composite_key(password, keyfile)
        db._load(data)
        return db

    def _load(self, data):
        r = io.BytesIO(data)
        try:
            s1, s2, minor, major = struct.unpack("<IIHH", r.read(12))
        except struct.error:
            raise KdbxError("File is too small to be a KeePass database")
        if s1 != SIG1:
            raise KdbxError("Not a KeePass database")
        if s2 == 0xB54BFB65:
            raise KdbxError("KeePass 1.x (.kdb) files are not supported. Use KeePass 2 to convert it to .kdbx first.")
        if s2 != SIG2:
            raise KdbxError("Unknown KeePass file signature")
        if major not in (3, 4):
            raise KdbxError("Unsupported KDBX version %d.%d" % (major, minor))
        self.version = (major, minor)
        fsize = 4 if major >= 4 else 2
        fields = {}
        while True:
            hdr = r.read(1 + fsize)
            if len(hdr) < 1 + fsize:
                raise KdbxError("Truncated header")
            fid = hdr[0]
            ln = struct.unpack("<I" if fsize == 4 else "<H", hdr[1:])[0]
            val = r.read(ln)
            if fid == 0:
                break
            fields[fid] = val
        header_end = r.tell()
        header = data[:header_end]

        self.cipher = fields.get(2, CIPHER_AES)
        self.compression = struct.unpack("<I", fields.get(3, b"\x01\x00\x00\x00"))[0]
        master_seed = fields[4]
        iv = fields[7]
        if major >= 4:
            self.kdf = read_variant_dict(fields[11])
            if 12 in fields:
                self.public_custom_data = fields[12]
        else:
            self.kdf = {"$UUID": (VD_BYTES, KDF_AES), "S": (VD_BYTES, fields[5]),
                        "R": (VD_UINT64, struct.unpack("<Q", fields[6])[0])}

        transformed = self._derive(self._composite)
        key = crypto.sha256(master_seed + transformed)

        if major >= 4:
            sha = r.read(32)
            mac = r.read(32)
            if crypto.sha256(header) != sha:
                raise KdbxError("Header checksum mismatch (file corrupted)")
            hmac_base = crypto.sha512(master_seed + transformed + b"\x01")
            hkey = crypto.sha512(struct.pack("<Q", 0xFFFFFFFFFFFFFFFF) + hmac_base)
            if crypto.hmac_sha256(hkey, header) != mac:
                raise InvalidKey("The master key is wrong or the file is corrupted")
            payload = self._read_hmac_blocks(r, hmac_base)
            plain = self._decrypt(key, iv, payload)
            if self.compression == 1:
                plain = gzip.decompress(plain)
            ir = io.BytesIO(plain)
            stream_id, stream_key = STREAM_CHACHA20, b""
            self.binaries = []
            while True:
                t = ir.read(1)[0]
                ln = struct.unpack("<i", ir.read(4))[0]
                val = ir.read(ln)
                if t == 0:
                    break
                if t == 1:
                    stream_id = struct.unpack("<I", val)[0]
                elif t == 2:
                    stream_key = val
                elif t == 3:
                    self.binaries.append([val[1:], bool(val[0] & 1)])
            xml = ir.read()
        else:
            try:
                plain = self._decrypt(key, iv, r.read())
            except crypto.CryptoError:
                raise InvalidKey("The master key is wrong or the file is corrupted")
            if plain[:32] != fields.get(9):
                raise InvalidKey("The master key is wrong or the file is corrupted")
            xml = self._read_hashed_blocks(io.BytesIO(plain[32:]))
            if self.compression == 1:
                xml = gzip.decompress(xml)
            stream_id = struct.unpack("<I", fields.get(10, b"\x02\x00\x00\x00"))[0]
            stream_key = fields.get(8, b"")
            self.binaries = []

        self._transformed = transformed
        stream = self._make_stream(stream_id, stream_key)
        try:
            self.root = ET.fromstring(xml)
        except ET.ParseError as e:
            raise KdbxError("Database XML is malformed: %s" % e)
        # decrypt protected values in document order
        for el in self.root.iter():
            if el.get("Protected", "").lower() == "true" and el.text:
                raw = base64.b64decode(el.text)
                el.text = stream.process(raw).decode("utf-8", "replace")
        self._normalize(major)

    def _derive(self, composite):
        uid = self.kdf["$UUID"][1]
        if uid in (KDF_AES, KDF_AES_KDBX3):
            return crypto.aes_kdf(composite, self.kdf["S"][1], self.kdf["R"][1])
        if uid in (KDF_ARGON2D, KDF_ARGON2ID):
            g = lambda k, d=None: self.kdf[k][1] if k in self.kdf else d
            return crypto.argon2(composite, g("S"), g("I"), g("M") // 1024, g("P"),
                                 version=g("V", 0x13), variant="id" if uid == KDF_ARGON2ID else "d",
                                 secret=g("K", b""), ad=g("A", b""))
        raise KdbxError("Unsupported key derivation function")

    def _decrypt(self, key, iv, data):
        if self.cipher == CIPHER_AES:
            return crypto.aes_cbc_decrypt(key, iv, data)
        if self.cipher == CIPHER_CHACHA:
            return crypto.chacha20(key, iv, data)
        if self.cipher == CIPHER_TWOFISH:
            raise KdbxError("Twofish-encrypted databases are not supported. Switch the cipher to AES or ChaCha20 in KeePass first.")
        raise KdbxError("Unsupported cipher")

    def _encrypt(self, key, iv, data):
        if self.cipher == CIPHER_CHACHA:
            return crypto.chacha20(key, iv, data)
        return crypto.aes_cbc_encrypt(key, iv, data)

    @staticmethod
    def _make_stream(sid, key):
        if sid == STREAM_CHACHA20:
            return crypto.ChaCha20Stream(key)
        if sid == STREAM_SALSA20:
            return crypto.Salsa20Stream(key)
        if sid == 0:
            class _Null:
                def process(self, d):
                    return d
            return _Null()
        raise KdbxError("Unsupported inner stream cipher")

    @staticmethod
    def _read_hmac_blocks(r, hmac_base):
        out = io.BytesIO()
        idx = 0
        while True:
            mac = r.read(32)
            szb = r.read(4)
            if len(mac) < 32 or len(szb) < 4:
                raise KdbxError("Truncated data block")
            size = struct.unpack("<i", szb)[0]
            data = r.read(size)
            bk = crypto.sha512(struct.pack("<Q", idx) + hmac_base)
            if crypto.hmac_sha256(bk, struct.pack("<Q", idx) + szb + data) != mac:
                raise KdbxError("Data block %d failed integrity check (file corrupted)" % idx)
            if size == 0:
                break
            out.write(data)
            idx += 1
        return out.getvalue()

    @staticmethod
    def _read_hashed_blocks(r):
        out = io.BytesIO()
        while True:
            hdr = r.read(40)
            if len(hdr) < 40:
                raise KdbxError("Truncated data block")
            _idx, h, size = struct.unpack("<I32sI", hdr)
            if size == 0:
                break
            data = r.read(size)
            if crypto.sha256(data) != h:
                raise KdbxError("Data block failed integrity check (file corrupted)")
            out.write(data)
        return out.getvalue()

    def _normalize(self, major):
        """Bring KDBX 3 specific bits into the KDBX 4 model."""
        # times -> ISO text internally
        for el in self.root.iter():
            if el.tag in TIME_TAGS and el.text:
                d = parse_time(el.text)
                if d:
                    el.text = fmt_time_iso(d)
        meta = self.meta
        if major < 4:
            pool = {}
            bins = meta.find("Binaries")
            if bins is not None:
                for b in bins.findall("Binary"):
                    raw = base64.b64decode(b.text or "")
                    if _bool(b.get("Compressed")):
                        raw = gzip.decompress(raw)
                    pool[b.get("ID")] = raw
                meta.remove(bins)
            # remap refs into self.binaries
            idmap = {}
            for v in self.root.iter("Value"):
                ref = v.get("Ref")
                if ref is not None and ref in pool:
                    if ref not in idmap:
                        idmap[ref] = len(self.binaries)
                        self.binaries.append([pool[ref], False])
                    v.set("Ref", str(idmap[ref]))
        hh = meta.find("HeaderHash")
        if hh is not None:
            meta.remove(hh)

    # ---------------------------------------------------------------- save
    def save(self, path=None):
        path = path or self.path
        data = self.serialize()
        tmp = path + ".tmp-velkey"
        try:
            os.unlink(tmp)  # never write through a leftover file or link
        except FileNotFoundError:
            pass
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)  # private from the start
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        try:
            st = os.stat(path)
            os.chmod(tmp, st.st_mode & 0o7777)
        except OSError:
            os.chmod(tmp, 0o600)
        os.replace(tmp, path)
        self.path = path
        self.file_mtime = os.path.getmtime(path)
        self.version = (4, 1)
        self.modified = False

    def serialize(self):
        if self._transformed is None:
            self._transformed = self._derive(self._composite)
        master_seed = crypto.random_bytes(32)
        iv = crypto.random_bytes(12 if self.cipher == CIPHER_CHACHA else 16)
        if self.cipher not in (CIPHER_AES, CIPHER_CHACHA):
            self.cipher = CIPHER_AES
            iv = crypto.random_bytes(16)
        stream_key = crypto.random_bytes(64)

        self._rebuild_binary_pool()
        settings = self.meta.find("SettingsChanged")
        gen = self.meta.find("Generator")
        if gen is not None:
            gen.text = "Velkey"

        # header
        def field(fid, val):
            return bytes([fid]) + struct.pack("<I", len(val)) + val
        kdf = dict(self.kdf)
        if kdf["$UUID"][1] == KDF_AES_KDBX3:
            kdf["$UUID"] = (VD_BYTES, KDF_AES)
        hdr = struct.pack("<IIHH", SIG1, SIG2, 1, 4)
        hdr += field(2, self.cipher)
        hdr += field(3, struct.pack("<I", self.compression))
        hdr += field(4, master_seed)
        hdr += field(7, iv)
        hdr += field(11, write_variant_dict(kdf))
        if self.public_custom_data:
            hdr += field(12, self.public_custom_data)
        hdr += field(0, b"\r\n\r\n")

        # inner header + xml
        inner = io.BytesIO()
        inner.write(b"\x01" + struct.pack("<i", 4) + struct.pack("<I", STREAM_CHACHA20))
        inner.write(b"\x02" + struct.pack("<i", len(stream_key)) + stream_key)
        for raw, prot in self.binaries:
            inner.write(b"\x03" + struct.pack("<i", len(raw) + 1) + (b"\x01" if prot else b"\x00") + raw)
        inner.write(b"\x00" + struct.pack("<i", 0))
        inner.write(self._xml_bytes(crypto.ChaCha20Stream(stream_key)))
        plain = inner.getvalue()
        if self.compression == 1:
            plain = gzip.compress(plain, 6)

        key = crypto.sha256(master_seed + self._transformed)
        enc = self._encrypt(key, iv, plain)
        hmac_base = crypto.sha512(master_seed + self._transformed + b"\x01")
        hkey = crypto.sha512(struct.pack("<Q", 0xFFFFFFFFFFFFFFFF) + hmac_base)
        out = io.BytesIO()
        out.write(hdr)
        out.write(crypto.sha256(hdr))
        out.write(crypto.hmac_sha256(hkey, hdr))
        bs = 1024 * 1024
        idx = 0
        pos = 0
        while True:
            chunk = enc[pos:pos + bs]
            pos += len(chunk)
            szb = struct.pack("<i", len(chunk))
            bk = crypto.sha512(struct.pack("<Q", idx) + hmac_base)
            out.write(crypto.hmac_sha256(bk, struct.pack("<Q", idx) + szb + chunk))
            out.write(szb)
            out.write(chunk)
            idx += 1
            if not chunk:
                break
        return out.getvalue()

    def _xml_bytes(self, stream):
        restore = []
        for el in self.root.iter():
            if el.tag in TIME_TAGS and el.text:
                d = parse_time(el.text)
                if d:
                    restore.append((el, el.text))
                    el.text = fmt_time_kdbx4(d)
            elif el.get("Protected", "").lower() == "true" and el.text:
                restore.append((el, el.text))
                el.text = b64(stream.process(el.text.encode("utf-8")))
        try:
            return ET.tostring(self.root, encoding="utf-8", xml_declaration=True)
        finally:
            for el, t in restore:
                el.text = t

    def _rebuild_binary_pool(self):
        new, mapping = [], {}
        for v in self.root.iter("Value"):
            ref = v.get("Ref")
            if ref is None:
                continue
            try:
                i = int(ref)
                raw, prot = self.binaries[i]
            except (ValueError, IndexError):
                continue
            key = (raw, prot)
            if key not in mapping:
                mapping[key] = len(new)
                new.append([raw, prot])
            v.set("Ref", str(mapping[key]))
        self.binaries = new

    # ------------------------------------------------------------ key mgmt
    def set_credentials(self, password, keyfile):
        self._composite = composite_key(password, keyfile)
        self.regenerate_kdf_salt()
        self._set_meta_time("MasterKeyChanged")
        self.modified = True

    def regenerate_kdf_salt(self):
        uid = self.kdf["$UUID"][1]
        if uid in (KDF_ARGON2D, KDF_ARGON2ID):
            self.kdf["S"] = (VD_BYTES, crypto.random_bytes(32))
        else:
            self.kdf["S"] = (VD_BYTES, crypto.random_bytes(32))
        self._transformed = None

    def check_credentials(self, password, keyfile):
        return composite_key(password, keyfile) == self._composite

    def kdf_settings(self):
        uid = self.kdf["$UUID"][1]
        if uid == KDF_AES_KDBX3:
            uid = KDF_AES
        g = lambda k, d=0: self.kdf[k][1] if k in self.kdf else d
        return {"uuid": uid, "rounds": g("R", 60000), "iterations": g("I", 2),
                "memory": g("M", 64 * 1024 * 1024), "parallelism": g("P", 2)}

    def set_kdf(self, uid, rounds=None, iterations=None, memory=None, parallelism=None):
        if uid == KDF_AES:
            self.kdf = {"$UUID": (VD_BYTES, KDF_AES), "R": (VD_UINT64, int(rounds)),
                        "S": (VD_BYTES, crypto.random_bytes(32))}
        else:
            self.kdf = {"$UUID": (VD_BYTES, uid), "S": (VD_BYTES, crypto.random_bytes(32)),
                        "P": (VD_UINT32, int(parallelism)), "M": (VD_UINT64, int(memory)),
                        "I": (VD_UINT64, int(iterations)), "V": (VD_UINT32, 0x13)}
        self._transformed = None
        self.modified = True

    # --------------------------------------------------------------- create
    @classmethod
    def create(cls, path, password, keyfile=None, name="Database"):
        db = cls()
        db.path = path
        db._composite = composite_key(password, keyfile)
        db.cipher = CIPHER_AES
        db.kdf = {"$UUID": (VD_BYTES, KDF_ARGON2D), "S": (VD_BYTES, crypto.random_bytes(32)),
                  "P": (VD_UINT32, 2), "M": (VD_UINT64, 64 * 1024 * 1024),
                  "I": (VD_UINT64, 10), "V": (VD_UINT32, 0x13)}
        t = fmt_time_iso(now_utc())
        root = ET.Element("KeePassFile")
        meta = _sub(root, "Meta")
        for tag, val in (("Generator", "Velkey"), ("DatabaseName", name),
                         ("DatabaseDescription", ""), ("DefaultUserName", ""),
                         ("MaintenanceHistoryDays", "365"), ("Color", ""),
                         ("MasterKeyChangeRec", "-1"), ("MasterKeyChangeForce", "-1")):
            if val is not None:
                _sub(meta, tag, val)
        _sub(meta, "DatabaseNameChanged", t)
        _sub(meta, "DatabaseDescriptionChanged", t)
        _sub(meta, "DefaultUserNameChanged", t)
        _sub(meta, "MasterKeyChanged", t)
        mp = _sub(meta, "MemoryProtection")
        for tag, val in (("ProtectTitle", "False"), ("ProtectUserName", "False"), ("ProtectPassword", "True"),
                         ("ProtectURL", "False"), ("ProtectNotes", "False")):
            _sub(mp, tag, val)
        _sub(meta, "RecycleBinEnabled", "True")
        _sub(meta, "RecycleBinUUID", b64(bytes(16)))
        _sub(meta, "RecycleBinChanged", t)
        _sub(meta, "EntryTemplatesGroup", b64(bytes(16)))
        _sub(meta, "EntryTemplatesGroupChanged", t)
        _sub(meta, "HistoryMaxItems", "10")
        _sub(meta, "HistoryMaxSize", "6291456")
        _sub(meta, "LastSelectedGroup", b64(bytes(16)))
        _sub(meta, "LastTopVisibleGroup", b64(bytes(16)))
        _sub(meta, "SettingsChanged", t)
        r = _sub(root, "Root")
        db.root = root
        top = db._new_group_element(name, 49)
        r.append(top)
        _sub(r, "DeletedObjects")
        for gname, icon in (("General", 48), ("Windows", 38), ("Network", 3), ("Internet", 1),
                            ("eMail", 19), ("Homebanking", 37)):
            top.append(db._new_group_element(gname, icon))
        db.modified = True
        return db

    def _new_group_element(self, name, icon=48):
        g = ET.Element("Group")
        _sub(g, "UUID", new_uuid_b64())
        _sub(g, "Name", name)
        _sub(g, "Notes", "")
        _sub(g, "IconID", str(icon))
        _times(g)
        _sub(g, "IsExpanded", "True")
        _sub(g, "DefaultAutoTypeSequence", "")
        _sub(g, "EnableAutoType", "null")
        _sub(g, "EnableSearching", "null")
        _sub(g, "LastTopVisibleEntry", b64(bytes(16)))
        return g

    # ---------------------------------------------------------------- model
    @property
    def meta(self):
        return self.root.find("Meta")

    @property
    def root_group(self):
        return Group(self, self.root.find("Root/Group"))

    def _set_meta_time(self, tag):
        _set_text(self.meta, tag, fmt_time_iso(now_utc()))

    def meta_get(self, tag, default=""):
        return _text(self.meta, tag, default)

    def meta_set(self, tag, value, changed_tag=None):
        _set_text(self.meta, tag, value)
        if changed_tag:
            self._set_meta_time(changed_tag)
        self.modified = True

    @property
    def name(self):
        return self.meta_get("DatabaseName")

    def protect_field(self, key):
        mp = self.meta.find("MemoryProtection")
        tag = {"Title": "ProtectTitle", "UserName": "ProtectUserName", "Password": "ProtectPassword",
               "URL": "ProtectURL", "Notes": "ProtectNotes"}.get(key)
        if tag is None:
            return False
        if mp is None:
            return key == "Password"
        return _bool(_text(mp, tag, "True" if key == "Password" else "False"))

    def history_limits(self):
        try:
            items = int(self.meta_get("HistoryMaxItems", "10"))
        except ValueError:
            items = 10
        try:
            size = int(self.meta_get("HistoryMaxSize", "6291456"))
        except ValueError:
            size = 6291456
        return items, size

    def recycle_bin(self, create=False):
        if not _bool(self.meta_get("RecycleBinEnabled", "True"), True):
            return None
        uid = self.meta_get("RecycleBinUUID")
        if uid and uid != b64(bytes(16)):
            g = self.find_group(uid)
            if g is not None:
                return g
        if not create:
            return None
        el = self._new_group_element("Recycle Bin", 43)
        _set_text(el, "EnableAutoType", "false")
        _set_text(el, "EnableSearching", "false")
        _set_text(el, "IsExpanded", "False")
        self.root.find("Root/Group").append(el)
        self.meta_set("RecycleBinUUID", _text(el, "UUID"), "RecycleBinChanged")
        return Group(self, el)

    def find_group(self, uid):
        for g in self.root.find("Root").iter("Group"):
            if _text(g, "UUID") == uid:
                return Group(self, g)
        return None

    def parent_of(self, el):
        if not hasattr(self, "_pmap_root") or self._pmap_root is not self.root or el not in self._pmap:
            self._pmap = {c: p for p in self.root.iter() for c in p}
            self._pmap_root = self.root
        return self._pmap.get(el)

    def invalidate(self):
        self._pmap_root = None

    def all_entries(self, include_recycle=False, group=None):
        rb = self.recycle_bin()
        rb_el = rb.el if rb else None
        out = []

        def walk(g):
            if g is rb_el and not include_recycle:
                return
            for c in g:
                if c.tag == "Entry":
                    out.append(Entry(self, c))
                elif c.tag == "Group":
                    walk(c)
        walk(group.el if group else self.root.find("Root/Group"))
        return out

    def add_deleted_object(self, uid):
        do = self.root.find("Root/DeletedObjects")
        if do is None:
            do = _sub(self.root.find("Root"), "DeletedObjects")
        d = _sub(do, "DeletedObject")
        _sub(d, "UUID", uid)
        _sub(d, "DeletionTime", fmt_time_iso(now_utc()))

    def custom_icon(self, uid):
        icons = self.meta.find("CustomIcons")
        if icons is None:
            return None
        for ic in icons.findall("Icon"):
            if _text(ic, "UUID") == uid:
                try:
                    return base64.b64decode(_text(ic, "Data"))
                except Exception:
                    return None
        return None

    def custom_icons(self):
        icons = self.meta.find("CustomIcons")
        out = []
        if icons is not None:
            for ic in icons.findall("Icon"):
                try:
                    out.append((_text(ic, "UUID"), base64.b64decode(_text(ic, "Data"))))
                except Exception:
                    pass
        return out

    def add_binary(self, data):
        self.binaries.append([data, False])
        return len(self.binaries) - 1


def _times(el):
    t = el.find("Times")
    if t is None:
        t = _sub(el, "Times")
        now = fmt_time_iso(now_utc())
        for tag in ("CreationTime", "LastModificationTime", "LastAccessTime", "ExpiryTime"):
            _sub(t, tag, now)
        _sub(t, "Expires", "False")
        _sub(t, "UsageCount", "0")
        _sub(t, "LocationChanged", now)
    return t


class _Node:
    def __init__(self, db, el):
        self.db = db
        self.el = el

    def __eq__(self, other):
        return isinstance(other, _Node) and other.el is self.el

    def __hash__(self):
        return id(self.el)

    @property
    def uuid(self):
        return _text(self.el, "UUID")

    @property
    def icon_id(self):
        try:
            return int(_text(self.el, "IconID", "0"))
        except ValueError:
            return 0

    @icon_id.setter
    def icon_id(self, v):
        _set_text(self.el, "IconID", str(v))

    @property
    def custom_icon_uuid(self):
        return _text(self.el, "CustomIconUUID")

    @custom_icon_uuid.setter
    def custom_icon_uuid(self, v):
        c = self.el.find("CustomIconUUID")
        if not v:
            if c is not None:
                self.el.remove(c)
        else:
            _set_text(self.el, "CustomIconUUID", v)

    def _time(self, tag):
        t = self.el.find("Times")
        return parse_time(_text(t, tag)) if t is not None else None

    @property
    def created(self):
        return self._time("CreationTime")

    @property
    def modified(self):
        return self._time("LastModificationTime")

    @property
    def accessed(self):
        return self._time("LastAccessTime")

    @property
    def expires(self):
        t = self.el.find("Times")
        return t is not None and _bool(_text(t, "Expires"))

    @property
    def expiry_time(self):
        return self._time("ExpiryTime")

    def set_expiry(self, enabled, when=None):
        t = _times(self.el)
        _set_text(t, "Expires", "True" if enabled else "False")
        if when is not None:
            _set_text(t, "ExpiryTime", fmt_time_iso(when))

    @property
    def is_expired(self):
        if not self.expires:
            return False
        e = self.expiry_time
        return e is not None and e <= now_utc()

    def touch(self, modify=True):
        t = _times(self.el)
        now = fmt_time_iso(now_utc())
        _set_text(t, "LastAccessTime", now)
        if modify:
            _set_text(t, "LastModificationTime", now)
        self.db.modified = True

    @property
    def parent(self):
        p = self.db.parent_of(self.el)
        if p is None or p.tag != "Group":
            return None
        return Group(self.db, p)

    def move_to(self, group):
        old = self.db.parent_of(self.el)
        if old is not None:
            old.remove(self.el)
        if isinstance(self, Group):
            group.el.append(self.el)
        else:
            # entries go before sub-groups to keep KeePass ordering
            idx = len(group.el)
            for i, c in enumerate(group.el):
                if c.tag == "Group":
                    idx = i
                    break
            group.el.insert(idx, self.el)
        t = _times(self.el)
        _set_text(t, "LocationChanged", fmt_time_iso(now_utc()))
        self.db.invalidate()
        self.db.modified = True

    def path(self):
        names = []
        g = self.parent if isinstance(self, Entry) else self
        while g is not None:
            names.append(g.name)
            g = g.parent
        return " / ".join(reversed(names))


class Group(_Node):
    @property
    def name(self):
        return _text(self.el, "Name")

    @name.setter
    def name(self, v):
        _set_text(self.el, "Name", v)

    @property
    def notes(self):
        return _text(self.el, "Notes")

    @notes.setter
    def notes(self, v):
        _set_text(self.el, "Notes", v)

    @property
    def expanded(self):
        return _bool(_text(self.el, "IsExpanded", "True"), True)

    @expanded.setter
    def expanded(self, v):
        _set_text(self.el, "IsExpanded", "True" if v else "False")

    @property
    def groups(self):
        return [Group(self.db, c) for c in self.el.findall("Group")]

    @property
    def entries(self):
        return [Entry(self.db, c) for c in self.el.findall("Entry")]

    @property
    def default_sequence(self):
        return _text(self.el, "DefaultAutoTypeSequence")

    @default_sequence.setter
    def default_sequence(self, v):
        _set_text(self.el, "DefaultAutoTypeSequence", v)

    def _tri(self, tag):
        v = _text(self.el, tag, "null").strip().lower()
        return None if v in ("null", "") else v == "true"

    @property
    def autotype_enabled(self):
        return self._tri("EnableAutoType")

    @autotype_enabled.setter
    def autotype_enabled(self, v):
        _set_text(self.el, "EnableAutoType", "null" if v is None else ("true" if v else "false"))

    @property
    def searching_enabled(self):
        return self._tri("EnableSearching")

    @searching_enabled.setter
    def searching_enabled(self, v):
        _set_text(self.el, "EnableSearching", "null" if v is None else ("true" if v else "false"))

    def effective(self, attr):
        g = self
        while g is not None:
            v = getattr(g, attr)
            if v is not None:
                return v
            g = g.parent
        return True

    def effective_sequence(self):
        g = self
        while g is not None:
            s = g.default_sequence
            if s:
                return s
            g = g.parent
        return "{USERNAME}{TAB}{PASSWORD}{ENTER}"

    def add_group(self, name, icon=48):
        el = self.db._new_group_element(name, icon)
        self.el.append(el)
        self.db.invalidate()
        self.db.modified = True
        return Group(self.db, el)

    def add_entry(self):
        el = ET.Element("Entry")
        _sub(el, "UUID", new_uuid_b64())
        _sub(el, "IconID", "0")
        _sub(el, "ForegroundColor", "")
        _sub(el, "BackgroundColor", "")
        _sub(el, "OverrideURL", "")
        _sub(el, "Tags", "")
        _times(el)
        for k in STANDARD_FIELDS:
            s = _sub(el, "String")
            _sub(s, "Key", k)
            v = _sub(s, "Value", "")
            if self.db.protect_field(k):
                v.set("Protected", "True")
        at = _sub(el, "AutoType")
        _sub(at, "Enabled", "True")
        _sub(at, "DataTransferObfuscation", "0")
        _sub(el, "History")
        idx = len(self.el)
        for i, c in enumerate(self.el):
            if c.tag == "Group":
                idx = i
                break
        self.el.insert(idx, el)
        self.db.invalidate()
        self.db.modified = True
        return Entry(self.db, el)

    def is_descendant_of(self, other):
        g = self
        while g is not None:
            if g.el is other.el:
                return True
            g = g.parent
        return False


class Entry(_Node):
    def get(self, key, default=""):
        for s in self.el.findall("String"):
            if _text(s, "Key") == key:
                v = s.find("Value")
                return (v.text or "") if v is not None else default
        return default

    def is_protected(self, key):
        for s in self.el.findall("String"):
            if _text(s, "Key") == key:
                v = s.find("Value")
                return v is not None and v.get("Protected", "").lower() == "true"
        return self.db.protect_field(key)

    def set(self, key, value, protected=None):
        if protected is None:
            protected = self.is_protected(key) if self.has(key) else self.db.protect_field(key)
        for s in self.el.findall("String"):
            if _text(s, "Key") == key:
                v = s.find("Value")
                if v is None:
                    v = _sub(s, "Value")
                break
        else:
            # insert after last String element
            idx = 0
            for i, c in enumerate(self.el):
                if c.tag == "String":
                    idx = i + 1
            if idx == 0:
                for i, c in enumerate(self.el):
                    if c.tag == "Times":
                        idx = i + 1
            s = ET.Element("String")
            _sub(s, "Key", key)
            v = _sub(s, "Value")
            self.el.insert(idx, s)
        v.text = value
        if protected:
            v.set("Protected", "True")
        elif "Protected" in v.attrib:
            del v.attrib["Protected"]

    def has(self, key):
        return any(_text(s, "Key") == key for s in self.el.findall("String"))

    def remove(self, key):
        for s in self.el.findall("String"):
            if _text(s, "Key") == key:
                self.el.remove(s)

    def fields(self):
        out = []
        for s in self.el.findall("String"):
            v = s.find("Value")
            out.append((_text(s, "Key"), (v.text or "") if v is not None else "",
                        v is not None and v.get("Protected", "").lower() == "true"))
        return out

    def custom_fields(self):
        return [f for f in self.fields() if f[0] not in STANDARD_FIELDS]

    title = property(lambda s: s.get("Title"), lambda s, v: s.set("Title", v))
    username = property(lambda s: s.get("UserName"), lambda s, v: s.set("UserName", v))
    password = property(lambda s: s.get("Password"), lambda s, v: s.set("Password", v))
    url = property(lambda s: s.get("URL"), lambda s, v: s.set("URL", v))
    notes = property(lambda s: s.get("Notes"), lambda s, v: s.set("Notes", v))

    @property
    def tags(self):
        t = _text(self.el, "Tags")
        return [x.strip() for x in t.replace(",", ";").split(";") if x.strip()]

    @tags.setter
    def tags(self, lst):
        _set_text(self.el, "Tags", ";".join(lst))

    # ---- attachments
    def attachments(self):
        out = []
        for b in self.el.findall("Binary"):
            name = _text(b, "Key")
            v = b.find("Value")
            data = b""
            if v is not None:
                ref = v.get("Ref")
                if ref is not None:
                    try:
                        data = self.db.binaries[int(ref)][0]
                    except (ValueError, IndexError):
                        data = b""
                elif v.text:
                    try:
                        data = base64.b64decode(v.text)
                        if _bool(v.get("Compressed")):
                            data = gzip.decompress(data)
                    except Exception:
                        data = b""
            out.append((name, data))
        return out

    def set_attachments(self, items):
        for b in self.el.findall("Binary"):
            self.el.remove(b)
        idx = 0
        for i, c in enumerate(self.el):
            if c.tag in ("String",):
                idx = i + 1
        for name, data in items:
            b = ET.Element("Binary")
            _sub(b, "Key", name)
            v = _sub(b, "Value")
            v.set("Ref", str(self.db.add_binary(data)))
            self.el.insert(idx, b)
            idx += 1

    # ---- auto-type
    def _at(self):
        at = self.el.find("AutoType")
        if at is None:
            at = _sub(self.el, "AutoType")
            _sub(at, "Enabled", "True")
            _sub(at, "DataTransferObfuscation", "0")
        return at

    @property
    def autotype_enabled(self):
        at = self.el.find("AutoType")
        return True if at is None else _bool(_text(at, "Enabled", "True"), True)

    @autotype_enabled.setter
    def autotype_enabled(self, v):
        _set_text(self._at(), "Enabled", "True" if v else "False")

    @property
    def autotype_sequence(self):
        at = self.el.find("AutoType")
        return _text(at, "DefaultSequence") if at is not None else ""

    @autotype_sequence.setter
    def autotype_sequence(self, v):
        at = self._at()
        if v:
            _set_text(at, "DefaultSequence", v)
        else:
            c = at.find("DefaultSequence")
            if c is not None:
                at.remove(c)

    def associations(self):
        at = self.el.find("AutoType")
        if at is None:
            return []
        return [(_text(a, "Window"), _text(a, "KeystrokeSequence")) for a in at.findall("Association")]

    def set_associations(self, items):
        at = self._at()
        for a in at.findall("Association"):
            at.remove(a)
        for w, seq in items:
            a = _sub(at, "Association")
            _sub(a, "Window", w)
            _sub(a, "KeystrokeSequence", seq)

    def effective_sequence(self):
        s = self.autotype_sequence
        if s:
            return s
        p = self.parent
        return p.effective_sequence() if p else "{USERNAME}{TAB}{PASSWORD}{ENTER}"

    # ---- colors
    @property
    def fg_color(self):
        return _text(self.el, "ForegroundColor")

    @property
    def bg_color(self):
        return _text(self.el, "BackgroundColor")

    def set_colors(self, fg, bg):
        _set_text(self.el, "ForegroundColor", fg or "")
        _set_text(self.el, "BackgroundColor", bg or "")

    # ---- history
    def history(self):
        h = self.el.find("History")
        return [Entry(self.db, e) for e in h.findall("Entry")] if h is not None else []

    def backup(self):
        """Push a copy of the current state into the history (call before edits)."""
        snap = copy.deepcopy(self.el)
        hh = snap.find("History")
        if hh is not None:
            snap.remove(hh)
        h = self.el.find("History")
        if h is None:
            h = _sub(self.el, "History")
        h.append(snap)
        self.trim_history()

    def trim_history(self):
        h = self.el.find("History")
        if h is None:
            return
        max_items, max_size = self.db.history_limits()
        items = h.findall("Entry")
        if max_items >= 0:
            while len(items) > max_items:
                h.remove(items.pop(0))
        if max_size >= 0:
            def size(e):
                return sum(len((x.text or "")) for x in e.iter()) + sum(
                    len(d) for _, d in Entry(self.db, e).attachments())
            while items and sum(size(e) for e in items) > max_size:
                h.remove(items.pop(0))

    def restore_history(self, hist_entry):
        self.backup()
        keep_hist = self.el.find("History")
        for c in list(self.el):
            self.el.remove(c)
        for c in hist_entry.el:
            if c.tag != "History":
                self.el.append(copy.deepcopy(c))
        self.el.append(keep_hist)
        self.touch()

    def delete_history(self, hist_entry):
        h = self.el.find("History")
        if h is not None and hist_entry.el in list(h):
            h.remove(hist_entry.el)
            self.db.modified = True

    def duplicate(self, suffix=" - Copy"):
        el = copy.deepcopy(self.el)
        _set_text(el, "UUID", new_uuid_b64())
        h = el.find("History")
        if h is not None:
            for c in list(h):
                h.remove(c)
        parent = self.db.parent_of(self.el)
        idx = list(parent).index(self.el) + 1
        parent.insert(idx, el)
        e = Entry(self.db, el)
        if suffix:
            e.title = e.title + suffix
        now = fmt_time_iso(now_utc())
        t = _times(el)
        for tag in ("CreationTime", "LastModificationTime", "LastAccessTime", "LocationChanged"):
            _set_text(t, tag, now)
        self.db.invalidate()
        self.db.modified = True
        return e

    def matches(self, terms):
        hay = " ".join([self.title, self.username, self.url, self.notes, " ".join(self.tags)] +
                       [v for k, v, p in self.custom_fields() if not p]).lower()
        return all(t in hay for t in terms)


# ------------------------------------------------------------------------
# copying entries between databases (export / import / copy to other tab)
# ------------------------------------------------------------------------
def _copy_custom_icon(src_db, dst_db, uid):
    if not uid or dst_db.custom_icon(uid) is not None:
        return
    data = src_db.custom_icon(uid)
    if data is None:
        return
    icons = dst_db.meta.find("CustomIcons")
    if icons is None:
        icons = _sub(dst_db.meta, "CustomIcons")
    for ic in src_db.meta.find("CustomIcons").findall("Icon"):
        if _text(ic, "UUID") == uid:
            icons.append(copy.deepcopy(ic))
            return


def _uuids(db):
    return {_text(el, "UUID") for el in db.root.find("Root").iter() if el.tag in ("Entry", "Group")}


def copy_entry(entry, group, taken=None):
    """Copy `entry` (with history, attachments and custom icon) into `group`,
    which may belong to another database. The copy gets a new UUID when the
    target database already has one with the same UUID. Returns the new Entry."""
    src, dst = entry.db, group.db
    el = copy.deepcopy(entry.el)
    if taken is None:
        taken = _uuids(dst)
    if _text(el, "UUID") in taken:
        _set_text(el, "UUID", new_uuid_b64())
    taken.add(_text(el, "UUID"))
    if src is not dst:
        for v in el.iter("Value"):
            ref = v.get("Ref")
            if ref is not None:
                try:
                    raw, prot = src.binaries[int(ref)]
                except (ValueError, IndexError):
                    raw, prot = b"", False
                dst.binaries.append([raw, prot])
                v.set("Ref", str(len(dst.binaries) - 1))
        for c in el.iter("CustomIconUUID"):
            _copy_custom_icon(src, dst, c.text)
    idx = len(group.el)
    for i, c in enumerate(group.el):
        if c.tag == "Group":
            idx = i
            break
    group.el.insert(idx, el)
    t = _times(el)
    _set_text(t, "LocationChanged", fmt_time_iso(now_utc()))
    dst.invalidate()
    dst.modified = True
    return Entry(dst, el)


def _child_group(group, name, icon):
    for g in group.groups:
        if g.name == name:
            return g
    return group.add_group(name, icon)


def copy_entries(entries, group):
    """Copy entries into `group`, recreating their sub-group path below the
    source database's top group (e.g. 'Internet/Shops'). Returns the copies."""
    taken = _uuids(group.db)
    out = []
    for e in entries:
        target = group
        chain = []
        p = e.parent
        while p is not None and p.parent is not None:  # stop below the top group
            chain.append(p)
            p = p.parent
        for g in reversed(chain):
            target = _child_group(target, g.name, g.icon_id)
            if g.custom_icon_uuid:
                _copy_custom_icon(e.db, group.db, g.custom_icon_uuid)
                target.custom_icon_uuid = g.custom_icon_uuid
        out.append(copy_entry(e, target, taken))
    return out


def export_entries(entries, path, password, keyfile=None, name="Exported entries"):
    """Write `entries` to a new encrypted KDBX file (no default groups)."""
    db = Database.create(path, password, keyfile, name)
    top = db.root_group
    for g in list(top.el.findall("Group")):
        top.el.remove(g)
    db.invalidate()
    for e in entries:
        copy_entry(e, top)
    db.save()
    return db


def find_duplicates(entries, db):
    """Which of `entries` the database `db` already has (recycle bin ignored).

    Returns [(entry, existing, same)]: `same` is True when title, user name,
    password and URL are identical, False when it is the same entry (same
    UUID) whose details differ, e.g. an older or newer password."""
    def key(e):
        return (e.title.strip(), e.username.strip(), e.password, e.url.strip())
    have = db.all_entries()
    by_key = {key(x): x for x in have}
    by_uuid = {x.uuid: x for x in have}
    out = []
    for e in entries:
        hit = by_key.get(key(e))
        if hit is not None:
            out.append((e, hit, True))
        elif e.uuid in by_uuid:
            out.append((e, by_uuid[e.uuid], False))
    return out
