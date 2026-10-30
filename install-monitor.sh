#!/usr/bin/env bash
# bash install-monitor.sh   (run on the Pi, from the app folder)
set -e
cp monitor.sh ~/desk-display/ && chmod +x ~/desk-display/monitor.sh
sudo cp desk-monitor.service /etc/systemd/system/desk-monitor@.service
# keep the system log across reboots (capped at 50 MB)
sudo mkdir -p /var/log/journal
sudo sed -i 's/^#\?Storage=.*/Storage=persistent/; s/^#\?SystemMaxUse=.*/SystemMaxUse=50M/' /etc/systemd/journald.conf
sudo systemctl restart systemd-journald
# hardware watchdog: reboot automatically if the Pi hangs for 15 s
sudo mkdir -p /etc/systemd/system.conf.d
printf '[Manager]\nRuntimeWatchdogSec=15\nRebootWatchdogSec=2min\n' | sudo tee /etc/systemd/system.conf.d/watchdog.conf >/dev/null
sudo apt-get install -y iw wtmpdb >/dev/null 2>&1 || true
sudo systemctl daemon-reload
sudo systemctl enable --now "desk-monitor@$USER"
echo "Monitoring on. Watch it:  tail -f ~/desk-monitor.log"
echo "Reboot once for the watchdog + persistent journal to take full effect."
