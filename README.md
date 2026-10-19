# opentop

**Einen zweiten Linux-Laptop als zusätzlichen Monitor verwenden – wie Sidecar, nur für KDE Plasma.**

> 🇬🇧 *opentop turns spare Linux laptops into extra monitors for a KDE Plasma
> (Wayland) machine. The host creates a real virtual output per client through
> the xdg-desktop-portal, encodes it with VA-API H.264 and streams it encrypted
> (SRTP) over UDP. Clients pair once by comparing a 6-digit code, then boot
> straight into a kiosk session (`cage` + GTK4) that shows the stream
> fullscreen – no setup needed on the second screen. Documentation below is in
> German.*

```
 ┌──────────────── Host (KDE Plasma) ─────────────────┐          ┌──────── Client (beliebiges Arch) ────────┐
 │ KWin ─► virtueller Monitor (ScreenCast-Portal)      │          │ SDDM-Autologin ─► cage (Kiosk)           │
 │      ─► PipeWire ─► videoconvertscale ─► vah264enc  │  RTP/UDP │      ─► vah264dec ─► GTK4-Vollbild       │
 │      ─► Pacer (gleichmäßiges Senden) ───────────────┼─────────►│                                          │
 │                                     ◄───────────────┼──────────┤ Beacon: „bin da, 1366×768,              │
 │  legt Monitor an / entfernt ihn, sendet Keyframes   │  UDP     │          brauche Keyframe“               │
 └─────────────────────────────────────────────────────┘          └──────────────────────────────────────────┘
```

## Was es kann

- **Echter zweiter Bildschirm:** Plasma legt einen virtuellen Monitor an. Fenster
  lassen sich wie gewohnt hinüberschieben, die Anordnung stellst du in den
  Systemeinstellungen → Anzeige ein.
- **Mehrere Clients:** Jeder gekoppelte Laptop bekommt seinen eigenen
  virtuellen Monitor mit eigenem Stream.
- **Gesicherte Kopplung:** Beim ersten Kontakt zeigen Client und Host denselben
  6-stelligen Code; erst nach Bestätigung am Host wird gestreamt. Danach sind
  alle Nachrichten signiert und das Video ist verschlüsselt.
- **Automatisch:** Der Client meldet sich per UDP-Broadcast. Sobald er da ist,
  erscheint der Monitor; ist er 6 s weg, verschwindet der Monitor wieder.
- **Am Client nichts zu tun:** Eigene Login-Sitzung „opentop“, optional per
  Autologin. Der Laptop bootet direkt in die Vollbildanzeige, der eigene
  Mauszeiger ist ausgeblendet.
- **Wartebildschirm:** Solange kein Host verbunden ist, zeigt der Client
  Hostname und IP-Adressen statt eines schwarzen Bildes.
- **Zurück zum normalen Desktop:** <kbd>Strg</kbd>+<kbd>Alt</kbd>+<kbd>Rücktaste</kbd>
  beendet die opentop-Sitzung; SDDM zeigt die Anmeldung, dort z. B. Plasma
  oder LXQt wählen.
- **Flüssig:** 60 fps, Hardware-Encoding und -Decoding über VA-API, kein
  Jitterbuffer im LAN.
- **Robust:** Pakete werden gleichmäßig verteilt gesendet (Pacing), damit
  Switches vor 100-Mbit-Anschlüssen nicht überlaufen. Bei Paketverlust oder
  fehlendem Bild fordert der Client sofort einen neuen Keyframe an.
- **Passende Größe:** Der Client meldet seine Panel-Auflösung. Der Host
  skaliert den Stream darauf und stellt die Skalierung des virtuellen Monitors
  passend ein (z. B. 140 % für 1366×768), damit Schrift so groß ist wie nativ.
- **Kabel vor WLAN:** Gibt es eine direkte Verbindung (IPv4-Link-Local,
  `169.254.x.x`, z. B. Thunderbolt-Netzwerk oder LAN-Kabel direkt zwischen den
  Laptops), wird sie automatisch bevorzugt; Umschalten ohne Unterbrechung.

## Voraussetzungen

| | Host | Client |
|---|---|---|
| System | Arch Linux, **KDE Plasma 6 unter Wayland** | Arch Linux, beliebige Oberfläche |
| Grafik | VA-API-fähige GPU (sonst Software-Fallback x264) | VA-API-fähige GPU empfohlen (sonst Software-Decoding) |
| Netz | gleiches LAN wie der Client | gleiches LAN wie der Host |

Getestet: Plasma 6.8 auf Intel Iris Xe (Host) → Intel HD 505 / Pentium N4200,
1366×768 (Client), beide per LAN, Client mit 100-Mbit-Anschluss.

## Installation

Repository auf beide Rechner holen:

```sh
git clone https://github.com/David-Birk/opentop.git
cd opentop
```

**Host** (der Plasma-Rechner):

```sh
./install-host.sh
```

Installiert die GStreamer-Pakete, `opentop-host` nach `~/.local/bin`, einen
systemd-User-Dienst, der mit Plasma startet, und den Starter **opentop** im
Menü (schaltet den Dienst an/aus).

**Client** (der Laptop, der Monitor sein soll):

```sh
./install-client.sh --autologin
```

Installiert `cage`, GTK4, GStreamer und `opentop-client`, die Sitzung
„opentop (Zweiter Monitor)“ und ein unsichtbares Cursor-Theme für diese Sitzung.
Mit `--autologin` richtet das Skript SDDM bzw. LightDM so ein, dass der Laptop
direkt in diese Sitzung startet. Ohne die Option wählst du sie im Login-Bildschirm.

> **VA-API-Treiber:** Das Skript installiert ihn nicht automatisch, weil er von
> der GPU abhängt. Intel ab Broadwell: `intel-media-driver`, ältere Intel:
> `libva-intel-driver`, AMD: in `mesa` enthalten. Ohne Treiber dekodiert der
> Client in Software – auf schwachen CPUs ruckelt das deutlich.

Beide Skripte öffnen bei aktivem `firewalld` bzw. `ufw` die nötigen Ports.

## Benutzung

### Erstes Koppeln

1. Client einschalten. Der Wartebildschirm zeigt „Nicht gekoppelt“.
2. Sobald der Host ihn sieht, erscheint am Client ein **6-stelliger Code** und
   am Host ein Dialog „opentop – Kopplung“ mit einem Code.
3. Stimmen beide Codes überein: **Koppeln** klicken. Sonst *Ablehnen* – dann
   versucht gerade jemand, sich dazwischenzuschalten.

Die Kopplung bleibt gespeichert. Ab dann einfach beide Rechner einschalten: Der
Monitor erscheint nach ein paar Sekunden, Plasma meldet „… verbunden“.

Gekoppelte Clients verwalten:

```sh
opentop-host --list                 # gekoppelte Clients anzeigen
opentop-host --forget arch-ideapad  # Kopplung aufheben (Name, ID oder "all")
```

Am Client hebt `rm ~/.config/opentop/client.json` die Kopplung auf.

### Alltag

- **Monitor anordnen:** Systemeinstellungen → Anzeige → Monitor „Virtual…“.
  Plasma merkt sich die Position.
- **Host aus/an:** Starter „opentop“ im Menü oder
  `systemctl --user stop|start opentop-host`.
- **Am Client zurück zum normalen Desktop:** <kbd>Strg</kbd>+<kbd>Alt</kbd>+<kbd>Rücktaste</kbd>.
  Die Sitzung endet, SDDM zeigt die Anmeldung, dort eine andere Sitzung wählen.
  Nach dem nächsten Neustart startet per Autologin wieder opentop. Autologin
  ganz abschalten: `/etc/sddm.conf.d/50-opentop.conf` löschen.

### Direkte Kabelverbindung

opentop läuft über IP. Jede Verbindung, die Linux als Netzwerkkarte erkennt,
funktioniert:

- **Thunderbolt/USB4 auf beiden Seiten:** passendes USB-C-Kabel, Interface
  `thunderbolt0`.
- **LAN-Kabel direkt** zwischen den Laptops oder zwei USB-LAN-Adapter.

Die Installationsskripte legen für `thunderbolt0` ein NetworkManager-Profil mit
Link-Local-Adressen an. Für andere Interfaces den Namen übergeben (`ip link`
zeigt ihn): `./install-host.sh enp0s13f0u1u4`.

> Ein normales USB-Kabel zwischen zwei Laptops funktioniert **nicht** – beide
> Seiten sind USB-Hosts.

## Optionen

```
opentop-host   [--bitrate KBIT] [--fps N] [--software] [--no-autoscale]
               [--list] [--forget NAME|ID|all]
opentop-client [--host IP] [--latency MS] [--software]
```

| Option | Bedeutung |
|---|---|
| `--host IP` (Client) | Host direkt anschreiben statt Broadcast, z. B. über Tailscale/VPN |
| `--bitrate` | Ziel-Bitrate in kbit/s, Standard 10000 |
| `--latency MS` | Jitterbuffer am Client, Standard 0 (aus). Für WLAN z. B. `30` |
| `--no-autoscale` | Skalierung des virtuellen Monitors nicht anfassen |
| `--software` | x264 bzw. avdec statt VA-API |

Optionen für den Host trägst du per `systemctl --user edit opentop-host` ein,
für den Client in der `Exec=`-Zeile von
`/usr/share/wayland-sessions/opentop.desktop`.

## Sicherheit

- **Kopplung:** X25519-Schlüsselaustausch, abgesichert durch einen
  6-stelligen Vergleichscode (Prinzip wie Bluetooth „Numeric Comparison“: der
  Host legt sich per Hash auf seine Zufallszahl fest, bevor er die des Clients
  kennt). Ein Angreifer in der Mitte erwischt denselben Code nur mit
  Wahrscheinlichkeit 1 : 1 000 000 – und auch dann musst du noch am Host
  bestätigen.
- **Nachrichten:** Alle Beacons gekoppelter Clients sind per HMAC-SHA256
  signiert und mit einer fortlaufenden Nummer gegen Wiederholung geschützt.
  Fremde oder gefälschte Beacons ignoriert der Host.
- **Video:** SRTP (AES-128, HMAC-SHA1-80) mit einem Schlüssel pro Client.
- **Was sichtbar bleibt:** Der Client meldet sich per Broadcast mit Hostname,
  Auflösung und Client-ID – andere Geräte im Netz sehen also, *dass* dort ein
  opentop-Client läuft, aber nicht, was er anzeigt.
- **Schlüssel:** `~/.config/opentop/` (`host.key`, `clients.json` bzw.
  `client.key`, `client.json`), nur für den eigenen Benutzer lesbar.

Ports: UDP **47800** (Anmeldung/Kopplung, am Host offen), UDP **47801** (Video,
am Client offen).

## Fehlersuche

Beide Seiten schreiben alle 5 Sekunden eine Statistik ins Journal:

```sh
journalctl --user -u opentop-host -f   # Host: Bilder von KWin, Caps, Fehler
journalctl -t opentop-client -f        # Client: Pakete, verloren, angezeigt
```

| Symptom | Ursache / Lösung |
|---|---|
| Pixelblöcke, rosa/grüne Farben, Abdrücke | Paketverlust → `verloren` in der Client-Statistik prüfen. Bitrate senken, Kabel statt WLAN, bei WLAN `--latency 30` |
| Ruckelt, Client-CPU hoch | kein VA-API-Treiber am Client, siehe Installation |
| Kein Monitor erscheint | Host-Log prüfen; Firewall (UDP 47800); am Client `--host IP` probieren |
| Kein Kopplungsdialog | `kdialog` installiert? Nach *Ablehnen* wartet der Host 5 min, der Client 1 min |
| Schrift zu klein/groß | Skalierung in den Anzeige-Einstellungen anpassen und `--no-autoscale` setzen |
| Weniger als 60 fps in der Statistik | normal: KWin liefert nur bei Bildänderungen neue Frames |

## Grenzen

- Nur KDE Plasma als Host. Andere Compositors bieten über das Portal
  (noch) keine virtuellen Monitore an.
- KDE legt virtuelle Monitore fest mit 1920×1080 an. Andere Seitenverhältnisse
  als 16:9 bekommen schwarze Ränder.
- Tastatur und Touchpad des Clients steuern nichts, er zeigt nur an.
- Kein Ton.

## Lizenz

MIT, siehe [LICENSE](LICENSE).
