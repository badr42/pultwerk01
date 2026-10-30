#!/usr/bin/env bash
# Pultwerk01 updater, run hourly by desk-update@<you>.timer. Installs the newest commit on the stable
# branch as its own folder in ~/desk-display/releases, switches to it, and goes back to the previous
# folder if the app does not stay up. Log:  journalctl -u desk-update@$USER
set -euo pipefail

REPO="badr42/pultwerk01"
BRANCH="stable"
KEEP=5                                  # release folders to keep
SETTLE=60                               # seconds the app must stay up on a new version

DEST="$HOME/desk-display"
RELEASES="$DEST/releases"
INCOMING="$RELEASES/.incoming"
PENDING="$DEST/.update-pending"         # holds the previous release while a new one is on trial
BAD="$DEST/.bad-version"                # the version that was rolled back; not tried again
SETTINGS_BAK="$DEST/.settings-before-update.json"
VENV="$HOME/.venvs/display"
SERVICE="desk-display@$USER"

log() { echo "$*"; }

current() { basename "$(readlink "$DEST/current")"; }

switch_to() {
  touch "$RELEASES/$1"                  # newest first when pruning
  ln -sfn "releases/$1" "$DEST/current"
}

restart_app() { sudo -n systemctl restart "$SERVICE"; }

stays_up() {
  local waited=0 first="" pid
  while [ "$waited" -lt "$SETTLE" ]; do
    sleep 5
    waited=$((waited + 5))
    [ "$(systemctl is-active "$SERVICE" || true)" = active ] || return 1
    pid="$(systemctl show -p MainPID --value "$SERVICE")"
    first="${first:-$pid}"
    [ "$pid" = "$first" ] || return 1   # a new PID means it crashed and systemd restarted it
  done
}

prune() {
  local name count=0 prev="$1"
  for name in $(ls -1t "$RELEASES"); do
    count=$((count + 1))
    if [ "$count" -gt "$KEEP" ] && [ "$name" != "$(current)" ] && [ "$name" != "$prev" ]; then
      log "removing old version $name"
      rm -rf "${RELEASES:?}/$name"
    fi
  done
}

install_new_updater() {
  local new="$DEST/current/update.sh"
  [ -f "$new" ] || return 0
  cmp -s "$new" "$DEST/update.sh" && return 0
  bash -n "$new" || { log "new update.sh has a syntax error; keeping this one"; return 0; }
  cp "$new" "$DEST/update.sh.new"
  chmod 755 "$DEST/update.sh.new"
  mv "$DEST/update.sh.new" "$DEST/update.sh"
  log "updater replaced"
}

# Also runs at the start of the next pass if a power cut interrupted the trial.
finish_trial() {
  local prev new
  prev="$(cat "$PENDING")"
  new="$(current)"
  if stays_up; then
    rm -f "$PENDING" "$SETTINGS_BAK"
    log "now running $new"
    install_new_updater
    prune "$prev"
    return 0
  fi
  log "$new did not stay up; going back to $prev"
  echo "$new" > "$BAD"
  [ -f "$SETTINGS_BAK" ] && mv "$SETTINGS_BAK" "$DEST/settings.json"
  switch_to "$prev"
  restart_app || log "could not restart the app on $prev"
  rm -f "$PENDING"
  stays_up || log "WARNING: $prev is not staying up either"
  return 1
}

# Download a version and check it before anything running is touched. 0 = ready, 1 = retry next
# pass, 2 = the version itself is broken.
prepare() {
  local sha="$1" name="$2" f
  [ -d "$RELEASES/$name" ] && return 0  # already archived: the stable branch was moved back to it
  rm -rf "$INCOMING"
  mkdir -p "$INCOMING"
  curl -fsSL --max-time 300 "https://github.com/$REPO/archive/$sha.tar.gz" \
    | tar xz --strip-components=1 -C "$INCOMING" || { log "download failed"; return 1; }
  for f in desk_display.py settings.py web.py web_ui.html config.example.toml fonts; do
    [ -e "$INCOMING/$f" ] || { log "$name has no $f"; return 2; }
  done
  "$VENV/bin/python" -m py_compile "$INCOMING"/*.py || { log "$name does not compile"; return 2; }
  if [ -f "$INCOMING/requirements.txt" ] \
     && ! cmp -s "$INCOMING/requirements.txt" "$DEST/current/requirements.txt"; then
    "$VENV/bin/pip" install -q -r "$INCOMING/requirements.txt" || { log "pip install failed"; return 1; }
  fi
  for f in desk-display.service desk-update.service desk-update.timer; do
    cmp -s "$INCOMING/$f" "/etc/systemd/system/${f%%.*}@.${f##*.}" \
      || log "$f changed: run install.sh by hand to apply it"
  done
  mv "$INCOMING" "$RELEASES/$name"
}

[ -L "$DEST/current" ] || { log "no $DEST/current: run install.sh first"; exit 1; }
rm -rf "$INCOMING"

[ ! -f "$PENDING" ] || finish_trial

sha="$(curl -fsS --max-time 20 -H "Accept: application/vnd.github.sha" \
  "https://api.github.com/repos/$REPO/commits/$BRANCH" || true)"
if ! [[ "$sha" =~ ^[0-9a-f]{40}$ ]]; then
  log "could not read the $BRANCH branch; will try again next time"
  exit 0
fi
name="${sha:0:12}"
[ "$name" != "$(current)" ] || exit 0
if [ "$name" = "$(cat "$BAD" 2>/dev/null || true)" ]; then
  log "$name was rolled back earlier; waiting for a newer version"
  exit 0
fi

log "new version $name (running $(current))"
status=0
prepare "$sha" "$name" || status=$?
if [ "$status" -eq 2 ]; then
  echo "$name" > "$BAD"
  rm -rf "$INCOMING"
  exit 1
elif [ "$status" -ne 0 ]; then
  rm -rf "$INCOMING"
  exit 0
fi

prev="$(current)"
if [ -f "$DEST/settings.json" ]; then
  cp "$DEST/settings.json" "$SETTINGS_BAK"
fi
echo "$prev" > "$PENDING"
switch_to "$name"
if ! restart_app; then
  log "could not restart the app (check /etc/sudoers.d/desk-update); staying on $prev"
  switch_to "$prev"
  rm -f "$PENDING" "$SETTINGS_BAK"
  exit 1
fi
finish_trial
