/* Shared specialty / subject filter chips for /portfolio and /reels.
   Wire a <nav data-pf ...> (see templates/site/portfolio.html + reels.html).
   Modes:
     namespaced — filters are "" | "sp:re" | "tag:dishes"; items use data-sp /
                  data-tag; optional hash sync + films-first toggle
     sp         — filters are "" | "re"; items use data-sp only
*/
(function () {
  const root = document.querySelector("[data-pf]");
  if (!root) return;

  const mode = root.dataset.pfMode || "namespaced";
  const itemSel = root.dataset.pfItems || "[data-sp]";
  const items = document.querySelectorAll(itemSel);
  const chips = root.querySelectorAll(".pf-chip[data-filter]");
  const useHash = root.dataset.pfHash === "1";
  const masonrySel = root.dataset.pfMasonry || "";
  const filmsFirstSel = root.dataset.pfFilmsFirst || "";
  const masonry = masonrySel ? document.querySelector(masonrySel) : null;
  const filmsFirst = filmsFirstSel ? document.querySelector(filmsFirstSel) : null;

  function match(el, f) {
    if (!f) return true;
    if (mode === "sp") return (el.dataset.sp || "") === f;
    if (f.startsWith("sp:")) return (el.dataset.sp || "") === f.slice(3);
    if (f.startsWith("tag:")) return (el.dataset.tag || "") === f.slice(4);
    return true;
  }

  function apply(f) {
    items.forEach((el) => {
      el.classList.toggle("pf-hidden", f !== "" && !match(el, f));
    });
    chips.forEach((c) => {
      const on = (c.dataset.filter || "") === (f || "");
      c.classList.toggle("pf-chip-active", on);
      c.setAttribute("aria-pressed", on ? "true" : "false");
    });
    if (useHash) {
      if (f) history.replaceState(null, "", "#" + f);
      else history.replaceState(null, "", location.pathname);
    }
  }

  chips.forEach((c) =>
    c.addEventListener("click", () => apply(c.dataset.filter || ""))
  );

  // ▶ Films first: lift film tiles to the head of the grid (flex order via
  // the sr-films-first class) — independent of the stock/subject filter.
  if (filmsFirst && masonry) {
    filmsFirst.addEventListener("click", () => {
      const on = filmsFirst.getAttribute("aria-pressed") !== "true";
      filmsFirst.setAttribute("aria-pressed", on ? "true" : "false");
      masonry.classList.toggle("sr-films-first", on);
    });
  }

  if (useHash) {
    let initial = (location.hash || "").slice(1);
    // Pre-revamp bookmarks/shares used bare tag hashes (#dishes) — map them
    // onto the namespaced filter so old links keep landing on the right view.
    if (
      initial &&
      !root.querySelector(`.pf-chip[data-filter="${CSS.escape(initial)}"]`)
    ) {
      initial = "tag:" + initial;
    }
    if (
      initial &&
      root.querySelector(`.pf-chip[data-filter="${CSS.escape(initial)}"]`)
    ) {
      apply(initial);
    }
  }
})();
