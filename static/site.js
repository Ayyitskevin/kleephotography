// Marketing-site interactions (Claude Design editorial home).
// Loaded on every public page via base_site.html; every block guards for
// absent elements so non-home pages run it harmlessly.
(function () {
  "use strict";

  // Respect the OS "reduce motion" setting (also honored in mise.css). When set,
  // we skip the JS-driven hero parallax — an inline transform CSS can't override.
  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // --- fixed nav scroll state + scroll progress + parallax ---
  var nav = document.querySelector("[data-nav]");
  var progressBar = document.querySelector("[data-progress-bar]");
  var parallaxEls = document.querySelectorAll("[data-parallax]");
  var ticking = false;
  function onScroll() {
    if (ticking) return;
    ticking = true;
    requestAnimationFrame(function () {
      var y = window.scrollY;
      if (nav) nav.classList.toggle("scrolled", y > 24);
      if (progressBar) {
        var h = document.documentElement.scrollHeight - window.innerHeight;
        progressBar.style.width = (h > 0 ? (y / h) * 100 : 0) + "%";
      }
      if (!reduceMotion) {
        parallaxEls.forEach(function (el) {
          var speed = parseFloat(el.getAttribute("data-parallax")) || 0;
          el.style.transform = "translateY(" + (y * speed) + "px)";
        });
      }
      ticking = false;
    });
  }
  window.addEventListener("scroll", onScroll, { passive: true });
  onScroll();

  // Reduced motion: ambient autoplay videos stop too — show the poster and
  // hand playback to the visitor instead of forcing motion on them.
  if (reduceMotion) {
    document.querySelectorAll("video[autoplay]").forEach(function (v) {
      v.removeAttribute("autoplay");
      v.removeAttribute("loop");
      v.setAttribute("controls", "");
      try { v.pause(); } catch (e) { /* not started yet */ }
    });
  }

  // --- Screening Room player chrome (house reel / premiere heroes) ---
  // One timeupdate listener per [data-sr-player]: live mono timecode
  // (HH:MM:SS:FF at 24fps, matching the marquee) + a progress line. Ambient
  // playback is IO-gated — the reel pauses offscreen. Reduced motion already
  // stripped autoplay above, so these sit on their posters with native
  // controls; the timecode still tracks manual playback.
  document.querySelectorAll("[data-sr-player]").forEach(function (wrap) {
    var video = wrap.querySelector("video");
    if (!video) return;
    var tc = wrap.querySelector("[data-sr-tc]");
    var bar = wrap.querySelector("[data-sr-bar]");
    function pad(n) { return String(n).padStart(2, "0"); }
    if (tc || bar) {
      video.addEventListener("timeupdate", function () {
        var t = video.currentTime || 0;
        if (tc) {
          tc.textContent = pad(Math.floor(t / 3600)) + ":" + pad(Math.floor(t / 60) % 60) +
            ":" + pad(Math.floor(t) % 60) + ":" + pad(Math.floor((t % 1) * 24));
        }
        if (bar && video.duration) {
          bar.style.width = ((video.currentTime / video.duration) * 100) + "%";
        }
      });
    }
    if (!reduceMotion && "IntersectionObserver" in window) {
      new IntersectionObserver(function (entries) {
        entries.forEach(function (e) {
          if (e.isIntersecting) video.play().catch(function () {});
          else video.pause();
        });
      }, { threshold: 0.25 }).observe(video);
    }
  });

  // --- mobile menu ---
  var menuBtn = document.querySelector("[data-menu-btn]");
  var mobileMenu = document.querySelector("[data-mobile-menu]");
  if (menuBtn && mobileMenu) {
    var setMenu = function (open) {
      mobileMenu.classList.toggle("open", open);
      document.body.style.overflow = open ? "hidden" : "";
      menuBtn.setAttribute("aria-expanded", open ? "true" : "false");
    };
    menuBtn.addEventListener("click", function () {
      setMenu(!mobileMenu.classList.contains("open"));
    });
    mobileMenu.addEventListener("click", function () { setMenu(false); });
    document.addEventListener("keydown", function (e) {
      if (e.key === "Escape" && mobileMenu.classList.contains("open")) {
        setMenu(false);
        menuBtn.focus();
      }
    });
  }

  // --- scroll reveal ---
  var reveals = document.querySelectorAll("[data-reveal]");
  if (reveals.length && "IntersectionObserver" in window) {
    var io = new IntersectionObserver(function (entries) {
      entries.forEach(function (e) {
        if (e.isIntersecting) {
          e.target.classList.remove("reveal-hidden");
          io.unobserve(e.target);
        }
      });
    }, { threshold: 0.08, rootMargin: "0px 0px -6% 0px" });
    reveals.forEach(function (el) {
      // already in view on load: leave visible, never hide
      if (el.getBoundingClientRect().top < window.innerHeight * 0.92) return;
      el.classList.add("reveal-hidden");
      io.observe(el);
    });
  }

  // --- delivery: interactive proofing demo ---
  var proof = document.querySelector("[data-proof]");
  if (proof) {
    var thumbs = proof.querySelectorAll("[data-proof-thumb]");
    var countEl = proof.querySelector("[data-proof-count]");
    var fillEl = proof.querySelector("[data-proof-fill]");
    var total = thumbs.length;
    var sync = function () {
      var picked = proof.querySelectorAll(".proof-thumb.picked").length;
      if (countEl) countEl.textContent = picked;
      if (fillEl) fillEl.style.width = (total ? picked / total * 100 : 0) + "%";
    };
    thumbs.forEach(function (t) {
      t.addEventListener("click", function () {
        t.classList.toggle("picked");
        sync();
      });
    });
    sync();
  }

  // --- magnetic buttons (fine-pointer + motion-safe only) ---
  if (!reduceMotion && window.matchMedia &&
      window.matchMedia("(pointer: fine)").matches) {
    document.querySelectorAll("[data-magnetic]").forEach(function (el) {
      var arrow = el.querySelector("[data-arrow]");
      el.addEventListener("mousemove", function (e) {
        var r = el.getBoundingClientRect();
        var dx = e.clientX - (r.left + r.width / 2);
        var dy = e.clientY - (r.top + r.height / 2);
        el.style.transform = "translate(" + (dx * 0.18) + "px," + (dy * 0.22) + "px)";
        if (arrow) arrow.style.transform = "translateX(4px)";
      });
      el.addEventListener("mouseleave", function () {
        el.style.transform = "";
        if (arrow) arrow.style.transform = "";
      });
    });
  }

  // --- delivery: social-crop switcher ---
  var crop = document.querySelector("[data-crop]");
  if (crop) {
    var btns = crop.querySelectorAll("[data-crop-btn]");
    var preview = crop.querySelector("[data-crop-preview]");
    var nameEl = crop.querySelector("[data-crop-name]");
    var useEl = crop.querySelector("[data-crop-use]");
    btns.forEach(function (b) {
      b.addEventListener("click", function () {
        btns.forEach(function (x) { x.classList.remove("on"); });
        b.classList.add("on");
        if (preview) preview.style.aspectRatio = b.dataset.ratio;
        if (nameEl) nameEl.textContent = b.dataset.name;
        if (useEl) useEl.textContent = b.dataset.use;
      });
    });
  }
})();
