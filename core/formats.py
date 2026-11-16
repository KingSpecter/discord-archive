"""Export writers. Files are written a message at a time, and message text is always escaped."""
import csv
import html
import json
import re
from urllib.parse import quote, urlsplit

FORMATS = {"md": "Markdown", "html": "HTML", "json": "JSON", "jsonl": "JSON Lines", "txt": "Text", "csv": "CSV"}

CSV_COLUMNS = ["message_id", "channel_id", "author_id", "author", "timestamp_utc", "edited_at",
               "content", "attachments", "reply_to", "message_url"]

HTML_STYLE = (
    "body{font:16px/1.6 system-ui;background:#eef1ef;color:#14201b;max-width:850px;margin:40px auto;padding:0 24px}"
    "article{background:#fff;border:1px solid #d3dad6;border-radius:6px;padding:20px;margin:14px 0;overflow-wrap:anywhere}"
    "pre{white-space:pre-wrap;font:inherit}a{color:#1f5b46}small{color:#5c6b64}h2{font-size:20px}header{margin-bottom:36px}"
)
HTML_FOOTER = ("<footer>Local archive. Deleted or inaccessible messages cannot be recovered. "
               "External attachment links may expire.</footer></html>")


## ---- Text helpers ----

def safe_url(value):
    try:
        url = urlsplit(str(value))
        if url.scheme in ("https", "http") and url.hostname and not url.username:
            return quote(str(value), safe=":/?=&%#@+;,~!$-_.")
    except ValueError:
        pass
    return ""


def text(message):
    """Message content with user mentions shown as names."""
    value = message.get("content") or ""
    for user in message.get("mentions", []):
        name = user.get("global_name") or user.get("username") or user["id"]
        value = re.sub(r"<@!?" + re.escape(user["id"]) + ">", lambda _: "@" + name, value)
    return value


def details(message):
    """Everything readable in a message: text, embeds, stickers, polls, forwards, reactions."""
    parts = [text(message)]
    for embed in message.get("embeds", []):
        parts.extend(str(embed[k]) for k in ("title", "description", "url") if embed.get(k))
        for field in embed.get("fields", []):
            parts.append(f"{field.get('name', '')}: {field.get('value', '')}")
        for kind in ("image", "thumbnail", "video"):
            url = (embed.get(kind) or {}).get("url")
            if url:
                parts.append(kind.capitalize() + ": " + url)
    for sticker in message.get("sticker_items", []):
        parts.append("Sticker: " + sticker.get("name", ""))
    poll = message.get("poll")
    if poll:
        parts.append("Poll: " + poll.get("question", {}).get("text", ""))
        parts.extend("• " + a.get("poll_media", {}).get("text", "") for a in poll.get("answers", []))
    for snapshot in message.get("message_snapshots", []):
        snap = snapshot.get("message", {})
        parts.append("Forwarded message: " + (snap.get("content") or "[see raw JSON for forwarded media]"))
    if message.get("reactions"):
        reactions = (f"{r.get('emoji', {}).get('name') or 'emoji'} × {r.get('count', 0)}" for r in message["reactions"])
        parts.append("Reactions: " + " · ".join(reactions))
    return "\n\n".join(p for p in parts if p)


def csv_safe(value):
    """Stop spreadsheets from running a cell as a formula or rounding a long ID."""
    value = str(value)
    long_number = value.isdigit() and len(value) >= 16
    formula = value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n"))
    control = value.startswith(("\t", "\r", "\n"))
    return "'" + value if long_number or formula or control else value


def md_label(value):
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", html.escape(str(value).replace("\n", " ")))


def author_name(message):
    author = message.get("author") or {}
    return author.get("global_name") or author.get("username") or "Unknown author"


def message_url(channel, message):
    return f"https://discord.com/channels/{channel['guild']}/{channel['id']}/{message['id']}"


def notes(message):
    """Short flags shown under the author line."""
    extra = []
    if message.get("pinned"):
        extra.append("Pinned")
    if message.get("edited_timestamp"):
        extra.append("Edited " + message["edited_timestamp"])
    if message.get("type", 0) not in (0, 19):
        extra.append("Message type " + str(message["type"]))
    ref = message.get("message_reference") or {}
    if ref.get("message_id"):
        extra.append("References " + ref["message_id"])
    return extra


## ---- Writers ----

def write_export(path, fmt, job, channel, messages, check=lambda: None, local_media=None):
    """Write one channel in one format. check() is called per message so a pause can interrupt."""
    temp = path.with_suffix(path.suffix + ".tmp")
    encoding = "utf-8-sig" if fmt == "csv" else "utf-8"
    with temp.open("w", encoding=encoding, newline="") as out:
        if fmt in ("json", "jsonl"):
            write_raw(out, fmt, job, channel, messages, check)
        elif fmt == "csv":
            write_csv(out, channel, messages, check)
        else:
            write_readable(out, fmt, channel, messages, check, local_media or {})
    temp.replace(path)


def write_raw(out, fmt, job, channel, messages, check):
    meta = {"channel": channel, "export": {k: job[k] for k in ("id", "name", "created", "status", "options")}}
    if fmt == "json":
        out.write('{"metadata":' + json.dumps(meta, ensure_ascii=False) + ',"messages":[')
    else:
        out.write(json.dumps({"_type": "archive_metadata", **meta}, ensure_ascii=False) + "\n")
    first = True
    for msg in messages:
        check()
        if fmt == "json" and not first:
            out.write(",")
        out.write(json.dumps(msg, ensure_ascii=False) + ("\n" if fmt == "jsonl" else ""))
        first = False
    if fmt == "json":
        out.write("]}\n")


def write_csv(out, channel, messages, check):
    writer = csv.writer(out)
    writer.writerow(CSV_COLUMNS)
    for msg in messages:
        check()
        author = msg.get("author") or {}
        ref = msg.get("message_reference") or {}
        row = [msg["id"], channel["id"], author.get("id", ""), author_name(msg), msg.get("timestamp", ""),
               msg.get("edited_timestamp") or "", details(msg),
               "\n".join(a.get("url", "") for a in msg.get("attachments", [])),
               ref.get("message_id", ""), message_url(channel, msg)]
        writer.writerow([csv_safe(v) for v in row])


def write_readable(out, fmt, channel, messages, check, local_media):
    """Markdown, HTML and plain text share one layout: a day heading, then one block per message."""
    e = html.escape
    title = channel["name"]
    status = f"Channel status: {channel['status']}. {channel.get('note', '')}"

    if fmt == "html":
        out.write(
            '<!doctype html><html lang="en"><meta charset="utf-8">'
            '<meta name="viewport" content="width=device-width,initial-scale=1">'
            '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; '
            'style-src \'unsafe-inline\'; img-src \'none\'; base-uri \'none\'">'
            f"<title>{e(title)} · Archive</title><style>{HTML_STYLE}</style>"
            f"<header><h1>#{e(title)}</h1><p>{e(status)}</p>"
            f"<p>{channel['count']:,} saved messages · UTC timestamps</p></header>")
    elif fmt == "md":
        out.write("# " + md_label(title) + "\n\n" + status + "\n\n")
    else:
        out.write(title + "\n\n" + status + "\n\n")

    day = None
    for msg in messages:
        check()
        author = msg.get("author") or {}
        name = author_name(msg)
        stamp = msg.get("timestamp", "")
        url = message_url(channel, msg)
        body = details(msg)
        extra = notes(msg)

        if day != stamp[:10]:
            day = stamp[:10]
            if fmt == "html":
                out.write(f"<h2>{e(day)}</h2>")
            else:
                out.write(f"\n{'## ' if fmt == 'md' else ''}{day}\n\n")

        if fmt == "html":
            out.write(f'<article id="m{msg["id"]}"><b>{e(name)}</b> <small>{e(author.get("id", ""))}</small><br>'
                      f'<a href="{e(url)}" rel="noreferrer">{e(stamp)}</a>'
                      f'<p><small>{e(" · ".join(extra))}</small></p><pre>{e(body)}</pre>')
        elif fmt == "md":
            out.write(f"### {md_label(name)} · [{stamp}](<{url}>)\n\nAuthor ID: `{author.get('id', '')}`\n\n")
            if extra:
                out.write(" · ".join(extra) + "\n\n")
            # escape HTML, leave Discord's own markdown and code fences alone
            out.write("\n".join("> " + line for line in html.escape(body).splitlines()) + "\n\n")
        else:
            out.write(f"{name} ({author.get('id', '')}) | {stamp}\n{url}\n" + " · ".join(extra) + "\n" + body + "\n")

        for a in msg.get("attachments", []):
            filename = a.get("filename", "attachment")
            # a downloaded copy wins over the remote link
            target = local_media.get(a.get("id")) or safe_url(a.get("url", ""))
            if not target:
                continue
            if fmt == "html":
                out.write(f'<p>Attachment: <a download href="{e(target, quote=True)}" rel="noreferrer">{e(filename)}</a></p>')
            elif fmt == "md":
                out.write(f"- Attachment: [{md_label(filename)}](<{target}>)\n")
            else:
                out.write(f"Attachment: {filename} | {target}\n")

        out.write("</article>" if fmt == "html" else "\n---\n\n")

    if fmt == "html":
        out.write(HTML_FOOTER)
