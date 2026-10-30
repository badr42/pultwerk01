#!/usr/bin/env bash
# Pultwerk01 installer. Run on the Pi from the folder that contains desk_display.py:  bash install.sh
# Safe to run again after an update: your settings.json, photos and a backup of config.toml are kept.
set -e
DEST="$HOME/desk-display"
SRC="$(cd "$(dirname "$0")" && pwd)"
mkdir -p "$DEST/photos"

echo "== files"
if [ "$SRC" != "$DEST" ]; then
  [ -f "$DEST/config.toml" ] && cp "$DEST/config.toml" "$DEST/config.toml.bak" && echo "kept your old config as config.toml.bak"
  cp -r "$SRC"/desk_display.py "$SRC"/settings.py "$SRC"/web.py "$SRC"/web_ui.html "$SRC"/config.toml "$SRC"/fonts "$DEST"/
fi

echo "== packages"
sudo apt-get update -qq
sudo apt-get install -y python3-gpiozero python3-lgpio python3-spidev python3-numpy python3-pil \
  python3-smbus2 python3-qrcode libraqm0 alsa-utils i2c-tools
[ -x "$HOME/.venvs/display/bin/python" ] || python3 -m venv --system-site-packages "$HOME/.venvs/display"
"$HOME/.venvs/display/bin/pip" install -q gc9b72 arabic-reshaper python-bidi

echo "== interfaces: SPI (screen), I2C (light sensor), I2S (amp)"
CFG=/boot/firmware/config.txt; [ -f "$CFG" ] || CFG=/boot/config.txt
REBOOT=0
for line in "dtparam=spi=on" "dtparam=i2c_arm=on" "dtparam=audio=off" "dtoverlay=max98357a"; do
  if ! grep -qxF "$line" "$CFG"; then
    # switch off a conflicting "audio=on" rather than adding a second line
    [ "$line" = "dtparam=audio=off" ] && sudo sed -i 's/^dtparam=audio=on$/dtparam=audio=off/' "$CFG"
    grep -qxF "$line" "$CFG" || echo "$line" | sudo tee -a "$CFG" >/dev/null
    echo "added $line"; REBOOT=1
  fi
done
sudo usermod -aG gpio,i2c,spi,audio "$USER" || true

echo "== service"
sudo cp "$SRC/desk-display.service" "/etc/systemd/system/desk-display@.service"
sudo systemctl daemon-reload
sudo systemctl enable "desk-display@$USER"

IP=$(hostname -I | awk '{print $1}')
echo
echo "Control page:  http://$IP:8080   (or http://$(hostname).local:8080)"
echo "Tip: sudo hostnamectl set-hostname pultwerk01   ->   http://pultwerk01.local:8080"
if [ "$REBOOT" = 1 ]; then
  echo "Interfaces changed: reboot now with  sudo reboot  (the display starts by itself afterwards)."
else
  sudo systemctl restart "desk-display@$USER"
  echo "Running. Logs: journalctl -u desk-display@$USER -f"
fi
