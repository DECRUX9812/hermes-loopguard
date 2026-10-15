// Ground-truth beacon: posts the DESKTOP-pixel rects of every [data-gt]
// element to the judge so P6's eval can compare model boxes against the real
// DOM geometry.  Page coords are translated to desktop pixels with the
// browser window's screen position + toolbar offsets (devicePixelRatio 1).
(function () {
  function rects() {
    var offX = window.screenX + (window.outerWidth - window.innerWidth) / 2;
    var offY = window.screenY + (window.outerHeight - window.innerHeight);
    var out = {};
    document.querySelectorAll("[data-gt]").forEach(function (el) {
      var r = el.getBoundingClientRect();
      out[el.getAttribute("data-gt")] = [
        Math.round(r.left + window.scrollX + offX),
        Math.round(r.top + window.scrollY + offY),
        Math.round(r.width),
        Math.round(r.height),
      ];
    });
    return out;
  }
  function post() {
    var r = rects();
    if (!Object.keys(r).length) return;
    fetch("/beacon", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        page: document.body.getAttribute("data-page") || document.title,
        url: location.href,
        viewport: [window.innerWidth, window.innerHeight],
        offset: [
          window.screenX + (window.outerWidth - window.innerWidth) / 2,
          window.screenY + (window.outerHeight - window.innerHeight),
        ],
        rects: r,
      }),
    }).catch(function () {});
  }
  window.__gtPost = post;
  if (document.readyState === "complete" || document.readyState === "interactive") {
    setTimeout(post, 400);
  } else {
    document.addEventListener("DOMContentLoaded", function () { setTimeout(post, 400); });
  }
})();
