"""Gemeinsames Protokoll von opentop-host und opentop-client.

Kopplung (ähnlich Bluetooth „Numeric Comparison“):

    Client                                Host
    hello  {id, name, pub}          ──►
                                    ◄──   commit {pub, commit = H(pubs, Nh)}
    nonce  {id, nonce = Nc}         ──►
                                    ◄──   reveal {nonce = Nh}
    beide zeigen code(X25519, Nc, Nh) an, der Nutzer bestätigt am Host
                                    ◄──   paired (signiert) | rejected

Weil der Host sich auf Nh festlegt, bevor er Nc kennt, und der Client Nc
schickt, bevor er Nh kennt, kann ein Angreifer in der Mitte die beiden Codes
nur mit Wahrscheinlichkeit 1:10^6 gleich machen.

Danach sind alle Nachrichten mit dem gemeinsamen Schlüssel per HMAC signiert,
und das Video läuft per SRTP verschlüsselt.
"""
import base64
import hashlib
import hmac
import json
import os
import secrets

# X25519 kommt aus python-cryptography oder, falls das fehlt, aus PyNaCl
# (libsodium). PyNaCl braucht kein Rust und ist auf schwachen Clients, die
# alles selbst kompilieren (z.B. Gentoo), deutlich schneller installiert.
# Beide liefern bitgleiche Schlüssel, Host und Client dürfen sie mischen.
try:
    from cryptography.hazmat.primitives import serialization
    from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey

    def _x25519_public(private):
        return X25519PrivateKey.from_private_bytes(private).public_key().public_bytes(
            serialization.Encoding.Raw, serialization.PublicFormat.Raw)

    def _x25519_exchange(private, peer_public):
        return X25519PrivateKey.from_private_bytes(private).exchange(
            X25519PublicKey.from_public_bytes(peer_public))
except ImportError:
    from nacl.bindings import crypto_scalarmult, crypto_scalarmult_base

    def _x25519_public(private):
        return crypto_scalarmult_base(private)

    def _x25519_exchange(private, peer_public):
        if len(peer_public) != 32:
            raise ValueError("Ungültiger öffentlicher Schlüssel")
        try:
            return crypto_scalarmult(private, peer_public)
        except Exception as e:  # libsodium lehnt Punkte kleiner Ordnung ab
            raise ValueError(str(e)) from e

VERSION = 2
BEACON_PORT = 47800
STREAM_PORT = 47801
PAIRING_TIMEOUT = 120
if os.name == "nt":
    CONFIG_DIR = os.path.join(os.environ.get("APPDATA", os.path.expanduser("~")), "opentop")
else:
    CONFIG_DIR = os.path.join(os.environ.get("XDG_CONFIG_HOME", os.path.expanduser("~/.config")), "opentop")

# Gleiche Verfahren für Sender (srtpenc-Properties) und Empfänger (Caps).
SRTPENC_PARAMS = ("rtp-cipher=aes-128-icm rtp-auth=hmac-sha1-80 "
                  "rtcp-cipher=aes-128-icm rtcp-auth=hmac-sha1-80")
SRTP_PARAMS = ("srtp-cipher=aes-128-icm,srtp-auth=hmac-sha1-80,"
               "srtcp-cipher=aes-128-icm,srtcp-auth=hmac-sha1-80")


def b64(data):
    return base64.b64encode(data).decode()


def unb64(text):
    return base64.b64decode(text)


def new_nonce():
    return b64(secrets.token_bytes(16))


def encode(msg):
    return json.dumps({"opentop": VERSION, **msg}, sort_keys=True).encode()


def decode(data):
    try:
        msg = json.loads(data)
    except ValueError:
        return None
    if not isinstance(msg, dict) or msg.get("opentop") != VERSION:
        return None
    return msg


def _mac(msg, key):
    body = {k: v for k, v in msg.items() if k != "mac"}
    return hmac.new(key, json.dumps(body, sort_keys=True).encode(), hashlib.sha256).hexdigest()


def sign(msg, key):
    return {**msg, "mac": _mac({"opentop": VERSION, **msg}, key)}


def verify(msg, key):
    return isinstance(msg.get("mac"), str) and hmac.compare_digest(msg["mac"], _mac(msg, key))


def watch_socket(sock, callback):
    """Ruft callback() auf, sobald auf sock Daten anliegen (im GLib-Mainloop).

    Unter Windows sind Sockets keine Dateideskriptoren; GLib braucht dort
    einen eigenen IOChannel-Typ.
    """
    from gi.repository import GLib
    if os.name == "nt":
        channel = GLib.IOChannel.win32_new_socket(sock.fileno())
        GLib.io_add_watch(channel, GLib.PRIORITY_DEFAULT, GLib.IO_IN, lambda *_: callback())
    else:
        GLib.io_add_watch(sock.fileno(), GLib.IO_IN, lambda *_: callback())


def load_json(path, default):
    try:
        with open(path) as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd = os.open(path + ".tmp", os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f, indent=2)
    os.replace(path + ".tmp", path)


class Identity:
    """Dauerhaftes X25519-Schlüsselpaar dieses Rechners."""

    def __init__(self, path):
        try:
            with open(path, "rb") as f:
                self.key = f.read()
            if len(self.key) != 32:
                raise ValueError("Schlüsseldatei beschädigt")
        except (OSError, ValueError):
            self.key = secrets.token_bytes(32)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(self.key)
        self.pub = b64(_x25519_public(self.key))

    def shared(self, peer_pub):
        return _x25519_exchange(self.key, unb64(peer_pub))


def commitment(host_pub, client_pub, host_nonce):
    return hashlib.sha256(f"opentop-commit|{host_pub}|{client_pub}|{host_nonce}".encode()).hexdigest()


def hkdf_sha256(key, length, salt, info):
    """HKDF nach RFC 5869 mit SHA-256; ohne salt wie üblich 32 Nullbytes."""
    prk = hmac.new(salt or bytes(32), key, hashlib.sha256).digest()
    out, block = b"", b""
    for counter in range(1, -(-length // 32) + 1):
        block = hmac.new(prk, block + info + bytes([counter]), hashlib.sha256).digest()
        out += block
    return out[:length]


def _derive(shared, host_pub, client_pub, client_nonce, host_nonce, info, length):
    salt = hashlib.sha256(f"{host_pub}|{client_pub}|{client_nonce}|{host_nonce}".encode()).digest()
    return hkdf_sha256(shared, length, salt, info)


def pairing_code(shared, host_pub, client_pub, client_nonce, host_nonce):
    raw = _derive(shared, host_pub, client_pub, client_nonce, host_nonce, b"opentop code", 8)
    code = int.from_bytes(raw, "big") % 1_000_000
    return f"{code // 1000:03d} {code % 1000:03d}"


def session_key(shared, host_pub, client_pub, client_nonce, host_nonce):
    return _derive(shared, host_pub, client_pub, client_nonce, host_nonce, b"opentop key", 32)


def srtp_key(key):
    # 16 Byte AES-Schlüssel + 14 Byte Salt für aes-128-icm.
    return hkdf_sha256(key, 30, None, b"opentop srtp")


def srtp_caps(key):
    return ("application/x-srtp,media=video,encoding-name=H264,payload=96,clock-rate=90000,"
            f"srtp-key=(buffer){srtp_key(key).hex()},{SRTP_PARAMS}")
