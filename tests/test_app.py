import json
import re
from html import unescape

import pytest
from conftest import make_token

from app.main import portal


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
    return [(c.method, c.url.path) for c in backend.calls if c.method != "GET"]


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
    assert konto.calls[0].url.raw_path == b"/bwsync/folders/"


def test_jede_anfrage_mit_der_eigenen_anmeldung(client, konto):
    token = make_token(sub="andere-person")
    seite(client, {"X-Portal-Assertion": token, "Accept": "text/html"})
    assert konto.calls[-1].headers["authorization"] == f"Bearer {token}"


def test_navigieren_mit_brotkrumen(client, as_student, konto):
    html = seite(client, as_student, "Studium/Semester 5").text
    assert konto.calls[-1].url.raw_path == b"/bwsync/folders/Studium/Semester%205"
    nav = html[html.index('class="crumbs"') : html.index("</nav>", html.index('class="crumbs"'))]
    assert portal.url("/") in links(nav) and portal.url("/?pfad=Studium") in links(nav)
    assert 'aria-current="page">Semester 5<' in nav
    assert "Dieser Ordner ist leer." in html and "Eine Ebene höher" not in html  # die Pfadleiste reicht


def test_formularfelder_ohne_sichtbares_label(client, as_student, konto):
    html = seite(client, as_student).text
    assert '<label for="name" class="visually-hidden">Name des Ordners</label>' in html
    assert '<label for="datei" class="visually-hidden">Datei</label>' in html
    assert "(Pflichtfeld)" not in html
    assert '<caption class="visually-hidden">Inhalt von Mein bwSync&amp;Share</caption>' in html
    assert re.search(r'<input type="text" id="name"[^>]*placeholder="Neuer Ordner"[^>]*required>', html)
    assert re.search(r'<input type="file" id="datei"[^>]*required>', html)


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
    assert download.headers["content-disposition"] == "attachment; filename*=UTF-8''notiz%20%C3%A4.txt"
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


def test_umbenennen(client, as_student, konto):
    formular = client.get(
        portal.url("/umbenennen"), headers=as_student, params={"pfad": "Studium/plan.pdf", "art": "datei"}
    )
    assert formular.status_code == 200 and 'value="plan.pdf"' in formular.text and 'value="Studium"' in formular.text
    assert aenderungen(konto) == []
    antwort = senden(
        client, as_student, "/umbenennen", pfad="Studium/plan.pdf", art="datei", name="Plan 2026.pdf", ziel="Studium"
    )
    assert antwort.status_code == 303 and antwort.headers["location"] == portal.url("/?pfad=Studium&ok=umbenannt")
    assert konto.files["Studium/Plan 2026.pdf"] == b"pdf" and "Studium/plan.pdf" not in konto.files
    move = [c for c in konto.calls if c.url.path == "/bwsync/move"][0]
    assert json.loads(move.content) == {"from": "Studium/plan.pdf", "to": "Studium/Plan 2026.pdf"}


def test_ordner_verschieben(client, as_student, konto):
    antwort = senden(client, as_student, "/umbenennen", pfad="Studium", art="ordner", name="Studium", ziel="Ärger")
    assert antwort.status_code == 303
    assert "Ärger/Studium/Semester 5" in konto.folders and "Ärger/Studium/plan.pdf" in konto.files


@pytest.mark.parametrize(
    "form,text",
    [
        ({"name": "zeta.txt", "ziel": ""}, "gibt es schon"),
        ({"name": "x.pdf", "ziel": "Fehlt"}, "Den Zielordner gibt es nicht."),
    ],
)
def test_umbenennen_fehler_vom_adapter(client, as_student, konto, form, text):
    antwort = senden(client, as_student, "/umbenennen", pfad="Studium/plan.pdf", art="datei", **form)
    assert antwort.status_code == 409 and text in antwort.text
    assert konto.files["Studium/plan.pdf"] == b"pdf"
    feld = "ziel" if form["ziel"] else "name"
    assert re.search(rf'<li><a href="#{feld}">[^<]*{text}', fehlerkasten(antwort.text))  # mit Feld: Link ins Feld


@pytest.mark.parametrize(
    "form,text",
    [
        ({"pfad": "Studium/plan.pdf", "art": "datei", "name": "plan.pdf", "ziel": "Studium"}, "neuen Namen"),
        ({"pfad": "Studium", "art": "ordner", "name": "Studium", "ziel": "Studium/Semester 5"}, "nicht in sich selbst"),
        ({"pfad": "zeta.txt", "art": "datei", "name": "a/b", "ziel": ""}, "höchstens 255 Zeichen"),
        ({"pfad": "zeta.txt", "art": "datei", "name": "neu", "ziel": "../x"}, "zum Beispiel Studium/Semester 5"),
    ],
)
def test_umbenennen_wird_vorher_geprueft(client, as_student, konto, form, text):
    antwort = senden(client, as_student, "/umbenennen", **form)
    assert antwort.status_code == 400 and text in antwort.text
    assert aenderungen(konto) == []


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


def test_ordner_mit_inhalt_wird_nicht_geloescht(client, as_student, konto):
    antwort = senden(client, as_student, "/loeschen", pfad="Studium", art="ordner")
    assert antwort.status_code == 409 and "Der Ordner ist nicht leer." in antwort.text
    assert "Studium/plan.pdf" in konto.files
    kasten = fehlerkasten(antwort.text)
    assert "<li>Der Ordner ist nicht leer." in kasten and "<a " not in kasten  # ohne Feld: Text, kein Link


def test_download_fehler_ohne_link_im_fehlerkasten(client, as_student, konto):
    antwort = client.get(portal.url("/datei"), headers=as_student, params={"pfad": "fehlt.txt"})
    assert antwort.status_code == 404
    kasten = fehlerkasten(antwort.text)
    assert "<li>Die Datei konnte nicht heruntergeladen werden." in kasten and "<a " not in kasten


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
    assert "Ihre Dateien aus bwSync&amp;Share." in antwort.text
    assert "Portal-Team" not in antwort.text and "Administration" not in antwort.text
    assert "gilt nicht mehr" not in antwort.text and 'class="crumbs"' not in antwort.text
    assert "Ordner anlegen" not in antwort.text and "Adaptertext" not in antwort.text


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
