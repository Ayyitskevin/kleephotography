// Marketing-site interactions (Claude Design editorial home).
// Loaded on every public page via base_site.html; every block guards for
// absent elements so non-home pages run it harmlessly.
(function () {
  "use strict";

  // Respect the OS "reduce motion" setting (also honored in mise.css). When set,
  // ambient autoplay videos stop too (handled below).
  var reduceMotion = window.matchMedia &&
    window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  // --- fixed nav scroll state + scroll progress ---
  var nav = document.querySelector("[data-nav]");
  var progressBar = document.querySelector("[data-progress-bar]");
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

  // Pause archive reels outside the viewport. Reduced-motion visitors retain
  // the native controls established above, and older browsers keep normal
  // autoplay behavior rather than losing playback entirely.
  if (!reduceMotion && "IntersectionObserver" in window) {
    var reelObserver = new IntersectionObserver(function (entries) {
      entries.forEach(function (entry) {
        var reel = entry.target;
        if (entry.isIntersecting) reel.play().catch(function () {});
        else reel.pause();
      });
    }, { threshold: 0.2 });
    document.querySelectorAll("video[data-reel-video]").forEach(function (video) {
      reelObserver.observe(video);
    });
  }

  // Plausible custom events. The external script defines window.plausible;
  // without it every hook is deliberately a no-op. Props are curated from
  // non-PII data attributes only.
  function track(eventName, element) {
    if (!eventName || typeof window.plausible !== "function") return;
    var props = {};
    ["service", "tier", "page"].forEach(function (key) {
      var value = element && element.dataset ? element.dataset["analytics" +
        key.charAt(0).toUpperCase() + key.slice(1)] : "";
      if (value) props[key] = value;
    });
    try {
      window.plausible(eventName, Object.keys(props).length ? { props: props } : undefined);
    } catch (e) { /* analytics must never block navigation or rendering */ }
  }

  document.querySelectorAll("[data-analytics-view]").forEach(function (marker) {
    var eventName = marker.dataset.analyticsView;
    var onceKey = marker.hasAttribute("data-analytics-once") ?
      "mise:analytics:" + eventName + ":" + window.location.pathname : "";
    if (onceKey) {
      try {
        if (window.sessionStorage.getItem(onceKey)) return;
        window.sessionStorage.setItem(onceKey, "1");
      } catch (e) { /* storage may be unavailable; tracking can still proceed */ }
    }
    track(eventName, marker);
  });

  document.addEventListener("click", function (event) {
    var target = event.target.closest && event.target.closest("[data-analytics-event]");
    if (target) track(target.dataset.analyticsEvent, target);
  });

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
  var mobileMenu = document.querySelector("[data-mobile-menu]");
  if (mobileMenu) {
    var menuButton = mobileMenu.querySelector("summary");
    var mobileNav = mobileMenu.querySelector(".nav-mobile");
    if (menuButton && mobileNav) {
      var inertTargets = document.querySelectorAll(
        ".skip-link, [data-nav], #main, .sticky-cta, .site-footer"
      );
      var inertStates = [];
      var lastMenuFocus = null;

      // Preserve any pre-existing inert state instead of assuming ownership.
      var setBackgroundInert = function (inert) {
        if (inert) {
          if (inertStates.length) return;
          inertTargets.forEach(function (element) {
            inertStates.push({
              element: element,
              wasInert: element.hasAttribute("inert")
            });
            element.setAttribute("inert", "");
          });
          return;
        }
        inertStates.forEach(function (state) {
          if (!state.wasInert) state.element.removeAttribute("inert");
        });
        inertStates = [];
      };

      var getMenuFocus = function () {
        var active = document.activeElement;
        return mobileMenu.contains(active) ? active : lastMenuFocus;
      };

      var restoreDesktopFocus = function (active) {
        var focusWasInMenu = active && mobileMenu.contains(active);
        var mobileLink = focusWasInMenu && active.closest &&
          active.closest("a[href]");
        var href = mobileLink && mobileNav.contains(mobileLink) ?
          mobileLink.getAttribute("href") : "";
        var matched = false;
        if (href && nav) {
          matched = Array.prototype.some.call(
            nav.querySelectorAll("nav a[href]"),
            function (link) {
              if (link.getAttribute("href") !== href) return false;
              link.focus();
              return true;
            }
          );
        }
        if (!matched && focusWasInMenu && nav) {
          var brand = nav.querySelector(".site-brand");
          if (brand) brand.focus();
        }
        lastMenuFocus = null;
      };

      var syncMenuState = function (focusOnOpen) {
        var active = getMenuFocus();
        var menuIsHidden =
          window.getComputedStyle(menuButton).display === "none";
        if (mobileMenu.open && !menuIsHidden) {
          document.documentElement.classList.add("nav-menu-open");
          setBackgroundInert(true);
          var firstLink = mobileNav.querySelector("a[href]");
          if (focusOnOpen && firstLink) {
            requestAnimationFrame(function () {
              if (mobileMenu.open) firstLink.focus();
            });
          }
        } else {
          var restoreFocus = mobileMenu.open && menuIsHidden;
          // A persisted disclosure must not lock a desktop-width page.
          if (mobileMenu.open) mobileMenu.open = false;
          document.documentElement.classList.remove("nav-menu-open");
          setBackgroundInert(false);
          if (restoreFocus) restoreDesktopFocus(active);
        }
      };

      var closeMenu = function (restoreFocus) {
        if (!mobileMenu.open) return;
        mobileMenu.open = false;
        syncMenuState(false);
        if (restoreFocus) menuButton.focus();
      };

      mobileMenu.addEventListener("focusin", function (e) {
        lastMenuFocus = e.target;
      });
      mobileMenu.addEventListener("toggle", function () {
        syncMenuState(true);
      });
      mobileNav.addEventListener("click", function (e) {
        var link = e.target.closest && e.target.closest("a[href]");
        if (link) closeMenu(false);
      });
      document.addEventListener("keydown", function (e) {
        if (!mobileMenu.open) return;
        if (e.key === "Escape") {
          e.preventDefault();
          closeMenu(true);
          return;
        }
        if (e.key === "Tab") {
          var links = Array.prototype.slice.call(
            mobileNav.querySelectorAll("a[href]")
          );
          var focusable = [menuButton].concat(links);
          var last = focusable[focusable.length - 1];
          if (!mobileMenu.contains(document.activeElement)) {
            e.preventDefault();
            (e.shiftKey ? last : (links[0] || menuButton)).focus();
          } else if (e.shiftKey && document.activeElement === menuButton) {
            e.preventDefault();
            last.focus();
          } else if (!e.shiftKey && document.activeElement === last) {
            e.preventDefault();
            menuButton.focus();
          }
        }
      });

      // If the responsive breakpoint disappears while open, close and move
      // focus to the equivalent desktop control rather than stranding it.
      window.addEventListener("resize", function () {
        syncMenuState(false);
      });
      window.addEventListener("pageshow", function () {
        syncMenuState(true);
      });
      document.documentElement.classList.add("nav-menu-enhanced");
      syncMenuState(true);
    }
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

  // --- small public form / player helpers ---
  // Keep these public-facing handlers here rather than loading behaviors.js,
  // which also carries admin and client-gallery interactions.
  document.addEventListener("change", function (event) {
    var select = event.target;
    if (select && select.matches && select.matches("select[data-autosubmit]") && select.form) {
      select.form.submit();
    }
    if (select && select.matches && select.matches("select[name='service']") && select.form) {
      var option = select.options[select.selectedIndex];
      var label = select.form.querySelector("[data-scope-label]");
      var input = select.form.querySelector("[data-scope-input]");
      if (option && label) label.textContent = option.dataset.scopeLabel;
      if (option && input) input.placeholder = option.dataset.scopePlaceholder;
    }
  });

  /* ── sr-dialog: styled confirm replacing window.confirm ──────────────────
     Keep in sync with the sr-dialog block in static/behaviors.js — same
     component, separate bundle (marketing pages don't load behaviors.js).
     Optional extras: data-confirm-title, data-confirm-ok, data-confirm-danger. */
  var dlg = null, dlgResolve = null, dlgChain = Promise.resolve();

  function srDialogEl() {
    if (dlg) return dlg;
    dlg = document.createElement("dialog");
    dlg.className = "sr-dialog";
    dlg.innerHTML =
      '<form method="dialog" class="sr-dialog-box">' +
      '<p class="sr-dialog-kicker"></p>' +
      '<p class="sr-dialog-msg"></p>' +
      '<div class="sr-dialog-actions">' +
      '<button value="0" class="sr-btn sr-btn--ghost" formnovalidate>Cancel</button>' +
      '<button value="1" class="sr-btn sr-dialog-ok">Confirm</button>' +
      "</div></form>";
    dlg.addEventListener("close", function () {
      if (dlgResolve) { var r = dlgResolve; dlgResolve = null; r(dlg.returnValue === "1"); }
    });
    return dlg;
  }

  function srConfirm(msg, opts) {
    opts = opts || {};
    var ask = function () {
      return new Promise(function (resolve) {
        var d = srDialogEl();
        if (!d.showModal) { resolve(window.confirm(msg)); return; }
        d.querySelector(".sr-dialog-kicker").textContent = opts.title || "Confirm";
        d.querySelector(".sr-dialog-msg").textContent = msg;
        var ok = d.querySelector(".sr-dialog-ok");
        ok.textContent = opts.ok || "Confirm";
        ok.classList.toggle("sr-btn--danger", !!opts.danger);
        dlgResolve = resolve;
        if (!document.body.contains(d)) document.body.appendChild(d);
        d.showModal();
      });
    };
    var p = dlgChain.then(ask);
    dlgChain = p.catch(function () {});
    return p;
  }

  document.addEventListener(
    "submit",
    function (event) {
      var form = event.target;
      if (!form || !form.matches || !form.matches("form[data-confirm]")) return;
      if (form.__srOk) { form.__srOk = false; return; }
      event.preventDefault();
      var submitter = event.submitter || null;
      srConfirm(form.getAttribute("data-confirm") || "Are you sure?", {
        title: form.getAttribute("data-confirm-title") || undefined,
        ok: form.getAttribute("data-confirm-ok") || undefined,
        danger: form.hasAttribute("data-confirm-danger")
      }).then(function (ok) {
        if (!ok) return;
        form.__srOk = true;
        if (form.requestSubmit) {
          if (submitter && submitter.form === form) form.requestSubmit(submitter);
          else form.requestSubmit();
        } else {
          form.submit();
        }
      });
    },
    true
  );

  document.addEventListener("click", function (event) {
    var chip = event.target.closest && event.target.closest("[data-seek]");
    if (!chip) return;
    var scope = chip.closest("[data-seek-scope]") || document;
    var video = scope.querySelector("video");
    var seconds = parseFloat(chip.getAttribute("data-seek"));
    if (!video || isNaN(seconds)) return;
    video.currentTime = seconds;
    video.play().catch(function () {});
  });

  /* data-sound-toggle / motion-strip reels — tap-for-sound on the reels page.
     Reels autoplay muted; the feature button unmutes the feature video, strip
     reels unmute on tap (one at a time — unmuting one re-mutes the rest).
     Delegated like everything else here: no per-page inline scripts, and the
     button label swaps sprite icons, never emoji. */
  function muteAllReels() {
    document.querySelectorAll(".motion-page video").forEach(function (v) { v.muted = true; });
    var btn = document.querySelector("[data-sound-toggle]");
    if (btn) setSoundBtn(btn, false);
  }
  function setSoundBtn(btn, on) {
    btn.setAttribute("aria-pressed", on ? "true" : "false");
    var off = btn.querySelector(".sound-off-ic");
    var on_ = btn.querySelector(".sound-on-ic");
    if (off) off.hidden = on;
    if (on_) on_.hidden = !on;
    var lbl = btn.querySelector(".sound-lbl");
    if (lbl) lbl.textContent = on ? "Sound on" : "Sound";
  }
  function toggleStripReel(v) {
    var wasMuted = v.muted;
    muteAllReels();
    v.muted = !wasMuted;
    if (wasMuted) v.play().catch(function () {});
  }
  document.addEventListener("click", function (event) {
    var btn = event.target.closest && event.target.closest("[data-sound-toggle]");
    if (btn) {
      var feature = document.querySelector(".motion-feature video");
      if (!feature) return;
      var enable = feature.muted;
      muteAllReels();
      if (enable) {
        feature.muted = false;
        feature.play().catch(function () {});
        setSoundBtn(btn, true);
      }
      return;
    }
    var v = event.target.closest && event.target.closest(".motion-strip video");
    if (v) toggleStripReel(v);
  });
  document.addEventListener("keydown", function (event) {
    if (event.key !== "Enter" && event.key !== " ") return;
    var v = event.target.closest && event.target.closest(".motion-strip video");
    if (!v) return;
    event.preventDefault();
    toggleStripReel(v);
  });
})();
