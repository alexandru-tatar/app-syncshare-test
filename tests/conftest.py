"""Testumgebung: Betriebsmodus mit festem Testschlüssel statt echtem Portal."""

import os
import time
from types import SimpleNamespace

os.environ.pop("PORTAL_DEV", None)
os.environ["PORTAL_ENV"] = "test"
os.environ["PORTAL_ISSUER"] = "https://portal.test/"

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from app import views
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


@pytest.fixture(autouse=True)
def adapter_backend(monkeypatch):
    backend = SimpleNamespace(files={}, calls=[], status=None)
    views.STORE.clear()

    def remote(request):
        backend.calls.append(request)
        assert request.headers["authorization"].startswith("Bearer ")
        assert request.url.path.startswith("/bwsync/files/adapter-test/")
        if backend.status:
            return httpx.Response(backend.status, json={"detail": "Testfehler"})
        path = request.url.path
        if request.method == "PUT":
            if path in backend.files:
                return httpx.Response(412, json={"detail": "Datei existiert"})
            backend.files[path] = request.content
            return httpx.Response(204)
        if path not in backend.files:
            return httpx.Response(404, json={"detail": "Datei fehlt"})
        return httpx.Response(200, content=backend.files[path])

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
            HTTPError=httpx.HTTPError,
        ),
    )
    monkeypatch.setattr(layout_module, "httpx", SimpleNamespace(AsyncClient=nav_factory, HTTPError=httpx.HTTPError))
    yield backend
    views.STORE.clear()
