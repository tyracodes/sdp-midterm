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
  });
})();
