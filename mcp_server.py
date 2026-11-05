"""Reddit Relay MCP server (stdio, standard library only).

Tools:
  reddit_read    read any Reddit address: a post with its comments, a subreddit, a user, a search
  reddit_search  search all of Reddit or one subreddit

Register with Claude Code:
  claude mcp add reddit -- python /absolute/path/to/mcp_server.py
"""

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import reddit  # noqa: E402
from relay import VERSION  # noqa: E402

TOOLS = [
    {
        "name": "reddit_read",
        "description": (
            "Read a Reddit page as plain text, fetched through the user's own browser session. "
            "A post address returns the post and its comment tree; a subreddit, user, or search "
            "address returns the listing. Accepts reddit.com, old.reddit.com, share links (/s/) "
            "and redd.it links. Add query parameters to the address for sorting, for example "
            "https://www.reddit.com/r/python/top/?t=week."
        ),
        "inputSchema": {
            "type": "object",
            "properties": {
                "url": {"type": "string", "description": "Any Reddit address."},
                "limit": {"type": "integer", "description": "Comments or items to request (default 500)."},
            },
            "required": ["url"],
        },
    },
    {
        "name": "reddit_search",
        "description": "Search Reddit posts, across all of Reddit or inside one subreddit.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "subreddit": {"type": "string", "description": "Optional subreddit name, without r/."},
                "sort": {"type": "string", "enum": ["relevance", "top", "new", "comments"]},
                "time": {"type": "string", "enum": ["hour", "day", "week", "month", "year", "all"]},
                "limit": {"type": "integer", "description": "Results to request (default 25)."},
            },
            "required": ["query"],
        },
    },
]


def call_tool(name, args):
    reddit.ensure_relay()
    if name == "reddit_read":
        return reddit.read(args["url"], int(args.get("limit", 500)))
    if name == "reddit_search":
        return reddit.search(
            args["query"],
            args.get("subreddit"),
            args.get("sort", "relevance"),
            args.get("time", "all"),
            min(int(args.get("limit", 25)), 100),
        )
    raise ValueError(f"unknown tool: {name}")


def handle(msg):
    method, mid, params = msg.get("method"), msg.get("id"), msg.get("params") or {}
    if mid is None:
        return None  # a notification needs no answer
    if method == "initialize":
        result = {
            "protocolVersion": params.get("protocolVersion", "2025-06-18"),
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "reddit-relay", "version": VERSION},
        }
    elif method == "tools/list":
        result = {"tools": TOOLS}
    elif method == "tools/call":
        try:
            text = call_tool(params.get("name"), params.get("arguments") or {})
            result = {"content": [{"type": "text", "text": text}]}
        except (reddit.RelayError, ValueError, KeyError) as e:
            result = {"content": [{"type": "text", "text": f"reddit-relay: {e}"}], "isError": True}
    elif method == "ping":
        result = {}
    else:
        return {
            "jsonrpc": "2.0",
            "id": mid,
            "error": {"code": -32601, "message": f"unknown method: {method}"},
        }
    return {"jsonrpc": "2.0", "id": mid, "result": result}


def main():
    stdin = open(sys.stdin.fileno(), encoding="utf-8", closefd=False)
    for line in stdin:
        if not line.strip():
            continue
        try:
            reply = handle(json.loads(line))
        except ValueError:
            reply = {"jsonrpc": "2.0", "id": None, "error": {"code": -32700, "message": "parse error"}}
        if reply is not None:
            sys.stdout.buffer.write((json.dumps(reply, ensure_ascii=False) + "\n").encode("utf-8"))
            sys.stdout.buffer.flush()


if __name__ == "__main__":
    main()
