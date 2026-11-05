// CampusBadge - small helpers: menu, confirmations, validation, table search, QR scanning.
document.addEventListener("DOMContentLoaded", function () {
  var sidebar = document.getElementById("sidebar");
  var toggle = document.getElementById("menu-toggle");
  if (toggle && sidebar) toggle.addEventListener("click", function () { sidebar.classList.toggle("open"); });

  // Confirmation dialog before risky actions (forms with data-confirm="...")
  document.querySelectorAll("form[data-confirm]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (!window.confirm(form.dataset.confirm)) { e.preventDefault(); e.stopImmediatePropagation(); }
    });
  });

  // Client-side validation (the server always validates again)
  document.querySelectorAll("form[data-validate]").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      var problem = "";
      form.querySelectorAll("[required]").forEach(function (field) {
        var empty = !field.value.trim();
        field.classList.toggle("invalid", empty);
        if (empty && !problem) problem = "Please fill in all required fields.";
      });
      var pw = form.querySelector("[name=password]"), cf = form.querySelector("[name=confirm_password]");
      if (pw && cf && pw.value !== cf.value && !problem) problem = "Passwords do not match.";
      if (pw && pw.value && pw.hasAttribute("minlength") && pw.value.length < 8 && !problem) problem = "Password must be at least 8 characters.";
      if (problem) { e.preventDefault(); e.stopImmediatePropagation(); window.alert(problem); }
    });
  });

  // Prevent double submits (disable the button after the first click)
  document.querySelectorAll("form:not(#scan-form):not(#sim-form)").forEach(function (form) {
    form.addEventListener("submit", function (e) {
      if (e.defaultPrevented) return;
      form.querySelectorAll("button:not([type=button])").forEach(function (b) { setTimeout(function () { b.disabled = true; }, 0); });
    });
  });

  // Live table search: <input data-filter="table-id">
  document.querySelectorAll("input[data-filter]").forEach(function (box) {
    box.addEventListener("input", function () {
      var q = box.value.toLowerCase();
      document.querySelectorAll("#" + box.dataset.filter + " tbody tr").forEach(function (row) {
        row.style.display = row.textContent.toLowerCase().indexOf(q) === -1 ? "none" : "";
      });
    });
  });

  document.querySelectorAll("[data-print]").forEach(function (b) { b.addEventListener("click", function () { window.print(); }); });

  // ---- QR attendance page
  var scanForm = document.getElementById("scan-form");
  if (!scanForm) return;
  var resultBox = document.getElementById("scan-result");
  var actionInput = document.getElementById("scan-action");
  var simAction = document.getElementById("sim-action");
  var busy = false;

  document.querySelectorAll("input[name=mode]").forEach(function (radio) {
    radio.addEventListener("change", function () {
      actionInput.value = radio.value; if (simAction) simAction.value = radio.value;
    });
  });

  function showResult(ok, message) {
    resultBox.textContent = message;
    resultBox.className = "alert " + (ok ? "alert-success" : "alert-error");
  }

  function send(form) {
    if (busy) return;                       // blocks repeated submissions
    busy = true;
    var button = form.querySelector("button:not([type=button])");
    if (button) button.disabled = true;
    fetch(form.action, { method: "POST", body: new FormData(form),
                         headers: { "X-Requested-With": "fetch", "X-CSRF-Token": form.querySelector("[name=_csrf]").value } })
      .then(function (r) { return r.json(); })
      .then(function (data) {
        showResult(data.ok, data.message);
        if (data.ok) setTimeout(function () { window.location.reload(); }, 2500);
      })
      .catch(function () { showResult(false, "Could not reach the server. Please try again."); })
      .finally(function () { busy = false; if (button) button.disabled = false; });
  }

  [scanForm, document.getElementById("sim-form")].forEach(function (form) {
    if (form) form.addEventListener("submit", function (e) { e.preventDefault(); send(form); });
  });

  // Camera scanning (html5-qrcode library). Falls back gracefully if unavailable.
  var startBtn = document.getElementById("scanner-start");
  var note = document.getElementById("camera-note");
  startBtn.addEventListener("click", function () {
    if (typeof Html5Qrcode === "undefined") {
      note.textContent = "The camera scanner could not load (no internet?). Paste the QR code text or use testing mode instead.";
      return;
    }
    var scanner = new Html5Qrcode("reader");
    startBtn.disabled = true;
    scanner.start({ facingMode: "environment" }, { fps: 10, qrbox: 240 }, function (text) {
      if (busy) return;
      document.getElementById("qr_code").value = text;
      scanner.stop().catch(function () {});
      startBtn.disabled = false;
      send(scanForm);
    }, function () {}).catch(function () {
      startBtn.disabled = false;
      note.textContent = "Camera not available or permission denied. Paste the QR code text or use testing mode.";
    });
  });
});
