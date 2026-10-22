"""opentop-Backend für KDE Plasma unter Wayland.

Virtuelle Monitore über das ScreenCast-Portal, Bild per PipeWire, Encoding mit
VA-API, Skalierung und Anordnung per kscreen-doctor, Kopplungsdialog per kdialog.
"""
import json
import os
import subprocess

import dbus
from dbus.mainloop.glib import DBusGMainLoop
from gi.repository import GLib

import opentop_proto as proto
from opentop_host import has_element, log

PORTAL_BUS = "org.freedesktop.portal.Desktop"
PORTAL_PATH = "/org/freedesktop/portal/desktop"
SOURCE_VIRTUAL = 4
CURSOR_EMBEDDED = 2
PERSIST_UNTIL_REVOKED = 2
TOKEN_FILE = os.path.join(proto.CONFIG_DIR, "restore_token")
LAYOUT_FILE = os.path.join(proto.CONFIG_DIR, "layouts.json")
LAYOUT_SAVE_INTERVAL = 10  # s; Anordnung auch bei Absturz des Hosts nicht verlieren
VIRTUAL_WIDTH = 1920  # KDE legt virtuelle Monitore fest mit 1920x1080 an


def kscreen_outputs():
    try:
        out = subprocess.run(["kscreen-doctor", "-j"], capture_output=True, text=True).stdout
        return [o for o in json.loads(out)["outputs"] if o.get("connected")]
    except (OSError, ValueError, KeyError):
        return []


def is_virtual(output):
    return output["name"].startswith("Virtual")


def virtual_outputs():
    return {o["id"] for o in kscreen_outputs() if is_virtual(o)}


def auto_scale(client_width):
    # Logische Größe des virtuellen Monitors an das Client-Panel angleichen,
    # damit Schrift dort so groß ist wie auf einem nativen Display.
    return max(1.0, round(VIRTUAL_WIDTH / client_width * 20) / 20)


def logical_right(output):
    return output["pos"]["x"] + output["size"]["width"] / (output.get("scale") or 1)


class VirtualScreen:
    """ScreenCast-Portal-Sitzung mit einem virtuellen Monitor als Quelle."""

    def __init__(self, bus, client_id, size, args, on_ready, on_error):
        self.bus = bus
        self.client_id = client_id
        self.output_id = None
        portal = bus.get_object(PORTAL_BUS, PORTAL_PATH)
        self.screencast = dbus.Interface(portal, "org.freedesktop.portal.ScreenCast")
        self.sender = bus.get_unique_name()[1:].replace(".", "_")
        self.counter = 0
        self.session = None
        self.size = size
        self.autoscale = size and not args.no_autoscale
        self.outputs_before = virtual_outputs()
        self.on_ready = on_ready
        self.on_error = on_error
        self.open()

    def _call(self, method, *args, options, on_response):
        self.counter += 1
        token = f"opentop{os.getpid()}_{self.counter}"
        path = f"{PORTAL_PATH}/request/{self.sender}/{token}"

        def handler(code, results):
            match.remove()
            on_response(int(code), results)

        match = self.bus.add_signal_receiver(
            handler, "Response", "org.freedesktop.portal.Request", PORTAL_BUS, path)
        options["handle_token"] = token
        getattr(self.screencast, method)(*args, dbus.Dictionary(options, signature="sv"))

    def open(self):
        def created(code, results):
            if code != 0:
                return self.on_error(f"CreateSession abgelehnt ({code})")
            self.session = str(results["session_handle"])
            options = {
                "types": dbus.UInt32(SOURCE_VIRTUAL),
                "cursor_mode": dbus.UInt32(CURSOR_EMBEDDED),
                "persist_mode": dbus.UInt32(PERSIST_UNTIL_REVOKED),
                "multiple": dbus.Boolean(False),
            }
            token = self._load_token()
            if token:
                options["restore_token"] = token
            self._call("SelectSources", self.session, options=options, on_response=selected)

        def selected(code, _results):
            if code != 0:
                return self.on_error(f"SelectSources abgelehnt ({code})")
            self._call("Start", self.session, "", options={}, on_response=started)

        def started(code, results):
            if code != 0:
                return self.on_error(f"Start abgelehnt ({code})")
            if "restore_token" in results:
                self._save_token(str(results["restore_token"]))
            node_id = int(results["streams"][0][0])
            fd = self.screencast.OpenPipeWireRemote(
                self.session, dbus.Dictionary({}, signature="sv")).take()
            new = virtual_outputs() - self.outputs_before
            if len(new) == 1:
                self.output_id = new.pop()
                self.restore_layout()
                GLib.timeout_add_seconds(LAYOUT_SAVE_INTERVAL, self.save_layout)
            # path= nimmt die Node-ID (target-object erwartet die Serial). Ohne
            # dont-fallback weicht PipeWire sonst still auf die Webcam aus.
            self.on_ready(
                f"pipewiresrc name=src fd={fd} path={node_id} "
                "stream-properties=\"props,node.dont-fallback=true,node.dont-reconnect=true\" "
                "do-timestamp=true keepalive-time=500")

        self._call("CreateSession", options={"session_handle_token": f"opentop{os.getpid()}"},
                   on_response=created)

    # KWin erkennt den virtuellen Monitor bei jedem Verbinden als neues Gerät
    # und vergisst dessen Anordnung. Darum merkt sich opentop sie pro Client:
    # Position und Skalierung des virtuellen Monitors und die Positionen der
    # übrigen Monitore, die ja um ihn herum angeordnet wurden.

    def restore_layout(self):
        layout = proto.load_json(LAYOUT_FILE, {}).get(self.client_id)
        others = {o["name"]: o for o in kscreen_outputs() if not is_virtual(o) and o.get("enabled")}
        ops = []
        if layout and set(layout["outputs"]) == set(others):
            scale = layout["scale"]
            x, y = layout["virtual"]
            ops += [f"output.{name}.position.{px},{py}" for name, (px, py) in layout["outputs"].items()]
            log(f"Stelle Anordnung des virtuellen Monitors wieder her ({x},{y}, Skalierung {scale:g})")
        else:
            # Erstes Mal oder andere Monitore: rechts an den letzten anlegen.
            # Erst nach dem Skalieren, sonst entsteht eine Lücke.
            scale = auto_scale(self.size[0]) if self.autoscale else None
            x, y = round(max(map(logical_right, others.values()), default=0)), 0
            log(f"Virtueller Monitor rechts angelegt bei {x},0"
                + (f", Skalierung {scale:g}" if scale else ""))
        if scale:
            ops.append(f"output.{self.output_id}.scale.{scale:g}")
        ops.append(f"output.{self.output_id}.position.{x},{y}")
        subprocess.run(["kscreen-doctor", *ops], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def save_layout(self):
        if not self.session:
            return False
        outputs = kscreen_outputs()
        virtual = next((o for o in outputs if o["id"] == self.output_id), None)
        if virtual is None:
            return True
        layouts = proto.load_json(LAYOUT_FILE, {})
        layout = {"scale": virtual.get("scale") or 1,
                  "virtual": [virtual["pos"]["x"], virtual["pos"]["y"]],
                  "outputs": {o["name"]: [o["pos"]["x"], o["pos"]["y"]] for o in outputs
                              if not is_virtual(o) and o.get("enabled")}}
        if layouts.get(self.client_id) != layout:
            layouts[self.client_id] = layout
            proto.save_json(LAYOUT_FILE, layouts)
        return True

    def close(self):
        if not self.session:
            return
        self.save_layout()
        try:
            obj = self.bus.get_object(PORTAL_BUS, self.session)
            dbus.Interface(obj, "org.freedesktop.portal.Session").Close()
        except dbus.DBusException as e:
            log("Session.Close fehlgeschlagen:", e)
        self.session = None

    @staticmethod
    def _load_token():
        try:
            with open(TOKEN_FILE) as f:
                return f.read().strip() or None
        except OSError:
            return None

    @staticmethod
    def _save_token(token):
        os.makedirs(proto.CONFIG_DIR, exist_ok=True)
        with open(TOKEN_FILE, "w") as f:
            f.write(token)


class KdeBackend:
    name = "KDE Plasma"

    def setup(self):
        DBusGMainLoop(set_as_default=True)
        self.bus = dbus.SessionBus()

    def open_screen(self, client_id, size, args, on_ready, on_error):
        return VirtualScreen(self.bus, client_id, size, args, on_ready, on_error)

    def encoder(self, size, args):
        # Auf die Panel-Auflösung des Clients skalieren: spart Bandbreite und
        # nimmt schwachen Clients das Herunterskalieren ab.
        dims = f",width={size[0]},height={size[1]}" if size else ""
        if has_element("nvh264enc") and not args.software:
            # NVIDIA (proprietärer Treiber, kein VA-API-Encoding): NVENC.
            nvenc = (f"nvh264enc bitrate={args.bitrate} gop-size={args.fps} bframes=0 "
                     "preset=p1 tune=ultra-low-latency rc-mode=cbr zerolatency=true "
                     "rc-lookahead=0 repeat-sequence-header=true ! "
                     # Baseline enthält per Definition keine Umsortierung, sonst
                     # puffert der Decoder am Client bis zu 16 Bilder (Delay).
                     "video/x-h264,profile=constrained-baseline")
            if has_element("cudaconvertscale"):
                # Mit NVRTC (Paket cuda) auch Skalierung und Konvertierung auf der GPU.
                return (f"video/x-raw ! cudaupload ! cudaconvertscale ! "
                        f"video/x-raw(memory:CUDAMemory),format=NV12{dims} ! {nvenc}")
            # Sonst nur skalieren; die Farbkonvertierung übernimmt NVENC selbst.
            return (f"video/x-raw ! videoconvertscale n-threads=4 ! "
                    f"video/x-raw,format=BGRx{dims} ! {nvenc}")
        if has_element("vah264enc") and not args.software:
            # KWin liefert DMABufs, die vapostproc nicht direkt importieren kann.
            return (f"video/x-raw ! videoconvertscale n-threads=4 ! video/x-raw,format=NV12{dims} ! "
                    f"vah264enc bitrate={args.bitrate} key-int-max={args.fps} b-frames=0 "
                    "rate-control=vbr target-usage=7")
        return (f"videoconvertscale n-threads=4 ! video/x-raw,format=I420{dims} ! "
                f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={args.bitrate} "
                f"key-int-max={args.fps}")

    def confirm_pairing(self, name, code, callback):
        text = (f"„{name}“ möchte sich als zweiter Monitor mit diesem Rechner koppeln.\n\n"
                f"Wird auf dem Laptop dieser Code angezeigt?\n\n{code}")
        try:
            pid, *_ = GLib.spawn_async(
                ["kdialog", "--title", "opentop – Kopplung", "--yes-label", "Koppeln",
                 "--no-label", "Ablehnen", "--yesno", text],
                flags=GLib.SpawnFlags.SEARCH_PATH | GLib.SpawnFlags.DO_NOT_REAP_CHILD)
        except GLib.Error as e:
            log("Kopplungsdialog konnte nicht geöffnet werden:", e.message)
            return None
        GLib.child_watch_add(GLib.PRIORITY_DEFAULT, pid, lambda _pid, status: callback(status == 0))
        return lambda: os.kill(pid, 15)

    def notify(self, summary, body=""):
        try:
            obj = self.bus.get_object("org.freedesktop.Notifications", "/org/freedesktop/Notifications")
            dbus.Interface(obj, "org.freedesktop.Notifications").Notify(
                "opentop", 0, "video-display", summary, body, [], {}, 3000)
        except dbus.DBusException:
            pass
