// Copy-link buttons (gallery / portal / proposal / contract / invoice admin
// pages). Tiny global handler — one delegated document listener copies the
// clicked .copy-link button's data-copy payload with
// navigator.clipboard.writeText, so buttons arriving inside an HTMX-swapped
// fragment work without rebinding. Sibling .copy-feedback span flashes
// "Copied!" for 2.5s. Graceful failure copy when the API isn't available
// (older browsers, non-HTTPS).
(function () {
  let timer = null;
  document.addEventListener("click", async (ev) => {
    const btn = ev.target.closest ? ev.target.closest(".copy-link") : null;
    if (!btn) return;
    const fb = btn.parentElement.querySelector(".copy-feedback");
    try {
      await navigator.clipboard.writeText(btn.dataset.copy);
      if (fb) fb.textContent = "Copied!";
    } catch (e) {
      if (fb) fb.textContent = "Copy failed — select and copy manually.";
    }
    if (!fb) return;
    fb.hidden = false;
    if (timer) clearTimeout(timer);
    timer = setTimeout(() => fb.hidden = true, 2500);
  });
})();
