#!/usr/bin/env bash
# Installiert opentop-host für den aktuellen Benutzer (KDE-Plasma-Laptop).
#   ./install-host.sh [Kabel-Interface]   (Standard: thunderbolt0)
set -euo pipefail
cd "$(dirname "$0")"
LINK_IFACE="${1:-thunderbolt0}"

sudo pacman -S --needed --noconfirm python-gobject python-dbus gst-plugins-base \
    gst-plugins-good gst-plugins-bad gst-plugin-pipewire gst-plugin-va gst-plugins-ugly libnotify \
    python-cryptography kdialog

install -Dm755 opentop-host "$HOME/.local/bin/opentop-host"
install -Dm644 opentop_proto.py "$HOME/.local/share/opentop/opentop_proto.py"
install -Dm644 data/opentop-host.service "$HOME/.config/systemd/user/opentop-host.service"
install -Dm644 data/opentop.desktop "$HOME/.local/share/applications/opentop.desktop"
systemctl --user daemon-reload
systemctl --user enable opentop-host.service
systemctl --user restart opentop-host.service

# Direkte Kabelverbindung: Link-Local-Adressen, kein DHCP nötig.
if ! nmcli -t -f NAME con show | grep -qx "opentop-$LINK_IFACE"; then
    sudo nmcli con add type ethernet ifname "$LINK_IFACE" con-name "opentop-$LINK_IFACE" \
        ipv4.method link-local ipv6.method link-local ipv4.never-default yes \
        connection.autoconnect yes >/dev/null
fi

if systemctl is-active -q firewalld; then
    sudo firewall-cmd --permanent --add-port=47800/udp && sudo firewall-cmd --reload
elif command -v ufw >/dev/null && sudo ufw status | grep -q active; then
    sudo ufw allow 47800/udp
fi

echo "opentop-host läuft und startet ab jetzt mit Plasma."
