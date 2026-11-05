# reddit-relay: notes for agents

README.md explains what this is and how it works. This file holds what an agent changing the code
needs and the README does not say.

## Checks

Fast, before every commit (CI runs the same on push):

```
python -m unittest discover -s tests
ruff check . && ruff format --check .
node --check extension/worker.js
```

Live check, only for the path you touched: one `python reddit.py <address>` call. Each live call
is a real request under the owner's Reddit account, so keep probes few and never loop them.

## Running code is not the code on disk

- **relay.py** runs as a detached `pythonw` process that outlives your shell. After editing it,
  stop that process (the listener on 127.0.0.1:8902 whose command line names `relay.py`) and
  let the next client call start the new code. `reddit.py --status` reports the running version.
- **extension/** loads only when the owner clicks reload on Reddit Relay in `chrome://extensions`.
  An agent cannot do this: ask for it. `--status` shows `extension_version`, which is `null`
  until a worker that sends the version header has polled.
- After a relay restart, the extension's worker reconnects on its next 30 s alarm, so the first
  call can wait up to that long.

## Reddit's blocks move

The README's "How Reddit's blocks are handled" table records which endpoints Reddit refuses as
JSON, with the date measured. When an address starts failing, probe the JSON and old Reddit HTML
forms through `/fetch` (`&raw=1` fetches an address as given) before changing code, then update
that table and its date.

## Rules

- Test fixtures are synthetic. Never commit real Reddit posts or comments: they are other
  people's words, and the repo is public.
- `entries()` reads a Listing's item list as the one list among its data fields, so fixtures may
  use any key for it.
- Port 8902 is fixed: the extension's manifest grants host access to that port only.
