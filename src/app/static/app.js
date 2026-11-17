// SyncShare: bwSync&Share-Konto verbinden (Nextcloud Login Flow v2) und trennen.
// Spricht nur mit dem Gateway (/connect/bwsync…, gleiche Herkunft). App-Passwort und Poll-Token bleiben im
// Adapter; diese Seite sieht nur login_url (Anmeldeseite von bwSync&Share) und den Zustand.
(function () {
  "use strict";

  var POLL_MS = 3000;
  var WAITING = "Bitte melden Sie sich im neuen Tab bei bwSync&Share an und erlauben Sie den Zugriff. " +
    "Diese Seite wartet darauf und zeigt danach Ihre Dateien.";

  function say(el, text) {
    if (el) el.textContent = text;
  }

  async function postJson(url, method) {
    var response = await fetch(url, {
      method: method || "POST",
      credentials: "same-origin",
      headers: { Accept: "application/json" },
    });
    var body = {};
    try {
      body = await response.json();
    } catch (e) {
      body = {};
    }
    return { ok: response.ok, status: response.status, body: body };
  }

  function problem(result) {
    if (result.status === 401) return "Ihre Anmeldung am Portal ist abgelaufen. Bitte laden Sie die Seite neu.";
    if (typeof result.body.detail === "string" && result.body.type) return result.body.detail;
    return "bwSync&Share ist gerade nicht erreichbar. Bitte versuchen Sie es später erneut.";
  }

  // --- Verbinden ---------------------------------------------------------------------------------------------
  var connect = document.querySelector("[data-bwsync-connect]");
  if (connect) {
    var status = document.getElementById("verbinden-status");
    var waiting = false;

    connect.addEventListener("click", async function () {
      if (waiting) return;
      waiting = true;
      // Sofort: Button aus, der einzige Hinweis da. Er ändert sich danach nicht mehr, nur ein Fehler ersetzt ihn.
      connect.disabled = true;
      say(status, WAITING);
      // Tab sofort im Klick öffnen (sonst greift der Popup-Blocker), Adresse folgt nach dem Start.
      var tab = window.open("", "_blank");
      if (!tab) return stop("Der neue Tab wurde blockiert. Bitte erlauben Sie Pop-ups für diese Seite.");
      tab.opener = null; // die Anmeldeseite bekommt keinen Zugriff auf diese Seite
      var started;
      try {
        started = await postJson(connect.dataset.startUrl);
      } catch (e) {
        started = { ok: false, status: 0, body: {} };
      }
      if (!started.ok || typeof started.body.login_url !== "string" || started.body.login_url.indexOf("https://") !== 0) {
        tab.close();
        return stop(problem(started));
      }
      tab.location.href = started.body.login_url;
      var until = Date.now() + 1000 * (Number(started.body.expires_in) || 1200);
      poll(until);
    });

    async function poll(until) {
      if (Date.now() > until) return stop("Die Anmeldung ist abgelaufen. Bitte verbinden Sie erneut.");
      await new Promise(function (resolve) { setTimeout(resolve, POLL_MS); });
      var result;
      try {
        result = await postJson(connect.dataset.pollUrl);
      } catch (e) {
        return poll(until); // kurz nicht erreichbar: weiter warten
      }
      if (!result.ok) return stop(problem(result));
      if (result.body.state === "linked") return window.location.assign(connect.dataset.doneUrl);
      if (result.body.state === "pending") return poll(until);
      stop("Die Anmeldung wurde abgebrochen oder ist abgelaufen. Bitte verbinden Sie erneut.");
    }

    function stop(text) {
      waiting = false;
      connect.disabled = false;
      say(status, text);
    }
  }

  // --- Trennen -----------------------------------------------------------------------------------------------
  // kit.js öffnet den Dialog; nach „Konto trennen“ (returnValue "confirm") hier DELETE ans Gateway.
  var dialog = document.querySelector("[data-bwsync-disconnect]");
  if (dialog) {
    dialog.addEventListener("close", async function () {
      if (dialog.returnValue !== "confirm") return;
      var status = document.getElementById("trennen-status");
      say(status, "Das Konto wird getrennt …");
      var result;
      try {
        result = await postJson(dialog.dataset.url, "DELETE");
      } catch (e) {
        result = { ok: false, status: 0, body: {} };
      }
      if (!result.ok) return say(status, problem(result));
      // revoked: true widerrufen, null von Hand angelegt (bleibt), false nicht bestätigt
      var revoked = result.body.revoked;
      var next = revoked === true ? dialog.dataset.doneRevoked
        : revoked === null ? dialog.dataset.doneKept : dialog.dataset.doneOpen;
      window.location.assign(next);
    });
  }

  // --- Umbenennen direkt in der Tabelle ------------------------------------------------------------------------
  // Klick auf „Umbenennen“: Name wird zum Eingabefeld (bei Dateien ist nur der Teil vor der Endung markiert).
  // Enter oder Fokus weg = speichern, Esc = abbrechen. Fehler stehen an der Zeile, der Name bleibt der alte.
  // Ohne JavaScript führt der Link auf die Ausweichseite /umbenennen.
  var table = document.querySelector("[data-rename-url]");
  if (table) {
    table.addEventListener("click", function (e) {
      var trigger = e.target.closest && e.target.closest("[data-rename]");
      if (!trigger) return;
      e.preventDefault();
      startRename(trigger.closest("tr"), trigger);
    });
  }

  function startRename(row, trigger) {
    var cell = row && row.querySelector("[data-name-cell]");
    if (!cell || cell.querySelector(".rename-input")) return;
    var oldName = row.dataset.name;
    var oldError = row.querySelector(".rename-error");
    if (oldError) oldError.remove();
    var original = Array.prototype.slice.call(cell.childNodes);
    var input = document.createElement("input");
    input.type = "text";
    input.className = "rename-input";
    input.value = oldName;
    input.maxLength = 255;
    input.setAttribute("aria-label", "Neuer Name für " + oldName);
    cell.replaceChildren(input);
    input.focus();
    var dot = oldName.lastIndexOf(".");
    if (row.dataset.art === "datei" && dot > 0) input.setSelectionRange(0, dot);
    else input.select();
    var finished = false;

    function restore() {
      cell.replaceChildren.apply(cell, original);
    }

    function cancel() {
      if (finished) return;
      finished = true;
      restore();
      trigger.focus();
    }

    async function save() {
      if (finished) return;
      finished = true;
      var name = input.value.trim();
      if (!name || name === oldName) return restore();
      input.readOnly = true;
      var form = new FormData();
      form.append("pfad", row.dataset.pfad);
      form.append("art", row.dataset.art);
      form.append("name", name);
      var body = {};
      var ok = false;
      try {
        var response = await fetch(table.dataset.renameUrl, {
          method: "POST",
          credentials: "same-origin",
          headers: { Accept: "application/json" },
          body: form,
        });
        ok = response.ok;
        body = await response.json();
      } catch (e) {
        body = body || {};
      }
      if (ok && typeof body.url === "string") return window.location.assign(body.url);
      restore();
      var error = document.createElement("p");
      error.className = "field__error rename-error";
      error.setAttribute("role", "alert");
      error.textContent = typeof body.error === "string" ? body.error
        : "Das Umbenennen hat nicht geklappt. Bitte versuchen Sie es erneut.";
      cell.appendChild(error);
      trigger.focus();
    }

    input.addEventListener("keydown", function (e) {
      if (e.key === "Enter") {
        e.preventDefault();
        save();
      } else if (e.key === "Escape") {
        e.preventDefault();
        cancel();
      }
    });
    input.addEventListener("blur", save);
  }
})();
