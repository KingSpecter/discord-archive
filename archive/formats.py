"""Streaming exports. HTML never interprets Discord content as executable markup."""
import csv
import html
import json
import re
from urllib.parse import quote, urlsplit

FORMATS = {"md": "Markdown", "html": "HTML", "json": "JSON", "jsonl": "JSON Lines", "txt": "Text", "csv": "CSV"}


def safe_url(value):
    try:
        url = urlsplit(str(value))
        if url.scheme in ("https", "http") and url.hostname and not url.username:
            return quote(str(value), safe=":/?=&%#@+;,~!$-_.")
    except ValueError:
        pass
    return ""


def text(message):
    value = message.get("content") or ""
    for user in message.get("mentions", []):
        name = user.get("global_name") or user.get("username") or user["id"]
        value = re.sub(r"<@!?" + re.escape(user["id"]) + ">", lambda _: "@" + name, value)
    return value


def details(message):
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
        parts.append("Reactions: " + " · ".join(f"{r.get('emoji', {}).get('name') or 'emoji'} × {r.get('count', 0)}" for r in message["reactions"]))
    return "\n\n".join(p for p in parts if p)


def csv_safe(value):
    value = str(value)
    return "'" + value if (value.isdigit() and len(value) >= 16) or value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r", "\n")) or value.startswith(("\t", "\r", "\n")) else value


def md_label(value):
    return re.sub(r"([\\`*_{}\[\]()#!|])", r"\\\1", html.escape(str(value).replace("\n", " ")))


def write_export(path, fmt, job, channel, messages, check=lambda: None, local_media=None):
    local_media = local_media or {}
    temp = path.with_suffix(path.suffix + ".tmp")
    meta = {"channel": channel, "export": {k: job[k] for k in ("id", "name", "created", "status", "options")}}
    title = channel["name"]
    status = f"Channel status: {channel['status']}. {channel.get('note', '')}"
    e = html.escape
    with temp.open("w", encoding="utf-8-sig" if fmt == "csv" else "utf-8", newline="") as out:
        if fmt == "json":
            out.write('{"metadata":' + json.dumps(meta, ensure_ascii=False) + ',"messages":[')
        elif fmt == "jsonl":
            out.write(json.dumps({"_type": "archive_metadata", **meta}, ensure_ascii=False) + "\n")
        elif fmt == "csv":
            writer = csv.writer(out)
            writer.writerow(["message_id", "channel_id", "author_id", "author", "timestamp_utc", "edited_at", "content", "attachments", "reply_to", "message_url"])
        elif fmt == "html":
            out.write('<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">'
                      '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; style-src \'unsafe-inline\'; img-src \'none\'; base-uri \'none\'">'
                      f'<title>{e(title)} · Archive</title><style>body{{font:16px/1.6 system-ui;background:#f5f7fa;color:#172638;max-width:850px;margin:40px auto;padding:0 24px}}'
                      'article{background:white;border:1px solid #dfe5ee;border-radius:12px;padding:22px;margin:16px 0;overflow-wrap:anywhere}'
                      'pre{white-space:pre-wrap;font:inherit}a{color:#245bd7}small{color:#526176}h2{font-size:20px}header{margin-bottom:36px}</style>'
                      f'<header><h1>#{e(title)}</h1><p>{e(status)}</p><p>{channel["count"]:,} saved messages · UTC timestamps</p></header>')
        else:
            out.write(("# " if fmt == "md" else "") + (md_label(title) if fmt == "md" else title) + "\n\n" + status + "\n\n")
        day, first = None, True
        for msg in messages:
            check()
            if fmt in ("json", "jsonl"):
                if fmt == "json" and not first:
                    out.write(",")
                out.write(json.dumps(msg, ensure_ascii=False) + ("\n" if fmt == "jsonl" else ""))
                first = False
                continue
            author = msg.get("author") or {}
            name = author.get("global_name") or author.get("username") or "Unknown author"
            stamp = msg.get("timestamp", "")
            url = f"https://discord.com/channels/{channel['guild']}/{channel['id']}/{msg['id']}"
            ref = msg.get("message_reference") or {}
            attachments = msg.get("attachments", [])
            body = details(msg)
            if fmt == "csv":
                writer.writerow([csv_safe(v) for v in [msg["id"], channel["id"], author.get("id", ""), name, stamp,
                    msg.get("edited_timestamp") or "", body, "\n".join(a.get("url", "") for a in attachments), ref.get("message_id", ""), url]])
                continue
            if day != stamp[:10]:
                day = stamp[:10]
                out.write(f'<h2>{e(day)}</h2>' if fmt == "html" else f"\n{'## ' if fmt == 'md' else ''}{day}\n\n")
            extra = []
            if msg.get("pinned"):
                extra.append("Pinned")
            if msg.get("edited_timestamp"):
                extra.append("Edited " + msg["edited_timestamp"])
            if msg.get("type", 0) not in (0, 19):
                extra.append("Message type " + str(msg["type"]))
            if ref.get("message_id"):
                extra.append("References " + ref["message_id"])
            if fmt == "html":
                out.write(f'<article id="m{msg["id"]}"><b>{e(name)}</b> <small>{e(author.get("id", ""))}</small><br>'
                          f'<a href="{e(url)}" rel="noreferrer">{e(stamp)}</a><p><small>{e(" · ".join(extra))}</small></p><pre>{e(body)}</pre>')
            elif fmt == "md":
                out.write(f"### {md_label(name)} · [{stamp}](<{url}>)\n\nAuthor ID: `{author.get('id', '')}`\n\n")
                if extra:
                    out.write(" · ".join(extra) + "\n\n")
                # Escape HTML while retaining ordinary Discord Markdown and code fences.
                out.write("\n".join("> " + line for line in html.escape(body).splitlines()) + "\n\n")
            else:
                out.write(f"{name} ({author.get('id', '')}) | {stamp}\n{url}\n" + " · ".join(extra) + "\n" + body + "\n")
            for a in attachments:
                remote = safe_url(a.get("url", ""))
                name = a.get("filename", "attachment")
                target = local_media.get(a.get("id")) or remote
                if not target:
                    continue
                if fmt == "html":
                    out.write(f'<p>Attachment: <a download href="{e(target, quote=True)}" rel="noreferrer">{e(name)}</a></p>')
                elif fmt == "md":
                    out.write(f"- Attachment: [{md_label(name)}](<{target}>)\n")
                else:
                    out.write(f"Attachment: {name} | {target}\n")
            out.write('</article>' if fmt == "html" else "\n---\n\n")
        if fmt == "json":
            out.write("]}\n")
        if fmt == "html":
            out.write('<footer>Local archive. Deleted or inaccessible messages cannot be recovered. External attachment links may expire.</footer></html>')
    temp.replace(path)
