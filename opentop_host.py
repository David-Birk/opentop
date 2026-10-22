"""Plattformunabhängiger Kern von opentop-host.

Kopplung, Sitzungen pro Client, Pipeline-Gerüst, Pacing und Kommandozeile.
Alles, was vom Betriebssystem abhängt (virtuellen Monitor anlegen, Bild
abgreifen, Encoder wählen, Dialoge, Benachrichtigungen), liefert ein Backend:
opentop_kde (KDE Plasma unter Wayland) oder opentop_windows.

Ein Backend stellt bereit:

    name                                Anzeigename für das Log
    setup()                             einmalig vor dem Mainloop
    open_screen(size, args, on_ready, on_error) -> Screen
        legt einen virtuellen Monitor an (size = Client-Auflösung oder None)
        und ruft danach on_ready(source) auf. source ist eine GStreamer-
        Beschreibung, deren Quellelement name=src trägt.
    encoder(size, args) -> str          Konvertierung, Skalierung, H.264-Encoder
    confirm_pairing(name, code, callback) -> cancel
        fragt den Nutzer, ob der Code stimmt; callback(bool)
    notify(summary, body)

Screen.close() entfernt den virtuellen Monitor wieder.
"""
import argparse
import ipaddress
import os
import queue
import socket
import sys
import threading
import time

import gi

gi.require_version("Gst", "1.0")
gi.require_version("GstVideo", "1.0")
from gi.repository import GLib, Gst, GstVideo

import opentop_proto as proto

CLIENT_TIMEOUT = 6
REJECT_COOLDOWN = 300
PACING_BITRATE = 80_000_000
PACING_BURST = 16 * 1024
KEYFRAME_MIN_INTERVAL = 0.3
LINK_LOCAL = ipaddress.ip_network("169.254.0.0/16")
KEY_FILE = os.path.join(proto.CONFIG_DIR, "host.key")
CLIENTS_FILE = os.path.join(proto.CONFIG_DIR, "clients.json")


def log(*args):
    print("[opentop]", *args, flush=True)


def has_element(name):
    return Gst.ElementFactory.find(name) is not None


def is_direct_link(ip):
    # Kabelverbindungen (Thunderbolt/USB) laufen über IPv4-Link-Local.
    return ipaddress.ip_address(ip) in LINK_LOCAL


class Pacer:
    """Verschickt RTP-Pakete gleichmäßig verteilt statt als Burst pro Bild.

    Ein Keyframe sind schnell über 100 Pakete; mit 1 Gbit/s am Stück
    rausgeschickt laufen die Puffer billiger Switches vor Clients mit
    100-Mbit-Anschluss über, und es fehlen Teile des Bildes.
    """

    def __init__(self, ip):
        self.ip = ip
        self.port = proto.STREAM_PORT
        self.queue = queue.Queue()
        self.max_wait = 0.0      # längste Wartezeit in der Queue seit letzter Statistik
        self.max_pipeline = 0.0  # längste Zeit Capture -> appsink seit letzter Statistik
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, 4 * 1024 * 1024)
        threading.Thread(target=self.run, daemon=True).start()

    def on_sample(self, sink):
        buf = sink.emit("pull-sample").get_buffer()
        clock = sink.get_clock()
        if clock and buf.pts != Gst.CLOCK_TIME_NONE:
            # pipewiresrc stempelt beim Capture (do-timestamp); die Differenz
            # zur aktuellen Laufzeit ist die Latenz innerhalb der Pipeline.
            age = (clock.get_time() - sink.get_base_time() - buf.pts) / Gst.SECOND
            self.max_pipeline = max(self.max_pipeline, age)
        self.queue.put((time.perf_counter(), buf.extract_dup(0, buf.get_size())))
        return Gst.FlowReturn.OK

    def take_stats(self):
        stats = (self.queue.qsize(), self.max_wait, self.max_pipeline)
        self.max_wait = self.max_pipeline = 0.0
        return stats

    def run(self):
        rate = PACING_BITRATE / 8
        tokens, last = PACING_BURST, time.perf_counter()
        while (item := self.queue.get()) is not None:
            queued, data = item
            now = time.perf_counter()
            self.max_wait = max(self.max_wait, now - queued)
            tokens = min(PACING_BURST, tokens + (now - last) * rate)
            last = now
            if tokens < len(data):
                time.sleep((len(data) - tokens) / rate)
                tokens, last = len(data), time.perf_counter()
            tokens -= len(data)
            try:
                self.sock.sendto(data, (self.ip, self.port))
            except OSError:
                pass

    def close(self):
        self.queue.put(None)


def build_pipeline(backend, source, size, key, args):
    desc = (f"{source} ! videorate drop-only=true max-rate={args.fps} ! "
            f"{backend.encoder(size, args)} ! "
            "h264parse config-interval=-1 ! rtph264pay config-interval=-1 pt=96 mtu=1400 ! "
            f"srtpenc name=srtp {proto.SRTPENC_PARAMS} ! "
            "appsink name=out emit-signals=true sync=false async=false")
    log("Pipeline:", desc)
    pipeline = Gst.parse_launch(desc)
    pipeline.get_by_name("srtp").set_property("key", Gst.Buffer.new_wrapped(proto.srtp_key(key)))
    return pipeline


class Session:
    """Virtueller Monitor und Stream für genau einen gekoppelten Client."""

    def __init__(self, host, client_id, info):
        self.host = host
        self.backend = host.backend
        self.id = client_id
        self.name = info["name"]
        self.key = proto.unb64(info["key"])
        self.screen = None
        self.pipeline = None
        self.pacer = None
        self.ip = None
        self.size = None
        self.peers = {}
        self.retry_at = 0.0
        self.backoff = 0
        self.last_keyframe = 0.0
        self.last_seq = 0
        self.frames = 0

    def on_beacon(self, msg, ip):
        if ip not in self.peers:
            log(f"{self.name} erreichbar über {ip}")
        self.peers[ip] = time.monotonic()
        if msg.get("keyframe") and ip == self.ip:
            self.request_keyframe()
        size = tuple(msg["size"]) if msg.get("size") else None
        if size != self.size:
            self.size = size
            if self.pipeline:
                log(f"{self.name}: Auflösung jetzt {size}, starte Stream neu")
                self.stop(quiet=True)
        self.update_target()

    def check_timeout(self):
        now = time.monotonic()
        for ip, seen in list(self.peers.items()):
            if now - seen > CLIENT_TIMEOUT:
                log(f"{self.name}: {ip} nicht mehr erreichbar")
                del self.peers[ip]
        self.update_target()

    def update_target(self):
        # Kabel vor WLAN; bei Gleichstand beim aktuellen Weg bleiben.
        best = max(self.peers, key=lambda ip: (is_direct_link(ip), ip == self.ip), default=None)
        if best is None:
            if self.ip:
                log(f"{self.name} weg, entferne virtuellen Monitor")
                self.stop()
        elif self.ip is None:
            # Nur eine Session gleichzeitig starten, damit das Backend den
            # neuen virtuellen Monitor eindeutig dieser Session zuordnen kann.
            if time.monotonic() >= self.retry_at and self.host.starting is None:
                self.start(best)
        elif best != self.ip:
            self.retarget(best)

    def retarget(self, ip):
        log(f"{self.name}: wechsle Verbindung {self.ip} -> {ip}")
        self.ip = ip
        if self.pipeline:
            self.pacer.ip = ip
            self.request_keyframe()
            self.backend.notify(self.name, "über Kabel" if is_direct_link(ip) else "über Netzwerk")

    def request_keyframe(self):
        # Der Client meldet Paketverlust oder fehlendes Bild; ohne neuen
        # Keyframe bleiben Artefakte sonst bis zum nächsten GOP stehen.
        now = time.monotonic()
        if not self.pipeline or now - self.last_keyframe < KEYFRAME_MIN_INTERVAL:
            return
        self.last_keyframe = now
        self.pipeline.get_by_name("out").send_event(
            GstVideo.video_event_new_upstream_force_key_unit(Gst.CLOCK_TIME_NONE, True, 0))

    def count_frame(self, _pad, _info):
        self.frames += 1
        return Gst.PadProbeReturn.OK

    def log_stats(self, pipeline):
        if pipeline is not self.pipeline:
            return False
        waiting, wait, latency = self.pacer.take_stats() if self.pacer else (0, 0.0, 0.0)
        log(f"{self.name}: Statistik 5s: {self.frames} Bilder erfasst, "
            f"Pipeline max {latency * 1000:.0f} ms, Pacer {waiting} Pakete wartend, "
            f"max {wait * 1000:.0f} ms gewartet")
        self.frames = 0
        return True

    def start(self, ip):
        self.ip = ip
        self.host.starting = self
        self.screen = self.backend.open_screen(self.size, self.host.args,
                                               self.on_ready, self.on_error)

    def on_ready(self, source):
        self.host.starting = None
        self.pipeline = build_pipeline(self.backend, source, self.size, self.key, self.host.args)
        self.pacer = Pacer(self.ip)
        self.pipeline.get_by_name("out").connect("new-sample", self.pacer.on_sample)
        src_pad = self.pipeline.get_by_name("src").get_static_pad("src")
        src_pad.add_probe(Gst.PadProbeType.BUFFER, self.count_frame)
        src_pad.connect("notify::caps", lambda pad, _: pad.get_current_caps() and
                        log("Quelle:", pad.get_current_caps().to_string()))
        GLib.timeout_add_seconds(5, self.log_stats, self.pipeline)
        self.pipeline.get_bus().add_signal_watch()
        self.pipeline.get_bus().connect("message::error", self.on_pipeline_error)
        self.pipeline.set_state(Gst.State.PLAYING)
        log(f"Streame virtuellen Monitor an {self.name} ({self.ip}:{proto.STREAM_PORT})")
        GLib.timeout_add_seconds(3, self.on_stable, self.pipeline)

    def on_stable(self, pipeline):
        # Erst melden, wenn der Stream ein paar Sekunden ohne Fehler läuft.
        if pipeline is self.pipeline:
            self.backoff = 0
            self.backend.notify(f"{self.name} verbunden",
                                "über Kabel" if is_direct_link(self.ip) else f"über Netzwerk ({self.ip})")
        return False

    def on_error(self, reason):
        self.backoff = min(self.backoff * 2 or 2, 60)
        log(f"{self.name}: Fehler: {reason} - neuer Versuch in {self.backoff}s")
        self.stop(quiet=True)
        self.retry_at = time.monotonic() + self.backoff

    def on_pipeline_error(self, _bus, msg):
        err, dbg = msg.parse_error()
        self.on_error(f"{err.message} ({dbg})")

    def stop(self, quiet=False):
        if self.host.starting is self:
            self.host.starting = None
        if self.pipeline:
            self.pipeline.set_state(Gst.State.NULL)
            self.pipeline = None
            self.pacer.close()
            if not quiet:
                self.backend.notify(f"{self.name} getrennt")
        if self.screen:
            self.screen.close()
            self.screen = None
        self.ip = None


class Pairing:
    """Laufende Kopplung mit einem noch unbekannten Client."""

    def __init__(self, msg, addr):
        self.id = msg["id"]
        self.name = str(msg.get("name", "?"))[:64]
        self.client_pub = msg["pub"]
        self.addr = addr
        self.host_nonce = proto.new_nonce()
        self.client_nonce = None
        self.code = None
        self.key = None
        self.cancel_dialog = None
        self.result = None
        self.created = time.monotonic()


class Host:
    def __init__(self, backend, args):
        self.backend = backend
        self.args = args
        self.identity = proto.Identity(KEY_FILE)
        self.name = socket.gethostname()
        self.clients = {}
        self.clients_mtime = None
        self.sessions = {}
        self.pairings = {}
        self.rejected = {}
        self.starting = None
        self.sock = None

    def run(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("0.0.0.0", proto.BEACON_PORT))
        proto.watch_socket(self.sock, self.on_message)
        GLib.timeout_add_seconds(1, self.check_timeout)
        log(f"Backend {self.backend.name}, warte auf opentop-clients (UDP {proto.BEACON_PORT}) ...")
        GLib.MainLoop().run()

    def load_clients(self):
        # Neu einlesen, falls "opentop-host --forget" die Datei geändert hat.
        try:
            mtime = os.stat(CLIENTS_FILE).st_mtime
        except OSError:
            mtime = None
        if mtime != self.clients_mtime:
            self.clients_mtime = mtime
            self.clients = proto.load_json(CLIENTS_FILE, {})
            for client_id in list(self.sessions):
                if client_id not in self.clients:
                    self.sessions.pop(client_id).stop()
        return self.clients

    def send(self, msg, addr):
        try:
            self.sock.sendto(proto.encode(msg), addr)
        except OSError as e:
            log(f"Senden an {addr[0]} fehlgeschlagen:", e)

    def on_message(self):
        data, addr = self.sock.recvfrom(2048)
        msg = proto.decode(data)
        if not msg or not isinstance(msg.get("id"), str):
            return True
        handler = {"beacon": self.on_beacon, "hello": self.on_hello,
                   "nonce": self.on_nonce}.get(msg.get("type"))
        if handler:
            try:
                handler(msg, addr)
            except (KeyError, TypeError, ValueError) as e:
                log(f"Ungültige Nachricht von {addr[0]}:", e)
        return True

    def on_beacon(self, msg, addr):
        if msg.get("host") != self.identity.pub:
            return  # gehört zu einem anderen Host
        info = self.load_clients().get(msg["id"])
        if info is None:
            self.send({"type": "unknown"}, addr)
            return
        if not proto.verify(msg, proto.unb64(info["key"])):
            return
        session = self.sessions.get(msg["id"])
        if session is None:
            session = self.sessions[msg["id"]] = Session(self, msg["id"], info)
        # Wiederholte (mitgeschnittene) Beacons verwerfen.
        if msg["seq"] <= session.last_seq:
            return
        session.last_seq = msg["seq"]
        session.on_beacon(msg, addr[0])

    def on_hello(self, msg, addr):
        info = self.load_clients().get(msg["id"])
        if info and info["pub"] == msg["pub"]:
            # Schon gekoppelt, der Client hat die Bestätigung aber verpasst.
            self.send(proto.sign({"type": "paired", "name": self.name},
                                 proto.unb64(info["key"])), addr)
            return
        if time.monotonic() < self.rejected.get(msg["id"], 0):
            return
        pairing = self.pairings.get(msg["id"])
        if pairing is None or pairing.client_pub != msg["pub"]:
            pairing = self.pairings[msg["id"]] = Pairing(msg, addr)
            log(f"Kopplungsanfrage von {pairing.name} ({addr[0]})")
        pairing.addr = addr
        self.send({"type": "commit", "pub": self.identity.pub,
                   "commit": proto.commitment(self.identity.pub, pairing.client_pub,
                                              pairing.host_nonce)}, addr)

    def on_nonce(self, msg, addr):
        pairing = self.pairings.get(msg["id"])
        if pairing is None:
            return
        pairing.addr = addr
        if pairing.client_nonce is None:
            pairing.client_nonce = msg["nonce"]
            shared = self.identity.shared(pairing.client_pub)
            args = (shared, self.identity.pub, pairing.client_pub,
                    pairing.client_nonce, pairing.host_nonce)
            pairing.code = proto.pairing_code(*args)
            pairing.key = proto.session_key(*args)
            pairing.cancel_dialog = self.backend.confirm_pairing(
                pairing.name, pairing.code, lambda ok: self.on_answer(pairing, ok))
        elif msg["nonce"] != pairing.client_nonce:
            return
        self.send({"type": "reveal", "nonce": pairing.host_nonce}, addr)
        if pairing.result == "paired":
            self.send(proto.sign({"type": "paired", "name": self.name}, pairing.key), addr)
        elif pairing.result == "rejected":
            self.send({"type": "rejected"}, addr)

    def on_answer(self, pairing, accepted):
        pairing.cancel_dialog = None
        if self.pairings.get(pairing.id) is not pairing:
            return  # inzwischen abgelaufen
        if accepted:
            pairing.result = "paired"
            clients = self.load_clients()
            clients[pairing.id] = {"name": pairing.name, "pub": pairing.client_pub,
                                   "key": proto.b64(pairing.key)}
            proto.save_json(CLIENTS_FILE, clients)
            log(f"{pairing.name} gekoppelt")
            self.send(proto.sign({"type": "paired", "name": self.name}, pairing.key), pairing.addr)
        else:
            pairing.result = "rejected"
            self.rejected[pairing.id] = time.monotonic() + REJECT_COOLDOWN
            log(f"Kopplung mit {pairing.name} abgelehnt")
            self.send({"type": "rejected"}, pairing.addr)

    def check_timeout(self):
        now = time.monotonic()
        for client_id, pairing in list(self.pairings.items()):
            if now - pairing.created > proto.PAIRING_TIMEOUT:
                if pairing.cancel_dialog:
                    pairing.cancel_dialog()
                del self.pairings[client_id]
        for session in list(self.sessions.values()):
            session.check_timeout()
        return True

    def stop(self):
        for session in self.sessions.values():
            session.stop(quiet=True)


def manage_clients(args):
    clients = proto.load_json(CLIENTS_FILE, {})
    if args.list:
        for client_id, info in clients.items():
            print(f"{info['name']}  ({client_id})")
        if not clients:
            print("Keine gekoppelten Clients.")
        return
    target = args.forget
    gone = [cid for cid, info in clients.items() if target in ("all", cid, info["name"])]
    for cid in gone:
        print(f"Entkoppelt: {clients.pop(cid)['name']}")
    if not gone:
        print(f"Kein gekoppelter Client „{target}“.")
    proto.save_json(CLIENTS_FILE, clients)


def main(backend):
    p = argparse.ArgumentParser(
        description="opentop-host: virtuelle Monitore für gekoppelte opentop-clients")
    p.add_argument("--bitrate", type=int, default=10000, help="kbit/s (Standard: 10000)")
    p.add_argument("--fps", type=int, default=60)
    p.add_argument("--software", action="store_true", help="x264 statt Hardware-Encoder verwenden")
    p.add_argument("--no-autoscale", action="store_true",
                   help="Skalierung des virtuellen Monitors nicht anpassen")
    p.add_argument("--list", action="store_true", help="gekoppelte Clients anzeigen")
    p.add_argument("--forget", metavar="NAME|ID|all", help="Kopplung mit einem Client aufheben")
    args = p.parse_args()
    if args.list or args.forget:
        return manage_clients(args)

    backend.setup()
    Gst.init(None)
    host = Host(backend, args)
    try:
        host.run()
    except KeyboardInterrupt:
        pass
    finally:
        host.stop()
