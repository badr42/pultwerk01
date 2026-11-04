#!/usr/bin/env python3
"""Pultwerk01 desk display: rotating slides, needle gauge, speaker, light sensor, web control.

Hardware: EstarDyn 2.10" round TFT (GC9B72, 360x360) + SG90 gauge + MAX98357A speaker
+ BH1750 light sensor + two keys, on a Raspberry Pi Zero 2 W.  config.toml = hardware,
settings.json = everything the web page changes (http://<pi>:8080).

    python desk_display.py                  # run on the Pi
    python desk_display.py --preview out/   # render every slide to PNGs with demo data
    python desk_display.py --light-test     # print lux readings
    python desk_display.py --gauge-test     # sweep the needle
    python desk_display.py --sound-test     # play every event sound
"""
import argparse
import io
import json
import math
import queue
import random
import shutil
import socket
import struct
import subprocess
import threading
import time
import tomllib
import urllib.request
import zlib
from collections import deque
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont, ImageOps, features

from settings import GAUGE_METRICS, SCREENS, Settings, _merge as merge_settings
from wifi_setup import HOTSPOT_IP, WifiSetup

HERE = Path(__file__).resolve().parent
DATA = HERE.parent.parent if HERE.parent.name == "releases" else HERE   # config and settings outlive a release
S = 360
BG = (0, 0, 0)
FG = (255, 255, 255)
DIM = (120, 120, 126)
FAINT = (52, 52, 58)
UP = (74, 214, 142)
DOWN = (255, 94, 94)
ORANGE = (232, 89, 12)
UA = {"User-Agent": "Mozilla/5.0 pultwerk01"}
START = time.time()


# --------------------------------------------------------------------------- drawing
_fonts = {}


def font(weight, px):
    key = (weight, px)
    if key not in _fonts:
        _fonts[key] = ImageFont.truetype(str(HERE / "fonts" / f"Inter-{weight}.otf"), px)
    return _fonts[key]


# Arabic: Noto Kufi Arabic (variable weight, bundled, OFL) - geometric, sits well next to Inter.
AR_WEIGHT = {"Thin": 100, "ExtraLight": 200, "Light": 300, "Regular": 400}
AR_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
AR_WEEKDAYS = ["الاثنين", "الثلاثاء", "الأربعاء", "الخميس", "الجمعة", "السبت", "الأحد"]
AR_MONTHS = ["يناير", "فبراير", "مارس", "أبريل", "مايو", "يونيو", "يوليو", "أغسطس", "سبتمبر", "أكتوبر", "نوفمبر", "ديسمبر"]
HAVE_RAQM = features.check("raqm")


def ar_font(weight, px):
    key = ("ar", weight, px)
    if key not in _fonts:
        f = ImageFont.truetype(str(HERE / "fonts" / "NotoKufiArabic-VF.ttf"), px)
        try:
            f.set_variation_by_axes([AR_WEIGHT.get(weight, 300)])
        except Exception:
            pass
        _fonts[key] = f
    return _fonts[key]


def ar_shape(t):
    """Return (text, kwargs) ready for ImageDraw.text. With libraqm Pillow shapes Arabic itself;
    without it, arabic-reshaper + python-bidi produce the visual form."""
    if HAVE_RAQM:
        return t, {"direction": "rtl", "language": "ar"}
    try:
        import arabic_reshaper
        from bidi.algorithm import get_display
        return get_display(arabic_reshaper.reshape(t)), {}
    except Exception:
        return t, {}


def ar_num(n):
    return str(n).translate(AR_DIGITS)


class Canvas:
    """Draw in 360x360 coordinates; rendered k times larger then reduced for smooth edges."""

    def __init__(self, k=2):
        self.k = k
        self.im = Image.new("RGB", (S * k, S * k), BG)
        self.d = ImageDraw.Draw(self.im)

    def _xy(self, pts):
        return [c * self.k for p in pts for c in p]

    def text(self, x, y, t, weight="Light", size=20, fill=FG, anchor="mm", track=0):
        f = font(weight, max(1, round(size * self.k)))
        if not track:
            self.d.text((x * self.k, y * self.k), t, font=f, fill=fill, anchor=anchor)
            return
        widths = [self.d.textlength(c, font=f) for c in t]
        total = sum(widths) + track * self.k * (len(t) - 1)
        x0 = x * self.k - (total / 2 if anchor[0] == "m" else total if anchor[0] == "r" else 0)
        for c, w in zip(t, widths):
            self.d.text((x0, y * self.k), c, font=f, fill=fill, anchor="l" + anchor[1])
            x0 += w + track * self.k

    def ar(self, x, y, t, weight="Light", size=24, fill=FG, anchor="mm"):
        try:
            txt, kw = ar_shape(t)
            self.d.text((x * self.k, y * self.k), txt, font=ar_font(weight, max(1, round(size * self.k))),
                        fill=fill, anchor=anchor, **kw)
        except Exception as e:
            print(f"[arabic] {e!r}", flush=True)

    def text_width(self, t, weight, size):
        return self.d.textlength(t, font=font(weight, round(size * self.k))) / self.k

    def line(self, pts, fill=FG, width=1.0):
        self.d.line(self._xy(pts), fill=fill, width=max(1, round(width * self.k)), joint="curve")

    def circle(self, cx, cy, r, outline=None, fill=None, width=1.0):
        k = self.k
        self.d.ellipse([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k],
                       outline=outline, fill=fill, width=max(1, round(width * k)))

    def arc(self, cx, cy, r, a0, a1, fill=FG, width=1.0):
        k = self.k
        self.d.arc([(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k], a0, a1,
                   fill=fill, width=max(1, round(width * k)))

    def poly(self, pts, fill=FG):
        self.d.polygon(self._xy(pts), fill=fill)

    def outline_mask(self, draw_fn, box, lw, color=FG, clear=True):
        k = self.k
        x0, y0, x1, y1 = [int(v * k) for v in box]
        m = Image.new("L", (x1 - x0, y1 - y0), 0)
        draw_fn(ImageDraw.Draw(m), k, x0, y0)
        n = 2 * max(1, round(lw * k)) + 1
        edge = ImageChops.subtract(m, m.filter(ImageFilter.MinFilter(n)))
        if clear:
            self.im.paste(BG, (x0, y0), m)
        self.im.paste(color, (x0, y0), edge)

    def cloud(self, cx, cy, w, lw=1.6, color=FG):
        def shape(md, k, x0, y0):
            def circ(ox, oy, r):
                md.ellipse([(cx + (ox - r) * w) * k - x0, (cy + (oy - r) * w) * k - y0,
                            (cx + (ox + r) * w) * k - x0, (cy + (oy + r) * w) * k - y0], fill=255)
            circ(-0.26, 0.02, 0.17)
            circ(-0.02, -0.10, 0.26)
            circ(0.27, 0.04, 0.15)
            md.rectangle([(cx - 0.26 * w) * k - x0, (cy + 0.02 * w) * k - y0,
                          (cx + 0.27 * w) * k - x0, (cy + 0.19 * w) * k - y0], fill=255)
        self.outline_mask(shape, (cx - 0.6 * w, cy - 0.5 * w, cx + 0.6 * w, cy + 0.5 * w), lw, color)

    def moon(self, cx, cy, r, lw=1.6, color=FG):
        def shape(md, k, x0, y0):
            md.ellipse([(cx - r) * k - x0, (cy - r) * k - y0, (cx + r) * k - x0, (cy + r) * k - y0], fill=255)
            o = 0.45 * r
            md.ellipse([(cx + o - r * 0.85) * k - x0, (cy - o * 0.6 - r * 0.85) * k - y0,
                        (cx + o + r * 0.85) * k - x0, (cy - o * 0.6 + r * 0.85) * k - y0], fill=0)
        self.outline_mask(shape, (cx - 1.3 * r, cy - 1.3 * r, cx + 1.3 * r, cy + 1.3 * r), lw, color, clear=False)

    def sun(self, cx, cy, r, lw=1.6, a0=0, a1=360, color=FG):
        self.arc(cx, cy, r, a0, a1, fill=color, width=lw)
        for i in range(8):
            a = math.radians(i * 45)
            if not (a0 <= i * 45 <= a1):
                continue
            self.line([(cx + math.cos(a) * r * 1.5, cy + math.sin(a) * r * 1.5),
                       (cx + math.cos(a) * r * 2.0, cy + math.sin(a) * r * 2.0)], color, lw)

    def moon_phase(self, cx, cy, r, phase, lit=(230, 230, 234), dark=(34, 34, 38)):
        """Disc with a correct terminator. phase 0 = new, 0.5 = full, waxing lights the right side."""
        k = self.k
        box = [(cx - r) * k, (cy - r) * k, (cx + r) * k, (cy + r) * k]
        self.d.ellipse(box, fill=dark)
        waxing = phase < 0.5
        m = Image.new("L", (int(2 * r * k) + 2, int(2 * r * k) + 2), 0)
        md = ImageDraw.Draw(m)
        md.ellipse([0, 0, 2 * r * k, 2 * r * k], fill=255)
        hm = Image.new("L", m.size, 0)
        hd = ImageDraw.Draw(hm)
        hd.rectangle([r * k if waxing else 0, 0, 2 * r * k if waxing else r * k, 2 * r * k], fill=255)
        w = abs(math.cos(2 * math.pi * phase)) * r * k
        ill = (1 - math.cos(2 * math.pi * phase)) / 2
        if ill > 0.5:                                  # gibbous: lit half plus a lit ellipse on the dark side
            hd.ellipse([r * k - w, 0, r * k + w, 2 * r * k], fill=255)
        else:                                          # crescent: lit half minus a dark ellipse
            hd.ellipse([r * k - w, 0, r * k + w, 2 * r * k], fill=0)
        litmask = ImageChops.multiply(m, hm)
        self.im.paste(lit, (int((cx - r) * k), int((cy - r) * k)), litmask)
        self.d.ellipse(box, outline=(90, 90, 96), width=max(1, round(1.2 * k)))

    def image(self):
        return self.im.reduce(self.k) if self.k > 1 else self.im


def fit_text(canvas, t, weight, size, max_w):
    while size > 20 and canvas.text_width(t, weight, size) > max_w:
        size -= 2
    return size


def placeholder():
    c = Canvas()
    c.text(180, 180, "···", "Thin", 80, FAINT)
    return c.image()


# --------------------------------------------------------------------------- network
def http_json(url, timeout=8):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=timeout) as r:
        return json.load(r)


def fetch_quote(symbol):
    j = http_json(f"https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range=1d&interval=15m")
    res = j["chart"]["result"][0]
    meta = res["meta"]
    closes = [c for c in res["indicators"]["quote"][0].get("close", []) if c is not None]
    price = meta["regularMarketPrice"]
    prev = meta.get("chartPreviousClose") or meta.get("previousClose") or price
    return {"price": price, "prev": prev, "closes": closes[-40:], "t": time.time(),
            "currency": meta.get("currency", "")}


def fetch_weather(lat, lon):
    j = http_json("https://api.open-meteo.com/v1/forecast?latitude=%s&longitude=%s"
                  "&current=temperature_2m,relative_humidity_2m,weather_code,is_day,wind_speed_10m,wind_direction_10m,uv_index"
                  "&daily=weather_code,temperature_2m_max,temperature_2m_min,sunrise,sunset,uv_index_max"
                  "&timezone=auto&forecast_days=4" % (lat, lon))
    c, d = j["current"], j["daily"]
    return {"temp": c["temperature_2m"], "code": c["weather_code"], "day": bool(c["is_day"]),
            "humidity": c["relative_humidity_2m"], "wind": c["wind_speed_10m"], "wind_dir": c["wind_direction_10m"],
            "uv": c.get("uv_index"), "uv_max": (d.get("uv_index_max") or [None])[0],
            "hi": d["temperature_2m_max"][0], "lo": d["temperature_2m_min"][0],
            "sunrise": d["sunrise"], "sunset": d["sunset"],
            "forecast": [{"date": d["time"][i], "code": d["weather_code"][i], "hi": d["temperature_2m_max"][i],
                          "lo": d["temperature_2m_min"][i]} for i in range(1, min(4, len(d["time"])))],
            "t": time.time()}


def fetch_aqi(lat, lon):
    j = http_json("https://air-quality-api.open-meteo.com/v1/air-quality?latitude=%s&longitude=%s"
                  "&current=european_aqi,pm2_5,pm10&timezone=auto" % (lat, lon))
    c = j["current"]
    return {"aqi": c["european_aqi"], "pm25": c["pm2_5"], "pm10": c["pm10"], "t": time.time()}


PRAYERS = ["Fajr", "Dhuhr", "Asr", "Maghrib", "Isha"]


def fetch_prayer(day, cfg, tz):
    loc = cfg["location"]
    j = http_json("https://api.aladhan.com/v1/timings/%s?latitude=%s&longitude=%s&method=%s&calendarMethod=UAQ"
                  % (day.strftime("%d-%m-%Y"), loc["lat"], loc["lon"], cfg["prayer"]["method"]))
    out = {}
    for name in PRAYERS:
        hh, mm = j["data"]["timings"][name][:5].split(":")
        out[name] = datetime(day.year, day.month, day.day, int(hh), int(mm), tzinfo=tz)
    h = j["data"]["date"].get("hijri")
    hijri = None
    if h:
        m = int(h["month"]["number"])
        hijri = {"day": int(h["day"]), "month": m, "month_en": HIJRI_MONTHS[m - 1], "month_ar": HIJRI_MONTHS_AR[m - 1],
                 "year": int(h["year"]), "weekday_ar": h["weekday"]["ar"]}
    return out, hijri


def fetch_h2g(hd, hm, hy):
    """Umm al-Qura Hijri -> Gregorian date (official Saudi calendar)."""
    j = http_json("https://api.aladhan.com/v1/hToG/%02d-%02d-%d?calendarMethod=UAQ" % (hd, hm, hy))
    dd, mm, yy = j["data"]["gregorian"]["date"].split("-")
    return date(int(yy), int(mm), int(dd))


def fetch_planes(lat, lon, radius):
    last = None
    for host in ("api.airplanes.live", "api.adsb.lol"):
        try:
            return http_json(f"https://{host}/v2/point/{lat}/{lon}/{int(radius)}", timeout=6).get("ac", [])
        except Exception as e:
            last = e
    raise last


# --------------------------------------------------------------------------- astronomy (offline)
def moon_phase(now_utc):
    """0 = new, 0.25 = first quarter, 0.5 = full, 0.75 = last quarter."""
    ref = datetime(2000, 1, 6, 18, 14, tzinfo=timezone.utc)
    days = (now_utc - ref).total_seconds() / 86400
    return (days / 29.530588853) % 1.0


def moon_name(p):
    names = ["NEW MOON", "WAXING CRESCENT", "FIRST QUARTER", "WAXING GIBBOUS",
             "FULL MOON", "WANING GIBBOUS", "LAST QUARTER", "WANING CRESCENT"]
    return names[int((p * 8 + 0.5) % 8)]


HIJRI_MONTHS = ["Muharram", "Safar", "Rabi' al-Awwal", "Rabi' al-Thani", "Jumada al-Ula", "Jumada al-Akhirah",
                "Rajab", "Sha'ban", "Ramadan", "Shawwal", "Dhu al-Qi'dah", "Dhu al-Hijjah"]
HIJRI_MONTHS_AR = ["محرم", "صفر", "ربيع الأول", "ربيع الثاني", "جمادى الأولى", "جمادى الآخرة",
                   "رجب", "شعبان", "رمضان", "شوال", "ذو القعدة", "ذو الحجة"]


def hijri_tabular(d):
    """Arithmetic (tabular) Islamic date: within a day of Umm al-Qura. Used until the API answers."""
    jd = (1461 * (d.year + 4800 + (d.month - 14) // 12)) // 4 + (367 * (d.month - 2 - 12 * ((d.month - 14) // 12))) // 12 \
        - (3 * ((d.year + 4900 + (d.month - 14) // 12) // 100)) // 4 + d.day - 32075
    l = jd - 1948440 + 10632
    n = (l - 1) // 10631
    l = l - 10631 * n + 354
    j = ((10985 - l) // 5316) * ((50 * l) // 17719) + (l // 5670) * ((43 * l) // 15238)
    l = l - ((30 - j) // 15) * ((17719 * j) // 50) - (j // 16) * ((15238 * j) // 43) + 29
    m = (24 * l) // 709
    day = l - (709 * m) // 24
    year = 30 * n + j - 30
    return {"day": day, "month": m, "month_en": HIJRI_MONTHS[m - 1], "month_ar": HIJRI_MONTHS_AR[m - 1], "year": year,
            "weekday_ar": "", "estimate": True}


# Occasions: Hijri ones come from the Umm al-Qura calendar (the actual day can still move with the moon sighting)
OCCASIONS_H = [(9, 1, "RAMADAN", "رمضان"), (10, 1, "EID AL-FITR", "عيد الفطر"), (12, 9, "DAY OF ARAFAH", "يوم عرفة"),
               (12, 10, "EID AL-ADHA", "عيد الأضحى"), (1, 1, "HIJRI NEW YEAR", "رأس السنة الهجرية")]
OCCASIONS_G = [(2, 22, "FOUNDING DAY", "يوم التأسيس"), (9, 23, "NATIONAL DAY", "اليوم الوطني")]


# --------------------------------------------------------------------------- shared state
def radar_area(cfg, st):
    """(lat, lon, radius_nm) for the radar: the web page's choice, else config.toml."""
    r, loc = st.get("radar") or {}, cfg["location"]
    custom = not r.get("home", True) and r.get("lat") is not None and r.get("lon") is not None
    lat, lon = (r["lat"], r["lon"]) if custom else (loc["lat"], loc["lon"])
    return float(lat), float(lon), float(r.get("radius_nm") or cfg["radar"]["radius_nm"])


class Data:
    def __init__(self, cfg, settings, tz):
        self.cfg, self.settings, self.tz = cfg, settings, tz
        self.lock = threading.Lock()
        self.quotes, self.weather, self.aqi, self.prayer = {}, None, None, {}
        self.hijri = {}                                  # iso date -> dict
        self.occ, self.occ_cache = {}, (None, [])        # (hm, hd, hy) -> Gregorian date from Umm al-Qura; per-day list
        self.kick = set()                                # jobs to run now (e.g. tickers changed on the web page)
        self.planes, self.trails = {}, {}
        self.planes_ok = False
        self.new_plane = threading.Event()               # set when an aircraft enters the inner ring
        self.radar_on = threading.Event()
        self.stop = threading.Event()
        self.area = radar_area(cfg, settings.get())     # (lat, lon, radius_nm) the radar covers

    def set_area(self, area):
        """Move or resize the radar: aircraft and trails from the old area are dropped, the new one is fetched now."""
        if area == self.area:
            return
        with self.lock:
            self.area = area
            self.planes.clear()
            self.trails.clear()
            self.planes_ok = False
            self.kick.add("planes")

    def to_nm(self, lat, lon):
        lat0, lon0, _ = self.area
        return ((lon - lon0) * 60 * math.cos(math.radians(lat0)), (lat - lat0) * 60)

    def ingest_planes(self, acs):
        now, seen = time.time(), set()
        inner = self.area[2] / 3
        with self.lock:
            for a in acs:
                if "lat" not in a or "lon" not in a or a.get("alt_baro") == "ground":
                    continue
                h = a.get("hex")
                alt = a.get("alt_baro")
                x, y = self.to_nm(a["lat"], a["lon"])
                was_inner = self.planes.get(h, {}).get("inner", False)
                is_inner = math.hypot(x, y) <= inner
                if is_inner and not was_inner:
                    self.new_plane.set()
                self.planes[h] = {
                    "lat": a["lat"], "lon": a["lon"], "track": a.get("track", a.get("true_heading", 0)) or 0,
                    "gs": a.get("gs", 0) or 0, "alt": alt if isinstance(alt, (int, float)) else None,
                    "name": (a.get("flight") or a.get("r") or h or "").strip(), "type": a.get("t", ""),
                    "t": now, "inner": is_inner}
                self.trails.setdefault(h, deque(maxlen=8)).append((x, y))
                seen.add(h)
            for h in [h for h, p in self.planes.items() if now - p["t"] > 20]:
                self.planes.pop(h, None)
                self.trails.pop(h, None)
            self.planes_ok = True

    def plane_count(self):
        rad = self.area[2]
        with self.lock:
            return sum(1 for p in self.planes.values() if math.hypot(*self.to_nm(p["lat"], p["lon"])) <= rad)

    def worker(self):
        cfg = self.cfg
        loc = cfg["location"]
        next_run = {"quotes": 0, "weather": 0, "aqi": 0, "prayer": 0, "occasions": 20, "planes": 0}
        fails = {"planes": 0}
        while not self.stop.is_set():
            with self.lock:
                for j in self.kick:
                    next_run[j] = 0
                self.kick.clear()
            now = time.time()
            for job in list(next_run):
                if now < next_run[job]:
                    continue
                try:
                    if job == "quotes":
                        for t in self.settings.get()["tickers"]:
                            try:
                                q = fetch_quote(t["symbol"])
                            except Exception as e:                  # one bad symbol must not block the others
                                print(f"[quotes] {t['symbol']}: {e!r}", flush=True)
                                continue
                            with self.lock:
                                self.quotes[t["symbol"]] = q
                        next_run[job] = now + 60
                    elif job == "weather":
                        w = fetch_weather(loc["lat"], loc["lon"])
                        with self.lock:
                            self.weather = w
                        next_run[job] = now + 900
                    elif job == "aqi":
                        a = fetch_aqi(loc["lat"], loc["lon"])
                        with self.lock:
                            self.aqi = a
                        next_run[job] = now + 1800
                    elif job == "prayer":
                        today = datetime.now(self.tz).date()
                        for d in (today, today + timedelta(days=1)):
                            if d.isoformat() not in self.prayer:
                                p, h = fetch_prayer(d, cfg, self.tz)
                                with self.lock:
                                    self.prayer[d.isoformat()] = p
                                    if h:
                                        self.hijri[d.isoformat()] = h
                        next_run[job] = now + 1800
                    elif job == "occasions":
                        today = datetime.now(self.tz).date()
                        for (hm, hd, hy) in self.occasion_keys(today):
                            if (hm, hd, hy) not in self.occ:
                                g = fetch_h2g(hd, hm, hy)
                                with self.lock:
                                    self.occ[(hm, hd, hy)] = g
                                    self.occ_cache = (None, [])
                        next_run[job] = now + 43200
                    elif job == "planes" and self.radar_on.is_set():
                        lat, lon, rad = self.area
                        self.ingest_planes(fetch_planes(lat, lon, rad * 1.15))
                        fails["planes"] = 0
                        next_run[job] = now + cfg["radar"]["poll_seconds"]
                except Exception as e:
                    print(f"[{job}] {e!r}", flush=True)
                    if job == "planes":
                        fails["planes"] += 1
                        next_run[job] = now + min(120, 5 * 2 ** fails["planes"])
                    else:
                        next_run[job] = now + 45
            self.stop.wait(0.5)

    def hijri_for(self, d):
        with self.lock:
            h = self.hijri.get(d.isoformat())
        return h or hijri_tabular(d)

    def occasion_keys(self, today):
        h = self.hijri_for(today)
        return [(m, d, h["year"] + (0 if (m, d) >= (h["month"], h["day"]) else 1)) for m, d, _, _ in OCCASIONS_H]

    def occasions(self, today):
        """Upcoming occasions, soonest first: (date, english, arabic, exact)."""
        if self.occ_cache[0] == today:
            return self.occ_cache[1]
        out = []
        for (m, d, hy), (_, _, en, ar) in zip(self.occasion_keys(today), OCCASIONS_H):
            g = self.occ.get((m, d, hy))
            exact = g is not None
            if g is None:                                  # tabular estimate until the API answers
                for k in range(0, 400):
                    x = today + timedelta(days=k)
                    t = hijri_tabular(x)
                    if (t["year"], t["month"], t["day"]) == (hy, m, d):
                        g = x
                        break
            if g and g >= today:
                out.append((g, en, ar, exact))
        for m, d, en, ar in OCCASIONS_G:
            g = date(today.year, m, d)
            if g < today:
                g = date(today.year + 1, m, d)
            out.append((g, en, ar, True))
        out.sort()
        self.occ_cache = (today, out)
        return out

    def sun_times(self, now):
        """(sunrise, sunset) datetimes for today, from the weather fetch."""
        w = self.weather
        if not w:
            return None, None
        try:
            idx = 0 if now.date().isoformat() == w["sunrise"][0][:10] else 1
            return (datetime.fromisoformat(w["sunrise"][idx]).replace(tzinfo=self.tz),
                    datetime.fromisoformat(w["sunset"][idx]).replace(tzinfo=self.tz))
        except Exception:
            return None, None


# --------------------------------------------------------------------------- slides
def draw_ring_dot(c, frac, r=167):
    a = math.radians(frac * 360 - 90)
    c.circle(180 + math.cos(a) * r, 180 + math.sin(a) * r, 2.2, fill=FG)


def slide_clock(app, now, arg=None):
    c = Canvas()
    t = now.strftime("%H:%M") if app.st["display"].get("clock_24h", True) else now.strftime("%I:%M").lstrip("0")
    c.text(180, 165, t, "Thin", 124)
    c.text(180, 246, now.strftime("%A").upper(), "Light", 15, DIM, track=5)
    c.text(180, 270, now.strftime("%-d %B").upper(), "Light", 15, DIM, track=5)
    draw_ring_dot(c, (now.second + now.microsecond / 1e6) / 60)
    return c.image()


def slide_date(app, now, arg=None):
    c = Canvas()
    c.text(180, 96, now.strftime("%A").upper(), "Light", 16, DIM, track=6)
    c.text(180, 182, str(now.day), "Thin", 150)
    c.text(180, 268, now.strftime("%B %Y").upper(), "Light", 15, DIM, track=5)
    doy = now.timetuple().tm_yday
    c.arc(180, 180, 167, 0, 360, FAINT, 1)
    c.arc(180, 180, 167, -90, -90 + 360 * doy / 366, FG, 1.6)
    return c.image()


def slide_hijri(app, now, arg=None):
    h = app.data.hijri_for(now.date())
    c = Canvas()
    c.text(180, 88, h["month_en"].upper(), "Light", 15, DIM, track=5)
    c.text(180, 172, str(h["day"]), "Thin", 140)
    c.text(180, 246, f"{h['year']} AH", "Light", 15, DIM, track=4)
    c.ar(180, 284, f"{ar_num(h['day'])} {h['month_ar']} {ar_num(h['year'])}", "Light", 19, DIM)
    c.arc(180, 180, 167, 0, 360, FAINT, 1)
    c.arc(180, 180, 167, -90, -90 + 360 * h["day"] / 30, FG, 1.6)
    return c.image()


def slide_arabic(app, now, arg=None):
    """The whole date in Arabic: weekday, Gregorian day/month/year, Hijri date."""
    h = app.data.hijri_for(now.date())
    c = Canvas()
    c.ar(180, 80, AR_WEEKDAYS[now.weekday()], "Light", 26, DIM)
    c.ar(180, 168, ar_num(now.day), "ExtraLight", 118)
    c.ar(180, 246, f"{AR_MONTHS[now.month - 1]} {ar_num(now.year)}", "Light", 22, FG)
    c.line([(156, 276), (204, 276)], FAINT, 1)
    c.ar(180, 304, f"{ar_num(h['day'])} {h['month_ar']} {ar_num(h['year'])} هـ", "Light", 17, DIM)
    return c.image()


def slide_occasions(app, now, arg=None):
    today = now.date()
    h = app.data.hijri_for(today)
    occ = app.data.occasions(today)
    c = Canvas()
    if h["month"] == 9:                                            # Ramadan in progress: count the days
        c.text(180, 84, "RAMADAN", "Light", 15, DIM, track=6)
        c.ar(180, 112, "رمضان", "Light", 20, DIM)
        c.text(180, 190, str(h["day"]), "Thin", 130)
        c.text(180, 262, "DAY", "Light", 14, DIM, track=5)
        c.arc(180, 180, 167, 0, 360, FAINT, 1)
        c.arc(180, 180, 167, -90, -90 + 360 * h["day"] / 30, FG, 1.6)
        eid = next((o for o in occ if o[1] == "EID AL-FITR"), None)
        if eid:
            c.text(180, 290, f"EID IN ABOUT {(eid[0] - today).days} DAYS", "Light", 12, FAINT, track=3)
        return c.image()
    if not occ:
        return placeholder()
    g, en, ar, exact = occ[0]
    days = (g - today).days
    c.text(180, 84, en, "Light", 15, DIM, track=5)
    c.ar(180, 113, ar, "Light", 20, DIM)
    if days == 0:
        c.text(180, 190, "TODAY", "Thin", 64)
    else:
        c.text(180, 190, str(days), "Thin", 130)
        c.text(180, 262, "DAY TO GO" if days == 1 else "DAYS TO GO", "Light", 14, DIM, track=5)
    c.text(180, 290, ("" if exact else "≈ ") + g.strftime("%-d %B %Y").upper(), "Light", 13, FAINT, track=3)
    return c.image()


def slide_price(app, now, idx):
    tks = app.st["tickers"]
    if idx >= len(tks):
        return placeholder()
    tk = tks[idx]
    c = Canvas()
    c.text(180, 92, tk.get("label", tk["symbol"]).upper(), "Regular", 16, DIM, track=6)
    q = app.data.quotes.get(tk["symbol"])
    if not q:
        c.text(180, 180, "···", "Thin", 80, FAINT)
        return c.image()
    chg = (q["price"] / q["prev"] - 1) * 100 if q["prev"] else 0
    col = UP if chg >= 0 else DOWN
    p = f"{q['price']:,.2f}"
    c.text(180, 168, p, "Thin", fit_text(c, p, "Thin", 92, 270))
    c.text(180, 230, f"{'+' if chg >= 0 else '−'}{abs(chg):.2f}%", "Light", 26, col)
    cl = q["closes"]
    if len(cl) > 3:
        lo, hi = min(cl), max(cl)
        span = (hi - lo) or 1
        pts = [(85 + 190 * i / (len(cl) - 1), 296 - 34 * (v - lo) / span) for i, v in enumerate(cl)]
        c.line(pts, col, 1.3)
    return c.image()


def weather_icon(c, code, day, cx, cy):
    s = 62
    if code == 0:
        c.sun(cx, cy, 15) if day else c.moon(cx, cy, 21)
    elif code in (1, 2):
        if day:
            c.sun(cx - 14, cy - 12, 11, a0=170, a1=320)
        else:
            c.moon(cx - 14, cy - 10, 14)
        c.cloud(cx + 6, cy + 6, s * 0.9)
    elif code in (45, 48):
        for i, w in enumerate((46, 36, 46)):
            c.line([(cx - w / 2, cy - 14 + i * 14), (cx + w / 2, cy - 14 + i * 14)], FG, 1.6)
    else:
        c.cloud(cx, cy - 6, s)
        if code in (51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82):
            for dx in (-14, 0, 14):
                c.line([(cx + dx + 3, cy + 16), (cx + dx - 3, cy + 28)], FG, 1.6)
        elif code in (71, 73, 75, 77, 85, 86):
            for dx in (-14, 0, 14):
                c.circle(cx + dx, cy + 23, 1.8, fill=FG)
        elif code in (95, 96, 99):
            c.line([(cx + 3, cy + 13), (cx - 5, cy + 24), (cx + 2, cy + 24), (cx - 4, cy + 35)], FG, 1.6)


def weather_word(code):
    for codes, w in (((0,), "CLEAR"), ((1, 2), "PARTLY CLOUDY"), ((3,), "OVERCAST"), ((45, 48), "FOG"),
                     ((51, 53, 55, 56, 57), "DRIZZLE"), ((61, 63, 65, 66, 67, 80, 81, 82), "RAIN"),
                     ((71, 73, 75, 77, 85, 86), "SNOW"), ((95, 96, 99), "THUNDERSTORM")):
        if code in codes:
            return w
    return ""


def slide_weather(app, now, arg=None):
    w = app.data.weather
    if not w:
        return placeholder()
    c = Canvas()
    weather_icon(c, w["code"], w["day"], 180, 88)
    c.text(180, 188, f"{round(w['temp'])}°", "Thin", 112)
    c.text(180, 252, weather_word(w["code"]), "Light", 14, DIM, track=5)
    c.text(180, 284, f"H {round(w['hi'])}°     L {round(w['lo'])}°", "Light", 15, DIM)
    return c.image()


def icon_into(c, code, day, cx, cy, size):
    """Draw a weather icon at any size: render it full size on its own canvas, scale it, paste it."""
    ic = Canvas()
    weather_icon(ic, code, day, 180, 180)
    k = ic.k
    im = ic.im.crop(((180 - 60) * k, (180 - 60) * k, (180 + 60) * k, (180 + 60) * k))
    px = max(1, int(size * c.k))
    im = im.resize((px, px), Image.LANCZOS)
    c.im.paste(im, (int((cx - size / 2) * c.k), int((cy - size / 2) * c.k)), im.convert("L"))


def slide_forecast(app, now, arg=None):
    w = app.data.weather
    if not w or not w.get("forecast"):
        return placeholder()
    c = Canvas()
    rows = w["forecast"][:3]
    for f, y in zip(rows, [112, 180, 248][:len(rows)]):
        d = date.fromisoformat(f["date"])
        c.text(78, y, d.strftime("%a").upper(), "Light", 15, DIM, anchor="lm", track=4)
        icon_into(c, f["code"], True, 160, y, 46)
        c.text(250, y, f"{round(f['hi'])}°", "Thin", 38, FG, anchor="rm")
        c.text(258, y + 3, f"{round(f['lo'])}°", "Light", 17, DIM, anchor="lm")
    return c.image()


def uv_word(v):
    for lim, w in ((3, "LOW"), (6, "MODERATE"), (8, "HIGH"), (11, "VERY HIGH")):
        if v < lim:
            return w
    return "EXTREME"


def slide_uv(app, now, arg=None):
    w = app.data.weather
    if not w or w.get("uv") is None:
        return placeholder()
    c = Canvas()
    uv = w["uv"]
    c.text(180, 86, "UV INDEX", "Light", 15, DIM, track=6)
    c.text(180, 168, f"{uv:.0f}", "Thin", 120)
    c.text(180, 240, uv_word(uv), "Light", 15, DIM, track=5)
    if w.get("uv_max") is not None:
        c.text(180, 282, f"PEAK TODAY {w['uv_max']:.0f}", "Light", 13, FAINT, track=3)
    c.arc(180, 180, 167, 0, 360, FAINT, 1)
    c.arc(180, 180, 167, -90, -90 + 360 * min(1, uv / 11), FG, 1.6)
    return c.image()


def slide_calendar(app, now, arg=None):
    import calendar
    c = Canvas()
    c.text(180, 66, now.strftime("%B").upper(), "Light", 15, DIM, track=6)
    weeks = calendar.Calendar(firstweekday=6).monthdayscalendar(now.year, now.month)   # Sunday first
    cw = 33
    x0 = 180 - 3 * cw
    rh = 30 if len(weeks) <= 5 else 26
    for i, hd in enumerate("SMTWTFS"):
        c.text(x0 + i * cw, 100, hd, "Regular", 11, DIM if i < 5 else FAINT)
    for r, wk in enumerate(weeks):
        y = 132 + r * rh
        for i, d in enumerate(wk):
            if not d:
                continue
            x = x0 + i * cw
            if d == now.day:
                c.circle(x, y, 12.5, fill=ORANGE)
                c.text(x, y, str(d), "Regular", 15, FG)
            else:
                c.text(x, y, str(d), "Light", 15, FG if i < 5 else DIM)      # Friday and Saturday dimmed
    return c.image()


def local_ip():
    try:
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sk.connect(("10.255.255.255", 1))                  # no packet is sent; picks the outgoing interface
        ip = sk.getsockname()[0]
        sk.close()
        return ip
    except Exception:
        return None


def qr_matrix(text):
    try:
        import qrcode
        q = qrcode.QRCode(border=0, error_correction=qrcode.constants.ERROR_CORRECT_M)
        q.add_data(text)
        q.make(fit=True)
        return q.get_matrix()
    except Exception:
        return None


def draw_qr(c, app, text):
    """Draw a QR code in the upper middle of the screen; False if it could not be made."""
    if app.qr[0] != text:
        app.qr = (text, qr_matrix(text))
    m = app.qr[1]
    if not m:
        return False
    side = 144
    mod = side / len(m)
    x0, y0 = 180 - side / 2, 140 - side / 2
    c.d.rounded_rectangle([(x0 - 9) * c.k, (y0 - 9) * c.k, (x0 + side + 9) * c.k, (y0 + side + 9) * c.k],
                          radius=8 * c.k, fill=(242, 240, 233))
    for r, row in enumerate(m):
        for q, on in enumerate(row):
            if on:
                c.d.rectangle([(x0 + q * mod) * c.k, (y0 + r * mod) * c.k,
                               (x0 + (q + 1) * mod) * c.k - 1, (y0 + (r + 1) * mod) * c.k - 1], fill=BG)
    return True


def slide_wifi(app, now=None, arg=None):
    """Wi-Fi setup: join the hotspot, then open the page, then wait while it tries the network."""
    c = Canvas()
    w = app.wifi
    v = w.view()
    if v["stage"] == "joining":
        c.text(180, 150, "CONNECTING", "Light", 14, DIM, track=4)
        c.text(180, 190, (v["ssid"] or "SAVED WI-FI")[:22], "Light", 22, FG)
        return c.image()
    if v["stage"] == "open":
        url = f"http://{HOTSPOT_IP}:{app.cfg.get('web', {}).get('port', 8080)}/wifi"
        c.text(180, 42, "OPEN SETUP", "Regular", 12, ORANGE, track=3)
        draw_qr(c, app, url)
        c.text(180, 246, url.replace("http://", ""), "Light", 17, FG)
        c.text(180, 272, "CHOOSE YOUR WI-FI ON THE PHONE", "Light", 10, DIM, track=1.5)
        return c.image()
    c.text(180, 42, "SCAN TO SET UP", "Regular", 12, ORANGE, track=3)
    draw_qr(c, app, w.join_qr())
    c.text(180, 246, w.hotspot_ssid, "Light", 17, FG)
    c.text(180, 270, f"PASSWORD  {w.hotspot_password}", "Light", 13, DIM, track=1)
    if v["error"]:
        c.text(180, 298, "COULD NOT JOIN, TRY AGAIN", "Regular", 10, DOWN, track=1.5)
    return c.image()


def slide_system(app, now, arg=None):
    """Where to find the control page: a QR code to scan with the phone, the address, a few vitals."""
    c = Canvas()
    if time.time() - app.ip_t > 60:
        app.ip, app.ip_t = local_ip(), time.time()
    port = app.cfg.get("web", {}).get("port", 8080)
    url = f"http://{app.ip}:{port}" if app.ip else None
    if not (url and draw_qr(c, app, url)):
        c.text(180, 140, "NO NETWORK" if not url else url.replace("http://", ""), "Light", 16, DIM, track=3)
    c.text(180, 246, f"{app.ip}:{port}" if app.ip else "", "Light", 17, FG)
    c.text(180, 270, f"{socket.gethostname()}.local:{port}".upper(), "Light", 11, DIM, track=2)
    try:
        temp = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        vit = f"CPU {temp:.0f}°"
    except Exception:
        vit = ""
    up = int(time.time() - START)
    vit += f"   UP {up // 86400}D {up % 86400 // 3600}H" if up >= 86400 else f"   UP {up // 3600}H {up % 3600 // 60}M"
    c.text(180, 298, vit.strip(), "Light", 12, FAINT, track=3)
    return c.image()


def compass(deg):
    return ["N", "NE", "E", "SE", "S", "SW", "W", "NW"][int((deg + 22.5) // 45) % 8]


def slide_wind(app, now, arg=None):
    w = app.data.weather
    if not w:
        return placeholder()
    c = Canvas()
    c.text(180, 86, "WIND", "Light", 15, DIM, track=6)
    c.text(180, 160, f"{round(w['wind'])}", "Thin", 112)
    c.text(180, 222, f"KM/H  ·  {compass(w['wind_dir'])}", "Light", 14, DIM, track=4)
    a = math.radians(w["wind_dir"] + 180 - 90)                       # arrow points where the wind goes
    cx, cy, r = 180, 180, 167
    c.circle(cx + math.cos(a) * r, cy + math.sin(a) * r, 3, fill=FG)
    c.text(180, 276, f"HUMIDITY {round(w['humidity'])}%", "Light", 15, DIM, track=3)
    return c.image()


def aqi_word(v):
    for lim, w in ((20, "GOOD"), (40, "FAIR"), (60, "MODERATE"), (80, "POOR"), (100, "VERY POOR")):
        if v <= lim:
            return w
    return "EXTREMELY POOR"


def slide_aqi(app, now, arg=None):
    a = app.data.aqi
    if not a:
        return placeholder()
    c = Canvas()
    c.text(180, 86, "AIR QUALITY", "Light", 15, DIM, track=6)
    c.text(180, 168, f"{round(a['aqi'])}", "Thin", 120)
    c.text(180, 240, aqi_word(a["aqi"]), "Light", 15, DIM, track=5)
    c.text(180, 282, f"PM2.5  {a['pm25']:.0f}     PM10  {a['pm10']:.0f}", "Light", 14, DIM)
    c.arc(180, 180, 167, 0, 360, FAINT, 1)
    c.arc(180, 180, 167, -90, -90 + 360 * min(1, a["aqi"] / 100), FG, 1.6)
    return c.image()


def slide_sun(app, now, arg=None):
    rise, sset = app.data.sun_times(now)
    if not rise:
        return placeholder()
    c = Canvas()
    day = rise <= now < sset
    total = (sset - rise).total_seconds()
    frac = max(0, min(1, (now - rise).total_seconds() / total)) if day else 0
    c.arc(180, 180, 167, 180, 360, FAINT, 1)                           # day arc, horizon to horizon
    if day:
        c.arc(180, 180, 167, 180, 180 + 180 * frac, FG, 1.6)
        a = math.radians(180 + 180 * frac)
        c.sun(180 + math.cos(a) * 167, 180 + math.sin(a) * 167, 7, lw=1.4)
    else:
        c.moon(180, 70, 14)
    c.text(180, 150, "SUNRISE" if not day else "SUNSET", "Light", 14, DIM, track=5)
    c.text(180, 196, (rise if not day and now < rise else sset).strftime("%H:%M"), "Thin", 84)
    left = (sset - now) if day else (rise + timedelta(days=1 if now > sset else 0) - now)
    h, m = int(left.total_seconds() // 3600), int(left.total_seconds() % 3600 // 60)
    c.text(180, 258, f"{h}H {m:02d}M {'OF DAYLIGHT LEFT' if day else 'TO SUNRISE'}", "Light", 13, DIM, track=3)
    c.text(180, 290, f"↑ {rise.strftime('%H:%M')}     ↓ {sset.strftime('%H:%M')}", "Light", 14, DIM)
    return c.image()


def slide_moon(app, now, arg=None):
    p = moon_phase(now.astimezone(timezone.utc))
    ill = (1 - math.cos(2 * math.pi * p)) / 2
    c = Canvas()
    c.moon_phase(180, 150, 72, p)
    c.text(180, 262, moon_name(p), "Light", 15, DIM, track=5)
    c.text(180, 292, f"{round(ill * 100)}% ILLUMINATED", "Light", 13, DIM, track=3)
    return c.image()


def prayer_events(app):
    ev = []
    with app.data.lock:
        for day in sorted(app.data.prayer):
            ev += [(dt, n) for n, dt in app.data.prayer[day].items()]
    return sorted(ev)


def slide_prayer(app, now, arg=None):
    ev = prayer_events(app)
    nxt = next(((dt, n) for dt, n in ev if dt > now), None)
    if not nxt:
        return placeholder()
    c = Canvas()
    dt, name = nxt
    prev = max((e for e in ev if e[0] <= now), default=None, key=lambda e: e[0])
    start = prev[0] if prev else dt - timedelta(hours=6)
    frac = max(0, min(1, (now - start).total_seconds() / max(1, (dt - start).total_seconds())))
    c.arc(180, 180, 167, 0, 360, FAINT, 1)
    c.arc(180, 180, 167, -90, -90 + 360 * frac, FG, 1.6)
    secs = int((dt - now).total_seconds())
    h, m = secs // 3600, (secs % 3600) // 60
    c.text(180, 104, name.upper(), "Regular", 18, DIM, track=8)
    if h:
        c.text(180, 182, f"{h}:{m:02d}", "Thin", 110)
        c.text(180, 248, "HOURS", "Light", 12, DIM, track=5)
    else:
        c.text(180, 182, f"{max(m, 1)}", "Thin", 120)
        c.text(180, 248, "MINUTES", "Light", 12, DIM, track=5)
    c.text(180, 284, dt.strftime("%H:%M"), "Light", 17, DIM)
    return c.image()


def slide_worldclock(app, now, arg=None):
    cities = app.st.get("worldclock", [])[:3]
    c = Canvas()
    if not cities:
        return placeholder()
    ys = {1: [180], 2: [140, 220], 3: [112, 180, 248]}[len(cities)]
    for (city, y) in zip(cities, ys):
        try:
            t = now.astimezone(ZoneInfo(city["tz"]))
        except Exception:
            continue
        off = round((t.utcoffset() - now.utcoffset()).total_seconds() / 3600)
        c.text(84, y - 8, city["name"], "Light", 13, DIM, anchor="lm", track=4)
        c.text(84, y + 10, f"{off:+d} H" if off else "SAME", "Light", 10, FAINT, anchor="lm", track=3)
        c.text(276, y, t.strftime("%H:%M"), "Thin", 40, anchor="rm")
    return c.image()


def slide_countdown(app, now, arg=None):
    cd = app.st.get("countdown", {})
    try:
        target = date.fromisoformat(cd.get("date", ""))
    except Exception:
        return placeholder()
    days = (target - now.date()).days
    c = Canvas()
    c.text(180, 92, cd.get("label", "").upper()[:16], "Light", 15, DIM, track=6)
    c.text(180, 178, str(abs(days)), "Thin", 140)
    c.text(180, 262, "DAYS TO GO" if days > 0 else ("TODAY" if days == 0 else "DAYS AGO"), "Light", 14, DIM, track=5)
    c.text(180, 292, target.strftime("%-d %B %Y").upper(), "Light", 13, FAINT, track=3)
    return c.image()


def slide_progress(app, now, arg=None):
    c = Canvas()
    yday = now.timetuple().tm_yday
    ylen = 366 if (now.year % 4 == 0 and (now.year % 100 or not now.year % 400)) else 365
    import calendar
    mlen = calendar.monthrange(now.year, now.month)[1]
    rows = [("DAY", (now.hour * 60 + now.minute) / 1440), ("MONTH", (now.day - 1 + now.hour / 24) / mlen),
            ("YEAR", (yday - 1 + now.hour / 24) / ylen)]
    for i, (label, f) in enumerate(rows):
        y = 112 + i * 68
        c.text(84, y - 18, label, "Light", 12, DIM, anchor="lm", track=5)
        c.text(276, y - 18, f"{f * 100:.0f}%", "Light", 14, FG, anchor="rm")
        c.line([(84, y + 4), (276, y + 4)], FAINT, 2)
        c.line([(84, y + 4), (84 + 192 * f, y + 4)], FG, 2)
    return c.image()


# --------------------------------------------------------------------------- photo
class Photos:
    EXT = {".jpg", ".jpeg", ".png", ".webp", ".bmp"}

    def __init__(self, cfg):
        self.folder = Path(cfg["photos"]["folder"]).expanduser()
        self.folder.mkdir(parents=True, exist_ok=True)
        self.shuffle = cfg["photos"].get("shuffle", True)
        self.queue, self.current, self.current_name, self.pinned = [], None, "", None

    def files(self):
        return [p for p in sorted(self.folder.rglob("*"))
                if p.suffix.lower() in self.EXT and p.is_file()
                and not any(part.startswith(".") for part in p.relative_to(self.folder).parts)]   # skips .thumbs/

    def load(self, p):
        try:
            im = Image.open(p)
            im.draft("RGB", (S * 2, S * 2))
            im = ImageOps.exif_transpose(im).convert("RGB")
            self.current = ImageOps.fit(im, (S, S), Image.LANCZOS)
            self.current_name = p.name
            return True
        except Exception as e:
            print(f"[photo] {p.name}: {e!r}", flush=True)
            return False

    def next(self):
        if self.pinned is not None:                      # chosen on the web page: shown on the next photo slide
            p, self.pinned = self.pinned, None
            if self.load(p):
                return True
        if not self.queue:
            self.queue = self.files()
            if self.shuffle:
                random.shuffle(self.queue)
        while self.queue:
            p = self.queue.pop()
            if p.exists() and self.load(p):
                return True
        return False

    def show(self, p):
        """Show this photo now (the web page then jumps to the photo slide)."""
        self.pinned = Path(p)
        return Path(p).is_file()

    def add_next(self, p):
        self.queue.append(Path(p))                       # pop() takes from the end: a fresh upload comes up next

    def forget(self, p):
        self.queue = [q for q in self.queue if q != Path(p)]


# --------------------------------------------------------------------------- radar
def fmt_alt(alt):
    if alt is None:
        return ""
    return f"FL{int(alt / 100):03d}" if alt >= 18000 else f"{int(alt):,} ft"


def slide_radar(app, now, arg=None):
    cfg, data = app.cfg, app.data
    R, cx, cy, rad = 168, 180, 180, data.area[2]
    scale = R / rad
    c = Canvas()
    for f in (1 / 3, 2 / 3, 1):
        c.circle(cx, cy, R * f, outline=FAINT, width=1)
    c.text(cx, cy - R + 14, "N", "Light", 11, DIM)
    c.circle(cx, cy, 2.5, outline=DIM, width=1)
    t_now = time.time()
    rows = []
    with data.lock:
        for h, p in data.planes.items():
            dt = t_now - p["t"]
            x, y = data.to_nm(p["lat"], p["lon"])
            if p["gs"]:
                d_nm = p["gs"] * dt / 3600
                a = math.radians(p["track"])
                x += d_nm * math.sin(a)
                y += d_nm * math.cos(a)
            dist = math.hypot(x, y)
            if dist <= rad * 1.02:
                rows.append((dist, h, p, x, y, list(data.trails.get(h, []))))
    rows.sort(key=lambda r: r[0])
    for i, (dist, h, p, x, y, trail) in enumerate(rows):
        px, py = cx + x * scale, cy - y * scale
        pts = [(cx + tx * scale, cy - ty * scale) for tx, ty in trail] + [(px, py)]
        for j in range(len(pts) - 1):
            g = int(30 + 90 * (j + 1) / (len(pts) - 1))
            c.line([pts[j], pts[j + 1]], (g, g, g + 4), 1)
        a = math.radians(p["track"])
        ux, uy = math.sin(a), -math.cos(a)
        L, Wd = 9.5, 4.6
        tip = (px + ux * L, py + uy * L)
        l = (px - ux * L * 0.6 - uy * Wd, py - uy * L * 0.6 + ux * Wd)
        r = (px - ux * L * 0.6 + uy * Wd, py - uy * L * 0.6 - ux * Wd)
        c.poly([tip, l, (px - ux * L * 0.2, py - uy * L * 0.2), r], FG if i == 0 else (205, 205, 210))
        if i == 0:
            c.circle(px, py, 12, outline=DIM, width=1)
    if rows:
        dist, h, p = rows[0][:3]
        c.text(cx, 300, p["name"] or h.upper(), "Regular", 17, FG, track=3)
        sub = "  ·  ".join(s for s in (fmt_alt(p["alt"]), f"{dist:.1f} nm") if s)
        c.text(cx, 322, sub, "Light", 13, DIM)
    else:
        c.text(cx, 306, "NO AIRCRAFT" if data.planes_ok else "···", "Light", 12, DIM, track=5)
    return c.image()


RENDERERS = {"photo": None, "clock": slide_clock, "date": slide_date, "hijri": slide_hijri, "arabic": slide_arabic,
             "occasions": slide_occasions, "price": slide_price, "weather": slide_weather, "forecast": slide_forecast,
             "uv": slide_uv, "wind": slide_wind, "aqi": slide_aqi, "sun": slide_sun, "moon": slide_moon,
             "prayer": slide_prayer, "worldclock": slide_worldclock, "countdown": slide_countdown,
             "calendar": slide_calendar, "progress": slide_progress, "system": slide_system}


# --------------------------------------------------------------------------- ambient light
class LightBH1750:
    """GY-302 / BH1750 on I2C. Continuous high-resolution mode, 1 lx steps, ~120 ms per reading."""

    def __init__(self, cfg):
        from smbus2 import SMBus
        c = cfg["light"]
        self.bus = SMBus(1)
        self.addr = int(c.get("address", 0x23))
        self.bus.write_byte(self.addr, 0x10)
        time.sleep(0.2)

    def lux(self):
        d = self.bus.read_i2c_block_data(self.addr, 0x10, 2)
        return ((d[0] << 8) | d[1]) / 1.2


class LightLDR:
    """LDR + capacitor charge-time reading, converted to a rough lux-like scale."""

    def __init__(self, cfg):
        import lgpio
        self.lgpio = lgpio
        c = cfg["light"]
        self.pin, self.bright_s, self.dark_s = c.get("pin", 17), c["bright_s"], c["dark_s"]
        self.h = lgpio.gpiochip_open(0)

    def lux(self):
        g, pin = self.lgpio, self.pin
        g.gpio_claim_output(self.h, pin, 0)
        time.sleep(0.02)
        g.gpio_free(self.h, pin)
        g.gpio_claim_input(self.h, pin)
        t0 = time.perf_counter()
        while g.gpio_read(self.h, pin) == 0 and time.perf_counter() - t0 < self.dark_s * 1.5:
            pass
        t = max(time.perf_counter() - t0, 1e-5)
        g.gpio_free(self.h, pin)
        f = (math.log(self.dark_s) - math.log(t)) / (math.log(self.dark_s) - math.log(self.bright_s))
        return 3 * (100 ** max(0.0, min(1.0, f)))                       # 3 lx dark .. 300 lx bright


def make_light(cfg):
    s = cfg["light"].get("sensor", "none")
    if s == "bh1750":
        return LightBH1750(cfg)
    if s == "ldr":
        return LightLDR(cfg)
    return None


def lux_to_brightness(lux, cfg, st):
    lo_lx, hi_lx = cfg["light"].get("dark_lux", 3), cfg["light"].get("bright_lux", 300)
    f = (math.log(max(lux, 0.1)) - math.log(lo_lx)) / (math.log(hi_lx) - math.log(lo_lx))
    f = max(0.0, min(1.0, f))
    lo, hi = st["min_brightness"], st["max_brightness"]
    return int(round(lo + (hi - lo) * f))


# --------------------------------------------------------------------------- gauge
class Gauge:
    """SG90 on a GPIO. Powered only while moving, then released, so it neither hums nor jitters."""

    def __init__(self, cfg):
        from gpiozero import AngularServo, LED
        g = cfg["gauge"]
        self.cfg = g
        self.servo = AngularServo(g["servo_pin"], min_angle=-90, max_angle=90,
                                  min_pulse_width=0.5 / 1000, max_pulse_width=2.5 / 1000)
        self.servo.detach()
        self.led = LED(g["backlight_pin"]) if g.get("backlight_pin", -1) >= 0 else None
        self.angle = None
        self.lock = threading.Lock()

    def move(self, angle):
        angle = max(-90, min(90, angle))
        if self.angle is not None and abs(angle - self.angle) < 1.0:
            return
        with self.lock:
            self.servo.angle = angle
            time.sleep(self.cfg.get("settle_seconds", 0.6) * (1 if self.angle is not None else 2))
            self.servo.detach()
            self.angle = angle

    def park(self):
        self.move(self.cfg.get("park_angle", self.cfg["min_angle"]))

    def show(self, frac):
        a0, a1 = self.cfg["min_angle"], self.cfg["max_angle"]
        self.move(a0 + (a1 - a0) * max(0.0, min(1.0, frac)))

    def backlight(self, on):
        if self.led:
            (self.led.on if on else self.led.off)()

    def sweep(self):
        for a in (self.cfg["min_angle"], self.cfg["max_angle"], (self.cfg["min_angle"] + self.cfg["max_angle"]) / 2):
            self.move(a)
            time.sleep(0.4)


class NullGauge:
    angle = None
    def move(self, a): self.angle = a
    def park(self): pass
    def show(self, f): self.angle = f
    def backlight(self, on): pass
    def sweep(self): pass


def gauge_value(app, metric, now):
    """Return (value, fraction 0..1) for the chosen metric, or None if unknown yet."""
    d = app.data
    m = next((m for m in GAUGE_METRICS if m["id"] == metric), None)
    if not m or metric == "off":
        return None
    v = None
    if metric == "temp" and d.weather:
        v = d.weather["temp"]
    elif metric == "humidity" and d.weather:
        v = d.weather["humidity"]
    elif metric == "wind" and d.weather:
        v = d.weather["wind"]
    elif metric == "aqi" and d.aqi:
        v = d.aqi["aqi"]
    elif metric == "price_change":
        tks = app.st["tickers"]
        q = d.quotes.get(tks[0]["symbol"]) if tks else None
        if q and q["prev"]:
            v = (q["price"] / q["prev"] - 1) * 100
    elif metric == "prayer":
        ev = prayer_events(app)
        nxt = next(((dt, n) for dt, n in ev if dt > now), None)
        prev = max((e for e in ev if e[0] <= now), default=None, key=lambda e: e[0])
        if nxt and prev:
            v = 100 * (now - prev[0]).total_seconds() / max(1, (nxt[0] - prev[0]).total_seconds())
    elif metric == "daylight":
        rise, sset = d.sun_times(now)
        if rise:
            v = 100 * max(0, min(1, (sset - now).total_seconds() / max(1, (sset - rise).total_seconds())))
    elif metric == "moon":
        v = 100 * (1 - math.cos(2 * math.pi * moon_phase(now.astimezone(timezone.utc)))) / 2
    elif metric == "planes":
        v = d.plane_count() if d.planes_ok else None
    elif metric == "uv" and d.weather and d.weather.get("uv") is not None:
        v = d.weather["uv"]
    elif metric == "lux":
        v = app.lux
    elif metric == "year":
        yday = now.timetuple().tm_yday
        v = 100 * (yday - 1 + now.hour / 24) / (366 if now.year % 4 == 0 and (now.year % 100 or not now.year % 400) else 365)
    elif metric == "hijri_month":
        v = 100 * d.hijri_for(now.date())["day"] / 30
    elif metric == "day":
        v = 100 * (now.hour * 60 + now.minute) / 1440
    elif metric == "cpu_temp":
        try:
            v = int(open("/sys/class/thermal/thermal_zone0/temp").read()) / 1000
        except Exception:
            v = None
    if v is None:
        return None
    return v, (v - m["min"]) / (m["max"] - m["min"])


# --------------------------------------------------------------------------- sound
class Sound:
    """Short tones rendered to WAV and played with aplay on the I2S amp. Nothing runs between sounds."""

    def __init__(self, cfg):
        self.cfg = cfg["sound"]
        self.rate = int(self.cfg.get("sample_rate", 22050))
        self.lock = threading.Lock()
        self.enabled = bool(self.cfg.get("enabled", True)) and shutil.which("aplay") is not None
        if not self.enabled:
            print("[sound] aplay not found or sound disabled; silent", flush=True)

    def _tone(self, notes, vol):
        """notes: list of (freq_hz, seconds, gap_seconds). Sine with a soft envelope."""
        out = bytearray()
        r = self.rate
        for f, dur, gap in notes:
            n = int(r * dur)
            att, rel = int(r * 0.008), int(r * min(0.08, dur / 2))
            for i in range(n):
                env = min(1.0, i / att if att else 1.0, (n - i) / rel if rel else 1.0)
                s = math.sin(2 * math.pi * f * i / r) * 0.8 + math.sin(4 * math.pi * f * i / r) * 0.15
                out += struct.pack("<h", int(s * env * vol * 32000))
            out += b"\0\0" * int(r * gap)
        return bytes(out)

    def _wav(self, pcm):
        pad = b"\0\0" * int(self.rate * 0.06)                # 60 ms of silence each side hides the amp's start click
        pcm = pad + pcm + pad
        hdr = struct.pack("<4sI4s4sIHHIIHH4sI", b"RIFF", 36 + len(pcm), b"WAVE", b"fmt ", 16, 1, 1,
                          self.rate, self.rate * 2, 2, 16, b"data", len(pcm))
        return hdr + pcm

    PATTERNS = {
        "key":    [(1900, 0.018, 0)],
        "hour":   [(880, 0.28, 0)],
        "prayer": [(660, 0.22, 0.08), (880, 0.34, 0)],
        "price":  [(988, 0.10, 0.05), (1319, 0.10, 0.05), (988, 0.16, 0)],
        "plane":  [(1200, 0.03, 0.06), (1200, 0.03, 0)],
        "boot":   [(523, 0.14, 0.04), (784, 0.22, 0)],
    }

    def play(self, event, volume):
        if not self.enabled or event not in self.PATTERNS:
            return
        vol = (max(1, min(100, volume)) / 100) ** 1.6                   # perceptual-ish volume curve
        data = self._wav(self._tone(self.PATTERNS[event], vol))

        def run():
            with self.lock:
                try:
                    subprocess.run(["aplay", "-q", "-D", self.cfg.get("device", "default"), "-"],
                                   input=data, timeout=5, check=False)
                except Exception as e:
                    print(f"[sound] {e!r}", flush=True)
        threading.Thread(target=run, daemon=True).start()


# --------------------------------------------------------------------------- hardware
class Screen:
    def __init__(self, cfg):
        from gc9b72 import GC9B72
        d = cfg["display"]
        self.lcd = GC9B72(dc=d["dc"], rst=d["rst"], bl=d["bl"], speed_hz=d["speed_hz"], rotation=d.get("rotation", 0))
        self.level = 80
        self.on(True)

    def show(self, im):
        self.lcd.show_image(im)

    def on(self, state):
        if state:
            try:
                self.lcd.reset()
                self.lcd.init()
            except Exception as e:
                print(f"[display] re-init failed: {e!r}", flush=True)
            self.lcd.backlight_pwm(self.level)
        else:
            self.lcd.backlight_pwm(0)
            self.lcd.backlight(False)

    def set_level(self, pct):
        if pct != self.level:
            self.level = pct
            self.lcd.backlight_pwm(pct)

    def close(self):
        self.lcd.close()


class NullScreen:
    level = 80
    def show(self, im): pass
    def set_level(self, pct): self.level = pct
    def on(self, state): pass
    def close(self): pass


def setup_buttons(cfg, events):
    from gpiozero import Button
    b = cfg["buttons"]
    a = Button(b["cycle"], pull_up=True, bounce_time=0.03, hold_time=b.get("long_press_seconds", 1.5))
    r = Button(b["radar"], pull_up=True, bounce_time=0.03)
    held = {"a": False}

    def a_held():
        held["a"] = True
        events.put("A_long")

    def a_released():
        if not held["a"]:
            events.put("A_short")
        held["a"] = False

    a.when_held, a.when_released = a_held, a_released
    r.when_pressed = lambda: events.put("B")
    return a, r


# --------------------------------------------------------------------------- app
class App:
    def __init__(self, cfg, settings, screen, events, gauge=None, sound=None, light=None):
        self.cfg, self.settings, self.screen, self.events = cfg, settings, screen, events
        self.tz = ZoneInfo(cfg["location"]["timezone"])
        self.st = settings.get()
        self.st_version = settings.version
        self.data = Data(cfg, settings, self.tz)
        self.photos = Photos(cfg)
        self.gauge = gauge or NullGauge()
        self.sound = sound
        self.light = light
        self.lux, self.light_t, self.brightness = None, 0.0, self.st["display"]["brightness"]
        self.light_failed = False
        self.slides, self.idx, self.slide_t = [], -1, 0.0
        self.radar, self.radar_t = False, 0.0
        self.override, self.override_sched = None, None
        self.asleep = False
        self.last_img, self.last_crc = None, None
        self.gauge_t, self.gauge_value = 0.0, None
        self.last_hour, self.prayer_done, self.price_alerted = None, set(), {}
        self.ip, self.ip_t, self.qr = None, 0.0, (None, None)
        self.offline = False                                     # demo mode: no network fetches
        self.boot_until = 0.0                                    # System screen (QR to the web page) after start-up
        self.wifi = None                                         # WifiSetup: the hotspot takes over the screen
        self.joined_until = 0.0                                  # monotonic: keep the QR up after joining a network
        self.lock = threading.Lock()
        self.rebuild_slides()

    # -- settings
    def rebuild_slides(self):
        slides = []
        for s in self.st["screens"]:
            if not s["enabled"]:
                continue
            if s["id"] == "price":
                slides += [("price", i, s["seconds"]) for i in range(len(self.st["tickers"]))]
            else:
                slides.append((s["id"], 0, s["seconds"]))
        if not slides:
            slides = [("clock", 0, 10)]
        cur = self.slides[self.idx][:2] if self.slides and 0 <= self.idx < len(self.slides) else None
        self.slides = slides
        self.idx = next((i for i, s in enumerate(slides) if s[:2] == cur), -1) if cur else -1

    def apply_settings(self):
        if self.settings.version == self.st_version:
            return
        old_tickers = [t["symbol"] for t in self.st["tickers"]]
        self.st = self.settings.get()
        self.st_version = self.settings.version
        if [t["symbol"] for t in self.st["tickers"]] != old_tickers:
            with self.data.lock:
                self.data.kick.add("quotes")                     # fetch new tickers now, not in a minute
        self.rebuild_slides()
        self.data.set_area(radar_area(self.cfg, self.st))
        if not self.st["display"]["auto_brightness"]:
            self.set_brightness(self.st["display"]["brightness"])
        self.gauge.backlight(self.st["gauge"]["backlight"] and not self.asleep)
        self.gauge_t = 0                                         # re-evaluate the needle now
        self.last_crc = None

    def set_brightness(self, pct):
        self.brightness = pct
        self.screen.set_level(pct)

    # -- sleep logic
    def schedule_asleep(self, now):
        n = self.st["display"]
        if not n.get("night_enabled"):
            return False
        hm = lambda s: tuple(int(v) for v in s.split(":"))
        cur, a, b = (now.hour, now.minute), hm(n["sleep_at"]), hm(n["wake_at"])
        return (cur >= a or cur < b) if a > b else (a <= cur < b)

    def effective_asleep(self, now):
        sch = self.schedule_asleep(now)
        if self.override is not None:
            if self.override_sched != sch:
                self.override = None
            else:
                return self.override == "sleep"
        return sch

    def toggle_sleep(self, now):
        sch = self.schedule_asleep(now)
        self.override = "wake" if self.effective_asleep(now) else "sleep"
        self.override_sched = sch

    def quiet(self, now):
        s = self.st["sound"]
        hm = lambda t: tuple(int(v) for v in t.split(":"))
        cur, a, b = (now.hour, now.minute), hm(s["quiet_from"]), hm(s["quiet_to"])
        return (cur >= a or cur < b) if a > b else (a <= cur < b)

    def beep(self, event, now=None, force=False):
        s = self.st["sound"]
        if not self.sound or (not force and (s["muted"] or not s["events"].get(event))):
            return
        now = now or datetime.now(self.tz)
        if not force and event != "key" and self.quiet(now):
            return
        self.sound.play(event, s["volume"])

    # -- slide logic
    def advance(self, step=1):
        for _ in range(len(self.slides)):
            self.idx = (self.idx + step) % len(self.slides)
            kind = self.slides[self.idx][0]
            if kind == "photo" and not self.photos.next():
                continue
            self.slide_t = time.time()
            return

    def render(self, now):
        if self.wifi and self.wifi.active:
            return slide_wifi(self)
        if self.radar:
            return slide_radar(self, now)
        if self.boot_until:
            return slide_system(self, now)
        kind, arg, _ = self.slides[self.idx]
        if kind == "photo":
            return self.photos.current or placeholder()
        fn = RENDERERS.get(kind)
        try:
            return fn(self, now, arg) if fn else placeholder()
        except Exception as e:
            print(f"[slide {kind}] {e!r}", flush=True)
            return placeholder()

    def push(self, im, fade=False):
        if fade and self.last_img is not None:
            for i in range(1, 5):
                self.screen.show(Image.blend(self.last_img, im, i / 4))
        else:
            self.screen.show(im)
        self.last_img = im
        self.last_crc = zlib.crc32(im.tobytes())

    def handle(self, ev, now):
        if self.boot_until and ev in ("A_short", "B", "prev") or str(ev).startswith("show:"):
            self.boot_until = self.joined_until = 0
        if ev == "sleep":
            if not self.effective_asleep(now):
                self.toggle_sleep(now)
            return False
        if ev == "wake":
            if self.effective_asleep(now):
                self.toggle_sleep(now)
            return False
        if self.asleep:
            if ev in ("A_short", "B", "A_long"):
                self.toggle_sleep(now)
            return False
        if ev == "A_long":
            self.toggle_sleep(now)
        elif ev == "A_short":
            self.beep("key", now)
            if self.radar:
                self.radar = False
                self.data.radar_on.clear()
            else:
                self.advance()
            return True
        elif ev == "B":
            self.beep("key", now)
            self.set_radar(not self.radar)
            return True
        elif ev == "prev":
            if self.radar:
                self.set_radar(False)
            self.advance(-1)
            return True
        elif ev.startswith("show:"):                              # jump to a slide (from the web page)
            want = ev[5:]
            for i, sl in enumerate(self.slides):
                if f"{sl[0]}:{sl[1]}" == want or sl[0] == want:
                    if self.radar:
                        self.set_radar(False)
                    if sl[0] == "photo" and not self.photos.next():
                        return False
                    self.idx, self.slide_t = i, time.time()
                    return True
        return False

    def set_radar(self, on):
        self.radar = on
        self.radar_t = time.time()
        (self.data.radar_on.set if on else self.data.radar_on.clear)()

    # -- periodic jobs that are not slides
    def tick_gauge(self, now):
        if time.time() - self.gauge_t < 5:
            return
        self.gauge_t = time.time()
        metric = self.st["gauge"]["metric"]
        if self.asleep or metric == "off":
            self.gauge_value = None
            self.gauge.park()
            return
        gv = gauge_value(self, metric, now)
        if gv is None:
            return
        self.gauge_value = gv[0]
        threading.Thread(target=self.gauge.show, args=(gv[1],), daemon=True).start()

    def tick_light(self):
        if time.time() - self.light_t < self.cfg["light"].get("interval", 2):
            return
        self.light_t = time.time()
        lux = None
        if self.light:
            try:
                lux = self.light.lux()
            except Exception as e:
                if self.lux is not None or not self.light_failed:          # once per failure, not every reading
                    print(f"[light] {e!r}", flush=True)
                self.light_failed = True
        if lux is None:
            self.lux = None
        else:
            self.light_failed = False
            self.lux = lux if self.lux is None else self.lux * 0.6 + lux * 0.4
        if self.st["display"]["auto_brightness"]:
            # no sensor or no reading: full brightness, so the screen is never stuck dim
            self.set_brightness(100 if self.lux is None else lux_to_brightness(self.lux, self.cfg, self.st["display"]))

    def tick_events(self, now):
        # hourly tone
        if now.minute == 0 and self.last_hour != now.hour:
            self.last_hour = now.hour
            self.beep("hour", now)
        elif now.minute != 0:
            self.last_hour = None
        # prayer time
        for dt, name in prayer_events(self):
            key = dt.isoformat()
            if 0 <= (now - dt).total_seconds() < 90 and key not in self.prayer_done:
                self.prayer_done.add(key)
                self.beep("prayer", now)
        # price move past the threshold (once per direction per ticker per day)
        thr = self.st["sound"].get("price_threshold_pct", 2.0)
        for t in self.st["tickers"]:
            q = self.data.quotes.get(t["symbol"])
            if q and q["prev"]:
                chg = (q["price"] / q["prev"] - 1) * 100
                tag = (t["symbol"], now.date().isoformat(), chg > 0)
                if abs(chg) >= thr and tag not in self.price_alerted:
                    self.price_alerted[tag] = True
                    self.beep("price", now)
        # aircraft entering the inner ring (only while the radar is up)
        if self.data.new_plane.is_set():
            self.data.new_plane.clear()
            if self.radar:
                self.beep("plane", now)

    # -- called from the web server thread
    def action(self, name, arg=None):
        if name in ("next", "prev", "radar", "sleep", "wake", "toggle_sleep"):
            self.events.put({"next": "A_short", "prev": "prev", "radar": "B", "toggle_sleep": "A_long"}.get(name, name))
        elif name == "show" and arg:
            self.events.put(f"show:{arg}")
        elif name == "sound_test" and self.sound:
            self.sound.play(str(arg), self.st["sound"]["volume"])
        elif name == "gauge_test":
            def run():
                self.gauge.sweep()
                self.gauge_t = 0
            threading.Thread(target=run, daemon=True).start()
        elif name == "refresh":
            with self.data.lock:
                self.data.kick.update({"quotes", "weather", "aqi"})
        elif name == "wifi_setup":
            return bool(self.wifi and self.wifi.request_setup())
        else:
            return False
        return True

    def preview_png(self):
        im = self.last_img if (self.last_img is not None and not self.asleep) else Image.new("RGB", (S, S), BG)
        buf = io.BytesIO()
        im.save(buf, "PNG", optimize=False, compress_level=1)
        return buf.getvalue()

    def status(self):
        """For the web page."""
        kind, arg, secs = self.slides[self.idx] if 0 <= self.idx < len(self.slides) else ("", 0, 0)
        label = kind
        if kind == "price" and arg < len(self.st["tickers"]):
            label = f"price · {self.st['tickers'][arg]['label']}"
        if kind == "photo":
            label = f"photo · {self.photos.current_name}"
        if self.ip is None or time.time() - self.ip_t > 60:
            self.ip, self.ip_t = local_ip(), time.time()
        if self.boot_until and not self.radar:
            kind = label = "system"
        return {"slide": "radar" if self.radar else label, "slide_id": "radar" if self.radar else kind,
                "radar": self.radar, "asleep": self.asleep, "ip": self.ip,
                "lux": None if self.lux is None else round(self.lux, 1), "brightness": self.brightness,
                "gauge_value": None if self.gauge_value is None else round(self.gauge_value, 1),
                "gauge_angle": getattr(self.gauge, "angle", None), "uptime_s": int(time.time() - START),
                "planes": self.data.plane_count() if self.data.planes_ok else None,
                "photos": len(self.photos.files()), "quotes_ok": sorted(self.data.quotes),
                "weather_ok": self.data.weather is not None, "time": datetime.now(self.tz).strftime("%H:%M:%S")}

    def run(self):
        if not self.offline:
            threading.Thread(target=self.data.worker, daemon=True).start()
        self.advance()
        if self.st["display"].get("boot_info", True):
            self.boot_until = time.time() + 20
        self.beep("boot", force=not self.st["sound"]["muted"] and self.st["sound"]["events"].get("boot"))
        self.gauge.backlight(self.st["gauge"]["backlight"])
        try:
            while True:
                now = datetime.now(self.tz)
                self.apply_settings()
                changed = False
                while True:
                    try:
                        changed |= self.handle(self.events.get_nowait(), now)
                    except queue.Empty:
                        break
                if self.wifi and self.wifi.take_connected():
                    self.action("refresh")
                    self.ip_t, self.joined_until = 0.0, time.monotonic() + 180
                    changed = True
                if time.monotonic() < self.joined_until:             # the clock may jump now, so not boot_until alone
                    self.boot_until = time.time() + 5
                asleep = self.effective_asleep(now) and not (self.wifi and self.wifi.active)
                if asleep != self.asleep:
                    self.asleep = asleep
                    self.screen.on(not asleep)
                    self.gauge.backlight(self.st["gauge"]["backlight"] and not asleep)
                    self.gauge_t = 0
                    if not asleep:
                        self.last_crc = None
                self.tick_gauge(now)
                if asleep:
                    time.sleep(0.15)
                    continue
                self.tick_light()
                self.tick_events(now)
                tmo = self.cfg["radar"].get("timeout_minutes", 0)
                if self.radar and tmo and time.time() - self.radar_t > tmo * 60:
                    self.set_radar(False)
                    changed = True
                if self.boot_until and time.time() > self.boot_until:
                    self.boot_until, self.slide_t = 0, time.time()
                    changed = True
                if not self.radar and not self.boot_until and time.time() - self.slide_t > self.slides[self.idx][2]:
                    self.advance()
                    changed = True
                t0 = time.time()
                im = self.render(now)
                if changed:
                    self.push(im, fade=True)
                elif zlib.crc32(im.tobytes()) != self.last_crc:
                    self.push(im)
                period = 0.1 if self.radar else 0.5
                time.sleep(max(0.01, period - (time.time() - t0)))
        finally:
            self.data.stop.set()


# --------------------------------------------------------------------------- demo / preview
def demo_fill(app):
    d, tz = app.data, app.tz
    now = datetime.now(tz).replace(hour=14, minute=42, second=20)
    random.seed(7)
    for i, t in enumerate(app.st.get("tickers", [])):
        base = 189.42 if i == 0 else 26.35
        walk = [base * (0.985 + 0.02 * (math.sin(j / 4 + i) + 1) / 2 + j * 0.0004) for j in range(36)]
        d.quotes[t["symbol"]] = {"price": walk[-1], "prev": base * (1.004 if i else 0.99), "closes": walk, "t": 0}
    day = now.date()
    d.weather = {"temp": 34.2, "code": 1, "day": True, "hi": 38.4, "lo": 24.1, "humidity": 18, "wind": 14.2,
                 "wind_dir": 320, "uv": 7.4, "uv_max": 9.1,
                 "sunrise": [f"{day}T05:52", f"{day + timedelta(1)}T05:53"],
                 "sunset": [f"{day}T17:21", f"{day + timedelta(1)}T17:20"],
                 "forecast": [{"date": str(day + timedelta(i)), "code": c, "hi": hi, "lo": lo}
                              for i, c, hi, lo in ((1, 0, 37.6, 23.2), (2, 3, 35.1, 22.8), (3, 61, 29.4, 19.9))],
                 "t": 0}
    app.lux = 140.0
    d.aqi = {"aqi": 46, "pm25": 21.4, "pm10": 88.0, "t": 0}
    times = {"Fajr": (4, 41), "Dhuhr": (11, 38), "Asr": (15, 4), "Maghrib": (17, 36), "Isha": (19, 6)}
    for off in (0, 1):
        dd = day + timedelta(days=off)
        d.prayer[dd.isoformat()] = {n: datetime(dd.year, dd.month, dd.day, h, m, tzinfo=tz) for n, (h, m) in times.items()}
    lat0, lon0, rad = d.area
    names = ["SVA1043", "FDB512", "UAE837", "SVA2217", "KNE334", "ETD401", "THY772", "SVA6", "QTR1129"]
    t = time.time()
    for i, nm in enumerate(names):
        r, th = rad * math.sqrt(random.random()) * 0.95, random.random() * 2 * math.pi
        x, y = r * math.sin(th), r * math.cos(th)
        trk, gs = random.random() * 360, random.uniform(220, 470)
        a = math.radians(trk)
        trail = [(x - gs / 3600 * 3 * k * math.sin(a), y - gs / 3600 * 3 * k * math.cos(a)) for k in range(7, 0, -1)]
        h = f"{i:06x}"
        lat, lon = lat0 + y / 60, lon0 + x / (60 * math.cos(math.radians(lat0)))
        d.planes[h] = {"lat": lat, "lon": lon, "track": trk, "gs": gs, "alt": random.choice([3200, 9800, 24000, 35000, 38000]),
                       "name": nm, "type": "", "t": t, "inner": False}
        d.trails[h] = deque(trail + [(x, y)], maxlen=8)
    d.planes_ok = True
    return now


def preview(cfg, settings, out):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    app = App(cfg, settings, NullScreen(), queue.Queue())
    now = demo_fill(app)
    mask = Image.new("L", (S * 2, S * 2), 0)
    ImageDraw.Draw(mask).ellipse([0, 0, S * 2 - 1, S * 2 - 1], fill=255)
    mask = mask.resize((S, S), Image.LANCZOS)

    def save(name, im):
        bg = Image.new("RGB", (S + 20, S + 20), (28, 28, 30))
        bg.paste(im, (10, 10), mask)
        bg.save(out / f"{name}.png")
        return bg

    shots = []
    photo = Image.new("RGB", (S, S))
    gx = ImageDraw.Draw(photo)
    for y in range(S):
        gx.line([(0, y), (S, y)], fill=(int(40 + 140 * y / S), int(70 + 90 * y / S), int(120 + 60 * (1 - y / S))))
    shots.append(save("photo", photo))
    for sc in SCREENS:
        k = sc["id"]
        if k == "photo":
            continue
        if k == "price":
            for i, t in enumerate(app.st["tickers"]):
                shots.append(save(f"price-{t['label'].lower()}", slide_price(app, now, i)))
        else:
            shots.append(save(k, RENDERERS[k](app, now, 0)))
    shots.append(save("radar", slide_radar(app, now)))
    cols = 4
    sheet = Image.new("RGB", (cols * (S + 20), ((len(shots) + cols - 1) // cols) * (S + 20)), (28, 28, 30))
    for i, s in enumerate(shots):
        sheet.paste(s, ((i % cols) * (S + 20), (i // cols) * (S + 20)))
    sheet.save(out / "contact-sheet.png")
    print("wrote", out, len(shots), "slides")


def load_config(path):
    """config.example.toml supplies any key the Pi's own config.toml does not have (yet)."""
    with open(HERE / "config.example.toml", "rb") as f:
        cfg = tomllib.load(f)
    if Path(path).exists():
        with open(path, "rb") as f:
            cfg = merge_settings(cfg, tomllib.load(f))
    return cfg


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--config", default=str(DATA / "config.toml"))
    ap.add_argument("--settings", default=str(DATA / "settings.json"))
    ap.add_argument("--preview", metavar="DIR", help="render all slides with demo data and exit")
    ap.add_argument("--light-test", action="store_true", help="print light readings and exit")
    ap.add_argument("--gauge-test", action="store_true", help="sweep the needle and exit")
    ap.add_argument("--sound-test", action="store_true", help="play every sound and exit")
    ap.add_argument("--demo", action="store_true",
                    help="no hardware, no network: demo data + the web page (try it on a laptop)")
    args = ap.parse_args()
    cfg = load_config(args.config)
    settings = Settings(args.settings)
    if args.preview:
        return preview(cfg, settings, args.preview)
    if args.light_test:
        lt = make_light(cfg)
        print("Ctrl+C to stop.")
        while True:
            lux = lt.lux()
            print(f"{lux:8.1f} lx  ->  brightness {lux_to_brightness(lux, cfg, settings.get()['display']):3d}%")
            time.sleep(0.5)
    if args.gauge_test:
        g = Gauge(cfg)
        print("min, max, middle, then 0..100 % in steps")
        g.sweep()
        for f in (0, 0.25, 0.5, 0.75, 1.0):
            g.show(f)
            time.sleep(0.8)
        return g.park()
    if args.sound_test:
        snd = Sound(cfg)
        for ev in Sound.PATTERNS:
            print(ev)
            snd.play(ev, 70)
            time.sleep(1.2)
        return
    if args.demo:
        cfg["photos"]["folder"] = str(HERE / "photos")
        app = App(cfg, settings, NullScreen(), queue.Queue())
        app.offline = True
        demo_fill(app)
        from web import serve
        threading.Thread(target=serve, args=(app,), daemon=True).start()
        print(f"demo: http://localhost:{cfg.get('web', {}).get('port', 8080)}", flush=True)
        return app.run()
    events = queue.Queue()
    screen = Screen(cfg)
    keep = setup_buttons(cfg, events)  # noqa: F841
    gauge = None
    if cfg.get("gauge", {}).get("enabled"):
        try:
            gauge = Gauge(cfg)
        except Exception as e:
            print(f"[gauge] disabled: {e!r}", flush=True)
    light = None
    try:
        light = make_light(cfg)
    except Exception as e:
        print(f"[light] disabled: {e!r}", flush=True)
    app = App(cfg, settings, screen, events, gauge=gauge, sound=Sound(cfg), light=light)
    app.wifi = WifiSetup(cfg)
    if app.wifi.enabled:
        threading.Thread(target=app.wifi.run, daemon=True).start()
    if cfg.get("web", {}).get("enabled", True):
        from web import serve
        threading.Thread(target=serve, args=(app,), daemon=True).start()
    try:
        app.run()
    except KeyboardInterrupt:
        pass
    finally:
        screen.on(False)
        screen.close()


if __name__ == "__main__":
    main()
