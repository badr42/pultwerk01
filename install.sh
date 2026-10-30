#!/usr/bin/env bash
# Pultwerk01 installer. Run on the Pi from the unpacked source folder:  bash install.sh
# Safe to run again: config.toml, settings.json and photos in ~/desk-display are never replaced.
# After this, desk-update@<you>.timer keeps the code up to date (see update.sh).
set -e
DEST="$HOME/desk-display"
SRC="$(cd "$(dirname "$0")" && pwd)"
STAMP="$(date +%Y%m%d-%H%M%S)"
case "$SRC" in
  "$DEST"|"$DEST"/*) echo "Run this from the unpacked source folder, not from $DEST."; exit 1 ;;
esac
mkdir -p "$DEST/photos" "$DEST/releases"

echo "== files"
if [ -f "$DEST/desk_display.py" ]; then           # the old layout, with the code directly in ~/desk-display
  OLD="$DEST/releases/pre-update-$STAMP"
  mkdir -p "$OLD"
  for f in desk_display.py settings.py web.py web_ui.html fonts __pycache__; do
    if [ -e "$DEST/$f" ]; then mv "$DEST/$f" "$OLD/"; fi
  done
  if [ -f "$DEST/config.toml" ]; then cp "$DEST/config.toml" "$OLD/"; fi
  echo "archived the previous version in $OLD"
fi
mkdir -p "$DEST/releases/install-$STAMP"
cp -R "$SRC/." "$DEST/releases/install-$STAMP/"
ln -sfn "releases/install-$STAMP" "$DEST/current"
if [ -f "$DEST/config.toml" ]; then
  echo "keeping your config.toml"
else
  cp "$SRC/config.example.toml" "$DEST/config.toml"
  echo "created config.toml: put your own coordinates in it"
fi
cp "$SRC/update.sh" "$DEST/update.sh"
chmod 755 "$DEST/update.sh"

echo "== packages"
sudo apt-get update -qq
sudo apt-get install -y python3-gpiozero python3-lgpio python3-spidev python3-numpy python3-pil \
  python3-smbus2 python3-qrcode libraqm0 alsa-utils i2c-tools curl
[ -x "$HOME/.venvs/display/bin/python" ] || python3 -m venv --system-site-packages "$HOME/.venvs/display"
"$HOME/.venvs/display/bin/pip" install -q -r "$SRC/requirements.txt"

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
sudo cp "$SRC/desk-update.service" "/etc/systemd/system/desk-update@.service"
sudo cp "$SRC/desk-update.timer" "/etc/systemd/system/desk-update@.timer"
# the updater runs as you and only needs to be allowed this one root command
RULE="$(mktemp)"
echo "$USER ALL=(root) NOPASSWD: $(command -v systemctl) restart desk-display@$USER" > "$RULE"
sudo visudo -cqf "$RULE"
sudo install -m 440 -o root -g root "$RULE" /etc/sudoers.d/desk-update
rm -f "$RULE"
sudo systemctl daemon-reload
sudo systemctl enable "desk-display@$USER" "desk-update@$USER.timer"

IP=$(hostname -I | awk '{print $1}')
echo
echo "Control page:  http://$IP:8080   (or http://$(hostname).local:8080)"
echo "Tip: sudo hostnamectl set-hostname pultwerk01   ->   http://pultwerk01.local:8080"
if [ "$REBOOT" = 1 ]; then
  echo "Interfaces changed: reboot now with  sudo reboot  (the display starts by itself afterwards)."
else
  sudo systemctl restart "desk-display@$USER"
  # started only now: with interfaces pending a reboot, a trial update would fail and get rolled back
  sudo systemctl restart "desk-update@$USER.timer"
  echo "Running. Logs: journalctl -u desk-display@$USER -f"
  echo "Update checks start in about 5 minutes. Logs: journalctl -u desk-update@$USER"
fi
