"""Reddit Relay server: a localhost job queue between Reddit clients (the CLI, the MCP server)
and the Reddit Relay Chrome extension, which fetches Reddit with the browser's own session.

Endpoints, on 127.0.0.1 only:
  GET  /api/ping        identity, version, and when the extension last polled
  GET  /fetch?url=...   queue one Reddit address, wait for the extension, return Reddit's JSON
                        (&raw=1: fetch the address as given and return {"url", "body"})
  GET  /job             extension long-poll (needs the X-Relay header)
  POST /result          extension posts a fetched response (needs the X-Relay header)

The server stops itself after --idle-minutes without a /fetch. Clients start it again on demand.
"""

import argparse
import json
import re
import threading
import time
import uuid
from collections import deque
from html import unescape
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qsl, urlparse

APP = "reddit-relay"
VERSION = "0.2.0"
PORT = 8902
POLL_WAIT = 20  # seconds a /job long-poll is held open
MIN_GAP = 1.0  # seconds between two Reddit requests, to stay polite on the owner's account
REDDIT_HOSTS = {
    "reddit.com",
    "www.reddit.com",
    "old.reddit.com",
    "new.reddit.com",
    "np.reddit.com",
    "m.reddit.com",
    "redd.it",
}

cond = threading.Condition()
pending = deque()  # jobs waiting for the extension
waiting = {}  # job id -> {"event": Event, "result": dict | None}
state = {"last_poll": 0.0, "last_dispatch": 0.0, "last_fetch": time.time(), "extension_version": None}


def visible_text(html):
    """The readable words of an HTML page or fragment, without its styles, scripts and tags."""
    html = re.sub(r"(?is)<(style|script|svg)\b.*?</\1>", " ", html)
    return re.sub(r"\s+", " ", unescape(re.sub(r"<[^>]+>", " ", html))).strip()


def make_job(raw_url, limit=500, raw=False):
    """Turn any Reddit address into a job for the extension, or raise ValueError.
    raw=True fetches the address exactly as given (HTML, RSS) instead of its .json form."""
    u = urlparse(raw_url if "://" in raw_url else "https://" + raw_url)
    host = (u.hostname or "").lower()
    if host not in REDDIT_HOSTS:
        raise ValueError(f"not a Reddit address: {raw_url}")
    if raw:
        return {"url": u._replace(scheme="https").geturl(), "json": False}
    if host == "redd.it":
        return {
            "url": f"https://redd.it{u.path}",
            "resolve": True,
            "json": True,
            "params": {"raw_json": "1", "limit": str(limit)},
        }
    params = dict(parse_qsl(u.query))
    params.setdefault("limit", str(limit))
    params["raw_json"] = "1"
    job = {"url": f"https://www.reddit.com{u.path or '/'}", "json": True, "params": params}
    if "/s/" in u.path:  # share links redirect to the real post
        job["resolve"] = True
    return job


class Handler(BaseHTTPRequestHandler):
    server_version = f"RedditRelay/{VERSION}"

    def log_message(self, *args):
        pass  # runs under pythonw, which has no stderr

    def send_json(self, code, obj=None, raw=None):
        body = raw if raw is not None else json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def refused(self, extension):
        """Refuse anything a web page could send. Returns True when the request was refused."""
        host = (self.headers.get("Host") or "").rsplit(":", 1)[0]
        if host not in ("127.0.0.1", "localhost"):
            self.send_json(403, {"error": "bad Host header"})
            return True
        if extension:
            # A custom header forces a CORS preflight, which this server never answers,
            # so a web page cannot reach the extension endpoints.
            if self.headers.get("X-Relay") != "1":
                self.send_json(403, {"error": "extension endpoint"})
                return True
            state["extension_version"] = self.headers.get("X-Relay-Version")
        elif self.headers.get("Origin") or self.headers.get("Sec-Fetch-Site"):
            self.send_json(403, {"error": "browser requests are refused"})
            return True
        return False

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/api/ping":
            return self.ping()
        if path == "/job":
            return self.job()
        if path == "/fetch":
            return self.fetch()
        self.send_json(404, {"error": "unknown path"})

    def ping(self):
        if self.refused(False):
            return
        lp = state["last_poll"]
        self.send_json(
            200,
            {
                "app": APP,
                "version": VERSION,
                "extension_version": state["extension_version"],
                "extension_last_poll_s": round(time.time() - lp, 1) if lp else None,
            },
        )

    def job(self):
        if self.refused(True):
            return
        state["last_poll"] = time.time()
        with cond:
            if not pending:
                cond.wait(POLL_WAIT)
            job = pending.popleft() if pending else None
        if job is not None:
            gap = MIN_GAP - (time.time() - state["last_dispatch"])
            if gap > 0:
                time.sleep(gap)
            state["last_dispatch"] = time.time()
        state["last_poll"] = time.time()
        if job is None:
            self.send_response(204)
            self.end_headers()
            return
        self.send_json(200, job)

    def fetch(self):
        if self.refused(False):
            return
        state["last_fetch"] = time.time()
        q = dict(parse_qsl(urlparse(self.path).query))
        raw = q.get("raw") == "1"
        try:
            job = make_job(q.get("url", ""), int(q.get("limit", "500")), raw)
            timeout = float(q.get("timeout", "45"))
        except ValueError as e:
            return self.send_json(400, {"error": str(e)})
        job["id"] = uuid.uuid4().hex
        slot = {"event": threading.Event(), "result": None}
        with cond:
            waiting[job["id"]] = slot
            pending.append(job)
            cond.notify_all()
        done = slot["event"].wait(timeout)
        with cond:
            waiting.pop(job["id"], None)
            if job in pending:
                pending.remove(job)
        if not done:
            lp = state["last_poll"]
            hint = (
                "the extension has never polled this relay: is it installed and enabled in Chrome?"
                if not lp
                else f"the extension last polled {round(time.time() - lp)}s ago: is Chrome open?"
            )
            return self.send_json(504, {"error": f"no answer in {timeout:g}s; {hint}"})
        res = slot["result"]
        if res.get("ok") and res.get("status") == 200:
            return self.send_json(
                200,
                raw=(
                    json.dumps({"url": res.get("url"), "body": res["body"]}) if raw else res["body"]
                ).encode(),
            )
        self.send_json(
            502,
            {
                "error": res.get("error") or f"Reddit returned HTTP {res.get('status')}",
                "status": res.get("status"),
                "url": res.get("url"),
                "page_text": visible_text(res.get("body") or "")[:600],
            },
        )

    def do_POST(self):
        if urlparse(self.path).path != "/result":
            return self.send_json(404, {"error": "unknown path"})
        if self.refused(True):
            return
        length = int(self.headers.get("Content-Length") or 0)
        try:
            res = json.loads(self.rfile.read(length))
        except ValueError:
            return self.send_json(400, {"error": "bad JSON"})
        with cond:
            slot = waiting.get(res.get("id"))
        if slot:
            slot["result"] = res
            slot["event"].set()
        self.send_json(200, {"ok": True})


def make_server(port=PORT):
    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def watch_idle(server, idle_minutes):
    while True:
        time.sleep(30)
        if time.time() - state["last_fetch"] > idle_minutes * 60:
            server.shutdown()
            return


def main():
    ap = argparse.ArgumentParser(description="Reddit Relay server (127.0.0.1 only).")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument(
        "--idle-minutes",
        type=float,
        default=60,
        help="stop after this long without a /fetch; 0 runs forever (default 60)",
    )
    args = ap.parse_args()
    server = make_server(args.port)
    if args.idle_minutes > 0:
        threading.Thread(target=watch_idle, args=(server, args.idle_minutes), daemon=True).start()
    server.serve_forever()


if __name__ == "__main__":
    main()
