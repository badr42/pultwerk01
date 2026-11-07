"""Orbit: a two-key game for the round screen.

Your ship runs round a ring near the edge. Rocks fly out from the core in the middle; hold A to move
anticlockwise and B to move clockwise, and let them pass. Each rock that gets past counts a point, and it all
speeds up every 15 seconds. One hit ends the game.

The game knows nothing about keys or the display loop: the app calls step() with the seconds since the last
call and which keys are down, and frame() for a 360x360 picture. When nobody plays, a demo pilot flies it.
"""
import math
import random

from PIL import Image, ImageDraw

import retro

S = 360
C = S / 2
RING = 148                       # the ship's orbit radius
SHIP_R = 9                       # ship hit radius
TURN = 3.1                       # radians a second while a key is held (a half turn in about one second)
CORE = 14                        # rocks start here
STEP = 1 / 60                    # physics step, whatever the frame rate
LEVEL_S = 15                     # it speeds up this often

BG = (4, 6, 16)
TRACK = (34, 52, 84)
SHIP = retro.PHOSPHOR
ROCK = retro.AMBER
CORE_C = retro.ORANGE
SCORE_C = (70, 100, 130)
WHITE = (255, 255, 255)


def _ang(a):
    """An angle folded to -pi..pi."""
    return (a + math.pi) % (2 * math.pi) - math.pi


def _xy(a, r):
    return C + r * math.sin(a), C - r * math.cos(a)        # 0 is the top, angles grow clockwise


class Orbit:
    def __init__(self, best=0, seed=None):
        self.rnd = random.Random(seed)
        self.best = int(best)
        self.stars = [(self.rnd.uniform(0, S), self.rnd.uniform(0, S), self.rnd.uniform(0, 6.3)) for _ in range(70)]
        self.shapes = [[self.rnd.uniform(0.7, 1.15) for _ in range(7)] for _ in range(16)]
        self.demo()

    # -- state
    def _reset(self):
        self.t = 0.0                    # seconds in this game (or demo)
        self.ship = 0.0
        self.moving = 0
        self.rocks = []
        self.next_spawn = 1.0
        self.score = 0
        self.level = 0
        self.boom = None                # (seconds since the hit, x, y, sparks)
        self.over_t = 0.0
        self.new_best = False

    def demo(self):
        """The attract mode: a pilot plays a gentle game by itself."""
        self.state = "demo"
        self._reset()

    def start(self):
        self.state = "play"
        self._reset()
        self.next_spawn = 1.2

    # -- the rules
    def _speed(self):
        t = min(self.t, 25) if self.state == "demo" else self.t
        return min(195.0, 78 + 8.5 * t)                         # rock speed, px/s

    def _gap(self):
        t = min(self.t, 25) if self.state == "demo" else self.t
        return max(0.36, 1.0 - 0.011 * t)                       # seconds between spawns

    def _rock(self, a, v):
        self.rocks.append({"a": a % (2 * math.pi), "r": float(CORE), "v": v, "size": self.rnd.uniform(7, 11),
                           "shape": self.rnd.randrange(len(self.shapes)), "spin": self.rnd.uniform(-3, 3)})

    def _spawn(self):
        v, roll = self._speed(), self.rnd.random()
        if self.t > 12 and roll < 0.22:                          # a wall with one way through
            gap_at, gap = self.rnd.uniform(0, 2 * math.pi), max(0.75, 1.15 - 0.01 * self.t)
            n = 16
            for i in range(n):
                a = gap_at + gap / 2 + (2 * math.pi - gap) * i / (n - 1)
                self._rock(a, v * 0.72)
            return self._gap() + 1.3
        if self.t > 6 and roll < 0.45:                           # a pair, opposite each other
            a = self.rnd.uniform(0, 2 * math.pi)
            self._rock(a, v)
            self._rock(a + math.pi, v)
            return self._gap() * 1.2
        a = self.ship + self.rnd.uniform(-0.5, 0.5) if roll < 0.75 else self.rnd.uniform(0, 2 * math.pi)
        self._rock(a, v * self.rnd.uniform(0.9, 1.1))           # mostly at you, so standing still does not work
        return self._gap()

    def _pilot(self):
        """Demo steering: go where the fewest rocks are about to arrive."""
        def danger(a):
            d = 0.0
            for k in self.rocks:
                tt = (RING - k["r"]) / k["v"]
                if tt < -0.05:
                    continue
                d += math.exp(-(_ang(k["a"] - a) / 0.32) ** 2) / (0.15 + max(0, tt))
            return d
        look = TURN * 0.22
        costs = {m: danger(self.ship + m * look) + 0.02 * abs(m) for m in (-1, 0, 1)}
        return min(costs, key=costs.get)

    def step(self, dt, left=False, right=False):
        """Move the game on by dt seconds. Returns cues for the app: "level", "over"."""
        cues = []
        dt = min(dt, 0.25)                                       # a long stall does not teleport the rocks
        if self.state == "over":
            self.over_t += dt
        while dt > 1e-6:
            h = min(STEP, dt)
            dt -= h
            cues += self._tick(h, left, right)
        return cues

    def _tick(self, h, left, right):
        cues = []
        self.t += h
        if self.boom:
            self.boom = (self.boom[0] + h,) + self.boom[1:]
            if self.boom[0] > 1.1 and self.state == "demo":
                self.demo()                                      # the pilot crashed: start the demo over
            for k in self.rocks:
                k["r"] += k["v"] * h
            self.rocks = [k for k in self.rocks if k["r"] < 200]
            return cues
        if self.state == "over":
            return cues
        if self.state == "demo":
            self.moving = self._pilot()
        else:
            self.moving = (1 if right else 0) - (1 if left else 0)
            lvl = int(self.t // LEVEL_S)
            if lvl > self.level:
                self.level = lvl
                cues.append("level")
        self.ship = (self.ship + self.moving * TURN * h) % (2 * math.pi)
        self.next_spawn -= h
        if self.next_spawn <= 0:
            self.next_spawn = self._spawn()
        hit = None
        for k in self.rocks:
            before = k["r"]
            k["r"] += k["v"] * h
            if abs(k["r"] - RING) < k["size"] + 5 and abs(_ang(k["a"] - self.ship)) * RING < k["size"] + SHIP_R - 2:
                hit = k
            if before <= RING + 14 < k["r"] and self.state == "play":
                self.score += 1
        self.rocks = [k for k in self.rocks if k["r"] < 200]
        if hit:
            x, y = _xy(self.ship, RING)
            sparks = [(self.rnd.uniform(0, 2 * math.pi), self.rnd.uniform(40, 160)) for _ in range(28)]
            self.boom = (0.0, x, y, sparks)
            if self.state == "play":
                self.state, self.over_t = "over", 0.0
                self.new_best = self.score > self.best
                self.best = max(self.best, self.score)
                cues.append("over")
        return cues

    # -- drawing
    def frame(self):
        im = Image.new("RGB", (S, S), BG)
        d = ImageDraw.Draw(im)
        tw = self.t * 1.7
        for x, y, p in self.stars:
            v = 30 + int(25 * (1 + math.sin(tw + p)))
            d.point((x, y), fill=(v, v, v + 20))
        for i in range(60):                                      # the track
            x, y = _xy(i * math.pi / 30, RING)
            d.rectangle((x - 1, y - 1, x + 1, y + 1), fill=TRACK)
        pulse = 1 + 0.25 * math.sin(self.t * 6)
        for rr, c in ((CORE * 1.6 * pulse, (60, 22, 4)), (CORE * pulse, CORE_C), (CORE * 0.45, (255, 200, 120))):
            d.ellipse((C - rr, C - rr, C + rr, C + rr), fill=c)
        if self.state != "demo":
            retro.put_text(im, C, C + 26, str(self.score), 3, SCORE_C, "m")
        for k in self.rocks:
            self._draw_rock(d, k)
        if self.boom:
            self._draw_boom(d)
        elif self.state != "over":
            self._draw_ship(d)
        if self.state == "demo":
            self._overlay_demo(im, d)
        elif self.state == "over" and self.over_t > 0.7:
            self._overlay_over(im, d)
        return im

    def _draw_rock(self, d, k):
        grow = 0.45 + 0.55 * min(1.0, k["r"] / RING)             # small near the core, full size at the ring
        x, y = _xy(k["a"], k["r"])
        tx, ty = _xy(k["a"], max(CORE, k["r"] - 16))
        d.line((tx, ty, x, y), fill=(70, 46, 10), width=3)
        sz, sh, rot = k["size"] * grow, self.shapes[k["shape"]], k["spin"] * self.t
        pts = [(x + sz * m * math.cos(rot + i * 2 * math.pi / len(sh)), y + sz * m * math.sin(rot + i * 2 * math.pi / len(sh)))
               for i, m in enumerate(sh)]
        d.polygon(pts, fill=ROCK, outline=(255, 220, 140))

    def _draw_ship(self, d):
        a = self.ship
        nose = _xy(a, RING - 13)                                  # points at the core
        lft, rgt = _xy(a - 0.085, RING + 8), _xy(a + 0.085, RING + 8)
        if self.moving:                                          # a side thruster on the trailing side
            fx, fy = _xy(a - self.moving * 0.11, RING + 2)
            f = 3 + 2 * (int(self.t * 20) % 2)
            d.ellipse((fx - f, fy - f, fx + f, fy + f), fill=retro.ORANGE)
        d.polygon([nose, lft, _xy(a, RING + 3), rgt], fill=SHIP, outline=(200, 255, 220))

    def _draw_boom(self, d):
        e, x, y, sparks = self.boom
        if e < 0.12:
            r = 30 + 200 * e
            d.ellipse((x - r, y - r, x + r, y + r), fill=WHITE)
        k = max(0.0, 1 - e / 1.1)
        for a, v in sparks:
            px, py = x + math.cos(a) * v * e, y + math.sin(a) * v * e
            c = (255, int(120 + 100 * k), int(40 * k))
            s = 1 + 2 * k
            d.rectangle((px - s, py - s, px + s, py + s), fill=tuple(int(ch * k) for ch in c))

    def _overlay_demo(self, im, d):
        retro.put_text(im, C, C - 82, "ORBIT", 6, retro.PHOSPHOR, "m")
        if int(self.t * 2) % 2 == 0:
            retro.put_text(im, C, C + 40, "PRESS B", 3, WHITE, "m")
        if self.best:
            retro.put_text(im, C, C + 72, f"BEST {self.best}", 2, SCORE_C, "m")

    def _overlay_over(self, im, d):
        d.ellipse((C - 112, C - 112, C + 112, C + 112), fill=(10, 12, 26))
        retro.put_text(im, C, C - 72, "GAME OVER", 3, retro.RED, "m")
        retro.put_text(im, C, C - 34, str(self.score), 7, WHITE, "m")
        if self.new_best:
            if int(self.over_t * 3) % 2 == 0:
                retro.put_text(im, C, C + 28, "NEW BEST", 3, retro.AMBER, "m")
        else:
            retro.put_text(im, C, C + 28, f"BEST {self.best}", 3, SCORE_C, "m")
        retro.put_text(im, C, C + 62, "B PLAYS AGAIN", 2, (150, 160, 180), "m")
