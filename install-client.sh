#!/usr/bin/env bash
# Installiert opentop-client auf dem Laptop, der als zweiter Monitor dient.
#   ./install-client.sh [--autologin] [Kabel-Interface]   (Standard: thunderbolt0)
set -euo pipefail
cd "$(dirname "$0")"
AUTOLOGIN=0
if [[ "${1:-}" == "--autologin" ]]; then AUTOLOGIN=1; shift; fi
LINK_IFACE="${1:-thunderbolt0}"

sudo pacman -S --needed --noconfirm cage python-gobject gst-plugins-base gst-plugins-good \
    gst-plugins-bad gst-plugin-va gst-libav gtk4 gst-plugin-gtk4

sudo install -Dm755 opentop-client /usr/local/bin/opentop-client
sudo install -Dm644 data/opentop-session.desktop /usr/share/wayland-sessions/opentop.desktop

# Unsichtbares Cursor-Theme, damit der eigene Mauszeiger nicht im Bild steht.
# Es heißt "default" und liegt in einem eigenen XCURSOR_PATH, weil auch das
# Videofenster selbst per libwayland-cursor das Default-Theme lädt.
THEME=/usr/share/opentop/icons/default
sudo rm -rf /usr/share/icons/opentop-invisible
sudo mkdir -p "$THEME/cursors"
printf '[Icon Theme]\nName=default\n' | sudo tee "$THEME/index.theme" >/dev/null
python3 -c 'import struct,sys; sys.stdout.buffer.write(b"Xcur"+struct.pack("<3I",16,0x10000,1)
    +struct.pack("<3I",0xfffd0002,1,28)+struct.pack("<9I",36,0xfffd0002,1,1,1,1,0,0,0)+b"\0"*4)' |
    sudo tee "$THEME/cursors/default" >/dev/null
for name in left_ptr arrow pointer text xterm hand2 watch grabbing crosshair; do
    sudo ln -sf default "$THEME/cursors/$name"
done

if ! nmcli -t -f NAME con show | grep -qx "opentop-$LINK_IFACE"; then
    sudo nmcli con add type ethernet ifname "$LINK_IFACE" con-name "opentop-$LINK_IFACE" \
        ipv4.method link-local ipv6.method link-local ipv4.never-default yes \
        connection.autoconnect yes >/dev/null
fi

if systemctl is-active -q firewalld; then
    sudo firewall-cmd --permanent --add-port=47801/udp && sudo firewall-cmd --reload
elif command -v ufw >/dev/null && sudo ufw status | grep -q active; then
    sudo ufw allow 47801/udp
fi

if (( AUTOLOGIN )); then
    if systemctl is-enabled -q sddm 2>/dev/null; then
        sudo mkdir -p /etc/sddm.conf.d
        printf '[Autologin]\nUser=%s\nSession=opentop\n' "$USER" |
            sudo tee /etc/sddm.conf.d/50-opentop.conf >/dev/null
    elif systemctl is-enabled -q lightdm 2>/dev/null; then
        sudo sed -i -e "s/^#\?autologin-user=.*/autologin-user=$USER/" \
            -e "s/^#\?autologin-session=.*/autologin-session=opentop/" /etc/lightdm/lightdm.conf
        sudo groupadd -f autologin && sudo gpasswd -a "$USER" autologin
    else
        echo "Unbekannter Display-Manager - Autologin bitte manuell auf Sitzung 'opentop' setzen."
    fi
fi

echo "Fertig. Im Login-Bildschirm die Sitzung 'opentop (Zweiter Monitor)' wählen."
