#!/usr/bin/env bash
# Installiert opentop-client-kms auf Gentoo mit OpenRC: der schlankste Client.
#
# Kein Desktop, kein Display-Manager, kein Compositor, kein GTK, kein Mesa und
# kein Rust. Der Client läuft als Systemdienst und gibt das Bild per kmssink
# direkt über DRM/KMS aus.
#
#   ./install-client-gentoo.sh [--va-driver iHD|i965] [--link IFACE] [--user NAME]
#                              [--software] [--ac-poweroff] [--start]
#
#   --va-driver    VA-API-Treiber für Intel-GPUs (Standard: iHD).
#                  i965 genügt bis einschließlich Gen9 (Skylake, Apollo Lake,
#                  Kaby Lake, Coffee Lake) und baut in Minuten statt Stunden,
#                  weil iHD Code für alle Generationen bis Xe3 kompiliert.
#   --link IFACE   NetworkManager-Profil mit Link-Local-Adresse für ein
#                  direktes Kabel anlegen (z.B. enp2s0)
#   --user NAME    Benutzer für den Dienst (Standard: opentop)
#   --software     zusätzlich Software-Decoder installieren (ffmpeg, groß)
#   --ac-poweroff  herunterfahren, wenn das Netzteil abgesteckt wird (nach 30 s,
#                  abbrechbar durch Anstecken oder "opentop-power --cancel")
#   --start        Dienst sofort starten (übernimmt sofort den Bildschirm)
set -euo pipefail
cd "$(dirname "$0")"

LINK_IFACE="" SVC_USER="opentop" SOFTWARE=0 START=0 VA_DRIVER="iHD" AC_POWEROFF=0
while (( $# )); do
    case "$1" in
        --va-driver) VA_DRIVER="$2"; shift ;;
        --link) LINK_IFACE="$2"; shift ;;
        --user) SVC_USER="$2"; shift ;;
        --software) SOFTWARE=1 ;;
        --ac-poweroff) AC_POWEROFF=1 ;;
        --start) START=1 ;;
        *) echo "Unbekannte Option: $1" >&2; exit 2 ;;
    esac
    shift
done

case "${VA_DRIVER,,}" in
    ihd) VA_PKG=media-libs/libva-intel-media-driver ;;
    i965) VA_PKG=media-libs/libva-intel-driver ;;
    *) echo "--va-driver: iHD oder i965" >&2; exit 2 ;;
esac

[[ $EUID -eq 0 ]] || { echo "Bitte als root ausführen." >&2; exit 1; }
command -v emerge >/dev/null || { echo "Kein Portage gefunden - das ist kein Gentoo." >&2; exit 1; }
command -v openrc-run >/dev/null || [[ -x /sbin/openrc-run ]] || { echo "OpenRC fehlt." >&2; exit 1; }

# Schreibt Zeilen nach /etc/portage/<art>, egal ob dort eine Datei oder ein
# Verzeichnis liegt. Ein vorhandener opentop-Block wird ersetzt.
portage_conf() {
    local kind="$1" body="$2" target="/etc/portage/$1"
    if [[ -d $target || ! -e $target ]]; then
        mkdir -p "$target"
        printf '%s\n' "$body" > "$target/opentop"
    else
        sed -i '/^# >>> opentop$/,/^# <<< opentop$/d' "$target"
        printf '# >>> opentop\n%s\n# <<< opentop\n' "$body" >> "$target"
    fi
    echo "  /etc/portage/$kind aktualisiert"
}

echo "==> Portage konfigurieren"
# GStreamer ohne OpenGL/EGL, X11 und Wayland: das hält Mesa und LLVM fern.
# Pango bleibt an, es zeichnet den Wartebildschirm.
portage_conf package.use "\
media-libs/gstreamer introspection
media-libs/gst-plugins-base introspection pango -egl -gles2 -opengl -gbm -X -wayland -alsa -ogg -vorbis -theora
media-libs/gst-plugins-bad introspection vaapi -X -wayland -vulkan -vnc
media-libs/libva -X -wayland -glx
media-libs/libva-intel-media-driver -X
media-libs/libva-intel-driver -X -wayland -hybrid
x11-libs/libdrm video_cards_intel
dev-python/pygobject -cairo
x11-libs/cairo -X
x11-libs/pango -X"

# Gentoo baut kmssink nicht mit (es gibt dafür kein eigenes Paket). Die
# Meson-Option lässt sich pro Paket über MYMESONARGS nachreichen.
mkdir -p /etc/portage/env
echo 'MYMESONARGS="-Dkms=enabled"' > /etc/portage/env/opentop-gst-kms.conf
portage_conf package.env "media-libs/gst-plugins-bad opentop-gst-kms.conf"

PKGS=(
    media-libs/gstreamer
    media-libs/gst-plugins-base
    media-libs/gst-plugins-good
    media-libs/gst-plugins-bad
    media-plugins/gst-plugins-srtp
    "$VA_PKG"
    dev-python/pygobject
    dev-python/pynacl
    media-fonts/dejavu
    sys-apps/iproute2
)
(( SOFTWARE )) && PKGS+=(media-plugins/gst-plugins-libav)

# Ein Binärpaket vom Binhost wäre ohne kmssink gebaut, daher gst-plugins-bad
# immer selbst bauen. libdrm muss vorher da sein, sonst fehlt kmssink still.
EMERGE=(emerge --ask --verbose --usepkg-exclude media-libs/gst-plugins-bad)
echo "==> Pakete installieren"
"${EMERGE[@]}" --oneshot --noreplace x11-libs/libdrm
"${EMERGE[@]}" --update --newuse "${PKGS[@]}"

if ! gst-inspect-1.0 --exists kmssink 2>/dev/null; then
    echo "==> gst-plugins-bad ohne kmssink installiert - neu bauen"
    "${EMERGE[@]}" --oneshot media-libs/gst-plugins-bad
fi

echo "==> GStreamer-Elemente prüfen"
missing=()
for e in udpsrc srtpdec rtph264depay h264parse identity videotestsrc textoverlay kmssink; do
    gst-inspect-1.0 --exists "$e" 2>/dev/null || missing+=("$e")
done
if (( ${#missing[@]} )); then
    echo "Es fehlen: ${missing[*]}" >&2
    exit 1
fi
if gst-inspect-1.0 --exists vah264dec 2>/dev/null; then
    echo "  VA-API-Decoder: vah264dec"
elif gst-inspect-1.0 --exists avdec_h264 2>/dev/null; then
    echo "  Warnung: kein VA-API-Decoder, nur Software (avdec_h264) - auf schwachen CPUs ruckelt das." >&2
else
    echo "  Kein H.264-Decoder: VA-API-Treiber prüfen (vainfo) oder mit --software installieren." >&2
    exit 1
fi

echo "==> Programm installieren"
install -Dm755 opentop-client-kms /usr/local/bin/opentop-client-kms
install -Dm644 opentop_proto.py /usr/local/share/opentop/opentop_proto.py
install -Dm755 data/opentop-client.openrc /etc/init.d/opentop-client
if [[ -e /etc/conf.d/opentop-client ]]; then
    echo "  /etc/conf.d/opentop-client existiert, bleibt unverändert"
else
    install -Dm644 data/opentop-client.confd /etc/conf.d/opentop-client
fi
if [[ $SVC_USER != opentop ]]; then
    sed -i "s/^OPENTOP_USER=.*/OPENTOP_USER=\"$SVC_USER\"/" /etc/conf.d/opentop-client
fi

echo "==> Benutzer $SVC_USER"
for g in video input; do
    getent group "$g" >/dev/null || groupadd -r "$g"
done
if id "$SVC_USER" &>/dev/null; then
    usermod -a -G video,input "$SVC_USER"
else
    useradd -r -M -d /var/lib/opentop -s /sbin/nologin -G video,input "$SVC_USER"
fi

if [[ -n $LINK_IFACE ]]; then
    if command -v nmcli >/dev/null; then
        nmcli -t -f NAME con show | grep -qx "opentop-$LINK_IFACE" ||
            nmcli con add type ethernet ifname "$LINK_IFACE" con-name "opentop-$LINK_IFACE" \
                ipv4.method link-local ipv6.method link-local ipv4.never-default yes \
                connection.autoconnect yes >/dev/null
        echo "  Link-Local-Profil opentop-$LINK_IFACE angelegt"
    else
        echo "  nmcli fehlt - Link-Local für $LINK_IFACE bitte selbst einrichten." >&2
    fi
fi

if (( AC_POWEROFF )); then
    install -Dm755 data/opentop-power /usr/local/sbin/opentop-power
    install -Dm755 data/opentop-power.openrc /etc/init.d/opentop-power
    [[ -e /etc/conf.d/opentop-power ]] ||
        printf '# Sekunden zwischen Abstecken und Herunterfahren\nOPENTOP_POWER_DELAY=30\n' \
            > /etc/conf.d/opentop-power
    rc-update add opentop-power default >/dev/null
    rc-service opentop-power restart >/dev/null
    echo "  Herunterfahren beim Abstecken aktiv (/etc/conf.d/opentop-power)"
fi

rc-update add opentop-client default >/dev/null
echo
echo "Fertig. Der Dienst startet ab dem nächsten Boot automatisch."
if (( START )); then
    rc-service opentop-client restart
else
    echo "  Sofort starten:   rc-service opentop-client start"
fi
echo "  Beenden:          Strg+Alt+Rücktaste oder rc-service opentop-client stop"
echo "  Protokoll:        grep opentop-client /var/log/messages"
echo "  Optionen:         /etc/conf.d/opentop-client (z.B. --latency 30 für WLAN)"
echo "  Port:             UDP 47801 eingehend muss erreichbar sein"
