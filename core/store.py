"""SQLite library: jobs, channels, saved messages, the activity log and the list of export files."""
import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
    PRAGMA journal_mode=WAL;
    CREATE TABLE IF NOT EXISTS jobs (
        id TEXT PRIMARY KEY, owner TEXT NOT NULL, name TEXT NOT NULL,
        created TEXT NOT NULL, status TEXT NOT NULL, options TEXT NOT NULL,
        note TEXT NOT NULL DEFAULT '', discovered INTEGER NOT NULL DEFAULT 0);
    CREATE TABLE IF NOT EXISTS channels (
        job TEXT, id TEXT, guild TEXT, name TEXT, kind INTEGER,
        status TEXT DEFAULT 'pending', cursor TEXT, count INTEGER DEFAULT 0,
        note TEXT DEFAULT '', PRIMARY KEY(job,id));
    CREATE TABLE IF NOT EXISTS messages (
        job TEXT, channel TEXT, id TEXT, timestamp TEXT, author TEXT,
        content TEXT, payload TEXT, PRIMARY KEY(job,channel,id));
    CREATE INDEX IF NOT EXISTS messages_time ON messages(job,timestamp,id);
    CREATE INDEX IF NOT EXISTS messages_author ON messages(job,author);
    CREATE TABLE IF NOT EXISTS events (
        seq INTEGER PRIMARY KEY AUTOINCREMENT, job TEXT, at TEXT, text TEXT);
    CREATE TABLE IF NOT EXISTS files (
        job TEXT, path TEXT, size INTEGER, PRIMARY KEY(job,path));
"""

JOB_LIST = """
    SELECT j.*,
        (SELECT COALESCE(SUM(count),0) FROM channels c WHERE c.job=j.id) messages,
        (SELECT COUNT(*) FROM channels c WHERE c.job=j.id) channels,
        (SELECT COUNT(*) FROM channels c WHERE c.job=j.id AND c.status IN ('done','skipped','error')) finished
    FROM jobs j ORDER BY created DESC LIMIT ? OFFSET ?
"""


def now():
    return datetime.now(timezone.utc).isoformat()


class Store:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.root.mkdir(parents=True, exist_ok=True)
        self.path = self.root / "library.sqlite3"
        with self.db() as db:
            db.executescript(SCHEMA)
            # anything that was mid-run when the app closed goes back to paused
            db.execute("UPDATE jobs SET status='paused', note='App restarted. Connect and resume this export.' "
                       "WHERE status IN ('running','queued')")
            db.execute("UPDATE channels SET status='pending' WHERE status='running'")

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=30)
        db.row_factory = sqlite3.Row
        db.create_function("casefold", 1, lambda value: str(value or "").casefold(), deterministic=True)
        try:
            with db:
                yield db
        finally:
            db.close()

    ## ---- Jobs ----

    def job(self, ident):
        with self.db() as db:
            row = db.execute("SELECT * FROM jobs WHERE id=?", (ident,)).fetchone()
        if not row:
            raise ValueError("Export not found")
        item = dict(row)
        item["options"] = json.loads(item["options"])
        return item

    def jobs(self, limit=200, offset=0):
        with self.db() as db:
            rows = db.execute(JOB_LIST, (limit, offset)).fetchall()
        return [{**dict(r), "options": json.loads(r["options"])} for r in rows]

    def update_job(self, ident, **values):
        assert set(values) <= {"status", "note", "discovered"}
        with self.db() as db:
            db.execute("UPDATE jobs SET " + ",".join(k + "=?" for k in values) + " WHERE id=?",
                       (*values.values(), ident))

    def event(self, job, text):
        with self.db() as db:
            db.execute("INSERT INTO events(job,at,text) VALUES(?,?,?)", (job, now(), str(text)[:2000]))
            # keep the newest 500 lines per job
            db.execute("DELETE FROM events WHERE job=? AND seq NOT IN "
                       "(SELECT seq FROM events WHERE job=? ORDER BY seq DESC LIMIT 500)", (job, job))

    ## ---- Channels ----

    def channels(self, job):
        with self.db() as db:
            return [dict(r) for r in db.execute("SELECT * FROM channels WHERE job=? ORDER BY guild,name,id", (job,))]

    def add_channel(self, job, channel):
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO channels(job,id,guild,name,kind) VALUES(?,?,?,?,?)",
                       (job, channel["id"], channel.get("guild_id", "@me"),
                        channel.get("name", channel["id"]), channel.get("type", 0)))

    def update_channel(self, job, channel, **values):
        assert set(values) <= {"status", "cursor", "count", "note"}
        with self.db() as db:
            db.execute("UPDATE channels SET " + ",".join(k + "=?" for k in values) + " WHERE job=? AND id=?",
                       (*values.values(), job, channel))

    ## ---- Messages ----

    def page(self, job, channel, messages, cursor):
        """Save one page of messages and move the channel's checkpoint."""
        rows = [(job, channel, m["id"], m.get("timestamp", ""), (m.get("author") or {}).get("id", ""),
                 m.get("content") or "", json.dumps(m, ensure_ascii=False)) for m in messages]
        with self.db() as db:
            db.executemany("INSERT OR IGNORE INTO messages VALUES(?,?,?,?,?,?,?)", rows)
            db.execute("UPDATE channels SET cursor=?, "
                       "count=(SELECT COUNT(*) FROM messages WHERE job=? AND channel=?) WHERE job=? AND id=?",
                       (cursor, job, channel, job, channel))

    def read_messages(self, job, channel):
        with self.db() as db:
            # ids are numbers stored as text, so sort by length first
            for row in db.execute("SELECT payload FROM messages WHERE job=? AND channel=? ORDER BY length(id),id",
                                  (job, channel)):
                yield json.loads(row[0])

    def search(self, job, query):
        clauses, args = ["m.job=?"], [job]
        for key, column in (("channel", "m.channel"), ("author", "m.author")):
            if query.get(key):
                clauses.append(column + "=?")
                args.append(query[key])
        if query.get("q"):
            clauses.append("instr(casefold(m.content),casefold(?))>0")
            args.append(query["q"][:500])
        if query.get("day"):
            clauses.append("substr(m.timestamp,1,10)=?")
            args.append(query["day"])
        offset = max(0, min(int(query.get("offset", 0)), 100000000))
        where = " AND ".join(clauses)

        with self.db() as db:
            total = db.execute("SELECT COUNT(*) FROM messages m WHERE " + where, args).fetchone()[0]
            rows = db.execute(
                "SELECT m.payload,c.name channel_name,c.guild FROM messages m "
                "JOIN channels c ON c.job=m.job AND c.id=m.channel WHERE " + where +
                " ORDER BY m.timestamp,m.id LIMIT 50 OFFSET ?", (*args, offset)).fetchall()
            days = db.execute(
                "SELECT substr(timestamp,1,10) day,COUNT(*) count FROM messages "
                "WHERE job=? GROUP BY day ORDER BY day", (job,)).fetchall()
            authors = db.execute(
                "SELECT author,COUNT(*) count FROM messages "
                "WHERE job=? GROUP BY author ORDER BY count DESC LIMIT 200", (job,)).fetchall()

        messages = [{**json.loads(r["payload"]), "channel_name": r["channel_name"], "guild_id": r["guild"]}
                    for r in rows]
        return {"total": total, "offset": offset, "messages": messages,
                "days": [dict(r) for r in days], "authors": [dict(r) for r in authors]}
