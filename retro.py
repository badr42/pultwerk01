"""Retro effects for Pultwerk01: the start-up sequence and the retro animation screen.

Everything is drawn in code, nothing is loaded from files. Each frame is worked out from the seconds since
the effect started, so a slow frame on the Pi skips ahead instead of slowing the effect down. Most effects
are drawn small and scaled up with NEAREST, which is both cheap and gives them chunky pixels.
"""
import math
import random

import numpy as np
from PIL import Image, ImageChops, ImageDraw

S = 360
C = S / 2
PHOSPHOR = (70, 255, 130)
AMBER = (255, 176, 40)
RED = (255, 84, 84)
ORANGE = (232, 89, 12)

# --------------------------------------------------------------------------- 5x7 bitmap font
_GLYPHS = {
    "A": " ### |#   #|#   #|#####|#   #|#   #|#   #", "B": "#### |#   #|#   #|#### |#   #|#   #|#### ",
    "C": " ### |#   #|#    |#    |#    |#   #| ### ", "D": "#### |#   #|#   #|#   #|#   #|#   #|#### ",
    "E": "#####|#    |#    |#### |#    |#    |#####", "F": "#####|#    |#    |#### |#    |#    |#    ",
    "G": " ### |#   #|#    |# ###|#   #|#   #| ####", "H": "#   #|#   #|#   #|#####|#   #|#   #|#   #",
    "I": " ### |  #  |  #  |  #  |  #  |  #  | ### ", "J": "  ###|   # |   # |   # |   # |#  # | ##  ",
    "K": "#   #|#  # |# #  |##   |# #  |#  # |#   #", "L": "#    |#    |#    |#    |#    |#    |#####",
    "M": "#   #|## ##|# # #|# # #|#   #|#   #|#   #", "N": "#   #|#   #|##  #|# # #|#  ##|#   #|#   #",
    "O": " ### |#   #|#   #|#   #|#   #|#   #| ### ", "P": "#### |#   #|#   #|#### |#    |#    |#    ",
    "Q": " ### |#   #|#   #|#   #|# # #|#  # | ## #", "R": "#### |#   #|#   #|#### |# #  |#  # |#   #",
    "S": " ####|#    |#    | ### |    #|    #|#### ", "T": "#####|  #  |  #  |  #  |  #  |  #  |  #  ",
    "U": "#   #|#   #|#   #|#   #|#   #|#   #| ### ", "V": "#   #|#   #|#   #|#   #|#   #| # # |  #  ",
    "W": "#   #|#   #|#   #|# # #|# # #|# # #| # # ", "X": "#   #|#   #| # # |  #  | # # |#   #|#   #",
    "Y": "#   #|#   #| # # |  #  |  #  |  #  |  #  ", "Z": "#####|    #|   # |  #  | #   |#    |#####",
    "0": " ### |#   #|#  ##|# # #|##  #|#   #| ### ", "1": "  #  | ##  |  #  |  #  |  #  |  #  | ### ",
    "2": " ### |#   #|    #|   # |  #  | #   |#####", "3": "#####|   # |  #  |   # |    #|#   #| ### ",
    "4": "   # |  ## | # # |#  # |#####|   # |   # ", "5": "#####|#    |#### |    #|    #|#   #| ### ",
    "6": "  ## | #   |#    |#### |#   #|#   #| ### ", "7": "#####|    #|   # |  #  | #   | #   | #   ",
    "8": " ### |#   #|#   #| ### |#   #|#   #| ### ", "9": " ### |#   #|#   #| ####|    #|   # | ##  ",
    ".": "     |     |     |     |     |     |  #  ", ":": "     |  #  |  #  |     |  #  |  #  |     ",
    "-": "     |     |     | ### |     |     |     ", "/": "    #|    #|   # |  #  | #   |#    |#    ",
    "_": "#####|#####|#####|#####|#####|#####|#####", " ": "     |     |     |     |     |     |     ",
}
_FONT = {ch: np.array([[c == "#" for c in row] for row in g.split("|")], bool) for ch, g in _GLYPHS.items()}


def bits(text):
    """Text in the 5x7 font as a bool array, one blank column between letters. "_" is a solid block."""
    cols = []
    for i, ch in enumerate(text.upper()):
        if i:
            cols.append(np.zeros((7, 1), bool))
        cols.append(_FONT.get(ch, _FONT[" "]))
    return np.hstack(cols) if cols else np.zeros((7, 0), bool)


def pixel_text(text, scale):
    """An "L" mask of the text in the bitmap font, each font pixel scale x scale."""
    b = bits(text).astype(np.uint8) * 255
    return Image.fromarray(b, "L").resize((b.shape[1] * scale, b.shape[0] * scale), Image.NEAREST)


def put_text(im, x, y, text, scale, color, anchor="l"):
    """Paste bitmap text with its top at y; anchor "l" puts its left edge at x, "m" centres it on x."""
    if not text:
        return 0
    m = pixel_text(text, scale)
    if anchor == "m":
        x -= m.width // 2
    im.paste(color, (int(x), int(y)), m)
    return m.width


# --------------------------------------------------------------------------- CRT look
_crt_cache = {}


def _crt_mask(strength):
    if strength not in _crt_cache:
        yy, xx = np.mgrid[0:S, 0:S]
        rows = np.where(yy % 3 == 2, 1 - 0.42 * strength, 1.0)
        r2 = ((xx - C + 0.5) ** 2 + (yy - C + 0.5) ** 2) / C ** 2
        vig = np.clip(1.08 - 0.5 * strength * r2, 0, 1)                # darker towards the rim, like curved glass
        m = (np.clip(rows * vig, 0, 1) * 255).astype(np.uint8)
        _crt_cache[strength] = Image.fromarray(m, "L").convert("RGB")
    return _crt_cache[strength]


def crt(im, strength=1.0):
    """Scanlines and a dark rim over a 360x360 RGB frame."""
    return ImageChops.multiply(im, _crt_mask(strength))


def ease_out(x):
    return 1 - (1 - max(0.0, min(1.0, x))) ** 3


def bounce(x):
    """0..1 with two small bounces at the end, like something dropped."""
    x = max(0.0, min(1.0, x))
    n, d = 7.5625, 2.75
    if x < 1 / d:
        return n * x * x
    if x < 2 / d:
        x -= 1.5 / d
        return n * x * x + 0.75
    if x < 2.5 / d:
        x -= 2.25 / d
        return n * x * x + 0.9375
    x -= 2.625 / d
    return n * x * x + 0.984375


def power_on(im, e):
    """Draw im as an old TV switching on, e seconds in. After about 0.9 s it is just im."""
    if e >= 0.9:
        return im
    out = Image.new("RGB", (S, S), (0, 0, 0))
    if e < 0.28:                                             # a bright line that opens into the picture
        h = max(3, int(S * ease_out(e / 0.28)))
        squeezed = im.resize((S, h), Image.BILINEAR)
        out.paste(ImageChops.add(squeezed, Image.new("RGB", squeezed.size, (60, 60, 60))), (0, (S - h) // 2))
        return crt(out)
    k = 0.72 + 0.28 * random.random() if e < 0.7 else 1.0     # then a short flicker
    return crt(Image.eval(im, lambda v: int(v * k)))


# --------------------------------------------------------------------------- start-up sequence
class BootSequence:
    """A: a CRT switching on. B: start-up checks typed out, with real results. C: the logo drops in on the chime.

    checks: list of (label, fn); fn() -> (value_text, ok) runs when its line appears, so it sees live state.
    cues: (seconds, name) the app acts on: "sweep" (gauge self-test) and "chime" (the boot sound)."""

    A_END, LINE_S, LOGO_DROP_S, HOLD_S = 1.0, 0.36, 0.7, 1.5
    BG = (4, 12, 7)

    def __init__(self, title, checks):
        self.title, self.checks = title.upper()[:20], checks
        self.results = {}
        self.b_end = self.A_END + 0.5 + self.LINE_S * len(checks) + 0.4
        self.land = self.b_end + self.LOGO_DROP_S * 0.36                   # first touch of bounce()
        self.length = self.b_end + self.LOGO_DROP_S + self.HOLD_S
        self.cues = [(self.A_END, "sweep"), (self.land - 0.15, "chime")]   # sound leaves ~0.15 s after it is queued
        self.logo = pixel_text("PULTWERK", 5)

    def due(self, t):
        """Cues whose time has come, each once."""
        out = [name for at, name in self.cues if t >= at]
        self.cues = [(at, name) for at, name in self.cues if t < at]
        return out

    def done(self, t):
        return t >= self.length

    def frame(self, t):
        if t < self.A_END:
            return self._power_on(t)
        if t < self.b_end:
            return crt(self._checks(t - self.A_END))
        return crt(self._logo(t - self.b_end))

    def _power_on(self, t):
        im = Image.new("RGB", (S, S), (0, 0, 0))
        d = ImageDraw.Draw(im)
        if t < 0.14:                                                         # a dot warms up
            r = 2 + 5 * t / 0.14
            d.ellipse([C - r * 2, C - r * 2, C + r * 2, C + r * 2], fill=(40, 70, 50))
            d.ellipse([C - r, C - r, C + r, C + r], fill=(235, 255, 240))
        elif t < 0.42:                                                       # into a line
            w = 6 + (S - 6) * ease_out((t - 0.14) / 0.28)
            d.rectangle([C - w / 2, C - 5, C + w / 2, C + 5], fill=(40, 90, 60))
            d.rectangle([C - w / 2, C - 1.5, C + w / 2, C + 1.5], fill=(245, 255, 248))
        elif t < 0.78:                                                       # the line opens up and cools
            f = ease_out((t - 0.42) / 0.36)
            h = 3 + (S - 3) * f
            col = tuple(int(a + (b - a) * f) for a, b in zip((235, 255, 240), self.BG))
            d.rectangle([0, C - h / 2, S, C + h / 2], fill=col)
        else:                                                                # and flickers once
            k = 1.0 if t > 0.92 else 1.8
            d.rectangle([0, 0, S, S], fill=tuple(min(255, int(v * k)) for v in self.BG))
        return crt(im)

    def _checks(self, t):
        im = Image.new("RGB", (S, S), self.BG)
        y0, step, x0 = 92, 21, 42                                         # 23 letters fit across
        put_text(im, C, y0, self.title, 2, PHOSPHOR, "m")
        shown = int(max(0, t - 0.5) / self.LINE_S)
        for i, (label, fn) in enumerate(self.checks[:shown + 1]):
            into = t - 0.5 - i * self.LINE_S
            if into < 0:
                break
            y = y0 + 30 + i * step
            head = f"{label[:8]} " + "." * max(1, 9 - len(label[:8]))
            typed = head[:int(len(head) * min(1, into / (self.LINE_S * 0.55)))]
            if into >= self.LINE_S * 0.8:
                if i not in self.results:
                    try:
                        self.results[i] = fn()
                    except Exception:
                        self.results[i] = ("ERR", False)
                value, ok = self.results[i]
                value = value[:22 - len(label[:8])]
                typed = typed[:22 - len(value)]                              # a long value eats the dots
                m = pixel_text(value, 2)
                im.paste(PHOSPHOR if ok else AMBER, (S - x0 - m.width, y), m)
            elif int(into * 8) % 2 == 0:
                put_text(im, x0 + len(typed) * 12, y, "_", 2, PHOSPHOR)
            put_text(im, x0, y, typed, 2, PHOSPHOR)
        return im

    def _logo(self, t):
        im = Image.new("RGB", (S, S), self.BG)
        top = -self.logo.height + (150 + self.logo.height) * bounce(t / self.LOGO_DROP_S)
        x = int(C - self.logo.width / 2)
        im.paste(AMBER, (x, int(top)), self.logo)
        lower = self.logo.crop((0, self.logo.height // 2, self.logo.width, self.logo.height))
        im.paste(ORANGE, (x, int(top) + self.logo.height // 2), lower)             # two-tone, like old box art
        if t > self.LOGO_DROP_S:
            w = put_text(im, C - 30, 222, "READY", 2, PHOSPHOR)
            if int(t * 3) % 2 == 0:
                put_text(im, C - 30 + w + 6, 222, "_", 2, PHOSPHOR)
        return im


# --------------------------------------------------------------------------- animations
class Scope:
    """Oscilloscope: Lissajous figures in green phosphor that fades slowly."""

    FIGURES = [(1, 2), (3, 2), (3, 4), (5, 4), (2, 3), (1, 3), (4, 5), (5, 6)]

    def __init__(self, rnd):
        self.rnd = rnd
        self.order = rnd.sample(self.FIGURES, len(self.FIGURES))
        self.ph = Image.new("L", (S, S), 0)
        self.fade = [int(v * 0.62) for v in range(256)]
        self.tint = (lambda v: int(v * 0.3), lambda v: v, lambda v: int(v * 0.55))
        g = Image.new("RGB", (S, S), (0, 0, 0))
        d = ImageDraw.Draw(g)
        for k in range(-4, 5):
            d.line([(C + k * 36, 20), (C + k * 36, S - 20)], fill=(10, 34, 18))
            d.line([(20, C + k * 36), (S - 20, C + k * 36)], fill=(10, 34, 18))
        d.line([(C, 10), (C, S - 10)], fill=(18, 60, 30))
        d.line([(10, C), (S - 10, C)], fill=(18, 60, 30))
        self.grid = g

    def frame(self, t):
        a, b = self.order[int(t / 6) % len(self.order)]
        n = 260
        u = np.linspace(0, 2 * math.pi, n)
        x = C + 138 * np.sin(a * u + t * 0.9)
        y = C + 138 * np.sin(b * u + t * 0.23)
        pts = list(zip(x.tolist(), y.tolist()))
        self.ph = self.ph.point(self.fade)
        d = ImageDraw.Draw(self.ph)
        d.line(pts, fill=110, width=6)
        d.line(pts, fill=255, width=2)
        rgb = Image.merge("RGB", [self.ph.point(f) for f in self.tint])
        return ImageChops.lighter(rgb, self.grid)


class Plasma:
    """Demoscene plasma: sine waves summed and run through a cycling palette."""

    N = 90

    def __init__(self, rnd):
        self.yy, self.xx = np.mgrid[0:self.N, 0:self.N].astype(np.float32)
        i = np.arange(256) / 256 * 2 * math.pi
        sh = rnd.random() * 6
        self.pal = np.stack([128 + 127 * np.sin(i + sh), 128 + 127 * np.sin(i + sh + 2.1),
                             128 + 127 * np.sin(i + sh + 4.2)], 1).astype(np.uint8)

    def frame(self, t):
        x, y, n = self.xx, self.yy, self.N
        cx, cy = n / 2 + n / 3 * math.sin(t * 0.4), n / 2 + n / 3 * math.cos(t * 0.31)
        v = (np.sin(x * 0.16 + t) + np.sin(y * 0.13 - t * 0.8) + np.sin((x + y) * 0.09 + t * 1.3)
             + np.sin(np.sqrt((x - cx) ** 2 + (y - cy) ** 2) * 0.22 - t * 1.6))
        idx = ((v + 4) * 32 + t * 40).astype(np.int32) & 255
        return Image.fromarray(self.pal[idx], "RGB").resize((S, S), Image.NEAREST)


class Starfield:
    """Flying through stars, with the speed surging now and then."""

    N = 230

    def __init__(self, rnd):
        self.g = np.random.default_rng(rnd.randrange(1 << 30))
        self.xy = self.g.uniform(-1, 1, (self.N, 2))
        self.z = self.g.uniform(0.05, 1, self.N)
        self.last = None

    def frame(self, t):
        dt = 0 if self.last is None else min(0.2, t - self.last)
        self.last = t
        speed = 0.32 + 0.28 * max(0, math.sin(t * 0.45)) ** 3 * 3
        z0 = self.z.copy()
        self.z -= speed * dt
        gone = self.z <= 0.03
        if gone.any():
            k = int(gone.sum())
            self.xy[gone] = self.g.uniform(-1, 1, (k, 2))
            self.z[gone] = z0[gone] = self.g.uniform(0.8, 1, k)
        im = Image.new("RGB", (S, S), (0, 0, 4))
        d = ImageDraw.Draw(im)
        f = 70
        tail = np.minimum(z0 + speed * 0.06, 1.2)
        for (x, y), z, zt in zip(self.xy.tolist(), self.z.tolist(), tail.tolist()):
            sx, sy = C + x / z * f, C + y / z * f
            tx, ty = C + x / zt * f, C + y / zt * f
            b = int(255 * min(1, (1.05 - z) ** 1.5))
            d.line([(tx, ty), (sx, sy)], fill=(int(b * 0.85), int(b * 0.9), b), width=2 if z < 0.3 else 1)
        return im


class CodeRain:
    """Columns of glyphs falling like an old green terminal gone strange."""

    CW, CH = 12, 16                                                 # cell: the 5x7 glyph doubled, plus a gap

    def __init__(self, rnd):
        self.g = np.random.default_rng(rnd.randrange(1 << 30))
        chars = "0123456789ABCDEFHKLMNPRSTUVXZ:-/"
        cells = []
        for ch in chars:
            b = np.kron(_FONT[ch], np.ones((2, 2), bool))[:, ::-1]  # mirrored, so it reads as unfamiliar
            cell = np.zeros((self.CH, self.CW), np.float32)
            cell[1:15, 1:11] = b
            cells.append(cell)
        self.glyphs = np.stack(cells)
        self.rows, self.cols = S // self.CH + 1, S // self.CW
        self.idx = self.g.integers(0, len(chars), (self.rows, self.cols))
        self.head = self.g.uniform(-self.rows, self.rows, self.cols)
        self.speed = self.g.uniform(6, 16, self.cols)
        self.trail = self.g.uniform(6, 16, self.cols)
        self.last = None

    def frame(self, t):
        dt = 0 if self.last is None else min(0.2, t - self.last)
        self.last = t
        self.head += self.speed * dt
        done = self.head - self.trail > self.rows
        self.head[done] = self.g.uniform(-12, -1, int(done.sum()))
        flip = self.g.random(self.idx.shape) < 0.04
        self.idx[flip] = self.g.integers(0, len(self.glyphs), int(flip.sum()))
        r = np.arange(self.rows)[:, None]
        behind = self.head[None, :] - r
        light = np.where(behind >= 0, np.clip(1 - behind / self.trail[None, :], 0, 1), 0)
        is_head = (behind >= 0) & (behind < 1)
        cells = self.glyphs[self.idx] * light[:, :, None, None]
        img = cells.transpose(0, 2, 1, 3).reshape(self.rows * self.CH, self.cols * self.CW)[:S, :S]
        head = np.kron(is_head, np.ones((self.CH, self.CW)))[:S, :S] * img
        g = np.clip(img * 255, 0, 255)
        rb = np.clip(head * 220 + img * 30, 0, 255)
        out = np.zeros((S, S, 3), np.uint8)
        w = img.shape[1]
        off = (S - w) // 2
        out[:img.shape[0], off:off + w, 0] = rb * 0.8
        out[:img.shape[0], off:off + w, 1] = g
        out[:img.shape[0], off:off + w, 2] = rb
        return Image.fromarray(out, "RGB")


class Bounce:
    """The logo drifting round the screen, bouncing off the round edge and changing colour each time."""

    COLOURS = [(255, 80, 80), (255, 190, 40), (90, 230, 120), (70, 190, 255), (190, 110, 255), (255, 110, 200)]
    WALL = 174

    def __init__(self, rnd):
        self.logo = pixel_text("PULTWERK", 3)
        a = rnd.random() * 2 * math.pi
        self.v = np.array([math.cos(a), math.sin(a)]) * 85
        self.p = np.array([0.0, 0.0])
        self.c = rnd.randrange(len(self.COLOURS))
        hw, hh = self.logo.width / 2, self.logo.height / 2
        self.corners = np.array([[-hw, -hh], [hw, -hh], [-hw, hh], [hw, hh]])
        self.last = None

    def frame(self, t):
        dt = 0 if self.last is None else min(0.2, t - self.last)
        self.last = t
        self.p += self.v * dt
        pts = self.corners + self.p
        dist = np.hypot(pts[:, 0], pts[:, 1])
        k = int(dist.argmax())
        if dist[k] > self.WALL:
            n = pts[k] / dist[k]
            if self.v @ n > 0:
                self.v -= 2 * (self.v @ n) * n
                self.c = (self.c + 1) % len(self.COLOURS)
            self.p -= n * (dist[k] - self.WALL)
        im = Image.new("RGB", (S, S), (0, 0, 0))
        ImageDraw.Draw(im).ellipse([C - self.WALL - 4, C - self.WALL - 4, C + self.WALL + 4, C + self.WALL + 4],
                                   outline=(26, 26, 30), width=1)
        x, y = C + self.p[0] - self.logo.width / 2, C + self.p[1] - self.logo.height / 2
        im.paste(self.COLOURS[self.c], (int(round(x)), int(round(y))), self.logo)
        return im


class Synthwave:
    """An 80s sunset: a striped sun behind mountains over a neon grid that rolls towards you."""

    HORIZON = 212

    def __init__(self, rnd):
        h = self.HORIZON
        bg = np.zeros((S, S, 3), np.float32)
        top, low = np.array([14, 2, 38]), np.array([120, 20, 110])
        f = (np.arange(h) / h)[:, None] ** 1.6
        bg[:h] = (top + (low - top) * f)[:, None, :]
        bg[h:] = (18, 0, 34)
        self.bg = Image.fromarray(bg.astype(np.uint8), "RGB")
        d = ImageDraw.Draw(self.bg)
        for _ in range(40):
            x, y = rnd.uniform(0, S), rnd.uniform(0, h * 0.7)
            d.point((x, y), fill=(200, 180, 255))
        r, cy = 84, h - 52
        sy = np.arange(2 * r)[:, None] / (2 * r)
        sun = np.zeros((2 * r, 2 * r, 3), np.float32) + np.array([255, 230, 90]) + (np.array([255, 60, 150]) - (255, 230, 90)) * sy
        self.sun = Image.fromarray(sun.astype(np.uint8), "RGB")
        self.sun_mask = Image.new("L", (2 * r, 2 * r), 0)
        ImageDraw.Draw(self.sun_mask).ellipse([0, 0, 2 * r - 1, 2 * r - 1], fill=255)
        self.sun_at, self.r = (int(C - r), int(cy - r)), r
        pts, x = [(0, h)], 0.0
        while x < S:
            x += rnd.uniform(18, 40)
            pts.append((x, h - rnd.uniform(8, 42) * (1 if int(x / 30) % 2 else 0.55)))
        pts += [(S, h), (S, h + 1), (0, h + 1)]
        self.mountains = pts

    def frame(self, t):
        h = self.HORIZON
        im = self.bg.copy()
        m = self.sun_mask.copy()
        d = ImageDraw.Draw(m)
        r = self.r
        for i in range(7):                                         # gaps in the lower half drift down
            y = r * 0.95 + ((i * 14 + t * 9) % 98)
            d.rectangle([0, y, 2 * r, y + 1.5 + i * 0.9], fill=0)
        im.paste(self.sun, self.sun_at, m)
        d = ImageDraw.Draw(im)
        d.polygon(self.mountains, fill=(28, 4, 48))
        d.line([(0, h), (S, h)], fill=(255, 90, 220), width=2)
        off = (t * 0.9) % 1
        for k in range(14):
            z = k + 1 - off
            y = h + 150 / z
            if y > S:
                continue
            g = int(80 + 175 * min(1, 2.4 / z))
            d.line([(0, y), (S, y)], fill=(g, int(g * 0.25), int(g * 0.85)), width=2 if z < 3 else 1)
        for i in range(-9, 10):
            d.line([(C + i * 9, h), (C + i * 70, S)], fill=(255, 60, 200), width=1)
        return im


ANIMATIONS = {"scope": Scope, "plasma": Plasma, "starfield": Starfield, "rain": CodeRain,
              "bounce": Bounce, "synthwave": Synthwave}


def make(anim_id, seed=None):
    return ANIMATIONS[anim_id](random.Random(seed))
