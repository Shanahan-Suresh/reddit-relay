"""Fast checks: address handling, rendering, the request guards and a full round trip
through a real relay with a fake extension. Run: python -m unittest discover -s tests"""

import json
import sys
import threading
import unittest
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import mcp_server  # noqa: E402
import reddit  # noqa: E402
import relay  # noqa: E402

relay.MIN_GAP = 0


def listing(items):
    # entries() reads the one list in a Listing's data, whatever its key.
    return {"kind": "Listing", "data": {"after": None, "items": items}}


POST = [
    listing(
        [
            {
                "kind": "t3",
                "data": {
                    "title": "Test post",
                    "subreddit": "test",
                    "author": "op1",
                    "score": 10,
                    "upvote_ratio": 0.9,
                    "num_comments": 3,
                    "created_utc": 1762300000,
                    "permalink": "/r/test/comments/abc/t/",
                    "is_self": True,
                    "selftext": "Body text",
                },
            }
        ]
    ),
    listing(
        [
            {
                "kind": "t1",
                "data": {
                    "author": "bob",
                    "score": 3,
                    "body": "Nice",
                    "permalink": "/c/1/",
                    "replies": listing(
                        [
                            {
                                "kind": "t1",
                                "data": {
                                    "author": "op1",
                                    "score": 2,
                                    "body": "Thanks",
                                    "permalink": "/c/2/",
                                    "replies": "",
                                },
                            }
                        ]
                    ),
                },
            },
            {"kind": "more", "data": {"count": 4}},
        ]
    ),
]


class MakeJob(unittest.TestCase):
    def test_post_goes_to_www_with_json(self):
        job = relay.make_job("https://old.reddit.com/r/test/comments/abc/t/")
        self.assertEqual(job["url"], "https://www.reddit.com/r/test/comments/abc/t/")
        self.assertTrue(job["json"])
        self.assertEqual(job["params"]["raw_json"], "1")
        self.assertNotIn("resolve", job)

    def test_query_survives(self):
        job = relay.make_job("https://www.reddit.com/r/test/top/?t=week&limit=5")
        self.assertEqual(job["url"], "https://www.reddit.com/r/test/top/")
        self.assertEqual(job["params"]["t"], "week")
        self.assertEqual(job["params"]["limit"], "5")

    def test_raw_keeps_address(self):
        url = reddit.search_url("mouse", "test", "top", "week")
        job = relay.make_job(url, raw=True)
        self.assertEqual(job, {"url": url, "json": False})
        self.assertIn("old.reddit.com/r/test/search?q=mouse", url)
        self.assertIn("restrict_sr=on", url)

    def test_share_and_short_links_resolve(self):
        self.assertTrue(relay.make_job("reddit.com/r/test/s/AbCd")["resolve"])
        self.assertTrue(relay.make_job("https://redd.it/abc")["resolve"])

    def test_other_hosts_refused(self):
        for bad in ("https://example.com/r/x", "https://reddit.com.evil.io/x", "https://notreddit.com/"):
            with self.assertRaises(ValueError):
                relay.make_job(bad)


class Render(unittest.TestCase):
    def test_post_tree(self):
        text = reddit.render(POST)
        self.assertIn("Test post", text)
        self.assertIn("- u/bob (3 points)", text)
        self.assertIn("  - u/op1 [OP] (2 points)", text)
        self.assertIn("[4 more replies not loaded", text)

    def test_listing(self):
        text = reddit.render(
            listing(
                [
                    {
                        "kind": "t3",
                        "data": {
                            "title": "A",
                            "subreddit": "test",
                            "score": 5,
                            "num_comments": 1,
                            "author": "x",
                            "created_utc": 1762300000,
                            "permalink": "/r/test/a/",
                            "selftext": "word " * 100,
                        },
                    },
                    {
                        "kind": "t1",
                        "data": {
                            "link_title": "B",
                            "subreddit": "test",
                            "score": 2,
                            "author": "y",
                            "created_utc": 1762300000,
                            "permalink": "/r/test/b/c/",
                            "body": "Reply",
                        },
                    },
                ]
            )
        )
        self.assertIn(
            "5 points · 1 comments · r/test · 2025-11-04 · u/x\n  A\n  https://www.reddit.com/r/test/a/", text
        )
        self.assertIn("...", text)  # the long self text is cut to a snippet
        self.assertIn("2 points · r/test · 2025-11-04 · u/y\n  Comment on: B", text)
        self.assertEqual(reddit.render(listing([])), "(no results)")

    def test_search_page(self):
        html = (
            '<div class="contents"><div class=" search-result search-result-link has-thumbnail" '
            'data-fullname="t3_abc"><header class="search-result-header"><a href="https://old.reddit.com'
            '/r/test/comments/abc/t/" class="search-title may-blank" >Fish &amp; chips</a></header>'
            '<span class="search-score">1,467 points</span> <a href="x" class="search-comments may-blank" >'
            '138 comments</a> <time title="t" datetime="2026-07-09T19:04:47+00:00">3 months ago</time> '
            '<a href="u" class="author may-blank id-t2_x" >someone</a> <a href="s" '
            'class="search-subreddit-link may-blank" >r/test</a><div class="search-result-body">'
            '<div class="md"><p>Snippet <em>text</em></p></div></div></div>'
        )
        [r] = reddit.parse_search(html)
        self.assertEqual(r["title"], "Fish & chips")
        self.assertEqual(r["url"], "https://www.reddit.com/r/test/comments/abc/t/")
        self.assertEqual(
            (r["score"], r["comments"], r["date"]), ("1,467 points", "138 comments", "2026-07-09")
        )
        self.assertEqual((r["author"], r["subreddit"], r["snippet"]), ("someone", "r/test", "Snippet text"))
        self.assertIn("u/someone", reddit.render_results([r]))
        self.assertEqual(reddit.parse_search("<html>no results</html>"), [])

    def test_old_listing_page(self):
        html = (
            '<div class=" thing id-t3_abc odd link self" data-fullname="t3_abc" data-author="someone" '
            'data-subreddit-prefixed="r/test" data-timestamp="1793880943000" '
            'data-permalink="/r/test/comments/abc/t/" data-comments-count="7" data-score="12" '
            'data-promoted="false" ><p class="title">'
            '<a class="title may-blank" href="/r/test/comments/abc/t/" >A title</a></p></div>'
            '<div class=" thing id-t3_ad" data-promoted="true" data-permalink="/ad/">'
            '<a class="title">Ad</a></div>'
        )
        [r] = reddit.parse_listing_html(html)
        self.assertEqual((r["title"], r["score"], r["comments"]), ("A title", "12 points", "7 comments"))
        self.assertEqual(
            (r["url"], r["subreddit"]), ("https://www.reddit.com/r/test/comments/abc/t/", "r/test")
        )
        self.assertEqual(r["date"], "2026-11-05")


class RoundTrip(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.server = relay.make_server(0)
        cls.base = f"http://127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()

    def fake_extension(self, body, status=200):
        req = urllib.request.Request(self.base + "/job", headers={"X-Relay": "1"})
        with urllib.request.urlopen(req, timeout=10) as r:
            job = json.loads(r.read())
        result = {"id": job["id"], "ok": status == 200, "status": status, "url": job["url"], "body": body}
        req = urllib.request.Request(
            self.base + "/result",
            data=json.dumps(result).encode(),
            headers={"X-Relay": "1", "Content-Type": "application/json"},
        )
        urllib.request.urlopen(req, timeout=10).close()

    def test_fetch_returns_extension_body(self):
        t = threading.Thread(target=self.fake_extension, args=(json.dumps(POST),))
        t.start()
        data = reddit.fetch("https://www.reddit.com/r/test/comments/abc/t/", relay=self.base, timeout=10)
        t.join()
        self.assertEqual(data, POST)

    def test_reddit_error_becomes_relay_error(self):
        t = threading.Thread(target=self.fake_extension, args=("blocked", 403))
        t.start()
        with self.assertRaisesRegex(reddit.RelayError, "HTTP 403"):
            reddit.fetch("https://www.reddit.com/r/test/", relay=self.base, timeout=10)
        t.join()

    def test_timeout_explains_missing_extension(self):
        with self.assertRaisesRegex(reddit.RelayError, "no answer"):
            reddit.fetch("https://www.reddit.com/r/test/", relay=self.base, timeout=0.3)

    def expect_403(self, path, headers):
        req = urllib.request.Request(self.base + path, headers=headers)
        with self.assertRaises(urllib.error.HTTPError) as cm:
            urllib.request.urlopen(req, timeout=5)
        cm.exception.close()
        self.assertEqual(cm.exception.code, 403)

    def test_guards(self):
        self.expect_403("/fetch?url=reddit.com/r/test", {"Origin": "https://evil.example"})
        self.expect_403("/fetch?url=reddit.com/r/test", {"Sec-Fetch-Site": "cross-site"})
        self.expect_403("/job", {})
        self.expect_403("/api/ping", {"Host": "evil.example:8902"})


class Mcp(unittest.TestCase):
    def test_handshake_and_tools(self):
        init = mcp_server.handle(
            {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {"protocolVersion": "2025-06-18"}}
        )
        self.assertEqual(init["result"]["serverInfo"]["name"], "reddit-relay")
        self.assertIsNone(mcp_server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        tools = mcp_server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})["result"]["tools"]
        self.assertEqual({t["name"] for t in tools}, {"reddit_read", "reddit_search"})
        err = mcp_server.handle({"jsonrpc": "2.0", "id": 3, "method": "nope"})
        self.assertEqual(err["error"]["code"], -32601)


if __name__ == "__main__":
    unittest.main()
