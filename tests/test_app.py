import re
from html import unescape
from pathlib import Path

import pytest
from conftest import make_token

from app.main import portal
from platform_sdk.adapters import transport
from platform_sdk.adapters.bwsync import errors as bwsync_errors


def test_healthz_ohne_anmeldung(client):
    assert client.get("/healthz").status_code == 200


def test_ohne_anmeldung_401(client):
    response = client.get("/", headers={"Accept": "text/html"})
    assert response.status_code == 401
    assert "Anmeldung fehlt" in response.text
    assert "set-cookie" not in response.headers


def test_ungueltige_anmeldung_401(client):
    response = client.get("/", headers={"X-Portal-Assertion": make_token(aud=["andere-app"])})
    assert response.status_code == 401
    assert response.headers["content-type"] == "application/problem+json"


def test_keine_api_doku(client, as_student):
    for path in ("/docs", "/redoc", "/openapi.json"):
        assert client.get(path, headers=as_student).status_code == 404


def test_startseite(client, as_student):
    response = client.get("/", headers=as_student)
    assert response.status_code == 200
    html = response.text
    assert '<html lang="de">' in html
    assert 'href="#main"' in html
    assert html.count("<h1") == 1
    assert "set-cookie" not in response.headers


def seite(client, headers, pfad="", **params):
    return client.get(portal.url("/"), headers=headers, params={"pfad": pfad, **params} if pfad or params else None)


def senden(client, headers, route, **form):
    return client.post(portal.url(route), headers=headers, data=form, follow_redirects=False)


def hochladen(client, headers, pfad="", name="notiz.txt", inhalt=b"Hallo Welt"):
    return client.post(
        portal.url("/upload"),
        headers=headers,
        data={"pfad": pfad},
        files={"datei": (name, inhalt)},
        follow_redirects=False,
    )


def links(html):
    return [unescape(h) for h in re.findall(r'href="([^"]+)"', html)]


def aenderungen(backend):
    return [(method, path) for method, path, _ in backend.calls if method != "GET"]


@pytest.fixture
def konto(adapter_backend):
    adapter_backend.folders |= {"Studium", "Studium/Semester 5", "Ärger"}
    adapter_backend.files.update({"zeta.txt": b"z" * 1536, "Studium/plan.pdf": b"pdf"})
    return adapter_backend


def test_wurzel_zeigt_ordner_und_dateien(client, as_student, konto):
    antwort = seite(client, as_student)
    assert antwort.status_code == 200
    html = antwort.text
    assert html.count("<h1") == 1 and "Mein bwSync&amp;Share" in html
    tabelle = html[html.index("<tbody>") :]
    assert tabelle.index("Studium") < tabelle.index("zeta.txt") and tabelle.index("Ärger") < tabelle.index("zeta.txt")
    assert "1,5 KB" in html and "16.11.2026" in html
    assert portal.url("/?pfad=Studium") in links(html)
    assert portal.url("/datei?pfad=zeta.txt") in links(html)
    assert konto.calls[0][:2] == ("GET", "/bwsync/folders/")


def test_jede_anfrage_mit_der_eigenen_anmeldung(client, konto, adapter_http):
    konto.uninstall()  # hier der echte Weg des SDK (HTTP), damit die mitgeschickte Anmeldung sichtbar ist
    token = make_token(sub="andere-person")
    seite(client, {"X-Portal-Assertion": token, "Accept": "text/html"})
    assert adapter_http[-1].headers["authorization"] == f"Bearer {token}"
    assert adapter_http[-1].url.raw_path == b"/bwsync/folders/"


def test_navigieren_mit_brotkrumen(client, as_student, konto):
    html = seite(client, as_student, "Studium/Semester 5").text
    assert konto.calls[-1][1] == "/bwsync/folders/Studium/Semester 5"
    nav = html[html.index('class="crumbs"') : html.index("</ol>", html.index('class="crumbs"'))]
    assert '<ol aria-label="Ordnerpfad">' in nav
    assert html.count("<nav") == html.count('aria-label="Hauptnavigation"')  # nur das Menü des Portals
    assert portal.url("/") in links(nav) and portal.url("/?pfad=Studium") in links(nav)
    assert 'aria-current="page">Semester 5<' in nav
    assert "Dieser Ordner ist leer." in html and "Eine Ebene höher" not in html  # die Pfadleiste reicht


def test_formularfelder_mit_sichtbarem_label_und_pflichtfeld(client, as_student, konto):
    html = seite(client, as_student).text
    pflicht = '<span class="field__required">(Pflichtfeld)</span>'
    assert f'<label for="name">Name des Ordners {pflicht}</label>' in html
    assert f'<label for="datei">Datei {pflicht}</label>' in html
    assert 'class="visually-hidden">Name' not in html and 'class="visually-hidden">Datei' not in html
    assert '<caption class="visually-hidden">Inhalt von Mein bwSync&amp;Share</caption>' in html
    assert re.search(r'<input type="text" id="name"[^>]*aria-describedby="name-hinweis"\s+required>', html)
    assert re.search(r'<input type="file" id="datei"[^>]*aria-describedby="datei-hinweis"\s+required>', html)
    assert "aria-invalid" not in html and "-fehler" not in html  # nur im Fehlerfall


def test_feldfehler_ist_mit_dem_feld_verknuepft(client, as_student, konto):
    html = senden(client, as_student, "/ordner", pfad="", name="a:b").text
    feld = re.search(r'<input type="text" id="name"[^>]*>', html).group(0)
    assert 'aria-describedby="name-hinweis name-fehler"' in feld and 'aria-invalid="true"' in feld
    assert '<p class="field__error" id="name-fehler">' in html
    html = hochladen(client, as_student, name="a:b.txt").text
    feld = re.search(r'<input type="file" id="datei"[^>]*>', html).group(0)
    assert 'aria-describedby="datei-hinweis datei-fehler"' in feld and 'aria-invalid="true"' in feld
    assert '<p class="field__error" id="datei-fehler">' in html
    html = senden(client, as_student, "/umbenennen", pfad="zeta.txt", art="datei", name="zeta.txt").text
    feld = re.search(r'<input type="text" id="name"[^>]*>', html).group(0)
    assert 'aria-describedby="name-hinweis name-fehler"' in feld and 'aria-invalid="true"' in feld


@pytest.mark.parametrize("pfad", ["..", "a/../b", "/etc", "a//b", "a:b", "x%2fy", "a\\b"])
def test_ungueltiger_pfad_ohne_adapteraufruf(client, as_student, konto, pfad):
    antwort = seite(client, as_student, pfad)
    assert antwort.status_code == 400 and "Diesen Ordner kann SyncShare nicht öffnen." in antwort.text
    assert konto.calls == []


def test_unbekannter_ordner(client, as_student, konto):
    antwort = seite(client, as_student, "Gibt es nicht")
    assert antwort.status_code == 404 and "Das gibt es nicht (mehr)" in antwort.text
    assert "Zur obersten Ebene" in antwort.text


def test_ordner_anlegen(client, as_student, konto):
    antwort = senden(client, as_student, "/ordner", pfad="Studium", name="  Neu ä ")
    assert antwort.status_code == 303
    assert antwort.headers["location"] == portal.url("/?pfad=Studium&ok=ordner")
    assert "Studium/Neu ä" in konto.folders
    html = client.get(antwort.headers["location"], headers=as_student).text
    assert "Der Ordner wurde angelegt." in html and "Neu ä" in html


def test_ordner_gibt_es_schon(client, as_student, konto):
    antwort = senden(client, as_student, "/ordner", pfad="", name="Studium")
    assert antwort.status_code == 409 and "gibt es schon einen Ordner oder eine Datei" in antwort.text
    assert 'value="Studium"' in antwort.text
    # Nur einmal: unter dem Feld, kein Fehlerkasten oben mit derselben Meldung
    assert antwort.text.count("gibt es schon einen Ordner oder eine Datei") == 1
    assert 'class="field__error"' in antwort.text and 'class="error-summary"' not in antwort.text


@pytest.mark.parametrize("name", ["", "a/b", "x:y", "..", "50%", "a?b", "x" * 256])
def test_ungueltiger_ordnername(client, as_student, konto, name):
    antwort = senden(client, as_student, "/ordner", pfad="", name=name)
    assert antwort.status_code == 400 and "höchstens 255 Zeichen" in antwort.text
    assert aenderungen(konto) == []


def test_hochladen_in_den_aktuellen_ordner_und_herunterladen(client, as_student, konto):
    antwort = hochladen(client, as_student, pfad="Studium/Semester 5", name="notiz ä.txt")
    assert antwort.status_code == 303 and antwort.headers["location"].endswith("ok=hochgeladen")
    assert konto.files["Studium/Semester 5/notiz ä.txt"] == b"Hallo Welt"
    html = client.get(antwort.headers["location"], headers=as_student).text
    assert "Die Datei wurde hochgeladen." in html
    download = client.get(portal.url("/datei"), headers=as_student, params={"pfad": "Studium/Semester 5/notiz ä.txt"})
    assert download.status_code == 200 and download.content == b"Hallo Welt"
    assert (
        download.headers["content-disposition"]
        == "attachment; filename=\"notiz a.txt\"; filename*=UTF-8''notiz%20%C3%A4.txt"
    )
    assert download.headers["x-content-type-options"] == "nosniff"
    assert download.headers["content-type"] == "application/octet-stream"


def test_hochladen_ueberschreibt_nie(client, as_student, konto):
    antwort = hochladen(client, as_student, name="zeta.txt")
    assert antwort.status_code == 412 and "gibt es schon" in antwort.text
    assert konto.files["zeta.txt"] == b"z" * 1536


def test_pfad_im_dateinamen_wird_entfernt(client, as_student, konto):
    antwort = hochladen(client, as_student, pfad="Studium", name="../../etc/passwd")
    assert antwort.status_code == 303 and "Studium/passwd" in konto.files


def test_dateiname_mit_doppelpunkt(client, as_student, konto):
    antwort = hochladen(client, as_student, name="a:b.txt")
    assert antwort.status_code == 400 and "Der Dateiname geht so nicht." in antwort.text
    assert aenderungen(konto) == []


def test_zu_grosse_datei_deutsche_meldung(client, as_student, konto):
    antwort = hochladen(client, as_student, inhalt=b"x" * (2 * 1024 * 1024 + 1))
    assert antwort.status_code == 400 and "größer als 2 MB" in antwort.text
    assert aenderungen(konto) == []


def test_ohne_datei_deutsche_meldung_statt_422(client, as_student, konto):
    antwort = client.post(portal.url("/upload"), headers=as_student)
    assert antwort.status_code == 400
    assert "Bitte wählen Sie eine Datei aus." in antwort.text


def test_ordner_als_zip_herunterladen(client, as_student, konto):
    import io
    import zipfile

    html = seite(client, as_student).text
    zeile = html[html.index('data-pfad="Studium"') : html.index("</tr>", html.index('data-pfad="Studium"'))]
    assert f'<a href="{portal.url("/zip?pfad=Studium")}">Herunterladen' in zeile and "als ZIP" in zeile
    antwort = client.get(portal.url("/zip"), headers=as_student, params={"pfad": "Studium"})
    assert antwort.status_code == 200 and antwort.headers["content-type"] == "application/zip"
    assert (
        antwort.headers["content-disposition"] == "attachment; filename=\"Studium.zip\"; filename*=UTF-8''Studium.zip"
    )
    assert antwort.headers["cache-control"] == "no-store"
    with zipfile.ZipFile(io.BytesIO(antwort.content)) as archiv:
        assert archiv.read("Studium/plan.pdf") == b"pdf"
    assert konto.calls[-1][1] == "/bwsync/zip/Studium"


def test_zip_dateiname_ohne_anfuehrungszeichen(client, as_student, konto):
    ordner = 'Ärger "x"'
    konto.folders.add(ordner)
    konto.files[ordner + "/a.txt"] = b"a"
    antwort = client.get(portal.url("/zip"), headers=as_student, params={"pfad": ordner})
    assert antwort.status_code == 200
    kopf = antwort.headers["content-disposition"]
    assert kopf == "attachment; filename=\"Arger x.zip\"; filename*=UTF-8''%C3%84rger%20x.zip"
    assert kopf.count('"') == 2  # nur die Anführungszeichen um den ASCII-Namen


@pytest.mark.parametrize(
    "name,erwartet",
    [
        ("plan.pdf", "attachment; filename=\"plan.pdf\"; filename*=UTF-8''plan.pdf"),
        ('a"b\\c\x07.txt', "attachment; filename=\"abc.txt\"; filename*=UTF-8''abc.txt"),
        ("日本.txt", "attachment; filename=\"download.txt\"; filename*=UTF-8''%E6%97%A5%E6%9C%AC.txt"),
        ('""', "attachment; filename=\"download\"; filename*=UTF-8''download"),
    ],
)
def test_attachment_kopf(name, erwartet):
    from platform_sdk.common.downloads import content_disposition as attachment

    assert attachment(name) == erwartet


@pytest.mark.parametrize("pfad,status", [("", 400), ("../x", 400), ("Fehlt", 404)])
def test_ordner_zip_fehler(client, as_student, konto, pfad, status):
    antwort = client.get(portal.url("/zip"), headers=as_student, params={"pfad": pfad})
    assert antwort.status_code == status and "text/html" in antwort.headers["content-type"]
    if status == 404:
        assert "Der Ordner konnte nicht heruntergeladen werden." in fehlerkasten(antwort.text)


def test_umbenennen_ausweichseite_ohne_zielordner(client, as_student, konto):
    formular = client.get(
        portal.url("/umbenennen"), headers=as_student, params={"pfad": "Studium/plan.pdf", "art": "datei"}
    )
    assert formular.status_code == 200 and 'value="plan.pdf"' in formular.text
    assert 'name="ziel"' not in formular.text and "verschieben" not in formular.text.lower()
    assert aenderungen(konto) == []
    antwort = senden(client, as_student, "/umbenennen", pfad="Studium/plan.pdf", art="datei", name="Plan 2026.pdf")
    assert antwort.status_code == 303 and antwort.headers["location"] == portal.url("/?pfad=Studium&ok=umbenannt")
    assert konto.files["Studium/Plan 2026.pdf"] == b"pdf" and "Studium/plan.pdf" not in konto.files
    move = [c for c in konto.calls if c[1] == "/bwsync/move"][0]
    assert move[2] == {"from": "Studium/plan.pdf", "to": "Studium/Plan 2026.pdf"}


def test_umbenennen_bleibt_im_selben_ordner(client, as_student, konto):
    # Ein altes Formular mit Zielordner verschiebt nicht: ziel wird ignoriert, der Elternordner bleibt.
    antwort = senden(client, as_student, "/umbenennen", pfad="Studium", art="ordner", name="Kurse", ziel="Ärger")
    assert antwort.status_code == 303 and antwort.headers["location"] == portal.url("/?ok=umbenannt")
    assert "Kurse/Semester 5" in konto.folders and "Kurse/plan.pdf" in konto.files
    assert not any(p.startswith("Ärger/") for p in konto.folders | set(konto.files))


def json_umbenennen(client, headers, **form):
    return client.post(
        portal.url("/umbenennen"), headers={**headers, "Accept": "application/json"}, data=form, follow_redirects=False
    )


def test_umbenennen_in_der_tabelle_antwortet_json(client, as_student, konto):
    antwort = json_umbenennen(client, as_student, pfad="zeta.txt", art="datei", name="omega.txt")
    assert antwort.status_code == 200 and antwort.json() == {"url": portal.url("/?ok=umbenannt")}
    assert antwort.headers["cache-control"] == "no-store"
    assert "omega.txt" in konto.files and "zeta.txt" not in konto.files
    html = client.get(antwort.json()["url"], headers=as_student).text
    assert "Umbenannt." in html


@pytest.mark.parametrize(
    "form,status,text",
    [
        ({"pfad": "Studium/plan.pdf", "art": "datei", "name": "plan.pdf"}, 400, "neuen Namen"),
        ({"pfad": "zeta.txt", "art": "datei", "name": "Studium/zeta.txt"}, 400, "kein „/“"),
        ({"pfad": "zeta.txt", "art": "datei", "name": "a:b"}, 400, "höchstens 255 Zeichen"),
        ({"pfad": "zeta.txt", "art": "datei", "name": "Studium"}, 409, "gibt es schon"),
        ({"pfad": "../zeta.txt", "art": "datei", "name": "x"}, 400, "nicht öffnen"),
    ],
)
def test_umbenennen_in_der_tabelle_fehler_als_json(client, as_student, konto, form, status, text):
    vorher = (set(konto.files), set(konto.folders))
    antwort = json_umbenennen(client, as_student, **form)
    assert antwort.status_code == status and text in antwort.json()["error"]
    assert (set(konto.files), set(konto.folders)) == vorher  # Name bleibt der alte


def test_umbenennen_gesperrte_datei(client, as_student, konto):
    konto.fail = (423, bwsync_errors.LOCKED)
    antwort = json_umbenennen(client, as_student, pfad="zeta.txt", art="datei", name="omega.txt")
    assert antwort.status_code == 423 and "gesperrt" in antwort.json()["error"]


@pytest.mark.parametrize(
    "form,text",
    [
        ({"name": "plan.pdf"}, "neuen Namen"),
        ({"name": "a/b"}, "kein „/“"),
        ({"name": "zeta.txt"}, None),  # gibt es im Ordner Studium nicht: erlaubt
    ],
)
def test_umbenennen_ausweichseite_fehler_am_feld(client, as_student, konto, form, text):
    antwort = senden(client, as_student, "/umbenennen", pfad="Studium/plan.pdf", art="datei", **form)
    if text is None:
        assert antwort.status_code == 303
        return
    assert antwort.status_code == 400 and antwort.text.count(text) == 1  # nur einmal: unter dem Feld
    assert 'class="field__error"' in antwort.text and 'class="error-summary"' not in antwort.text


def test_tabelle_hat_daten_fuer_umbenennen(client, as_student, konto):
    html = seite(client, as_student, "Studium").text
    assert f'<table class="table" data-rename-url="{portal.url("/umbenennen")}">' in html
    assert '<tr data-pfad="Studium/plan.pdf" data-art="datei" data-name="plan.pdf">' in html
    assert '<th scope="row" data-name-cell>plan.pdf</th>' in html
    assert re.search(r'<a href="[^"]*/umbenennen\?pfad=Studium%2Fplan.pdf&amp;art=datei" data-rename>Umbenennen', html)


def test_skript_umbenennen_markiert_name_ohne_endung(client, as_student):
    js = client.get(portal.url("/static/app.js"), headers=as_student).text
    teil = js[js.index("// --- Umbenennen") :]
    assert 'lastIndexOf(".")' in teil and "setSelectionRange(0, dot)" in teil
    assert '"Enter"' in teil and '"Escape"' in teil and 'addEventListener("blur", save)' in teil
    assert 'Accept: "application/json"' in teil and "textContent" in teil and "innerHTML" not in js


def test_zeile_hat_verschieben(client, as_student, konto):
    html = seite(client, as_student, "Studium").text
    assert (
        f'<a href="{portal.url("/verschieben?pfad=Studium%2Fplan.pdf&art=datei").replace("&", "&amp;")}">Verschieben'
        in html
    )


def test_verschieben_seite_zeigt_nur_ordner_und_startet_im_elternordner(client, as_student, konto):
    seite_ = client.get(
        portal.url("/verschieben"), headers=as_student, params={"pfad": "Studium/plan.pdf", "art": "datei"}
    )
    html = seite_.text
    assert seite_.status_code == 200 and "<h1>Datei verschieben</h1>" in html
    assert 'aria-current="page">Studium<' in html  # Start im eigenen Ordner
    assert "Semester 5" in html and "plan.pdf</a>" not in html  # nur Ordner zur Auswahl
    assert "Datei liegt schon hier." in html and "verschieben</button>" not in html
    assert aenderungen(konto) == []


def test_verschieben_navigieren_und_ordner_selbst_nicht_anbieten(client, as_student, konto):
    html = client.get(
        portal.url("/verschieben"), headers=as_student, params={"pfad": "Studium", "art": "ordner", "ziel": ""}
    ).text
    liste = html[html.index('class="folder-pick"') : html.index("</ul>", html.index('class="folder-pick"'))]
    assert ">Ärger<" in liste and ">Studium<" not in liste  # der Ordner selbst fehlt
    assert "Ordner liegt schon hier." in html  # Studium liegt schon auf der obersten Ebene
    ziel = client.get(
        portal.url("/verschieben"), headers=as_student, params={"pfad": "Studium", "art": "ordner", "ziel": "Ärger"}
    ).text
    assert "Nach „Ärger“ verschieben</button>" in ziel and 'name="ziel" value="Ärger"' in ziel


def test_verschieben_pfadleiste_fuehrt_zur_obersten_ebene(client, as_student, konto):
    import html as html_lib
    import re

    start = client.get(
        portal.url("/verschieben"), headers=as_student, params={"pfad": "Studium/plan.pdf", "art": "datei"}
    ).text
    leiste = start[
        start.index('aria-label="Zielordner"') : start.index("</ol>", start.index('aria-label="Zielordner"'))
    ]
    oben = html_lib.unescape(re.search(r'<a href="([^"]+)">Mein bwSync&amp;Share</a>', leiste).group(1))
    assert oben.endswith("&ziel=")  # leeres Ziel bleibt im Link
    seite_ = client.get(oben, headers=as_student).text  # dem Link folgen wie im Browser
    assert 'aria-current="page">Mein bwSync&amp;Share<' in seite_
    assert 'name="ziel" value=""' in seite_
    assert "Nach „Mein bwSync&amp;Share“ verschieben</button>" in seite_
    unter = html_lib.unescape(re.search(r'<a href="([^"]+)">Studium<', seite_).group(1))
    assert 'aria-current="page">Studium<' in client.get(unter, headers=as_student).text  # und wieder hinein
    antwort = senden(client, as_student, "/verschieben", pfad="Studium/plan.pdf", art="datei", ziel="")
    assert antwort.status_code == 303 and antwort.headers["location"] == portal.url("/?ok=verschoben")
    assert konto.files["plan.pdf"] == b"pdf" and "Studium/plan.pdf" not in konto.files
    move = [c for c in konto.calls if c[1] == "/bwsync/move"][-1]
    assert move[2] == {"from": "Studium/plan.pdf", "to": "plan.pdf"}


def test_verschieben_datei_und_ordner(client, as_student, konto):
    antwort = senden(client, as_student, "/verschieben", pfad="Studium/plan.pdf", art="datei", ziel="Ärger")
    assert antwort.status_code == 303 and antwort.headers["location"] == portal.url("/?pfad=%C3%84rger&ok=verschoben")
    assert konto.files["Ärger/plan.pdf"] == b"pdf" and "Studium/plan.pdf" not in konto.files
    antwort = senden(client, as_student, "/verschieben", pfad="Studium", art="ordner", ziel="Ärger")
    assert antwort.status_code == 303
    assert "Ärger/Studium/Semester 5" in konto.folders
    move = [c for c in konto.calls if c[1] == "/bwsync/move"][-1]
    assert move[2] == {"from": "Studium", "to": "Ärger/Studium"}
    assert "Verschoben." in client.get(antwort.headers["location"], headers=as_student).text


@pytest.mark.parametrize(
    "form,status,text",
    [
        ({"pfad": "Studium/plan.pdf", "art": "datei", "ziel": "Studium"}, 400, "liegt schon hier"),
        ({"pfad": "Studium", "art": "ordner", "ziel": "Studium/Semester 5"}, 400, "nicht in sich selbst"),
        ({"pfad": "Studium", "art": "ordner", "ziel": "Studium"}, 400, "nicht in sich selbst"),
        ({"pfad": "zeta.txt", "art": "datei", "ziel": "../x"}, 400, "nicht öffnen"),
        ({"pfad": "zeta.txt", "art": "datei", "ziel": "Fehlt"}, 409, "Diesen Ordner gibt es nicht"),
    ],
)
def test_verschieben_wird_geprueft(client, as_student, konto, form, status, text):
    vorher = (set(konto.files), set(konto.folders))
    antwort = senden(client, as_student, "/verschieben", **form)
    assert antwort.status_code == status and text in fehlerkasten(antwort.text)
    assert "<a " not in fehlerkasten(antwort.text)  # Fehler ohne Formularfeld: Text
    assert (set(konto.files), set(konto.folders)) == vorher


def test_verschieben_name_schon_im_ziel(client, as_student, konto):
    konto.files["Ärger/zeta.txt"] = b"anderes"
    antwort = senden(client, as_student, "/verschieben", pfad="zeta.txt", art="datei", ziel="Ärger")
    assert antwort.status_code == 409
    assert "In „Ärger“ gibt es schon etwas mit dem Namen „zeta.txt“." in fehlerkasten(antwort.text)
    assert konto.files["Ärger/zeta.txt"] == b"anderes" and "zeta.txt" in konto.files  # nie überschrieben


def test_loeschen_fragt_erst_nach(client, as_student, konto):
    frage = client.get(portal.url("/loeschen"), headers=as_student, params={"pfad": "zeta.txt", "art": "datei"})
    assert frage.status_code == 200 and "wirklich löschen" in frage.text and "Papierkorb" in frage.text
    assert aenderungen(konto) == [] and "zeta.txt" in konto.files
    antwort = senden(client, as_student, "/loeschen", pfad="zeta.txt", art="datei")
    assert antwort.status_code == 303 and antwort.headers["location"] == portal.url("/?ok=geloescht")
    assert "zeta.txt" not in konto.files
    assert aenderungen(konto) == [("DELETE", "/bwsync/files/zeta.txt")]


def test_leeren_ordner_loeschen(client, as_student, konto):
    antwort = senden(client, as_student, "/loeschen", pfad="Studium/Semester 5", art="ordner")
    assert antwort.status_code == 303 and antwort.headers["location"] == portal.url("/?pfad=Studium&ok=geloescht")
    assert "Studium/Semester 5" not in konto.folders


def test_ordner_rueckfrage_zeigt_inhalt_und_papierkorb(client, as_student, konto):
    konto.files["Studium/b.txt"] = b"b"
    frage = client.get(portal.url("/loeschen"), headers=as_student, params={"pfad": "Studium", "art": "ordner"})
    assert frage.status_code == 200
    assert "Direkt darin: 2 Dateien und 1 Ordner. Unterordner werden mit ihrem ganzen Inhalt gelöscht." in frage.text
    assert (
        "Alles landet im Papierkorb von bwSync&amp;Share und lässt sich dort für einige Zeit wiederherstellen."
        in frage.text
    )
    assert "Nur leere Ordner" not in frage.text and aenderungen(konto) == []
    leer = client.get(
        portal.url("/loeschen"), headers=as_student, params={"pfad": "Studium/Semester 5", "art": "ordner"}
    ).text
    assert "Der Ordner ist leer." in leer
    konto.files["Ärger/x.txt"] = b"x"
    einer = client.get(portal.url("/loeschen"), headers=as_student, params={"pfad": "Ärger", "art": "ordner"}).text
    assert "Direkt darin: 1 Datei und 0 Ordner." in einer
    datei = client.get(portal.url("/loeschen"), headers=as_student, params={"pfad": "zeta.txt", "art": "datei"}).text
    assert "ordner-inhalt" not in datei and "für einige Zeit" not in datei


def test_ordner_rueckfrage_ohne_liste(client, as_student, konto):
    frage = client.get(portal.url("/loeschen"), headers=as_student, params={"pfad": "Fehlt", "art": "ordner"})
    assert frage.status_code == 200 and "ordner-inhalt" not in frage.text  # ohne Zahl, Löschen meldet den Fehler
    assert '<button type="submit" class="btn btn--primary">Löschen</button>' in frage.text


@pytest.mark.parametrize(
    "route,params,aktion",
    [
        ("/loeschen", {"pfad": "zeta.txt", "art": "datei"}, ">Löschen</button>"),
        ("/umbenennen", {"pfad": "zeta.txt", "art": "datei"}, ">Speichern</button>"),
        ("/verschieben", {"pfad": "zeta.txt", "art": "datei", "ziel": "Studium"}, "verschieben</button>"),
    ],
)
def test_knoepfe_abbrechen_links_ohne_ja(client, as_student, konto, route, params, aktion):
    html = client.get(portal.url(route), headers=as_student, params=params).text
    aktionen = html[html.index('<div class="actions">') :]
    assert aktionen.index(">Abbrechen</a>") < aktionen.index(aktion)
    assert "Ja, " not in html


def test_ordner_mit_inhalt_loeschen(client, as_student, konto):
    antwort = senden(client, as_student, "/loeschen", pfad="Studium", art="ordner")
    assert antwort.status_code == 303 and antwort.headers["location"] == portal.url("/?ok=geloescht")
    assert not any(p == "Studium" or p.startswith("Studium/") for p in konto.folders | set(konto.files))
    assert aenderungen(konto) == [("DELETE", "/bwsync/folders/Studium")]
    assert "Ärger" in konto.folders and "zeta.txt" in konto.files  # sonst nichts


def test_download_fehler_ohne_link_im_fehlerkasten(client, as_student, konto):
    antwort = client.get(portal.url("/datei"), headers=as_student, params={"pfad": "fehlt.txt"})
    assert antwort.status_code == 404
    kasten = fehlerkasten(antwort.text)
    assert "<li>Die Datei konnte nicht heruntergeladen werden." in kasten and "<a " not in kasten
    assert 'class="field__error"' not in antwort.text  # Fehler ohne Feld: nur oben


def test_fehlerkasten_bei_mehreren_feldfehlern_ohne_links():
    from app.views import templates

    html = templates.get_template("_meldungen.html").render(
        errors={"name": "Name fehlt.", "datei": "Datei fehlt."}, done=None
    )
    kasten = fehlerkasten(html)
    assert "<li>Name fehlt.</li>" in kasten and "<li>Datei fehlt.</li>" in kasten
    assert "<a " not in kasten and 'href="#' not in kasten


def test_app_hat_keine_anker_auf_dieselbe_seite():
    # Nur der Sprunglink des Portal-Layouts (#main) ist ein Anker; die Vorlagen der App haben keine.
    vorlagen = Path(__file__).parents[1] / "src" / "app" / "templates"
    for datei in vorlagen.glob("*.html"):
        assert 'href="#' not in datei.read_text(encoding="utf-8"), datei.name


def fehlerkasten(html: str) -> str:
    start = html.index('class="error-summary"')
    return html[start : html.index("</div>", start)]


@pytest.mark.parametrize(
    "params",
    [{"pfad": "", "art": "ordner"}, {"pfad": "zeta.txt", "art": "alles"}, {"pfad": "../zeta.txt", "art": "datei"}],
)
def test_loeschen_ungueltig(client, as_student, konto, params):
    assert client.get(portal.url("/loeschen"), headers=as_student, params=params).status_code == 400
    assert senden(client, as_student, "/loeschen", **params).status_code == 400
    assert aenderungen(konto) == []


def test_nicht_verbunden_zeigt_hinweis_statt_formularen(client, as_student, konto):
    konto.fail = (403, "bwsync-not-linked")
    antwort = seite(client, as_student)
    assert antwort.status_code == 403
    assert "Verbinden Sie Ihr bwSync&amp;Share-Konto, um Ihre Dateien hier zu sehen." in antwort.text
    assert 'class="lead"' not in antwort.text
    assert "Portal-Team" not in antwort.text and "Administration" not in antwort.text
    assert "gilt nicht mehr" not in antwort.text and 'class="crumbs"' not in antwort.text
    assert "Ordner anlegen" not in antwort.text and "Adaptertext" not in antwort.text


def test_nicht_verbunden_bietet_verbinden_an(client, as_student, konto):
    konto.fail = (403, "bwsync-not-linked")
    html = seite(client, as_student).text
    button = re.search(r"<button[^>]*data-bwsync-connect[^>]*>bwSync&amp;Share verbinden</button>", html, re.S)
    assert button
    assert "/connect/bwsync" not in html  # die Routen kennt connect.js aus dem SDK
    assert f'data-done-url="{portal.url("/?ok=verbunden")}"' in button.group(0)
    assert 'id="verbinden-status" role="status"' in html
    sdk = f'<script src="{portal.url("/_sdk/bwsync/connect.js")}" defer></script>'
    app = f'<script src="{portal.url("/static/app.js")}" defer></script>'
    assert sdk in html and app in html and html.index(sdk) < html.index(app)
    assert "<script>" not in html  # kein Inline-Skript (CSP der Apps)


def test_skript_wird_ausgeliefert_und_nutzt_nur_gateway_routen(client, as_student):
    antwort = client.get(portal.url("/static/app.js"), headers=as_student)
    assert antwort.status_code == 200 and "javascript" in antwort.headers["content-type"]
    js = antwort.text
    verbinden = js[: js.index("// --- Umbenennen")]
    assert "window.PortalBwSync" in js and "fetch(" not in verbinden  # Verbinden/Trennen nur über das SDK-Skript
    assert "appPassword" not in js and "localStorage" not in js
    sdk = client.get(portal.url("/_sdk/bwsync/connect.js"), headers=as_student)
    assert sdk.status_code == 200 and "javascript" in sdk.headers["content-type"]
    assert "tab.opener = null" in sdk.text and 'credentials: "same-origin"' in sdk.text
    assert client.get(portal.url("/_sdk/bwsync/connect.js")).status_code == 401


def test_verbunden_zeigt_trennen_mit_bestaetigung(client, as_student, konto):
    html = seite(client, as_student).text
    kopf = html[html.index('class="page-head"') : html.index("</div>", html.index('class="page-head"'))]
    assert "<h1>SyncShare</h1>" in kopf
    assert re.search(
        r'<button type="button" class="btn btn--link" data-dialog-open="trennen-dialog">'
        r"bwSync&amp;Share-Konto trennen …</button>",
        kopf,
    )
    dialog = html[html.index('<dialog id="trennen-dialog"') : html.index("</dialog>", html.index("trennen-dialog"))]
    assert "data-bwsync-disconnect" in dialog and "/connect/bwsync" not in dialog
    assert f'data-done-revoked="{portal.url("/?ok=getrennt")}"' in dialog
    assert f'data-done-kept="{portal.url("/?ok=getrennt-behalten")}"' in dialog
    assert f'data-done-open="{portal.url("/?ok=getrennt-offen")}"' in dialog
    assert '<button value="confirm" class="btn btn--primary">Konto trennen</button>' in dialog
    assert "data-submit-form" not in dialog  # kein Formular: connect.js aus dem SDK trennt


def test_kopf_nur_ueberschrift_pfadleiste_tabelle(client, as_student, konto):
    html = seite(client, as_student, "Studium").text
    h1, nav, table = html.index("<h1>SyncShare</h1>"), html.index('class="crumbs"'), html.index('<table class="table"')
    assert h1 < nav < table
    assert "<h2" not in html[h1:table] and 'class="lead"' not in html  # kein Ordnername als Überschrift, kein Satz
    assert 'aria-current="page">Studium<' in html
    assert '<caption class="visually-hidden">Inhalt von Studium</caption>' in html
    css = client.get(portal.url("/static/app.css"), headers=as_student).text
    assert '.crumbs [aria-current="page"] { font-weight: 700; }' in css


def test_trennen_dialog_text(client, as_student, konto):
    html = seite(client, as_student).text
    assert "Danach müssen Sie bwSync&amp;Share neu verbinden, um Ihre Dateien hier wieder zu sehen." in html
    assert "Ihre Dateien bleiben\n      unverändert." in html


def test_verbinden_zeigt_hinweis_vor_der_serverantwort(client, as_student):
    js = client.get(portal.url("/static/app.js"), headers=as_student).text
    klick = js[js.index('connect.addEventListener("click"') :]
    # Button sofort aus; den Hinweis zeigt der Zustand "waiting", den connect.js vor der ersten Serveranfrage meldet
    assert klick.index("connect.disabled = true") < klick.index("sdk.connect(") < klick.index("say(status, WAITING)")
    sdk = client.get(portal.url("/_sdk/bwsync/connect.js"), headers=as_student).text
    ablauf = sdk[sdk.index("function connect(") :]
    assert ablauf.index('onState({ state: "waiting" })') < ablauf.index("await call(")
    # Nur ein Text beim Verbinden; andere Texte im Verbinden-Teil nur über stop() (Fehler, Button wieder aktiv)
    verbinden = js[js.index("// --- Verbinden") : js.index("// --- Trennen")]
    assert verbinden.count("say(") == 2  # WAITING beim Klick, Fehlertext in stop()
    assert "wird vorbereitet" not in js and "werden geladen" not in js and "Anmeldeseite öffnen" not in js


def test_ohne_konto_kein_trennen(client, as_student, konto):
    konto.fail = (403, "bwsync-not-linked")
    html = seite(client, as_student).text
    assert "trennen-dialog" not in html and "Konto trennen" not in html


@pytest.mark.parametrize(
    "ok,text",
    [
        ("getrennt", "wurde in bwSync&amp;Share widerrufen"),
        ("getrennt-behalten", "bleibt in bwSync&amp;Share bestehen"),
        ("getrennt-offen", "ließ sich in bwSync&amp;Share nicht widerrufen"),
    ],
)
def test_nach_dem_trennen_meldung(client, as_student, konto, ok, text):
    konto.fail = (403, "bwsync-not-linked")
    html = client.get(portal.url("/"), headers=as_student, params={"ok": ok}).text
    assert "Ihr bwSync&amp;Share-Konto ist getrennt." in html and text in html
    assert "bwSync&amp;Share verbinden" in html


def test_nach_dem_verbinden_meldung(client, as_student, konto):
    html = client.get(portal.url("/"), headers=as_student, params={"ok": "verbunden"}).text
    assert "Ihr bwSync&amp;Share-Konto ist verbunden." in html


def test_verbindung_ungueltig_sagt_neu_verbinden(client, as_student, konto):
    konto.fail = (403, "bwsync-link-invalid")
    antwort = seite(client, as_student)
    assert "gilt nicht mehr" in antwort.text and "Bitte verbinden Sie es neu." in antwort.text
    assert "Ordner anlegen" not in antwort.text


def test_adapter_nicht_erreichbar(client, as_student, konto):
    konto.fail = (502, None)
    antwort = seite(client, as_student)
    assert antwort.status_code == 502 and "gerade nicht erreichbar" in antwort.text


def test_namen_werden_maskiert(client, as_student, konto):
    konto.folders.add('<img src=x onerror="alert(1)">')
    html = seite(client, as_student).text
    assert "<img src=x" not in html and "&lt;img src=x onerror=&#34;alert(1)&#34;&gt;" in html


def test_hinweise_bei_vielen_und_ausgelassenen_eintraegen(client, as_student, konto):
    konto.listing = {
        "path": "",
        "truncated": True,
        "skipped": 2,
        "entries": [{"name": "a.txt", "type": "file", "size": 1, "modified": None}],
    }
    html = seite(client, as_student).text
    assert "mehr als 1000 Einträge" in html and "2 Einträge werden nicht angezeigt" in html


def test_erfolgsmeldung_nur_aus_festen_texten(client, as_student, konto):
    html = seite(client, as_student, ok="<b>boese</b>").text
    assert "boese" not in html


# --- Fehlertypen aus SDK 1.2.0-dev und Befunde aus dem UI-Review ------------------------------------------------


@pytest.mark.parametrize(
    "kind,status,text",
    [
        (bwsync_errors.NOT_A_FOLDER, 409, "Das ist eine Datei, kein Ordner."),
        (bwsync_errors.NOT_A_FILE, 409, "Das ist ein Ordner, keine Datei."),
        (bwsync_errors.FOLDER_TOO_LARGE, 422, "direkt in bwSync&amp;Share"),
        (bwsync_errors.MOVE_INTO_SELF, 400, "nicht in sich selbst verschoben"),
        (transport.BACKEND_TIMEOUT, 504, "zu langsam"),
        (transport.BACKEND_UNAVAILABLE, 503, "gerade nicht erreichbar"),
        ("bwsync-ganz-neu", 409, "Das hat in bwSync&amp;Share nicht geklappt."),  # unbekannt: fachlich, allgemein
    ],
)
def test_fehlertypen_mit_eigenem_text(client, as_student, konto, kind, status, text):
    konto.fail = (status, kind)
    antwort = seite(client, as_student, "Studium")
    assert antwort.status_code == status and antwort.text.count(text) == 1
    if kind not in (transport.BACKEND_TIMEOUT, transport.BACKEND_UNAVAILABLE):
        assert "nicht erreichbar" not in antwort.text


def test_liste_auf_einer_datei_und_loeschen_falscher_art(client, as_student, konto):
    antwort = seite(client, as_student, "zeta.txt")  # Fake wie Adapter: 409 bwsync-not-a-folder
    assert antwort.status_code == 409 and "Das ist eine Datei, kein Ordner." in antwort.text
    antwort = senden(client, as_student, "/loeschen", pfad="zeta.txt", art="ordner")
    assert antwort.status_code == 409 and "Das ist eine Datei, kein Ordner." in antwort.text
    antwort = senden(client, as_student, "/loeschen", pfad="Studium", art="datei")
    assert antwort.status_code == 409 and "Das ist ein Ordner, keine Datei." in antwort.text
    assert aenderungen(konto) == [("DELETE", "/bwsync/folders/zeta.txt"), ("DELETE", "/bwsync/files/Studium")]
    assert "zeta.txt" in konto.files and "Studium" in konto.folders  # nichts gelöscht


def test_aufruf_fehler_geht_an_die_standardbehandlung_des_sdk(client, as_student, konto):
    konto.fail = (403, transport.SCOPE_NOT_GRANTED)
    antwort = client.get(portal.url("/"), headers={**as_student, "X-Request-Id": "req-42"})
    assert antwort.status_code == 500
    assert "Fehler in der Anwendung" in antwort.text and "Vorgangsnummer: req-42" in antwort.text
    assert "nicht erreichbar" not in antwort.text and "scope" not in antwort.text.lower()


def test_derselbe_fehler_nur_einmal(client, as_student, konto):
    konto.fail = (503, None)  # Anlegen scheitert, die Liste danach auch
    antwort = senden(client, as_student, "/ordner", pfad="", name="Neu")
    assert antwort.text.count("gerade nicht erreichbar") == 1
    antwort = hochladen(client, as_student)
    assert antwort.text.count("gerade nicht erreichbar") == 1


@pytest.mark.parametrize("route,pfad", [("/datei", "zeta.txt"), ("/zip", "Studium")])
def test_herunterladen_nicht_verbunden_nur_ein_hinweis(client, as_student, konto, route, pfad):
    konto.fail = (403, bwsync_errors.NOT_LINKED)
    antwort = client.get(portal.url(route), headers=as_student, params={"pfad": pfad})
    assert antwort.status_code == 403
    assert "Verbinden Sie Ihr bwSync&amp;Share-Konto" in antwort.text
    assert "nicht heruntergeladen" not in antwort.text and 'class="error-summary"' not in antwort.text


def test_hochladen_nicht_verbunden_nur_ein_hinweis(client, as_student, konto):
    konto.fail = (403, bwsync_errors.NOT_LINKED)
    antwort = hochladen(client, as_student)
    assert antwort.status_code == 403 and "Verbinden Sie Ihr bwSync&amp;Share-Konto" in antwort.text
    assert "nicht verbunden. Bitte verbinden Sie es" not in antwort.text and 'class="field__error"' not in antwort.text


def test_oberste_ebene_ohne_link_auf_sich_selbst(client, as_student, konto):
    konto.fail = (503, None)
    assert "Zur obersten Ebene" not in seite(client, as_student).text
    assert "Zur obersten Ebene" in seite(client, as_student, "Studium").text
    params = {"pfad": "Studium/plan.pdf", "art": "datei"}
    oben = client.get(portal.url("/verschieben"), headers=as_student, params={**params, "ziel": ""}).text
    darunter = client.get(portal.url("/verschieben"), headers=as_student, params={**params, "ziel": "Studium"}).text
    assert "Zur obersten Ebene" not in oben and "Zur obersten Ebene" in darunter


def test_fehlerkasten_fokussierbar_und_erfolgsmeldung_markiert(client, as_student, konto):
    from app.views import templates

    html = templates.get_template("_meldungen.html").render(errors={"liste": "x"}, done=None)
    assert '<div class="error-summary" role="alert" tabindex="-1">' in html
    html = seite(client, as_student, ok="verbunden").text
    assert '<div class="notice" role="status" data-done>' in html


def test_nach_dem_trennen_meldung_genau_einmal(client, as_student, konto):
    konto.fail = (403, bwsync_errors.NOT_LINKED)
    html = client.get(portal.url("/"), headers=as_student, params={"ok": "getrennt-behalten"}).text
    assert html.count("Ihr bwSync&amp;Share-Konto ist getrennt.") == 1
    assert 'class="error-summary"' not in html and "nicht erreichbar" not in html


def test_skript_raeumt_erfolgsmeldung_auf_und_zeigt_fehler_deutlich(client, as_student):
    js = client.get(portal.url("/static/app.js"), headers=as_student).text
    assert 'url.searchParams.delete("ok")' in js and "history.replaceState" in js
    assert 'querySelectorAll("[data-done]")' in js
    verbinden = js[js.index('connect.addEventListener("click"') : js.index("// --- Trennen")]
    assert verbinden.index("clearDone()") < verbinden.index("sdk.connect(")
    assert "say(status, text, true)" in verbinden  # Fehler beim Verbinden: als Fehler
    trennen = js[js.index("// --- Trennen") : js.index("// --- Umbenennen")]
    assert trennen.index("clearDone()") < trennen.index("sdk.disconnect()")
    assert "say(status, problem(error), true)" in trennen
    assert 'el.classList.toggle("field__error", Boolean(isError))' in js
    assert '"bwsync-link-failed":' in js
