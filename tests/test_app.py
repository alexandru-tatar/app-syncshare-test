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


def hochladen(client, headers, name="notiz.txt", inhalt=b"Hallo Welt"):
    return client.post(portal.url("/upload"), headers=headers, files={"datei": (name, inhalt)})


def test_hochladen_und_herunterladen(client, as_student):
    antwort = hochladen(client, as_student)
    assert antwort.status_code == 200
    assert "notiz.txt" in antwort.text
    link = antwort.text.split(f'href="{portal.url("/datei/")}')[1].split('"')[0]
    download = client.get(portal.url(f"/datei/{link}"), headers=as_student)
    assert download.status_code == 200
    assert download.content == b"Hallo Welt"
    assert download.headers["content-disposition"].startswith("attachment;")
    assert download.headers["x-content-type-options"] == "nosniff"


def test_andere_person_sieht_die_datei_nicht(client, as_student):

    antwort = hochladen(client, as_student, name="geheim.txt")
    link = antwort.text.split(f'href="{portal.url("/datei/")}')[1].split('"')[0]
    fremd = {"X-Portal-Assertion": make_token(sub="andere-person"), "Accept": "text/html"}
    assert "geheim.txt" not in client.get(portal.url("/"), headers=fremd).text
    assert client.get(portal.url(f"/datei/{link}"), headers=fremd).status_code == 404


def test_pfad_im_dateinamen_wird_entfernt(client, as_student):
    antwort = hochladen(client, as_student, name="../../etc/passwd")
    assert "passwd" in antwort.text
    assert "../" not in antwort.text


def test_zu_grosse_datei_deutsche_meldung(client, as_student):
    antwort = hochladen(client, as_student, inhalt=b"x" * (2 * 1024 * 1024 + 1))
    assert antwort.status_code == 400
    assert "größer als 2 MB" in antwort.text


def test_ohne_datei_deutsche_meldung_statt_422(client, as_student):
    antwort = client.post(portal.url("/upload"), headers=as_student)
    assert antwort.status_code == 400
    assert "Bitte wählen Sie eine Datei aus." in antwort.text


def test_upload_ohne_anmeldung_401(client):
    antwort = client.post(portal.url("/upload"), files={"datei": ("a.txt", b"x")})
    assert antwort.status_code == 401
