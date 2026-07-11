/* Delegated handlers for what used to be inline on*-attributes — the CSP ships
   without script-src 'unsafe-inline', so templates opt in via data attributes:

     form[data-confirm]          confirm() at submit time; {name} in the message
                                 interpolates the named form control's current
                                 value (e.g. "Sign as {signer_name}?")
     button/a[data-confirm]      confirm() at click time, for buttons that share
                                 a form with non-destructive siblings
     [data-print]                window.print()
     select[data-autosubmit]     submit the owning form on change
     [data-goto]                 navigate to the given URL on click, unless
                                 window.__stuDragged is set (the studio board
                                 raises it during a drag so drop != navigate)
     [data-seek]                 chapter chips: jump the nearest scoped <video>
                                 (closest [data-seek-scope], else the page's
                                 first video) to the given second and play

   Capture-phase listeners so a cancelled confirm also stops htmx/other
   delegated listeners from acting on the same event. */
(function () {
  "use strict";

  function message(el, form) {
    var msg = el.getAttribute("data-confirm") || "";
    return msg.replace(/\{([A-Za-z0-9_-]+)\}/g, function (whole, name) {
      var field = form && form.elements ? form.elements[name] : null;
      return field && "value" in field ? field.value : whole;
    });
  }

  document.addEventListener(
    "submit",
    function (ev) {
      var form = ev.target;
      if (form && form.hasAttribute && form.hasAttribute("data-confirm")) {
        if (!window.confirm(message(form, form))) {
          ev.preventDefault();
          ev.stopImmediatePropagation();
        }
      }
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
      if (!window.confirm(message(el, el.form || el.closest("form")))) {
        ev.preventDefault();
        ev.stopImmediatePropagation();
      }
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
