(function () {
  const lb = document.getElementById("lightbox");
  if (!lb) return;
  const stage = lb.querySelector(".lb-stage");
  const favBtn = lb.querySelector(".lb-fav");
  const dlLink = lb.querySelector(".lb-dl");
  const dlMp4 = lb.querySelector(".lb-dl-mp4");
  const playBtn = lb.querySelector(".lb-play");
  const proofLabel = lb.querySelector(".lb-proof");
  const tiles = Array.from(document.querySelectorAll(".tile"));
  let idx = -1;
  let timer = null;

  // ── Timecoded review comments (only present on the client gallery) ──────────
  const slug = lb.dataset.slug;
  const cWrap = lb.querySelector(".lb-comments");
  const cList = cWrap && cWrap.querySelector(".vc-list");
  const cForm = cWrap && cWrap.querySelector(".vc-form");
  const cBody = cForm && cForm.querySelector(".vc-body");
  const cAt = cForm && cForm.querySelector(".vc-at");
  const cTc = cForm && cForm.querySelector(".vc-timecode");
  const cParent = cForm && cForm.querySelector(".vc-parent");
  const cCancel = cForm && cForm.querySelector(".vc-cancel-reply");
  const cCount = cWrap && cWrap.querySelector(".vc-count");
  const cFilter = cWrap && cWrap.querySelector(".vc-filter");
  let activeVideo = null;
  let activeAsset = null;
  let lastComments = [];
  let commentRequestVersion = 0;
  const commentDrafts = new Map();
  let commentDraftRevision = 0;
  const pendingCommentAssets = new Set();

  function ownsCommentResponse(assetId, requestVersion) {
    return activeAsset === assetId && commentRequestVersion === requestVersion;
  }

  function fmtTC(s) {
    s = Math.max(0, Math.floor(Number(s) || 0));
    return Math.floor(s / 60) + ":" + String(s % 60).padStart(2, "0");
  }

  function saveCommentDraft(assetId, changed = false) {
    if (!cForm || !assetId) return null;
    const previous = commentDrafts.get(assetId);
    const draft = {
      body: cBody.value,
      parent: cParent.value,
      timecode: cTc.value || "0",
      revision: changed ? ++commentDraftRevision : (previous ? previous.revision : 0),
    };
    commentDrafts.set(assetId, draft);
    return draft;
  }

  function restoreCommentDraft(assetId) {
    if (!cForm) return;
    const draft = (assetId && commentDrafts.get(assetId)) || {
      body: "",
      parent: "",
      timecode: "0",
      revision: 0,
    };
    cBody.value = draft.body;
    cParent.value = draft.parent;
    cCancel.hidden = !draft.parent;
    cTc.value = draft.timecode;
    cAt.textContent = "Comment at " + fmtTC(draft.timecode);
  }

  function clearSubmittedDraft(assetId, revision) {
    const draft = commentDrafts.get(assetId);
    if (!draft || draft.revision !== revision) return false;
    commentDrafts.delete(assetId);
    return true;
  }

  function clearReply() {
    if (!cForm) return;
    cParent.value = "";
    cCancel.hidden = true;
  }

  function renderComments(list) {
    if (!cList) return;
    lastComments = list;
    // Open-count = root threads still open (cascade keeps a thread's status coherent).
    if (cCount) {
      const open = list.filter((c) => !c.parent_id && (c.status || "open") === "open").length;
      cCount.textContent = open ? open + " open" : "all resolved";
      cCount.classList.toggle("ok", open === 0 && list.length > 0);
    }
    const hideResolved = cFilter && cFilter.checked;
    cList.innerHTML = "";
    const byParent = {};
    list.forEach((c) => { (byParent[c.parent_id || 0] = byParent[c.parent_id || 0] || []).push(c); });
    (function build(parent, depth) {
      (byParent[parent] || []).forEach((c) => {
        const resolved = (c.status || "open") === "resolved";
        // Cascade guarantees a resolved thread is resolved top-to-bottom, so
        // skipping per-comment hides the whole thread when the filter is on.
        if (hideResolved && resolved) return;
        const li = document.createElement("li");
        li.className = "vc" + (resolved ? " vc-resolved" : "");
        li.style.marginLeft = (depth * 1.1) + "rem";
        const tc = document.createElement("button");
        tc.type = "button";
        tc.className = "vc-tc";
        tc.textContent = fmtTC(c.timecode);
        // The seek payoff: clicking a timecode jumps the player there.
        tc.addEventListener("click", () => {
          if (activeVideo) { activeVideo.currentTime = c.timecode; activeVideo.play().catch(() => {}); }
        });
        const role = document.createElement("span");
        role.className = "vc-role" + (c.author_role === "admin" ? " studio" : "");
        role.textContent = c.author_role === "admin" ? "Studio" : "You";
        const text = document.createElement("span");
        text.className = "vc-text";
        text.textContent = c.body;
        const reply = document.createElement("button");
        reply.type = "button";
        reply.className = "vc-reply";
        reply.textContent = "reply";
        reply.addEventListener("click", () => {
          stopShow();
          cParent.value = c.id;
          cCancel.hidden = false;
          saveCommentDraft(activeAsset, true);
          cBody.focus();
        });
        li.append(tc, " ", role, " ", text, " ", reply);
        cList.appendChild(li);
        build(c.id, depth + 1);
      });
    })(0, 0);
  }

  async function loadComments(assetId) {
    if (!cWrap) return;
    const requestVersion = ++commentRequestVersion;
    try {
      const res = await fetch("/g/" + slug + "/comments/" + assetId);
      if (!ownsCommentResponse(assetId, requestVersion)) return;
      if (!res.ok) return;
      const comments = await res.json();
      if (!ownsCommentResponse(assetId, requestVersion)) return;
      renderComments(comments);
    } catch (e) { /* leave the thread empty on a transient error */ }
  }

  function stopShow() {
    if (timer) { clearInterval(timer); timer = null; }
    if (playBtn) {
      playBtn.innerHTML = "▶";
      playBtn.setAttribute("aria-pressed", "false");
    }
  }

  function startShow() {
    timer = setInterval(() => step(1), 4000);
    playBtn.innerHTML = "❚❚";
    playBtn.setAttribute("aria-pressed", "true");
  }

  // the marketing-site lightbox has no action bar — every bar element may be null
  function syncFav(t) {
    if (!favBtn) return;
    const fb = t.querySelector(".fav-btn");
    if (!fb) return;
    const faved = fb.classList.contains("faved");
    favBtn.innerHTML = faved ? "♥" : "♡";
    favBtn.classList.toggle("faved", faved);
  }

  // Mirror the section's live "X of N picked" label into the lightbox when the
  // current tile sits in a proofing section. Hide the slot otherwise.
  function refreshProof(t) {
    if (!proofLabel) return;
    const sec = t && t.dataset.section;
    const src = sec && document.getElementById("proof-" + sec);
    if (!src) { proofLabel.hidden = true; proofLabel.textContent = ""; return; }
    proofLabel.textContent = src.textContent.trim();
    proofLabel.classList.toggle("ok", src.classList.contains("ok"));
    proofLabel.hidden = false;
  }

  // Filter-aware navigation: tiles hidden by the grid's active filter
  // (.pf-hidden or any display:none) are skipped, so arrows/swipe/slideshow
  // only visit what the visitor can currently see in the grid.
  function visibleTile(t) { return t.offsetParent !== null; }
  function step(dir) {
    let i = idx;
    for (let n = 0; n < tiles.length; n++) {
      i = (i + dir + tiles.length) % tiles.length;
      if (visibleTile(tiles[i])) return render(i);
    }
  }

  function mediaName(t, fallback) {
    const source = t && t.querySelector("img");
    return (source && source.alt) || fallback;
  }

  function render(i) {
    saveCommentDraft(activeAsset);
    idx = (i + tiles.length) % tiles.length;
    const t = tiles[idx];
    syncFav(t);
    refreshProof(t);
    if (dlLink) dlLink.href = t.dataset.dl || "#";
    // videos also offer the web-ready MP4 straight from the viewer
    if (dlMp4) {
      if (t.dataset.dlWeb) { dlMp4.href = t.dataset.dlWeb; dlMp4.hidden = false; }
      else { dlMp4.hidden = true; dlMp4.href = "#"; }
    }
    stage.innerHTML = "";
    if (t.dataset.kind === "video") {
      const v = document.createElement("video");
      v.src = t.dataset.web;
      if (t.dataset.poster) v.poster = t.dataset.poster;
      v.controls = true;
      v.playsInline = true;
      v.setAttribute("playsinline", "");
      v.setAttribute("aria-label", mediaName(t, "Video"));
      stage.appendChild(v);
      activeVideo = v;
      activeAsset = t.dataset.id;
      if (cWrap) {
        cWrap.hidden = false;
        lastComments = [];
        if (cList) cList.innerHTML = "";
        if (cCount) {
          cCount.textContent = "";
          cCount.classList.remove("ok");
        }
        vcError("");
        restoreCommentDraft(activeAsset);
        loadComments(activeAsset);
      }
    } else {
      const img = document.createElement("img");
      img.src = t.dataset.web;
      // Carry the tile's alt onto the enlarged image so screen-reader users
      // get the same description in the viewer as in the grid.
      img.alt = mediaName(t, "");
      stage.appendChild(img);
      activeVideo = null;
      activeAsset = null;
      commentRequestVersion += 1;
      if (cWrap) {
        cWrap.hidden = true;
        restoreCommentDraft(null);
      }
    }
  }

  // Return focus to whatever opened the lightbox when it closes.
  let lastFocused = null;
  const closeBtn = lb.querySelector(".lb-close");

  function open(i) {
    lastFocused = document.activeElement;
    render(i);
    lb.hidden = false;
    document.body.style.overflow = "hidden";
    if (closeBtn) closeBtn.focus();
  }
  function close() {
    saveCommentDraft(activeAsset);
    stopShow();
    lb.hidden = true; stage.innerHTML = ""; document.body.style.overflow = "";
    activeVideo = null; activeAsset = null;
    commentRequestVersion += 1;
    if (cWrap) {
      cWrap.hidden = true;
      if (cList) cList.innerHTML = "";
      restoreCommentDraft(null);
    }
    if (lastFocused && lastFocused.focus) lastFocused.focus();
    lastFocused = null;
  }

  // Each tile image opens the lightbox. It's a real control, so make it
  // keyboard-reachable (Tab) and operable (Enter/Space), not just a click
  // target — the figure itself stays a plain container.
  tiles.forEach((t, i) => {
    const img = t.querySelector("img");
    if (!img) return;
    img.setAttribute("tabindex", "0");
    img.setAttribute("role", "button");
    if (!img.getAttribute("aria-label")) {
      const action = t.dataset.kind === "video" ? "open video" : "view larger";
      const fallback = t.dataset.kind === "video" ? "Video" : "Photo";
      img.setAttribute("aria-label", mediaName(t, fallback) + " — " + action);
    }
    img.addEventListener("click", () => open(i));
    img.addEventListener("keydown", (e) => {
      if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") {
        e.preventDefault();
        open(i);
      }
    });
  });
  // Shared fav trigger — used by both the ♥ button and the double-tap gesture.
  // Routes through htmx.ajax so OOB section-progress swap + HX-Trigger
  // 'proof-cap' toast process exactly like a grid-driven heart click. The
  // promise resolves after HTMX has processed swaps, so we can re-mirror the
  // state into the lightbox then.
  function triggerFav() {
    const t = tiles[idx];
    if (!t || !t.dataset.fav) return;
    const target = t.querySelector("button.icon-btn");
    return htmx.ajax("POST", t.dataset.fav, { target: target, swap: "innerHTML" })
      .then(() => { syncFav(t); refreshProof(t); });
  }
  if (favBtn) favBtn.addEventListener("click", triggerFav);
  if (playBtn) playBtn.addEventListener("click", () => (timer ? stopShow() : startShow()));

  // Freeze the note to the current playhead, then show it on the button.
  if (cAt) cAt.addEventListener("click", () => {
    if (!activeVideo) return;
    stopShow();
    cTc.value = activeVideo.currentTime;
    cAt.textContent = "Comment at " + fmtTC(activeVideo.currentTime);
    saveCommentDraft(activeAsset, true);
  });
  if (cCancel) cCancel.addEventListener("click", () => {
    stopShow();
    clearReply();
    saveCommentDraft(activeAsset, true);
  });
  if (cForm) cForm.addEventListener("focusin", stopShow);
  if (cBody) cBody.addEventListener("input", () => {
    stopShow();
    saveCommentDraft(activeAsset, true);
  });
  // Client-side filter — re-render the already-loaded thread, no fetch.
  if (cFilter) cFilter.addEventListener("change", () => renderComments(lastComments));
  if (cForm) cForm.addEventListener("submit", async (e) => {
    e.preventDefault();
    const assetId = activeAsset;
    if (!assetId || pendingCommentAssets.has(assetId)) return;
    const draft = saveCommentDraft(assetId);
    const submittedRevision = draft.revision;
    const body = draft.body.trim();
    if (!body) return;
    const requestVersion = ++commentRequestVersion;
    const fd = new FormData();
    fd.append("body", body);
    // A reply inherits its parent's timecode server-side; a top-level note uses
    // the frozen "Comment at" value, falling back to the live playhead.
    if (draft.parent) {
      fd.append("parent_id", draft.parent);
    } else {
      fd.append("timecode", draft.timecode || (activeVideo ? activeVideo.currentTime : 0));
    }
    pendingCommentAssets.add(assetId);
    try {
      let res;
      try {
        res = await fetch("/g/" + slug + "/comments/" + assetId, { method: "POST", body: fd });
      } catch (err) {
        res = null;
      }
      if (res && res.ok) {
        const comments = await res.json().catch(() => null);
        const draftCleared = clearSubmittedDraft(assetId, submittedRevision);
        if (draftCleared && activeAsset === assetId) restoreCommentDraft(assetId);
        if (!ownsCommentResponse(assetId, requestVersion)) return;
        if (!comments) {
          vcError("Your note was posted, but comments couldn't refresh — reload to see it.");
          return;
        }
        renderComments(comments);
        vcError("");
        return;
      }
      if (!ownsCommentResponse(assetId, requestVersion)) return;
      if (res && (res.status === 403 || res.status === 410)) {
        // session aged out or gallery expired mid-session — reload to the gate so
        // the client re-unlocks rather than losing the typed note to a dead button
        window.location.reload();
      } else {
        // keep the typed text; tell the client it didn't post instead of silently
        // swallowing the failure
        vcError("Couldn't post your note — refresh the page and try again.");
      }
    } finally {
      pendingCommentAssets.delete(assetId);
    }
  });

  function vcError(msg) {
    if (!cForm) return;
    let el = cForm.querySelector(".vc-error");
    if (!el) {
      el = document.createElement("p");
      el.className = "vc-error";
      cForm.appendChild(el);
    }
    el.textContent = msg;
    el.hidden = !msg;
  }
  lb.querySelector(".lb-close").addEventListener("click", close);
  lb.querySelector(".lb-prev").addEventListener("click", () => { stopShow(); step(-1); });
  lb.querySelector(".lb-next").addEventListener("click", () => { stopShow(); step(1); });
  lb.addEventListener("click", (e) => { if (e.target === lb) close(); });

  document.addEventListener("keydown", (e) => {
    if (lb.hidden || e.defaultPrevented || e.isComposing) return;
    if (e.key === "Escape") {
      close();
      return;
    }

    const target = e.target;
    const arrowOwner = target && (
      target.isContentEditable ||
      (target.closest && target.closest("input, textarea, select, video"))
    );

    if (e.key === "ArrowLeft" || e.key === "ArrowRight") {
      if (e.metaKey || e.ctrlKey || e.altKey || e.shiftKey || arrowOwner) return;
      e.preventDefault();
      stopShow();
      step(e.key === "ArrowLeft" ? -1 : 1);
      return;
    }
    // Keep Tab inside the modal so focus can't wander back to the muted grid.
    if (e.key === "Tab") {
      const focusable = Array.from(
        lb.querySelectorAll('button, a[href], input, textarea, [tabindex]:not([tabindex="-1"])')
      ).filter((el) => !el.hidden && el.getClientRects().length > 0);
      if (!focusable.length) return;
      const first = focusable[0];
      const last = focusable[focusable.length - 1];
      if (e.shiftKey && document.activeElement === first) {
        e.preventDefault();
        last.focus();
      } else if (!e.shiftKey && document.activeElement === last) {
        e.preventDefault();
        first.focus();
      }
    }
  });

  // Touch gestures: horizontal swipe → navigate, double-tap on the image →
  // favorite (matches Pixieset-style proofing UX on phones, where the ♥ icon
  // is small relative to the photo). Marketing-site tiles have no data-fav
  // so triggerFav() no-ops on them; the swipe behavior is unchanged.
  let x0 = null, y0 = null, t0 = 0, lastTap = 0;
  lb.addEventListener("touchstart", (e) => {
    x0 = e.touches[0].clientX;
    y0 = e.touches[0].clientY;
    t0 = Date.now();
  }, { passive: true });
  lb.addEventListener("touchend", (e) => {
    if (x0 === null) return;
    const dx = e.changedTouches[0].clientX - x0;
    const dy = e.changedTouches[0].clientY - y0;
    const dt = Date.now() - t0;
    x0 = null;
    // Horizontal swipe → navigate; vertical bias filters out scroll attempts
    if (Math.abs(dx) > 50 && Math.abs(dx) > Math.abs(dy)) {
      stopShow(); step(dx < 0 ? 1 : -1);
      lastTap = 0;
      return;
    }
    // Short, near-stationary touch → candidate tap
    if (dt < 300 && Math.abs(dx) < 20 && Math.abs(dy) < 20) {
      const now = Date.now();
      // Second quick tap on the image area → favorite. Buttons in .lb-actions
      // handle their own clicks; only fav from taps on the stage itself.
      if (now - lastTap < 350 && e.target.closest(".lb-stage")) {
        lastTap = 0;
        triggerFav();
      } else {
        lastTap = now;
      }
    }
  }, { passive: true });
})();
