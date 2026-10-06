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
    loadRepoKpis();
  });
})();
