window.addEventListener("load", () => {

    // Remove the original stylesheet
    document.querySelectorAll('link[href*="youtube-chat.css"]').forEach(link => {
        link.remove();
    });
    // Load CSS
    // Inject your own stylesheet
    const css = document.createElement("link");
    css.rel = "stylesheet";
    css.href = chrome.runtime.getURL("youtube.css");
    document.head.appendChild(css);

    

});