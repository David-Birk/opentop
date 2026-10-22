"""opentop-Backend für Windows 10/11.

Windows kann von sich aus keine virtuellen Monitore anlegen. Dafür wird der
quelloffene Virtual Display Driver (VDD) verwendet:
https://github.com/VirtualDrivers/Virtual-Display-Driver

Ablauf pro Client:
1. Anzahl der VDD-Monitore um eins erhöhen und den Treiber neu laden.
2. Den neu erschienenen Monitor finden und auf die Client-Auflösung stellen
   (anders als unter KDE ist hier jede Auflösung möglich, Skalierung entfällt).
3. Bild per d3d11screencapturesrc abgreifen, auf der GPU nach NV12 wandeln und
   mit dem passenden Hardware-Encoder kodieren.

STATUS: Gerüst, bisher nicht auf Windows getestet. Stellen, deren Verhalten
in einer Windows-VM geprüft werden muss, sind mit TODO(VM) markiert.
"""
import ctypes
import os
import re
import threading
import time
from ctypes import wintypes

from gi.repository import GLib

from opentop_host import has_element, log

VDD_SETTINGS = os.environ.get("OPENTOP_VDD_SETTINGS", r"C:\VirtualDisplayDriver\vdd_settings.xml")
VDD_PIPE = r"\\.\pipe\MTTVirtualDisplayPipe"
VDD_ADAPTER = "Virtual Display"  # Teil des Adapternamens in EnumDisplayDevices
MONITOR_APPEAR_TIMEOUT = 15

user32 = ctypes.WinDLL("user32", use_last_error=True) if os.name == "nt" else None

DISPLAY_DEVICE_ATTACHED_TO_DESKTOP = 0x1
ENUM_CURRENT_SETTINGS = -1
DM_PELSWIDTH = 0x80000
DM_PELSHEIGHT = 0x100000
DM_DISPLAYFREQUENCY = 0x400000
CDS_UPDATEREGISTRY = 0x1
DISP_CHANGE_SUCCESSFUL = 0
MB_YESNO = 0x4
MB_ICONQUESTION = 0x20
MB_TOPMOST = 0x40000
MB_SETFOREGROUND = 0x10000
IDYES = 6


class DISPLAY_DEVICEW(ctypes.Structure):
    _fields_ = [("cb", wintypes.DWORD), ("DeviceName", wintypes.WCHAR * 32),
                ("DeviceString", wintypes.WCHAR * 128), ("StateFlags", wintypes.DWORD),
                ("DeviceID", wintypes.WCHAR * 128), ("DeviceKey", wintypes.WCHAR * 128)]


class DEVMODEW(ctypes.Structure):
    # Gekürzt auf die Display-Variante der Union; Größe stimmt mit der WinAPI überein.
    _fields_ = [("dmDeviceName", wintypes.WCHAR * 32), ("dmSpecVersion", wintypes.WORD),
                ("dmDriverVersion", wintypes.WORD), ("dmSize", wintypes.WORD),
                ("dmDriverExtra", wintypes.WORD), ("dmFields", wintypes.DWORD),
                ("dmPositionX", wintypes.LONG), ("dmPositionY", wintypes.LONG),
                ("dmDisplayOrientation", wintypes.DWORD), ("dmDisplayFixedOutput", wintypes.DWORD),
                ("dmColor", ctypes.c_short), ("dmDuplex", ctypes.c_short),
                ("dmYResolution", ctypes.c_short), ("dmTTOption", ctypes.c_short),
                ("dmCollate", ctypes.c_short), ("dmFormName", wintypes.WCHAR * 32),
                ("dmLogPixels", wintypes.WORD), ("dmBitsPerPel", wintypes.DWORD),
                ("dmPelsWidth", wintypes.DWORD), ("dmPelsHeight", wintypes.DWORD),
                ("dmDisplayFlags", wintypes.DWORD), ("dmDisplayFrequency", wintypes.DWORD),
                ("dmICMMethod", wintypes.DWORD), ("dmICMIntent", wintypes.DWORD),
                ("dmMediaType", wintypes.DWORD), ("dmDitherType", wintypes.DWORD),
                ("dmReserved1", wintypes.DWORD), ("dmReserved2", wintypes.DWORD),
                ("dmPanningWidth", wintypes.DWORD), ("dmPanningHeight", wintypes.DWORD)]


class MONITORINFOEXW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD),
                ("szDevice", wintypes.WCHAR * 32)]


def virtual_displays():
    """Gerätenamen (\\\\.\\DISPLAYn) aller aktiven VDD-Monitore."""
    names = set()
    device = DISPLAY_DEVICEW(cb=ctypes.sizeof(DISPLAY_DEVICEW))
    i = 0
    while user32.EnumDisplayDevicesW(None, i, ctypes.byref(device), 0):
        if VDD_ADAPTER in device.DeviceString and device.StateFlags & DISPLAY_DEVICE_ATTACHED_TO_DESKTOP:
            names.add(device.DeviceName)
        i += 1
    return names


def monitor_handle(device_name):
    """HMONITOR zu einem Gerätenamen, für d3d11screencapturesrc monitor-handle."""
    found = []
    proc_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HMONITOR, wintypes.HDC,
                                   ctypes.POINTER(wintypes.RECT), wintypes.LPARAM)

    def callback(hmonitor, _hdc, _rect, _data):
        info = MONITORINFOEXW(cbSize=ctypes.sizeof(MONITORINFOEXW))
        if user32.GetMonitorInfoW(hmonitor, ctypes.byref(info)) and info.szDevice == device_name:
            found.append(hmonitor)
        return True

    user32.EnumDisplayMonitors(None, None, proc_type(callback), 0)
    return found[0] if found else None


def set_resolution(device_name, width, height):
    mode = DEVMODEW(dmSize=ctypes.sizeof(DEVMODEW))
    if not user32.EnumDisplaySettingsW(device_name, ENUM_CURRENT_SETTINGS, ctypes.byref(mode)):
        return False
    mode.dmPelsWidth, mode.dmPelsHeight, mode.dmDisplayFrequency = width, height, 60
    mode.dmFields = DM_PELSWIDTH | DM_PELSHEIGHT | DM_DISPLAYFREQUENCY
    result = user32.ChangeDisplaySettingsExW(device_name, ctypes.byref(mode), None,
                                             CDS_UPDATEREGISTRY, None)
    return result == DISP_CHANGE_SUCCESSFUL


class VddController:
    """Steuert die Anzahl und Auflösungen der VDD-Monitore.

    TODO(VM): Format von vdd_settings.xml und die Befehle der Named Pipe gegen
    die installierte VDD-Version prüfen.
    """

    lock = threading.Lock()

    def __init__(self, path=VDD_SETTINGS):
        self.path = path

    def _edit(self, transform):
        with open(self.path, encoding="utf-8") as f:
            xml = f.read()
        new = transform(xml)
        if new != xml:
            with open(self.path, "w", encoding="utf-8") as f:
                f.write(new)
        return new != xml

    def ensure_resolution(self, width, height):
        entry = (f"<resolution><width>{width}</width><height>{height}</height>"
                 "<refresh_rate>60</refresh_rate></resolution>")

        def add(xml):
            if re.search(rf"<width>{width}</width>\s*<height>{height}</height>", xml):
                return xml
            return xml.replace("</resolutions>", f"    {entry}\n    </resolutions>", 1)
        return self._edit(add)

    def set_count(self, count):
        return self._edit(lambda xml: re.sub(r"<count>\d+</count>", f"<count>{count}</count>", xml, 1))

    def reload(self):
        # TODO(VM): Prüfen, ob RELOAD_DRIVER der richtige Befehl ist und ob
        # eine Antwort gelesen werden muss.
        with open(VDD_PIPE, "r+b", buffering=0) as pipe:
            pipe.write("RELOAD_DRIVER".encode("utf-16-le"))


class VirtualScreen:
    """Ein VDD-Monitor für genau einen Client."""

    active = 0  # Anzahl laufender Bildschirme über alle Sessions

    def __init__(self, vdd, size, on_ready, on_error):
        self.vdd = vdd
        self.size = size
        self.device = None
        self.closed = False
        self.on_ready = on_ready
        self.on_error = on_error
        threading.Thread(target=self.open, daemon=True).start()

    def open(self):
        # Läuft im Thread, weil das Neuladen des Treibers Sekunden dauert.
        try:
            with VddController.lock:
                before = virtual_displays()
                if self.size:
                    self.vdd.ensure_resolution(*self.size)
                VirtualScreen.active += 1
                self.vdd.set_count(VirtualScreen.active)
                self.vdd.reload()
                new = self.wait_for_new(before)
            if new is None:
                raise RuntimeError("kein neuer virtueller Monitor erschienen")
            self.device = new
            if self.size and not set_resolution(new, *self.size):
                log(f"Auflösung {self.size} für {new} konnte nicht gesetzt werden")
            hmonitor = monitor_handle(new)
            if hmonitor is None:
                raise RuntimeError(f"kein HMONITOR für {new}")
        except Exception as e:  # noqa: BLE001 - jeder Fehler beendet nur diese Session
            GLib.idle_add(lambda: self.on_error(str(e)) and False)
            return
        if not self.closed:
            # TODO(VM): show-cursor und Bildrate prüfen; ggf. capture-api=wgc.
            GLib.idle_add(lambda: self.on_ready(
                f"d3d11screencapturesrc name=src monitor-handle={hmonitor} show-cursor=true") and False)

    @staticmethod
    def wait_for_new(before):
        deadline = time.monotonic() + MONITOR_APPEAR_TIMEOUT
        while time.monotonic() < deadline:
            new = virtual_displays() - before
            if new:
                return sorted(new)[0]
            time.sleep(0.25)
        return None

    def close(self):
        # TODO(VM): Der Treiber entfernt beim Verringern der Anzahl vermutlich
        # den zuletzt angelegten Monitor, nicht zwingend diesen. Bei mehreren
        # Clients prüfen und ggf. Monitore gezielt abschalten.
        self.closed = True
        if self.device is None:
            return
        with VddController.lock:
            VirtualScreen.active = max(0, VirtualScreen.active - 1)
            try:
                self.vdd.set_count(VirtualScreen.active)
                self.vdd.reload()
            except OSError as e:
                log("Virtuellen Monitor entfernen fehlgeschlagen:", e)


# Hardware-Encoder in Reihenfolge der Bevorzugung. Alle nehmen D3D11-Speicher
# direkt an, das Bild bleibt also von der Erfassung bis zum Encoder auf der GPU.
# TODO(VM): Property-Namen gegen die installierte GStreamer-Version prüfen.
ENCODERS = [
    ("qsvh264enc",     "qsvh264enc bitrate={bitrate} gop-size={gop} b-frames=0 rate-control=vbr"),
    ("nvd3d11h264enc", "nvd3d11h264enc bitrate={bitrate} gop-size={gop} bframes=0 "
                       "preset=p1 tune=ultra-low-latency"),
    ("amfh264enc",     "amfh264enc bitrate={bitrate} gop-size={gop} usage=ultra-low-latency"),
    ("mfh264enc",      "mfh264enc bitrate={bitrate} gop-size={gop} bframes=0 low-latency=true"),
]


class WindowsBackend:
    name = "Windows"

    def setup(self):
        if os.name != "nt":
            raise SystemExit("Das Windows-Backend läuft nur unter Windows.")
        if not os.path.exists(VDD_SETTINGS):
            raise SystemExit(f"Virtual Display Driver nicht gefunden ({VDD_SETTINGS}). "
                             "Siehe README, Abschnitt Windows.")
        self.vdd = VddController()

    def open_screen(self, _client_id, size, _args, on_ready, on_error):
        return VirtualScreen(self.vdd, size, on_ready, on_error)

    def encoder(self, size, args):
        dims = f",width={size[0]},height={size[1]}" if size else ""
        if not args.software:
            for element, template in ENCODERS:
                if has_element(element):
                    return (f"d3d11convert ! video/x-raw(memory:D3D11Memory),format=NV12{dims} ! "
                            + template.format(bitrate=args.bitrate, gop=args.fps))
            log("Kein Hardware-Encoder gefunden, verwende x264")
        return (f"d3d11download ! videoconvertscale ! video/x-raw,format=I420{dims} ! "
                f"x264enc tune=zerolatency speed-preset=ultrafast bitrate={args.bitrate} "
                f"key-int-max={args.fps}")

    def confirm_pairing(self, name, code, callback):
        text = (f"„{name}“ möchte sich als zweiter Monitor mit diesem Rechner koppeln.\n\n"
                f"Wird auf dem Laptop dieser Code angezeigt?\n\n{code}")

        def ask():
            # MessageBoxW blockiert, deshalb im Thread; die Antwort geht über
            # den GLib-Mainloop zurück. Abbrechen lässt sich die Box nicht, eine
            # verspätete Antwort ignoriert der Kern.
            answer = user32.MessageBoxW(None, text, "opentop – Kopplung",
                                        MB_YESNO | MB_ICONQUESTION | MB_TOPMOST | MB_SETFOREGROUND)
            GLib.idle_add(lambda: callback(answer == IDYES) and False)

        threading.Thread(target=ask, daemon=True).start()
        return None

    def notify(self, summary, body=""):
        # TODO: Windows-Toast-Benachrichtigung; vorerst nur ins Log.
        log(f"{summary} {body}".strip())
