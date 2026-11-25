/* Лёгкий плеер: показывает картинку, а видео YouTube загружает только по нажатию. */
(function () {
  document.querySelectorAll(".lesson-player").forEach(function (box) {
    var btn = box.querySelector(".lesson-play");
    if (!btn) return;
    btn.addEventListener("click", function () {
      var id = box.getAttribute("data-vid");
      if (!/^[A-Za-z0-9_-]{6,20}$/.test(id || "")) return;
      var f = document.createElement("iframe");
      f.src = "https://www.youtube-nocookie.com/embed/" + id + "?autoplay=1&rel=0";
      f.title = box.getAttribute("data-title") || "Видео";
      f.allow = "accelerometer; autoplay; clipboard-write; encrypted-media; gyroscope; picture-in-picture; web-share";
      f.allowFullscreen = true;
      f.referrerPolicy = "strict-origin-when-cross-origin";
      box.innerHTML = "";
      box.appendChild(f);
    });
  });
})();
