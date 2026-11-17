"""SyncShare: Datei-Explorer für das eigene bwSync&Share-Konto.

Alle Dateien und Ordner liegen in bwSync&Share; die App speichert nichts. Jeder Aufruf geht mit der
Anmeldung der Person über das Portal-SDK an den bwSync-Adapter, der nur das eigene Konto kennt.
Seiten mit GET ändern nie etwas; Anlegen, Hochladen, Umbenennen und Löschen sind Formulare (POST) und
leiten danach auf die Ordneransicht um (Post/Redirect/Get). Fremde Herkunft lehnt das Gateway bei POST ab.
"""

from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlencode
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from fastapi import APIRouter, Depends, File, Form, Query, UploadFile
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.templating import Jinja2Templates

from platform_sdk import AdapterError, Portal, User

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")  # autoescape ist an

MAX_BYTES = 2 * 1024 * 1024  # 2 MB je Datei
MAX_NAME = 255
MAX_PATH = 1024
FORBIDDEN_CHARS = "/\\:%?#"  # kann der Adapter nicht verarbeiten

try:
    LOCAL_TIME = ZoneInfo("Europe/Berlin")
except ZoneInfoNotFoundError:  # ohne Zeitzonendaten im Image: UTC anzeigen
    LOCAL_TIME = None

ROOT_NAME = "Mein bwSync&Share"
INVALID_NAME = (
    "Bitte geben Sie einen Namen an. Er darf höchstens 255 Zeichen lang sein und keines dieser Zeichen "
    "enthalten: / \\ : % ? #"
)
INVALID_PATH = "Diesen Ordner kann SyncShare nicht öffnen."
UNAVAILABLE = "bwSync&Share ist gerade nicht erreichbar. Bitte versuchen Sie es später erneut."

# Fehlertypen des Adapters (problem+json „type“), in eigenen Worten
PROBLEMS = {
    "bwsync-not-linked": "Ihr bwSync&Share-Konto ist nicht verbunden. Bitte verbinden Sie es auf der Startseite "
    "von SyncShare.",
    "bwsync-link-invalid": "Die Verbindung zu Ihrem bwSync&Share-Konto gilt nicht mehr, zum Beispiel weil das "
    "App-Passwort widerrufen wurde. Bitte verbinden Sie es neu.",
    "bwsync-forbidden": "Dafür fehlt Ihnen in bwSync&Share die Berechtigung, zum Beispiel in einem nur lesbar "
    "geteilten Ordner.",
    "bwsync-exists": "Hier gibt es schon einen Ordner oder eine Datei mit diesem Namen. Bitte wählen Sie einen "
    "anderen Namen.",
    "bwsync-parent-missing": "Diesen Ordner gibt es nicht (mehr). Bitte laden Sie die Seite neu.",
    "bwsync-folder-not-empty": "Der Ordner ist nicht leer. Bitte löschen Sie zuerst seinen Inhalt.",
}
LINK_PROBLEMS = {"bwsync-not-linked", "bwsync-link-invalid"}
STATUS_TEXTS = {
    400: "Das geht so nicht. Bitte prüfen Sie Ordner und Namen.",
    404: "Das gibt es nicht (mehr). Vielleicht wurde es in bwSync&Share verschoben oder gelöscht.",
    412: PROBLEMS["bwsync-exists"],
    413: "Die Datei ist zu groß.",
    423: "Die Datei ist gerade gesperrt, zum Beispiel weil sie geöffnet ist. Bitte versuchen Sie es später erneut.",
}
# Erfolgsmeldungen nach der Umleitung: nur feste Schlüssel, kein Text aus der Adresse
DONE = {
    "ordner": "Der Ordner wurde angelegt.",
    "hochgeladen": "Die Datei wurde hochgeladen.",
    "umbenannt": "Umbenannt beziehungsweise verschoben.",
    "geloescht": "Gelöscht. Es liegt jetzt im Papierkorb von bwSync&Share.",
}
KINDS = {"datei": "Datei", "ordner": "Ordner"}


def clean_name(raw: str | None) -> str:
    """Nur der Dateiname, ohne Pfad und Steuerzeichen."""
    name = Path((raw or "").replace("\\", "/")).name
    return "".join(c for c in name if c.isprintable()).strip()


def valid_name(name: str) -> bool:
    return (
        0 < len(name) <= MAX_NAME
        and name not in (".", "..")
        and name == name.strip()
        and not any(c in FORBIDDEN_CHARS for c in name)
        and name.isprintable()
    )


def valid_path(path: str, *, root: bool = True) -> bool:
    """Relativer Pfad im eigenen Konto; "" ist die oberste Ebene (nur wo erlaubt)."""
    if path == "":
        return root
    return len(path) <= MAX_PATH and all(valid_name(part) for part in path.split("/"))


def join(folder: str, name: str) -> str:
    return f"{folder}/{name}" if folder else name


def parent_of(path: str) -> str:
    return path.rsplit("/", 1)[0] if "/" in path else ""


def name_of(path: str) -> str:
    return path.rsplit("/", 1)[-1]


def size_text(size: int | None) -> str:
    if size is None:
        return "–"
    if size < 1024:
        return f"{size} B"
    for unit in ("KB", "MB", "GB", "TB"):
        size /= 1024
        if size < 1024 or unit == "TB":
            return f"{size:.1f} {unit}".replace(".", ",")
    return "–"  # pragma: no cover


def date_text(value: datetime | None) -> str:
    if value is None:
        return "–"
    if value.tzinfo is None:
        value = value.replace(tzinfo=UTC)
    if LOCAL_TIME is None:
        return value.astimezone(UTC).strftime("%d.%m.%Y, %H:%M UTC")
    return value.astimezone(LOCAL_TIME).strftime("%d.%m.%Y, %H:%M")


def problem_text(exc: AdapterError) -> str:
    return PROBLEMS.get(exc.type) or STATUS_TEXTS.get(exc.status) or UNAVAILABLE


def page_status(exc: AdapterError) -> int:
    return exc.status if 400 <= exc.status < 600 else 502


def build_router(portal: Portal) -> APIRouter:
    router = portal.router()
    bwsync = portal.adapters.bwsync

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

    def crumbs(path: str) -> list[dict[str, str]]:
        items = [{"name": ROOT_NAME, "url": folder_url("")}]
        so_far = ""
        for part in path.split("/") if path else []:
            so_far = join(so_far, part)
            items.append({"name": part, "url": folder_url(so_far)})
        return items

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
                folder = await bwsync.list(path)
            except AdapterError as exc:
                if exc.type in LINK_PROBLEMS:
                    link_problem = exc.type
                else:
                    errors.setdefault("liste", problem_text(exc))
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
                    "size": "–" if is_folder else size_text(entry.size),
                    "modified": date_text(entry.modified),
                    "open_url": folder_url(entry_path) if is_folder else None,
                    "download_url": None if is_folder else link("/datei", pfad=entry_path),
                    "rename_url": link("/umbenennen", pfad=entry_path, art=kind),
                    "delete_url": link("/loeschen", pfad=entry_path, art=kind),
                }
            )
        return await render(
            user,
            "page.html",
            "SyncShare",
            status_code,
            pfad=path if folder else "",
            folder=folder,
            rows=rows,
            crumbs=crumbs(path) if folder else [],
            title=name_of(path) if path else ROOT_NAME,
            root_url=folder_url(""),
            link_problem=link_problem,
            link_invalid=PROBLEMS["bwsync-link-invalid"],
            errors=errors,
            done=DONE.get(ok, "") if not errors else "",
            values=values or {},
            mkdir_url=link("/ordner"),
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
            await bwsync.mkdir(join(pfad, name))
        except AdapterError as exc:
            if exc.type in LINK_PROBLEMS:
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
            await bwsync.upload(join(pfad, name), data)
        except AdapterError as exc:
            return await explorer(user, pfad, errors={"datei": problem_text(exc)}, status_code=page_status(exc))
        return back_to(pfad, "hochgeladen")

    @router.get("/datei")
    async def download(user: User = Depends(portal.user), pfad: Annotated[str, Query()] = "") -> Response:
        if not valid_path(pfad, root=False):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        try:
            data = await bwsync.download(pfad, max_bytes=MAX_BYTES)
        except AdapterError as exc:
            msg = "Die Datei konnte nicht heruntergeladen werden. " + problem_text(exc)
            if exc.status == 413:
                msg = f"Die Datei ist größer als {MAX_BYTES // (1024 * 1024)} MB; SyncShare kann sie nicht abholen."
            return await explorer(user, parent_of(pfad), errors={"liste": msg}, status_code=page_status(exc))
        # Nie im Browser ausführen oder anzeigen: immer als Download, ohne Typ-Erkennung.
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(name_of(pfad), safe='')}",
                "X-Content-Type-Options": "nosniff",
                "Cache-Control": "no-store",
            },
        )

    def target_ok(pfad: str, art: str) -> bool:
        return valid_path(pfad, root=False) and art in KINDS

    # --- Löschen: GET zeigt nur die Rückfrage, erst POST löscht -----------------------------------------------

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
            await bwsync.delete(pfad, folder=art == "ordner")
        except AdapterError as exc:
            return await delete_page(
                user, pfad, art, errors={"loeschen": problem_text(exc)}, status_code=page_status(exc)
            )
        return back_to(parent_of(pfad), "geloescht")

    # --- Umbenennen und verschieben -------------------------------------------------------------------------

    async def rename_page(user: User, pfad: str, art: str, *, name=None, ziel=None, errors=None, status_code=200):
        return await render(
            user,
            "umbenennen.html",
            f"{KINDS[art]} umbenennen – SyncShare",
            status_code,
            old_name=name_of(pfad),
            name=name_of(pfad) if name is None else name,
            ziel=parent_of(pfad) if ziel is None else ziel,
            pfad=pfad,
            art=art,
            kind=KINDS[art],
            action=link("/umbenennen"),
            back_url=folder_url(parent_of(pfad)),
            errors=errors or {},
        )

    @router.get("/umbenennen", response_class=HTMLResponse)
    async def rename_form(
        user: User = Depends(portal.user), pfad: Annotated[str, Query()] = "", art: Annotated[str, Query()] = ""
    ) -> HTMLResponse:
        if not target_ok(pfad, art):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        return await rename_page(user, pfad, art)

    @router.post("/umbenennen", response_class=HTMLResponse)
    async def rename(
        user: User = Depends(portal.user),
        pfad: Annotated[str, Form()] = "",
        art: Annotated[str, Form()] = "",
        name: Annotated[str, Form()] = "",
        ziel: Annotated[str, Form()] = "",
    ) -> Response:
        if not target_ok(pfad, art):
            return await explorer(user, "", errors={"liste": INVALID_PATH}, status_code=400)
        name, ziel = name.strip(), ziel.strip().strip("/")
        errors = {}
        if not valid_name(name):
            errors["name"] = INVALID_NAME
        if not valid_path(ziel):
            errors["ziel"] = (
                "Bitte geben Sie den Ordner als Pfad an, zum Beispiel Studium/Semester 5, oder lassen Sie "
                "das Feld für die oberste Ebene leer."
            )
        target = join(ziel, name)
        if not errors and target == pfad:
            errors["name"] = "Bitte geben Sie einen neuen Namen oder einen anderen Ordner an."
        if not errors and art == "ordner" and target.startswith(pfad + "/"):
            errors["ziel"] = "Ein Ordner kann nicht in sich selbst verschoben werden."
        if errors:
            return await rename_page(user, pfad, art, name=name, ziel=ziel, errors=errors, status_code=400)
        try:
            await bwsync.move(pfad, target)
        except AdapterError as exc:
            field = "ziel" if exc.type == "bwsync-parent-missing" else "name"
            text = problem_text(exc)
            if exc.type == "bwsync-parent-missing":
                text = "Den Zielordner gibt es nicht. Bitte prüfen Sie den Pfad."
            return await rename_page(
                user, pfad, art, name=name, ziel=ziel, errors={field: text}, status_code=page_status(exc)
            )
        return back_to(ziel, "umbenannt")

    return router
