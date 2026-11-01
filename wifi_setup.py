"""Pultwerk01 Wi-Fi setup: with no network to join, the Pi opens its own hotspot and the phone sets it up.

    no Wi-Fi -> hotspot -> screen shows a QR code to join it -> screen shows a QR code to /wifi
             -> pick a network on the phone -> hotspot down, try it -> joined, or hotspot back with the error

The Pi has one radio: it cannot scan or join while the hotspot is up, so the network list is read just
before the hotspot starts, and known networks are retried by taking the hotspot down for a moment.
Everything goes through nmcli (NetworkManager); install.sh allows this user to change networks.
Times are monotonic because the clock jumps when the Pi first reaches a time server.
"""
import secrets
import shutil
import subprocess
import threading
import time

HOTSPOT_IP = "10.42.0.1"
HOTSPOT_CON = "pultwerk-hotspot"
CLIENT_CON_PREFIX = "wifi-"
PASSWORD_LETTERS = "abcdefghjkmnpqrstuvwxyz23456789"       # nothing that reads like another on a small screen
WIFI_TYPE = "802-11-wireless"
PAGE_IDLE = 300                                            # seconds a visit to the page keeps the hotspot up


def split_terse(line):
    """Split one line of nmcli -t output: fields end at ':', and '\\:' is a colon inside a field."""
    fields, cur, i = [], "", 0
    while i < len(line):
        if line[i] == "\\" and i + 1 < len(line):
            cur += line[i + 1]
            i += 2
        elif line[i] == ":":
            fields.append(cur)
            cur = ""
            i += 1
        else:
            cur += line[i]
            i += 1
    return fields + [cur]


class WifiSetup:
    def __init__(self, cfg, arp="/proc/net/arp"):
        w = cfg.get("wifi", {})
        self.enabled = bool(w.get("setup_hotspot", True)) and shutil.which("nmcli") is not None
        self.hotspot_ssid = str(w.get("hotspot_name", "Pultwerk-Setup"))
        self.grace_new = float(w.get("wait_new_seconds", 5))       # no saved network: nothing to wait for
        self.grace_saved = float(w.get("wait_saved_seconds", 180))  # a saved network may just be restarting
        self.retry_every = float(w.get("retry_saved_seconds", 300))
        self.poll = float(w.get("poll_seconds", 3))
        self.poll_online = 15.0                                    # nmcli is not free on a Zero 2 W
        self.drop_delay = 2.0                                      # lets the page get its answer first
        self.arp = arp
        self.iface = "wlan0"
        self.has_client = False
        self.hotspot_password = "".join(secrets.choice(PASSWORD_LETTERS) for _ in range(8))
        self.lock = threading.Lock()
        self.wake = threading.Event()
        self.state = "idle"                                        # idle | hotspot | joining
        self.error = None
        self.networks = []
        self.joining_ssid = None
        self.pending = None
        self.force = False
        self.connected = False
        self.offline_since = None
        self.hotspot_t = 0.0
        self.page_t = None
        self.next_start = 0.0

    # -- nmcli
    def nm(self, *args, wait=None):
        cmd = ["nmcli"]
        if wait:
            cmd += ["--wait", str(wait)]
        try:
            r = subprocess.run(cmd + list(args), capture_output=True, text=True, timeout=(wait or 15) + 15)
        except (OSError, subprocess.TimeoutExpired) as e:
            print(f"[wifi] nmcli {args[0]}: {e!r}", flush=True)
            return False, ""
        return r.returncode == 0, r.stdout if r.returncode == 0 else r.stderr.strip()

    def terse(self, *args):
        ok, out = self.nm("-t", *args)
        return [split_terse(line) for line in out.splitlines() if line] if ok else None

    def devices(self):
        return self.terse("-f", "DEVICE,TYPE,STATE,CONNECTION", "device")

    def wifi_device(self):
        for d in self.devices() or []:
            if len(d) == 4 and d[1] == "wifi":
                return d[0]
        return None

    def online(self, rows=None):
        """True or False, or None when NetworkManager gives no answer (then the hotspot stays off)."""
        rows = self.devices() if rows is None else rows
        if rows is None:
            return None
        return any(len(d) == 4 and d[1] in ("wifi", "ethernet") and d[2].startswith("connected") and d[3] != HOTSPOT_CON
                   for d in rows)

    def hotspot_running(self, rows):
        return any(len(d) == 4 and d[3] == HOTSPOT_CON and d[2].startswith("connected") for d in rows or [])

    def saved_networks(self):
        rows = self.terse("-f", "NAME,TYPE", "connection", "show") or []
        return [r[0] for r in rows if len(r) == 2 and r[1] == WIFI_TYPE and r[0] != HOTSPOT_CON]

    def scan(self):
        dev = self.iface = self.wifi_device() or self.iface
        rows = None
        for rescan in ("yes", "auto", "auto"):                     # the radio may have only just come free
            rows = self.terse("-f", "SSID,SIGNAL,SECURITY", "device", "wifi", "list", "ifname", dev, "--rescan", rescan)
            if rows:
                break
            time.sleep(self.drop_delay)
        if rows is None:
            return
        best = {}
        for r in rows:
            if len(r) != 3 or not r[0] or r[0] == self.hotspot_ssid or "802.1X" in r[2]:   # no school/office logins
                continue
            signal = int(r[1]) if r[1].isdigit() else 0
            if r[0] not in best or signal > best[r[0]]["signal"]:
                best[r[0]] = {"ssid": r[0], "signal": signal, "security": r[2]}
        with self.lock:
            self.networks = sorted(best.values(), key=lambda n: -n["signal"])

    # -- hotspot
    def start_hotspot(self):
        self.scan()
        self.nm("radio", "wifi", "on")
        self.nm("connection", "delete", "id", HOTSPOT_CON)
        ok, msg = self.nm("connection", "add", "type", "wifi", "ifname", self.iface,
                          "con-name", HOTSPOT_CON, "autoconnect", "no", "ssid", self.hotspot_ssid,
                          "mode", "ap", "wifi.band", "bg", "wifi.channel", "6",
                          "ipv4.method", "shared", "ipv4.addresses", f"{HOTSPOT_IP}/24", "ipv6.method", "disabled",
                          "wifi-sec.key-mgmt", "wpa-psk", "wifi-sec.proto", "rsn", "wifi-sec.pairwise", "ccmp",
                          "wifi-sec.group", "ccmp", "wifi-sec.pmf", "disable", "wifi-sec.psk", self.hotspot_password)
        if ok:
            ok, msg = self.nm("connection", "up", "id", HOTSPOT_CON, wait=30)
        if ok:
            with self.lock:
                self.state = "hotspot"
            self.hotspot_t = time.monotonic()
            self.page_t, self.has_client = None, False
            print(f"[wifi] hotspot {self.hotspot_ssid} is up", flush=True)
        else:
            with self.lock:
                self.state = "idle"
            self.next_start = time.monotonic() + 60
            print(f"[wifi] could not start the hotspot: {msg}", flush=True)
        return ok

    def stop_hotspot(self):
        self.nm("connection", "down", "id", HOTSPOT_CON)
        self.nm("connection", "delete", "id", HOTSPOT_CON)

    def clients(self):
        """Phones on the hotspot: anything it has exchanged packets with shows in the ARP table."""
        try:
            lines = open(self.arp).read().splitlines()[1:]
        except OSError:
            return 0
        return sum(1 for ln in lines if len(p := ln.split()) >= 6 and p[5] == self.iface and p[2] != "0x0")

    def in_use(self):
        recent = self.page_t is not None and time.monotonic() - self.page_t < PAGE_IDLE
        return recent or self.has_client

    # -- joining
    def join(self, ssid, password):
        with self.lock:
            self.state, self.joining_ssid, self.error = "joining", ssid, None
            known = next((n for n in self.networks if n["ssid"] == ssid), None)
        time.sleep(self.drop_delay)
        self.stop_hotspot()
        con = CLIENT_CON_PREFIX + ssid
        self.nm("connection", "delete", "id", con)
        args = ["connection", "add", "type", "wifi", "ifname", self.iface, "con-name", con, "ssid", ssid, "autoconnect", "yes"]
        if not known:
            args += ["wifi.hidden", "yes"]                         # typed by hand: it may not be broadcasting
        if password:
            sec = known["security"] if known else ""
            mgmt = "sae" if "WPA3" in sec and "WPA2" not in sec and "WPA1" not in sec else "wpa-psk"
            args += ["wifi-sec.key-mgmt", mgmt, "wifi-sec.psk", password]
        ok, msg = self.nm(*args)
        if ok:
            ok, msg = self.nm("connection", "up", "id", con, wait=45)
        if ok:
            print(f"[wifi] joined {ssid}", flush=True)
            with self.lock:
                self.state, self.joining_ssid, self.connected = "idle", None, True
            self.offline_since = None
            return True
        print(f"[wifi] could not join {ssid}: {msg}", flush=True)
        self.nm("connection", "delete", "id", con)
        with self.lock:
            self.joining_ssid = None
            self.error = f"Could not join {ssid}. Check the password and try again."
        self.start_hotspot()
        return False

    def retry_saved(self):
        """Take the hotspot down for a moment in case a saved network has come back."""
        with self.lock:
            self.state = "joining"
            self.joining_ssid = None
        self.stop_hotspot()
        ok, _ = self.nm("device", "connect", self.iface, wait=40)
        if ok and self.online():
            print("[wifi] back on a saved network", flush=True)
            with self.lock:
                self.state, self.connected = "idle", True
            self.offline_since = None
            return True
        self.start_hotspot()
        return False

    # -- the loop
    def step(self):
        now = time.monotonic()
        with self.lock:
            pending, self.pending = self.pending, None
            force, self.force = self.force, False
            state = self.state
        if pending:
            self.join(*pending)
            return
        if force:
            self.stop_hotspot()
            self.start_hotspot()
            return
        rows = self.devices()
        link = self.online(rows) if rows is not None else None
        if state == "hotspot":
            self.has_client = self.clients() > 0
            if link:
                print("[wifi] online another way; closing the hotspot", flush=True)
                self.stop_hotspot()
                with self.lock:
                    self.state, self.connected = "idle", True
            elif not self.hotspot_running(rows):
                print("[wifi] the hotspot went down; starting it again", flush=True)
                self.start_hotspot()
            elif now - self.hotspot_t > self.retry_every and not self.in_use() and self.saved_networks():
                self.retry_saved()
            return
        if link is None:
            return
        if link:
            self.offline_since = None
            return
        if self.hotspot_running(rows):                             # left up by the last run, with its password
            self.start_hotspot()
            return
        if self.offline_since is None:
            self.offline_since = now
        wait = self.grace_saved if self.saved_networks() else self.grace_new
        if now - self.offline_since >= wait and now >= self.next_start:
            self.start_hotspot()

    def run(self):
        print("[wifi] watching the connection", flush=True)
        while True:
            try:
                self.step()
            except Exception as e:
                print(f"[wifi] {e!r}", flush=True)
            self.wake.wait(self.poll if self.active or self.offline_since is not None else self.poll_online)
            self.wake.clear()

    # -- for the screen and the web server
    @property
    def active(self):
        return self.state != "idle"

    def page_seen(self):
        self.page_t = time.monotonic()

    def view(self):
        with self.lock:
            state, ssid, error = self.state, self.joining_ssid, self.error
            networks = list(self.networks)
        if state == "joining":
            stage = "joining"
        elif state == "hotspot" and (self.page_t is not None or self.has_client):
            stage = "open"
        else:
            stage = "join"
        return {"stage": stage, "ssid": ssid, "error": error, "networks": networks}

    def join_qr(self):
        return f"WIFI:T:WPA;S:{self.hotspot_ssid};P:{self.hotspot_password};;"

    def request_join(self, ssid, password):
        """Called from the web server. Returns an error message, or None when the attempt will start."""
        ssid, password = str(ssid or ""), str(password or "")
        if not 1 <= len(ssid.encode()) <= 32:
            return "Enter a network name."
        if password and not 8 <= len(password) <= 63:
            return "Wi-Fi passwords are 8 to 63 characters."
        with self.lock:
            if self.state != "hotspot":
                return "Busy, try again in a moment."
            self.state, self.joining_ssid, self.pending = "joining", ssid, (ssid, password)
        self.wake.set()
        return None

    def request_setup(self):
        """Open the hotspot on request; a saved network is retried as usual if nobody uses it."""
        if not self.enabled:
            return False
        with self.lock:
            if self.state != "idle":
                return True
            self.force = True
            self.error = None
        self.page_t = None
        self.wake.set()
        return True

    def take_connected(self):
        with self.lock:
            connected, self.connected = self.connected, False
        return connected
