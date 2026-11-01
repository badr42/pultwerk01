#!/usr/bin/env bash
# Pultwerk01: get an installed Pi ready to be copied as the master card for new units, then shut it down.
# It forgets every saved network, so the next start opens the setup hotspot. config.toml is kept, so each
# copy starts with this Pi's location and pins. Run on the Pi:  bash ~/desk-display/current/prepare-image.sh
set -euo pipefail
DEST="$HOME/desk-display"

[ -L "$DEST/current" ] || { echo "Pultwerk is not installed here: run install.sh first."; exit 1; }
command -v nmcli >/dev/null || { echo "NetworkManager is missing, and the setup hotspot needs it."; exit 1; }
[ -f /etc/polkit-1/rules.d/50-pultwerk-wifi.rules ] || { echo "Run install.sh again first: the Wi-Fi setup is not installed."; exit 1; }

echo "This will, on THIS Pi:"
echo "  - forget all saved networks (your connection drops at the end)"
echo "  - delete settings.json, photos, update history, logs and SSH keys"
echo "  - shut down"
read -r -p "Type RESET to continue: " answer
[ "$answer" = RESET ] || { echo "Nothing changed."; exit 1; }

sudo systemctl stop "desk-display@$USER"

rm -f "$DEST/settings.json" "$DEST/.bad-version" "$DEST/.update-pending" "$DEST/.settings-before-update.json"
find "$DEST/photos" -mindepth 1 -delete
rm -f "$HOME/.ssh/authorized_keys" "$HOME/.bash_history"

# each copy makes its own SSH host keys and machine id when it first starts
sudo mkdir -p /etc/systemd/system/ssh.service.d
printf '[Service]\nExecStartPre=/usr/bin/ssh-keygen -A\n' | sudo tee /etc/systemd/system/ssh.service.d/pultwerk-keys.conf >/dev/null
sudo rm -f /etc/ssh/ssh_host_*
sudo truncate -s 0 /etc/machine-id

sudo journalctl --rotate >/dev/null
sudo journalctl --vacuum-time=1s >/dev/null
sudo rfkill unblock wifi || true
sudo rm -f /etc/NetworkManager/system-connections/*.nmconnection

echo "Shutting down. When the green light stops blinking, take the card out and copy it."
sudo shutdown -h now
