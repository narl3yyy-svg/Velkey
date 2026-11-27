"""TOTP support compatible with KeePass 2.47+ (TimeOtp-* fields), KeePassXC
(`otp` field with an otpauth:// URI), the KeeOtp plugin and legacy
KeePassXC 'TOTP Seed' / 'TOTP Settings' fields."""
import base64
import hashlib
import hmac
import struct
import time
import urllib.parse

STEAM_CHARS = "23456789BCDFGHJKMNPQRTVWXY"
_ALGOS = {"SHA1": hashlib.sha1, "SHA256": hashlib.sha256, "SHA512": hashlib.sha512}


class OtpConfig:
    def __init__(self, secret, digits=6, period=30, algo="SHA1", steam=False):
        self.secret = secret
        self.digits = digits
        self.period = period
        self.algo = algo
        self.steam = steam

    def code(self, now=None):
        now = time.time() if now is None else now
        counter = int(now // self.period)
        mac = hmac.new(self.secret, struct.pack(">Q", counter), _ALGOS.get(self.algo, hashlib.sha1)).digest()
        off = mac[-1] & 0x0F
        val = struct.unpack(">I", mac[off:off + 4])[0] & 0x7FFFFFFF
        if self.steam:
            out = ""
            for _ in range(5):
                out += STEAM_CHARS[val % len(STEAM_CHARS)]
                val //= len(STEAM_CHARS)
            return out
        return str(val % (10 ** self.digits)).zfill(self.digits)

    def remaining(self, now=None):
        now = time.time() if now is None else now
        return self.period - (int(now) % self.period)


def looks_like_code(s):
    """True if `s` is a one-time code (e.g. '123 456') rather than a secret."""
    s = "".join((s or "").split())
    return s.isdigit() and 4 <= len(s) <= 10


def b32decode(s):
    s = "".join(s.split()).replace("-", "").upper().rstrip("=")
    s += "=" * (-len(s) % 8)
    return base64.b32decode(s)


def _algo_name(a):
    a = (a or "SHA1").upper().replace("HMAC-", "").replace("-", "")
    return a if a in _ALGOS else "SHA1"


def parse_uri(uri):
    u = urllib.parse.urlparse(uri.strip())
    q = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(u.query).items()}
    if "secret" not in q:
        return None
    steam = q.get("encoder", "").lower() == "steam" or u.netloc.lower() == "steam"
    return OtpConfig(b32decode(q["secret"]), int(q.get("digits", 6)), int(q.get("period", 30)),
                     _algo_name(q.get("algorithm")), steam)


def from_entry(entry):
    """Return an OtpConfig for the entry or None."""
    try:
        g = entry.get
        # KeePass 2.47+ native fields
        secret = None
        if g("TimeOtp-Secret-Base32"):
            secret = b32decode(g("TimeOtp-Secret-Base32"))
        elif g("TimeOtp-Secret-Hex"):
            secret = bytes.fromhex("".join(g("TimeOtp-Secret-Hex").split()))
        elif g("TimeOtp-Secret-Base64"):
            secret = base64.b64decode(g("TimeOtp-Secret-Base64"))
        elif g("TimeOtp-Secret"):
            secret = g("TimeOtp-Secret").encode("utf-8")
        if secret:
            return OtpConfig(secret, int(g("TimeOtp-Length") or 6), int(g("TimeOtp-Period") or 30),
                             _algo_name(g("TimeOtp-Algorithm")))
        otp = g("otp") or g("OTP")
        if otp:
            otp = otp.strip()
            if otp.lower().startswith("otpauth://"):
                return parse_uri(otp)
            # KeeOtp: key=...&size=6&step=30&otpHashMode=Sha256
            q = {k.lower(): v[0] for k, v in urllib.parse.parse_qs(otp).items()}
            if "key" in q:
                if q.get("type", "totp").lower() != "totp":
                    return None
                return OtpConfig(b32decode(q["key"]), int(q.get("size", 6)), int(q.get("step", 30)),
                                 _algo_name(q.get("otphashmode")))
            # plain base32 secret
            return OtpConfig(b32decode(otp))
        seed = g("TOTP Seed")
        if seed:
            period, digits, steam = 30, 6, False
            st = g("TOTP Settings")
            if st and ";" in st:
                p, d = st.split(";", 1)
                period = int(p or 30)
                if d.strip().upper() == "S":
                    steam = True
                else:
                    digits = int(d or 6)
            return OtpConfig(b32decode(seed), digits, period, "SHA1", steam)
    except Exception:
        return None
    return None


def has_otp(entry):
    return from_entry(entry) is not None


def current_code(entry):
    cfg = from_entry(entry)
    return cfg.code() if cfg else ""


OTP_FIELDS = ("TimeOtp-Secret", "TimeOtp-Secret-Hex", "TimeOtp-Secret-Base32", "TimeOtp-Secret-Base64",
              "TimeOtp-Length", "TimeOtp-Period", "TimeOtp-Algorithm", "otp")


def store(entry, secret_text, digits=6, period=30, algo="SHA1"):
    """Write TOTP settings to an entry in KeePass 2.47+ format.  `secret_text`
    may be a base32 secret or an otpauth:// URI."""
    for k in OTP_FIELDS:
        if entry.has(k):
            entry.remove(k)
    secret_text = (secret_text or "").strip()
    if not secret_text:
        return
    if secret_text.lower().startswith("otpauth://"):
        cfg = parse_uri(secret_text)
        if cfg is None:
            raise ValueError("The otpauth URI has no secret")
        if cfg.steam:
            entry.set("otp", secret_text, True)
            return
        secret_b32 = base64.b32encode(cfg.secret).decode().rstrip("=")
        digits, period, algo = cfg.digits, cfg.period, cfg.algo
    else:
        b32decode(secret_text)  # validate
        secret_b32 = "".join(secret_text.split()).upper()
    entry.set("TimeOtp-Secret-Base32", secret_b32, True)
    if digits != 6:
        entry.set("TimeOtp-Length", str(digits), False)
    if period != 30:
        entry.set("TimeOtp-Period", str(period), False)
    if algo != "SHA1":
        entry.set("TimeOtp-Algorithm", {"SHA256": "HMAC-SHA-256", "SHA512": "HMAC-SHA-512"}[algo], False)
