"""Read-only Discord client. One request at a time, tokens and response bodies are never logged."""
import json
import math
import threading
import time
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import HTTPRedirectHandler, Request, build_opener

API = "https://discord.com/api/v10/"
USER_AGENT = "DiscordBot (https://github.com/KingSpecter/discord-archive, 1.0)"
MAX_BODY = 32 * 1024 * 1024
ATTEMPTS = 7

REASONS = {
    401: "Token rejected or expired. Connect again to resume.",
    403: "Access denied. Check the bot's channel and history permissions.",
    404: "Channel or server is unavailable or was deleted.",
}


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


class Stopped(Exception):
    pass


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        return None


class Discord:
    def __init__(self, token, interval=0.55):
        self.token = token
        self.interval = interval
        self.gate = threading.Lock()
        self.next_request = 0
        self.notice = ""
        self.opener = build_opener(NoRedirect())

    def wait(self, seconds, stop):
        if stop.wait(max(0, seconds)):
            raise Stopped()

    ## ---- Requests ----

    def get(self, path, params=None, stop=None):
        stop = stop or threading.Event()
        if not path.startswith(("users/", "guilds/", "channels/")) or ".." in path:
            raise ValueError("Invalid Discord endpoint")
        url = API + path
        if params:
            url += "?" + urlencode(params)

        for attempt in range(ATTEMPTS):
            if stop.is_set():
                raise Stopped()
            with self.gate:
                self.wait(self.next_request - time.monotonic(), stop)
                if not self.token:
                    raise ApiError(401, "Session ended. Connect again to resume.")
                request = Request(url, headers={
                    "Authorization": self.token,
                    "User-Agent": USER_AGENT,
                    "Accept": "application/json",
                })
                try:
                    with self.opener.open(request, timeout=25) as response:
                        raw = response.read(MAX_BODY + 1)
                        if len(raw) > MAX_BODY:
                            raise ApiError(502, "Discord response was larger than the 32 MB limit.")
                        result = json.loads(raw)
                        delay = self.interval
                        if response.headers.get("X-RateLimit-Remaining") == "0":
                            delay = max(delay, self.number(response.headers.get("X-RateLimit-Reset-After"), 1))
                        self.next_request = time.monotonic() + delay
                        self.notice = ""
                        return result
                except HTTPError as exc:
                    if exc.code == 429:
                        try:
                            data = json.loads(exc.read(65536))
                        except (ValueError, TypeError):
                            data = {}
                        delay = self.number(data.get("retry_after", exc.headers.get("Retry-After")), 5) + 0.5
                        self.notice = f"Discord rate limit. Waiting {delay:.0f}s before retrying."
                    elif 500 <= exc.code < 600:
                        delay = min(2 ** (attempt + 1), 30)
                        self.notice = "Discord is temporarily unavailable. Retrying."
                    else:
                        message = REASONS.get(exc.code, f"Discord rejected the request (HTTP {exc.code}).")
                        raise ApiError(exc.code, message) from None
                except (URLError, TimeoutError, OSError):
                    delay = min(2 ** (attempt + 1), 30)
                    self.notice = "Connection interrupted. Retrying after a short wait."
                except (ValueError, UnicodeError):
                    raise ApiError(502, "Discord returned an unreadable response. Try again later.") from None
                self.next_request = time.monotonic() + delay
        raise ApiError(503, "Discord could not be reached after seven attempts. Progress is saved, resume later.")

    @staticmethod
    def number(value, fallback):
        try:
            n = float(value)
            return n if math.isfinite(n) and n >= 0 else fallback
        except (ValueError, TypeError):
            return fallback

    ## ---- Servers ----

    def guilds(self):
        result, cursor = {}, None
        while True:
            params = {"limit": 200}
            if cursor:
                params["after"] = cursor
            page = self.get("users/@me/guilds", params)
            if not isinstance(page, list):
                raise ApiError(502, "Unexpected server list response.")
            if not page:
                break
            for g in page:
                result[g["id"]] = {"id": g["id"], "name": g.get("name", g["id"])}
            next_cursor = str(max(int(g["id"]) for g in page))
            if cursor and int(next_cursor) <= int(cursor):
                raise ApiError(502, "Server pagination stopped advancing.")
            cursor = next_cursor
            if len(page) < 200:
                break
        return sorted(result.values(), key=lambda g: g["name"].casefold())
