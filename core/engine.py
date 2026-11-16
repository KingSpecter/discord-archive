"""Job runner: finds the channels, pages through their history, then writes the export files."""
import json
import re
import shutil
import threading
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit
from urllib.request import Request

from .api import ApiError, Discord, Stopped, USER_AGENT
from .formats import FORMATS, write_export
from .store import now

# channel types with a message history; 15 and 16 are forum and media containers
READABLE = {0, 2, 5, 10, 11, 12, 13, 15, 16}
THREAD_PARENTS = {0, 5, 15, 16}
CONTAINERS = (15, 16)
DISCORD_EPOCH = 1420070400000
MB = 1024 * 1024
MIN_FREE = 50 * MB
CDN_HOSTS = {"cdn.discordapp.com", "media.discordapp.net"}

SCOPE_NOTE = ("Accessible messages only. New messages after the job's upper bound, deleted messages, "
              "voice audio and ephemeral messages are excluded.")


## ---- Options ----

def snowflake(moment):
    """Smallest Discord ID that could have been created at this time."""
    return str(max(0, int(moment.timestamp() * 1000) - DISCORD_EPOCH) << 22)


def ident(value):
    value = str(value)
    if not re.fullmatch(r"[0-9]{1,22}", value):
        raise ValueError("Invalid Discord ID")
    return value


def options(data):
    """Check what the page sent and turn it into the options saved with the job."""
    formats = list(dict.fromkeys(data.get("formats", ["md"])))
    if not formats or any(f not in FORMATS for f in formats):
        raise ValueError("Choose at least one supported export format")

    lower, upper = None, datetime.now(timezone.utc)
    if data.get("from"):
        lower = datetime.strptime(data["from"], "%Y-%m-%d").replace(tzinfo=timezone.utc)
    if data.get("to"):
        end = datetime.strptime(data["to"], "%Y-%m-%d").replace(tzinfo=timezone.utc) + timedelta(days=1)
        upper = min(upper, end)
    if lower and lower >= upper:
        raise ValueError("Start date must be before the end of the range and not in the future")

    channels = list(dict.fromkeys(ident(x) for x in data.get("channels", [])))
    guilds = list(dict.fromkeys(ident(x) for x in data.get("guilds", [])))
    if not channels and not guilds:
        raise ValueError("Select a channel or a whole server first")
    if len(channels) > 10000 or len(guilds) > 200:
        raise ValueError("Selection exceeds 10,000 channels or 200 servers per job")
    authors = [ident(x) for x in re.split(r"[,\s]+", data.get("authors", "").strip()) if x]

    return {
        "formats": formats,
        "channels": channels,
        "guilds": guilds,
        "lower": snowflake(lower) if lower else "0",
        "upper": snowflake(upper),
        "from": data.get("from", ""),
        "to": data.get("to", ""),
        "threads": bool(data.get("threads", True)),
        "media": bool(data.get("media", False)),
        "media_mb": max(1, min(int(data.get("media_mb", 25)), 500)),
        "budget_mb": max(1, min(int(data.get("budget_mb", 500)), 100000)),
        "authors": authors,
        "contains": str(data.get("contains", ""))[:500],
        "pinned": bool(data.get("pinned", False)),
        "exclude_bots": bool(data.get("exclude_bots", False)),
    }


def matches(msg, opts):
    author = msg.get("author") or {}
    return (int(opts["lower"]) <= int(msg["id"]) < int(opts["upper"])
            and (not opts["authors"] or author.get("id") in opts["authors"])
            and (not opts["contains"] or opts["contains"].casefold() in (msg.get("content") or "").casefold())
            and (not opts["pinned"] or msg.get("pinned"))
            and (not opts["exclude_bots"] or not author.get("bot")))


def allowed_attachment(link):
    """Only fetch attachments from Discord's own CDN, over https, with no surprises in the URL."""
    url = urlsplit(link)
    return (url.scheme == "https" and url.hostname in CDN_HOSTS and not url.username
            and url.port in (None, 443) and url.path.startswith("/attachments/"))


## ---- Engine ----

class Engine:
    def __init__(self, store, root):
        self.store, self.root = store, Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.client, self.user = None, None
        self.lock = threading.RLock()
        self.stop, self.wake, self.shutdown = threading.Event(), threading.Event(), threading.Event()
        self.active, self.action = None, "paused"
        self.worker = threading.Thread(target=self.loop, daemon=True, name="archive-worker")
        self.worker.start()

    ## ---- Session ----

    def connect(self, token):
        token = str(token).strip().removeprefix("Bot ").strip()
        if not 10 <= len(token) <= 4096 or any(c.isspace() for c in token):
            raise ValueError("Paste the bot token from the Developer Portal, nothing else")
        with self.lock:
            if self.active:
                raise ValueError("Pause the current export before changing bots")
            client = Discord("Bot " + token)
            user = client.get("users/@me")
            if self.client:
                self.client.token = ""
            self.client = client
            self.user = {
                "id": ident(user["id"]),
                "name": user.get("global_name") or user.get("username", "Discord bot"),
                "bot": bool(user.get("bot")),
            }
            self.wake.set()
            return self.user

    def disconnect(self):
        with self.lock:
            self.action = "paused"
            self.stop.set()
            if self.client:
                self.client.token = ""
            self.client, self.user = None, None
            with self.store.db() as db:
                db.execute("UPDATE jobs SET status='paused',note='Disconnected. Connect again and resume.' "
                           "WHERE status='queued'")

    def require(self):
        if not self.client or not self.user:
            raise ApiError(401, "Connect a bot first")
        return self.client

    ## ---- Queue ----

    def create(self, data):
        opts = options(data)
        with self.lock:
            self.require()
            job = uuid.uuid4().hex
            title = str(data.get("name") or "Channel archive").strip()[:100]
            with self.store.db() as db:
                db.execute("INSERT INTO jobs(id,owner,name,created,status,options) VALUES(?,?,?,?,?,?)",
                           (job, self.user["id"], title, now(), "queued", json.dumps(opts)))
            self.store.event(job, "Export queued. Selection and date range saved.")
            self.wake.set()
            return job

    def control(self, job, action):
        with self.lock:
            item = self.store.job(job)
            if action == "resume":
                self.require()
                if item["owner"] != self.user["id"]:
                    raise ValueError("Connect the same bot that created this export")
                if item["status"] not in ("paused", "failed", "partial", "cancelled") or job == self.active:
                    raise ValueError("This export cannot be resumed yet")
                with self.store.db() as db:
                    db.execute("UPDATE channels SET status='pending',note='' "
                               "WHERE job=? AND status IN ('error','skipped','running')", (job,))
                self.store.update_job(job, status="queued", note="")
                self.wake.set()
            elif action in ("pause", "cancel"):
                if item["status"] not in ("queued", "running"):
                    raise ValueError("Only a queued or running export can be paused or cancelled")
                target = "paused" if action == "pause" else "cancelled"
                if self.active == job:
                    self.action = target
                    self.stop.set()
                    self.store.update_job(job, note="Stopping after the current request...")
                else:
                    self.store.update_job(job, status=target)
            else:
                raise ValueError("Unknown export action")

    def check(self):
        if self.stop.is_set() or self.shutdown.is_set():
            raise Stopped()

    def loop(self):
        """Worker thread: takes the oldest queued job of the connected bot and runs it."""
        while not self.shutdown.is_set():
            self.wake.wait(1)
            self.wake.clear()
            with self.lock:
                if not self.client or not self.user:
                    continue
                with self.store.db() as db:
                    queued = db.execute("SELECT id FROM jobs WHERE status='queued' AND owner=? ORDER BY created LIMIT 1",
                                        (self.user["id"],)).fetchone()
                if not queued:
                    continue
                job = self.store.job(queued["id"])
                self.active, self.action = job["id"], "paused"
                self.stop.clear()
                client = self.client
                self.store.update_job(job["id"], status="running", note="Finding channels and threads...")
            try:
                self.run(job, client)
            except Stopped:
                self.store.update_job(job["id"], status=self.action,
                                      note="Progress saved. Resume to continue from the checkpoint.")
                self.store.event(job["id"], "Progress saved; export " + self.action + ".")
            except ApiError as exc:
                self.store.update_job(job["id"], status="paused" if exc.status == 401 else "failed", note=str(exc))
                self.store.event(job["id"], str(exc))
                if exc.status == 401:
                    self.disconnect()
            except Exception as exc:
                if isinstance(exc, OSError):
                    reason = "Not enough disk space or file access was denied."
                else:
                    reason = "Export stopped after an unexpected response. Resume to retry."
                self.store.update_job(job["id"], status="failed", note=reason)
                self.store.event(job["id"], reason + " Error type: " + type(exc).__name__)
            finally:
                with self.lock:
                    self.active = None
                    self.wake.set()

    ## ---- Discovery ----

    def discover(self, job, client):
        """Fill the job's channel list: picked channels, whole servers, then their threads."""
        jid, opts = job["id"], job["options"]
        selected, warnings = {}, []

        def add(channel):
            if channel.get("type", 0) in READABLE:
                selected[channel["id"]] = channel
                self.store.add_channel(jid, channel)

        def warn(message):
            warnings.append(message)
            self.store.event(jid, message)

        for gid in opts["guilds"]:
            self.check()
            try:
                for channel in client.get(f"guilds/{gid}/channels", stop=self.stop):
                    channel["guild_id"] = gid
                    add(channel)
            except ApiError as exc:
                if exc.status == 401:
                    raise
                warn(f"Server {gid}: {exc}")

        for cid in opts["channels"]:
            self.check()
            try:
                channel = client.get(f"channels/{cid}", stop=self.stop)
                if channel.get("type", 0) not in READABLE:
                    warn(f"Channel {cid}: this channel type has no supported message history.")
                else:
                    add(channel)
            except ApiError as exc:
                if exc.status == 401:
                    raise
                warn(f"Channel {cid}: {exc}")

        if opts["threads"]:
            parents = [c for c in selected.values() if c.get("type") in THREAD_PARENTS]
            self.active_threads(client, parents, selected, add, warn)
            for parent in parents:
                self.archived_threads(client, jid, parent, add, warn)

        if not selected:
            warn("No readable channels were found. Check the selection and the bot's permissions.")
        self.store.update_job(jid, discovered=0 if warnings else 1)
        return warnings

    def active_threads(self, client, parents, selected, add, warn):
        for gid in sorted({c.get("guild_id") for c in parents if c.get("guild_id")}):
            try:
                result = client.get(f"guilds/{gid}/threads/active", stop=self.stop)
                for thread in result.get("threads", []):
                    if thread.get("parent_id") in selected:
                        thread["guild_id"] = gid
                        add(thread)
            except ApiError as exc:
                if exc.status == 401:
                    raise
                warn(f"Active thread discovery for {gid}: {exc}")

    def archived_threads(self, client, jid, parent, add, warn):
        # the joined-private list pages by thread id, the other two by archive time
        sources = [(f"channels/{parent['id']}/threads/archived/public", False)]
        if parent.get("type") == 0:
            sources += [(f"channels/{parent['id']}/users/@me/threads/archived/private", True),
                        (f"channels/{parent['id']}/threads/archived/private", False)]
        for path, by_id in sources:
            cursor = None
            while True:
                self.check()
                params = {"limit": 100}
                if cursor:
                    params["before"] = cursor
                try:
                    result = client.get(path, params, self.stop)
                except ApiError as exc:
                    if exc.status == 401:
                        raise
                    # listing every private thread needs Manage Threads; the joined ones were tried above
                    if exc.status == 403 and path.endswith("/threads/archived/private") and "/users/@me/" not in path:
                        self.store.event(jid, f"#{parent['name']}: all-private-thread listing unavailable; "
                                              "joined private threads were checked.")
                    else:
                        warn(f"#{parent['name']} archived threads: {exc}")
                    break
                threads = result.get("threads", [])
                for thread in threads:
                    thread["guild_id"] = parent.get("guild_id", "@me")
                    add(thread)
                if not result.get("has_more"):
                    break
                if not threads:
                    warn(f"#{parent['name']}: Discord returned an empty thread page with more pages indicated.")
                    break
                if by_id:
                    nxt = str(min(int(t["id"]) for t in threads))
                else:
                    nxt = min((t.get("thread_metadata") or {}).get("archive_timestamp", "") for t in threads)
                if not nxt or nxt == cursor:
                    warn(f"#{parent['name']}: thread pagination did not advance.")
                    break
                cursor = nxt

    ## ---- Reading ----

    def run(self, job, client):
        jid, opts = job["id"], job["options"]
        old_bundle = self.root / jid / "archive.zip"
        if old_bundle.exists():
            old_bundle.unlink()
        with self.store.db() as db:
            db.execute("DELETE FROM files WHERE job=? AND path='archive.zip'", (jid,))

        warnings = self.discover(job, client) if not job["discovered"] else []
        for channel in self.store.channels(jid):
            self.check()
            if channel["status"] == "done":
                continue
            if channel["kind"] in CONTAINERS:
                self.store.update_channel(jid, channel["id"], status="done",
                                          note="Forum/media container. Posts exported as separate threads when enabled.")
                continue
            self.store.update_channel(jid, channel["id"], status="running", note="")
            self.store.update_job(jid, note="Reading #" + channel["name"])
            try:
                self.read_channel(jid, opts, channel, client)
            except ApiError as exc:
                if exc.status == 401:
                    raise
                status = "skipped" if exc.status in (403, 404) else "error"
                self.store.update_channel(jid, channel["id"], status=status, note=str(exc))
                self.store.event(jid, f"#{channel['name']}: {exc}")

        self.store.update_job(jid, note="Writing export files...")
        self.render(job, client, warnings)

    def read_channel(self, jid, opts, channel, client):
        """Page backwards from the checkpoint until the start date or the end of history."""
        before, pages = channel["cursor"] or opts["upper"], 0
        while True:
            self.check()
            if shutil.disk_usage(self.root).free < MIN_FREE:
                raise OSError("Low disk space")
            page = client.get(f"channels/{channel['id']}/messages", {"limit": 100, "before": before}, self.stop)
            if not isinstance(page, list):
                raise ApiError(502, "Unexpected message response")
            if not page:
                count = next(c["count"] for c in self.store.channels(jid) if c["id"] == channel["id"])
                if count:
                    note = "Reached the end of accessible history."
                else:
                    note = "No matching messages. Empty history may also mean missing Read Message History permission."
                self.store.update_channel(jid, channel["id"], status="done", note=note)
                return
            oldest = str(min(int(m["id"]) for m in page))
            if int(oldest) >= int(before):
                raise ApiError(502, "Message pagination stopped advancing; saved data retained")
            self.store.page(jid, channel["id"], [m for m in page if matches(m, opts)], oldest)
            before, pages = oldest, pages + 1
            if pages % 10 == 0:
                self.store.event(jid, f"#{channel['name']}: saved another {pages * 100:,} messages scanned in this session.")
            if int(oldest) <= int(opts["lower"]):
                self.store.update_channel(jid, channel["id"], status="done", note="Reached the requested start date.")
                return

    ## ---- Attachments ----

    def media(self, job, client, folder):
        """Download attachments within the per-file and total limits. Returns {attachment id: local path}."""
        opts, local, used, skipped = job["options"], {}, 0, 0
        file_limit, budget = opts["media_mb"] * MB, opts["budget_mb"] * MB
        media_dir = folder / "attachments"
        media_dir.mkdir(exist_ok=True)

        for channel in self.store.channels(job["id"]):
            for msg in self.store.read_messages(job["id"], channel["id"]):
                self.check()
                for item in msg.get("attachments", []):
                    aid = ident(item["id"])
                    if aid in local:
                        continue
                    size = int(item.get("size", 0))
                    if size < 0 or size > file_limit or used + size > budget:
                        skipped += 1
                        continue
                    if not allowed_attachment(item.get("url", "")):
                        skipped += 1
                        continue
                    name = re.sub(r"[^\w. -]", "_", item.get("filename", "file"))[:100].rstrip(". ") or "file"
                    target = media_dir / (aid + "-" + name)
                    if target.exists() and target.stat().st_size == size:
                        local[aid] = "attachments/" + target.name
                        used += size
                        continue
                    temp = target.with_name(target.name + ".part")
                    try:
                        self.check()
                        if shutil.disk_usage(folder).free < size + MIN_FREE:
                            raise OSError("Low disk space")
                        # plain request with no Authorization header: the token never goes to the CDN
                        request = Request(item["url"], headers={"User-Agent": USER_AGENT})
                        with client.opener.open(request, timeout=25) as response, temp.open("wb") as out:
                            actual = 0
                            while True:
                                self.check()
                                block = response.read(65536)
                                if not block:
                                    break
                                actual += len(block)
                                if actual > file_limit or used + actual > budget:
                                    raise ValueError("Attachment exceeds budget")
                                out.write(block)
                        if size and actual != size:
                            raise ValueError("Attachment size mismatch")
                        temp.replace(target)
                        local[aid] = "attachments/" + target.name
                        used += actual
                    except Stopped:
                        raise
                    except Exception:
                        skipped += 1
                    finally:
                        if temp.exists():
                            temp.unlink()
                    self.stop.wait(0.3)

        if skipped:
            self.store.event(job["id"], f"{skipped} attachment downloads skipped (budget, expired URL, "
                                        "unsupported host or connection). Remote links remain in the archive.")
        return local, skipped

    ## ---- Writing files ----

    def render(self, job, client, warnings):
        folder = self.root / job["id"]
        folder.mkdir(exist_ok=True)
        local, skipped = self.media(job, client, folder) if job["options"]["media"] else ({}, 0)
        channels = self.store.channels(job["id"])
        partial = bool(warnings or skipped or any(c["status"] in ("error", "skipped") for c in channels))
        job["status"] = "partial" if partial else "completed"

        for channel in channels:
            for fmt in job["options"]["formats"]:
                self.check()
                name = re.sub(r"[^\w.-]", "-", channel["name"])[:60].strip(".-") or "channel"
                path = folder / f"{channel['guild']}--{name}--{channel['id']}.{fmt}"
                messages = self.store.read_messages(job["id"], channel["id"])
                write_export(path, fmt, job, channel, messages, self.check, local)

        report = {"job": job, "channels": channels, "warnings": warnings,
                  "attachments_skipped": skipped, "scope": SCOPE_NOTE}
        temp = folder / "manifest.json.tmp"
        temp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        temp.replace(folder / "manifest.json")

        written = [(job["id"], str(p.relative_to(folder)).replace("\\", "/"), p.stat().st_size)
                   for p in folder.rglob("*") if p.is_file() and not p.name.endswith((".tmp", ".part"))]
        with self.store.db() as db:
            db.execute("DELETE FROM files WHERE job=?", (job["id"],))
            db.executemany("INSERT INTO files VALUES(?,?,?)", written)

        if partial:
            note = "Finished with gaps. Check the channel report and activity log."
        else:
            note = "Export ready. Open it in Library to read or download."
        self.store.update_job(job["id"], status=job["status"], note=note)
        self.store.event(job["id"], note)

    def close(self):
        self.shutdown.set()
        self.stop.set()
        self.wake.set()
        self.worker.join(timeout=30)
