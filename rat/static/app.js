(function () {
  "use strict";

  function showError(message) {
    var box = document.getElementById("form-error");
    if (!box) { return; }
    box.textContent = message;
    box.classList.remove("hidden");
  }

  async function postForm(form) {
    var res = await fetch("/api/repos", { method: "POST", body: new FormData(form) });
    var data = {};
    try { data = await res.json(); } catch (e) { /* non-JSON error page */ }
    if (res.ok && data.repo_id) {
      window.location.href = "/repo/" + data.repo_id;
    } else {
      showError(data.error || ("Request failed (HTTP " + res.status + ")"));
    }
  }

  function pollJobs() {
    var bars = Array.prototype.slice.call(
      document.querySelectorAll(".progress[data-job-id]")
    );
    if (!bars.length) { return; }
    var timer = setInterval(async function () {
      var anyRunning = false;
      for (var i = 0; i < bars.length; i++) {
        var el = bars[i];
        try {
          var res = await fetch("/api/jobs/" + el.dataset.jobId);
          if (!res.ok) { continue; }
          var job = await res.json();
          if (job.status === "running") {
            anyRunning = true;
            var pct = job.total ? Math.floor((100 * job.processed) / job.total) : 0;
            el.querySelector(".bar").style.width = pct + "%";
            var counts = job.total
              ? " (" + job.processed.toLocaleString() + "/" + job.total.toLocaleString() + ", " + pct + "%)"
              : "";
            el.querySelector(".progress-text").textContent =
              (job.phase || "") + " — " + (job.message || "working...") + counts;
          }
        } catch (e) { /* server busy; retry on the next tick */ }
      }
      if (!anyRunning) {
        clearInterval(timer);
        window.location.reload();
      }
    }, 1000);
  }

  function cellValue(td) {
    var raw = td.textContent.trim();
    var num = Number(raw.replace(/,/g, ""));
    return raw !== "" && !isNaN(num) ? num : raw.toLowerCase();
  }

  function initSortableTables() {
    var tables = document.querySelectorAll("table[data-sortable]");
    for (var t = 0; t < tables.length; t++) {
      (function (table) {
        var tbody = table.querySelector("tbody");
        if (!tbody) { return; }
        var heads = table.querySelectorAll("thead th");
        for (var h = 0; h < heads.length; h++) {
          (function (th, index) {
            th.addEventListener("click", function () {
              var dir = th.dataset.sort === "asc" ? "desc" : "asc";
              for (var i = 0; i < heads.length; i++) { delete heads[i].dataset.sort; }
              th.dataset.sort = dir;
              var all = Array.prototype.slice.call(tbody.querySelectorAll("tr"));
              var rows = all.filter(function (row) { return row.cells.length === heads.length; });
              var hints = all.filter(function (row) { return row.cells.length !== heads.length; });
              rows.sort(function (a, b) {
                var ka = cellValue(a.cells[index]);
                var kb = cellValue(b.cells[index]);
                var cmp = (typeof ka === "number" && typeof kb === "number")
                  ? ka - kb
                  : String(ka).localeCompare(String(kb));
                return dir === "asc" ? cmp : -cmp;
              });
              for (var r = 0; r < rows.length; r++) { tbody.appendChild(rows[r]); }
              for (var h2 = 0; h2 < hints.length; h2++) { tbody.appendChild(hints[h2]); }
            });
          })(heads[h], h);
        }
      })(tables[t]);
    }
  }

  function initTableSearch() {
    var inputs = document.querySelectorAll("input.table-search[data-table]");
    for (var i = 0; i < inputs.length; i++) {
      (function (input) {
        var table = document.getElementById(input.dataset.table);
        if (!table) { return; }
        input.addEventListener("input", function () {
          var query = input.value.trim().toLowerCase();
          var rows = table.querySelectorAll("tbody tr");
          for (var r = 0; r < rows.length; r++) {
            var match = !query || rows[r].textContent.toLowerCase().indexOf(query) !== -1;
            rows[r].classList.toggle("hidden", !match);
          }
        });
      })(inputs[i]);
    }
  }

  async function loadRepoKpis() {
    var cells = document.querySelectorAll("[data-kpis-repo]");
    for (var i = 0; i < cells.length; i++) {
      var cell = cells[i];
      if (cell.dataset.kpisState !== "ready") { continue; }
      try {
        var res = await fetch("/api/repos/" + cell.dataset.kpisRepo);
        var data = res.ok ? await res.json() : null;
        if (data && data.summary) {
          var s = data.summary;
          cell.textContent = s.files.toLocaleString() + " files · " +
            s.directories.toLocaleString() + " dirs · " +
            s.authors.toLocaleString() + " authors · churn " + s.churn.toLocaleString();
        } else {
          cell.textContent = "—";
        }
      } catch (e) {
        cell.textContent = "—";
      }
    }
  }

  var SVG_NS = "http://www.w3.org/2000/svg";

  function svgEl(tag, attrs, text) {
    var el = document.createElementNS(SVG_NS, tag);
    for (var key in attrs) { el.setAttribute(key, attrs[key]); }
    if (text !== undefined) { el.textContent = text; }
    return el;
  }

  function fmtShort(n) {
    if (n >= 1000000) { return (n / 1000000).toFixed(1) + "M"; }
    if (n >= 1000) { return (n / 1000).toFixed(1) + "k"; }
    return String(n);
  }

  function renderBarChart(container, rows, labelKey, valueKey, color, suffixFn) {
    if (!container) { return; }
    if (!rows || !rows.length) { container.textContent = "No data."; return; }
    var width = 700;
    var labelW = 210;
    var valueW = 90;
    var rowH = 24;
    var pad = 8;
    var max = 1;
    for (var i = 0; i < rows.length; i++) {
      if (rows[i][valueKey] > max) { max = rows[i][valueKey]; }
    }
    var height = rows.length * rowH + pad * 2;
    var svg = svgEl("svg", { viewBox: "0 0 " + width + " " + height });
    for (var r = 0; r < rows.length; r++) {
      var row = rows[r];
      var y = pad + r * rowH;
      var barMax = width - labelW - valueW - pad * 2;
      var barW = Math.max(2, Math.round(barMax * (row[valueKey] / max)));
      var label = String(row[labelKey]);
      if (label.length > 32) { label = "…" + label.slice(-31); }
      var suffix = suffixFn ? suffixFn(row) : "";
      svg.appendChild(svgEl("text", { x: pad, y: y + 16, "font-size": 12, fill: "#8b95a8" }, label));
      svg.appendChild(svgEl("rect", { x: labelW, y: y + 3, width: barW, height: rowH - 9, rx: 3, fill: color }));
      svg.appendChild(svgEl("text", { x: labelW + barW + 6, y: y + 16, "font-size": 12, fill: "#e8ebf2" }, fmtShort(row[valueKey]) + suffix));
    }
    container.appendChild(svg);
  }

  function renderTimeline(container, points) {
    if (!container) { return; }
    if (!points || !points.length) { container.textContent = "No data."; return; }
    var W = 900;
    var H = 260;
    var padL = 54;
    var padR = 14;
    var padT = 14;
    var padB = 30;
    var innerW = W - padL - padR;
    var innerH = H - padT - padB;
    var max = 1;
    for (var i = 0; i < points.length; i++) {
      if (points[i][1] > max) { max = points[i][1]; }
      if (points[i][2] > max) { max = points[i][2]; }
    }
    var n = points.length;
    function px(i) { return padL + (n === 1 ? innerW / 2 : (innerW * i) / (n - 1)); }
    function py(v) { return padT + innerH * (1 - v / max); }
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
    var levels = [0, 0.5, 1];
    for (var g = 0; g < levels.length; g++) {
      var gy = py(max * levels[g]);
      svg.appendChild(svgEl("line", { x1: padL, y1: gy, x2: W - padR, y2: gy, stroke: "#263141", "stroke-width": 1 }));
      svg.appendChild(svgEl("text", { x: padL - 6, y: gy + 4, "font-size": 11, fill: "#8b95a8", "text-anchor": "end" }, fmtShort(Math.round(max * levels[g]))));
    }
    var ticks = n === 1 ? [0] : [0, Math.floor((n - 1) / 2), n - 1];
    for (var t = 0; t < ticks.length; t++) {
      if (t > 0 && ticks[t] === ticks[t - 1]) { continue; }
      svg.appendChild(svgEl("text", { x: px(ticks[t]), y: H - 8, "font-size": 11, fill: "#8b95a8", "text-anchor": "middle" }, points[ticks[t]][0]));
    }
    function series(idx, color) {
      var parts = [];
      for (var p = 0; p < n; p++) { parts.push(px(p) + "," + py(points[p][idx])); }
      svg.appendChild(svgEl("polyline", { points: parts.join(" "), fill: "none", stroke: color, "stroke-width": 1.5 }));
    }
    series(1, "#3ddc97");
    series(2, "#ff6b6b");
    svg.appendChild(svgEl("rect", { x: W - 170, y: 6, width: 10, height: 10, rx: 2, fill: "#3ddc97" }));
    svg.appendChild(svgEl("text", { x: W - 155, y: 15, "font-size": 11, fill: "#8b95a8" }, "added"));
    svg.appendChild(svgEl("rect", { x: W - 95, y: 6, width: 10, height: 10, rx: 2, fill: "#ff6b6b" }));
    svg.appendChild(svgEl("text", { x: W - 80, y: 15, "font-size": 11, fill: "#8b95a8" }, "removed"));
    container.appendChild(svg);
  }

  function renderTreemap(container, rows) {
    if (!container) { return; }
    if (!rows || !rows.length) { container.textContent = "No data."; return; }
    var total = 0;
    for (var i = 0; i < rows.length; i++) { total += rows[i].churn; }
    if (!total) { container.textContent = "No data."; return; }
    var palette = ["#5b9dff", "#3ddc97", "#ff6b6b", "#f7b32b", "#b980ff", "#4dd0e1", "#f06292", "#9ccc65"];
    var W = 900;
    var H = 280;
    var rects = [];
    function split(items, x, y, w, h, vertical) {
      if (!items.length) { return; }
      if (items.length === 1) { rects.push({ row: items[0], x: x, y: y, w: w, h: h }); return; }
      var sum = 0;
      for (var i = 0; i < items.length; i++) { sum += items[i].churn; }
      var acc = 0;
      var cut = items.length - 1;
      for (var j = 0; j < items.length - 1; j++) {
        acc += items[j].churn;
        if (acc >= sum / 2) { cut = j + 1; break; }
      }
      var groupA = items.slice(0, cut);
      var frac = 0;
      for (var a = 0; a < groupA.length; a++) { frac += groupA[a].churn; }
      frac = sum > 0 ? frac / sum : 0.5;
      if (vertical) {
        split(groupA, x, y, w * frac, h, false);
        split(items.slice(cut), x + w * frac, y, w * (1 - frac), h, false);
      } else {
        split(groupA, x, y, w, h * frac, true);
        split(items.slice(cut), x, y + h * frac, w, h * (1 - frac), true);
      }
    }
    split(rows, 0, 0, W, H, true);
    var svg = svgEl("svg", { viewBox: "0 0 " + W + " " + H });
    for (var r = 0; r < rects.length; r++) {
      var rect = rects[r];
      var other = rect.row.path.indexOf("(other") === 0;
      var fill = other ? "#3a4657" : palette[r % palette.length];
      var textFill = other ? "#c7cfdd" : "#0f141c";
      var g = svgEl("g", {});
      var box = svgEl("rect", {
        x: rect.x, y: rect.y, width: Math.max(0, rect.w - 2), height: Math.max(0, rect.h - 2),
        rx: 3, fill: fill, "fill-opacity": 0.85, stroke: "#0f141c", "stroke-width": 1
      });
      var share = ((100 * rect.row.churn) / total).toFixed(1);
      box.appendChild(svgEl("title", {}, rect.row.path + " — " + fmtShort(rect.row.churn) + " churn (" + share + "%)"));
      g.appendChild(box);
      if (rect.w > 64 && rect.h > 22) {
        var label = rect.row.path;
        var maxChars = Math.floor((rect.w - 12) / 6.5);
        if (label.length > maxChars) { label = label.slice(0, Math.max(1, maxChars - 1)) + "…"; }
        g.appendChild(svgEl("text", { x: rect.x + 6, y: rect.y + 15, "font-size": 11, fill: textFill }, label));
        if (rect.h > 36) {
          g.appendChild(svgEl("text", { x: rect.x + 6, y: rect.y + 29, "font-size": 10, fill: textFill, opacity: 0.75 }, fmtShort(rect.row.churn) + " (" + share + "%)"));
        }
      }
      svg.appendChild(g);
    }
    container.appendChild(svg);
  }

  function renderCharts() {
    var el = document.getElementById("chart-data");
    if (!el) { return; }
    var data;
    try { data = JSON.parse(el.textContent); } catch (e) { return; }
    renderBarChart(document.getElementById("chart-files"), data.files, "path", "churn", "#5b9dff", null);
    renderBarChart(
      document.getElementById("chart-authors"), data.authors, "name", "churn", "#3ddc97",
      function (row) { return " (" + (row.ownership * 100).toFixed(1) + "%)"; }
    );
    renderTimeline(document.getElementById("chart-timeline"), data.timeline);
    renderTreemap(document.getElementById("chart-treemap"), data.dirs);
  }

  function csvCell(value) {
    if (/[",\r\n]/.test(value)) {
      return '"' + value.replace(/"/g, '""') + '"';
    }
    return value;
  }

  function tableToCsv(table) {
    var lines = [];
    var rows = table.querySelectorAll("tr");
    for (var i = 0; i < rows.length; i++) {
      var cells = rows[i].querySelectorAll("th, td");
      if (cells.length === 1 && cells[0].hasAttribute("colspan")) { continue; }
      var values = [];
      for (var j = 0; j < cells.length; j++) {
        values.push(csvCell(cells[j].textContent.trim()));
      }
      lines.push(values.join(","));
    }
    return lines.join("\r\n");
  }

  function initTableExport() {
    var tables = document.querySelectorAll("table[data-sortable]");
    for (var i = 0; i < tables.length; i++) {
      (function (table) {
        var button = document.createElement("button");
        button.type = "button";
        button.className = "table-export";
        button.textContent = "Export CSV";
        button.addEventListener("click", function () {
          var blob = new Blob([tableToCsv(table)], { type: "text/csv" });
          var url = URL.createObjectURL(blob);
          var link = document.createElement("a");
          link.href = url;
          link.download = (table.id || "table") + ".csv";
          document.body.appendChild(link);
          link.click();
          document.body.removeChild(link);
          URL.revokeObjectURL(url);
        });
        table.parentNode.insertBefore(button, table);
      })(tables[i]);
    }
  }

  document.addEventListener("DOMContentLoaded", function () {
    var zipForm = document.getElementById("zip-form");
    if (zipForm) {
      zipForm.addEventListener("submit", function (event) {
        event.preventDefault();
        postForm(zipForm);
      });
    }
    var urlForm = document.getElementById("url-form");
    if (urlForm) {
      urlForm.addEventListener("submit", function (event) {
        event.preventDefault();
        postForm(urlForm);
      });
    }
    pollJobs();
    initSortableTables();
    initTableSearch();
    initTableExport();
    loadRepoKpis();
    renderCharts();
  });
})();
