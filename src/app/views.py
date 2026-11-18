"""SyncShare: Datei-Explorer für das eigene bwSync&Share-Konto.

Alle Dateien und Ordner liegen in bwSync&Share; die App speichert nichts. Jeder Aufruf geht mit der
Anmeldung der Person über das Portal-SDK an den bwSync-Adapter, der nur das eigene Konto kennt.
Seiten mit GET ändern nie etwas; Anlegen, Hochladen, Umbenennen und Löschen sind Formulare (POST) und
leiten danach auf die Ordneransicht um (Post/Redirect/Get). Fremde Herkunft lehnt das Gateway bei POST ab.
"""

from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode

from fastapi import APIRouter, Depends, File, Form, Query, Request, UploadFile
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates
from starlette.background import BackgroundTask
from starlette.responses import StreamingResponse

from platform_sdk import AdapterError, Portal, User
from platform_sdk.adapters import transport
from platform_sdk.adapters.bwsync import connect, paths
from platform_sdk.adapters.bwsync import errors as bwsync_errors
from platform_sdk.adapters.bwsync.paths import clean_name, join, name_of, parent_of, valid_name, valid_path
from platform_sdk.common.dates import format_datetime
from platform_sdk.common.downloads import download_headers
from platform_sdk.common.sizes import format_size

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")  # autoescape ist an

MAX_BYTES = 2 * 1024 * 1024  # 2 MB je Datei
ROOT_NAME = "Mein bwSync&Share"
INVALID_NAME = (
    "Bitte geben Sie einen Namen an. Er darf höchstens 255 Zeichen lang sein und keines dieser Zeichen "
    "enthalten: / \\ : % ? #"
)
INVALID_PATH = "Diesen Ordner kann SyncShare nicht öffnen."
UNAVAILABLE = "bwSync&Share ist gerade nicht erreichbar. Bitte versuchen Sie es später erneut."
TOO_SLOW = "bwSync&Share antwortet gerade zu langsam. Bitte versuchen Sie es gleich noch einmal."
# Fachlicher Fehler, den SyncShare (noch) nicht kennt: allgemein, aber nicht „nicht erreichbar“
NOT_POSSIBLE = "Das hat in bwSync&Share nicht geklappt. Bitte laden Sie die Seite neu und versuchen Sie es erneut."

# Fehlertypen (Konstanten aus dem SDK), in eigenen Worten; Bedeutung: platform_sdk/adapters/bwsync/README.md
PROBLEMS = {
    bwsync_errors.NOT_LINKED: "Ihr bwSync&Share-Konto ist nicht verbunden. Bitte verbinden Sie es auf der Startseite "
    "von SyncShare.",
    bwsync_errors.LINK_INVALID: "Die Verbindung zu Ihrem bwSync&Share-Konto gilt nicht mehr, zum Beispiel weil das "
    "App-Passwort widerrufen wurde. Bitte verbinden Sie es neu.",
    bwsync_errors.FORBIDDEN: "Dafür fehlt Ihnen in bwSync&Share die Berechtigung, zum Beispiel in einem nur lesbar "
    "geteilten Ordner.",
    bwsync_errors.EXISTS: "Hier gibt es schon einen Ordner oder eine Datei mit diesem Namen. Bitte wählen Sie einen "
    "anderen Namen.",
    bwsync_errors.PARENT_MISSING: "Diesen Ordner gibt es nicht (mehr). Bitte laden Sie die Seite neu.",
    bwsync_errors.NOT_FOUND: "Das gibt es nicht (mehr). Vielleicht wurde es in bwSync&Share verschoben oder gelöscht.",
    bwsync_errors.CONFLICT: "Das geht in bwSync&Share gerade nicht. Bitte laden Sie die Seite neu und versuchen Sie es "
    "erneut.",
    bwsync_errors.TOO_LARGE: "Die Datei ist zu groß.",
    bwsync_errors.LOCKED: "Die Datei ist gerade gesperrt, zum Beispiel weil sie geöffnet ist. Bitte versuchen Sie es "
    "später erneut.",
    bwsync_errors.NOT_A_FILE: "Das ist ein Ordner, keine Datei. Bitte laden Sie die Seite neu.",
    bwsync_errors.NOT_A_FOLDER: "Das ist eine Datei, kein Ordner. Bitte laden Sie die Seite neu.",
    bwsync_errors.MOVE_INTO_SELF: "Ein Ordner kann nicht in sich selbst verschoben werden. Bitte wählen Sie einen "
    "anderen Ordner.",
    bwsync_errors.FOLDER_TOO_LARGE: "Dieser Ordner ist zu groß für SyncShare. Bitte öffnen Sie ihn direkt in "
    "bwSync&Share.",
    transport.BACKEND_TIMEOUT: TOO_SLOW,
}
# Erfolgsmeldungen nach der Umleitung: nur feste Schlüssel, kein Text aus der Adresse
DONE = {
    "ordner": "Der Ordner wurde angelegt.",
    "hochgeladen": "Die Datei wurde hochgeladen.",
    "umbenannt": "Umbenannt.",
    "verschoben": "Verschoben.",
    "geloescht": "Gelöscht. Es liegt jetzt im Papierkorb von bwSync&Share.",
    "verbunden": "Ihr bwSync&Share-Konto ist verbunden.",
    "getrennt": "Ihr bwSync&Share-Konto ist getrennt. Das App-Passwort von SyncShare wurde in bwSync&Share widerrufen.",
    "getrennt-behalten": "Ihr bwSync&Share-Konto ist getrennt. Das App-Passwort wurde nicht von SyncShare angelegt "
    "und bleibt in bwSync&Share bestehen; Sie können es dort unter Einstellungen → Sicherheit löschen.",
    "getrennt-offen": "Ihr bwSync&Share-Konto ist getrennt. Das App-Passwort ließ sich in bwSync&Share nicht "
    "widerrufen; bitte löschen Sie es dort unter Einstellungen → Sicherheit.",
}
KINDS = {"datei": "Datei", "ordner": "Ordner"}


def problem_text(exc: AdapterError) -> str:
    """Eigener Text nach type. Aufruf-Fehler (Programmierfehler, z. B. scope-not-granted) behandelt SyncShare nicht
    selbst: Sie gehen an die Standardbehandlung des SDK („Fehler in der Anwendung“ mit Vorgangsnummer)."""
    kind = transport.category(exc)
    if kind == "call":
        raise exc
    if exc.type in PROBLEMS:
        return PROBLEMS[exc.type]
    return UNAVAILABLE if kind == "technical" else NOT_POSSIBLE


def page_status(exc: AdapterError) -> int:
    return exc.status if 400 <= exc.status < 600 else 502


def build_router(portal: Portal) -> APIRouter:
    router = portal.router()

    def link(route: str, **params: str) -> str:
        query = {key: value for key, value in params.items() if value}
        return portal.url(route) + ("?" + urlencode(query, quote_via=quote) if query else "")

    def folder_url(path: str, **params: str) -> str:
        return link("/", pfad=path, **params)

    def back_to(path: str, ok: str) -> RedirectResponse:
        return RedirectResponse(folder_url(path, ok=ok), status_code=303)

    async def render(user: User, template: str, page_title: str, status_code: int, **context) -> HTMLResponse:
        body = templates.get_template(template).render(**context)
        html = await portal.layout(title=page_title, body=body, current=portal.url("/"), user=user)
        return HTMLResponse(html, status_code=status_code)

    def crumbs(path: str, url=None) -> list[dict[str, str]]:
        url = url or folder_url
        return [{"name": ROOT_NAME, "url": url("")}] + [
            {"name": c["name"], "url": url(c["path"])} for c in paths.breadcrumbs(path)
        ]

    async def explorer(
        user: User,
        path: str,
        *,
        errors: dict[str, str] | None = None,
        status_code: int = 200,
        ok: str = "",
        values: dict[str, str] | None = None,
    ) -> HTMLResponse:
        errors = dict(errors or {})
        folder = link_problem = None
        if not valid_path(path):
            errors["liste"] = INVALID_PATH
            status_code = 400
        else:
            try:
                folder = await portal.adapters.bwsync.list(path)
            except AdapterError as exc:
                if exc.type in bwsync_errors.LINK_PROBLEMS:
                    link_problem = exc.type
                else:
                    # Ohne Liste gibt es keine Formulare: derselbe Text (Anlegen und Liste scheitern) nur einmal oben
                    text = problem_text(exc)
                    errors = {field: message for field, message in errors.items() if message != text}
                    errors.setdefault("liste", text)
                if status_code == 200:
                    status_code = page_status(exc)
        rows = []
        for entry in folder.entries if folder else []:
            entry_path = join(path, entry.name)
            is_folder = entry.type == "folder"
            kind = "ordner" if is_folder else "datei"
            rows.append(
                {
                    "name": entry.name,
                    "folder": is_folder,
                    "size": format_size(None if is_folder else entry.size),
                    "modified": format_datetime(entry.modified),
                    "open_url": folder_url(entry_path) if is_folder else None,
                    "download_url": link("/zip" if is_folder else "/datei", pfad=entry_path),
                    "pfad": entry_path,
                    "art": kind,
                    "rename_url": link("/umbenennen", pfad=entry_path, art=kind),
                    "move_url": link("/verschieben", pfad=entry_path, art=kind),
                    "delete_url": link("/loeschen", pfad=entry_path, art=kind),
                }
            )
        return await render(
            user,
            "page.html",
            "SyncShare",
            status_code,
            pfad=path if folder else "",
            at_root=path == "",
            folder=folder,
            rows=rows,
            crumbs=crumbs(path) if folder else [],
            title=name_of(path) if path else ROOT_NAME,
            root_url=folder_url(""),
            link_problem=link_problem,
            link_invalid=PROBLEMS[bwsync_errors.LINK_INVALID],
            connect_js_url=portal.url(connect.SCRIPT),  # Ablauf aus dem SDK; Knopf und Texte macht die App
            connected_url=folder_url("", ok="verbunden"),
            disconnected_urls={
                "revoked": folder_url("", ok="getrennt"),
                "kept": folder_url("", ok="getrennt-behalten"),
                "open": folder_url("", ok="getrennt-offen"),
            },
            js_url=portal.url("/static/app.js"),
            errors=errors,
            done=DONE.get(ok, "") if not errors else "",
            values=values or {},
            mkdir_url=link("/ordner"),
            rename_action=link("/umbenennen"),
            upload_url=link("/upload"),
            max_mb=MAX_BYTES // (1024 * 1024),
        )

    @router.get("/", response_class=HTMLResponse)
    async def home(
        user: User = Depends(portal.user), pfad: Annotated[str, Query()] = "", ok: Annotated[str, Query()] = ""
    ) -> HTMLResponse:
        return await explorer(user, pfad, ok=ok)

    @router.post("/ordner", response_class=HTMLResponse)
    async def make_folder(
        user: User = Depends(portal.user), pfad: Annotated[str, Form()] = "", name: Annotated[str, Form()] = ""
    ) -> Response:
        name = name.strip()
        if not valid_path(pfad) or not valid_name(name):
            return await explorer(user, pfad, errors={"name": INVALID_NAME}, status_code=400, values={"name": name})
        try:
            await portal.adapters.bwsync.mkdir(join(pfad, name))
        except AdapterError as exc:
            if exc.type in bwsync_errors.LINK_PROBLEMS:
                return await explorer(user, pfad, status_code=page_status(exc))
            return await explorer(
                user, pfad, errors={"name": problem_text(exc)}, status_code=page_status(exc), values={"name": name}
            )
        return back_to(pfad, "ordner")

    @router.post("/upload", response_class=HTMLResponse)
    async def upload(
        user: User = Depends(portal.user),
        pfad: Annotated[str, Form()] = "",
        datei: Annotated[UploadFile | None, File()] = None,
    ) -> Response:
        name = clean_name(datei.filename) if datei else ""
        if not name:
            return await explorer(user, pfad, errors={"datei": "Bitte wählen Sie eine Datei aus."}, status_code=400)
        if not valid_path(pfad) or not valid_name(name):
            return await explorer(
                user, pfad, errors={"datei": "Der Dateiname geht so nicht. " + INVALID_NAME}, status_code=400
            )
        data = await datei.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            msg = f"Die Datei ist größer als {MAX_BYTES // (1024 * 1024)} MB. Bitte wählen Sie eine kleinere Datei."
            return await explorer(user, pfad, errors={"datei": msg}, status_code=400)
        try:
            await portal.adapters.bwsync.upload(join(pfad, name), data)
        except AdapterError as exc:
            if exc.type in bwsync_errors.LINK_PROBLEMS:  # den Hinweis zum Verbinden zeigt die Seite schon
                return await explorer(user, pfad, status_code=page_status(exc))
            return await explorer(user, pfad, errors={"datei": problem_text(exc)}, status_code=page_status(exc))
        return back_to(pfad, "hochgeladen")

    @router.get("/datei")
    async def download(user: User = Depends(portal.user), pfad: Annotated[str, Query()] = "") -> Response:
        if not valid_path(pfad, root=False):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        try:
            data = await portal.adapters.bwsync.download(pfad, max_bytes=MAX_BYTES)
        except AdapterError as exc:
            if exc.type in bwsync_errors.LINK_PROBLEMS:  # den Hinweis zum Verbinden zeigt die Seite schon
                return await explorer(user, parent_of(pfad), status_code=page_status(exc))
            if exc.type == transport.DOWNLOAD_TOO_LARGE:
                msg = f"Die Datei ist größer als {MAX_BYTES // (1024 * 1024)} MB; SyncShare kann sie nicht abholen."
            else:
                msg = "Die Datei konnte nicht heruntergeladen werden. " + problem_text(exc)
            return await explorer(user, parent_of(pfad), errors={"liste": msg}, status_code=page_status(exc))
        # Nie im Browser ausführen oder anzeigen: immer als Download, ohne Typ-Erkennung.
        return Response(
            data,
            media_type="application/octet-stream",
            headers=download_headers(name_of(pfad)),  # Content-Disposition, nosniff, no-store
        )

    @router.get("/zip")
    async def download_folder(user: User = Depends(portal.user), pfad: Annotated[str, Query()] = "") -> Response:
        """Ordner als ZIP, von bwSync gestreamt (nicht im Speicher der App); nicht die oberste Ebene."""
        if not valid_path(pfad, root=False):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        try:
            stream = await portal.adapters.bwsync.download_zip(pfad)
        except AdapterError as exc:
            if exc.type in bwsync_errors.LINK_PROBLEMS:  # den Hinweis zum Verbinden zeigt die Seite schon
                return await explorer(user, parent_of(pfad), status_code=page_status(exc))
            msg = "Der Ordner konnte nicht heruntergeladen werden. " + problem_text(exc)
            return await explorer(user, parent_of(pfad), errors={"liste": msg}, status_code=page_status(exc))
        return StreamingResponse(
            stream,
            media_type="application/zip",
            headers=download_headers(name_of(pfad) + ".zip"),
            background=BackgroundTask(stream.aclose),
        )

    def target_ok(pfad: str, art: str) -> bool:
        return valid_path(pfad, root=False) and art in KINDS

    # --- Löschen: GET zeigt nur die Rückfrage, erst POST löscht -----------------------------------------------

    async def folder_content(pfad: str) -> str | None:
        """Was direkt im Ordner liegt, als Satz; None, wenn die Liste gerade nicht zu holen ist."""
        try:
            listing = await portal.adapters.bwsync.list(pfad)
        except AdapterError:
            return None
        files = sum(1 for e in listing.entries if e.type != "folder")
        folders = len(listing.entries) - files
        if not files and not folders and not listing.skipped and not listing.truncated:
            return "Der Ordner ist leer."
        parts = [f"{files} {'Datei' if files == 1 else 'Dateien'}", f"{folders} Ordner"]
        text = ("mindestens " if listing.truncated else "") + " und ".join(parts)
        if listing.skipped:
            text += f" sowie {listing.skipped} weitere, die SyncShare nicht anzeigt"
        return f"Direkt darin: {text}. Unterordner werden mit ihrem ganzen Inhalt gelöscht."

    async def delete_page(user: User, pfad: str, art: str, *, errors=None, status_code=200) -> HTMLResponse:
        return await render(
            user,
            "loeschen.html",
            f"{KINDS[art]} löschen – SyncShare",
            status_code,
            name=name_of(pfad),
            where=parent_of(pfad) or ROOT_NAME,
            pfad=pfad,
            art=art,
            kind=KINDS[art],
            folder=art == "ordner",
            content=await folder_content(pfad) if art == "ordner" else None,
            action=link("/loeschen"),
            back_url=folder_url(parent_of(pfad)),
            errors=errors or {},
        )

    @router.get("/loeschen", response_class=HTMLResponse)
    async def confirm_delete(
        user: User = Depends(portal.user), pfad: Annotated[str, Query()] = "", art: Annotated[str, Query()] = ""
    ) -> HTMLResponse:
        if not target_ok(pfad, art):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        return await delete_page(user, pfad, art)

    @router.post("/loeschen", response_class=HTMLResponse)
    async def delete(
        user: User = Depends(portal.user), pfad: Annotated[str, Form()] = "", art: Annotated[str, Form()] = ""
    ) -> Response:
        if not target_ok(pfad, art):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        try:
            await portal.adapters.bwsync.delete(pfad, folder=art == "ordner")
        except AdapterError as exc:
            return await delete_page(
                user, pfad, art, errors={"loeschen": problem_text(exc)}, status_code=page_status(exc)
            )
        return back_to(parent_of(pfad), "geloescht")

    # --- Umbenennen (nur der Name, im selben Ordner) ---------------------------------------------------------
    # Normalfall: direkt in der Tabelle (app.js schickt POST mit Accept: application/json, Antwort JSON).
    # Ohne JavaScript: eigene Seite als Ausweichweg. Der Elternordner bleibt; Verschieben hat eine eigene Seite.

    async def rename_page(user: User, pfad: str, art: str, *, name=None, errors=None, status_code=200):
        return await render(
            user,
            "umbenennen.html",
            f"{KINDS[art]} umbenennen – SyncShare",
            status_code,
            old_name=name_of(pfad),
            name=name_of(pfad) if name is None else name,
            pfad=pfad,
            art=art,
            kind=KINDS[art],
            action=link("/umbenennen"),
            back_url=folder_url(parent_of(pfad)),
            errors=errors or {},
        )

    def rename_problem(name: str, pfad: str) -> str | None:
        if "/" in name:
            return "Der Name darf kein „/“ enthalten. Verschieben ist hier nicht möglich."
        if not valid_name(name):
            return INVALID_NAME
        if name == name_of(pfad):
            return "Bitte geben Sie einen neuen Namen an."
        return None

    @router.get("/umbenennen", response_class=HTMLResponse)
    async def rename_form(
        user: User = Depends(portal.user), pfad: Annotated[str, Query()] = "", art: Annotated[str, Query()] = ""
    ) -> HTMLResponse:
        if not target_ok(pfad, art):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        return await rename_page(user, pfad, art)

    @router.post("/umbenennen", response_class=HTMLResponse)
    async def rename(
        request: Request,
        user: User = Depends(portal.user),
        pfad: Annotated[str, Form()] = "",
        art: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
    ) -> Response:
        as_json = "application/json" in request.headers.get("accept", "")

        def failed(text: str, status_code: int) -> Response | None:
            if as_json:
                return JSONResponse({"error": text}, status_code=status_code, headers={"Cache-Control": "no-store"})
            return None

        if not target_ok(pfad, art):
            return failed(INVALID_PATH, 400) or await explorer(
                user, "", errors={"liste": INVALID_PATH}, status_code=400
            )
        name = name.strip()
        text = rename_problem(name, pfad)
        if text:
            return failed(text, 400) or await rename_page(
                user, pfad, art, name=name, errors={"name": text}, status_code=400
            )
        folder = parent_of(pfad)  # immer derselbe Elternordner
        try:
            await portal.adapters.bwsync.move(pfad, join(folder, name))
        except AdapterError as exc:
            text, status_code = problem_text(exc), page_status(exc)
            return failed(text, status_code) or await rename_page(
                user, pfad, art, name=name, errors={"name": text}, status_code=status_code
            )
        if as_json:
            return JSONResponse({"url": folder_url(folder, ok="umbenannt")}, headers={"Cache-Control": "no-store"})
        return back_to(folder, "umbenannt")

    # --- Verschieben: Zielordner Ebene für Ebene auswählen, dann „Hierher verschieben“ (geht ohne JavaScript) ----

    def move_blocker(pfad: str, art: str, ziel: str) -> str | None:
        """Warum es in diesen Ordner nicht geht, oder None."""
        if ziel == parent_of(pfad):
            return f"{KINDS[art]} liegt schon hier. Bitte wählen Sie einen anderen Ordner."
        if art == "ordner" and (ziel == pfad or ziel.startswith(pfad + "/")):
            return "Ein Ordner kann nicht in sich selbst verschoben werden."
        return None

    def move_url(pfad: str, art: str, ziel: str) -> str:
        """Link der Verschieben-Seite; ziel steht immer drin, auch leer (= oberste Ebene).
        Fehlt ziel, startet die Seite im eigenen Ordner; link() ließe den leeren Wert weg."""
        return portal.url("/verschieben") + "?" + urlencode({"pfad": pfad, "art": art, "ziel": ziel}, quote_via=quote)

    async def move_page(user: User, pfad: str, art: str, ziel: str, *, errors=None, status_code=200):
        errors = dict(errors or {})
        folders, listed = [], False
        try:
            listing = await portal.adapters.bwsync.list(ziel)
            listed = True
            for entry in listing.entries:
                sub = join(ziel, entry.name)
                if entry.type == "folder" and sub != pfad:  # den Ordner selbst nicht anbieten
                    folders.append({"name": entry.name, "url": move_url(pfad, art, sub)})
        except AdapterError as exc:
            errors.setdefault("verschieben", problem_text(exc))
            if status_code == 200:
                status_code = page_status(exc)

        def here(path: str) -> str:
            return move_url(pfad, art, path)

        return await render(
            user,
            "verschieben.html",
            f"{KINDS[art]} verschieben – SyncShare",
            status_code,
            name=name_of(pfad),
            where=parent_of(pfad) or ROOT_NAME,
            pfad=pfad,
            art=art,
            kind=KINDS[art],
            ziel=ziel,
            ziel_name=name_of(ziel) if ziel else ROOT_NAME,
            crumbs=crumbs(ziel, here),
            folders=folders,
            listed=listed,
            blocker=move_blocker(pfad, art, ziel) if listed else None,
            root_url=here(""),
            action=link("/verschieben"),
            back_url=folder_url(parent_of(pfad)),
            errors=errors,
        )

    @router.get("/verschieben", response_class=HTMLResponse)
    async def move_form(
        user: User = Depends(portal.user),
        pfad: Annotated[str, Query()] = "",
        art: Annotated[str, Query()] = "",
        ziel: Annotated[str | None, Query()] = None,
    ) -> HTMLResponse:
        if not target_ok(pfad, art):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        ziel = parent_of(pfad) if ziel is None else ziel
        if not valid_path(ziel):
            return await move_page(user, pfad, art, "", errors={"verschieben": INVALID_PATH}, status_code=400)
        return await move_page(user, pfad, art, ziel)

    @router.post("/verschieben", response_class=HTMLResponse)
    async def move(
        user: User = Depends(portal.user),
        pfad: Annotated[str, Form()] = "",
        art: Annotated[str, Form()] = "",
        ziel: Annotated[str, Form()] = "",
    ) -> Response:
        if not target_ok(pfad, art):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        if not valid_path(ziel):
            return await move_page(user, pfad, art, "", errors={"verschieben": INVALID_PATH}, status_code=400)
        blocker = move_blocker(pfad, art, ziel)
        if blocker:
            return await move_page(user, pfad, art, ziel, errors={"verschieben": blocker}, status_code=400)
        try:
            await portal.adapters.bwsync.move(pfad, join(ziel, name_of(pfad)))  # Name bleibt, nie überschreiben
        except AdapterError as exc:
            text = problem_text(exc)
            if exc.type == bwsync_errors.EXISTS:
                text = f"In „{name_of(ziel) or ROOT_NAME}“ gibt es schon etwas mit dem Namen „{name_of(pfad)}“."
            return await move_page(user, pfad, art, ziel, errors={"verschieben": text}, status_code=page_status(exc))
        return back_to(ziel, "verschoben")

    return router
