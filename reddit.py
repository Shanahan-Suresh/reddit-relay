"""Read Reddit through the Reddit Relay: the command-line client and the library the MCP server uses.

  python reddit.py <reddit url>                 a post with its comments, or any listing
  python reddit.py --search "query" [-r sub]    search all of Reddit or one subreddit
  python reddit.py <reddit url> --json          Reddit's raw JSON (search: the parsed results)
  python reddit.py --status                     relay and extension state

The relay starts on demand. Chrome must be open with the Reddit Relay extension enabled.

Reddit answers some addresses only as HTML, even inside a logged-in browser: it refuses
search.json and the /new and /hot listing JSON with "blocked by network security". Those
go through old Reddit's pages, whose markup is stable, and are parsed here.
"""

import argparse
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from html import unescape
from pathlib import Path

from relay import PORT, visible_text

RELAY = f"http://127.0.0.1:{PORT}"
READ_LIMIT = 500  # comments or items requested when reading an address
SEARCH_LIMIT = 25  # search results requested; old Reddit serves at most 100
SNIPPET_CHARS = 300


class RelayError(Exception):
    pass


# --- relay client ----------------------------------------------------------------------------


def ping(relay=RELAY):
    """The relay's identity and state, or None when nothing (or something else) answers."""
    try:
        with urllib.request.urlopen(relay + "/api/ping", timeout=2) as r:
            info = json.loads(r.read())
        return info if info.get("app") == "reddit-relay" else None
    except (OSError, ValueError):
        return None


def ensure_relay(relay=RELAY):
    """Start relay.py in the background when it is not answering."""
    if ping(relay):
        return
    cmd = [sys.executable, str(Path(__file__).resolve().with_name("relay.py"))]
    kw = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, close_fds=True)
    if os.name == "nt":
        pyw = Path(sys.executable).with_name("pythonw.exe")
        if pyw.exists():
            cmd[0] = str(pyw)
        kw["creationflags"] = 0x00000008 | 0x00000200 | 0x08000000  # DETACHED, NEW_GROUP, NO_WINDOW
    else:
        kw["start_new_session"] = True
    subprocess.Popen(cmd, **kw)
    for _ in range(25):
        time.sleep(0.2)
        if ping(relay):
            return
    raise RelayError(f"the relay did not start on 127.0.0.1:{PORT}; is another program using that port?")


def _relay_fetch(params, relay, timeout):
    q = urllib.parse.urlencode({**params, "timeout": timeout})
    try:
        with urllib.request.urlopen(f"{relay}/fetch?{q}", timeout=timeout + 15) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        with e:
            body = e.read()
        try:
            msg = json.loads(body).get("error")
        except ValueError:
            msg = None
        raise RelayError(msg or f"relay returned HTTP {e.code}") from None
    except urllib.error.URLError as e:
        raise RelayError(f"cannot reach the relay: {e.reason}") from None


def fetch(url, limit=READ_LIMIT, relay=RELAY, timeout=45):
    """Reddit's parsed JSON for a Reddit address, fetched by the extension."""
    return _relay_fetch({"url": url, "limit": limit}, relay, timeout)


def fetch_raw(url, relay=RELAY, timeout=45):
    """{"url", "body"} for a Reddit address fetched exactly as given (HTML or RSS)."""
    return _relay_fetch({"url": url, "raw": "1"}, relay, timeout)


# --- Reddit JSON -----------------------------------------------------------------------------


def entries(listing):
    """The item list of a Reddit Listing: the one list among its data fields."""
    return next(v for v in listing["data"].values() if isinstance(v, list))


def day(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else ""


def indent(text, pad):
    return "\n".join(pad + line for line in text.splitlines()) if text else ""


def json_result(item):
    """One post (t3) or comment (t1) from a JSON listing, as a result dict."""
    d = item["data"]
    link = item["kind"] == "t3"
    return {
        "title": d.get("title", "") if link else f"Comment on: {d.get('link_title', '')}",
        "url": "https://www.reddit.com" + d.get("permalink", ""),
        "score": f"{d.get('score')} points",
        "comments": f"{d.get('num_comments')} comments" if link else "",
        "date": day(d.get("created_utc")),
        "author": d.get("author", ""),
        "subreddit": f"r/{d['subreddit']}" if d.get("subreddit") else "",
        "snippet": d.get("selftext", "") if link else d.get("body", ""),
    }


def render_post(data):
    """A post page ([post listing, comment listing]) as the post plus an indented comment tree."""
    p = entries(data[0])[0]["data"]
    op = p["author"]
    out = [
        p["title"],
        f"r/{p['subreddit']} · u/{op} · {p['score']} points ({round(p.get('upvote_ratio', 0) * 100)}% up)"
        f" · {p['num_comments']} comments · {day(p['created_utc'])}",
        f"https://www.reddit.com{p['permalink']}",
    ]
    if not p.get("is_self") and p.get("url"):
        out.append(f"Link: {p['url']}")
    if p.get("selftext"):
        out += ["", p["selftext"]]
    out += ["", "-" * 60, "Comments", ""]

    def walk(items, depth):
        pad = "  " * depth
        for c in items:
            d = c["data"]
            if c["kind"] == "more":
                if d.get("count"):
                    out.append(
                        f"{pad}[{d['count']} more replies not loaded: read a parent comment's permalink]"
                    )
                continue
            tag = " [OP]" if d.get("author") == op else ""
            out.append(
                f"{pad}- u/{d.get('author')}{tag} ({d.get('score')} points) "
                f"https://www.reddit.com{d.get('permalink', '')}"
            )
            out.append(indent(d.get("body", ""), pad + "  "))
            if isinstance(d.get("replies"), dict):
                walk(entries(d["replies"]), depth + 1)

    walk(entries(data[1]), 0)
    return "\n".join(out)


# --- old Reddit HTML -------------------------------------------------------------------------


def _first(pattern, text):
    m = re.search(pattern, text, re.S)
    return unescape(m.group(1)).strip() if m else ""


def _strip_tags(html):
    return re.sub(r"<[^>]+>", "", html)


def search_url(query, subreddit=None, sort="relevance", time_range="all", limit=SEARCH_LIMIT):
    q = {"q": query, "sort": sort, "t": time_range, "limit": limit}
    if subreddit:
        q["restrict_sr"] = "on"
        return f"https://old.reddit.com/r/{subreddit.removeprefix('r/')}/search?{urllib.parse.urlencode(q)}"
    return f"https://old.reddit.com/search?{urllib.parse.urlencode(q)}"


def parse_search(html):
    """Post results from an old Reddit search page, as result dicts."""
    results = []
    for block in html.split('<div class=" search-result search-result-link')[1:]:
        results.append(
            {
                "title": _strip_tags(_first(r'class="search-title[^"]*"[^>]*>(.*?)</a>', block)),
                "url": _first(r'<a href="([^"]+)" class="search-title', block).replace("://old.", "://www."),
                "score": _first(r'class="search-score">([^<]*)<', block),
                "comments": _first(r'class="search-comments[^"]*"[^>]*>([^<]*)<', block),
                "date": _first(r'datetime="([^"T]+)', block),
                "author": _first(r'class="author[^"]*"[^>]*>([^<]*)<', block),
                "subreddit": _first(r'class="search-subreddit-link[^"]*"[^>]*>([^<]*)<', block),
                "snippet": visible_text(
                    _first(r'<div class="search-result-body"><div class="md">(.*?)</div>', block)
                ),
            }
        )
    return results


def parse_listing_html(html):
    """Posts from an old Reddit listing page (a subreddit or user), as result dicts."""
    results = []
    for m in re.finditer(r'<div class=" thing id-t3_[^>]+>', html):
        a = dict(re.findall(r'data-([a-z-]+)="([^"]*)"', m.group(0)))
        if a.get("promoted") == "true":
            continue
        results.append(
            {
                "title": _strip_tags(
                    _first(r'<a class="title[^"]*"[^>]*>(.*?)</a>', html[m.end() : m.end() + 4000])
                ),
                "url": "https://www.reddit.com" + unescape(a.get("permalink", "")),
                "score": f"{a.get('score', '?')} points",
                "comments": f"{a.get('comments-count', '?')} comments",
                "date": day(int(a.get("timestamp") or 0) / 1000),
                "author": a.get("author", ""),
                "subreddit": a.get("subreddit-prefixed", ""),
                "snippet": "",
            }
        )
    return results


def render_results(results):
    """Result dicts (from search, listings or JSON) as text: one block per post or comment."""
    out = []
    for r in results:
        meta = [r["score"], r["comments"], r["subreddit"], r["date"], r["author"] and f"u/{r['author']}"]
        snippet = " ".join(r["snippet"].split())
        if len(snippet) > SNIPPET_CHARS:
            snippet = snippet[:SNIPPET_CHARS] + "..."
        out.append(
            " · ".join(x for x in meta if x)
            + f"\n  {r['title']}\n  {r['url']}"
            + (f"\n  {snippet}" if snippet else "")
        )
    return "\n\n".join(out) or "(no results)"


# --- public API ------------------------------------------------------------------------------


def render(data):
    """Reddit JSON as text: a post page, or any listing."""
    if isinstance(data, list):
        return render_post(data)
    return render_results([json_result(c) for c in entries(data) if c["kind"] in ("t1", "t3")])


def search(query, subreddit=None, sort="relevance", time_range="all", limit=SEARCH_LIMIT, relay=RELAY):
    """Search results as text."""
    page = fetch_raw(search_url(query, subreddit, sort, time_range, min(limit, 100)), relay=relay)
    return render_results(parse_search(page["body"]))


def read(url, limit=READ_LIMIT, relay=RELAY):
    """Any Reddit address as text. A search address goes through the search page, and a listing
    whose JSON Reddit refuses falls back to old Reddit's listing page."""
    u = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    q = dict(urllib.parse.parse_qsl(u.query))
    if u.path.rstrip("/").endswith("/search"):
        sub = re.match(r"/r/([^/]+)/search", u.path)
        return search(
            q.get("q", ""),
            sub.group(1) if sub and q.get("restrict_sr") else None,
            q.get("sort", "relevance"),
            q.get("t", "all"),
            int(q.get("limit", SEARCH_LIMIT)),
            relay,
        )
    try:
        return render(fetch(url, limit, relay))
    except RelayError as e:
        if "HTTP 403" not in str(e) or "/comments/" in u.path or u.hostname == "redd.it":
            raise
        q.setdefault("limit", str(min(limit, 100)))
        old = u._replace(scheme="https", netloc="old.reddit.com", query=urllib.parse.urlencode(q)).geturl()
        results = parse_listing_html(fetch_raw(old, relay)["body"])
        if not results:
            raise
        return render_results(results)


# --- command line ----------------------------------------------------------------------------


def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Read Reddit through the Reddit Relay.")
    ap.add_argument("url", nargs="?", help="any Reddit address: post, comment, subreddit, user, search")
    ap.add_argument("--search", metavar="QUERY", help="search Reddit instead of reading an address")
    ap.add_argument("-r", "--subreddit", help="with --search: search only this subreddit")
    ap.add_argument("--sort", default="relevance", help="with --search: relevance, top, new, comments")
    ap.add_argument("-t", "--time", default="all", help="with --search: hour, day, week, month, year, all")
    ap.add_argument(
        "--limit",
        type=int,
        help=f"items to request (default {READ_LIMIT} for an address, {SEARCH_LIMIT} for a search)",
    )
    ap.add_argument(
        "--json", action="store_true", help="print Reddit's raw JSON (search: the parsed results)"
    )
    ap.add_argument("--status", action="store_true", help="show relay and extension state")
    args = ap.parse_args()
    try:
        ensure_relay()
        if args.status or not (args.url or args.search):
            print(json.dumps(ping(), indent=2))
        elif args.search:
            limit = args.limit or SEARCH_LIMIT
            if args.json:
                page = fetch_raw(
                    search_url(args.search, args.subreddit, args.sort, args.time, min(limit, 100))
                )
                print(json.dumps(parse_search(page["body"]), ensure_ascii=False, indent=1))
            else:
                print(search(args.search, args.subreddit, args.sort, args.time, limit))
        elif args.json:
            print(json.dumps(fetch(args.url, args.limit or READ_LIMIT), ensure_ascii=False))
        else:
            print(read(args.url, args.limit or READ_LIMIT))
    except RelayError as e:
        sys.exit(f"reddit-relay: {e}")


if __name__ == "__main__":
    main()
