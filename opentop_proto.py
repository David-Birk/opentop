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

from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.kdf.hkdf import HKDF

VERSION = 2
BEACON_PORT = 47800
STREAM_PORT = 47801
PAIRING_TIMEOUT = 120
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
                self.key = X25519PrivateKey.from_private_bytes(f.read())
        except (OSError, ValueError):
            self.key = X25519PrivateKey.generate()
            os.makedirs(os.path.dirname(path), exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "wb") as f:
                f.write(self.key.private_bytes(serialization.Encoding.Raw,
                                               serialization.PrivateFormat.Raw,
                                               serialization.NoEncryption()))
        self.pub = b64(self.key.public_key().public_bytes(serialization.Encoding.Raw,
                                                          serialization.PublicFormat.Raw))

    def shared(self, peer_pub):
        return self.key.exchange(X25519PublicKey.from_public_bytes(unb64(peer_pub)))


def commitment(host_pub, client_pub, host_nonce):
    return hashlib.sha256(f"opentop-commit|{host_pub}|{client_pub}|{host_nonce}".encode()).hexdigest()


def _derive(shared, host_pub, client_pub, client_nonce, host_nonce, info, length):
    salt = hashlib.sha256(f"{host_pub}|{client_pub}|{client_nonce}|{host_nonce}".encode()).digest()
    return HKDF(algorithm=hashes.SHA256(), length=length, salt=salt, info=info).derive(shared)


def pairing_code(shared, host_pub, client_pub, client_nonce, host_nonce):
    raw = _derive(shared, host_pub, client_pub, client_nonce, host_nonce, b"opentop code", 8)
    code = int.from_bytes(raw, "big") % 1_000_000
    return f"{code // 1000:03d} {code % 1000:03d}"


def session_key(shared, host_pub, client_pub, client_nonce, host_nonce):
    return _derive(shared, host_pub, client_pub, client_nonce, host_nonce, b"opentop key", 32)


def srtp_key(key):
    # 16 Byte AES-Schlüssel + 14 Byte Salt für aes-128-icm.
    return HKDF(algorithm=hashes.SHA256(), length=30, salt=None, info=b"opentop srtp").derive(key)


def srtp_caps(key):
    return ("application/x-srtp,media=video,encoding-name=H264,payload=96,clock-rate=90000,"
            f"srtp-key=(buffer){srtp_key(key).hex()},{SRTP_PARAMS}")
