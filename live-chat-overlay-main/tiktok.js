// Captures newly arriving TikTok LIVE chat rows and sends them through the
// same overlay session used by the YouTube popout extension.
(function () {
  if (window.__liveChatOverlayTikTokInstalled) return;
  window.__liveChatOverlayTikTokInstalled = true;

  var rowFingerprints = new WeakMap();
  var lastFingerprints = new Map();
  var fingerprintOrder = [];
  var lastCaptureStatus = "";

  function detectCaptureStatus() {
    if (window.location.href.indexOf("https://livecenter.tiktok.com/common_live_chat") === 0) {
      return document.querySelector("[data-e2e]") ? "connected" : "waiting";
    }
    var chatSurface = document.querySelector(
      '[data-e2e="chat-room"], [data-e2e="live-chat-container"], ' +
      '[data-e2e="public-screen-live-chat-slot"], [class*="DivChatRoomContent"], ' +
      '.live-shared-ui-chat-list-scrolling-list, [data-e2e="chat-message"]'
    );
    return chatSurface ? "connected" : "waiting";
  }

  function reportCaptureStatus() {
    var status = detectCaptureStatus();
    lastCaptureStatus = status;
    chrome.runtime.sendMessage({ type: "TIKTOK_CAPTURE_STATUS", status: status }, function () {
      if (chrome.runtime.lastError) return;
    });
  }

  reportCaptureStatus();
  setInterval(reportCaptureStatus, 3500);

  function getText(row, selectors) {
    for (var i = 0; i < selectors.length; i++) {
      var element = row.querySelector(selectors[i]);
      if (element && element.textContent && element.textContent.trim()) return element.textContent.trim();
    }
    return "";
  }

  function handleRow(row) {
    if (!row) return;

    var name = getText(row, ["[data-e2e='message-owner-name']", "[data-e2e='message-owner-name'] span", "[class*='DivUserInfo'] [title]"]);
    var body = getText(row, ["[data-e2e='chat-message'] .break-words.align-middle", "[data-e2e='chat-message']", ".live-shared-ui-chat-list-chat-message-comment", "[class*='DivComment']"]);
    if (!name || !body) return;

    var fingerprint = name + "\n" + body;
    if (rowFingerprints.get(row) === fingerprint) return;
    rowFingerprints.set(row, fingerprint);
    if (lastFingerprints.has(fingerprint)) return;
    lastFingerprints.set(fingerprint, Date.now());
    fingerprintOrder.push(fingerprint);
    while (fingerprintOrder.length > 300) lastFingerprints.delete(fingerprintOrder.shift());

    var avatar = row.querySelector("img[src]");
    chrome.runtime.sendMessage({
      type: "TIKTOK_CHAT_TO_POPOUT",
      chat: { chatname: name, chatmessage: body, chatimg: avatar ? avatar.src : "" }
    }, function (response) {
      if (chrome.runtime.lastError) return;
      if (response && !response.ok) console.warn("[TikTok → YouTube chat]", response.error);
    });
  }

  function findRows(node) {
    if (!node || node.nodeType !== 1) return;
    var closestRow = node.closest && node.closest("[data-e2e='chat-message']");
    if (closestRow) handleRow(closestRow);
    if (node.matches("[data-e2e='chat-message']")) handleRow(node);
    node.querySelectorAll("[data-e2e='chat-message']").forEach(handleRow);
  }

  // Ignore chat already on screen when the page loads; only forward new messages.
  document.querySelectorAll("[data-e2e='chat-message']").forEach(function (row) {
    rowFingerprints.set(row, getText(row, ["[data-e2e='message-owner-name']"]) + "\n" +
      getText(row, ["[data-e2e='chat-message'] .break-words.align-middle", "[data-e2e='chat-message']", ".live-shared-ui-chat-list-chat-message-comment", "[class*='DivComment']" ]));
  });

  new MutationObserver(function (mutations) {
    var changed = false;
    mutations.forEach(function (mutation) {
      mutation.addedNodes.forEach(findRows);
      if (mutation.addedNodes.length) changed = true;
      if (mutation.type === "characterData" && mutation.target.parentElement) {
        var row = mutation.target.parentElement.closest("[data-e2e='chat-message']");
        if (row) {
          handleRow(row);
          changed = true;
        }
      }
    });
    if (changed && detectCaptureStatus() !== lastCaptureStatus) reportCaptureStatus();
  }).observe(document.documentElement, { childList: true, subtree: true, characterData: true });
})();
