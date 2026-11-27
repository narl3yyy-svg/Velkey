"""Crypto primitives for Velkey.

Everything heavy (AES, ChaCha20, Argon2) is delegated to the system OpenSSL
libcrypto (>= 3.2) through ctypes, so no Python packages or compilers are
needed.  ctypes releases the GIL during foreign calls, so key derivation can
run in a worker thread without freezing the UI.
"""
import ctypes
import ctypes.util
import hashlib
import hmac
import os
import struct

__all__ = [
    "sha256", "sha512", "hmac_sha256", "aes_cbc_encrypt", "aes_cbc_decrypt",
    "chacha20", "ChaCha20Stream", "Salsa20Stream", "aes_kdf", "argon2",
    "random_bytes", "CryptoError",
]


class CryptoError(Exception):
    pass


def _load_libcrypto():
    names = ["libcrypto.so.3"]
    found = ctypes.util.find_library("crypto")
    if found:
        names.append(found)
    for n in names:
        try:
            return ctypes.CDLL(n)
        except OSError:
            continue
    raise CryptoError("OpenSSL libcrypto (version 3.x) was not found on this system")


_lib = _load_libcrypto()
_vp = ctypes.c_void_p

_lib.EVP_CIPHER_CTX_new.restype = _vp
_lib.EVP_CIPHER_CTX_free.argtypes = [_vp]
_lib.EVP_CipherInit_ex.argtypes = [_vp, _vp, _vp, ctypes.c_char_p, ctypes.c_char_p, ctypes.c_int]
_lib.EVP_CIPHER_CTX_set_padding.argtypes = [_vp, ctypes.c_int]
_lib.EVP_CipherUpdate.argtypes = [_vp, _vp, ctypes.POINTER(ctypes.c_int), _vp, ctypes.c_int]
_lib.EVP_CipherFinal_ex.argtypes = [_vp, _vp, ctypes.POINTER(ctypes.c_int)]
for _fn in ("EVP_aes_256_cbc", "EVP_chacha20"):
    getattr(_lib, _fn).restype = _vp
    getattr(_lib, _fn).argtypes = []

_AES_CBC = _lib.EVP_aes_256_cbc()
_CHACHA = _lib.EVP_chacha20()


def random_bytes(n):
    return os.urandom(n)


def sha256(data):
    return hashlib.sha256(data).digest()


def sha512(data):
    return hashlib.sha512(data).digest()


def hmac_sha256(key, data):
    return hmac.new(key, data, hashlib.sha256).digest()


class _Cipher:
    """Thin wrapper around an EVP_CIPHER_CTX."""

    def __init__(self, cipher, key, iv, encrypt, padding):
        self.ctx = _lib.EVP_CIPHER_CTX_new()
        if not self.ctx:
            raise CryptoError("EVP_CIPHER_CTX_new failed")
        if not _lib.EVP_CipherInit_ex(self.ctx, cipher, None, key, iv, 1 if encrypt else 0):
            self.close()
            raise CryptoError("cipher init failed")
        _lib.EVP_CIPHER_CTX_set_padding(self.ctx, 1 if padding else 0)

    def update(self, data, out=None):
        n = len(data)
        if out is None:
            out = ctypes.create_string_buffer(n + 32)
        outl = ctypes.c_int(0)
        src = ctypes.c_char_p(bytes(data)) if not isinstance(data, ctypes.Array) else data
        if not _lib.EVP_CipherUpdate(self.ctx, out, ctypes.byref(outl), src, n):
            raise CryptoError("cipher update failed")
        return out, outl.value

    def final(self):
        out = ctypes.create_string_buffer(64)
        outl = ctypes.c_int(0)
        if not _lib.EVP_CipherFinal_ex(self.ctx, out, ctypes.byref(outl)):
            raise CryptoError("decryption failed (wrong key or corrupted data)")
        return out.raw[:outl.value]

    def close(self):
        if self.ctx:
            _lib.EVP_CIPHER_CTX_free(self.ctx)
            self.ctx = None

    def __del__(self):
        self.close()


def _run(cipher, key, iv, data, encrypt, padding):
    c = _Cipher(cipher, key, iv, encrypt, padding)
    try:
        out, n = c.update(data)
        return out.raw[:n] + c.final()
    finally:
        c.close()


def aes_cbc_encrypt(key, iv, data):
    return _run(_AES_CBC, key, iv, data, True, True)


def aes_cbc_decrypt(key, iv, data):
    return _run(_AES_CBC, key, iv, data, False, True)


def chacha20(key, nonce, data, counter=0):
    if len(nonce) != 12:
        raise CryptoError("ChaCha20 nonce must be 12 bytes")
    iv = struct.pack("<I", counter) + nonce
    return _run(_CHACHA, key, iv, data, True, False)


class ChaCha20Stream:
    """Continuous ChaCha20 keystream used for KDBX4 protected values."""

    def __init__(self, key):
        h = sha512(key)
        self._c = _Cipher(_CHACHA, h[:32], struct.pack("<I", 0) + h[32:44], True, False)

    def process(self, data):
        if not data:
            return b""
        out, n = self._c.update(data)
        return out.raw[:n]


# --------------------------------------------------------------------------
# Salsa20 (only needed for the inner stream of KDBX 3.1 files; data is tiny)
# --------------------------------------------------------------------------
_M32 = 0xFFFFFFFF


def _rotl(v, c):
    return ((v << c) & _M32) | (v >> (32 - c))


def _salsa20_block(state):
    x = list(state)
    for _ in range(10):
        # column round
        x[4] ^= _rotl((x[0] + x[12]) & _M32, 7); x[8] ^= _rotl((x[4] + x[0]) & _M32, 9)
        x[12] ^= _rotl((x[8] + x[4]) & _M32, 13); x[0] ^= _rotl((x[12] + x[8]) & _M32, 18)
        x[9] ^= _rotl((x[5] + x[1]) & _M32, 7); x[13] ^= _rotl((x[9] + x[5]) & _M32, 9)
        x[1] ^= _rotl((x[13] + x[9]) & _M32, 13); x[5] ^= _rotl((x[1] + x[13]) & _M32, 18)
        x[14] ^= _rotl((x[10] + x[6]) & _M32, 7); x[2] ^= _rotl((x[14] + x[10]) & _M32, 9)
        x[6] ^= _rotl((x[2] + x[14]) & _M32, 13); x[10] ^= _rotl((x[6] + x[2]) & _M32, 18)
        x[3] ^= _rotl((x[15] + x[11]) & _M32, 7); x[7] ^= _rotl((x[3] + x[15]) & _M32, 9)
        x[11] ^= _rotl((x[7] + x[3]) & _M32, 13); x[15] ^= _rotl((x[11] + x[7]) & _M32, 18)
        # row round
        x[1] ^= _rotl((x[0] + x[3]) & _M32, 7); x[2] ^= _rotl((x[1] + x[0]) & _M32, 9)
        x[3] ^= _rotl((x[2] + x[1]) & _M32, 13); x[0] ^= _rotl((x[3] + x[2]) & _M32, 18)
        x[6] ^= _rotl((x[5] + x[4]) & _M32, 7); x[7] ^= _rotl((x[6] + x[5]) & _M32, 9)
        x[4] ^= _rotl((x[7] + x[6]) & _M32, 13); x[5] ^= _rotl((x[4] + x[7]) & _M32, 18)
        x[11] ^= _rotl((x[10] + x[9]) & _M32, 7); x[8] ^= _rotl((x[11] + x[10]) & _M32, 9)
        x[9] ^= _rotl((x[8] + x[11]) & _M32, 13); x[10] ^= _rotl((x[9] + x[8]) & _M32, 18)
        x[12] ^= _rotl((x[15] + x[14]) & _M32, 7); x[13] ^= _rotl((x[12] + x[15]) & _M32, 9)
        x[14] ^= _rotl((x[13] + x[12]) & _M32, 13); x[15] ^= _rotl((x[14] + x[13]) & _M32, 18)
    return struct.pack("<16I", *[(x[i] + state[i]) & _M32 for i in range(16)])


class Salsa20Stream:
    IV = bytes([0xE8, 0x30, 0x09, 0x4B, 0x97, 0x20, 0x5D, 0x2A])

    def __init__(self, key):
        k = sha256(key)
        kw = struct.unpack("<8I", k)
        iv = struct.unpack("<2I", self.IV)
        c = struct.unpack("<4I", b"expand 32-byte k")
        self.state = [c[0], kw[0], kw[1], kw[2], kw[3], c[1], iv[0], iv[1],
                      0, 0, c[2], kw[4], kw[5], kw[6], kw[7], c[3]]
        self.buf = b""

    def _more(self):
        self.buf += _salsa20_block(self.state)
        self.state[8] = (self.state[8] + 1) & _M32
        if self.state[8] == 0:
            self.state[9] = (self.state[9] + 1) & _M32

    def process(self, data):
        while len(self.buf) < len(data):
            self._more()
        ks, self.buf = self.buf[:len(data)], self.buf[len(data):]
        return bytes(a ^ b for a, b in zip(data, ks))


# --------------------------------------------------------------------------
# Key derivation
# --------------------------------------------------------------------------
def aes_kdf(key, seed, rounds):
    """KeePass AES-KDF: encrypt each 16-byte half of `key` `rounds` times with
    AES-256-ECB keyed by `seed`.  Implemented as CBC over zero blocks with the
    half as IV, which yields exactly the same chain but runs at native speed."""
    if len(key) != 32 or len(seed) != 32:
        raise CryptoError("AES-KDF needs 32-byte key and seed")
    chunk_blocks = 65536
    zeros = ctypes.create_string_buffer(chunk_blocks * 16)
    out = ctypes.create_string_buffer(chunk_blocks * 16 + 32)
    result = b""
    for half in (key[:16], key[16:]):
        if rounds == 0:
            result += half
            continue
        c = _Cipher(_AES_CBC, seed, half, True, False)
        remaining = rounds
        last = half
        while remaining:
            n = min(remaining, chunk_blocks)
            outl = ctypes.c_int(0)
            if not _lib.EVP_CipherUpdate(c.ctx, out, ctypes.byref(outl), zeros, n * 16):
                raise CryptoError("AES-KDF failed")
            last = ctypes.string_at(ctypes.addressof(out) + (n - 1) * 16, 16)
            remaining -= n
        c.close()
        result += last
    return sha256(result)


class _OSSL_PARAM(ctypes.Structure):
    _fields_ = [("key", ctypes.c_char_p), ("data_type", ctypes.c_uint),
                ("data", _vp), ("data_size", ctypes.c_size_t),
                ("return_size", ctypes.c_size_t)]


_OSSL_PARAM_UNSIGNED_INTEGER = 2
_OSSL_PARAM_OCTET_STRING = 5
_UNMODIFIED = ctypes.c_size_t(-1).value
_argon_threads_set = False

try:
    _lib.EVP_KDF_fetch.restype = _vp
    _lib.EVP_KDF_fetch.argtypes = [_vp, ctypes.c_char_p, ctypes.c_char_p]
    _lib.EVP_KDF_CTX_new.restype = _vp
    _lib.EVP_KDF_CTX_new.argtypes = [_vp]
    _lib.EVP_KDF_CTX_free.argtypes = [_vp]
    _lib.EVP_KDF_free.argtypes = [_vp]
    _lib.EVP_KDF_derive.argtypes = [_vp, _vp, ctypes.c_size_t, _vp]
    _HAVE_KDF = True
except AttributeError:
    _HAVE_KDF = False


def argon2(password, salt, iterations, memory_kib, parallelism, version=0x13,
           variant="d", secret=b"", ad=b"", length=32):
    """Argon2d / Argon2id via OpenSSL >= 3.2."""
    global _argon_threads_set
    if not _HAVE_KDF:
        raise CryptoError("This OpenSSL is too old for Argon2 (need 3.2+)")
    name = b"ARGON2ID" if variant == "id" else b"ARGON2D"
    kdf = _lib.EVP_KDF_fetch(None, name, None)
    if not kdf:
        raise CryptoError("OpenSSL on this system does not provide Argon2 (need 3.2+)")
    threads = max(1, min(parallelism, os.cpu_count() or 1))
    if threads > 1 and not _argon_threads_set:
        try:
            _lib.OSSL_set_max_threads.argtypes = [_vp, ctypes.c_uint64]
            if _lib.OSSL_set_max_threads(None, max(64, threads)):
                _argon_threads_set = True
            else:
                threads = 1
        except AttributeError:
            threads = 1
    ctx = _lib.EVP_KDF_CTX_new(kdf)
    keep = []

    def octet(k, v):
        b = ctypes.create_string_buffer(bytes(v), max(1, len(v)))
        keep.append(b)
        return _OSSL_PARAM(k, _OSSL_PARAM_OCTET_STRING, ctypes.cast(b, _vp), len(v), _UNMODIFIED)

    def u32(k, v):
        b = ctypes.c_uint32(v)
        keep.append(b)
        return _OSSL_PARAM(k, _OSSL_PARAM_UNSIGNED_INTEGER, ctypes.cast(ctypes.pointer(b), _vp), 4, _UNMODIFIED)

    plist = [octet(b"pass", password), octet(b"salt", salt),
             u32(b"iter", iterations), u32(b"memcost", memory_kib),
             u32(b"lanes", parallelism), u32(b"threads", threads),
             u32(b"version", version)]
    if secret:
        plist.append(octet(b"secret", secret))
    if ad:
        plist.append(octet(b"ad", ad))
    plist.append(_OSSL_PARAM(None, 0, None, 0, 0))
    params = (_OSSL_PARAM * len(plist))(*plist)
    out = ctypes.create_string_buffer(length)
    try:
        ok = _lib.EVP_KDF_derive(ctx, out, length, params)
        if not ok and threads > 1:
            # retry single-threaded (thread pool may be unavailable)
            params[5] = u32(b"threads", 1)
            ok = _lib.EVP_KDF_derive(ctx, out, length, params)
        if not ok:
            raise CryptoError("Argon2 key derivation failed")
        return out.raw
    finally:
        _lib.EVP_KDF_CTX_free(ctx)
        _lib.EVP_KDF_free(kdf)
