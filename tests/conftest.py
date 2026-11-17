"""Testumgebung: Betriebsmodus mit festem Testschlüssel statt echtem Portal."""

import io
import json
import os
import time
import zipfile
from types import SimpleNamespace

os.environ.pop("PORTAL_DEV", None)
os.environ["PORTAL_ENV"] = "test"
os.environ["PORTAL_ISSUER"] = "https://portal.test/"

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app.main import app, portal
from platform_sdk import layout as layout_module
from platform_sdk.adapters import transport as transport_module
from platform_sdk.jwks import JwksCache

PRIVATE_KEY = rsa.generate_private_key(public_exponent=65537, key_size=2048)
portal.jwks = JwksCache.static({"test-key": PRIVATE_KEY.public_key()})


def make_token(**overrides) -> str:
    now = int(time.time())
    claims = {
        "iss": "https://portal.test/",
        "aud": [portal.config.slug, "portal", "adapters"],
        "sub": "test-person",
        "name": "Erika Musterfrau",
        "roles": ["student"],
        "v": 1,
        "iat": now,
        "exp": now + 60,
    }
    claims.update(overrides)
    return jwt.encode(claims, PRIVATE_KEY, algorithm="RS256", headers={"kid": "test-key"})


@pytest.fixture
def client() -> TestClient:
    return TestClient(app)


@pytest.fixture
def as_student():
    return {"X-Portal-Assertion": make_token(), "Accept": "text/html"}


MODIFIED = "2026-11-16T13:28:22+00:00"


class FakeBwSync:
    """bwSync-Adapter im Speicher: Ordner, Dateien und die Fehlertypen des echten Adapters."""

    def __init__(self):
        self.folders = {""}
        self.files: dict[str, bytes] = {}
        self.calls: list[httpx.Request] = []
        self.fail: tuple[int, str | None] | None = None  # (Status, type) für jeden Aufruf
        self.listing: dict | None = None  # feste Antwort für GET /bwsync/folders/

    @staticmethod
    def problem(status, kind=None):
        body = {"detail": "Adaptertext"}
        if kind:
            body["type"] = f"urn:hka:problem:{kind}"
        return httpx.Response(status, json=body, headers={"Content-Type": "application/problem+json"})

    def exists(self, path):
        return path in self.folders or path in self.files

    @staticmethod
    def parent(path):
        return path.rsplit("/", 1)[0] if "/" in path else ""

    def children(self, path):
        return [p for p in self.folders | set(self.files) if p and self.parent(p) == path]

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.calls.append(request)
        assert request.headers["authorization"].startswith("Bearer ")
        if self.fail:
            return self.problem(*self.fail)
        path, method = request.url.path, request.method
        if path.startswith("/bwsync/folders/"):
            rel = path.removeprefix("/bwsync/folders/")
            if method == "GET":
                if self.listing is not None:
                    return httpx.Response(200, json=self.listing)
                if rel in self.files:
                    return self.problem(400)
                if rel not in self.folders:
                    return self.problem(404)
                names = sorted(self.children(rel), key=lambda p: (p not in self.folders, p.lower()))
                entries = [
                    {
                        "name": p.rsplit("/", 1)[-1],
                        "type": "folder" if p in self.folders else "file",
                        "size": None if p in self.folders else len(self.files[p]),
                        "modified": MODIFIED,
                    }
                    for p in names
                ]
                return httpx.Response(200, json={"path": rel, "entries": entries, "truncated": False, "skipped": 0})
            if method == "POST":
                if self.exists(rel):
                    return self.problem(409, "bwsync-exists")
                if self.parent(rel) not in self.folders:
                    return self.problem(409, "bwsync-parent-missing")
                self.folders.add(rel)
                return httpx.Response(201)
            if method == "DELETE":
                if rel not in self.folders:
                    return self.problem(404)
                if self.children(rel):
                    return self.problem(409, "bwsync-folder-not-empty")
                self.folders.discard(rel)
                return httpx.Response(204)
        if path == "/bwsync/move" and method == "POST":
            body = json.loads(request.content)
            source, target = body["from"], body["to"]
            if not self.exists(source):
                return self.problem(404)
            if self.exists(target):
                return self.problem(409, "bwsync-exists")
            if self.parent(target) not in self.folders:
                return self.problem(409, "bwsync-parent-missing")
            if source in self.files:
                self.files[target] = self.files.pop(source)
            else:
                for p in sorted(self.folders):
                    if p == source or p.startswith(source + "/"):
                        self.folders.discard(p)
                        self.folders.add(target + p[len(source) :])
                for p in list(self.files):
                    if p.startswith(source + "/"):
                        self.files[target + p[len(source) :]] = self.files.pop(p)
            return httpx.Response(204)
        if path.startswith("/bwsync/zip/") and method == "GET":
            rel = path.removeprefix("/bwsync/zip/")
            if rel not in self.folders or rel == "":
                return self.problem(404)
            buffer = io.BytesIO()
            with zipfile.ZipFile(buffer, "w") as archive:
                for name, data in sorted(self.files.items()):
                    if name.startswith(rel + "/"):
                        archive.writestr(name, data)
            return httpx.Response(200, content=buffer.getvalue(), headers={"Content-Type": "application/zip"})
        if path.startswith("/bwsync/files/"):
            rel = path.removeprefix("/bwsync/files/")
            if method == "PUT":
                if self.exists(rel):
                    return self.problem(412)
                if self.parent(rel) not in self.folders:
                    return self.problem(409)
                self.files[rel] = request.content
                return httpx.Response(204)
            if method == "GET":
                if rel not in self.files:
                    return self.problem(404)
                return httpx.Response(200, content=self.files[rel])
            if method == "DELETE":
                if rel in self.folders:
                    return self.problem(409)
                if rel not in self.files:
                    return self.problem(404)
                del self.files[rel]
                return httpx.Response(204)
        raise AssertionError(f"unerwarteter Aufruf {method} {path}")


@pytest.fixture(autouse=True)
def adapter_backend(monkeypatch):
    backend = FakeBwSync()
    remote = backend

    def factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(remote), **kwargs)

    def nav_factory(**kwargs):
        return httpx.AsyncClient(transport=httpx.MockTransport(lambda request: httpx.Response(503)), **kwargs)

    monkeypatch.setattr(
        transport_module,
        "httpx",
        SimpleNamespace(
            AsyncClient=factory,
            TimeoutException=httpx.TimeoutException,
            Timeout=httpx.Timeout,
            HTTPError=httpx.HTTPError,
        ),
    )
    monkeypatch.setattr(layout_module, "httpx", SimpleNamespace(AsyncClient=nav_factory, HTTPError=httpx.HTTPError))
    yield backend
