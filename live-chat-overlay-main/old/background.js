// Relays live Gift Combo count updates from the YouTube content
// script (youtube.js) to the local Timer_combo4.py Flask server.
//
// This runs in the extension's BACKGROUND service worker, not on the
// youtube.com page itself. That matters: a fetch() made directly from
// a content script inherits the page's origin (https://youtube.com)
// for CORS purposes, and browsers increasingly block a public https
// page from reaching a private/loopback address like 127.0.0.1
// (Private Network Access). A fetch from the background service
// worker is NOT subject to that page-context restriction - it only
// needs the target host listed under "host_permissions" in
// manifest.json, which has been added there.

const TIMER_SERVER_URL = "http://127.0.0.1:5000/combo/live-update";

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  if (!message || message.type !== "COMBO_UPDATE") {
    return false; // not for us, let other listeners (if any) handle it
  }

  fetch(TIMER_SERVER_URL, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      user: message.user,
      gift: message.gift,
      count: message.count
    })
  })
    .then((res) => res.json().catch(() => ({})))
    .then((data) => {
      sendResponse({ ok: true, data });
    })
    .catch((err) => {
      // Most likely cause: Timer_combo4.py isn't running right now.
      // Fail silently from the page's point of view - there's
      // nothing useful the content script can do about it, and we
      // don't want console spam on every stream where the timer
      // app happens to not be running.
      sendResponse({ ok: false, error: String(err) });
    });

  // Returning true keeps the message channel open so the async
  // sendResponse() above is allowed to fire later.
  return true;
});
