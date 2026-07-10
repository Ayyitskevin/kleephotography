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
      var el = ev.target.closest ? ev.target.closest("[data-print], [data-goto], button[data-confirm], a[data-confirm]") : null;
      if (!el) return;
      if (el.hasAttribute("data-print")) {
        window.print();
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
})();
