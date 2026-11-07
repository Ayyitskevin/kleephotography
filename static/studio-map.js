/* Read-only age/value projection. Pure layout also runs under Node's test runner. */
(function () {
  "use strict";
  function number(value) {
    if (value === null || value === undefined || String(value).trim() === "") return null;
    var n = Number(value);
    return Number.isFinite(n) ? n : null;
  }
  function layout(rows, clock, days) {
    var valid = [], excluded = [];
    rows.forEach(function (row) {
      var age = number(row[clock]);
      if (age === null || age < 0) excluded.push({ row: row, reason: "Age unavailable" });
      else if (days !== null && age > days) excluded.push({ row: row, reason: "Older than this window" });
      else valid.push({ row: row, age: age });
    });
    var maxAge = days === null ? Math.max(1, ...valid.map(function (p) { return p.age; })) : days;
    var maxValue = Math.max(1, ...rows.map(function (r) { return Math.max(0, number(r.value) || 0); }));
    var groups = new Map();
    valid.sort(function (a, b) { return String(a.row.id).localeCompare(String(b.row.id)); });
    valid.forEach(function (p) {
      var value = number(p.row.value);
      var x = value !== null && value > 0 ? 42 + 144 * value / maxValue : 14;
      var y = 20 + 360 * p.age / maxAge;
      // Quantized collision buckets have stable membership for the same snapshot.
      var key = p.row.stage + ":" + (x === 14 ? "unknown" : Math.round(x / 28)) + ":" + Math.round(y / 28);
      if (!groups.has(key)) groups.set(key, { stage: p.row.stage, items: [], x: 0, y: 0 });
      var g = groups.get(key);
      g.items.push(p); g.x += x; g.y += y;
    });
    var points = Array.from(groups.values()).map(function (g) {
      g.x /= g.items.length; g.y /= g.items.length; return g;
    });
    return { points: points, excluded: excluded, maxAge: maxAge, maxValue: maxValue, count: valid.length };
  }
  if (typeof module !== "undefined" && module.exports) module.exports = { layout: layout, number: number };
  if (typeof document === "undefined") return;
  var money = new Intl.NumberFormat("en-US", { style: "currency", currency: "USD", maximumFractionDigits: 0 });
  function el(tag, cls, text) {
    var node = document.createElement(tag);
    if (cls) node.className = cls;
    if (text !== undefined) node.textContent = text;
    return node;
  }
  function render() {
    var map = document.getElementById("studio-map");
    var board = document.getElementById("studio-board");
    if (!map || map.hidden || !board) return;
    var clock = map.querySelector("[data-map-clock]").value;
    var windowValue = map.querySelector("[data-map-window]").value;
    var days = windowValue === "all" ? null : Number(windowValue);
    var rows = Array.from(board.querySelectorAll(".stu-card")).map(function (card) {
      return { id: card.dataset.pid, stage: card.dataset.stage, age: card.dataset.age,
        quiet: card.dataset.quiet, value: card.dataset.value, touch: card.dataset.clientTouch,
        title: card.querySelector(".stu-card-proj").textContent.trim(),
        company: card.querySelector(".stu-card-co").textContent.trim(),
        action: (card.querySelector(".stu-intel") || {}).textContent || "",
        href: card.getAttribute("data-goto") };
    });
    var result = layout(rows, clock, days);
    var plot = map.querySelector("[data-map-plot]");
    plot.replaceChildren();
    var axis = el("div", "stu-map-axis");
    axis.appendChild(el("span", "stu-map-axis-title", "Age ↓"));
    for (var i = 0; i <= 4; i++) {
      var tick = el("span", "stu-map-tick", Math.round(result.maxAge * i / 4 * 10) / 10 + "d");
      tick.style.top = 76 + i * 90 + "px"; axis.appendChild(tick);
    }
    plot.appendChild(axis);
    function description(p) {
      var r = p.row;
      return r.title + " · " + r.company + " · " + p.age + "d " + (clock === "quiet" ? "client quiet" : "in stage") +
        " · " + (number(r.value) > 0 ? money.format(r.value / 100) : "No positive value shown") +
        (clock === "quiet" && !r.touch ? " · No recorded client action; using stage/creation date" : "") +
        (r.action ? " · " + r.action : "");
    }
    function link(p, cls) {
      var a = el("a", cls, p.row.title);
      a.href = p.row.href;
      a.setAttribute("aria-label", description(p));
      a.title = description(p);
      return a;
    }
    Array.from(board.querySelectorAll(".stu-col")).filter(function (col) {
      return col.dataset.stage !== "archived";
    }).forEach(function (col) {
      var stage = col.dataset.stage;
      var groups = result.points.filter(function (g) { return g.stage === stage; });
      var count = groups.reduce(function (n, g) { return n + g.items.length; }, 0);
      var lane = el("section", "stu-map-lane");
      lane.appendChild(el("h2", "stu-map-heading", col.querySelector(".stu-col-name").textContent.trim() + " · " + count));
      var field = el("div", "stu-map-field");
      groups.forEach(function (g) {
        var node;
        if (g.items.length === 1) {
          node = link(g.items[0], "stu-map-point");
          node.replaceChildren(el("span", "stu-map-tooltip", description(g.items[0])));
        } else {
          node = el("details", "stu-map-cluster");
          var summary = el("summary", "", String(g.items.length));
          summary.setAttribute("aria-label", g.items.length + " nearby projects; expand to choose");
          node.appendChild(summary);
          var list = el("ul", "stu-map-cluster-list");
          g.items.forEach(function (p) { var li = el("li"); li.appendChild(link(p)); list.appendChild(li); });
          node.appendChild(list);
        }
        node.style.left = g.x + "px"; node.style.top = g.y + "px";
        field.appendChild(node);
      });
      lane.appendChild(field);
      lane.appendChild(el("p", "stu-map-scale", "—    " + money.format(0) + " → " + money.format(result.maxValue / 100)));
      plot.appendChild(lane);
    });
    map.querySelector("[data-map-summary]").textContent = result.count + " of " + rows.length + " projects plotted";
    map.querySelector("[data-map-clock-help]").hidden = clock !== "quiet";
    var excluded = map.querySelector("[data-map-excluded]");
    excluded.hidden = !result.excluded.length;
    map.querySelector("[data-map-excluded-label]").textContent = result.excluded.length + " projects outside this view";
    var list = map.querySelector("[data-map-excluded-list]");
    list.replaceChildren();
    result.excluded.forEach(function (p) {
      var li = el("li"); var a = el("a", "", p.row.title); a.href = p.row.href;
      li.append(a, document.createTextNode(" — " + p.reason)); list.appendChild(li);
    });
  }
  window.miseStudioMap = { render: render };
  var toggle = document.querySelector("[data-map-toggle]");
  if (toggle) toggle.hidden = false;
  document.addEventListener("change", function (ev) {
    if (ev.target.matches("[data-map-clock], [data-map-window]")) render();
  });
})();
