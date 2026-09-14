var showOnlyFirstName;

var highlightWords = [];
var usePersistentSessionID = false;
var sessionID = "";

// Tracks how many times each user has sent each gift during this stream
var giftCounters = {};

var remoteWindowURL = "https://chat.aaronpk.tv/overlay/";
var remoteServerURL = "https://chat.aaronpk.tv/overlay/pub";
var version = "0.3.9";
var config = {};
var lastID = "";
var videoID = "";
var autoHideTimer = null;
// Words to censor in chat messages
var censoredWords = {
  "nigger": "n*gger",
  "faggot": "f*ggot"
};




document.addEventListener("click", function(e) {
    console.log("CLICK:", e.target);
}, true);

$("body").off("pointerdown").on("pointerdown", "yt-live-chat-text-message-renderer,yt-live-chat-paid-message-renderer,yt-live-chat-membership-item-renderer,ytd-sponsorships-live-chat-gift-purchase-announcement-renderer,yt-live-chat-paid-sticker-renderer, yt-gift-message-view-model", function() {

  $(".active-comment").removeClass("active-comment");

  // "Click" on some innocuous part of the page to hide the moderation popup thingy.
  // YouTube seems to want to pop that up any time you click anywhere on a message.
  // Hide moderation popup
setTimeout(function(){

    $("yt-live-chat-message-input-renderer").click();

}, 200);

  clearTimeout(autoHideTimer);

  // Don't show deleted messages
  if($(this)[0].hasAttribute("is-deleted")) {
    console.log("Not showing deleted message");
    return;
  }

  var data = {};

  $(".hl-c-cont").remove();

  data.chatId = $(this).attr("id");

  if(data.chatId === lastID) {
    hideActiveChat();
    return;
  }

  if ($(this).is("yt-gift-message-view-model")) {

    data.authorname = $(this).find("#author-name-v2").text().trim();

    data.message = "";

    data.giftImage = $(this).find("#gift-image img").attr("src");

    if (data.giftImage && data.giftImage.startsWith("//")) {
        data.giftImage = "https:" + data.giftImage;
    }

    // Create Gift Banner (all styling inline)
data.membershipHTML = `
<style>
    @keyframes giftShine {
        from { left:-120%; }
        to   { left:160%; }
    }
    </style>
<div
    style="
        position:absolute;
        display:block;
        text-align:center;
        left:10px;
        top:108px;
        z-index:3;
        min-width:128px;
        border-radius:10px;
        padding:30px 5px 0;
        overflow:hidden;
        background:linear-gradient(to right,#d879fd,#d299ed,#ff60f2);
        color:#fff;
        transform:rotate(-5deg) translateX(-50%);
    ">
    <div
        style="
            position:absolute;
            top:0;
            left:0;
            width:100%;
            padding:5px 0 0;
            font-size:18px;
            text-align:center;
            background:rgba(0,0,0,.23);
            border-radius:10px 10px 0 0;
            color:#fff;
            z-index:2;
            display:flex;
            align-items:center;
            justify-content:center;
            gap:6px;
        ">
        <div
            style="
                background:#fff;
                border-radius:6px;
                padding:2px 3px;
                display:flex;
                align-items:center;
                justify-content:center;
                box-shadow:0 1px 3px rgba(0,0,0,.35);
                flex-shrink:0;
                margin-top:-3px;
            ">
        <svg width="22" height="16" viewBox="0 0 22 16" style="flex-shrink:0; display:block;">
            <defs>
                <linearGradient id="ytGradA" x1="0%" y1="0%" x2="0%" y2="100%">
                    <stop offset="0%" stop-color="#ff4d4d"/>
                    <stop offset="55%" stop-color="#e50000"/>
                    <stop offset="100%" stop-color="#a80000"/>
                </linearGradient>
                <linearGradient id="ytShineA" x1="0%" y1="0%" x2="0%" y2="100%">
                    <stop offset="0%" stop-color="#fff" stop-opacity="0.55"/>
                    <stop offset="45%" stop-color="#fff" stop-opacity="0"/>
                </linearGradient>
                <filter id="ytDropA" x="-30%" y="-30%" width="160%" height="160%">
                    <feDropShadow dx="0" dy="1" stdDeviation="0.8" flood-color="#000" flood-opacity="0.45"/>
                </filter>
            </defs>
            <rect width="22" height="16" rx="4" fill="url(#ytGradA)" filter="url(#ytDropA)"/>
            <rect x="0.6" y="0.6" width="20.8" height="14.8" rx="3.4" fill="none" stroke="rgba(255,255,255,0.35)" stroke-width="0.8"/>
            <rect width="22" height="8" rx="4" fill="url(#ytShineA)"/>
            <polygon points="9,5 9,11 14.5,8" fill="#8c0000" opacity="0.4" transform="translate(0.4,0.6)"/>
            <polygon points="9,5 9,11 14.5,8" fill="#fff"/>
            <polygon points="9,5 9,7.6 11.8,6.3" fill="#ffffff" opacity="0.55"/>
        </svg>
        </div>
        <span>GIFT</span>
    </div>
    <!-- Shine -->
   <!-- Shine -->
    <div
        style="
            position:absolute;
            top:-50%;
            left:-120%;
            width:8px;
            height:220%;
            background:#fff;
            box-shadow:0 0 30px 15px rgba(255,255,255,.9);
            opacity:.9;
            transform:rotate(12deg);
            animation:giftShine 2.5s linear infinite;
            pointer-events:none;
            z-index:6;
        ">
    </div>
</div>
`;
} else {

    data.authorname = $(this).find("#author-name").text();

    data.message = $(this).find("#message").html();
// Censor selected words
if (data.message) {
  Object.keys(censoredWords).forEach(function(word) {
    var replacement = censoredWords[word];

    var regex = new RegExp("\\b" + word + "\\b", "gi");

    data.message = data.message.replace(regex, replacement);
  });
}

// Detect YouTube "Stars"/gift messages that show up as normal chat rows
// (e.g. "Sent a gift ⭐ the game name is Steal A Egg") instead of the
// classic yt-gift-message-view-model element, so we can still show the
// purple gift banner for them without needing to know the exact tag
// YouTube is using this week.
var fullRowText = $(this).text();
if (/sent\s+(a\s+)?gift|sent\s+star/i.test(fullRowText)) {
  data.isStarGift = true;

  // Best-effort: grab any icon image embedded in the row that isn't the
  // author avatar (id="img"), in case it's a small gift/star icon.
  var starIcon = $(this).find("img").not("#img").first().attr("src");
  if (starIcon) {
    if (starIcon.startsWith("//")) {
      starIcon = "https:" + starIcon;
    }
    data.giftImage = starIcon;
  }
}


}
  //data.authorname = $(this).find("#author-name").text();
  if(showOnlyFirstName) {
    data.authorname = data.authorname.replace(/ [^ ]+$/, '');
  }
  data.authorimg = $(this).find("#img").attr("src");
  // Replace the 32px and 64px avatar with a 128px avatar but keep the identifier identical before the first '='
  const equalIndex = data.authorimg.indexOf("=");
  if (equalIndex !== -1) {
    let part1 = data.authorimg.slice(0, equalIndex);
    let part2 = data.authorimg.slice(equalIndex);
    part2 = part2.replace("s32", "s128").replace("s64", "s128");

    data.authorimg = part1 + part2;
  }

  //data.message = $(this).find("#message").html();

  data.sticker = $(this).find(".yt-live-chat-paid-sticker-renderer #sticker #img").attr("src");


  // Donation amounts for stickers use a different id than regular superchats
  if(data.sticker) {
    data.donation = $(this).find("#purchase-amount-chip").html();
  } else {
    data.donation = $(this).find("#purchase-amount .yt-live-chat-paid-message-renderer").html();
  }

  data.badges = "";
  if($(this).find("#chat-badges .yt-live-chat-author-badge-renderer img").length > 0) {
    data.badges = $(this).find("#chat-badges .yt-live-chat-author-badge-renderer img").parent().html();
  }

  // Mark this comment as shown
  $(this).addClass("shown-comment").addClass("active-comment");
  console.log("Clicked:", this.tagName);
console.log("Has active-comment:", $(this).hasClass("active-comment"));
console.log(this);
  

  data.donationHTML = '';

if (data.sticker) {

    data.donationHTML = `
    <style>
    @keyframes giftShine {
        from { left:-120%; }
        to   { left:160%; }
    }
    </style>
    <div
        style="
            position:absolute;
            display:block;
            text-align:center;
            left:10px;
            top:108px;
            z-index:3;
            min-width:128px;
            border-radius:10px;
            padding:30px 5px 0;
            overflow:hidden;
            background:linear-gradient(to right,#d879fd,#d299ed,#ff60f2);
            color:#fff;
            transform:rotate(-5deg) translateX(-50%);
        ">
        <div
            style="
                position:absolute;
                top:0;
                left:0;
                width:100%;
                padding:5px 0 0;
                font-size:18px;
                text-align:center;
                background:rgba(0,0,0,.23);
                border-radius:10px 10px 0 0;
                color:#fff;
                z-index:2;
                display:flex;
                align-items:center;
                justify-content:center;
                gap:6px;
            ">
            <div
                style="
                    background:#fff;
                    border-radius:6px;
                    padding:2px 3px;
                    display:flex;
                    align-items:center;
                    justify-content:center;
                    box-shadow:0 1px 3px rgba(0,0,0,.35);
                    flex-shrink:0;
                    margin-top:-3px;
                ">
            <svg width="22" height="16" viewBox="0 0 22 16" style="flex-shrink:0; display:block; ">
                <defs>
                    <linearGradient id="ytGradB" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%" stop-color="#ff4d4d"/>
                        <stop offset="55%" stop-color="#e50000"/>
                        <stop offset="100%" stop-color="#a80000"/>
                    </linearGradient>
                    <linearGradient id="ytShineB" x1="0%" y1="0%" x2="0%" y2="100%">
                        <stop offset="0%" stop-color="#fff" stop-opacity="0.55"/>
                        <stop offset="45%" stop-color="#fff" stop-opacity="0"/>
                    </linearGradient>
                    <filter id="ytDropB" x="-30%" y="-30%" width="160%" height="160%">
                        <feDropShadow dx="0" dy="1" stdDeviation="0.8" flood-color="#000" flood-opacity="0.45"/>
                    </filter>
                </defs>
                <rect width="22" height="16" rx="4" fill="url(#ytGradB)" filter="url(#ytDropB)"/>
                <rect x="0.6" y="0.6" width="20.8" height="14.8" rx="3.4" fill="none" stroke="rgba(255,255,255,0.35)" stroke-width="0.8"/>
                <rect width="22" height="8" rx="4" fill="url(#ytShineB)"/>
                <polygon points="9,5 9,11 14.5,8" fill="#8c0000" opacity="0.4" transform="translate(0.4,0.6)"/>
                <polygon points="9,5 9,11 14.5,8" fill="#fff"/>
                <polygon points="9,5 9,7.6 11.8,6.3" fill="#ffffff" opacity="0.55"/>
            </svg>
            </div>
            <span>GIFT</span>
        </div>
        <!-- Shine -->
        <div
            style="
                position:absolute;
                top:-40%;
                left:-120%;
                width:70%;
                height:220%;
                background:linear-gradient(
                    90deg,
                    rgba(255,255,255,0) 0%,
                    rgba(255,255,255,.25) 35%,
                    rgba(255,255,255,.95) 50%,
                    rgba(255,255,255,.25) 65%,
                    rgba(255,255,255,0) 100%
                );
                transform:rotate(20deg);
                animation:giftShine 2.2s linear infinite;
                pointer-events:none;
                z-index:6;
            ">
        </div>
    </div>`;
} else if (data.donation) {

    // Normal Super Chat
    data.donationHTML = '<div class="donation">' + data.donation + '</div>';

}

  data.membership = $(this).find(".yt-live-chat-membership-item-renderer #header-subtext").html(); // membership level e.g. "SILVER"
  data.giftedMembership = $(this).find(".ytd-sponsorships-live-chat-header-renderer #primary-text").html(); // Bob gifted 20 memberships

  if (!data.membershipHTML) {
    data.membershipHTML = "";
}

  // Try to find the membership level name
  data.membershipLevel = '';
  if(data.membership) {
    var membershipLevelName;
    if(m=data.membership.match(/(Welcome|Upgraded membership) to (.+)!/)) {
      membershipLevelName = m[2];
    } else {
      membershipLevelName = data.membership;
    }
    switch(membershipLevelName) {
      case 'SILVER':
        data.membershipLevel = 'silver'; break;
      case 'GOLD':
        data.membershipLevel = 'gold'; break;
      case 'PLATINUM':
        data.membershipLevel = 'platinum'; break;
      case 'DIAMOND':
        data.membershipLevel = 'diamond'; break;
      case 'EMERALD':
        data.membershipLevel = 'emerald'; break;
    }
  }


  if (data.giftedMembership) {

    if (!data.membershipHTML) {

        data.membershipHTML = `
        <style>
    @keyframes giftShine {
        from { left:-120%; }
        to   { left:160%; }
    }
    </style>
        <div
            style="
                position:absolute;
                display:block;
                text-align:center;
                left:10px;
                top:108px;
                z-index:3;
                min-width:128px;
                border-radius:10px;
                padding:30px 5px 0;
                overflow:hidden;
                background:linear-gradient(to right,#d879fd,#d299ed,#ff60f2);
                color:#fff;
                transform:rotate(-5deg) translateX(-50%);
            ">

            <div
                style="
                    position:absolute;
                    top:0;
                    left:0;
                    width:100%;
                    padding:5px 0 0;
                    font-size:18px;
                    text-align:center;
                    background:rgba(0,0,0,.23);
                    border-radius:10px 10px 0 0;
                    color:#fff;
                    z-index:2;
                    display:flex;
                    align-items:center;
                    justify-content:center;
                    gap:6px;
                ">
                <div
                    style="
                        background:#fff;
                        border-radius:6px;
                        padding:2px 3px;
                        display:flex;
                        align-items:center;
                        justify-content:center;
                        box-shadow:0 1px 3px rgba(0,0,0,.35);
                        flex-shrink:0;
                        margin-top:-3px;
                    ">
                <svg width="22" height="16" viewBox="0 0 22 16" style="flex-shrink:0; display:block; ">
                    <defs>
                        <linearGradient id="ytGradC" x1="0%" y1="0%" x2="0%" y2="100%">
                            <stop offset="0%" stop-color="#ff4d4d"/>
                            <stop offset="55%" stop-color="#e50000"/>
                            <stop offset="100%" stop-color="#a80000"/>
                        </linearGradient>
                        <linearGradient id="ytShineC" x1="0%" y1="0%" x2="0%" y2="100%">
                            <stop offset="0%" stop-color="#fff" stop-opacity="0.55"/>
                            <stop offset="45%" stop-color="#fff" stop-opacity="0"/>
                        </linearGradient>
                        <filter id="ytDropC" x="-30%" y="-30%" width="160%" height="160%">
                            <feDropShadow dx="0" dy="1" stdDeviation="0.8" flood-color="#000" flood-opacity="0.45"/>
                        </filter>
                    </defs>
                    <rect width="22" height="16" rx="4" fill="url(#ytGradC)" filter="url(#ytDropC)"/>
                    <rect x="0.6" y="0.6" width="20.8" height="14.8" rx="3.4" fill="none" stroke="rgba(255,255,255,0.35)" stroke-width="0.8"/>
                    <rect width="22" height="8" rx="4" fill="url(#ytShineC)"/>
                    <polygon points="9,5 9,11 14.5,8" fill="#8c0000" opacity="0.4" transform="translate(0.4,0.6)"/>
                    <polygon points="9,5 9,11 14.5,8" fill="#fff"/>
                    <polygon points="9,5 9,7.6 11.8,6.3" fill="#ffffff" opacity="0.55"/>
                </svg>
                </div>
                <span>GIFT</span>
            </div>

            <!-- Shine -->
            <div
                style="
                    position:absolute;
                    top:-50%;
                    left:0px;
                    width:8px;
                    height:220%;
                    background:#fff;
                    box-shadow:0 0 30px 15px rgba(255,255,255,.9);
                    opacity:.9;
                    transform:rotate(12deg) translateX(-260px);
                    animation:giftShine 2.5s linear infinite;
                    pointer-events:none;
                    z-index:6;
                ">
            </div>

        </div>
        `;
    }

    data.message = data.giftedMembership;
} else if(data.membership) {
    if(data.message) {
      data.membershipLength = $(this).find(".yt-live-chat-membership-item-renderer #header-primary-text").text(); // "Member for 20 months"
      if(data.membershipLength) {
        if(m = data.membershipLength.match(/Member for (.+)/)) {
          data.membership = data.membership + '<br><span class="membership-length">'+m[1]+'</span>';
        }
      }
      // Member chat, show their member tier under their photo. Message will have been extracted already.
      data.membershipHTML = '<div class="donation membership '+data.membershipLevel+'">'+data.membership+'</div>';
    } else {
      // New member or upgrade, show the tier in the main message section
      if(data.membership.match(/Upgraded membership/)) {
        data.membershipHTML = '<div class="donation membership '+data.membershipLevel+'">UPGRADE</div>';
      } else {
        data.membershipHTML = '<div class="donation membership '+data.membershipLevel+'">NEW<br>MEMBER!</div>';
      }
      data.message = data.membership;
    }
  }


  if(data.sticker) {
    data.message = '<img class="sticker" src="'+data.sticker+'">';
  }

  // If this looked like a YouTube "Stars"/gift message (detected by text,
  // not by tag name), render the same purple gift banner used for classic
  // gift messages.
  if (data.isStarGift && !data.membershipHTML) {
    data.membershipHTML = `
<style>
    @keyframes giftShine {
        from { left:-120%; }
        to   { left:160%; }
    }
</style>
<div
    style="
        position:absolute;
        display:block;
        text-align:center;
        left:10px;
        top:108px;
        z-index:3;
        min-width:128px;
        border-radius:10px;
        padding:30px 5px 0;
        overflow:hidden;
        background:linear-gradient(to right,#d879fd,#d299ed,#ff60f2);
        color:#fff;
        transform:rotate(-5deg) translateX(-50%);
    ">
    <div
        style="
            position:absolute;
            top:0;
            left:0;
            width:100%;
            padding:5px 0 0;
            font-size:18px;
            text-align:center;
            background:rgba(0,0,0,.23);
            border-radius:10px 10px 0 0;
            color:#fff;
            z-index:2;
            display:flex;
            align-items:center;
            justify-content:center;
            gap:6px;
        ">
        <div
            style="
                background:#fff;
                border-radius:6px;
                padding:2px 3px;
                display:flex;
                align-items:center;
                justify-content:center;
                box-shadow:0 1px 3px rgba(0,0,0,.35);
                flex-shrink:0;
                margin-top:-3px;
            ">
        <svg width="22" height="16" viewBox="0 0 22 16" style="flex-shrink:0; display:block;">
            <defs>
                <linearGradient id="ytGradD" x1="0%" y1="0%" x2="0%" y2="100%">
                    <stop offset="0%" stop-color="#ff4d4d"/>
                    <stop offset="55%" stop-color="#e50000"/>
                    <stop offset="100%" stop-color="#a80000"/>
                </linearGradient>
                <linearGradient id="ytShineD" x1="0%" y1="0%" x2="0%" y2="100%">
                    <stop offset="0%" stop-color="#fff" stop-opacity="0.55"/>
                    <stop offset="45%" stop-color="#fff" stop-opacity="0"/>
                </linearGradient>
                <filter id="ytDropD" x="-30%" y="-30%" width="160%" height="160%">
                    <feDropShadow dx="0" dy="1" stdDeviation="0.8" flood-color="#000" flood-opacity="0.45"/>
                </filter>
            </defs>
            <rect width="22" height="16" rx="4" fill="url(#ytGradD)" filter="url(#ytDropD)"/>
            <rect x="0.6" y="0.6" width="20.8" height="14.8" rx="3.4" fill="none" stroke="rgba(255,255,255,0.35)" stroke-width="0.8"/>
            <rect width="22" height="8" rx="4" fill="url(#ytShineD)"/>
            <polygon points="9,5 9,11 14.5,8" fill="#8c0000" opacity="0.4" transform="translate(0.4,0.6)"/>
            <polygon points="9,5 9,11 14.5,8" fill="#fff"/>
            <polygon points="9,5 9,7.6 11.8,6.3" fill="#ffffff" opacity="0.55"/>
        </svg>
        </div>
        <span>GIFT</span>
    </div>
    <div
        style="
            position:absolute;
            top:-50%;
            left:-120%;
            width:8px;
            height:220%;
            background:#fff;
            box-shadow:0 0 30px 15px rgba(255,255,255,.9);
            opacity:.9;
            transform:rotate(12deg);
            animation:giftShine 2.5s linear infinite;
            pointer-events:none;
            z-index:6;
        ">
    </div>
</div>
`;
  }

  // Add gift image to gift messages
let giftHTML = "";

if (data.giftImage) {
    giftHTML = `
        <div class="gift-icon-container">
            <img class="gift-icon" src="${data.giftImage}"  style="width: 120px; height: 120px; margin-top: 10px; margin-left: 70px; z-index:9999;">
        </div>
    `;
}

if (data.giftImage) {
    giftHTML = `
        <div
            style="
                background:${overlayStyle.commentBg};
                color:${overlayStyle.commentColor};
                border-radius:${overlayStyle.commentRadius};
                padding:0;
                width:fit-content;
            "
        >
            <div class="gift-icon-container">
                <img class="gift-icon"
                     src="${data.giftImage}"
                     style="width:120px;height:120px;margin-top:10px;margin-left:70px;">
            </div>
        </div>
    `;
}





//   data.backgroundColor = "background-color:" + overlayStyle.commentBg + ";";
// data.textColor = "color:" + overlayStyle.commentColor + ";";
data.backgroundColor = "";
  data.textColor = "";
  if(this.style.getPropertyValue('--yt-live-chat-paid-message-primary-color')) {
    data.backgroundColor = "background-color: "+this.style.getPropertyValue('--yt-live-chat-paid-message-primary-color')+";";
    data.textColor = "color: #111;";
  }

  // This doesn't work yet
  // if(this.style.getPropertyValue('--yt-live-chat-sponsor-color')) {
  //   data.backgroundColor = "background-color: "+this.style.getPropertyValue('--yt-live-chat-sponsor-color')+";";
  //   data.textColor = "color: #111;";
  // }

  // console.log(data);

var html =
    '<div class="hl-c-cont fadeout" style="'
+'position:relative;'
+'padding:20px;'
+'width:100%;'
+'font-family:'+overlayStyle.fontFamily+';'
+'">'
    + '<div style="'
+'position:absolute;'
+'top:-20px;'
+'left:50px;'
+'background:'+overlayStyle.authorBg+';'
+'color:'+overlayStyle.authorColor+';'
+'padding:10px;'
+'border-radius:'+overlayStyle.authorRadius+';'
+'font-size:'+overlayStyle.authorFontSize+';'
+'font-weight:700;'
+'font-family:'+overlayStyle.fontFamily+';'
+'z-index:1;'
+'">' + data.authorname
    + '<div class="hl-badges">' + data.badges + '</div>'
    + '</div>'

    + (data.message ?
        '<div class="hl-message" style="' + data.backgroundColor + ' ' + data.textColor + '">'
    + data.message +
    '</div>'
    : '')

    + giftHTML

    + '<div style="'
    + 'position:absolute;'
    + 'top:0;'
    + 'left:-60px;'
    + 'width:128px;'
    + 'height:128px;'
    + 'background:' + overlayStyle.avatarBorder + ';'
    + 'border-radius:50%;'
    + 'padding:3px;'
    + 'overflow:hidden;'
    + 'z-index:2;'
    + '">'
    + '<img src="' + data.authorimg + '" style="width:100%;height:100%;border-radius:50%;">'
    + '</div>'

    + data.donationHTML
    + data.membershipHTML
    + '</div>';

  lastID = data.chatId;

  if(sessionID) {

    var remote = {
      version: version,
      command: "show",
      html: html,
      config: config,
      v: videoID
    }
    $.post(remoteServerURL+"?v="+videoID+"&id="+sessionID, JSON.stringify(remote));

  } else {

    $( "highlight-chat" ).removeClass("preview").append(html)
    .delay(10).queue(function(next){
      $( ".hl-c-cont" ).removeClass("fadeout");
      next();
    });

  }
  if(config.autoHideSeconds && config.autoHideSeconds > 0) {
    autoHideTimer = setTimeout(function(){
      hideActiveChat();
    }, config.autoHideSeconds*1000);
  }

});

function hideActiveChat() {
  if(sessionID) {
    var remote = {
      version: version,
      command: "hide",
      config: config,
      v: videoID
    };
    $.post(remoteServerURL+"?v="+videoID+"&id="+sessionID, JSON.stringify(remote));
  }

  $(".hl-c-cont").addClass("fadeout").delay(300).queue(function(){
    $(".hl-c-cont").remove().dequeue();
  });

  lastID = false;
}

$("body").on("click", ".btn-clear", function() {
  hideActiveChat();
});

$("yt-live-chat-app").before( '<highlight-chat></highlight-chat><button class="btn-clear">CLEAR</button>' );
$("body").addClass("inline-chat");

// Restore settings
var configProperties = ["color","scale","sizeOffset",
  "commentBottom","commentHeight","authorBackgroundColor",
  "authorAvatarBorderColor","authorColor","commentBackgroundColor","commentColor",
  "fontFamily","showOnlyFirstName","highlightWords",
  "popoutURL","serverURL","autoHideSeconds",
  "authorAvatarOverlayOpacity","persistentSessionID","sessionID"
];
chrome.storage.sync.get(configProperties, function(item){
  var color = "#000";
  if(item.color) {
    color = item.color;
  }

  let root = document.documentElement;
  root.style.setProperty("--keyer-bg-color", color);

  if(item.authorBackgroundColor) {
    root.style.setProperty("--author-bg-color", item.authorBackgroundColor);
    root.style.setProperty("--author-avatar-border-color", item.authorBackgroundColor);
  }
  if(item.authorAvatarBorderColor) {
    root.style.setProperty("--author-avatar-border-color", item.authorAvatarBorderColor);
  }
  if(item.authorAvatarOverlayOpacity) {
    root.style.setProperty("--author-avatar-overlay-opacity", item.authorAvatarOverlayOpacity);
  }
  if(item.commentBackgroundColor) {
    root.style.setProperty("--comment-bg-color", item.commentBackgroundColor);
  }
  if(item.authorColor) {
    root.style.setProperty("--author-color", item.authorColor);
  }
  if(item.commentColor) {
    root.style.setProperty("--comment-color", item.commentColor);
  }
  if(item.fontFamily) {
    root.style.setProperty("--font-family", item.fontFamily);
  }
  if(item.scale) {
    root.style.setProperty("--comment-scale", item.scale);
  }
  if(item.commentBottom) {
    root.style.setProperty("--comment-area-bottom", item.commentBottom);
  }
  if(item.commentHeight) {
    root.style.setProperty("--comment-area-height", item.commentHeight);
  }
  if(item.sizeOffset) {
    root.style.setProperty("--comment-area-size-offset", item.sizeOffset);
  }
  showOnlyFirstName = item.showOnlyFirstName;
  highlightWords = item.highlightWords;

  if(item.popoutURL) {
    remoteWindowURL = item.popoutURL;
  }
  if(item.serverURL) {
    remoteServerURL = item.serverURL;
  }

  if(item.persistentSessionID && item.sessionID) {
    usePersistentSessionID = item.sessionID;
  }

  window.overlayStyle = {

    keyerBg: item.color || "#000",

    commentBg: item.commentBackgroundColor || "#fff",
    commentColor: item.commentColor || "#000",
    commentRadius: "0px",
    commentFontSize: "40px",

    authorBg: item.authorBackgroundColor || "#fff",
    authorColor: item.authorColor || "#000",
    authorRadius: "0px",
    authorFontSize: "30px",

    avatarBorder: item.authorAvatarBorderColor || "#fff",
    avatarBorderSize: "3px",
    avatarSize: "128px",
    avatarOverlayOpacity: item.authorAvatarOverlayOpacity || 0.1,

    fontFamily: item.fontFamily || "Arial",

    scale: item.scale || 1,
    commentBottom: item.commentBottom || "0",
    commentHeight: item.commentHeight || "0vh",
    sizeOffset: item.sizeOffset || 0 ,
    

};



const highlight = document.querySelector("highlight-chat");

if (highlight) {
    Object.assign(highlight.style, {
        position: "absolute",
        bottom: window.overlayStyle.commentBottom,
        left: "0",
        right: "0",
        height: window.overlayStyle.commentHeight,
        overflow: "hidden",
        padding: "40px 50px 40px 100px",
        background: window.overlayStyle.keyerBg,
        transform: "scale(" + window.overlayStyle.scale + ")",
        fontFamily: window.overlayStyle.fontFamily,
        zIndex: "99999999999"
    });
}

  config = item;

  

// If we're in popout mode, push config immediately so things like
// commentHeight apply before the very first message shows up.
if (window.location.hash) {
  sessionID = window.location.hash.replace("#", "");
  videoID = new URLSearchParams(window.location.search).get('v');
  var remote = {
    version: version,
    command: "config",
    config: config,
    v: videoID
  };
  $.post(remoteServerURL + "?v=" + videoID + "&id=" + sessionID, JSON.stringify(remote));
}
});


// $("#primary-content").append('<span id="aspect-ratio-container" style="font-size: 0.7em">Aspect Ratio: <span id="aspect-ratio"></span></span>');
$("#primary-content").append('<span id="get-overlay-url-container"><a href="#" id="pop-out-button" class="button">Get Overlay URL</a></span>');
$("#primary-content").append('<span class="hidden"><input type="url" readonly id="pop-out-url"></span>');

function displayAspectRatio() {
  var ratio = Math.round(window.innerWidth / window.innerHeight * 100) / 100;
  ratio += " (target 1.77)";
  $("#aspect-ratio").text(ratio);
}
// displayAspectRatio();
// window.onresize = displayAspectRatio;

$("#pop-out-button").click(function(e){
  e.preventDefault();

  if(usePersistentSessionID) {
    sessionID = usePersistentSessionID;
  }

  if(!sessionID) {
    if(window.location.hash) {
      sessionID = window.location.hash.replace("#", "");
    } else {
      sessionID = generateSessionID();
    }
  }

  window.location.hash = sessionID;

  $("#pop-out-url").val(remoteWindowURL+"#"+sessionID);
  $("#pop-out-url").parent().removeClass("hidden");

  $("highlight-chat").remove();
  $("body").removeClass("inline-chat");
  $("#aspect-ratio-container").addClass("hidden");
  $("#get-overlay-url-container").addClass("hidden");
});

$("#pop-out-url").click(function(){
  $(this).select();
});

$(document).keyup(function(e){

    // Escape key hides active chat
    if(e.keyCode === 27) {
      hideActiveChat();
    }

});

$(function(){

  // Show a placeholder message so you can position the window before the chat is live
  var data = {};
  data.message = "this livestream is the best!";
  data.authorimg = remoteWindowURL+"/youtube-live-chat-sample-avatar.png";
  $( "highlight-chat" ).addClass("preview").append('<div class="hl-c-cont fadeout"><div class="hl-name">Sample User<div class="hl-badges"></div></div><div class="hl-<div class="hl-img"><img src="' + data.authorimg + '"></div>message">' + data.message + '</div></div>')
  .delay(10).queue(function(next){
    $( ".hl-c-cont" ).removeClass("fadeout");
    next();
  });

  // Restore the popout URL field if they refresh the page
  if(window.location.hash) {
    $("#pop-out-button").click();
  }

  // Show banner
  const timezone = Intl.DateTimeFormat().resolvedOptions().timeZone;

  const params = new URLSearchParams(window.location.search);
  videoID = params.get('v');

  $.post("https://chat.aaronpk.tv/featured.php", {
    lang: window.navigator.language,
    tz: timezone,
    version: version,
    v: videoID
  }, function(response){
    if(response && response.img) {
      var link = 'https://chat.aaronpk.tv/redirect.php?tag='+response.tag+'&lang='+window.navigator.language+'&tz='+timezone+"&version="+version;
      $("body").append('<div id="featured"><a href="'+link+'" target="_blank"><img src="'+response.img+'" height="32" width="160"></a></span>');
    }
  });

  removeReactionButtons();

});

function generateSessionID(){
  var text = "";
  var chars = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghjkmnpqrstuvwxyz23456789";
  for (var i = 0; i < 10; i++){
    text += chars.charAt(Math.floor(Math.random() * chars.length));
  }
  return text;
};

function onElementInserted(containerSelector, callback) {

    var watchedTagNames = [
      "yt-live-chat-text-message-renderer".toUpperCase(),
      "yt-live-chat-paid-message-renderer".toUpperCase(),
      "yt-live-chat-membership-item-renderer".toUpperCase(),
      "yt-live-chat-paid-sticker-renderer".toUpperCase(),
      "ytd-sponsorships-live-chat-gift-purchase-announcement-renderer".toUpperCase()
    ];

    var onMutationsObserved = function(mutations) {
        mutations.forEach(function(mutation) {
            // console.log("A mutation happened");
            if (mutation.addedNodes.length) {
                for (var i = 0, len = mutation.addedNodes.length; i < len; i++) {
                    if(watchedTagNames.includes(mutation.addedNodes[i].tagName)) {
                        callback(mutation.addedNodes[i]);
                    }
                }
            }
        });
    };

    var target = document.querySelectorAll(containerSelector)[0];
    var config = { childList: true, subtree: true };
    var MutationObserver = window.MutationObserver || window.WebKitMutationObserver;
    var observer = new MutationObserver(onMutationsObserved);
    observer.observe(target, config);

}


onElementInserted(".yt-live-chat-item-list-renderer#items", function(element){
  // console.log("New dom element inserted", element.tagName);
  // Check for highlight words
  var chattext = $(element).find("#message").text();
  var chatWords = chattext.split(" ");
  var highlights = chatWords.filter(value => highlightWords.includes(value.toLowerCase().replace(/[^a-z0-9]/gi, '')));
  $(element).removeClass("shown-comment");
  if(highlights.length > 0) {
    $(element).addClass("highlighted-comment");
  }
});

document.addEventListener("wheel", function(e){

    const scroller = document.querySelector("#item-scroller");

    if (!scroller) return;

    scroller.scrollTop += e.deltaY;

}, {
    passive: true,
    capture: true
});

$("body").on("click", "yt-gift-message-view-model", function () {

    const gift = this;

    console.log("Clicked gift");

    const observer = new MutationObserver((mutations) => {
        mutations.forEach(m => {
            console.log(
                "Mutation:",
                m.type,
                m.attributeName,
                gift.className
            );
        });
    });

    observer.observe(gift, {
        attributes: true,
        childList: true,
        subtree: false
    });

    setTimeout(() => observer.disconnect(), 2000);

});


/*
function removeModerationMenu(element) {
  $(element).find("tp-yt-iron-dropdown, #menu").remove();
  // Remove the "top chat/live chat" option since removing the iron-dropdown also removes the dropdown from that.
  // This way people won't be confused about why pressing it isn't working anymore.
  $(element).find("yt-sort-filter-sub-menu-renderer").remove();
}

function removeReactionButtons() {
  // $("yt-reaction-control-panel-overlay-view-model").remove();
}
*/
