#!/usr/bin/env bash
# Health logger. Writes ~/desk-monitor.log every 30 s; detects unclean shutdowns.
LOG="$HOME/desk-monitor.log"
FLAG="$HOME/.desk-monitor-running"
if [ -f "$FLAG" ]; then
  echo "$(date '+%F %T') BOOT  *** previous run ended WITHOUT a clean shutdown (power loss / hang / watchdog) *** last seen: $(cat "$FLAG")" >> "$LOG"
else
  echo "$(date '+%F %T') BOOT  clean start" >> "$LOG"
fi
echo "$(date '+%F %T') BOOT  throttled=$(vcgencmd get_throttled) boot_took_until_network=$(awk '{print int($1)}' /proc/uptime)s" >> "$LOG"
trap 'rm -f "$FLAG"; echo "$(date "+%F %T") STOP  clean shutdown" >> "$LOG"; exit 0' TERM INT
while :; do
  now="$(date '+%F %T')"
  echo "$now" > "$FLAG"
  thr=$(vcgencmd get_throttled | cut -d= -f2)
  temp=$(vcgencmd measure_temp | cut -d= -f2)
  volt=$(vcgencmd measure_volts core | cut -d= -f2)
  load=$(cut -d' ' -f1 /proc/loadavg)
  mem=$(free -m | awk '/Mem/{print $7"MB_avail"}')
  disk=$(df -h / | awk 'NR==2{print $5}')
  wifi=$(iw dev wlan0 link 2>/dev/null | awk '/signal/{print $2" dBm"}')
  svc=$(systemctl is-active "desk-display@$USER")
  net=$(ping -c1 -W2 1.1.1.1 >/dev/null 2>&1 && echo up || echo DOWN)
  flag=""; [ "$thr" != "0x0" ] && flag=" <<< POWER/THERMAL EVENT"
  echo "$now OK    thr=$thr temp=$temp core=$volt load=$load $mem disk=$disk wifi=${wifi:-n/a} net=$net app=$svc$flag" >> "$LOG"
  sleep 30 & wait $!
done
