// Reddit Relay worker: takes fetch jobs from the local relay, fetches each one with this
// browser's Reddit cookies, and posts the response back. It never opens or touches a tab.
const RELAY = "http://127.0.0.1:8902";
const HEADERS = { "X-Relay": "1", "X-Relay-Version": chrome.runtime.getManifest().version };
const REDDIT_HOST = /(^|\.)reddit\.com$/;
const FETCH_TIMEOUT_MS = 30000;

let running = false;

function setStatus(connected) {
  chrome.action.setTitle({
    title: connected
      ? "Reddit Relay: connected to the relay on 127.0.0.1:8902"
      : "Reddit Relay: the relay is not running (it starts when a client asks for Reddit)",
  });
}

async function loop() {
  if (running) return;
  running = true;
  try {
    while (true) {
      // An extension API call resets the service worker's idle timer, so the worker
      // stays alive while the relay is up and sleeps when it is not.
      await chrome.runtime.getPlatformInfo();
      let res;
      try {
        res = await fetch(`${RELAY}/job`, { headers: HEADERS, cache: "no-store" });
      } catch (e) {
        setStatus(false);
        return; // relay is down; the alarm checks again in 30 s
      }
      if (!res.ok && res.status !== 204) {
        setStatus(false);
        return;
      }
      setStatus(true);
      if (res.status === 204) continue; // long-poll ended with no job
      await handle(await res.json());
    }
  } finally {
    running = false;
  }
}

async function handle(job) {
  const out = { id: job.id };
  try {
    let url = job.url;
    if (job.resolve) {
      // Share links (/s/...) and redd.it links redirect to the real address.
      const r = await fetch(url, { credentials: "include", cache: "no-store",
                                   signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
      url = r.url;
    }
    const u = new URL(url);
    if (!REDDIT_HOST.test(u.hostname)) throw new Error(`not a reddit.com address: ${u.hostname}`);
    if (job.json && !u.pathname.endsWith(".json")) {
      u.pathname = u.pathname.replace(/\/+$/, "") + ".json";
    }
    for (const [k, v] of Object.entries(job.params || {})) u.searchParams.set(k, v);
    const r = await fetch(u.toString(), { credentials: "include", cache: "no-store",
                                          signal: AbortSignal.timeout(FETCH_TIMEOUT_MS) });
    out.ok = r.ok;
    out.status = r.status;
    out.url = r.url;
    out.body = await r.text();
  } catch (e) {
    out.ok = false;
    out.error = String(e);
  }
  try {
    await fetch(`${RELAY}/result`, {
      method: "POST",
      headers: { ...HEADERS, "Content-Type": "application/json" },
      body: JSON.stringify(out),
    });
  } catch (e) {
    // The relay went away mid-job; nobody is waiting for the answer.
  }
}

chrome.alarms.create("wake", { periodInMinutes: 0.5 });
chrome.alarms.onAlarm.addListener(loop);
chrome.runtime.onStartup.addListener(loop);
chrome.runtime.onInstalled.addListener(loop);
loop();
