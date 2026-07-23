/* Delegated handlers for what used to be inline on*-attributes — the CSP ships
   without script-src 'unsafe-inline', so templates opt in via data attributes:

     form[data-confirm]          styled <dialog> confirm at submit time; {name}
                                 in the message interpolates the named form
                                 control's current value (e.g. "Sign as
                                 {signer_name}?")
     button/a[data-confirm]      same at click time, for buttons that share a
                                 form with non-destructive siblings
     [data-print]                window.print()
     select[data-autosubmit]     submit the owning form on change
     [data-goto]                 navigate to the given URL on click, unless
                                 window.__stuDragged is set (the studio board
                                 raises it during a drag so drop != navigate)
     [data-seek]                 chapter chips: jump the nearest scoped <video>
                                 (closest [data-seek-scope], else the page's
                                 first video) to the given second and play

   Capture-phase listeners so a cancelled confirm also stops htmx/other
   delegated listeners from acting on the same event; on confirm the original
   submit/click is re-issued with a one-shot flag that passes this handler, so
   htmx & co. see an ordinary event. Optional dialog extras: data-confirm-title
   (kicker), data-confirm-ok (button label), data-confirm-danger (red confirm). */
(function () {
  "use strict";

  function message(el, form) {
    var msg = el.getAttribute("data-confirm") || "";
    return msg.replace(/\{([A-Za-z0-9_-]+)\}/g, function (whole, name) {
      var field = form && form.elements ? form.elements[name] : null;
      return field && "value" in field ? field.value : whole;
    });
  }

  /* ── sr-dialog: one lazy-built <dialog> answers every data-confirm ────────
     Native <dialog>: Esc/backdrop cancel, focus returns to the trigger, and
     the first focusable element (Cancel) takes initial focus — the safe
     default. Where showModal is missing we fall back to native confirm(). */
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
    /* serialize: two confirms never stack — each opens after the last closes */
    var p = dlgChain.then(ask);
    dlgChain = p.catch(function () {});
    return p;
  }

  function confirmOpts(el) {
    return {
      title: el.getAttribute("data-confirm-title") || undefined,
      ok: el.getAttribute("data-confirm-ok") || undefined,
      danger: el.hasAttribute("data-confirm-danger")
    };
  }

  /* ── toasts: window.miseToast(message, kind) — kind: ok / warn / danger ──
     Lands in the .sr-toasts live region from base.html (created on demand if
     a page lacks it); 4s dwell, max three stacked. */
  window.miseToast = function (msg, kind) {
    var host = document.querySelector(".sr-toasts");
    if (!host) {
      host = document.createElement("div");
      host.className = "sr-toasts";
      host.setAttribute("aria-live", "polite");
      document.body.appendChild(host);
    }
    while (host.children.length >= 3) host.removeChild(host.firstChild);
    var t = document.createElement("div");
    t.className = "sr-toast" + (kind ? " sr-toast--" + kind : "");
    t.setAttribute("role", "status");
    t.textContent = msg;
    host.appendChild(t);
    window.setTimeout(function () {
      t.classList.add("is-out");
      window.setTimeout(function () { if (t.parentNode) t.parentNode.removeChild(t); }, 300);
    }, 4000);
  };

  document.addEventListener(
    "submit",
    function (ev) {
      var form = ev.target;
      if (!form || !form.hasAttribute || !form.hasAttribute("data-confirm")) return;
      if (form.__srOk) { form.__srOk = false; return; }
      ev.preventDefault();
      ev.stopImmediatePropagation();
      var submitter = ev.submitter || null;
      srConfirm(message(form, form), confirmOpts(form)).then(function (ok) {
        if (!ok) return;
        form.__srOk = true;
        if (form.requestSubmit) {
          /* re-issue through the ORIGINAL button so its name/value (and any
             formaction) rides along; the flag passes the new submit event
             straight through this handler */
          if (submitter && submitter.form === form) form.requestSubmit(submitter);
          else form.requestSubmit();
        } else {
          form.submit();
        }
      });
    },
    true
  );

  document.addEventListener(
    "click",
    function (ev) {
      var el = ev.target.closest ? ev.target.closest("[data-print], [data-goto], [data-seek], button[data-confirm], a[data-confirm]") : null;
      if (!el) return;
      if (el.hasAttribute("data-print")) {
        window.print();
        return;
      }
      if (el.hasAttribute("data-seek")) {
        var scope = el.closest("[data-seek-scope]") || document;
        var vid = scope.querySelector("video");
        var t = parseFloat(el.getAttribute("data-seek"));
        if (vid && !isNaN(t)) {
          vid.currentTime = t;
          vid.play().catch(function () { /* poster-only until user presses play */ });
        }
        return;
      }
      if (el.hasAttribute("data-goto")) {
        if (!window.__stuDragged) window.location.href = el.getAttribute("data-goto");
        return;
      }
      if (el.__srOk) { el.__srOk = false; return; }
      ev.preventDefault();
      ev.stopImmediatePropagation();
      srConfirm(message(el, el.form || el.closest("form")), confirmOpts(el)).then(function (ok) {
        if (!ok) return;
        el.__srOk = true;
        el.click();
      });
    },
    true
  );

  document.addEventListener("change", function (ev) {
    var el = ev.target;
    if (el && el.matches && el.matches("select[data-autosubmit]") && el.form) el.form.submit();
  });

  /* data-cull — keyboard culling on the admin bench (Screening Room 3i).
     Arrows move the active frame; S stars it for the portfolio, B sets the
     cover, 1–9 bin it into the Nth section, X cuts it (the delete form's
     data-confirm guard still fires). Every key submits one of the tile's
     EXISTING forms — no new endpoints; the page reload restores the active
     frame from the location hash. */
  /* data-deck-swipe — ON DECK in one hand (Screening Room 3j). At phone width
     the ranked queue deals one card at a time: swipe left = done, swipe right
     = snooze. Both submit the card's existing snooze form — the deck's only
     dismissal endpoint (a nudge sleeps until tomorrow either way; anything
     truly done clears itself server-side once paid/replied/shipped). Cards
     without a snooze key just advance. The Back/Skip buttons cover browsing
     without gestures; on desktop or without JS the deck stays a plain list. */
  var deck = document.querySelector("[data-deck-swipe]");
  if (deck && document.body.classList.contains("sr-admin")) {
    var deckCards = Array.prototype.slice.call(deck.querySelectorAll(".sr-deckcard"));
    var deckNav = document.querySelector("[data-deck-nav]");
    var deckHint = document.querySelector("[data-deck-hint]");
    var deckCount = document.querySelector("[data-deck-count]");
    var deckMq = window.matchMedia("(max-width: 860px)");
    if (deckCards.length) {
      var deckCur = 0;
      var deckPaint = function () {
        deckCards.forEach(function (c, i) {
          c.classList.toggle("is-current", i === deckCur);
          if (i !== deckCur) { c.classList.remove("is-flying"); c.style.transform = ""; }
        });
        if (deckCount) deckCount.textContent = (deckCur + 1) + " of " + deckCards.length;
      };
      var deckStep = function (dir) {
        deckCur = (deckCur + dir + deckCards.length) % deckCards.length;
        deckPaint();
      };
      var deckMode = function () {
        var on = deckMq.matches;
        deck.classList.toggle("is-stack", on);
        if (deckNav) deckNav.hidden = !on;
        if (deckHint) deckHint.hidden = !on;
        if (on) deckPaint();
        else deckCards.forEach(function (c) {
          c.classList.remove("is-current", "is-flying");
          c.style.transform = "";
        });
      };
      if (deckMq.addEventListener) deckMq.addEventListener("change", deckMode);
      else if (deckMq.addListener) deckMq.addListener(deckMode);
      deckMode();

      var prevBtn = document.querySelector("[data-deck-prev]");
      var nextBtn = document.querySelector("[data-deck-next]");
      if (prevBtn) prevBtn.addEventListener("click", function () { deckStep(-1); });
      if (nextBtn) nextBtn.addEventListener("click", function () { deckStep(1); });

      /* the gesture: the card follows the finger horizontally (touch-action:
         pan-y leaves vertical page scroll native); past the threshold it
         flies off and acts, otherwise it springs back */
      /* one finger owns the drag: track its identifier so a second finger
         landing or lifting mid-gesture can neither move the card nor commit
         the swipe with the wrong coordinates; touchcancel (system gesture,
         notification shade) resets cleanly */
      var drag = null;
      var findTouch = function (list, id) {
        for (var i = 0; i < list.length; i++) if (list[i].identifier === id) return list[i];
        return null;
      };
      deck.addEventListener("touchstart", function (e) {
        if (drag || !deck.classList.contains("is-stack") || e.touches.length !== 1) return;
        var card = e.target.closest ? e.target.closest(".sr-deckcard.is-current") : null;
        if (!card) return;
        drag = { card: card, id: e.touches[0].identifier,
                 x: e.touches[0].clientX, y: e.touches[0].clientY, on: false };
        card.classList.remove("is-flying");
      }, { passive: true });
      deck.addEventListener("touchmove", function (e) {
        if (!drag || !deck.classList.contains("is-stack")) return;
        var t = findTouch(e.touches, drag.id);
        if (!t) return;
        var dx = t.clientX - drag.x;
        var dy = t.clientY - drag.y;
        if (!drag.on) {
          if (Math.abs(dx) < 8 || Math.abs(dx) < Math.abs(dy) * 1.2) return;
          drag.on = true;
        }
        drag.card.style.transform = "translateX(" + dx + "px) rotate(" + dx / 28 + "deg)";
      }, { passive: true });
      deck.addEventListener("touchend", function (e) {
        if (!drag) return;
        var t = findTouch(e.changedTouches, drag.id);
        if (!t) return; /* some other finger lifted — ours is still down */
        var d = drag; drag = null;
        var dx = t.clientX - d.x;
        if (!d.on || !deck.classList.contains("is-stack") || Math.abs(dx) < 90) {
          d.card.style.transform = "";
          return;
        }
        var dir = dx > 0 ? 1 : -1;
        d.card.classList.add("is-flying");
        d.card.style.transform = "translateX(" + dir * 130 + "%) rotate(" + dir * 9 + "deg)";
        var form = d.card.querySelector("form.sr-deckcard-snooze");
        window.setTimeout(function () {
          if (form) { if (form.requestSubmit) form.requestSubmit(); else form.submit(); }
          else {
            /* nothing to snooze on this card — reset it so a one-card deck
               springs back instead of staying flown-out blank, then advance */
            d.card.classList.remove("is-flying");
            d.card.style.transform = "";
            deckStep(1);
          }
        }, 200);
      }, { passive: true });
      deck.addEventListener("touchcancel", function () {
        if (!drag) return;
        drag.card.classList.remove("is-flying");
        drag.card.style.transform = "";
        drag = null;
      }, { passive: true });
    }
  }

  var cullGrid = document.querySelector("[data-cull]");
  if (cullGrid) {
    var tiles = Array.prototype.slice.call(cullGrid.querySelectorAll(".gd-tile"));
    if (tiles.length) {
      var cur = 0;
      var m = location.hash.match(/^#asset-(\d+)$/);
      if (m) {
        var ix = tiles.map(function (t) { return t.id; }).indexOf("asset-" + m[1]);
        if (ix >= 0) cur = ix;
      }
      var mark = function () {
        tiles.forEach(function (t, i) { t.classList.toggle("is-culling", i === cur); });
        tiles[cur].scrollIntoView({ block: "nearest" });
        history.replaceState(null, "", "#" + tiles[cur].id);
      };
      var submitIn = function (sel) {
        var f = tiles[cur].querySelector(sel);
        if (!f) return false;
        if (f.requestSubmit) f.requestSubmit(); else f.submit();
        return true;
      };
      document.addEventListener("keydown", function (e) {
        if (e.metaKey || e.ctrlKey || e.altKey) return;
        if (e.target.matches && e.target.matches("input, textarea, select, [contenteditable]")) return;
        var k = e.key;
        if (k === "ArrowRight" || k === "ArrowLeft") {
          e.preventDefault();
          cur = (cur + (k === "ArrowRight" ? 1 : -1) + tiles.length) % tiles.length;
          mark();
        } else if (k === "s" || k === "S") {
          if (submitIn('form[action$="/portfolio"]')) e.preventDefault();
        } else if (k === "b" || k === "B") {
          if (submitIn('form[action$="/cover"]')) e.preventDefault();
        } else if (k >= "1" && k <= "9") {
          var sel = tiles[cur].querySelector('form[action$="/section"] select[name="section_id"]');
          if (sel && sel.options.length > +k) {
            e.preventDefault();
            sel.selectedIndex = +k;
            if (sel.form.requestSubmit) sel.form.requestSubmit(); else sel.form.submit();
          }
        } else if (k === "x" || k === "X") {
          if (submitIn('form[action$="/delete"]')) e.preventDefault();
        }
      });
      mark();
    }
  }
})();
