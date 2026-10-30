"""Live settings for Pultwerk01: everything the web page can change.

Hardware (pins, SPI speed, I2C address) stays in config.toml and needs a restart.
Everything here is read by the display loop on every pass and saved to settings.json
atomically, so a change on the phone shows on the screen within a second.
"""
import copy
import json
import os
import re
import threading
from pathlib import Path

# Every screen the display knows, in the default order. "kind" is what the renderer
# dispatches on; a kind may appear once (ticker slides are expanded from the ticker list).
SCREENS = [
    {"id": "photo",      "group": "Time & photos", "name": "Photos",          "desc": "Your pictures, one at a time"},
    {"id": "clock",      "group": "Time & photos", "name": "Clock",           "desc": "Time, weekday and date"},
    {"id": "date",       "group": "Time & photos", "name": "Date",            "desc": "Day of the month, big"},
    {"id": "calendar",   "group": "Time & photos", "name": "Month calendar",  "desc": "This month, today in orange"},
    {"id": "worldclock", "group": "Time & photos", "name": "World clocks",    "desc": "Three cities"},
    {"id": "progress",   "group": "Time & photos", "name": "Day, month, year","desc": "How far through each"},
    {"id": "countdown",  "group": "Time & photos", "name": "Countdown",       "desc": "Days to a date you set"},
    {"id": "hijri",      "group": "Islamic",       "name": "Hijri date",      "desc": "Umm al-Qura, with the Arabic month"},
    {"id": "arabic",     "group": "Islamic",       "name": "Arabic date",     "desc": "Weekday, date and Hijri date in Arabic"},
    {"id": "prayer",     "group": "Islamic",       "name": "Next prayer",     "desc": "Countdown ring to the next prayer"},
    {"id": "occasions",  "group": "Islamic",       "name": "Occasions",       "desc": "Days to Ramadan, Eid, Arafah, New Year, Founding and National Day"},
    {"id": "price",      "group": "Markets",       "name": "Share prices",    "desc": "One slide per ticker"},
    {"id": "weather",    "group": "Weather & sky", "name": "Weather",         "desc": "Now, high and low"},
    {"id": "forecast",   "group": "Weather & sky", "name": "Forecast",        "desc": "The next three days"},
    {"id": "uv",         "group": "Weather & sky", "name": "UV index",        "desc": "Now and today's peak"},
    {"id": "wind",       "group": "Weather & sky", "name": "Wind & humidity", "desc": "Speed, direction, humidity"},
    {"id": "aqi",        "group": "Weather & sky", "name": "Air quality",     "desc": "European AQI and PM2.5"},
    {"id": "sun",        "group": "Weather & sky", "name": "Sun",             "desc": "Sunrise, sunset, daylight left"},
    {"id": "moon",       "group": "Weather & sky", "name": "Moon",            "desc": "Phase and illumination"},
    {"id": "system",     "group": "System",        "name": "System",          "desc": "QR code to this page, address, CPU temperature"},
]

# What the needle can show. value_fn is resolved in desk_display.py; min/max map to the dial ends.
GAUGE_METRICS = [
    {"id": "off",          "name": "Parked (needle at rest)",            "unit": "",     "min": 0,   "max": 1},
    {"id": "temp",         "name": "Outdoor temperature",                 "unit": "°C",   "min": 0,   "max": 50},
    {"id": "humidity",     "name": "Humidity",                            "unit": "%",    "min": 0,   "max": 100},
    {"id": "wind",         "name": "Wind speed",                          "unit": "km/h", "min": 0,   "max": 60},
    {"id": "aqi",          "name": "Air quality (EU AQI)",                "unit": "",     "min": 0,   "max": 100},
    {"id": "price_change", "name": "First ticker, day change",            "unit": "%",    "min": -5,  "max": 5},
    {"id": "prayer",       "name": "Time to next prayer",                 "unit": "%",    "min": 0,   "max": 100},
    {"id": "daylight",     "name": "Daylight remaining",                  "unit": "%",    "min": 0,   "max": 100},
    {"id": "moon",         "name": "Moon illumination",                   "unit": "%",    "min": 0,   "max": 100},
    {"id": "planes",       "name": "Aircraft within radar range",         "unit": "",     "min": 0,   "max": 20},
    {"id": "uv",           "name": "UV index",                            "unit": "",     "min": 0,   "max": 11},
    {"id": "lux",          "name": "Room light (light sensor)",           "unit": "lx",   "min": 0,   "max": 500},
    {"id": "year",         "name": "Year progress",                       "unit": "%",    "min": 0,   "max": 100},
    {"id": "hijri_month",  "name": "Hijri month progress",                "unit": "%",    "min": 0,   "max": 100},
    {"id": "day",          "name": "Day progress (midnight to midnight)", "unit": "%",    "min": 0,   "max": 100},
    {"id": "cpu_temp",     "name": "Pi CPU temperature",                  "unit": "°C",   "min": 30,  "max": 80},
]

SOUND_EVENTS = [
    {"id": "key",    "name": "Key click",                    "desc": "A soft tick on NEXT and RADAR"},
    {"id": "hour",   "name": "Hourly tone",                  "desc": "One note on the hour, not during quiet hours"},
    {"id": "prayer", "name": "Prayer time",                  "desc": "Two notes when a prayer time arrives"},
    {"id": "price",  "name": "Share price move",             "desc": "Chime when a ticker moves past the threshold"},
    {"id": "plane",  "name": "Aircraft overhead",            "desc": "Tick when a new aircraft enters the inner ring"},
    {"id": "boot",   "name": "Start-up",                     "desc": "Rising two notes when the display starts"},
]

DEFAULTS = {
    "version": 1,
    "screens": [  # order matters; every id from SCREENS appears exactly once
        {"id": "photo",      "enabled": True,  "seconds": 12},
        {"id": "clock",      "enabled": True,  "seconds": 12},
        {"id": "price",      "enabled": True,  "seconds": 10},
        {"id": "weather",    "enabled": True,  "seconds": 12},
        {"id": "prayer",     "enabled": True,  "seconds": 12},
        {"id": "hijri",      "enabled": True,  "seconds": 8},
        {"id": "arabic",     "enabled": True,  "seconds": 8},
        {"id": "occasions",  "enabled": True,  "seconds": 8},
        {"id": "moon",       "enabled": True,  "seconds": 8},
        {"id": "forecast",   "enabled": False, "seconds": 10},
        {"id": "uv",         "enabled": False, "seconds": 8},
        {"id": "date",       "enabled": False, "seconds": 8},
        {"id": "calendar",   "enabled": False, "seconds": 10},
        {"id": "wind",       "enabled": False, "seconds": 8},
        {"id": "aqi",        "enabled": False, "seconds": 8},
        {"id": "sun",        "enabled": False, "seconds": 8},
        {"id": "worldclock", "enabled": False, "seconds": 10},
        {"id": "countdown",  "enabled": False, "seconds": 8},
        {"id": "progress",   "enabled": False, "seconds": 8},
        {"id": "system",     "enabled": False, "seconds": 10},
    ],
    "tickers": [
        {"symbol": "AAPL",    "label": "AAPL"},
        {"symbol": "2222.SR", "label": "ARAMCO"},
    ],
    "display": {
        "auto_brightness": True,
        "brightness": 80,          # used when auto is off
        "min_brightness": 8,       # auto range
        "max_brightness": 100,
        "clock_24h": True,
        "night_enabled": True,
        "sleep_at": "00:30",
        "wake_at": "06:30",
        "boot_info": True,         # show the System screen (QR code to this page) for 20 s after start-up
    },
    "gauge": {
        "metric": "temp",
        "backlight": False,
    },
    "sound": {
        "muted": False,
        "volume": 60,              # 1-100
        "quiet_from": "22:00",
        "quiet_to": "07:00",
        "events": {"key": True, "hour": False, "prayer": True, "price": True, "plane": False, "boot": True},
        "price_threshold_pct": 2.0,
    },
    "worldclock": [
        {"name": "LONDON", "tz": "Europe/London"},
        {"name": "CAIRO",  "tz": "Africa/Cairo"},
        {"name": "TOKYO",  "tz": "Asia/Tokyo"},
    ],
    "countdown": {"label": "HOLIDAY", "date": "2027-01-01"},
}


def _merge(base, new):
    """Fill missing keys from base; keep user values. Lists are taken whole from new."""
    out = copy.deepcopy(base)
    for k, v in (new or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _merge(out[k], v)
        else:
            out[k] = v
    return out


_HM = re.compile(r"^([01]\d|2[0-3]):[0-5]\d$")


def _hm(v, default):
    v = str(v or "").strip()
    return v if _HM.match(v) else default


def _num(v, default, lo, hi, kind=float):
    try:
        return kind(max(lo, min(hi, float(v))))
    except (TypeError, ValueError):
        return default


def _fix_screens(lst):
    """Make sure every known screen appears once, unknown ids are dropped, numbers are sane."""
    known = {s["id"] for s in SCREENS}
    seen, out = set(), []
    for s in lst or []:
        if not isinstance(s, dict) or s.get("id") not in known or s["id"] in seen:
            continue
        seen.add(s["id"])
        out.append({"id": s["id"], "enabled": bool(s.get("enabled", True)),
                    "seconds": _num(s.get("seconds"), 10, 3, 600, int)})
    for d in DEFAULTS["screens"]:
        if d["id"] not in seen:
            out.append(dict(d))
    return out


def _tz(v):
    from zoneinfo import ZoneInfo
    try:
        ZoneInfo(str(v))
        return str(v)
    except Exception:
        return "UTC"


class Settings:
    def __init__(self, path):
        self.path = Path(path)
        self.lock = threading.Lock()
        self.version = 0                      # bumps on every change; the app watches it
        self._d = copy.deepcopy(DEFAULTS)
        self.load()

    # -- persistence
    def load(self):
        try:
            with open(self.path, "r", encoding="utf-8") as f:
                self._d = _merge(DEFAULTS, json.load(f))
        except FileNotFoundError:
            pass
        except Exception as e:
            print(f"[settings] {self.path.name} unreadable ({e!r}); using defaults", flush=True)
        self._d["screens"] = _fix_screens(self._d.get("screens"))
        self.version += 1

    def save(self):
        tmp = self.path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(self._d, f, indent=2, ensure_ascii=False)
        os.replace(tmp, self.path)

    # -- access
    def get(self):
        with self.lock:
            return copy.deepcopy(self._d)

    def update(self, patch):
        """Merge a partial dict (as sent by the web page), validate, save, bump version."""
        with self.lock:
            d = _merge(self._d, patch)
            d["screens"] = _fix_screens(d.get("screens"))
            d["tickers"] = [{"symbol": str(t.get("symbol", "")).strip().upper()[:16],
                             "label": str(t.get("label") or t.get("symbol", "")).strip().upper()[:12]}
                            for t in d.get("tickers", []) if isinstance(t, dict) and str(t.get("symbol", "")).strip()][:8]
            disp, old = d["display"], self._d["display"]
            disp["brightness"] = _num(disp.get("brightness"), 80, 1, 100, int)
            disp["min_brightness"] = _num(disp.get("min_brightness"), 8, 1, 100, int)
            disp["max_brightness"] = _num(disp.get("max_brightness"), 100, disp["min_brightness"], 100, int)
            disp["sleep_at"] = _hm(disp.get("sleep_at"), old.get("sleep_at", "00:30"))
            disp["wake_at"] = _hm(disp.get("wake_at"), old.get("wake_at", "06:30"))
            for k in ("auto_brightness", "clock_24h", "night_enabled", "boot_info"):
                disp[k] = bool(disp.get(k))
            d["gauge"]["backlight"] = bool(d["gauge"].get("backlight"))
            if d["gauge"].get("metric") not in {m["id"] for m in GAUGE_METRICS}:
                d["gauge"]["metric"] = "off"
            snd = d["sound"]
            snd["volume"] = _num(snd.get("volume"), 60, 1, 100, int)
            snd["muted"] = bool(snd.get("muted"))
            snd["price_threshold_pct"] = _num(snd.get("price_threshold_pct"), 2.0, 0.1, 50)
            snd["quiet_from"] = _hm(snd.get("quiet_from"), self._d["sound"].get("quiet_from", "22:00"))
            snd["quiet_to"] = _hm(snd.get("quiet_to"), self._d["sound"].get("quiet_to", "07:00"))
            snd["events"] = {e["id"]: bool(snd.get("events", {}).get(e["id"], False)) for e in SOUND_EVENTS}
            d["worldclock"] = [{"name": str(w.get("name", ""))[:10].upper(), "tz": _tz(w.get("tz"))}
                               for w in d.get("worldclock", []) if isinstance(w, dict)][:3]
            cd = d.get("countdown") or {}
            d["countdown"] = {"label": str(cd.get("label", ""))[:16].upper(),
                              "date": cd.get("date") if re.match(r"^\d{4}-\d{2}-\d{2}$", str(cd.get("date", ""))) else "2027-01-01"}
            self._d = d
            self.save()
            self.version += 1
            return copy.deepcopy(self._d)

    def catalogue(self):
        """Static lists the web page needs to build its controls."""
        return {"screens": SCREENS, "gauge_metrics": GAUGE_METRICS, "sound_events": SOUND_EVENTS}
