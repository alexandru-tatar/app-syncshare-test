// SyncShare: bwSync&Share-Konto verbinden und trennen (Ablauf: window.PortalBwSync aus dem Portal-SDK,
// /_sdk/bwsync/connect.js) und Umbenennen in der Tabelle. Texte und Knöpfe gehören der App.
(function () {
  "use strict";

  var sdk = window.PortalBwSync;
  var WAITING = "Bitte melden Sie sich im neuen Tab bei bwSync&Share an und erlauben Sie den Zugriff. " +
    "Diese Seite wartet darauf und zeigt danach Ihre Dateien.";
  var UNAVAILABLE = "bwSync&Share ist gerade nicht erreichbar. Bitte versuchen Sie es später erneut.";
  // Text nach type (Tabellen in platform_sdk/adapters/bwsync/README.md), nie nach dem Text des Servers
  var TEXTS = {
    "connect-popup-blocked": "Der neue Tab wurde blockiert. Bitte erlauben Sie Pop-ups für diese Seite.",
    "connect-expired": "Die Anmeldung ist abgelaufen. Bitte verbinden Sie erneut.",
    "connect-aborted": "Die Anmeldung wurde abgebrochen oder ist abgelaufen. Bitte verbinden Sie erneut.",
    "portal-session-expired": "Ihre Anmeldung am Portal ist abgelaufen. Bitte laden Sie die Seite neu.",
    "bwsync-forbidden": "Dafür fehlt Ihnen in bwSync&Share die Berechtigung.",
    "bwsync-link-failed": "bwSync&Share hat die Verbindung nicht angenommen. Bitte verbinden Sie erneut.",
  };

  // Hinweis (grau) oder Fehler (wie ein Feldfehler, deutlich sichtbar)
  function say(el, text, isError) {
    if (!el) return;
    el.textContent = text;
    el.classList.toggle("muted", !isError);
    el.classList.toggle("field__error", Boolean(isError));
  }

  // Erfolgsmeldung nach der Umleitung (?ok=…) nur einmal: nicht in der Adresse lassen (sonst beim Neuladen wieder)
  // und entfernen, sobald eine neue Aktion beginnt (sonst steht sie neben einem neuen Fehler).
  if (window.history && window.history.replaceState && /[?&]ok=/.test(window.location.search)) {
    var url = new URL(window.location.href);
    url.searchParams.delete("ok");
    window.history.replaceState(null, "", url.pathname + url.search + url.hash);
  }

  function clearDone() {
    document.querySelectorAll("[data-done]").forEach(function (el) {
      el.remove();
    });
  }

  function problem(error) {
    return (error && TEXTS[error.type]) || UNAVAILABLE;
  }

  // --- Verbinden ---------------------------------------------------------------------------------------------
  var connect = document.querySelector("[data-bwsync-connect]");
  if (connect && sdk) {
    var status = document.getElementById("verbinden-status");
    var waiting = false;

    connect.addEventListener("click", function () {
      if (waiting) return;
      waiting = true;
      clearDone();
      // Sofort: Button aus, der einzige Hinweis da. Er ändert sich danach nicht mehr, nur ein Fehler ersetzt ihn.
      connect.disabled = true;
      sdk.connect(function (state) {
        if (state.state === "waiting") return say(status, WAITING);
        if (state.state === "linked") return window.location.assign(connect.dataset.doneUrl);
        stop(problem(state));
      });
    });

    function stop(text) {
      waiting = false;
      connect.disabled = false;
      say(status, text, true);
    }
  }

  // --- Trennen -----------------------------------------------------------------------------------------------
  // kit.js öffnet den Dialog; nach „Konto trennen“ (returnValue "confirm") trennt das SDK-Skript.
  var dialog = document.querySelector("[data-bwsync-disconnect]");
  if (dialog && sdk) {
    dialog.addEventListener("close", async function () {
      if (dialog.returnValue !== "confirm") return;
      clearDone();
      var status = document.getElementById("trennen-status");
      say(status, "Das Konto wird getrennt …");
      var result;
      try {
        result = await sdk.disconnect();
      } catch (error) {
        return say(status, problem(error), true);
      }
      // revoked: true widerrufen, null von Hand angelegt (bleibt), false nicht bestätigt
      var next = result.revoked === true ? dialog.dataset.doneRevoked
        : result.revoked === null ? dialog.dataset.doneKept : dialog.dataset.doneOpen;
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

  // --- Löschen: Bestätigung wie beim Trennen -----------------------------------------------------------------------
  // Klick auf „Löschen“: Titel, Name und Pfad aus der Zeile in den Dialog, dann öffnen. „Löschen“ im Dialog: kit.js
  // schickt #loeschen-form ab (POST /loeschen). Ohne JavaScript führt der Link auf die Seite /loeschen.
  var deleteDialog = document.getElementById("loeschen-dialog");
  var deleteForm = document.getElementById("loeschen-form");
  if (table && deleteDialog && deleteForm && deleteDialog.showModal) {
    table.addEventListener("click", function (e) {
      var trigger = e.target.closest && e.target.closest("[data-delete]");
      if (!trigger) return;
      e.preventDefault();
      var row = trigger.closest("tr");
      document.getElementById("loeschen-titel").textContent =
        row.dataset.art === "ordner" ? "Ordner löschen?" : "Datei löschen?";
      document.getElementById("loeschen-name").textContent = row.dataset.name;
      deleteForm.elements.pfad.value = row.dataset.pfad;
      deleteForm.elements.art.value = row.dataset.art;
      deleteDialog.returnValue = "";
      deleteDialog.showModal();
    });
  }
})();
