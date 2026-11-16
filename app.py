"""Discord Archive. Run with: python app.py (Python 3.10+, no extra packages)."""
import argparse
import hmac
import json
import os
from pathlib import Path
import secrets
import shutil
import sys
import threading
from http.cookies import SimpleCookie
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlsplit
import webbrowser
import zipfile

sys.dont_write_bytecode = True
from core.api import ApiError
from core.engine import Engine, ident
from core.instance import InstanceLock
from core.lifecycle import StopSignal, request_stop
from core.store import Store

BASE = Path(__file__).resolve().parent
MB = 1024 * 1024

ASSETS = {"/": "index.html", "/app.js": "app.js", "/style.css": "style.css"}
MIME = {".html": "text/html", ".js": "text/javascript", ".css": "text/css"}
CSP = ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
       "connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; form-action 'none'; object-src 'none'")
CHANNEL_FIELDS = ("id", "name", "type", "position", "parent_id", "topic", "last_message_id")


## ---- Server ----

class AppServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = False

    def __init__(self, port=0, data=None):
        data = data or BASE / "data"
        self.instance = InstanceLock(data)
        try:
            super().__init__(("127.0.0.1", port), Handler)
        except Exception:
            self.instance.close()
            raise
        # the secret is in the link the launcher opens, then lives in a cookie
        self.secret = secrets.token_urlsafe(32)
        self.origin = f"http://127.0.0.1:{self.server_port}"
        self.cookie_name = "archive_" + str(self.server_port)
        self.store = Store(data)
        self.engine = Engine(self.store, self.store.root.parent / "exports")
        self.bundle_lock = threading.Lock()

    def server_close(self):
        super().server_close()
        self.instance.close()


class Handler(BaseHTTPRequestHandler):
    server_version = "LocalArchive"

    def log_message(self, *args):
        pass

    ## ---- Replies ----

    def headers_common(self):
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Content-Security-Policy", CSP)

    def answer(self, value, code=200, cookie=False):
        raw = json.dumps(value, ensure_ascii=False).encode()
        self.send_response(code)
        self.headers_common()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        if cookie:
            self.send_header(
                "Set-Cookie",
                f"{self.server.cookie_name}={self.server.secret}; HttpOnly; SameSite=Strict; Path=/")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    ## ---- Access checks ----

    def allowed_host(self):
        return self.headers.get("Host") == f"127.0.0.1:{self.server.server_port}"

    def authorized(self):
        jar = SimpleCookie()
        try:
            jar.load(self.headers.get("Cookie", ""))
            morsel = jar.get(self.server.cookie_name)
            return bool(morsel and hmac.compare_digest(morsel.value, self.server.secret))
        except Exception:
            return False

    def guarded(self, mutation=False):
        if not self.allowed_host():
            self.answer({"error": "Invalid local host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if (origin and origin != self.server.origin) or (mutation and origin != self.server.origin):
            self.answer({"error": "This request did not come from the local app"}, 403)
            return False
        if not self.authorized():
            self.answer({"error": "Local session expired. Open the app again from its launcher link."}, 401)
            return False
        return True

    ## ---- GET ----

    def do_GET(self):
        try:
            self.route_get()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, ApiError) as exc:
            self.answer({"error": str(exc)}, 400)
        except Exception:
            self.answer({"error": "The app could not finish this request. Check file access and try again."}, 500)

    def route_get(self):
        parsed = urlsplit(self.path)
        path = unquote(parsed.path)
        query = {k: v[0] for k, v in parse_qs(parsed.query).items()}
        if not self.allowed_host():
            return self.answer({"error": "Invalid local host"}, 403)
        if not path.startswith("/api/"):
            return self.send_asset(path)
        if not self.guarded():
            return
        app, store = self.server.engine, self.server.store

        if path == "/api/state":
            return self.answer({
                "user": app.user,
                "jobs": store.jobs(),
                "active": app.active,
                "network": app.client.notice if app.client else "",
                "free_bytes": shutil.disk_usage(store.root).free,
                "database_bytes": store.path.stat().st_size,
                "export_path": str(app.root),
            })

        if path == "/api/history":
            offset = max(0, min(int(query.get("offset", 0)), 10000000))
            return self.answer(store.jobs(offset=offset))

        if path == "/api/guilds":
            return self.answer(app.require().guilds())

        if path == "/api/channels":
            gid = ident(query.get("guild", ""))
            channels = app.require().get(f"guilds/{gid}/channels")
            return self.answer([{k: c.get(k) for k in CHANNEL_FIELDS} for c in channels])

        if path == "/api/resolve":
            link = urlsplit(query.get("url", ""))
            bits = link.path.strip("/").split("/")
            if (link.hostname not in {"discord.com", "www.discord.com"}
                    or len(bits) not in (3, 4) or bits[0] != "channels"):
                raise ValueError("Paste a Discord channel or message link")
            channel = app.require().get(f"channels/{ident(bits[2])}")
            return self.answer({k: channel.get(k) for k in ("id", "name", "type", "guild_id", "parent_id")})

        if path == "/api/job":
            job = store.job(query.get("id", ""))
            with store.db() as db:
                events = [dict(r) for r in db.execute(
                    "SELECT at,text FROM events WHERE job=? ORDER BY seq DESC LIMIT 100", (job["id"],))]
                files = [dict(r) for r in db.execute(
                    "SELECT path,size FROM files WHERE job=? ORDER BY path LIMIT 10000", (job["id"],))]
            return self.answer({"job": job, "channels": store.channels(job["id"]), "events": events, "files": files})

        if path == "/api/messages":
            store.job(query.get("job", ""))
            return self.answer(store.search(query["job"], query))

        if path == "/api/file":
            return self.send_export_file(query)

        return self.answer({"error": "Not found"}, 404)

    def send_asset(self, path):
        if path not in ASSETS:
            return self.answer({"error": "Not found"}, 404)
        file = BASE / "web" / ASSETS[path]
        raw = file.read_bytes()
        self.send_response(200)
        self.headers_common()
        self.send_header("Content-Type", MIME[file.suffix] + "; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.end_headers()
        self.wfile.write(raw)

    def send_export_file(self, query):
        app, store = self.server.engine, self.server.store
        job = store.job(query.get("job", ""))
        relative = query.get("path", "")
        with store.db() as db:
            registered = db.execute("SELECT 1 FROM files WHERE job=? AND path=?", (job["id"], relative)).fetchone()
        root = (app.root / job["id"]).resolve()
        target = (root / relative).resolve()
        # only files the export itself wrote, and only inside its own folder
        if not registered or not target.is_relative_to(root) or not target.is_file():
            return self.answer({"error": "File not found"}, 404)
        name = target.name.encode("ascii", "replace").decode().replace('"', "_")
        self.send_response(200)
        self.headers_common()
        self.send_header("Content-Type", "application/octet-stream")
        self.send_header("Content-Disposition", f'attachment; filename="{name}"')
        self.send_header("Content-Length", str(target.stat().st_size))
        self.end_headers()
        with target.open("rb") as source:
            shutil.copyfileobj(source, self.wfile, 64 * 1024)

    ## ---- POST ----

    def do_POST(self):
        try:
            self.route_post()
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, ApiError, KeyError, TypeError) as exc:
            self.answer({"error": str(exc)}, 400)
        except Exception:
            self.answer({"error": "Action failed. Check disk space and file permissions, then try again."}, 500)

    def route_post(self):
        if self.path == "/api/bootstrap":
            key = self.headers.get("X-Archive-Key", "")
            if (not self.allowed_host() or self.headers.get("Origin") != self.server.origin
                    or not hmac.compare_digest(key, self.server.secret)):
                return self.answer({"error": "Invalid session link. Open the launcher again."}, 403)
            return self.answer({"ok": True}, cookie=True)
        if not self.guarded(mutation=True):
            return
        if self.headers.get("Content-Type", "").split(";")[0] != "application/json":
            return self.answer({"error": "JSON required"}, 415)
        length = int(self.headers.get("Content-Length", "0"))
        if not 0 < length <= MB:
            return self.answer({"error": "Request too large or empty"}, 413)
        data = json.loads(self.rfile.read(length))
        if not isinstance(data, dict):
            raise ValueError("Expected an object")
        app, store = self.server.engine, self.server.store

        if self.path == "/api/connect":
            return self.answer(app.connect(data.get("token", "")))

        if self.path == "/api/disconnect":
            app.disconnect()
            return self.answer({"ok": True})

        if self.path == "/api/jobs":
            return self.answer({"id": app.create(data)})

        if self.path == "/api/control":
            with self.server.bundle_lock:
                app.control(data.get("id", ""), data.get("action", ""))
            return self.answer({"ok": True})

        if self.path == "/api/open-folder":
            job = store.job(data.get("id", ""))
            folder = app.root / job["id"]
            if not folder.is_dir():
                raise ValueError("Files are written when the export finishes. Saved messages can already be read in Library.")
            if sys.platform == "win32":
                os.startfile(folder)
            return self.answer({"path": str(folder)})

        if self.path == "/api/bundle":
            return self.answer({"path": self.make_zip(store.job(data.get("id", "")))})

        if self.path == "/api/shutdown":
            app.stop.set()
            self.answer({"ok": True})
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        return self.answer({"error": "Not found"}, 404)

    def make_zip(self, job):
        app, store = self.server.engine, self.server.store
        if job["status"] not in ("completed", "partial") or app.active == job["id"]:
            raise ValueError("Wait for this export to finish before creating a ZIP")
        with self.server.bundle_lock:
            root = (app.root / job["id"]).resolve()
            with store.db() as db:
                files = db.execute(
                    "SELECT path,size FROM files WHERE job=? AND path<>'archive.zip'", (job["id"],)).fetchall()
            if not files:
                raise ValueError("No export files are available yet")
            if shutil.disk_usage(root).free < sum(f["size"] for f in files) + 50 * MB:
                raise ValueError("Not enough free space to create a ZIP. Use Open export folder instead.")
            target = root / "archive.zip"
            temp = root / "archive.zip.tmp"
            # stored, not compressed: quick, and each file is streamed from disk
            with zipfile.ZipFile(temp, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as bundle:
                for row in files:
                    source = (root / row["path"]).resolve()
                    if not source.is_relative_to(root) or not source.is_file():
                        raise ValueError("An export file was moved or removed. Check the export folder.")
                    bundle.write(source, row["path"])
            temp.replace(target)
            with store.db() as db:
                db.execute("INSERT OR REPLACE INTO files VALUES(?,?,?)",
                           (job["id"], "archive.zip", target.stat().st_size))
        return "archive.zip"


## ---- Start ----

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=0, help="local port, default picks a free one")
    parser.add_argument("--no-browser", action="store_true", help="do not open the browser")
    parser.add_argument("--data", type=Path, help="use another data folder")
    parser.add_argument("--stop", action="store_true", help="stop the running app (Windows)")
    args = parser.parse_args()
    data_dir = args.data or BASE / "data"

    if args.stop:
        stopped = request_stop(BASE, data_dir)
        print("Stop requested. Running exports pause and the app closes." if stopped
              else "Discord Archive is not running.")
        return

    server = AppServer(args.port, args.data)
    stop_signal = StopSignal(server, BASE, data_dir) if os.name == "nt" else None
    url = server.origin + "/#key=" + server.secret
    print("Discord Archive is running. Keep this window open.\n" + url, flush=True)
    print("Stop with Ctrl+C or Settings > Quit. The token is never saved.", flush=True)
    if not args.no_browser:
        webbrowser.open(url)
    try:
        server.serve_forever(poll_interval=0.3)
    except KeyboardInterrupt:
        pass
    finally:
        server.engine.close()
        server.server_close()
        if stop_signal:
            stop_signal.close()


if __name__ == "__main__":
    try:
        main()
    except (RuntimeError, OSError) as exc:
        sys.exit(str(exc))
