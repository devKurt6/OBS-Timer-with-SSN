// Relays live data from the YouTube content script (youtube.js) to
// two separate local Flask servers, run as two separate processes:
//   - Timer_combo4.py (Test.py), the timer server:
//       - COMBO_UPDATE: live Gift Combo count updates
//       - SUPERCHAT_COLOR: YouTube's real tier color for a Super
//         Chat that just landed in chat, so the Timer.py overlay's
//         flying Super Chat animation can match it
//   - keyboard_color_server.py, the keyboard-lighting server:
//       - ACTIVE_MESSAGE_COLOR: which message (if any) is currently
//         shown in the Live Chat Overlay, so it can light the
//         Corsair keyboard to match it (violet for gifts, the real
//         tier color for Super Chats, white for anything else, off
//         when nothing is selected)
//
// These used to be the same server/process (both handled by
// Test.py on port 5000). Keyboard lighting has since been split
// into its own standalone server (keyboard_color_server.py) so it
// can be started, stopped, and restarted independently of the
// timer - that's why ACTIVE_MESSAGE_COLOR below points at a
// different port than the other two message types.
//
// This runs in the extension's BACKGROUND service worker, not on the
// youtube.com page itself. That matters: a fetch() made directly from
// a content script inherits the page's origin (https://youtube.com)
// for CORS purposes, and browsers increasingly block a public https
// page from reaching a private/loopback address like 127.0.0.1
// (Private Network Access). A fetch from the background service
// worker is NOT subject to that page-context restriction - it only
// needs the target host listed under "host_permissions" in
// manifest.json, which has been added there for BOTH ports below.

const TIMER_SERVER_BASE = "http://127.0.0.1:5000";
const KEYBOARD_SERVER_BASE = "http://127.0.0.1:5001";

// Maps each message type the content script can send to the
// endpoint that handles it (on whichever of the two servers owns
// that feature), and how to build that endpoint's request body
// from the message.
const MESSAGE_HANDLERS = {
  COMBO_UPDATE: {
    url: TIMER_SERVER_BASE + "/combo/live-update",
    buildBody: (message) => ({
      user: message.user,
      gift: message.gift,
      count: message.count
    })
  },
  SUPERCHAT_COLOR: {
    url: TIMER_SERVER_BASE + "/superchat/color-update",
    buildBody: (message) => ({
      color: message.color,
      amount: message.amount
    })
  },
  ACTIVE_MESSAGE_COLOR: {
    url: KEYBOARD_SERVER_BASE + "/overlay/active-message-color",
    buildBody: (message) => ({
      status: message.status,
      messageType: message.messageType,
      tierColor: message.tierColor
    })
  }
};

chrome.runtime.onMessage.addListener((message, sender, sendResponse) => {
  const handler = message && MESSAGE_HANDLERS[message.type];

  if (!handler) {
    return false; // not for us, let other listeners (if any) handle it
  }

  fetch(handler.url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(handler.buildBody(message))
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
