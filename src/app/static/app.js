// SyncShare: bwSync&Share-Konto verbinden (Nextcloud Login Flow v2) und trennen.
// Spricht nur mit dem Gateway (/connect/bwsync…, gleiche Herkunft). App-Passwort und Poll-Token bleiben im
// Adapter; diese Seite sieht nur login_url (Anmeldeseite von bwSync&Share) und den Zustand.
(function () {
  "use strict";

  var POLL_MS = 3000;

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
      connect.disabled = true;
      // Tab sofort im Klick öffnen (sonst greift der Popup-Blocker), Adresse folgt nach dem Start.
      var tab = window.open("", "_blank");
      if (tab) tab.opener = null; // die Anmeldeseite bekommt keinen Zugriff auf diese Seite
      say(status, "Die Anmeldung bei bwSync&Share wird vorbereitet …");
      var started;
      try {
        started = await postJson(connect.dataset.startUrl);
      } catch (e) {
        started = { ok: false, status: 0, body: {} };
      }
      if (!started.ok || typeof started.body.login_url !== "string" || started.body.login_url.indexOf("https://") !== 0) {
        if (tab) tab.close();
        return stop(problem(started));
      }
      var loginUrl = started.body.login_url;
      if (tab) {
        tab.location.href = loginUrl;
        say(status, "Bitte melden Sie sich im neuen Tab bei bwSync&Share an und erlauben Sie den Zugriff. " +
          "Diese Seite wartet darauf und zeigt danach Ihre Dateien.");
      } else {
        say(status, "Bitte öffnen Sie die Anmeldeseite von bwSync&Share, melden Sie sich an und erlauben Sie den " +
          "Zugriff: ");
        var a = document.createElement("a");
        a.href = loginUrl;
        a.target = "_blank";
        a.rel = "noopener noreferrer";
        a.textContent = "Anmeldeseite öffnen";
        status.appendChild(a);
      }
      var until = Date.now() + 1000 * (Number(started.body.expires_in) || 1200);
      poll(until);
    });

    async function poll(until) {
      if (Date.now() > until) return stop("Die Zeit für die Anmeldung ist abgelaufen. Bitte verbinden Sie erneut.");
      await new Promise(function (resolve) { setTimeout(resolve, POLL_MS); });
      var result;
      try {
        result = await postJson(connect.dataset.pollUrl);
      } catch (e) {
        return poll(until); // kurz nicht erreichbar: weiter warten
      }
      if (!result.ok) return stop(problem(result));
      if (result.body.state === "linked") {
        say(status, "Verbunden. Ihre Dateien werden geladen …");
        window.location.assign(connect.dataset.doneUrl);
        return;
      }
      if (result.body.state === "pending") return poll(until);
      stop("Die Anmeldung ist abgelaufen oder wurde abgebrochen. Bitte verbinden Sie erneut.");
    }

    function stop(text) {
      waiting = false;
      connect.disabled = false;
      say(status, text);
    }
  }
})();
