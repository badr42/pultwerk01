"""Pultwerk01 control page: open http://<pi>:8080 (or http://<hostname>.local:8080) on your phone.

Standard library only (no Flask): a threaded HTTP server with a small JSON API and one HTML page.

    GET  /                       the page (web_ui.html)
    GET  /api/state              settings + catalogue + live status
    GET  /api/status             live status only (polled every 2 s)
    GET  /api/screen.png         what the round screen shows right now
    POST /api/settings           partial settings JSON, merged and saved
    POST /api/action             {"name": "next"|"prev"|"radar"|"toggle_sleep"|"show"|"sound_test"|"gauge_test"|"refresh"|"show_photo", "arg": ...}
    GET  /api/photos             list of photos
    GET  /api/photos/thumb?name= 240 px JPEG thumbnail (cached in .thumbs/)
    POST /api/photos?name=       raw image bytes in the body (the page resizes to 1440 px before sending)
    DELETE /api/photos?name=     delete a photo

Set [web] password = "..." in config.toml to require it (HTTP basic auth, user name ignored).
"""
import base64
import hmac
import io
import json
import re
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from PIL import Image, ImageOps

HERE = Path(__file__).resolve().parent
MAX_UPLOAD = 30 * 1024 * 1024
NAME_OK = re.compile(r"[^A-Za-z0-9._ -]+")


def safe_name(name, folder):
    """Return a path inside folder for a client-supplied relative name, or None."""
    name = str(name or "").replace("\\", "/").strip("/")
    if not name or any(part in ("", ".", "..") or part.startswith(".") for part in name.split("/")):
        return None
    p = (folder / name).resolve()
    try:
        p.relative_to(folder.resolve())
    except ValueError:
        return None
    return p


def upload_name(name, folder):
    """Clean an uploaded file name, force an image extension, never overwrite."""
    stem = NAME_OK.sub("", Path(str(name or "photo")).stem).strip(" .") or "photo"
    ext = Path(str(name or "")).suffix.lower()
    if ext not in (".jpg", ".jpeg", ".png", ".webp"):
        ext = ".jpg"
    stem = stem[:60]
    p, i = folder / f"{stem}{ext}", 2
    while p.exists():
        p, i = folder / f"{stem}-{i}{ext}", i + 1
    return p


class Handler(BaseHTTPRequestHandler):
    app = None
    password = ""
    server_version = "pultwerk01"

    # -- plumbing
    def log_message(self, fmt, *args):            # keep the journal quiet; errors still print
        pass

    def _send(self, code, body=b"", ctype="application/json", extra=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False).encode()
        elif isinstance(body, str):
            body = body.encode()
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _authorised(self):
        if not self.password:
            return True
        h = self.headers.get("Authorization", "")
        if h.startswith("Basic "):
            try:
                pw = base64.b64decode(h[6:]).decode("utf-8", "replace").split(":", 1)[-1]
                if hmac.compare_digest(pw, self.password):
                    return True
            except Exception:
                pass
        self._send(401, {"error": "password required"}, extra={"WWW-Authenticate": 'Basic realm="Pultwerk01"'})
        return False

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n > MAX_UPLOAD:
            raise ValueError("too large")
        return self.rfile.read(n) if n else b""

    def _json(self):
        return json.loads(self._body() or b"{}")

    @property
    def folder(self):
        return self.app.photos.folder

    # -- routes
    def do_GET(self):
        if not self._authorised():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path in ("/", "/index.html"):
                return self._send(200, (HERE / "web_ui.html").read_bytes(), "text/html; charset=utf-8")
            if u.path == "/api/state":
                return self._send(200, {"settings": self.app.settings.get(), "catalogue": self.app.settings.catalogue(),
                                        "status": self.app.status(), "location": self.app.cfg.get("location", {})})
            if u.path == "/api/status":
                return self._send(200, self.app.status())
            if u.path == "/api/screen.png":
                return self._send(200, self.app.preview_png(), "image/png")
            if u.path == "/api/photos":
                files = self.app.photos.files()
                out = [{"name": str(p.relative_to(self.folder)), "size": p.stat().st_size, "mtime": int(p.stat().st_mtime)}
                       for p in files]
                out.sort(key=lambda f: -f["mtime"])
                return self._send(200, out)
            if u.path == "/api/photos/thumb":
                p = safe_name(q.get("name", [""])[0], self.folder)
                if not p or not p.is_file():
                    return self._send(404, {"error": "no such photo"})
                return self._send(200, self._thumb(p), "image/jpeg", {"Cache-Control": "max-age=86400"})
            return self._send(404, {"error": "not found"})
        except BrokenPipeError:
            pass
        except Exception as e:
            print(f"[web] GET {u.path}: {e!r}", flush=True)
            self._send(500, {"error": str(e)})

    do_HEAD = do_GET

    def do_POST(self):
        if not self._authorised():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        try:
            if u.path == "/api/settings":
                patch = self._json()
                if not isinstance(patch, dict):
                    return self._send(400, {"error": "expected an object"})
                return self._send(200, self.app.settings.update(patch))
            if u.path == "/api/action":
                a = self._json()
                name, arg = a.get("name"), a.get("arg")
                if name == "show_photo":
                    p = safe_name(arg, self.folder)
                    ok = bool(p and p.is_file() and self.app.photos.show(p))
                    if ok:
                        self.app.events.put("show:photo")
                    return self._send(200 if ok else 404, {"ok": ok})
                ok = self.app.action(name, arg)
                return self._send(200 if ok else 400, {"ok": ok})
            if u.path == "/api/photos":
                data = self._body()
                if not data:
                    return self._send(400, {"error": "empty upload"})
                try:
                    Image.open(io.BytesIO(data)).verify()       # reject anything that is not an image
                except Exception:
                    return self._send(415, {"error": "not an image this display can read (HEIC: share as JPEG)"})
                p = upload_name(q.get("name", ["photo.jpg"])[0], self.folder)
                tmp = p.with_name(f".{p.name}.part")
                tmp.write_bytes(data)
                tmp.replace(p)
                self.app.photos.add_next(p)
                return self._send(200, {"name": p.name, "size": len(data)})
            return self._send(404, {"error": "not found"})
        except ValueError as e:
            self._send(413 if "large" in str(e) else 400, {"error": str(e)})
        except Exception as e:
            print(f"[web] POST {u.path}: {e!r}", flush=True)
            self._send(500, {"error": str(e)})

    def do_DELETE(self):
        if not self._authorised():
            return
        u = urlparse(self.path)
        q = parse_qs(u.query)
        if u.path != "/api/photos":
            return self._send(404, {"error": "not found"})
        p = safe_name(q.get("name", [""])[0], self.folder)
        if not p or not p.is_file():
            return self._send(404, {"error": "no such photo"})
        p.unlink()
        t = self._thumb_path(p)
        if t.exists():
            t.unlink()
        self.app.photos.forget(p)
        return self._send(200, {"ok": True})

    # -- thumbnails
    def _thumb_path(self, p):
        rel = str(p.relative_to(self.folder)).replace("/", "__")
        return self.folder / ".thumbs" / f"{rel}.jpg"

    def _thumb(self, p):
        t = self._thumb_path(p)
        if t.exists() and t.stat().st_mtime >= p.stat().st_mtime:
            return t.read_bytes()
        im = Image.open(p)
        im.draft("RGB", (480, 480))
        im = ImageOps.fit(ImageOps.exif_transpose(im).convert("RGB"), (240, 240), Image.LANCZOS)
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=82)
        t.parent.mkdir(exist_ok=True)
        t.write_bytes(buf.getvalue())
        return buf.getvalue()


def serve(app):
    w = app.cfg.get("web", {})
    Handler.app = app
    Handler.password = str(w.get("password", "") or "")
    port = int(w.get("port", 8080))
    for attempt in range(30):                   # the port can still be held for a moment after a restart
        try:
            srv = ThreadingHTTPServer((w.get("bind", "0.0.0.0"), port), Handler)
            break
        except OSError as e:
            print(f"[web] port {port}: {e!r}; retrying", flush=True)
            time.sleep(2)
    else:
        return
    srv.daemon_threads = True
    print(f"[web] control page on port {port}", flush=True)
    srv.serve_forever()
