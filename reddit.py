"""Read Reddit through the Reddit Relay: the command-line client and the library the MCP server uses.

  python reddit.py <reddit url>                      a post with its comments, or any listing
  python reddit.py --search "query" [-r sub]         search all of Reddit or one subreddit
  python reddit.py <reddit url> --json               Reddit's raw JSON
  python reddit.py --status                          relay and extension state

The relay starts on demand. Chrome must be open with the Reddit Relay extension enabled.
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

from relay import visible_text

RELAY = "http://127.0.0.1:8902"
HERE = Path(__file__).resolve().parent


class RelayError(Exception):
    pass


def ping(relay=RELAY):
    try:
        with urllib.request.urlopen(relay + "/api/ping", timeout=2) as r:
            info = json.loads(r.read())
        return info if info.get("app") == "reddit-relay" else None
    except (OSError, ValueError):
        return None


def ensure_relay(relay=RELAY):
    """Start relay.py in the background when nothing answers on the relay port."""
    if ping(relay):
        return
    cmd = [sys.executable, str(HERE / "relay.py")]
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
    raise RelayError("the relay did not start on 127.0.0.1:8902; is another program using that port?")


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


def fetch(url, limit=500, relay=RELAY, timeout=45):
    """Return Reddit's parsed JSON for any Reddit address, fetched by the extension."""
    return _relay_fetch({"url": url, "limit": limit}, relay, timeout)


def fetch_raw(url, relay=RELAY, timeout=45):
    """Return {"url", "body"} for a Reddit address fetched as-is (HTML or RSS), not as JSON."""
    return _relay_fetch({"url": url, "raw": "1"}, relay, timeout)


# Reddit refuses search.json ("blocked by network security") even with a browser session,
# so search reads old Reddit's search page, whose markup is stable, and parses it.
def search_url(query, subreddit=None, sort="relevance", time_range="all", limit=25):
    q = {"q": query, "sort": sort, "t": time_range, "limit": limit}
    if subreddit:
        q["restrict_sr"] = "on"
        return f"https://old.reddit.com/r/{subreddit.removeprefix('r/')}/search?{urllib.parse.urlencode(q)}"
    return f"https://old.reddit.com/search?{urllib.parse.urlencode(q)}"


def _first(pattern, text):
    m = re.search(pattern, text, re.S)
    return unescape(m.group(1)).strip() if m else ""


def parse_search(html):
    """Post results from an old Reddit search page, as dicts."""
    results = []
    for block in html.split('<div class=" search-result search-result-link')[1:]:
        url = _first(r'<a href="([^"]+)" class="search-title', block)
        results.append({
            "title": re.sub(r"<[^>]+>", "", _first(r'class="search-title[^"]*"[^>]*>(.*?)</a>', block)),
            "url": url.replace("://old.reddit.com", "://www.reddit.com"),
            "score": _first(r'class="search-score">([^<]*)<', block),
            "comments": _first(r'class="search-comments[^"]*"[^>]*>([^<]*)<', block),
            "date": _first(r'datetime="([^"T]+)', block),
            "author": _first(r'class="author[^"]*"[^>]*>([^<]*)<', block),
            "subreddit": _first(r'class="search-subreddit-link[^"]*"[^>]*>([^<]*)<', block),
            "snippet": visible_text(_first(r'<div class="search-result-body"><div class="md">(.*?)</div>', block)),
        })
    return results


def parse_listing_html(html):
    """Posts from an old Reddit listing page (subreddit, user), as the same dicts as parse_search."""
    results = []
    for m in re.finditer(r'<div class=" thing id-t3_[^>]+>', html):
        a = dict(re.findall(r'data-([a-z-]+)="([^"]*)"', m.group(0)))
        if a.get("promoted") == "true":
            continue
        title = _first(r'<a class="title[^"]*"[^>]*>(.*?)</a>', html[m.end():m.end() + 4000])
        ts = int(a.get("timestamp") or 0) / 1000
        results.append({
            "title": re.sub(r"<[^>]+>", "", title),
            "url": "https://www.reddit.com" + unescape(a.get("permalink", "")),
            "score": f"{a.get('score', '?')} points",
            "comments": f"{a.get('comments-count', '?')} comments",
            "date": datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d") if ts else "",
            "author": a.get("author", ""),
            "subreddit": a.get("subreddit-prefixed", ""),
            "snippet": "",
        })
    return results


def render_results(results, snippet_chars=300):
    out = []
    for r in results:
        meta = [r["score"], r["comments"], r["subreddit"], r["date"], r["author"] and f"u/{r['author']}"]
        snippet = r["snippet"][:snippet_chars] + ("..." if len(r["snippet"]) > snippet_chars else "")
        out.append(" · ".join(x for x in meta if x) + f"\n  {r['title']}\n  {r['url']}"
                   + (f"\n  {snippet}" if snippet else ""))
    return "\n\n".join(out) or "(no results)"


def search(query, subreddit=None, sort="relevance", time_range="all", limit=25, relay=RELAY):
    page = fetch_raw(search_url(query, subreddit, sort, time_range, limit), relay=relay)
    return render_results(parse_search(page["body"]))


def read(url, limit=500, relay=RELAY):
    """Any Reddit address as text. Search addresses go through the search page parser, and a
    listing whose JSON Reddit refuses (it blocks /new and /hot) falls back to old Reddit's page."""
    u = urllib.parse.urlparse(url if "://" in url else "https://" + url)
    if u.path.rstrip("/").endswith("/search"):
        q = dict(urllib.parse.parse_qsl(u.query))
        sub = re.match(r"/r/([^/]+)/search", u.path)
        return search(q.get("q", ""), sub.group(1) if sub and q.get("restrict_sr") else None,
                      q.get("sort", "relevance"), q.get("t", "all"), int(q.get("limit", 25)), relay)
    try:
        return render(fetch(url, limit, relay))
    except RelayError as e:
        if "HTTP 403" not in str(e) or "/comments/" in u.path or u.hostname == "redd.it":
            raise
        q = dict(urllib.parse.parse_qsl(u.query))
        q.setdefault("limit", str(min(limit, 100)))
        old = u._replace(scheme="https", netloc="old.reddit.com", query=urllib.parse.urlencode(q)).geturl()
        results = parse_listing_html(fetch_raw(old, relay)["body"])
        if not results:
            raise
        return render_results(results)


# --- rendering -------------------------------------------------------------------------------

def entries(listing):
    """The item list of a Reddit Listing: the one list among its data fields."""
    return next(v for v in listing["data"].values() if isinstance(v, list))


def when(ts):
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def indent(text, pad):
    return "\n".join(pad + line for line in text.splitlines()) if text else ""


def render_post(data):
    p = entries(data[0])[0]["data"]
    out = [p["title"],
           f"r/{p['subreddit']} · u/{p['author']} · {p['score']} points "
           f"({round(p.get('upvote_ratio', 0) * 100)}% up) · {p['num_comments']} comments · {when(p['created_utc'])}",
           f"https://www.reddit.com{p['permalink']}"]
    if not p.get("is_self") and p.get("url"):
        out.append(f"Link: {p['url']}")
    if p.get("selftext"):
        out += ["", p["selftext"]]
    out += ["", "-" * 60, "Comments", ""]
    op = p["author"]

    def walk(items, depth):
        for c in items:
            pad = "  " * depth
            d = c["data"]
            if c["kind"] == "more":
                if d.get("count"):
                    out.append(f"{pad}[{d['count']} more replies not loaded: open a parent comment's permalink to see them]")
                continue
            tag = " [OP]" if d.get("author") == op else ""
            out.append(f"{pad}- u/{d.get('author')}{tag} ({d.get('score')} points) "
                       f"https://www.reddit.com{d.get('permalink', '')}")
            out.append(indent(d.get("body", ""), pad + "  "))
            if isinstance(d.get("replies"), dict):
                walk(entries(d["replies"]), depth + 1)

    walk(entries(data[1]), 0)
    return "\n".join(out)


def render_listing(data):
    out = []
    for c in entries(data):
        d = c["data"]
        if c["kind"] == "t3":
            out.append(f"{d['score']:>6} pts {d['num_comments']:>5} comments  r/{d['subreddit']}  {d['title']}\n"
                       f"{'':>26}https://www.reddit.com{d['permalink']}")
        elif c["kind"] == "t1":
            out.append(f"u/{d['author']} ({d['score']} points) in r/{d['subreddit']}: {d.get('link_title', '')}\n"
                       f"{indent(d['body'], '  ')}\n  https://www.reddit.com{d['permalink']}")
    return "\n".join(out) or "(no results)"


def render(data):
    return render_post(data) if isinstance(data, list) else render_listing(data)


# --- command line ----------------------------------------------------------------------------

def main():
    sys.stdout.reconfigure(encoding="utf-8")
    ap = argparse.ArgumentParser(description="Read Reddit through the Reddit Relay.")
    ap.add_argument("url", nargs="?", help="any Reddit address: post, comment, subreddit, user, search")
    ap.add_argument("--search", metavar="QUERY", help="search Reddit instead of reading an address")
    ap.add_argument("-r", "--subreddit", help="with --search: search only this subreddit")
    ap.add_argument("--sort", default="relevance", help="with --search: relevance, top, new, comments")
    ap.add_argument("-t", "--time", default="all", help="with --search: hour, day, week, month, year, all")
    ap.add_argument("--limit", type=int, default=500, help="comments or items to request (default 500)")
    ap.add_argument("--json", action="store_true", help="print Reddit's raw JSON")
    ap.add_argument("--status", action="store_true", help="show relay and extension state")
    args = ap.parse_args()
    try:
        ensure_relay()
        if args.status or not (args.url or args.search):
            print(json.dumps(ping(), indent=2))
            return
        if args.search:
            limit = min(args.limit, 100)
            if args.json:
                page = fetch_raw(search_url(args.search, args.subreddit, args.sort, args.time, limit))
                print(json.dumps(parse_search(page["body"]), ensure_ascii=False, indent=1))
            else:
                print(search(args.search, args.subreddit, args.sort, args.time, limit))
        elif args.json:
            print(json.dumps(fetch(args.url, args.limit), ensure_ascii=False))
        else:
            print(read(args.url, args.limit))
    except RelayError as e:
        sys.exit(f"reddit-relay: {e}")


if __name__ == "__main__":
    main()
