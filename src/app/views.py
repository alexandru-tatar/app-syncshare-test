import secrets
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, File, UploadFile
from fastapi.responses import HTMLResponse, Response
from fastapi.templating import Jinja2Templates

from platform_sdk import AdapterError, Portal, User

templates = Jinja2Templates(directory=Path(__file__).parent / "templates")  # autoescape ist an

MAX_BYTES = 2 * 1024 * 1024  # 2 MB je Datei
MAX_FILES = 20  # je Person


@dataclass(frozen=True)
class StoredFile:
    name: str
    size: int
    remote_path: str


# Nur die Dateiliste liegt im RAM; Dateiinhalte liegen in bwSync.
# Jede Person sieht ihre eigenen Einträge (Schlüssel: User.sub).
STORE: dict[str, dict[str, StoredFile]] = {}


def clean_name(raw: str | None) -> str:
    """Nur der Dateiname, ohne Pfad und Steuerzeichen."""
    name = Path((raw or "").replace("\\", "/")).name
    name = "".join(c for c in name if c.isprintable()).strip()
    return name[:100]


def build_router(portal: Portal) -> APIRouter:
    router = portal.router()

    async def show(user: User, *, errors: dict[str, str] | None = None, status_code: int = 200) -> HTMLResponse:
        files = [
            {"name": f.name, "size_kb": max(1, round(f.size / 1024)), "url": portal.url(f"/datei/{file_id}")}
            for file_id, f in STORE.get(user.sub, {}).items()
        ]
        body = templates.get_template("page.html").render(
            files=files, errors=errors or {}, upload_url=portal.url("/upload"), max_mb=MAX_BYTES // (1024 * 1024)
        )
        html = await portal.layout(title="SyncShare", body=body, current=portal.url("/"), user=user)
        return HTMLResponse(html, status_code=status_code)

    @router.get("/", response_class=HTMLResponse)
    async def home(user: User = Depends(portal.user)) -> HTMLResponse:
        return await show(user)

    @router.post("/upload", response_class=HTMLResponse)
    async def upload(
        user: User = Depends(portal.user), datei: Annotated[UploadFile | None, File()] = None
    ) -> HTMLResponse:
        mine = STORE.setdefault(user.sub, {})
        name = clean_name(datei.filename) if datei else ""
        if not name:
            return await show(user, errors={"datei": "Bitte wählen Sie eine Datei aus."}, status_code=400)
        data = await datei.read(MAX_BYTES + 1)
        if len(data) > MAX_BYTES:
            msg = f"Die Datei ist größer als {MAX_BYTES // (1024 * 1024)} MB. Bitte wählen Sie eine kleinere Datei."
            return await show(user, errors={"datei": msg}, status_code=400)
        if len(mine) >= MAX_FILES:
            msg = f"Sie haben schon {MAX_FILES} Dateien hochgeladen. Mehr sind nicht möglich."
            return await show(user, errors={"datei": msg}, status_code=400)
        file_id = secrets.token_hex(16)
        remote_path = f"adapter-test/{file_id}"
        try:
            await portal.adapters.bwsync.upload(remote_path, data)
        except AdapterError as exc:
            message = "Die Datei konnte nicht hochgeladen werden. Bitte versuchen Sie es erneut."
            if exc.status == 412:
                message = "Am Ziel existiert bereits eine Datei. Bitte versuchen Sie den Upload erneut."
            return await show(user, errors={"datei": message}, status_code=exc.status)
        mine[file_id] = StoredFile(name=name, size=len(data), remote_path=remote_path)
        return await show(user)

    @router.get("/datei/{file_id}")
    async def download(file_id: str, user: User = Depends(portal.user)) -> Response:
        stored = STORE.get(user.sub, {}).get(file_id)
        if stored is None:
            return await show(user, errors={"datei": "Diese Datei gibt es nicht (mehr)."}, status_code=404)
        try:
            data = await portal.adapters.bwsync.download(stored.remote_path, max_bytes=MAX_BYTES)
        except AdapterError as exc:
            return await show(
                user, errors={"datei": "Die Datei konnte nicht heruntergeladen werden."}, status_code=exc.status
            )
        # Nie im Browser ausführen oder anzeigen: immer als Download, ohne Typ-Erkennung.
        return Response(
            data,
            media_type="application/octet-stream",
            headers={
                "Content-Disposition": f"attachment; filename*=UTF-8''{quote(stored.name, safe='')}",
                "X-Content-Type-Options": "nosniff",
            },
        )

    return router
