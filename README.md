# reddit-relay

Let your AI agent read Reddit through **your own browser session**. Posts, full comment trees,
subreddits, users and search, with no Reddit API key and no API approval.

Reddit now refuses most automated reads. Plain HTTP clients get `403` or a login wall, the
anonymous `.json` endpoints answer only browsers, new API apps need manual approval, and browser
agents such as Claude in Chrome refuse reddit.com outright. reddit-relay hands the fetch to a tiny
Chrome extension, so Reddit sees an ordinary logged-in browser.

```
CLI / MCP server ──► relay.py (127.0.0.1:8902) ◄── long-poll ── Chrome extension ──► reddit.com
       ◄──────────────── text ◄──────────────────── result ◄──────────┘   (your cookies)
```

- **Standard library only.** Python 3.9+, no `pip install`. The extension has no build step.
- **No tab automation.** The extension never opens, reads or clicks a tab. It makes background
  requests and nothing else.
- **LLM-friendly output.** Posts come back as an indented comment tree with authors, scores and
  permalinks, not raw JSON.

## Install

1. Clone this repo.
2. In Chrome, open `chrome://extensions`, turn on **Developer mode**, click **Load unpacked** and
   pick the `extension` folder. Stay logged in to Reddit in that browser.
3. Use it from the command line, or register the MCP server (below).

The relay starts by itself on the first request and stops after 60 idle minutes.

## Command line

```
python reddit.py https://www.reddit.com/r/LocalLLaMA/comments/abc123/some_post/
python reddit.py https://www.reddit.com/r/LocalLLaMA/top/?t=week --limit 10
python reddit.py --search "mcp server" -r ClaudeAI --sort top -t month
python reddit.py <address> --json      # raw Reddit JSON (search: parsed results as JSON)
python reddit.py --status              # relay version, and when the extension last polled
```

Any Reddit address works: posts, comment permalinks, subreddits and their sort tabs, users,
`old.reddit.com`, share links (`/r/<sub>/s/...`) and `redd.it` links.

A post comes back like this (an invented example):

```
Which mechanical keyboard switches are quietest?
r/keyboards · u/quiet_typist · 412 points (97% up) · 3 comments · 2026-10-02
https://www.reddit.com/r/keyboards/comments/abc123/which_switches_are_quietest/

Open office, need something my neighbours won't hate.

------------------------------------------------------------
Comments

- u/switch_nerd (120 points) https://www.reddit.com/r/keyboards/comments/abc123/x/def456/
  Silent linears with O-rings. Nothing else comes close.
  - u/quiet_typist [OP] (40 points) https://www.reddit.com/r/keyboards/comments/abc123/x/ghi789/
    Any brand you'd pick first?
[1 more replies not loaded: read a parent comment's permalink]
```

Listings and search come back one block per post: score, comment count, subreddit, date, author,
title, link, and a short text snippet.

## MCP server

```
claude mcp add reddit -- python /absolute/path/to/reddit-relay/mcp_server.py
```

Any MCP client works the same way: the command is `python mcp_server.py` over stdio. Tools:

| Tool | Does |
| --- | --- |
| `reddit_read(url, limit?)` | Any Reddit address as text: a post with its comments, or a listing. |
| `reddit_search(query, subreddit?, sort?, time?, limit?)` | Search all of Reddit or one subreddit. |

## How Reddit's blocks are handled

Reddit's "blocked by network security" page hits some JSON endpoints even inside a logged-in
browser. Measured 2026-11-05:

| Address | JSON | What reddit-relay does |
| --- | --- | --- |
| Posts, comments, users, `/r/<sub>/`, `/top/` | answers | JSON, rendered as text |
| `/r/<sub>/new/`, `/r/<sub>/hot/` | 403 | falls back to old Reddit's listing page and parses it |
| Search | 403 | reads old Reddit's search page and parses it |

A thread deeper than one request shows `[N more replies not loaded]`. Open the parent comment's
permalink, which the output prints, to read that branch.

## Files

| File | Job |
| --- | --- |
| `relay.py` | HTTP server on 127.0.0.1:8902: queues jobs, holds the extension's long-polls, returns results. |
| `reddit.py` | Client library and CLI: starts the relay, fetches, falls back, renders text. |
| `mcp_server.py` | MCP stdio server over `reddit.py`. |
| `extension/` | Manifest V3 extension. `worker.js` polls the relay and fetches with your cookies. |
| `tests/` | `python -m unittest discover -s tests`: parsing, rendering, guards and a full round trip with a fake extension. |
| `CLAUDE.md` | Notes for AI agents changing the code: checks, restarting the relay, re-probing Reddit's blocks. |

## Security

The relay can make requests as you, so it is locked down:

- It binds 127.0.0.1 only and checks the `Host` header, which defeats DNS rebinding.
- `/fetch` refuses any request with an `Origin` or `Sec-Fetch-Site` header, so no web page can
  drive it. `/job` and `/result` need an `X-Relay` header, which forces a CORS preflight that
  the relay never answers.
- Only Reddit hosts are accepted, and the extension refuses any address that resolves off
  reddit.com. It is not a general proxy for your cookies.
- Requests are spaced at least 1 second apart.

Any program running as you on the same computer can still call the relay. Treat it like your
browser's cookie jar.

## Use it politely

Every request goes out under your Reddit account. Reddit's terms forbid bulk scraping, and heavy
automated use can get an account flagged. reddit-relay is for reading the threads you would open
yourself, not for crawling.

## Development

```
python -m unittest discover -s tests
ruff check . && ruff format --check .
```

CI runs both on every push, on Python 3.9 and 3.13.

## Licence

MIT
